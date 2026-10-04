import pytest

from cfdbot.instruments import get_instruments
from cfdbot.mt5specs import (ACCOUNT_FILE, SPECS_FILE, annual_financing, apply_spec, describe_account, load_account,
                             load_specs, spec_for)
from cfdbot.train.config import DEFAULT_SYMBOL_MAP, set_local_option

from .test_check import _cfg, _data

COLS = ["symbol", "digits", "point", "tick_size", "tick_value", "contract_size", "volume_min", "volume_step",
        "volume_max", "volume_limit", "stops_level", "freeze_level", "swap_mode", "swap_long", "swap_short",
        "swap_3days", "currency_base", "currency_profit", "currency_margin", "calc_mode", "trade_mode",
        "filling_mode", "bid", "ask", "spread_points", "margin_1lot", "expiration"]
USDJPY = 150.0


def silver_row(**over):
    """銀: 1 ロット 5,000oz、口座は円。1 ティック（0.001 ドル）の損益 = 0.001 × 5000 × 150 = 750 円。"""
    mid = 30.015
    row = dict(symbol="XAGUSD", digits="3", point="0.001", tick_size="0.001", tick_value=str(0.001 * 5000 * USDJPY),
               contract_size="5000", volume_min="0.01", volume_step="0.01", volume_max="50", volume_limit="0",
               stops_level="20", freeze_level="0", swap_mode="SYMBOL_SWAP_MODE_POINTS", swap_long="-5",
               swap_short="1", swap_3days="WEDNESDAY", currency_base="XAG", currency_profit="USD",
               currency_margin="XAG", calc_mode="SYMBOL_CALC_MODE_CFDLEVERAGE", trade_mode="SYMBOL_TRADE_MODE_FULL",
               filling_mode="2", bid="30.000", ask="30.030", spread_points="30",
               margin_1lot=str(mid * 5000 * USDJPY * 0.05), expiration="0")
    row.update(over)
    return row


def write_specs(path, rows):
    lines = ["\t".join(COLS)] + ["\t".join(str(r[c]) for c in COLS) for r in rows]
    path.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")


def test_apply_spec_converts_lots_margin_and_swap():
    inst, notes = apply_spec(get_instruments()["SILVER"], silver_row())
    assert not notes
    assert inst.min_qty == 50 and inst.qty_step == 50 and inst.max_qty == 250000
    assert inst.margin_rate == pytest.approx(0.05)
    assert inst.stop_level == pytest.approx(0.02) and inst.point == 0.001 and inst.mt5_symbol == "XAGUSD"
    # スワップ -5 ポイント/日 = 0.005 ドル/oz/日 → 年 1.825 ドル ÷ 30.015 ≒ 6.1% の支払い
    assert inst.financing_long == pytest.approx(5 * 0.001 * 365 / 30.015, rel=1e-4)
    assert inst.financing_short == pytest.approx(-1 * 0.001 * 365 / 30.015, rel=1e-4)   # 受け取り
    assert "slippage は要実測" in inst.note


@pytest.mark.parametrize("mode,swap,expected", [
    ("SYMBOL_SWAP_MODE_CURRENCY_DEPOSIT", -1125.5625, 1125.5625 * 365 / (30.015 * 5000 * USDJPY)),
    ("SYMBOL_SWAP_MODE_CURRENCY_PROFIT", -7.5, 7.5 * 365 / (30.015 * 5000)),
    ("SYMBOL_SWAP_MODE_INTEREST_CURRENT", -3.0, 0.03),
    ("SYMBOL_SWAP_MODE_DISABLED", 0.0, 0.0),
])
def test_swap_modes(mode, swap, expected):
    assert annual_financing(silver_row(swap_mode=mode, swap_long=str(swap)), "long") == pytest.approx(expected, rel=1e-4)


def test_unknown_swap_mode_and_missing_margin_are_reported():
    inst, notes = apply_spec(get_instruments()["SILVER"], silver_row(swap_mode="SYMBOL_SWAP_MODE_REOPEN_BID",
                                                                     margin_1lot="0"))
    assert len(notes) == 3 and inst.margin_rate == get_instruments()["SILVER"].margin_rate


def test_spec_for_handles_broker_suffix(tmp_path):
    write_specs(tmp_path / SPECS_FILE, [silver_row(symbol="XAGUSD.ph")])
    specs = load_specs(tmp_path / SPECS_FILE)
    assert spec_for("SILVER", specs, DEFAULT_SYMBOL_MAP)["symbol"] == "XAGUSD.ph"
    assert spec_for("WTI", specs, DEFAULT_SYMBOL_MAP) is None


def test_account_summary(tmp_path):
    (tmp_path / ACCOUNT_FILE).write_text("company=Phillip\r\nserver=PhillipSecuritiesJP-PROD\r\ncurrency=JPY\r\n"
                                         "leverage=20\r\nmargin_mode=ACCOUNT_MARGIN_MODE_RETAIL_NETTING\r\n"
                                         "trade_mode=ACCOUNT_TRADE_MODE_DEMO\r\nbuild=5000\r\nmax_bars=2147483647\r\n")
    text = " ".join(describe_account(load_account(tmp_path / ACCOUNT_FILE)))
    assert "ネッティング" in text and "デモ" in text and "JPY" in text


def test_check_uses_mt5_specs(tmp_path):
    d = _data(tmp_path)
    write_specs(d / SPECS_FILE, [silver_row()])
    from cfdbot.train.check import run_check

    res = run_check(_cfg(d))
    assert not res.warnings, res.warnings
    s = res.measured["SILVER"]
    assert s.min_qty == 50 and s.margin_rate == pytest.approx(0.05) and s.spread == 0.03
    assert any("仕様（MT5 から）" in line for line in res.lines)


def test_local_config_overrides_train_toml(tmp_path):
    from cfdbot.train.config import load_train_config

    cfg_path = tmp_path / "train.toml"
    cfg_path.write_text('[data]\ndir = "data"\ninstruments = ""\nfx = 150.0\n\n[account]\nequity = 1000000\n')
    assert set_local_option(cfg_path, "data", "instruments", "config/instruments_measured.json")
    assert not set_local_option(cfg_path, "data", "instruments", "config/instruments_measured.json")
    assert set_local_option(cfg_path, "account", "equity", 500000)
    cfg = load_train_config(cfg_path)
    assert cfg.data.instruments == "config/instruments_measured.json" and cfg.data.dir == "data"
    assert cfg.data.fx == 150.0 and cfg.account.equity == 500000
    assert 'instruments = ""' in cfg_path.read_text()          # train.toml は書き換えない
