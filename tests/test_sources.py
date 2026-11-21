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
    assert "統計 1 行" in res.output and "koten db init" in res.output
