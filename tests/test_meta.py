from dataclasses import replace
from itertools import combinations

import numpy as np
import pandas as pd
import pytest

from cfdbot.backtest import BacktestConfig, FilterConfig, Sleeve, run_backtest
from cfdbot.context import from_bars
from cfdbot.indicators import extension_z
from cfdbot.instruments import get_instruments
from cfdbot.meta import (DAY_NS, LogisticModel, classification, cpcv, feature_columns, fit_logistic,
                         meta_feature_frame, meta_gates, meta_rows, meta_weights, uniqueness)
from cfdbot.stats import (deflated_threshold, expected_max_z, hhi, longest_underwater_days, moments, psr,
                          psr_of)

from .conftest import FixedSignals


# --------------------------------------------------------------------------- 統計
def test_expected_max_z_grows_with_trials():
    vals = [expected_max_z(n) for n in (1, 2, 10, 18, 100, 1000)]
    assert vals[0] == 0.0
    assert all(a < b for a, b in zip(vals, vals[1:]))
    assert vals[3] == pytest.approx(1.85, abs=0.01)
    # 乱数で確かめる（1000 回の最大値の平均）
    rng = np.random.default_rng(1)
    assert rng.normal(size=(4000, 18)).max(axis=1).mean() == pytest.approx(vals[3], abs=0.1)


def test_psr_basic_properties():
    assert psr(0.0, 500) == pytest.approx(0.5)
    assert psr(0.05, 1000) > psr(0.05, 100) > 0.5
    assert psr(0.05, 1000, skew=-2, kurt=12) < psr(0.05, 1000)      # 左に歪んで裾が厚いと確信は下がる
    rng = np.random.default_rng(2)
    r = rng.normal(0.001, 0.01, 2000)
    sr, n, sk, ku = moments(r)
    assert n == 2000 and abs(sk) < 0.2 and ku == pytest.approx(3, abs=0.4)
    assert psr_of(r) == pytest.approx(psr(sr, n, sk, ku))


def test_deflated_threshold_is_mean_plus_scaled_std():
    v = np.array([-0.1, 0.0, 0.1, 0.2])
    assert deflated_threshold(v, 1) == pytest.approx(v.mean())
    assert deflated_threshold(v, 18) == pytest.approx(v.mean() + v.std(ddof=1) * expected_max_z(18))


def test_hhi_and_underwater():
    assert hhi([1, 1, 1, 1]) == pytest.approx(0.0)
    assert hhi([0, 0, 0, 5]) == pytest.approx(1.0)
    assert np.isnan(hhi([1, 2]))
    eq = pd.Series([1.0, 2.0, 1.5, 1.8, 2.1, 2.0, 1.9], index=pd.date_range("2020-01-01", periods=7))
    assert longest_underwater_days(eq) == 3.0          # 1/2 の高値 → 1/5 に更新
    assert longest_underwater_days(pd.Series([1.0, 2.0, 3.0], index=eq.index[:3])) == 0.0


# --------------------------------------------------------------------------- 重み
def test_uniqueness_and_weights():
    s = np.array([0, 0, 10]) * DAY_NS
    e = np.array([4, 4, 12]) * DAY_NS
    assert uniqueness(s, e) == pytest.approx([0.5, 0.5, 1.0])
    # 半分だけ重なる: 0〜3 日と 2〜5 日 → 4 日のうち 2 日が 1/2
    assert uniqueness(np.array([0, 2]) * DAY_NS, np.array([3, 5]) * DAY_NS) == pytest.approx([0.75, 0.75])
    w = meta_weights(np.array([-1.0, 2.0, 10.0]), s, e)
    assert w.mean() == pytest.approx(1.0)
    raw = np.array([1 * 0.5, 2 * 0.5, 5 * 1.0])         # 10R は 5 で頭打ち
    assert w == pytest.approx(raw / raw.mean())


# --------------------------------------------------------------------------- 2 次モデル
def test_logistic_recovers_direction_and_shrinks_to_base_rate():
    rng = np.random.default_rng(0)
    x = pd.DataFrame({"a": rng.normal(size=3000), "b": rng.normal(size=3000)})
    y = (rng.random(3000) < 1 / (1 + np.exp(-(0.8 * x["a"] - 0.3)))).astype(float)
    m = fit_logistic(x, y, lam_per_row=0.0)
    assert m.importance["a"] == pytest.approx(0.8, abs=0.1)
    assert abs(m.importance["b"]) < 0.1
    heavy = fit_logistic(x, y, lam_per_row=1000.0)
    assert heavy.prob(x) == pytest.approx(np.full(3000, y.mean()), abs=0.01)
    # 重みは「勝ちの割合」を重み付きにする（重い勝ちが多ければ 0.5 を超える）
    w = np.where(y > 0, 3.0, 1.0)
    assert fit_logistic(x, y, w, lam_per_row=1000.0).base_rate == pytest.approx(
        3 * y.sum() / (3 * y.sum() + (1 - y).sum()))


def test_missing_inputs_count_as_average():
    m = LogisticModel(["a"], np.array([1.0]), np.array([2.0]), np.array([1.0]), 0.0)
    assert m.prob(pd.DataFrame({"a": [np.nan]}))[0] == pytest.approx(0.5)
    assert m.prob(pd.DataFrame({"a": [3.0]}))[0] == pytest.approx(1 / (1 + np.exp(-1)))


# --------------------------------------------------------------------------- CPCV
def test_cpcv_combinations_paths_purge_and_embargo():
    decide = np.arange(60) * DAY_NS
    exit_ = decide + 3 * DAY_NS
    cv = cpcv(decide, exit_, n_groups=6, n_test=2, embargo_ns=2 * DAY_NS)
    assert len(cv.combos) == 15
    paths = cv.paths()
    assert len(paths) == 5                                     # φ = k/N × C(N,k) = 2/6 × 15
    for g in range(6):
        used = [p[g] for p in paths]
        assert len(set(used)) == 5 and all(g in cv.combos[c] for c in used)
    for c, train in zip(cv.combos, cv.train):
        assert not np.isin(cv.groups[train], c).any()
        for g in c:
            lo, hi = cv.bounds[g]
            assert not ((decide[train] < hi) & (exit_[train] >= lo)).any()          # 重なる取引は無い
            if g < 5:
                assert not ((decide[train] >= hi) & (decide[train] < hi + 2 * DAY_NS)).any()
    # 組み合わせ (0, 1): 学習はグループ 2〜5 の 40 回から、直後 2 日（2 回）を除いた 38 回
    assert len(cv.train[cv.combos.index((0, 1))]) == 38
    assert cv.combos == list(combinations(range(6), 2))


# --------------------------------------------------------------------------- 入力
def _daily(n=400, seed=0, start="2020-01-01"):
    idx = pd.date_range(start, periods=n, freq="D", tz="UTC")
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame({"open": close, "high": close * 1.005, "low": close * 0.995, "close": close}, index=idx)


def test_meta_features_match_definitions_and_flip_with_side():
    df = _daily()
    partner = _daily(seed=1)
    tf = pd.Timedelta("1D")
    ff = meta_feature_frame("GOLD", df, tf, from_bars("SILVER", partner, tf))
    at = np.array([300, 350])
    long_rows, short_rows = ff.rows(1, at), ff.rows(-1, at)
    ext = extension_z(df["close"], 20, 252).to_numpy()[at]
    assert long_rows["ext20"].to_numpy() == pytest.approx(ext)
    assert short_rows["ext20"].to_numpy() == pytest.approx(-ext)
    assert short_rows["er55"].to_numpy() == pytest.approx(long_rows["er55"].to_numpy())   # 向きのない入力
    assert ((long_rows["er55"] >= 0) & (long_rows["er55"] <= 1)).all()
    pext = extension_z(partner["close"], 20, 252).to_numpy()[at]
    assert long_rows["partner_ext20"].to_numpy() == pytest.approx(pext)
    alone = meta_feature_frame("GOLD", df, tf, None)
    assert alone.rows(1, at)["partner_ext20"].isna().all()


def test_meta_rows_use_the_decision_bar():
    df = _daily()
    tf = pd.Timedelta("1D")
    ff = meta_feature_frame("GOLD", df, tf)
    trades = pd.DataFrame({"symbol": ["GOLD", "GOLD"], "side": [1, -1],
                           "entry_time": [df.index[301], df.index[351]],      # 判断した足の次の足の始まり
                           "exit_time": [df.index[320], df.index[380]], "r_multiple": [2.0, -1.0]})
    rows = meta_rows(trades, {"GOLD": ff})
    assert list(rows.columns[:len(feature_columns())]) == feature_columns()
    assert rows["decide_ns"].tolist() == [ff.decide[300], ff.decide[350]]
    assert rows["ext20"].to_numpy() == pytest.approx([ff.directional["ext20"][300], -ff.directional["ext20"][350]])
    assert rows["r"].tolist() == [2.0, -1.0]


def test_meta_gates_skip_and_half():
    df = _daily()
    tf = pd.Timedelta("1D")
    ff = meta_feature_frame("GOLD", df, tf)
    # ext20 がプラス（買いなら上げた後）ほど勝ちやすいとするモデル
    model = LogisticModel(["ext20"], np.array([0.0]), np.array([1.0]), np.array([5.0]), 0.0)
    ext = ff.directional["ext20"].to_numpy()
    lo = int(ff.decide[260])
    skip = meta_gates({"GOLD": ff}, [(lo, np.iinfo(np.int64).max, model)], "skip")["GOLD"]
    ok = np.isfinite(ext) & (np.arange(len(ext)) >= 260)
    assert (skip["long"].to_numpy()[ok] == (ext[ok] > 0)).all()
    assert (skip["short"].to_numpy()[ok] == (ext[ok] < 0)).all()
    assert skip.iloc[:260].all().all()                         # モデルの無い期間は今まで通り
    half = meta_gates({"GOLD": ff}, [(lo, np.iinfo(np.int64).max, model)], "half")["GOLD"]
    assert half[["long", "short"]].all().all()
    assert (half["long_size"].to_numpy()[ok] == np.where(ext[ok] > 0, 1.0, 0.5)).all()


def test_classification_counts():
    p = np.array([0.9, 0.8, 0.3, 0.2, np.nan])
    r = np.array([2.0, -1.0, 1.0, -1.0, 5.0])
    c = classification(p, r)
    assert c["n"] == 4 and c["win_rate"] == 0.5 and c["accepted"] == 0.5
    assert c["precision"] == 0.5 and c["recall"] == 0.5
    assert c["r_accepted"] == pytest.approx(0.5) and c["r_rejected"] == pytest.approx(0.0)
    assert classification(np.array([0.9, 0.1]), np.array([1.0, -1.0]))["auc"] == 1.0


# --------------------------------------------------------------------------- バックテストの量の調整
def test_entry_size_multiplier_scales_risk(oil_df):
    df = oil_df.iloc[:300]
    inst = {"WTI": replace(get_instruments("phillip")["WTI"], min_qty=1, qty_step=1)}
    strat = FixedSignals(entries={100: 1, 200: -1}, stop_dist=2.0)
    cfg = BacktestConfig(initial_equity=10_000_000, filters=FilterConfig(oil_events=False, no_entry_after_fri_et=None))
    full = run_backtest({"WTI": df}, inst, [Sleeve("WTI", strat)], cfg).trades
    gate = pd.DataFrame({"long_size": 0.5, "short_size": 1.0}, index=df.index)
    half = run_backtest({"WTI": df}, inst, [Sleeve("WTI", strat, entry_gate=gate)], cfg).trades
    assert len(full) == len(half) == 2
    q_full = full.set_index("side")["qty"]
    q_half = half.set_index("side")["qty"]
    assert q_half[1] == pytest.approx(q_full[1] * 0.5, abs=1)
    assert q_half[-1] == q_full[-1]
    gate0 = pd.DataFrame({"long_size": 0.0, "short_size": 1.0}, index=df.index)
    res = run_backtest({"WTI": df}, inst, [Sleeve("WTI", strat, entry_gate=gate0)], cfg)
    assert set(res.trades["side"]) == {-1}
