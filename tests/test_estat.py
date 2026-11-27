"""e-Stat の API の応答の読み方（src/koten/stats/estat.py）。実際の API は呼ばない。"""

from __future__ import annotations

import json

import httpx
import pytest

from koten.net import ApiError, Fetcher
from koten.stats.estat import (
    API_BASE,
    APP_ID_ENV,
    ESTAT_API_CREDIT,
    EstatClient,
    EstatError,
    EstatNoData,
    as_list,
    cross_check,
    meta_record,
    parse_meta,
    parse_values,
    period_of,
    resolve_selector,
    status_error,
    units_of,
)
from koten.stats.series import Series

APP_ID = "0123456789abcdef0123456789abcdef01234567"
SID = "0003090287"


def item(code, name, unit="", level="1"):
    d = {"@code": code, "@name": name, "@level": level}
    if unit:
        d["@unit"] = unit
    return d


def cls(cid, name, items):
    return {"@id": cid, "@name": name, "CLASS": items}


def table_inf(**kw):
    d = {
        "@id": SID,
        "STAT_NAME": {"@code": "00200532", "$": "就業構造基本調査"},
        "GOV_ORG": {"@code": "00200", "$": "総務省"},
        "STATISTICS_NAME": "平成24年就業構造基本調査",
        "TITLE": {"@no": "410101", "$": "年齢別の割合"},
        "CYCLE": "-",
        "SURVEY_DATE": 201210,
        "OPEN_DATE": "2014-02-28",
        "UPDATED_DATE": "2014-03-19",
    }
    d.update(kw)
    return d


def result(status=0, msg="正常に終了しました。"):
    return {"STATUS": status, "ERROR_MSG": msg, "DATE": "2026-11-26T00:00:00.000+09:00"}


def classes_default():
    return [
        cls("tab", "表章項目", [item("01", "割合", "%")]),
        cls("cat01", "年齢", [item("1", "総数"), item("2", "20～29歳")]),
        cls("area", "地域", [item("00000", "全国")]),
        cls(
            "time",
            "時間軸(年次)",
            [
                item("2013000000", "2013年"),
                item("2014000000", "2014年"),
                item("2015000000", "2015年"),
            ],
        ),
    ]


def meta_json(classes=None, table=None):
    return {
        "GET_META_INFO": {
            "RESULT": result(),
            "PARAMETER": {},
            "METADATA_INF": {
                "TABLE_INF": table or table_inf(),
                "CLASS_INF": {"CLASS_OBJ": classes if classes is not None else classes_default()},
            },
        }
    }


def v(year, value, **kw):
    d = {"@tab": "01", "@cat01": "2", "@area": "00000", "@time": f"{year}000000", "@unit": "%"}
    d.update(kw)
    d["$"] = value
    return d


def data_json(values, next_key=None, status=0, classes=None, notes=None):
    result_inf = {"TOTAL_NUMBER": len(values), "FROM_NUMBER": 1, "TO_NUMBER": len(values)}
    if next_key is not None:
        result_inf["NEXT_KEY"] = next_key
    data_inf = {"NOTE": notes or [{"@char": "-", "$": "該当数値のないもの"}], "VALUE": values}
    if not values:
        data_inf = None
    sd = {
        "RESULT_INF": result_inf,
        "TABLE_INF": table_inf(),
        "CLASS_INF": {"CLASS_OBJ": classes if classes is not None else classes_default()},
    }
    if data_inf is not None:
        sd["DATA_INF"] = data_inf
    return {"GET_STATS_DATA": {"RESULT": result(status), "PARAMETER": {}, "STATISTICAL_DATA": sd}}


SEL = {"statsDataId": SID, "cdTab": "01", "cdCat01": "2", "cdArea": "00000"}


def make_series(**kw):
    base = dict(
        series_id="age_rate", themes=("仕事",), label="割合", unit="%", survey="就業構造基本調査",
        publisher="総務省", source_kind="estat_api", period_kind="年", selector=dict(SEL),
        table_title="年齢別の割合", url="https://www.e-stat.go.jp/", latest_known="", breaks=(),
        joins=(), trend="", trend_from="", trend_to="", trend_note="", status="", checked_by="",
        checked_at="", checked_sha="", where="", note="",
    )  # fmt: skip
    base.update(kw)
    return Series(**base)


# --- 形 -----------------------------------------------------------------------


def test_constants():
    assert API_BASE == "https://api.e-stat.go.jp/rest/3.0/app/json"
    assert APP_ID_ENV == "KOTEN_ESTAT_APPID"
    assert ESTAT_API_CREDIT == (
        "このサービスは、政府統計総合窓口(e-Stat)のAPI機能を使用していますが、"
        "サービスの内容は国によって保証されたものではありません。"
    )


def test_as_list():
    assert as_list(None) == []
    assert as_list({"a": 1}) == [{"a": 1}]
    assert as_list([1, 2]) == [1, 2]


def one_item(code, name):
    return item(code, name)  # 1件だけは並びでなく dict で来る


def test_single_item_lists_come_as_dicts_and_still_parse():
    data = meta_json(classes=[cls("cat01", "性別", one_item("1", "総数"))])
    data["GET_META_INFO"]["METADATA_INF"]["CLASS_INF"]["CLASS_OBJ"] = cls(
        "cat01", "性別", one_item("1", "総数")
    )
    m = parse_meta(data)
    assert m.classes[0].items[0].name == "総数"
    assert m.classes[0].id == "cat01"


def test_parse_meta_fields_are_strings():
    m = parse_meta(meta_json())
    assert m.stats_data_id == SID
    assert (m.stat_code, m.stat_name) == ("00200532", "就業構造基本調査")
    assert (m.gov_org_code, m.gov_org) == ("00200", "総務省")
    assert (m.title_no, m.title) == ("410101", "年齢別の割合")
    assert m.survey_date == "201210" and m.cycle == "-"
    assert m.open_date == "2014-02-28" and m.updated_date == "2014-03-19"
    assert m.classes[0].items[0].unit == "%"


def test_title_as_plain_string():
    m = parse_meta(meta_json(table=table_inf(TITLE="ただの題")))
    assert m.title == "ただの題" and m.title_no == ""


def test_parse_meta_accepts_stats_data_root():
    m = parse_meta(data_json([v("2013", "1")]))
    assert m.stats_data_id == SID and len(m.classes) == 4


def test_resolve_by_names_and_auto_fill_single_item_classes():
    m = parse_meta(meta_json())
    got = resolve_selector(m, {"年齢": "20〜29歳"})
    assert got == {"statsDataId": SID, "cdTab": "01", "cdCat01": "2", "cdArea": "00000"}


def test_resolve_normalizes_width_and_space():
    m = parse_meta(meta_json())
    assert resolve_selector(m, {"年 齢": " ２０～２９歳 "})["cdCat01"] == "2"


def test_ambiguous_or_missing_name_lists_candidates():
    m = parse_meta(meta_json())
    with pytest.raises(EstatError, match="総数"):
        resolve_selector(m, {})
    with pytest.raises(EstatError, match="20～29歳"):
        resolve_selector(m, {"年齢": "30代"})


def test_resolve_ambiguity_and_unknown_class():
    dup = classes_default()
    dup[1] = cls("cat01", "年齢", [item("1", "総数"), item("9", "総　数")])
    m = parse_meta(meta_json(classes=dup))
    with pytest.raises(EstatError, match="総数"):
        resolve_selector(m, {"年齢": "総数"})
    m2 = parse_meta(meta_json())
    with pytest.raises(EstatError, match="性別"):
        resolve_selector(m2, {"年齢": "総数", "性別": "男"})


def test_resolve_candidates_capped_at_30():
    many = [item(str(i), f"項目{i}") for i in range(40)]
    m = parse_meta(meta_json(classes=[cls("cat01", "多い", many), cls("time", "t", [])]))
    with pytest.raises(EstatError) as e:
        resolve_selector(m, {})
    assert "項目29" in str(e.value) and "項目30" not in str(e.value)


# --- 年 -----------------------------------------------------------------------


def test_periods_wareki_fiscal_and_monthly():
    assert period_of("2013000000", "平成25年") == ("2013", "年")
    assert period_of("2013100000", "2013年度") == ("2013", "年度")
    with pytest.raises(EstatError):
        period_of("2013000101", "2013年1月")


def test_period_average_suffix_accepted():
    assert period_of("2013000000", "2013年平均") == ("2013", "年")
    assert period_of("2013000000", "2013年 年平均") == ("2013", "年")


def test_period_year_must_match_code():
    with pytest.raises(EstatError, match="2014"):
        period_of("2014000000", "2013年")


def test_period_quarter_refused():
    with pytest.raises(EstatError):
        period_of("2013000000", "2013年1-3月期")


# --- 値 -----------------------------------------------------------------------


def test_marks_and_commas():
    vals, nxt = parse_values(data_json([v("2013", "1,234"), v("2014", "-"), v("2015", "***")]), SEL)
    assert vals == [("2013", "1,234", "", "%"), ("2014", "", "-", "%"), ("2015", "", "***", "%")]
    assert nxt is None


@pytest.mark.parametrize("mark", ["-", "…", "x", "X", "***", "*"])
def test_marks_never_become_values(mark):
    vals, _ = parse_values(data_json([v("2013", mark)]), SEL)
    assert vals == [("2013", "", mark, "%")]


def test_numbers_may_arrive_as_json_numbers():
    vals, _ = parse_values(data_json([v("2013", 12.5)]), SEL)
    assert vals[0][:3] == ("2013", "12.5", "")


def test_unit_empty_when_value_has_none():
    row_ = v("2013", "5")
    del row_["@unit"]
    vals, _ = parse_values(data_json([row_]), SEL)
    assert vals == [("2013", "5", "", "")]


def test_single_value_and_note_dicts_and_next_key():
    data = data_json([v("2013", "5")], next_key=101)
    di = data["GET_STATS_DATA"]["STATISTICAL_DATA"]["DATA_INF"]
    di["VALUE"] = di["VALUE"][0]
    di["NOTE"] = di["NOTE"][0]
    vals, nxt = parse_values(data, SEL)
    assert vals == [("2013", "5", "", "%")] and nxt == "101"


def test_no_data_inf_means_no_values():
    vals, nxt = parse_values(data_json([]), SEL)
    assert vals == [] and nxt is None


def test_row_with_other_category_is_refused():
    with pytest.raises(EstatError, match="cat01"):
        parse_values(data_json([v("2013", "1", **{"@cat01": "1"})]), SEL)


def test_duplicate_year_refused():
    with pytest.raises(EstatError, match="2013"):
        parse_values(data_json([v("2013", "1"), v("2013", "2")]), SEL)


def test_time_with_month_refused():
    cl = classes_default()
    cl[3] = cls("time", "時間軸", [item("2013000101", "2013年1月")])
    with pytest.raises(EstatError):
        parse_values(data_json([v("2013", "1", **{"@time": "2013000101"})], classes=cl), SEL)


# --- STATUS -------------------------------------------------------------------


def test_status_error():
    assert status_error(meta_json()) is None
    assert status_error(data_json([], status=1)) is None
    assert status_error(data_json([], status=2)) is None
    assert status_error(data_json([], status=100)) is not None  # ERROR_MSG が正常文でも


def test_status_100_number_and_string():
    d = data_json([], status=100)
    d["GET_STATS_DATA"]["RESULT"]["ERROR_MSG"] = "認証エラー"
    m = status_error(d)
    assert m and "認証エラー" in m and "100" in m
    d["GET_STATS_DATA"]["RESULT"]["STATUS"] = "100"
    assert status_error(d) and "認証エラー" in status_error(d)
    m2 = meta_json()
    m2["GET_META_INFO"]["RESULT"]["STATUS"] = 102
    assert status_error(m2)


def test_status_missing_result():
    assert "RESULT" in status_error({"GET_STATS_DATA": {}})
    assert status_error({"x": 1})


# --- 表の情報 -----------------------------------------------------------------


def test_meta_record_keys_and_chosen_only():
    m = parse_meta(meta_json())
    rec = meta_record(m, resolve_selector(m, {"年齢": "20～29歳"}))
    assert set(rec) == {
        "stats_data_id", "stat_code", "stat_name", "gov_org", "statistics_name", "title",
        "title_no", "cycle", "survey_date", "open_date", "updated_date", "chosen", "times",
        "fetched_at",
    }  # fmt: skip
    assert rec["chosen"] == [
        {"class": "表章項目", "item": "割合", "code": "01", "unit": "%"},
        {"class": "年齢", "item": "20～29歳", "code": "2", "unit": ""},
        {"class": "地域", "item": "全国", "code": "00000", "unit": ""},
    ]
    assert rec["times"][0] == {"code": "2013000000", "name": "2013年"}
    assert rec["stat_name"] == "就業構造基本調査" and rec["gov_org"] == "総務省"
    assert rec["fetched_at"].endswith("+00:00") or rec["fetched_at"].endswith("Z")


def test_cross_check():
    m = parse_meta(meta_json())
    assert cross_check(make_series(), m, {"％"}) == []
    assert cross_check(make_series(), m, set()) == []
    bad = make_series(unit="世帯", survey="別の調査", publisher="厚労省", table_title="別の題")
    msgs = cross_check(bad, m, {"%"})
    assert len(msgs) == 4
    joined = "\n".join(msgs)
    for w in ("世帯", "別の調査", "厚労省", "別の題"):
        assert w in joined


# --- クライアント -------------------------------------------------------------


def make_client(tmp_path, responder):
    seen: list[httpx.Request] = []

    def server(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return responder(req)

    fetcher = Fetcher(
        tmp_path / "raw",
        user_agent="t",
        per_host_per_sec=0,
        client=httpx.Client(transport=httpx.MockTransport(server)),
    )
    return EstatClient(fetcher, APP_ID), seen, fetcher


def test_empty_app_id_refused_without_echo(tmp_path):
    f = Fetcher(tmp_path, user_agent="t", per_host_per_sec=0)
    for bad in ("", "   "):
        with pytest.raises(EstatError) as e:
            EstatClient(f, bad)
        assert "KOTEN_ESTAT_APPID" in str(e.value) and "appId" in str(e.value)


def respond_by_path(pages):
    def responder(req):
        path = req.url.path
        if path.endswith("getMetaInfo"):
            return httpx.Response(200, json=meta_json())
        key = req.url.params.get("startPosition")
        return httpx.Response(200, json=pages[key])

    return responder


def test_next_key_pages_are_joined(tmp_path):
    pages = {
        None: data_json([v("2013", "1"), v("2014", "2")], next_key=3),
        "3": data_json([v("2015", "3,000")]),
    }
    client, seen, _ = make_client(tmp_path, respond_by_path(pages))
    points, meta = client.points(make_series())
    assert [(p.period, p.value) for p in points] == [
        ("2013", "1"),
        ("2014", "2"),
        ("2015", "3,000"),
    ]
    assert meta.title == "年齢別の割合"
    p = points[0]
    assert (p.series_id, p.part, p.src_label, p.src_unit) == ("age_rate", "", "", "%")
    assert p.fetched_at and len(p.src_sha) == 64
    data_reqs = [r for r in seen if r.url.path.endswith("getStatsData")]
    assert len(data_reqs) == 2
    q = data_reqs[0].url.params
    assert q["limit"] == "100000" and q["metaGetFlg"] == "Y" and q["lang"] == "J"
    assert q["cdTab"] == "01" and q["cdCat01"] == "2" and q["cdArea"] == "00000"
    assert q["statsDataId"] == SID and q["annotationGetFlg"] == "Y" and q["replaceSpChar"] == "0"
    assert q["cntGetFlg"] == "N" and q["explanationGetFlg"] == "N"
    mq = next(r for r in seen if r.url.path.endswith("getMetaInfo")).url.params
    assert dict(mq) == {
        "appId": APP_ID, "lang": "J", "statsDataId": SID, "explanationGetFlg": "N",
    }  # fmt: skip
    assert "startPosition" not in q and data_reqs[1].url.params["startPosition"] == "3"
    assert len({p.src_sha for p in points}) == 1


def test_src_sha_changes_when_a_value_changes(tmp_path):
    def run(val):
        pages = {None: data_json([v("2013", val)])}
        client, _, _ = make_client(tmp_path / val, respond_by_path(pages))
        return client.points(make_series())[0][0].src_sha

    assert run("1") != run("2")


def test_duplicate_year_across_pages(tmp_path):
    pages = {None: data_json([v("2013", "1")], next_key=2), "2": data_json([v("2013", "2")])}
    client, _, _ = make_client(tmp_path, respond_by_path(pages))
    with pytest.raises(EstatError, match="2013"):
        client.points(make_series())


def test_page_guard(tmp_path):
    def responder(req):
        if req.url.path.endswith("getMetaInfo"):
            return httpx.Response(200, json=meta_json())
        n = int(req.url.params.get("startPosition", "0"))
        yr = f"{2000 + n}"
        return httpx.Response(
            200,
            json=data_json(
                [v(yr, "1", **{"@time": f"{yr}000000"})], next_key=n + 1, classes=_cls_years()
            ),
        )

    client, _, _ = make_client(tmp_path, responder)
    with pytest.raises(EstatError, match="50"):
        client.points(make_series())


def _cls_years():
    cl = classes_default()
    cl[3] = cls("time", "時間軸", [item(f"{y}000000", f"{y}年") for y in range(2000, 2100)])
    return cl


def test_status_100_drops_cache_and_status_1_is_no_data(tmp_path):
    def responder(req):
        if req.url.path.endswith("getMetaInfo"):
            return httpx.Response(200, json=meta_json())
        d = data_json([], status=100)
        d["GET_STATS_DATA"]["RESULT"]["ERROR_MSG"] = (
            f"アプリケーションIDが正しくありません {APP_ID}"
        )
        d["ECHO"] = str(req.url)  # 応答が送った URL（appId つき）をそのまま返す
        return httpx.Response(200, json=d)

    client, _, fetcher = make_client(tmp_path, responder)
    with pytest.raises(ApiError, match="正しくありません") as e:
        client.points(make_series())
    assert APP_ID not in str(e.value) and "***" in str(e.value)
    cached = list((fetcher.raw_dir / "estat").glob("*.json"))
    assert all("GET_STATS_DATA" not in c.read_text("utf-8") for c in cached)
    for f in fetcher.raw_dir.rglob("*"):
        if f.is_file():
            assert APP_ID not in f.read_text("utf-8", errors="replace"), f

    def responder1(req):
        if req.url.path.endswith("getMetaInfo"):
            return httpx.Response(200, json=meta_json())
        return httpx.Response(200, json=data_json([], status=1))

    client1, _, _ = make_client(tmp_path / "b", responder1)
    with pytest.raises(EstatNoData):
        client1.points(make_series())


def test_status_as_string_100_through_client(tmp_path):
    def responder(req):
        d = meta_json()
        d["GET_META_INFO"]["RESULT"]["STATUS"] = "100"
        return httpx.Response(200, json=d)

    client, _, _ = make_client(tmp_path, responder)
    with pytest.raises(ApiError):
        client.meta(SID)


def test_selector_must_fix_every_class(tmp_path):
    client, seen, _ = make_client(tmp_path, respond_by_path({None: data_json([v("2013", "1")])}))
    sel = dict(SEL)
    del sel["cdCat01"]
    with pytest.raises(EstatError, match="cdCat01"):
        client.points(make_series(selector=sel))
    assert not any(r.url.path.endswith("getStatsData") for r in seen)


def test_period_kind_mismatch(tmp_path):
    client, _, _ = make_client(tmp_path, respond_by_path({None: data_json([v("2013", "1")])}))
    with pytest.raises(EstatError, match="年度"):
        client.points(make_series(period_kind="年度"))


def test_refresh_is_passed_through(tmp_path):
    client, seen, _ = make_client(tmp_path, respond_by_path({None: data_json([v("2013", "1")])}))
    client.meta(SID)
    client.meta(SID)
    assert len(seen) == 1
    client.meta(SID, refresh=True)
    assert len(seen) == 2


def test_app_id_never_in_files_or_errors(tmp_path):
    pages = {None: data_json([v("2013", "1")], next_key=2), "2": data_json([v("2014", "2")])}

    def echoing(req):
        resp = respond_by_path(pages)(req)
        body = resp.json()
        body["ECHO"] = str(req.url)  # 送った URL（appId つき）が本文に入る
        return httpx.Response(200, json=body)

    client, seen, fetcher = make_client(tmp_path, echoing)
    client.points(make_series())
    client.meta(SID)
    # 送った URL には appId がある（これが無いと API は動かない）
    assert all(r.url.params.get("appId") == APP_ID for r in seen)
    files = [f for f in (tmp_path / "raw").rglob("*") if f.is_file()]
    assert len(files) >= 3
    for f in files:
        assert APP_ID not in f.read_text("utf-8", errors="replace"), f
    assert any("***" in f.read_text("utf-8") for f in files)
    for line in (tmp_path / "raw" / "manifest.jsonl").read_text("utf-8").splitlines():
        entry = json.loads(line)
        assert "appId" not in entry["url"] and APP_ID not in line
        assert entry["url"].startswith(API_BASE)

    def boom(req):
        return httpx.Response(403)

    client2, _, _ = make_client(tmp_path / "c", boom)
    with pytest.raises(httpx.HTTPError) as e:
        client2.meta(SID)
    assert APP_ID not in str(e.value) and APP_ID not in repr(e.value)

    def bad_json(req):
        return httpx.Response(200, text="not json " + APP_ID)

    client3, _, _ = make_client(tmp_path / "d", bad_json)
    with pytest.raises(Exception) as e3:
        client3.meta(SID)
    assert APP_ID not in str(e3.value)


# --- 修正 1 回目 --------------------------------------------------------------


def test_fetched_at_kept_when_cached_and_new_when_bytes_change(tmp_path):
    state = {"val": "1"}
    clock = iter(f"2026-11-27T00:00:{i:02d}+00:00" for i in range(60))

    def responder(req):
        if req.url.path.endswith("getMetaInfo"):
            return httpx.Response(200, json=meta_json())
        return httpx.Response(200, json=data_json([v("2013", state["val"])]))

    import koten.net as net

    class FakeDT:
        @staticmethod
        def now(tz=None):
            class T:
                @staticmethod
                def isoformat(timespec=None):
                    return next(clock)

            return T

    orig = net.datetime
    net.datetime = FakeDT
    try:
        client, seen, _ = make_client(tmp_path, responder)
        p1, m1 = client.points(make_series())
        p2, m2 = client.points(make_series())
        assert p1[0].fetched_at == p2[0].fetched_at and m1.fetched_at == p1[0].fetched_at
        assert meta_record(m1, SEL)["fetched_at"] == p1[0].fetched_at
        state["val"] = "2"
        p3, _ = client.points(make_series(), refresh=True)
        assert p3[0].fetched_at != p1[0].fetched_at and p3[0].src_sha != p1[0].src_sha
    finally:
        net.datetime = orig


def test_meta_fetched_at_from_manifest(tmp_path):
    client, _, fetcher = make_client(tmp_path, respond_by_path({}))
    m = client.meta(SID)
    entry = next(iter(fetcher.manifest.entries.values()))
    assert m.fetched_at == entry.fetched_at != ""


def test_duplicate_normalized_class_names_refused():
    cl = classes_default()
    cl[2] = cls("cat02", "年 齢", [item("1", "総数"), item("2", "他")])
    m = parse_meta(meta_json(classes=cl))
    with pytest.raises(EstatError, match="cat01.*cat02|cat02.*cat01"):
        resolve_selector(m, {"年齢": "総数"})


def test_two_keys_for_one_class_refused():
    m = parse_meta(meta_json())
    with pytest.raises(EstatError, match="同じ分類"):
        resolve_selector(m, {"年齢": "総数", "年 齢": "20～29歳"})


def test_units_of_and_cross_check_skips_empty():
    m = parse_meta(meta_json())
    from koten.stats.series import Point

    pt = [Point("a", "2013", "1", "", "", "", u, "", "") for u in ("%", "", "％")]
    assert units_of(pt, m, SEL) == {"%", "％"}
    assert units_of([], m, SEL) == {"%"}  # 表章項目の単位
    assert cross_check(make_series(), m, {"", " "}) == []


def test_average_after_fiscal_year():
    assert period_of("2013100000", "2013年度平均") == ("2013", "年度")


def test_candidates_joined_with_slash():
    cl = classes_default()
    cl[1] = cls("cat01", "年齢", [item("1", "農業・林業"), item("2", "他")])
    m = parse_meta(meta_json(classes=cl))
    with pytest.raises(EstatError, match="農業・林業／他"):
        resolve_selector(m, {})


def test_select_by_code_with_hash():
    cl = classes_default()
    cl[1] = cls("cat01", "年齢", [item("001", "総数", level="1"), item("002", "総数", level="2")])
    m = parse_meta(meta_json(classes=cl))
    with pytest.raises(EstatError, match='"#001"'):
        resolve_selector(m, {"年齢": "総数"})
    assert resolve_selector(m, {"年齢": "#002"})["cdCat01"] == "002"
    assert resolve_selector(m, {"年齢": " # 001 "})["cdCat01"] == "001"
    with pytest.raises(EstatError, match="999"):
        resolve_selector(m, {"年齢": "#999"})


@pytest.mark.parametrize("extra", ["cdCat02", "cdTime"])
def test_extra_cd_key_refused_without_data_request(tmp_path, extra):
    client, seen, _ = make_client(tmp_path, respond_by_path({None: data_json([v("2013", "1")])}))
    with pytest.raises(EstatError, match=extra):
        client.points(make_series(selector={**SEL, extra: "x"}))
    assert not any(r.url.path.endswith("getStatsData") for r in seen)


def test_zero_rows_is_no_data_through_client(tmp_path):
    client, _, _ = make_client(tmp_path, respond_by_path({None: data_json([])}))
    with pytest.raises(EstatNoData):
        client.points(make_series())


def test_empty_value_refused():
    with pytest.raises(EstatError, match="空"):
        parse_values(data_json([v("2013", "")]), SEL)


def test_row_missing_selector_attribute_refused():
    row_ = v("2013", "1")
    del row_["@cat01"]
    with pytest.raises(EstatError, match="cat01"):
        parse_values(data_json([row_]), SEL)


def test_points_refresh_true_requests_again(tmp_path):
    client, seen, _ = make_client(tmp_path, respond_by_path({None: data_json([v("2013", "1")])}))
    client.points(make_series())
    n = len(seen)
    client.points(make_series())
    assert len(seen) == n
    client.points(make_series(), refresh=True)
    assert len(seen) == n + 2  # 表の情報と値の両方を取り直す


def test_resolve_refuses_a_non_string_item_name():
    meta = parse_meta(meta_json())
    with pytest.raises(EstatError) as e:
        resolve_selector(meta, {"年齢": 20})
    assert "年齢" in str(e.value) and "文字列" in str(e.value)


# --- 最後の見直しの直し（R25・R33） ------------------------------------------------------


def test_official_fields_copy_names_and_one_unit():
    from koten.stats.estat import official_fields
    from koten.stats.series import Point

    m = parse_meta(meta_json(table=table_inf(TITLE={"@no": "1", "$": "年齢別の　割合"})))
    pts = [Point("a", y, "1", "", "", "", u, "", "") for y, u in (("2013", "％"), ("2014", "%"))]
    got = official_fields(m, pts)
    assert got == {
        "survey": "就業構造基本調査", "publisher": "総務省", "table_title": "年齢別の　割合",
        "unit": "%",
    }  # fmt: skip
    # e-Stat が単位を出さないときは unit を写さない（台帳の単位のまま）
    blank = [Point("a", "2013", "1", "", "", "", "", "", "")]
    assert "unit" not in official_fields(m, blank)
    # 点ごとに単位が違えば、その系列は取れない
    pairs = (("2013", "%"), ("2014", "千人"))
    mixed = [Point("a", y, "1", "", "", "", u, "", "") for y, u in pairs]
    with pytest.raises(EstatError, match="単位"):
        official_fields(m, mixed)
    half = [Point("a", y, "1", "", "", "", u, "", "") for y, u in (("2013", "%"), ("2014", ""))]
    with pytest.raises(EstatError, match="単位"):
        official_fields(m, half)


def test_cross_check_compares_names_like_the_gate():
    m = parse_meta(meta_json(table=table_inf(TITLE={"@no": "1", "$": "年齢別の　割合"})))
    assert cross_check(make_series(table_title="年齢別の 割合"), m, {"%"}) == []
    assert cross_check(make_series(table_title="年齢別の割合"), m, {"%"})


def test_status_1_drops_the_cached_answer_and_names_the_selector(tmp_path):
    state = {"status": 1}

    def responder(req):
        if req.url.path.endswith("getMetaInfo"):
            return httpx.Response(200, json=meta_json())
        if state["status"] == 1:
            return httpx.Response(200, json=data_json([], status=1))
        return httpx.Response(200, json=data_json([v("2013", "1")]))

    client, seen, fetcher = make_client(tmp_path, responder)
    with pytest.raises(EstatNoData, match="selector"):
        client.points(make_series())
    cached = [p.read_text("utf-8") for p in (fetcher.raw_dir / "estat").glob("*.json")]
    assert cached and all("GET_STATS_DATA" not in c for c in cached)  # 表の情報の控えだけ
    state["status"] = 0
    points, _ = client.points(make_series())  # --refresh なしでも、次は取り直す
    assert [p.period for p in points] == ["2013"]
    assert len([r for r in seen if r.url.path.endswith("getStatsData")]) == 2


def test_selector_from_codes_checks_every_class():
    from koten.stats.estat import selector_from_codes

    m = parse_meta(meta_json())
    assert selector_from_codes(m, SEL) == SEL
    assert selector_from_codes(m, {**SEL, "cdCat01": " 2 "}) == SEL
    with pytest.raises(EstatError, match="cdTime"):
        selector_from_codes(m, {**SEL, "cdTime": "2013000000"})
    with pytest.raises(EstatError, match="地域") as e:
        selector_from_codes(m, {k: v for k, v in SEL.items() if k != "cdArea"})
    assert "全国（00000）" in str(e.value)  # 候補は名前と符号
    with pytest.raises(EstatError, match="表の番号"):
        selector_from_codes(m, {**SEL, "statsDataId": "0000000001"})
