import pytest

from cfdbot.exits import ExitConfig, ExitState, on_bar_close
from cfdbot.instruments import get_instruments
from cfdbot.risk import RiskConfig, open_risk, position_size

INST = get_instruments("phillip")


def test_position_size_one_percent():
    # 資金100万円、1% = 1万円。銀の損切り幅 1.4ドル、1ドル=150円 → 47.6oz → 10oz刻みで 40oz
    r = position_size(1_000_000, 1.4, INST["SILVER"], 150.0, RiskConfig())
    assert r.qty == 40


def test_position_size_min_lot_skip_and_overshoot():
    # 資金30万円: 1% = 3000円、最小10oz の損失 = 10*4*150 = 6000円 → 見送り
    r = position_size(300_000, 4.0, INST["SILVER"], 150.0, RiskConfig())
    assert r.qty == 0 and r.reason == "min_lot"
    r = position_size(300_000, 4.0, INST["SILVER"], 150.0, RiskConfig(min_lot_overshoot=2.0))
    assert r.qty == 10


def test_position_size_budget_and_cap():
    r = position_size(1_000_000, 1.0, INST["WTI"], 150.0, RiskConfig(), risk_budget=3_000)
    assert r.qty == 20
    r = position_size(1_000_000, 0.01, INST["WTI"], 150.0, RiskConfig(max_qty={"WTI": 100}))
    assert r.qty == 100


def test_open_risk_zero_after_breakeven():
    assert open_risk(1, 10, 100.0, 98.0, 150.0) == pytest.approx(3000)
    assert open_risk(1, 10, 100.0, 100.5, 150.0) == 0.0
    assert open_risk(-1, 10, 100.0, 101.0, 150.0) == pytest.approx(1500)


def _state(side=1, entry=100.0, stop=97.5, atr=1.0):
    return ExitState(side=side, entry=entry, stop=stop, r_dist=abs(entry - stop), atr_entry=atr, extreme=entry)


def test_chandelier_ratchets_up_only():
    cfg = ExitConfig(trail_atr=3.0, breakeven_trigger_atr=0)
    st = _state()
    upd = on_bar_close(st, cfg, high=104.0, low=100.0, close=103.5, atr=1.0, spread=0.0)
    assert upd.new_stop == pytest.approx(101.0)
    st.stop = upd.new_stop
    upd = on_bar_close(st, cfg, high=103.0, low=102.0, close=102.5, atr=1.0, spread=0.0)
    assert upd.new_stop is None  # 高値更新なし → 動かさない


def test_breakeven_uses_close():
    cfg = ExitConfig(trail_atr=0, breakeven_trigger_atr=1.0, breakeven_offset_atr=0.1)
    st = _state()
    # ヒゲで +2ATR 付けても終値が +0.5ATR なら建値に上げない
    assert on_bar_close(st, cfg, 102.0, 99.5, 100.5, 1.0, 0.0).new_stop is None
    upd = on_bar_close(st, cfg, 101.5, 100.0, 101.2, 1.0, 0.0)
    assert upd.new_stop == pytest.approx(100.1)


def test_short_trailing_includes_spread():
    cfg = ExitConfig(trail_atr=2.0, breakeven_trigger_atr=0)
    st = _state(side=-1, entry=100.0, stop=102.5)
    upd = on_bar_close(st, cfg, high=98.5, low=97.0, close=97.2, atr=1.0, spread=0.05)
    assert upd.new_stop == pytest.approx(97.0 + 2.0 + 0.05)


def test_time_stop_and_max_hold():
    cfg = ExitConfig(trail_atr=0, breakeven_trigger_atr=0, time_stop_bars=3, time_stop_min_r=0.5)
    st = _state()
    assert on_bar_close(st, cfg, 101, 99, 100.2, 1.0, 0.0).exit_now is None
    assert on_bar_close(st, cfg, 101, 99, 100.2, 1.0, 0.0).exit_now is None
    assert on_bar_close(st, cfg, 101, 99, 100.2, 1.0, 0.0).exit_now == "time_stop"
    cfg = ExitConfig(trail_atr=0, breakeven_trigger_atr=0, max_hold_bars=1)
    assert on_bar_close(_state(), cfg, 101, 99, 105, 1.0, 0.0).exit_now == "max_hold"


def test_stop_level_forces_market_exit():
    cfg = ExitConfig(trail_atr=1.0, breakeven_trigger_atr=0)
    st = _state()
    # 高値 110 から急落して終値 100.5: 新ライン 109 は現在値より上 → 次の始値で成行
    upd = on_bar_close(st, cfg, high=110.0, low=100.0, close=100.5, atr=1.0, spread=0.0)
    assert upd.exit_now == "stop_level"


def test_clip_stop():
    cfg = ExitConfig(init_stop_atr=2.5, min_stop_atr=1.0, max_stop_atr=4.0)
    assert cfg.clip_stop(float("nan"), 2.0) == pytest.approx(5.0)
    assert cfg.clip_stop(0.5, 2.0) == pytest.approx(2.0)
    assert cfg.clip_stop(20.0, 2.0) == pytest.approx(8.0)
