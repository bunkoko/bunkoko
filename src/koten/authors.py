"""著者・訳者の表示名。

青空文庫では、同じ人が別名で別の人物として載り、1つの作品に2人の著者として並ぶことがある
（例：『茶の本』の岡倉覚三と岡倉天心）。出典で「岡倉覚三・岡倉天心」と言うと2人に聞こえるので、
取得のときに同じ人の名前を「主な名前（別名）」の1つにまとめた author_display を作っておく。
"""

from __future__ import annotations

from typing import Any


def author_label(row: dict[str, Any], *, aliases: bool = False) -> str:
    """著者名。語り・タイトル・出典では主な名前だけにし（例：岡倉天心）、
    aliases=True（確認用の表示・説明欄）では別名も括弧で添える（例：岡倉天心（岡倉覚三））。

    author_display が無い古い候補表なら、載っている名前を並べる。
    """
    names = row.get("author_display") or row.get("author_names") or []
    if not aliases:
        names = [n.split("（", 1)[0] for n in names]
    return "・".join(names)


def translator_label(row: dict[str, Any]) -> str:
    """訳者名（訳書の一節は訳者の文章なので、出典に添える）。"""
    return "・".join(row.get("translator_names") or [])
