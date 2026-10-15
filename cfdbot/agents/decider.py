"""判断 AI: 銘柄選定の点数と IR 評価の点数（どちらもファイル）だけを見て、買う銘柄を決め、理由を記録する。docs/agents.md。"""

from __future__ import annotations

import numpy as np
import pandas as pd

TOP_SHARE, MIN_VALID = 0.10, 50        # 銘柄選定・足した点数: 上位 10%（値のある銘柄が 50 未満の日は買わない）
IR_TOP, IR_MIN = 0.20, 5               # IR だけ: 開示のあった株の上位 20%（5 社未満の日は買わない）
E1_UP = 0.05                           # E1 の帳: 通期の純利益の予想を 5% 以上引き上げた（docs/agents.md 10.2 と同じ）
RANDOM_BOOK, E1_BOOK = "random", "e1"  # 台帳に足す帳（docs/agents.md 12 章）


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


def random_pick(eligible: list[str], k: int, day: pd.Timestamp) -> list[str]:
    """でたらめの帳（docs/agents.md 12 章）: その日に値のある銘柄から k 個を、日付から作った種で選ぶ（同じ日なら何度でも同じ）。"""
    names = sorted(eligible)
    k = min(int(k), len(names))
    if k <= 0:
        return []
    rng = np.random.default_rng(int(pd.Timestamp(day).strftime("%Y%m%d")))
    return sorted(rng.choice(names, size=k, replace=False).tolist())


def e1_pick(revisions: pd.DataFrame, day: pd.Timestamp, eligible: list[str], up: float = E1_UP) -> pd.DataFrame:
    """E1 の帳: その日の判断に使える開示で、通期の純利益の予想を up 以上引き上げた株（銘柄選定の点数のある株だけ）。

    列: ticker / fc_rev / fc_op_rev（営業利益の予想の変化率。本業の印に使うだけ。表に無ければ NaN）。
    """
    cols = ["ticker", "fc_rev", "fc_op_rev"]
    if revisions is None or revisions.empty:
        return pd.DataFrame(columns=cols)
    r = revisions[(pd.to_datetime(revisions["date"]) == pd.Timestamp(day)) & (revisions["fc_rev"] >= up)
                  & revisions["ticker"].isin(set(eligible))]
    r = r.assign(fc_op_rev=r["fc_op_rev"] if "fc_op_rev" in r else np.nan)
    return r.drop_duplicates("ticker")[cols].sort_values("ticker").reset_index(drop=True)


CORE_UP = 0.05     # 本業の印: 通期の営業利益の予想も 5% 以上の引き上げ（docs/text_signals.md 6 章。帳の中身は変えない・判定しない）


def e1_reason(fc_rev: float, fc_op_rev: float | None) -> str:
    """E1 の行の理由の文（本業の印つき）。台帳の列は増やさず、理由の文に入れる。"""
    base = f"通期の純利益の会社予想 {fc_rev:+.0%}（E1: 5% 以上の引き上げ）"
    if fc_op_rev is None or not np.isfinite(fc_op_rev):
        return base + "【本業: 不明（営業利益の予想の変化が分からない）】"
    if fc_op_rev >= CORE_UP:
        return base + f"【本業: 営業利益の予想も {fc_op_rev:+.0%}】"
    return base + f"【本業以外: 営業利益の予想は {fc_op_rev:+.0%}】"


def tradable_today(open_: pd.DataFrame, close: pd.DataFrame) -> tuple[pd.Timestamp | None, list[str]]:
    """銘柄選定の点数が付く株（その日に終値と、前日の引け → 寄りの値がある株）と、その最後の日。

    銘柄選定（selector.scores）は、この印のある所だけに点数を付ける（cfdbot/stocks_short.py の walk_forward）。
    だから E1 の帳に要る「点数のある株」は、学び直しを待たずに値段だけで分かる（docs/stocks_live.md 4.2）。
    """
    from .. import stocks_short as H

    ok = close.notna() & H.parts(open_, close)["night"].notna()
    rows = ok.any(axis=1).to_numpy()
    if not rows.any():
        return None, []
    day = ok.index[rows][-1]
    return day, ok.columns[ok.loc[day].to_numpy()].tolist()


def e1_preview(open_: pd.DataFrame, close: pd.DataFrame, revisions: pd.DataFrame | None, names: dict[str, str]) -> tuple[pd.Timestamp | None, pd.DataFrame]:
    """E1 の帳を、銘柄選定の学び直しの前に作る（同じ作り・同じ入力。台帳の E1 の帳は今までどおり判断のときに書く）。列: ticker / name / reason。"""
    day, eligible = tradable_today(open_, close)
    if day is None:
        return None, pd.DataFrame(columns=["ticker", "name", "reason"])
    r = e1_pick(revisions, day, eligible)
    return day, pd.DataFrame({"ticker": r["ticker"], "name": [names.get(t, t) for t in r["ticker"]],
                              "reason": [e1_reason(v, o) for v, o in zip(r["fc_rev"], r["fc_op_rev"])]})


def extra_rows(day: pd.Timestamp, selector_row: pd.Series, n_selector: int, revisions: pd.DataFrame | None,
               names: dict[str, str]) -> pd.DataFrame:
    """台帳に足す 2 つの帳（でたらめ・E1）の行。列は explain の結果と同じ（date / ticker / book / selector / ir / combined / name / reason）。"""
    eligible = selector_row.dropna().index.tolist()
    rows = []
    for t in random_pick(eligible, n_selector, day):
        rows.append((day, t, RANDOM_BOOK, selector_row.get(t), np.nan, np.nan, names.get(t, t),
                     f"でたらめ（種 {pd.Timestamp(day):%Y%m%d}、{len(eligible)} 銘柄から {n_selector} 個）"))
    for r in e1_pick(revisions, day, eligible).itertuples():
        rows.append((day, r.ticker, E1_BOOK, selector_row.get(r.ticker), np.nan, np.nan, names.get(r.ticker, r.ticker),
                     e1_reason(r.fc_rev, r.fc_op_rev)))
    return pd.DataFrame(rows, columns=["date", "ticker", "book", "selector", "ir", "combined", "name", "reason"])


def explain(day_rows: pd.DataFrame, ir_day: pd.DataFrame, names: dict[str, str]) -> pd.DataFrame:
    """その日の判断に理由を付ける（点数の内訳と、効いた開示の題名）。ir_day は ticker / titles。"""
    titles = dict(zip(ir_day["ticker"], ir_day["titles"])) if len(ir_day) else {}
    out = day_rows.copy()
    out["name"] = [names.get(t, t) for t in out["ticker"]]
    out["reason"] = [f"値動きの点数 {s:+.3f}" + (f"、IR の点数 {i:+.3f}（{titles.get(t, '')}）" if np.isfinite(i) else "、開示なし")
                     for t, s, i in zip(out["ticker"], out["selector"], out["ir"])]
    return out
