import numpy as np
import pandas as pd

from cfdbot import indicators as ind
from cfdbot.kalman import fit, loglik_grid, steady_state, trend_z
from cfdbot.strategies import make_strategy


def _smooth_trend(n: int, sig_zeta: float, sig_eps: float, seed: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    slope = np.cumsum(rng.normal(0, sig_zeta, n))
    level = np.cumsum(slope)
    return level + rng.normal(0, sig_eps, n), slope


def test_fit_recovers_the_two_noise_parameters():
    y, _ = _smooth_trend(3000, 1e-4, 1e-2, 0)
    q, r = fit(y)
    assert 0.5e-4 < np.sqrt(q) < 2e-4 and 0.7e-2 < np.sqrt(r) < 1.4e-2
    ll = loglik_grid(y, np.array([q, q * 100]), np.array([r, r]))
    assert ll[0] > ll[1]                                                  # 本当の値の方が尤度が高い


def test_steady_state_gain_grows_with_the_trend_noise():
    _, k, fv, pf = steady_state(np.array([1e-8, 1e-4]), np.array([1e-4, 1e-4]))
    assert k[1, 1] > k[0, 1] and fv[1] > fv[0]                            # 流れが変わりやすいほど速く追う
    assert (pf[:, 1, 1] > 0).all() and (pf[:, 0, 0] < fv).all()


def test_trend_z_tracks_the_true_slope_and_uses_only_the_past():
    y, slope = _smooth_trend(3000, 1e-4, 1e-2, 1)
    close = pd.Series(np.exp(y), index=pd.bdate_range("2010-01-01", periods=3000, tz="UTC"))
    z = trend_z(close)
    assert z.iloc[:500].isna().all() and z.iloc[500:].notna().all()        # 最初の推定まで無し
    m = z.notna().to_numpy()
    assert np.corrcoef(z[m].to_numpy(), slope[m])[0, 1] > 0.9
    part = trend_z(close.iloc[:1300])
    pd.testing.assert_series_equal(z.iloc[:1300], part)                   # 先の値を使っていない
    prior = close.copy()                                                  # 別の系列（1 日前にずらした同じ値）で推定しても形は同じ
    prior.index = prior.index - pd.Timedelta(days=1)
    z2 = trend_z(close, prior_close=prior)
    assert z2.notna().sum() == z.notna().sum() and np.corrcoef(z2[m], z[m])[0, 1] > 0.95


def test_kalman_strategy_enters_with_the_trend_and_exits_on_sign_change():
    rng = np.random.default_rng(2)
    n = 1200
    drift = np.where(np.arange(n) < 800, 0.003, -0.003)                   # 上げ → 下げ
    y = np.cumsum(drift + rng.normal(0, 0.01, n))
    idx = pd.bdate_range("2015-01-01", periods=n, tz="UTC")
    close = np.exp(y)
    df = pd.DataFrame({"open": close, "high": close * 1.005, "low": close * 0.995, "close": close}, index=idx)
    sig = make_strategy("kalman").generate(df, ind.atr(df, 20))
    e = sig["entry"]
    assert (e.iloc[:500] == 0).all()                                      # 最初の推定までは入らない
    up, down = e.iloc[520:780], e.iloc[900:]
    assert (up == 1).sum() > 20 and (up == 1).sum() > 3 * (up == -1).sum()        # 上げでは買い
    assert (down == -1).sum() > 20 and (down == -1).sum() > 3 * (down == 1).sum()  # 下げでは売り
    assert sig["exit_long"].iloc[900:].mean() > 0.6 and sig["exit_long"].iloc[600:780].mean() < 0.5   # 符号で手仕舞い
    assert sig["exit_short"].iloc[600:780].mean() > 0.5 and sig["exit_short"].iloc[900:].mean() < 0.5
