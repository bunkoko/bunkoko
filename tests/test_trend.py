import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tests.test_train import write_mt5_csv

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("trend_study", ROOT / "scripts" / "trend_study.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["trend_study"] = mod          # dataclass が __future__ annotations で自分のモジュールを引くため
    spec.loader.exec_module(mod)
    return mod


def _yahoo_csv(path: Path, seed: int, start="2015-01-01", days=2600):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=days)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, days)))
    pd.DataFrame({"date": idx.strftime("%Y-%m-%d"), "open": close, "high": close * 1.01, "low": close * 0.99,
                  "close": close, "volume": 1.0}).to_csv(path, index=False)


def test_volcap_only_reduces_and_needs_history():
    mod = _load()
    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2015-01-01", periods=800, freq="B", tz="UTC")
    a, b = rng.normal(0, 0.01, 800), rng.normal(0, 0.01, 800)
    b[600:] = a[600:]                                              # 最後の 200 日は 2 つが同じ向きに動く（かごの変動が増える）
    frames = {s: pd.DataFrame({"close": 100 * np.exp(np.cumsum(x))}, index=idx) for s, x in (("GOLD", a), ("SILVER", b))}
    g = mod.volcap_gates(frames)
    m = g["GOLD"]["long_size"]
    assert (m <= 1.0).all() and (m >= 0.5).all()
    assert (m.iloc[:300] == 1.0).all()                             # 250 日たまるまでは減らさない
    assert m.iloc[-100:].mean() < 0.85                             # 同じ向きに動く所では減る（銘柄ごとの ATR では分からない）
    assert (g["GOLD"]["long_size"] == g["GOLD"]["short_size"]).all()


def test_trend_study_runs_three_periods_and_writes_verdicts(tmp_path, monkeypatch):
    mod = _load()
    uni = tmp_path / "universe"
    uni.mkdir()
    for i, k in enumerate(("GOLD", "SILVER", "WTI", "BRENT", "COPPER", "PLATINUM", "NATGAS")):
        _yahoo_csv(uni / f"{k}.csv", i)
    data = tmp_path / "data"
    data.mkdir()
    write_mt5_csv(data / "XTIUSD_H1.csv", "oil", 1, start="2021-01-02", end="2024-06-30")
    write_mt5_csv(data / "XAGUSD_H1.csv", "silver", 2, start="2021-01-02", end="2024-06-30")
    cfg = tmp_path / "train.toml"
    cfg.write_text(f'output_dir = "{tmp_path / "out"}"\n[data]\ndir = "{data}"\n', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["trend_study.py", "--config", str(cfg), "--data", str(uni),
                                      "--split", "2022-06-01", "--long-start", "2016-01-01",
                                      "--out", str(tmp_path / "trend")])
    mod.main()
    md = next((tmp_path / "trend").glob("*/report.md")).read_text(encoding="utf-8")
    assert "## 期間A" in md and "## 期間B" in md and "## 期間C" in md
    assert "| multi:" in md and "| volcap:" in md and "| tp2r:" in md
    assert "期間B（フィリップのデータ）はこの環境に無い" not in md
    assert md.count("採用候補**") + md.count("| 参考 |") + md.count("| 採らない |") == 5
