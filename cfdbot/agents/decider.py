"""判断 AI: 銘柄選定の点数と IR 評価の点数（どちらもファイル）だけを見て、買う銘柄を決め、理由を記録する。docs/agents.md。"""

from __future__ import annotations

import numpy as np
import pandas as pd

TOP_SHARE, MIN_VALID = 0.10, 50        # 銘柄選定・足した点数: 上位 10%（値のある銘柄が 50 未満の日は買わない）
IR_TOP, IR_MIN = 0.20, 5               # IR だけ: 開示のあった株の上位 20%（5 社未満の日は買わない）


def ir_matrix(ir: pd.DataFrame, index: pd.DatetimeIndex, columns) -> pd.DataFrame:
    """IR の点数（date / ticker / score）を 日 × 株 の表に。開示の無い所は NaN。"""
    ok = ir.dropna(subset=["score"])
    wide = ok.pivot_table(index="date", columns="ticker", values="score", aggfunc="mean")
    return wide.reindex(index=index, columns=columns)


def top_rows(score: np.ndarray, share: float, min_valid: int) -> np.ndarray:
    """日ごとに、値のある銘柄の上位 share を選ぶ（min_valid 未満の日は選ばない）。"""
    ok = np.isfinite(score)
    n = ok.sum(axis=1)
    k = np.where(n >= min_valid, np.maximum(np.round(n * share), 1), 0).astype(int)
    order = np.argsort(np.where(ok, -score, np.inf), axis=1, kind="stable")
    rank = np.empty_like(order)
    np.put_along_axis(rank, order, np.arange(score.shape[1])[None, :], axis=1)
    return (rank < k[:, None]) & ok


def combined_score(selector: pd.DataFrame, ir: pd.DataFrame) -> pd.DataFrame:
    """点数 = 銘柄選定の予測 ＋ IR 評価の予測（開示の無い株は 0）。銘柄選定の点数が無い株は対象外。"""
    return selector + ir.reindex_like(selector).fillna(0.0)


def books(selector: pd.DataFrame, ir: pd.DataFrame) -> dict[str, np.ndarray]:
    """3 つの買い方: 銘柄選定だけ・足した点数（判断 AI の本番）・IR だけ。"""
    s = selector.to_numpy(dtype=float)
    i = ir.reindex_like(selector).where(selector.notna()).to_numpy(dtype=float)
    return {"selector": top_rows(s, TOP_SHARE, MIN_VALID),
            "combined": top_rows(combined_score(selector, ir).to_numpy(dtype=float), TOP_SHARE, MIN_VALID),
            "ir": top_rows(i, IR_TOP, IR_MIN)}


def decisions_table(bk: dict[str, np.ndarray], selector: pd.DataFrame, ir: pd.DataFrame, start: str) -> pd.DataFrame:
    """買いの記録（date / ticker / book / selector / ir / combined）。start より前は書かない。"""
    i = ir.reindex_like(selector)
    comb = combined_score(selector, ir)
    first = int(selector.index.searchsorted(pd.Timestamp(start)))
    parts = []
    for name, sel in bk.items():
        t, j = np.nonzero(sel[first:])
        t = t + first
        parts.append(pd.DataFrame({"date": selector.index[t], "ticker": selector.columns[j], "book": name,
                                   "selector": selector.to_numpy()[t, j], "ir": i.to_numpy()[t, j], "combined": comb.to_numpy()[t, j]}))
    return pd.concat(parts, ignore_index=True)


def explain(day_rows: pd.DataFrame, ir_day: pd.DataFrame, names: dict[str, str]) -> pd.DataFrame:
    """その日の判断に理由を付ける（点数の内訳と、効いた開示の題名）。ir_day は ticker / titles。"""
    titles = dict(zip(ir_day["ticker"], ir_day["titles"])) if len(ir_day) else {}
    out = day_rows.copy()
    out["name"] = [names.get(t, t) for t in out["ticker"]]
    out["reason"] = [f"値動きの点数 {s:+.3f}" + (f"、IR の点数 {i:+.3f}（{titles.get(t, '')}）" if np.isfinite(i) else "、開示なし")
                     for t, s, i in zip(out["ticker"], out["selector"], out["ir"])]
    return out
