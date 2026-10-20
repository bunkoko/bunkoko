"""J7 決算発表をまたいで持つ（docs/earnings_premium.md）。決算短信の時刻と値段の日足だけで計算する。

発表が値段に入る最初の日 t0 の前の営業日の引けで買い、t0 の次の営業日の引けで売る（2 日持つ）。
過去の検証は、docs/stocks.md の C7 で見た Yahoo の決算日より前の決算だけを使う。先の記録は 2026-10-21 から。
"""

from __future__ import annotations

import unicodedata

import numpy as np
import pandas as pd

from . import stocks as S
from . import stocks_short as H

COST = H.COST                        # 往復 0.10%
CLOSE_CHANGE = pd.Timestamp("2024-11-05")   # この日から東証の引けは 15:30（それまでは 15:00）
START_FORWARD = "2026-10-21"
TRIALS = 87                          # 81 ＋ 1C・案 2 ＋ C3 ＋ M2a・M2b ＋ J7
MIN_EVENTS, MIN_MONTHS = 1000, 36    # 使っていない部分がこれに届かなければ判定しない
BUFFER = 5                           # Yahoo の最初の決算日より、この営業日数以上前の決算だけを使う
PLACEBO = 200
LOOKS = (250, 500, 750)              # 先の記録の見る時点（営業日の数）
T_CONTINUES = 1.0


def is_tanshin(title: str) -> bool:
    t = unicodedata.normalize("NFKC", str(title))
    return "決算短信" in t and "訂正" not in t


def events(disc: pd.DataFrame, index: pd.DatetimeIndex, columns) -> pd.DataFrame:
    """決算短信ごとの t0（発表が値段に入る最初の営業日の位置）。列: ticker / j / t0 / time。同じ会社・同じ t0 は 1 つ。"""
    d = disc[disc["title"].map(is_tanshin)].copy()
    cols = {t: k for k, t in enumerate(columns)}
    d = d[d["ticker"].isin(cols)]
    if d.empty:
        return pd.DataFrame(columns=["ticker", "j", "t0", "time"])
    tm = pd.to_datetime(d["time"], errors="coerce")
    d = d[tm.notna().to_numpy()]
    tm = tm[tm.notna()]
    sec = tm.dt.hour * 3600 + tm.dt.minute * 60 + tm.dt.second
    close = np.where(tm.dt.normalize() >= CLOSE_CHANGE, 15 * 3600 + 1800, 15 * 3600)
    after = sec.to_numpy() >= close                                  # 引けちょうど（15:00 の開示など）は引けの後
    day = tm.dt.normalize() + pd.to_timedelta(after.astype(int), unit="D")
    out = pd.DataFrame({"ticker": d["ticker"].to_numpy(), "j": d["ticker"].map(cols).to_numpy(int),
                        "t0": index.searchsorted(day.to_numpy()), "time": tm.to_numpy()})
    out = out[(out["t0"] >= 1) & (out["t0"] <= len(index) - 2)]
    return out.sort_values(["j", "t0", "time"]).drop_duplicates(["j", "t0"]).reset_index(drop=True)


def weights(ev: pd.DataFrame, shape: tuple[int, int], t0: np.ndarray | None = None) -> np.ndarray:
    """t 日の引けの後の重み: t0−1 と t0 の引けの後に持つ（t0 と t0+1 の値動きを受ける）。同じ日に持つ株は等分。"""
    t0 = ev["t0"].to_numpy() if t0 is None else t0
    j = ev["j"].to_numpy()
    flag = np.zeros(shape, dtype=float)
    for k in (1, 0):
        np.maximum.at(flag, (t0 - k, j), 1.0)
    n = flag.sum(axis=1, keepdims=True)
    return np.where(n > 0, flag / np.maximum(n, 1), 0.0)


def seen_cutoffs(earn: pd.DataFrame, index: pd.DatetimeIndex) -> dict[str, int]:
    """会社ごとに、C7 で見た Yahoo の最初の決算日の位置 − BUFFER（これより後の t0 は「見た」もの）。"""
    if earn is None or earn.empty:
        return {}
    first = pd.to_datetime(earn["date"]).groupby(earn["ticker"]).min()
    return {t: int(index.searchsorted(d)) - BUFFER for t, d in first.items()}


def unseen_mask(ev: pd.DataFrame, cut: dict[str, int]) -> np.ndarray:
    """使っていない決算（その会社の Yahoo の決算日より BUFFER 営業日以上前。決算日の無い会社は全部）。"""
    lim = ev["ticker"].map(cut).fillna(np.inf).to_numpy(float)
    return ev["t0"].to_numpy() <= lim


def placebo_t0(ev: pd.DataFrame, index: pd.DatetimeIndex, rng: np.random.Generator) -> np.ndarray:
    """決算ごとに、同じ会社・同じ四半期の中のでたらめな日に t0 をずらす（本当の t0 の前後 2 日は避ける）。"""
    q = index.to_period("Q")
    codes = np.asarray(q.year * 4 + q.quarter)
    first = pd.Series(np.arange(len(index))).groupby(codes).min()
    last = pd.Series(np.arange(len(index))).groupby(codes).max()
    t0 = ev["t0"].to_numpy()
    qs = np.maximum(first.reindex(codes[t0]).to_numpy(), 1)
    qe = np.minimum(last.reindex(codes[t0]).to_numpy(), len(index) - 2)
    span = qe - qs - 4
    ok = span > 0
    r = qs + np.floor(rng.random(len(t0)) * np.maximum(span, 1)).astype(int)
    r = np.where(r >= t0 - 2, r + 5, r)
    return np.where(ok, np.clip(r, 1, len(index) - 2), t0)


def two_day(ev: pd.DataFrame, cc: pd.DataFrame) -> float:
    """決算ごとの t0 と t0+1 の 2 日の「株 − 全銘柄の平均」の平均（コストを引く前）。"""
    if ev.empty:
        return float("nan")
    a = cc.sub(cc.mean(axis=1), axis=0).to_numpy()
    t0, j = ev["t0"].to_numpy(), ev["j"].to_numpy()
    return float(np.nanmean(a[t0, j] + a[t0 + 1, j]))


def _pnl(ev: pd.DataFrame, cc: pd.DataFrame, cost: float = COST, t0: np.ndarray | None = None) -> pd.DataFrame:
    return H.weights_pnl(cc, weights(ev, cc.shape, t0), cost)


def _span(ev: pd.DataFrame, index: pd.DatetimeIndex) -> tuple[pd.Timestamp, pd.Timestamp]:
    return index[int(ev["t0"].min()) - 1], index[int(ev["t0"].max()) + 1]


def evaluate_past(disc: pd.DataFrame, earn: pd.DataFrame, open_: pd.DataFrame, close: pd.DataFrame,
                  placebo: int = PLACEBO, seed: int = 7) -> dict:
    """3 章: 使っていない決算だけで判定する。足りなければ verdict は「判定しない」。"""
    cc = H.parts(open_, close)["cc"]
    ev_all = events(disc, close.index, close.columns)
    ev_all = ev_all[ev_all["time"] < pd.Timestamp(START_FORWARD)]
    m = unseen_mask(ev_all, seen_cutoffs(earn, close.index))
    ev, seen = ev_all[m].reset_index(drop=True), ev_all[~m].reset_index(drop=True)
    out = {"events": len(ev), "seen_events": len(seen), "firms": int(ev["ticker"].nunique()) if len(ev) else 0}
    out["two_day"], out["seen_two_day"] = two_day(ev, cc), two_day(seen, cc)
    if len(seen):
        s0, s1 = _span(seen, close.index)
        ps = _pnl(seen, cc)
        ps = ps[(ps.index >= s0) & (ps.index <= s1)]
        out["seen"] = {"start": s0, "end": s1, "net_ann": float(ps["d"].mean() * 252), "gross_ann": float(ps["gross"].mean() * 252)}
    if ev.empty:
        out.update(verdict="判定しない（使っていない決算が無い）", months=0)
        return out
    start, end = _span(ev, close.index)
    pnl = _pnl(ev, cc)
    keep = (pnl.index >= start) & (pnl.index <= end)
    pnl = pnl[keep]
    d = S.month_sum(pnl["d"])
    months = len(d)
    out.update(start=start, end=end, months=months, gross_ann=float(pnl["gross"].mean() * 252),
               net_ann=float(pnl["d"].mean() * 252), turnover=float(pnl["turnover"].mean()),
               zero_cost_ann=float(_pnl(ev, cc, 0.0)["d"][keep].mean() * 252))
    if len(ev) < MIN_EVENTS or months < MIN_MONTHS:
        out["verdict"] = (f"判定しない（使っていない決算が {len(ev)} 回・{months} か月。{MIN_EVENTS} 回以上・{MIN_MONTHS} か月以上が要る。"
                          "試した数も増やさない）")
        return out
    split = d.index[months // 2]
    rng = np.random.default_rng(seed)
    real = float(pnl["gross"].mean())
    null = np.array([_pnl(ev, cc, 0.0, placebo_t0(ev, close.index, rng))["gross"][keep].mean() for _ in range(placebo)])
    j = S.judge(d, f"{split:%Y-%m-01}", TRIALS, float(np.mean(null < real)) if placebo else None)
    out.update(judge=j, verdict=j["verdict"], split=split, d=d)
    return out


def forward(disc: pd.DataFrame, open_: pd.DataFrame, close: pd.DataFrame, start: str = START_FORWARD) -> dict:
    """4 章: start からの決算短信に同じ決まりを当てた途中経過（コストの後）。"""
    cc = H.parts(open_, close)["cc"]
    s = int(close.index.searchsorted(pd.Timestamp(start)))
    ev = events(disc, close.index, close.columns)
    ev = ev[ev["t0"] - 1 >= s].reset_index(drop=True)
    days = close.index[s:]
    out = {"events": len(ev), "days": len(days), "start": pd.Timestamp(start)}
    if ev.empty or not len(days):
        out.update(daily=pd.Series(dtype=float), monthly=pd.Series(dtype=float), verdict=verdict(pd.Series(dtype=float), len(days)))
        return out
    pnl = _pnl(ev, cc)
    daily = pnl["d"][pnl.index >= days[0]]
    out.update(daily=daily, monthly=daily.resample("ME").sum(), verdict=verdict(daily, len(days)), two_day=two_day(ev, cc))
    return out


def verdict(daily: pd.Series, n_days: int) -> str:
    """4 章（250・500・750 営業日の時点だけ）。"""
    looks = [k for k in LOOKS if n_days >= k]
    if not looks:
        return f"途中経過（{n_days} 営業日。{LOOKS[0]} 営業日で 1 回目、あと {LOOKS[0] - n_days} 営業日）"
    k = looks[-1]
    part = daily.iloc[:k]
    total = float(part.sum())
    t = S.t_value(part.resample("ME").sum())
    if total <= 0:
        return f"続いていない（{k} 営業日の時点で、合計 {total:+.2%}）"
    if np.isfinite(t) and t >= T_CONTINUES:
        return f"続いている（{k} 営業日の時点で、合計 {total:+.2%}・月ごとの t 値 {t:.2f}）"
    return f"まだ分からない（{k} 営業日の時点で、合計 {total:+.2%} だが t 値 {t:.2f} が {T_CONTINUES} 未満）"


# --------------------------------------------------------------------------- 7 章の点検（結果を見た後に足した参考。判定は変えない）
CROWD = 10       # 同じ日に持つ株がこの数以上の日を「混んだ日」とする
OLD_YEARS = 5    # Yahoo の最初の決算日より、この年数以上前の決算を「古い」とする


def _excess(ev: pd.DataFrame, cc: pd.DataFrame) -> np.ndarray:
    a = cc.sub(cc.mean(axis=1), axis=0).to_numpy()
    t0, j = ev["t0"].to_numpy(), ev["j"].to_numpy()
    return a[t0, j] + a[t0 + 1, j]


def _crowd(ev: pd.DataFrame, shape: tuple[int, int]) -> np.ndarray:
    """決算ごとの、買った日（t0−1 の引けの後）に同じ組で持っていた株の数。"""
    flag = np.zeros(shape, dtype=float)
    for k in (1, 0):
        np.maximum.at(flag, (ev["t0"].to_numpy() - k, ev["j"].to_numpy()), 1.0)
    return flag.sum(axis=1)[ev["t0"].to_numpy() - 1]


def _row(label: str, ev: pd.DataFrame, x: np.ndarray, index: pd.DatetimeIndex) -> dict:
    """1 回に同じ金額で持った形（コストは 1 回 0.10%）の平均と、月ごとの合計の t 値。"""
    ok = np.isfinite(x)
    if not ok.any():
        return {"group": label, "events": 0, "firms": 0, "mean": np.nan, "median": np.nan, "net": np.nan, "t": np.nan}
    net = pd.Series(x[ok] - COST, index=index[ev["t0"].to_numpy()[ok]])
    return {"group": label, "events": int(ok.sum()), "firms": int(len(pd.unique(ev["ticker"].to_numpy()[ok]))),
            "mean": float(np.mean(x[ok])), "median": float(np.median(x[ok])), "net": float(net.mean()),
            "t": S.t_value(net.resample("ME").sum())}


def diagnose(disc: pd.DataFrame, earn: pd.DataFrame, open_: pd.DataFrame, close: pd.DataFrame) -> dict:
    """7 章: 使った決算と見た決算の差がどこから来るか（年・会社の群・混んだ日・大きな値動き）。"""
    cc = H.parts(open_, close)["cc"]
    idx = close.index
    ev = events(disc, idx, close.columns)
    ev = ev[ev["time"] < pd.Timestamp(START_FORWARD)].reset_index(drop=True)
    cut = seen_cutoffs(earn, idx)
    m = unseen_mask(ev, cut)
    x = _excess(ev, cc)
    year = idx[ev["t0"].to_numpy()].year
    by_year = pd.DataFrame({"year": year, "unseen": m, "x": x}).dropna(subset=["x"]).groupby(["year", "unseen"])["x"].agg(["count", "mean"])
    by_year = by_year.unstack("unseen")
    has_date = ev["ticker"].isin(cut).to_numpy()
    first = pd.to_datetime(earn["date"]).groupby(earn["ticker"]).min() if earn is not None and len(earn) else pd.Series(dtype="datetime64[ns]")
    years_before = ((ev["ticker"].map(first) - pd.to_datetime(ev["time"])).dt.days / 365.25).to_numpy(float)
    rows = []
    for label, sel in (("使った決算（全部）", m), ("　うち Yahoo の決算日が 1 つも無い会社", m & ~has_date),
                       ("　うち Yahoo の決算日より前（その会社の最初の日より 5 年以上前）", m & has_date & (years_before >= OLD_YEARS)),
                       ("　うち Yahoo の決算日より前（5 年未満前）", m & has_date & (years_before < OLD_YEARS)),
                       ("見た決算", ~m)):
        rows.append(_row(label, ev[sel], x[sel], idx))
    crowd = []
    for label, sel in (("使った決算", m), ("見た決算", ~m)):
        e, xs = ev[sel], x[sel]
        c = _crowd(e, close.shape) if len(e) else np.array([])
        for name, s in ((f"混んだ日（同じ日に {CROWD} 社以上）", c >= CROWD), (f"空いた日（{CROWD} 社未満）", c < CROWD)):
            r = _row(f"{label}・{name}", e[s], xs[s], idx)
            r["days"] = int(len(np.unique(e[s]["t0"].to_numpy())))
            crowd.append(r)
    xu = x[m & np.isfinite(x)]
    big = {}
    if len(xu):
        top = np.sort(xu)[::-1]
        k = max(1, int(round(len(top) * 0.01)))
        big = {"mean": float(xu.mean()), "trim": float(np.mean(top[k:])) if len(top) > k else np.nan, "k": k,
               "top_share": float(top[:k].sum() / xu.sum()) if xu.sum() != 0 else np.nan,
               "pos": float(np.mean(xu > 0)), "over10": float(np.mean(np.abs(xu) > 0.10))}
    return {"by_year": by_year, "groups": pd.DataFrame(rows), "crowd": pd.DataFrame(crowd), "big": big}
