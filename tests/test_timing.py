import numpy as np
import pandas as pd

from cfdbot.timing import (max_drawdown, month_ends, placebo_sharpes, sharpe, sma_weight, strategy_returns,
                           vol_managed_weight)


def _index(n=3000, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2000-01-03", periods=n, tz="UTC")
    vol = np.where((np.arange(n) // 500) % 2 == 0, 0.008, 0.025)        # 静かな期間と荒い期間が交互
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0003, vol))), index=idx)


def test_vol_managed_weight_is_lower_when_recent_volatility_is_high():
    c = _index()
    w = vol_managed_weight(c)
    assert (w >= 0).all() and (w <= 1.5 + 1e-9).all()
    me = month_ends(c.index)
    changes = (w.diff().abs() > 0).to_numpy()
    assert changes[1:][~me[:-1]].sum() == 0                            # 月末の翌日以外は変わらない
    quiet = w.iloc[2100:2400].mean()                                   # 静かな期間（2000〜2499）
    rough = w.iloc[2600:2900].mean()                                   # 荒い期間（2500〜2999）
    assert quiet > rough


def test_sma_weight_is_one_in_uptrends_and_zero_in_downtrends():
    idx = pd.bdate_range("2000-01-03", periods=1200, tz="UTC")
    up_then_down = np.r_[np.linspace(100, 200, 700), np.linspace(200, 120, 500)]
    c = pd.Series(up_then_down, index=idx)
    w = sma_weight(c)
    assert set(w.unique()) <= {0.0, 1.0}
    assert w.iloc[400:690].mean() > 0.95 and w.iloc[1000:].mean() < 0.05


def test_strategy_returns_charge_costs_and_hold_equals_index():
    c = _index()
    hold = strategy_returns(c, pd.Series(1.0, index=c.index))
    assert abs(hold.iloc[5] - c.pct_change().iloc[5]) < 1e-12 and hold.iloc[0] == -0.00025   # 最初に買うコスト
    w = sma_weight(c)
    r = strategy_returns(c, w)
    assert np.isfinite(sharpe(r)) and -1 < max_drawdown(r) <= 0
    pl = placebo_sharpes(c, w, np.ones(len(c), bool), n=4)
    assert len(pl) == 4
