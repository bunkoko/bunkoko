"""資料の台帳と本文を SSD の SQLite に作り直す（koten ref build）と、それを読む関数（M8a）。

入力は config の CSV（ref_works・ref_people・renderings・dict_entries）と、
取り込んだ電子版の本文（imported。古い英訳を巻・節に分けたもの）。
どれかがおかしければ1件も入れ替えない。使えない資料（NEEDS_REVIEW・COPYRIGHTED など）は
書誌だけを入れ、本文は入れない（R2）。台本づくりはこの表だけを読む（HDD は読まない）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from koten.ref.registry import RefWork, read_registry
from koten.ref.texts import (
    ENTRY_COLUMNS,
    RENDERING_COLUMNS,
    USABLE_STATUS,
    Rendering,
    TextsError,
    load_canon,
    read_dict_entries,
    read_renderings,
)

WORK_DB_COLUMNS = [
    "ref_id",
    "source_class",
    "kind",
    "title",
    "volume",
    "edition",
    "pub_year",
    "first_pub_year",
    "publisher",
    "credited_as",
    "lang",
    "source",
    "pid",
    "url",
    "ndl_access",
    "ndl_checked_at",
    "tradition",
    "based_on",
    "credit",
    "license",
    "license_reason",
    "reviewed_by",
    "reviewed_at",
    "review_note",
    "people",
]


@dataclass
class RefBuildResult:
    works: int
    usable: int
    renderings: int
    imported: int
    entries: int


def _work_row(w: RefWork) -> list[Any]:
    values = {
        **{c: getattr(w, c, None) for c in WORK_DB_COLUMNS},
        "based_on": json.dumps(w.based_on, ensure_ascii=False),
        "license": str(w.license),
        "license_reason": w.computed_reason,
        "people": json.dumps(
            [{"name": p.name, "role": p.role, "death_year": p.death_year} for p in w.people],
            ensure_ascii=False,
        ),
    }
    return [values[c] for c in WORK_DB_COLUMNS]


def build_refs(
    conn: sqlite3.Connection, config_dir: Path, imported: Iterable[Rendering] = ()
) -> RefBuildResult:
    works = read_registry(config_dir)
    by_id = {w.ref_id: w for w in works}
    renderings = read_renderings(config_dir, by_id, load_canon(config_dir))
    entries = read_dict_entries(config_dir, by_id)
    imported = [r for r in imported if by_id.get(r.ref_id) and by_id[r.ref_id].usable]
    clash = {r.rendering_id for r in renderings} & {r.rendering_id for r in imported}
    if clash:
        raise TextsError(f"取り込んだ本文と renderings.csv で ID が重なる：{sorted(clash)[:5]}")
    rcols = RENDERING_COLUMNS + ["text_sha"]
    ecols = ENTRY_COLUMNS + ["text_sha"]
    with conn:
        for table in ("ref_works", "renderings", "dict_entries"):
            conn.execute(f"DELETE FROM {table}")
        conn.executemany(
            f"INSERT INTO ref_works({', '.join(WORK_DB_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in WORK_DB_COLUMNS)})",
            [_work_row(w) for w in works],
        )
        conn.executemany(
            f"INSERT INTO renderings({', '.join(rcols)}) VALUES ({', '.join('?' for _ in rcols)})",
            [[r.row()[c] or None for c in rcols] for r in renderings + imported],
        )
        conn.executemany(
            f"INSERT INTO dict_entries({', '.join(ecols)}) "
            f"VALUES ({', '.join('?' for _ in ecols)})",
            [[e.row()[c] or None for c in ecols] for e in entries],
        )
    return RefBuildResult(
        works=len(works),
        usable=sum(w.usable for w in works),
        renderings=len(renderings),
        imported=len(imported),
        entries=len(entries),
    )


# --- 読む（企画・台本から。SSD だけ） ---------------------------------------


def _usable_join(table: str) -> str:
    # 本文の行と、使える資料の書誌をつなぐ。台帳で使えなくなった資料の行は出さない
    return (
        f"SELECT t.*, w.title AS ref_title, w.volume AS ref_volume, w.edition AS ref_edition, "
        f"w.pub_year AS ref_pub_year, w.credit AS ref_credit, w.license AS license, "
        f"w.kind AS ref_kind, w.pid AS ref_pid, w.url AS ref_url, w.tradition AS ref_tradition "
        f"FROM {table} t JOIN ref_works w ON w.ref_id = t.ref_id "
        f"WHERE w.license IN ('PD_JP', 'CC0', 'CC_BY')"
    )


def load_entries(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict[str, Any]]:
    """語釈を ID で引く（verified でなくても返す。使えるかは呼ぶ側が status で見る）。"""
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    sql = _usable_join("dict_entries") + f" AND t.entry_id IN ({marks})"
    return {r["entry_id"]: dict(r) for r in conn.execute(sql, ids)}


def load_renderings(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    sql = _usable_join("renderings") + f" AND t.rendering_id IN ({marks})"
    return {r["rendering_id"]: dict(r) for r in conn.execute(sql, ids)}


def entries_for_words(
    conn: sqlite3.Connection, text: str, *, verified_only: bool = True
) -> list[dict[str, Any]]:
    """文に出てくる見出し語の語釈（長い見出し語から。刊行年の古い順）。"""
    sql = _usable_join("dict_entries")
    if verified_only:
        sql += f" AND t.status = '{USABLE_STATUS}'"
    rows = [dict(r) for r in conn.execute(sql) if r["headword"] in text]
    return sorted(
        rows,
        key=lambda r: (-len(r["headword"]), r["ref_pub_year"], r["sense"] or "", r["entry_id"]),
    )


def renderings_for_passage(
    conn: sqlite3.Connection, passage_key: str, *, verified_only: bool = True
) -> list[dict[str, Any]]:
    """一節に結び付いた原文・訓読・訳・注釈（刊行年の古い順）。範囲で結び付けたものも含む。"""
    work, _, num = passage_key.partition(":")
    sql = _usable_join("renderings") + " AND (t.passage_key = ? OR (t.passage_to IS NOT NULL "
    sql += "AND t.passage_key LIKE ?))"
    out = []
    for r in conn.execute(sql, (passage_key, f"{work}:%")):
        r = dict(r)
        if r["passage_key"] != passage_key and not _in_range(
            num, r["passage_key"].partition(":")[2], (r["passage_to"] or "").partition(":")[2]
        ):
            continue
        if verified_only and r["status"] != USABLE_STATUS:
            continue
        out.append(r)
    return sorted(out, key=lambda r: (r["ref_pub_year"], r["rendering_id"]))


def _num_key(n: str) -> tuple:
    return tuple(
        int(p) if p.isdigit() else p for p in n.replace("a", ".0").replace("b", ".1").split(".")
    )


def _in_range(num: str, start: str, end: str) -> bool:
    if not (start and end):
        return False
    try:
        return _num_key(start) <= _num_key(num) <= _num_key(end)
    except TypeError:
        return False
