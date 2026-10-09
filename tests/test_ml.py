import numpy as np
import pandas as pd
import pytest

import cfdbot.ml as ml
from cfdbot.ml import (FEATURES, Panel, PolicyModel, build_panel, fit_policy, market_frame, rule_positions,
                       sharpe_and_grad, shift_targets, split_markets, strategy_returns, turtle_state, walk_forward)


def _closes(n=900, k=3, seed=0, start="2005-01-03"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    return {f"M{i}": pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, n))), index=idx) for i in range(k)}


def _panel(**kw) -> Panel:
    c = _closes(**kw)
    return build_panel(c, {k: (0.001, 0.03) for k in c})


@pytest.mark.parametrize("hidden", [0, 4])
def test_sharpe_gradient_matches_finite_differences(hidden):
    panel = _panel()
    panel.x = panel.x.astype(np.float64)
    rng = np.random.default_rng(1)
    params = {k: v.astype(np.float64) for k, v in ml._init(panel.x.shape[1], hidden, rng).items()}
    for v in params.values():
        v += rng.normal(size=v.shape) * 0.2
    model = PolicyModel(hidden, params)
    _, g = sharpe_and_grad(model, panel, l2=0.0)
    for k, v in model.params.items():
        i = (1,) * v.ndim if v.size > 1 else (0,)
        old, eps = v[i], 1e-6
        v[i] = old + eps
        s1, _ = sharpe_and_grad(model, panel, 0.0)
        v[i] = old - eps
        s0, _ = sharpe_and_grad(model, panel, 0.0)
        v[i] = old
        assert g[k][i] == pytest.approx((s1 - s0) / (2 * eps), rel=1e-3, abs=1e-4)


def test_turtle_state_follows_breakouts_and_exits():
    up = list(np.linspace(100, 90, 60)) + list(np.linspace(91, 130, 30)) + list(np.linspace(129, 100, 30))
    s = turtle_state(pd.Series(up), entry=55, exit_=20)
    assert s[:60].max() <= 0
    first_long = int(np.argmax(s > 0))
    assert 60 <= first_long < 70 and s[89] == 1          # 55 日の高値を超えてから買い
    assert s[-1] <= 0                                     # 20 日の安値を割ったら手仕舞い


def test_market_frame_uses_only_past_prices():
    c = _closes(k=1)["M0"]
    f1 = market_frame(c)
    c2 = c.copy()
    c2.iloc[500:] *= 1.5                                  # 先の値を変えても
    f2 = market_frame(c2)
    pd.testing.assert_frame_equal(f1.iloc[:499][list(FEATURES)], f2.iloc[:499][list(FEATURES)])   # 前の入力は変わらない
    assert f1["next_ret"].iloc[10] == pytest.approx(c.iloc[11] / c.iloc[10] - 1)


def test_strategy_returns_charge_trading_and_holding_costs():
    p = Panel(x=np.zeros((3, 1), np.float32), next_ret=np.array([0.01, -0.02, 0.0]), lev=np.array([2.0, 2.0, 1.0]),
              cost=np.full(3, 0.001), fin=np.full(3, 0.0001), market=np.array([0, 0, 1]),
              dates=np.array(["2020-01-01", "2020-01-02", "2020-01-01"], dtype="datetime64[ns]"), keys=["a", "b"],
              first=np.array([True, False, True]))
    r = strategy_returns(np.array([1.0, 0.5, -1.0]), p)
    assert r[0] == pytest.approx(2 * 0.01 - 0.001 * 2 - 0.0001 * 2)
    assert r[1] == pytest.approx(1 * -0.02 - 0.001 * 1 - 0.0001 * 1)
    assert r[2] == pytest.approx(0.0 - 0.001 * 1 - 0.0001 * 1)          # 別の市場は前日 0 から


def test_fit_policy_learns_a_planted_signal():
    rng = np.random.default_rng(2)
    n = 6000
    x = rng.normal(size=(n, 3)).astype(np.float32)
    nr = 0.004 * np.sign(x[:, 0]) + rng.normal(0, 0.01, n)
    p = Panel(x, nr, np.ones(n), np.zeros(n), np.zeros(n), np.zeros(n, int),
              pd.bdate_range("2000-01-03", periods=n).to_numpy(), ["a"], np.r_[True, np.zeros(n - 1, bool)])
    model = fit_policy(p, None, hidden=0, iters=200, lr=0.05)
    w = model.params["w"]
    assert w[0] > 5 * max(abs(w[1]), abs(w[2]))
    assert ml.ann_sharpe(strategy_returns(model.positions(x), p)) > 3


def test_walk_forward_trains_only_on_earlier_data(monkeypatch):
    panel = _panel(n=2600, start="2005-01-03")
    seen = []
    real = ml.fit_policy

    def spy(train, valid, **kw):
        seen.append((train.dates.max(), valid.dates.max() if valid is not None else None))
        return real(train, valid, **kw)

    monkeypatch.setattr(ml, "fit_policy", spy)
    pos = walk_forward(panel, 2012, 2014, step=2, iters=5)
    years = panel.dates.astype("datetime64[Y]").astype(int) + 1970
    assert np.isnan(pos[years < 2012]).all() and np.isfinite(pos[years >= 2012]).all()
    for (tmax, vmax), y0 in zip(seen, (2012, 2014)):
        assert tmax < np.datetime64(f"{y0 - 3}-01-01") and vmax < np.datetime64(f"{y0}-01-01")


def test_shift_targets_and_market_split():
    panel = _panel()
    fake = shift_targets(panel, np.random.default_rng(0))
    for mi in np.unique(panel.market):
        a, b = panel.next_ret[panel.market == mi], fake.next_ret[panel.market == mi]
        assert sorted(a) == sorted(b) and not np.allclose(a, b)
    groups = {f"K{i}": ("g1" if i < 6 else "g2") for i in range(10)}
    a, b = split_markets(list(groups), groups)
    assert a.isdisjoint(b) and a | b == set(groups)
    assert sum(groups[k] == "g1" for k in a) == 3 and sum(groups[k] == "g2" for k in a) == 2


def test_rule_positions_ranges():
    panel = _panel()
    assert set(np.unique(rule_positions(panel, "tsmom"))) <= {-1.0, 0.0, 1.0}
    m = rule_positions(panel, "macd")
    assert m.min() >= -1 and m.max() <= 1
    assert (rule_positions(panel, "long") == 1).all()
