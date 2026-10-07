"""バックテストの統計（AFML 14 章、Bailey & López de Prado 2012・2014 の式）。

- PSR（確率的シャープレシオ）: 観測したシャープ・日数・リターンの歪み・裾の厚さから、
  本当のシャープが基準（既定 0）を超える確率
- 試した数での割り引き（DSR の考え方）: 役に立たない候補を N 個試すと、一番良いものは偶然でも
  平均 + 標準偏差 × E[N 個の標準正規の最大値] くらいになる。それを超えたかで判断する
- HHI（集中度）: 利益（損失）が少数の取引に集中しているか。0 = 均等、1 = 1 回だけ
- 水面下の期間: 資産が前の最高値を下回っていた最長の日数
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd

_N = NormalDist()
EULER_GAMMA = 0.5772156649015329


def moments(returns) -> tuple[float, int, float, float]:
    """1 期間あたりのシャープ（年率にしない）・観測数・歪度・尖度（正規分布で 3）。"""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = len(r)
    if n < 3:
        return float("nan"), n, float("nan"), float("nan")
    sd = r.std(ddof=1)
    if sd <= 0:
        return float("nan"), n, float("nan"), float("nan")
    z = (r - r.mean()) / r.std(ddof=0)
    return float(r.mean() / sd), n, float(np.mean(z ** 3)), float(np.mean(z ** 4))


def psr(sr: float, n: int, skew: float = 0.0, kurt: float = 3.0, sr_star: float = 0.0) -> float:
    """本当のシャープ（1 期間あたり）が sr_star を超える確率。"""
    if not (np.isfinite(sr) and n > 1):
        return float("nan")
    var = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if var <= 0:
        return float("nan")
    return _N.cdf((sr - sr_star) * math.sqrt(n - 1) / math.sqrt(var))


def psr_of(returns, sr_star: float = 0.0) -> float:
    sr, n, sk, ku = moments(returns)
    return psr(sr, n, sk, ku, sr_star)


def expected_max_z(n_trials: int) -> float:
    """独立な標準正規の値を n_trials 個とったときの最大値の期待値（近似）。1 個なら 0。"""
    if n_trials <= 1:
        return 0.0
    return ((1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / n_trials)
            + EULER_GAMMA * _N.inv_cdf(1 - 1 / (n_trials * math.e)))


def deflated_threshold(null_values, n_trials: int) -> float:
    """偶然（null_values）を n_trials 回試したときに、一番良いものが出しそうな値。"""
    v = np.asarray(null_values, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) < 2:
        return float("nan")
    return float(v.mean() + v.std(ddof=1) * expected_max_z(n_trials))


def hhi(values) -> float:
    """集中度（AFML 14 章）。values はすべて同じ符号の取引結果。2 個以下なら NaN。"""
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) <= 2 or v.sum() == 0:
        return float("nan")
    w = v / v.sum()
    n = len(v)
    return float(((w ** 2).sum() - 1 / n) / (1 - 1 / n))


def longest_underwater_days(equity: pd.Series) -> float:
    """資産が前の最高値を下回っていた最長の期間（日）。最後まで戻らなければ最後の日までで数える。"""
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    under = (equity < peak).to_numpy()
    t = equity.index
    longest, start = pd.Timedelta(0), None
    for i, u in enumerate(under):
        if u and start is None:
            start = t[i - 1] if i > 0 else t[i]   # 最高値をつけた時点から数える
        elif not u and start is not None:
            longest = max(longest, t[i] - start)
            start = None
    if start is not None:
        longest = max(longest, t[-1] - start)
    return longest / pd.Timedelta(days=1)
