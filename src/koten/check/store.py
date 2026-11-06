"""照合の判定を残し、公開を保留する回を見つける（SPEC R8・D-017）。

決まり（2026-10-29 利用者）：
- 今の本と内容が同じ（same）、触れていない（not_covered）なら、何も変えない
- 内容が食い違う（differs）か、台本の文が本の言い回しに近いなら、その回の公開を保留する
- 保留は、その文を直してから同じ本でもう一度照合し、same か not_covered になれば解ける。
  文が台本から消えたときは、koten check resolve で理由を書いて解く
- 自前の訳（M8b）も、訳の文ごとに照合できる（任意。利用者 2026-11-06）。記録は episode_id を
  空にし、shiyaku_id に訳の ID を入れる。保留のある訳は承認できず、使った回も保留する
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
    shiyaku_id: str | None = None,
) -> int:
    """判定を1つ残す。自前の訳の照合のときは episode_id を空にし、shiyaku_id を渡す。"""
    if len(note) > NOTE_MAX:
        raise ValueError(f"メモは{NOTE_MAX}字まで（本の文を書かない）")
    if bool(episode_id) == bool(shiyaku_id):
        raise ValueError("回の ID か自前の訳の ID の、どちらか1つを渡す")
    with conn:
        cur = conn.execute(
            "INSERT INTO checks(episode_id, claim_key, claim_sha, claim_text, book_id, "
            "book_label, locator, verdict, close_wording, run_len, share, method, checked_by, "
            "checked_at, note, shiyaku_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                episode_id or "",
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
                shiyaku_id,
            ),
        )
    return int(cur.lastrowid)


def _rows(conn: sqlite3.Connection, episode_id: str, shiyaku_id: str | None) -> list[dict]:
    if shiyaku_id:
        sql, arg = "SELECT * FROM checks WHERE shiyaku_id = ? ORDER BY check_id", shiyaku_id
    else:
        sql, arg = "SELECT * FROM checks WHERE episode_id = ? ORDER BY check_id", episode_id
    return [dict(r) for r in conn.execute(sql, (arg,))]


def latest(
    conn: sqlite3.Connection, episode_id: str, *, shiyaku_id: str | None = None
) -> dict[tuple[str, str], dict[str, Any]]:
    """文と本の組ごとの、いちばん新しい判定。"""
    return {(r["claim_key"], r["book_id"]): r for r in _rows(conn, episode_id, shiyaku_id)}


def _bad(r: dict[str, Any]) -> bool:
    return r["verdict"] in HOLD_VERDICTS or (
        r["verdict"] != "resolved" and bool(r["close_wording"])
    )


def _good(r: dict[str, Any]) -> bool:
    return r["verdict"] in ("same", "not_covered") and not r["close_wording"]


def holds(
    conn: sqlite3.Connection,
    episode_id: str,
    current: list[Claim],
    *,
    shiyaku_id: str | None = None,
) -> list[Hold]:
    """公開を保留する理由（空なら保留しない）。

    current は今の台本の全部の文（claims(..., everything=True)）。保留は文の中身（指紋）に付く：
    - 食い違い・言い回しが近いと判定した文が、台本のどこかに同じ中身で残っていれば保留
      （文の位置が変わっても、人の判定で一致にしても、文を直さないかぎり解けない）
    - 文が変わった・消えたときは、同じ本で、同じ場所（ch2-s3#1）の文を照合し直して一致か
      触れていないになるか、resolve で理由を書くまで保留
    """
    by_sha = {c.sha: c for c in current}
    at_key = {c.key: c for c in current}
    rows = _rows(conn, episode_id, shiyaku_id)
    if shiyaku_id and by_sha:
        # 訳し直しで ID が変わっても、同じ一節のほかの訳で、食い違い・本に近いと出た文と同じ文が
        # あれば保留する（文を直さないかぎり解けない。SPEC R8）
        marks = ",".join("?" * len(by_sha))
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT c.* FROM checks c JOIN shiyaku s ON s.shiyaku_id = c.shiyaku_id "
                "WHERE c.shiyaku_id != ? AND s.passage_key = "
                "(SELECT passage_key FROM shiyaku WHERE shiyaku_id = ?) "
                f"AND c.claim_sha IN ({marks}) ORDER BY c.check_id",
                (shiyaku_id, shiyaku_id, *by_sha),
            )
            if _bad(dict(r))
        ] + rows
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


def translation_holds(
    conn: sqlite3.Connection, script: dict[str, Any], readings: dict[str, Any] | None = None
) -> list[Hold]:
    """台本で使った自前の訳（M8b）の、照合の保留と、使えなくなった・変わった訳。

    - 書いたときの自前の訳が、承認済みでなくなった・元の本文が使えなくなった
    - 今の一節の日本語が、書いたときの訳と違う（readings を渡したとき。書き直すまで公開しない）
    - 書いたときの訳と今の訳の、照合の保留
    """
    from koten.check.claims import text_claims
    from koten.shiyaku.store import get, pinned, reading_drift, source_problem

    out: list[Hold] = []
    sids: set[str] = set()
    for cid, tr in pinned(script).items():
        ref = tr["ref"]
        if ref.startswith("S-"):
            sids.add(ref)
            sy = get(conn, ref)
            if sy is None or sy.status != "approved":
                status = sy.status if sy else "無い"
                out.append(Hold(cid, "", f"訳 {ref} が使えなくなった（{status}）", True))
            elif (why := source_problem(conn, sy)) is not None:
                out.append(Hold(cid, "", f"訳 {ref}：{why}", True))
        if readings is not None and (cur := readings.get(cid)) is not None:
            if cur.ref.startswith("S-"):
                sids.add(cur.ref)
    if readings is not None:
        out += [Hold("", "", x, True) for x in reading_drift(script, readings)]
    for sid in sorted(sids):
        row = conn.execute("SELECT text FROM shiyaku WHERE shiyaku_id = ?", (sid,)).fetchone()
        if row is None:
            continue
        for h in holds(conn, "", text_claims(sid, row["text"]), shiyaku_id=sid):
            out.append(Hold(h.claim_key, h.book_id, f"訳 {sid} の {h.reason}", h.text_changed))
    return out
