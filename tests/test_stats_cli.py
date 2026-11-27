"""koten stats meta・fetch・list。実際の e-Stat は呼ばず MockTransport で動かす。"""

from __future__ import annotations

import csv
import io
import json
import re
from types import SimpleNamespace

import httpx
import openpyxl
import pytest
from typer.testing import CliRunner

import koten.cli as cli
from koten.cli import app
from koten.db import AI_CHECKER, HUMAN_CHECKER
from koten.stats.series import (
    POINTS_COLUMNS,
    POINTS_FILE,
    SERIES_COLUMNS,
    SERIES_FILE,
    check_sha,
    load_meta,
    parse_points,
    parse_series,
    read_point_rows,
    read_series_rows,
)
from tests.test_estat import SEL, SID, cls, data_json, item, meta_json, table_inf, v

runner = CliRunner()
SECRET = "0123456789abcdef0123456789abcdef01234567"
OTHER_SID = "0009999999"
TIMES = [item(f"{y}000000", f"{y}年") for y in (2019, 2020, 2021, 2022, 2023)]
CLASSES = [
    cls("tab", "表章項目", [item("01", "割合", "%")]),
    cls("cat01", "年齢", [item("1", "総数"), item("2", "20～29歳")]),
    cls("area", "地域", [item("00000", "全国")]),
    cls("time", "時間軸(年次)", TIMES),
]
VALUES = [v(2019, "10"), v(2021, "12"), v(2022, "13")]  # 2020 が抜けている


def srow(**kw):
    base = {c: "" for c in SERIES_COLUMNS}
    base.update(
        series_id="age_rate",
        themes="仕事",
        label="20代の割合",
        unit="%",
        survey="就業構造基本調査",
        publisher="総務省",
        table_title="年齢別の割合",
        source_kind="estat_api",
        period_kind="年",
        selector=json.dumps(SEL, ensure_ascii=False),
        url="https://www.e-stat.go.jp/stat-search/files?toukei=1",
    )
    base.update(kw)
    return base


def prow(period, value="", mark="", **kw):
    base = {c: "" for c in POINTS_COLUMNS}
    base.update(series_id="age_rate", period=period, value=value, mark=mark)
    base.update(kw)
    return base


def write_csv(path, cols, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def estat_server(seen, state):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if "file-download" in req.url.path:
            body = state["files"].get(req.url.params["statInfId"])
            return httpx.Response(404 if body is None else 200, content=body or b"")
        q = req.url.params
        assert q.get("appId") == SECRET
        if q.get("statsDataId") == OTHER_SID:
            if state.get("html"):
                return httpx.Response(200, text="<html>メンテナンス中</html>")
            return httpx.Response(403, text="forbidden")
        table = table_inf(TITLE={"@no": "410101", "$": state["title"]})
        if req.url.path.endswith("getMetaInfo"):
            return httpx.Response(200, json=meta_json(CLASSES, table=table))
        vals = [{**x, "@cat01": q.get("cdCat01", "2")} for x in state["values"]]
        body = data_json(vals, classes=CLASSES)
        body["GET_STATS_DATA"]["STATISTICAL_DATA"]["TABLE_INF"] = table
        return httpx.Response(200, json=body)

    return handler


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    cfg = root / "config"
    cfg.mkdir(parents=True)
    (root / "docs" / "estat").mkdir(parents=True)
    (cfg / "sources.yaml").write_text("{}\n", encoding="utf-8")
    (cfg / "themes.yaml").write_text("themes:\n  - 仕事\n  - 孤独\n", encoding="utf-8")
    (cfg / "blocked_keywords.yaml").write_text(
        "categories:\n  医療:\n    - うつ病\n", encoding="utf-8"
    )
    write_csv(cfg / "stats.csv", ["stat_id", "theme", "label", "value", "url"], [])
    write_csv(cfg / SERIES_FILE, SERIES_COLUMNS, [srow()])
    write_csv(cfg / POINTS_FILE, POINTS_COLUMNS, [])
    settings = cfg / "settings.toml"
    settings.write_text(
        f'[paths]\nhdd_root = "{tmp_path / "nohdd" / "koten"}"\n'
        f'ssd_root = "{tmp_path / "ssd"}"\n\n[rate_limit]\nper_host_per_sec = 1000\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("KOTEN_CONTACT", "test@koten.invalid")
    monkeypatch.delenv("KOTEN_ESTAT_APPID", raising=False)
    monkeypatch.setattr("koten.config.REPO_ROOT", root)  # data/estat-cache を tmp に
    seen: list[httpx.Request] = []
    state = {"values": list(VALUES), "title": "年齢別の割合", "files": {}}
    client = httpx.Client(transport=httpx.MockTransport(estat_server(seen, state)))
    real = cli._make_fetcher
    holder = SimpleNamespace(client=client)
    monkeypatch.setattr(
        cli, "_make_fetcher", lambda s, raw_dir, client=None: real(s, raw_dir, client=holder.client)
    )
    return SimpleNamespace(
        root=root, config=cfg, docs=root / "docs" / "estat", settings=str(settings),
        seen=seen, tmp=tmp_path, state=state,
    )  # fmt: skip


def stats(env, *args):
    return runner.invoke(app, ["stats", *args, "--settings", env.settings])


def tree_text(env):
    return "\n".join(
        p.read_bytes().decode("utf-8", "replace") for p in env.tmp.rglob("*") if p.is_file()
    )


def test_fetch_skips_api_series_without_app_id(env):
    r = stats(env, "fetch", "--check")
    assert "e-Stat の ID が無い" in r.output and r.exit_code == 0
    assert env.seen == []


def test_fetch_writes_points_and_never_prints_the_id(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    r = stats(env, "fetch")
    assert r.exit_code == 0, r.output
    assert SECRET not in r.output
    assert (
        "3 点" in r.output and "抜けた年 2020" in r.output and "最新の数のある年 2022" in r.output
    )
    assert (
        "uv run koten db init" in r.output
        and "git add config/stat_points.csv docs/estat" in r.output
    )
    rows = read_point_rows(env.config)
    assert [x["period"] for x in rows] == ["2019", "2021", "2022"]
    rec = json.loads((env.docs / "age_rate.json").read_text("utf-8"))
    assert [c["item"] for c in rec["chosen"]] == ["割合", "20～29歳", "全国"]
    assert SECRET not in tree_text(env)  # マニフェスト・キャッシュ・CSV・docs/estat のどこにも無い
    assert env.seen and all(req.url.params["appId"] == SECRET for req in env.seen)


def test_check_writes_nothing(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    r = stats(env, "fetch", "--check")
    assert r.exit_code == 0 and "3 点" in r.output
    assert read_point_rows(env.config) == [] and not list(env.docs.glob("*.json"))
    assert "db init" not in r.output


def test_unknown_series_exits_2(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    r = stats(env, "fetch", "--series", "nope")
    assert r.exit_code == 2 and "nope" in r.output


def test_403_prints_hint_and_other_series_still_run(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    bad = srow(series_id="bad_rate", selector=json.dumps({**SEL, "statsDataId": OTHER_SID}))
    write_csv(env.config / SERIES_FILE, SERIES_COLUMNS, [srow(), bad])
    r = stats(env, "fetch")
    assert r.exit_code == 1
    assert "bad_rate" in r.output and "ID が違うか、使えなくなっている" in r.output
    assert SECRET not in r.output and SECRET not in tree_text(env)
    assert [x["series_id"] for x in read_point_rows(env.config)] == ["age_rate"] * 3


def test_changed_values_report_and_owner_warning(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    write_csv(env.config / POINTS_FILE, POINTS_COLUMNS, [prow("2019", "9"), prow("2021", "12")])
    r = stats(env, "fetch", "--check")
    assert "2019: 9 → 10" in r.output and "2021:" not in r.output.split("前から")[1]
    assert "オーナーの確かめが外れる" not in r.output  # 台帳の確かめがオーナーでないので出さない
    assert "増えた年：2022" in r.output and "消えた年" not in r.output
    assert read_point_rows(env.config)[0]["value"] == "9"  # --check は書かない


def owner_checked(env):
    """今の取り方で台帳に書いたあと、その数・表の情報に対してオーナーが確かめた形にする。"""
    assert stats(env, "fetch").exit_code == 0
    s = parse_series(read_series_rows(env.config))[0]
    sha = check_sha(s, parse_points(read_point_rows(env.config)), load_meta(env.docs, "age_rate"))
    row = srow(checked_by=HUMAN_CHECKER, checked_at="2026-11-01", checked_sha=sha)
    write_csv(env.config / SERIES_FILE, SERIES_COLUMNS, [row])


def test_owner_note_follows_the_real_lapse_rule(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    owner_checked(env)
    r = stats(env, "fetch", "--check", "--refresh")  # 何も変わらない
    assert r.exit_code == 0 and "オーナーの確かめが外れる" not in r.output
    env.state["values"] = [*VALUES, v(2023, "14")]  # 新しい年だけ
    r = stats(env, "fetch", "--check", "--refresh")
    assert "オーナーの確かめが外れる" in r.output and "増えた年：2023" in r.output
    assert "前から変わった数" not in r.output
    env.state["values"] = VALUES[:2]  # 年が消える
    r = stats(env, "fetch", "--check", "--refresh")
    assert "消えた年：2022" in r.output and "オーナーの確かめが外れる" in r.output


def test_owner_note_when_only_the_table_title_changes(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    owner_checked(env)
    env.state["title"] = "新しい題"
    r = stats(env, "fetch", "--check", "--refresh")
    assert "オーナーの確かめが外れる" in r.output and "前から変わった数" not in r.output
    assert "増えた年" not in r.output


def served_files(env):
    csv_body = "表題,,,\n,2021年,2022年,2023年\n孤独,39.5,40.0,40.1\n".encode()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "表1"
    ws.append(["", 2021, 2022])
    ws.append(["x", 1.5, "-"])
    buf = io.BytesIO()
    wb.save(buf)
    env.state["files"] = {"000001": csv_body, "000002": buf.getvalue()}
    csv_part = {
        "statInfId": "000001", "fileKind": "0", "updated": "2026-01-01", "sheet": "",
        "values": "B3:D3", "periods_cells": "B2:D2", "label_cells": ["A3"],
    }  # fmt: skip
    xlsx_part = {
        "statInfId": "000002", "fileKind": "0", "updated": "2026-01-01", "sheet": "表1",
        "values": "B2:C2", "periods_cells": "B1:C1",
    }  # fmt: skip
    rows = [
        srow(series_id=sid, source_kind="estat_file", selector=json.dumps({"parts": [part]}))
        for sid, part in (("csv_rate", csv_part), ("xlsx_rate", xlsx_part))
    ]
    write_csv(env.config / SERIES_FILE, SERIES_COLUMNS, rows)


def manifest_times(env):
    path = env.root / "data" / "estat-cache" / "manifest.jsonl"
    entries = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
    return {re.search(r"statInfId=(\d+)", e["url"]).group(1): e["fetched_at"] for e in entries}


def test_file_series_end_to_end_with_cache_and_refresh(env):  # appId は無くてよい
    served_files(env)
    r = stats(env, "fetch")
    assert r.exit_code == 0, r.output
    assert (
        "csv_rate：3 点" in r.output and "xlsx_rate：2 点" in r.output and "mark 2022:-" in r.output
    )
    rows = read_point_rows(env.config)
    assert [(x["series_id"], x["period"], x["value"], x["mark"]) for x in rows] == [
        ("csv_rate", "2021", "39.5", ""), ("csv_rate", "2022", "40.0", ""),
        ("csv_rate", "2023", "40.1", ""), ("xlsx_rate", "2021", "1.5", ""),
        ("xlsx_rate", "2022", "", "-"),
    ]  # fmt: skip
    times = manifest_times(env)
    for x in rows:
        assert x["fetched_at"] == times["000001" if x["series_id"] == "csv_rate" else "000002"]
    assert len(env.seen) == 2
    r = stats(env, "fetch")  # 取り直さない：要求は増えず、取った日時も変わらない
    assert len(env.seen) == 2 and manifest_times(env) == times
    assert {x["fetched_at"] for x in read_point_rows(env.config)} == set(times.values())
    r = stats(env, "fetch", "--refresh")
    assert r.exit_code == 0 and len(env.seen) == 4
    assert manifest_times(env) == times  # 中身が同じなら日時は前のまま


def test_drafts_are_check_only(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    draft = {"series": [{**srow(series_id="draft_rate"), "themes": ["仕事"], "selector": SEL}]}
    path = env.tmp / "draft.json"
    path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    before = (env.config / POINTS_FILE).read_bytes()
    r = stats(env, "fetch", "--drafts", str(path))
    assert r.exit_code == 0, r.output
    assert "draft_rate" in r.output and "台帳には書かない" in r.output
    assert (env.config / POINTS_FILE).read_bytes() == before
    assert not list(env.docs.glob("*.json"))


def test_meta_writes_selector_into_drafts_and_chosen_items_only(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    item_ = {
        "series_id": "age_rate", "source_kind": "estat_api", "stats_data_id": SID,
        "selector_names": {"年齢": "20～29歳"},
    }  # fmt: skip
    skipped = {"series_id": "file_rate", "source_kind": "estat_file"}
    path = env.tmp / "draft.json"
    path.write_text(json.dumps({"series": [item_, skipped]}, ensure_ascii=False), "utf-8")
    r = stats(env, "meta", "--drafts", str(path))
    assert r.exit_code == 0, r.output
    data = json.loads(path.read_text("utf-8"))
    assert data["series"][0]["selector"] == SEL and "selector" not in data["series"][1]
    text = (env.docs / "age_rate.json").read_text("utf-8")
    assert "20～29歳" in text and "総数" not in text and text.endswith("\n")
    assert "git add docs/estat docs/research" in r.output
    assert SECRET not in r.output and SECRET not in tree_text(env)


def test_meta_failures_continue_and_exit_1(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    bad = {"series_id": "bad_rate", "source_kind": "estat_api", "stats_data_id": OTHER_SID,
           "selector_names": {"年齢": "20～29歳"}}  # fmt: skip
    wrong = {"series_id": "wrong_rate", "source_kind": "estat_api", "stats_data_id": SID,
             "selector_names": {"年齢": "30代"}}  # fmt: skip
    good = {"series_id": "age_rate", "source_kind": "estat_api",
            "selector": {"statsDataId": SID}, "selector_names": {"年齢": "20～29歳"}}  # fmt: skip
    path = env.tmp / "draft.json"
    path.write_text(json.dumps({"series": [bad, wrong, good]}, ensure_ascii=False), "utf-8")
    r = stats(env, "meta", "--drafts", str(path))
    assert r.exit_code == 1
    assert "bad_rate" in r.output and "使えなくなっている" in r.output
    assert "wrong_rate" in r.output and "総数" in r.output  # 候補の名前
    assert json.loads(path.read_text("utf-8"))["series"][2]["selector"] == SEL


def test_meta_without_app_id_exits_2(env):
    path = env.tmp / "draft.json"
    path.write_text('{"series": []}', encoding="utf-8")
    r = stats(env, "meta", "--drafts", str(path))
    assert r.exit_code == 2 and "KOTEN_ESTAT_APPID" in r.output


def test_list_filters(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    r = stats(env, "list")
    assert r.exit_code == 0 and "db init" in r.output  # まだ SSD に無い
    assert stats(env, "fetch").exit_code == 0
    write_csv(env.config / SERIES_FILE, SERIES_COLUMNS, [srow(latest_known="2023")])
    r = runner.invoke(app, ["db", "init", "--settings", env.settings])
    assert r.exit_code == 0, r.output
    r = stats(env, "list")
    assert "age_rate" in r.output and "20代の割合" in r.output and "3 点" in r.output
    assert "最新 2022" in r.output and "確かめ 未" in r.output and "[stale]" in r.output
    assert "age_rate" in stats(env, "list", "--theme", "仕事").output
    assert "age_rate" not in stats(env, "list", "--theme", "孤独").output
    assert "age_rate" in stats(env, "list", "--stale").output
    assert "age_rate" in stats(env, "list", "--unchecked").output
    write_csv(
        env.config / SERIES_FILE,
        SERIES_COLUMNS,
        [srow(checked_by=AI_CHECKER, checked_at="2026-11-01", checked_sha="x")],
    )
    r = runner.invoke(app, ["db", "init", "--settings", env.settings])
    assert r.exit_code == 0, r.output
    out = stats(env, "list").output
    assert "確かめ AI" in out and "[stale]" not in out
    assert "age_rate" not in stats(env, "list", "--stale").output


def test_estat_cache_falls_back_when_hdd_is_missing(env):
    s = cli._settings(env.settings)
    assert cli._estat_cache(s) == env.root / "data" / "estat-cache"
    s.hdd_root.mkdir(parents=True)
    assert cli._estat_cache(s) == s.hdd_root / "raw" / "estat"


def test_crash_never_shows_locals():
    assert app.pretty_exceptions_show_locals is False


def two_series(env, **kw):
    other = srow(
        series_id="all_rate", label="全体の割合", selector=json.dumps({**SEL, "cdCat01": "1"})
    )
    write_csv(env.config / SERIES_FILE, SERIES_COLUMNS, [srow(**kw), other])


def test_failed_file_series_does_not_stop_the_others(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    served_files(env)
    env.state["files"].pop("000001")  # csv_rate のファイルは 404
    csv_row = next(r for r in read_series_rows(env.config) if r["series_id"] == "csv_rate")
    write_csv(env.config / SERIES_FILE, SERIES_COLUMNS, [csv_row, srow()])
    write_csv(env.config / POINTS_FILE, POINTS_COLUMNS, [prow("2020", "7", series_id="csv_rate")])
    r = stats(env, "fetch")
    assert r.exit_code == 1 and "csv_rate" in r.output and "404" in r.output
    rows = read_point_rows(env.config)
    assert [(x["series_id"], x["period"]) for x in rows] == [
        ("age_rate", "2019"), ("age_rate", "2021"), ("age_rate", "2022"), ("csv_rate", "2020"),
    ]  # fmt: skip
    assert (env.docs / "age_rate.json").exists()  # 点が書けた系列の表の情報だけ
    assert SECRET not in r.output and SECRET not in tree_text(env)


def test_non_json_answer_is_a_series_failure(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    env.state["html"] = True
    bad = srow(series_id="bad_rate", selector=json.dumps({**SEL, "statsDataId": OTHER_SID}))
    write_csv(env.config / SERIES_FILE, SERIES_COLUMNS, [bad, srow()])
    r = stats(env, "fetch")
    assert r.exit_code == 1 and "bad_rate" in r.output
    assert [x["series_id"] for x in read_point_rows(env.config)] == ["age_rate"] * 3
    assert not (env.docs / "bad_rate.json").exists()


def test_nothing_is_written_when_check_only_even_for_meta(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    assert stats(env, "fetch", "--check", "--refresh").exit_code == 0
    assert not list(env.docs.glob("*.json"))


def test_same_table_two_series_keep_their_own_meta_and_owner_check(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    two_series(env)
    assert stats(env, "fetch").exit_code == 0
    a = json.loads((env.docs / "age_rate.json").read_text("utf-8"))
    b = json.loads((env.docs / "all_rate.json").read_text("utf-8"))
    assert [c["item"] for c in a["chosen"]] == ["割合", "20～29歳", "全国"]
    assert [c["item"] for c in b["chosen"]] == ["割合", "総数", "全国"]
    rows = []
    for s in parse_series(read_series_rows(env.config)):
        mine = [r for r in read_point_rows(env.config) if r["series_id"] == s.series_id]
        sha = check_sha(s, parse_points(mine), load_meta(env.docs, s.series_id))
        rows.append(
            srow(
                series_id=s.series_id,
                label=s.label,
                selector=json.dumps(s.selector),
                checked_by=HUMAN_CHECKER,
                checked_at="2026-11-01",
                checked_sha=sha,
            )
        )
    write_csv(env.config / SERIES_FILE, SERIES_COLUMNS, rows)
    assert runner.invoke(app, ["db", "init", "--settings", env.settings]).exit_code == 0
    assert stats(env, "list").output.count("確かめ オーナー") == 2  # 表の情報と選びが合っている
    before_b = (env.docs / "all_rate.json").read_bytes()
    r = stats(env, "fetch", "--series", "age_rate", "--refresh")  # 片方だけ取り直す
    assert r.exit_code == 0 and "all_rate" not in r.output
    assert "オーナーの確かめが外れる" not in r.output
    assert (env.docs / "all_rate.json").read_bytes() == before_b
    r = stats(env, "fetch", "--check")  # 両方とも、確かめは残っている
    assert r.exit_code == 0 and "オーナーの確かめが外れる" not in r.output
    assert runner.invoke(app, ["db", "init", "--settings", env.settings]).exit_code == 0
    assert stats(env, "list").output.count("確かめ オーナー") == 2  # 取り直したあとも


def test_series_option_narrows_the_run(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    two_series(env)
    write_csv(env.config / POINTS_FILE, POINTS_COLUMNS, [prow("2019", "9", series_id="age_rate")])
    r = stats(env, "fetch", "--series", "all_rate")
    assert r.exit_code == 0 and "all_rate：" in r.output and "age_rate：" not in r.output
    rows = read_point_rows(env.config)
    assert [(x["series_id"], x["value"]) for x in rows if x["series_id"] == "age_rate"] == [
        ("age_rate", "9")
    ]  # 頼まなかった系列は触らない
    assert len([x for x in rows if x["series_id"] == "all_rate"]) == 3
    assert not (env.docs / "age_rate.json").exists()


def test_empty_message_names_the_drafts_file(env):
    path = env.tmp / "empty.json"
    path.write_text('{"series": []}', encoding="utf-8")
    r = stats(env, "fetch", "--drafts", str(path))
    assert "empty.json" in r.output and SERIES_FILE not in r.output


def test_meta_bad_items_are_reported_and_skipped(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    names = {"年齢": "20～29歳"}
    items = [
        {"source_kind": "estat_api", "stats_data_id": SID, "selector_names": names},
        {"series_id": "../evil", "source_kind": "estat_api", "stats_data_id": SID,
         "selector_names": names},
        {"series_id": "str_sel", "source_kind": "estat_api", "selector": "oops",
         "selector_names": names},
        {"series_id": "list_names", "source_kind": "estat_api", "stats_data_id": SID,
         "selector_names": ["年齢"]},
        {"series_id": "age_rate", "source_kind": "estat_api", "stats_data_id": SID,
         "selector_names": names},
    ]  # fmt: skip
    path = env.tmp / "draft.json"
    path.write_text(json.dumps({"series": items}, ensure_ascii=False), encoding="utf-8")
    r = stats(env, "meta", "--drafts", str(path))
    assert r.exit_code == 1
    for name in ("series_id なし", "../evil", "str_sel", "list_names"):
        assert name in r.output
    assert [p.name for p in env.docs.glob("*.json")] == ["age_rate.json"]
    assert not list(env.root.rglob("evil*")) and not list(env.docs.glob("*.tmp"))
    assert json.loads(path.read_text("utf-8"))["series"][4]["selector"] == SEL


def test_list_shows_a_lapsed_owner_check(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    assert stats(env, "fetch").exit_code == 0
    owner = srow(checked_by=HUMAN_CHECKER, checked_at="2026-11-01", checked_sha="old")
    write_csv(env.config / SERIES_FILE, SERIES_COLUMNS, [owner])
    assert runner.invoke(app, ["db", "init", "--settings", env.settings]).exit_code == 0
    out = stats(env, "list").output
    assert "確かめが外れた" in out and "確かめ 未" not in out and "確かめ オーナー" not in out
    out = stats(env, "list", "--theme", "孤独").output
    assert "条件に合う系列がありません" in out and "db init" not in out


def test_failure_text_names_the_exception_and_handles_empty_messages():
    assert cli._estat_failure("x", httpx.HTTPError("")) == "x：HTTPError"
    assert cli._estat_failure("x", httpx.ReadTimeout("遅い")) == "x：ReadTimeout：遅い"
    assert cli._estat_failure("x", ValueError("")) == "x：ValueError"
    assert cli._estat_failure("x", cli._ManifestError("記録が壊れた")) == "x：記録が壊れた"


def test_http_failures_carry_the_exception_name(env, monkeypatch):
    served_files(env)
    env.state["files"].pop("000001")
    r = stats(env, "fetch", "--series", "csv_rate")
    assert r.exit_code == 1 and "csv_rate：HTTPStatusError：" in r.output


def test_broken_manifest_is_reported_as_local(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    cache = env.root / "data" / "estat-cache"
    cache.mkdir(parents=True)
    (cache / "manifest.jsonl").write_text("{broken\n", encoding="utf-8")
    r = stats(env, "fetch", "--check")
    assert r.exit_code == 1 and "manifest.jsonl" in r.output and "取得の記録" in r.output
    assert "JSONDecodeError：" not in r.output


def _meta_draft(env):
    d = {"series": [{"series_id": "age_rate", "source_kind": "estat_api", "stats_data_id": SID,
                     "selector_names": {"年齢": "20～29歳"}}]}  # fmt: skip
    path = env.tmp / "draft.json"
    path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return path


def test_meta_refuses_a_selection_different_from_the_ledger_row(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    for moved in (
        {**SEL, "statsDataId": "0001112223"},  # 別の表
        {**SEL, "cdCat01": "1"},  # 同じ表の別の選び方
    ):
        write_csv(env.config / SERIES_FILE, SERIES_COLUMNS, [srow(selector=json.dumps(moved))])
        path = _meta_draft(env)
        before = path.read_text("utf-8")
        r = stats(env, "meta", "--drafts", str(path))
        assert r.exit_code == 1, r.output
        assert "台帳の age_rate と選び方が違う。新しい series_id で下書きする" in r.output
        assert not (env.docs / "age_rate.json").exists()  # 表の情報は書き換えない
        assert "selector" not in json.loads(path.read_text("utf-8"))["series"][0]
        assert json.loads(path.read_text("utf-8")) == json.loads(before)


def test_meta_writes_when_the_selection_equals_the_ledger_row(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    path = _meta_draft(env)
    r = stats(env, "meta", "--drafts", str(path))  # 台帳の行は SEL と同じ
    assert r.exit_code == 0, r.output
    assert (env.docs / "age_rate.json").exists()
    assert json.loads(path.read_text("utf-8"))["series"][0]["selector"] == SEL


def test_meta_with_non_string_name_value_is_an_item_failure(env, monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    d = {"series": [{"series_id": "age_rate", "source_kind": "estat_api", "stats_data_id": SID,
                     "selector_names": {"年齢": 20}}]}  # fmt: skip
    path = env.tmp / "draft.json"
    path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    r = stats(env, "meta", "--drafts", str(path))
    assert r.exit_code == 1 and "age_rate" in r.output and "文字列" in r.output


@pytest.mark.parametrize("body", ["これはJSONでない", "[1, 2]", '{"series": [1]}'])
def test_meta_refuses_a_bad_drafts_file(env, monkeypatch, body):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    path = env.tmp / "draft.json"
    path.write_text(body, encoding="utf-8")
    r = stats(env, "meta", "--drafts", str(path))
    assert r.exit_code == 2 and "下書き" in r.output and "Traceback" not in r.output
