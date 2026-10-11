"""2 つの差の戻り（オルンシュタイン＝ウーレンベック過程）。docs/spread.md。

差 s に ds = θ(μ − s)dt + σ dW を当てはめ、落ち着き先 μ から大きく離れたら戻る向きに両建てする。
推定は直近 WINDOW 日の最小二乗（s_t − s_{t−1} を s_{t−1} に回帰）、REFIT 日ごと、その時点より前の値だけ。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

PAIRS = {   # 名前 → (1 つ目, 2 つ目, 差の種類, 表示)
    "oil": ("WTI", "BRENT", "diff", "WTI−ブレント（ドル/バレル）"),
    "metal": ("GOLD", "SILVER", "logratio", "金÷銀（対数の差）"),
}
WINDOW, REFIT = 500, 20            # 推定に使う日数、推定し直す間隔
Z_IN, Z_STOP = 2.0, 4.0            # 入る・損切りの z
HL_MIN, HL_MAX = 5.0, 120.0        # 新規に入れる半減期の範囲（日）
VOL_TARGET, VOL_WINDOW = 0.10, 60  # 年率の変動の目標、差の変動を測る日数
MAX_LEV = 2.0                      # 両足の名目の合計 ÷ 資金 の上限（今の EA と同じ）
COST, FINANCING = 0.0008, 0.03     # 片足ごとの往復コスト、建玉の年率コスト（docs/universe.md の今の 4 商品と同じ）


def load_close(path) -> pd.Series:
    """Yahoo の CSV の終値（マイナスの日も残す。WTI の 2020-04-20 は本当の値）。"""
    df = pd.read_csv(path).dropna(subset=["close"])
    s = pd.Series(df["close"].to_numpy(float), index=pd.DatetimeIndex(pd.to_datetime(df["date"]), tz="UTC"))
    return s[~s.index.duplicated(keep="last")].sort_index()


def spread_series(p1: pd.Series, p2: pd.Series, kind: str) -> pd.Series:
    a, b = p1.align(p2, join="inner")
    if kind == "diff":
        return (a - b).rename("spread")
    return (np.log(a) - np.log(b)).rename("spread")


def ou_fit(s: np.ndarray) -> dict:
    """s_t − s_{t−1} = a + b s_{t−1} + ε の最小二乗 → θ・μ・平衡時の標準偏差・半減期。戻る力が無ければ NaN。"""
    s = np.asarray(s, float)
    x, y = s[:-1], np.diff(s)
    xm, ym = x.mean(), y.mean()
    vx = ((x - xm) ** 2).sum()
    nan = {"theta": np.nan, "mu": np.nan, "sigma_eq": np.nan, "half_life": np.nan}
    if vx <= 0:
        return nan
    b = ((x - xm) * (y - ym)).sum() / vx
    a = ym - b * xm
    if not (-1.0 < b < 0.0):
        return nan
    resid = y - (a + b * x)
    sig = float(resid.std(ddof=2))
    theta = -np.log1p(b)
    return {"theta": float(theta), "mu": float(-a / b), "sigma_eq": sig / np.sqrt(1 - (1 + b) ** 2),
            "half_life": float(np.log(2) / theta)}


def ou_state(s: pd.Series, window: int = WINDOW, refit: int = REFIT) -> pd.DataFrame:
    """日ごとの z・半減期・μ・平衡時の標準偏差。推定はその日より前の window 日で、refit 日ごと。"""
    v = s.to_numpy(float)
    n = len(v)
    out = np.full((n, 4), np.nan)
    fit = None
    for i in range(window, n):
        if (i - window) % refit == 0:
            fit = ou_fit(v[i - window:i])
        if fit is None or not np.isfinite(fit["theta"]):
            continue
        out[i] = [(v[i] - fit["mu"]) / fit["sigma_eq"], fit["half_life"], fit["mu"], fit["sigma_eq"]]
    return pd.DataFrame(out, index=s.index, columns=["z", "half_life", "mu", "sigma_eq"])


def positions(state: pd.DataFrame, z_in: float = Z_IN, z_stop: float = Z_STOP, hl_min: float = HL_MIN,
              hl_max: float = HL_MAX) -> np.ndarray:
    """終値で決める向き（+1 差を買う・−1 差を売る・0 なし）。次の日から効く。"""
    z, hl = state["z"].to_numpy(float), state["half_life"].to_numpy(float)
    pos = np.zeros(len(z), np.int8)
    p = 0
    for i in range(len(z)):
        if not np.isfinite(z[i]):
            p = 0
        elif p == 0:
            if np.isfinite(hl[i]) and hl_min <= hl[i] <= hl_max:
                p = -1 if z[i] > z_in else (1 if z[i] < -z_in else 0)
        elif (p == -1 and z[i] <= 0) or (p == 1 and z[i] >= 0) or abs(z[i]) > z_stop:
            p = 0
        pos[i] = p
    return pos


def pair_returns(p1: pd.Series, p2: pd.Series, s: pd.Series, pos: np.ndarray, kind: str,
                 vol_target: float = VOL_TARGET, max_lev: float = MAX_LEV, cost: float = COST,
                 financing: float = FINANCING) -> pd.DataFrame:
    """資金 1 に対する日ごとの損益（単純。複利にしない）と、名目 ÷ 資金。

    量は入るときに決めて持っている間は変えない: 差の直近 60 日の変動から年率 vol_target になる単位数、
    ただし両足の名目の合計 ≤ max_lev。コストは両足とも片道 cost/2、建玉に年 financing。
    """
    a, b = p1.reindex(s.index).to_numpy(float), p2.reindex(s.index).to_numpy(float)
    v = s.to_numpy(float)
    n = len(v)
    move1 = np.diff(a) if kind == "diff" else a[1:] / a[:-1] - 1       # 足ごとの 1 単位の値動き
    move2 = np.diff(b) if kind == "diff" else b[1:] / b[:-1] - 1
    notional = (a + b) if kind == "diff" else np.full(n, 2.0)          # 1 単位の両足の名目
    sd = pd.Series(v).diff().rolling(VOL_WINDOW, min_periods=VOL_WINDOW // 2 + 10).std().to_numpy()
    units = np.zeros(n)
    w = 0.0
    for i in range(n):
        if pos[i] == 0:
            w = 0.0
        elif i == 0 or pos[i - 1] == 0 or np.sign(pos[i]) != np.sign(pos[i - 1]):
            if not np.isfinite(sd[i]) or sd[i] <= 0 or notional[i] <= 0:
                pos[i] = 0
                w = 0.0
            else:
                w = min(vol_target / np.sqrt(252) / sd[i], max_lev / notional[i])
        units[i] = pos[i] * w
    ret = np.zeros(n)
    lev = np.abs(units) * notional
    for i in range(1, n):
        pnl = units[i - 1] * (move1[i - 1] - move2[i - 1])
        fin = lev[i - 1] * financing / 252
        change = units[i - 1] - (units[i - 2] if i >= 2 else 0.0)       # 前日の終値で決めた分を、この日の始値で約定
        trade = abs(change) * notional[i - 1] * cost / 2
        ret[i] = pnl - fin - trade
    return pd.DataFrame({"ret": ret, "lev": lev, "units": units}, index=s.index)


def sharpe(r: pd.Series | np.ndarray) -> float:
    r = pd.Series(r).dropna()
    if len(r) < 60 or r.std() <= 0:
        return float("nan")
    return float(r.mean() / r.std() * np.sqrt(252))


def placebo_sharpes(p1: pd.Series, p2: pd.Series, s: pd.Series, pos: np.ndarray, kind: str, mask: np.ndarray,
                    n: int = 200, seed: int = 7) -> np.ndarray:
    """持つ向きの並びを時間方向にずらしたときのシャープ（mask の期間で）。"""
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        k = int(rng.integers(1, len(pos)))
        r = pair_returns(p1, p2, s, np.roll(pos, k).copy(), kind)["ret"].to_numpy()
        out.append(sharpe(r[mask]))
    return np.array(out)
