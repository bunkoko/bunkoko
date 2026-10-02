"""複数の時間足（売買の足・約定の再現・上位足フィルタ）のテスト。"""

from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdbot.backtest import BacktestConfig, Sleeve, run_backtest
from cfdbot.data import resample_ohlc
from cfdbot.instruments import get_instruments
from cfdbot.risk import RiskConfig
from cfdbot.strategies import make_strategy
from cfdbot.train.config import StrategySearch, TrainConfig, normalize
from cfdbot.train.costs import timeframe_costs
from cfdbot.train.dataset import load_dataset
from cfdbot.train.pipeline import build_tasks

from .conftest import FixedSignals, make_bars
from .test_backtest import PLAIN_EXIT, TEST_INST, _cfg


def random_walk_m5(seed: int, start="2023-01-01", end="2024-03-31", price=80.0, daily_vol=0.02, trend=0.0):
    """期待値ほぼゼロ（trend で傾き）の M5 足。17:00 ET 区切り・週末なし。"""
    rng = np.random.default_rng(seed)
    grid = pd.date_range(pd.Timestamp(start) + pd.Timedelta(hours=17), end, freq="5min")
    grid = grid[(grid + pd.Timedelta(hours=7)).weekday < 5]
    idx = grid.tz_localize("America/New_York", ambiguous="NaT", nonexistent="NaT")
    idx = idx[~idx.isna()]
    n, sub = len(idx), 5
    s = daily_vol / np.sqrt(276)
    drift = trend * s * np.sin(np.arange(n) / 4000.0)
    steps = rng.normal(0, s / np.sqrt(sub), (n, sub)) + (drift / sub)[:, None]
    path = np.log(price) + np.cumsum(steps.ravel()).reshape(n, sub)
    c = path[:, -1]
    o = np.r_[np.log(price), c[:-1]]
    df = pd.DataFrame({
        "open": np.exp(o), "high": np.exp(np.maximum(path.max(1), o)),
        "low": np.exp(np.minimum(path.min(1), o)), "close": np.exp(c),
        "spread": 4.0,
    }, index=idx.tz_convert("UTC"))
    return df.round(3)


def write_mt5(df: pd.DataFrame, path: Path):
    srv = (df.index.tz_convert("America/New_York") + pd.Timedelta(hours=7)).tz_localize(None)
    pd.DataFrame({
        "<DATE>": srv.strftime("%Y.%m.%d"), "<TIME>": srv.strftime("%H:%M:%S"),
        "<OPEN>": df["open"].values, "<HIGH>": df["high"].values, "<LOW>": df["low"].values,
        "<CLOSE>": df["close"].values, "<TICKVOL>": 1, "<VOL>": 0, "<SPREAD>": df["spread"].astype(int).values,
    }).to_csv(path, sep="\t", index=False)


# --------------------------------------------------------------------------- 約定の再現
def _fine(rows, start, minutes=5, spread=None):
    start = pd.Timestamp(start)
    start = start.tz_localize("UTC") if start.tz is None else start.tz_convert("UTC")
    idx = pd.date_range(start, periods=len(rows), freq=f"{minutes}min")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)
    if spread is not None:
        df["spread"] = spread
    return df


def test_fine_bars_resolve_tp_before_stop():
    # H1 の足 1 は高値 103・安値 98 で利確・損切りの両方に届く。足だけなら損切り優先、
    # M5 で見ると先に 103 まで上がってから 98 まで下がっている
    rows = [(100, 101, 99, 100), (100, 103, 98, 99), (99, 100, 98.5, 99.5)]
    h1 = make_bars(rows, hours=1)
    up = [(100 + k * 0.25, 100 + k * 0.25 + 0.3, 100 + k * 0.25 - 0.1, 100 + (k + 1) * 0.25) for k in range(11)]
    sub1 = up + [(102.75, 102.8, 98.0, 98.2)]
    sub = [(100, 101, 99, 100)] * 12 + sub1 + [(99, 100, 98.5, 99.5)] * 12
    fine = _fine(sub, h1.index[0])
    cfg = _cfg(exit=replace(PLAIN_EXIT, partial_tp_r=1.0, partial_fraction=0.5))
    strat = FixedSignals(entries={0: 1}, stop_dist=2.0)
    coarse = run_backtest({"SILVER": h1}, TEST_INST, [Sleeve("SILVER", strat)], cfg).trades.iloc[0]
    fine_t = run_backtest({"SILVER": h1}, TEST_INST, [Sleeve("SILVER", strat)], cfg,
                          fine_data={"SILVER": fine}).trades.iloc[0]
    assert coarse.pnl == pytest.approx((98.10 - 100.15) * 50 * 100)
    assert fine_t.pnl == pytest.approx((102.15 - 100.15) * 25 * 100 + (98.10 - 100.15) * 25 * 100)
    assert fine_t.exit_time > coarse.exit_time  # 小足の時刻で記録される


def test_fine_spread_spike_triggers_short_stop():
    # 売りの損切り 101.95。Bid の高値は 101.5 までだが、ある 5 分だけスプレッドが 0.6 に広がる
    rows = [(100, 101, 99, 100), (100, 101.5, 99.5, 100), (100, 100.5, 99.5, 100)]
    h1 = make_bars(rows, hours=1)
    spread = np.full(36, 100.0)          # point 0.001 × 100 = 0.1
    spread[12 + 6] = 600.0               # 0.6 に拡大
    sub = [(100, 100.5, 99.5, 100)] * 12 + [(100, 101.5, 99.5, 100)] * 12 + [(100, 100.5, 99.5, 100)] * 12
    fine = _fine(sub, h1.index[0], spread=spread)
    strat = FixedSignals(entries={0: -1}, stop_dist=2.0)
    cfg = _cfg()
    coarse = run_backtest({"SILVER": h1}, TEST_INST, [Sleeve("SILVER", strat)], cfg).trades.iloc[0]
    fine_t = run_backtest({"SILVER": h1}, TEST_INST, [Sleeve("SILVER", strat)], cfg,
                          fine_data={"SILVER": fine}).trades.iloc[0]
    assert coarse.reason == "end"
    assert fine_t.reason == "stop" and fine_t.exit_price == pytest.approx(101.95 + 0.05)


def test_fine_data_must_be_finer():
    m5 = random_walk_m5(1, end="2023-02-28")
    h1 = resample_ohlc(m5, "1h")
    sl = [Sleeve("WTI", make_strategy("pullback", fast_ema=20, slow_ema=60, adx_min=10))]
    with pytest.raises(ValueError):
        run_backtest({"WTI": h1}, get_instruments(), sl, BacktestConfig(), fine_data={"WTI": h1})


# --------------------------------------------------------------------------- 上位足フィルタ
def test_htf_filter_uses_only_completed_bars():
    m5 = random_walk_m5(2, end="2023-09-30", trend=3.0)
    h1 = resample_ohlc(m5, "1h")
    h4 = resample_ohlc(m5, "4h")
    inst = get_instruments()
    cfg = BacktestConfig(risk=RiskConfig(max_drawdown_halt=1, daily_loss_limit=1))
    strat = make_strategy("donchian", entry_period=20, exit_period=10, trend_ema=0)
    base = run_backtest({"WTI": h1}, inst, [Sleeve("WTI", strat)], cfg)
    filt = run_backtest({"WTI": h1}, inst, [Sleeve("WTI", strat, htf_frame=h4, htf_ema=20, htf_timeframe="H4")], cfg)
    assert 0 < len(filt.trades) < len(base.trades)
    # 先読みしていないこと: ある時点より後の上位足を書き換えても、それ以前の判断は変わらない
    cut = h4.index[len(h4) // 2]
    h4_mod = h4.copy()
    h4_mod.loc[h4_mod.index >= cut, ["open", "high", "low", "close"]] *= 0.5
    mod = run_backtest({"WTI": h1}, inst, [Sleeve("WTI", strat, htf_frame=h4_mod, htf_ema=20, htf_timeframe="H4")], cfg)
    before = lambda t: t[t["entry_time"] < cut][["entry_time", "side"]].reset_index(drop=True)  # noqa: E731
    pd.testing.assert_frame_equal(before(filt.trades), before(mod.trades))


# --------------------------------------------------------------------------- 時間足の混在
def test_mixed_timeframes_match_separate_runs():
    """銘柄ごとに時間足が違っても、相互作用が無ければ単独で走らせた結果と同じ売買になる。"""
    m5a, m5b = random_walk_m5(3, end="2023-08-31", trend=2.0), random_walk_m5(4, end="2023-08-31", price=70, trend=2.0)
    wti_h1, silver_h4 = resample_ohlc(m5a, "1h"), resample_ohlc(m5b, "4h")
    inst = get_instruments()
    loose = RiskConfig(max_drawdown_halt=1, daily_loss_limit=1, max_total_risk=1, cluster_max_risk={},
                       max_leverage_symbol=0, max_leverage_total=0)
    cfg = BacktestConfig(initial_equity=1e9, risk=loose)
    s1 = Sleeve("WTI", make_strategy("donchian", entry_period=40, exit_period=20, trend_ema=0))
    s2 = Sleeve("SILVER", make_strategy("donchian", entry_period=20, exit_period=10, trend_ema=0))
    both = run_backtest({"WTI": wti_h1, "SILVER": silver_h4}, inst, [s1, s2], cfg,
                        fine_data={"WTI": m5a, "SILVER": m5b}).trades
    cols = ["entry_time", "exit_time", "entry_price", "exit_price", "reason"]
    for s, data, fine in ((s1, wti_h1, m5a), (s2, silver_h4, m5b)):
        alone = run_backtest({s.symbol: data}, inst, [s], cfg, fine_data={s.symbol: fine}).trades
        pd.testing.assert_frame_equal(both[both["symbol"] == s.symbol][cols].reset_index(drop=True),
                                      alone[cols].reset_index(drop=True))


# --------------------------------------------------------------------------- データ・タスク・コスト
@pytest.fixture(scope="module")
def mtf_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("mtf")
    write_mt5(random_walk_m5(5, trend=2.0), d / "XTIUSD_M5.csv")                       # M5 だけ
    m5 = random_walk_m5(6, price=70, trend=2.0)
    write_mt5(m5, d / "XAGUSD_M5.csv")
    write_mt5(resample_ohlc(m5, "1h"), d / "XAGUSD_H1.csv")                          # H1 と M5
    return d


def _cfg_mtf(data_dir, **data_kw) -> TrainConfig:
    cfg = TrainConfig()
    cfg.data.dir = str(data_dir)
    for k, v in data_kw.items():
        setattr(cfg.data, k, v)
    return normalize(cfg)


def test_dataset_derives_signal_and_picks_fine(mtf_dir):
    ds = load_dataset(_cfg_mtf(mtf_dir, signal_timeframes=["H1", "H4"]).data)
    assert ds.symbols == ["SILVER", "WTI"]
    assert ds.file_timeframes("WTI") == ["M5"]
    h1 = ds.signal_frame("WTI", "H1")                     # M5 から作る
    assert pd.Timedelta(h1.index[1] - h1.index[0]) == pd.Timedelta("1h")
    assert ds.fine_timeframe("WTI", "H1") == "M5" and ds.fine_timeframe("SILVER", "H4") == "M5"
    assert ds.context_frame("SILVER", "D1") is not None   # 上位足も作れる
    ds2 = load_dataset(_cfg_mtf(mtf_dir, fill_timeframe="none").data)
    assert ds2.fine_timeframe("WTI", "H1") is None


def test_build_tasks_scales_grid_and_htf(mtf_dir):
    cfg = _cfg_mtf(mtf_dir, signal_timeframes=["H1", "H4"])
    cfg.strategies = [StrategySearch("donchian", ["WTI"], {
        "entry_period": [60, 120], "exit_period": [30],
        "htf.timeframe": ["none", "H4", "D1"], "htf.ema": [50, 100],
    })]
    ds = load_dataset(cfg.data)
    tasks, dims = build_tasks(cfg, ds)
    h1 = [t for t in tasks if t.timeframe == "H1"]
    h4 = [t for t in tasks if t.timeframe == "H4"]
    # H1: パラメータ 2 通り × (フィルタなし 1 + H4×2 + D1×2) / H4: 2 通り × (なし 1 + D1×2)
    assert len(h1) == 2 * 5 and len(h4) == 2 * 3
    assert sorted({t.params["entry_period"] for t in h4}) == [15, 30]   # H1 の本数を H4 に換算
    assert all(t.htf.get("timeframe") != "H4" for t in h4)              # 売買の足以下の上位足は使わない
    assert set(dims) == {"WTI:donchian@H1", "WTI:donchian@H4"}


def test_timeframe_costs_decrease_with_timeframe(mtf_dir):
    ds = load_dataset(_cfg_mtf(mtf_dir).data)
    costs = timeframe_costs(ds, get_instruments())
    wti = costs[costs["symbol"] == "WTI"].set_index("timeframe")
    assert {"M5", "M15", "H1", "H4"} <= set(wti.index)
    assert wti.loc["M5", "cost_per_r"] > wti.loc["H1", "cost_per_r"] > wti.loc["H4", "cost_per_r"]
    assert not wti.loc["H1", "from_file"] and wti.loc["M5", "from_file"]


def test_training_with_multiple_timeframes(tmp_path, mtf_dir):
    from cfdbot.train.config import load_train_config
    from cfdbot.train.pipeline import run_training

    p = tmp_path / "t.toml"
    p.write_text(f"""
output_dir = "{tmp_path / 'out'}"
[data]
dir = "{mtf_dir}"
signal_timeframes = ["H1", "H4"]
[walkforward]
train_months = 6
test_months = 3
min_trades = 3
[portfolio]
min_score = -100
[compute]
workers = 1
port = 0
[strategies.donchian]
symbols = ["WTI", "SILVER"]
[strategies.donchian.grid]
entry_period = [40, 80]
exit_period = [20]
trend_ema = [0]
"htf.timeframe" = ["none", "D1"]
"htf.ema" = [10]
[strategies.squeeze]
enabled = false
[strategies.pullback]
enabled = false
[strategies.reversion]
enabled = false
""", encoding="utf-8")
    logs = []
    out = run_training(load_train_config(p), log=logs.append)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "時間足ごとのコスト" in report and "約定の再現" in report
    assert any("約定の再現 M5" in m for m in logs)
    ea = list((out / "ea").glob("*.txt"))
    for f in ea:
        text = f.read_text()
        tf = f.stem.rsplit("_", 1)[1]
        assert f"ea_timeframe_minutes={ {'H1': 60, 'H4': 240}[tf] }" in text
        assert "htf_minutes=" in text
