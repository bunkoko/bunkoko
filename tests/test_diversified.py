import numpy as np
import pandas as pd

from cfdbot.diversified import (annual_vol, month_ends, placebo_sharpes, portfolio_returns, positions, returns, sharpe,
                                tsmom_sign)


def _closes(n=900, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=n, tz="UTC")
    up = 100 * np.exp(np.cumsum(rng.normal(0.002, 0.01, n)))          # 上げ続ける
    down = 100 * np.exp(np.cumsum(rng.normal(-0.002, 0.01, n)))       # 下げ続ける
    return pd.DataFrame({"UP": up, "DOWN": down}, index=idx)


def test_sign_vol_and_month_ends():
    c = _closes()
    s = tsmom_sign(c)
    assert s.iloc[:21].isna().all().all() and s.iloc[300:]["UP"].mean() > 0.4 and s.iloc[300:]["DOWN"].mean() < -0.4
    v = annual_vol(c)
    assert 0.1 < v.iloc[-1]["UP"] < 0.25
    me = month_ends(c.index)
    assert me[-1] and me.sum() == len(c.index.to_period("M").unique())
    neg = pd.DataFrame({"X": [10.0, -20.0, 5.0]}, index=pd.bdate_range("2020-01-01", periods=3, tz="UTC"))
    r = returns(neg)["X"]
    assert abs(r.iloc[1] - (-3.0)) < 1e-12 and abs(r.iloc[2] - 1.25) < 1e-12   # −20 → 5 は買いの利益


def test_positions_hold_for_a_month_and_split_one_over_n():
    c = _closes()
    pos = positions(c)
    assert (pos.iloc[:260] == 0).all().all()                           # 12 か月たつまでは持たない
    late = pos.iloc[400:]
    assert (late["UP"] > 0).mean() > 0.8 and (late["DOWN"] < 0).mean() > 0.8
    me = month_ends(c.index)
    changes = (pos.diff().abs().sum(axis=1) > 0).to_numpy()
    assert changes[1:][~me[:-1]].sum() == 0                            # 月末の翌日以外は変わらない
    one = positions(c[["UP"]])
    assert abs(one["UP"].iloc[-1] - 2 * pos["UP"].iloc[-1]) < 1e-9     # 1/N: 2 市場なら半分


def test_portfolio_returns_charge_costs_and_trend_earns():
    c = _closes()
    pos = positions(c)
    groups = {"UP": "fx", "DOWN": "index"}
    res = portfolio_returns(c, pos, groups)
    free = portfolio_returns(c, pos, {"UP": "fx", "DOWN": "fx"})
    assert res["ret"].sum() < free["ret"].sum() or groups["DOWN"] == "fx"
    assert sharpe(res["ret"].iloc[300:]) > 1.0                        # 上げ・下げが続くなら勝つ
    assert (res["gross"] >= 0).all() and res["turnover"].iloc[300:].sum() > 0
    pl = placebo_sharpes(c, pos, groups, np.asarray(c.index >= c.index[300]), n=5)
    futures = portfolio_returns(c, pos, groups, financing=False)
    assert futures["ret"].sum() > res["ret"].sum()                    # 先物なら保有コストが無い
    assert len(pl) == 5
