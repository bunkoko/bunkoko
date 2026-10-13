"""まだ入れていなかった項目: 出来高・割安さ・会社の大きさ・業種（docs/agents.md 11 章）。

置いてあるのは、Yahoo の日足（終値・出来高・株式分割）の取得と、入力を作る計算だけ。点数を付けるのは selector と earnings、
採点は evaluator。どの入力も、t 日の 13:30 までに分かる値だけで作る（値動き・出来高は前日の引けまで）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

INTERVAL = 1.0                 # Yahoo に頼む間隔（秒）。1 秒に 1 社、同時に 1 本だけ
STALE_DAYS = 400               # 開示の数字を使い続ける長さ（次の開示が無いまま、この日数を超えたら欠け）
AMBIGUOUS_BEFORE, AMBIGUOUS_AFTER = 100, 10    # 開示のこの日数前〜後の株式分割は、株数に入れてあるかどうかを前の開示とのつながりで決める
VOLUME = ["turnover_60", "vol_shock_1", "vol_shock_5", "amihud_60", "vol_trend"]
FUNDAMENTAL = ["ep_fc", "bm", "dy_fc", "size", "equity_ratio", "roe_fc"]
INDUSTRY = ["ind_ret_20", "ind_ret_250", "rel_ind_5"]
LABELS = {"turnover_60": "売買代金の 60 日平均", "vol_shock_1": "前日の出来高 ÷ その前の 50 日の平均", "vol_shock_5": "直近 5 日の出来高 ÷ その前の 50 日の平均",
          "amihud_60": "売買しにくさ", "vol_trend": "出来高の 20 日平均 ÷ 250 日平均", "ep_fc": "予想益回り", "bm": "自己資本 ÷ 時価総額",
          "dy_fc": "予想配当利回り", "size": "時価総額", "equity_ratio": "自己資本比率", "roe_fc": "予想 ROE",
          "ind_ret_20": "業種の 20 日の値動き", "ind_ret_250": "業種の 250 日の値動き", "rel_ind_5": "5 日の値動き − 業種の平均"}


# --------------------------------------------------------------------------- 取得（Yahoo の日足）
def _wait(started: float | None, interval: float) -> float:
    """前の取り始めから interval 秒たつまで待ち、今回の取り始めの時刻を返す。"""
    if started is not None:
        rest = interval - (time.monotonic() - started)
        if rest > 0:
            time.sleep(rest)
    return time.monotonic()


def fetch_raw(tickers: list[str], out_dir: Path, start: str = "2000-01-01", interval: float = INTERVAL,
              history: Callable[[str], pd.DataFrame] | None = None, log: Callable[[str], None] = print) -> tuple[int, int]:
    """1 社ずつ（interval 秒に 1 社）、終値（配当を調整しない）・出来高・株式分割を取って out_dir/<銘柄>.csv に残す。

    取得済みの銘柄は飛ばす（途中で止めても続きから）。空で返ってきた銘柄は残さない（次の回にまた試す）。返すのは（取れた数, 取れなかった数）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if history is None:
        import yfinance as yf  # type: ignore

        def history(t: str) -> pd.DataFrame:
            return yf.Ticker(t).history(start=start, auto_adjust=False, actions=True, timeout=60)
    done = failed = 0
    started: float | None = None
    todo = [t for t in tickers if not (out_dir / f"{t}.csv").exists()]
    for n, t in enumerate(todo):
        started = _wait(started, interval)
        try:
            d = history(t)
        except Exception:  # noqa: BLE001
            d = None
        if d is None or len(d) == 0:
            failed += 1
        else:
            idx = d.index.tz_localize(None) if d.index.tz is not None else d.index
            pd.DataFrame({"date": idx.normalize(), "close": d["Close"].to_numpy(), "volume": d["Volume"].to_numpy(),
                          "split": d["Stock Splits"].to_numpy()}).to_csv(out_dir / f"{t}.csv", index=False)
            done += 1
        if (n + 1) % 50 == 0 or n + 1 == len(todo):
            log(f"  日足 {n + 1}/{len(todo)}（取れた {done}・取れなかった {failed}）")
    return done, failed


def repair_unadjusted(d: pd.DataFrame, tolerance: float = 0.25, min_ratio: float = 0.3) -> tuple[pd.DataFrame, int]:
    """Yahoo が値段をさかのぼって直していない分割を直す。d は 1 社の日足（index = 日付、列 close / volume / split）。

    分割の記録の前後 3 日に、値段が分割の比率どおりに飛んでいれば「直されていない」とみなし、飛んだ日より前の終値を比率で割り、
    出来高に比率を掛け、分割の日付を飛んだ日に移す。返すのは（直した表, 直した数）。
    """
    d = d.copy()
    fixed = 0
    for day, ratio in d["split"][d["split"] > 0].items():
        if abs(np.log(ratio)) < min_ratio:
            continue
        c = d["close"].where(d["close"] > 0).dropna()
        i = int(c.index.searchsorted(day))
        win = c.iloc[max(i - 3, 0):i + 2]
        jump = np.log(win.shift(1) / win).dropna()             # 前の日 ÷ その日
        if jump.empty:
            continue
        k = jump.abs().idxmax()
        if abs(jump[k] - np.log(ratio)) < tolerance:
            before = d.index < k
            d.loc[before, "close"] /= ratio
            d.loc[before, "volume"] *= ratio
            d.loc[day, "split"] = 0.0
            d.loc[k, "split"] = ratio
            fixed += 1
    return d, fixed


def load_raw(out_dir: Path, index: pd.DatetimeIndex, tickers: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """保存した日足を 日 × 株 の表に（index・tickers にそろえる）。返すのは 終値・出来高（0 は欠け）・分割（date / ticker / ratio）。"""
    close, volume, splits = {}, {}, []
    for t in tickers:
        path = out_dir / f"{t}.csv"
        if not path.exists():
            continue
        d = pd.read_csv(path, parse_dates=["date"]).drop_duplicates("date").set_index("date").astype(float)
        d, _ = repair_unadjusted(d)
        close[t], volume[t] = d["close"], d["volume"]
        s = d["split"][d["split"] > 0]
        splits.append(pd.DataFrame({"date": s.index, "ticker": t, "ratio": s.to_numpy()}))
    c = pd.DataFrame(close).reindex(index=index, columns=tickers)
    v = pd.DataFrame(volume).reindex(index=index, columns=tickers)
    sp = pd.concat(splits, ignore_index=True) if splits else pd.DataFrame(columns=["date", "ticker", "ratio"])
    return c.where(c > 0), v.where(v > 0), sp


# --------------------------------------------------------------------------- 出来高
def volume_features(cc: pd.DataFrame, rawclose: pd.DataFrame, volume: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """出来高の入力 5 個（日 × 株）。t 日の行は t−1 日の引けまでの値だけで作る。cc は 1 日の値動き。"""
    vol = volume.where(volume > 0)
    turnover = rawclose * vol                                   # 売買代金（終値も出来高も分割を調整済みなので、掛けると分割の影響が消える）
    v1 = vol.shift(1)
    return {"turnover_60": turnover.shift(1).rolling(60, min_periods=40).mean(),
            "vol_shock_1": v1 / vol.shift(2).rolling(50, min_periods=30).mean(),
            "vol_shock_5": v1.rolling(5, min_periods=3).mean() / vol.shift(6).rolling(50, min_periods=30).mean(),
            "amihud_60": (cc.abs() / turnover).shift(1).rolling(60, min_periods=40).mean(),
            "vol_trend": v1.rolling(20, min_periods=15).mean() / v1.rolling(250, min_periods=200).mean()}


def volume_surge(volume: pd.DataFrame, window: int = 50, top: float = 0.10) -> pd.DataFrame:
    """参考: 前日の出来高が、その日までの window 日の中で上位 top に入っていたか（Gervais らの作り）。"""
    v1 = volume.where(volume > 0).shift(1)
    return v1 >= v1.rolling(window, min_periods=window).quantile(1 - top)


# --------------------------------------------------------------------------- 業種
def industry_features(close: pd.DataFrame, sector: pd.Series) -> dict[str, pd.DataFrame]:
    """業種の入力 3 個。sector は 銘柄 → 業種。値動きは前日の引けまで。"""
    prev = close.shift(1)
    group = sector.reindex(close.columns).fillna("?").to_numpy()

    def ret(k: int) -> pd.DataFrame:
        return np.log(prev / prev.shift(k))

    def mean(df: pd.DataFrame) -> pd.DataFrame:
        return df.T.groupby(group).transform("mean").T

    r5 = ret(5)
    return {"ind_ret_20": mean(ret(20)), "ind_ret_250": mean(ret(250)), "rel_ind_5": r5 - mean(r5)}


# --------------------------------------------------------------------------- 割安さ・大きさ
def split_after(splits: pd.DataFrame, ticker: str, dates: pd.Series) -> np.ndarray:
    """dates のそれぞれより後に効力の出た、その銘柄の分割の比率の積（無ければ 1）。"""
    s = splits[splits["ticker"] == ticker]
    d = pd.to_datetime(dates).dt.normalize().to_numpy()
    out = np.ones(len(d))
    for ex, ratio in zip(pd.to_datetime(s["date"]).to_numpy(), s["ratio"].to_numpy()):
        out[d < ex] *= ratio
    return out


def adjusted_shares(times: pd.Series, shares: np.ndarray, splits: pd.DataFrame, ticker: str,
                    before: int = AMBIGUOUS_BEFORE, after: int = AMBIGUOUS_AFTER) -> np.ndarray:
    """開示ごとの株数（times はその株数が書かれた開示の時刻）を、今の Yahoo の終値と同じ数え方に直す。

    開示より後の分割は掛ける。開示の before 日前〜after 日後に権利落ちのあった分割は、会社によって株数に入れてあったり無かったりする
    （期末の実際の株数を書く会社と、分割の後の数え方に直して書く会社がある）ので、直前の開示の株数（直した後）とつながる方を選ぶ。
    直前が無ければ「開示の日までの分割は入れてある」とみなす。
    """
    s = splits[splits["ticker"] == ticker]
    ex, ratio = pd.to_datetime(s["date"]).to_numpy(), s["ratio"].to_numpy(dtype=float)
    d = pd.to_datetime(times).dt.normalize().to_numpy()
    out = np.full(len(d), np.nan)
    prev_time, prev = None, np.nan
    for i in range(len(d)):
        if not np.isfinite(shares[i]):
            continue
        if prev_time is not None and d[i] == prev_time and np.isfinite(out[i - 1]) and shares[i] == shares[i - 1]:
            out[i] = out[i - 1]                                   # 同じ開示から持ち越した株数
            continue
        lo, hi = d[i] - np.timedelta64(before, "D"), d[i] + np.timedelta64(after, "D")
        sure = float(np.prod(ratio[ex > hi]))
        maybe = ratio[(ex > lo) & (ex <= hi)]
        default = float(np.prod(ratio[(ex > d[i]) & (ex <= hi)]))
        options = {default}
        if np.isfinite(prev) and len(maybe) <= 4:
            options = {float(np.prod(maybe[[bool(m >> k & 1) for k in range(len(maybe))]])) for m in range(2 ** len(maybe))}
            best = min(options, key=lambda f: abs(np.log(shares[i] * sure * f / prev)))
        else:
            best = default
        out[i] = shares[i] * sure * best
        prev_time, prev = d[i], out[i]
    return out


def fundamental_panel(events: pd.DataFrame, pos: np.ndarray, rawclose: pd.DataFrame, splits: pd.DataFrame,
                      stale_days: int = STALE_DAYS) -> dict[str, pd.DataFrame]:
    """割安さ・大きさの入力 6 個（日 × 株）。その時点で分かっていた値だけで作る。

    events は開示ごとの行（ticker / time と、その開示の後に分かっている fc・dps・shares・equity・eq_ratio）。pos はその開示を
    判断に使える日の位置。t 日の値は「t 日までに使えるようになった最後の開示」と「前日の終値」から作る。
    株数と 1 株の配当は「それが書かれた開示の日の数え方」とみなす（shares_time・dps_time の列。無ければ time）。
    """
    index, tickers = rawclose.index, list(rawclose.columns)
    fields = ["fc", "div", "shares", "equity", "eq_ratio"]
    state = {f: np.full(rawclose.shape, np.nan) for f in fields}
    days = np.arange(len(index))
    dates = index.to_numpy()
    ev = events.assign(pos=pos, div=np.nan)
    for col in ("shares_time", "dps_time"):
        ev[col] = ev[col].where(ev[col].astype(str) != "", ev["time"]) if col in ev else ev["time"]
    ev = ev[ev["pos"] < len(index)]
    for j, t in enumerate(tickers):
        g = ev[ev["ticker"] == t]
        if g.empty:
            continue
        g = g.sort_values("pos", kind="stable")
        vals = g[fields].to_numpy(dtype=float)
        vals[:, 2] = adjusted_shares(g["shares_time"], vals[:, 2], splits, t)   # 株数を、今の Yahoo の終値と同じ数え方に
        vals[:, 1] = g["dps"].to_numpy(dtype=float) * vals[:, 2] / split_after(splits, t, g["dps_time"])   # 配当の総額 = 1 株の配当 × その時点の株数
        p = g["pos"].to_numpy()
        k = np.searchsorted(p, days, side="right") - 1           # その日までで最後の開示
        ok = k >= 0
        last = pd.to_datetime(g["time"]).dt.normalize().to_numpy()[np.maximum(k, 0)]
        ok &= (dates - last) <= np.timedelta64(stale_days, "D")
        for i, f in enumerate(fields):
            state[f][ok, j] = vals[k[ok], i]
    cap = state["shares"] * rawclose.shift(1).to_numpy()
    cap = np.where(cap > 0, cap, np.nan)
    equity = np.where(state["equity"] > 0, state["equity"], np.nan)
    out = {"ep_fc": state["fc"] / cap, "bm": state["equity"] / cap, "dy_fc": state["div"] / cap,
           "size": np.log(cap), "equity_ratio": state["eq_ratio"], "roe_fc": state["fc"] / equity}
    return {k: pd.DataFrame(v, index=index, columns=tickers) for k, v in out.items()}


# --------------------------------------------------------------------------- 参考の表
def spread(feature: pd.DataFrame, forward: np.ndarray, mask: np.ndarray, step: int, share: float = 0.20, min_valid: int = 50) -> tuple[float, float, int]:
    """参考: 入力の上位 share − 下位 share の、その後の「株 − 全銘柄の平均」の差。step 日おきの日だけで数える（重なりを避ける）。

    返すのは（平均, t 値, 数えた日数）。
    """
    x = feature.to_numpy(dtype=float)
    rows = [t for t in np.flatnonzero(mask)[::step]]
    diffs = []
    for t in rows:
        ok = np.isfinite(x[t]) & np.isfinite(forward[t])
        n = int(ok.sum())
        if n < min_valid:
            continue
        order = np.argsort(x[t][ok])
        k = max(int(round(n * share)), 1)
        f = forward[t][ok][order]
        diffs.append(f[-k:].mean() - f[:k].mean())
    d = np.asarray(diffs)
    if len(d) < 3:
        return float("nan"), float("nan"), len(d)
    return float(d.mean()), float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))), len(d)
