"""統計の系列を SSD に入れる（koten.db.load_series・koten db init）。"""

from __future__ import annotations

import csv
import json
import shutil

import pytest
from typer.testing import CliRunner

from koten.cli import app
from koten.db import AI_CHECKER, HUMAN_CHECKER, StatsError, connect, load_series
from koten.stats.series import (
    POINTS_COLUMNS,
    POINTS_FILE,
    SERIES_COLUMNS,
    SERIES_FILE,
    check_sha,
    load_meta,
    parse_points,
    parse_series,
)

META = {"stat_name": "労働力調査", "gov_org": "総務省統計局", "title": "就業状態別人口"}
SELECTOR = {"statsDataId": "0003000001", "cdCat01": "001"}


def srow(**kw):
    base = {c: "" for c in SERIES_COLUMNS}
    base.update(
        series_id="work_rate",
        themes="仕事;孤独",
        label="就業している人の割合",
        unit="％",
        survey="労働力調査",
        publisher="総務省統計局",
        table_title="就業状態別人口",
        source_kind="estat_api",
        period_kind="年",
        selector=json.dumps(SELECTOR, ensure_ascii=False),
        url="https://www.e-stat.go.jp/stat-search/files?toukei=1",
        latest_known="2022",
    )
    base.update(kw)
    return base


def prow(period, value="", mark="", **kw):
    base = {c: "" for c in POINTS_COLUMNS}
    base.update(series_id="work_rate", period=period, value=value, mark=mark)
    base.update(kw)
    return base


GOOD_POINTS = [prow("2020", "10"), prow("2021", mark="…"), prow("2022", "1,230")]


def write_csv(path, cols, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def cfg(tmp_path):
    c = tmp_path / "config"
    c.mkdir()
    (c / "themes.yaml").write_text("themes:\n  - 仕事\n  - 孤独\n", encoding="utf-8")
    (c / "blocked_keywords.yaml").write_text(
        "categories:\n  医療:\n    - うつ病\n", encoding="utf-8"
    )
    write_csv(
        c / "stats.csv",
        ["stat_id", "theme", "label", "value", "url"],
        [dict(stat_id="old_stat", theme="仕事", label="x", value="1", url="https://x.example/")],
    )
    meta = tmp_path / "docs" / "estat"
    meta.mkdir(parents=True)
    (meta / "work_rate.json").write_text(json.dumps(META, ensure_ascii=False), encoding="utf-8")
    write_csv(c / SERIES_FILE, SERIES_COLUMNS, [srow()])
    write_csv(c / POINTS_FILE, POINTS_COLUMNS, GOOD_POINTS)
    return c


@pytest.fixture
def meta_dir(cfg):
    return cfg.parent / "docs" / "estat"


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "ssd")


def count(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_load_series_loads_both_tables(conn, cfg, meta_dir):
    assert load_series(conn, cfg, meta_dir) == (1, 3)
    r = conn.execute("SELECT * FROM stat_series").fetchone()
    assert json.loads(r["themes"]) == ["仕事", "孤独"]
    assert json.loads(r["selector"]) == SELECTOR
    assert r["latest_period"] == "2022" and r["stale"] == 0 and r["trend_ok"] == 0
    assert len(r["check_sha"]) == 16 and r["owner_ok"] == 0


def test_header_only_loads_zero(conn, cfg, meta_dir):
    write_csv(cfg / SERIES_FILE, SERIES_COLUMNS, [])
    write_csv(cfg / POINTS_FILE, POINTS_COLUMNS, [])
    assert load_series(conn, cfg, meta_dir) == (0, 0)


def test_load_series_is_all_or_nothing(conn, cfg, meta_dir):
    write_csv(cfg / SERIES_FILE, SERIES_COLUMNS, [srow(), srow(series_id="Bad Id")])
    with pytest.raises(StatsError):
        load_series(conn, cfg, meta_dir)
    assert count(conn, "stat_series") == 0 and count(conn, "stat_points") == 0


def test_bad_reload_keeps_the_previous_load(conn, cfg, meta_dir):
    load_series(conn, cfg, meta_dir)
    write_csv(cfg / POINTS_FILE, POINTS_COLUMNS, [prow("2020", "10"), prow("2020", "11")])
    with pytest.raises(StatsError):
        load_series(conn, cfg, meta_dir)
    assert count(conn, "stat_series") == 1 and count(conn, "stat_points") == 3


def test_missing_meta_is_a_problem(conn, cfg, meta_dir):
    (meta_dir / "work_rate.json").unlink()
    with pytest.raises(StatsError):
        load_series(conn, cfg, meta_dir)
    assert count(conn, "stat_series") == 0


def test_loaded_points_carry_segment_value_num_and_label(conn, cfg, meta_dir):
    pts = [
        prow("2020", "10", src_label="A"),
        prow("2021", mark="…", src_label="A"),
        prow("2022", "1,230", src_label="B"),
    ]
    write_csv(cfg / POINTS_FILE, POINTS_COLUMNS, pts)
    load_series(conn, cfg, meta_dir)
    r = conn.execute("SELECT * FROM stat_points WHERE period='2021'").fetchone()
    assert r["segment"] == 0 and r["value_num"] is None and r["mark"] == "…"
    assert r["period_label"] == "2021年" and r["value"] == ""
    r = conn.execute("SELECT * FROM stat_points WHERE period='2022'").fetchone()
    assert r["segment"] == 1 and r["value_num"] == 1230.0 and r["value"] == "1,230"


def test_owner_ok_lapses_after_a_refetch(conn, cfg, meta_dir):
    s = parse_series([srow()])[0]
    sha = check_sha(s, parse_points(GOOD_POINTS), load_meta(meta_dir, "work_rate"))
    write_csv(
        cfg / SERIES_FILE,
        SERIES_COLUMNS,
        [srow(checked_by=HUMAN_CHECKER, checked_at="2026-11-20", checked_sha=sha)],
    )
    load_series(conn, cfg, meta_dir)
    assert conn.execute("SELECT owner_ok FROM stat_series").fetchone()[0] == 1
    changed = [prow("2020", "11"), *GOOD_POINTS[1:]]
    write_csv(cfg / POINTS_FILE, POINTS_COLUMNS, changed)
    load_series(conn, cfg, meta_dir)
    assert conn.execute("SELECT owner_ok FROM stat_series").fetchone()[0] == 0
    # AI の確認では、sha が合っていても owner_ok にならない
    write_csv(
        cfg / SERIES_FILE,
        SERIES_COLUMNS,
        [srow(checked_by=AI_CHECKER, checked_at="2026-11-20", checked_sha=sha)],
    )
    write_csv(cfg / POINTS_FILE, POINTS_COLUMNS, GOOD_POINTS)
    load_series(conn, cfg, meta_dir)
    assert conn.execute("SELECT owner_ok FROM stat_series").fetchone()[0] == 0


def test_stale_and_trend_ok(conn, cfg, meta_dir):
    write_csv(
        cfg / SERIES_FILE,
        SERIES_COLUMNS,
        [
            srow(
                latest_known="2023",
                trend="増加",
                trend_from="2020",
                trend_to="2022",
                trend_note="同じ調査",
            )
        ],
    )
    write_csv(cfg / POINTS_FILE, POINTS_COLUMNS, [prow("2020", "1"), prow("2022", "5")])
    load_series(conn, cfg, meta_dir)
    r = conn.execute("SELECT * FROM stat_series").fetchone()
    assert r["stale"] == 1 and r["latest_period"] == "2022" and r["trend_ok"] == 1
    write_csv(cfg / POINTS_FILE, POINTS_COLUMNS, [prow("2020", "5"), prow("2022", "1")])
    load_series(conn, cfg, meta_dir)
    assert conn.execute("SELECT trend_ok FROM stat_series").fetchone()[0] == 0


def test_latest_period_is_null_without_numbers(conn, cfg, meta_dir):
    write_csv(cfg / POINTS_FILE, POINTS_COLUMNS, [prow("2020", mark="-")])
    load_series(conn, cfg, meta_dir)
    assert conn.execute("SELECT latest_period FROM stat_series").fetchone()[0] is None


def test_estat_file_series_has_sha_without_meta(conn, cfg, meta_dir):
    sel = {
        "parts": [
            {"statInfId": "1", "fileKind": "0", "sheet": "", "values": "B2:B5", "periods": ["2020"]}
        ]
    }
    write_csv(
        cfg / SERIES_FILE,
        SERIES_COLUMNS,
        [
            srow(
                source_kind="estat_file",
                selector=json.dumps(sel),
                survey="x",
                publisher="y",
                table_title="z",
            )
        ],
    )
    assert load_series(conn, cfg, meta_dir)[0] == 1


@pytest.fixture
def cli_env(cfg, tmp_path):
    shutil.copy("config/sources.yaml", cfg / "sources.yaml")
    p = cfg / "settings.toml"
    p.write_text(
        f'[paths]\nhdd_root = "{tmp_path / "hdd"}"\nssd_root = "{tmp_path / "ssd2"}"\n',
        encoding="utf-8",
    )
    return p


def test_db_init_prints_series_line(cli_env):
    r = CliRunner().invoke(app, ["db", "init", "--settings", str(cli_env)])
    assert r.exit_code == 0, r.output
    assert "統計 1 件を読み込みました（config/stats.csv）" in r.output
    assert (
        "統計の系列 1 件・年ごとの数 3 件を読み込みました"
        "（config/stat_series.csv・stat_points.csv）"
    ) in r.output


def test_db_init_exits_2_on_a_bad_ledger(cli_env, cfg):
    write_csv(cfg / SERIES_FILE, SERIES_COLUMNS, [srow(series_id="Bad Id")])
    r = CliRunner().invoke(app, ["db", "init", "--settings", str(cli_env)])
    assert r.exit_code == 2
