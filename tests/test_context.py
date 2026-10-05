import io
import json
import zipfile

import numpy as np
import pandas as pd

from cfdbot.backtest import BacktestConfig, FilterConfig, Sleeve, run_backtest
from cfdbot.context import (SPECS, ContextStore, CSeries, at_times, cot_percentile, cot_series, load_series,
                            parse_cot, parse_fred_csv, parse_yahoo_chart, yahoo_frame)
from cfdbot.features import Hypothesis, decide_ns, feature_frame, fit_ridge, hypothesis_gate, ml_gates, trade_rows
from cfdbot.instruments import get_instruments

from .conftest import FixedSignals


def _ns(ts: str, tz="America/New_York") -> int:
    return pd.Timestamp(ts, tz=tz).tz_convert("UTC").value


def test_fred_values_are_used_only_after_publication(tmp_path):
    raw = b"observation_date,DGS10\n2026-01-05,4.10\n2026-01-06,.\n2026-01-07,4.30\n"
    df = parse_fred_csv(raw)
    assert list(df["value"]) == [4.10, 4.30]       # 欠損（.）は落とす
    p = tmp_path / "ust10y.csv"
    df.to_csv(p, index=False)
    cs = load_series(p, SPECS["ust10y"])            # 翌営業日 17:00 ET に公表
    t = np.array([_ns("2026-01-06 16:59"), _ns("2026-01-06 17:01"), _ns("2026-01-08 16:00"), _ns("2026-01-08 17:00")])
    got = at_times(cs, cs.main.to_numpy(), t)
    assert np.isnan(got[0])                          # 1/5 の値はまだ公表前
    assert got[1] == 4.10
    assert got[2] == 4.10                            # 1/7 の値は 1/8 17:00 から
    assert got[3] == 4.30


def test_yahoo_values_become_known_at_the_end_of_the_exchange_day(tmp_path):
    ts = [int(pd.Timestamp("2026-03-02 00:00", tz="America/New_York").timestamp()),
          int(pd.Timestamp("2026-03-03 00:00", tz="America/New_York").timestamp())]
    raw = json.dumps({"chart": {"result": [{
        "meta": {"exchangeTimezoneName": "America/New_York"}, "timestamp": ts,
        "indicators": {"quote": [{"open": [1.0, 2.0], "high": [1.5, 2.5], "low": [0.5, 1.5], "close": [1.2, None],
                                  "volume": [0, 0]}]}}], "error": None}}).encode()
    df = parse_yahoo_chart(raw)
    assert list(df["date"]) == ["2026-03-02"]      # 終値の無い日は落とす
    p = tmp_path / "spx.csv"
    df.to_csv(p, index=False)
    cs = load_series(p, SPECS["spx"])
    t = np.array([_ns("2026-03-02 23:59"), _ns("2026-03-03 00:00")])
    got = at_times(cs, cs.main.to_numpy(), t)
    assert np.isnan(got[0]) and got[1] == 1.2


def test_cot_zip_is_parsed_and_percentile_uses_only_the_past():
    header = ("Market_and_Exchange_Names,As_of_Date_In_Form_YYMMDD,Report_Date_as_YYYY-MM-DD,"
              "CFTC_Contract_Market_Code,Open_Interest_All,M_Money_Positions_Long_All,M_Money_Positions_Short_All\n")
    rows = "".join(f'"GOLD - COMMODITY EXCHANGE INC.",2601{d:02d},2026-01-{d:02d},088691 ,1000,{500 + d},100\n'
                   for d in (6, 13, 20, 27))
    rows += '"SILVER",260106,2026-01-06,084691,10,5,1\n'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("f_year.txt", header + rows)
    t = parse_cot(buf.getvalue())
    g = cot_series(t, "088691")
    assert len(g) == 4 and abs(g["net"].iloc[0] - 0.406) < 1e-9
    cs = CSeries("cot_gold", g.set_index(pd.DatetimeIndex(g["date"])).drop(columns="date"), np.arange(4), "level")
    pct = cot_percentile(cs, window=4)
    assert np.isnan(pct[0]) and pct[-1] == 1.0     # 上がり続けたので最新は過去の全部より上


def test_hypothesis_gate_blocks_the_side_against_the_input():
    days = pd.bdate_range("2025-01-01", periods=60)
    up = CSeries("x", pd.DataFrame({"close": np.linspace(100, 160, 60)}, index=days),
                 (days + pd.Timedelta(days=1)).tz_localize("UTC").as_unit("ns").asi8, "price")
    idx = pd.date_range("2025-03-10", periods=5, freq="D", tz="UTC")
    h = Hypothesis("t", "test", {"GOLD": "x"}, sign=-1)          # 系列が上がっていれば売りだけ
    g = hypothesis_gate(h, "GOLD", idx, pd.Timedelta("1D"), lambda k, s: up)
    assert (~g["long"]).all() and g["short"].all()
    assert hypothesis_gate(h, "SILVER", idx, pd.Timedelta("1D"), lambda k, s: up) is None   # 対象外


def test_entry_gate_removes_blocked_entries_in_backtest(oil_df):
    df = oil_df.iloc[:300]
    inst = get_instruments("phillip")
    strat = FixedSignals(entries={100: 1, 200: -1}, stop_dist=2.0)
    gate = pd.DataFrame({"long": False, "short": True}, index=df.index)
    cfg = BacktestConfig(filters=FilterConfig(oil_events=False, no_entry_after_fri_et=None))
    res = run_backtest({"WTI": df}, inst, [Sleeve("WTI", strat, entry_gate=gate)], cfg)
    assert set(res.trades["side"]) == {-1}
    res = run_backtest({"WTI": df}, inst, [Sleeve("WTI", strat)], cfg)
    assert set(res.trades["side"]) == {1, -1}


def test_yahoo_frame_stops_before_non_positive_prices(tmp_path):
    store = ContextStore(tmp_path)
    p = store.path(SPECS["fut_wti"])
    p.parent.mkdir(parents=True)
    pd.DataFrame({"date": ["2020-04-16", "2020-04-17", "2020-04-20", "2020-04-21"],
                  "open": [20, 19, 18, 10], "high": [21, 20, 18, 12], "low": [19, 18, -40, 9],
                  "close": [20, 18, -37, 11], "volume": 0, "tz": "America/New_York"}).to_csv(p, index=False)
    f = yahoo_frame(store, "fut_wti")
    assert len(f) == 2
    assert f.index[0] == pd.Timestamp("2020-04-15 18:00", tz="America/New_York")   # 前日 18:00 ET 開始


def test_ml_gate_learns_only_from_trades_closed_before_the_year():
    idx = pd.date_range("2018-01-01", "2021-12-31", freq="D", tz="UTC")
    rng = np.random.default_rng(0)
    signal = pd.Series(rng.normal(size=len(idx)), index=idx)
    frame = pd.DataFrame({"close": 100 + np.cumsum(rng.normal(size=len(idx)))}, index=idx)
    cs = CSeries("dxy", pd.DataFrame({"close": np.exp(signal.cumsum().to_numpy() / 50)},
                                          index=idx.tz_localize(None)),
                 decide_ns(idx, pd.Timedelta("1D")), "price")
    ff = feature_frame("GOLD", frame, pd.Timedelta("1D"), lambda k, s: cs if k == "dxy" else None)
    # 買いの結果が dxy の変化と同じ向き（偶然ではない関係）を作る
    z = ff.directional["dxy_z20"].to_numpy()
    pick = np.flatnonzero(np.isfinite(z))[::3]
    trades = pd.DataFrame({"symbol": "GOLD", "side": 1, "entry_time": idx[pick] + pd.Timedelta("1D"),
                           "exit_time": idx[pick] + pd.Timedelta("5D"), "r_multiple": np.sign(z[pick])})
    x, y, e = trade_rows(trades, {"GOLD": ff})
    assert len(y) == len(pick)
    model = fit_ridge(x, y)
    assert model.importance["dxy_z20"] > 0
    gates, models = ml_gates({"GOLD": ff}, x, y, e, pd.Timestamp("2019-01-01", tz="UTC"),
                             pd.Timestamp("2022-01-01", tz="UTC"), min_trades=50)
    assert models[0][1] is not None and models[0][1].n < len(y)   # 2019 年のモデルは 2018 年までの取引だけ
    g = gates["GOLD"].loc["2020"]
    zz = pd.Series(z, index=idx).loc["2020"]
    agree = (g["long"].to_numpy() == (zz.to_numpy() > 0))[np.isfinite(zz.to_numpy())]
    assert agree.mean() > 0.9


def test_fred_api_json_and_yahoo_substitutes(monkeypatch):
    from cfdbot import context as ctx

    raw = json.dumps({"observations": [{"date": "2026-01-05", "value": "1.80"},
                                       {"date": "2026-01-06", "value": "."}]}).encode()
    df = ctx.parse_fred_api(raw)
    assert list(df["value"]) == [1.80]

    def fake_yahoo(ticker, since):
        price = {"TIP": [100.0, 99.0, 98.0], "IEF": [100.0, 100.0, 100.0]}[ticker]
        return pd.DataFrame({"date": ["2026-01-05", "2026-01-06", "2026-01-07"], "close": price})

    monkeypatch.setattr(ctx, "fetch_yahoo", fake_yahoo)
    real = ctx.fetch_alt("real10y", "2000-01-01")
    assert real["value"].is_monotonic_increasing          # 物価連動債が下がる ＝ 実質金利が上がる
    be = ctx.fetch_alt("breakeven10y", "2000-01-01")
    assert be["value"].is_monotonic_decreasing and be["via"].iloc[0] == "Yahoo TIP/IEF"


def test_get_falls_back_to_curl_and_hides_query(monkeypatch):
    from cfdbot import context as ctx

    def boom(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(ctx.urllib.request, "urlopen", boom)
    monkeypatch.setattr(ctx.time, "sleep", lambda s: None)
    monkeypatch.setattr(ctx, "_curl", lambda url, timeout, ua: b"ok")
    assert ctx._get("https://example.com/x?api_key=SECRET") == b"ok"

    def curl_fails(url, timeout, ua):
        raise RuntimeError("curl failed")

    monkeypatch.setattr(ctx, "_curl", curl_fails)
    try:
        ctx._get("https://example.com/x?api_key=SECRET")
    except RuntimeError as e:
        assert "SECRET" not in str(e)
    else:
        raise AssertionError("例外にならない")


def test_eia_sheet_and_404_is_not_an_outage(monkeypatch):
    from cfdbot import context as ctx

    sheet = pd.DataFrame([["Back to Contents", "Data 1: Weekly U.S. Ending Stocks"],
                          ["Sourcekey", "WCESTUS1"],
                          ["Date", "Weekly U.S. Ending Stocks excluding SPR of Crude Oil (Thousand Barrels)"],
                          [pd.Timestamp("2026-09-18"), 415000],
                          [pd.Timestamp("2026-09-25"), 413500]])
    t = ctx.eia_table(sheet)
    assert list(t["date"]) == ["2026-09-18", "2026-09-25"] and t["value"].iloc[-1] == 413500

    def not_found(*a, **k):
        raise ctx.urllib.error.HTTPError("https://x/y?id=1", 404, "Not Found", {}, None)

    monkeypatch.setattr(ctx.urllib.request, "urlopen", not_found)
    monkeypatch.setattr(ctx, "_curl", lambda *a: (_ for _ in ()).throw(AssertionError("curl は試さない")))
    try:
        ctx._get("https://x/y?id=1")
    except ctx.NotFound:
        pass
    else:
        raise AssertionError("NotFound にならない")


def test_hypothesis_uses_the_fallback_series_when_the_first_is_missing():
    days = pd.bdate_range("2025-01-01", periods=60)
    up = CSeries("usd_broad", pd.DataFrame({"close": np.linspace(100, 120, 60)}, index=days),
                 (days + pd.Timedelta(days=1)).tz_localize("UTC").as_unit("ns").asi8, "price")
    idx = pd.date_range("2025-03-10", periods=3, freq="D", tz="UTC")
    h = Hypothesis("usd", "test", {"GOLD": "dxy|usd_broad"}, sign=-1)
    g = hypothesis_gate(h, "GOLD", idx, pd.Timedelta("1D"), lambda k, s: up if k == "usd_broad" else None)
    assert (~g["long"]).all() and g["short"].all()     # ドル高なので買わない


def test_extension_gate_blocks_entries_after_a_big_move():
    days = pd.bdate_range("2024-01-01", periods=400)
    rng = np.random.default_rng(1)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))
    close[-25:] = close[-26] * np.exp(np.linspace(0.01, 0.25, 25))     # 最後の 25 日で大きく上げる
    cs = CSeries("own", pd.DataFrame({"close": close}, index=days),
                 (days + pd.Timedelta(days=1)).tz_localize("UTC").as_unit("ns").asi8, "price")
    idx = pd.DatetimeIndex([days[100], days[-1]]).tz_localize("UTC")
    h = Hypothesis("ext", "test", {"GOLD": "own"}, kind="extension", threshold=1.5)
    g = hypothesis_gate(h, "GOLD", idx, pd.Timedelta("1D"), lambda k, s: cs)
    assert g["long"].iloc[0] and g["short"].iloc[0]              # ふだんの日はどちらも入れる
    assert not g["long"].iloc[1] and g["short"].iloc[1]          # 大きく上げた後は買わない（売りは入れる）
