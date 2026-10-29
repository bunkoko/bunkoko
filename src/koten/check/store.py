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


def holds(conn: sqlite3.Connection, episode_id: str, current: list[Claim]) -> list[Hold]:
    """公開を保留する理由（空なら保留しない）。

    current は今の台本の全部の文（claims(..., everything=True)）。
    """
    now = {c.key: c for c in current}
    out = []
    for (key, book_id), r in latest(conn, episode_id).items():
        bad = r["verdict"] in HOLD_VERDICTS or (r["verdict"] != "resolved" and r["close_wording"])
        if not bad:
            continue
        what = "内容が食い違う" if r["verdict"] in HOLD_VERDICTS else "言い回しが本に近い"
        cur = now.get(key)
        if cur is not None and cur.sha == r["claim_sha"]:
            reason = f"{cur.number}（{key}）：{r['book_label']} {r['locator']} と{what}"
            out.append(Hold(key, book_id, reason, False))
        else:
            state = "書き直した" if cur is not None else "台本から消えた"
            number = cur.number if cur is not None else "（消えた文）"
            reason = (
                f"{number}（{key}）：{r['book_label']} {r['locator']} と{what}。"
                f"そのあと文が{state}。"
                + (
                    "同じ本でもう一度照合する"
                    if cur is not None
                    else "koten check resolve で理由を書いて解く"
                )
            )
            out.append(Hold(key, book_id, reason, True))
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
    rows = [
        r
        for (key, _), r in latest(conn, episode_id).items()
        if key == claim_key and r["verdict"] != "resolved"
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
