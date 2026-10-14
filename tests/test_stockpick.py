import numpy as np
import pandas as pd

from cfdbot.stockpick import (basket_returns, month_ends, monthly_picks, monthly_returns, random_pick_excess, scores,
                           sharpe_monthly, universe_returns)


def _closes(n_stocks=50, days=1500, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=days, tz="UTC")
    drift = np.linspace(-0.0005, 0.0015, n_stocks)                   # 銘柄ごとに流れが違う（続く）
    vol = np.linspace(0.03, 0.01, n_stocks)
    data = {f"{1000 + i}.T": 100 * np.exp(np.cumsum(rng.normal(drift[i], vol[i], days))) for i in range(n_stocks)}
    return pd.DataFrame(data, index=idx)


def test_scores_rank_momentum_and_low_vol_between_0_and_1():
    c = _closes()
    sc = scores(c)
    assert sc.iloc[:252].isna().all().all()
    last = sc.iloc[-1].dropna()
    assert len(last) == 50 and 0 < last.min() and last.max() <= 1.0
    assert last["1049.T"] > last["1000.T"]                           # 上げ続け・変動小 の銘柄が上位


def test_monthly_picks_choose_top_n_each_month_end_and_returns_line_up():
    c = _closes()
    picks = monthly_picks(c)
    me = month_ends(c.index)
    assert len(picks) == me.sum()
    counts = picks.sum(axis=1)
    assert set(counts.unique()) <= {0, 20} and (counts.iloc[-12:] == 20).all()
    mret = monthly_returns(c)
    assert mret.index.equals(picks.index) and mret.iloc[-1].isna().all()   # 最後の月末の「翌月」はまだ無い
    top = basket_returns(picks, mret)
    bench = universe_returns(c, mret)
    r = (top - bench).dropna()
    assert len(r) > 30 and sharpe_monthly(r) > 0.5                   # 流れが続く合成データでは選ぶ価値がある
    pl = random_pick_excess(c, mret, bench, n=5)
    assert len(pl) == 5 and np.mean(pl < sharpe_monthly(r)) >= 0.8


def test_basket_returns_charge_turnover():
    idx = pd.date_range("2020-01-31", periods=3, freq="ME", tz="UTC")
    picks = pd.DataFrame([[True, False], [False, True], [False, True]], index=idx, columns=["A", "B"])
    mret = pd.DataFrame([[0.10, 0.0], [0.0, 0.10], [0.0, 0.0]], index=idx, columns=["A", "B"])
    r = basket_returns(picks, mret, cost=0.01)
    assert abs(r.iloc[0] - (0.10 - 0.5 * 0.01)) < 1e-12              # 最初に全部買う（片側 50% 相当）
    assert abs(r.iloc[1] - (0.10 - 1.0 * 0.01)) < 1e-12              # A → B に全部入れ替え
    assert abs(r.iloc[2] - 0.0) < 1e-12                              # 入れ替え無し


def test_clean_scale_jumps_keeps_only_data_after_a_relisting_jump():
    from cfdbot.stockpick import clean_scale_jumps

    idx = pd.bdate_range("2012-09-10", periods=8, tz="UTC")
    s = pd.Series([2.0, 2.0, 2.0, 2.0, 3790.0, 3800.0, 3700.0, 3750.0], index=idx)   # JAL の再上場の形
    out, n = clean_scale_jumps(s)
    assert n == 1 and out.index[0] == idx[4] and len(out) == 4
    s2 = pd.Series(np.linspace(100, 180, 8), index=idx)                              # 普通の上げは触らない
    out2, n2 = clean_scale_jumps(s2)
    assert n2 == 0 and len(out2) == 8
