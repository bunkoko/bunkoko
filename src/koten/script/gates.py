"""検証ゲート G1〜G6（SPEC §8.8）。G7（尺）は音声合成のあと（M6）。

各ゲートは {"ok": bool, "issues": [...]} を返す。結果は verification_json に保存し、確認画面に出す。
- G1 差し込み：全プレースホルダが解決でき、「」の中は差し込んだ引用か悩み文だけ。
  著者名・作品名・統計の数字を直接書いていない（R6）
- G2 ライセンス：使う一節が全て使用可（PD_JP・CC0・CC_BY）。外国語は自前訳が要る（R5）
- G3 忠実さ：一節の解説が一節と前後の文脈から言える範囲（別の LLM が判定）
- G4 再使用間隔：主役の作品・著者が直近の回と重ならない
- G5 安全：悩みが blocked でなく除外キーワードに当たらず、一節に caution がない
- G6 要確認ソース：要確認の一節・自前訳を含めば review_required
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from koten.license import is_usable
from koten.llm import ChatLLM, fill, load_prompt, parse_json_object
from koten.script.data import EpisodeData
from koten.script.placeholders import PLACEHOLDER, Resolver
from koten.textnorm import normalize_for_match

QUOTED = re.compile(r"「([^「」]*)」")
SHORT_QUOTE_MAX = 10  # 「」で語を強調するだけなら許す長さ
JUDGED_ROLES = {"work_intro", "summary", "compare"}  # 一節を持たないが、一節に触れる章

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "faithful": {"type": "boolean"},
        "problems": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["faithful", "problems"],
    "additionalProperties": False,
}


def resolver_for(data: EpisodeData) -> Resolver:
    worries = {wid: w["text_general"] for wid, w in data.worries.items()}
    return Resolver(data.candidates, data.stats, worries)


def _gate(issues: list[str], **extra: Any) -> dict[str, Any]:
    return {"ok": not issues, "issues": issues, **extra}


# --- G1 ---------------------------------------------------------------------


def g1_chapter(chapter: dict[str, Any], data: EpisodeData, shape_issues: list[str]) -> list[str]:
    resolver = resolver_for(data)
    issues = [f"{chapter['chapter_id']}：{x}" for x in shape_issues]
    quotes = [normalize_for_match(c["quote_text"]) for c in data.candidates.values()]
    worries = [normalize_for_match(w["text_general"]) for w in data.worries.values()]
    names: set[str] = set()
    for c in data.candidates.values():
        names.update(n for n in c.get("author_names") or [] if len(n) >= 2)
        if len(c["title"]) >= 2:
            names.add(f"『{c['title']}』")
    stat_values = [
        str(s["value"]) for s in data.stats.values() if re.search(r"\d", str(s["value"]))
    ]
    for seg in chapter["segments"]:
        sid, text = seg["seg_id"], seg["text"]
        for p in resolver.problems(text):
            issues.append(f"{sid}：解決できない記号 {p}")
        if seg["kind"] == "transition":
            bare_title = PLACEHOLDER.sub("", text)
            issues += [
                f"{sid}：章タイトルに著者名・作品名「{n}」を直接書いている"
                for n in names
                if n in bare_title
            ]
        if seg["kind"] != "narration":
            continue
        for m in QUOTED.finditer(text):
            inner = m.group(1).strip()
            pm = PLACEHOLDER.fullmatch(inner)
            if pm and pm.group(1) in ("quote", "worry"):
                continue
            norm = normalize_for_match(inner)
            if len(norm) <= SHORT_QUOTE_MAX:
                continue
            if any(norm in q for q in quotes) or any(norm in w for w in worries):
                continue  # 一節や悩み文の一部をそのまま引いている
            issues.append(f"{sid}：「」の中が一節でも悩み文でもない（{inner[:30]}）")
        bare = PLACEHOLDER.sub("", text)
        for name in names:
            if name in bare:
                issues.append(f"{sid}：著者名・作品名「{name}」を直接書いている（記号を使う）")
        for v in stat_values:
            if re.search(rf"(?<![\d.]){re.escape(v)}(?![\d.])", bare):
                issues.append(f"{sid}：統計の数字「{v}」を直接書いている（{{{{stat:…}}}} を使う）")
    return issues


# --- G2・G4・G5・G6 ---------------------------------------------------------


def g2_license(data: EpisodeData) -> dict[str, Any]:
    issues = []
    for s in data.plan["selection"]:
        c = data.candidates.get(s["candidate_id"])
        if c is None:
            issues.append(f"{s['candidate_id']}：候補表に無い")
            continue
        if not is_usable(c["license"]):
            issues.append(f"{s['candidate_id']}：ライセンス {c['license']} は使えない（R1）")
        if (c.get("lang") or "ja") != "ja":
            issues.append(f"{s['candidate_id']}：外国語の一節には自前訳が要る（R5。M8 で対応）")
    return _gate(issues)


def g4_recency(
    conn: sqlite3.Connection, data: EpisodeData, work_episodes: int, author_episodes: int
) -> dict[str, Any]:
    created = conn.execute(
        "SELECT created_at FROM episodes WHERE episode_id = ?", (data.episode_id,)
    ).fetchone()[0]

    def recent(column: str, n: int) -> set[str]:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT episode_id FROM episodes WHERE state != 'discarded' AND episode_id != ? "
                "AND created_at <= ? ORDER BY created_at DESC, episode_id DESC LIMIT ?",
                (data.episode_id, created, n),
            )
        ]
        if not ids:
            return set()
        marks = ",".join("?" for _ in ids)
        return {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT {column} FROM usage WHERE role = 'main' AND {column} IS NOT NULL "
                f"AND episode_id IN ({marks})",
                ids,
            )
        }

    works, authors = recent("work_id", work_episodes), recent("author_id", author_episodes)
    issues = []
    for s in data.plan["selection"]:
        if s["work_id"] in works:
            issues.append(f"{s['title']}：直近{work_episodes}回で主役にした作品")
        for a in s.get("author_ids") or []:
            if a in authors:
                issues.append(f"{s['title']}：直近{author_episodes}回で主役にした著者")
    return _gate(issues)


def g5_safety(data: EpisodeData, categories: dict[str, list[str]]) -> dict[str, Any]:
    from koten.worries.master import keyword_block

    issues = []
    for wid, w in data.worries.items():
        if w["status"] != "active":
            issues.append(f"{wid}：悩みが {w['status']}（{w.get('blocked_reason') or ''}）")
        hit = keyword_block(w["text_general"] or w["text_raw"], categories)
        if hit:
            issues.append(f"{wid}：除外カテゴリ「{hit[0]}」のキーワード（{hit[1]}）")
    for cid, c in data.candidates.items():
        if c.get("caution"):
            issues.append(f"{cid}：注意フラグ（{c.get('caution_reason') or ''}）")
    return _gate(issues)


def g6_review(data: EpisodeData, script: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    for cid, c in data.candidates.items():
        if c.get("review_required"):
            reasons.append(f"{cid}：要確認のソース（NDL など）")
    for ch in script["chapters"]:
        for seg in ch["segments"]:
            if seg["text"].startswith("{{quote_ja:"):
                reasons.append(f"{seg['seg_id']}：自前訳（試訳）を含む")
    # G6 は不合格ではなく「人の確認待ち」にする
    return {"ok": True, "issues": [], "review_required": bool(reasons), "reasons": reasons}


# --- G3 ---------------------------------------------------------------------


class FaithfulnessJudge:
    def __init__(self, llm: ChatLLM, prompts_dir, prompt: str = "judge_v1"):
        self.llm = llm
        self.system, self.user_tpl = load_prompt(prompts_dir, prompt)

    def needs_judge(self, chapter: dict[str, Any]) -> bool:
        return bool(chapter.get("sources")) or chapter["role"] in JUDGED_ROLES

    def judge(self, chapter: dict[str, Any], data: EpisodeData) -> list[str]:
        """問題の一覧（空なら合格）。判定できなかったときも問題として返す。"""
        ids = chapter.get("sources") or [s["candidate_id"] for s in data.plan["selection"]]
        passages = "\n".join(
            f"- 一節：{data.candidates[c]['quote_text']}\n"
            f"  前後の文脈：{data.candidates[c]['context_text']}"
            for c in ids
            if c in data.candidates
        )
        resolver = resolver_for(data)
        lines = []
        for seg in chapter["segments"]:
            if seg["kind"] == "stat":
                continue
            try:
                text = resolver.resolve(seg["text"])
            except KeyError:
                text = seg["text"]
            lines.append(f"（一節）「{text}」" if seg["kind"] == "quote" else text)
        user = fill(
            self.user_tpl,
            passages=passages,
            chapter_title=chapter["title"],
            narration="\n".join(lines),
        )
        for _ in range(3):
            try:
                d = parse_json_object(self.llm.complete(self.system, user, JUDGE_SCHEMA))
            except ValueError:
                continue
            if isinstance(d.get("faithful"), bool):
                problems = [str(p) for p in d.get("problems") or [] if str(p).strip()]
                if d["faithful"]:
                    return []
                return problems or ["忠実さに問題がある（理由なし）"]
        return ["忠実さを判定できなかった"]


def summarize(verification: dict[str, Any]) -> str:
    parts = []
    for g in ("G1", "G2", "G3", "G4", "G5", "G6"):
        v = verification.get(g)
        if v is None:
            continue
        mark = "合格" if v["ok"] else "不合格"
        if g == "G6" and v.get("review_required"):
            mark = "要確認"
        if g == "G3" and v.get("needs_human"):
            mark = "人の確認へ"
        parts.append(f"{g} {mark}")
    return "、".join(parts)


def dumps(verification: dict[str, Any]) -> str:
    return json.dumps(verification, ensure_ascii=False)
