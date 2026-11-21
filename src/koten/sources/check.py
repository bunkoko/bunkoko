"""統計と研究の下書きを、スマホで確かめるページにし、確かめた行を台帳に書く。

下書き（JSON）の形：

    {"made": "2026-11-21",
     "stats": [{stat_id, theme, label, value, unit, survey, year, url, publisher, published,
                where, evidence, trend, trend_note, replaces, check_note}],
     "findings": [{findings.csv の列（status などを除く）, license_evidence, where, check_note}],
     "leads": [{lead_id, kind: private_survey か paid_paper, theme, title, publisher, date, url,
                terms_url, doi, authors, year, journal, volume, issue, pages, why}]}

ページのデータベースの文書（checks/<キー>）の形：

    {k, kind, v: "ok" か "ng", at（押した時刻。ミリ秒）, note,
     fix: {人が直した列}, trend: 増え方も確かめたか,
     read: 本文の結果の節で確かめたか, retr: 取り下げ・訂正を確かめたか,
     terms: 引用の決まりを読んだか, entry: {人が書いた列}}

キーは下書きの中身から作る（下書きを直したら、確かめ直してもらうため）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from koten.db import STATS_COLUMNS, TRENDS, stats_problems
from koten.findings import COLUMNS as FINDING_COLUMNS
from koten.findings import DESIGNS, EFFECTS, REPLICATION, Finding, FindingsError, read_findings
from koten.script.placeholders import format_stat

TEMPLATE = Path(__file__).with_name("page.html")
ITEMS_MARK = "/*__ITEMS__*/[]"
MADE_MARK = '/*__MADE__*/""'
# stats.csv の列。読み込み（STATS_COLUMNS）のあとに、確かめた場所・人・日・メモを足す
STATS_FILE_COLUMNS = [*STATS_COLUMNS, "where", "checked_by", "checked_at", "note"]
CHECKER = "オーナー"
TOKYO = ZoneInfo("Asia/Tokyo")
STAT_FIX = ("label", "value", "unit", "year", "survey")
FINDING_FIX = ("claim_ja", "limits_ja", "population_ja")
LEAD_STAT = ("label", "value", "unit", "survey", "year", "url")
LEAD_FINDING = (
    "claim_ja",
    "limits_ja",
    "population_ja",
    "effect_ja",
    "design",
    "replication",
    "replication_note",
    "affiliation_ja",
    "authors_ja",
)
ASCII_ID = re.compile(r"[a-z0-9][a-z0-9_]*$")
REPLICATION_JA = {
    "replicated": "再現した",
    "mixed": "まちまち",
    "not_tested": "まだ確かめていない",
    "failed": "再現しなかった",
}


class SourcesError(ValueError):
    pass


def _key(prefix: str, *parts: Any) -> str:
    body = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return f"{prefix}-{hashlib.sha1(body.encode()).hexdigest()[:12]}"


def stat_key(d: dict[str, Any]) -> str:
    return _key(
        "s", *(str(d.get(c, "")) for c in ("stat_id", "label", "value", "unit", "survey", "year"))
    )


def finding_key(d: dict[str, Any]) -> str:
    return _key(
        "f",
        *(str(d.get(c, "")) for c in ("finding_id", "claim_ja", "limits_ja", "population_ja")),
        str(d.get("doi", "")),
    )


def lead_key(d: dict[str, Any]) -> str:
    return _key("l", str(d.get("lead_id", "")), str(d.get("url", "")))


def read_drafts(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict):
        raise SourcesError(f"{path} は下書きの形ではありません")
    for part in ("stats", "findings", "leads"):
        data.setdefault(part, [])
        if not isinstance(data[part], list):
            raise SourcesError(f"{path} の {part} が並びではありません")
    return data


def _finding(d: dict[str, Any], **over: str) -> Finding:
    v = {c: str(d.get(c) or "").strip() for c in FINDING_COLUMNS}
    v.update(over)
    themes = tuple(t.strip() for t in re.split(r"[;；]", v["themes"]) if t.strip())
    return Finding(**{**v, "themes": themes})


def finding_lines(d: dict[str, Any]) -> list[str]:
    """台本に入る形（出典と一文・限界・調べた人）。docs/findings.md のとおり。"""
    f = _finding(d)
    out = [f"{f.source_label()}では、{f.claim_ja.rstrip('。')}。"]
    if f.limits_ja:
        out.append(f"ただ、この研究は、{f.limits_ja.rstrip('。')}。")
    if f.population_ja and f.design != "theory":
        out.append(f"この研究で調べたのは、{f.population_ja.rstrip('。')}です。")
    return out


def page_items(drafts: dict[str, Any]) -> list[dict[str, Any]]:
    """ページに出す1件ずつ。統計・研究は下書きの文、民間の調査・有料の論文は書誌だけ。"""
    out: list[dict[str, Any]] = []
    for d in drafts["stats"]:
        out.append(
            {
                "k": stat_key(d),
                "kind": "stat",
                "id": d["stat_id"],
                "theme": d.get("theme", ""),
                "line": format_stat(d),
                "f": {c: str(d.get(c, "")) for c in STAT_FIX},
                "publisher": d.get("publisher", ""),
                "published": d.get("published", ""),
                "url": d.get("url", ""),
                "where": d.get("where", ""),
                "evidence": d.get("evidence", ""),
                "trend": d.get("trend", "") if d.get("trend") in TRENDS else "",
                "trend_note": d.get("trend_note", "") if d.get("trend") in TRENDS else "",
                "replaces": d.get("replaces", ""),
                "check_note": d.get("check_note", ""),
            }
        )
    for d in drafts["findings"]:
        f = _finding(d)
        out.append(
            {
                "k": finding_key(d),
                "kind": "finding",
                "id": d["finding_id"],
                "theme": d.get("themes", ""),
                "lines": finding_lines(d),
                "src": f.source_label(),
                "f": {c: str(d.get(c, "")) for c in FINDING_FIX},
                "citation": f.citation(),
                "license": d.get("license", ""),
                "license_evidence": d.get("license_evidence", ""),
                "url": f"https://doi.org/{f.doi}" if f.doi else f.url,
                "where": d.get("where", ""),
                "design": DESIGNS.get(f.design, f.design),
                "replication": REPLICATION_JA.get(f.replication, f.replication),
                "replication_note": f.replication_note,
                "effect": f.effect_ja,
                "check_note": d.get("check_note", ""),
            }
        )
    for d in drafts["leads"]:
        kind = d.get("kind")
        if kind not in ("private_survey", "paid_paper"):
            raise SourcesError(f"{d.get('lead_id')}：kind は private_survey か paid_paper")
        item = {
            "k": lead_key(d),
            "kind": kind,
            "id": d["lead_id"],
            "theme": d.get("theme", ""),
            "title": d.get("title", ""),
            "publisher": d.get("publisher", ""),
            "date": d.get("date", ""),
            "url": d.get("url", "") or (f"https://doi.org/{d['doi']}" if d.get("doi") else ""),
            "terms_url": d.get("terms_url", ""),
            "why": d.get("why", ""),
        }
        if kind == "private_survey":
            item["f"] = {
                "survey": f"{d.get('publisher', '')}「{d.get('title', '')}」",
                "url": item["url"],
            }
        else:
            item["citation"] = _finding(
                {**d, "authors": d.get("authors", ""), "year": str(d.get("year", ""))}
            ).citation()
            item["f"] = {
                "affiliation_ja": d.get("affiliation_ja", ""),
                "authors_ja": d.get("authors_ja", ""),
            }
        out.append(item)
    keys = [x["k"] for x in out]
    if len(keys) != len(set(keys)):
        raise SourcesError("下書きに同じ中身の行が2つあります")
    return out


def _script_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def build_page(drafts: dict[str, Any]) -> str:
    html = TEMPLATE.read_text("utf-8")
    if ITEMS_MARK not in html or MADE_MARK not in html:
        raise ValueError(f"{TEMPLATE.name} に差し込む印がない")
    return html.replace(ITEMS_MARK, _script_json(page_items(drafts))).replace(
        MADE_MARK, _script_json(str(drafts.get("made", "")))
    )


# --- 台帳に書く -----------------------------------------------------------------


@dataclass
class MergeResult:
    stats_added: list[str] = field(default_factory=list)
    stats_removed: list[str] = field(default_factory=list)
    findings_added: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # 人が「違う」「使わない」を押した
    # 押したが、確かめが足りず入れなかった（理由つき）
    skipped: list[str] = field(default_factory=list)
    # 下書きに無いキー（下書きを直す前の押した跡）
    unknown: list[str] = field(default_factory=list)
    unchecked: int = 0  # まだ押していない行


def _day(at: Any) -> str:
    try:
        return datetime.fromtimestamp(float(at) / 1000, TOKYO).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return date.today().isoformat()


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _themes(config_dir: Path) -> set[str]:
    path = config_dir / "themes.yaml"
    if not path.exists():
        return set()
    return set(yaml.safe_load(path.read_text("utf-8")).get("themes") or [])


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def _csv_text(columns: list[str], rows: list[dict[str, str]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") or "" for c in columns})
    return buf.getvalue()


def _stat_row(d: dict[str, Any], c: dict[str, Any], *, from_lead: bool) -> dict[str, str]:
    fix = c.get("entry" if from_lead else "fix") or {}
    row = {col: _text(d.get(col, "")) for col in STATS_COLUMNS}
    for col in LEAD_STAT if from_lead else STAT_FIX:
        if _text(fix.get(col)):
            row[col] = _text(fix[col])
    row["status"] = ""
    if from_lead or not (c.get("trend") and d.get("trend") in TRENDS and d.get("trend_note")):
        row["trend"] = row["trend_note"] = ""
    row["where"] = _text(d.get("where", "")) if not from_lead else "人が元のページで確かめた"
    row["checked_by"] = CHECKER
    row["checked_at"] = _day(c.get("at"))
    row["note"] = _text(c.get("note", ""))[:200]
    return row


def merge_checks(drafts: dict[str, Any], checks: dict[str, Any], config_dir: Path) -> MergeResult:
    """人が「合っている」を押した行だけを、stats.csv と findings.csv に書く。

    どちらかの台帳が決まりに合わなくなるなら、どちらも書かずに止める（SourcesError）。
    同じ stat_id・finding_id の行があれば置き換える。統計の下書きの replaces に書いた古い行
    （転載サイトを出典にした行など）は、置き換える行を人が確かめたときだけ外す。
    """
    res = MergeResult()
    themes = _themes(config_dir)
    by_key: dict[str, tuple[str, dict[str, Any]]] = {}
    for d in drafts["stats"]:
        by_key[stat_key(d)] = ("stat", d)
    for d in drafts["findings"]:
        by_key[finding_key(d)] = ("finding", d)
    for d in drafts["leads"]:
        by_key[lead_key(d)] = (d.get("kind", ""), d)
    res.unknown = [k for k in checks if k not in by_key]
    res.unchecked = sum(1 for k in by_key if k not in checks)

    new_stats: list[dict[str, str]] = []
    new_findings: list[dict[str, str]] = []
    removes: set[str] = set()
    for key, (kind, d) in by_key.items():
        c = checks.get(key)
        if not isinstance(c, dict):
            continue
        name = d.get("stat_id") or d.get("finding_id") or d.get("lead_id") or key
        if c.get("v") == "ng":
            res.rejected.append(name)
            continue
        if c.get("v") != "ok":
            continue
        day = _day(c.get("at"))
        if kind == "stat":
            new_stats.append(_stat_row(d, c, from_lead=False))
            if d.get("replaces"):
                removes.add(str(d["replaces"]))
        elif kind == "private_survey":
            entry = c.get("entry") or {}
            if not c.get("terms"):
                res.skipped.append(f"{name}：引用の決まりを読んだ印が無い")
                continue
            if not (_text(entry.get("label")) and re.search(r"\d", _text(entry.get("value")))):
                res.skipped.append(f"{name}：何の数字か（label）と数字（value）が要る")
                continue
            sid = str(d.get("stat_id") or "").strip() or f"priv_{d['lead_id']}"
            if not ASCII_ID.match(sid):
                raise SourcesError(f"{name}：stat_id は英小文字・数字・_ で（{sid}）")
            new_stats.append(
                _stat_row(
                    {**d, "stat_id": sid, "theme": d.get("theme", ""), "url": d.get("url", "")},
                    c,
                    from_lead=True,
                )
            )
        elif kind in ("finding", "paid_paper"):
            if not (c.get("read") and c.get("retr")):
                res.skipped.append(f"{name}：本文の結果の節と、取り下げ・訂正を確かめた印が要る")
                continue
            row = {col: _text(d.get(col, "")) for col in FINDING_COLUMNS}
            if kind == "paid_paper":
                row["finding_id"] = str(d.get("finding_id") or f"FN-{d['lead_id']}")
                row["themes"] = _text(d.get("themes") or d.get("theme"))
                row["license"] = "COPYRIGHTED"
                row["year"] = _text(d.get("year"))
                for col in LEAD_FINDING:
                    if _text((c.get("entry") or {}).get(col)):
                        row[col] = _text(c["entry"][col])
                if not row["replication"]:
                    row["replication"] = "not_tested"
            else:
                for col in FINDING_FIX:
                    if _text((c.get("fix") or {}).get(col)):
                        row[col] = _text(c["fix"][col])
            row.update(
                status="verified",
                verified_from="full_text",
                checked_by=CHECKER,
                checked_at=day,
                retraction_checked_at=day,
                note=_text(c.get("note", ""))[:200] or row.get("note", ""),
            )
            new_findings.append(row)
    for r in new_stats:
        if themes and r["theme"] not in themes:
            raise SourcesError(f"{r['stat_id']}：themes.yaml に無いテーマ（{r['theme']}）")
    for r in new_findings:
        if r["design"] not in DESIGNS or r["effect_ja"] not in EFFECTS:
            raise SourcesError(f"{r['finding_id']}：design・effect_ja が決まりの言葉でない")
        if r["replication"] not in REPLICATION:
            raise SourcesError(f"{r['finding_id']}：replication が決まりの言葉でない")

    stats_path = config_dir / "stats.csv"
    cols, old = _read_csv(stats_path)
    columns = [*cols, *(c for c in STATS_FILE_COLUMNS if c not in cols)]
    added = {r["stat_id"] for r in new_stats}
    kept = []
    for r in old:
        if r.get("stat_id") in added:
            continue
        if r.get("stat_id") in removes:
            res.stats_removed.append(r["stat_id"])
            continue
        kept.append(r)
    stats_rows = kept + new_stats
    if problems := stats_problems(stats_rows):
        raise SourcesError("stats.csv に問題が出るので書きません：" + "、".join(problems))

    find_path = config_dir / "findings.csv"
    fcols, fold = _read_csv(find_path)
    if fcols and fcols != FINDING_COLUMNS:
        raise SourcesError(f"{find_path.name} の列が決まりと違うので書きません")
    fadded = {r["finding_id"] for r in new_findings}
    find_rows = [r for r in fold if r.get("finding_id") not in fadded] + new_findings
    find_text = _csv_text(FINDING_COLUMNS, find_rows)

    # findings.csv は、書く前に決まり（koten findings build と同じ）で読めるかを確かめる
    if new_findings:
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp)
            (trial / "findings.csv").write_text(find_text, "utf-8")
            if (config_dir / "themes.yaml").exists():
                shutil.copy(config_dir / "themes.yaml", trial / "themes.yaml")
            try:
                read_findings(trial)
            except FindingsError as e:
                raise SourcesError(f"findings.csv に問題が出るので書きません：{e}") from e

    if new_stats or res.stats_removed:
        stats_path.write_text(_csv_text(columns, stats_rows), "utf-8")
    if new_findings:
        find_path.write_text(find_text, "utf-8")
    res.stats_added = [r["stat_id"] for r in new_stats]
    res.findings_added = [r["finding_id"] for r in new_findings]
    return res
