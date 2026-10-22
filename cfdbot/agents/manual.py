"""M2 を手で注文する形（docs/agents.md 13 章）: 1 日遅れの 5 日保有（M2a）と、週 1 回・10 銘柄・1 日遅れ（M2b）。

過去の検証は M2 の点数（selector_scores.csv）と値段だけを読む。先の記録は、週の最初の営業日に上位 10 銘柄を
ledger_extra.csv の帳 m2_manual として書く（ledger.csv は変えない）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import stocks as S
from .. import stocks_short as H

BOOK = "m2_manual"
WEEK_TOP, DELAY = 10, 1
COST = H.COST                         # 往復 0.10%（下げない）
START, SPLIT = "2010-01-01", "2018-01-01"
TRIALS = {"M2a": 85, "M2b": 86}       # 81 ＋ 1C・案 2 ＋ C3 ＝ 84 に、M2a・M2b
PLACEBO = 300
REF_DELAYS = (0, 2, 5)                # 参考: 5 日保有の遅れ


# --------------------------------------------------------------------------- 選び方
def delayed(sel: np.ndarray, tradable: np.ndarray, days: int = DELAY) -> np.ndarray:
    """t 日に選んだものを t+days 日に買う（その日に値段の無い株は買えない）。"""
    if days <= 0:
        return sel.copy()
    out = np.zeros_like(sel)
    out[days:] = sel[:-days]
    return out & tradable


def week_starts(index: pd.DatetimeIndex) -> np.ndarray:
    """週の最初の営業日の位置（前の営業日と ISO の週が違う日。最初の日も含む）。"""
    iso = index.isocalendar()
    key = (iso["year"].to_numpy() * 100 + iso["week"].to_numpy()).astype(int)
    return np.flatnonzero(np.r_[True, key[1:] != key[:-1]])


def weekly_weights(sel_days: np.ndarray, starts: np.ndarray, delay: int = DELAY) -> np.ndarray:
    """週の最初の営業日 s に選んだもの（sel_days[s]）を s+delay 日の引けに等分で買い、次の週の買いの日の引けまで持つ。

    戻り値は t 日の引けの後の重み（weights_pnl の形）。
    """
    n, m = sel_days.shape
    w = np.zeros((n, m))
    buys = starts + delay
    for i, (s, b) in enumerate(zip(starts, buys)):
        if b >= n:
            break
        end = starts[i + 1] + delay if i + 1 < len(starts) else n
        row = sel_days[s].astype(float)
        k = row.sum()
        if k > 0:
            w[b:min(end, n)] = row / k
    return w


def weekly_selection(score: pd.DataFrame, tradable: pd.DataFrame, count: int = WEEK_TOP) -> tuple[np.ndarray, np.ndarray]:
    """週の最初の営業日だけ、上位 count 銘柄を選ぶ（ほかの日は空）。戻り値: 選んだもの、週の最初の営業日の位置。"""
    starts = week_starts(score.index)
    top = H.top_share(score, tradable, count=count)
    keep = np.zeros(len(score), dtype=bool)
    keep[starts] = True
    return top & keep[:, None], starts


# --------------------------------------------------------------------------- 過去の検証（13.2）
def _placebo(make_weights, sel: np.ndarray, tradable: np.ndarray, cc: pd.DataFrame, m: np.ndarray,
             n: int, rng: np.random.Generator) -> np.ndarray:
    return np.array([H.weights_pnl(cc, make_weights(H.random_like(sel, tradable, rng)))["gross"][m].mean() for _ in range(n)])


def evaluate(score: pd.DataFrame, open_: pd.DataFrame, close: pd.DataFrame, placebo: int = PLACEBO,
             seed: int = 11) -> dict:
    """M2a・M2b の判定と参考の数字。score は selector_scores.csv（日 × 株）を値段の形にそろえたもの。"""
    score = score.reindex(index=close.index, columns=close.columns)
    cc = H.parts(open_, close)["cc"]
    tradable_df = close.notna() & H.parts(open_, close)["night"].notna()
    tradable = tradable_df.to_numpy()
    m = np.asarray(cc.index >= pd.Timestamp(START))
    rng = np.random.default_rng(seed)
    mkt = H.market_mean(cc)
    out: dict = {"ref": {}}

    def result(key: str, pnl: pd.DataFrame, null: np.ndarray) -> dict:
        real = float(pnl["gross"].mean())
        pl = float(np.mean(null < real)) if len(null) else None
        j = S.judge(S.month_sum(pnl["d"]), SPLIT, TRIALS[key], pl)
        total = H.perf(pnl["d"] + pnl["invested"] * mkt[m])
        j.update(key=key, gross_ann=real * 252, net_ann=float(pnl["d"].mean() * 252), turnover=float(pnl["turnover"].mean()),
                 total=total, hold=H.perf(mkt[m]), by_year=pnl["d"].groupby(pnl.index.year).sum(), d_daily=pnl["d"])
        return j

    sel = H.top_share(score, tradable_df)
    # M2a: 1 日遅れの 5 日保有
    pnl_a = H.weights_pnl(cc, H.cohort_weights(delayed(sel, tradable)), COST)[m]
    null_a = _placebo(lambda s: H.cohort_weights(delayed(s, tradable)), sel, tradable, cc, m, placebo, rng)
    out["M2a"] = result("M2a", pnl_a, null_a)
    # M2b: 週 1 回・10 銘柄・1 日遅れ
    wsel, starts = weekly_selection(score, tradable_df)
    pnl_b = H.weights_pnl(cc, weekly_weights(wsel, starts), COST)[m]
    null_b = _placebo(lambda s: weekly_weights(s, starts), wsel, tradable, cc, m, placebo, rng)
    out["M2b"] = result("M2b", pnl_b, null_b)
    # 参考（判定しない）
    for dly in REF_DELAYS:
        p = H.weights_pnl(cc, H.cohort_weights(delayed(sel, tradable, dly)), COST)[m]
        out["ref"][f"5 日保有・遅れ {dly} 日"] = _ref(p)
    out["ref"]["5 日保有・遅れ 1 日（M2a）"] = _ref(pnl_a)
    p0 = H.weights_pnl(cc, weekly_weights(wsel, starts, delay=0), COST)[m]
    out["ref"]["週 1 回・10 銘柄・遅れ 0 日"] = _ref(p0)
    out["ref"]["週 1 回・10 銘柄・遅れ 1 日（M2b）"] = _ref(pnl_b)
    out["period"] = (cc.index[m][0], cc.index[m][-1]) if m.any() else (None, None)
    return out


def _ref(p: pd.DataFrame) -> dict:
    late = p[p.index >= pd.Timestamp(SPLIT)]
    return {"gross_ann": float(p["gross"].mean() * 252), "net_ann": float(p["d"].mean() * 252),
            "net_late": float(late["d"].mean() * 252) if len(late) else np.nan, "turnover": float(p["turnover"].mean())}


# --------------------------------------------------------------------------- 先の記録（13.3）
def is_week_start(index: pd.DatetimeIndex, day: pd.Timestamp) -> bool:
    """day が週の最初の営業日か（前の営業日と ISO の週が違う）。前の営業日が無ければ True。"""
    pos = int(index.searchsorted(pd.Timestamp(day)))
    if pos >= len(index) or index[pos] != pd.Timestamp(day):
        return False
    if pos == 0:
        return True
    a, b = index[pos - 1].isocalendar(), index[pos].isocalendar()
    return (a[0], a[1]) != (b[0], b[1])


def manual_rows(day: pd.Timestamp, selector_row: pd.Series, index: pd.DatetimeIndex, names: dict[str, str],
                count: int = WEEK_TOP) -> pd.DataFrame:
    """週の最初の営業日なら、M2 の点数の上位 count 銘柄（列は台帳と同じ）。ほかの日は空。"""
    cols = ["date", "ticker", "book", "selector", "ir", "combined", "name", "reason"]
    if not is_week_start(index, day):
        return pd.DataFrame(columns=cols)
    s = selector_row.dropna()
    if len(s) < H.MIN_VALID:
        return pd.DataFrame(columns=cols)
    top = s.sort_values(ascending=False, kind="stable").head(count)
    rows = [(day, t, BOOK, float(v), np.nan, np.nan, names.get(t, t),
             f"手で買う候補 {i}/{count} 位（値動きの点数 {v:+.3f}、{len(s)} 銘柄から。次の営業日の引けで買い、翌週に入れ替える）")
            for i, (t, v) in enumerate(top.items(), 1)]
    return pd.DataFrame(rows, columns=cols)


def this_week(extra: pd.DataFrame) -> dict:
    """ledger_extra.csv から、最新の週の m2_manual と、前の週からの入れ替え（売る・買う・持ち続ける）。"""
    g = extra[extra["book"] == BOOK].copy() if len(extra) and "book" in extra else pd.DataFrame()
    if g.empty:
        return {"date": None}
    g["date"] = pd.to_datetime(g["date"].astype(str).str[:10])
    days = sorted(g["date"].unique())
    now = g[g["date"] == days[-1]]
    prev = g[g["date"] == days[-2]] if len(days) > 1 else g.iloc[0:0]
    a, b = set(prev["ticker"].astype(str)), set(now["ticker"].astype(str))
    names = dict(zip(g["ticker"].astype(str), g["name"]))
    return {"date": pd.Timestamp(days[-1]), "prev_date": pd.Timestamp(days[-2]) if len(days) > 1 else None,
            "rows": now, "buy": sorted(b - a), "sell": sorted(a - b), "keep": sorted(a & b), "names": names,
            "decided_at": str(now["decided_at"].iloc[0]) if "decided_at" in now and len(now) else ""}
