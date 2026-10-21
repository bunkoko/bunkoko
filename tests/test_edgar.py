"""案 2-US（docs/us_text.md）の点検 1 の試験。作ったデータだけを使う。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdbot import edgar as EG

ROOT = Path(__file__).resolve().parents[1]


def _list(n=500, old=False, drop=0, extra=()):
    tick = [f"T{i}" for i in range(drop, n - 1)] + ["BRK.B"] + list(extra)
    df = pd.DataFrame({"Ticker symbol" if old else "Symbol": tick, "Company" if old else "Security": [f"Co {t}" for t in tick],
                       "GICS Sector": ["Energy", "Utilities"] * (len(tick) // 2) + ["Energy"] * (len(tick) % 2)})
    if not old:
        df["CIK"] = range(1000, 1000 + len(tick))
    return df


def _api(lists):
    """月 → 一覧の表。query は版の番号を、parse はその版の HTML を返す作り物。"""
    calls = []

    def api(params):
        calls.append(params)
        if params["action"] == "query":
            m = pd.Timestamp(params["rvstart"][:10]) - pd.Timedelta(days=1)
            return {"query": {"pages": [{"revisions": [{"revid": int(f"{m:%Y%m}"), "timestamp": params["rvstart"]}]}]}}
        m = pd.Timestamp(f"{str(params['oldid'])[:4]}-{str(params['oldid'])[4:6]}-01") + pd.offsets.MonthEnd(0)
        nav = pd.DataFrame({"vteS&P 500 companies": ["x"]})
        return {"parse": {"text": lists(m).to_html(index=False) + nav.to_html(index=False)}}
    api.calls = calls
    return api


def test_constituents_are_read_from_new_and_old_page_versions():
    new = EG.parse_constituents([_list(), pd.DataFrame({"a": [1]})])
    assert len(new) == 500 and "BRK-B" in set(new["ticker"]) and new["cik"].notna().all()
    old = EG.parse_constituents([_list(old=True)])
    assert len(old) == 500 and old["cik"].isna().all() and set(old["sector"]) == {"Energy", "Utilities"}
    with pytest.raises(ValueError, match="表の形"):
        EG.parse_constituents([pd.DataFrame({"vteS&P 500 companies": ["x"] * 11})])
    mem, info = EG.membership_from_snapshots({pd.Timestamp("2015-01-31"): old, pd.Timestamp("2015-02-28"): new})
    assert len(mem[pd.Timestamp("2015-01-31")]) == 500 and set(info["month"]) == {pd.Timestamp("2015-01-31"), pd.Timestamp("2015-02-28")}


def test_coverage_gate_counts_missing_member_months():
    ends = EG.month_ends("2014-12-31", "2016-12-31")
    mem = {m: frozenset(_list()["Symbol"].map(EG.yahoo_symbol)) for m in ends}
    idx = pd.bdate_range("2014-12-01", "2016-12-30")
    tick = sorted({t for ts in mem.values() for t in ts})
    close = pd.DataFrame(100.0, index=idx, columns=tick)
    cov = EG.coverage(mem, EG.monthly_last(close))
    assert cov["ok"] and cov["missing"] == 0 and len(cov["bad_months"]) == 0
    close[tick[:60]] = np.nan                                                          # 60 社（12%）の値段が無い
    cov2 = EG.coverage(mem, EG.monthly_last(close))
    assert not cov2["ok_price"] and cov2["ok_count"] and 0.11 < cov2["missing"] < 0.13
    assert EG.missing_names(mem, EG.monthly_last(close)).iloc[0] == len(ends)          # 値段の無い会社は全部の月で数える
    small = {m: frozenset(list(ts)[:450]) for m, ts in mem.items()}                     # 数が足りない月ばかり
    assert not EG.coverage(small, EG.monthly_last(close.fillna(1.0)))["ok_count"]
    ml = EG.monthly_last(pd.DataFrame({"A": [1.0, 2.0, np.nan]}, index=pd.to_datetime(["2015-01-29", "2015-01-30", "2015-02-02"])))
    assert ml.index.tolist() == [pd.Timestamp("2015-01-31"), pd.Timestamp("2015-02-28")] and ml["A"].iloc[0] == 2.0


def test_universe_command_reads_monthly_versions_reuses_them_and_reports(tmp_path):
    spec = importlib.util.spec_from_file_location("edgar_cmd", ROOT / "scripts" / "edgar.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # 2016-01 から OLD1 が外れて NEW1 が入る
    lists = lambda m: _list(old=m < pd.Timestamp("2015-06-30"), extra=("OLD1",) if m < pd.Timestamp("2016-01-31") else ("NEW1",), drop=1)  # noqa: E731
    api = _api(lists)

    def prices(tickers, since):
        idx = pd.bdate_range(since, "2016-12-30")
        return pd.DataFrame(100.0, index=idx, columns=[t for t in tickers if t != "OLD1"])

    args = ["universe", "--root", str(tmp_path / "edgar"), "--out", str(tmp_path / "out"), "--today", "2017-01-15"]
    path = mod.main(args, api=api, prices=prices)
    text = path.read_text(encoding="utf-8")
    assert "## 1. 判定" in text and "点検 1: 通った" in text and "OLD1" in text and "古い版 25 個" in text
    info = pd.read_csv(tmp_path / "edgar" / "universe" / "members.csv")
    assert {"OLD1", "NEW1"} <= set(info["ticker"]) and len(api.calls) == 50                # 25 か月 × 2 回
    mod.main(args, api=api, prices=prices)
    assert len(api.calls) == 50                                                           # 2 回目は取った版を使い回す
