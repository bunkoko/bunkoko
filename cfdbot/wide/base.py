"""W1〜W7 と組み合わせが共通で使う型と小さな道具。docs/wide.md 3 章。

ルールのモジュール（w1.py〜w7.py）は、次の 2 つを必ず持つ:
    check(data: Path) -> Check      手元のファイルだけを見て、期間・件数・形を返す（リターンの数字は出さない。通信もしない）
    compute(data: Path) -> Stream   月の系列 d を作る（データが無い・形が違うときは NotReady）
data はリポジトリの data フォルダ（Path("data")）。通信が要るルールは fetch(data: Path, log=print) -> None も持つ。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

MIN_MONTHS = 60        # d がこれより短いルール・組は判定しない
MIN_EVENTS = 30        # 事象のルール（W2・W4・W5・W6）は、事象がこれより少なければ判定しない
COST_JP = 0.0010       # 日本株 往復（docs/stocks.md と同じ）
COST_US = 0.0030       # 米国 往復


class NotReady(Exception):
    """データが無い・形が登録と違う。何も計算せずに止まる（理由を日本語で）。"""


@dataclass
class Check:
    key: str                                   # "W1" など
    ok: bool
    lines: list[str] = field(default_factory=list)   # 期間・件数・形（リターンの数字は入れない）
    why: str = ""                              # ok でない理由


@dataclass
class Stream:
    key: str
    d: pd.Series                               # 月末の日付（時刻なし）で並んだ月の系列。コストの後。動ける期間の中で取引の無い月は 0
    events: int | None = None                  # 事象のルールだけ: 使った事象の数
    gross: float = float("nan")                # コストを引く前の月の平均（参考）
    notes: list[str] = field(default_factory=list)   # 報告の「データと注意」に出す文（期間・使わなかった事象の数など）


def month_end(ts) -> pd.Timestamp:
    """その日の属する月の、月末の日付（時刻なし）。cfdbot.stocks.monthly_returns の並べ方と同じ。"""
    return pd.Timestamp(ts).to_period("M").to_timestamp(how="end").normalize()


def finished_months(d: pd.Series, last_day) -> pd.Series:
    """終わっていない月を落とす: データの最後の日が属する月より前の月だけ残す。"""
    return d[d.index < pd.Timestamp(last_day).to_period("M").to_timestamp()]


def fill_months(d: pd.Series, start, end) -> pd.Series:
    """start〜end の月をすべて並べ、値の無い月を 0 にする（その月は取引が無かった）。"""
    idx = pd.period_range(pd.Timestamp(start), pd.Timestamp(end), freq="M").to_timestamp(how="end").normalize()
    return d.reindex(idx).fillna(0.0)
