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


def _write_ledger(config_dir, points, series_rows=()):
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
    )  # fmt: skip
    return config_dir


def _sdrafts(*series):
    return {"made": "2026-11-26", "stats": [], "findings": [], "leads": [], "series": list(series)}


def _skey(config_dir, item=API_SERIES):
    full = with_series_data(_sdrafts(item), config_dir)
    return series_key(full["series"][0])


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
    res = merge_checks(_sdrafts(API_SERIES), {_skey(sconf): {"v": "ng", "at": _at()}}, sconf)
    assert res.rejected == ["work_rate"] and res.series_added == []
    assert _series_file(sconf) == []


def test_merge_replaces_same_id_keeps_order_and_appends(sconf):
    other = _srow({**FILE_SERIES, "series_id": "zzz_other"})
    old = _srow(API_SERIES, label="古い", checked_by=AI_CHECKER, checked_at="2026-11-01")
    _write_ledger(sconf, [_prow()], [other, old])
    key = _skey(sconf)
    merge_checks(_sdrafts(API_SERIES), {key: {"v": "ok", "at": _at()}}, sconf)
    ids = [r["series_id"] for r in _series_file(sconf)]
    assert ids == ["zzz_other", "work_rate"]
    assert _series_file(sconf)[1]["label"] == API_SERIES["label"]


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
    assert res.series_ai == [] or "work_rate" not in res.series_ai
    row = next(r for r in _series_file(sconf) if r["series_id"] == "work_rate")
    assert row["label"] == "人の文" and row["checked_by"] == HUMAN_CHECKER


def test_owner_ok_on_a_series_without_points_is_skipped(sconf):
    _write_ledger(sconf, [])
    drafts = _sdrafts(API_SERIES)
    res = merge_checks(drafts, {_skey(sconf): {"v": "ok", "at": _at()}}, sconf)
    assert res.series_added == [] and any(
        "数がまだ無い" in x and "koten stats fetch" in x for x in res.skipped
    )
    assert _series_file(sconf) == []


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
    checks = {stat_key(STAT): {"v": "ok", "at": _at()}, _skey(sconf, bad): {"v": "ok", "at": _at()}}
    with pytest.raises(SourcesError, match="stat_series.csv"):
        merge_checks(drafts, checks, sconf)
    assert (sconf / "stats.csv").read_text("utf-8") == stats_before
    assert not (sconf / "findings.csv").exists()
    assert _series_file(sconf) == []
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
    _write_ledger(sconf, [])
    res = merge_checks(_sdrafts(API_SERIES), {}, sconf, accept_rest=True)
    assert res.series_ai == ["work_rate"]
    row = _series_file(sconf)[0]
    assert row["checked_by"] == AI_CHECKER and row["checked_sha"] == ""


def test_series_without_points_loads_into_the_ssd(sconf, tmp_path):
    from koten.db import connect, load_series

    _write_ledger(sconf, [])
    merge_checks(_sdrafts({**API_SERIES, "latest_known": "2023"}), {}, sconf, accept_rest=True)
    conn = connect(tmp_path / "ssd")
    assert load_series(conn, sconf, sconf.parent / "docs" / "estat")[0] == 1
    r = conn.execute("SELECT latest_period, stale FROM stat_series").fetchone()
    assert r["latest_period"] is None and r["stale"] == 1  # 数が無いので、今の数としては使えない
