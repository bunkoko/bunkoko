"""統計と研究の下書きを、スマホで確かめて台帳に入れる（koten.sources.check）。"""

from __future__ import annotations

import csv
import json
import shutil
import time

import pytest
from typer.testing import CliRunner

from koten.cli import app
from koten.config import DEFAULT_CONFIG_DIR
from koten.findings import read_findings
from koten.sources.check import (
    SourcesError,
    build_page,
    finding_key,
    lead_key,
    merge_checks,
    page_items,
    stat_key,
)

STAT = {
    "stat_id": "anzen_r6_amount",
    "theme": "仕事の量",
    "label": "強い不安・悩み・ストレスがある労働者のうち、その内容に「仕事の量」を挙げた人の割合",
    "value": "43.2",
    "unit": "%",
    "survey": "労働安全衛生調査（実態調査）",
    "year": "2024",
    "url": "https://www.mhlw.go.jp/toukei/list/r06-46-50b.html",
    "publisher": "厚生労働省",
    "published": "2025-07-30",
    "where": "概況 第17表",
    "evidence": "仕事の量 43.2",
    "trend": "増加",
    "trend_note": "令和4年→令和6年で増えた（同じ設問）",
    "replaces": "rodo_r7_amount",
}
FINDING = {
    "finding_id": "FN-lonely-2021",
    "themes": "孤独",
    "claim_ja": "人との交流が少ない人ほど、孤独を感じやすい傾向がありました",
    "limits_ja": "ある時点の調査で、交流が孤独を生むかどうかまでは分かっていません",
    "effect_ja": "小さい",
    "design": "survey",
    "population_ja": "イギリスの成人5万人",
    "country": "UK",
    "affiliation_ja": "マンチェスター大学",
    "authors_ja": "",
    "authors": "Smith, J.",
    "year": "2021",
    "title": "Loneliness",
    "journal": "PLOS ONE",
    "volume": "16",
    "issue": "3",
    "pages": "e1",
    "doi": "10.1371/journal.pone.0000001",
    "url": "",
    "license": "CC_BY",
    "replication": "not_tested",
    "replication_note": "",
    "retraction_checked_at": "",
    "verified_from": "",
    "checked_by": "",
    "checked_at": "",
    "status": "draft",
    "note": "",
    "license_evidence": "記事のページに CC BY 4.0",
}
PRIVATE = {
    "lead_id": "persol_growth",
    "kind": "private_survey",
    "theme": "学び・成長",
    "title": "働く1万人の成長実態調査",
    "publisher": "パーソル総合研究所",
    "date": "2025",
    "url": "https://rc.persol-group.co.jp/x",
    "terms_url": "https://rc.persol-group.co.jp/terms",
}
PAID = {
    "lead_id": "paid-envy",
    "kind": "paid_paper",
    "theme": "嫉妬・比較",
    "title": "Envy at work",
    "publisher": "Journal of X",
    "authors": "Doe, A.",
    "year": "2019",
    "journal": "Journal of X",
    "doi": "10.1000/envy",
    "url": "",
}


def _drafts():
    return {"made": "2026-11-21", "stats": [STAT], "findings": [FINDING], "leads": [PRIVATE, PAID]}


@pytest.fixture
def config_dir(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    shutil.copy(DEFAULT_CONFIG_DIR / "themes.yaml", d / "themes.yaml")
    shutil.copy(DEFAULT_CONFIG_DIR / "blocked_keywords.yaml", d / "blocked_keywords.yaml")
    with (d / "stats.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(
            "stat_id,theme,label,value,unit,survey,year,url,trend,trend_note,status".split(",")
        )
        w.writerow(
            [
                "rodo_r7_amount",
                "仕事の量",
                "古い",
                "39.5",
                "%",
                "転載",
                "2025",
                "https://x.jp/",
                "",
                "",
                "",
            ]
        )
        w.writerow(
            ["keep_me", "孤独", "残る", "1", "%", "調査", "2025", "https://y.jp/", "", "", ""]
        )
    return d


def _at(day="2026-11-21"):
    return int(time.mktime(time.strptime(day + " 12:00", "%Y-%m-%d %H:%M")) * 1000)


def _stats(d):
    with (d / "stats.csv").open(encoding="utf-8") as f:
        return {r["stat_id"]: r for r in csv.DictReader(f)}


def test_page_shows_the_sentence_as_narrated_and_only_bibliography_for_leads():
    items = page_items(_drafts())
    by = {x["kind"]: x for x in items}
    assert by["stat"]["line"].startswith(STAT["label"] + "は43.2%（労働安全衛生調査")
    assert by["finding"]["lines"][0].startswith("マンチェスター大学の調査（2021年）では、")
    assert by["finding"]["lines"][1].startswith("ただ、この研究は、")
    # 民間の調査・有料の論文は、書誌と URL だけ（数字や一文の下書きは無い）
    assert "value" not in by["private_survey"]["f"] and "label" not in by["private_survey"]["f"]
    assert "claim_ja" not in by["paid_paper"]["f"]
    html = build_page({**_drafts(), "stats": [{**STAT, "label": "</script><b>"}]})
    assert "</script><b>" not in html and "\\u003c/script\\u003e" in html


def test_keys_change_when_the_draft_changes():
    assert stat_key(STAT) != stat_key({**STAT, "value": "43.3"})
    assert finding_key(FINDING) != finding_key({**FINDING, "claim_ja": "別の一文"})
    assert lead_key(PRIVATE) == lead_key({**PRIVATE, "why": "理由"})


def test_merge_writes_only_checked_rows(config_dir):
    checks = {
        stat_key(STAT): {"v": "ok", "at": _at(), "fix": {"value": "43.3"}, "trend": False},
        finding_key(FINDING): {"v": "ok", "at": _at(), "read": True, "retr": True},
        lead_key(PRIVATE): {
            "v": "ok",
            "at": _at(),
            "terms": True,
            "entry": {
                "label": "職場で学ぶ機会があると答えた人の割合",
                "value": "40",
                "unit": "%",
                "year": "2025",
                "survey": "パーソル総合研究所「働く1万人の成長実態調査」",
                "url": "https://rc.persol-group.co.jp/x",
            },
        },
        lead_key(PAID): {"v": "ng", "at": _at()},
        "s-unknown": {"v": "ok"},
    }
    res = merge_checks(_drafts(), checks, config_dir)
    assert res.stats_added == ["anzen_r6_amount", "priv_persol_growth"]
    assert res.stats_removed == ["rodo_r7_amount"] and res.rejected == ["paid-envy"]
    assert res.unknown == ["s-unknown"] and res.findings_added == ["FN-lonely-2021"]
    rows = _stats(config_dir)
    assert set(rows) == {"keep_me", "anzen_r6_amount", "priv_persol_growth"}
    new = rows["anzen_r6_amount"]
    # 人が直した数字を使う。増え方は「確かめた」に印が無ければ書かない
    assert (new["value"], new["trend"], new["trend_note"]) == ("43.3", "", "")
    assert (new["where"], new["checked_by"], new["checked_at"]) == (
        "概況 第17表",
        "オーナー",
        "2026-11-21",
    )
    assert rows["priv_persol_growth"]["survey"].startswith("パーソル総合研究所")
    found = read_findings(config_dir)
    assert [f.finding_id for f in found] == ["FN-lonely-2021"] and found[0].usable
    assert found[0].retraction_checked_at == "2026-11-21"


def test_merge_keeps_trend_only_when_ticked(config_dir):
    merge_checks(_drafts(), {stat_key(STAT): {"v": "ok", "at": _at(), "trend": True}}, config_dir)
    assert _stats(config_dir)["anzen_r6_amount"]["trend"] == "増加"


def test_merge_skips_rows_without_the_owner_checks(config_dir):
    checks = {
        finding_key(FINDING): {"v": "ok", "at": _at(), "read": True, "retr": False},
        lead_key(PRIVATE): {
            "v": "ok",
            "at": _at(),
            "terms": False,
            "entry": {"label": "x", "value": "1"},
        },
    }
    res = merge_checks(_drafts(), checks, config_dir)
    assert not res.findings_added and not res.stats_added and len(res.skipped) == 2
    assert not (config_dir / "findings.csv").exists()
    assert set(_stats(config_dir)) == {"rodo_r7_amount", "keep_me"}


def test_paid_paper_takes_the_owner_words_and_stays_copyrighted(config_dir):
    entry = {
        "claim_ja": "人と比べる人ほど、仕事に満足しにくい傾向がありました",
        "limits_ja": "ある会社の人を調べたもので、ほかの職場に当てはまるかは分かっていません",
        "population_ja": "アメリカの会社員300人",
        "design": "survey",
        "effect_ja": "",
        "replication": "not_tested",
        "authors_ja": "ドウら",
    }
    checks = {lead_key(PAID): {"v": "ok", "at": _at(), "read": True, "retr": True, "entry": entry}}
    res = merge_checks(_drafts(), checks, config_dir)
    assert res.findings_added == ["FN-paid-envy"]
    f = read_findings(config_dir)[0]
    assert f.license == "COPYRIGHTED" and f.usable and f.authors == "Doe, A."


def test_merge_writes_nothing_when_a_row_breaks_the_rules(config_dir):
    bad = {**FINDING, "claim_ja": "交流が孤独を必ず減らすことが証明されました"}
    drafts = {**_drafts(), "findings": [bad]}
    checks = {
        stat_key(STAT): {"v": "ok", "at": _at()},
        finding_key(bad): {"v": "ok", "at": _at(), "read": True, "retr": True},
    }
    before = (config_dir / "stats.csv").read_text("utf-8")
    with pytest.raises(SourcesError, match="findings.csv"):
        merge_checks(drafts, checks, config_dir)
    assert (config_dir / "stats.csv").read_text("utf-8") == before
    with pytest.raises(SourcesError, match="themes.yaml"):
        merge_checks(
            {**_drafts(), "stats": [{**STAT, "theme": "仕事"}]},
            {stat_key({**STAT, "theme": "仕事"}): {"v": "ok", "at": _at()}},
            config_dir,
        )


def test_cli_page_and_merge(config_dir, tmp_path):
    drafts = tmp_path / "drafts.json"
    drafts.write_text(json.dumps(_drafts(), ensure_ascii=False), "utf-8")
    out = tmp_path / "page.html"
    res = CliRunner().invoke(app, ["sources", "page", "--drafts", str(drafts), "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "統計 1・研究 1・あなたが読む資料 2 件" in res.output and out.exists()
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps({stat_key(STAT): {"v": "ok", "at": _at()}}), "utf-8")
    # 設定のファイルの隣に sources.yaml があれば、そのフォルダを config として読む
    shutil.copy(DEFAULT_CONFIG_DIR / "sources.yaml", config_dir / "sources.yaml")
    settings = config_dir / "s.toml"
    settings.write_text("[paths]\n", "utf-8")
    res = CliRunner().invoke(
        app,
        [
            "sources",
            "merge",
            "--drafts",
            str(drafts),
            "--from",
            str(checks),
            "--settings",
            str(settings),
        ],
    )
    assert res.exit_code == 0, res.output
    assert "人が確かめた行：統計 1" in res.output and "koten db init" in res.output


def test_accept_rest_adds_ai_checked_rows_but_keeps_human_ones(config_dir):
    other = {**STAT, "stat_id": "anzen_r6_failure", "label": "失敗の割合", "replaces": ""}
    drafts = {**_drafts(), "stats": [STAT, other]}
    # 人が押した行・押していない行・違うを押した行
    checks = {stat_key(STAT): {"v": "ok", "at": _at("2026-11-23")}}
    res = merge_checks(drafts, checks, config_dir, accept_rest=True)
    assert res.stats_added == ["anzen_r6_amount"] and res.stats_ai == ["anzen_r6_failure"]
    assert res.findings_ai == ["FN-lonely-2021"] and not res.findings_added
    rows = _stats(config_dir)
    assert rows["anzen_r6_amount"]["checked_by"] == "オーナー"
    ai = rows["anzen_r6_failure"]
    # AI の行は下書きの日付、増え方は書かない（人が確かめたときだけ）
    assert (ai["checked_by"], ai["checked_at"], ai["trend"]) == (
        "AI（調べ役と見直し役）",
        "2026-11-21",
        "",
    )
    f = read_findings(config_dir)[0]
    assert f.status == "ai_checked" and f.usable and not f.human_verified
    assert f.retraction_checked_at == "2026-11-21"
    # 民間の調査・有料の論文は、押していなければ入れない
    assert "priv_persol_growth" not in rows
    # もう一度 AI で入れ直しても、人が確かめた行は上書きしない
    res = merge_checks(drafts, {}, config_dir, accept_rest=True)
    assert res.stats_ai == ["anzen_r6_failure"]
    assert _stats(config_dir)["anzen_r6_amount"]["checked_by"] == "オーナー"
    # 違うを押した行は入れない
    (config_dir / "stats.csv").write_text(
        "stat_id,theme,label,value,unit,survey,year,url,trend,trend_note,status\n", "utf-8"
    )
    res = merge_checks(drafts, {stat_key(other): {"v": "ng"}}, config_dir, accept_rest=True)
    assert "anzen_r6_failure" not in _stats(config_dir) and res.rejected == ["anzen_r6_failure"]


def test_ai_checked_needs_an_open_license(config_dir):
    from koten.findings import FindingsError

    row = {
        **FINDING,
        "license": "COPYRIGHTED",
        "status": "ai_checked",
        "verified_from": "full_text",
    }
    row.update(checked_by="AI", checked_at="2026-11-21", retraction_checked_at="2026-11-21")
    from koten.findings import COLUMNS
    from koten.sources.check import _csv_text

    (config_dir / "findings.csv").write_text(_csv_text(COLUMNS, [row]), "utf-8")
    with pytest.raises(FindingsError, match="CC BY・CC0"):
        read_findings(config_dir)


def test_todo_page_uses_the_ledger_text_and_the_draft_location(config_dir):
    from koten.sources.check import todo_drafts

    merge_checks(_drafts(), {}, config_dir, accept_rest=True)
    # 台帳の数字を人が直したあとでも、ページは台帳の形（動画で言っている形）を出す
    text = (config_dir / "stats.csv").read_text("utf-8").replace(",43.2,", ",43.1,")
    (config_dir / "stats.csv").write_text(text, "utf-8")
    todo = {"episode_id": "E1", "stats": ["anzen_r6_amount"], "findings": ["FN-lonely-2021"]}
    data = todo_drafts(_drafts(), todo, config_dir)
    s = data["stats"][0]
    assert s["value"] == "43.1" and s["evidence"] == "仕事の量 43.2" and not s["replaces"]
    assert data["findings"][0]["status"] == "ai_checked" and data["made"] == "回 E1"
    with pytest.raises(SourcesError, match="台帳に無い"):
        todo_drafts(_drafts(), {"stats": ["nope"]}, config_dir)
    # 押したら人が確かめた行になる
    page = page_items(data)
    checks = {
        page[0]["k"]: {"v": "ok", "at": _at("2026-11-24")},
        page[1]["k"]: {"v": "ok", "at": _at("2026-11-24"), "read": True, "retr": True},
    }
    res = merge_checks(data, checks, config_dir)
    assert res.stats_added == ["anzen_r6_amount"] and res.findings_added == ["FN-lonely-2021"]
    assert _stats(config_dir)["anzen_r6_amount"]["value"] == "43.1"
    assert read_findings(config_dir)[0].human_verified


def test_unchecked_sources_lists_cited_rows_without_a_human_check():
    from types import SimpleNamespace

    from koten.media.produce import unchecked_sources

    script = {
        "chapters": [
            {
                "chapter_id": "ch1",
                "segments": [
                    {"seg_id": "a", "kind": "stat", "visual": {"ref": "s_ai"}},
                    {"seg_id": "b", "kind": "stat", "visual": {"ref": "s_human"}},
                    {"seg_id": "c", "kind": "narration", "findings": ["FN-ai", "FN-human"]},
                ],
            }
        ]
    }
    data = SimpleNamespace(
        stats={
            "s_ai": {"checked_by": "AI（調べ役と見直し役）"},
            "s_human": {"checked_by": "オーナー"},
            "s_unused": {"checked_by": ""},
        },
        findings={"FN-ai": {"status": "ai_checked"}, "FN-human": {"status": "verified"}},
    )
    assert unchecked_sources(SimpleNamespace(script=script, data=data)) == ["s_ai", "FN-ai"]


# --- 統計の系列（series） ----------------------------------------------------------

from koten.db import AI_CHECKER, HUMAN_CHECKER  # noqa: E402
from koten.sources.check import (  # noqa: E402
    collect_drafts,
    read_drafts,
    series_key,
    series_line,
    todo_drafts,
    with_series_data,
)
from koten.stats.report import series_from_draft  # noqa: E402
from koten.stats.series import (  # noqa: E402
    POINTS_COLUMNS,
    POINTS_FILE,
    SERIES_COLUMNS,
    SERIES_FILE,
    check_sha,
    parse_points,
    read_point_rows,
    read_series_rows,
)

API_META = {
    "stats_data_id": "0003000001",
    "stat_name": "労働力調査",
    "gov_org": "総務省統計局",
    "title": "就業状態別人口",
    "title_no": "1-1",
    "survey_date": "2020",
    "open_date": "2021-02-01",
    "updated_date": "2021-03-01",
    "chosen": [{"class": "区分", "item": "計", "code": "001", "unit": ""}],
    "times": [{"code": "2020000000", "name": "2020年"}],
}
API_SERIES = {
    "series_id": "work_rate",
    "themes": ["仕事の量"],
    "label": "就業している人の割合",
    "unit": "％",
    "survey": "労働力調査",
    "publisher": "総務省統計局",
    "source_kind": "estat_api",
    "period_kind": "年",
    "selector": {"statsDataId": "0003000001", "cdCat01": "001"},
    "selector_names": {"cdCat01": "区分：計"},
    "table_title": "就業状態別人口",
    "url": "https://www.e-stat.go.jp/stat-search/files?toukei=1",
    "latest_known": "",
    "breaks": [],
    "trend": "増加",
    "trend_from": "2019",
    "trend_to": "2021",
    "trend_note": "3年続けて増えた",
    "where": "",
    "note": "",
    "evidence": "表 1-1 の計",
    "check_note": "",
}
FILE_SELECTOR = {
    "parts": [
        {
            "statInfId": "000031000001",
            "fileKind": "0",
            "sheet": "表1",
            "values": "D12:F12",
            "periods_cells": "D5:F5",
            "label_cells": ["A12"],
            "unit_cell": "B3",
        }
    ]
}
FILE_SERIES = {
    **API_SERIES,
    "series_id": "file_rate",
    "label": "ファイルの系列の割合",
    "source_kind": "estat_file",
    "selector": FILE_SELECTOR,
    "selector_names": {},
    "trend": "",
    "trend_from": "",
    "trend_to": "",
    "trend_note": "",
    "table_title": "表1",
}


def _prow(sid="work_rate", period="2019", value="10", **kw):
    row = {c: "" for c in POINTS_COLUMNS}
    row.update(series_id=sid, period=period, value=value, part="0", src_label="計")
    row.update(kw)
    return row


def _write_ledger(config_dir, points, series_rows=None):
    """series_rows を省くと、今の系列の行はそのまま（台帳に無い系列には数が付かない）。"""
    if series_rows is None:
        series_rows = read_series_rows(config_dir) if (config_dir / SERIES_FILE).exists() else []
    for name, cols, rows in (
        (POINTS_FILE, POINTS_COLUMNS, points),
        (SERIES_FILE, SERIES_COLUMNS, series_rows),
    ):
        with (config_dir / name).open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)


def _srow(item=API_SERIES, **kw):
    row = {c: "" for c in SERIES_COLUMNS}
    for c in SERIES_COLUMNS:
        v = item.get(c, "")
        if c == "themes":
            v = ";".join(v)
        elif isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        row[c] = v
    row.update(kw)
    return row


@pytest.fixture
def sconf(config_dir):
    shutil.copy(DEFAULT_CONFIG_DIR / "blocked_keywords.yaml", config_dir / "blocked_keywords.yaml")
    meta_dir = config_dir.parent / "docs" / "estat"
    meta_dir.mkdir(parents=True)
    (meta_dir / "work_rate.json").write_text(json.dumps(API_META, ensure_ascii=False), "utf-8")
    _write_ledger(
        config_dir,
        [_prow(period="2019", value="10"), _prow(period="2020", value="11"),
         _prow(period="2021", value="12")],
        [_srow(API_SERIES, checked_by=AI_CHECKER, checked_at="2026-11-01")],
    )  # fmt: skip
    return config_dir


def _sdrafts(*series):
    return {"made": "2026-11-26", "stats": [], "findings": [], "leads": [], "series": list(series)}


def _skey(config_dir, item=API_SERIES):
    full = with_series_data(_sdrafts(item), config_dir)
    return series_key(full["series"][0])


def _only_ai_row(d):
    """台帳の系列の行が、最初の AI の行のまま（何も書いていない。全部の列を比べる）。"""
    return _series_file(d) == [_srow(API_SERIES, checked_by=AI_CHECKER, checked_at="2026-11-01")]


def _series_file(d):
    return read_series_rows(d)


def test_read_drafts_defaults_series_and_collect_keys_by_series_id(tmp_path):
    p1, p2 = tmp_path / "a.json", tmp_path / "b.json"
    p1.write_text(json.dumps({"made": "2026-11-01", "stats": []}), "utf-8")
    assert read_drafts(p1)["series"] == []
    p1.write_text(json.dumps(_sdrafts(API_SERIES)), "utf-8")
    p2.write_text(json.dumps(_sdrafts({**API_SERIES, "label": "あとの文"})), "utf-8")
    got = collect_drafts([p1, p2])
    assert [x["label"] for x in got["series"]] == ["あとの文"]
    assert collect_drafts([])["series"] == []


def test_with_series_data_adds_points_and_meta_without_touching_the_input(sconf):
    drafts = _sdrafts(API_SERIES, FILE_SERIES)
    full = with_series_data(drafts, sconf)
    assert "points" not in drafts["series"][0]
    api, file_ = full["series"]
    assert [p["period"] for p in api["points"]] == ["2019", "2020", "2021"]
    assert api["meta"]["title"] == "就業状態別人口"
    assert file_["points"] == [] and file_["meta"] is None


def test_series_card_shows_machine_values_and_only_label_is_editable(sconf):
    items = page_items(with_series_data(_sdrafts(API_SERIES), sconf))
    it = items[0]
    assert it["kind"] == "series" and it["id"] == "work_rate"
    assert list(it["f"]) == ["label"]
    assert it["line"] == "就業している人の割合は12％（労働力調査、2021年）"
    assert [(p["period"], p["value"]) for p in it["points"]] == [
        ("2019", "10"), ("2020", "11"), ("2021", "12")
    ]  # fmt: skip
    assert "区分：計" in it["chosen"]
    assert dict(it["source"])["統計名"] == "労働力調査"
    assert it["link"] == "https://www.e-stat.go.jp/dbview?sid=0003000001"
    assert it["trend"] == "増加" and it["trend_note"] == "3年続けて増えた"
    html = build_page(with_series_data(_sdrafts(API_SERIES), sconf))
    body = html.split("function cardSeries")[1].split("\nfunction ")[0]
    assert '"fix.label"' in body and '"fix.value"' not in body and '"fix.unit"' not in body
    assert "fix.year" not in body and "fix.survey" not in body


def test_series_line_is_the_newest_numeric_point(sconf):
    pts = parse_points([_prow(period="2021", value="", mark="-"), _prow(period="2020", value="11")])
    item = {**API_SERIES, "points": [dict(r) for r in [_prow(period="2020", value="11")]]}
    assert series_line(item, pts[1]) == "就業している人の割合は11％（労働力調査、2020年）"
    full = with_series_data(_sdrafts(API_SERIES), sconf)
    assert page_items(full)[0]["line"].endswith("12％（労働力調査、2021年）")


def test_points_table_marks_gaps_marks_and_breaks(sconf):
    _write_ledger(
        sconf,
        [_prow(period="2018", value="9", src_label="計"),
         _prow(period="2020", value="", mark="x", src_label="総数"),
         _prow(period="2021", value="12", src_label="総数")],
    )  # fmt: skip
    pts = page_items(with_series_data(_sdrafts(API_SERIES), sconf))[0]["points"]
    assert [p["period"] for p in pts] == ["2018", "2019", "2020", "2021"]
    assert pts[1].get("gap") is True
    assert pts[2]["mark"] == "x" and pts[2]["brk"] is True and pts[3]["brk"] is False
    assert pts[0]["segment"] == 0 and pts[2]["segment"] == 1


def test_excerpt_provider_is_called_only_for_file_series(sconf):
    seen = []

    def provider(item):
        seen.append(item["series_id"])
        return {"rows": [["a", "b"]], "hl": [[0, 1]], "label_rows": [0]}

    items = page_items(with_series_data(_sdrafts(API_SERIES, FILE_SERIES), sconf), excerpt=provider)
    assert seen == ["file_rate"]
    assert items[0]["excerpt"] is None and items[1]["excerpt"]["hl"] == [[0, 1]]
    assert items[1]["link"].startswith("https://www.e-stat.go.jp/stat-search/file-download")


def test_draft_key_changes_once_points_exist(sconf):
    _write_ledger(sconf, [])
    before = _skey(sconf)
    _write_ledger(sconf, [_prow(period="2020", value="11")])
    after = _skey(sconf)
    assert before != after
    # 数が変われば、また変わる
    _write_ledger(sconf, [_prow(period="2020", value="12")])
    assert _skey(sconf) != after


def test_merge_ok_series_records_owner_and_sha(sconf):
    drafts = _sdrafts(API_SERIES)
    key = _skey(sconf)
    res = merge_checks(
        drafts, {key: {"v": "ok", "at": _at(), "fix": {"label": "働いている人の割合"}}}, sconf
    )
    assert res.series_added == ["work_rate"]
    row = _series_file(sconf)[0]
    assert list(row) == SERIES_COLUMNS
    assert row["checked_by"] == HUMAN_CHECKER and row["checked_at"] == "2026-11-21"
    assert row["label"] == "働いている人の割合" and row["themes"] == "仕事の量"
    assert json.loads(row["selector"]) == API_SERIES["selector"] and row["breaks"] == "[]"
    s = series_from_draft({**API_SERIES, "label": "働いている人の割合"})
    want = check_sha(s, parse_points(read_point_rows(sconf)), API_META)
    assert row["checked_sha"] == want
    # 直さなかったときの鍵・sha とは違う（label の直しを当てたあとの sha）
    plain = check_sha(series_from_draft(API_SERIES), parse_points(read_point_rows(sconf)), API_META)
    assert want != plain


def test_merge_ng_series_is_not_written(sconf):
    # 台帳の系列に「違う」：中身は書き換えず、保留にするだけ（L9）
    res = merge_checks(_sdrafts(API_SERIES), {_skey(sconf): {"v": "ng", "at": _at()}}, sconf)
    assert res.rejected == ["work_rate"] and res.series_added == [] and res.series_ai == []
    assert _series_file(sconf) == [
        _srow(API_SERIES, checked_by=AI_CHECKER, checked_at="2026-11-01", status="保留",
              note="2026-11-21 オーナーが「違う」")
    ]  # fmt: skip


def test_merge_replaces_same_id_keeps_order_and_appends(sconf):
    other = _srow({**FILE_SERIES, "series_id": "zzz_other"})
    old = _srow(API_SERIES, label="古い", checked_by=AI_CHECKER, checked_at="2026-11-01")
    _write_ledger(sconf, [_prow()], [old, other])
    key = _skey(sconf)
    merge_checks(_sdrafts(API_SERIES), {key: {"v": "ok", "at": _at()}}, sconf)
    ids = [r["series_id"] for r in _series_file(sconf)]
    assert ids == ["work_rate", "zzz_other"]  # 同じ場所で入れ替える（後ろに回さない）
    assert _series_file(sconf)[0]["label"] == API_SERIES["label"]
    new = _sdrafts({**FILE_SERIES, "series_id": "work_rate2"})
    assert merge_checks(new, {}, sconf, accept_rest=True).series_ai == ["work_rate2"]
    assert [r["series_id"] for r in _series_file(sconf)][-1] == "work_rate2"  # 新しいのは末尾


def test_accept_rest_series_is_ai_and_keeps_human_rows(sconf):
    file_item = {**FILE_SERIES}
    _write_ledger(sconf, [_prow(), _prow("file_rate", "2020", "5")])
    drafts = _sdrafts(API_SERIES, file_item)
    res = merge_checks(drafts, {}, sconf, accept_rest=True)
    assert sorted(res.series_ai) == ["file_rate", "work_rate"] and res.series_added == []
    rows = {r["series_id"]: r for r in _series_file(sconf)}
    assert rows["work_rate"]["checked_by"] == AI_CHECKER and rows["work_rate"]["checked_sha"] == ""
    assert rows["work_rate"]["trend"] == ""  # AI だけの行には増え方を入れない
    # 人が確かめた行は、AI の行で上書きしない
    human = {
        **rows["work_rate"],
        "checked_by": HUMAN_CHECKER,
        "checked_sha": "abc",
        "label": "人の文",
    }
    others = [r for r in _series_file(sconf) if r["series_id"] != "work_rate"]
    _write_ledger(sconf, [_prow(), _prow("file_rate", "2020", "5")], [human, *others])
    res = merge_checks(drafts, {}, sconf, accept_rest=True)
    assert res.series_ai == ["file_rate"]
    row = next(r for r in _series_file(sconf) if r["series_id"] == "work_rate")
    assert row["label"] == "人の文" and row["checked_by"] == HUMAN_CHECKER


def test_owner_ok_on_a_series_without_points_is_skipped(sconf):
    _write_ledger(sconf, [])
    drafts = _sdrafts(API_SERIES)
    res = merge_checks(drafts, {_skey(sconf): {"v": "ok", "at": _at()}}, sconf)
    assert res.series_added == [] and any(
        "数がまだ無い" in x and "koten stats fetch" in x for x in res.skipped
    )
    assert _only_ai_row(sconf)


def test_series_trend_kept_only_when_ticked_and_verified(sconf):
    key = _skey(sconf)
    merge_checks(_sdrafts(API_SERIES), {key: {"v": "ok", "at": _at(), "trend": False}}, sconf)
    assert _series_file(sconf)[0]["trend"] == ""
    assert _series_file(sconf)[0]["trend_from"] == ""
    merge_checks(_sdrafts(API_SERIES), {key: {"v": "ok", "at": _at(), "trend": True}}, sconf)
    row = _series_file(sconf)[0]
    assert (row["trend"], row["trend_from"], row["trend_to"]) == ("増加", "2019", "2021")
    assert row["trend_note"] == "3年続けて増えた"
    # 数が増え続けていないと、印を付けても入らない
    _write_ledger(sconf, [_prow(period="2019", value="10"), _prow(period="2020", value="9"),
                          _prow(period="2021", value="12")])  # fmt: skip
    assert page_items(with_series_data(_sdrafts(API_SERIES), sconf))[0]["trend"] == ""
    key = _skey(sconf)
    merge_checks(_sdrafts(API_SERIES), {key: {"v": "ok", "at": _at(), "trend": True}}, sconf)
    assert _series_file(sconf)[0]["trend"] == ""


def test_broken_series_writes_nothing_anywhere(sconf):
    stats_before = (sconf / "stats.csv").read_text("utf-8")
    bad = {**API_SERIES, "themes": ["ない主題"]}
    drafts = {**_drafts(), "series": [bad]}
    checks = {
        stat_key(STAT): {"v": "ok", "at": _at()},
        finding_key(FINDING): {"v": "ok", "at": _at(), "read": True, "retr": True},
        _skey(sconf, bad): {"v": "ok", "at": _at()},
    }
    with pytest.raises(SourcesError, match="stat_series.csv"):
        merge_checks(drafts, checks, sconf)
    assert (sconf / "stats.csv").read_text("utf-8") == stats_before
    assert not (sconf / "findings.csv").exists()
    assert _only_ai_row(sconf)
    # 表の情報が無い estat_api の系列も止まる
    (sconf.parent / "docs" / "estat" / "work_rate.json").unlink()
    with pytest.raises(SourcesError, match="表の情報"):
        merge_checks(_sdrafts(API_SERIES), {_skey(sconf): {"v": "ok", "at": _at()}}, sconf)


def test_todo_series_comes_from_the_ledger(sconf):
    _write_ledger(
        sconf,
        [_prow()],
        [_srow(API_SERIES, label="台帳の文", checked_by=AI_CHECKER, checked_at="2026-11-01")],
    )
    drafts = _sdrafts({**API_SERIES, "label": "下書きの文", "evidence": "下書きの写し"})
    out = todo_drafts(drafts, {"episode_id": "E1", "series": ["work_rate"]}, sconf)
    (it,) = page_items(out)
    assert it["kind"] == "series" and it["label"] == "台帳の文" and it["evidence"] == "下書きの写し"
    assert [p["period"] for p in it["points"]] == ["2019"]
    assert it["source"] and out["made"] == "回 E1"
    with pytest.raises(SourcesError, match="台帳に無い行"):
        todo_drafts(drafts, {"series": ["nope"]}, sconf)


def test_cli_page_and_merge_with_series(sconf, tmp_path):
    drafts = tmp_path / "drafts.json"
    drafts.write_text(json.dumps(_sdrafts(API_SERIES), ensure_ascii=False), "utf-8")
    shutil.copy(DEFAULT_CONFIG_DIR / "sources.yaml", sconf / "sources.yaml")
    settings = sconf / "s.toml"
    settings.write_text("[paths]\n", "utf-8")
    out = tmp_path / "page.html"
    res = CliRunner().invoke(
        app,
        [
            "sources",
            "page",
            "--drafts",
            str(drafts),
            "--out",
            str(out),
            "--settings",
            str(settings),
        ],
    )
    assert res.exit_code == 0, res.output
    assert "統計の系列 1" in res.output and "work_rate" in out.read_text("utf-8")
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps({_skey(sconf): {"v": "ok", "at": _at()}}), "utf-8")
    res = CliRunner().invoke(
        app,
        ["sources", "merge", "--drafts", str(drafts), "--from", str(checks),
         "--settings", str(settings)],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert "統計の系列 1" in res.output and "koten db init" in res.output


def test_excerpt_from_grid_marks_value_cells_and_label_rows():
    from koten.sources.check import excerpt_from_grid

    grid = [[f"r{r}c{c}" for c in range(10)] for r in range(30)]
    x = excerpt_from_grid(FILE_SELECTOR["parts"][0], grid)  # values D12:F12, label A12, unit B3
    assert [x["rows"][r][c] for r, c in x["hl"]] == ["r11c3", "r11c4", "r11c5"]
    assert len(x["rows"][0]) == 7 and len(x["rows"]) == 7  # 数の前後 2 列・3 行
    assert all(i != x["hl"][0][0] for i in x["label_rows"])


def test_excerpt_from_grid_marks_every_cell_of_a_vertical_range():
    from koten.sources.check import excerpt_from_grid

    grid = [[f"r{r}c{c}" for c in range(10)] for r in range(30)]
    spec = {
        **FILE_SELECTOR["parts"][0],
        "values": "D5:D9",
        "label_cells": ["A6"],
        "unit_cell": "B2",
    }
    x = excerpt_from_grid(spec, grid)
    assert [x["rows"][r][c] for r, c in x["hl"]] == [f"r{r}c3" for r in range(4, 9)]
    # 数の列の中にある見出しの行（A6）は黄色にしない。窓の中の単位の行（B2）は黄色
    assert x["label_rows"] == [0]


def test_accept_rest_takes_in_a_series_without_points_as_ai(sconf):
    new = {**FILE_SERIES, "series_id": "brand_new"}  # 台帳に無い系列
    res = merge_checks(_sdrafts(new), {}, sconf, accept_rest=True)
    assert res.series_ai == ["brand_new"]
    row = {r["series_id"]: r for r in _series_file(sconf)}["brand_new"]
    assert row["checked_by"] == AI_CHECKER and row["checked_sha"] == ""
    assert read_point_rows(sconf) and "brand_new" not in {
        r["series_id"] for r in read_point_rows(sconf)
    }


def test_series_without_points_loads_into_the_ssd(sconf, tmp_path):
    from koten.db import connect, load_series

    _write_ledger(sconf, [])
    merge_checks(_sdrafts({**API_SERIES, "latest_known": "2023"}), {}, sconf, accept_rest=True)
    conn = connect(tmp_path / "ssd")
    assert load_series(conn, sconf, sconf.parent / "docs" / "estat")[0] == 1
    r = conn.execute("SELECT latest_period, stale FROM stat_series").fetchone()
    assert r["latest_period"] is None and r["stale"] == 1  # 数が無いので、今の数としては使えない


# --- 選び方（selector）の変更・数なしの「合っている」・鍵・抜き出し -----------------------

NEW_SELECTOR = {"parts": [{**FILE_SELECTOR["parts"][0], "values": "D13:F13"}]}


def _file_ledger(config_dir, by=HUMAN_CHECKER):
    """ファイル系列と api 系列が台帳にあり、それぞれ点がある。"""
    _write_ledger(
        config_dir,
        [_prow(), _prow("file_rate", "2019", "5"), _prow("file_rate", "2020", "6")],
        [
            _srow(API_SERIES),
            _srow(FILE_SERIES, checked_by=by, checked_at="2026-11-01"),
        ],
    )


def _changed_item():
    return {**FILE_SERIES, "selector": NEW_SELECTOR}


def test_selector_change_is_never_taken_in_ai_or_owner_row(sconf):
    NOTE = "台帳の file_rate と選び方が違う。新しい series_id で下書きする"
    for by in (HUMAN_CHECKER, AI_CHECKER):
        _file_ledger(sconf, by)
        it = page_items(with_series_data(_sdrafts(_changed_item()), sconf))[0]
        assert it["points"] == [] and it["has_numbers"] is False and it["line"] == ""
        assert it["selector_state"] == "different"
        before = [(sconf / f).read_text("utf-8") for f in (SERIES_FILE, POINTS_FILE)]
        for checks, rest in (({it["k"]: {"v": "ok", "at": _at()}}, False), ({}, True),
                             ({it["k"]: {"v": "ok", "at": _at()}}, True)):  # fmt: skip
            res = merge_checks(_sdrafts(_changed_item()), checks, sconf, accept_rest=rest)
            assert res.series_added == [] and res.series_ai == []
            assert any(NOTE in x and "--accept-rest" not in x for x in res.skipped), res.skipped
            # 行も数もそのまま
            assert [(sconf / f).read_text("utf-8") for f in (SERIES_FILE, POINTS_FILE)] == before
    same = page_items(with_series_data(_sdrafts(FILE_SERIES), sconf))[0]
    assert [p["period"] for p in same["points"]] == ["2019", "2020"] and same["has_numbers"]
    assert same["selector_state"] == "same"


def test_series_not_in_the_ledger_never_shows_stray_points(sconf):
    _write_ledger(sconf, [_prow(), _prow("stray_id", "2020", "9")])
    it = page_items(with_series_data(_sdrafts({**FILE_SERIES, "series_id": "stray_id"}), sconf))[0]
    assert it["points"] == [] and it["has_numbers"] is False and it["selector_state"] == "new"


def test_page_shows_the_instruction_and_refuses_ok_for_a_changed_selector(sconf):
    _file_ledger(sconf)
    html = build_page(with_series_data(_sdrafts(_changed_item()), sconf))
    assert 'it.kind === "series" && it.selector_note' in html
    assert "新しい series_id で下書きする" in html


def test_label_only_change_keeps_the_points(sconf):
    _file_ledger(sconf)
    item = {**FILE_SERIES, "label": "言い直したファイルの系列"}
    it = page_items(with_series_data(_sdrafts(item), sconf))[0]
    assert it["has_numbers"] and [p["period"] for p in it["points"]] == ["2019", "2020"]


def test_cli_prints_the_selector_note_and_writes_nothing(sconf, tmp_path):
    _file_ledger(sconf, AI_CHECKER)
    before = [(sconf / f).read_text("utf-8") for f in (SERIES_FILE, POINTS_FILE)]
    drafts = tmp_path / "d.json"
    drafts.write_text(json.dumps(_sdrafts(_changed_item()), ensure_ascii=False), "utf-8")
    checks = tmp_path / "c.json"
    checks.write_text("{}", "utf-8")
    shutil.copy(DEFAULT_CONFIG_DIR / "sources.yaml", sconf / "sources.yaml")
    settings = sconf / "s.toml"
    settings.write_text("[paths]\n", "utf-8")
    res = CliRunner().invoke(
        app,
        ["sources", "merge", "--drafts", str(drafts), "--from", str(checks), "--accept-rest",
         "--settings", str(settings)],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert "新しい series_id で下書きする" in res.output and "取り直す" not in res.output
    assert [(sconf / f).read_text("utf-8") for f in (SERIES_FILE, POINTS_FILE)] == before


def test_owner_ok_without_points_is_an_untapped_row(sconf):
    _write_ledger(sconf, [])
    drafts = _sdrafts(API_SERIES)
    key = _skey(sconf)
    res = merge_checks(drafts, {key: {"v": "ok", "at": _at()}}, sconf)
    assert res.series_added == [] and res.series_ai == [] and _only_ai_row(sconf)
    assert any("--accept-rest" in x for x in res.skipped)
    res = merge_checks(drafts, {key: {"v": "ok", "at": _at()}}, sconf, accept_rest=True)
    assert res.series_ai == ["work_rate"] and res.series_added == []
    assert any("数がまだ無い" in x for x in res.skipped)
    assert _series_file(sconf)[0]["checked_by"] == AI_CHECKER


def test_page_refuses_ok_when_a_series_has_no_numbers(sconf):
    _write_ledger(sconf, [])
    html = build_page(with_series_data(_sdrafts(API_SERIES), sconf))
    assert 'it.kind === "series" && !it.has_numbers' in html and "数が入るまで押せません" in html
    assert page_items(with_series_data(_sdrafts(API_SERIES), sconf))[0]["has_numbers"] is False
    # 数でない印だけの系列も「数が無い」
    _write_ledger(sconf, [_prow(value="", mark="x")])
    assert page_items(with_series_data(_sdrafts(API_SERIES), sconf))[0]["has_numbers"] is False


def test_series_link_and_count_ignore_gaps_and_bad_schemes():
    html = build_page({"made": "", "stats": [], "findings": [], "leads": [], "series": []})
    assert "const webUrl" in html and "filter((p) => !p.gap).length" in html
    assert "opener(webUrl(it.link) || webUrl(it.url)" in html


def test_series_key_has_id_and_trend_and_survey(sconf):
    a = with_series_data(_sdrafts(API_SERIES), sconf)["series"][0]
    b = {**a, "series_id": "work_rate_b"}
    assert series_key(a) != series_key(b)  # 中身が同じでも、別の系列は別の鍵
    assert series_key(a) != series_key({**a, "trend_note": "言い直した"})
    assert series_key(a) != series_key({**a, "survey": "別の調査"})
    assert series_key(a) != series_key({**a, "period_kind": "年度"})
    # 取り込み：同じ中身の2系列がどちらも入る
    _write_ledger(sconf, [_prow(), _prow("work_rate_b", "2019", "10")])
    (sconf.parent / "docs" / "estat" / "work_rate_b.json").write_text(
        json.dumps(API_META, ensure_ascii=False), "utf-8"
    )
    drafts = _sdrafts(API_SERIES, {**API_SERIES, "series_id": "work_rate_b"})
    res = merge_checks(drafts, {}, sconf, accept_rest=True)
    assert sorted(res.series_ai) == ["work_rate", "work_rate_b"]


def test_stats_only_merge_cannot_reuse_a_series_id(sconf):
    _write_ledger(sconf, [_prow()], [_srow(API_SERIES)])
    before = (sconf / "stats.csv").read_text("utf-8")
    stat = {**STAT, "stat_id": "work_rate", "replaces": ""}
    with pytest.raises(SourcesError, match="work_rate"):
        merge_checks(
            {**_drafts(), "stats": [stat]}, {stat_key(stat): {"v": "ok", "at": _at()}}, sconf
        )
    assert (sconf / "stats.csv").read_text("utf-8") == before


def test_missing_blocked_keywords_stops_the_series_merge(sconf):
    (sconf / "blocked_keywords.yaml").unlink()
    before = [(sconf / f).read_bytes() for f in (SERIES_FILE, POINTS_FILE, "stats.csv")]
    with pytest.raises(SourcesError, match="blocked_keywords"):
        merge_checks(_sdrafts(API_SERIES), {}, sconf, accept_rest=True)
    assert [(sconf / f).read_bytes() for f in (SERIES_FILE, POINTS_FILE, "stats.csv")] == before


def test_todo_survives_a_short_ledger_row(sconf):
    (sconf / SERIES_FILE).write_text(",".join(SERIES_COLUMNS) + "\nwork_rate,仕事の量\n", "utf-8")
    out = todo_drafts(_sdrafts(API_SERIES), {"series": ["work_rate"]}, sconf)
    assert out["series"][0]["series_id"] == "work_rate"


# --- ファイルの抜き出し（file_excerpt・_series_excerpt） --------------------------------


def _csv_file(path):
    rows = [[f"r{r}c{c}" for c in range(8)] for r in range(16)]
    rows[11][3:6] = ["10.1", "10.2", "10.3"]
    path.write_text("\n".join(",".join(r) for r in rows), "utf-8")
    return path


def _file_item():
    spec = {**FILE_SELECTOR["parts"][0], "sheet": ""}
    return {**FILE_SERIES, "selector": {"parts": [spec]}, "points": [], "meta": None}


def test_file_excerpt_fetches_the_newest_part_and_cuts_the_cells(tmp_path):
    from koten.sources.check import file_excerpt

    seen = []

    def fetch(url, rel):
        seen.append((url, rel))
        return _csv_file(tmp_path / "f.csv"), "2026-11-27"

    x = file_excerpt(_file_item(), fetch)
    assert seen == [
        ("https://www.e-stat.go.jp/stat-search/file-download?statInfId=000031000001&fileKind=0",
         "files/000031000001_0")
    ]  # fmt: skip
    assert [x["rows"][r][c] for r, c in x["hl"]] == ["10.1", "10.2", "10.3"]


def test_file_excerpt_refuses_ids_that_are_not_digits(tmp_path):
    from koten.sources.check import file_excerpt

    item = _file_item()
    item["selector"]["parts"][0]["statInfId"] = "../../etc"
    with pytest.raises(SourcesError, match="数字"):
        file_excerpt(item, lambda url, rel: pytest.fail("取りに行かない"))


class _FakeFetcher:
    def __init__(self, path=None, error=None):
        self.path, self.error, self.manifest, self.calls = path, error, {}, []

    def fetch(self, url, rel, refresh=False, validate=None):
        self.calls.append(rel)
        if self.error:
            raise self.error
        return self.path, None


def _cli_settings(config_dir):
    import koten.cli as cli

    path = config_dir / "s.toml"
    root = config_dir.parent
    path.write_text(
        f'[paths]\nhdd_root = "{root / "nohdd"}"\nssd_root = "{root / "ssd"}"\n', "utf-8"
    )
    return cli, cli._settings(str(path))


def test_series_excerpt_provider_reads_through_the_fetcher(sconf, tmp_path, monkeypatch):
    cli, s = _cli_settings(sconf)
    fake = _FakeFetcher(_csv_file(tmp_path / "f.csv"))
    monkeypatch.setenv("KOTEN_CONTACT", "t@koten.invalid")
    monkeypatch.setattr(cli, "_make_fetcher", lambda s, raw_dir, client=None: fake)
    x = cli._series_excerpt(s)(_file_item())
    assert x and fake.calls == ["files/000031000001_0"]


def test_series_excerpt_provider_notes_failures_and_still_returns_none(
    sconf, tmp_path, monkeypatch, capsys
):
    cli, s = _cli_settings(sconf)
    monkeypatch.setenv("KOTEN_CONTACT", "t@koten.invalid")
    fake = _FakeFetcher(error=OSError("取れない"))
    monkeypatch.setattr(cli, "_make_fetcher", lambda s, raw_dir, client=None: fake)
    assert cli._series_excerpt(s)(_file_item()) is None
    assert "ファイルの抜き出しを作れません" in capsys.readouterr().err


def test_sources_page_works_without_a_contact_and_says_so_once(sconf, tmp_path, monkeypatch):
    monkeypatch.delenv("KOTEN_CONTACT", raising=False)
    drafts = tmp_path / "d.json"
    two = {**_file_item(), "series_id": "file_rate2"}
    drafts.write_text(json.dumps(_sdrafts(_file_item(), two), ensure_ascii=False), "utf-8")
    shutil.copy(DEFAULT_CONFIG_DIR / "sources.yaml", sconf / "sources.yaml")
    settings = sconf / "s.toml"
    settings.write_text("[paths]\n", "utf-8")
    out = tmp_path / "p.html"
    res = CliRunner().invoke(
        app,
        [
            "sources",
            "page",
            "--drafts",
            str(drafts),
            "--out",
            str(out),
            "--settings",
            str(settings),
        ],
    )
    assert res.exit_code == 0, res.output
    assert res.output.count("KOTEN_CONTACT") == 1 and "file_rate2" in out.read_text("utf-8")


def test_series_excerpt_provider_survives_a_bad_manifest(sconf, monkeypatch, capsys):
    cli, s = _cli_settings(sconf)
    monkeypatch.setenv("KOTEN_CONTACT", "t@koten.invalid")

    def boom(s, raw_dir, client=None):
        raise TypeError("ManifestEntry.__init__() got an unexpected keyword argument 'x'")

    monkeypatch.setattr(cli, "_make_fetcher", boom)
    assert cli._series_excerpt(s)(_file_item()) is None
    assert "ファイルの抜き出しを作れません" in capsys.readouterr().err


def test_contact_note_tells_unset_from_malformed(sconf, monkeypatch, capsys):
    cli, s = _cli_settings(sconf)
    monkeypatch.delenv("KOTEN_CONTACT", raising=False)
    assert cli._series_excerpt(s)(_file_item()) is None
    assert "設定されていない" in capsys.readouterr().err
    monkeypatch.setenv("KOTEN_CONTACT", "nowhere")
    assert cli._series_excerpt(s)(_file_item()) is None
    assert "形が正しくない" in capsys.readouterr().err


# --- 部分を足した estat_file の系列（growth）・selector がまだ無い下書き ------------------

PART2 = {**FILE_SELECTOR["parts"][0], "statInfId": "000031000002", "values": "D12:F12"}


def _grown(**over):
    parts = [{**FILE_SELECTOR["parts"][0], **over.pop("first", {})}, PART2]
    return {**FILE_SERIES, "selector": {"parts": parts[: over.pop("n", 2)]}, **over}


def _pp(*part_years):
    """file_rate の点。(部分の番号, 年) の並びから。"""
    return parse_points([_prow("file_rate", y, "5", part=str(i)) for i, y in part_years])


def test_selection_change_rules():
    from koten.stats.series import selection_change as sc

    one = FILE_SELECTOR
    p0 = one["parts"][0]
    got0 = _pp((0, "2019"), (0, "2020"))  # 1 番目の部分は数を取った
    assert sc("estat_file", one, one, points=got0) == "same"
    assert sc("estat_file", {"parts": [p0, PART2]}, one, points=got0) == "growth"
    assert sc("estat_file", {"parts": [{**p0, "updated": "2027-01-01"}]}, one, points=got0) == (
        "growth"
    )
    assert (
        sc(
            "estat_file",
            {"parts": [{**p0, "label_cells": ["A13"], "unit_cell": "B4"}]},
            one,
            points=got0,
        )
        == "growth"
    )
    assert sc("estat_file", {"parts": [{**p0, "values": "D13:F13"}]}, one, points=got0) == (
        "different"
    )
    for key in ("statInfId", "fileKind", "sheet", "periods_cells"):
        assert sc("estat_file", {"parts": [{**p0, key: "9"}]}, one, points=got0) == "different"
    # 数を取った部分を外した下書きは、台帳より古い下書き
    assert sc("estat_file", {"parts": []}, one, points=got0) == "stale"
    two = {"parts": [p0, PART2]}
    assert sc("estat_file", {"parts": [PART2, p0]}, two, points=got0) == "different"  # 並べ替え
    assert sc("estat_file", {"parts": [PART2]}, two, points=got0) == "different"
    assert (
        sc("estat_api", {"statsDataId": "1", "cdCat01": "2"}, {"statsDataId": "1"}, points=[])
        == "different"
    )
    assert sc("estat_api", {"statsDataId": "1"}, {"statsDataId": "1"}, points=[]) == "same"


def _growth_ledger(config_dir, by):
    _write_ledger(
        config_dir,
        [_prow(), _prow("file_rate", "2019", "5"), _prow("file_rate", "2020", "6")],
        [_srow(API_SERIES), _srow(FILE_SERIES, checked_by=by, checked_at="2026-11-01",
                                  checked_sha="abc")],
    )  # fmt: skip


def test_growth_card_is_pointless_and_refuses_ok(sconf):
    _growth_ledger(sconf, HUMAN_CHECKER)
    it = page_items(with_series_data(_sdrafts(_grown()), sconf))[0]
    assert it["selector_state"] == "growth" and it["points"] == [] and not it["has_numbers"]
    note = it["selector_note"]
    assert note.startswith("部分を足した・直した")
    # 先に Mac の確かめるだけの取得で部分を確かめ、そのあと取り込んで数を取る
    a = note.index("uv run koten stats fetch --drafts <下書き>")
    b = note.index("merge --accept-rest")
    assert a < b and "koten stats fetch で数を取ってから確かめる" in note[b:]


def test_growth_merge_downgrades_an_owner_row_and_keeps_the_points(sconf):
    _growth_ledger(sconf, HUMAN_CHECKER)
    item = _grown()
    it = page_items(with_series_data(_sdrafts(item), sconf))[0]
    points = (sconf / POINTS_FILE).read_bytes()
    # 押さないとき：何も書かない（--accept-rest の案内）
    before = (sconf / SERIES_FILE).read_bytes()
    res = merge_checks(_sdrafts(item), {it["k"]: {"v": "ok", "at": _at()}}, sconf)
    assert res.series_ai == [] and (sconf / SERIES_FILE).read_bytes() == before
    assert any(
        x.startswith("file_rate：部分を足した・直した")
        and x.index("uv run koten stats fetch --drafts <下書き>") < x.index("--accept-rest")
        for x in res.skipped
    ), res.skipped
    res = merge_checks(_sdrafts(item), {it["k"]: {"v": "ok", "at": _at()}}, sconf, accept_rest=True)
    assert res.series_ai == ["file_rate"] and res.series_downgraded == ["file_rate"]
    assert res.series_added == []
    row = {r["series_id"]: r for r in _series_file(sconf)}["file_rate"]
    assert row["checked_by"] == AI_CHECKER and row["checked_sha"] == ""
    assert len(json.loads(row["selector"])["parts"]) == 2
    assert (sconf / POINTS_FILE).read_bytes() == points  # 数は外さない


def test_growth_merge_on_an_ai_row_is_not_a_downgrade_and_cli_prints_it(sconf, tmp_path):
    _growth_ledger(sconf, AI_CHECKER)
    res = merge_checks(_sdrafts(_grown()), {}, sconf, accept_rest=True)
    assert res.series_ai == ["file_rate"] and res.series_downgraded == []
    _growth_ledger(sconf, HUMAN_CHECKER)
    drafts = tmp_path / "d.json"
    drafts.write_text(
        json.dumps(_sdrafts(_grown(first={"updated": "2027-01-01"})), ensure_ascii=False), "utf-8"
    )
    checks = tmp_path / "c.json"
    checks.write_text("{}", "utf-8")
    shutil.copy(DEFAULT_CONFIG_DIR / "sources.yaml", sconf / "sources.yaml")
    settings = sconf / "s.toml"
    settings.write_text("[paths]\n", "utf-8")
    res = CliRunner().invoke(
        app,
        ["sources", "merge", "--drafts", str(drafts), "--from", str(checks), "--accept-rest",
         "--settings", str(settings)],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert "file_rate：部分を足した・直したので AI の行に戻した。数を取ってから確かめ直す" in (
        res.output
    )


def test_updated_only_is_growth_and_identity_changes_are_different(sconf):
    _growth_ledger(sconf, AI_CHECKER)
    upd = _grown(n=1, first={"updated": "2027-01-01"})
    assert page_items(with_series_data(_sdrafts(upd), sconf))[0]["selector_state"] == "growth"
    for item in (_grown(n=1, first={"values": "D13:F13"}),
                 {**FILE_SERIES, "selector": {"parts": [PART2]}}):  # fmt: skip
        full = with_series_data(_sdrafts(item), sconf)
        got = page_items(full)[0]
        assert got["selector_state"] == "different"
        res = merge_checks(_sdrafts(item), {}, sconf, accept_rest=True)
        assert res.series_ai == [] and any(
            "新しい series_id で下書きする" in x for x in res.skipped
        )


def test_unresolved_selector_gets_the_meta_instruction(sconf):
    item = {**API_SERIES, "selector": {}, "series_id": "later_rate"}
    it = page_items(with_series_data(_sdrafts(item), sconf))[0]
    assert it["selector_state"] == "unresolved"
    assert it["selector_note"] == "selector がまだ無い（Mac で uv run koten stats meta）"
    html = build_page(with_series_data(_sdrafts(item), sconf))
    assert "it.selector_note" in html
    before = (sconf / SERIES_FILE).read_bytes()
    res = merge_checks(_sdrafts(item), {}, sconf, accept_rest=True)
    assert res.series_ai == [] and (sconf / SERIES_FILE).read_bytes() == before
    assert any("selector がまだ無い" in x and "選び方が違う" not in x for x in res.skipped)
    # 台帳にある系列でも、selector が空なら「違う」ではなく「まだ無い」
    ledger_item = {**API_SERIES, "selector": {}}
    it = page_items(with_series_data(_sdrafts(ledger_item), sconf))[0]
    assert it["selector_state"] == "unresolved" and "選び方が違う" not in it["selector_note"]


# --- 数を取った部分だけが変えられない（R23）・重なる部分・古い下書き・selector の形 -------------

EXP0 = {"statInfId": "000031000001", "fileKind": "0", "sheet": "表1", "values": "D12:E12",
        "periods": ["2019", "2020"], "label_cells": ["A12"]}  # fmt: skip
EXP1 = {**EXP0, "statInfId": "000031000002", "periods": ["2021"]}


def _exp_item(parts):
    return {**FILE_SERIES, "selector": {"parts": parts}}


def _exp_ledger(config_dir, parts, part_years, by=AI_CHECKER):
    """file_rate（部分は parts）と work_rate が台帳にある。file_rate の点は (部分の番号, 年)。"""
    sha = "abc" if by == HUMAN_CHECKER else ""
    _write_ledger(
        config_dir,
        [_prow(), *(_prow("file_rate", y, "5", part=str(i)) for i, y in part_years)],
        [_srow(API_SERIES),
         _srow(_exp_item(parts), checked_by=by, checked_at="2026-11-01", checked_sha=sha)],
    )  # fmt: skip


def _ledger_bytes(config_dir):
    return [(config_dir / f).read_bytes() for f in (SERIES_FILE, POINTS_FILE, "stats.csv")]


def _file_row(config_dir):
    return {r["series_id"]: r for r in _series_file(config_dir)}["file_rate"]


def test_selection_change_freezes_only_the_parts_with_points():
    from koten.stats.series import selection_change as sc

    ledger = {"parts": [EXP0, EXP1]}
    only0 = _pp((0, "2019"), (0, "2020"))  # 2 番目の部分はまだ数を取っていない
    fixed1 = {**EXP1, "values": "D13:E13"}
    assert sc("estat_file", {"parts": [EXP0, fixed1]}, ledger, points=only0) == "growth"
    assert sc("estat_file", {"parts": [EXP0]}, ledger, points=only0) == "growth"  # 外す
    moved0 = {**EXP0, "values": "D13:E13"}
    assert sc("estat_file", {"parts": [moved0, EXP1]}, ledger, points=only0) == "different"
    # 数がまだ1つも無い系列は、どの部分を変えても growth。同じなら same
    other = {"parts": [{**EXP0, "statInfId": "000031000009"}]}
    assert sc("estat_file", other, ledger, points=[]) == "growth"
    assert sc("estat_file", ledger, ledger, points=[]) == "same"
    # 両方の部分に数がある：前の部分だけの下書きは台帳より古い。2 番目を変えるのは different
    both = _pp((0, "2019"), (1, "2021"))
    assert sc("estat_file", {"parts": [EXP0]}, ledger, points=both) == "stale"
    assert sc("estat_file", {"parts": [EXP0, fixed1]}, ledger, points=both) == "different"
    assert sc("estat_file", {"parts": [EXP0, fixed1, EXP1]}, ledger, points=both) == "different"
    # 番号の間の数の無い部分は直せる（数のある部分の番号は動かせない）
    three = {"parts": [EXP0, EXP1, {**EXP1, "statInfId": "000031000003"}]}
    gap = _pp((0, "2019"), (2, "2022"))
    assert sc("estat_file", {"parts": [EXP0, fixed1, three["parts"][2]]}, three, points=gap) == (
        "growth"
    )
    assert sc("estat_file", {"parts": [EXP0, three["parts"][2]]}, three, points=gap) == "different"
    # どの部分の数か分からない点があれば、台帳の部分は全部変えられない
    odd = parse_points([_prow("file_rate", "2019", "5", part="")])
    assert sc("estat_file", {"parts": [EXP0, fixed1]}, ledger, points=odd) == "different"
    assert sc("estat_file", {"parts": [EXP0]}, ledger, points=odd) == "stale"


def test_selection_change_normalizes_the_values_it_compares():
    from koten.stats.series import selection_change as sc

    p0 = FILE_SELECTOR["parts"][0]
    got0 = _pp((0, "2019"))
    one = {"parts": [p0]}
    # periods の [] と null と書いていない、statInfId の前後の空白は同じ
    for draft in ({**p0, "periods": []}, {**p0, "periods": None},
                  {**p0, "statInfId": " 000031000001 ", "values": "D12:F12 "}):  # fmt: skip
        assert sc("estat_file", {"parts": [draft]}, one, points=got0) == "same", draft
        assert sc("estat_file", {"parts": [draft, PART2]}, one, points=got0) == "growth", draft
    assert sc("estat_file", {"parts": [{**EXP0, "periods": [2019, 2020]}]}, {"parts": [EXP0]},
              points=got0) == "same"  # fmt: skip
    # sheet の "" は CSV。書いていないのとは違う
    csv0 = {**p0, "sheet": ""}
    no_sheet = {k: v for k, v in p0.items() if k != "sheet"}
    assert sc("estat_file", {"parts": [no_sheet]}, {"parts": [csv0]}, points=got0) == "different"


def test_normalized_same_selector_keeps_the_points_on_the_card(sconf):
    _growth_ledger(sconf, AI_CHECKER)
    spaced = {**FILE_SELECTOR["parts"][0], "statInfId": " 000031000001", "periods": []}
    it = page_items(with_series_data(_sdrafts(_exp_item([spaced])), sconf))[0]
    assert it["selector_state"] == "same" and it["has_numbers"] and it["selector_note"] == ""


def test_stale_draft_is_told_to_follow_the_ledger_and_writes_nothing(sconf):
    note = "下書きが台帳より古い（台帳の部分のほうが多い）。下書きを台帳に合わせる"
    _exp_ledger(sconf, [EXP0, EXP1], [(0, "2019"), (0, "2020"), (1, "2021")])
    item = _exp_item([EXP0])
    it = page_items(with_series_data(_sdrafts(item), sconf))[0]
    assert it["selector_state"] == "stale" and not it["has_numbers"]
    assert it["selector_note"] == note
    before = _ledger_bytes(sconf)
    for checks, rest in (({it["k"]: {"v": "ok", "at": _at()}}, False), ({}, True)):
        res = merge_checks(_sdrafts(item), checks, sconf, accept_rest=rest)
        assert res.series_ai == [] and res.series_added == []
        assert f"file_rate：{note}" in res.skipped
        assert _ledger_bytes(sconf) == before


def test_growth_part_that_hits_a_fetched_year_is_refused(sconf):
    _exp_ledger(sconf, [EXP0], [(0, "2019"), (0, "2020")], by=HUMAN_CHECKER)
    bad = {**EXP1, "periods": ["2020", "2021"]}  # 新しい年のファイルに前の年も載っている
    item = _exp_item([EXP0, bad])
    it = page_items(with_series_data(_sdrafts(item), sconf))[0]
    assert it["selector_state"] == "growth" and not it["has_numbers"]
    assert it["selector_note"] == (
        "足した部分が前の部分と重なる（2 番目の部分の 2020 年はもう数がある）。下書きを直す"
    )
    before = _ledger_bytes(sconf)
    for checks, rest in (({}, True), ({it["k"]: {"v": "ok", "at": _at()}}, True), ({}, False)):
        res = merge_checks(_sdrafts(item), checks, sconf, accept_rest=rest)
        assert res.series_ai == [] and res.series_downgraded == [] and res.series_added == []
        assert (
            "file_rate：足した部分が前の部分と重なる（2 番目の部分の 2020 年はもう数がある）。"
            "下書きを直す"
        ) in res.skipped, res.skipped
        assert _ledger_bytes(sconf) == before


def test_growth_parts_whose_periods_meet_each_other_are_refused(sconf):
    _exp_ledger(sconf, [EXP0], [(0, "2019"), (0, "2020")])
    twin = {**EXP1, "statInfId": "000031000003", "periods": ["2021", "2022"]}
    item = _exp_item([EXP0, EXP1, twin])
    before = _ledger_bytes(sconf)
    res = merge_checks(_sdrafts(item), {}, sconf, accept_rest=True)
    assert res.series_ai == [] and _ledger_bytes(sconf) == before
    assert (
        "file_rate：足した部分が前の部分と重なる（3 番目の部分の 2021 年は 2 番目の部分にもある）。"
        "下書きを直す"
    ) in res.skipped, res.skipped


def test_growth_part_that_repeats_another_part_is_refused(sconf):
    _exp_ledger(sconf, [EXP0], [(0, "2019"), (0, "2020")])
    # 同じ表の同じセルを、別の年として読む（ファイルの取得では見つからない）
    dup = {**EXP0, "statInfId": " 000031000001", "periods": ["2021", "2022"]}
    item = _exp_item([EXP0, dup])
    assert page_items(with_series_data(_sdrafts(item), sconf))[0]["selector_note"].startswith(
        "足した部分が前の部分と重なる（2 番目の部分は 1 番目と同じセルを読む"
    )
    before = _ledger_bytes(sconf)
    res = merge_checks(_sdrafts(item), {}, sconf, accept_rest=True)
    assert res.series_ai == [] and _ledger_bytes(sconf) == before
    assert any(
        x.startswith("file_rate：足した部分が前の部分と重なる（2 番目の部分は 1 番目と同じセル")
        for x in res.skipped
    ), res.skipped


def test_unfetched_part_can_be_fixed_or_removed_by_a_later_draft(sconf):
    fixed = [EXP0, {**EXP1, "values": "D13:E13"}]
    for parts in (fixed, [EXP0]):
        _exp_ledger(sconf, [EXP0, EXP1], [(0, "2019"), (0, "2020")], by=HUMAN_CHECKER)
        item = _exp_item(parts)
        assert with_series_data(_sdrafts(item), sconf)["series"][0]["selector_state"] == "growth"
        points = (sconf / POINTS_FILE).read_bytes()
        res = merge_checks(_sdrafts(item), {}, sconf, accept_rest=True)
        assert res.series_ai == ["file_rate"] and res.series_downgraded == ["file_rate"]
        row = _file_row(sconf)
        assert json.loads(row["selector"])["parts"] == parts and row["checked_by"] == AI_CHECKER
        assert (sconf / POINTS_FILE).read_bytes() == points
    # 数を取った部分を変えるのは、今までどおり different
    _exp_ledger(sconf, [EXP0, EXP1], [(0, "2019"), (0, "2020")])
    moved = _exp_item([{**EXP0, "values": "D13:E13"}, EXP1])
    assert with_series_data(_sdrafts(moved), sconf)["series"][0]["selector_state"] == "different"
    res = merge_checks(_sdrafts(moved), {}, sconf, accept_rest=True)
    assert res.series_ai == [] and any("新しい series_id で下書きする" in x for x in res.skipped)


def test_overlapping_part_from_before_has_a_working_repair_path(sconf):
    # オーナーの行：1 番目の部分は 2019・2020 年。2020・2021 年の部分を足す下書きは入らない
    _exp_ledger(sconf, [EXP0], [(0, "2019"), (0, "2020")], by=HUMAN_CHECKER)
    bad = {**EXP1, "periods": ["2020", "2021"]}
    res = merge_checks(_sdrafts(_exp_item([EXP0, bad])), {}, sconf, accept_rest=True)
    assert res.series_ai == [] and any("重なる" in x for x in res.skipped)
    # 前の決まりで、重なる部分が台帳に入ってしまった形（取れないので 2 番目の部分に数は無い）
    for parts in ([EXP0, {**bad, "periods": ["2021"]}], [EXP0]):
        _exp_ledger(sconf, [EXP0, bad], [(0, "2019"), (0, "2020")])
        res = merge_checks(_sdrafts(_exp_item(parts)), {}, sconf, accept_rest=True)
        assert res.series_ai == ["file_rate"], res.skipped
        assert json.loads(_file_row(sconf)["selector"])["parts"] == parts


def test_unresolved_note_follows_the_source_kind(sconf):
    file_item = {k: v for k, v in FILE_SERIES.items() if k != "selector"}
    for item in (file_item, {**FILE_SERIES, "selector": {}}, {**FILE_SERIES, "selector": ""}):
        it = page_items(with_series_data(_sdrafts(item), sconf))[0]
        assert it["selector_state"] == "unresolved"
        assert it["selector_note"] == "selector（parts）がまだ無い。ファイルの位置を下書きに書く"
        before = _ledger_bytes(sconf)
        res = merge_checks(_sdrafts(item), {}, sconf, accept_rest=True)
        assert res.series_ai == [] and _ledger_bytes(sconf) == before
        assert "file_rate：selector（parts）がまだ無い。ファイルの位置を下書きに書く" in res.skipped
    api = {k: v for k, v in API_SERIES.items() if k != "selector"}
    it = page_items(with_series_data(_sdrafts(api), sconf))[0]
    assert it["selector_note"] == "selector がまだ無い（Mac で uv run koten stats meta）"


def test_selector_that_is_not_an_object_is_named_and_writes_nothing(sconf):
    note = "selector が JSON のオブジェクトになっていない"
    for bad in ("oops", [1, 2], 5, True, "[]", '"x"'):
        for base in (API_SERIES, FILE_SERIES):
            item = {**base, "series_id": "bad_sel", "selector": bad}
            it = page_items(with_series_data(_sdrafts(item), sconf))[0]
            assert it["selector_state"] == "invalid" and it["selector_note"] == note, bad
            assert not it["has_numbers"]
            before = _ledger_bytes(sconf)
            res = merge_checks(_sdrafts(item), {}, sconf, accept_rest=True)
            assert res.series_ai == [] and f"bad_sel：{note}" in res.skipped
            assert _ledger_bytes(sconf) == before
    # JSON の文字列で書いたオブジェクトは読む（台帳の行から作る todo の形）
    as_text = {**API_SERIES, "selector": json.dumps(API_SERIES["selector"])}
    assert with_series_data(_sdrafts(as_text), sconf)["series"][0]["selector_state"] == "same"
    out = todo_drafts(_sdrafts(API_SERIES), {"series": ["work_rate"]}, sconf)
    assert out["series"][0]["selector_state"] == "same"


# --- 最後の見直しの直し：単位・公の名前（R25・R33） --------------------------------------


def test_api_card_shows_the_e_stat_unit_next_to_the_ledger_unit(sconf):
    meta = {**API_META, "chosen": [{"class": "表章項目", "item": "人数", "code": "001",
                                    "unit": "千人"}]}  # fmt: skip
    (sconf.parent / "docs" / "estat" / "work_rate.json").write_text(
        json.dumps(meta, ensure_ascii=False), "utf-8"
    )
    _write_ledger(sconf, [_prow(period="2021", value="12", src_unit="千人")])
    it = page_items(with_series_data(_sdrafts(API_SERIES), sconf))[0]
    pairs = [p[0] for p in it["source"]]
    src = dict(it["source"])
    assert src["台帳の単位"] == "％"
    chosen_units = src["e-Stat の単位（選んだ項目）"]
    assert "千人" in chosen_units and "表章項目" in chosen_units
    assert src["e-Stat の単位（数）"] == "千人"
    assert pairs.index("台帳の単位") + 1 == pairs.index("e-Stat の単位（選んだ項目）")
    # 数がまだ無い系列は、選んだ項目の単位だけ
    _write_ledger(sconf, [])
    src = dict(page_items(with_series_data(_sdrafts(API_SERIES), sconf))[0]["source"])
    assert "e-Stat の単位（数）" not in src and "千人" in src["e-Stat の単位（選んだ項目）"]
    # 表が単位を出さないときは「なし」と出す
    (sconf.parent / "docs" / "estat" / "work_rate.json").write_text(
        json.dumps(API_META, ensure_ascii=False), "utf-8"
    )
    src = dict(page_items(with_series_data(_sdrafts(API_SERIES), sconf))[0]["source"])
    assert src["e-Stat の単位（選んだ項目）"] == "なし"


def test_file_card_shows_the_unit_cell_next_to_the_ledger_unit(sconf):
    _file_ledger(sconf, AI_CHECKER)
    _write_ledger(sconf, [_prow("file_rate", "2019", "5", src_unit="（％）")])
    src = page_items(with_series_data(_sdrafts(FILE_SERIES), sconf))[0]["source"]
    names = [p[0] for p in src]
    assert dict(src)["台帳の単位"] == "％" and dict(src)["単位のセル"] == "（％）"
    assert names.index("台帳の単位") + 1 == names.index("単位のセル")
    _write_ledger(sconf, [_prow("file_rate", "2019", "5", src_unit="")])
    src = dict(page_items(with_series_data(_sdrafts(FILE_SERIES), sconf))[0]["source"])
    assert src["単位のセル"] == "（なし）"


def test_merge_refuses_an_api_unit_that_differs_from_the_fetched_unit(sconf):
    _write_ledger(sconf, [_prow(period="2021", value="12", src_unit="千人")])
    before = _ledger_bytes(sconf)
    with pytest.raises(SourcesError, match="e-Stat の単位 '千人' と unit '％' が違う"):
        merge_checks(_sdrafts(API_SERIES), {}, sconf, accept_rest=True)
    key = _skey(sconf)
    with pytest.raises(SourcesError, match="e-Stat の単位"):
        merge_checks(_sdrafts(API_SERIES), {key: {"v": "ok", "at": _at()}}, sconf)
    assert _ledger_bytes(sconf) == before


def test_title_with_a_full_width_space_merges(sconf):
    meta = {**API_META, "title": "就業状態別　人口"}
    (sconf.parent / "docs" / "estat" / "new_rate.json").write_text(
        json.dumps(meta, ensure_ascii=False), "utf-8"
    )
    item = {**API_SERIES, "series_id": "new_rate", "table_title": "就業状態別　人口"}
    res = merge_checks(_sdrafts(item), {}, sconf, accept_rest=True)
    assert res.series_ai == ["new_rate"]
    from koten.db import connect, load_series

    conn = connect(sconf.parent / "ssd")
    assert load_series(conn, sconf, sconf.parent / "docs" / "estat")[0] == 2
    # 空白を抜いた題は違う題
    bad = {**item, "series_id": "bad_rate", "table_title": "就業状態別人口"}
    (sconf.parent / "docs" / "estat" / "bad_rate.json").write_text(
        json.dumps(meta, ensure_ascii=False), "utf-8"
    )
    with pytest.raises(SourcesError, match="と違う"):
        merge_checks(_sdrafts(bad), {}, sconf, accept_rest=True)


# --- 最後の見直しの直し：ID の空白・重なる部分・growth・保留・書き方（R26〜R28・L9・L10） ---------


def _owner_held_ledger(config_dir):
    """work_rate がオーナーの行で保留。確かめの sha は今の数と表の情報のもの。"""
    s = series_from_draft(API_SERIES)
    sha = check_sha(s, parse_points(read_point_rows(config_dir)), API_META)
    row = _srow(API_SERIES, label="人が直した文", checked_by=HUMAN_CHECKER,
                checked_at="2026-11-01", checked_sha=sha, status="保留")  # fmt: skip
    _write_ledger(config_dir, read_point_rows(config_dir), [row])
    return row


@pytest.mark.parametrize("sid", ["work_rate ", "work_rate　", " work_rate"])
def test_padded_draft_id_never_overwrites_an_owner_row(sconf, sid):
    _owner_held_ledger(sconf)
    before = _ledger_bytes(sconf)
    res = merge_checks(_sdrafts({**API_SERIES, "series_id": sid}), {}, sconf, accept_rest=True)
    assert res.series_added == [] and res.series_ai == []
    assert _ledger_bytes(sconf) == before


def test_padded_kind_and_id_tap_takes_effect_for_the_stripped_id(sconf, tmp_path):
    from koten.db import connect, load_series

    item = {**API_SERIES, "series_id": " work_rate", "source_kind": "estat_api "}
    it = page_items(with_series_data(_sdrafts(item), sconf))[0]
    assert it["id"] == "work_rate" and dict(it["source"])["統計名"] == "労働力調査"
    res = merge_checks(_sdrafts(item), {it["k"]: {"v": "ok", "at": _at()}}, sconf)
    assert res.series_added == ["work_rate"] and res.unknown == []
    row = _series_file(sconf)[0]
    assert (row["series_id"], row["source_kind"], row["checked_by"]) == (
        "work_rate", "estat_api", HUMAN_CHECKER,
    )  # fmt: skip
    conn = connect(tmp_path / "ssd")
    load_series(conn, sconf, sconf.parent / "docs" / "estat")
    assert conn.execute("SELECT owner_ok FROM stat_series").fetchone()[0] == 1


def test_file_series_round_trip_gives_owner_ok(sconf, tmp_path):
    from koten.db import connect, load_series

    _file_ledger(sconf, AI_CHECKER)
    item = {**FILE_SERIES, "series_id": "file_rate ", "period_kind": " 年"}
    it = page_items(with_series_data(_sdrafts(item), sconf))[0]
    assert it["has_numbers"]
    res = merge_checks(_sdrafts(item), {it["k"]: {"v": "ok", "at": _at()}}, sconf)
    assert res.series_added == ["file_rate"]
    conn = connect(tmp_path / "ssd")
    load_series(conn, sconf, sconf.parent / "docs" / "estat")
    got = dict(conn.execute("SELECT series_id, owner_ok FROM stat_series").fetchall())
    assert got["file_rate"] == 1


def test_padded_stat_id_never_overwrites_an_owner_stat(config_dir):
    owner = {**STAT, "stat_id": "keep_me", "replaces": ""}
    merge_checks(
        {**_drafts(), "stats": [owner]}, {stat_key(owner): {"v": "ok", "at": _at()}}, config_dir
    )
    assert _stats(config_dir)["keep_me"]["checked_by"] == HUMAN_CHECKER
    before = (config_dir / "stats.csv").read_bytes()
    padded = {**owner, "stat_id": "keep_me ", "label": "AI の文"}
    res = merge_checks({**_drafts(), "stats": [padded]}, {}, config_dir, accept_rest=True)
    assert res.stats_ai == [] and (config_dir / "stats.csv").read_bytes() == before


def test_page_and_merge_keys_agree_for_padded_stat_ids(config_dir):
    padded = {**STAT, "stat_id": " anzen_r6_amount "}
    (it,) = [x for x in page_items({**_drafts(), "stats": [padded]}) if x["kind"] == "stat"]
    res = merge_checks(
        {**_drafts(), "stats": [padded]}, {it["k"]: {"v": "ok", "at": _at()}}, config_dir
    )
    assert res.stats_added == ["anzen_r6_amount"] and res.unknown == []


def test_new_file_series_with_a_copied_part_is_refused(sconf):
    dup = {**EXP0, "periods": ["2021"]}
    item = {**_exp_item([EXP0, dup]), "series_id": "copy_rate"}
    it = page_items(with_series_data(_sdrafts(item), sconf))[0]
    assert it["selector_state"] == "new"
    assert it["selector_note"].startswith(
        "足した部分が前の部分と重なる（2 番目の部分は 1 番目と同じセルを読む"
    )
    before = _ledger_bytes(sconf)
    for checks, rest in (({}, True), ({it["k"]: {"v": "ok", "at": _at()}}, True)):
        res = merge_checks(_sdrafts(item), checks, sconf, accept_rest=rest)
        assert res.series_ai == [] and res.series_added == []
        assert any(x.startswith("copy_rate：足した部分が前の部分と重なる") for x in res.skipped)
        assert _ledger_bytes(sconf) == before
    # 年ごとに別のファイルなら入る
    ok = {**_exp_item([EXP0, {**EXP1, "periods": ["2021"]}]), "series_id": "year_rate"}
    assert merge_checks(_sdrafts(ok), {}, sconf, accept_rest=True).series_ai == ["year_rate"]


def test_same_draft_with_an_overlapping_unfetched_part_is_refused(sconf):
    bad = {**EXP1, "periods": ["2020", "2021"]}
    _exp_ledger(sconf, [EXP0, bad], [(0, "2019"), (0, "2020")])
    item = _exp_item([EXP0, bad])
    it = page_items(with_series_data(_sdrafts(item), sconf))[0]
    assert it["selector_state"] == "same" and "重なる" in it["selector_note"]
    before = _ledger_bytes(sconf)
    res = merge_checks(_sdrafts(item), {}, sconf, accept_rest=True)
    assert res.series_ai == [] and _ledger_bytes(sconf) == before
    assert any("重なる" in x for x in res.skipped)


def test_growth_keeps_the_ledger_label_and_names_and_says_so(sconf):
    _growth_ledger(sconf, HUMAN_CHECKER)
    rows = _series_file(sconf)
    for r in rows:
        if r["series_id"] == "file_rate":
            r.update(
                label="オーナーが直した文", status="保留", note="人のメモ", url="https://a.jp/"
            )
    _write_ledger(sconf, read_point_rows(sconf), rows)
    item = _grown(label="ファイルの系列の割合", survey="別の調査", url="https://b.jp/",
                  latest_known="2023", note="")  # fmt: skip
    res = merge_checks(_sdrafts(item), {}, sconf, accept_rest=True)
    assert res.series_ai == ["file_rate"] and res.series_downgraded == ["file_rate"]
    assert res.series_label_kept == ["file_rate"]
    row = _file_row(sconf)
    assert row["label"] == "オーナーが直した文" and row["survey"] == FILE_SERIES["survey"]
    assert row["url"] == "https://a.jp/" and row["status"] == "保留"
    assert row["latest_known"] == "2023" and row["note"] == ""
    assert len(json.loads(row["selector"])["parts"]) == 2
    assert row["checked_by"] == AI_CHECKER and row["checked_sha"] == "" and row["trend"] == ""


def test_growth_with_a_padded_id_downgrades_and_keeps_hold(sconf):
    _growth_ledger(sconf, HUMAN_CHECKER)
    rows = _series_file(sconf)
    for r in rows:
        if r["series_id"] == "file_rate":
            r["status"] = "保留"
    _write_ledger(sconf, read_point_rows(sconf), rows)
    res = merge_checks(_sdrafts(_grown(series_id="file_rate ")), {}, sconf, accept_rest=True)
    assert res.series_downgraded == ["file_rate"] and res.series_ai == ["file_rate"]
    assert _file_row(sconf)["status"] == "保留"


def test_cli_says_the_label_stays(sconf, tmp_path):
    _growth_ledger(sconf, HUMAN_CHECKER)
    drafts = tmp_path / "d.json"
    drafts.write_text(json.dumps(_sdrafts(_grown(label="別の言い方")), ensure_ascii=False), "utf-8")
    checks = tmp_path / "c.json"
    checks.write_text("{}", "utf-8")
    shutil.copy(DEFAULT_CONFIG_DIR / "sources.yaml", sconf / "sources.yaml")
    settings = sconf / "s.toml"
    settings.write_text("[paths]\n", "utf-8")
    res = CliRunner().invoke(
        app,
        ["sources", "merge", "--drafts", str(drafts), "--from", str(checks), "--accept-rest",
         "--settings", str(settings)],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert "file_rate：ラベルは台帳のまま（オーナーが直した言い方を残す）" in res.output


def test_held_series_card_says_so_and_an_ok_keeps_the_hold(sconf):
    rows = [_srow(API_SERIES, checked_by=AI_CHECKER, checked_at="2026-11-01", status="保留")]
    _write_ledger(sconf, read_point_rows(sconf), rows)
    it = page_items(with_series_data(_sdrafts(API_SERIES), sconf))[0]
    assert it["held"] is True
    html = build_page(with_series_data(_sdrafts(API_SERIES), sconf))
    assert "保留中" in html.split("function cardSeries")[1].split("\nfunction ")[0]
    res = merge_checks(_sdrafts(API_SERIES), {it["k"]: {"v": "ok", "at": _at()}}, sconf)
    assert res.series_added == ["work_rate"]
    row = _series_file(sconf)[0]
    assert row["checked_by"] == HUMAN_CHECKER and row["status"] == "保留"
    plain = page_items(with_series_data(_sdrafts(FILE_SERIES), sconf))[0]
    assert plain["held"] is False


def test_ng_on_a_ledger_series_puts_it_on_hold_with_the_owner_note(sconf):
    s = series_from_draft(API_SERIES)
    sha = check_sha(s, parse_points(read_point_rows(sconf)), API_META)
    owner = _srow(API_SERIES, checked_by=HUMAN_CHECKER, checked_at="2026-11-01",
                  checked_sha=sha, note="前のメモ")  # fmt: skip
    _write_ledger(sconf, read_point_rows(sconf), [owner])
    it = page_items(with_series_data(_sdrafts(API_SERIES), sconf))[0]
    res = merge_checks(
        _sdrafts(API_SERIES), {it["k"]: {"v": "ng", "at": _at(), "note": "分類が違う"}}, sconf
    )
    assert res.rejected == ["work_rate"] and res.series_held == ["work_rate"]
    assert res.series_added == [] and res.series_ai == []
    row = _series_file(sconf)[0]
    assert row["status"] == "保留" and row["checked_by"] == HUMAN_CHECKER
    assert row["note"].startswith("前のメモ") and "分類が違う" in row["note"]
    assert {k: v for k, v in row.items() if k not in ("status", "note")} == {
        k: v for k, v in owner.items() if k not in ("status", "note")
    }
    # 台帳に無い系列の「違う」は、何も書かない
    before = _ledger_bytes(sconf)
    new = {**FILE_SERIES, "series_id": "brand_new"}
    it = page_items(with_series_data(_sdrafts(new), sconf))[0]
    res = merge_checks(_sdrafts(new), {it["k"]: {"v": "ng", "at": _at()}}, sconf)
    assert res.rejected == ["brand_new"] and res.series_held == []
    assert _ledger_bytes(sconf) == before


def test_ledgers_are_written_through_temp_files(sconf, monkeypatch):
    (sconf / f"{SERIES_FILE}.tmp").mkdir()  # 系列の一時ファイルが書けない
    before = _ledger_bytes(sconf)
    drafts = {**_drafts(), "series": [{**FILE_SERIES, "series_id": "brand_new"}]}
    checks = {stat_key(STAT): {"v": "ok", "at": _at()}}
    with pytest.raises(OSError):
        merge_checks(drafts, checks, sconf, accept_rest=True)
    assert _ledger_bytes(sconf) == before and not (sconf / "findings.csv").exists()
    assert sorted(p.name for p in sconf.glob("*.tmp")) == [f"{SERIES_FILE}.tmp"]
