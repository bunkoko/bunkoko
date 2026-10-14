"""判断の評価 AI: 判断の記録・点数・開示の一覧・値段（どれもファイル）だけで採点する。docs/agents.md。

ほかの 4 つの AI のモジュールは読み込まない。使うのは、損益の数え方（stocks_short）と判定の条件（stocks）だけ。
選び方の決まり（上位 10% など）も、判断 AI とは別にここに持ち、記録が決まりどおりかを点検する。
"""

from __future__ import annotations

import re
import unicodedata

import numpy as np
import pandas as pd

from ..stocks import CLIP, judge, t_value
from ..stocks_short import cohort_weights, weights_pnl

PRIOR_TRIALS, NEW_TRIALS = 64, 2
COST, HOLD = 0.0010, 5
TOP_SHARE, MIN_VALID, IR_TOP, IR_MIN = 0.10, 50, 0.20, 5
START, SPLIT, STRICT_FROM = "2016-01-01", "2021-01-01", "2025-07-01"
CUTOFF_HOUR = 13
# 参考: AI を使わない分け方（題名の言葉）。上から順に最初に当てはまったもの
KEYWORDS: tuple[tuple[str, str], ...] = (
    ("訂正", r"訂正"), ("上方修正", r"上方修正"), ("下方修正", r"下方修正"), ("増配", r"増配"), ("減配・無配", r"減配|無配"),
    ("自己株式の取得（決定）", r"自己株式.*取得.*(決定|係る事項)|自己株式.*買付"), ("自己株式の取得状況・終了", r"自己株式.*取得(状況|結果|終了)"),
    ("自己株式の消却", r"自己株式.*消却"), ("株式分割", r"株式分割"), ("公開買付け", r"公開買付"),
    ("特別損失・減損", r"特別損失|減損"), ("特別利益", r"特別利益"), ("業績予想の修正（向きは題名に無い）", r"業績予想.*修正|予想.*修正"),
    ("配当予想の修正・配当", r"配当"), ("決算短信", r"決算短信"), ("月次", r"月次"), ("中期経営計画", r"中期経営|中期計画"),
)


def returns(close: pd.DataFrame) -> pd.DataFrame:
    return close.pct_change(fill_method=None).clip(-CLIP, CLIP)


def to_matrix(decisions: pd.DataFrame, book: str, index: pd.DatetimeIndex, columns) -> np.ndarray:
    """買いの記録（date / ticker / book）を 日 × 株 の表に。"""
    d = decisions[decisions["book"] == book]
    out = np.zeros((len(index), len(columns)), dtype=bool)
    t = index.get_indexer(pd.to_datetime(d["date"]))
    j = pd.Index(columns).get_indexer(d["ticker"])
    ok = (t >= 0) & (j >= 0)
    out[t[ok], j[ok]] = True
    return out


def top_k(score: np.ndarray, share: float, min_valid: int) -> np.ndarray:
    ok = np.isfinite(score)
    n = ok.sum(axis=1)
    k = np.where(n >= min_valid, np.maximum(np.round(n * share), 1), 0).astype(int)
    order = np.argsort(np.where(ok, -score, np.inf), axis=1, kind="stable")
    rank = np.empty_like(order)
    np.put_along_axis(rank, order, np.arange(score.shape[1])[None, :], axis=1)
    return (rank < k[:, None]) & ok


def expected_books(selector: np.ndarray, ir: np.ndarray) -> dict[str, np.ndarray]:
    """決まりどおりなら選ばれるはずの銘柄（点数のファイルから計算する）。"""
    ir = np.where(np.isfinite(selector), ir, np.nan)
    return {"selector": top_k(selector, TOP_SHARE, MIN_VALID), "combined": top_k(selector + np.nan_to_num(ir), TOP_SHARE, MIN_VALID),
            "ir": top_k(ir, IR_TOP, IR_MIN)}


def book_pnl(sel: np.ndarray, cc: pd.DataFrame, cost: float = COST, hold: int = HOLD) -> pd.DataFrame:
    """hold 日保有（毎日 1/hold ずつ入れ替え）。gross = 全銘柄の平均との差、d = コストを引いた後。"""
    return weights_pnl(cc, cohort_weights(sel, hold), cost)


def shuffle_rows(values: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """日ごとに、値のある所の中身だけをでたらめに入れ替える（どの株に値があるかは変えない）。"""
    t, j = np.nonzero(np.isfinite(values))
    order = np.lexsort((rng.random(len(t)), t))
    out = np.full(values.shape, np.nan)
    out[t, j] = values[t[order], j[order]]
    return out


def random_among(pool: np.ndarray, k: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """日ごとに、pool の中からでたらめに k 個選ぶ。"""
    r = np.where(pool, rng.random(pool.shape, dtype=np.float32), np.inf)
    order = np.argsort(r, axis=1)
    rank = np.empty_like(order)
    np.put_along_axis(rank, order, np.arange(pool.shape[1])[None, :], axis=1)
    return (rank < k[:, None]) & pool


def verdict(d: pd.Series, placebo_pct: float | None, n_trials: int = PRIOR_TRIALS + NEW_TRIALS) -> dict:
    """docs/agents.md 5 章: 4 つの条件 ＋ 2025-07 以降だけの平均がプラス。"""
    j = judge(d, SPLIT, n_trials, placebo_pct)
    strict = d[d.index >= pd.Timestamp(STRICT_FROM)]
    j["strict_mean"] = float(strict.mean()) if len(strict) else np.nan
    if j["verdict"] == "使えそう" and not j["strict_mean"] > 0:
        j["verdict"] = "まだ判断できない"
    return j


def excess_forward(cc: pd.DataFrame, hold: int = HOLD) -> np.ndarray:
    """t 日の引けから hold 営業日後の引けまでの「株 − 全銘柄の平均」の合計（日 × 株）。先が足りない所は NaN。"""
    ex = cc.sub(cc.mean(axis=1), axis=0).to_numpy()
    cs = np.cumsum(np.nan_to_num(ex), axis=0)
    out = np.full(ex.shape, np.nan)
    out[:-hold] = cs[hold:] - cs[:-hold]
    out[~np.isfinite(ex)] = np.nan
    return out


def decision_pos(times: pd.Series, index: pd.DatetimeIndex) -> np.ndarray:
    """開示の時刻 → 使う営業日の位置（13:00 までならその日、後なら翌日。docs/agents.md 3 章）。"""
    t = pd.to_datetime(times)
    late = (t.dt.hour * 3600 + t.dt.minute * 60 + t.dt.second) > CUTOFF_HOUR * 3600
    return index.searchsorted((t.dt.normalize() + pd.to_timedelta(late.astype(int), unit="D")).to_numpy())


def keyword_of(title: str) -> str:
    t = unicodedata.normalize("NFKC", str(title))
    return next((name for name, pat in KEYWORDS if re.search(pat, t)), "その他")


def keyword_table(disc: pd.DataFrame, cc: pd.DataFrame) -> pd.DataFrame:
    """参考: 題名の言葉で分けた種類ごとの、その後 5 日の「株 − 全銘柄の平均」（START から。株 × 日 × 種類で 1 回と数える）。"""
    index, col = cc.index, {t: k for k, t in enumerate(cc.columns)}
    d = pd.DataFrame({"pos": decision_pos(disc["time"], index), "j": disc["ticker"].map(col),
                      "kind": [keyword_of(t) for t in disc["title"]]}).dropna()
    d = d[d["pos"] < len(index)].astype({"j": int}).drop_duplicates()
    d["ex"] = excess_forward(cc)[d["pos"].to_numpy(), d["j"].to_numpy()]
    d["date"] = index[d["pos"].to_numpy()]
    d = d[(d["date"] >= START) & d["ex"].notna()]
    rows = []
    for kind, g in d.groupby("kind"):
        a, b = g[g["date"] < SPLIT]["ex"], g[g["date"] >= SPLIT]["ex"]
        rows.append({"kind": kind, "n": len(g), "mean": g["ex"].mean(), "t": t_value(g["ex"]), "mean1": a.mean(), "mean2": b.mean()})
    return pd.DataFrame(rows).sort_values("mean", ascending=False).reset_index(drop=True)


def ledger_summary(ledger: pd.DataFrame, cc: pd.DataFrame) -> pd.DataFrame:
    """台帳（その日の 15:25 より前に書かれた判断だけ）を、5 日後の値段で採点する。book ごとの日数・平均。"""
    if ledger.empty:
        return pd.DataFrame(columns=["book", "days", "scored_days", "mean_excess"])
    date = pd.to_datetime(ledger["date"])
    live = pd.to_datetime(ledger["decided_at"]) < date + pd.Timedelta(hours=15, minutes=25)
    lg = ledger[live.to_numpy()].copy()
    ex = excess_forward(cc)
    t = cc.index.get_indexer(pd.to_datetime(lg["date"]))
    j = pd.Index(cc.columns).get_indexer(lg["ticker"])
    lg["ex"] = np.where((t >= 0) & (j >= 0), ex[np.maximum(t, 0), np.maximum(j, 0)], np.nan)
    rows = []
    for book, g in lg.groupby("book"):
        per_day = g.groupby("date")["ex"].mean()
        rows.append({"book": book, "days": int(g["date"].nunique()), "scored_days": int(per_day.notna().sum()),
                     "mean_excess": float(per_day.mean()) if per_day.notna().any() else np.nan})
    return pd.DataFrame(rows)
