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
    Join,
    Point,
    Series,
    SeriesError,
    check_sha,
    comparable,
    is_stale,
    join_status,
    label_drifts,
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
    assert SERIES_COLUMNS[0] == "series_id" and len(SERIES_COLUMNS) == 24
    assert SERIES_COLUMNS.index("joins") == SERIES_COLUMNS.index("breaks") + 1
    assert POINTS_COLUMNS[-1] == "src_sha"
    assert SERIES_FILE == "stat_series.csv" and POINTS_FILE == "stat_points.csv"


def test_the_repo_ledgers_have_the_current_columns():
    """config の台帳の見出しは、決まりの列と同じ（R44 で joins を足した）。"""
    from koten.config import DEFAULT_CONFIG_DIR

    for name, cols in ((SERIES_FILE, SERIES_COLUMNS), (POINTS_FILE, POINTS_COLUMNS)):
        with (DEFAULT_CONFIG_DIR / name).open(encoding="utf-8-sig", newline="") as f:
            assert next(csv.reader(f)) == cols, name


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
    part = {"statInfId": "1", "fileKind": "1", "sheet": "", "values": "B2:B9", "periods": ["2020"]}
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


@pytest.mark.parametrize(
    "a,b,same",
    [
        ("強い不安、悩み\nストレス", "強い不安、悩み ストレス", True),
        ("合　計", "合計", True),
        ("（％）", "(%)", True),
        ("企業規模 計", "企業規模計", True),
        ("労働組合の有無計", "男女計", False),
    ],
)
def test_labels_are_compared_without_whitespace(a, b, same):
    pts = [point(period="2021", src_label=a), point(period="2022", src_label=b)]
    assert segments(series(), pts) == {"2021": 0, "2022": 0 if same else 1}


def test_whitespace_only_label_counts_as_empty_at_a_part_boundary():
    pts = [
        point(period="2021", src_label="合計", part="0"),
        point(period="2022", src_label=" \u3000\n", part="1"),
    ]
    assert segments(series(), pts) == {"2021": 0, "2022": 1}


def test_changed_labels_ignores_whitespace_only_changes():
    from koten.stats.report import changed_labels

    old = [point(period="2021", src_label="合　計")]
    assert changed_labels(old, [point(period="2021", src_label="合計")]) == []
    assert changed_labels(old, [point(period="2021", src_label="男女計")])


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
        ("1986(昭和61)年", ("1986", "年")),
        ("1986（昭和61）年", ("1986", "年")),
        ("2019(令和元)年", ("2019", "年")),
        ("2020(令和2)年度", ("2020", "年度")),
        ("2020 ( 令和2 ) 年度", ("2020", "年度")),
    ],
)
def test_parse_year(text, want):
    assert parse_year(text) == want


@pytest.mark.parametrize(
    "text",
    [
        "2013年1月", "2013年1-3月期", "2013年度第1四半期", "2013", "", "abc", "59年",
        "1986(昭和62)年", "1990(昭和65)年", "2019(令和0)年",
        "1986(昭和61)年1月", "1986(昭和61)",
    ],
)  # fmt: skip
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
    part = {"statInfId": "1", "fileKind": "1", "sheet": "", "values": "B2:B9", "periods": ["2020"]}
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


def test_file_ids_must_be_ascii_digits(env):
    for bad in ("../x", "1a", "１２"):
        for key in ("statInfId", "fileKind"):
            part = {"statInfId": "1", "fileKind": "1", "sheet": "", "values": "B2:B9",
                    "periods": ["2020"], key: bad}  # fmt: skip
            r = row(
                source_kind="estat_file", selector=json.dumps({"parts": [part]}), table_title=""
            )
            probs = series_problems([r], [], **{**env, "metas": {}})
            assert any("半角の数字だけ" in p for p in probs), (key, bad)


# --- 最後の見直しの直し（R25・R33・L8・S3） ---------------------------------------------


def test_norm_unit_is_nfkc_percent_and_strip():
    from koten.stats.series import norm_unit

    assert norm_unit("％") == "%" and norm_unit(" 千人 ") == "千人" and norm_unit("ｋｇ") == "kg"
    assert norm_unit("") == ""


def test_api_unit_must_match_the_points_src_unit(env):
    probs = series_problems([row(unit="人")], [prow(src_unit="千人")], **env)
    assert has(probs, "work_rate：e-Stat の単位 '千人' と unit '人' が違う")
    # ％ と % は同じ。src_unit が空の点は照らさない（e-Stat が単位を出さない表）
    assert series_problems([row(unit="％")], [prow(src_unit="%")], **env) == []
    assert series_problems([row(unit="人")], [prow(src_unit="")], **env) == []
    # 1系列に1つの文（年ごとには出さない）
    two = [prow(src_unit="千人"), prow(period="2021", src_unit="千人")]
    probs = series_problems([row(unit="人")], two, **env)
    assert len([x for x in probs if "e-Stat の単位" in x]) == 1
    # ファイルの系列は照らさない（単位のセルは文の一部のことがある）
    part = {"statInfId": "1", "fileKind": "1", "sheet": "", "values": "B2:B9", "periods": ["2020"]}
    f = row(source_kind="estat_file", selector=json.dumps({"parts": [part]}), unit="人")
    assert series_problems([f], [prow(src_unit="千人")], **{**env, "metas": {}}) == []


def test_official_names_compare_nfkc_and_collapsed_whitespace(env):
    for theirs in ("就業状態別　人口", "就業状態別\n人口", "就業状態別  人口", "就業状態別\t人口"):
        meta = {**META, "title": theirs}
        probs = series_problems(
            [row(table_title="就業状態別 人口")], [], **{**env, "metas": {"work_rate": meta}}
        )
        assert probs == [], theirs
    meta = {**META, "title": "就業状態別　人口"}
    probs = series_problems(
        [row(table_title="就業状態別人口")], [], **{**env, "metas": {"work_rate": meta}}
    )
    assert has(probs, "table_title", "と違う")
    meta = {**META, "stat_name": "労働力　調査"}
    env2 = {**env, "metas": {"work_rate": meta}}
    probs = series_problems([row(survey="労働力 調査")], [], **env2)
    assert probs == []


def test_appid_in_any_field_is_refused_without_echoing_it(env):
    fake = "f" * 40
    url = f"https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData?appId={fake}&statsDataId=1"
    for col in ("url", "note", "where", "latest_known", "label"):
        probs = series_problems([row(**{col: url})], [], **env)
        assert has(probs, col, "appId"), col
        assert all(fake not in x for x in probs), col
    sel = json.dumps({**API_SELECTOR, "appId": fake})
    probs = series_problems([row(selector=sel)], [], **env)
    assert has(probs, "selector", "appId") and all(fake not in x for x in probs)
    # 変数の名前を書いただけのメモは断らない
    assert series_problems([row(note="KOTEN_ESTAT_APPID は Mac だけ")], [], **env) == []


@pytest.mark.parametrize(
    "col,items",
    [
        ("joins", [{"before": "2021", "from": "合計注)", "to": "合計", "note": '{"appId": "X"}'}]),
        ("breaks", [{"before": "2020", "note": '{"appId": "X"}'}]),
    ],
)
def test_appid_as_a_json_key_inside_a_note_is_refused(env, col, items):
    """JSON の欄（joins・breaks）の note の中の {"appId": …} は、" が \\" になって入る（m5）。"""
    fake = "f" * 40
    cell = json.dumps(items, ensure_ascii=False).replace("X", fake)
    assert '\\"appId\\"' in cell
    probs = series_problems([row(**{col: cell})], [], **env)
    assert has(probs, f"{col} に appId が入っている"), probs
    assert all(fake not in x for x in probs)


def test_check_sha_of_a_series_without_joins_is_the_same_as_before():
    """joins の無い系列の sha は、joins を足す前（5e45418）の版で計算した値のまま（R44）。"""
    s = series(breaks=(Break("2020", "郵送に変わった"),), period_kind="年度")
    pts = [
        point(period="2019", value="1,234", src_label="合計", src_unit="千人"),
        point(period="2020", value="", mark="…", src_label="合計"),
        point(period="2021", value="12.5", src_label="合 計", src_unit="千人"),
    ]
    assert check_sha(s, pts, META) == "1f734bbfab5dbcf6"
    assert check_sha(s, pts, None) == "276d75dc10f59ebe"


def test_check_sha_covers_period_kind_survey_publisher_and_src_unit():
    s = series()
    base = check_sha(s, [point(value="10")], None)
    assert check_sha(replace(s, period_kind="年度"), [point(value="10")], None) != base
    assert check_sha(replace(s, survey="別の調査"), [point(value="10")], None) != base
    assert check_sha(replace(s, publisher="別の機関"), [point(value="10")], None) != base
    assert check_sha(s, [point(value="10", src_unit="千人")], None) != base


# --- ファイルの部分の欄の型（R30・F8） ------------------------------------------------------

GOOD_PART = {
    "statInfId": "000001", "fileKind": "0", "sheet": "", "values": "B3:D3",
    "periods_cells": "B2:D2", "label_cells": ["A3"], "unit_cell": "A1", "updated": "2026-01-01",
}  # fmt: skip


def file_row(part):
    return row(source_kind="estat_file", selector=json.dumps({"parts": [part]}), table_title="")


def file_probs(env, part):
    return series_problems([file_row(part)], [], **{**env, "metas": {}})


def test_good_file_part_and_optional_keys(env):
    assert file_probs(env, GOOD_PART) == []
    for key in ("label_cells", "unit_cell", "updated"):
        assert file_probs(env, {k: v for k, v in GOOD_PART.items() if k != key}) == [], key
    explicit = {k: v for k, v in GOOD_PART.items() if k != "periods_cells"}
    assert file_probs(env, {**explicit, "periods": ["2021", "2022", "2023"]}) == []
    assert file_probs(env, {**GOOD_PART, "label_cells": ["A3", "B1:D1"], "sheet": "表1"}) == []


@pytest.mark.parametrize(
    "key,bad",
    [
        ("label_cells", None), ("label_cells", "A3"), ("label_cells", [3]), ("label_cells", ["A0"]),
        ("values", None), ("values", 5), ("values", ["B3:D3"]), ("values", "B3:D4"),
        ("values", "東京"), ("periods_cells", None), ("periods_cells", ["B2:D2"]),
        ("periods_cells", 5), ("unit_cell", None), ("unit_cell", 5), ("unit_cell", ["A1"]),
        ("updated", None), ("updated", 20260101), ("sheet", None), ("sheet", 1),
        ("statInfId", 1), ("fileKind", 0), ("statInfId", None),
    ],
)  # fmt: skip
def test_file_part_fields_of_the_wrong_type_are_problems(env, key, bad):
    probs = file_probs(env, {**GOOD_PART, key: bad})
    assert has(probs, "parts[1]", key), probs


@pytest.mark.parametrize("bad", [2021, [2021], ["21"], None, "2021", [None]])
def test_explicit_periods_must_be_four_digit_strings(env, bad):
    part = {k: v for k, v in GOOD_PART.items() if k != "periods_cells"} | {"periods": bad}
    assert has(file_probs(env, part), "parts[1]", "periods"), bad


@pytest.mark.parametrize(
    "values,periods_cells,ok",
    [
        ("B3:D3", "B2:D2", True), ("B3:D3", "B9:D9", True), ("B3:D3", "C2:E2", False),
        ("B3:D3", "B2:C2", False), ("B3:D3", "A3:A5", False), ("B3:B5", "A3:A5", True),
        ("B3:B5", "A4:A6", False), ("B3:B5", "C3:E3", False), ("B3", "B2", True),
        ("B3", "A3", True), ("B3", "A1", False), ("B3", "B1:B2", False),
    ],
)  # fmt: skip
def test_periods_cells_must_line_up_with_the_values(env, values, periods_cells, ok):
    probs = file_probs(env, {**GOOD_PART, "values": values, "periods_cells": periods_cells})
    assert (probs == []) is ok, probs
    if not ok:
        assert has(probs, "periods_cells", "並んでいない")


# --- 部分の境の見出し・年の注の印（R32・F10） ------------------------------------------------


def test_unknown_label_at_a_part_boundary_is_a_break():
    pts = [
        point(period="2021", part="0", src_label="強い不安"),
        point(period="2022", part="1", src_label=""),
        point(period="2023", part="2", src_label="強い不安"),
    ]
    assert segments(series(), pts) == {"2021": 0, "2022": 1, "2023": 2}
    ok, why = comparable(series(), pts, "2021", "2023")
    assert not ok and "見出し" in why
    # 1つの部分の中は今までどおり（空の見出しの点は区切らない）
    one = [point(period=y, part="0", src_label="") for y in ("2021", "2022")]
    assert segments(series(), one) == {"2021": 0, "2022": 0}
    # 見出しのそろった部分の境は区切らない。API の点（part が空）は部分の境が無い
    same = [point(period=y, part=n, src_label="A") for y, n in (("2021", "0"), ("2022", "1"))]
    assert segments(series(), same) == {"2021": 0, "2022": 0}
    api = [point(period=y, part="", src_label="") for y in ("2021", "2022")]
    assert segments(series(), api) == {"2021": 0, "2022": 0}


def test_trend_cannot_cross_a_part_with_an_unknown_label():
    s = series(trend="増加", trend_from="2021", trend_to="2023", trend_note="n")
    pts = [
        point(period="2021", value="1", part="0", src_label="A"),
        point(period="2022", value="2", part="1", src_label=""),
        point(period="2023", value="3", part="1", src_label=""),
    ]
    assert not verify_trend(s, pts)[0]


@pytest.mark.parametrize(
    "text,want",
    [
        ("平成23年 1)", ("2011", "年")),
        ("2010年注1)", ("2010", "年")),
        ("2019年 2)", ("2019", "年")),
        ("2013年\n(平成25年)", ("2013", "年")),
        ("2021年（1）", ("2021", "年")),
        ("2021年(1)", ("2021", "年")),
        ("2021年※", ("2021", "年")),
        ("2021年※1", ("2021", "年")),
        ("2021年*", ("2021", "年")),
        ("2021年度 2)\n", ("2021", "年度")),
    ],
)
def test_period_names_with_footnote_marks_and_line_breaks(text, want):
    from koten.stats.series import parse_period_name

    assert parse_period_name(text) == want


@pytest.mark.parametrize("text", ["2019)", "2021年 x)", "2021年(注意)", "2021年1月 1)"])
def test_other_leftovers_are_still_refused(text):
    from koten.stats.series import parse_period_name

    with pytest.raises(SeriesError):
        parse_period_name(text)


# --- 見出しの言い方だけが違う年の境（joins。R44） ----------------------------------------------

OLD = "強い不安、悩み、ストレスを感じる事柄がある"
NEW = "強い不安、悩み、ストレスとなっていると感じる事柄がある"


def lpts(*labels, parts=None):
    """2019 年からの年ごとの点。labels は src_label、parts は part（省くと全部 "0"）。"""
    parts = parts or ["0"] * len(labels)
    return [
        point(period=str(2019 + i), src_label=lab, part=pt)
        for i, (lab, pt) in enumerate(zip(labels, parts, strict=True))
    ]


def joined(*joins, **kw):
    return series(joins=tuple(Join(*j) for j in joins), **kw)


def test_a_join_keeps_one_segment_where_only_the_wording_changes():
    pts = lpts(OLD, OLD, NEW, NEW)
    assert segments(series(), pts) == {"2019": 0, "2020": 0, "2021": 1, "2022": 1}
    s = joined(("2021", OLD, NEW, "両方の年の調査票で問いが同じ"))
    assert segments(s, pts) == {"2019": 0, "2020": 0, "2021": 0, "2022": 0}
    assert comparable(s, pts, "2019", "2022") == (True, "")


def test_a_join_whose_to_does_not_match_still_splits():
    pts = lpts(OLD, OLD, NEW, NEW)
    s = joined(("2021", OLD, "別の言い方", "n"))
    assert segments(s, pts) == {"2019": 0, "2020": 0, "2021": 1, "2022": 1}
    s = joined(("2021", "別の言い方", NEW, "n"))
    assert segments(s, pts) == {"2019": 0, "2020": 0, "2021": 1, "2022": 1}


def test_a_join_never_cancels_a_crossed_break():
    # 2020 の点が無く、2020 の区切りを 2021 の点でまたぐ
    pts = [point(period="2019", src_label=OLD), point(period="2021", src_label=NEW)]
    s = joined(("2021", OLD, NEW, "n"), breaks=(Break("2020", "郵送に変わった"),))
    assert segments(s, pts) == {"2019": 0, "2021": 1}


def test_a_join_never_cancels_an_unknown_part_boundary():
    # 2021 は部分の境で、前の点の見出しが空（R32）。from・to は合っている
    pts = lpts(OLD, "", NEW, parts=["0", "0", "1"])
    s = joined(("2021", OLD, NEW, "n"))
    assert segments(s, pts) == segments(series(), pts) == {"2019": 0, "2020": 0, "2021": 1}


def test_a_join_cancels_only_its_own_year():
    pts = lpts(OLD, NEW, NEW, "もっと別の言い方")
    s = joined(("2020", OLD, NEW, "n"))
    assert segments(s, pts) == {"2019": 0, "2020": 0, "2021": 0, "2022": 1}
    # 境の年が違えば、つながない（before は、新しい言い方の最初の年そのもの）
    assert segments(joined(("2021", OLD, NEW, "n")), pts) == {
        "2019": 0, "2020": 1, "2021": 1, "2022": 2,
    }  # fmt: skip


def test_join_labels_are_compared_without_whitespace_and_width():
    pts = [point(period="2020", src_label="合計注)"), point(period="2021", src_label="合　計")]
    s = joined(("2021", "合計 注）", "合計", "注の印だけが違う"))
    assert segments(s, pts) == {"2020": 0, "2021": 0}


def test_join_from_is_the_last_non_empty_label():
    # 間に見出しの空の点があっても、from は最後の空でない見出しと比べる（R32 でない、同じ部分）
    pts = lpts(OLD, "", NEW)
    s = joined(("2021", OLD, NEW, "n"))
    assert segments(s, pts) == {"2019": 0, "2020": 0, "2021": 0}
    assert join_status(s, pts) == [(s.joins[0], "")]


def test_join_status_gives_each_reason_in_order():
    def why(s, pts):
        ((_, w),) = join_status(s, pts)
        return w

    pts = lpts(OLD, OLD, NEW, NEW)
    assert why(joined(("2021", OLD, NEW, "n")), pts) == ""
    assert why(joined(("2030", OLD, NEW, "n")), pts) == "その年の点が無い"
    assert why(joined(("2019", OLD, NEW, "n")), pts) == "その年より前に点が無い"
    gap = [point(period="2019", src_label=OLD), point(period="2021", src_label=NEW)]
    brk = joined(("2021", OLD, NEW, "n"), breaks=(Break("2020", "x"),))
    assert why(brk, gap) == "その年の前に区切り（breaks）がある"
    # 区切りが先：from・to が違っていても区切りの理由
    brk_bad = joined(("2021", "別", "別の2", "n"), breaks=(Break("2020", "x"),))
    assert why(brk_bad, gap) == "その年の前に区切り（breaks）がある"
    unknown = lpts(OLD, "", NEW, parts=["0", "0", "1"])
    assert why(joined(("2021", OLD, NEW, "n")), unknown) == "見出しの分からない部分の境（R32）"
    # 部分の境で、この年の見出しが空：R32 が先
    empty_at_part = lpts(OLD, "", parts=["0", "1"])
    r32 = "見出しの分からない部分の境（R32）"
    assert why(joined(("2020", OLD, NEW, "n")), empty_at_part) == r32
    assert why(joined(("2020", OLD, NEW, "n")), pts) == "見出しが変わっていない"
    assert why(joined(("2020", OLD, NEW, "n")), lpts(OLD, "", NEW)) == "見出しが変わっていない"
    # 前の見出しは、最後に空でない見出しを持っていた点の src_label のまま（label_key にしない）
    stored = lpts("合 計 注)", "男女計")
    assert why(joined(("2020", OLD, NEW, "n")), stored) == (
        "見出しが from・to と違う（前 '合 計 注)'・この年 '男女計'）"
    )
    skipped = lpts("合 計", "", "男女計")
    assert why(joined(("2021", OLD, NEW, "n")), skipped) == (
        "見出しが from・to と違う（前 '合 計'・この年 '男女計'）"
    )
    # 前に空でない見出しが1つも無い（同じ部分）：区切らないが、つないでもいない
    no_label_before = lpts("", NEW)
    assert segments(joined(("2020", OLD, NEW, "n")), no_label_before) == {"2019": 0, "2020": 0}
    assert why(joined(("2020", OLD, NEW, "n")), no_label_before) == (
        f"見出しが from・to と違う（前 ''・この年 '{NEW}'）"
    )


def test_join_status_lists_every_join_in_order_and_agrees_with_segments():
    pts = lpts(OLD, OLD, NEW, NEW, "最後の言い方")
    s = joined(
        ("2021", OLD, NEW, "a"), ("2022", OLD, NEW, "b"), ("2023", NEW, "最後の言い方", "c"),
        ("1999", OLD, NEW, "d"),
    )  # fmt: skip
    got = join_status(s, pts)
    assert [j.note for j, _ in got] == ["a", "b", "c", "d"]
    assert [w for _, w in got] == ["", "見出しが変わっていない", "", "その年の点が無い"]
    plain, seg = segments(series(), pts), segments(s, pts)
    years = sorted(plain)
    for j, w in got:
        if j.before not in plain or j.before == years[0]:
            continue
        before = years[years.index(j.before) - 1]
        cancelled = plain[j.before] > plain[before] and seg[j.before] == seg[before]
        assert cancelled == (w == ""), j


def test_join_status_without_joins_is_empty():
    assert join_status(series(), lpts(OLD, NEW)) == []


def jrow(joins, **kw):
    text = joins if isinstance(joins, str) else json.dumps(joins, ensure_ascii=False)
    return row(joins=text, **kw)


GOOD_JOIN = {"before": "2021", "from": OLD, "to": NEW, "note": "両方の年の調査票で問いが同じ"}


def test_good_joins_have_no_problems(env):
    assert series_problems([jrow([GOOD_JOIN])], [], **env) == []
    assert series_problems([jrow("")], [], **env) == []
    assert series_problems([jrow("[]")], [], **env) == []
    two = [GOOD_JOIN, {**GOOD_JOIN, "before": "2023", "from": NEW, "to": "最後の言い方"}]
    assert series_problems([jrow(two, breaks='[{"before":"2020","note":"x"}]')], [], **env) == []


@pytest.mark.parametrize("bad", ["oops", '{"before": "2021"}', '"2021"'])
def test_joins_must_be_a_json_list(env, bad):
    probs = series_problems([jrow(bad)], [], **env)
    assert probs == [
        'stat_series.csv 2行目：joins は [{"before":"YYYY","from":"…","to":"…","note":"…"}] の JSON'
    ]


@pytest.mark.parametrize(
    "item",
    [
        "2021", None, ["2021"],
        {**GOOD_JOIN, "before": "21"}, {**GOOD_JOIN, "before": "２０２１"},
        {k: v for k, v in GOOD_JOIN.items() if k != "before"},
        {**GOOD_JOIN, "from": ""}, {**GOOD_JOIN, "to": " 　"}, {**GOOD_JOIN, "note": " "},
        {k: v for k, v in GOOD_JOIN.items() if k != "note"},
        {**GOOD_JOIN, "from": None}, {**GOOD_JOIN, "note": 5},
    ],
)  # fmt: skip
def test_each_join_needs_a_year_and_non_empty_texts(env, item):
    probs = series_problems([jrow([GOOD_JOIN, item, item])], [], **env)
    want = (
        "stat_series.csv 2行目：joins の各項目は before（YYYY）と、空でない from・to・note が要る"
    )
    assert probs.count(want) == 1, probs  # 1行に1回


def test_join_problems_same_wording_twice_and_on_a_break(env):
    same = {**GOOD_JOIN, "from": "合　計", "to": "合計"}
    probs = series_problems([jrow([same])], [], **env)
    assert probs == [
        "stat_series.csv 2行目：joins の 2021 は from と to が同じ"
        "（空白と全角・半角の違いは、つながなくても同じ見出し。R41）"
    ]
    width = {**GOOD_JOIN, "from": "(%)", "to": "（％）"}
    assert has(series_problems([jrow([width])], [], **env), "joins の 2021 は from と to が同じ")
    twice = [GOOD_JOIN, {**GOOD_JOIN, "from": NEW, "to": OLD}, GOOD_JOIN]
    probs = series_problems([jrow(twice)], [], **env)
    assert probs == ["stat_series.csv 2行目：joins の 2021 が2つある"]
    on_break = jrow([GOOD_JOIN], breaks='[{"before":"2021","note":"郵送に変わった"}]')
    assert series_problems([on_break], [], **env) == [
        "stat_series.csv 2行目：joins の 2021 は breaks にもある（区切りの年はつながない）"
    ]


def test_parse_reads_joins_leniently():
    s = parse_series([jrow([GOOD_JOIN, "x", 3, {"before": "2022"}])])[0]
    good = Join("2021", OLD, NEW, "両方の年の調査票で問いが同じ")
    assert s.joins == (good, Join("2022", "", "", ""))
    assert parse_series([jrow("oops")])[0].joins == ()
    assert parse_series([jrow("")])[0].joins == ()
    no_column = {k: v for k, v in row().items() if k != "joins"}
    assert parse_series([no_column])[0].joins == ()


def test_series_from_draft_passes_the_joins_list_on():
    from koten.stats.report import series_from_draft

    item = {**row(), "themes": ["仕事"], "selector": API_SELECTOR, "joins": [GOOD_JOIN]}
    assert series_from_draft(item).joins == (
        Join("2021", OLD, NEW, "両方の年の調査票で問いが同じ"),
    )
    assert series_from_draft({**item, "joins": []}).joins == ()
    assert series_from_draft({k: v for k, v in item.items() if k != "joins"}).joins == ()


def test_check_sha_covers_the_joins():
    pts = lpts(OLD, NEW)
    base = check_sha(series(), pts, META)
    one = joined(("2020", OLD, NEW, "根拠"))
    assert check_sha(one, pts, META) != base
    note = joined(("2020", OLD, NEW, "別の根拠"))
    assert check_sha(note, pts, META) != check_sha(one, pts, META)
    for other in (("2021", OLD, NEW, "根拠"), ("2020", OLD + "。", NEW, "根拠"),
                  ("2020", OLD, NEW + "。", "根拠")):  # fmt: skip
        assert check_sha(joined(other), pts, META) != check_sha(one, pts, META)


def test_label_drifts_lists_each_wording_change_that_joins_do_not_cancel():
    pts = lpts(OLD, OLD, NEW, NEW, "最後の言い方")
    assert label_drifts(series(), pts) == [("2021", OLD, NEW), ("2023", NEW, "最後の言い方")]
    assert label_drifts(joined(("2021", OLD, NEW, "n")), pts) == [("2023", NEW, "最後の言い方")]
    # 区切りの年の見出しの変化も出す（つなげない年でも、言い方が変わったことは分かる）
    brk = series(breaks=(Break("2021", "郵送に変わった"),))
    assert label_drifts(brk, pts) == [("2021", OLD, NEW), ("2023", NEW, "最後の言い方")]
    assert label_drifts(series(), lpts(OLD, OLD)) == []


def test_label_drifts_skip_empty_labels_and_show_the_stored_text():
    # 見出しの空の部分の境（R32）は出さない。前の見出しは、最後の空でない見出しの点のもの
    pts = lpts("合 計", "", "合 計", "男女 計", parts=["0", "1", "2", "2"])
    assert label_drifts(series(), pts) == [("2022", "合 計", "男女 計")]
    # 部分の境で前の点の見出しが空でも、この年の見出しが最後の空でない見出しと違えば出す
    across = lpts(OLD, "", NEW, parts=["0", "0", "1"])
    assert label_drifts(series(), across) == [("2021", OLD, NEW)]
