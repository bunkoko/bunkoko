"""J-Quants の確認（docs/survivorship.md の段階 1）の試験。ネットにはつながず、作り物の応答だけを使う。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd

from cfdbot import jquants as J

ROOT = Path(__file__).resolve().parents[1]
TODAY = pd.Timestamp("2026-10-15")


def _fake(gone_listed_in_bars=True, revision=True, pages=True):
    """2 年前の一覧には 6 社、新しい一覧には 4 社（TOPIX500 の 1003 と小型の 1009 が消えた）。"""
    d = J.check_dates(TODAY)
    old = f"{d['old']:%Y-%m-%d}"
    master_old = [{"Date": old, "Code": c, "CoName": f"会社{c}", "ScaleCat": s, "Mkt": "0111", "MktNm": "プライム"}
                  for c, s in (("1001", "TOPIX Core30"), ("1002", "TOPIX Large70"), ("1003", "TOPIX Mid400"), ("1004", "TOPIX Mid400"),
                               ("1005", "TOPIX Small 1"), ("1009", "-"))]
    master_new = [r for r in master_old if r["Code"] not in ("1003", "1009")]
    calls = []

    def get(path, params):
        calls.append((path, dict(params)))
        if path == "/equities/master":
            rows = master_old if params["date"] == old else master_new
            if pages and params["date"] == old and "pagination_key" not in params:
                return 200, json.dumps({"data": rows[:3], "pagination_key": "p2"}).encode()
            if pages and params["date"] == old:
                return 200, json.dumps({"data": rows[3:]}).encode()
            return 200, json.dumps({"data": rows}).encode()
        if path == "/equities/bars/daily" and "code" not in params:
            codes = [r["Code"] for r in master_old if gone_listed_in_bars or r["Code"] not in ("1003", "1009")]
            return 200, json.dumps({"data": [{"Date": old, "Code": c, "O": 1.0, "C": 1.0, "AdjC": 1.0, "AdjFactor": 1.0} for c in codes]}).encode()
        if path == "/equities/bars/daily":
            return (200, json.dumps({"data": [{"Date": old, "Code": params["code"], "C": 1.0}] * 5}).encode()) if gone_listed_in_bars \
                else (210, json.dumps({"message": "no data"}).encode())
        if path == "/fins/summary":
            kinds = ["2QFinancialStatements_Consolidated_JP"] * 3 + (["EarnForecastRevision"] if revision else [])
            return 200, json.dumps({"data": [{"DiscDate": params["date"], "DiscTime": "15:00:00", "DocType": k, "FNP": "1", "FSales": "2",
                                               "FOP": "3"} for k in kinds]}).encode()
        return 400, b'{"message": "bad"}'
    return get, calls


def test_check_dates_stay_inside_the_free_window():
    d = J.check_dates(TODAY)
    assert d["old"] >= TODAY - pd.DateOffset(years=2) and d["new"] <= TODAY - pd.Timedelta(weeks=12)
    assert d["old"].weekday() < 5 and d["new"].weekday() < 5 and 1 <= len(d["fins"]) <= 3
    assert all(d["old"] <= t <= d["new"] for t in d["fins"])


def test_records_finds_the_container_and_pick_ignores_case():
    assert J.records({"data": [{"a": 1}]}) == ([{"a": 1}], "data")
    assert J.records({"info": [{"a": 1}], "pagination_key": "x"}) == ([{"a": 1}], "info")      # 名前が違っても報告できる
    assert J.records({"message": "x"}) == ([], None) and J.records([{"a": 2}])[1] == "(配列そのもの)"
    assert J.pick(["code", "ScaleCat"], "Code") == "code" and J.pick(["x"], "Code") is None


def test_client_waits_between_calls_retries_429_once_and_stops_on_the_second():
    slept = []
    seq = iter([(429, b""), (200, b'{"data": [{"a": 1}]}')])
    c = J.Client(lambda p, q: next(seq), interval=13.0, sleep=slept.append, log=lambda s: None)
    r = c.get("/x", {})
    assert r.ok and len(r.rows) == 1 and slept == [120.0, 13.0]                         # 最初の呼び出しの前は待たない。429 の後は 2 分
    c2 = J.Client(lambda p, q: (429, b""), sleep=lambda s: None, log=lambda s: None)
    try:
        c2.get("/x", {})
        raise AssertionError("止まらなかった")
    except J.Stop as e:
        assert "429" in str(e)
    r403 = J.Client(lambda p, q: (403, b'{"message": "not in your plan"}'), sleep=lambda s: None, log=lambda s: None).get("/x", {})
    assert r403.status == 403 and "not in your plan" in r403.message


def test_check_finds_gone_companies_their_prices_and_revision_documents(tmp_path):
    get, calls = _fake()
    res = J.run_check(J.Client(get, sleep=lambda s: None, log=lambda s: None), TODAY, log=lambda s: None)
    assert res["n_old"] == 6 and res["n_new"] == 4 and res["n_gone"] == 2 and res["gone_t500"] == ["1003"]
    assert res["n_t500_old"] == 4 and res["gone_with_bars_by_date"] == 2 and res["bars_adj"] == {"AdjC": True, "AdjFactor": True}
    assert res["gone_by_code"]["1003"] == (200, 5)
    assert any("EarnForecastRevision" in f["doc_types"] for f in res["fins"]) and res["fins"][0]["forecast"]["FNP"]
    assert ("/equities/master", {"date": f"{J.check_dates(TODAY)['old']:%Y-%m-%d}", "pagination_key": "p2"}) in calls   # 続きを取る
    text = "\n".join(J.report(res, TODAY))
    assert "**入る**" in text and "TOPIX500 だった会社 1 社" in text and "EarnForecastRevision" in text and "AdjC）あり" in text


def test_check_reports_when_gone_companies_or_revisions_are_missing():
    get, _ = _fake(gone_listed_in_bars=False, revision=False, pages=False)
    res = J.run_check(J.Client(get, sleep=lambda s: None, log=lambda s: None), TODAY, log=lambda s: None)
    text = "\n".join(J.report(res, TODAY))
    assert res["gone_with_bars_by_date"] == 0 and res["gone_by_code"]["1003"][0] == 210
    assert "見つからなかった（E1 は J-Quants では作り直せない見込み" in text
    bad = J.run_check(J.Client(lambda p, q: (403, b'{"message": "invalid key"}'), sleep=lambda s: None, log=lambda s: None), TODAY,
                      log=lambda s: None)
    assert "上場銘柄一覧が取れない（403" in bad["stopped"] and "止めた" in "\n".join(J.report(bad, TODAY))


def test_command_writes_report_without_network(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("jquants_script", ROOT / "scripts" / "jquants.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    get, _ = _fake()
    out = mod.main(["check", "--today", "2026-10-15", "--out", str(tmp_path)], getter=get, sleep=lambda s: None)
    assert (out / "check.md").exists() and json.loads((out / "check.json").read_text(encoding="utf-8"))["n_gone"] == 2
    monkeypatch.setenv("JQUANTS_API_KEY", "")
    key_file = tmp_path / "k" / "jquants.json"
    J.save_key(" abc ", key_file)
    assert J.load_key(key_file) == "abc" and oct(key_file.stat().st_mode)[-3:] == "600"
