import json

import pandas as pd

from cfdbot.instruments import get_instruments
from cfdbot.train.check import run_check, write_measured
from cfdbot.train.config import TrainConfig, normalize

from .test_multitf import random_walk_m5, write_mt5


def _data(tmp_path):
    m5 = random_walk_m5(9, start="2024-04-01", end="2024-06-30")   # 夏時間だけの期間
    et = m5.index.tz_convert("America/New_York")
    m5 = m5[et.hour != 17]                                           # 毎日の休止（17 時台）
    m5["spread"] = 30.0                                              # 0.03 ドル（point 0.001）
    write_mt5(m5, tmp_path / "XAGUSD_M5.csv")
    return tmp_path


def _cfg(d, server_tz="ny_close"):
    cfg = TrainConfig()
    cfg.data.dir = str(d)
    cfg.data.server_tz = server_tz
    cfg.data.fill_timeframe = "M5"
    return normalize(cfg)


def test_check_ok(tmp_path):
    res = run_check(_cfg(_data(tmp_path)))
    assert not res.warnings, res.warnings
    assert any("毎日の休止は米東部時間 17 時台" in line for line in res.lines)
    assert res.measured["SILVER"].spread == 0.03
    assert set(res.costs["timeframe"]) >= {"M5", "H1", "H4"}


def test_check_detects_wrong_server_time(tmp_path):
    res = run_check(_cfg(_data(tmp_path), server_tz=5.0))
    msgs = " ".join(res.warnings)
    assert "休止が米東部時間 15 時台" in msgs and "server_tz を 3" in msgs


def test_write_measured(tmp_path):
    res = run_check(_cfg(_data(tmp_path)))
    path = write_measured(res, get_instruments(), tmp_path / "inst.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["SILVER"]["spread"] == 0.03 and "WTI" in data


def test_split_exports_are_merged(tmp_path):
    from cfdbot.train.dataset import load_dataset

    from .test_multitf import random_walk_m5, write_mt5

    m5 = random_walk_m5(3, start="2024-01-01", end="2024-03-31")
    half = len(m5) // 2
    write_mt5(m5.iloc[: half + 100], tmp_path / "XAGUSD_M5_part1.csv")   # 重なりあり
    write_mt5(m5.iloc[half:], tmp_path / "XAGUSD_M5_part2.csv")
    ds = load_dataset(_cfg(tmp_path).data)
    assert len(ds.frames["SILVER"]["M5"]) == len(m5)
    assert ds.frames["SILVER"]["M5"].index.is_monotonic_increasing


def test_compare_signals_with_final(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from compare_signals import python_signals_from_final

    d = _data(tmp_path)
    final = {"sleeves": [{"symbol": "SILVER", "strategy": "donchian", "timeframe": "H1",
                          "params": {"entry_period": 40, "exit_period": 20, "trend_ema": 0}, "exit": {},
                          "htf": {"timeframe": "H4", "ema": 10}}]}
    fp = tmp_path / "final.json"
    fp.write_text(json.dumps(final), encoding="utf-8")
    cfg = tmp_path / "train.toml"
    cfg.write_text(f'[data]\ndir = "{d}"\n', encoding="utf-8")
    sig = python_signals_from_final(str(fp), "SILVER", str(cfg))
    # EA と同じ形式の記録を Python の結果から作る → 一致するはず
    log = pd.DataFrame({
        "time_utc": sig.index.strftime("%Y.%m.%d %H:%M"), "close": 0, "atr": sig["atr"].values,
        "entry": sig["entry"].values, "stop_dist": sig["stop_dist"].fillna(-1).values,
        "exit_long": sig["exit_long"].astype(int).values, "exit_short": sig["exit_short"].astype(int).values,
    })
    log_path = tmp_path / "ea_log.csv"
    log.to_csv(log_path, index=False)
    root = Path(__file__).resolve().parents[1]
    p = subprocess.run([sys.executable, str(root / "scripts" / "compare_signals.py"), "--final", str(fp),
                        "--symbol", "SILVER", "--config", str(cfg), "--ea-log", str(log_path)],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert "結果: 一致" in p.stdout
