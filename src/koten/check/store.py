"""照合の判定を残し、公開を保留する回を見つける（SPEC R8・D-017）。

決まり（2026-10-29 利用者）：
- 今の本と内容が同じ（same）、触れていない（not_covered）なら、何も変えない
- 内容が食い違う（differs）か、台本の文が本の言い回しに近いなら、その回の公開を保留する
- 保留は、その文を直してから同じ本でもう一度照合し、same か not_covered になれば解ける。
  文が台本から消えたときは、koten check resolve で理由を書いて解く
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from koten.check.claims import Claim

HOLD_VERDICTS = ("differs",)
NOTE_MAX = 60


@dataclass(frozen=True)
class Hold:
    claim_key: str
    book_id: str
    reason: str
    text_changed: bool  # 食い違いのあと、文が変わったか消えた


def record(
    conn: sqlite3.Connection,
    episode_id: str,
    claim: Claim,
    *,
    book_id: str,
    book_label: str,
    locator: str,
    verdict: str,
    close_wording: bool,
    run_len: int | None,
    share: float | None,
    method: str,
    checked_by: str,
    note: str = "",
    now: datetime | None = None,
) -> int:
    if len(note) > NOTE_MAX:
        raise ValueError(f"メモは{NOTE_MAX}字まで（本の文を書かない）")
    with conn:
        cur = conn.execute(
            "INSERT INTO checks(episode_id, claim_key, claim_sha, claim_text, book_id, "
            "book_label, locator, verdict, close_wording, run_len, share, method, checked_by, "
            "checked_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                episode_id,
                claim.key,
                claim.sha,
                claim.text,
                book_id,
                book_label,
                locator,
                verdict,
                int(close_wording),
                run_len,
                share,
                method,
                checked_by,
                (now or datetime.now(UTC)).isoformat(timespec="seconds"),
                note or None,
            ),
        )
    return int(cur.lastrowid)


def latest(conn: sqlite3.Connection, episode_id: str) -> dict[tuple[str, str], dict[str, Any]]:
    """文と本の組ごとの、いちばん新しい判定。"""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT * FROM checks WHERE episode_id = ? ORDER BY check_id", (episode_id,)
    ):
        out[(r["claim_key"], r["book_id"])] = dict(r)
    return out


def _bad(r: dict[str, Any]) -> bool:
    return r["verdict"] in HOLD_VERDICTS or (
        r["verdict"] != "resolved" and bool(r["close_wording"])
    )


def _good(r: dict[str, Any]) -> bool:
    return r["verdict"] in ("same", "not_covered") and not r["close_wording"]


def holds(conn: sqlite3.Connection, episode_id: str, current: list[Claim]) -> list[Hold]:
    """公開を保留する理由（空なら保留しない）。

    current は今の台本の全部の文（claims(..., everything=True)）。保留は文の中身（指紋）に付く：
    - 食い違い・言い回しが近いと判定した文が、台本のどこかに同じ中身で残っていれば保留
      （文の位置が変わっても、人の判定で一致にしても、文を直さないかぎり解けない）
    - 文が変わった・消えたときは、同じ本で、同じ場所（ch2-s3#1）の文を照合し直して一致か
      触れていないになるか、resolve で理由を書くまで保留
    """
    by_sha = {c.sha: c for c in current}
    at_key = {c.key: c for c in current}
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM checks WHERE episode_id = ? ORDER BY check_id", (episode_id,)
        )
    ]
    out: list[Hold] = []
    seen: set[tuple[str, str]] = set()
    for i, r in enumerate(rows):
        if not _bad(r) or (r["claim_sha"], r["book_id"]) in seen:
            continue
        what = "内容が食い違う" if r["verdict"] in HOLD_VERDICTS else "言い回しが本に近い"
        where = f"{r['book_label']} {r['locator']} と{what}"
        cur = by_sha.get(r["claim_sha"])
        if cur is not None:
            seen.add((r["claim_sha"], r["book_id"]))
            out.append(Hold(cur.key, r["book_id"], f"{cur.number}（{cur.key}）：{where}", False))
            continue
        later = [x for x in rows[i + 1 :] if x["book_id"] == r["book_id"]]
        if any(
            x["claim_key"] == r["claim_key"] and (_good(x) or x["verdict"] == "resolved")
            for x in later
        ):
            continue
        seen.add((r["claim_sha"], r["book_id"]))
        now = at_key.get(r["claim_key"])
        if now is not None:
            reason = (
                f"{now.number}（{r['claim_key']}）：{where}。そのあと文が書き直された。"
                "同じ本でもう一度照合する"
            )
        else:
            reason = (
                f"（消えた文）（{r['claim_key']}）：{where}。そのあと文が台本から消えた。"
                "koten check resolve で理由を書いて解く"
            )
        out.append(Hold(r["claim_key"], r["book_id"], reason, True))
    return out


def summary(conn: sqlite3.Connection, episode_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in latest(conn, episode_id).values():
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return counts


def resolve_gone(
    conn: sqlite3.Connection,
    episode_id: str,
    current: list[Claim],
    claim_key: str,
    *,
    checked_by: str,
    note: str,
    now: datetime | None = None,
) -> int:
    """台本から消えた文の保留を解く（文が残っているあいだは、もう一度照合して解く）。"""
    if not note.strip():
        raise ValueError("理由（--note）を書く")
    if len(note) > NOTE_MAX:
        raise ValueError(f"理由は{NOTE_MAX}字まで")
    if any(c.key == claim_key for c in current):
        raise ValueError(
            f"{claim_key} の文はまだ台本にあります。直してから同じ本でもう一度照合してください"
        )
    held = [h for h in holds(conn, episode_id, current) if h.claim_key == claim_key]
    if any(not h.text_changed for h in held):
        raise ValueError("食い違った文が、台本の別の場所に同じ中身で残っています（check status）")
    books = {h.book_id for h in held}
    rows = [
        r
        for (key, book), r in latest(conn, episode_id).items()
        if key == claim_key and book in books
    ]
    if not rows:
        raise ValueError(f"{claim_key} に解く保留はありません")
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    with conn:
        for r in rows:
            conn.execute(
                "INSERT INTO checks(episode_id, claim_key, claim_sha, claim_text, book_id, "
                "book_label, locator, verdict, close_wording, method, checked_by, checked_at, "
                "note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'resolved', 0, 'human', ?, ?, ?)",
                (
                    episode_id,
                    claim_key,
                    r["claim_sha"],
                    r["claim_text"],
                    r["book_id"],
                    r["book_label"],
                    r["locator"],
                    checked_by,
                    stamp,
                    note.strip(),
                ),
            )
    return len(rows)
