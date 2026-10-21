"""案 2-US（docs/us_text.md）の点検 1 の試験。作ったデータだけを使う。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

from cfdbot import edgar as EG

ROOT = Path(__file__).resolve().parents[1]


def _tables(n=500):
    cur = pd.DataFrame({"Symbol": [f"T{i}" for i in range(n - 1)] + ["BRK.B"], "Security": [f"Co {i}" for i in range(n)],
                        "GICS Sector": ["Energy", "Utilities"] * (n // 2), "CIK": list(range(1000, 1000 + n))})
    cols = pd.MultiIndex.from_tuples([("Effective Date", "Effective Date"), ("Added", "Ticker"), ("Added", "Security"),
                                      ("Removed", "Ticker"), ("Removed", "Security"), ("Reason", "Reason")])
    ch = pd.DataFrame([["March 18, 2024", "T1", "Co 1", "OLD1", "Old 1", "x"],
                       ["June 22, 2020[3]", "T2", "Co 2", "OLD2", "Old 2", "x"],
                       ["January 2, 2015", "T3", "Co 3", "", "", "x"]], columns=cols)
    return [cur, ch, pd.DataFrame({"x": [1]})]


def test_membership_undoes_later_changes_newest_first():
    current, changes = EG.parse_wiki(_tables())
    assert "BRK-B" in set(current["ticker"]) and changes["date"].iloc[0] == pd.Timestamp("2024-03-18")
    assert changes.loc[changes["added"] == "T3", "removed"].isna().all()               # 足しただけの変更
    ends = EG.month_ends("2014-12-31", "2024-06-30")
    mem = EG.membership(current, changes, ends)
    assert "T1" in mem[pd.Timestamp("2024-03-31")] and "OLD1" not in mem[pd.Timestamp("2024-03-31")]
    assert "OLD1" in mem[pd.Timestamp("2024-02-29")] and "T1" not in mem[pd.Timestamp("2024-02-29")]
    assert "OLD2" in mem[pd.Timestamp("2020-05-31")] and "T3" not in mem[pd.Timestamp("2014-12-31")]
    assert len(mem[pd.Timestamp("2014-12-31")]) == 499 and len(mem[pd.Timestamp("2024-06-30")]) == 500


def test_coverage_gate_counts_missing_member_months():
    current, changes = EG.parse_wiki(_tables())
    ends = EG.month_ends("2014-12-31", "2016-12-31")
    mem = EG.membership(current, changes, ends)
    idx = pd.bdate_range("2014-12-01", "2016-12-30")
    tick = sorted({t for ts in mem.values() for t in ts})
    close = pd.DataFrame(100.0, index=idx, columns=tick)
    cov = EG.coverage(mem, EG.monthly_last(close))
    assert cov["ok"] and cov["missing"] == 0 and len(cov["bad_months"]) == 0
    close[tick[:60]] = np.nan                                                          # 60 社（12%）の値段が無い
    cov2 = EG.coverage(mem, EG.monthly_last(close))
    assert not cov2["ok_price"] and cov2["ok_count"] and 0.11 < cov2["missing"] < 0.13
    assert EG.missing_names(mem, EG.monthly_last(close)).iloc[0] == len(ends)                # 値段の無い会社は全部の月で数える
    small = {m: frozenset(list(ts)[:450]) for m, ts in mem.items()}                     # 数が足りない月ばかり
    assert not EG.coverage(small, EG.monthly_last(close.fillna(1.0)))["ok_count"]
    ml = EG.monthly_last(pd.DataFrame({"A": [1.0, 2.0, np.nan]}, index=pd.to_datetime(["2015-01-29", "2015-01-30", "2015-02-02"])))
    assert ml.index.tolist() == [pd.Timestamp("2015-01-31"), pd.Timestamp("2015-02-28")] and ml["A"].iloc[0] == 2.0


def test_universe_command_writes_the_check_report(tmp_path):
    spec = importlib.util.spec_from_file_location("edgar_cmd", ROOT / "scripts" / "edgar.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def prices(tickers, since):
        idx = pd.bdate_range(since, "2016-12-30")
        return pd.DataFrame(100.0, index=idx, columns=[t for t in tickers if t != "OLD2"])

    path = mod.main(["universe", "--root", str(tmp_path / "edgar"), "--out", str(tmp_path / "out"), "--today", "2017-01-15"],
                    tables=_tables(), prices=prices)
    text = path.read_text(encoding="utf-8")
    assert "## 1. 判定" in text and "点検 1: 通った" in text and "OLD2" in text
    assert (tmp_path / "edgar" / "universe" / "members.csv").exists() and (tmp_path / "edgar" / "us_close.csv").exists()


def test_changes_table_with_header_rows_inside_the_body_is_found():
    t = _tables()
    body = pd.DataFrame([["Effective Date", "Added", "Added", "Removed", "Removed", "Reason"],
                         ["Effective Date", "Ticker", "Security", "Ticker", "Security", "Reason"],
                         ["March 18, 2024", "T1", "Co 1", "OLD1", "Old 1", "x"]])
    current, changes = EG.parse_wiki([t[0], body])
    assert changes.loc[0, "added"] == "T1" and changes.loc[0, "removed"] == "OLD1" and changes.loc[0, "date"] == pd.Timestamp("2024-03-18")
    import pytest
    with pytest.raises(ValueError, match="表の形"):
        EG.parse_wiki([t[0], pd.DataFrame({"a": [1]})])
