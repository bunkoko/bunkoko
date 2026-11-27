"""統計と研究の下書きを、スマホで確かめるページにし、確かめた行を台帳に書く。

下書き（JSON）の形：

    {"made": "2026-11-21",
     "stats": [{stat_id, theme, label, value, unit, survey, year, url, publisher, published,
                where, evidence, trend, trend_note, replaces, check_note}],
     "findings": [{findings.csv の列（status などを除く）, license_evidence, where, check_note}],
     "leads": [{lead_id, kind: private_survey か paid_paper, theme, title, publisher, date, url,
                terms_url, doi, authors, year, journal, volume, issue, pages, why}],
     "series": [{stat_series.csv の列（status・checked_* を除く）, selector_names, stats_data_id,
                 evidence, check_note}]}

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
import os
import re
import shutil
import tempfile
from collections.abc import Callable
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
    REPLICATION_JA,
    Finding,
    FindingsError,
    read_findings,
)
from koten.script.placeholders import format_stat
from koten.stats.files import FILE_URL, cell_pos, read_grid
from koten.stats.report import series_from_draft
from koten.stats.series import (
    DIGITS_RE,
    SERIES_COLUMNS,
    SERIES_FILE,
    Point,
    SeriesError,
    check_sha,
    fetched_parts,
    load_meta,
    meta_dir_for,
    parse_points,
    part_overlaps,
    period_label,
    read_point_rows,
    read_series_rows,
    segments,
    selection_change,
    series_problems,
    value_num,
    verify_trend,
)
from koten.stats.series import POINTS_COLUMNS as POINT_COLUMNS
from koten.worries.master import load_blocked_keywords

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
    for part in ("stats", "findings", "leads", "series"):
        data.setdefault(part, [])
        if not isinstance(data[part], list):
            raise SourcesError(f"{path} の {part} が並びではありません")
    return data


# 鍵・照らし合わせに使う ID と種類。下書きでも台帳でも、前後の空白を1回だけ取る（R26）
_ID_FIELDS = ("series_id", "stat_id", "finding_id", "lead_id", "source_kind", "period_kind")


def _strip_ids(d: Any) -> Any:
    if not isinstance(d, dict):
        return d
    return {**d, **{k: d[k].strip() for k in _ID_FIELDS if isinstance(d.get(k), str)}}


def normalize_drafts(drafts: dict[str, Any]) -> dict[str, Any]:
    """下書きの各項目の ID と種類（_ID_FIELDS）の前後の空白を取った写し。

    ページの鍵と取り込みの鍵・台帳との照らし合わせ・書く行が、同じ値になるように。何度でも同じ。
    """
    out = dict(drafts)
    for part in ("stats", "findings", "leads", "series"):
        if isinstance(drafts.get(part), list):
            out[part] = [_strip_ids(d) for d in drafts[part]]
    return out


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


# --- 統計の系列（e-Stat の複数年の数） ---------------------------------------------


SELECTOR_CHANGED = "台帳の {id} と選び方が違う。新しい series_id で下書きする"
# selector がまだ無い下書き（生の値が無い・{}）の案内は、source_kind で分ける
SELECTOR_UNRESOLVED = {
    "estat_api": "selector がまだ無い（Mac で uv run koten stats meta）",
    "estat_file": "selector（parts）がまだ無い。ファイルの位置を下書きに書く",
}
SELECTOR_NO_KIND = "selector がまだ無い。source_kind を estat_api か estat_file にする"
SELECTOR_NOT_OBJECT = "selector が JSON のオブジェクトになっていない"
SELECTOR_STALE = "下書きが台帳より古い（台帳の部分のほうが多い）。下書きを台帳に合わせる"
SELECTOR_GROWTH = (
    "部分を足した・直した。先に Mac で uv run koten stats fetch --drafts <下書き> で部分を"
    "確かめてから merge --accept-rest で入れ、Mac で koten stats fetch で数を取ってから確かめる"
)
SELECTOR_GROWTH_SKIP = (
    "部分を足した・直した。先に Mac で uv run koten stats fetch --drafts <下書き> で部分を"
    "確かめてから、AI の行として --accept-rest で取り込む"
)
SELECTOR_OVERLAP = "足した部分が前の部分と重なる（{why}）。下書きを直す"
_SELECTOR_NOTES = {"different": SELECTOR_CHANGED, "invalid": SELECTOR_NOT_OBJECT,
                   "stale": SELECTOR_STALE, "growth": SELECTOR_GROWTH}  # fmt: skip
# 台帳に何も書かない選び方の状態（押しても、--accept-rest でも）
_REFUSED_STATES = ("different", "unresolved", "invalid", "stale")
# 部分の重なりを見る状態（R27：新しい系列は全部の部分を、same・growth は数の無い部分を）
_OVERLAP_STATES = ("new", "same", "growth")


def _raw_selector_state(raw: Any) -> str:
    """下書きの selector の生の値：まだ無い（unresolved）・オブジェクトでない（invalid）・ある。

    あるときは ""。まだ無いのは、無い・null・空の文字・{} だけ。JSON の文字列で書いた
    オブジェクトは読む（台帳の行から作る todo の形）。"""
    if isinstance(raw, str):
        if not raw.strip():
            return "unresolved"
        try:
            raw = json.loads(raw)
        except ValueError:
            return "invalid"
    if raw is None or raw == {}:
        return "unresolved"
    return "" if isinstance(raw, dict) else "invalid"


def _selector_note(d: dict[str, Any]) -> str:
    """系列の選び方の状態の注記（カードと取り込みで同じ文）。無ければ ""。"""
    state = d.get("selector_state", "")
    if state == "unresolved":
        return SELECTOR_UNRESOLVED.get(str(d.get("source_kind", "")).strip(), SELECTOR_NO_KIND)
    if state in _OVERLAP_STATES and d.get("selector_overlap"):
        return SELECTOR_OVERLAP.format(why=d["selector_overlap"])
    return _SELECTOR_NOTES.get(state, "").format(id=str(d.get("series_id", "")).strip())


def with_series_data(drafts: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """series の各項目に、台帳の点（points）と表の情報（meta）を足した写しを返す。

    ページの鍵と取り込みの鍵は、どちらもこの写しから作る（同じデータから鍵を出すため）。
    ID と種類は前後の空白を取った値にする（normalize_drafts）。
    selector_state は new・same・growth・stale・different・unresolved・invalid。点が付くのは
    same だけ（台帳に無い系列・selector がまだ無い・オブジェクトでない系列・台帳の行と
    選び方が違う系列は数なし）。estat_file の部分がほかの部分と重なれば、その理由を
    selector_overlap に入れる（取り込まない。R27：new は全部の部分、same・growth は数を
    取っていない部分）。ledger_status は台帳の行の status（保留 など）。表の情報は estat_api の
    系列だけ（docs/estat/<series_id>.json。db.py と同じ）。
    """
    drafts = normalize_drafts(drafts)
    rows = read_point_rows(config_dir)
    meta_dir = meta_dir_for(config_dir)
    ledger = {r.get("series_id", "").strip(): r for r in read_series_rows(config_dir)}
    out = []
    for item in drafts.get("series") or []:
        sid = str(item.get("series_id", "")).strip()
        # 選び方は変えられない（R21）。変えられるのは estat_file の数を取っていない部分（R22・R23）
        s = series_from_draft(item)
        mine = [r for r in rows if r.get("series_id", "").strip() == sid]
        overlap = ""
        state = _raw_selector_state(item.get("selector"))
        if not state and sid not in ledger:
            state = "new"
            if s.source_kind == "estat_file":
                overlap = "、".join(part_overlaps(s.selector, set(), []))
        elif not state:
            old = series_from_draft(ledger[sid]).selector
            pts = parse_points(mine)
            state = selection_change(s.source_kind, s.selector, old, points=pts)
            if state in _OVERLAP_STATES and s.source_kind == "estat_file":
                n = len(old["parts"]) if isinstance(old.get("parts"), list) else 0
                overlap = "、".join(part_overlaps(s.selector, fetched_parts(pts, n), pts))
        shown = sorted(mine, key=lambda r: r.get("period", "")) if state == "same" else []
        meta = None
        if s.source_kind == "estat_api":
            try:
                meta = load_meta(meta_dir, sid)
            except SeriesError as e:
                raise SourcesError(str(e)) from e
        held = str((ledger.get(sid) or {}).get("status") or "").strip()
        out.append({**item, "points": [{c: r.get(c, "") for c in POINT_COLUMNS} for r in shown],
                    "meta": meta, "selector_state": state, "selector_overlap": overlap,
                    "ledger_status": held})  # fmt: skip
    return {**drafts, "series": out}


def _points_of(item: dict[str, Any]) -> list[Point]:
    return parse_points(list(item.get("points") or []))


def series_key(item: dict[str, Any]) -> str:
    """数・名前・区切りのどれかが変われば変わる鍵（check_sha）。数がまだ無いうちは別の鍵。

    check_sha に入らない series_id・period_kind・survey・publisher・trend も鍵に入れる
    （同じ中身の2系列がぶつからず、増え方の言葉を直したら押し直しになる）。
    """
    s = series_from_draft(item)
    sha = check_sha(s, _points_of(item), item.get("meta"))
    return _key(
        "r", s.series_id, sha, s.period_kind, s.survey, s.publisher,
        s.trend, s.trend_from, s.trend_to, s.trend_note,
    )  # fmt: skip


def _has_numbers(item: dict[str, Any]) -> bool:
    return _newest_numeric(_points_of(item)) is not None


def _newest_numeric(points: list[Point]) -> Point | None:
    nums = [p for p in points if value_num(p) is not None]
    return max(nums, key=lambda p: p.period) if nums else None


def series_line(item: dict[str, Any], point: Point | None) -> str:
    """台本で読む文（段2 で比べる文が足される。今は最新の数の点の文）。"""
    if point is None:
        return ""
    s = series_from_draft(item)
    return (
        f"{s.label}は{point.value}{s.unit}"
        f"（{s.survey}、{period_label(point.period, s.period_kind)}）"
    )


def _newest_part(item: dict[str, Any], points: list[Point]) -> tuple[int, dict[str, Any]] | None:
    """estat_file の系列の、いちばん新しい点の part（無ければ最後の part）。"""
    sel = series_from_draft(item).selector
    parts = sel.get("parts") if isinstance(sel, dict) else None
    if not isinstance(parts, list) or not parts:
        return None
    newest = max(points, key=lambda p: p.period) if points else None
    idx = len(parts) - 1
    if newest is not None and newest.part.strip().isdigit() and int(newest.part) < len(parts):
        idx = int(newest.part)
    return (idx, parts[idx]) if isinstance(parts[idx], dict) else None


EXCERPT_ROWS, EXCERPT_COLS = 14, 10  # 1つの表で見せる大きさの上限
_VERTICAL_CELLS = 12  # 縦の範囲は先頭の 12 セルまで


def _ref_cells(ref: Any, limit: int | None = None) -> list[tuple[int, int]]:
    """ "A3"・"D12:R12"・"D5:D20" のセルの位置（行, 列。0 始まり）。"""
    a, _, b = str(ref).partition(":")
    r1, c1 = cell_pos(a)
    r2, c2 = cell_pos(b) if b else (r1, c1)
    out = [(r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]
    return out[:limit] if limit else out


def _excerpt_table(
    grid: list[list[str]],
    rows: list[int],
    cols: list[int],
    values: list[tuple[int, int]],
    heads: list[tuple[int, int]],
) -> dict[str, Any]:
    """rows・cols（並んだ番号）の表。番号が飛ぶところには「…」の行・列を入れる。

    hl は数のセル、lab は見出し・年・単位のセルの位置。label_rows は見出しのセルがあって
    数のセルの無い行（行ごと黄色）。"""

    def with_gaps(nums: list[int]) -> list[int | None]:
        out: list[int | None] = []
        for n in sorted(set(nums)):
            if out and out[-1] is not None and n > out[-1] + 1:
                out.append(None)
            out.append(n)
        return out

    rr, cc = with_gaps(rows), with_gaps(cols)
    rpos = {r: i for i, r in enumerate(rr) if r is not None}
    cpos = {c: j for j, c in enumerate(cc) if c is not None}

    def text(r: int | None, c: int | None) -> str:
        if r is None or c is None:
            return "…"
        row = grid[r] if r < len(grid) else []
        return row[c] if c < len(row) else ""

    def where(cells: list[tuple[int, int]]) -> list[list[int]]:
        seen: list[list[int]] = []
        for r, c in cells:
            if r in rpos and c in cpos and [rpos[r], cpos[c]] not in seen:
                seen.append([rpos[r], cpos[c]])
        return seen

    value_rows = {rpos[r] for r, c in values if r in rpos and c in cpos}
    return {
        "rows": [[text(r, c) for c in cc] for r in rr],
        "hl": where(values),
        "lab": where(heads),
        "label_rows": sorted({i for i, _ in where(heads)} - value_rows),
    }


def excerpt_from_grid(spec: dict[str, Any], grid: list[list[str]]) -> dict[str, Any]:
    """数のセル（赤）と、年・見出し・単位のセル（黄色）が並んで見える抜き出し（R36）。

    全部が 14 行 × 10 列に入るなら、まわりを含めた1つの表。大きいときは、数の行（列）と
    その年のセル・その行（列）の見出しの列（行）を1つの表にし（飛ぶところは「…」）、
    そこに入らない見出し・単位のセルは小さい表（caption つき）にする。表は tables に並べ、
    先頭の表は rows・hl・lab・label_rows にもある。縦の範囲は先頭の 12 セルまで。"""
    a, _, b = str(spec["values"]).partition(":")
    r1, c1 = cell_pos(a)
    r2, c2 = cell_pos(b) if b else (r1, c1)
    vertical = c1 == c2 and r2 > r1
    limit = _VERTICAL_CELLS if vertical else None
    values = _ref_cells(spec["values"], limit)
    periods = _ref_cells(spec["periods_cells"], limit) if spec.get("periods_cells") else []
    refs = [("見出しのセル", ref) for ref in spec.get("label_cells") or []]
    if spec.get("unit_cell"):
        refs.append(("単位のセル", spec["unit_cell"]))
    named = [(name, ref, _ref_cells(ref, _VERTICAL_CELLS)) for name, ref in refs]
    heads = periods + [cell for _, _, cells in named for cell in cells]
    every = values + heads
    top, bottom = min(r for r, _ in every), max(r for r, _ in every)
    left, right = min(c for _, c in every), max(c for _, c in every)
    if bottom - top + 1 <= EXCERPT_ROWS and right - left + 1 <= EXCERPT_COLS:
        # まわりを1行・1列ずつ（上限まで）
        if bottom - top + 1 < EXCERPT_ROWS and top > 0:
            top -= 1
        if bottom - top + 1 < EXCERPT_ROWS and bottom + 1 < len(grid):
            bottom += 1
        if right - left + 1 < EXCERPT_COLS and left > 0:
            left -= 1
        if right - left + 1 < EXCERPT_COLS:
            right += 1
        main = _excerpt_table(
            grid, list(range(top, bottom + 1)), list(range(left, right + 1)), values, heads
        )
        return {**main, "tables": [main]}
    vrows, vcols = {r for r, _ in values}, {c for _, c in values}
    label_cells = [cell for name, _, cells in named if name == "見出しのセル" for cell in cells]
    if vertical:  # 数の列・年の列と、数の行の上下にある見出しの行
        rows = sorted(vrows | {r for r, c in label_cells if c in vcols})
        cols = sorted(vcols | {c for _, c in periods})
    else:  # 数の行・年の行と、数の行の左右にある見出しの列
        rows = sorted(vrows | {r for r, _ in periods})
        cols = sorted(vcols | {c for r, c in label_cells if r in vrows})
    main = _excerpt_table(grid, rows, cols, values, heads)
    shown = {(r, c) for r in rows for c in cols}
    tables = [main]
    for name, ref, cells in named:
        if all(cell in shown for cell in cells):
            continue
        small = _excerpt_table(grid, [r for r, _ in cells], [c for _, c in cells], [], cells)
        tables.append({**small, "caption": f"{name} {str(ref).strip()}"})
    return {**main, "tables": tables}


def file_excerpt(
    item: dict[str, Any], fetch: Callable[[str, str], tuple[Path, str]]
) -> dict[str, Any] | None:
    """estat_file の系列の、いちばん新しい part のファイルから数のまわりを抜き出す。"""
    part = _newest_part(item, _points_of(item))
    if part is None:
        return None
    spec = part[1]
    sid, kind = str(spec.get("statInfId", "")).strip(), str(spec.get("fileKind", "")).strip()
    if not (sid and kind and spec.get("values")):
        return None
    if not (DIGITS_RE.fullmatch(sid) and DIGITS_RE.fullmatch(kind)):
        raise SourcesError(f"statInfId・fileKind は半角の数字だけ（{sid!r}・{kind!r}）")
    path, _ = fetch(FILE_URL.format(sid=sid, kind=kind), f"files/{sid}_{kind}")
    return excerpt_from_grid(spec, read_grid(Path(path), str(spec.get("sheet", ""))))


def _source_words(
    item: dict[str, Any], points: list[Point]
) -> tuple[list[list[str]], list[str], str]:
    """元の資料の言葉（名前と値の並び）・選んだ分類・元の表へのリンク。"""
    meta = item.get("meta") or {}
    pairs: list[list[str]] = []
    chosen: list[str] = []
    link = ""
    if series_from_draft(item).source_kind == "estat_api":
        if not meta:
            pairs.append(["表の情報", "まだ無い（Mac で koten stats meta）"])
        no = f"（{meta['title_no']}）" if meta.get("title_no") else ""
        for name, val in (
            ("統計名", meta.get("stat_name")),
            ("作った機関", meta.get("gov_org")),
            ("表の題", f"{meta['title']}{no}" if meta.get("title") else ""),
            ("調査の年月", meta.get("survey_date")),
            ("更新日", meta.get("updated_date")),
            ("公開日", meta.get("open_date")),
            ("statsDataId", meta.get("stats_data_id")),
        ):
            if val not in (None, ""):
                pairs.append([name, str(val)])
        chosen = [
            f"{c.get('class', '')}：{c.get('item', '')}"
            for c in meta.get("chosen") or []
            if isinstance(c, dict)
        ]
        # 台帳の単位の隣に、e-Stat の単位（選んだ項目の単位と、数に付いた単位）
        units = [
            f"{c.get('unit')}（{c.get('class', '')}：{c.get('item', '')}）"
            for c in meta.get("chosen") or []
            if isinstance(c, dict) and str(c.get("unit") or "").strip()
        ]
        pairs.append(["台帳の単位", series_from_draft(item).unit])
        if meta:
            pairs.append(["e-Stat の単位（選んだ項目）", "・".join(units) or "なし"])
        if points:
            got = sorted({p.src_unit.strip() for p in points if p.src_unit.strip()})
            pairs.append(["e-Stat の単位（数）", "・".join(got) or "なし"])
        sdid = str(
            series_from_draft(item).selector.get("statsDataId") or item.get("stats_data_id") or ""
        )
        link = f"https://www.e-stat.go.jp/dbview?sid={sdid}" if sdid else ""
    else:
        part = _newest_part(item, points)
        if part:
            spec = part[1]
            pairs += [
                ["statInfId", str(spec.get("statInfId", ""))],
                ["シート", str(spec.get("sheet", ""))],
            ]
            sid, kind = str(spec.get("statInfId", "")), str(spec.get("fileKind", ""))
            link = FILE_URL.format(sid=sid, kind=kind) if sid and kind else ""
            updated = spec.get("updated") or item.get("updated")
            if updated:
                pairs.append(["更新日", str(updated)])
        newest = max(points, key=lambda p: p.period) if points else None
        if newest is not None:
            pairs += [
                ["見出しのセル", newest.src_label],
                ["台帳の単位", series_from_draft(item).unit],
                ["単位のセル", newest.src_unit or "（なし）"],
            ]
    return [p for p in pairs if p[1] != ""], chosen, link


def _point_rows(item: dict[str, Any], points: list[Point]) -> list[dict[str, Any]]:
    s = series_from_draft(item)
    seg = segments(s, points)
    out: list[dict[str, Any]] = []
    prev: Point | None = None
    for p in sorted(points, key=lambda p: p.period):
        if prev is not None and p.period.isdigit() and prev.period.isdigit():
            for y in range(int(prev.period) + 1, min(int(p.period), int(prev.period) + 200)):
                out.append({"period": f"{y:04d}", "label": period_label(f"{y:04d}", s.period_kind),
                            "value": "", "mark": "", "gap": True, "segment": seg[prev.period],
                            "brk": False})  # fmt: skip
        out.append(
            {
                "period": p.period,
                "label": period_label(p.period, s.period_kind),
                "value": p.value,
                "mark": p.mark,
                "gap": False,
                "segment": seg[p.period],
                "brk": prev is not None and seg[p.period] > seg[prev.period],
            }
        )
        prev = p
    return out


def _series_item(
    d: dict[str, Any], excerpt: Callable[[dict[str, Any]], dict[str, Any] | None] | None
) -> dict[str, Any]:
    s = series_from_draft(d)
    points = _points_of(d)
    source, chosen, link = _source_words(d, points)
    ok_trend = bool(s.trend) and bool(s.trend_note.strip()) and verify_trend(s, points)[0]
    meta = {k: v for k, v in (d.get("meta") or {}).items() if k != "times"} or None
    newest = _newest_numeric(points)
    return {
        "k": series_key(d),
        "kind": "series",
        "id": s.series_id,
        "theme": "／".join(s.themes),
        "label": s.label,
        "unit": s.unit,
        "survey": s.survey,
        "publisher": s.publisher,
        "table_title": s.table_title,
        "url": s.url,
        "source_kind": s.source_kind,
        "period_kind": s.period_kind,
        "line": series_line(d, newest),
        # 文の「〇〇は」より後ろ。ページで label を直したときに文を組み直す
        "tail": series_line(d, newest).removeprefix(f"{s.label}は") if newest else "",
        "meta": meta,
        "source": source,
        "chosen": chosen,
        "link": link,
        "selector_state": d.get("selector_state", ""),
        "selector_note": _selector_note(d),
        "held": d.get("ledger_status") == "保留",
        "has_numbers": _newest_numeric(points) is not None,
        "points": _point_rows(d, points),
        "breaks": [{"before": b.before, "note": b.note} for b in s.breaks],
        "excerpt": excerpt(d) if excerpt and s.source_kind == "estat_file" else None,
        "trend": s.trend if ok_trend else "",
        "trend_from": s.trend_from if ok_trend else "",
        "trend_to": s.trend_to if ok_trend else "",
        "trend_note": s.trend_note if ok_trend else "",
        "evidence": d.get("evidence", ""),
        "check_note": d.get("check_note", ""),
        "f": {"label": s.label},
    }


def page_items(
    drafts: dict[str, Any],
    *,
    excerpt: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> list[dict[str, Any]]:
    """ページに出す1件ずつ。統計・研究は下書きの文、民間の調査・有料の論文は書誌だけ。"""
    drafts = normalize_drafts(drafts)
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
    for d in drafts.get("series") or []:
        out.append(_series_item(d, excerpt))
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


def build_page(
    drafts: dict[str, Any],
    *,
    excerpt: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    items: list[dict[str, Any]] | None = None,
) -> str:
    html = TEMPLATE.read_text("utf-8")
    if ITEMS_MARK not in html or MADE_MARK not in html:
        raise ValueError(f"{TEMPLATE.name} に差し込む印がない")
    return html.replace(
        ITEMS_MARK,
        _script_json(items if items is not None else page_items(drafts, excerpt=excerpt)),
    ).replace(MADE_MARK, _script_json(str(drafts.get("made", ""))))


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
    series_added: list[str] = field(default_factory=list)
    series_ai: list[str] = field(default_factory=list)
    # 部分を足した・直した系列で、人の行を AI の行に戻した
    series_downgraded: list[str] = field(default_factory=list)
    # 部分を足した・直した下書きの label が台帳と違った（台帳の label のまま）
    series_label_kept: list[str] = field(default_factory=list)
    # 台帳の系列に「違う」が押されて、保留にした
    series_held: list[str] = field(default_factory=list)


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


def _series_cell(col: str, v: Any) -> str:
    """下書きの値を stat_series.csv の欄の文字にする（selector は JSON のまま）。"""
    if col == "themes" and isinstance(v, list):
        v = ";".join(str(x) for x in v)
    elif isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    return "" if v is None else _text(v) if col != "selector" else str(v)


def _series_row(
    d: dict[str, Any], c: dict[str, Any], *, by: str, day: str, old: dict[str, str] | None
) -> dict[str, str]:
    """stat_series.csv の1行。人が確かめた行は、label の直しを当てたあとの check_sha を入れる。

    status は台帳の行のまま（保留の系列は、「合っている」でも保留のまま。L9）。"""
    row = {col: _series_cell(col, d.get(col, "")) for col in SERIES_COLUMNS}
    fix = c.get("fix") or {}
    if _text(fix.get("label")):
        row["label"] = _text(fix["label"])
    human = by == CHECKER
    s = series_from_draft(row)
    if not (human and c.get("trend") and verify_trend(s, _points_of(d))[0]):
        for col in ("trend", "trend_from", "trend_to", "trend_note"):
            row[col] = ""
    row["status"] = str((old or {}).get("status") or "").strip()
    row["checked_by"], row["checked_at"] = by, day
    row["checked_sha"] = (
        check_sha(series_from_draft(row), _points_of(d), d.get("meta")) if human else ""
    )
    row["note"] = _text(c.get("note", ""))[:200] or row.get("note", "")
    return row


# 部分を足した・直した下書き（growth）から台帳に取る欄（R28）。ほかは台帳の行のまま
GROWTH_FROM_DRAFT = ("selector", "latest_known", "breaks", "where", "note")


def _grown_row(prev: dict[str, str], d: dict[str, Any], *, day: str) -> dict[str, str]:
    """growth の行：台帳の行に、下書きの selector・latest_known・breaks・where・note だけを当てる。

    label（オーナーが直した言い方）・themes・unit・survey・publisher・table_title・period_kind・
    url・status は台帳のまま。増え方は外し、AI の行にする（数を取ってから確かめ直す）。"""
    row = {col: str(prev.get(col) or "") for col in SERIES_COLUMNS}
    for col in GROWTH_FROM_DRAFT:
        if col in d:
            row[col] = _series_cell(col, d[col])
    for col in ("trend", "trend_from", "trend_to", "trend_note"):
        row[col] = ""
    row["checked_by"], row["checked_at"], row["checked_sha"] = AI_CHECKER, day, ""
    return row


def _held_row(prev: dict[str, str], c: dict[str, Any], *, day: str) -> dict[str, str]:
    """台帳の系列に「違う」が押された：保留にして、オーナーのメモを足す（ほかの欄はそのまま）。"""
    row = dict(prev)
    row["status"] = "保留"
    note = _text(c.get("note", ""))[:200]
    mark = f"{day} オーナーが「違う」" + (f"：{note}" if note else "")
    row["note"] = "／".join(x for x in (_text(prev.get("note")), mark) if x)
    return row


def _series_check(
    config_dir: Path,
    rows: list[dict[str, str]],
    point_rows: list[dict[str, str]],
    stats_rows: list[dict[str, str]],
    themes: set[str],
) -> None:
    metas: dict[str, dict] = {}
    for r in rows:
        sid = r.get("series_id", "").strip()
        if r.get("source_kind", "").strip() == "estat_api" and sid and sid not in metas:
            try:
                meta = load_meta(meta_dir_for(config_dir), sid)
            except SeriesError as e:
                raise SourcesError(f"{SERIES_FILE} に問題が出るので書きません：{e}") from e
            if meta is not None:
                metas[sid] = meta
    try:
        blocked = load_blocked_keywords(config_dir)
    except (OSError, KeyError, TypeError, AttributeError, yaml.YAMLError) as e:
        raise SourcesError(f"blocked_keywords.yaml を読めない（{type(e).__name__}）") from e
    problems = series_problems(
        rows,
        point_rows,
        themes=themes,
        legacy_ids={(r.get("stat_id") or "").strip() for r in stats_rows},
        blocked=blocked,
        metas=metas,
    )
    if problems:
        raise SourcesError(f"{SERIES_FILE} に問題が出るので書きません：" + "、".join(problems))


def _replace_all(writes: list[tuple[Path, str]]) -> None:
    """台帳を書く：先に全部を一時ファイル（同じ場所の .tmp）に書き、そのあと1つずつ置き換える。

    一時ファイルを書いている途中で止まれば、どの台帳も前のまま（L10）。"""
    done: list[tuple[Path, Path]] = []
    try:
        for path, text in writes:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8", newline="")
            done.append((tmp, path))
        for tmp, path in done:
            os.replace(tmp, path)
    finally:
        for tmp, _ in done:
            tmp.unlink(missing_ok=True)


def merge_checks(
    drafts: dict[str, Any],
    checks: dict[str, Any],
    config_dir: Path,
    *,
    accept_rest: bool = False,
) -> MergeResult:
    """押した結果を、stats.csv・findings.csv・stat_series.csv（統計の系列）に書く。

    - 人が「合っている」「入れる」を押した行：人が確かめた行（checked_by オーナー・verified）
    - accept_rest のとき、統計と CC BY の研究と統計の系列で押していない行：AI が確かめた行
      （checked_by AI・ai_checked）。台本で使えるが、回で使ったら承認の前に人が確かめる。
      民間の調査と有料の論文は人が数字や一文を書くまで入れない
    - 「違う」「使わない」を押した行は入れない
    - 人が確かめた行を、AI が確かめた行で上書きしない。ただし統計の系列で部分を足した・直した
      下書き（growth）は、accept_rest のとき人の行も AI の行に戻して入れる（series_downgraded。
      人は前の部分の数だけを確かめたので）。growth で下書きから取るのは selector・
      latest_known・breaks・where・note だけ（label などは台帳のまま。R28）
    - 統計の系列の選び方が台帳と違う・台帳より古い・selector がまだ無い・オブジェクトでない
      下書きと、部分がほかの部分と重なる下書きは入れない（理由を skipped に）
    - 台帳にある系列のカード（数が出ていたもの）に「違う」が押されたら、その行を保留にする
      （series_held。メモはオーナーの言葉を足す）
    - どれかの台帳が決まりに合わなくなるなら、どれも書かずに止める（SourcesError）。
      書くときは3つとも一時ファイルに書いてから置き換える
    - 統計の下書きの replaces に書いた古い行（転載サイトを出典にした行など）は、置き換える行を
      入れたときに外す
    - ID と種類（series_id・stat_id・finding_id・lead_id・source_kind・period_kind）は、
      下書きでも台帳でも、前後の空白を取った値で照らし、書く（R26）
    """
    res = MergeResult()
    drafts = with_series_data(normalize_drafts(drafts), config_dir)
    themes = _themes(config_dir)
    ai_day = str(drafts.get("made") or date.today().isoformat())
    by_key: dict[str, tuple[str, dict[str, Any]]] = {}
    for d in drafts["stats"]:
        by_key[stat_key(d)] = ("stat", d)
    for d in drafts["findings"]:
        by_key[finding_key(d)] = ("finding", d)
    for d in drafts["leads"]:
        by_key[lead_key(d)] = (d.get("kind", ""), d)
    for d in drafts["series"]:
        by_key[series_key(d)] = ("series", d)
    res.unknown = [k for k in checks if k not in by_key]
    res.unchecked = sum(1 for k in by_key if k not in checks)

    stats_path = config_dir / "stats.csv"
    cols, old = _read_csv(stats_path)
    old = [_strip_ids(r) for r in old]
    find_path = config_dir / "findings.csv"
    fcols, fold = _read_csv(find_path)
    fold = [_strip_ids(r) for r in fold]
    if fcols and fcols != FINDING_COLUMNS:
        raise SourcesError(f"{find_path.name} の列が決まりと違うので書きません")
    human_stats = {r["stat_id"] for r in old if (r.get("checked_by") or "").strip() == CHECKER}
    human_findings = {r["finding_id"] for r in fold if (r.get("status") or "") == "verified"}
    old_series = [_strip_ids(r) for r in read_series_rows(config_dir)]
    human_series = {
        r["series_id"] for r in old_series if (r.get("checked_by") or "").strip() == CHECKER
    }
    new_series: list[dict[str, str]] = []

    new_stats: list[dict[str, str]] = []
    new_findings: list[dict[str, str]] = []
    removes: set[str] = set()
    ledger_series = {r["series_id"]: r for r in old_series}
    for key, (kind, d) in by_key.items():
        c = checks.get(key)
        name = (
            d.get("stat_id") or d.get("finding_id") or d.get("lead_id") or d.get("series_id") or key
        )
        ng = isinstance(c, dict) and c.get("v") == "ng"
        state = d.get("selector_state") if kind == "series" else None
        if state in _REFUSED_STATES and not ng:
            # 台帳の行の選び方は変えない（AI の行も人の行も）。行も数もそのまま
            res.skipped.append(f"{name}：{_selector_note(d)}")
            continue
        if kind == "series" and d.get("selector_overlap") and not ng:
            # 部分がほかの部分と重なる（R27）。行も数もそのまま
            res.skipped.append(f"{name}：{_selector_note(d)}")
            continue
        if state == "growth" and not ng:
            if not accept_rest:
                res.skipped.append(f"{name}：{SELECTOR_GROWTH_SKIP}")
                continue
            # 部分を足した・直した行は AI の行（人の行も AI に戻す）。数はそのまま。
            # 下書きから取るのは選び方などだけ。label（オーナーが直した言い方）は台帳のまま
            sid = d["series_id"]
            prev = ledger_series.get(sid) or {}
            if sid in human_series:
                res.series_downgraded.append(sid)
            if "label" in d and _text(d.get("label")) != _text(prev.get("label")):
                res.series_label_kept.append(sid)
            new_series.append(_grown_row(prev, d, day=ai_day))
            res.series_ai.append(sid)
            continue
        if kind == "series" and isinstance(c, dict) and c.get("v") == "ok" and not _has_numbers(d):
            # 数が無い系列の「合っている」は押していないのと同じ（数が入ると鍵も変わる）
            res.skipped.append(
                f"{name}：数がまだ無いので、人の確かめには入れなかった（koten stats fetch のあと、"
                "数が入ってから確かめる）"
                + ("" if accept_rest else "。AI の行として入れるなら --accept-rest で取り込む")
            )
            c = None
        if not isinstance(c, dict) or c.get("v") not in ("ok", "ng"):
            if not accept_rest or kind not in ("stat", "finding", "series"):
                continue
            if kind == "series":
                if d["series_id"] in human_series:
                    continue
                # 数がまだ無くても AI の行として入れる（koten stats fetch は台帳の系列だけ取る）
                new_series.append(
                    _series_row(
                        d, {}, by=AI_CHECKER, day=ai_day, old=ledger_series.get(d["series_id"])
                    )
                )
                res.series_ai.append(d["series_id"])
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
                    removes.add(str(d["replaces"]).strip())
            else:
                new_findings.append(
                    _finding_row(d, {}, kind=kind, by=AI_CHECKER, day=ai_day, ai_day=ai_day)
                )
                res.findings_ai.append(d["finding_id"])
            continue
        if c.get("v") == "ng":
            res.rejected.append(name)
            sid = d.get("series_id")
            if kind == "series" and state == "same" and sid in ledger_series:
                # 台帳の系列のカード（台帳の数が出ていた）に「違う」：使わず・取らない（L9）
                new_series.append(_held_row(ledger_series[sid], c, day=_day(c.get("at"))))
                res.series_held.append(sid)
            continue
        day = _day(c.get("at"))
        if kind == "series":
            new_series.append(
                _series_row(d, c, by=CHECKER, day=day, old=ledger_series.get(d["series_id"]))
            )
        elif kind == "stat":
            new_stats.append(_stat_row(d, c, from_lead=False, by=CHECKER, day=day))
            if d.get("replaces"):
                removes.add(str(d["replaces"]).strip())
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

    # 同じ series_id の行は同じ場所で入れ替える（並びを変えない）
    by_new = {r["series_id"]: r for r in new_series}
    series_rows = [by_new.pop(r.get("series_id"), r) for r in old_series]
    series_rows += list(by_new.values())
    point_rows = read_point_rows(config_dir)
    if series_rows:  # stats だけの取り込みでも、stat_id と series_id がぶつかるのを見る
        _series_check(config_dir, series_rows, point_rows, stats_rows, themes)

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

    writes: list[tuple[Path, str]] = []
    if new_stats or res.stats_removed:
        writes.append((stats_path, _csv_text(columns, stats_rows)))
    if new_findings:
        writes.append((find_path, find_text))
    if new_series:
        writes.append((config_dir / SERIES_FILE, _csv_text(SERIES_COLUMNS, series_rows)))
    _replace_all(writes)
    ai_series, held = set(res.series_ai), set(res.series_held)
    res.series_added = [
        r["series_id"] for r in new_series if r["series_id"] not in ai_series | held
    ]
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
    out: dict[str, Any] = {"made": "", "stats": {}, "findings": {}, "leads": {}, "series": {}}
    for path in paths:
        d = read_drafts(path)
        out["made"] = str(d.get("made") or out["made"])
        for part, idk in (
            ("stats", "stat_id"),
            ("findings", "finding_id"),
            ("leads", "lead_id"),
            ("series", "series_id"),
        ):
            for x in d[part]:
                x = _strip_ids(x)
                out[part][x[idk]] = x
    return {**out, **{part: list(out[part].values()) for part in out if part != "made"}}


def todo_drafts(drafts: dict[str, Any], todo: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """回で使った、人がまだ確かめていない行だけのページの材料。

    文と数字は今の台帳（動画で言っている形）、元の資料の場所・該当箇所は下書きから取る。
    下書きに無い行（人が台帳に直に書いた行）も、台帳の文と URL で出す。
    """
    drafts = normalize_drafts(drafts)
    _, stat_rows = _read_csv(config_dir / "stats.csv")
    _, find_rows = _read_csv(config_dir / FINDINGS_FILE)
    ledger_s = {r["stat_id"]: r for r in map(_strip_ids, stat_rows)}
    ledger_f = {r["finding_id"]: r for r in map(_strip_ids, find_rows)}
    draft_s = {d["stat_id"]: d for d in drafts["stats"]}
    draft_f = {d["finding_id"]: d for d in drafts["findings"]}
    todo = {k: [str(x).strip() for x in v] if isinstance(v, list) else v for k, v in todo.items()}
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
    series_rows = [_strip_ids(r) for r in read_series_rows(config_dir)]
    ledger_r = {r["series_id"]: r for r in series_rows}
    draft_r = {d["series_id"]: d for d in drafts.get("series") or []}
    series = []
    for rid in todo.get("series") or []:
        if rid not in ledger_r:
            missing.append(rid)
            continue
        row = {k: v for k, v in ledger_r[rid].items() if k in SERIES_COLUMNS}
        row["themes"] = [x.strip() for x in row["themes"].split(";") if x.strip()]
        keep = {
            k: v
            for k, v in draft_r.get(rid, {}).items()
            if k in ("evidence", "check_note", "selector_names", "stats_data_id", "updated")
        }
        series.append({**keep, **row})
    if missing:
        raise SourcesError(
            f"台帳に無い行があります（{'・'.join(missing)}）：git pull してから作り直す"
        )
    return with_series_data(
        {
            "made": f"回 {todo.get('episode_id', '')}",
            "stats": stats,
            "findings": findings,
            "leads": [],
            "series": series,
        },
        config_dir,
    )
