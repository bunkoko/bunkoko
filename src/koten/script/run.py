"""台本づくりの流れ（SPEC §8.7・§8.8・§7 状態）：planned → script_done（不合格なら error）。

1. G5（安全）・G2（ライセンス）・G4（再使用間隔）を先に確かめる。不合格なら LLM を呼ばずに止める
2. 章ごとに台本を書かせ、G1（差し込み）と G3（忠実さ）に通す。不合格の章は問題を伝えて
   書き直させる（2回まで）。G1 が直らなければ回を error、G3 が直らなければ人の確認へ
3. 台本JSONをスキーマで検証し、G6（要確認ソース）を付け、verification_json と一緒に保存する
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from koten.script import gates
from koten.script.data import EpisodeData
from koten.script.placeholders import Resolver
from koten.script.writer import ChapterDraft, ScriptConfig, ScriptWriter, load_schema

RUNNABLE_STATES = {"planned", "error"}


class ScriptStateError(RuntimeError):
    pass


@dataclass
class ScriptResult:
    episode_id: str
    state: str
    state_msg: str
    review_required: bool
    script: dict[str, Any] | None
    verification: dict[str, Any]


def schema_errors(script: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(load_schema())
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '（全体）'}：{e.message}"
        for e in sorted(validator.iter_errors(script), key=lambda e: list(e.absolute_path))
    ]


def chapter_length_issue(
    chapter: dict[str, Any], resolver: Resolver, cfg: ScriptConfig
) -> str | None:
    """章の文字数が目安（target_sec × 1分あたりの字数）から大きく外れていれば、直し方を返す。

    一節の長さは書き手が変えられないので、一節だけで上限を超える章は問わない。
    """
    target = chapter["target_sec"] * cfg.chars_per_min / 60
    total = quotes = 0
    for seg in chapter["segments"]:
        try:
            n = len(resolver.resolve(seg["text"]))
        except KeyError:
            n = len(seg["text"])
        total += n
        if seg["kind"] == "quote":
            quotes += n
    upper, lower = target * (1 + cfg.length_tolerance), target * (1 - cfg.length_tolerance)
    if total > upper and quotes < upper:
        return (
            f"長すぎる：約{total}字（目安は一節を含めて{int(target)}字）。"
            f"語りを約{int(total - target)}字削って、{int(target)}字前後にする"
        )
    if total < lower:
        return (
            f"短すぎる：約{total}字（目安{int(target)}字）。語りを足して{int(target)}字前後にする"
        )
    return None


def length_estimate(
    script: dict[str, Any], resolver: Resolver, chars_per_min: int
) -> dict[str, Any]:
    """読み上げる文字数から尺を見積もる（実測は音声合成のあと。G7 は M6）。"""
    chapters = {}
    total_chars = 0
    for ch in script["chapters"]:
        chars = 0
        for seg in ch["segments"]:
            try:
                chars += len(resolver.resolve(seg["text"]))
            except KeyError:
                chars += len(seg["text"])
        chapters[ch["chapter_id"]] = {
            "chars": chars,
            "est_sec": round(chars * 60 / chars_per_min),
            "target_sec": ch["target_sec"],
        }
        total_chars += chars
    return {
        "chars": total_chars,
        "est_sec": round(total_chars * 60 / chars_per_min),
        "chars_per_min": chars_per_min,
        "chapters": chapters,
    }


def run_script(
    conn: sqlite3.Connection,
    ssd_root: Path,
    episode_id: str,
    writer: ScriptWriter,
    judge: gates.FaithfulnessJudge,
    categories: dict[str, list[str]],
    cfg: ScriptConfig,
    *,
    work_episodes: int = 10,
    author_episodes: int = 5,
    force: bool = False,
    log: Callable[[str], None] = lambda _: None,
    now: datetime | None = None,
) -> ScriptResult:
    row = conn.execute("SELECT state FROM episodes WHERE episode_id = ?", (episode_id,)).fetchone()
    if row is None:
        raise ScriptStateError(f"{episode_id} という回はありません")
    if row["state"] not in RUNNABLE_STATES and not force:
        raise ScriptStateError(
            f"{episode_id} は {row['state']} です（台本を作れるのは planned か error。"
            "作り直すときは --force）"
        )
    data = EpisodeData.load(conn, ssd_root, episode_id)
    verification: dict[str, Any] = {
        "created_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "llm": writer.llm.name,
        "judge": judge.llm.name,
    }

    # 1. LLM を呼ぶ前に止めるべきもの
    verification["G5"] = gates.g5_safety(data, categories)
    verification["G2"] = gates.g2_license(data)
    verification["G4"] = gates.g4_recency(conn, data, work_episodes, author_episodes)
    early = [g for g in ("G5", "G2", "G4") if not verification[g]["ok"]]
    if early:
        msg = "；".join(f"{g}：{'、'.join(verification[g]['issues'])}" for g in early)
        if "G5" in early:
            msg = "回ごと中止（安全）。" + msg
        else:
            msg += "（企画からやり直す：koten plan）"
        return _save(conn, episode_id, "error", msg, False, None, verification, log)

    # 2. 章ごとに書かせ、G1・G3 に通す
    drafts: list[ChapterDraft] = []
    g1_issues: list[str] = []
    g3_issues: list[str] = []
    resolver = gates.resolver_for(data)
    length_notes: list[str] = []
    for chapter in data.plan["chapters"]:
        feedback: list[str] | None = None
        for attempt in range(cfg.retries + 1):
            draft = writer.write_chapter(data, chapter, feedback)
            issues1 = gates.g1_chapter(draft.chapter, data, draft.issues)
            issues3: list[str] = []
            if not issues1 and judge.needs_judge(draft.chapter):
                issues3 = judge.judge(draft.chapter, data)
            # 長さは G1・G3 が通ってから見る（尺の本当の確かめは音声のあとの G7）
            length = None
            if not issues1 and not issues3:
                length = chapter_length_issue(draft.chapter, resolver, cfg)
            if not issues1 and not issues3 and length is None:
                break
            feedback = issues1 + issues3 + ([length] if length else [])
            log(
                f"  {chapter['chapter_id']}：書き直し（{attempt + 1}回目）"
                f"{'、'.join(feedback)[:120]}"
            )
        drafts.append(draft)
        g1_issues += issues1
        g3_issues += [f"{chapter['chapter_id']}：{p}" for p in issues3]
        if length:
            length_notes.append(f"{chapter['chapter_id']}：{length}")
        if issues1:
            # 直らない G1 があれば回は error になるので、残りの章は書かせない（利用枠を使わない）
            log(f"  {chapter['chapter_id']}：G1 が直らないので止めます")
            break
        n_seg = len(draft.chapter["segments"])
        log(f"  {chapter['chapter_id']} {draft.chapter['title']}（{n_seg}段落）")

    if g1_issues:
        msg = f"G1：{'、'.join(g1_issues)[:300]}"
        verification["G1"] = gates._gate(g1_issues)
        return _save(conn, episode_id, "error", msg, False, None, verification, log)
    script = writer.assemble(data, drafts)
    verification["G1"] = gates._gate(g1_issues)
    verification["G3"] = gates._gate(g3_issues, needs_human=bool(g3_issues))
    verification["G6"] = gates.g6_review(data, script)
    verification["length_estimate"] = length_estimate(script, resolver, cfg.chars_per_min)
    verification["length_estimate"]["notes"] = length_notes  # 書き直しても目安に収まらなかった章
    review = bool(script["review_required"] or verification["G6"]["review_required"] or g3_issues)
    script["review_required"] = review
    script["verification"] = {
        k: v for k, v in verification.items() if k in ("G1", "G2", "G3", "G4", "G5", "G6")
    }
    errors = schema_errors(script)
    verification["schema"] = gates._gate(errors)

    if g1_issues or errors:
        msg = "；".join(
            [f"G1：{'、'.join(g1_issues)[:300]}"] * bool(g1_issues)
            + [f"スキーマ：{'、'.join(errors)[:300]}"] * bool(errors)
        )
        return _save(conn, episode_id, "error", msg, review, script, verification, log)
    msg = gates.summarize(verification)
    return _save(conn, episode_id, "script_done", msg, review, script, verification, log)


def _save(
    conn: sqlite3.Connection,
    episode_id: str,
    state: str,
    msg: str,
    review: bool,
    script: dict[str, Any] | None,
    verification: dict[str, Any],
    log: Callable[[str], None],
) -> ScriptResult:
    with conn:
        conn.execute(
            "UPDATE episodes SET state = ?, state_msg = ?, review_required = ?, "
            "script_json = COALESCE(?, script_json), verification_json = ? WHERE episode_id = ?",
            (
                state,
                msg,
                int(review),
                json.dumps(script, ensure_ascii=False) if script else None,
                gates.dumps(verification),
                episode_id,
            ),
        )
    log(f"{episode_id}：{state}（{msg}）")
    return ScriptResult(episode_id, state, msg, review, script, verification)


def render(script: dict[str, Any], resolver: Resolver) -> list[str]:
    """確認用に、プレースホルダを差し込んだ台本を行の並びにする。"""
    lines = [f"# {script['title']}（{script['episode_id']}・形式{script['format']}）"]
    for ch in script["chapters"]:
        lines.append(f"\n## {ch['chapter_id']} {ch['title']}（目安 {ch['target_sec']}秒）")
        for seg in ch["segments"]:
            try:
                text = resolver.resolve(seg["text"])
            except KeyError as e:
                text = f"{seg['text']}  ※解決できない：{e}"
            if seg["kind"] == "quote":
                how = (
                    "原文を読む" if seg.get("read_as") == "original" else "画面は原文・読みは現代語"
                )
                lines.append(f"  【一節｜{how}】「{text}」")
            elif seg["kind"] == "stat":
                lines.append(f"  【統計】{text}")
            elif seg["kind"] == "transition":
                lines.append(f"  【章タイトル】{text}")
            else:
                lines.append(f"  {text}")
        if ch["chapter_id"] == script["human_take"]["chapter_id"]:
            lines.append("  【人の一言】（確認のときに入れる。省略もできる）")
    if script.get("comment_prompt"):
        lines.append(f"\nコメントへの問いかけ：{script['comment_prompt']}")
    return lines
