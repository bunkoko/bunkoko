import numpy as np
import pandas as pd
import pytest

from cfdbot.backtest import BacktestConfig, FilterConfig, Sleeve, run_backtest
from cfdbot.context import yahoo_csv_frame
from cfdbot.strategies import make_strategy
from cfdbot.universe import (BY_KEY, GROUPS, UNIVERSE, at_vol, day_returns, group_portfolio, market_instrument,
                             portfolio, rank_corr, selection_test, sharpe, with_costs)


def test_universe_is_well_formed():
    assert len(UNIVERSE) == 57
    assert len(BY_KEY) == len(UNIVERSE)                       # キーが重ならない
    assert len({m.ticker for m in UNIVERSE}) == len(UNIVERSE)
    assert all(m.group in GROUPS for m in UNIVERSE)
    assert [m.key for m in UNIVERSE if m.group == "core"] == ["GOLD", "SILVER", "WTI", "BRENT"]


def test_yahoo_repair_fills_zero_open_but_cuts_at_zero_close(tmp_path):
    p = tmp_path / "x.csv"
    pd.DataFrame({"date": ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"],
                  "open": [0.0, 10.0, 10.0, 10.0], "high": [11, 11, 11, 11], "low": [9, 9, 9, 9],
                  "close": [10.0, 10.5, 0.0, 11.0]}).to_csv(p, index=False)
    f = yahoo_csv_frame(p, repair=True)
    assert len(f) == 2 and f["open"].iloc[0] == 10.0
    assert yahoo_csv_frame(p).empty                           # 既定（先物用）は 0 の行から先を使わない


def test_costs_scale_with_price_and_run_in_backtest(oil_df):
    m = BY_KEY["SP500"]
    frame = oil_df.iloc[:400][["open", "high", "low", "close"]]
    f = with_costs(frame, m)
    inst = market_instrument(m)
    assert f["spread"].iloc[0] * inst.point_size == pytest.approx(GROUPS["index"].cost * frame["close"].iloc[0])
    strat = make_strategy("donchian", entry_period=20, exit_period=10, trend_ema=0)
    # 研究と同じく、スプレッドはデータの列（仕様値 0 との比較で止めない）
    cfg = BacktestConfig(initial_equity=10_000_000, filters=FilterConfig(oil_events=False, no_entry_after_fri_et=None,
                                                                         max_spread_mult=float("inf")))
    res = run_backtest({m.key: f}, {m.key: inst}, [Sleeve(m.key, strat)], cfg)
    assert len(res.trades) > 0
    assert (res.trades["qty"] % 1 != 0).any()                 # 数量はほぼ連続（最小単位の影響を受けない）
    r = day_returns(res.equity)
    assert len(r) > 50 and r.index.tz is None             # H4 の 400 本 ≒ 66 取引日


def _returns(n=2000, k=6, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2005-01-03", periods=n)
    return pd.DataFrame(rng.normal(0.0002, 0.01, (n, k)), index=idx, columns=[f"M{i}" for i in range(k)])


def test_portfolios_average_members_and_groups():
    r = _returns()
    p = portfolio(r, ["M0", "M1", "missing"])
    assert p.to_numpy() == pytest.approx(r[["M0", "M1"]].mean(axis=1).to_numpy())
    g = group_portfolio(r, {"a": ["M0"], "b": ["M1", "M2", "M3"]}, ["a", "b"])
    assert g.to_numpy() == pytest.approx(((r["M0"] + r[["M1", "M2", "M3"]].mean(axis=1)) / 2).to_numpy())
    assert sharpe(p) > sharpe(r["M0"]) - 1                    # 計算できる
    cagr, mdd = at_vol(r["M0"], 0.10)
    assert 0 < mdd < 1 and np.isfinite(cagr)


def test_selection_test_detects_persistence_only_when_present():
    rng = np.random.default_rng(1)
    n, k = 2500, 24
    idx = pd.bdate_range("2001-01-01", periods=n)
    skill = np.linspace(-0.5, 1.5, k) / np.sqrt(252) * 0.01   # 市場ごとに本当の差がある
    r = pd.DataFrame(rng.normal(skill, 0.01, (n, k)), index=idx, columns=[f"M{i}" for i in range(k)])
    split = idx[n // 2]
    h1, h2 = r[r.index < split], r[r.index >= split]
    sr1 = pd.Series({c: sharpe(h1[c]) for c in r})
    sr2 = pd.Series({c: sharpe(h2[c]) for c in r})
    sel = selection_test(sr1, h2, sr2, draws=500)
    assert sel.rho > 0.5 and sel.rho_p <= 0.05 and sel.pct_vs_random >= 0.95
    noise = pd.DataFrame(rng.normal(0.0001, 0.01, (n, k)), index=idx, columns=r.columns)
    h1, h2 = noise[noise.index < split], noise[noise.index >= split]
    sel0 = selection_test(pd.Series({c: sharpe(h1[c]) for c in r}), h2,
                          pd.Series({c: sharpe(h2[c]) for c in r}), draws=500)
    assert sel0.rho_p > 0.05 or sel0.pct_vs_random < 0.95
    assert rank_corr(np.arange(10.0), np.arange(10.0)) == pytest.approx(1.0)
