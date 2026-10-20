"""J7 決算発表をまたいで持つ（docs/earnings_premium.md）の試験。作ったデータだけを使う。"""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path

import numpy as np
import pandas as pd

from cfdbot import earnings_premium as EP

ROOT = Path(__file__).resolve().parents[1]


def test_t0_follows_the_close_time_and_skips_corrections():
    idx = pd.bdate_range("2024-10-01", "2024-11-29")
    disc = pd.DataFrame({
        "time": ["2024-10-10 14:00:00", "2024-10-11 15:00:00", "2024-11-06 15:00:00", "2024-11-07 15:30:00",
                 "2024-10-12 10:00:00", "2024-10-15 15:00:00", "2024-10-16 15:00:00", "2024-10-17 12:00:00"],
        "ticker": ["A.T", "B.T", "C.T", "D.T", "E.T", "F.T", "G.T", "A.T"],
        "title": ["2025年3月期 第2四半期決算短信〔日本基準〕（連結）", "決算短信", "決算短信", "決算短信", "決算短信",
                  "（訂正）決算短信の一部訂正", "業績予想の修正に関するお知らせ", "決算短信"]})
    ev = EP.events(disc, idx, ["A.T", "B.T", "C.T", "D.T", "E.T", "F.T", "G.T"]).set_index("ticker")
    day = {t: idx[int(r.t0)] for t, r in ev.iterrows()}
    assert day["B.T"] == pd.Timestamp("2024-10-14")          # 15:00 は引け（15:00）ちょうど → 次の営業日
    assert day["C.T"] == pd.Timestamp("2024-11-06")          # 2024-11-05 から引けは 15:30 → 15:00 はその日
    assert day["D.T"] == pd.Timestamp("2024-11-08")          # 15:30 は引けの後
    assert day["E.T"] == pd.Timestamp("2024-10-14")          # 土曜 → 月曜
    assert "F.T" not in day and "G.T" not in day              # 訂正・短信でない開示は使わない
    assert len(ev.loc[["A.T"]]) == 2                          # 同じ会社の別の決算は別々
    assert EP.is_tanshin("２０２６年３月期　決算短信") and not EP.is_tanshin("決算説明資料")


def test_weights_hold_two_days_from_the_day_before():
    ev = pd.DataFrame({"j": [0, 1], "t0": [3, 3]})
    w = EP.weights(ev, (8, 3))
    assert np.allclose(w[2], [0.5, 0.5, 0]) and np.allclose(w[3], [0.5, 0.5, 0]) and w[[0, 1, 4, 5]].sum() == 0


def _world(n=80, start="2009-10-01", end="2017-12-29", effect=0.01, seed=0):
    idx = pd.bdate_range(start, end)
    cols = [f"{1000 + i}.T" for i in range(n)]
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0003, 0.015, (len(idx), n))
    rows = []
    for j, t in enumerate(cols):
        for q in pd.date_range(start, end, freq="QS"):
            d = q + pd.Timedelta(days=int(40 + j % 20))
            pos = int(idx.searchsorted(d))
            if 1 <= pos <= len(idx) - 2:
                rows.append({"time": f"{idx[pos - 1]:%Y-%m-%d} 16:00:00", "ticker": t, "title": "決算短信〔日本基準〕（連結）"})
                r[pos, j] += effect
                r[pos + 1, j] += effect
    close = pd.DataFrame(100 * np.exp(np.cumsum(r, axis=0)), index=idx, columns=cols)
    return pd.DataFrame(rows), close.shift(1).fillna(100.0), close


def test_past_test_uses_only_the_unseen_part_and_judges():
    disc, open_, close = _world()
    earn = pd.DataFrame({"ticker": close.columns.repeat(2), "date": ["2014-01-15", "2015-01-15"] * len(close.columns)})
    earn.loc[earn["ticker"] == "1079.T", "date"] = "2009-01-01"            # この会社は全部「見た」
    r = EP.evaluate_past(disc, earn, open_, close, placebo=20)
    assert r["events"] >= EP.MIN_EVENTS and r["months"] >= EP.MIN_MONTHS and r["end"] < pd.Timestamp("2014-01-15")
    assert r["verdict"] == "使えそう" and r["judge"]["placebo"] == 1.0 and abs(r["judge"]["threshold"] - 2.481) < 0.01
    assert r["two_day"] > 0.015 and r["seen_events"] > 0 and "seen" in r
    # 見た期間しか無ければ判定しない
    early = earn.assign(date="2009-06-01")
    r2 = EP.evaluate_past(disc, early, open_, close, placebo=5)
    assert r2["verdict"].startswith("判定しない（使っていない決算が無い")
    short = earn.assign(date="2011-06-01")
    assert EP.evaluate_past(disc, short, open_, close, placebo=5)["verdict"].startswith("判定しない（使っていない決算が")
    noise_disc, o2, c2 = _world(effect=0.0, seed=4)
    assert EP.evaluate_past(noise_disc, earn, o2, c2, placebo=20)["verdict"] == "使えない"


def test_forward_record_counts_from_the_start_and_judges_at_looks():
    disc, open_, close = _world(start="2026-06-01", end="2027-12-31", effect=0.01)
    r = EP.forward(disc, open_, close)
    assert r["events"] > 0 and r["daily"].index[0] >= pd.Timestamp(EP.START_FORWARD)
    assert r["verdict"].startswith("続いている（250 営業日")
    early = EP.forward(disc, open_[open_.index <= "2027-02-01"], close[close.index <= "2027-02-01"])
    assert early["verdict"].startswith("途中経過") and "あと" in early["verdict"]


def test_premium_command_writes_both_reports(tmp_path):
    disc, open_, close = _world(n=40, end="2012-12-28")
    root, data = tmp_path / "agents", tmp_path / "stocks"
    (root / "ir").mkdir(parents=True)
    data.mkdir()
    disc.assign(id="x", company="会社", url="", xbrl="").to_csv(root / "ir" / "disclosures.csv", index=False)
    close.to_csv(data / "jp_close.csv")
    open_.to_csv(data / "jp_open.csv")
    pd.DataFrame({"ticker": close.columns, "date": "2012-01-10"}).to_csv(data / "jp_earnings.csv", index=False)
    spec = importlib.util.spec_from_file_location("agents_premium", ROOT / "scripts" / "agents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.cmd_premium(Namespace(root=str(root), data=str(data), out=str(tmp_path / "out"), test=True, placebo=5))
    text = next((tmp_path / "out").glob("*/premium.md")).read_text(encoding="utf-8")
    assert "## 1. 過去の検証" in text and "判定しない（使っていない決算が" in text and "見た決算では" in text
    assert "## 2. 点検" in text and "| 2010 |" in text and "混んだ日" in text
    mod.cmd_premium(Namespace(root=str(root), data=str(data), out=str(tmp_path / "out2"), test=False, placebo=5))
    text2 = next((tmp_path / "out2").glob("*/premium.md")).read_text(encoding="utf-8")
    assert "## 1. 先の記録" in text2 and "途中経過" in text2


def test_diagnose_splits_groups_years_and_crowding():
    disc, open_, close = _world()
    earn = pd.DataFrame({"ticker": close.columns.repeat(2), "date": ["2016-01-15", "2017-01-15"] * len(close.columns)})
    earn = earn[~earn["ticker"].isin(["1000.T", "1001.T"])]                 # この 2 社は Yahoo の決算日が無い → 全部「使った」
    g = EP.diagnose(disc, earn, open_, close)
    grp = g["groups"].set_index("group")
    total = grp.loc["使った決算（全部）", "events"]
    parts = grp.loc[[k for k in grp.index if k.startswith("　うち")], "events"].sum()
    assert total == parts and grp.loc["　うち Yahoo の決算日が 1 つも無い会社", "firms"] == 2
    assert grp.loc["　うち Yahoo の決算日より前（その会社の最初の日より 5 年以上前）", "events"] > 0
    assert abs(grp.loc["見た決算", "net"] - (grp.loc["見た決算", "mean"] - EP.COST)) < 1e-12
    assert grp.loc["使った決算（全部）", "mean"] > 0.015 and grp.loc["使った決算（全部）", "t"] > 2
    cr = g["crowd"]
    assert cr["events"].sum() == total + grp.loc["見た決算", "events"] and (cr["days"] > 0).all()
    assert g["big"]["k"] >= 1 and 0 < g["big"]["pos"] <= 1
    assert 2016 in g["by_year"].index and g["by_year"][("count", True)].loc[2010] > 0
