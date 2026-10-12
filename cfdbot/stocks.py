"""日本の個別株をルールで選ぶ（docs/stocks.md）。価格だけで計算できる 2 つの点数（モメンタム・低ボラティリティ）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

# 日本の大型株（日経平均・TOPIX の中核銘柄を中心に手で並べた。今の銘柄だけ = 生存者バイアスあり）
CODES = """
7203 6758 9432 8306 9984 6501 7267 7751 4502 8604 6861 8035 6098 9983 4063 6367 7974 8316 8411 6902 6954 6981 4568 4519
4503 4578 2914 8058 8031 8001 8002 8053 9433 9434 6273 6594 6503 6752 6702 6701 6857 6723 6762 6971 7741 4543 4901 4452
4911 2802 2502 2503 2587 3382 8267 9843 3038 7269 7270 7201 7261 7202 7211 5401 5411 5713 5802 3407 4188 4005 4183 4021
3402 3401 5108 5020 5019 1605 9101 9104 9107 9020 9022 9021 9201 9202 9501 9502 9503 9531 9532 8766 8750 8725 8630 8591
8801 8802 8830 1925 1928 1801 1802 1803 1812 6301 6305 6326 7011 7012 7013 6506 6645 6479 6963 6920 6146 7735 4062 6988
4704 4689 4755 2413 4307 9613 9735 4661 9602 7832 9697 9766 3659 2801 2269 2282 2501 4523 4506 4507 4151 4528 7733 7731
7752 6724 6471 6472 6473 7272 7951 7912 7911 3861 3863 5201 5301 5332 5333 5214 5233 5631 5706 5711 5801 5803 2768 8015
8233 8252 8282 3086 3099 9064 9147 9301 9008 9009 9005 9007 9001 9042 9041 7186 8309 8308 8331 8354 8601 8795 8697 2432
4324 4385 3092 6178 7182 7181 2371 4751 6965 6841 6504 6674 6103 6141 6268 6361 6302 7004 7003 8803 3289 3231 8804 2871
2002 2201 2206 2531 2602 1332 1333 4631 4612 4061 4004 4208 4272 4927 4922 4516 4536 4587 4967 7956 7936 8113 3105 3101
""".split()
TICKERS = tuple(f"{c}.T" for c in CODES)
MOM_LONG, MOM_SKIP, VOL_WINDOW = 252, 21, 60
TOP_N = 20
COST = 0.001                     # 入れ替えた分の往復コスト
MIN_HISTORY = 3 * 252
JUMP = 2.0                       # 1 日で ×2 超・÷2 未満の動きは、再上場・持ち株会社化などの値段の尺度の変化とみなす


def stock_path(root: str | Path, ticker: str) -> Path:
    return Path(root) / f"{ticker}.csv"


def clean_scale_jumps(s: pd.Series, jump: float = JUMP) -> tuple[pd.Series, int]:
    """1 日で ×jump 超・÷jump 未満の飛び（JAL の 2012 年の再上場、東京海上の 2005 年の持ち株会社化など）より前は使わない。

    戻り値の 2 つ目は見つけた飛びの数。大型株がこれほど 1 日で動くことは無く、値段の尺度が変わったとみなす。
    """
    ratio = s / s.shift(1)
    bad = ratio[(ratio > jump) | (ratio < 1 / jump)]
    if bad.empty:
        return s, 0
    return s[s.index >= bad.index[-1]], len(bad)


def load_closes(root: str | Path, tickers=TICKERS, log=None) -> pd.DataFrame:
    """取れている銘柄の終値（列: ティッカー、index: 日付 UTC 0 時）。尺度の飛びより前は省く。"""
    cols = {}
    for t in tickers:
        p = stock_path(root, t)
        if not p.exists():
            continue
        df = pd.read_csv(p).dropna(subset=["close"])
        df = df[df["close"] > 0]
        s = pd.Series(df["close"].to_numpy(float), index=pd.DatetimeIndex(pd.to_datetime(df["date"]), tz="UTC"))
        s = s[~s.index.duplicated(keep="last")].sort_index()
        s, n_jump = clean_scale_jumps(s)
        if n_jump and log is not None:
            log(f"  {t}: 値段の尺度の飛び {n_jump} 回 → {s.index[0]:%Y-%m-%d} から使う")
        if len(s) >= MIN_HISTORY:
            cols[t] = s
    return pd.DataFrame(cols).sort_index()


def month_ends(index: pd.DatetimeIndex) -> np.ndarray:
    naive = index.tz_convert(None) if index.tz is not None else index
    m = naive.to_period("M")
    return np.r_[m[1:] != m[:-1], True]


def scores(close: pd.DataFrame) -> pd.DataFrame:
    """総合点（0〜1。高いほど良い）: モメンタム（12-1 か月）と低ボラティリティ（60 日）の順位の平均。その日の終値まで。"""
    mom = close.shift(MOM_SKIP) / close.shift(MOM_LONG) - 1
    vol = close.pct_change().rolling(VOL_WINDOW, min_periods=VOL_WINDOW // 2 + 10).std()
    r_mom = mom.rank(axis=1, pct=True)
    r_vol = (-vol).rank(axis=1, pct=True)
    both = r_mom.notna() & r_vol.notna()
    return ((r_mom + r_vol) / 2).where(both)


def monthly_picks(close: pd.DataFrame, top_n: int = TOP_N) -> pd.DataFrame:
    """月末ごとの持ち株（True）。点数の上位 top_n。点数が付いた銘柄が 2 × top_n 未満の月は持たない。"""
    sc = scores(close)
    me = month_ends(close.index)
    sm = sc[me]
    picks = pd.DataFrame(False, index=sm.index, columns=sm.columns)
    for t, row in sm.iterrows():
        v = row.dropna()
        if len(v) >= 2 * top_n:
            picks.loc[t, v.nlargest(top_n).index] = True
    return picks


def monthly_returns(close: pd.DataFrame) -> pd.DataFrame:
    """月末から翌月末までの銘柄ごとの変化率（行: 決めた月末）。"""
    me = month_ends(close.index)
    cm = close[me]
    return (cm.shift(-1) / cm - 1)


def basket_returns(picks: pd.DataFrame, mret: pd.DataFrame, cost: float = COST) -> pd.Series:
    """選んだ銘柄を同じ金額で持った翌月のリターン（入れ替えた分の往復コストを引く）。"""
    w = picks.astype(float)
    n = w.sum(axis=1).replace(0, np.nan)
    w = w.div(n, axis=0).fillna(0.0)
    gross = (w * mret.reindex(w.index).fillna(0.0)).sum(axis=1)
    turnover = (w - w.shift(1).fillna(0.0)).abs().sum(axis=1) / 2        # 入れ替えた割合（片側）
    r = gross - turnover * cost
    return r.where(n.notna())


def universe_returns(close: pd.DataFrame, mret: pd.DataFrame) -> pd.Series:
    """比べる相手: 点数の付く銘柄を同じ金額で持ったもの（同じ生存者バイアス）。"""
    sc = scores(close)
    me = month_ends(close.index)
    have = sc[me].notna()
    w = have.astype(float)
    n = w.sum(axis=1).replace(0, np.nan)
    w = w.div(n, axis=0).fillna(0.0)
    return (w * mret.reindex(w.index).fillna(0.0)).sum(axis=1).where(n.notna())


def sharpe_monthly(r: pd.Series) -> float:
    r = pd.Series(r).dropna()
    if len(r) < 24 or r.std() <= 0:
        return float("nan")
    return float(r.mean() / r.std() * np.sqrt(12))


def random_pick_excess(close: pd.DataFrame, mret: pd.DataFrame, bench: pd.Series, n: int = 200, seed: int = 29,
                       top_n: int = TOP_N) -> np.ndarray:
    """でたらめに top_n 銘柄を選んだ場合の、相手との差のシャープ（n 回）。"""
    rng = np.random.default_rng(seed)
    sc = scores(close)
    me = month_ends(close.index)
    have = sc[me].notna()
    out = []
    for _ in range(n):
        picks = pd.DataFrame(False, index=have.index, columns=have.columns)
        for t, row in have.iterrows():
            names = row[row].index
            if len(names) >= 2 * top_n:
                picks.loc[t, rng.choice(names, top_n, replace=False)] = True
        r = basket_returns(picks, mret)
        out.append(sharpe_monthly((r - bench).dropna()))
    return np.array(out)
