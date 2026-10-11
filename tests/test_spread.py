import numpy as np
import pandas as pd

from cfdbot.spread import (ou_fit, ou_state, pair_returns, placebo_sharpes, positions, sharpe, spread_series)


def _ou(n: int, theta: float, mu: float, sigma: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    s = np.empty(n)
    s[0] = mu
    for i in range(1, n):
        s[i] = s[i - 1] + theta * (mu - s[i - 1]) + rng.normal(0, sigma)
    return s


def test_ou_fit_recovers_theta_mu_and_half_life():
    s = _ou(4000, 0.05, 3.0, 0.5, 0)
    f = ou_fit(s)
    assert 0.03 < f["theta"] < 0.07 and 2.5 < f["mu"] < 3.5
    assert abs(f["half_life"] - np.log(2) / 0.05) < 6
    assert abs(f["sigma_eq"] - 0.5 / np.sqrt(1 - 0.95**2)) < 0.3
    rw = np.cumsum(np.random.default_rng(1).normal(0, 1, 2000))              # 戻る力の無いランダムウォーク
    assert not np.isfinite(ou_fit(rw)["theta"]) or ou_fit(rw)["half_life"] > 200


def test_ou_state_uses_only_the_past_and_refits_on_schedule():
    idx = pd.bdate_range("2010-01-01", periods=1500, tz="UTC")
    s = pd.Series(_ou(1500, 0.05, 0.0, 1.0, 2), index=idx)
    st = ou_state(s, window=500, refit=20)
    assert st["z"].iloc[:500].isna().all() and st["z"].iloc[500:].notna().all()
    assert (st["mu"].iloc[500:520].nunique() == 1) and (st["mu"].iloc[500] != st["mu"].iloc[520])   # 20 日ごとに推定し直す
    part = ou_state(s.iloc[:900], window=500, refit=20)
    pd.testing.assert_frame_equal(st.iloc[:900], part)                                         # 先の値を使っていない


def test_positions_follow_the_entry_exit_stop_and_half_life_rules():
    z = np.array([0.0, 2.5, 1.0, 0.5, -0.1, -2.5, -1.0, 0.2, 2.5, 4.5, 1.0, np.nan, 2.5])
    hl = np.full(len(z), 30.0)
    hl[0:1] = 300.0                                                       # 範囲外の時は新規に入らない
    st = pd.DataFrame({"z": z, "half_life": hl})
    pos = positions(st)
    assert list(pos) == [0, -1, -1, -1, 0, 1, 1, 0, -1, 0, 0, 0, -1]
    #            z>2 売り、持ち続け、符号が変わって手仕舞い、z<-2 買い、手仕舞い、売り、|z|>4 損切り、無し、NaN は無し、再び売り
    st2 = pd.DataFrame({"z": [2.5, 2.5], "half_life": [3.0, 200.0]})
    assert list(positions(st2)) == [0, 0]                                 # 半減期が短すぎ・長すぎでは入らない


def test_pair_returns_charge_both_legs_and_cap_leverage():
    idx = pd.bdate_range("2020-01-01", periods=80, tz="UTC")
    p1 = pd.Series(np.linspace(50.0, 50.0, 80), index=idx)
    p2 = pd.Series(np.linspace(40.0, 40.0, 80), index=idx)
    p1.iloc[:] = 50 + np.sin(np.arange(80)) * 2                             # 差が揺れる（量を決めるための変動）
    s = spread_series(p1, p2, "diff")
    pos = np.zeros(80, np.int8)
    pos[70] = pos[71] = 1                                                 # 70 日目の終値で差を買い、72 日目の終値で手仕舞い
    res = pair_returns(p1, p2, s, pos.copy(), "diff", cost=0.001, financing=0.0)
    units = res["units"].iloc[70]
    assert units > 0 and res["units"].iloc[71] == units and res["units"].iloc[72] == 0
    assert res["lev"].iloc[70] <= 2.0 + 1e-9
    n70, n72 = p1.iloc[70] + p2.iloc[70], p1.iloc[72] + p2.iloc[72]
    expect = units * ((p1.iloc[71] - p1.iloc[70]) - (p2.iloc[71] - p2.iloc[70])) - units * n70 * 0.001 / 2
    assert abs(res["ret"].iloc[71] - expect) < 1e-12                       # 1 日目の損益 − 入るときの片道のコスト
    assert abs(res["ret"].iloc[72] - units * (p1.iloc[72] - p1.iloc[71])) < 1e-12   # 2 日目の損益（p2 は動かない）
    assert abs(res["ret"].iloc[73] + units * n72 * 0.0005) < 1e-12         # 手仕舞いの片道のコスト（73 日目の始値で約定）
    assert res["ret"].iloc[74] == 0 and res["ret"].iloc[70] == 0
    lr = pair_returns(p1, p2, spread_series(p1, p2, "logratio"), pos.copy(), "logratio", cost=0.0, financing=0.0)
    assert abs(lr["lev"].iloc[70] - 2 * lr["units"].iloc[70]) < 1e-12       # 対数の差: 1 単位 = 両足 1 ドルずつ


def test_mean_reverting_spread_earns_and_placebo_runs():
    idx = pd.bdate_range("2005-01-01", periods=2500, tz="UTC")
    rng = np.random.default_rng(5)
    base = 60 + np.cumsum(rng.normal(0, 0.8, 2500))
    p2 = pd.Series(base, index=idx)
    p1 = pd.Series(base + _ou(2500, 0.08, 2.0, 0.6, 6), index=idx)      # 差が強く戻る
    s = spread_series(p1, p2, "diff")
    st = ou_state(s)
    pos = positions(st)
    res = pair_returns(p1, p2, s, pos.copy(), "diff")
    mask = st["z"].notna().to_numpy()
    assert sharpe(res["ret"][mask]) > 1.0
    pl = placebo_sharpes(p1, p2, s, pos, "diff", mask, n=10)
    assert len(pl) == 10 and np.mean(pl < sharpe(res["ret"][mask])) > 0.8
