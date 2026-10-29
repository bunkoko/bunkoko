"""台本の文（主張）と、今の本の該当箇所を、手元の AI で比べる（SPEC R8）。

- 本の該当箇所は、照合のときに人がその場で入力する。この関数の外へは出さない
  （ファイル・DB・ログ・画面に書かない。AI に理由の文を書かせない）
- 使う AI は手元の Ollama（127.0.0.1）だけ。クラウドの AI には渡さない
- 言い回しの近さは、コードで数える（字数はふるい分けの目安で、法律の線ではない）
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

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
