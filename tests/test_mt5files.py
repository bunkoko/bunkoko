import re
from pathlib import Path

import pandas as pd

from cfdbot.backtest import BacktestConfig, Sleeve
from cfdbot.events import Event
from cfdbot.export import export_sleeve
from cfdbot.instruments import get_instruments
from cfdbot.mt5files import (EVENT_INPUT_MAX, EVENT_INPUTS, build_presets, fetch, find_terminals, inline_events,
                             install, magic_for, read_set, write_set)
from cfdbot.strategies import DonchianBreakout, SqueezeBreakout

ROOT = Path(__file__).resolve().parents[1]
INST = get_instruments("phillip")
UTC = "UTC"


def _wine_terminal(home: Path) -> tuple[Path, Path]:
    drive = home / "Library" / "Application Support" / "net.metaquotes.wine.metatrader5" / "drive_c"
    mql5 = drive / "Program Files" / "MetaTrader 5" / "MQL5"
    (mql5 / "Experts").mkdir(parents=True)
    common = drive / "users" / "alice" / "AppData" / "Roaming" / "MetaQuotes" / "Terminal" / "Common" / "Files"
    common.mkdir(parents=True)
    return mql5, common


def _events(n: int, tag: str, start="2026-10-05") -> list[Event]:
    t0 = pd.Timestamp(start, tz=UTC)
    return [Event(t0 + pd.Timedelta(days=3 * i, hours=12, minutes=30), f"ev{i}", tag) for i in range(n)]


def test_find_wine_terminal(tmp_path):
    mql5, common = _wine_terminal(tmp_path)
    found = find_terminals(home=tmp_path)
    assert len(found) == 1
    assert found[0].mql5 == mql5 and found[0].common == common


def test_find_windows_terminal(tmp_path):
    base = tmp_path / "MetaQuotes" / "Terminal"
    (base / "ABCDEF" / "MQL5" / "Experts").mkdir(parents=True)
    (base / "Common" / "Files").mkdir(parents=True)
    found = find_terminals(home=tmp_path / "nohome", appdata=tmp_path)
    assert len(found) == 1 and found[0].common == base / "Common" / "Files"


def test_inline_events_fit_mt5_input_limit():
    events = _events(60, "usd_macro") + _events(5, "oil")
    start, end = pd.Timestamp("2026-10-01", tz=UTC), pd.Timestamp("2027-12-31", tz=UTC)
    inputs, n, cut = inline_events(events, "SILVER", ["usd_macro"], start, end)
    assert set(inputs) == set(EVENT_INPUTS)
    assert all(len(v) <= EVENT_INPUT_MAX for v in inputs.values())
    assert 40 <= n < 60 and cut is not None
    times = [t for v in inputs.values() for t in v.split(",") if t]
    assert len(times) == n
    assert times[0] == "2026.10.05 12:30"          # EA の StringToTime が読める形
    assert all(re.fullmatch(r"\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}", t) for t in times)
    # 原油のタグは銀に入らない。期間外も入らない
    inputs, n, cut = inline_events(events, "WTI", ["oil"], start, end)
    assert n == 5 and cut is None and inputs["ea_events_utc_2"] == ""
    inputs, n, _ = inline_events(events, "WTI", ["oil"], pd.Timestamp("2026-10-07", tz=UTC), end)
    assert n == 4


def test_set_roundtrip(tmp_path):
    p = write_set({"a": "1", "ea_param_file": "", "ea_events_utc_1": "2026.11.04 18:00,2026.11.06 13:30"},
                  tmp_path / "x.set")
    assert p.read_bytes()[:2] == b"\xff\xfe"
    assert read_set(p) == {"a": "1", "ea_param_file": "", "ea_events_utc_1": "2026.11.04 18:00,2026.11.06 13:30"}


def test_magic_is_stable_per_symbol():
    assert magic_for("SILVER") == 2609003 and magic_for("wti") == 2609001
    assert magic_for("COPPER") == magic_for("COPPER") and 2609010 <= magic_for("COPPER") < 2609910


def _export(tmp_path) -> Path:
    ea = tmp_path / "run" / "ea"
    export_sleeve(Sleeve("SILVER", SqueezeBreakout()), INST["SILVER"], BacktestConfig(), ea, 60, "H1")
    export_sleeve(Sleeve("WTI", DonchianBreakout()), INST["WTI"], BacktestConfig(), ea, 240, "H4")
    return ea


def test_build_presets_and_install(tmp_path):
    ea = _export(tmp_path)
    events = _events(3, "usd_macro") + _events(2, "oil")
    presets = build_presets(ea, events, pd.Timestamp("2026-10-01", tz=UTC), pd.Timestamp("2027-01-31", tz=UTC),
                            risk_scale=0.5)
    by = {p.symbol: p for p in presets}
    assert set(by) == {"SILVER", "WTI"} and by["WTI"].timeframe == "H4"
    s = by["SILVER"].params
    assert s["ea_magic"] == "2609003" and s["ea_risk_scale"] == "0.5" and s["ea_param_file"] == ""
    assert s["ea_timeframe_minutes"] == "60" and by["SILVER"].events == 3 and by["WTI"].events == 2
    assert s["ea_peak_since"] == ""
    again = build_presets(ea, [], pd.Timestamp("2026-10-01", tz=UTC), pd.Timestamp("2027-01-31", tz=UTC),
                          peak_since="2027-03-06")
    assert again[0].params["ea_peak_since"] == "2027.03.06"

    mql5, common = _wine_terminal(tmp_path / "home")
    term = find_terminals(home=tmp_path / "home")[0]
    events_csv = tmp_path / "events.csv"
    events_csv.write_text("time_utc,name,tag\n2026-11-04 18:00,FOMC,usd_macro\n")
    install(term, presets, ROOT, events_csv)
    assert (mql5 / "Experts" / "CfdCommodityEA.mq5").is_file()
    assert (mql5 / "Scripts" / "CfdExportBars.mq5").is_file()
    live = read_set(mql5 / "Presets" / "cfdbot_SILVER_squeeze_H1.set")
    tester = read_set(mql5 / "Profiles" / "Tester" / "cfdbot_SILVER_squeeze_H1.set")
    assert live["ea_log_signals"] == "false" and tester["ea_log_signals"] == "true" and tester["ea_risk_scale"] == "1"
    assert (common / "cfdbot_events.csv").is_file()


def test_preset_keys_are_ea_inputs(tmp_path):
    """プリセットの項目名が EA の input と全部一致する（綴りが違うと MT5 は黙って無視する）。"""
    src = (ROOT / "mql5" / "Experts" / "CfdCommodityEA.mq5").read_text(encoding="utf-8-sig")
    inputs = set(re.findall(r"^input\s+\w+\s+(\w+)\s*=", src, flags=re.M))
    presets = build_presets(_export(tmp_path), [], pd.Timestamp("2026-10-01", tz=UTC),
                            pd.Timestamp("2026-12-31", tz=UTC))
    for p in presets:
        missing = set(p.params) - inputs
        assert not missing, f"EA に無い input: {missing}"


def test_fetch(tmp_path):
    src = tmp_path / "Common" / "Files" / "cfdbot_data"
    src.mkdir(parents=True)
    (src / "XAGUSD_M5.csv").write_text("x")
    (src / "note.txt").write_text("x")
    got = fetch(src, "*.csv", tmp_path / "data")
    assert [p.name for p in got] == ["XAGUSD_M5.csv"] and (tmp_path / "data" / "XAGUSD_M5.csv").is_file()


def test_export_bars_format_loads(tmp_path):
    """CfdExportBars.mq5 が書く形式（タブ区切り・CRLF・TIME_DATE/TIME_SECONDS）を読み込める。"""
    from cfdbot.data import load_mt5_csv

    head = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\r\n"
    rows = "".join(f"2026.03.02\t{h:02d}:00:00\t30.100\t30.200\t30.000\t30.150\t120\t0\t25\r\n" for h in range(3))
    p = tmp_path / "XAGUSD_H1.csv"
    p.write_bytes((head + rows).encode("ascii"))
    df = load_mt5_csv(p, server_tz="ny_close")
    assert len(df) == 3 and df["spread"].iloc[0] == 25 and df["close"].iloc[-1] == 30.15


def test_broker_suffix_ps01(tmp_path):
    """フィリップの XAGUSD.ps01 のような名前: ファイル名・銘柄仕様・チャート名の表示がすべて通る。"""
    import importlib.util

    from cfdbot.mt5specs import load_specs, spec_for
    from cfdbot.train.config import DEFAULT_SYMBOL_MAP, TrainConfig, normalize
    from cfdbot.train.dataset import load_dataset

    from .test_mt5specs import silver_row, write_specs
    from .test_multitf import random_walk_m5, write_mt5

    write_mt5(random_walk_m5(1, start="2024-01-01", end="2024-01-31"), tmp_path / "XAGUSD.ps01_M5.csv")
    write_mt5(random_walk_m5(2, start="2024-01-01", end="2024-01-31"), tmp_path / "USDJPY.ps01_M5.csv")
    cfg = TrainConfig()
    cfg.data.dir = str(tmp_path)
    ds = load_dataset(normalize(cfg).data)
    assert "M5" in ds.frames["SILVER"] and ds.fx is not None and not ds.skipped

    write_specs(tmp_path / "symbol_specs.txt", [silver_row(symbol="XAGUSD.ps01")])
    assert spec_for("SILVER", load_specs(tmp_path / "symbol_specs.txt"), DEFAULT_SYMBOL_MAP)["symbol"] == "XAGUSD.ps01"

    spec = importlib.util.spec_from_file_location("mt5_files_cli", ROOT / "scripts" / "mt5_files.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli._mt5_name("SILVER", DEFAULT_SYMBOL_MAP, tmp_path) == "XAGUSD.ps01"
    assert cli._mt5_name("WTI", DEFAULT_SYMBOL_MAP, tmp_path) == "XTIUSD"     # ファイルが無ければ標準の名前
