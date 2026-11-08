"""向こう N 日で上がる確率の順位表（docs/chance.md）。

M2 と同じ 24 個の入力から、「次の営業日の引けで買い、N 営業日後の引けで売ったとき値段が上がっているか」を当てる分類の AI。
2010 年から毎年、その年より前だけで学び直す（台帳の M2 と同じ切り方。答えが年をまたがないよう N＋2 営業日あける）。
過去の確率はどれも答えを見ていない部品の数字なので、「AI が 55% と言った行は、実際に何 % 上がったか」を数えられる。
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from .. import stocks as S
from .. import stocks_short as H

JUDGED = {5: ("RK5", 89), 20: ("RK20", 90)}    # 事前登録した日数 → 記号・試した数（docs/chance.md 3 章）
RESULT = {5: ("使えない", "2026-11-08", "コストの後 年 −2.8%、t −1.58"),   # 1 回だけ計算した判定（docs/chance.md 8 章）。
          20: ("使えない", "2026-11-08", "コストの後 年 +0.2%、t 0.08")}   # 書いてあれば rank --test は動かさない
TOP, DELAY, MAX_DAYS = 10, 1, 60
COST = H.COST                                   # 往復 0.10%
START, SPLIT = "2010-01-01", "2018-01-01"
PLACEBO = 300
EDGES = (0.40, 0.45, 0.50, 0.55, 0.60, 0.65)    # 確率の帯（4 章）
BIN_LABELS = ("40% 未満", "40〜45%", "45〜50%", "50〜55%", "55〜60%", "60〜65%", "65% 以上")
CAL_TOL, CAL_MIN_SHARE = 0.03, 0.01             # 「そのまま読んでよい」の線: 差 3 ポイント・行の 1% 以上の帯
PAST_MIN = 1000                                 # 表の「過去に同じ帯で上がった割合」を出す最少の行


# --------------------------------------------------------------------------- 答えと学び方
def answers(close: pd.DataFrame, days: int, delay: int = DELAY) -> tuple[pd.DataFrame, pd.DataFrame]:
    """t 日の行の答え: t+delay 日の引けで買い、t+delay+days 日の引けで売る。上がれば 1、同じか下がれば 0（分からなければ NaN）と、その対数の値動き。"""
    buy, sell = close.shift(-delay), close.shift(-(delay + days))
    ok = buy.notna() & sell.notna() & (buy > 0) & (sell > 0)
    up = (sell > buy).astype(np.float32).where(ok)
    ret = np.log(sell.where(ok) / buy.where(ok))
    return up, ret


def tradable_of(open_: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    """その日に寄りと引けの値段がある（M2 と同じ）。"""
    return close.notna() & H.parts(open_, close)["night"].notna()


def embargo(days: int, delay: int = DELAY) -> int:
    """学ぶ行と判定の年のあいだにあける営業日。t 日の答えは t+delay+days 日の引けまで使うので、その分＋1。"""
    return days + delay + 1


def walk_forward(x: np.ndarray, up: pd.DataFrame, tradable: np.ndarray, days: int, first_year: int = H.FIRST_TEST_YEAR,
                 delay: int = DELAY, params: dict | None = None, log: Callable[[str], None] = print) -> tuple[pd.DataFrame, dict]:
    """毎年、その年より前（embargo 営業日ぶん手前まで）だけで学び、その年の売買できる行すべてに確率を付ける。

    返り値: 確率（日 × 株。付けない所は NaN）と、年ごとの {n_train, base（学習の期間に上がった割合）, train_end}。
    """
    from sklearn.ensemble import HistGradientBoostingClassifier

    idx = up.index
    yv = up.to_numpy(dtype=np.float32)
    prob = np.full(yv.shape, np.nan, dtype=np.float32)
    info: dict[int, dict] = {}
    gap = embargo(days, delay)
    for year in range(first_year, idx[-1].year + 1):
        test = np.flatnonzero(idx.year == year)
        if len(test) == 0:
            continue
        end = max(test[0] - gap, 0)
        tr = tradable[:end] & np.isfinite(yv[:end])
        if tr.sum() < 10_000:
            continue
        xt, yt = x[:end][tr], yv[:end][tr]
        if yt.min() == yt.max():
            continue
        use = np.isfinite(xt).any(axis=0)                 # 学習の期間に 1 つも値の無い入力は外す（M2 と同じ）
        model = HistGradientBoostingClassifier(**(params or H.GBM_PARAMS)).fit(xt[:, use], yt.astype(np.int8))
        te = tradable[test]
        block = np.full(te.shape, np.nan, dtype=np.float32)
        if te.any():
            block[te] = model.predict_proba(x[test][te][:, use])[:, 1]
        prob[test] = block
        info[year] = {"n_train": int(tr.sum()), "base": float(yt.mean()), "train_end": idx[end - 1]}
        log(f"  {year}: 学習 {int(tr.sum()):,} 件（{idx[end - 1]:%Y-%m-%d} まで）→ 確率 {int(te.sum()):,} 件")
    return pd.DataFrame(prob, index=idx, columns=up.columns), info


def probabilities(open_: pd.DataFrame, close: pd.DataFrame, days: int, params: dict | None = None,
                  log: Callable[[str], None] = print, x: np.ndarray | None = None) -> dict:
    """値段から、確率・答え・値動き・売買できる印・年ごとの情報をまとめて作る。x（H.features の入力）を渡せば作り直さない。"""
    if x is None:
        _, x = H.features(open_, close)
    up, ret = answers(close, days)
    tradable = tradable_of(open_, close)
    prob, info = walk_forward(x, up, tradable.to_numpy(), days, params=params, log=log)
    return {"prob": prob, "up": up, "ret": ret, "tradable": tradable, "info": info, "days": days}


# --------------------------------------------------------------------------- 過去の当たり具合（4 章）
def halves(index: pd.DatetimeIndex) -> dict[str, np.ndarray]:
    t = pd.DatetimeIndex(index)
    a = (t >= pd.Timestamp(START)) & (t < pd.Timestamp(SPLIT))
    b = t >= pd.Timestamp(SPLIT)
    return {"全期間": a | b, "前半": a, "後半": b}


def calibration(prob: pd.DataFrame, up: pd.DataFrame, ret: pd.DataFrame, days_mask: np.ndarray) -> pd.DataFrame:
    """帯ごとの行の数・割合・AI の確率の平均・実際に上がった割合・値動きの平均（答えの分かる行だけ）。"""
    p, u, r = prob.to_numpy(dtype=float), up.to_numpy(dtype=float), ret.to_numpy(dtype=float)
    ok = np.isfinite(p) & np.isfinite(u) & np.asarray(days_mask)[:, None]
    df = pd.DataFrame({"bin": np.digitize(p[ok], EDGES), "p": p[ok], "u": u[ok], "r": r[ok]})
    g = df.groupby("bin").agg(rows=("p", "size"), prob=("p", "mean"), up=("u", "mean"), ret=("r", "mean"))
    g = g.reindex(range(len(EDGES) + 1))
    g["rows"] = g["rows"].fillna(0).astype(int)
    g["share"] = g["rows"] / max(int(g["rows"].sum()), 1)
    g["label"] = list(BIN_LABELS)
    return g


def brier(prob: pd.DataFrame, up: pd.DataFrame, info: dict, days_mask: np.ndarray) -> tuple[float, float, int]:
    """ブライアスコア（AI）と、学習の期間に上がった割合をその年の全部の行に言ったもの。行の数も。"""
    p, u = prob.to_numpy(dtype=float), up.to_numpy(dtype=float)
    base_day = np.array([info.get(y, {}).get("base", np.nan) for y in prob.index.year], dtype=float)
    base = np.broadcast_to(base_day[:, None], p.shape)
    ok = np.isfinite(p) & np.isfinite(u) & np.isfinite(base) & np.asarray(days_mask)[:, None]
    if not ok.any():
        return float("nan"), float("nan"), 0
    return float(np.mean((p[ok] - u[ok]) ** 2)), float(np.mean((base[ok] - u[ok]) ** 2)), int(ok.sum())


def readable(cal: dict[str, pd.DataFrame], scores: dict[str, tuple[float, float, int]]) -> tuple[bool, list[str]]:
    """4 章の「そのまま読んでよい」か（前半・後半とも）と、満たさなかった理由。"""
    why: list[str] = []
    for half in ("前半", "後半"):
        g = cal[half]
        big = g[g["share"] >= CAL_MIN_SHARE]
        off = big[(big["up"] - big["prob"]).abs() > CAL_TOL]
        for b in off.itertuples():
            why.append(f"{half}の {b.label}: AI {b.prob:.1%} に対して実際 {b.up:.1%}")
        model, base, n = scores[half]
        if not (n > 0 and model < base):
            why.append(f"{half}のブライアスコア {model:.4f} が、学習の期間の割合を言うだけ（{base:.4f}）より小さくない")
    return not why, why


def hit_rates(prob: pd.DataFrame, up: pd.DataFrame, tradable: pd.DataFrame, top: int = TOP) -> pd.DataFrame:
    """日ごとの、上位 top の上がった割合と全銘柄の上がった割合（答えの分かる行だけ。上位を選べない日は落とす）。"""
    sel = H.top_share(prob, tradable, count=top)
    u = up.to_numpy(dtype=float)
    known = np.isfinite(u) & tradable.to_numpy() & np.isfinite(prob.to_numpy(dtype=float))
    s = sel & known
    k, n = s.sum(axis=1), known.sum(axis=1)
    top_up = np.where(k > 0, np.nansum(np.where(s, u, 0), axis=1) / np.maximum(k, 1), np.nan)
    all_up = np.where(n > 0, np.nansum(np.where(known, u, 0), axis=1) / np.maximum(n, 1), np.nan)
    out = pd.DataFrame({"top": top_up, "all": all_up}, index=prob.index)
    return out[(k > 0) & (out.index >= pd.Timestamp(START))]


# --------------------------------------------------------------------------- 判定（3 章）
def _delayed(sel: np.ndarray, tradable: np.ndarray, days: int = DELAY) -> np.ndarray:
    out = np.zeros_like(sel)
    out[days:] = sel[:-days]
    return out & tradable


def evaluate(prob: pd.DataFrame, open_: pd.DataFrame, close: pd.DataFrame, days: int, trials: int, placebo: int = PLACEBO,
             seed: int = 11, top: int = TOP) -> dict:
    """上位 top を次の営業日の引けで買い days 日持つ（毎日 1 組を重ねる）形の、全銘柄の平均との差と判定。"""
    cc = H.parts(open_, close)["cc"]
    tradable_df = tradable_of(open_, close)
    tradable = tradable_df.to_numpy()
    m = np.asarray(cc.index >= pd.Timestamp(START))
    sel = H.top_share(prob.reindex(index=close.index, columns=close.columns), tradable_df, count=top)

    def weights(s: np.ndarray) -> np.ndarray:
        return H.cohort_weights(_delayed(s, tradable), hold=days)

    pnl = H.weights_pnl(cc, weights(sel), COST)[m]
    rng = np.random.default_rng(seed)
    null = np.array([H.weights_pnl(cc, weights(H.random_like(sel, tradable, rng)))["gross"][m].mean() for _ in range(placebo)])
    real = float(pnl["gross"].mean())
    pl = float(np.mean(null < real)) if len(null) else None
    j = S.judge(S.month_sum(pnl["d"]), SPLIT, trials, pl)
    j.update(gross_ann=real * 252, net_ann=float(pnl["d"].mean() * 252), turnover=float(pnl["turnover"].mean()),
             by_year=pnl["d"].groupby(pnl.index.year).sum(), period=(cc.index[m][0], cc.index[m][-1]) if m.any() else (None, None))
    return j


# --------------------------------------------------------------------------- 今日の順位（5 章）
def latest_day(prob: pd.DataFrame) -> pd.Timestamp | None:
    """確率が H.MIN_VALID 銘柄以上に付いている最後の日。"""
    n = prob.notna().sum(axis=1)
    ok = n[n >= H.MIN_VALID]
    return ok.index[-1] if len(ok) else None


def ranking(prob: pd.DataFrame, day: pd.Timestamp, cal: pd.DataFrame, names: dict[str, str]) -> pd.DataFrame:
    """day の全銘柄を確率の高い順に。過去に同じ帯だった行の上がった割合・値動きの平均を横に付ける。"""
    s = prob.loc[day].dropna().astype(float).sort_values(ascending=False, kind="stable")
    b = np.digitize(s.to_numpy(), EDGES)
    past = cal.reindex(b)
    enough = past["rows"].to_numpy() >= PAST_MIN
    return pd.DataFrame({"rank": np.arange(1, len(s) + 1), "ticker": s.index, "name": [names.get(t, t) for t in s.index],
                         "prob": s.to_numpy(), "bin": [BIN_LABELS[i] for i in b],
                         "past_up": np.where(enough, past["up"].to_numpy(), np.nan), "past_rows": past["rows"].to_numpy(),
                         "past_ret": np.where(enough, past["ret"].to_numpy(), np.nan)})
