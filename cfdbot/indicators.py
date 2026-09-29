"""テクニカル指標。

すべて「その足の終値までの情報」だけで計算する（先読みなし）。
EA (mql5/Experts/CfdCommodityEA.mq5) と同じ式・同じ初期化方法にしてあり、
十分な本数の履歴があれば両者の値は一致する。

- EMA: alpha = 2/(n+1)、初期値 = 最初の値
- RMA (Wilder): alpha = 1/n、初期値 = 最初の値
- 標準偏差: 母標準偏差 (ddof=0)
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=2.0 / (n + 1), adjust=False).mean()


def rma(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def stdev(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).std(ddof=0)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return tr


def atr(df: pd.DataFrame, n: int = 20) -> pd.Series:
    return rma(true_range(df), n)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff().fillna(0.0)
    up = rma(d.clip(lower=0.0), n)
    dn = rma((-d).clip(lower=0.0), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = up / dn
        out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(dn > 0, 100.0)
    out = out.where((up > 0) | (dn > 0), 50.0)
    return out


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up_move = df["high"].diff()
    dn_move = -df["low"].diff()
    plus_dm = up_move.where((up_move > dn_move) & (up_move > 0), 0.0).fillna(0.0)
    minus_dm = dn_move.where((dn_move > up_move) & (dn_move > 0), 0.0).fillna(0.0)
    tr_s = rma(true_range(df), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * rma(plus_dm, n) / tr_s
        minus_di = 100.0 * rma(minus_dm, n) / tr_s
        di_sum = plus_di + minus_di
        dx = (100.0 * (plus_di - minus_di).abs() / di_sum).where(di_sum > 0, 0.0)
    return rma(dx.fillna(0.0), n)


def efficiency_ratio(close: pd.Series, n: int = 20) -> pd.Series:
    """Kaufman の効率比。1 に近いほど一方向（トレンド）、0 に近いほどノイズ。"""
    direction = (close - close.shift(n)).abs()
    volatility = close.diff().abs().rolling(n).sum()
    with np.errstate(divide="ignore", invalid="ignore"):
        return (direction / volatility).where(volatility > 0, 0.0)


def donchian(df: pd.DataFrame, n: int) -> tuple[pd.Series, pd.Series]:
    """直前 n 本（現在足を含まない）の最高値・最安値。"""
    upper = df["high"].rolling(n).max().shift(1)
    lower = df["low"].rolling(n).min().shift(1)
    return upper, lower
