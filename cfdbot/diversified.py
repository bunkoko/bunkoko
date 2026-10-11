"""市場を分散したトレンドフォロー（論文どおりの TSMOM）。docs/diversified.md。

向き = 1・3・12 か月の値動きの符号の平均。量 = 向き × 0.40 ÷ 年率の変動（直近 60 日）。
全市場を 1/N で足し、月末に見直す。コストはクラスごと。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .universe import GROUPS, Market

HORIZONS = (21, 63, 252)       # 1・3・12 か月（営業日）
VOL_WINDOW, VOL_TARGET = 60, 0.40
MIN_HISTORY_DAYS = 3 * 252
_M = Market
EXTRA_MARKETS: tuple[Market, ...] = (     # docs/universe.md の 59 市場に足す（Yahoo にある先物・為替）
    _M("HEATOIL", "HO=F", "commodity", "暖房油"), _M("SOYMEAL", "ZM=F", "commodity", "大豆粕"),
    _M("SOYOIL", "ZL=F", "commodity", "大豆油"), _M("KCWHEAT", "KE=F", "commodity", "KC 小麦"),
    _M("COCOA", "CC=F", "commodity", "ココア"), _M("COTTON", "CT=F", "commodity", "綿花"),
    _M("CATTLE", "LE=F", "commodity", "生牛"), _M("HOGS", "HE=F", "commodity", "豚赤身"),
    _M("OJ", "OJ=F", "commodity", "オレンジジュース"),
    _M("UST2Y", "ZT=F", "bond", "米2年国債先物"), _M("USTULTRA", "UB=F", "bond", "米ウルトラ国債先物"),
    _M("USDMXN", "MXN=X", "fx", "米ドルメキシコペソ"), _M("USDNOK", "NOK=X", "fx", "米ドルノルウェークローネ"),
    _M("USDSEK", "SEK=X", "fx", "米ドルスウェーデンクローナ"), _M("USDZAR", "ZAR=X", "fx", "米ドル南アランド"),
)
STOCK_GROUPS = ("jp_stock", "us_stock")   # 生存者バイアスがあるので使わない


def tsmom_sign(close: pd.DataFrame, horizons=HORIZONS) -> pd.DataFrame:
    """1・3・12 か月の値動きの符号の平均（−1〜+1）。その日の終値まで。"""
    parts = [np.sign(close - close.shift(h)) for h in horizons]       # 差の符号（WTI の 2020-04 のマイナス価格でも壊れない）
    return sum(parts) / len(parts)


def returns(close: pd.DataFrame) -> pd.DataFrame:
    """1 単位の損益 ÷ 前日の価格の絶対値（価格が正ならふつうの変化率と同じ）。"""
    return close.diff() / close.shift(1).abs()


def annual_vol(close: pd.DataFrame, window: int = VOL_WINDOW) -> pd.DataFrame:
    return returns(close).rolling(window, min_periods=window // 2 + 10).std() * np.sqrt(252)


def month_ends(index: pd.DatetimeIndex) -> np.ndarray:
    """月の最後の営業日（その日の終値で見直す）。"""
    naive = index.tz_convert(None) if index.tz is not None else index
    m = naive.to_period("M")
    return np.r_[m[1:] != m[:-1], True]


def positions(close: pd.DataFrame, vol_target: float = VOL_TARGET) -> pd.DataFrame:
    """市場ごとの持つ量（名目 ÷ 資金、符号付き）。月末の終値で決め、翌月はそのまま。1/N はここで掛ける。"""
    s = tsmom_sign(close)
    v = annual_vol(close)
    raw = (s * vol_target / v).where(v > 0)
    n = close.notna().sum(axis=1).replace(0, np.nan)
    raw = raw.div(n, axis=0)
    me = np.broadcast_to(month_ends(close.index)[:, None], raw.shape)
    held = raw.where(me).ffill()                                     # 月末の値を翌月に持ち越す
    held = held.where(close.notna())                                 # データの無い日は持たない
    return held.shift(1).fillna(0.0)                                 # 決めた翌日から効く


def portfolio_returns(close: pd.DataFrame, pos: pd.DataFrame, groups: dict[str, str],
                      financing: bool = True) -> pd.DataFrame:
    """日ごとの損益 ÷ 資金（複利にしない）: 向き × 値動き − 保有コスト − 売買コスト。

    financing=False は先物の場合（名目に対する保有コストが無く、証拠金だけ。売買コストは同じ）。
    """
    r = returns(close).fillna(0.0)
    cost = pd.Series({k: GROUPS[g].cost for k, g in groups.items()})
    fin = pd.Series({k: (GROUPS[g].financing if financing else 0.0) for k, g in groups.items()})
    pnl = (pos * r).sum(axis=1)
    carry = (pos.abs() * fin / 252).sum(axis=1)
    turnover = (pos - pos.shift(1).fillna(0.0)).abs()
    trade = (turnover * cost / 2).sum(axis=1)
    return pd.DataFrame({"ret": pnl - carry - trade, "gross": pos.abs().sum(axis=1), "turnover": turnover.sum(axis=1)})


def sharpe(r: pd.Series) -> float:
    r = pd.Series(r).dropna()
    if len(r) < 60 or r.std() <= 0:
        return float("nan")
    return float(r.mean() / r.std() * np.sqrt(252))


def placebo_sharpes(close: pd.DataFrame, pos: pd.DataFrame, groups: dict[str, str], mask: np.ndarray,
                    n: int = 200, seed: int = 11) -> np.ndarray:
    """市場ごとに持つ量の並びを時間方向にずらす（量・売買の頻度はそのまま、時期だけ合わなくなる）。"""
    rng = np.random.default_rng(seed)
    out = []
    arr = pos.to_numpy()
    for _ in range(n):
        sh = np.empty_like(arr)
        for j in range(arr.shape[1]):
            sh[:, j] = np.roll(arr[:, j], int(rng.integers(1, len(arr))))
        p = pd.DataFrame(sh, index=pos.index, columns=pos.columns).where(close.notna(), 0.0)
        out.append(sharpe(portfolio_returns(close, p, groups)["ret"][mask]))
    return np.array(out)
