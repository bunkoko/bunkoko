"""章に付ける背景の事実を選ぶ（企画のとき。SSD の facts 表だけを読む）。

- その章の一節の作品・作者（代表の人物ID）・訳者についての、使える事実（auto・verified）
- 訳書の章には、訳の事実（ここで読むのは○○の訳）を必ず先に置く
- 人が確かめた事実を、自動の事実より先にする。最近10回で使った事実は外す（訳の事実は除く）
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

# 役割ごとの種類の並び（小さいほど先）
KIND_RANK = {
    "translation": 0,
    "work_origin": 1,
    "first_pub": 2,
    "original_title": 3,
    "life_event": 4,
    "residence": 5,
    "relation": 6,
    "era_event": 7,
    "lifespan": 8,
    "years_since": 9,
}
ALWAYS = ("translation",)  # 再使用の間隔を見ない種類（訳者を示す事実）


def canonical(conn: sqlite3.Connection, person_ids: list[str]) -> list[str]:
    if not person_ids:
        return []
    marks = ",".join("?" for _ in person_ids)
    found = {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT person_id, canonical_id FROM persons WHERE person_id IN ({marks})", person_ids
        )
    }
    out: list[str] = []
    for p in person_ids:
        c = found.get(p, p)
        if c not in out:
            out.append(c)
    return out


def recently_used(conn: sqlite3.Connection, n: int) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT u.fact_id FROM fact_usage u WHERE u.episode_id IN ("
        "SELECT episode_id FROM episodes WHERE state != 'discarded' "
        "ORDER BY created_at DESC, episode_id DESC LIMIT ?)",
        (n,),
    ).fetchall()
    return {r[0] for r in rows}


def select_facts(
    conn: sqlite3.Connection,
    *,
    work_ids: list[str],
    person_ids: list[str],
    limit: int,
    exclude: set[str] | None = None,
    taken: set[str] | None = None,
    translator_ids: list[str] | None = None,
) -> list[str]:
    """作品と人物に付いた事実から、limit 件まで選ぶ（並びは KIND_RANK と面白さ）。

    exclude：最近の回で使った事実（訳の事実は除外しない）。taken：この回でもう付けた事実
    （訳の事実も含め、同じ回で2度は付けない）。
    """
    if limit <= 0 or not (work_ids or person_ids):
        return []
    exclude = exclude or set()
    taken = taken or set()
    translators = set(canonical(conn, translator_ids or [])) - set(canonical(conn, person_ids))
    subjects = list(dict.fromkeys(work_ids + canonical(conn, person_ids) + sorted(translators)))
    marks = ",".join("?" for _ in subjects)
    rows = conn.execute(
        f"SELECT * FROM facts WHERE status IN ('auto', 'verified') AND sensitive = 'none' "
        f"AND subject_id IN ({marks})",
        subjects,
    ).fetchall()
    # 時代の事実などは、人が作品・人物に結んだときだけ（links）
    linked = conn.execute(
        "SELECT * FROM facts WHERE status IN ('auto', 'verified') AND sensitive = 'none' "
        "AND links IS NOT NULL AND links != '[]'"
    ).fetchall()
    pool: dict[str, dict[str, Any]] = {r["fact_id"]: dict(r) for r in rows}
    for r in linked:
        if set(json.loads(r["links"] or "[]")) & set(subjects):
            pool.setdefault(r["fact_id"], dict(r))
    picked = [
        f
        for f in pool.values()
        if f["fact_id"] not in taken
        and (f["fact_id"] not in exclude or f["kind"] in ALWAYS)
        # 訳者については、生没年は付けない（話の中心は作者と作品。訳の事実は作品に付く）
        and not (f["subject_id"] in translators and f["kind"] == "lifespan")
    ]
    picked.sort(
        key=lambda f: (
            f["kind"] not in ALWAYS,
            f["origin"] != "curated",
            KIND_RANK.get(f["kind"], 5),
            -int(f["interest"] or 2),
            f["fact_id"],
        )
    )
    return [f["fact_id"] for f in picked[:limit]]
