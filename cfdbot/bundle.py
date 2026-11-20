"""束ねる検証（docs/bundle.md。結果を見る前に決めたもの）。柱ごとの月の系列を、値動きの大きさをそろえて一緒に持つ。

値動きの大きさ（sigmas）・t 値・前半と後半の分け方は、広げた候補（docs/wide.md。cfdbot/wide/core.py）と同じものを使う。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .wide import base
from .wide import core as W

N_TRIES, N_WORLDS, SEED = 100, 20_000, 20261120      # 物差し（4.3）: 試した数・世界の数・乱数の種
BEYOND, LOOKS_GOOD, USELESS = "偶然を超えた", "良く見えるが、選んだ分と区別できない", "使えない"


# --------------------------------------------------------------------------- 月ごとにそろえる（3 章の 1）
def to_monthly(daily: pd.Series) -> pd.Series:
    """日ごとの成績を暦の月ごとに足す（複利にしない）。最後の日の属する月は終わっていないので落とす。間の、取引の無い月は 0。"""
    s = pd.Series(daily).dropna().sort_index()
    m = s.groupby(pd.DatetimeIndex(s.index).map(base.month_end)).sum()
    m = base.finished_months(m, s.index[-1])
    if m.empty:
        return m
    last = base.month_end(pd.Timestamp(s.index[-1]).to_period("M").to_timestamp() - pd.Timedelta(days=1))   # 最後の日の月の、前の月
    return base.fill_months(m, m.index[0], last)


# --------------------------------------------------------------------------- 重さと束（3 章の 2〜4）
def _present(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> dict[str, list[str]]:
    """系列のある家族だけ（家族 → その系列の名前）。"""
    return {f: [m for m in ms if m in series] for f, ms in families.items() if any(m in series for m in ms)}


def weights(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> pd.DataFrame:
    """月ごとの重さ。∝（1 ÷ 前の月までの値動きの大きさ）÷（同じ家族でその月に使える系列の数）。足して 1。使えない系列は欠け。"""
    fam = _present(series, families)
    cols = [m for ms in fam.values() for m in ms]
    d = pd.concat({k: series[k] for k in cols}, axis=1, sort=True)
    sg = pd.concat({k: W.sigmas(series[k].dropna()) for k in cols}, axis=1, sort=True).reindex(d.index)
    ok = d.notna() & sg.notna() & (sg > 0)
    raw = (1.0 / sg).where(ok)
    for ms in fam.values():
        n = ok[ms].sum(axis=1)
        raw[ms] = raw[ms].div(n.where(n > 0), axis=0)
    return raw.div(raw.sum(axis=1, min_count=1), axis=0)


def bundle(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> pd.Series:
    """束の月の系列。使える家族が半分以上ある月だけ（足りない月は入れない）。"""
    fam = _present(series, families)
    w = weights(series, families)
    d = pd.concat({k: series[k] for k in w.columns}, axis=1, sort=True).reindex(w.index)
    usable = pd.DataFrame({f: w[ms].notna().any(axis=1) for f, ms in fam.items()})
    keep = usable.sum(axis=1) >= math.ceil(len(fam) / 2)
    return (w * d).sum(axis=1, min_count=1)[keep]


# --------------------------------------------------------------------------- 束の数字（4.1）
def stats(d: pd.Series) -> dict:
    """月の数・平均・前半と後半の平均・t 値（全期間・後半）・年率の値動き・最大の下げ（足し上げた線の、山からの落ち込み）。"""
    d = d.dropna()
    a, b = W.halves(d)
    line = d.cumsum()
    return {"n": len(d), "mean": float(d.mean()), "mean1": float(a.mean()), "mean2": float(b.mean()),
            "t": W.t_value(d), "t2": W.t_value(b), "vol": float(d.std(ddof=1) * math.sqrt(12)),
            "max_dd": float((line.cummax().clip(lower=0.0) - line).max())}


# --------------------------------------------------------------------------- 物差しと判定（4.2・4.3）
def yardstick(n_months: int, n_tries: int = N_TRIES, n_worlds: int = N_WORLDS, seed: int = SEED) -> dict:
    """何も効いていない世界で、良く見えたものだけを同じ重さで束ねたときの、束の全期間の t 値の分布（50・90・95% の点）。

    1 つの世界: 平均 0・互いに無関係・月の数 n_months の系列を n_tries 本。「前半・後半とも平均がプラス、全期間の t 値 1.65 以上」
    のものだけを選ぶ。選ばれたのが 0 本なら t 値は 0。pass_share は、束が「1 回目として使えそう」に見える世界の割合。
    """
    rng = np.random.default_rng(seed)
    h, root = n_months // 2, math.sqrt(n_months)
    step = max(1, min(n_worlds, 4_000_000 // max(1, n_months * n_tries)))    # 一度に作る世界の数（メモリを使いすぎない）
    ts, passed, picked = [], 0, 0
    for start in range(0, n_worlds, step):
        x = rng.standard_normal((min(step, n_worlds - start), n_months, n_tries))
        sel = ((x[:, :h].mean(1) > 0) & (x[:, h:].mean(1) > 0) & (x.mean(1) / (x.std(1, ddof=1) / root) >= W.T_MIN))
        k = sel.sum(1)
        port = (x * sel[:, None, :]).sum(2) / np.maximum(k, 1)[:, None]
        some = k > 0
        t = np.zeros(len(k))
        t[some] = port[some].mean(1) / (port[some].std(1, ddof=1) / root)
        b = port[some][:, h:]
        t2 = b.mean(1) / (b.std(1, ddof=1) / math.sqrt(n_months - h))
        passed += int(((port[some][:, :h].mean(1) > 0) & (b.mean(1) > 0) & (t[some] >= W.T_MIN) & (t2 >= W.T_MIN)).sum())
        picked += int(k.sum())
        ts.append(t)
    t = np.concatenate(ts)
    return {"q50": float(np.quantile(t, 0.50)), "q90": float(np.quantile(t, 0.90)), "q95": float(np.quantile(t, 0.95)),
            "pass_share": passed / n_worlds, "picked_mean": picked / n_worlds}


def verdict(st: dict, y95: float) -> str:
    """4.2 の 3 つの言葉。物差しの 95% の点（y95）に届いても、1 回目としての線（1.65）は必ず要る。"""
    if st["n"] < base.MIN_MONTHS:
        return W.SHORT
    if st["mean1"] > 0 and st["mean2"] > 0 and st["t"] >= W.T_MIN:
        return BEYOND if st["t2"] >= W.T_MIN and st["t"] >= y95 else LOOKS_GOOD
    return USELESS


# --------------------------------------------------------------------------- 点検（4.1。判定しない）
def family_series(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> dict[str, pd.Series]:
    """家族ごとの月の系列（3 章の重さを、家族の中だけで使ったもの）。"""
    return {f: bundle({m: series[m] for m in ms}, {f: tuple(ms)}) for f, ms in _present(series, families).items()}


def correlations(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> pd.DataFrame:
    """家族どうしの相関の表（どちらにも値のある月で）。"""
    return pd.DataFrame(family_series(series, families)).corr()


def leave_one_out(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> pd.DataFrame:
    """家族を 1 つずつ抜いた束の数字（行 = 抜いた家族）。"""
    fam = _present(series, families)
    rows = {}
    for f in fam:
        rest = {g: tuple(ms) for g, ms in fam.items() if g != f}
        rows[f] = stats(bundle({m: series[m] for ms in rest.values() for m in ms}, rest))
    return pd.DataFrame(rows).T
