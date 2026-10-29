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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from koten.check.books import read_books
from koten.facts.dates import era_year
from koten.license import License
from koten.ref.registry import RefWork, read_registry
from koten.ref.texts import (
    ENTRY_COLUMNS,
    IMPORTED,
    RENDERING_COLUMNS,
    USABLE_STATUS,
    Rendering,
    TextsError,
    load_canon,
    passage_problem,
    passage_sort_key,
    read_dict_entries,
    read_renderings,
    same_shape,
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
    tightened: list[str] = field(default_factory=list)  # SSD で使えなくした資料（厳しくなった）


def tighten(conn: sqlite3.Connection, works: list[RefWork]) -> list[str]:
    """台帳で使えなくなった資料（と、台帳から消えた資料）を、SSD ですぐ使えなくする。

    renderings.csv などに行が残っていて作り直しが止まるときも、先にこれだけは行う
    （厳しくなった判定が SSD に届かず、古い本文が台本に使われ続けないように）。
    """
    by_id = {w.ref_id: w for w in works}
    out = []
    with conn:
        for r in conn.execute("SELECT ref_id, license FROM ref_works").fetchall():
            w = by_id.get(r["ref_id"])
            if w is not None and w.usable:
                continue
            if r["license"] in ("PD_JP", "CC0", "CC_BY"):
                out.append(r["ref_id"])
            if w is None:
                conn.execute("DELETE FROM ref_works WHERE ref_id = ?", (r["ref_id"],))
            else:
                conn.execute(
                    "UPDATE ref_works SET license = ?, license_reason = ? WHERE ref_id = ?",
                    (str(w.license), w.computed_reason, w.ref_id),
                )
            for table in ("renderings", "dict_entries"):
                conn.execute(f"DELETE FROM {table} WHERE ref_id = ?", (r["ref_id"],))
    return out


def restrict(works: list[RefWork], reasons: dict[str, str]) -> None:
    """台帳の外で分かった、より厳しい事情（国会図書館のマニフェストの公開範囲など）を当てる。"""
    for w in works:
        if w.ref_id in reasons and w.usable:
            w.license = License.UNKNOWN
            w.computed_reason = f"{w.computed_reason}；{reasons[w.ref_id]}"


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
    conn: sqlite3.Connection,
    config_dir: Path,
    imported: Iterable[Rendering] | None = (),
    *,
    restrictions: dict[str, str] | None = None,
) -> RefBuildResult:
    """imported が None なら（HDD が未接続のとき）、SSD にある取り込んだ本文を残す。

    restrictions：台帳の外で分かった、資料を使えなくする理由（ref_id → 理由）。
    台帳で使えなくなった資料は、ほかの行に問題があって作り直しが止まっても、先に SSD で
    使えなくする（tighten）。止まったときは TextsError に tightened を付ける。
    """
    works = read_registry(config_dir)
    restrict(works, restrictions or {})
    tightened = tighten(conn, works)
    titles = sorted({b.title for b in read_books(config_dir).values()})
    with conn:
        conn.execute("DELETE FROM s4_titles")
        conn.executemany(
            "INSERT OR IGNORE INTO s4_titles(title) VALUES (?)", [(t,) for t in titles]
        )
    by_id = {w.ref_id: w for w in works}
    try:
        renderings = read_renderings(config_dir, by_id, load_canon(config_dir))
        entries = read_dict_entries(config_dir, by_id)
    except TextsError as e:
        e.tightened = tightened  # type: ignore[attr-defined]
        raise
    if imported is None:
        imported = [
            Rendering(**{c: r[c] or "" for c in RENDERING_COLUMNS})
            for r in conn.execute("SELECT * FROM renderings WHERE status = ?", (IMPORTED,))
        ]
    # 取り込んだ本文は、使える資料のものだけ入れる（使えない資料のものは ref build が HDD から消す）
    imported = [r for r in imported if by_id.get(r.ref_id) and by_id[r.ref_id].usable]
    canon = load_canon(config_dir)
    bad = [(r.rendering_id, p) for r in imported if (p := passage_problem(r.passage_key, canon))]
    if bad:
        raise TextsError(f"取り込んだ本文の一節の番号が合わない：{bad[:3]}")
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
        tightened=tightened,
    )


# --- 読む（企画・台本から。SSD だけ） ---------------------------------------


def _usable_join(table: str) -> str:
    # 本文の行と、使える資料の書誌をつなぐ。台帳で使えなくなった資料の行は出さない
    return (
        f"SELECT t.*, w.title AS ref_title, w.volume AS ref_volume, w.edition AS ref_edition, "
        f"w.pub_year AS ref_pub_year, w.credit AS ref_credit, w.license AS license, "
        f"w.kind AS ref_kind, w.pid AS ref_pid, w.url AS ref_url, w.tradition AS ref_tradition, "
        f"w.source AS ref_source, w.first_pub_year AS ref_first_pub_year "
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


def _in_range(num: str, start: str, end: str) -> bool:
    if not (start and end) or not (same_shape(start, num) and same_shape(start, end)):
        return False
    return passage_sort_key(start) <= passage_sort_key(num) <= passage_sort_key(end)


# --- 台本で使う（{{def:}}・{{def_src:}}・{{interp:}}・{{interp_src:}}） -------------

# 打ち直した電子版。年は、取り込んだ版ではなく、訳・著作が最初に出た年で言う
DIGITAL_SOURCES = ("standardebooks", "gutenberg", "aozora")


def load_refs(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict[str, Any]]:
    """辞書の語釈（J-）と時代の解釈（I-）をまとめて引く。使える資料のものだけ。"""
    out = load_entries(conn, [i for i in ids if i.startswith("J-")])
    out.update(load_renderings(conn, [i for i in ids if i.startswith("I-")]))
    return out


def ref_year(r: dict[str, Any]) -> int:
    if r.get("ref_source") in DIGITAL_SOURCES and r.get("ref_first_pub_year"):
        return int(r["ref_first_pub_year"])
    return int(r["ref_pub_year"])


def ref_source_label(r: dict[str, Any]) -> str:
    """語りで出典を言うときの形（例：1891年（明治24年）の『言海』）。"""
    year = ref_year(r)
    era = era_year(year)
    when = f"{year}年（{era}）" if era else f"{year}年"
    return f"{when}の『{r['ref_title']}』"


def ref_text(r: dict[str, Any]) -> str:
    """画面と語りに出す本文（表記を改めたものがあればそれ）。"""
    return r.get("display_text") or r["text"]


def ref_shas(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    return {i: r["text_sha"] for i, r in load_refs(conn, ids).items()}
