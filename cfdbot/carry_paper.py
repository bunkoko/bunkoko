"""キャリーの記録だけの売買（C4。docs/carry.md 8 章）。Yahoo の限月ごとの WTI（CLZ26.NYM など）を使う。

限月 1・2 は規則（最終取引日 = 受渡月の前月 25 日の 3 営業日前）で決める。毎月の最後の営業日の終値で
キャリー = ln(限月 1 ÷ 限月 2) を計算し、プラスなら翌月は買い、マイナスなら売り（6 章の carry と同じ）。お金は動かさない。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from .carry import COST, FINANCING, basis, evaluate, front_returns, month_ends, rule_expiry

MONTH_CODES = "FGHJKMNQUVXZ"
START = "2026-10-30"                 # この日（10 月の最後の営業日）の終値で最初の向きを決め、2026-11 から持つ
LOOKS = (12, 24, 36)                 # 見る時点（月の数）
T_CONTINUES = 1.0
HISTORY_DAYS = 150                   # 量（60 日の変動）を決めるために、始まりより前から取る日数
LATE_DAYS = 7                        # 月末からこれより後に記録した月は「遅れて記録」
LOG_COLUMNS = ["logged_at", "month_end", "c1", "c2", "c1_close", "c2_close", "carry", "position", "late"]
FRONT = "CL=F"


@lru_cache(maxsize=None)
def expiry(year: int, month: int) -> pd.Timestamp:
    """受渡月の最終取引日（rule_expiry を覚えておく。祝日の表を作るのに時間がかかるため）。"""
    return rule_expiry(year, month)


def ticker(year: int, month: int) -> str:
    """受渡月（year, month）の Yahoo の記号。例: (2026, 12) → CLZ26.NYM。"""
    return f"CL{MONTH_CODES[month - 1]}{year % 100:02d}.NYM"


def _next(ym: tuple[int, int]) -> tuple[int, int]:
    y, m = ym
    return (y + 1, 1) if m == 12 else (y, m + 1)


def front_pair(day: pd.Timestamp) -> tuple[tuple[int, int], tuple[int, int]]:
    """day に限月 1・2 である受渡月（最終取引日がまだ来ていない＝その日を含む、一番近い限月と、その次）。"""
    day = pd.Timestamp(day).normalize()
    ym = (day.year, day.month)
    while expiry(*ym) < day:
        ym = _next(ym)
    return ym, _next(ym)


def needed(start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    """start〜end の間に限月 1・2 になる限月の記号（受渡月の順）。"""
    ym, last = front_pair(start)[0], front_pair(end)[1]
    out = [ticker(*ym)]
    while ym != last:
        ym = _next(ym)
        out.append(ticker(*ym))
    return out


def build_curve(closes: dict[str, pd.Series], days: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.Series]:
    """日ごとの限月 1・2 の終値（c1・c2）と記号（t1・t2）、限月 1 が替わった日（sw）。"""
    rows = []
    for d in days:
        a, b = front_pair(d)
        t1, t2 = ticker(*a), ticker(*b)
        rows.append((closes.get(t1, pd.Series(dtype=float)).get(d, np.nan), closes.get(t2, pd.Series(dtype=float)).get(d, np.nan), t1, t2))
    curve = pd.DataFrame(rows, columns=["c1", "c2", "t1", "t2"], index=days)
    sw = pd.Series(np.r_[False, curve["t1"].to_numpy()[1:] != curve["t1"].to_numpy()[:-1]], index=days)
    return curve, sw


def month_complete(month_end: pd.Timestamp, now: pd.Timestamp) -> bool:
    """その月が終わったか（月の最後の平日の翌日以降）。"""
    last = pd.Timestamp(month_end).normalize() + pd.offsets.BMonthEnd(0)
    return pd.Timestamp(now).normalize() > last


def decisions(curve: pd.DataFrame, now: pd.Timestamp, start: str = START) -> pd.DataFrame:
    """終わった月の、月末の向き（start の月から）。"""
    b = basis(curve)
    me = month_ends(curve.index)
    out = []
    for d in curve.index[me]:
        if d.to_period("M") < pd.Timestamp(start).to_period("M") or not month_complete(d, now):
            continue
        c = b.get(d, np.nan)
        out.append({"month_end": d, "c1": curve.at[d, "t1"], "c2": curve.at[d, "t2"], "c1_close": curve.at[d, "c1"],
                    "c2_close": curve.at[d, "c2"], "carry": c, "position": int(np.sign(c)) if np.isfinite(c) else 0})
    return pd.DataFrame(out)


def positions(index: pd.DatetimeIndex, dec: pd.DataFrame) -> pd.Series:
    """月末に決めた向きを、翌日から次の月末まで（決めていない日は 0）。"""
    pos = pd.Series(np.nan, index=index)
    for r in dec.itertuples():
        pos[pd.Timestamp(r.month_end)] = r.position
    return pos.ffill().shift(1).fillna(0.0)


def t_value(x: pd.Series) -> float:
    x = x.dropna()
    if len(x) < 3 or x.std(ddof=1) <= 0:
        return float("nan")
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


def verdict(monthly: pd.Series) -> str:
    """docs/carry.md 8.2（12・24・36 か月の時点だけ）。"""
    n = len(monthly)
    looks = [k for k in LOOKS if n >= k]
    if not looks:
        return f"途中経過（{n} か月。{LOOKS[0]} か月で 1 回目、あと {LOOKS[0] - n} か月）"
    k = looks[-1]
    m = monthly.iloc[:k]
    total, t = float(m.sum()), t_value(m)
    if total <= 0:
        return f"続いていない（{k} か月の時点で、合計 {total:+.1%}）"
    if np.isfinite(t) and t >= T_CONTINUES:
        return f"続いている（{k} か月の時点で、合計 {total:+.1%}・t 値 {t:.2f}）"
    return f"まだ分からない（{k} か月の時点で、合計 {total:+.1%} だが t 値 {t:.2f} が {T_CONTINUES} 未満）"


def run(closes: dict[str, pd.Series], front: pd.Series | None, now: pd.Timestamp, start: str = START) -> dict:
    """値段から、向き・日々の損益（CFD・先物・いつも買い）・月ごとの損益をまとめて計算する。"""
    days = pd.DatetimeIndex(sorted(set().union(*[set(s.index) for s in closes.values()]))) if closes else pd.DatetimeIndex([])
    days = days[days <= pd.Timestamp(now).normalize()]
    curve, sw = build_curve(closes, days)
    fr = front_returns(curve[["c1", "c2"]], sw=sw)
    if front is not None and len(front):                         # 始まりより前の、限月の値の足りない日は CL=F の変化で補う（量のためだけ）
        fill = front.reindex(days).pct_change(fill_method=None)
        fill = fill.where(fill.abs() < 0.3)
        early = fr.index < pd.Timestamp(start)
        fr.loc[early, "ret"] = fr.loc[early, "ret"].fillna(fill[early])
    dec = decisions(curve, now, start)
    pos = positions(days, dec)
    after = days > pd.Timestamp(start)
    res = {}
    for name, p, fin in (("cfd", pos, FINANCING), ("futures", pos, 0.0),
                         ("long", pd.Series(np.where(days > pd.Timestamp(start), 1.0, 0.0), index=days).shift(1).fillna(0.0), FINANCING)):
        r = evaluate(fr, p, financing=fin, cost=COST)["ret"][after]
        res[name] = r
    done = dec["month_end"].max() if len(dec) else None
    monthly = {}
    for k, r in res.items():
        m = r.resample("ME").sum()
        last = pd.Timestamp(done).to_period("M") if done is not None else None
        monthly[k] = m[(m.index.to_period("M") > pd.Timestamp(start).to_period("M")) & (m.index.to_period("M") <= last)] \
            if last is not None else m.iloc[0:0]
    return {"curve": curve, "fr": fr, "decisions": dec, "daily": res, "monthly": monthly}


# --------------------------------------------------------------------------- 置き場
class Store:
    def __init__(self, root: str | Path = "data/carry"):
        self.root = Path(root)

    def contract_path(self, t: str) -> Path:
        return self.root / "contracts" / f"{t}.csv"

    @property
    def log_path(self) -> Path:
        return self.root / "paper" / "log.csv"

    def load_close(self, t: str) -> pd.Series:
        p = self.contract_path(t)
        if not p.exists():
            return pd.Series(dtype=float)
        df = pd.read_csv(p)
        return pd.Series(df["close"].to_numpy(float), index=pd.to_datetime(df["date"])).dropna().sort_index()

    def save(self, t: str, df: pd.DataFrame) -> int:
        """足して保存（同じ日は新しい方）。増えた日数を返す。"""
        p = self.contract_path(t)
        old = pd.read_csv(p) if p.exists() else pd.DataFrame(columns=["date", "close"])
        new = df[["date", "close"]].copy()
        new["date"] = pd.to_datetime(new["date"]).dt.strftime("%Y-%m-%d")
        both = pd.concat([old, new], ignore_index=True).drop_duplicates("date", keep="last").sort_values("date")
        p.parent.mkdir(parents=True, exist_ok=True)
        both.to_csv(p, index=False)
        return len(both) - len(old)

    def load_log(self) -> pd.DataFrame:
        if not self.log_path.exists():
            return pd.DataFrame(columns=LOG_COLUMNS)
        return pd.read_csv(self.log_path)

    def append(self, dec: pd.DataFrame, now: pd.Timestamp) -> int:
        """まだ記録していない月末の向きを追記する（書き換えない）。"""
        old = self.load_log()
        have = set(old["month_end"].astype(str).str[:10]) if len(old) else set()
        rows = []
        for r in dec.itertuples():
            d = f"{pd.Timestamp(r.month_end):%Y-%m-%d}"
            if d in have:
                continue
            late = (pd.Timestamp(now).normalize() - pd.Timestamp(r.month_end)).days > LATE_DAYS
            rows.append({"logged_at": f"{pd.Timestamp(now):%Y-%m-%d %H:%M:%S}", "month_end": d, "c1": r.c1, "c2": r.c2,
                         "c1_close": r.c1_close, "c2_close": r.c2_close, "carry": r.carry, "position": r.position,
                         "late": "遅れて記録" if late else ""})
        if rows:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows, columns=LOG_COLUMNS).to_csv(self.log_path, mode="a", header=not self.log_path.exists(), index=False)
        return len(rows)
