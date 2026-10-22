"""EDINET の取得（cfdbot/edinet.py、./cfd edinet）の試験。作ったデータだけを使い、外にはつながない。"""

from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from cfdbot import edinet as ED

ROOT = Path(__file__).resolve().parents[1]
TODAY = pd.Timestamp("2026-10-16")
HEAD = ["要素ID", "項目名", "コンテキストID", "相対年度", "連結・個別", "期間・時点", "ユニットID", "単位", "値"]


def csv_zip(rows: list[tuple[str, str]]) -> bytes:
    lines = ["\t".join(f'"{h}"' for h in HEAD)]
    for el, val in rows:
        lines.append("\t".join(['"' + el + '"', '"x"', '"FilingDateInstant"', '"当期"', '"その他"', '"時点"', '""', '""', '"' + val + '"']))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("XBRL_TO_CSV/jpcrp030000-asr-001.csv", ("\n".join(lines) + "\n").encode("utf-16"))
    return buf.getvalue()


def xbrl_zip(body: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("XBRL/PublicDoc/jpcrp030000-asr-001.xbrl", f"<xbrli:xbrl>{body}</xbrli:xbrl>".encode("utf-8"))
        z.writestr("XBRL/AuditDoc/jpaud.xbrl", "<jpcrp_cor:BusinessRisksTextBlock>監査</jpcrp_cor:BusinessRisksTextBlock>")
    return buf.getvalue()


def test_sections_from_csv_and_xbrl_zips():
    blob = csv_zip([("jpcrp_cor:DescriptionOfBusinessTextBlock", "<p>当社は自動車を製造しています。</p><p>海外にも&amp;販売</p>"),
                    ("jpcrp_cor:BusinessRisksTextBlock", "<div>為替の変動</div>"),
                    ("jpcrp_cor:NetSales", "100")])
    s = ED.sections_from_csv_zip(blob)
    assert s["business"] == "当社は自動車を製造しています。\n海外にも&販売" and s["risks"] == "為替の変動" and "mdna" not in s
    x = ED.sections_from_xbrl_zip(xbrl_zip(
        '<jpcrp_cor:BusinessRisksTextBlock contextRef="FilingDateInstant">&lt;p&gt;原材料の値上がり&lt;/p&gt;</jpcrp_cor:BusinessRisksTextBlock>'
        '<jpcrp_cor:ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock>&lt;p&gt;増収&lt;/p&gt;'
        '</jpcrp_cor:ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock>'))
    assert x == {"risks": "原材料の値上がり", "mdna": "増収"}                              # 監査報告書の中は読まない
    assert ED.ticker_code("7203.T") == "72030" and ED.ticker_code("285A.T") == "285A0"


def test_older_mdna_element_name_is_read():
    old = "jpcrp_cor:AnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock"     # 2018-03 期より前の名前
    s = ED.sections_from_csv_zip(csv_zip([(old, "<p>売上が増えた</p>"), ("jpcrp_cor:NetSalesTextBlock", "<p>違う</p>")]))
    assert s == {"mdna": "売上が増えた"}
    x = ED.sections_from_xbrl_zip(xbrl_zip(f"<{old}>&lt;p&gt;利益が増えた&lt;/p&gt;</{old}>"))
    assert x == {"mdna": "利益が増えた"}
    assert ED.section_key("DescriptionOfBusinessTextBlock") == "business" and ED.section_key("jpcrp_cor:Foo") is None


class FakeEdinet:
    """一覧（日付ごと）と書類（docID・種類）を返す作り物。2016-08-01 より前は一覧が空（10 年の外）。"""

    def __init__(self, first: str = "2016-08-01"):
        self.first, self.calls = pd.Timestamp(first), []

    def __call__(self, path: str, params: dict):
        self.calls.append((path, dict(params)))
        assert "Subscription-Key" not in params                                     # キーは http_getter が足す（ここには来ない）
        if path == "documents.json":
            d = pd.Timestamp(params["date"])
            if d < self.first:
                return 200, json.dumps({"metadata": {"status": "200"}, "results": []}).encode()
            res = []
            if d.month == 6 and d.day >= 24:
                res = [{"docID": f"S{d:%Y%m%d}A", "secCode": "72030", "filerName": "トヨタ自動車株式会社", "docTypeCode": "120",
                        "submitDateTime": f"{d:%Y-%m-%d} 15:00", "periodStart": f"{d.year - 1}-04-01", "periodEnd": f"{d.year}-03-31",
                        "csvFlag": "1", "xbrlFlag": "1", "withdrawalStatus": "0"},
                       {"docID": f"S{d:%Y%m%d}B", "secCode": "99990", "docTypeCode": "120", "submitDateTime": f"{d:%Y-%m-%d} 15:00"},
                       {"docID": f"S{d:%Y%m%d}C", "secCode": "72030", "docTypeCode": "130", "submitDateTime": f"{d:%Y-%m-%d} 16:00"}]
            else:
                res = [{"docID": f"X{d:%Y%m%d}", "secCode": "11110", "docTypeCode": "350"}]
            return 200, json.dumps({"metadata": {"status": "200"}, "results": res}).encode()
        if params.get("type") == 5:
            return 200, csv_zip([("jpcrp_cor:DescriptionOfBusinessTextBlock", "<p>自動車の製造と販売</p>" * 20),
                                 ("jpcrp_cor:BusinessRisksTextBlock", "<p>為替</p>" * 30)])
        return 404, b""


def test_client_waits_between_calls_and_after_refusals():
    t = [0.0]
    slept = []

    def clock():
        return t[0]

    def sleep(s):
        slept.append(s)
        t[0] += s

    answers = [(429, b""), (200, b'{"results": []}'), (200, b'{"results": []}')]
    cl = ED.Client(lambda p, q: answers.pop(0), sleep=sleep, clock=clock, log=lambda s: None)
    cl.documents(TODAY)
    cl.documents(TODAY)
    assert slept == [600.0, 2.0]                                                     # 断られたら 10 分待つ。一覧は 2 秒おき
    assert cl.calls == 3
    with pytest.raises(ED.Stop):
        ED.Client(lambda p, q: (403, b""), sleep=sleep, clock=clock, log=lambda s: None).documents(TODAY)   # キーが通らなければ止める
    with pytest.raises(ED.Stop):
        ED.Client(lambda p, q: (429, b""), sleep=sleep, clock=clock, log=lambda s: None, waits=(1.0, 1.0)).documents(TODAY)
    said = []
    with pytest.raises(ED.Stop):                                                     # つながらないときは、理由も記録に出す
        ED.Client(lambda p, q: (-1, b"[Errno 8] nodename nor servname provided"), sleep=sleep, clock=clock,
                  log=said.append, waits=(1.0,)).documents(TODAY)
    assert "つながらない: [Errno 8]" in said[0]


def test_fetch_lists_and_docs_resume_and_keep_only_first_reports(tmp_path):
    fake = FakeEdinet()
    cl = ED.Client(fake, sleep=lambda s: None, log=lambda s: None)
    store = ED.Store(tmp_path / "edinet")
    days = list(pd.bdate_range("2025-06-23", "2025-07-02"))
    n = ED.fetch_lists(cl, store, days, {"72030": "7203.T"}, TODAY)
    cand = store.candidates()
    assert n == len(days) and cand["docID"].tolist() == [f"S{d:%Y%m%d}A" for d in days if d.day >= 24 and d.month == 6]
    assert set(cand["docTypeCode"]) == {"120"}                                     # 訂正（130）とほかの会社は入れない
    got = ED.fetch_docs(cl, store, TODAY)
    assert got["docs"] == len(cand) and got["missing"] == 0
    idx = store.index()
    doc = store.load_doc("7203.T", idx["docID"].iloc[0])
    assert doc["business"].startswith("自動車の製造と販売") and idx["source"].eq("csv").all()
    calls = len(fake.calls)
    assert ED.fetch_lists(cl, store, days, {"72030": "7203.T"}, TODAY) == 0             # 見た日・取った有報は二度取らない
    assert ED.fetch_docs(cl, store, TODAY)["docs"] == 0 and len(fake.calls) == calls
    assert ED.fetch_days(TODAY)[0] == pd.Timestamp("2016-10-24") and ED.fetch_days(TODAY)[-1] == TODAY


def test_check_finds_the_oldest_day_and_reports_sections(tmp_path):
    mod_spec = importlib.util.spec_from_file_location("edinet_script", ROOT / "scripts" / "edinet.py")
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    data = tmp_path / "stocks"
    data.mkdir()
    pd.DataFrame({"ticker": ["7203.T"], "name": ["トヨタ自動車"], "sector": ["輸送用機器"]}).to_csv(data / "universe_jp.csv", index=False)
    fake = FakeEdinet(first="2016-09-15")
    path = mod.main(["check", "--data", str(data), "--out", str(tmp_path / "out"), "--today", f"{TODAY:%Y-%m-%d}"],
                    getter=fake, sleep=lambda s: None)
    text = path.read_text(encoding="utf-8")
    assert "2016-09-15" in text and "事業の内容" in text
    res = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert [s["year"] for s in res["samples"]] == [2017, 2020, 2024] and res["samples"][0]["chars_business"] > 100
    assert len(fake.calls) < 60                                                         # 呼び出しは少なく
