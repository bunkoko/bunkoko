"""統計の系列と点の形・決まり（src/koten/stats/series.py）。"""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from koten.db import AI_CHECKER, HUMAN_CHECKER
from koten.stats.series import (
    POINTS_COLUMNS,
    POINTS_FILE,
    SERIES_COLUMNS,
    SERIES_FILE,
    Break,
    Point,
    Series,
    SeriesError,
    check_sha,
    comparable,
    is_stale,
    load_meta,
    meta_dir_for,
    owner_ok,
    parse_points,
    parse_series,
    parse_year,
    period_label,
    read_point_rows,
    read_series_rows,
    segments,
    series_problems,
    value_num,
    verify_trend,
    write_points,
)

META = {
    "stat_name": "労働力調査", "gov_org": "総務省統計局", "title": "就業状態別人口",
    "stats_data_id": "0003000001",
    "chosen": [{"class": "区分", "item": "計", "code": "001", "unit": ""}],
}  # fmt: skip
API_SELECTOR = {"statsDataId": "0003000001", "cdCat01": "001"}


def row(**kw):
    base = {c: "" for c in SERIES_COLUMNS}
    base.update(
        series_id="work_rate",
        themes="仕事",
        label="就業している人の割合",
        unit="％",
        survey="労働力調査",
        publisher="総務省統計局",
        table_title="就業状態別人口",
        source_kind="estat_api",
        period_kind="年",
        selector=json.dumps(API_SELECTOR, ensure_ascii=False),
        url="https://www.e-stat.go.jp/stat-search/files?toukei=1",
    )
    base.update(kw)
    return base


def prow(**kw):
    base = {c: "" for c in POINTS_COLUMNS}
    base.update(series_id="work_rate", period="2020", value="1")
    base.update(kw)
    return base


def point(**kw):
    base = {c: "" for c in POINTS_COLUMNS}
    base.update(series_id="work_rate", period="2020", value="1")
    base.update(kw)
    return Point(**base)


def series(**kw):
    s = parse_series([row()])[0]
    return replace(s, **kw)


def pts_values(d):
    return [point(period=k, value=v) for k, v in d.items()]


@pytest.fixture
def env():
    return dict(
        themes={"仕事", "孤独"},
        legacy_ids=set(),
        blocked={"医療": ["うつ病"]},
        metas={"work_rate": META},
    )


def test_columns_and_files():
    assert SERIES_COLUMNS[0] == "series_id" and len(SERIES_COLUMNS) == 23
    assert POINTS_COLUMNS[-1] == "src_sha"
    assert SERIES_FILE == "stat_series.csv" and POINTS_FILE == "stat_points.csv"


def test_good_rows_have_no_problems(env):
    assert series_problems([row()], [prow()], **env) == []


def test_mark_is_never_zero():
    p = point(value="", mark="-")
    assert value_num(p) is None
    assert value_num(point(value="1,234")) == Decimal("1234")
    assert point(value="1,234").value == "1,234"


def test_label_with_a_year_is_refused(env):
    probs = series_problems([row(label="2020年の就業率")], [], **env)
    assert any("label" in x and "年" in x for x in probs)
    assert series_problems([row(label="平成25年の就業率")], [], **env)
    assert series_problems([row(label="令和元年度の就業率")], [], **env)
    assert series_problems([row(label="従業員2000人以上の企業で働く人の割合")], [], **env) == []


def test_series_id_must_not_collide_with_stats_csv(env):
    probs = series_problems(
        [row(series_id="anzen_r7_amount")], [], **{**env, "legacy_ids": {"anzen_r7_amount"}}
    )
    assert probs
    assert series_problems([row(series_id="Bad-ID")], [], **env)
    assert series_problems([row(), row()], [], **env)


def test_themes_rules(env):
    assert series_problems([row(themes="")], [], **env)
    assert series_problems([row(themes="仕事;孤独;仕事;孤独")], [], **env)
    assert series_problems([row(themes="未知")], [], **env)
    assert series_problems([row(themes="仕事;孤独")], [], **env) == []


def test_required_fields_and_url(env):
    for col in ("label", "unit", "survey", "publisher", "url", "selector"):
        assert series_problems([row(**{col: ""})], [], **env), col
    assert series_problems([row(url="ftp://x")], [], **env)


def test_blocked_words(env):
    assert series_problems([row(label="うつ病の人の割合")], [], **env)
    assert series_problems([row(table_title="うつ病")], [], **env)
    assert series_problems([row(survey="うつ病調査")], [], **env)


def test_kinds(env):
    assert series_problems([row(source_kind="web")], [], **env)
    assert series_problems([row(period_kind="月")], [], **env)


def test_selector_shapes(env):
    bad = [
        "not json",
        "[]",
        json.dumps({"cdCat01": "001"}),
        json.dumps({"statsDataId": "1"}),
        json.dumps({"statsDataId": "1", "cdCat01": "001,002"}),
    ]
    for sel in bad:
        assert series_problems([row(selector=sel)], [], **env), sel
    part = {"statInfId": "x", "fileKind": "1", "sheet": "", "values": "B2:B9", "periods": ["2020"]}
    ok = json.dumps({"parts": [part]})
    assert series_problems([row(source_kind="estat_file", selector=ok)], [], **env) == []
    for broken in (
        {"parts": []},
        {"parts": [{k: v for k, v in part.items() if k != "statInfId"}]},
        {"parts": [{k: v for k, v in part.items() if k != "periods"}]},
    ):
        assert series_problems(
            [row(source_kind="estat_file", selector=json.dumps(broken))], [], **env
        )
    cells = {k: v for k, v in part.items() if k != "periods"} | {"periods_cells": "A2:A9"}
    assert (
        series_problems(
            [row(source_kind="estat_file", selector=json.dumps({"parts": [cells]}))], [], **env
        )
        == []
    )


def test_breaks_trend_status_checks(env):
    assert series_problems([row(breaks='[{"before":"2020","note":"郵送"}]')], [], **env) == []
    assert series_problems([row(breaks='[{"before":"2020","note":""}]')], [], **env)
    assert series_problems([row(breaks='[{"before":"20","note":"x"}]')], [], **env)
    assert series_problems([row(breaks="oops")], [], **env)
    assert series_problems([row(trend="増える")], [], **env)
    assert series_problems([row(trend="増加")], [], **env)
    full = dict(trend="増加", trend_from="2015", trend_to="2020", trend_note="同じ設問")
    assert series_problems([row(**full)], [], **env) == []
    assert series_problems([row(status="保留")], [], **env) == []
    assert series_problems([row(status="x")], [], **env)
    assert series_problems([row(checked_by="誰か")], [], **env)
    assert series_problems([row(checked_by=AI_CHECKER)], [], **env)
    assert (
        series_problems([row(checked_by=HUMAN_CHECKER, checked_at="2026-11-27")], [], **env) == []
    )


def test_value_and_mark_are_exclusive(env):
    assert series_problems([row()], [prow(value="1", mark="-")], **env)
    assert series_problems([row()], [prow(value="", mark="")], **env)
    assert series_problems([row()], [prow(value="abc")], **env)
    assert series_problems([row()], [prow(value="", mark="-")], **env) == []
    assert series_problems([row()], [prow(value="1,234")], **env) == []


def test_point_rules(env):
    assert series_problems([row()], [prow(series_id="nope")], **env)
    assert series_problems([row()], [prow(period="20")], **env)
    assert series_problems([row()], [prow(), prow()], **env)


def test_parse_is_lenient():
    s = parse_series([row(selector="oops", breaks="oops", themes="仕事; ;孤独")])[0]
    assert s.selector == {} and s.breaks == () and s.themes == ("仕事", "孤独")
    s = parse_series([row(breaks='[{"before":"2020","note":"郵送"}]')])[0]
    assert s.breaks == (Break("2020", "郵送"),)
    assert parse_points([prow()])[0].value == "1"


def test_period_label():
    assert period_label("2004", "年度") == "2004年度"
    assert period_label("2004", "年") == "2004年"


def test_break_starts_a_new_segment():
    s = series(breaks=(Break("2020", "郵送に変わった"),))
    pts = [point(period=y) for y in ("2018", "2019", "2021")]
    seg = segments(s, pts)
    assert seg == {"2018": 0, "2019": 0, "2021": 1}
    assert comparable(s, pts, "2018", "2021")[0] is False
    assert comparable(s, pts, "2018", "2019")[0] is True


def test_break_outside_range_changes_nothing():
    pts = [point(period=y) for y in ("2018", "2019")]
    s = series(breaks=(Break("2010", "a"), Break("2030", "b")))
    assert segments(s, pts) == {"2018": 0, "2019": 0}


def test_label_drift_in_files_is_a_break():
    pts = [
        point(period="2021", src_label="強い不安"),
        point(period="2022", src_label="強い不安やストレス"),
    ]
    assert segments(series(), pts) == {"2021": 0, "2022": 1}
    pts = [point(period="2021", src_label="強い不安"), point(period="2022", src_label="")]
    assert segments(series(), pts) == {"2021": 0, "2022": 0}


def test_comparable_missing_period():
    ok, why = comparable(series(), [point(period="2018")], "2018", "2019")
    assert not ok and why


def test_check_sha_changes_when_a_value_changes_and_owner_check_lapses():
    s = series(checked_by=HUMAN_CHECKER)
    a = check_sha(s, [point(value="10")], None)
    assert len(a) == 16
    s2 = replace(s, checked_sha=a)
    assert owner_ok(s2, a)
    b = check_sha(s2, [point(value="11")], None)
    assert a != b and not owner_ok(s2, b)
    assert not owner_ok(replace(s2, checked_by=AI_CHECKER), a)
    m = check_sha(s, [point(value="10")], {"title": "表", "chosen": {}})
    assert m != a


def test_trend_needs_strict_monotone_in_one_segment():
    s = series(trend="増加", trend_from="2015", trend_to="2020", trend_note="同じ設問")
    assert verify_trend(s, pts_values({"2015": "1", "2018": "2", "2020": "3"}))[0]
    assert not verify_trend(s, pts_values({"2015": "1", "2018": "1", "2020": "3"}))[0]
    assert not verify_trend(series(), [])[0]
    d = replace(s, trend="減少")
    assert verify_trend(d, pts_values({"2015": "3", "2020": "1"}))[0]
    assert not verify_trend(d, pts_values({"2015": "1", "2020": "3"}))[0]
    assert not verify_trend(s, pts_values({"2015": "1"}))[0]
    ok, why = verify_trend(s, pts_values({"2015": "1", "2016": "", "2020": "3"}))
    assert ok and "2016" in why
    seg = replace(s, breaks=(Break("2018", "x"),))
    assert not verify_trend(seg, pts_values({"2015": "1", "2020": "3"}))[0]


def test_trend_missing_years_are_listed():
    s = series(trend="増加", trend_from="2015", trend_to="2020", trend_note="n")
    pts = pts_values({"2015": "1", "2020": "3"}) + [point(period="2017", value="", mark="-")]
    ok, why = verify_trend(s, pts)
    assert ok and "2017" in why


def test_is_stale():
    s = series(latest_known="2022")
    assert is_stale(s, pts_values({"2020": "1"}))
    assert not is_stale(s, pts_values({"2022": "1"}))
    assert is_stale(s, [point(period="2022", value="", mark="-"), point(period="2021")])
    assert not is_stale(series(latest_known=""), pts_values({"2020": "1"}))


@pytest.mark.parametrize(
    "text,want",
    [
        ("2013年", ("2013", "年")),
        ("2013年度", ("2013", "年度")),
        ("平成25年", ("2013", "年")),
        ("平成25年度", ("2013", "年度")),
        ("令和元年", ("2019", "年")),
        ("令和7年", ("2025", "年")),
        ("２０１３年", ("2013", "年")),
        ("昭和61年度", ("1986", "年度")),
        ("大正1年", ("1912", "年")),
        ("明治45年", ("1912", "年")),
    ],
)
def test_parse_year(text, want):
    assert parse_year(text) == want


@pytest.mark.parametrize(
    "text", ["2013年1月", "2013年1-3月期", "2013年度第1四半期", "2013", "", "abc"]
)
def test_parse_year_refuses(text):
    with pytest.raises(SeriesError):
        parse_year(text)


def test_read_rows_and_load_meta(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    assert read_series_rows(config) == [] and read_point_rows(config) == []
    line = ",".join(row()[c] for c in SERIES_COLUMNS)
    text = "\ufeff" + ",".join(SERIES_COLUMNS) + "\n" + line + "\n"
    (config / SERIES_FILE).write_text(text, encoding="utf-8")
    rows = read_series_rows(config)
    assert rows[0]["series_id"] == "work_rate" and "\ufeffseries_id" not in rows[0]
    meta = meta_dir_for(config)
    assert meta == tmp_path / "docs" / "estat"
    assert load_meta(meta, "0001") is None
    meta.mkdir(parents=True)
    (meta / "0001.json").write_text('{"title": "表"}', encoding="utf-8")
    assert load_meta(meta, "0001") == {"title": "表"}
    (meta / "0002.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(SeriesError, match="0002.json"):
        load_meta(meta, "0002")
    (meta / "0003.json").write_text("[1]", encoding="utf-8")
    with pytest.raises(SeriesError, match="0003.json"):
        load_meta(meta, "0003")


def test_write_points_replaces_only_the_fetched_series(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    write_points(
        config,
        {"a", "b"},
        [
            point(series_id="b", period="2021", value="2"),
            point(series_id="a", period="2020", value="1,5"),
        ],
    )
    write_points(
        config,
        {"a"},
        [
            point(series_id="a", period="2019", value="9"),
            point(series_id="a", period="2018", value="8"),
        ],
    )
    rows = read_point_rows(config)
    assert [(r["series_id"], r["period"], r["value"]) for r in rows] == [
        ("a", "2018", "8"),
        ("a", "2019", "9"),
        ("b", "2021", "2"),
    ]
    with (config / POINTS_FILE).open(encoding="utf-8", newline="") as f:
        assert next(csv.reader(f)) == POINTS_COLUMNS
    assert "\r" not in (config / POINTS_FILE).read_text(encoding="utf-8")
    assert [p.value for p in parse_points(rows)] == ["8", "9", "2"]
    assert isinstance(parse_series([row()])[0], Series)


def test_meta_is_required_and_must_match_for_api_series(env):
    assert series_problems([row()], [], **env) == []
    probs = series_problems([row()], [], **{**env, "metas": {}})
    assert any("work_rate.json" in x and "stats meta" in x for x in probs)
    for col, key in (("survey", "stat_name"), ("publisher", "gov_org"), ("table_title", "title")):
        bad = {**META, key: "別の名前"}
        probs = series_problems([row()], [], **{**env, "metas": {"work_rate": bad}})
        assert any(col in x and "別の名前" in x for x in probs), col
    padded = {**META, **{k: f" {META[k]} " for k in ("stat_name", "gov_org", "title")}}
    assert series_problems([row()], [], **{**env, "metas": {"work_rate": padded}}) == []


def test_meta_must_belong_to_the_rows_table_and_selection(env):
    other = {**META, "stats_data_id": "0009999999"}
    probs = series_problems([row()], [], **{**env, "metas": {"work_rate": other}})
    assert any("別の表" in x and "0009999999" in x for x in probs)
    wrong = {**META, "chosen": [{"class": "区分", "item": "男", "code": "002", "unit": ""}]}
    probs = series_problems([row()], [], **{**env, "metas": {"work_rate": wrong}})
    assert any("選んだ項目の符号" in x and "002" in x for x in probs)
    none = {k: v for k, v in META.items() if k != "chosen"}
    probs = series_problems([row()], [], **{**env, "metas": {"work_rate": none}})
    assert any("選んだ項目の符号" in x for x in probs)


def test_load_meta_refuses_an_unsafe_series_id(tmp_path):
    with pytest.raises(SeriesError):
        load_meta(tmp_path, "../x")


def test_file_series_is_not_checked_against_metas(env):
    part = {"statInfId": "x", "fileKind": "1", "sheet": "", "values": "B2:B9", "periods": ["2020"]}
    r = row(source_kind="estat_file", selector=json.dumps({"parts": [part]}), table_title="")
    assert series_problems([r], [], **{**env, "metas": {}}) == []


def has(probs, *frags):
    return any(all(f in x for f in frags) for x in probs)


def test_full_width_digits_are_refused_in_years_and_dates(env):
    probs = series_problems([row(breaks='[{"before":"２０２０","note":"郵送"}]')], [], **env)
    assert has(probs, "breaks")
    assert has(
        series_problems(
            [
                row(),
            ],
            [prow(period="２０２０")],
            **env,
        ),
        "period",
    )
    assert has(
        series_problems(
            [
                row(),
            ],
            [prow(period="٢٠٢٠")],
            **env,
        ),
        "period",
    )
    probs = series_problems(
        [row(trend="増加", trend_from="２０１５", trend_to="2020", trend_note="n")], [], **env
    )
    assert has(probs, "trend_from")
    probs = series_problems([row(checked_by=AI_CHECKER, checked_at="２０２６-11-27")], [], **env)
    assert has(probs, "checked_at")
    assert has(series_problems([row()], [prow(value="１２")], **env), "value")


def test_latest_known_must_be_a_year(env):
    assert has(series_problems([row(latest_known="2022年度")], [], **env), "latest_known")
    assert has(series_problems([row(latest_known="２０２２")], [], **env), "latest_known")
    assert series_problems([row(latest_known="2022")], [], **env) == []


@pytest.mark.parametrize(
    "label",
    [
        "２０２０年度の就業率",
        "二〇二〇年の就業率",
        "平成二十五年の就業率",
        "昭和六十年の就業率",
        "令和元年の就業率",
    ],
)
def test_label_year_forms(env, label):
    assert has(series_problems([row(label=label)], [], **env), "label")


def test_blocked_word_in_chosen_names(env):
    meta = {**META, "chosen": [{"class": "区分", "item": "うつ病の人", "code": "1", "unit": "％"}]}
    probs = series_problems([row()], [], **{**env, "metas": {"work_rate": meta}})
    assert has(probs, "item", "うつ病")
    meta = {**META, "chosen": [{"class": "うつ病", "item": "計", "code": "1", "unit": "％"}]}
    probs = series_problems([row()], [], **{**env, "metas": {"work_rate": meta}})
    assert has(probs, "class", "うつ病")


def test_comparable_refuses_a_mark_side():
    pts = [point(period="2019", value="1"), point(period="2020", value="", mark="x")]
    ok, why = comparable(series(), pts, "2019", "2020")
    assert not ok and "2020" in why and "x" in why


def test_label_drift_is_not_hidden_by_an_empty_label():
    pts = [
        point(period="2020", src_label="A"),
        point(period="2021", src_label=""),
        point(period="2022", src_label="B"),
    ]
    assert segments(series(), pts) == {"2020": 0, "2021": 0, "2022": 1}


def test_trend_range_must_be_covered_and_not_cross_a_break():
    s = series(trend="増加", trend_from="2015", trend_to="2020", trend_note="n")
    only_late = pts_values({"2017": "1", "2018": "2", "2020": "3"})
    ok, why = verify_trend(s, only_late)
    assert not ok and "2015" in why
    ok, why = verify_trend(s, pts_values({"2015": "1", "2018": "2"}))
    assert not ok and "2020" in why
    brk = replace(s, breaks=(Break("2016", "x"),))
    ok, why = verify_trend(brk, pts_values({"2015": "1", "2017": "2", "2020": "3"}))
    assert not ok and "区切り" in why
    brk_at_from = replace(s, breaks=(Break("2015", "x"),))
    assert verify_trend(brk_at_from, pts_values({"2015": "1", "2020": "3"}))[0]
    ok, why = verify_trend(s, pts_values({"2015": "1", "2018": "2", "2020": "3"}))
    assert ok and "2016" in why


def test_parse_strips_key_fields_but_not_values():
    p = parse_points([prow(series_id=" work_rate ", period=" 2020", value=" 1,234 ")])[0]
    assert p.series_id == "work_rate" and p.period == "2020" and p.value == " 1,234 "
    assert parse_series([row(series_id=" work_rate ")])[0].series_id == "work_rate"


def test_write_points_refuses_stray_series_and_leaves_no_temp(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    write_points(config, {"a"}, [point(series_id="a", period="2020")])
    before = (config / POINTS_FILE).read_bytes()
    with pytest.raises(SeriesError, match="b"):
        write_points(config, {"a"}, [point(series_id="b", period="2020")])
    assert (config / POINTS_FILE).read_bytes() == before
    assert [f.name for f in config.iterdir()] == [POINTS_FILE]


def test_problem_prefixes_and_row_numbers(env):
    probs = series_problems(
        [row(), row(series_id="other", unit="")], [prow(), prow(period="x")], **env
    )
    assert any(x.startswith("stat_series.csv 3行目：") and "unit" in x for x in probs)
    assert any(x.startswith("stat_points.csv 3行目：") for x in probs)
    assert not any("2行目" in x for x in probs)


def test_gate_gaps(env):
    int_id = json.dumps({"statsDataId": 3000001, "cdCat01": "001"})
    assert has(series_problems([row(selector=int_id)], [], **env), "statsDataId")
    assert has(series_problems([row(themes="仕事;仕事")], [], **env), "themes", "2回")
    assert has(
        series_problems([row(checked_by=HUMAN_CHECKER, checked_at="2026-13-45")], [], **env),
        "checked_at",
    )
    swapped = dict(trend="増加", trend_from="2020", trend_to="2015", trend_note="n")
    assert has(series_problems([row(**swapped)], [], **env), "trend_from", "後")
    assert has(series_problems([row(selector="{}")], [], **env), "selector", "空")
    nulled = {**META, "title": None}
    probs = series_problems([row()], [], **{**env, "metas": {"work_rate": nulled}})
    assert has(probs, "title", "文字列でない") and not has(probs, "'None'")


@pytest.mark.parametrize(
    "text,want",
    [
        ("2013年（平成25年）", ("2013", "年")),
        ("2013年度（平成25年度）", ("2013", "年度")),
        ("2013年(平成25年)", ("2013", "年")),
        ("平成31年", ("2019", "年")),
        ("令和8年", ("2026", "年")),
    ],
)
def test_parse_year_parenthetical_era(text, want):
    assert parse_year(text) == want


@pytest.mark.parametrize(
    "text",
    [
        "2013年（平成26年）",
        "2013年度（平成25年）",
        "平成32年",
        "昭和65年",
        "大正16年",
        "明治46年",
        "平成0年",
    ],
)
def test_parse_year_refuses_disagreement_and_overrun(text):
    with pytest.raises(SeriesError):
        parse_year(text)


def test_is_stale_without_any_number():
    s = series(latest_known="2022")
    assert is_stale(s, [])
    assert is_stale(s, [point(period="2022", value="", mark="x")])
