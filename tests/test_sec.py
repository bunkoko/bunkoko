"""SEC EDGAR の取得（cfdbot/sec.py、docs/us_text.md）の試験。作ったデータだけを使い、外にはつながない。"""

from __future__ import annotations

import gzip
import io
import json
from urllib.error import HTTPError, URLError

import pandas as pd
import pytest

from cfdbot import sec as S

NOW = pd.Timestamp("2026-10-21 10:00")
UA = "Taro Testname taro.testname@example.invalid"           # 作り物の名乗り（記録・例外に出てはいけない）
FILLER = "Our business depends on many things that may change over time. " * 8


def fake_clock():
    t, slept = [0.0], []

    def clock():
        return t[0]

    def sleep(s):
        slept.append(s)
        t[0] += s
    return clock, sleep, slept


def tenk_html(variant: str = "plain") -> str:
    """目次（短い行）と本文のある、作り物の 10-K。"""
    toc = "".join(f"<tr><td>Item {n}.</td><td>{t}</td><td>{p}</td></tr>" for n, t, p in [
        ("1", "Business", 3), ("1A", "Risk Factors", 10), ("1B", "Unresolved Staff Comments", 20),
        *([("1C", "Cybersecurity", 21)] if variant == "1c" else []), ("2", "Properties", 22),
        ("7", "Management&#8217;s Discussion and Analysis", 30), ("7A", "Quantitative and Qualitative Disclosures", 40),
        ("8", "Financial Statements", 41)])
    if variant == "split":
        head_1a = "<p><b>ITEM&#160;1A.</b></p><p><b>RISK FACTORS</b></p>"
        head_1b = "<table><tr><td>ITEM&#160;1B.</td><td>UNRESOLVED STAFF COMMENTS</td></tr></table>"
    else:
        head_1a = "<p><span>Item 1A.</span> <span>Risk Factors</span></p>"
        head_1b = "<p>Item 1B. Unresolved Staff Comments</p>"
    risks = "".join(f"<p>Risk number {i}: demand for our products could fall &amp; costs could rise. {FILLER}</p>"
                    for i in range(4))
    cyber = f"<p>Item 1C. Cybersecurity</p><p>We run a security program. {FILLER}</p>" if variant == "1c" else ""
    return ("<html><head><title>10-K</title><style>p {color: red}</style></head><body>"
            "<div style='display:none'><ix:header><ix:hidden>HIDDENFACT</ix:hidden></ix:header></div>"
            "<script>var x = 'Item 1A. Risk Factors';</script>"
            f"<p>TABLE OF CONTENTS</p><table>{toc}</table>"
            f"<p>Item 1. Business</p><p>We make widgets. For more, see Item 1A. Risk Factors below. {FILLER}</p>"
            f"{head_1a}{risks}{head_1b}<p>None.</p>{cyber}"
            f"<p>Item 2. Properties</p><p>We own a building. {FILLER}</p>"
            f"<p>Item 7. Management&#8217;s Discussion and Analysis of Financial Condition</p>"
            f"<p>Sales grew by 0%. {FILLER}</p><p>Item 7A. Quantitative and Qualitative Disclosures</p><p>Rates.</p>"
            "<p>Item 8. Financial Statements</p><p>See below.</p></body></html>")


def test_user_agent_file(tmp_path):
    p = S.save_user_agent(f"  {UA} ", tmp_path / "x" / "sec.json")
    assert S.load_user_agent(p) == UA and json.loads(p.read_text()) == {"user_agent": UA}
    assert p.stat().st_mode & 0o777 == 0o600
    assert S.load_user_agent(tmp_path / "none.json") is None


def test_client_interval_and_waits():
    clock, sleep, slept = fake_clock()
    answers = [(429, b""), (200, b"a"), (200, b"b")]
    cl = S.Client(lambda u: answers.pop(0), sleep=sleep, clock=clock, log=lambda s: None)
    assert cl.get("u1") == (200, b"a") and cl.get("u2") == (200, b"b")
    assert slept == [600.0, 1.0] and cl.calls == 3                       # 断られたら 10 分待つ。1 秒に 1 件
    slept.clear()
    cl = S.Client(lambda u: (404, b"nf"), sleep=sleep, clock=clock, log=lambda s: None)
    assert cl.get("u") == (404, b"nf") and slept == []                   # 無い書類は待たない
    for status, word in [(403, "名乗る"), (429, "429"), (503, "503"), (-1, "つながらない")]:
        said = []
        with pytest.raises(S.Stop) as e:
            S.Client(lambda u: (status, b"x"), sleep=sleep, clock=clock, log=said.append, waits=(1.0, 2.0)).get("u")
        assert word in str(e.value) and len(said) == 2
    with pytest.raises(S.Stop) as e:
        S.Client(lambda u: (403, b""), sleep=sleep, clock=clock, log=lambda s: None, waits=()).get("u")
    assert "速さ" in str(e.value)


class FakeResponse:
    def __init__(self, status, body, headers):
        self.status, self._body, self.headers = status, body, headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_getter_sends_ua_but_never_logs_or_raises_it(monkeypatch):
    sent, plan = [], []

    def fake_urlopen(req, timeout):
        sent.append(dict(req.header_items()))
        kind = plan.pop(0)
        if kind == "gzip":
            return FakeResponse(200, gzip.compress(b'{"ok": 1}'), {"Content-Encoding": "gzip"})
        if kind == "403":
            raise HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"Undeclared Automated Tool"))
        raise URLError(f"refused for {req.get_header('User-agent')} at {req.full_url}")

    monkeypatch.setattr(S, "urlopen", fake_urlopen)
    get = S.http_getter(UA)
    plan[:] = ["gzip", "403", "url"]
    assert get("https://data.sec.gov/x.json") == (200, b'{"ok": 1}')                # gzip を戻す
    assert get("https://data.sec.gov/x.json") == (403, b"Undeclared Automated Tool")
    st, body = get("https://data.sec.gov/secret-path.json")
    assert st == -1 and UA.encode() not in body
    assert sent[0]["User-agent"] == UA and "gzip" in sent[0]["Accept-encoding"]
    said = []
    plan[:] = ["url"] * 3 + ["403"] * 3
    for _ in range(2):
        with pytest.raises(S.Stop) as e:
            S.Client(get, sleep=lambda s: None, log=said.append, waits=(1.0, 1.0)).get("https://data.sec.gov/y.json")
        assert UA not in str(e.value) and "taro" not in str(e.value).lower()
    assert said and all(UA not in s and "taro" not in s.lower() for s in said)


def test_submissions_merges_older_pages():
    recent = {"accessionNumber": ["0000000123-25-000002", "0000000123-24-000001"], "form": ["10-K", "10-Q"],
              "filingDate": ["2025-02-20", "2024-11-01"], "reportDate": ["2024-12-31", "2024-09-30"],
              "acceptanceDateTime": ["2025-02-19T18:08:27.000Z", "2024-11-01T09:00:00.000Z"],
              "primaryDocument": ["a10k.htm", "q.htm"]}
    old = {"accessionNumber": ["0000000123-20-000009", "0000000123-19-000003"], "form": ["10-K", "10-K"],
           "filingDate": ["2020-02-20", "2019-02-21"], "reportDate": ["2019-12-31", "2018-12-31"],
           "primaryDocument": ["old.htm", "older.htm"]}                                 # acceptanceDateTime が無いページ
    pages = {S.SUBMISSIONS.format(cik=123): {"cik": "0000000123", "name": "Widget Co",
                                             "filings": {"recent": recent, "files": [{"name": "CIK0000000123-submissions-001.json"}]}},
             "https://data.sec.gov/submissions/CIK0000000123-submissions-001.json": old}
    seen = []

    def getter(url):
        seen.append(url)
        return (200, json.dumps(pages[url]).encode()) if url in pages else (404, b"")

    sub = S.submissions(S.Client(getter, sleep=lambda s: None, log=lambda s: None), 123)
    allf = sub["filings_all"]
    assert len(seen) == 2 and allf["accessionNumber"][-1] == "0000000123-19-000003"
    assert all(len(v) == 4 for v in allf.values()) and allf["acceptanceDateTime"][2:] == [None, None]
    assert S.submissions(S.Client(getter, sleep=lambda s: None, log=lambda s: None), 999) == {}
    df = S.tenk_filings(sub, 123)
    assert df["accession"].tolist() == ["0000000123-20-000009", "0000000123-25-000002"]   # 期末 2018 は外す・10-Q は外す
    assert df["acceptance"].iloc[1] == pd.Timestamp("2025-02-19 18:08:27") and pd.isna(df["acceptance"].iloc[0])


def test_tenk_filings_forms_since_and_first_filed():
    rows = [("A1", "10-K", "2021-12-31", "2022-02-10T16:00:00.000Z"), ("A2", "10-K/A", "2021-12-31", "2022-03-01T10:00:00.000Z"),
            ("A3", "10-KT", "2022-06-30", "2022-09-01T10:00:00.000Z"), ("A4", "10-K405", "2020-12-31", "2021-02-01T10:00:00.000Z"),
            ("A5", "10-K405/A", "2020-12-31", "2021-03-01T10:00:00.000Z"), ("A6", "10-K", "2018-12-31", "2019-02-01T10:00:00.000Z"),
            ("A7", "10-K", "2022-12-31", "2023-02-11T09:00:00.000Z"), ("A8", "10-K", "2022-12-31", "2023-02-10T09:00:00.000Z"),
            ("A9", "8-K", "2022-12-31", "2023-01-05T09:00:00.000Z")]
    recent = {"accessionNumber": [r[0] for r in rows], "form": [r[1] for r in rows], "reportDate": [r[2] for r in rows],
              "acceptanceDateTime": [r[3] for r in rows], "filingDate": [r[3][:10] for r in rows],
              "primaryDocument": [f"{r[0]}.htm" for r in rows]}
    df = S.tenk_filings({"filings": {"recent": recent}}, 42)
    assert df.columns.tolist() == S.COLUMNS
    assert df["accession"].tolist() == ["A4", "A1", "A8"]                         # 同じ期末は先に受け付けた方
    assert S.tenk_filings({"filings": {"recent": recent}}, 42, since="2022-01-01")["accession"].tolist() == ["A8"]
    assert S.tenk_filings({}, 42).empty
    assert S.doc_url(42, "0000000042-23-000001", "x10k.htm") == \
        "https://www.sec.gov/Archives/edgar/data/42/000000004223000001/x10k.htm"


def test_html_to_text_and_extract_risks():
    t = S.html_to_text(tenk_html())
    assert "HIDDENFACT" not in t and "var x" not in t and "color" not in t and "<" not in t
    assert "Item 1A. Risk Factors" in t and "costs could rise" in t and "&amp;" not in t and "\xa0" not in t
    r = S.extract_section(t, "risks")
    assert r.startswith("Item 1A. Risk Factors\nRisk number 0") and r.endswith(FILLER.strip())
    assert "Unresolved" not in r and "Business" not in r and r.count("Risk number") == 4
    m = S.extract_section(t, "mdna")
    assert m.startswith("Item 7. Management’s Discussion") and "Sales grew" in m and "Rates" not in m
    r1c = S.extract_section(S.html_to_text(tenk_html("1c")), "risks")
    assert r1c.count("Risk number") == 4 and "security program" not in r1c and "Cybersecurity" not in r1c
    rs = S.extract_section(S.html_to_text(tenk_html("split")), "risks")
    assert rs.startswith("ITEM 1A.\nRISK FACTORS") and rs.count("Risk number") == 4 and "UNRESOLVED" not in rs
    for head in ["ITEM 1A: RISK FACTORS", "Item 1A — Risk Factors", "Item\xa01A.\xa0Risk\xa0Factors", "Item 1A–Risk Factors"]:
        txt = f"Item 1A. Risk Factors 10\nItem 1B. Unresolved 20\nItem 2. Properties 21\n{head}\n{FILLER}\nItem 2. Properties\nOwn."
        assert S.extract_section(txt, "risks").startswith(head), head
    assert S.extract_section("Item 1A. Risk Factors\nshort.\nItem 2. Properties", "risks") == ""   # 200 字未満
    assert S.extract_section("no headings here", "mdna") == ""
    curly = f"ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS\n{FILLER}\nITEM 8. FINANCIAL STATEMENTS"
    assert S.extract_section(curly, "mdna").startswith("ITEM 7. MANAGEMENT'S")


def test_fetch_docs_resumes_and_never_stores_html(tmp_path):
    filings = pd.DataFrame({"cik": [7, 7, 8], "accession": ["0000000007-23-000001", "0000000007-24-000001", "0000000008-24-000001"],
                            "form": ["10-K"] * 3, "filing_date": pd.to_datetime(["2023-02-10", "2024-02-10", "2024-03-01"]),
                            "report_date": pd.to_datetime(["2022-12-31", "2023-12-31", "2023-12-31"]),
                            "acceptance": pd.to_datetime(["2023-02-09 18:00", "2024-02-09 16:00", "2024-03-01 08:00"]),
                            "primary": ["a.htm", "b.htm", "c.htm"]})
    bodies = {S.doc_url(7, "0000000007-23-000001", "a.htm"): tenk_html().encode("utf-8"),
              S.doc_url(7, "0000000007-24-000001", "b.htm"): b"<html><body><p>No sections here.</p></body></html>"}
    calls = []

    def getter(url):
        calls.append(url)
        return (200, bodies[url]) if url in bodies else (404, b"<html>gone</html>")

    store = S.Store(tmp_path / "sec")
    cl = S.Client(getter, sleep=lambda s: None, log=lambda s: None)
    got = S.fetch_docs(cl, store, filings.head(2), NOW, max_docs=1)
    assert got == {"docs": 1, "empty": 0, "missing": 0, "left": 1}
    got = S.fetch_docs(cl, store, filings, NOW)
    assert got == {"docs": 0, "empty": 1, "missing": 1, "left": 0} and len(calls) == 3   # 取った分は二度取らない
    assert S.fetch_docs(cl, store, filings, NOW)["docs"] == 0 and len(calls) == 3
    idx = store.index()
    assert idx.columns.tolist() == S.INDEX_COLUMNS and idx["status"].tolist() == ["ok", "empty", "missing"]
    assert idx["acceptance"].iloc[0] == "2023-02-09 18:00:00" and int(idx["chars_risks"].iloc[0]) > 400
    doc = store.load_doc(7, "0000000007-23-000001")
    assert doc["risks"].startswith("Item 1A. Risk Factors") and doc["mdna"].startswith("Item 7.")
    for p in (tmp_path / "sec").rglob("*"):                                          # 元の HTML・名乗りは残さない
        if p.is_file():
            raw = gzip.decompress(p.read_bytes()) if p.suffix == ".gz" else p.read_bytes()
            assert b"<html" not in raw and b"<p>" not in raw and b"HIDDENFACT" not in raw and UA.encode() not in raw
    assert not store.load_doc(8, "0000000008-24-000001")
    rows = S.check_rows(store, filings.assign(year=[2023, 2024, 2024]))
    assert rows[0]["head"].startswith("Item 1A. Risk Factors") and rows[0]["chars"] == len(doc["risks"])
    assert rows[0]["tail"] == doc["risks"][-300:] and rows[2]["chars"] == 0 and rows[2]["status"] == "missing"
    store.save_sub(7, {"name": "Widget"})
    assert store.load_sub(7) == {"name": "Widget"} and store.load_sub(9) == {}
    assert store.lock() and not store.lock()
    store.unlock()
    assert store.lock()
    store.unlock()


def test_sample_for_check_is_stratified_and_deterministic():
    n = 400
    years = [2019 + i % 8 for i in range(n)]                                         # 2019〜2026 年
    filings = pd.DataFrame({"cik": range(n), "accession": [f"{i * 7919 % 100003:010d}-{y % 100:02d}-000001" for i, y in enumerate(years)],
                            "filing_date": pd.to_datetime([f"{y}-03-01" for y in years]), "form": "10-K"})
    a = S.sample_for_check(filings, S.CHECK_PER_YEAR)
    b = S.sample_for_check(filings.sample(frac=1.0, random_state=3), S.CHECK_PER_YEAR)   # 並びが違っても同じくじ
    assert a["accession"].tolist() == b["accession"].tolist() and len(a) == 60
    assert a.groupby("year").size().to_dict() == {2020: 6, **{y: 9 for y in range(2021, 2027)}}
    assert (a["filing_date"].dt.year == a["year"]).all() and a["accession"].is_unique
    assert S.sample_for_check(filings, S.CHECK_PER_YEAR, seed=1)["accession"].tolist() != a["accession"].tolist()
    few = S.sample_for_check(filings.head(10), {2019: 5, 2030: 3})
    assert len(few) == 2 and set(few["year"]) == {2019}                             # 足りない年は、あるだけ
    assert sum(S.CHECK_PER_YEAR.values()) == 60


def test_source_newlines_do_not_make_cross_references_into_headings():
    html = ("<html><body><p>Table of Contents</p><p>Item 1. Business</p><p>We sell things. For risks, see\n"
            "Item 1A. Risk Factors of this report. " + "More business text. " * 30 + "</p>"
            "<p>Item 1A. Risk Factors</p><p>" + "Our risks are many. " * 60 + "</p><p>Item 1B. Unresolved Staff Comments</p><p>None.</p>"
            "<p>Item 2. Properties</p></body></html>")
    risks = S.extract_section(S.html_to_text(html), "risks")
    assert risks.startswith("Item 1A. Risk Factors") and "Our risks are many." in risks and "business text" not in risks


def test_duplicate_accessions_are_fetched_once(tmp_path):
    calls = []

    def getter(url):
        calls.append(url)
        return 200, b"<p>Item 1A. Risk Factors</p><p>" + b"Risk text. " * 40 + b"</p><p>Item 2. Properties</p>"

    cl = S.Client(getter, sleep=lambda s: None, clock=lambda: 0.0, log=lambda s: None, interval=0.0)
    store = S.Store(tmp_path / "sec")
    f = pd.DataFrame({"cik": [1, 1], "accession": ["0001-26-000001", "0001-26-000001"], "form": ["10-K"] * 2,
                      "filing_date": ["2026-02-01"] * 2, "report_date": ["2025-12-31"] * 2,
                      "acceptance": [pd.Timestamp("2026-02-01 16:00")] * 2, "primary": ["a.htm"] * 2})
    S.fetch_docs(cl, store, f, pd.Timestamp("2026-10-21"), log=lambda s: None)
    assert len(calls) == 1 and len(store.index()) == 1
    assert len(S.sample_for_check(f.assign(year=2026), {2026: 2})) == 1
