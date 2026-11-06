"""訳の材料を集める：何から訳すか（元の本文）と、下書き・判定に見せる時代の解釈（S2）。

- 外国語の作品（論語など）で、人が影印から起こした原文（verified）があれば、原文から訳す（shiyaku）
- 原文が無く、保護期間の切れた外国語の訳（ロングの英訳など）があれば、その訳から訳す（relay）。
  打ち直した電子版（Standard Ebooks・Gutenberg）の訳は、取り込んだまま（imported）でも元にできる
  （decisions.md 2026-10-29：取り込んだ本文は試訳の材料にする）。画面には元の訳の出典が出る
- 日本の古文は、人が起こした原文（verified）から今の言葉にする（modern）
- 保護期間の切れた日本語訳（verified）がある一節は、訳を作らない（D-014）
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from koten.ref.store import DIGITAL_SOURCES, ref_source_label, renderings_for_passage
from koten.ref.texts import IMPORTED, ROLES, USABLE_STATUS, passage_problem
from koten.shiyaku.store import SHIYAKU_LABEL, ShiyakuError, pd_translation

# 時代の解釈として下書き・判定に見せる役割（原文は元の本文として別に見せる）
BASIS_ROLES = ("kundoku", "translation", "commentary")


@dataclass
class Material:
    passage_key: str
    title: str  # 作品の名前（canon.yaml）
    work_lang: str  # 作品の言葉（canon.yaml）
    kind: str  # shiyaku / relay / modern
    source: dict[str, Any]  # 訳す元の本文（renderings の行）
    basis: list[dict[str, Any]]  # 見せる時代の解釈（元の本文のほか）
    label: str  # 画面と説明欄の表示

    def versions(self) -> int:
        return independent_versions(self.basis)


def _usable_text(r: dict[str, Any], *, digital_ok: bool) -> bool:
    if r["status"] == USABLE_STATUS:
        return True
    return digital_ok and r["status"] == IMPORTED and r.get("ref_source") in DIGITAL_SOURCES


def independent_versions(rows: list[dict[str, Any]]) -> int:
    """互いに独立した版の数。同じ資料・元にした資料（based_on）でつながる資料・同じ注の流れ
    （tradition）は1つと数える（sources_strategy_plan §11）。"""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    refs = {r["ref_id"]: r for r in rows}
    for rid, r in refs.items():
        find(rid)
        for b in json.loads(r.get("ref_based_on") or "[]"):
            union(rid, b)
        tradition = (r.get("ref_tradition") or "").strip()
        if tradition:
            union(rid, f"tradition:{tradition}")
    return len({find(rid) for rid in refs})


def gather(
    conn: sqlite3.Connection,
    canon: dict[str, dict[str, Any]],
    passage_key: str,
    *,
    min_versions: int = 2,
) -> Material:
    if problem := passage_problem(passage_key, canon):
        raise ShiyakuError(problem)
    work = canon[passage_key.partition(":")[0]]
    lang = work.get("lang") or "ja"
    if pd := pd_translation(conn, passage_key):
        raise ShiyakuError(
            f"{passage_key} には保護期間の切れた日本語訳 {pd['rendering_id']}"
            f"（{pd.get('ref_credit') or ref_source_label(pd)}）があります。"
            "その訳を出典つきで使い、AI の訳は作りません（D-014）"
        )
    rows = renderings_for_passage(conn, passage_key, verified_only=False)
    originals = [
        r
        for r in rows
        if r["role"] == "original" and r["lang"] == lang and r["status"] == USABLE_STATUS
    ]
    if lang == "ja":
        kind = "modern"
        if not originals:
            raise ShiyakuError(
                f"{passage_key}（{work['title']}）の原文が、人が確かめた形（verified）で"
                "renderings.csv にありません"
            )
        source = originals[0]
    elif originals:
        kind, source = "shiyaku", originals[0]
    else:
        relays = [
            r
            for r in rows
            if r["role"] == "translation"
            and r["lang"] not in ("ja", lang)
            and _usable_text(r, digital_ok=True)
        ]
        if not relays:
            raise ShiyakuError(
                f"{passage_key}（{work['title']}）は、訳の元にできる本文がありません"
                "（人が起こした原文か、保護期間の切れた外国語の訳。renderings.csv か"
                " koten ref fetch）"
            )
        # 古い訳ほど先（ref_year の順で並んでいる）。2つ以上あれば最初のものを元にし、ほかは見せる
        kind, source = "relay", relays[0]
    basis = [
        r
        for r in rows
        if r["rendering_id"] != source["rendering_id"]
        and r["role"] in BASIS_ROLES
        and _usable_text(r, digital_ok=True)
    ]
    if kind in ("shiyaku", "modern") and independent_versions(basis) < min_versions:
        raise ShiyakuError(
            f"{passage_key} の時代の解釈（訓読・訳・注釈）が、互いに独立した版で{min_versions}つ"
            f"以上要ります（今は{independent_versions(basis)}つ。based_on・tradition でつながる"
            "資料は1つと数える）"
        )
    if kind == "relay":
        label = (source.get("ref_credit") or "").strip()
        if not label:
            raise ShiyakuError(
                f"元の訳の資料 {source['ref_id']} の credit（「ロングの英訳（1862）」の形）が空です"
            )
    else:
        label = SHIYAKU_LABEL if kind == "shiyaku" else ""
    return Material(passage_key, work["title"], lang, kind, source, basis, label)


def describe(r: dict[str, Any]) -> str:
    """下書き・判定に見せる版の見出し（ID・役割・資料・年）。"""
    role = ROLES.get(r["role"], r["role"])
    return f"{r['rendering_id']}（{role}・{ref_source_label(r)}・{r['lang']}）"
