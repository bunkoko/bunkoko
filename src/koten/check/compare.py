"""台本の文（主張）と、今の本の該当箇所を、手元の AI で比べる（SPEC R8）。

- 本の該当箇所は、照合のときに人がその場で入力する。この関数の外へは出さない
  （ファイル・DB・ログ・画面に書かない。AI に理由の文を書かせない）
- 使う AI は手元の Ollama（127.0.0.1）だけ。クラウドの AI には渡さない
- 言い回しの近さは、コードで数える（字数はふるい分けの目安で、法律の線ではない）
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from typing import IO, Any
from urllib.parse import urlparse

import httpx

from koten.llm import ChatLLM, LLMError, fill, parse_json_object
from koten.textnorm import normalize_for_match

VERDICTS = {
    "same": "一致（内容が同じ）",
    "differs": "食い違い（内容が違う）",
    "not_covered": "触れていない",
}
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
BOOK_MIN, BOOK_MAX = 10, 1500  # 本の該当箇所は、主張にかかわる所だけ
# 言い回しが近い（ふるい分けの目安）：同じ並びが12字以上続く、
# または主張の4字の並びの35%以上が本にある
CLOSE_RUN = 12
CLOSE_SHARE = 0.35
GRAM = 4

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": list(VERDICTS)}},
    "required": ["verdict"],
    "additionalProperties": False,
}


class CheckError(RuntimeError):
    pass


def require_local(conf: dict[str, Any]) -> None:
    """照合の AI が手元の Ollama か確かめる（本の文をクラウドに送らない）。"""
    host = urlparse(str(conf.get("endpoint") or "")).hostname or ""
    if conf.get("provider") != "ollama" or host not in LOCAL_HOSTS:
        raise CheckError(
            "照合に使えるのは手元の Ollama（127.0.0.1）だけです。settings.toml の [llm.check] を"
            ' provider = "ollama"・endpoint = "http://127.0.0.1:11434" にしてください'
        )


# Ollama は 127.0.0.1 を通しても、次のモデルの要求を ollama.com へ転送する（v0.40 のソースで確認）：
# 名前の末尾が :cloud・-cloud のモデルと、マニフェストに remote_host を持つモデル
CLOUD_SUFFIXES = (":cloud", "-cloud")
# 送った文をディスクに書く設定（OLLAMA_DEBUG_LOG_REQUESTS は要求の本文を一時フォルダに残す。
# OLLAMA_DEBUG=2 はモデルの出力を記録に書く）
DEBUG_ENV = ("OLLAMA_DEBUG_LOG_REQUESTS", "OLLAMA_DEBUG")


def _launchctl_getenv(name: str) -> str:
    """Mac のアプリから起動した Ollama が受け取る環境変数（launchctl setenv の値）。"""
    if sys.platform != "darwin":
        return ""
    try:
        r = subprocess.run(["launchctl", "getenv", name], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip()


def debug_env_problems(getenv: Callable[[str], str] | None = None) -> list[str]:
    """Ollama に、送った文をディスクに書かせる設定が入っていないか。"""
    sources = [("このシェル", lambda n: os.environ.get(n, ""))]
    sources.append(("launchctl", getenv or _launchctl_getenv))
    out = []
    for where, get in sources:
        for name in DEBUG_ENV:
            v = (get(name) or "").strip().lower()
            if not v or v in ("0", "false", "no", "off"):
                continue
            if name == "OLLAMA_DEBUG" and v in ("1", "true", "debug", "info"):
                continue  # DEBUG は文の長さだけを書く（TRACE＝2 で中身を書く）
            out.append(f"{where}で {name}={v} が設定されている（送った文が記録に残る）")
    return out


def model_problems(conf: dict[str, Any], client: httpx.Client | None = None) -> list[str]:
    """照合のモデルが、手元で動き、クラウドへ転送されないか（/api/show で確かめる）。"""
    model = str(conf.get("model") or "")
    if not model:
        return ["[llm.check] の model が空"]
    if model.endswith(CLOUD_SUFFIXES):
        return [f"モデル {model} はクラウドで動く（名前が :cloud・-cloud で終わる）"]
    endpoint = str(conf["endpoint"]).rstrip("/").removesuffix("/v1")
    try:
        r = (client or httpx.Client(timeout=10)).post(f"{endpoint}/api/show", json={"model": model})
    except httpx.HTTPError:
        return [f"Ollama（{endpoint}）に接続できない。Ollama を起動する"]
    if r.status_code != 200:
        return [f"モデル {model} が Ollama に無い（ollama pull {model}）"]
    info = r.json()
    remote = info.get("remote_host") or info.get("remote_model")
    if remote:
        return [f"モデル {model} はクラウド（{remote}）へ転送される"]
    return []


def longest_run(a: str, b: str) -> int:
    """a と b に共通する、いちばん長い文字の並びの長さ。"""
    best = 0
    prev = [0] * (len(b) + 1)
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, bj in enumerate(b, start=1):
            if ch == bj:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def wording(claim: str, book: str) -> tuple[int, float]:
    """言い回しの近さ：（共通する最長の並びの字数, 主張の4字の並びのうち本にあるものの割合）。"""
    a, b = normalize_for_match(claim), normalize_for_match(book)
    grams = {a[i : i + GRAM] for i in range(max(len(a) - GRAM + 1, 0))}
    found = {g for g in grams if g in b}
    share = len(found) / len(grams) if grams else 0.0
    return longest_run(a, b), round(share, 3)


def is_close(run: int, share: float) -> bool:
    return run >= CLOSE_RUN or share >= CLOSE_SHARE


def read_pasted(stream: IO[str] | None = None) -> str:
    """貼り付けた本の文を読む。端末なら画面に出さない（スクロールの記録に残さない）。

    pbpaste | koten check run … のように、パイプでも渡せる。
    """
    stream = stream or sys.stdin
    if not stream.isatty():
        return stream.read().strip()
    try:
        import termios
    except ImportError:  # Windows など
        return stream.read().strip()
    fd = stream.fileno()
    old = termios.tcgetattr(fd)
    new = list(old)
    new[3] &= ~termios.ECHO
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        return stream.read().strip()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def clear_clipboard() -> bool:
    """Mac のクリップボードを空にする（本の文をクリップボードに残さない）。"""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(["pbcopy"], input="", text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def ask(llm: ChatLLM, system: str, user_tpl: str, claim: str, context: str, book_text: str) -> str:
    """判定のラベルだけを返す。失敗しても本の文を含むメッセージは出さない。"""
    user = fill(user_tpl, claim=claim, context=context, book=book_text)
    for _ in range(3):
        try:
            d = parse_json_object(llm.complete(system, user, VERDICT_SCHEMA))
        except (ValueError, LLMError):
            continue
        if d.get("verdict") in VERDICTS:
            return d["verdict"]
    raise CheckError(
        "手元の AI で照合できませんでした（Ollama が動いているか、ollama ps で確かめる）"
    )
