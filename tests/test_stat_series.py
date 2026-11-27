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
        metas={},
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
    (config / SERIES_FILE).write_text("﻿" + ",".join(SERIES_COLUMNS) + "\n", encoding="utf-8")
    assert read_series_rows(config) == []
    meta = meta_dir_for(config)
    assert meta == tmp_path / "docs" / "estat"
    assert load_meta(meta, "0001") is None
    meta.mkdir(parents=True)
    (meta / "0001.json").write_text('{"title": "表"}', encoding="utf-8")
    assert load_meta(meta, "0001") == {"title": "表"}


def test_write_points_replaces_only_the_fetched_series(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    write_points(
        config,
        set(),
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
