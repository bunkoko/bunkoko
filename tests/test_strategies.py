import numpy as np
import pandas as pd
import pytest

from cfdbot import indicators as ind
from cfdbot.strategies import STRATEGIES, DonchianBreakout, RegimeSwitch, make_strategy


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_no_lookahead(name, silver_df):
    """k 本目までのデータで計算したシグナルが、全データで計算したものと一致すること。"""
    df = silver_df.iloc[:1500]
    strat = make_strategy(name)
    full = strat.generate(df, ind.atr(df, 20))
    for k in (700, 1100, 1499):
        part_df = df.iloc[:k]
        part = strat.generate(part_df, ind.atr(part_df, 20))
        pd.testing.assert_frame_equal(full.iloc[:k], part, check_dtype=False)


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_generates_both_directions(name, oil_df, silver_df):
    total_long = total_short = 0
    for df in (oil_df, silver_df):
        sig = make_strategy(name).generate(df)
        total_long += int((sig["entry"] == 1).sum())
        total_short += int((sig["entry"] == -1).sum())
        assert set(np.unique(sig["entry"])) <= {-1, 0, 1}
        assert (sig.loc[sig["entry"] != 0, "stop_dist"].dropna() > 0).all()
    assert total_long > 0 and total_short > 0


def test_unknown_param_rejected():
    with pytest.raises(ValueError):
        DonchianBreakout(entry_periodd=10)
    with pytest.raises(ValueError):
        DonchianBreakout(entry_period=10, exit_period=20)


def test_donchian_breakout_bar():
    n = 80
    close = np.full(n, 100.0)
    close[70] = 105.0
    idx = pd.date_range("2026-01-05", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5, "close": close}, index=idx)
    df.loc[idx[70], "open"] = 100.0
    sig = DonchianBreakout(entry_period=20, exit_period=10, trend_ema=0).generate(df)
    assert sig["entry"].iloc[70] == 1
    assert (sig["entry"].iloc[:70] == 0).all()


def test_regime_switch_tags_and_exit_columns(silver_df):
    strat = RegimeSwitch()
    sig = strat.generate(silver_df)
    tags = set(sig.loc[sig["entry"] != 0, "tag"])
    assert tags <= set(strat.children)
    for child in strat.children:
        assert f"exit_long:{child}" in sig and f"exit_short:{child}" in sig
    # レンジ枠の逆張りは専用の出口設定を持つ
    assert strat.exit_overrides("reversion")["max_hold_bars"] == 10
