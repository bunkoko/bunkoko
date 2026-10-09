import numpy as np
import pandas as pd
import pytest

from cfdbot.backtest import BacktestConfig, FilterConfig, Sleeve, run_backtest
from cfdbot.context import yahoo_csv_frame
from cfdbot.strategies import make_strategy
from cfdbot.universe import (BY_KEY, GROUPS, UNIVERSE, at_vol, day_returns, group_portfolio, hold_returns,
                             market_instrument, portfolio, rank_corr, selection_test, sharpe, timeframe_costs,
                             with_costs)


def test_universe_is_well_formed():
    assert len(UNIVERSE) == 59
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


def _crypto_days(n=120, seed=3):
    """土日も含む日足（Yahoo の暗号資産と同じ並び）を yahoo_csv_frame の形にしたもの。"""
    rng = np.random.default_rng(seed)
    d = pd.date_range("2024-01-01", periods=n, freq="D")
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, n)))
    o = np.r_[c[0], c[:-1]]
    opens = (d - pd.Timedelta(hours=6)).tz_localize("America/New_York").tz_convert("UTC")
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.01, "low": np.minimum(o, c) * 0.99, "close": c},
                        index=opens)


def test_hold_returns_match_close_to_close_on_strategy_dates():
    f = _crypto_days()
    r = hold_returns(f)
    assert r.iloc[0] == pytest.approx(f["close"].iloc[1] / f["close"].iloc[0] - 1)
    # 戦略の日ごとの資産と同じ日付（土日の分は金曜にまとまる）
    assert all(d.weekday() < 5 for d in r.index)
    week = f.iloc[:10]
    eq = pd.Series(week["close"].to_numpy(), index=week.index + pd.Timedelta(days=1))
    assert list(day_returns(eq).index) == list(hold_returns(week).index)


def test_weekend_bars_can_enter_when_the_friday_rule_is_off():
    from .conftest import FixedSignals

    f = _crypto_days()
    sat = int(np.flatnonzero((f.index + pd.Timedelta(days=1)).tz_convert("America/New_York").weekday == 5)[0])
    m = BY_KEY["BTC"]
    inst = market_instrument(m)
    strat = FixedSignals(entries={sat: 1}, stop_dist=5.0)
    off = BacktestConfig(initial_equity=10_000_000,
                         filters=FilterConfig(oil_events=False, no_entry_after_fri_et=None, max_spread_mult=float("inf")))
    on = replace_filters(off, 12.0)
    assert len(run_backtest({m.key: with_costs(f, m)}, {m.key: inst}, [Sleeve(m.key, strat)], off).trades) == 1
    res = run_backtest({m.key: with_costs(f, m)}, {m.key: inst}, [Sleeve(m.key, strat)], on)
    assert res.trades.empty and set(res.rejections["reason"]) == {"weekend"}


def replace_filters(cfg, fri):
    from dataclasses import replace
    return replace(cfg, filters=replace(cfg.filters, no_entry_after_fri_et=fri))


def test_timeframe_costs_shrink_with_longer_bars():
    rng = np.random.default_rng(5)
    t = pd.date_range("2025-01-01", periods=24 * 200, freq="h", tz="UTC")
    c = 50000 * np.exp(np.cumsum(rng.normal(0, 0.006, len(t))))
    o = np.r_[c[0], c[:-1]]
    h = pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.001, "low": np.minimum(o, c) * 0.999, "close": c},
                     index=t)
    tc = timeframe_costs(h, {"x": 0.002})
    assert list(tc["timeframe"]) == ["1 時間足", "4 時間足", "日足"]
    assert tc["x"].is_monotonic_decreasing
    assert tc["x"].iloc[0] == pytest.approx(0.002 / (2.5 * tc["atr_pct"].iloc[0]))
    assert GROUPS["crypto"].weekend and not GROUPS["core"].weekend
