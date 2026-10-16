#!/usr/bin/env python3
"""Claude Code の PreToolUse フック：顧客データ（案件フォルダ）へのアクセスを止める。

- 案件フォルダ（既定 ~/fukugyo-c-jobs）を指すパス、その名前を含む入力、
  案件フォルダを丸ごと含む範囲への再帰検索を拒否する。
- Write / Edit などは「書き込み先のパス」だけを調べる。
  ドキュメントやコードにフォルダ名を書くこと自体は止めない。
- 入力を読めないなど判断できないときも拒否する（fail-closed）。

macOS 標準の /usr/bin/python3（3.9）でも動くよう、標準ライブラリだけで書く。
"""

import fnmatch
import json
import os
import re
import shlex
import sys

TOKEN = "fukugyo-c-jobs"
# 案件フォルダを追加するとき（例：暗号化ディスクイメージのマウント先）に ":" 区切りで指定する
EXTRA_ROOTS_ENV = "SC_EXTRA_JOB_ROOTS"

# 書き込み系：書き込み先のパスだけを調べる
PATH_ONLY_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
PATH_KEYS = ("file_path", "notebook_path", "path")
# 文章だけを扱い、ファイルに触れないツール
TEXT_ONLY_TOOLS = {
    "Agent",
    "Task",
    "TodoWrite",
    "TaskCreate",
    "TaskUpdate",
    "SendMessage",
    "AskUserQuestion",
    "ExitPlanMode",
}
# 案件フォルダの親を指定されると中まで読みに行くツールやコマンド
RECURSIVE_TOOLS = {"Grep", "Glob"}
RECURSIVE_COMMANDS = {
    "find", "fd", "rg", "ag", "ack", "tree", "du", "tar", "zip", "rsync", "ditto", "xargs",
}
RECURSIVE_FLAG_COMMANDS = {"grep", "egrep", "fgrep", "ls", "cp", "chmod", "chown"}
# Spotlight の索引から中身を引けるので常に拒否する
BLOCKED_COMMANDS = {"mdfind", "mdls"}

SHELL_SPLIT = re.compile(r"[;&|<>()`\n]")
GLOB_CHARS = set("*?[")


def deny(reason):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
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
    return s.startswith(("~", "/", "./", "../", "$HOME", "${HOME}")) or "/" in s


def check_path(candidate, cwd, roots, recursive):
    """候補パスが案件フォルダの中、またはそれを丸ごと含む範囲なら理由を返す。"""
    if not looks_like_path(candidate):
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
    if tool in TEXT_ONLY_TOOLS:
        sys.exit(0)

    roots = job_roots()
    hint = "顧客データは Claude Code から扱いません。架空データ（samples/fictional/ や tests/fixtures/）を使ってください。"

    if tool in PATH_ONLY_TOOLS and isinstance(tool_input, dict):
        texts = [tool_input.get(k) for k in PATH_KEYS if isinstance(tool_input.get(k), str)]
    else:
        texts = list(strings_in(tool_input))

    recursive = tool in RECURSIVE_TOOLS
    for text in texts:
        if TOKEN in text.lower():
            deny("案件フォルダ（" + TOKEN + "）を指す入力は拒否します。" + hint)
        for root in roots:
            if root in text.lower():
                deny("案件フォルダのパスを含む入力は拒否します。" + hint)
        if tool in ("Bash", "Monitor", "PowerShell"):
            name = blocked_command(text)
            if name:
                deny(name + " は Spotlight の索引から顧客データを引けるので拒否します。" + hint)
            candidates = words(text)
            rec = command_is_recursive(text)
        else:
            candidates = [text]
            rec = recursive
        for cand in candidates:
            reason = check_path(cand, cwd, roots, rec)
            if reason:
                deny(reason + "。" + hint)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 想定外の失敗も止める側に倒す
        fail_closed("判定中にエラーが起きました（" + type(exc).__name__ + "）")
