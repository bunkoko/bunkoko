import pandas as pd
import pytest

from cfdbot.backtest import BacktestConfig, Sleeve
from cfdbot.data import load_mt5_csv, resample_ohlc
from cfdbot.export import EA_STRATEGIES, ea_params, export_sleeve
from cfdbot.instruments import get_instruments, load_instruments, save_instruments
from cfdbot.strategies import RegimeSwitch, SqueezeBreakout
from cfdbot.walkforward import make_windows, walk_forward

INST = get_instruments("phillip")

MT5_CSV = (
    "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
    "2026.07.01\t00:00:00\t70.100\t70.500\t69.900\t70.300\t1200\t0\t30\n"
    "2026.07.01\t04:00:00\t70.300\t70.800\t70.200\t70.700\t1500\t0\t25\n"
)


def test_load_mt5_csv(tmp_path):
    p = tmp_path / "XAGUSD_H4.csv"
    p.write_text(MT5_CSV, encoding="utf-8")
    df = load_mt5_csv(p, server_tz="ny_close")
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "spread"]
    assert df.index[0] == pd.Timestamp("2026-06-30 21:00", tz="UTC")
    assert df["spread"].iloc[0] == 30


def test_resample_h1_to_h4(silver_df):
    h4 = resample_ohlc(silver_df.iloc[:60], "8h")
    assert (h4["high"] >= h4["low"]).all()


def test_instruments_roundtrip(tmp_path):
    p = tmp_path / "inst.json"
    save_instruments(INST, p)
    assert load_instruments(p) == INST


def test_export_set_and_txt(tmp_path):
    sleeve = Sleeve("SILVER", SqueezeBreakout(box_period=24))
    set_path, txt_path = export_sleeve(sleeve, INST["SILVER"], BacktestConfig(), tmp_path)
    raw = set_path.read_bytes()
    assert raw[:2] == b"\xff\xfe"  # UTF-16LE BOM
    text = raw.decode("utf-16")
    assert f"strategy={EA_STRATEGIES.index('squeeze')}" in text
    assert "sq_box_period=24" in text
    txt = txt_path.read_text()
    assert "strategy=squeeze" in txt
    # 戦略既定の出口設定（時間ストップ）が反映される
    assert "ex_time_stop_bars=12" in txt
    params = ea_params(sleeve, INST["SILVER"], BacktestConfig())
    assert params["rk_cluster_id"] == 2 and params["ft_oil_events"] is False


def test_export_rejects_python_only_strategy():
    with pytest.raises(NotImplementedError):
        ea_params(Sleeve("SILVER", RegimeSwitch()), INST["SILVER"], BacktestConfig())


def test_make_windows():
    idx = pd.date_range("2020-01-01", "2023-12-31", freq="D", tz="UTC")
    w = make_windows(idx, 24, 6)
    assert len(w) == 4
    assert w[1].train_start == idx[0] + pd.DateOffset(months=6)


def test_walk_forward_small(oil_df):
    wf = walk_forward(
        {"WTI": oil_df}, INST, "WTI", "donchian",
        {"entry_period": [30, 50], "exit.trail_atr": [2.5, 3.5]},
        train_months=12, test_months=6, min_trades=5, n_jobs=1,
    )
    assert len(wf.windows) >= 3
    assert {"p.entry_period", "p.exit.trail_atr", "test_cagr"} <= set(wf.windows.columns)
    assert not wf.oos_equity.empty
    assert wf.oos_equity.index.is_monotonic_increasing
