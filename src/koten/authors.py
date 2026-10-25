"""著者の表示名。

青空文庫では、同じ人が別名で別の人物として載り、1つの作品に2人の著者として並ぶことがある
（例：『茶の本』の岡倉覚三と岡倉天心）。出典で「岡倉覚三・岡倉天心」と言うと2人に聞こえるので、
取得のときに同じ人の名前を「主な名前（別名）」の1つにまとめた author_display を作っておく。
"""

from __future__ import annotations

from typing import Any


def author_label(row: dict[str, Any]) -> str:
    """出典・画面・説明欄で使う著者名（author_display が無い古い候補表なら名前を並べる）。"""
    return "・".join(row.get("author_display") or row.get("author_names") or [])
