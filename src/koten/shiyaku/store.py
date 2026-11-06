"""自前の訳の表（SSD の shiyaku）を読み書きする。台本で使う「一節の日本語」もここで決める。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from koten.ref.store import load_renderings, ref_source_label, renderings_for_passage
from koten.ref.texts import USABLE_STATUS, text_sha

KINDS = {
    "shiyaku": "外国語の原文からの訳（「試訳」と出す。R5）",
    "relay": "保護期間の切れた外国語の訳からの訳（元の訳の出典だけを出す。D-024）",
    "modern": "日本の古文を今の言葉にした訳（表示なし。D-023）",
}
STATUSES = ("draft", "flagged", "judged", "approved", "rejected")
SHIYAKU_LABEL = "試訳"


class ShiyakuError(ValueError):
    pass


@dataclass
class Shiyaku:
    shiyaku_id: str
    passage_key: str
    kind: str
    text: str
    source_id: str
    source_sha: str
    basis_ids: list[str]
    label: str
    status: str
    drafter: str
    issues: list[str] = field(default_factory=list)
    judge_json: list[dict[str, Any]] | None = None
    judge: str = ""
    divided: bool = False
    created_at: str = ""
    approved_by: str = ""
    approved_at: str = ""
    note: str = ""

    @property
    def text_sha(self) -> str:
        return text_sha(self.text)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def next_id(conn: sqlite3.Connection, passage_key: str) -> str:
    """S-<作品>-<番号>-<連番>（同じ一節の訳を作り直すたびに連番が増える）。"""
    work, _, num = passage_key.partition(":")
    head = f"S-{work}-{num}-"
    used = [
        int(r[0][len(head) :])
        for r in conn.execute(
            "SELECT shiyaku_id FROM shiyaku WHERE shiyaku_id LIKE ?", (head + "%",)
        )
        if r[0][len(head) :].isdigit()
    ]
    return f"{head}{max(used, default=0) + 1}"


def save(conn: sqlite3.Connection, s: Shiyaku) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO shiyaku(shiyaku_id, passage_key, kind, text, source_id, "
            "source_sha, basis_ids, label, status, issues, judge_json, divided, drafter, judge, "
            "created_at, approved_by, approved_at, note, text_sha) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s.shiyaku_id,
                s.passage_key,
                s.kind,
                s.text,
                s.source_id,
                s.source_sha,
                json.dumps(s.basis_ids, ensure_ascii=False),
                s.label,
                s.status,
                json.dumps(s.issues, ensure_ascii=False),
                json.dumps(s.judge_json, ensure_ascii=False) if s.judge_json is not None else None,
                int(s.divided),
                s.drafter,
                s.judge,
                s.created_at or _now(),
                s.approved_by or None,
                s.approved_at or None,
                s.note or None,
                s.text_sha,
            ),
        )


def _from_row(r: sqlite3.Row) -> Shiyaku:
    return Shiyaku(
        shiyaku_id=r["shiyaku_id"],
        passage_key=r["passage_key"],
        kind=r["kind"],
        text=r["text"],
        source_id=r["source_id"],
        source_sha=r["source_sha"],
        basis_ids=json.loads(r["basis_ids"]),
        label=r["label"],
        status=r["status"],
        drafter=r["drafter"],
        issues=json.loads(r["issues"]),
        judge_json=json.loads(r["judge_json"]) if r["judge_json"] else None,
        judge=r["judge"] or "",
        divided=bool(r["divided"]),
        created_at=r["created_at"],
        approved_by=r["approved_by"] or "",
        approved_at=r["approved_at"] or "",
        note=r["note"] or "",
    )


def get(conn: sqlite3.Connection, shiyaku_id: str) -> Shiyaku | None:
    r = conn.execute("SELECT * FROM shiyaku WHERE shiyaku_id = ?", (shiyaku_id,)).fetchone()
    return _from_row(r) if r else None


def list_shiyaku(
    conn: sqlite3.Connection, *, status: str | None = None, passage_key: str | None = None
) -> list[Shiyaku]:
    sql, args = "SELECT * FROM shiyaku WHERE 1 = 1", []
    if status:
        sql += " AND status = ?"
        args.append(status)
    if passage_key:
        sql += " AND passage_key = ?"
        args.append(passage_key)
    return [_from_row(r) for r in conn.execute(sql + " ORDER BY created_at, shiyaku_id", args)]


def source_problem(conn: sqlite3.Connection, s: Shiyaku) -> str | None:
    """訳した元の本文が、今も使えて、訳したときと同じか（変われば訳は使わない）。"""
    src = load_renderings(conn, [s.source_id]).get(s.source_id)
    if src is None:
        return f"元の本文 {s.source_id} が資料の表に無いか、使えない資料になった"
    if src["text_sha"] != s.source_sha:
        return f"元の本文 {s.source_id} が、訳したあとで変わった（訳し直す）"
    return None


def approved_for(conn: sqlite3.Connection, passage_key: str) -> Shiyaku | None:
    """台本で使える自前の訳（承認済みで、元の本文が変わっていない、いちばん新しいもの）。"""
    for s in reversed(list_shiyaku(conn, status="approved", passage_key=passage_key)):
        if source_problem(conn, s) is None:
            return s
    return None


def pd_translation(conn: sqlite3.Connection, passage_key: str) -> dict[str, Any] | None:
    """保護期間の切れた日本語訳（人が画像で確かめたもの）。あれば AI の訳は作らない（D-014）。"""
    for r in renderings_for_passage(conn, passage_key):
        if r["role"] == "translation" and r["lang"] == "ja" and r["status"] == USABLE_STATUS:
            return r
    return None


@dataclass
class Reading:
    """台本で一節の日本語として読むもの（{{quote_ja:候補ID}} に入る）。"""

    kind: str  # pd（保護期間の切れた日本語訳）/ shiyaku / relay / modern
    text: str
    label: str  # 画面と説明欄の表示（コードが付ける）
    ref: str  # I-…（pd）か S-…
    sha: str
    divided: bool = False  # 読み方が分かれる（人が承認のときに確かめた）

    def row(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "label": self.label,
            "sha": self.sha,
            "divided": self.divided,
        }


def reading_for(conn: sqlite3.Connection, passage_key: str) -> Reading | None:
    """一節の日本語：保護期間の切れた日本語訳があればそれ（D-014）、無ければ承認済みの自前の訳。"""
    pd = pd_translation(conn, passage_key)
    if pd is not None:
        text = pd.get("display_text") or pd["text"]
        label = pd.get("ref_credit") or ref_source_label(pd)
        return Reading("pd", text, label, pd["rendering_id"], pd["text_sha"])
    s = approved_for(conn, passage_key)
    if s is not None:
        return Reading(s.kind, s.text, s.label, s.shiyaku_id, s.text_sha, s.divided)
    return None


def approve(
    conn: sqlite3.Connection,
    s: Shiyaku,
    *,
    by: str,
    divided: bool = False,
    note: str = "",
    holds: list[str] | None = None,
) -> None:
    """人の承認。判定役を通ったもの（judged）か、判定役が止めたものを人が読んで決めたもの
    （flagged。note に理由を書く）だけ。照合で食い違い・言い回しが近いものは承認しない。"""
    if s.status not in ("judged", "flagged"):
        raise ShiyakuError(f"{s.shiyaku_id} は {s.status}（承認できるのは judged・flagged）")
    if s.status == "flagged" and not note:
        raise ShiyakuError(
            f"{s.shiyaku_id} は判定役が止めた訳です。読んで承認するときは --note に理由を書く"
        )
    if problem := source_problem(conn, s):
        raise ShiyakuError(problem)
    if holds:
        raise ShiyakuError(
            "今の本との照合で、食い違いか言い回しが近い文があります（訳し直す。SPEC R8）："
            + "、".join(holds)
        )
    s.status = "approved"
    s.approved_by = by
    s.approved_at = _now()
    s.divided = divided
    s.note = note or s.note
    save(conn, s)


def reject(conn: sqlite3.Connection, s: Shiyaku, *, note: str) -> None:
    if s.status == "rejected":
        return
    s.status = "rejected"
    s.note = note or s.note
    save(conn, s)
