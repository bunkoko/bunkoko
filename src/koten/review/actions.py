"""人の確認のあとの、回の状態の変え方（承認・差し戻し・人の一言）。

- 承認：動画ができた回（rendered）で、承認できない理由（blockers）が無いときだけ reviewed にする
- 差し戻し：理由を必ず書き、planned か script_done に戻す（SPEC §8.12）
- 人の一言：文字か、自分の声の wav か、省くかを決める。音声や動画を作ったあとに中身を変えたら
  （声の wav を入れ直したときはいつも）、その音声・動画には入っていないので、script_done に戻して
  音声から作り直してもらう
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

# 回の状態の並び（差し戻しは、いまの状態より前にだけ戻せる）
ORDER = ["planned", "script_done", "voiced", "rendered", "reviewed"]
REJECT_TO = ("planned", "script_done")
# 人の一言を変えられる状態（承認のあとは、先に差し戻す）
TAKE_STATES = {"script_done", "voiced", "rendered"}


class ReviewError(RuntimeError):
    pass


def _row(conn: sqlite3.Connection, episode_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT state, script_json FROM episodes WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    if row is None:
        raise ReviewError(f"{episode_id} という回はありません")
    return row


def approve(
    conn: sqlite3.Connection, episode_id: str, *, note: str = "", blockers: list[str]
) -> None:
    """承認する（rendered → reviewed）。承認できない理由があれば、それを並べて止める。"""
    state = _row(conn, episode_id)["state"]
    if state == "reviewed":
        raise ReviewError(f"{episode_id} はもう承認してあります（reviewed）")
    if state != "rendered":
        raise ReviewError(
            f"{episode_id} は {state} です。承認できるのは、動画ができた回（rendered）だけです"
        )
    if blockers:
        lines = "\n".join(f"- {b}" for b in blockers)
        raise ReviewError(f"{episode_id} はまだ承認できません：\n{lines}")
    note = (note or "").strip()
    msg = "人が確かめて承認" + (f"：{note}" if note else "")
    with conn:
        # 確かめてから書くまでのあいだに状態が変わっていたら、承認しない
        cur = conn.execute(
            "UPDATE episodes SET state = 'reviewed', state_msg = ?, reviewer_note = ? "
            "WHERE episode_id = ? AND state = 'rendered'",
            (msg, note or None, episode_id),
        )
    if cur.rowcount != 1:
        raise ReviewError(
            f"{episode_id} の状態が途中で変わったので、承認しませんでした。確かめ直してください"
        )


def reject(
    conn: sqlite3.Connection, episode_id: str, *, note: str, to_state: str = "script_done"
) -> None:
    """差し戻す。理由（note）は必須。

    戻し先は planned（企画はそのままで、台本を koten script で書き直す）か
    script_done（台本はそのままで、音声から作り直す）。
    """
    if to_state not in REJECT_TO:
        raise ReviewError(
            f"戻し先は planned（台本から書き直す）か script_done（音声から作り直す）です"
            f"（{to_state} は使えません）"
        )
    note = (note or "").strip()
    if not note:
        raise ReviewError("差し戻しには理由が要ります（--note に、何を直すかを書く）")
    row = _row(conn, episode_id)
    state = row["state"]
    if state in ("published", "discarded"):
        raise ReviewError(f"{episode_id} は {state} です。公開済み・取り消した回は差し戻せません")
    if state == "error" and to_state != "planned":
        # 台本の検査（G1・スキーマ）で止まった回の台本を、検査を通さずに script_done にしない
        raise ReviewError(
            f"{episode_id} は error です。止まった回は planned に戻すか、"
            "koten script で作り直してください"
        )
    later = state in ORDER and ORDER.index(state) > ORDER.index(to_state)
    if not (later or state == "error"):
        raise ReviewError(
            f"{episode_id} は {state} です。{to_state} へは、それより先に進んだ回だけを戻せます"
        )
    if to_state == "script_done" and not row["script_json"]:
        raise ReviewError(f"{episode_id} には台本がありません。planned に戻してください")
    with conn:
        conn.execute(
            "UPDATE episodes SET state = ?, state_msg = ?, reviewer_note = ? WHERE episode_id = ?",
            (to_state, f"差し戻し（{state} から）：{note}", note, episode_id),
        )


def _voiced(take: dict[str, Any]) -> tuple[Any, Any]:
    """音声に入る中身（文字と声）。省くときは何も入らない。"""
    if take.get("skip"):
        return (None, None)
    return (take.get("text") or None, take.get("audio_path") or None)


def set_human_take(
    conn: sqlite3.Connection,
    episode_id: str,
    *,
    text: str | None = None,
    audio_path: str | None = None,
    skip: bool = False,
) -> dict[str, Any]:
    """人の一言を決める。episodes.human_take と、台本（script_json）の human_take を同じ中身にする。

    - text：読み上げる文字。audio_path と一緒なら、その声の字幕になる
    - audio_path：自分の声で録った wav（media_dir からの相対パスか、絶対パス）
    - skip：この回は人の一言を入れない（text・audio_path とは一緒に使えない）

    音声（voiced）や動画（rendered）を作ったあとに中身が変わったら（声の wav を入れたときは
    いつも）、script_done に戻す。戻り値は新しい human_take。
    """
    text = (text or "").strip() or None
    audio_path = (audio_path or "").strip() or None
    if skip and (text or audio_path):
        raise ReviewError("省く（--skip）ときは、文字も声も入れません")
    if not skip and not (text or audio_path):
        raise ReviewError(
            "人の一言は、文字（--text）か声の wav（--audio）を入れるか、--skip で省いてください"
        )
    if audio_path and not audio_path.lower().endswith(".wav"):
        raise ReviewError(f"人の一言の声は wav にしてください（{audio_path}）")
    row = _row(conn, episode_id)
    state = row["state"]
    if state not in TAKE_STATES:
        hint = "（承認済みの回は、先に差し戻す）" if state == "reviewed" else ""
        raise ReviewError(
            f"{episode_id} は {state} です。人の一言を決められるのは、台本ができてから"
            f"承認するまで（script_done・voiced・rendered）です{hint}"
        )
    if not row["script_json"]:
        raise ReviewError(f"{episode_id} の台本がありません（koten script が先）")
    script = json.loads(row["script_json"])
    old = dict(script.get("human_take") or {})
    if not old.get("chapter_id"):
        raise ReviewError(
            f"{episode_id} の台本に人の一言の枠がありません（koten script で作り直す）"
        )
    take = {"chapter_id": old["chapter_id"], "text": text, "audio_path": audio_path, "skip": skip}
    script["human_take"] = take
    new_state, msg = state, None
    # 声の wav は、同じパスでも録り直したかもしれない（中身までは比べない）ので、いつも作り直す
    if state != "script_done" and (_voiced(old) != _voiced(take) or audio_path):
        new_state = "script_done"
        msg = "人の一言を変えた。音声から作り直す（koten produce）"
    with conn:
        conn.execute(
            "UPDATE episodes SET human_take = ?, script_json = ?, state = ?, "
            "state_msg = COALESCE(?, state_msg) WHERE episode_id = ?",
            (
                json.dumps(take, ensure_ascii=False),
                json.dumps(script, ensure_ascii=False),
                new_state,
                msg,
                episode_id,
            ),
        )
    return take
