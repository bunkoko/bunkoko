"""台本づくりで使うデータ（一節・統計・悩み）。

SSD の候補表と SQLite だけを読む（HDD は読まない）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from koten.index.build import cache_path


def load_candidates(ssd_root: Path, candidate_ids: list[str]) -> dict[str, dict[str, Any]]:
    """SSD の候補表から、指定した候補だけを読む。"""
    if not candidate_ids:
        return {}
    table = pq.read_table(cache_path(ssd_root), filters=[("candidate_id", "in", candidate_ids)])
    return {r["candidate_id"]: r for r in table.to_pylist()}


@dataclass
class EpisodeData:
    episode_id: str
    plan: dict[str, Any]
    candidates: dict[str, dict[str, Any]]
    stats: dict[str, dict[str, Any]]
    worries: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, conn: sqlite3.Connection, ssd_root: Path, episode_id: str) -> EpisodeData:
        row = conn.execute("SELECT * FROM episodes WHERE episode_id = ?", (episode_id,)).fetchone()
        if row is None:
            raise KeyError(f"{episode_id} という回はありません")
        plan = json.loads(row["plan_json"])
        ids = [s["candidate_id"] for s in plan["selection"]]
        stat_ids = sorted({sid for c in plan["chapters"] for sid in c.get("stat_ids", [])})
        stats = {}
        if stat_ids:
            marks = ",".join("?" for _ in stat_ids)
            stats = {
                r["stat_id"]: dict(r)
                for r in conn.execute(f"SELECT * FROM stats WHERE stat_id IN ({marks})", stat_ids)
            }
        worry_ids = [w["worry_id"] for w in plan["worries"]]
        marks = ",".join("?" for _ in worry_ids)
        worries = {
            r["worry_id"]: dict(r)
            for r in conn.execute(f"SELECT * FROM worries WHERE worry_id IN ({marks})", worry_ids)
        }
        return cls(episode_id, plan, load_candidates(ssd_root, ids), stats, worries)
