"""J-Quants の確認（docs/survivorship.md の段階 1）の試験。ネットにはつながず、作り物の応答だけを使う。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from cfdbot import jquants as J

ROOT = Path(__file__).resolve().parents[1]
TODAY = pd.Timestamp("2026-10-15")


@pytest.fixture(autouse=True)
def _lock_outside_home(tmp_path, monkeypatch):
    """同時に 1 本の印（~/.bunkoko/jquants.lock）を、試験では本物の場所に置かない。"""
    monkeypatch.setattr(J, "LOCK_FILE", tmp_path / "home" / "jquants.lock")


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
        if path == "/fins/summary" and "code" in params:                                   # その会社の開示（前の短信 → 今回の修正）
            day = J.check_dates(TODAY)["fins"][-1]
            rows = [{"Code": params["code"], "DiscDate": f"{day - pd.Timedelta(days=60):%Y-%m-%d}", "DiscTime": "15:00:00",
                     "DocType": "1QFinancialStatements_Consolidated_JP", "FNP": "100", "CurFYEn": "2026-03-31"},
                    {"Code": params["code"], "DiscDate": f"{day:%Y-%m-%d}", "DiscTime": "15:00:00", "DocType": "EarnForecastRevision",
                     "FNP": "120" if revision else "100", "CurFYEn": "2026-03-31"}]
            return 200, json.dumps({"data": rows}).encode()
        if path == "/fins/summary":
            kinds = ["2QFinancialStatements_Consolidated_JP"] * 3 + ["EarnForecastRevision"]
            return 200, json.dumps({"data": [{"Code": f"20{i:02d}", "DiscDate": params["date"], "DiscTime": "15:00:00", "DocType": k, "FNP": "1",
                                               "FSales": "2", "FOP": "3"} for i, k in enumerate(kinds)]}).encode()
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
    pr = res["e1_probe"]
    assert pr["non_fs"] == len(res["fins"]) and pr["checked"] >= 1 and pr["changed"] >= 1 and pr["fy_field"] == "CurFYEn"   # 操作で決める
    assert ("/equities/master", {"date": f"{J.check_dates(TODAY)['old']:%Y-%m-%d}", "pagination_key": "p2"}) in calls   # 続きを取る
    text = "\n".join(J.report(res, TODAY))
    assert "**入る**" in text and "TOPIX500 だった会社 1 社" in text and "EarnForecastRevision" in text and "AdjC）あり" in text
    assert "変わっていたもの **" in text and "機械では決めない" in text


def test_check_reports_when_gone_companies_or_revisions_are_missing():
    get, _ = _fake(gone_listed_in_bars=False, revision=False, pages=False)
    res = J.run_check(J.Client(get, sleep=lambda s: None, log=lambda s: None), TODAY, log=lambda s: None)
    text = "\n".join(J.report(res, TODAY))
    assert res["gone_with_bars_by_date"] == 0 and res["gone_by_code"]["1003"][0] == 210
    assert res["e1_probe"]["changed"] == 0 and "変わっていたもの **0 件**" in text                 # 予想が動かなければ 0 件と出す
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


# --------------------------------------------------------------------------- 取得（docs/survivorship.md 8.6）。作り物の応答だけ
HOLIDAYS = {"2025-01-01", "2025-01-02", "2025-01-03", "2025-01-13", "2025-02-11", "2025-02-24"}


def _fake_fetch(fail=None, refuse_before=None, wrong_master_date=False, page_bars=True, endless=False):
    """平日のうち HOLIDAYS は行なし。日足は 3 社（2 ページに分かれる）、財務は 1 行、一覧は 3 社。"""
    calls, fail = [], dict(fail or {})

    def get(path, params):
        calls.append((path, dict(params)))
        day = params["date"]
        if (path, day) in fail and fail[(path, day)] > 0:
            fail[(path, day)] -= 1
            return 500, b'{"message": "oops"}'
        if refuse_before and day < refuse_before:
            return 403, b'{"message": "out of plan"}'
        if day in HOLIDAYS:
            return 200, b'{"data": []}'
        if path == "/equities/bars/daily":
            rows = [{"Date": day, "Code": c, "O": 10.0, "C": 11.0, "AdjC": 11.0, "AdjFactor": 1.0, "Vo": 100} for c in ("13010", "0130A", "99840")]
            if endless:
                return 200, json.dumps({"data": rows[:1], "pagination_key": "again"}).encode()
            if page_bars and "pagination_key" not in params:
                return 200, json.dumps({"data": rows[:2], "pagination_key": "p2"}).encode()
            return 200, json.dumps({"data": rows[2:] if page_bars else rows}).encode()
        if path == "/fins/summary":
            return 200, json.dumps({"data": [{"DiscDate": day, "DiscTime": "15:00:00", "Code": "13010", "DocType": "EarnForecastRevision", "FNP": "1"}]}).encode()
        if path == "/equities/master":
            d = "2099-01-01" if wrong_master_date else day
            return 200, json.dumps({"data": [{"Date": d, "Code": c, "ScaleCat": "TOPIX Mid400"} for c in ("13010", "0130A", "99840")]}).encode()
        if path == "/extra/thing":
            return 200, json.dumps({"data": [{"Date": day, "Code": "13010", "X": 1}]}).encode()
        return 400, b'{"message": "bad"}'
    return get, calls


def _client(get, sleeps=None, interval=13.0):
    return J.Client(get, interval=interval, sleep=(sleeps.append if sleeps is not None else lambda s: None), log=lambda s: None,
                    path_interval=J.PATH_INTERVAL)


NOW = pd.Timestamp("2026-11-11")
F0, F1 = pd.Timestamp("2024-12-23"), pd.Timestamp("2025-02-28")


def test_fetch_saves_by_day_and_picks_last_trading_day_of_each_month(tmp_path):
    get, calls = _fake_fetch()
    res = J.run_fetch(_client(get), F0, F1, ["bars", "fins", "master"], "free", root=tmp_path, now=NOW, log=lambda s: None)
    assert res["stopped"] is None
    days = J.weekdays(F0, F1)
    assert res["kinds"]["bars"] == {"saved": len(days) - len(HOLIDAYS), "empty": len(HOLIDAYS), "failed": 0, "rows": 3 * (len(days) - len(HOLIDAYS))}
    # 一覧は月ごとの最後の取引日だけ（2024-12 は 12-31 に取引があった作り。2025-01・02 は月末の平日）
    masters = sorted(p["date"] for path, p in calls if path == "/equities/master")
    assert masters == ["2024-12-31", "2025-01-31", "2025-02-28"]
    # 順番: 日足 → 財務 → 一覧（一覧は取引のあった日が分かってから）
    order = [path for path, _ in calls]
    assert order.index("/equities/master") > max(i for i, x in enumerate(order) if x == "/equities/bars/daily")
    df = J.read_day(tmp_path, "bars", "2024-12-23")
    assert list(df["Code"]) == ["13010", "0130A", "99840"] and df["Date"].eq("2024-12-23").all()      # コードは文字のまま・2 ページ分がそろう
    assert not J.day_file(tmp_path, "bars", "2025-01-01").exists()
    st = J.fetch_status(tmp_path)
    assert st["kinds"]["bars"]["days"] == len(days) - len(HOLIDAYS) and st["kinds"]["bars"]["gaps"] == [] and st["kinds"]["master"]["days"] == 3
    assert st["kinds"]["master"]["wrong_date"] == [] and st["bytes"] > 0
    assert any("日足" in x for x in J.status_lines(st))


def test_month_end_steps_back_over_holidays_and_waits_for_unfetched_days(tmp_path):
    hol = HOLIDAYS | {"2025-01-31", "2025-01-30"}
    state = {"items": {"bars": {f"{t:%Y-%m-%d}": {"status": 200, "rows": 0 if f"{t:%Y-%m-%d}" in hol else 3, "plan": "free"}
                                for t in J.weekdays(pd.Timestamp("2025-01-06"), pd.Timestamp("2025-02-26"))}}}
    days, undecided = J.month_end_days(state, pd.Timestamp("2025-01-06"), pd.Timestamp("2025-03-14"))
    assert days == ["2025-01-29"]                      # 月末が休みなら、その前の取引のあった日
    assert undecided == ["2025-02"]                    # 2-27・2-28 をまだ取っていない月は決めない。3 月は月の途中なので数えない


def test_month_end_not_decided_when_empty_run_is_too_long():
    state = {"items": {"bars": {f"{t:%Y-%m-%d}": {"status": 210, "rows": 0, "plan": "free"}
                                for t in J.weekdays(pd.Timestamp("2025-03-17"), pd.Timestamp("2025-03-31"))}}}
    state["items"]["bars"]["2025-03-14"] = {"status": 200, "rows": 3, "plan": "free"}
    assert J.month_end_days(state, pd.Timestamp("2025-03-03"), pd.Timestamp("2025-03-31")) == ([], ["2025-03"])


def test_fetch_resumes_and_skips_done_days(tmp_path):
    get, calls = _fake_fetch()
    r1 = J.run_fetch(_client(get), F0, F1, ["bars", "fins", "master"], "free", root=tmp_path, now=NOW, max_calls=10, log=lambda s: None)
    assert "上限" in r1["stopped"] and r1["calls"] <= 11             # 2 ページ目の分だけ超えることがある
    n1 = len(calls)
    r2 = J.run_fetch(_client(get), F0, F1, ["bars", "fins", "master"], "free", root=tmp_path, now=NOW, log=lambda s: None)
    assert r2["stopped"] is None
    asked = [(path, p["date"]) for path, p in calls if "pagination_key" not in p]
    assert len(asked) == len(set(asked))                              # 同じ日を 2 度聞かない
    n2 = len(calls)
    r3 = J.run_fetch(_client(get), F0, F1, ["bars", "fins", "master"], "free", root=tmp_path, now=NOW, log=lambda s: None)
    assert len(calls) == n2 > n1 and r3["calls"] == 0 and r3["stopped"] is None      # 全部済みなら 1 回も呼ばない


def test_fetch_retries_failed_days_and_lost_files_next_time(tmp_path):
    get, calls = _fake_fetch(fail={("/equities/bars/daily", "2024-12-25"): 1})
    r1 = J.run_fetch(_client(get), F0, pd.Timestamp("2024-12-27"), ["bars"], "free", root=tmp_path, now=NOW, log=lambda s: None)
    assert r1["kinds"]["bars"]["failed"] == 1 and r1["stopped"] is None
    st = J.fetch_status(tmp_path)
    assert st["kinds"]["bars"]["failed"] == ["2024-12-25"] and any("取れなかった日" in x for x in J.status_lines(st))
    J.day_file(tmp_path, "bars", "2024-12-24").unlink()
    assert J.fetch_status(tmp_path)["kinds"]["bars"]["lost"] == ["2024-12-24"]
    n = len(calls)
    r2 = J.run_fetch(_client(get), F0, pd.Timestamp("2024-12-27"), ["bars"], "free", root=tmp_path, now=NOW, log=lambda s: None)
    assert r2["kinds"]["bars"]["saved"] == 2 and sorted({p["date"] for _, p in calls[n:]}) == ["2024-12-24", "2024-12-25"]
    st = J.fetch_status(tmp_path)["kinds"]["bars"]
    assert st["failed"] == [] and st["lost"] == [] and st["days"] == 5


def test_fetch_stops_after_three_refusals_in_a_row(tmp_path):
    get, calls = _fake_fetch(refuse_before="2025-01-10")
    r = J.run_fetch(_client(get), F0, F1, ["bars", "fins"], "free", root=tmp_path, now=NOW, log=lambda s: None)
    assert len(calls) == J.MAX_REFUSALS and "断られた" in r["stopped"]
    assert not list(tmp_path.glob("bars/*.gz"))


def test_fetch_stops_on_429_and_keeps_what_it_has(tmp_path):
    get, _ = _fake_fetch()
    n = {"i": 0}

    def limited(path, params):
        n["i"] += 1
        return (429, b"{}") if n["i"] > 4 else get(path, params)
    r = J.run_fetch(_client(limited), F0, F1, ["bars"], "free", root=tmp_path, now=NOW, log=lambda s: None)
    assert "429" in r["stopped"] and r["kinds"]["bars"]["saved"] == 2
    assert J.fetch_status(tmp_path)["kinds"]["bars"]["days"] == 2      # 取れた日は記録に残る（続きから取れる）


def test_fetch_does_not_save_a_day_cut_off_by_too_many_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(J, "FETCH_MAX_PAGES", 3)
    get, _ = _fake_fetch(endless=True)
    r = J.run_fetch(_client(get), F0, F0, ["bars"], "free", root=tmp_path, now=NOW, log=lambda s: None)
    assert r["kinds"]["bars"] == {"saved": 0, "empty": 0, "failed": 1, "rows": 0} and not J.day_file(tmp_path, "bars", "2024-12-23").exists()


def test_fetch_keeps_the_pace_per_plan_and_per_path(tmp_path):
    sleeps: list[float] = []
    get, calls = _fake_fetch(page_bars=False)
    J.run_fetch(_client(get, sleeps), F0, pd.Timestamp("2024-12-24"), ["bars", "fins"], "free", root=tmp_path, now=NOW, log=lambda s: None)
    assert sleeps == [13.0] * (len(calls) - 1)                         # Free は 13 秒に 1 件（最初の 1 回の前だけ待たない）
    sleeps2: list[float] = []
    get, calls = _fake_fetch(page_bars=False)
    J.run_fetch(_client(get, sleeps2, interval=J.PLAN_INTERVAL["premium"]), F0, pd.Timestamp("2024-12-24"), ["bars", "fins"], "premium",
                root=tmp_path / "p", now=NOW, log=lambda s: None)
    assert sleeps2 == [0.15, 1.2, 1.2]                                 # 財務は全プラン 1 分 60 回より速くしない
    assert 60 / J.PLAN_INTERVAL["free"] <= 5 and 60 / J.PLAN_INTERVAL["premium"] <= 500 and 60 / J.PATH_INTERVAL["/fins/summary"] <= 60
    assert all(v >= J.MIN_INTERVAL for v in J.PLAN_INTERVAL.values())


def test_changing_plan_refetches_and_recent_empty_days_are_asked_again(tmp_path):
    get, calls = _fake_fetch(page_bars=False)
    one = pd.Timestamp("2025-01-01")                                   # 行なしの日
    J.run_fetch(_client(get), F0, F0, ["bars"], "free", root=tmp_path, now=NOW, log=lambda s: None)
    J.run_fetch(_client(get), one, one, ["bars"], "free", root=tmp_path, now=NOW, log=lambda s: None)
    n = len(calls)
    J.run_fetch(_client(get), F0, one, ["bars"], "free", root=tmp_path, now=pd.Timestamp("2025-01-03"), log=lambda s: None)
    assert [p["date"] for _, p in calls[n:]] == ["2024-12-24", "2024-12-25", "2024-12-26", "2024-12-27", "2024-12-30", "2024-12-31", "2025-01-01"]
    n = len(calls)                                                     # ↑ 新しい「行なし」（7 日以内）はもう一度聞く
    J.run_fetch(_client(get), F0, F0, ["bars"], "premium", root=tmp_path, now=NOW, log=lambda s: None)
    assert len(calls) == n + 1                                         # プランを替えたら取り直す（調整後の値の基準を混ぜない）
    assert any("プランの違う回" in x for x in J.status_lines(J.fetch_status(tmp_path)))


def test_master_with_wrong_date_is_flagged(tmp_path):
    get, _ = _fake_fetch(wrong_master_date=True)
    J.run_fetch(_client(get), pd.Timestamp("2025-02-03"), F1, ["bars", "master"], "free", root=tmp_path, now=NOW, log=lambda s: None)
    st = J.fetch_status(tmp_path)
    assert st["kinds"]["master"]["wrong_date"] == ["2025-02-28"] and any("日付が指定と違う" in x for x in J.status_lines(st))


def test_cli_fetch_status_dry_run_and_lock(tmp_path, capsys, monkeypatch):
    spec = importlib.util.spec_from_file_location("jq_script2", ROOT / "scripts" / "jquants.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    get, calls = _fake_fetch()
    base = ["fetch", "--from", "2025-02-03", "--to", "2025-02-28", "--dir", str(tmp_path / "jq"), "--today", "2026-11-11"]
    mod.main(base + ["--dry-run"], getter=get, sleep=lambda s: None)
    assert not calls and "これから取る日" in capsys.readouterr().out
    mod.main(base + ["--also", "extra=/extra/thing"], getter=get, sleep=lambda s: None)
    out = capsys.readouterr().out
    assert "頼まれた分は終わった" in out and "上場銘柄一覧（月末）: 1 日分" in out and "extra" in out
    assert not J.LOCK_FILE.exists() and not (tmp_path / "jq" / "fetch.lock").exists()
    assert (tmp_path / "jq" / "extra" / "2025-02-03.csv.gz").exists()
    mod.main(["status", "--dir", str(tmp_path / "jq")])
    assert "日足（全銘柄）" in capsys.readouterr().out
    J.LOCK_FILE.write_text(str(__import__("os").getpid()))                             # 動いている番号の印があれば、2 本目は始めない
    n = len(calls)
    try:
        mod.main(base + ["--retry-empty"], getter=get, sleep=lambda s: None)
        raise AssertionError("止まるはず")
    except SystemExit as e:
        assert "もう 1 本" in str(e) and len(calls) == n
    J.LOCK_FILE.unlink()
    for bad in (["fetch", "--dir", str(tmp_path / "x")], base + ["--what", "bars,nazo"], base + ["--also", "bars=/x"]):
        try:
            mod.main(bad, getter=get, sleep=lambda s: None)
            raise AssertionError("止まるはず")
        except SystemExit:
            pass
    assert J.DATA_DIR == Path("data/jquants") and "data/" in (ROOT / ".gitignore").read_text().split()


# --------------------------------------------------------------------------- 2026-11-11 点検の指摘で足した試験
def _script(name="jq_script3"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "jquants.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SEP_HOL = {"2026-09-21", "2026-09-22", "2026-09-23"}
S0, S1 = pd.Timestamp("2026-09-01"), pd.Timestamp("2026-09-30")


def _fake_sep(empty_once=(), page2=None, bad=None):
    """2026-09 の作り物。empty_once の日の日足は 1 回だけ行なしを返す。page2[(入口, 日)] は 2 ページ目の状態。bad[(入口, 日)] は 1 ページ目の応答。"""
    calls, once, page2, bad = [], set(empty_once), dict(page2 or {}), dict(bad or {})

    def get(path, params):
        calls.append((path, dict(params)))
        day = params["date"]
        if (path, day) in bad:
            return bad[(path, day)]
        if day in SEP_HOL:
            return 200, b'{"data": []}'
        if path == "/equities/bars/daily":
            if day in once:
                once.discard(day)
                return 200, b'{"data": []}'
            row = {"Date": day, "Code": "13010", "C": 11.0}
            if (path, day) in page2:
                if "pagination_key" in params:
                    return page2[(path, day)], b'{"message": "no data"}'
                return 200, json.dumps({"data": [row], "pagination_key": "p2"}).encode()
            return 200, json.dumps({"data": [row]}).encode()
        if path.startswith("/fins/"):
            return 200, json.dumps({"data": [{"DiscDate": day, "Code": "13010", "FNP": "1"}]}).encode()
        return 200, json.dumps({"data": [{"Date": day, "Code": "13010", "ScaleCat": "TOPIX Mid400"}]}).encode()
    return get, calls


def test_trading_day_remembered_as_empty_is_flagged_and_master_stays_one_per_month(tmp_path):
    get, calls = _fake_sep(empty_once={"2026-09-15", "2026-09-30"})
    kinds = ["bars", "fins", "master"]
    J.run_fetch(_client(get), S0, S1, kinds, "premium", root=tmp_path, now=NOW, log=lambda s: None)
    assert [p["date"] for path, p in calls if path == "/equities/master"] == ["2026-09-29"]        # 1 日古い一覧を取ってしまう形
    st = J.fetch_status(tmp_path)
    c = st["checks"]
    assert c["empty_with_fins"] == ["2026-09-15", "2026-09-30"] and "2026-09-15" in c["isolated_empty"]
    text = "\n".join(J.status_lines(st))
    assert "財務に開示のある平日 2: 2026-09-15、2026-09-30" in text and "まだ突き合わせていない" in text and "⚠ は無い" not in text
    # 別の出所の営業日（休みの 3 日を除く平日）と突き合わせると、取引日が混じっていると分かる
    cal = tmp_path / "cal.csv"
    days = [f"{t:%Y-%m-%d}" for t in J.weekdays(pd.Timestamp("2026-08-03"), pd.Timestamp("2026-10-30")) if f"{t:%Y-%m-%d}" not in SEP_HOL]
    pd.DataFrame({"Date": days, "Close": 1}).to_csv(cal, index=False)
    td = J.read_trading_days(cal)
    assert J.fetch_status(tmp_path, td)["checks"]["traded_but_empty"] == ["2026-09-15", "2026-09-30"]
    # 取り直すと、一覧は月に 1 つ（最後の取引日）だけ残る。⚠ は消える
    J.run_fetch(_client(get), S0, S1, kinds, "premium", root=tmp_path, now=NOW, retry_empty=True, log=lambda s: None)
    st = J.fetch_status(tmp_path, td)
    assert st["kinds"]["master"]["days"] == 1 and st["kinds"]["master"]["last"] == "2026-09-30"
    assert not J.day_file(tmp_path, "master", "2026-09-29").exists() and J.day_file(tmp_path, "master", "2026-09-30").exists()
    text = "\n".join(J.status_lines(st))
    assert "⚠ は無い" in text and "休みの日" in text
    # 突き合わせていなければ、行なしの平日が残るかぎり ⚠ は消えない（安全な側）
    assert "まだ突き合わせていない" in "\n".join(J.status_lines(J.fetch_status(tmp_path)))


def test_non_200_after_first_page_is_a_failure_not_an_empty_day(tmp_path):
    get, _ = _fake_sep(page2={("/equities/bars/daily", "2026-09-02"): 210, ("/equities/bars/daily", "2026-09-03"): 500})
    r = J.run_fetch(_client(get), S0, pd.Timestamp("2026-09-04"), ["bars"], "premium", root=tmp_path, now=NOW, log=lambda s: None)
    assert r["kinds"]["bars"] == {"saved": 2, "empty": 0, "failed": 2, "rows": 2}
    st = J.fetch_status(tmp_path)["kinds"]["bars"]
    assert st["failed"] == ["2026-09-02", "2026-09-03"] and st["empty"] == 0
    assert not J.day_file(tmp_path, "bars", "2026-09-02").exists()


def test_status_flags_odd_number_of_empty_weekdays_in_a_year(tmp_path):
    days = [f"{t:%Y-%m-%d}" for t in J.weekdays(pd.Timestamp("2025-01-01"), pd.Timestamp("2025-12-31"))]
    state = {"items": {"bars": {d: {"status": 200, "rows": 3, "plan": "premium"} for d in days}}}
    c = J._cross_checks(state, None)
    assert c["odd_years"] == ["2025 年（0 日）"]                       # 1 年そろっているのに休みが 0 日
    for d in days[10:40]:
        state["items"]["bars"][d]["rows"] = 0
    assert J._cross_checks(state, None)["odd_years"] == ["2025 年（30 日）"]
    for d in days[10:40]:
        state["items"]["bars"][d]["rows"] = 3
    for d in days[5:230:15]:
        state["items"]["bars"][d]["rows"] = 0
    assert J._cross_checks(state, None)["odd_years"] == []            # 15 日なら見当の内


def test_month_ending_on_weekend_still_gets_its_master_and_missing_months_are_flagged(tmp_path):
    assert f"{J.last_weekday(pd.Period('2026-10', 'M')):%Y-%m-%d}" == "2026-10-30"      # 10-31 は土曜
    get, calls = _fake_sep()
    a, b = pd.Timestamp("2026-09-24"), pd.Timestamp("2026-10-30")
    r = J.run_fetch(_client(get), a, b, ["bars", "master"], "premium", root=tmp_path, now=NOW, log=lambda s: None)
    assert [p["date"] for path, p in calls if path == "/equities/master"] == ["2026-09-30", "2026-10-30"] and r["undecided_months"] == []
    assert J.fetch_status(tmp_path)["checks"]["master_missing"] == []
    st = J.load_state(tmp_path)
    del st["items"]["master"]["2026-09-30"]
    J.save_state(tmp_path, st)
    s2 = J.fetch_status(tmp_path)
    assert s2["checks"]["master_missing"] == ["2026-09-30"] and s2["kinds"]["master"]["stray"] == ["2026-09-30"]
    text = "\n".join(J.status_lines(s2))
    assert "月末の一覧が無い月 1" in text and "記録に無いのにファイルがある日 1" in text


def test_plan_change_removes_old_files_of_days_that_turn_empty_or_fail(tmp_path):
    get, _ = _fake_sep()
    end = pd.Timestamp("2026-09-03")
    J.run_fetch(_client(get), S0, end, ["bars"], "free", root=tmp_path, now=NOW, log=lambda s: None)
    assert all(J.day_file(tmp_path, "bars", d).exists() for d in ("2026-09-01", "2026-09-02", "2026-09-03"))
    get2, _ = _fake_sep(bad={("/equities/bars/daily", "2026-09-02"): (200, b'{"data": []}'),
                             ("/equities/bars/daily", "2026-09-03"): (500, b'{"message": "oops"}')})
    J.run_fetch(_client(get2), S0, end, ["bars"], "premium", root=tmp_path, now=NOW, log=lambda s: None)
    assert sorted(f.name for f in (tmp_path / "bars").glob("*.gz")) == ["2026-09-01.csv.gz"]       # Free の分は残らない
    assert J.fetch_status(tmp_path)["kinds"]["bars"]["stray"] == []


def test_pace_cannot_be_made_faster_and_unknown_or_fins_paths_are_slow(tmp_path, capsys):
    assert J.plan_interval("free", 0.12) == 13.0 and J.plan_interval("free", 30) == 30 and J.plan_interval("premium", None) == 0.15
    assert J.path_floor("/fins/dividend") == 1.2 and J.path_floor("/fins/summary") == 1.2 and J.path_floor("/markets/whatever") == 1.2
    assert J.path_floor("/equities/bars/daily") == 0.0 and J.path_floor("/equities/master") == 0.0
    mod = _script()
    sleeps: list[float] = []
    get, _ = _fake_sep()
    base = ["fetch", "--from", "2026-09-01", "--to", "2026-09-02", "--dir", str(tmp_path / "a"), "--today", "2026-11-11", "--what", "bars"]
    mod.main(base + ["--plan", "free", "--interval", "0.12"], getter=get, sleep=sleeps.append)
    assert sleeps == [13.0] and "遅くする方にだけ効く" in capsys.readouterr().out          # 速くする指定は使わない
    sleeps.clear()
    mod.main(["fetch", "--from", "2026-09-01", "--to", "2026-09-02", "--dir", str(tmp_path / "b"), "--today", "2026-11-11", "--what", "bars",
              "--plan", "premium", "--also", "dividend=/fins/dividend", "--also", "other=/markets/thing"], getter=get, sleep=sleeps.append)
    assert sleeps == [0.15, 1.2, 1.2, 1.2, 1.2]                       # 財務の仲間・確かめていない入口は 1.2 秒より速くしない
    sleeps.clear()
    mod.main(base[:6] + [str(tmp_path / "c")] + base[7:] + ["--plan", "premium", "--interval", "2"], getter=get, sleep=sleeps.append)
    assert sleeps == [2.0]                                            # 遅くする方には効く
    c = J.Client(get, interval=0.0, sleep=sleeps.append, log=lambda s: None)
    assert c.interval == J.MIN_INTERVAL


def test_one_lock_for_every_dir_and_for_check_and_stale_locks_are_cleared(tmp_path):
    import os
    import time as _time
    mod = _script("jq_script4")
    get, calls = _fake_sep()
    J.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    J.LOCK_FILE.write_text(str(os.getpid()))                           # 取得が動いている
    for argv in (["fetch", "--from", "2026-09-01", "--to", "2026-09-02", "--dir", str(tmp_path / "other"), "--today", "2026-11-11"],
                 ["check", "--today", "2026-10-15", "--out", str(tmp_path / "out")]):
        with pytest.raises(SystemExit, match="もう 1 本"):
            mod.main(argv, getter=get, sleep=lambda s: None)
    assert not calls and J.LOCK_FILE.exists()
    old = _time.time() - 3600
    for stale in ("", "0", "-5", "abc", "999999999"):                 # 空・0 以下・読めない・もう無い番号の印は古い印として消す
        J.LOCK_FILE.write_text(stale)
        os.utime(J.LOCK_FILE, (old, old))
        mod.main(["fetch", "--from", "2026-09-01", "--to", "2026-09-01", "--dir", str(tmp_path / "jq"), "--today", "2026-11-11", "--what", "bars",
                  "--retry-empty"], getter=get, sleep=lambda s: None)
        assert not J.LOCK_FILE.exists()
    J.LOCK_FILE.write_text("")                                         # いま置かれたばかりの空の印は、書いている途中かもしれないので消さない
    with pytest.raises(SystemExit, match="もう 1 本"):
        mod.main(["fetch", "--from", "2026-09-01", "--to", "2026-09-01", "--dir", str(tmp_path / "jq"), "--today", "2026-11-11"], getter=get,
                 sleep=lambda s: None)


def test_network_exceptions_become_stop_after_one_retry_and_fetch_keeps_state(tmp_path):
    import http.client

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"data": []}'

    for exc in (TimeoutError("timed out"), ConnectionResetError("reset"), http.client.IncompleteRead(b"x")):
        n, waits = {"i": 0}, []

        def opener(req, timeout, exc=exc, n=n):
            n["i"] += 1
            raise exc
        g = J.http_getter("SECRETKEY", sleep=waits.append, opener=opener)
        with pytest.raises(J.Stop) as e:
            g("/x", {"date": "2026-09-01"})
        assert n["i"] == 2 and waits == [J.NET_RETRY_WAIT] and "SECRETKEY" not in str(e.value) and "続きから" in str(e.value)
    seq = iter([TimeoutError("t"), None])

    def flaky(req, timeout):
        if next(seq) is not None:
            raise TimeoutError("t")
        return Resp()
    assert J.http_getter("k", sleep=lambda s: None, opener=flaky)("/x", {}) == (200, b'{"data": []}')     # 1 回だけなら、やり直して続ける
    get, _ = _fake_sep()

    def dies(path, params):
        if params["date"] == "2026-09-03":
            raise TimeoutError("timed out")
        return get(path, params)
    r = J.run_fetch(_client(dies), S0, S1, ["bars"], "premium", root=tmp_path, now=NOW, log=lambda s: None)
    assert "途中で止まった" in r["stopped"] and J.fetch_status(tmp_path)["kinds"]["bars"]["days"] == 2      # 落ちずに止まり、取れた日は残る


def test_state_file_with_wrong_shape_stops(tmp_path):
    for text in ("[]", "{}", '{"items": []}', '{"items": {"bars": []}}', '{"items": {"bars": {"2026-09-01": 3}}}', "{broken"):
        (tmp_path / J.STATE_FILE).write_text(text, encoding="utf-8")
        with pytest.raises(J.Stop, match="読めない"):
            J.load_state(tmp_path)
        with pytest.raises(J.Stop):
            J.run_fetch(_client(_fake_sep()[0]), S0, S0, ["bars"], "free", root=tmp_path, now=NOW, log=lambda s: None)
    (tmp_path / J.STATE_FILE).unlink()
    assert J.load_state(tmp_path) == {"version": 1, "items": {}}
