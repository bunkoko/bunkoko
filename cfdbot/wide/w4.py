"""W4 配当の権利落ちの日。docs/wide.md 4 章 W4。

権利付きの最後の日の引けで買い、権利落ちの日の引けで売る。値段は配当込みの調整後終値なので、その日のリターンに配当が入る。
事象 =（銘柄, 権利落ちの日）。権利落ちの日は data/stocks/jp_div.csv の date（同じ銘柄・同じ日は 1 件）。
事象の値 = 権利落ちの日の株のリターン − その日に権利落ちでない株（その日にリターンのある株）の平均リターン − 往復 0.10%。
権利落ちでない株が 30 に届かない日は、その日の事象をすべて使わない。
d = 日ごとに事象の値を平均し、月の中で足す（事象の無い月は 0）。最初に使った事象の月から、終わった最後の月まで。税金は入れない。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .. import stocks as S
from . import base
from .w3 import FILE as CLOSE_FILE
from .w3 import _days, _last_month, _list, _stale, read_close

KEY = "W4"
FILE = Path("stocks") / "jp_div.csv"       # data の下。列 date・ticker（・amount）。date が権利落ちの日
MIN_CTRL = 30                              # その日に権利落ちでない株がこれに届かない日は使わない
COST = base.COST_JP                        # 往復 0.10%（事象ごと）
ORDER = ("no_ticker", "before", "after", "unfinished", "holiday", "no_return", "few_ctrl")


def _why(state: str) -> str:
    return {"no_ticker": "値段の表に無い銘柄", "before": "値段の表の最初の日より前", "after": "値段の表の最後の日より後",
            "unfinished": "終わっていない月", "holiday": "値段の表で営業日でない日",
            "no_return": "その日のリターンが無い（その日か前の営業日の値段が無い）",
            "few_ctrl": f"権利落ちでない株が {MIN_CTRL} に届かない日"}[state]


# --------------------------------------------------------------------------- 読み取りと形の点検
def read_div(data: Path) -> tuple[pd.DataFrame, int]:
    """権利落ちの日の表（date・ticker）と、同じ銘柄・同じ日の重なりの数。無い・読めない・形が違うなら NotReady。"""
    path = Path(data) / FILE
    if not path.exists():
        raise base.NotReady(f"権利落ちの日の表 {path} が無い（./cfd stocks --fetch）")
    try:
        t = pd.read_csv(path, dtype=str)
    except Exception as e:  # 空・壊れたファイル
        raise base.NotReady(f"権利落ちの日の表 {path} を読めない（{type(e).__name__}）") from e
    miss = [c for c in ("date", "ticker") if c not in t.columns]
    if miss:
        raise base.NotReady(f"{path} に列が無い: {'、'.join(miss)}（列: {list(t.columns)}）")
    if t.empty:
        raise base.NotReady(f"{path} に 1 行も無い")
    raw = t["date"].fillna("").str.strip()
    day = _days(raw)                                            # 時差の付いた日付は、その土地の日付のまま使う
    if day.isna().any():
        raise base.NotReady(f"{path} に日付（2020-03-27 の形）として読めない行がある（{_list(raw[day.isna()].map(repr), 5)}）")
    tk = t["ticker"].fillna("").str.strip()
    if (tk == "").any():
        raise base.NotReady(f"{path} に銘柄の空いた行がある（{int((tk == '').sum())} 行）")
    ev = pd.DataFrame({"date": day, "ticker": tk.to_numpy()})
    dup = int(ev.duplicated().sum())
    return ev.drop_duplicates().sort_values(["date", "ticker"]).reset_index(drop=True), dup


# --------------------------------------------------------------------------- 計算
def _events(close: pd.DataFrame, ev: pd.DataFrame, min_ctrl: int) -> pd.DataFrame:
    """事象ごとに、株のリターン（ret）・その日に権利落ちでない株の平均（ctrl）と数（n_ctrl）・扱い（state）を付ける。

    state が used のものだけ使う。ほかは ORDER の順に、最初にあてはまった理由。
    その日に権利落ちの株は、使えない事象（リターンが無いなど）でも、比べる相手からは外す。
    """
    r = close.pct_change(fill_method=None).clip(-S.CLIP, S.CLIP).to_numpy()
    cols = pd.Index(close.columns.astype(str))
    ri, ci = close.index.get_indexer(ev["date"]), cols.get_indexer(ev["ticker"])
    on = (ri >= 0) & (ci >= 0)
    ex = np.zeros(r.shape, dtype=bool)
    ex[ri[on], ci[on]] = True
    ctrl = np.isfinite(r) & ~ex
    n_ctrl = ctrl.sum(axis=1)
    mean_ctrl = np.where(ctrl, r, 0.0).sum(axis=1) / np.maximum(n_ctrl, 1)
    i, j = np.maximum(ri, 0), np.maximum(ci, 0)
    ret = np.where(on, r[i, j], np.nan)
    nc = np.where(ri >= 0, n_ctrl[i], 0)
    day, last = ev["date"].to_numpy(), close.index[-1]
    cut = last.to_period("M").to_timestamp()                    # base.finished_months と同じ区切り
    conds = [ci < 0, day < close.index[0].to_datetime64(), day > last.to_datetime64(), day >= cut.to_datetime64(),
             ri < 0, ~np.isfinite(ret), nc < min_ctrl]
    return ev.assign(ret=ret, ctrl=np.where(ri >= 0, mean_ctrl[i], np.nan), n_ctrl=nc,
                     state=np.select(conds, list(ORDER), "used"))


def compute(data: Path) -> base.Stream:
    close, dropped = read_close(data)
    ev, dup = read_div(data)
    e = _events(close, ev, MIN_CTRL)
    u = e[e["state"] == "used"]
    if u.empty:
        raise base.NotReady("W4 で使える事象が無い（権利落ちの日の表と値段の表の、銘柄・期間が合わない）")
    end = _last_month(close)
    day = (u["ret"] - u["ctrl"]).groupby(u["date"]).mean()     # 日ごとの事象の値の平均（コストの前）

    def monthly(x: pd.Series) -> pd.Series:
        return base.fill_months(x.groupby(x.index.map(base.month_end)).sum(), base.month_end(x.index[0]), end)

    d = monthly(day - COST)                                     # 事象ごとに 0.10% を引いた値の平均 = 平均 − 0.10%
    notes = [
        (f"日本株の調整後終値（配当込み）{close.index[0]:%Y-%m-%d}〜{close.index[-1]:%Y-%m-%d}（{len(close)} 営業日、{close.shape[1]} 銘柄）。"
         f"値の 1 つも無い日と休場日なのに前の日の値のまま入っていた日、合わせて {dropped} 日を落とした"),
        (f"権利落ちの日の表 {len(ev)} 件（{ev['date'].min():%Y-%m-%d}〜{ev['date'].max():%Y-%m-%d}、{ev['ticker'].nunique()} 銘柄）"
         + (f"。同じ銘柄・同じ日の重なり {dup} 件は 1 件にした" if dup else "")),
        (f"使った事象 {len(u)}（{u['date'].nunique()} 日）。d は {d.index[0]:%Y-%m}〜{d.index[-1]:%Y-%m}（{len(d)} か月。事象の無い月は 0）。"
         f"事象ごとに往復 {COST:.2%} を引いた。税金は入れない"),
    ]
    for state in ORDER:
        if (k := int((e["state"] == state).sum())):
            extra = f"（{e.loc[e['state'] == state, 'date'].nunique()} 日）" if state == "few_ctrl" else ""
            notes.append(f"{_why(state)}{extra}の事象 {k} 件は使わない")
    return base.Stream(KEY, d, events=len(u), gross=float(monthly(day).mean()), notes=notes)


def check(data: Path) -> base.Check:
    """手元の 2 つの表だけを見る。期間・件数・使えない事象の理由ごとの数だけで、リターンの数字は出さない。"""
    files = f"ファイル {Path(data) / FILE}・{Path(data) / CLOSE_FILE}"
    try:
        close, dropped = read_close(data)
        ev, dup = read_div(data)
    except base.NotReady as e:
        return base.Check(KEY, False, [files], why=str(e))
    e = _events(close, ev, MIN_CTRL)
    u = e[e["state"] == "used"]
    lines = [
        (f"値段の表 {close.index[0]:%Y-%m-%d}〜{close.index[-1]:%Y-%m-%d}（{len(close)} 営業日、{close.shape[1]} 銘柄。"
         f"値の 1 つも無い日と休場日なのに前の日の値のまま入っていた日、合わせて {dropped} 日は落とす）"),
        (f"権利落ちの日の表 {len(ev)} 件（{ev['date'].min():%Y-%m-%d}〜{ev['date'].max():%Y-%m-%d}、{ev['ticker'].nunique()} 銘柄"
         + (f"。同じ銘柄・同じ日の重なり {dup} 件は 1 件に" if dup else "") + "）"),
    ]
    if (warn := _stale(close)):
        lines.append(warn)
    lines += [f"使わない事象: {_why(s)} {int((e['state'] == s).sum())} 件" for s in ORDER if (e["state"] == s).any()]
    if u.empty:
        return base.Check(KEY, False, lines, why="使える事象が無い（権利落ちの日の表と値段の表の、銘柄・期間が合わない）")
    lines.append(f"使う事象 {len(u)}（{u['date'].nunique()} 日）。d は {u['date'].min():%Y-%m}〜{_last_month(close):%Y-%m}")
    return base.Check(KEY, True, lines)
