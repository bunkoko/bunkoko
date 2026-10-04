import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdbot.data import synthetic_ohlc
from cfdbot.train.config import PortfolioConfig, WalkForwardConfig, load_train_config
from cfdbot.train.dataset import load_dataset
from cfdbot.train.portfolio import (
    SleeveCandidates,
    allocate,
    erc_weights,
    make_windows,
    max_drawdown,
    select_params,
)

ROOT = Path(__file__).resolve().parents[1]


def write_mt5_csv(path: Path, kind: str, seed: int, start="2022-01-02", end="2024-06-30", hours=1):
    df = synthetic_ohlc(kind, start=start, end=end, hours=hours, seed=seed)
    srv = (df.index.tz_convert("America/New_York") + pd.Timedelta(hours=7)).tz_localize(None)
    pd.DataFrame({
        "<DATE>": srv.strftime("%Y.%m.%d"), "<TIME>": srv.strftime("%H:%M:%S"),
        "<OPEN>": df["open"], "<HIGH>": df["high"], "<LOW>": df["low"], "<CLOSE>": df["close"],
        "<TICKVOL>": 1, "<VOL>": 0, "<SPREAD>": df["spread"].astype(int),
    }).to_csv(path, sep="\t", index=False)


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("data")
    write_mt5_csv(d / "XTIUSD_H1.csv", "oil", 1)
    write_mt5_csv(d / "XAGUSD-H1.csv", "silver", 2)
    write_mt5_csv(d / "XAUUSD_H4.csv", "gold", 3, hours=4)   # 時間足が違う → 読み飛ばし
    (d / "notes.csv").write_text("x\n")                        # 銘柄名が分からない → 読み飛ばし
    return d


def tiny_config(tmp_path: Path, data_dir: Path) -> Path:
    p = tmp_path / "train.toml"
    p.write_text(f"""
output_dir = "{tmp_path / 'out'}"
[data]
dir = "{data_dir}"
[walkforward]
train_months = 9
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
entry_period = [60, 120]
exit_period = [30, 90]
[strategies.squeeze]
enabled = false
[strategies.pullback]
enabled = false
[strategies.reversion]
enabled = false
""", encoding="utf-8")
    return p


def test_config_defaults_and_overrides(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[account]\nequity = 2000000\n[account.cluster_max_risk]\nenergy = 0.01\n', encoding="utf-8")
    cfg = load_train_config(p)
    assert cfg.account.equity == 2_000_000
    assert cfg.account.cluster_max_risk == {"energy": 0.01, "metals": 0.02}
    assert cfg.data.signal_timeframes == ["H1"] and len(cfg.strategies) == 4
    p.write_text("[account]\nequityy = 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_train_config(p)


def test_dataset_discovery(data_dir):
    cfg = load_train_config(None).data
    cfg.dir = str(data_dir)
    ds = load_dataset(cfg)
    # 金は H4 しか無いので H1 の売買はできない（H4 から H1 は作れない）
    assert ds.symbols == ["SILVER", "WTI"]
    assert ds.file_timeframes("GOLD") == ["H4"]
    assert ds.fx is None
    assert len(ds.skipped) == 1  # notes.csv


def test_erc_equalizes_risk_contributions():
    cov = np.array([[0.04, 0.03, 0.0], [0.03, 0.04, 0.0], [0.0, 0.0, 0.01]])
    w = erc_weights(cov)
    rc = w * (cov @ w)
    assert np.allclose(rc, rc.mean(), rtol=1e-4)
    # 相関の高い 2 つ（WTI とブレントのような組）は合わせても 1 つ分程度に抑えられる
    assert w[0] + w[1] < 2 * w[2]


def test_allocate_hits_target_vol_and_dd_cap():
    rng = np.random.default_rng(0)
    r = rng.normal(0.0004, 0.006, (3, 500))
    pc = PortfolioConfig(target_vol=0.10, max_train_dd=1.0)
    m, vol, dd = allocate(r, np.ones(3), pc, 0.0, 10.0)
    assert vol == pytest.approx(0.10, rel=0.15)
    pc = PortfolioConfig(target_vol=0.30, max_train_dd=0.05)
    m2, vol2, dd2 = allocate(r, np.ones(3), pc, 0.0, 10.0)
    assert dd2 <= 0.05 * 1.1 and vol2 < 0.30


def test_select_params_prefers_plateau():
    days = 300
    rng = np.random.default_rng(1)
    base = rng.normal(0, 0.01, days)
    # 5 点の一次元グリッド。中央の 1 点だけ尖って良い（ノイズ）、右側 3 点は安定して良い
    quality = [0.0, 0.0, 0.004, 0.002, 0.002]
    rets = np.vstack([base + q + (0.0 if i != 2 else 0.0) for i, q in enumerate(quality)])
    rets[2] = base * 0.3 + 0.004  # 尖った点
    rets[3] = base + 0.0025
    rets[4] = base + 0.0025
    c = SleeveCandidates("WTI", "donchian", [{"p": i} for i in range(5)], [{}] * 5,
                         [(i,) for i in range(5)], (5,), rets, [np.arange(0, days, 5)] * 5)
    wf = WalkForwardConfig(min_trades=1, plateau=True)
    assert select_params(c, 0, days, wf).index in (3, 4)
    wf = WalkForwardConfig(min_trades=1, plateau=False)
    assert select_params(c, 0, days, wf).index == 2


def test_make_windows_and_drawdown():
    cal = pd.DatetimeIndex(pd.bdate_range("2021-01-01", "2024-12-31"))
    w = make_windows(cal, cal[0], 24, 6)
    assert len(w) == 4 and all(a < b < c for a, b, c in w)
    assert max_drawdown(np.array([0.1, -0.5, 0.2])) == pytest.approx(0.5)


def test_training_end_to_end_and_cache(tmp_path, data_dir):
    from cfdbot.train.pipeline import run_training

    cfg_path = tiny_config(tmp_path, data_dir)
    logs: list[str] = []
    out = run_training(load_train_config(cfg_path), log=logs.append, config_path=str(cfg_path))
    assert (out / "report.md").exists() and (out / "windows.csv").exists()
    final = json.loads((out / "final.json").read_text(encoding="utf-8"))
    for s in final["sleeves"]:
        assert 0.0025 <= s["risk_per_trade"] <= 0.02
        for f in s["ea_files"]:
            assert (out / f).exists()
    assert any("ウォークフォワード" in m for m in logs)
    # 2 回目はキャッシュを使い、計算しない
    logs2: list[str] = []
    run_training(load_train_config(cfg_path), log=logs2.append)
    assert any("計算済み" in m for m in logs2)


def test_remote_worker_bootstrap(tmp_path, data_dir):
    """iPad などの外部ワーカー: コードを zip で受け取り、データを HTTP で受け取って計算できる。"""
    from cfdbot.train.distributed import Coordinator, ResultStore
    from cfdbot.train.pipeline import _eval_settings, _sha, build_tasks

    cfg = load_train_config(tiny_config(tmp_path, data_dir))
    ds = load_dataset(cfg.data)
    tasks, _ = build_tasks(cfg, ds)
    meta = {"version": 1, "server_tz": cfg.data.server_tz, "signal_timeframes": ["H1"], "fill_timeframe": "auto",
            "files": {k: {"path": "/nonexistent/" + p.name, "name": p.name, "sha256": _sha(p)}
                      for k, p in ds.files.items()},
            "settings": _eval_settings(cfg, ds).to_dict()}
    store = ResultStore(tmp_path / "r.sqlite")
    coord = Coordinator([t.to_dict() for t in tasks[:2]], meta, ds.files, store, "tok", port=0)
    coord.start()
    try:
        worker = tmp_path / "remote_worker.py"
        worker.write_bytes((ROOT / "scripts" / "remote_worker.py").read_bytes())
        p = subprocess.run([sys.executable, str(worker), coord.url, "tok"], cwd=tmp_path,
                           capture_output=True, text=True, timeout=300)
        assert p.returncode == 0, p.stderr
        st = coord.status()
        assert st["done"] == 2 and st["failed"] == 0
        assert all(w.startswith("remote-") for w in st["completed_by"])
    finally:
        coord.stop()
        store.close()


def test_candidates_not_ready_are_skipped():
    """指標が落ち着く前に学習期間が始まる回では、その候補（例: 日足の長い EMA）を使わない。"""
    from cfdbot.train.config import PortfolioConfig
    from cfdbot.train.portfolio import build_portfolio

    days = 600
    rng = np.random.default_rng(3)
    rets = rng.normal(0.002, 0.01, (1, days))
    h4 = SleeveCandidates("GOLD", "pullback", [{}], [{}], [(0,)], (1,), rets, [np.arange(0, days, 5)],
                          timeframe="H4", ready=0)
    d1 = SleeveCandidates("WTI", "donchian", [{}], [{}], [(0,)], (1,), rets.copy(), [np.arange(0, days, 5)],
                          timeframe="D1", ready=300)
    wf, pc = WalkForwardConfig(min_trades=1, plateau=False), PortfolioConfig(min_score=0.0)
    early, _, _ = build_portfolio([h4, d1], 0, 250, wf, pc, 0.25, 2.0)
    late, _, _ = build_portfolio([h4, d1], 300, 550, wf, pc, 0.25, 2.0)
    assert {p.symbol for p in early} == {"GOLD"}
    assert {p.symbol for p in late} == {"GOLD", "WTI"}


def test_jst_server_h4_then_d1_reuses_cache_safely(tmp_path):
    """フィリップ（日本時間）のデータで H4 だけ学習した後、H4+D1 で学習し直しても落ちない（実際に起きた不具合）。

    日本時間のサーバーでは金曜の米国午後の分が土曜の足になる。取引日は金曜に寄せ、カレンダーに土日を入れない。
    """
    from cfdbot.train.config import load_train_config
    from cfdbot.train.dataset import load_dataset
    from cfdbot.train.pipeline import run_training

    d = tmp_path / "data"
    d.mkdir()
    for name, kind, seed in (("XTIUSD.ps01", "oil", 1), ("XAUUSD.ps01", "gold", 3)):
        df = synthetic_ohlc(kind, start="2022-01-02", end="2024-06-30", hours=1, seed=seed)
        srv = df.index.tz_convert("Asia/Tokyo").tz_localize(None)        # 日本時間で書き出す
        pd.DataFrame({"<DATE>": srv.strftime("%Y.%m.%d"), "<TIME>": srv.strftime("%H:%M:%S"),
                      "<OPEN>": df["open"], "<HIGH>": df["high"], "<LOW>": df["low"], "<CLOSE>": df["close"],
                      "<TICKVOL>": 1, "<VOL>": 0, "<SPREAD>": df["spread"].astype(int)}
                     ).to_csv(d / f"{name}_H1.csv", sep="\t", index=False)
    text = tiny_config(tmp_path, d).read_text(encoding="utf-8")
    text = text.replace(f'dir = "{d}"', f'dir = "{d}"\nserver_tz = 9\nsignal_timeframes = ["H4"]')
    text = text.replace('symbols = ["WTI", "SILVER"]', 'symbols = ["WTI", "GOLD"]')
    text += '[strategies.donchian.grid_D1]\nentry_period = [20, 40]\nexit_period = [10]\n'
    cfg_path = tmp_path / "train.toml"
    cfg_path.write_text(text, encoding="utf-8")

    run_training(load_train_config(cfg_path), log=lambda m: None)
    cfg_path.write_text(text.replace('signal_timeframes = ["H4"]', 'signal_timeframes = ["H4", "D1"]'),
                        encoding="utf-8")
    cfg = load_train_config(cfg_path)
    out = run_training(cfg, log=lambda m: None)
    assert (out / "report.md").exists()

    ds = load_dataset(cfg.data)
    assert set(ds.calendar().weekday) <= {0, 1, 2, 3, 4}                # 土日が無い
    d1 = ds.signal_frame("GOLD", "D1")
    assert (d1.index.tz_convert("Asia/Tokyo").weekday == 5).any()       # 土曜の短い日足はある
