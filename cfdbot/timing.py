"""S&P500 を持ち方で上回れるか（docs/us_equity.md: vol_managed・sma_timing）。比べる相手は持っているだけ。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

VOL_WINDOW = 21            # vol_managed: 直近 1 か月の変動
VOL_CAP = 1.5              # 量の上限（倍）
SMA_MONTHS = 10            # sma_timing: 10 か月の平均
COST = 0.0005              # 量の変化分の往復コスト（指数の ETF・CFD の水準）


def load_index(path: str | Path) -> pd.Series:
    df = pd.read_csv(path).dropna(subset=["close"])
    df = df[df["close"] > 0]
    s = pd.Series(df["close"].to_numpy(float), index=pd.DatetimeIndex(pd.to_datetime(df["date"]), tz="UTC"))
    return s[~s.index.duplicated(keep="last")].sort_index()


def month_ends(index: pd.DatetimeIndex) -> np.ndarray:
    naive = index.tz_convert(None) if index.tz is not None else index
    m = naive.to_period("M")
    return np.r_[m[1:] != m[:-1], True]


def vol_managed_weight(close: pd.Series, window: int = VOL_WINDOW, cap: float = VOL_CAP) -> pd.Series:
    """量 = 目標の変動 ÷ 直近 window 日の変動（目標 = それまでの変動の中央値、未来は見ない）。月末に決めて翌月は固定。"""
    r = close.pct_change()
    vol = r.rolling(window, min_periods=window).std() * np.sqrt(252)
    target = vol.expanding(min_periods=252).median()
    w = (target / vol).clip(lower=0.0, upper=cap)
    me = month_ends(close.index)
    return w.where(pd.Series(me, index=close.index)).ffill().shift(1).fillna(0.0)


def sma_weight(close: pd.Series, months: int = SMA_MONTHS) -> pd.Series:
    """月末の終値が直近 months か月の月末の平均より上なら 1、下なら 0。翌月はそのまま。"""
    me = month_ends(close.index)
    cm = close[me]
    above = (cm > cm.rolling(months, min_periods=months).mean()).astype(float)
    above = above.where(cm.rolling(months, min_periods=months).mean().notna())
    w = pd.Series(np.nan, index=close.index)
    w[me] = above
    return w.ffill().shift(1).fillna(0.0)


def strategy_returns(close: pd.Series, weight: pd.Series, cost: float = COST) -> pd.Series:
    """資金 1 に対する日ごとの損益（複利にしない）: 量 × 値動き − 量の変化分の往復コストの半分。現金の金利は 0。"""
    r = close.pct_change().fillna(0.0)
    w = weight.reindex(close.index).fillna(0.0)
    trade = (w - w.shift(1).fillna(0.0)).abs() * cost / 2
    return w.shift(1).fillna(0.0) * r - trade


def sharpe(r) -> float:
    r = pd.Series(r).dropna()
    if len(r) < 60 or r.std() <= 0:
        return float("nan")
    return float(r.mean() / r.std() * np.sqrt(252))


def max_drawdown(r: pd.Series) -> float:
    eq = (1 + r.fillna(0.0)).cumprod()
    return float((eq / eq.cummax() - 1).min()) if len(eq) else float("nan")


def placebo_sharpes(close: pd.Series, weight: pd.Series, mask: np.ndarray, n: int = 200, seed: int = 31) -> np.ndarray:
    """量の並びを時間方向にずらしたときのシャープ（mask の期間）。"""
    rng = np.random.default_rng(seed)
    out = []
    w = weight.to_numpy()
    for _ in range(n):
        k = int(rng.integers(1, len(w)))
        r = strategy_returns(close, pd.Series(np.roll(w, k), index=weight.index))
        out.append(sharpe(r.to_numpy()[mask]))
    return np.array(out)
