"""W1 資産のトレンド（上がっている資産だけ持つ）。docs/wide.md 4 章。

5 つの ETF を、月末の終値が「t を含む直近 10 か月の月末の終値の平均」より上のものだけ翌月持つ（残りは現金）。
比べる相手は 5 つを 5 分の 1 ずつ持ちっぱなし（毎月等分に戻す・コストなし）。
値動きの大きさを、前の月までの直近 36 か月でそろえてから差をとる: d = k ×（G − c）−（B − c）。
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import base

KEY = "W1"
ASSETS = ("SPY", "EFA", "IEF", "GLD", "VNQ")   # 米国株・米国以外の先進国株・米国債 7〜10 年・金・米国の不動産
CASH = "^IRX"                                   # 米国債 13 週の利回り（年率 %）
FILE = Path("wide") / "w1_close.csv"           # data の下。日付 × 6 列
START = "1993-01-01"                            # 取る期間の始まり（5 つがそろうのは 2004 年の終わりごろ）
SMA, WIN, MIN_WIN = 10, 36, 12                  # 平均の月数・倍率を測る月数・倍率を出す最低の月数
K_MIN, K_MAX = 0.5, 3.0
COST = base.COST_US / 2                         # 片道 0.15%（入れ替えた重さに掛ける）


# --------------------------------------------------------------------------- 取得（Mac で）
def _close(d, t: str) -> pd.Series | None:
    """yf.download の結果から、記号 t の終値（日付は時刻なし。同じ日付は 1 つに）。取れていなければ None。"""
    if d is None or d.empty or "Close" not in d:
        return None
    c = d["Close"]
    if isinstance(c, pd.DataFrame):                            # 列が 2 段（Price × Ticker）の版
        c = c[t] if t in c.columns else (c.iloc[:, 0] if c.shape[1] == 1 else None)
    if c is None:
        return None
    c = pd.to_numeric(c, errors="coerce")
    if c.index.tz is not None:
        c.index = c.index.tz_localize(None)
    c.index = pd.DatetimeIndex(c.index).normalize()
    c = c.groupby(level=0).last()
    return c if c.notna().any() else None


def fetch(data: Path, log=print) -> None:
    """Yahoo から 5 つの ETF の調整後終値（配当込み）と ^IRX の終値を 1 つずつ取り、data/wide/w1_close.csv に書く。"""
    import yfinance as yf  # type: ignore

    tickers, got, first = [*ASSETS, CASH], {}, True
    for t in tickers:
        for k in range(3):                                     # 1 つずつ・1 秒に 1 件まで。だめなら間をあけて 3 回まで
            if not first:
                time.sleep(1.0 if k == 0 else 30.0 * k)
            first = False
            try:
                c = _close(yf.download([t], start=START, auto_adjust=True, progress=False, threads=False,
                                       group_by="column"), t)
            except Exception:  # noqa: BLE001  回線・Yahoo の不調は種類を問わずやり直す
                c = None
            if c is not None:
                got[t] = c
                break
            log(f"  W1: Yahoo から {t} が取れない（{k + 1} 回目）")
        else:
            raise RuntimeError(f"Yahoo から W1 の {t} が取れない（3 回試した）。時間をおいて ./cfd wide --fetch")
    close = pd.DataFrame(got)[tickers].sort_index()
    close.index.name = "date"
    close = close.dropna(how="all")
    out = data / FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    close.to_csv(tmp)
    tmp.replace(out)
    log(f"  W1: {out}（{len(close)} 日、{close.index[0]:%Y-%m-%d}〜{close.index[-1]:%Y-%m-%d}）")


# --------------------------------------------------------------------------- 読み取りと形の点検
def _load(data: Path) -> pd.DataFrame:
    path = data / FILE
    if not path.exists():
        raise base.NotReady(f"{path} が無い。先に ./cfd wide --fetch")
    try:
        df = pd.read_csv(path, index_col=0)
        df.index = pd.to_datetime(df.index)
        df = df[df.index.notna()]
    except Exception as e:                                    # 壊れたファイル・日付でない行
        raise base.NotReady(f"{path} を読めない（{type(e).__name__}）。取り直す（./cfd wide --fetch）") from e
    miss = [c for c in (*ASSETS, CASH) if c not in df.columns]
    if miss:
        raise base.NotReady(f"{path} に列が無い: {'、'.join(miss)}（{'・'.join((*ASSETS, CASH))} の 6 列のはず）")
    df = df[[*ASSETS, CASH]].apply(pd.to_numeric, errors="coerce").sort_index(kind="stable")   # 同じ日付はファイルの順のまま
    if df.empty or df[list(ASSETS)].isna().all().any():
        raise base.NotReady(f"{path} に値の無い列がある")
    if (df[list(ASSETS)] <= 0).any().any():
        raise base.NotReady(f"{path} に 0 以下の値段がある")
    return df


def _table(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, dict]:
    """月末の値（その月の最後の値）。5 つがそろう最初の月から、終わった月まで。欠けた月があれば NotReady。"""
    last = df.index.max()
    m = df.groupby(df.index.to_period("M")).last()            # 列ごとに、その月の最後の値
    m = m.reindex(pd.period_range(m.index.min(), m.index.max(), freq="M"))   # 行が 1 つも無い月も「欠けた月」として見る
    m = m[m.index < last.to_period("M")]                       # データの最後の日の月は、終わっていないかもしれないので使わない
    m.index = m.index.to_timestamp(how="end").normalize()
    have = m[list(ASSETS)].notna().all(axis=1)
    if not have.any():
        raise base.NotReady("5 つの ETF がそろう終わった月が無い")
    first = have.idxmax()
    m = m.loc[first:]
    gap = m.isna()
    if gap.any().any():
        where = "、".join(f"{c} {gap.index[gap[c]][0]:%Y-%m} など {int(gap[c].sum())} か月" for c in m.columns if gap[c].any())
        raise base.NotReady(f"月末の値が 1 つも無い月がある（{where}）。取り直す（./cfd wide --fetch）")
    if not m[CASH].between(-1.0, 25.0).all():
        raise base.NotReady(f"{CASH} が年率 %（-1〜25）に見えない値を含む（単位の取り違えかもしれない）")
    return m[list(ASSETS)], m[CASH], {"first": first, "last": last, "months": len(m), "end": m.index[-1]}


# --------------------------------------------------------------------------- 計算
def _series(px: pd.DataFrame, irx: pd.Series, sma: int = SMA, win: int = WIN, min_win: int = MIN_WIN) -> pd.DataFrame:
    """月末の値（px は資産ごと、irx は年率 %。月が続いていること）から、得た月ごとの G・B・c・入れ替え・k・d。

    月末 t に決めて、月 t+1 に持つ。行は得た月（t+1）の月末。k は前の月までの直近 win か月（min_win か月以上）で測る。
    """
    n = px.shape[1]
    avg = px.rolling(sma, min_periods=sma).mean()             # t を含む直近 sma か月の月末の平均
    ready = avg.notna().all(axis=1)
    w = (px > avg).astype(float).div(n)                       # 月末 t に決めた重さ
    w.loc[~ready] = np.nan                                    # 10 か月そろわない月は決められない
    held = w.shift(1)                                         # 月 t に持つ重さ（t-1 月末に決めた）
    on = held.notna().all(axis=1)
    turn = (held - held.shift(1).fillna(0.0)).abs().sum(axis=1)   # 最初の月は買い始めの分（0 からの変化）
    r = px.pct_change(fill_method=None)                       # 月 t のリターン（t-1 月末 → t 月末）
    c = irx.shift(1) / 100 / 12                               # 月 t の現金のリターン（t-1 月末の ^IRX）
    g = (held * r).sum(axis=1, min_count=n) + (1 - held.sum(axis=1)) * c - COST * turn
    b = r.mean(axis=1)
    out = pd.DataFrame({"g": g, "b": b, "c": c, "turn": turn})[on]
    gx, bx = out["g"] - out["c"], out["b"] - out["c"]
    sg = gx.rolling(win, min_periods=min_win).std().shift(1)  # 前の月まで
    sb = bx.rolling(win, min_periods=min_win).std().shift(1)
    k = (sb / sg.where(sg > 0)).clip(K_MIN, K_MAX)
    k = k.mask(sg == 0, K_MAX).where(sg.notna() & sb.notna())  # G の標準偏差が 0 なら 3.0
    out["k"] = k
    out["d"] = k * gx - bx
    return out


def _ready(data: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """読み取り → 月末の表 → d を作れる月だけの表。compute と check が同じ道を通る（止まる理由も同じ）。"""
    df = _load(data)
    px, irx, info = _table(df)
    s = _series(px, irx)
    s = s.loc[s["d"].first_valid_index():] if s["d"].notna().any() else s.iloc[:0]
    s = s.loc[base.finished_months(s["d"], info["last"]).index]
    if s.empty:
        raise base.NotReady(f"倍率 k を出せる月が無い（5 つがそろう {info['first']:%Y-%m} から {info['months']} か月しかない）")
    if s["d"].isna().any():
        raise base.NotReady("d の途中に計算できない月がある（データの形を確かめる）")
    return df, s, info


def compute(data: Path) -> base.Stream:
    _, s, info = _ready(data)
    d = base.fill_months(s["d"], s.index[0], s.index[-1])
    gross = float((s["d"] + s["k"] * COST * s["turn"]).reindex(d.index).mean())   # コストを引く前（同じ k で）
    notes = [
        (f"期間 {d.index[0]:%Y-%m}〜{d.index[-1]:%Y-%m}（{len(d)} か月）。5 つの ETF がそろう最初の月 {info['first']:%Y-%m}、"
         f"データの最後の日 {info['last']:%Y-%m-%d}"),
        f"{'・'.join(ASSETS)} の調整後終値（配当込み）。現金は {CASH}（月末 t の年率 % ÷ 100 ÷ 12 を翌月に）",
        (f"比べる相手は 5 つを 5 分の 1 ずつ（毎月戻す・コストなし）。コストは入れ替えた重さ × {COST:.2%}（片道）。"
         f"倍率 k は前の月までの直近 {WIN} か月（{MIN_WIN} か月以上）で測り {K_MIN}〜{K_MAX} に切る"),
    ]
    return base.Stream(KEY, d, events=None, gross=gross, notes=notes)


def check(data: Path) -> base.Check:
    path = data / FILE
    try:
        df, s, info = _ready(data)
    except base.NotReady as e:
        return base.Check(KEY, False, [f"ファイル {path}"], why=str(e))
    since = "、".join(f"{c} {df[c].first_valid_index():%Y-%m-%d}〜" for c in (*ASSETS, CASH))
    lines = [
        f"ファイル {path}（{len(df)} 行、{df.index[0]:%Y-%m-%d}〜{info['last']:%Y-%m-%d}）",
        f"始まり: {since}",
        f"5 つの ETF がそろう最初の月 {info['first']:%Y-%m}。終わった月は {info['months']} か月（〜{info['end']:%Y-%m}）。欠けた月なし",
        f"d を作れる月 {s.index[0]:%Y-%m}〜{s.index[-1]:%Y-%m}（{len(s)} か月）",
    ]
    return base.Check(KEY, True, lines)
