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

from koten.db import AI_CHECKER, HUMAN_CHECKER, STATS_COLUMNS, TRENDS, stats_problems
from koten.findings import COLUMNS as FINDING_COLUMNS
from koten.findings import (
    DESIGNS,
    EFFECTS,
    FINDINGS_FILE,
    REPLICATION,
    Finding,
    FindingsError,
    read_findings,
)
from koten.script.placeholders import format_stat

TEMPLATE = Path(__file__).with_name("page.html")
ITEMS_MARK = "/*__ITEMS__*/[]"
MADE_MARK = '/*__MADE__*/""'
# stats.csv の列。読み込み（STATS_COLUMNS）のあとに、確かめた場所・人・日・メモを足す
STATS_FILE_COLUMNS = [*STATS_COLUMNS, "where", "note"]
CHECKER = HUMAN_CHECKER
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
                # 元の資料の該当ページ（koten sources locate）：画像・ページの番号・飛ぶリンク
                "img": d.get("img", ""),
                "zoom": d.get("zoom", ""),
                "page": d.get("page") or "",
                "jump": d.get("jump") or d.get("doc_url") or d.get("url", ""),
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
                # 論文の結果の文（本文にそのままあると確かめた文）と、その文に飛ぶリンク
                "passages": [
                    {k: x.get(k) for k in ("role", "section", "text", "mark")}
                    for x in d.get("passages") or []
                ],
                "jump": d.get("jump") or (f"https://doi.org/{f.doi}" if f.doi else f.url),
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
    # 押していないので、AI が確かめた行として入れた（accept_rest のとき）
    stats_ai: list[str] = field(default_factory=list)
    findings_ai: list[str] = field(default_factory=list)


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


def _stat_row(
    d: dict[str, Any], c: dict[str, Any], *, from_lead: bool, by: str, day: str
) -> dict[str, str]:
    fix = c.get("entry" if from_lead else "fix") or {}
    row = {col: _text(d.get(col, "")) for col in STATS_COLUMNS}
    for col in LEAD_STAT if from_lead else STAT_FIX:
        if _text(fix.get(col)):
            row[col] = _text(fix[col])
    row["status"] = ""
    # 増え方は、人が「増え方も確かめた」に印を付けたときだけ（AI だけの行には書かない）
    if (
        from_lead
        or by != CHECKER
        or not (c.get("trend") and d.get("trend") in TRENDS and d.get("trend_note"))
    ):
        row["trend"] = row["trend_note"] = ""
    row["where"] = _text(d.get("where", "")) if not from_lead else "人が元のページで確かめた"
    row["checked_by"] = by
    row["checked_at"] = day
    row["note"] = _text(c.get("note", ""))[:200]
    return row


def _finding_row(
    d: dict[str, Any], c: dict[str, Any], *, kind: str, by: str, day: str, ai_day: str
) -> dict[str, str]:
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
    human = by == CHECKER
    row.update(
        status="verified" if human else "ai_checked",
        verified_from="full_text",
        checked_by=by,
        checked_at=day,
        # 人が押したときは押した日。AI の行は、調べ役が Crossref と論文のページで見た日
        retraction_checked_at=day if human else ai_day,
        note=_text(c.get("note", ""))[:200] or row.get("note", ""),
    )
    return row


def merge_checks(
    drafts: dict[str, Any],
    checks: dict[str, Any],
    config_dir: Path,
    *,
    accept_rest: bool = False,
) -> MergeResult:
    """押した結果を、stats.csv と findings.csv に書く。

    - 人が「合っている」「入れる」を押した行：人が確かめた行（checked_by オーナー・verified）
    - accept_rest のとき、統計と CC BY の研究で押していない行：AI が確かめた行（checked_by AI・
      ai_checked）。台本で使えるが、回で使ったら承認の前に人が確かめる。民間の調査と有料の論文は
      人が数字や一文を書くまで入れない
    - 「違う」「使わない」を押した行は入れない
    - 人が確かめた行を、AI が確かめた行で上書きしない
    - どちらかの台帳が決まりに合わなくなるなら、どちらも書かずに止める（SourcesError）
    - 統計の下書きの replaces に書いた古い行（転載サイトを出典にした行など）は、置き換える行を
      入れたときに外す
    """
    res = MergeResult()
    themes = _themes(config_dir)
    ai_day = str(drafts.get("made") or date.today().isoformat())
    by_key: dict[str, tuple[str, dict[str, Any]]] = {}
    for d in drafts["stats"]:
        by_key[stat_key(d)] = ("stat", d)
    for d in drafts["findings"]:
        by_key[finding_key(d)] = ("finding", d)
    for d in drafts["leads"]:
        by_key[lead_key(d)] = (d.get("kind", ""), d)
    res.unknown = [k for k in checks if k not in by_key]
    res.unchecked = sum(1 for k in by_key if k not in checks)

    stats_path = config_dir / "stats.csv"
    cols, old = _read_csv(stats_path)
    find_path = config_dir / "findings.csv"
    fcols, fold = _read_csv(find_path)
    if fcols and fcols != FINDING_COLUMNS:
        raise SourcesError(f"{find_path.name} の列が決まりと違うので書きません")
    human_stats = {r["stat_id"] for r in old if (r.get("checked_by") or "").strip() == CHECKER}
    human_findings = {r["finding_id"] for r in fold if (r.get("status") or "") == "verified"}

    new_stats: list[dict[str, str]] = []
    new_findings: list[dict[str, str]] = []
    removes: set[str] = set()
    for key, (kind, d) in by_key.items():
        c = checks.get(key)
        name = d.get("stat_id") or d.get("finding_id") or d.get("lead_id") or key
        if not isinstance(c, dict) or c.get("v") not in ("ok", "ng"):
            if not accept_rest or kind not in ("stat", "finding"):
                continue
            if kind == "stat" and d["stat_id"] in human_stats:
                continue
            if kind == "finding" and d["finding_id"] in human_findings:
                continue
            if kind == "finding" and d.get("license") not in ("CC_BY", "CC0"):
                continue
            if kind == "stat":
                new_stats.append(_stat_row(d, {}, from_lead=False, by=AI_CHECKER, day=ai_day))
                res.stats_ai.append(d["stat_id"])
                if d.get("replaces"):
                    removes.add(str(d["replaces"]))
            else:
                new_findings.append(
                    _finding_row(d, {}, kind=kind, by=AI_CHECKER, day=ai_day, ai_day=ai_day)
                )
                res.findings_ai.append(d["finding_id"])
            continue
        if c.get("v") == "ng":
            res.rejected.append(name)
            continue
        day = _day(c.get("at"))
        if kind == "stat":
            new_stats.append(_stat_row(d, c, from_lead=False, by=CHECKER, day=day))
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
                    by=CHECKER,
                    day=day,
                )
            )
        elif kind in ("finding", "paid_paper"):
            if not (c.get("read") and c.get("retr")):
                res.skipped.append(f"{name}：本文の結果の節と、取り下げ・訂正を確かめた印が要る")
                continue
            new_findings.append(_finding_row(d, c, kind=kind, by=CHECKER, day=day, ai_day=ai_day))
    for r in new_stats:
        if themes and r["theme"] not in themes:
            raise SourcesError(f"{r['stat_id']}：themes.yaml に無いテーマ（{r['theme']}）")
    for r in new_findings:
        if r["design"] not in DESIGNS or r["effect_ja"] not in EFFECTS:
            raise SourcesError(f"{r['finding_id']}：design・effect_ja が決まりの言葉でない")
        if r["replication"] not in REPLICATION:
            raise SourcesError(f"{r['finding_id']}：replication が決まりの言葉でない")

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
    ai_stats, ai_findings = set(res.stats_ai), set(res.findings_ai)
    res.stats_added = [r["stat_id"] for r in new_stats if r["stat_id"] not in ai_stats]
    res.findings_added = [
        r["finding_id"] for r in new_findings if r["finding_id"] not in ai_findings
    ]
    return res


# --- 回で使った行だけを確かめる -----------------------------------------------------

DRAFTS_GLOB = "docs/research/*-source-drafts.json"


def collect_drafts(paths: list[Path]) -> dict[str, Any]:
    """下書きのファイルをまとめる。同じ ID は後のファイルを使う（ファイル名の日付の順に渡す）。"""
    out: dict[str, Any] = {"made": "", "stats": {}, "findings": {}, "leads": {}}
    for path in paths:
        d = read_drafts(path)
        out["made"] = str(d.get("made") or out["made"])
        for part, idk in (("stats", "stat_id"), ("findings", "finding_id"), ("leads", "lead_id")):
            for x in d[part]:
                out[part][x[idk]] = x
    return {**out, **{part: list(out[part].values()) for part in ("stats", "findings", "leads")}}


def todo_drafts(drafts: dict[str, Any], todo: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """回で使った、人がまだ確かめていない行だけのページの材料。

    文と数字は今の台帳（動画で言っている形）、元の資料の場所・該当箇所は下書きから取る。
    下書きに無い行（人が台帳に直に書いた行）も、台帳の文と URL で出す。
    """
    _, stat_rows = _read_csv(config_dir / "stats.csv")
    _, find_rows = _read_csv(config_dir / FINDINGS_FILE)
    ledger_s = {r["stat_id"]: r for r in stat_rows}
    ledger_f = {r["finding_id"]: r for r in find_rows}
    draft_s = {d["stat_id"]: d for d in drafts["stats"]}
    draft_f = {d["finding_id"]: d for d in drafts["findings"]}
    stats, findings, missing = [], [], []
    for sid in todo.get("stats") or []:
        if sid not in ledger_s:
            missing.append(sid)
            continue
        row = {k: v for k, v in ledger_s[sid].items() if k in STATS_COLUMNS or k == "where"}
        stats.append(
            {**draft_s.get(sid, {}), **{k: v for k, v in row.items() if v}, "replaces": ""}
        )
    for fid in todo.get("findings") or []:
        if fid not in ledger_f:
            missing.append(fid)
            continue
        findings.append({**draft_f.get(fid, {}), **ledger_f[fid]})
    if missing:
        raise SourcesError(
            f"台帳に無い行があります（{'・'.join(missing)}）：git pull してから作り直す"
        )
    return {
        "made": f"回 {todo.get('episode_id', '')}",
        "stats": stats,
        "findings": findings,
        "leads": [],
    }
