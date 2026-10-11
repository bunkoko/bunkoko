import numpy as np
import pandas as pd

from cfdbot.carry import (basis, evaluate, fetch_curve, front_returns, placebo_sharpes, rule_expiry, sharpe, signals,
                          switch_days)


def _curve(n_months=36, seed=0, back=0.02):
    """期近が期先より back だけ高い（バックワーデーション）曲線。毎月 20 日ごろに限月 1 が切り替わる。"""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2015-01-02", periods=n_months * 21)
    base = 60 * np.exp(np.cumsum(rng.normal(0.0, 0.015, len(days))))
    c1 = np.empty(len(days))
    c2 = np.empty(len(days))
    c3 = np.empty(len(days))
    c4 = np.empty(len(days))
    k = 0.0                                      # 切り替わるごとに期近が次の限月（低い）になる
    for i, d in enumerate(days):
        if i > 0 and d.day >= 20 and days[i - 1].day < 20:
            k += 1
        c1[i] = base[i] * (1 - back) ** k * 1.0
        c2[i] = c1[i] * (1 - back)
        c3[i] = c2[i] * (1 - back)
        c4[i] = c3[i] * (1 - back)
    return pd.DataFrame({"c1": c1, "c2": c2, "c3": c3, "c4": c4}, index=pd.DatetimeIndex(days, name="date"))


def test_switch_days_are_found_from_data_and_returns_skip_the_roll_jump():
    cv = _curve()
    sw = switch_days(cv)
    assert sw.attrs["by_data"] > 0.9 and 33 <= sw.sum() <= 37
    fr = front_returns(cv)
    assert set(fr["held"].unique()) == {1, 2}
    # 切り替わる日: 前日の限月 2 → 当日の限月 1 なので、値段の 2% の飛びが損益に出ない
    i = np.flatnonzero(sw.to_numpy())[5]
    assert abs(fr["ret"].iloc[i]) < 0.1
    big = (fr["ret"].abs() > 0.08).sum()
    assert big < 5                                                   # 1.5% の日次の揺れなら 8% 超はほぼ無い
    adj = (1 + fr["ret"].fillna(0)).cumprod()
    assert adj.iloc[-1] > cv["c1"].iloc[-1] / cv["c1"].iloc[0]       # バックワーデーションではロールの利益が足される


def test_basis_and_rule_expiry():
    cv = _curve()
    b = basis(cv)
    assert (b.dropna() > 0).all() and abs(b.iloc[10] - (-np.log(0.98))) < 1e-9
    neg = cv.copy()
    neg.loc[neg.index[3], "c1"] = -5.0
    assert np.isnan(basis(neg).iloc[3])
    assert rule_expiry(2026, 7) == pd.Timestamp("2026-06-22")        # 7 月限: 6 月 25 日（木）の 3 営業日前
    assert rule_expiry(2026, 6) == pd.Timestamp("2026-05-19")        # 6 月限: 5 月 25 日は祝日 → 22 日（金）の 3 営業日前


def test_signals_follow_carry_and_trend_and_combo_requires_agreement():
    cv = _curve(seed=1)
    fr = front_returns(cv)
    sig = signals(cv, fr)
    assert set(sig.columns) == {"carry", "trend", "combo"}
    assert (sig["carry"].iloc[30:] == 1).all()                       # ずっとバックワーデーション → 買い
    assert set(sig["combo"].unique()) <= {-1.0, 0.0, 1.0}
    agree = sig["combo"] != 0
    assert (sig.loc[agree, "combo"] == sig.loc[agree, "trend"]).all()
    assert (sig.loc[sig["trend"] == -1, "combo"] == 0).all()         # 向きが揃わなければ持たない
    me_changes = (sig.diff().abs().sum(axis=1) > 0)
    assert me_changes.sum() <= 40                                   # 月に 1 回しか変わらない


def test_evaluate_sizes_to_target_and_charges_costs():
    cv = _curve(seed=2)
    fr = front_returns(cv)
    pos = pd.Series(1.0, index=cv.index)
    res = evaluate(fr, pos, financing=0.0, cost=0.0)
    r = res["ret"].iloc[100:]
    assert 0.05 < r.std() * np.sqrt(252) < 0.2 and (res["lev"] <= 2.0 + 1e-9).all()
    with_cost = evaluate(fr, pos, financing=0.03, cost=0.001)
    assert with_cost["ret"].sum() < res["ret"].sum()
    pl = placebo_sharpes(fr, pos, np.ones(len(cv), bool), n=3)
    assert len(pl) == 3 and np.isfinite(sharpe(res["ret"]))


def test_fetch_curve_merges_the_four_series():
    def fake(code):
        d = pd.bdate_range("2020-01-01", periods=5)
        v = {"RCLC1": 50, "RCLC2": 49, "RCLC3": 48, "RCLC4": 47}[code]
        return pd.DataFrame({"date": d.strftime("%Y-%m-%d"), "value": [v] * 5})
    cv = fetch_curve(fake)
    assert list(cv.columns) == ["c1", "c2", "c3", "c4"] and len(cv) == 5 and cv["c2"].iloc[0] == 49
