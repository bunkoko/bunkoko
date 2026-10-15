"""相方の行き過ぎフィルタを、使っていない商品の組で確かめる（docs/cross_asset.md 7 章）の試験。作ったデータだけを使う。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from cfdbot import partner as P
from tests.test_trend import _yahoo_csv

ROOT = Path(__file__).resolve().parents[1]


def test_pairs_are_disjoint_known_and_not_the_core_four():
    keys = [k for pair in P.PAIRS for k in pair]
    assert len(keys) == len(set(keys)) == 10                                         # 1 つの市場の相方は 1 つだけ（EA と同じ形）
    assert all(k in P.MARKETS and P.MARKETS[k].ticker for k in keys)
    assert not set(keys) & {"GOLD", "SILVER", "WTI", "BRENT"}                           # 見つけた期間の市場は判定に入れない
    assert P.MARKETS["FEEDER"].ticker == "GF=F" and P.partner_map(P.PAIRS)["FEEDER"] == "CATTLE"
    assert (P.N_DAYS, P.THRESHOLD) == (20, 1.5)                                         # cross_asset.md 6 章と同じ


def test_partner_gate_blocks_only_the_direction_the_partner_already_ran():
    idx = pd.date_range("2015-01-01", periods=600, freq="B", tz="UTC")
    rng = np.random.default_rng(0)
    a = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 600)))
    r = rng.normal(0, 0.01, 600)
    r[-20:] = 0.03                                                                     # 相方は最後の 20 日に大きく上げた
    b = 100 * np.exp(np.cumsum(r))
    frames = {"A": pd.DataFrame({"close": a}, index=idx), "B": pd.DataFrame({"close": b}, index=idx)}
    g = P.partner_gates(frames, {"A": "B", "B": "A"})
    ga = g["A"]
    assert not ga["long"].iloc[-1] and ga["short"].iloc[-1]                            # A の買いは止める・売りは止めない
    assert ga["long"].iloc[300:560].mean() > 0.85                                      # ふだんはほとんど止めない
    assert set(g) == {"A", "B"}
    assert P.partner_gates({"A": frames["A"]}, {"A": "B"}) == {}                        # 相方の値が無ければ止めない


def test_verdict_needs_all_three_conditions():
    assert P.verdict(0.05, 0.96, 0.6).startswith("採用候補")
    assert P.verdict(0.05, 0.94, 0.6).startswith("採らない")
    assert P.verdict(0.05, 0.99, 0.4).startswith("採らない")
    assert P.verdict(-0.02, 1.0, 1.0).startswith("採らない")                          # 逆の結果を反転して「効く」とはしない
    assert P.verdict(float("nan"), None, None).startswith("判定できない")


def test_partner_study_runs_end_to_end(tmp_path, monkeypatch):
    uni = tmp_path / "universe"
    uni.mkdir()
    keys = [k for pair in P.PAIRS + P.CORE_PAIRS for k in pair]
    for i, k in enumerate(keys):
        _yahoo_csv(uni / f"{k}.csv", 10 + i, days=2000)
    cfg = tmp_path / "train.toml"
    cfg.write_text(f'output_dir = "{tmp_path / "out"}"\n[data]\ndir = "{tmp_path / "data"}"\n', encoding="utf-8")
    spec = importlib.util.spec_from_file_location("partner_study", ROOT / "scripts" / "partner_study.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(sys, "argv", ["partner_study.py", "--config", str(cfg), "--data", str(uni), "--start", "2016-01-01",
                                      "--split", "2020-06-01", "--placebo", "4", "--no-fetch", "--out", str(tmp_path / "partner")])
    mod.main()
    md = next((tmp_path / "partner").glob("*/report.md")).read_text(encoding="utf-8")
    assert "## 1. 判定（使っていない組だけ）" in md and "| 判定の 5 組 | 10 |" in md
    assert ("途中（偶然の回数が 4 回" in md) or ("採らない" in md)                   # 200 回より少ないと採用候補にしない
    assert "試した数: 79 ＋ 1 ＝ 80" in md and "## 3. 参考" in md and "素牛" in md
