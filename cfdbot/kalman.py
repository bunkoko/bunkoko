"""なめらかなトレンドの状態空間モデル（カルマンフィルタ）。docs/trend.md 6 章。

対数の価格 y を「流れ（slope）＋揺れ（ε）」で表す:
    y_t      = level_t + ε_t            ε ~ N(0, σ_ε²)   揺れ
    level_t  = level_{t-1} + slope_{t-1}                  なめらかな水準（水準そのものには揺れを入れない）
    slope_t  = slope_{t-1} + ζ_t        ζ ~ N(0, σ_ζ²)   流れの変わりやすさ
推定するのは σ_ζ と σ_ε の 2 つだけ（最尤法。格子で探す）。フィルタは定常の利得で動かす。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

BURN = 50                       # 尤度に数えない最初の本数（初期値の影響を外す）
GRID_ZETA = (-6.0, -2.0, 9)     # log10 σ_ζ の範囲と刻みの数
GRID_EPS = (-4.0, -1.0, 9)      # log10 σ_ε
REFINE = (7, 0.25)              # 一番良い所の周りを、刻み × 0.25 で 7 × 7


def steady_state(q: np.ndarray, r: np.ndarray, iters: int = 300) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """定常の予測共分散 P（…,2,2）、利得 K（…,2）、イノベーションの分散 Fv、フィルタ後の共分散 P_f。

    q・r は同じ形の配列（格子をまとめて計算できる）。
    """
    q, r = np.asarray(q, float), np.asarray(r, float)
    p00 = np.ones_like(q) * (r + q)
    p01 = np.zeros_like(q)
    p11 = np.ones_like(q) * q
    for _ in range(iters):
        fv = p00 + r
        f00, f01, f11 = p00 - p00 * p00 / fv, p01 - p00 * p01 / fv, p11 - p01 * p01 / fv   # フィルタ後
        n00, n01, n11 = f00 + 2 * f01 + f11, f01 + f11, f11 + q                         # F P_f F' + Q
        if np.all(np.abs(n00 - p00) < 1e-14 * (1 + np.abs(p00))):
            p00, p01, p11 = n00, n01, n11
            break
        p00, p01, p11 = n00, n01, n11
    fv = p00 + r
    k = np.stack([p00 / fv, p01 / fv], axis=-1)
    pf = np.stack([np.stack([p00 - p00 * p00 / fv, p01 - p00 * p01 / fv], -1),
                   np.stack([p01 - p00 * p01 / fv, p11 - p01 * p01 / fv], -1)], -2)
    p = np.stack([np.stack([p00, p01], -1), np.stack([p01, p11], -1)], -2)
    return p, k, fv, pf


def loglik_grid(y: np.ndarray, q: np.ndarray, r: np.ndarray) -> np.ndarray:
    """格子の各点（q, r）での対数尤度（定数を除く）。y は対数の価格。"""
    _, k, fv, _ = steady_state(q, r)
    k0, k1 = k[..., 0], k[..., 1]
    lv = np.full(q.shape, y[0])
    sl = np.zeros(q.shape)
    ll = np.zeros(q.shape)
    log_fv = np.log(fv)
    for t in range(1, len(y)):
        pred = lv + sl
        v = y[t] - pred
        if t >= BURN:
            ll -= 0.5 * (log_fv + v * v / fv)
        lv = pred + k0 * v
        sl = sl + k1 * v
    return ll


def fit(y: np.ndarray) -> tuple[float, float]:
    """σ_ζ²・σ_ε² を最尤法で（粗い格子 → 一番良い所の周りを細かく）。"""
    y = np.asarray(y, float)
    lz = np.linspace(*GRID_ZETA)
    le = np.linspace(*GRID_EPS)
    best = None
    for _round in range(2):
        gz, ge = np.meshgrid(lz, le, indexing="ij")
        ll = loglik_grid(y, (10.0 ** gz) ** 2, (10.0 ** ge) ** 2)
        i, j = np.unravel_index(int(np.nanargmax(ll)), ll.shape)
        best = (float(gz[i, j]), float(ge[i, j]))
        n, frac = REFINE
        dz, de = (lz[1] - lz[0]) * frac, (le[1] - le[0]) * frac
        lz = best[0] + dz * np.arange(-(n // 2), n // 2 + 1)
        le = best[1] + de * np.arange(-(n // 2), n // 2 + 1)
    return (10.0 ** best[0]) ** 2, (10.0 ** best[1]) ** 2


def trend_z(close: pd.Series, prior_close: pd.Series | None = None, min_fit: int = 500, refit_bars: int = 250,
            fit_window: int = 2000) -> pd.Series:
    """流れの推定値 ÷ その不確かさ（z）。推定し直すのは refit_bars ごと、使うのはその時点より前の値だけ。

    prior_close があれば、パラメータの推定にはそちら（例: 先物の長い履歴）を使い、フィルタは close に当てる。
    最初の推定（min_fit 本たまるまで）より前は NaN。
    """
    y = np.log(close.to_numpy(float))
    n = len(y)
    z = np.full(n, np.nan)
    if prior_close is not None:
        py = np.log(prior_close.to_numpy(float))
        pt = prior_close.index.asi8
    k0 = k1 = sd = None
    lv = sl = 0.0
    for i in range(n):
        if i >= min_fit and (i - min_fit) % refit_bars == 0:
            if prior_close is None:
                series = y[max(0, i - fit_window):i]
            else:
                m = int(np.searchsorted(pt, close.index.asi8[i], side="left"))
                series = py[max(0, m - fit_window):m]
            if len(series) >= min_fit:
                q, r = fit(series)
                _, k, _, pf = steady_state(np.array(q), np.array(r))
                k0, k1, sd = float(k[0]), float(k[1]), float(np.sqrt(pf[1, 1]))
                if z[:i].size and np.all(np.isnan(z[:i])):          # 最初の推定: ここまでの値でフィルタを温める
                    lv, sl = y[0], 0.0
                    for t in range(1, i):
                        pred = lv + sl
                        v = y[t] - pred
                        lv, sl = pred + k0 * v, sl + k1 * v
        if k0 is None or i == 0:
            continue
        pred = lv + sl
        v = y[i] - pred
        lv, sl = pred + k0 * v, sl + k1 * v
        z[i] = sl / sd
    return pd.Series(z, index=close.index)
