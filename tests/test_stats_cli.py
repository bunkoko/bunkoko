"""koten stats meta・fetch・list。実際の e-Stat は呼ばず MockTransport で動かす。"""

from __future__ import annotations

import csv
import json
import re
from types import SimpleNamespace

import httpx
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
    read_point_rows,
)
from tests.test_estat import SEL, SID, cls, data_json, item, meta_json, v

runner = CliRunner()
SECRET = "0123456789abcdef0123456789abcdef01234567"
OTHER_SID = "0009999999"
TIMES = [item(f"{y}000000", f"{y}年") for y in (2019, 2020, 2021, 2022)]
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


def estat_server(seen):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        q = req.url.params
        assert q.get("appId") == SECRET
        if q.get("statsDataId") == OTHER_SID:
            return httpx.Response(403, text="forbidden")
        if req.url.path.endswith("getMetaInfo"):
            return httpx.Response(200, json=meta_json(CLASSES))
        return httpx.Response(200, json=data_json(VALUES, classes=CLASSES))

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
    client = httpx.Client(transport=httpx.MockTransport(estat_server(seen)))
    real = cli._make_fetcher
    holder = SimpleNamespace(client=client)
    monkeypatch.setattr(
        cli, "_make_fetcher", lambda s, raw_dir, client=None: real(s, raw_dir, client=holder.client)
    )
    return SimpleNamespace(
        root=root, config=cfg, docs=root / "docs" / "estat", settings=str(settings),
        seen=seen, tmp=tmp_path,
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
    rec = json.loads((env.docs / f"{SID}.json").read_text("utf-8"))
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
    assert "オーナーの確かめが外れる" not in r.output
    owner = srow(checked_by=HUMAN_CHECKER, checked_at="2026-11-01", checked_sha="x")
    write_csv(env.config / SERIES_FILE, SERIES_COLUMNS, [owner])
    r = stats(env, "fetch", "--check")
    assert "オーナーの確かめが外れる" in r.output
    # 同じ数なら言わない
    write_csv(env.config / POINTS_FILE, POINTS_COLUMNS, [prow("2019", "10")])
    r = stats(env, "fetch", "--check")
    assert "オーナーの確かめが外れる" not in r.output


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
    text = (env.docs / f"{SID}.json").read_text("utf-8")
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


def test_secret_pattern_sanity():
    assert re.fullmatch(r"[0-9a-f]{40}", SECRET)
