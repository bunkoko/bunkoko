"""J2 夜だけ持つ（cfdbot/overnight.py、./cfd overnight）の試験。作ったデータだけ。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

from cfdbot import overnight as O

ROOT = Path(__file__).resolve().parents[1]


def _prices(days, night=0.0005, day=-0.0003, seed=0):
    rng = np.random.default_rng(seed)
    o, c, rows = None, 1000.0, []
    for d in days:
        o = c * (1 + night + rng.normal(0, 0.002))
        c = o * (1 + day + rng.normal(0, 0.008))
        rows.append((f"{d:%Y-%m-%d}", o, c))
    return pd.DataFrame(rows, columns=["date", "open", "close"])


def test_legs_split_the_hold_into_night_and_day_and_drop_errors():
    days = pd.bdate_range("2026-10-19", periods=5)
    p = pd.DataFrame({"date": [f"{d:%Y-%m-%d}" for d in days], "open": [100, 101, 99, 300, 101.0], "close": [100, 100, 100, 100, 102.0]})
    lg = O.legs(p)
    assert np.isclose(lg.loc["2026-10-19", "night"], 0.01) and np.isclose(lg.loc["2026-10-19", "hold"], 0.0)
    assert np.isclose((1 + lg["night"]) * (1 + lg["day"]) - 1, lg["hold"]).all()          # 夜 × 昼 ＝ 持ち続け
    assert pd.Timestamp("2026-10-21") not in lg.index                                     # 寄り 300 は誤りとして除く
    assert lg.loc["2026-10-19", "sold_on"] == pd.Timestamp("2026-10-20")


def test_forward_starts_on_the_20th_and_verdicts_use_the_look_points():
    days = pd.bdate_range("2025-01-01", "2030-12-31")
    lg = O.legs(_prices(days))
    fw = O.forward(lg)
    assert fw.index[0] == pd.Timestamp("2026-10-20")
    assert O.verdict(O.summary(fw.iloc[:100], O.JUDGE_COST)).startswith("途中経過（100 夜。250 夜で 1 回目、あと 150 夜）")
    s = O.summary(O.forward(lg, upto=750), O.JUDGE_COST)
    assert s["nights"] == 750 and s["diff_ann"] > 0 and O.verdict(s).startswith("続いている（750 夜")   # 昼が下がる世界では夜が勝つ
    bad = O.legs(_prices(days, night=-0.0005, day=0.0005))
    assert O.verdict(O.summary(O.forward(bad, upto=250), O.JUDGE_COST)).startswith("続いていない")
    r = pd.Series([0.10, -0.05], index=pd.to_datetime(["2027-06-01", "2028-06-01"]))
    assert np.isclose(O.after_tax_yearly(r), (1 + 0.10 * (1 - O.TAX)) * 0.95 - 1)            # 毎年の利益に税（損の年は税なし）
    assert np.isclose(O.after_tax_at_end(r), (1.10 * 0.95 - 1) * (1 - O.TAX))


def test_report_command_runs_without_the_network(tmp_path):
    spec = importlib.util.spec_from_file_location("overnight_study", ROOT / "scripts" / "overnight_study.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    days = pd.bdate_range("2009-01-05", "2026-11-30")
    path = mod.main(["--report", "--data", str(tmp_path / "d"), "--out", str(tmp_path / "out"), "--today", "2026-12-01"],
                    getter=lambda t, s: _prices(days))
    text = path.read_text(encoding="utf-8")
    assert "## 1. 途中経過と判定" in text and "途中経過（" in text and "往復 0.10% では「使えない」と判定した後の参考" in text
    assert (tmp_path / "d" / "1306.csv").exists()
