"""年と和暦（背景の事実で言う年をコードで決める）。

- 和暦は 1873-01-01（新暦への改暦）以降だけ付ける。それより前は旧暦との換算がぶれるので西暦だけ
- 元号が年の途中で変わった年（1912・1926・1989・2019）は、年までしか分からなければ和暦を付けない
- 日まで分かれば境目の日で決める
"""

from __future__ import annotations

from datetime import date

GREGORIAN_START = date(1873, 1, 1)

# (元号, 始まりの日)。新しい順に見る
ERAS = [
    ("令和", date(2019, 5, 1)),
    ("平成", date(1989, 1, 8)),
    ("昭和", date(1926, 12, 25)),
    ("大正", date(1912, 7, 30)),
    ("明治", date(1868, 1, 25)),
]
ERA_CHANGE_YEARS = {e[1].year for e in ERAS}


def era_year(year: int, month: int | None = None, day: int | None = None) -> str | None:
    """和暦の年（例：明治39年・昭和元年）。言えないときは None。"""
    if year < GREGORIAN_START.year:
        return None
    if month is None or day is None:
        if year in ERA_CHANGE_YEARS:
            return None  # 年の途中で元号が変わり、年だけでは決まらない
        when = date(year, 12, 31)
    else:
        when = date(year, month, day)
    if when < GREGORIAN_START:
        return None
    for name, start in ERAS:
        if when >= start:
            n = year - start.year + 1
            return f"{name}{'元' if n == 1 else n}年"
    return None


def years_since(year: int, today: date, step: int = 10) -> int | None:
    """「出てから○年あまり」の○（step 年単位で切り下げる。年がたっても言い直さずに済む）。"""
    n = today.year - year
    if n < step:
        return None
    return n // step * step
