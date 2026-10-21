#!/usr/bin/env python3
"""Claude Code の PreToolUse フック：顧客データ（案件フォルダ）へのアクセスを止める。

調べる範囲はツールの種類で変える。文章の中にフォルダ名が出てくるだけでは止めない。

- コマンド系（Bash・Monitor・PowerShell）：入力の全文。フォルダ名・パス・ワイルドカード・
  シンボリックリンク・ホームからの再帰検索・Spotlight を止める。
  他のツールでも、command・script など「コマンドを表す項目」は同じ扱いにする。
- ファイル系（Read・Write・Edit・Grep・Glob など）：パスの項目だけ。
- それ以外（MCP、StructuredOutput など）：パスを表す項目（path・file・dir・uri など）と、
  値が1つのパスそのものの場合だけ。
- 入力を読めないなど判断できないときは拒否する（fail-closed）。

macOS 標準の /usr/bin/python3（3.9）でも動くよう、標準ライブラリだけで書く。
"""

import fnmatch
import json
import os
import re
import shlex
import sys

TOKEN = "fukugyo-c-jobs"
# 案件フォルダを追加するときに ":" 区切りで指定する（通常は不要。暗号化イメージも案件フォルダ内にマウントする）
EXTRA_ROOTS_ENV = "SC_EXTRA_JOB_ROOTS"

COMMAND_TOOLS = {"Bash", "Monitor", "PowerShell"}
# ファイル系：ツールごとのパスの項目
FILE_TOOLS = {
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "NotebookRead": ("notebook_path",),
    "LS": ("path",),
    "Grep": ("path",),
    "Glob": ("path", "pattern"),
}
# 案件フォルダの親を指定されると中まで読みに行くツールやコマンド
RECURSIVE_TOOLS = {"Grep", "Glob"}
RECURSIVE_COMMANDS = {
    "find", "fd", "rg", "ag", "ack", "tree", "du", "tar", "zip", "rsync", "ditto", "xargs",
}
RECURSIVE_FLAG_COMMANDS = {"grep", "egrep", "fgrep", "ls", "cp", "chmod", "chown"}
# Spotlight の索引から中身を引けるので常に拒否する
BLOCKED_COMMANDS = {"mdfind", "mdls"}

# その他のツールで「パスを表す項目」とみなす名前（キーの最後の単語）
PATH_KEY_WORDS = {
    "path", "paths", "filepath", "file", "files", "filename", "filenames",
    "dir", "dirs", "directory", "directories", "folder", "folders",
    "cwd", "root", "roots", "uri", "uris",
}
# その他のツールで「コマンドを表す項目」とみなす名前（Bash と同じく全文を調べる）
COMMAND_KEY_WORDS = {"command", "commands", "cmd", "script", "shell", "args", "argv", "code"}
PATH_PREFIXES = ("/", "~", "./", "../", "$HOME", "${HOME}", "file://")
MAX_PATH_LEN = 4096

SHELL_SPLIT = re.compile(r"[;&|<>()`\n]")
KEY_WORD_SPLIT = re.compile(r"[^a-z0-9]+")
CAMEL = re.compile(r"([a-z0-9])([A-Z])")
GLOB_CHARS = set("*?[")
HINT = "顧客データは Claude Code から扱いません。架空データ（samples/fictional/ や tests/fixtures/）を使ってください。"


def deny(reason):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason + "。" + HINT,
                }
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)


def fail_closed(message):
    sys.stderr.write("guard_customer_data: " + message + "\n")
    sys.exit(2)


def norm(path):
    return os.path.normcase(path).lower().rstrip("/") or "/"


def job_roots():
    raw = [os.path.join(os.path.expanduser("~"), TOKEN)]
    raw += [p for p in os.environ.get(EXTRA_ROOTS_ENV, "").split(":") if p.strip()]
    roots = set()
    for r in raw:
        r = os.path.expanduser(r.strip())
        roots.add(norm(os.path.abspath(r)))
        roots.add(norm(os.path.realpath(r)))
    return sorted(roots)


def key_word(key):
    """項目名の最後の単語（file_path → path、notebookPath → path、profile → profile）。"""
    parts = [p for p in KEY_WORD_SPLIT.split(CAMEL.sub(r"\1_\2", str(key)).lower()) if p]
    return parts[-1] if parts else ""


def strings_in(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            for s in strings_in(v):
                yield s
    elif isinstance(value, list):
        for v in value:
            for s in strings_in(v):
                yield s


def classify_strings(value, kind=None):
    """その他のツールの入力を (種類, 文字列) に分ける。種類は path / command / None。"""
    if isinstance(value, str):
        yield kind, value
    elif isinstance(value, dict):
        for k, v in value.items():
            word = key_word(k)
            if word in PATH_KEY_WORDS:
                child = "path"
            elif word in COMMAND_KEY_WORDS:
                child = "command"
            else:
                child = kind
            for item in classify_strings(v, child):
                yield item
    elif isinstance(value, list):
        for v in value:
            for item in classify_strings(v, kind):
                yield item


def is_single_path(text):
    """値が1つのパスそのものか（1行で、空白を含まないか、パスの書き出しで始まる）。"""
    s = text.strip()
    if not s or len(s) > MAX_PATH_LEN or "\n" in s or "\r" in s:
        return False
    if not any(c.isspace() for c in s):
        return True
    return s.startswith(PATH_PREFIXES)


def words(text):
    """シェルのコマンド文字列を、パスになりそうな語に分ける。"""
    out = []
    for part in SHELL_SPLIT.split(text):
        try:
            tokens = shlex.split(part, posix=True)
        except ValueError:
            tokens = part.split()
        for t in tokens:
            out.append(t)
            if "=" in t:
                out.append(t.split("=", 1)[1])
    return out


def resolve(candidate, cwd):
    if candidate.startswith("file://"):
        candidate = candidate[len("file://"):]
    expanded = os.path.expandvars(os.path.expanduser(candidate))
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    return norm(os.path.abspath(expanded)), norm(os.path.realpath(expanded))


def is_under(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def glob_reaches(pattern, root):
    """ワイルドカード付きのパスが、案件フォルダそのものかその中に一致しうるか。"""
    ps = [s for s in pattern.split("/") if s]
    rs = [s for s in root.split("/") if s]
    for i, seg in enumerate(rs):
        if i >= len(ps):
            return False
        if ps[i] == "**":
            return True
        if not fnmatch.fnmatchcase(seg, ps[i]):
            return False
    return True


def looks_like_path(s):
    return s.startswith(PATH_PREFIXES) or "/" in s


def check_path(candidate, cwd, roots, recursive):
    """候補パスが案件フォルダの中、またはそれを丸ごと含む範囲なら理由を返す。"""
    if not looks_like_path(candidate) or len(candidate) > MAX_PATH_LEN:
        return None
    has_glob = any(c in GLOB_CHARS for c in candidate)
    for p in resolve(candidate, cwd):
        for root in roots:
            if is_under(p, root):
                return "案件フォルダの中を指しています"
            if has_glob and glob_reaches(p, root):
                return "ワイルドカードが案件フォルダに一致します"
            if recursive and is_under(root, p):
                return "案件フォルダを含む範囲を再帰的に読もうとしています"
    return None


def command_is_recursive(text):
    for part in SHELL_SPLIT.split(text):
        try:
            tokens = shlex.split(part, posix=True)
        except ValueError:
            tokens = part.split()
        if not tokens:
            continue
        name = os.path.basename(tokens[0])
        if name in ("sudo", "env", "command", "nice", "time") and len(tokens) > 1:
            name = os.path.basename(tokens[1])
        if name in RECURSIVE_COMMANDS:
            return True
        if name in RECURSIVE_FLAG_COMMANDS and any(
            t.startswith("-") and not t.startswith("--") and ("r" in t or "R" in t)
            for t in tokens[1:]
        ):
            return True
        if any(t in ("--recursive", "--dereference-recursive") for t in tokens[1:]):
            return True
    return False


def blocked_command(text):
    for part in SHELL_SPLIT.split(text):
        tokens = part.split()
        if tokens and os.path.basename(tokens[0]) in BLOCKED_COMMANDS:
            return os.path.basename(tokens[0])
    return None


def check_name(text, roots):
    """フォルダ名や案件フォルダのパスが文字列に含まれていれば拒否する。"""
    low = text.lower()
    if TOKEN in low:
        deny("案件フォルダ（" + TOKEN + "）を指す入力は拒否します")
    for root in roots:
        if root in low:
            deny("案件フォルダのパスを含む入力は拒否します")


def check_command(text, cwd, roots):
    """コマンド系：全文を調べる。"""
    check_name(text, roots)
    name = blocked_command(text)
    if name:
        deny(name + " は Spotlight の索引から顧客データを引けるので拒否します")
    rec = command_is_recursive(text)
    for cand in words(text):
        reason = check_path(cand, cwd, roots, rec)
        if reason:
            deny(reason)


def check_path_field(text, cwd, roots, recursive):
    """パスの項目：フォルダ名を含むか、案件フォルダに届くかを調べる。"""
    check_name(text, roots)
    reason = check_path(text.strip(), cwd, roots, recursive)
    if reason:
        deny(reason)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001 - 判断できない入力は止める
        fail_closed("入力を JSON として読めませんでした（" + type(exc).__name__ + "）")
    if not isinstance(payload, dict):
        fail_closed("入力の形式が想定と違います")

    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    roots = job_roots()

    if tool in COMMAND_TOOLS:
        for text in strings_in(tool_input):
            check_command(text, cwd, roots)
        sys.exit(0)

    if tool in FILE_TOOLS and isinstance(tool_input, dict):
        recursive = tool in RECURSIVE_TOOLS
        path = tool_input.get("path")
        has_path = isinstance(path, str) and path.strip()
        if recursive and not has_path:
            # 検索範囲の指定がなければ作業フォルダから再帰的に読む
            reason = check_path(cwd, cwd, roots, True)
            if reason:
                deny(reason)
        for key in FILE_TOOLS[tool]:
            value = tool_input.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            if tool == "Glob" and key == "pattern":
                base = os.path.expanduser(path) if has_path else cwd
                value = os.path.join(base, os.path.expanduser(value))
            check_path_field(value, cwd, roots, recursive)
        sys.exit(0)

    # その他（MCP、StructuredOutput など）
    for kind, text in classify_strings(tool_input):
        if kind == "command":
            check_command(text, cwd, roots)
        elif kind == "path":
            check_path_field(text, cwd, roots, False)
        elif is_single_path(text):
            s = text.strip()
            if not any(c.isspace() for c in s):
                check_name(s, roots)
            reason = check_path(s, cwd, roots, False)
            if reason:
                deny(reason)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 想定外の失敗も止める側に倒す
        fail_closed("判定中にエラーが起きました（" + type(exc).__name__ + "）")
