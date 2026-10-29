"""台本づくりで使うデータ（一節・統計・悩み・背景の事実・昔の辞書の語釈・時代の解釈）。

SSD の候補表と SQLite だけを読む（HDD は読まない）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from koten.facts.model import Fact
from koten.facts.store import load_facts, load_sources, text_sha
from koten.index.build import cache_path
from koten.ref.store import load_refs
from koten.ref.texts import USABLE_STATUS


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
    facts: dict[str, Fact] = field(default_factory=dict)  # 企画に付いた事実のうち、今も使えるもの
    fact_sources: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    fact_issues: list[str] = field(default_factory=list)  # 使えなくなった・文が変わった事実
    person_names: list[str] = field(default_factory=list)  # 書誌にある人名（語りでの直書きを見る）
    # 企画に付いた昔の辞書の語釈（J-）・時代の解釈（I-）のうち、今も使えて確かめ済みのもの
    refs: dict[str, dict[str, Any]] = field(default_factory=dict)
    ref_issues: list[str] = field(default_factory=list)

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
        fact_ids = list(dict.fromkeys(f for c in plan["chapters"] for f in c.get("fact_ids", [])))
        loaded = load_facts(conn, fact_ids)
        facts, issues = {}, []
        for fid in fact_ids:
            f = loaded.get(fid)
            if f is None:
                issues.append(f"{fid}：事実の表に無い（koten facts build で消えた）")
            elif not f.usable:
                issues.append(f"{fid}：使えない状態（{f.status}・{f.sensitive}）")
            else:
                if (plan.get("facts") or {}).get(fid) not in (None, text_sha(f.text_ja)):
                    issues.append(f"{fid}：企画のあとで事実の文が変わった（今の文を使う）")
                facts[fid] = f
        names = [
            r[0] for r in conn.execute("SELECT DISTINCT name FROM persons WHERE length(name) >= 3")
        ]
        ref_ids = list(dict.fromkeys(i for c in plan["chapters"] for i in c.get("ref_ids", [])))
        found = load_refs(conn, ref_ids)
        refs, ref_issues = {}, []
        for rid in ref_ids:
            r = found.get(rid)
            if r is None:
                ref_issues.append(
                    f"{rid}：資料の表に無いか、使えない資料になった（koten ref build）"
                )
            elif r["status"] != USABLE_STATUS:
                ref_issues.append(f"{rid}：まだ画像と照らし合わせていない（{r['status']}）")
            else:
                if (plan.get("refs") or {}).get(rid) not in (None, r["text_sha"]):
                    ref_issues.append(f"{rid}：企画のあとで本文が変わった（今の本文を使う）")
                refs[rid] = r
        return cls(
            episode_id,
            plan,
            load_candidates(ssd_root, ids),
            stats,
            worries,
            facts=facts,
            fact_sources=load_sources(conn, list(facts)),
            fact_issues=issues,
            person_names=names,
            refs=refs,
            ref_issues=ref_issues,
        )
