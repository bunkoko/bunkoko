"""M2 を「その株だけの動き」で学び直す（M2r。docs/agents.md 15 章）。

その株だけの動き（残り）= 値動き − ベータ × 市場の値動き − 同じ業種の平均（業種が 5 社未満の日は全銘柄の平均）。
答え（これから 5 日の順位）と、値動きの入力 14 個を残りで作る。ほかの 10 個と学び方は M2 と同じ。
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from .. import stocks as S
from .. import stocks_short as H
from . import evaluator as E
from . import manual as M

BETA_WINDOW, BETA_MIN, BETA_CLIP = 250, 120, (0.0, 3.0)
MIN_GROUP = 5
TRIALS = 91
START, SPLIT = "2010-01-01", "2018-01-01"
PLACEBO = 300
RESIDUAL_INPUTS = [f"{p}_{k}" for p in ("night", "day", "ret") for k in H.WINDOWS] + ["gap", "day_prev"]   # 作り替える 14 個
RESULT: dict = {}            # 1 回だけ計算した判定（15.5 に書いたら、ここにも書いて動かし直せないようにする）


# --------------------------------------------------------------------------- ベータと残り
def betas(cc: pd.DataFrame, window: int = BETA_WINDOW, min_periods: int = BETA_MIN) -> pd.DataFrame:
    """株ごとの、市場（全銘柄の平均）へのベータ。t 日の値は t−1 日までの window 日で計算。0〜3 に切り、無ければ 1。"""
    x = cc
    m = cc.mean(axis=1)
    y = x.notna().mul(m, axis=0).where(x.notna())                  # その株に値のある日だけの市場
    n = x.notna().astype(float).rolling(window, min_periods=1).sum()
    sx = x.fillna(0).rolling(window, min_periods=1).sum()
    sy = y.fillna(0).rolling(window, min_periods=1).sum()
    sxy = (x * y).fillna(0).rolling(window, min_periods=1).sum()
    syy = (y * y).fillna(0).rolling(window, min_periods=1).sum()
    cov = sxy - sx * sy / n.where(n > 0)
    var = syy - sy * sy / n.where(n > 0)
    b = (cov / var.where(var > 1e-12)).where(n >= min_periods)
    return b.shift(1).clip(*BETA_CLIP).fillna(1.0)


def groups(columns: pd.Index, sector: pd.Series) -> np.ndarray:
    """銘柄ごとの業種。業種の分からない株は 1 社だけの組（業種の平均の代わりに全銘柄の平均を引く）。"""
    s = sector.reindex(columns)
    return np.array([str(v) if isinstance(v, str) and v else f"?{c}" for c, v in zip(columns, s)], dtype=object)


def residual(r: pd.DataFrame, beta: pd.DataFrame, group: np.ndarray, min_group: int = MIN_GROUP) -> pd.DataFrame:
    """その日の値動き r の残り: r − ベータ × 市場の r − 同じ業種の平均（その日に 5 社未満の業種は全銘柄の平均）。"""
    ok = r.notna()
    m = r.mean(axis=1)
    a = r - beta.reindex_like(r).where(ok).mul(m, axis=0)
    g_mean = a.T.groupby(group).transform("mean").T
    g_count = ok.T.groupby(group).transform("sum").T
    allm = pd.DataFrame(np.repeat(a.mean(axis=1).to_numpy()[:, None], a.shape[1], axis=1), index=a.index, columns=a.columns)
    center = g_mean.where(g_count >= min_group, allm)
    return (a - center).where(ok)


# --------------------------------------------------------------------------- 入力と答え
def parts_resid(open_: pd.DataFrame, close: pd.DataFrame, sector: pd.Series) -> dict[str, pd.DataFrame]:
    """毎日の夜・昼・終値のリターンの残りと、ベータ（cc で計算。夜・昼にも同じものを使う）。"""
    p = H.parts(open_, close)
    beta = betas(p["cc"])
    g = groups(close.columns, sector)
    return {"night": residual(p["night"], beta, g), "day": residual(p["day"], beta, g), "cc": residual(p["cc"], beta, g),
            "beta": beta, "group": g}


def features(open_: pd.DataFrame, close: pd.DataFrame, sector: pd.Series, rp: dict | None = None) -> tuple[list[str], np.ndarray]:
    """M2 の 24 個のうち、値動きの 14 個を残りから作り直したもの（ほかの 10 個は M2 と同じ値）。"""
    names, x = H.features(open_, close)
    rp = rp or parts_resid(open_, close, sector)
    night, day, cc = rp["night"], rp["day"], rp["cc"]
    new: dict[str, pd.DataFrame] = {}
    for k in H.WINDOWS:
        new[f"night_{k}"] = night.rolling(k, min_periods=k).mean()
        new[f"day_{k}"] = day.shift(1).rolling(k, min_periods=k).mean()
        new[f"ret_{k}"] = cc.shift(1).rolling(k, min_periods=k).sum()
    new["gap"] = night
    new["day_prev"] = day.shift(1)
    for k, v in new.items():
        x[:, :, names.index(k)] = H.rank_pct(v).to_numpy(dtype=np.float32)
    return names, x


def target(close: pd.DataFrame, beta: pd.DataFrame, group: np.ndarray, hold: int = H.HOLD) -> pd.DataFrame:
    """答え: これから hold 日の終値のリターン（t 日の引け → t+hold 日の引け）の残りの、その日の中での順位。"""
    r = np.log(close.shift(-hold) / close)
    return H.rank_pct(residual(r, beta, group))


def scores(open_: pd.DataFrame, close: pd.DataFrame, sector: pd.Series, params: dict | None = None,
           log: Callable[[str], None] = print) -> dict:
    """M2 と M2r の点数（どちらも 2010 年から毎年学び直す）と、比べるのに使うもの。"""
    tradable = (close.notna() & H.parts(open_, close)["night"].notna()).to_numpy()
    _, x = H.features(open_, close)
    _, y5 = H.targets(open_, close)
    log("M2（今の作り方。同じ値段で学び直す）")
    base = H.walk_forward(x, y5, tradable, embargo=H.EMBARGO, params=params, log=log)
    del x
    rp = parts_resid(open_, close, sector)
    _, xr = features(open_, close, sector, rp)
    yr = target(close, rp["beta"], rp["group"])
    log("M2r（その株だけの動きで学ぶ）")
    new = H.walk_forward(xr, yr, tradable, embargo=H.EMBARGO, params=params, log=log)
    return {"base": base, "new": new, "beta": rp["beta"], "group": rp["group"], "y_raw": y5, "y_res": yr}


# --------------------------------------------------------------------------- 判定（15.3）
def evaluate(base: pd.DataFrame, new: pd.DataFrame, open_: pd.DataFrame, close: pd.DataFrame, beta: pd.DataFrame,
             y_raw: pd.DataFrame, y_res: pd.DataFrame, placebo: int = PLACEBO, seed: int = 11) -> dict:
    """M2r − M2（上位 10% を引けで買い 5 日持つ形）の判定と参考の数字。"""
    cc = H.parts(open_, close)["cc"]
    tradable_df = close.notna() & H.parts(open_, close)["night"].notna()
    tradable = tradable_df.to_numpy()
    b, w = base.reindex_like(close).to_numpy(dtype=float), new.reindex_like(close).to_numpy(dtype=float)
    book_b, book_n = E.top_k(b, E.TOP_SHARE, E.MIN_VALID), E.top_k(w, E.TOP_SHARE, E.MIN_VALID)
    pnl_b, pnl_n = E.book_pnl(book_b, cc), E.book_pnl(book_n, cc)
    i0 = int(cc.index.searchsorted(pd.Timestamp(START)))
    part = cc.iloc[i0:]

    def gross(book: np.ndarray) -> float:
        return float(E.book_pnl(book, part, 0.0)["gross"].mean())

    base_gross = gross(book_b[i0:])
    diff = (w - b)[i0:]
    rng = np.random.default_rng(seed)
    null = np.array([gross(E.top_k(b[i0:] + E.shuffle_rows(diff, rng), E.TOP_SHARE, E.MIN_VALID)) - base_gross for _ in range(placebo)])
    real = gross(book_n[i0:]) - base_gross
    d = (pnl_n["d"] - pnl_b["d"])
    d = d[d.index >= pd.Timestamp(START)]
    j = S.judge(d.resample("ME").sum(), SPLIT, TRIALS, float(np.mean(null < real)) if placebo else None)
    m = np.asarray(cc.index >= pd.Timestamp(START))

    def own(p: pd.DataFrame) -> dict:
        x = p[m]
        late = x[x.index >= pd.Timestamp(SPLIT)]
        return {"gross": float(x["gross"].mean() * 252), "net": float(x["d"].mean() * 252), "net_late": float(late["d"].mean() * 252),
                "turnover": float(x["turnover"].mean())}

    def weekly(score: pd.DataFrame) -> pd.DataFrame:
        wsel, starts = M.weekly_selection(score.reindex_like(close), tradable_df)
        return H.weights_pnl(cc, M.weekly_weights(wsel, starts), E.COST)[m]

    wk_b, wk_n = weekly(base), weekly(new)
    wk = wk_n["d"] - wk_b["d"]
    bt = beta.to_numpy(dtype=float)

    def mean_beta(book: np.ndarray) -> float:
        sel = book[m]
        return float(np.nanmean(np.where(sel, bt[m], np.nan)))

    def ic(score: pd.DataFrame, y: pd.DataFrame) -> pd.Series:
        keep = score.index >= pd.Timestamp(START)
        return H.yearly_ic(score.loc[keep], y.loc[keep])

    j.update(real_gross=real * 252, base=own(pnl_b), new=own(pnl_n), by_year=d.groupby(d.index.year).sum(),
             weekly={"diff": float(wk.mean() * 252), "late": float(wk[wk.index >= pd.Timestamp(SPLIT)].mean() * 252),
                     "base": float(wk_b["d"].mean() * 252), "new": float(wk_n["d"].mean() * 252)},
             beta={"base": mean_beta(book_b), "new": mean_beta(book_n), "all": float(np.nanmean(np.where(tradable[m], bt[m], np.nan)))},
             ic={"base_raw": ic(base, y_raw), "new_raw": ic(new, y_raw), "base_res": ic(base, y_res), "new_res": ic(new, y_res)},
             period=(cc.index[m][0], cc.index[m][-1]) if m.any() else (None, None))
    return j
