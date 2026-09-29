from dataclasses import replace

import pandas as pd
import pytest

from cfdbot.backtest import BacktestConfig, CostModel, FilterConfig, Sleeve, run_backtest
from cfdbot.exits import ExitConfig
from cfdbot.instruments import get_instruments
from cfdbot.profiles import default_config, default_sleeves
from cfdbot.risk import RiskConfig

from .conftest import FixedSignals, make_bars

BASE = get_instruments("phillip")
# 計算しやすい仕様: スプレッド0.1、スリッページ0.05、1単位刻み、金利なし
TEST_INST = {
    "SILVER": replace(BASE["SILVER"], spread=0.1, slippage=0.05, min_qty=1, qty_step=1,
                      financing_long=0.0, financing_short=0.0),
    "WTI": replace(BASE["WTI"], spread=0.1, slippage=0.05, min_qty=1, qty_step=1,
                   financing_long=0.0, financing_short=0.0),
}
PLAIN_EXIT = ExitConfig(trail_atr=0, breakeven_trigger_atr=0, min_stop_atr=0.1, max_stop_atr=100)


def _cfg(**kw):
    base = dict(
        initial_equity=1_000_000.0,
        fx_rate=100.0,
        exit=PLAIN_EXIT,
        filters=FilterConfig(oil_events=False, no_entry_after_fri_et=None),
    )
    base.update(kw)
    return BacktestConfig(**base)


def _run(rows, entries, symbol="SILVER", stop_dist=2.0, exits=None, cfg=None, start="2026-09-08 01:00"):
    df = make_bars(rows, start=start)
    strat = FixedSignals(entries=entries, stop_dist=stop_dist, exits=exits or {})
    return run_backtest({symbol: df}, TEST_INST, [Sleeve(symbol, strat)], cfg or _cfg())


def test_long_entry_next_open_and_intrabar_stop():
    res = _run([(100, 101, 99, 100), (100, 101, 98, 99), (99, 100, 98, 99)], {0: 1})
    t = res.trades.iloc[0]
    assert t.entry_price == pytest.approx(100.15)  # 始値 + スプレッド + スリッページ
    assert t.qty == 50                              # 1万円 / (2ドル × 100円)
    assert t.reason == "stop"
    assert t.exit_price == pytest.approx(98.10)     # 逆指値 98.15 - スリッページ
    assert t.pnl == pytest.approx((98.10 - 100.15) * 50 * 100)
    assert t.r_multiple == pytest.approx(-2.05 / 2.0)


def test_gap_through_stop_fills_at_open():
    res = _run([(100, 101, 99, 100), (100, 100.5, 99.5, 100), (97, 97.5, 96, 97)], {0: 1})
    t = res.trades.iloc[0]
    assert t.reason == "stop_gap"
    assert t.exit_price == pytest.approx(96.95)


def test_short_stop_uses_ask():
    res = _run([(100, 101, 99, 100), (100, 101.9, 99, 101)], {0: -1})
    t = res.trades.iloc[0]
    assert t.entry_price == pytest.approx(99.95)
    assert t.reason == "stop"                       # 高値101.9 + スプレッド0.1 ≥ 逆指値101.95
    assert t.exit_price == pytest.approx(102.0)


def test_signal_exit_next_open():
    rows = [(100, 101, 99, 100), (100, 101, 99.5, 100.5), (100.5, 101, 100, 100.8), (101, 102, 100.5, 101.5)]
    res = _run(rows, {0: 1}, exits={2: 1})
    t = res.trades.iloc[0]
    assert t.reason == "signal"
    assert t.exit_price == pytest.approx(101 - 0.05)
    assert t.exit_time == res.equity.index[3] - pd.Timedelta(hours=4)


def test_partial_take_profit():
    rows = [(100, 101, 99, 100), (100, 101, 99.5, 100.5), (100.5, 102.5, 100, 102), (102, 102.5, 101.5, 102)]
    cfg = _cfg(exit=replace(PLAIN_EXIT, partial_tp_r=1.0, partial_fraction=0.5))
    res = _run(rows, {0: 1}, cfg=cfg)
    t = res.trades.iloc[0]
    # 50 単位のうち 25 を 102.15 で利確、残り 25 は最終足の終値 102 - 0.05 で決済
    expected = (102.15 - 100.15) * 25 * 100 + (101.95 - 100.15) * 25 * 100
    assert t.pnl == pytest.approx(expected)
    assert t.reason == "end"


def test_equity_matches_trade_pnl(oil_df, silver_df):
    data = {"WTI": oil_df, "SILVER": silver_df}
    res = run_backtest(data, BASE, default_sleeves(), default_config())
    assert len(res.trades) > 20
    assert res.equity.iloc[-1] == pytest.approx(1_000_000 + res.trades["pnl"].sum(), rel=1e-9)
    # 同一銘柄で同時に持つのは1ポジションまで
    for sym, g in res.trades.groupby("symbol"):
        g = g.sort_values("entry_time")
        assert (g["entry_time"].iloc[1:].values >= g["exit_time"].iloc[:-1].values).all()


def test_trade_start_respected():
    rows = [(100, 101, 99, 100)] * 6
    df_start = make_bars(rows).index[3]
    res = _run(rows, {0: 1, 4: 1}, cfg=_cfg(trade_start=df_start))
    assert len(res.trades) == 1
    assert res.trades.iloc[0].entry_time == make_bars(rows).index[5]


def test_eia_window_blocks_oil_entry():
    # 水曜 2026-09-09 14:30 UTC が EIA。09:00 UTC 始まりの足の判断時刻 13:00 UTC は窓の中
    rows = [(100, 101, 99, 100)] * 4
    cfg = _cfg(filters=FilterConfig(oil_events=True, no_entry_after_fri_et=None))
    res = _run(rows, {0: 1}, symbol="WTI", cfg=cfg, start="2026-09-09 09:00")
    assert res.trades.empty
    assert list(res.rejections["reason"]) == ["event"]


def test_friday_cutoff_blocks_entry():
    # 金曜 2026-09-11 17:00 UTC (= 13:00 ET) 判断 → 12:00 ET 以降なので新規停止
    rows = [(100, 101, 99, 100)] * 3
    cfg = _cfg(filters=FilterConfig(oil_events=False, no_entry_after_fri_et=12.0))
    res = _run(rows, {0: 1}, cfg=cfg, start="2026-09-11 13:00")
    assert res.trades.empty and list(res.rejections["reason"]) == ["weekend"]


def test_daily_loss_limit_blocks_new_entries():
    rows = [(100, 101, 99, 100), (100, 100, 96, 96), (96, 97, 95, 96), (96, 97, 95, 96), (96, 97, 95, 96)]
    cfg = _cfg(risk=RiskConfig(risk_per_trade=0.05, daily_loss_limit=0.03, max_total_risk=0.1,
                               cluster_max_risk={}))
    res = _run(rows, {0: 1, 2: 1}, cfg=cfg)
    assert len(res.trades) == 1
    assert "daily_loss" in set(res.rejections["reason"])


def test_spread_stress_reduces_profit(oil_df):
    sleeves = [s for s in default_sleeves() if s.symbol == "WTI"]
    base = run_backtest({"WTI": oil_df}, BASE, sleeves, default_config())
    stressed = run_backtest(
        {"WTI": oil_df}, BASE, sleeves, default_config(costs=CostModel(spread_mult=3.0, slippage_mult=3.0))
    )
    assert stressed.trades["pnl"].sum() < base.trades["pnl"].sum()
