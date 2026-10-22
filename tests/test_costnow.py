"""今のスプレッドで計算し直す（./cfd costnow、docs/costnow.md）のテスト。"""

import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdbot.costnow import floor_spread, recent_levels, verdict

from .test_demo_check_phillip import EXPORT, SYMS, _write_h1

ROOT = Path(__file__).resolve().parents[1]


def _bars(n=400, price=50.0, spread_pts=30, recent_pts=None, freq="1h"):
    idx = pd.date_range("2025-01-01", periods=n, freq=freq, tz="UTC")
    c = price * np.exp(np.cumsum(np.random.default_rng(0).normal(0, 0.01, n)))
    df = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "spread": float(spread_pts)}, index=idx)
    if recent_pts is not None:
        df.loc[df.index >= idx[-1] - pd.Timedelta(days=5), "spread"] = float(recent_pts)
    return df


def test_recent_levels_uses_the_last_days_only():
    fine = _bars(24 * 30, spread_pts=50, recent_pts=330)                  # 最後の 5 日だけ広い
    daily = fine.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    lv = recent_levels(fine, daily, point=0.001, slippage=0.01, days=5)
    assert abs(lv["spread"] - 0.33) < 1e-9
    assert abs(lv["rel"] - lv["spread"] / lv["price"]) < 1e-12
    assert lv["cost_ratio"] == (0.33 + 0.02) / (2.5 * lv["atr"])
    assert recent_levels(fine, daily, 0.001, 0.01, days=60)["spread"] < 0.33   # 長くとると昔の狭い分が混ざる


def test_floor_spread_keeps_wider_actual_spread():
    df = _bars(10, price=50.0, spread_pts=30)
    df.loc[df.index[3], "spread"] = 9999.0
    out = floor_spread(df, rel=0.005, point=0.001)                       # 値段 50 × 0.5% = 0.25 = 250 ポイント
    assert out["spread"].iloc[3] == 9999.0
    np.testing.assert_allclose(out["spread"].drop(out.index[3]), 0.005 * df["close"].drop(df.index[3]) / 0.001)
    assert (df["spread"] != out["spread"]).any() and df["spread"].iloc[0] == 30.0     # 元は変えない


def test_verdict_needs_both_periods_and_cost():
    assert verdict({"A": 0.4, "B": 0.3}, {"A": 0.35, "B": 0.3}, 0.08) == "銀を今のスプレッドでも入れる"
    assert verdict({"A": 0.4, "B": 0.2}, {"A": 0.35, "B": 0.3}, 0.08).startswith("今のまま（銀を入れた方が良いのが")
    assert "10% を超える" in verdict({"A": 0.4, "B": 0.3}, {"A": 0.3, "B": 0.2}, 0.12)
    assert verdict({"B": 0.4}, {"B": 0.3}, 0.05).startswith("判定できない")


def _write_yahoo(path: Path, price: float, seed: int) -> None:
    rng = np.random.default_rng(seed)
    d = pd.bdate_range("2000-01-03", "2021-06-30")
    n = len(d)
    drift = np.repeat(rng.normal(0, 0.004, n // 40 + 1), 40)[:n]
    c = price * np.exp(np.cumsum(drift + rng.normal(0, 0.015, n)))
    o = np.r_[c[0], c[:-1]]
    pd.DataFrame({"date": d.strftime("%Y-%m-%d"), "open": o, "high": np.maximum(o, c) * 1.005,
                  "low": np.minimum(o, c) * 0.995, "close": c}).to_csv(path, index=False)


@pytest.fixture(scope="module")
def phillip_and_futures(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("costnow")
    data = tmp_path / "data"
    data.mkdir()
    for name, (_, price, dvol, digits, mon, seed) in SYMS.items():
        _write_h1(data / f"{name}_H1.csv", mon, price, dvol, digits, seed)
    # 銀だけ、書き出しの最後の 90 日のスプレッドを広げる（30 → 330 ポイント = 0.033 → 0.33）
    silver = data / "XAGUSD.ps01_H1.csv"
    df = pd.read_csv(silver, sep="\t")
    t = pd.to_datetime(df["<DATE>"] + " " + df["<TIME>"], format="%Y.%m.%d %H:%M:%S")
    df.loc[t >= EXPORT - pd.Timedelta(days=90), "<SPREAD>"] = 330
    df.to_csv(silver, sep="\t", index=False)
    ctx = tmp_path / "context" / "yahoo"
    ctx.mkdir(parents=True)
    for key, price, seed in (("fut_gold", 400.0, 1), ("fut_silver", 5.0, 2), ("fut_wti", 25.0, 3), ("fut_brent", 25.0, 4)):
        _write_yahoo(ctx / f"{key}.csv", price, seed)
    cfg = tmp_path / "train.toml"
    cfg.write_text(f'[data]\ndir = "{data}"\nserver_tz = 9\nsignal_timeframes = ["D1"]\nevents = ""\n', encoding="utf-8")
    return tmp_path, cfg


def test_costnow_command_end_to_end(phillip_and_futures):
    tmp_path, cfg = phillip_and_futures
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "costnow_study.py"), "--config", str(cfg),
                        "--context", str(tmp_path / "context"), "--out", str(tmp_path / "out")],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-3000:]
    out = r.stdout
    assert "スプレッド 0.33" in out and "今のコスト比率" in out
    assert "## 期間A 先物の長期データ" in out and "## 期間B フィリップ" in out
    assert out.count("| ② SILVER を今のスプレッドで |") == 2 and out.count("| ③ SILVER を外す |") == 2
    assert "## 判定（docs/costnow.md 3 章）" in out
    assert any(w in out for w in ("**銀を今のスプレッドでも入れる**", "**今のまま（"))
    assert list((tmp_path / "out").glob("*/report.md"))


def test_risk_table_long_period(phillip_and_futures):
    tmp_path, cfg = phillip_and_futures
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "risk_table.py"), "--final",
                        str(ROOT / "config" / "baselines" / "turtle55_d1_ext.json"), "--start", "2024-12-01",
                        "--equity", "10000000", "--risk", "0.01,0.03", "--config", str(cfg),
                        "--context", str(tmp_path / "context"), "--long"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-3000:]
    a = r.stdout.split("期間A 先物の長期データ 2001-01〜2024-11")[1]
    rows = [ln for ln in a.splitlines() if "万円" in ln]
    assert len(rows) == 2 and "1.0%" in rows[0] and "3.0%" in rows[1]
    dd = [float(ln.split("%")[1].split()[-1]) for ln in rows]          # 2 つ目の % の前 = 最大DD
    assert dd[1] > dd[0]                                              # 1 回の損失を上げると下げも大きくなる
