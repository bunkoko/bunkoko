"""台本の生成（SPEC §8.7・§10）。章ごとに LLM に書かせ、台本JSONに組み立てる。

LLM に書かせるのは、章のタイトルと段落の並び（語り・一節・統計）だけ。
段落の ID、画面の種類（visual）、一節の読み方（read_as）、出典の一覧、人の一言の枠、
紹介する本の一覧は、企画（plan_json）と DB の値から機械的に決める。
背景の事実（作者の歩み・作品の成り立ち・時代）は、企画で章に付けた確認済みの事実だけを渡し、
名前・年・数はその事実の項目の記号（{{fact:ID.項目}}）で書かせる（SPEC R6・D-012）。
昔の辞書の語釈・時代の解釈（訓読・古い訳・注釈）も、企画で章に付けた確かめ済みの本文だけを渡し、
本文と出典は記号（{{def:ID}}・{{def_src:ID}}・{{interp:ID}}・{{interp_src:ID}}）で差し込ませる（D-015）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from koten.authors import author_label
from koten.llm import ChatLLM, fill, load_prompt, parse_json_object
from koten.ref.store import ref_source_label, ref_text
from koten.script.data import EpisodeData

SCRIPT_SCHEMA_VERSION = "1.2"  # 1.2：段落と章の refs、画面 ref_card。1.1：facts・fact_card

ROLE_NAMES = {
    "opening": "オープニング",
    "context": "悩みの解像度を上げる",
    "classic": "古典の一節",
    "work_intro": "作品の紹介",
    "worry_block": "悩みと一節",
    "view_a": "1つ目の考え",
    "view_b": "2つ目の考え",
    "compare": "2つの考えを比べる",
    "summary": "まとめ",
    "ending": "エンディング",
}

ROLE_GUIDE = {
    "opening": "最初の段落で今日の悩みを {{worry:…}} で言い、冒頭の30秒（約150字）のうちに、"
    "この回で分かることを言う。挨拶・チャンネルの紹介・登録のお願いは入れない。一節はまだ出さない。",
    "context": "その悩みがどんな場面で起きるかを、よくある場面で具体的に描く。"
    "統計があれば stat の段落で示す（語りの中では数字を書かない）。"
    "昔の辞書の語釈を渡したときは、「そもそもこの言葉は、昔どう説明されていたか」として"
    "1つだけ短く紹介し、今の悩みに戻る。",
    "classic": "割り当てた一節ごとに、①出典を {{source:候補ID}} で紹介 ②一節（kind=quote）"
    "③一節の意味 ④今の悩みへの当てはめ、の順で書く。背景の事実を渡したときは、①の前後か"
    "③のあとに、その言葉が書かれたころの作者や作品の背景を短く（20〜30秒）語る。",
    "work_intro": "これから読む作品を {{title:候補ID}}・{{author:候補ID}} で紹介し、"
    "この回の悩みとのつながりを言う。背景の事実を渡したときは、書き手の歩み・作品の成り立ち・"
    "時代を、渡した事実の範囲で語る（渡していない経歴や成り立ちは書かない）。",
    "worry_block": "1つの悩み（{{worry:悩みID}}）を取り上げ、割り当てた一節で考える。"
    "一節ごとに、出典の紹介・一節・意味・当てはめの順で書く。背景の事実を渡したときは、"
    "一節の前後で1つだけ短く添える。",
    "view_a": "1人目の著者の考え（A）を紹介する。背景の事実を渡したときは、まず「人物と時代」を"
    "渡した事実の範囲で語り、そのあと出典の紹介・一節・意味の順で書く。",
    "view_b": "2人目の著者の、Aとは逆の考え（B）を紹介する。背景の事実を渡したときは、まず"
    "「人物と時代」を渡した事実の範囲で語り、そのあと出典の紹介・一節・意味の順で書く。",
    "compare": "AとBを比べ、どちらが正しいとは決めずに、視聴者に問いかける。",
    "summary": "この回で見た一節をふり返り、今日からできる小さな一歩を1つ示す。"
    "このあと制作者の一言が入るので、締めくくりすぎない。",
    "ending": "コメントで悩みや考えを募集し、次の回も古典の一節で悩みを考えることを予告する。"
    "チャンネル登録のお願いは入れるなら1文だけ。長いあいさつで締めない。",
}

CHAPTER_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["narration", "quote", "stat"]},
                    "text": {"type": "string"},
                },
                "required": ["kind", "text"],
                "additionalProperties": False,
            },
        },
        "episode_title": {"type": "string"},
        "comment_prompt": {"type": "string"},
    },
    "required": ["title", "segments", "episode_title", "comment_prompt"],
    "additionalProperties": False,
}

QUOTE_ONLY = re.compile(r"^\{\{quote:([^{}]+)\}\}$")
FACT_REF = re.compile(r"\{\{fact:\s*([^{}.\s]+)\s*(?:\.[^{}]+)?\}\}")
REF_REF = re.compile(r"\{\{(?:def|def_src|interp|interp_src):\s*([^{}\s]+)\s*\}\}")
STAT_ONLY = re.compile(r"^\{\{stat:([^{}]+)\}\}$")


@dataclass
class ScriptConfig:
    prompt: str = "script_v4"
    judge_prompt: str = "judge_v1"
    judge_facts_prompt: str = "judge_v3"  # 背景の事実・語釈・解釈を使った章の判定（1文ずつ）
    chars_per_min: int = 300
    min_sec: int = 600
    max_sec: int = 900
    retries: int = 2  # G1・G3・長さで章を書き直す回数
    length_tolerance: float = 0.3  # 章の文字数が目安から ±30% を超えたら書き直させる

    @classmethod
    def from_settings(cls, script: dict[str, Any], episode: dict[str, Any]) -> ScriptConfig:
        kw = {k: v for k, v in script.items() if k in cls.__dataclass_fields__}
        for k in ("chars_per_min", "min_sec", "max_sec"):
            if k in episode:
                kw[k] = episode[k]
        return cls(**kw)


@dataclass
class ChapterDraft:
    chapter: dict[str, Any]
    episode_title: str = ""
    comment_prompt: str = ""
    issues: list[str] = field(default_factory=list)  # 形の問題（G1 に入れる）


def read_as(candidate: dict[str, Any]) -> str:
    """新字新仮名の日本語は原文を読み上げる。旧字・旧仮名・外国語は画面に原文、読み上げは現代語。"""
    if (candidate.get("lang") or "ja") == "ja" and candidate.get("kana_type") == "新字新仮名":
        return "original"
    return "modern"


class ScriptWriter:
    def __init__(self, llm: ChatLLM, prompts_dir: Path, cfg: ScriptConfig):
        self.llm = llm
        self.cfg = cfg
        self.system, self.user_tpl = load_prompt(prompts_dir, cfg.prompt)

    def _outline(self, plan: dict[str, Any]) -> str:
        return "\n".join(
            f"- {c['chapter_id']} {ROLE_NAMES.get(c['role'], c['role'])}（約{c['target_sec']}秒）"
            for c in plan["chapters"]
        )

    def _passages(self, data: EpisodeData, ids: list[str]) -> str:
        if not ids:
            return "（なし）"
        reasons = {s["candidate_id"]: s for s in data.plan["selection"]}
        blocks = []
        for cid in ids:
            c = data.candidates[cid]
            sel = reasons.get(cid, {})
            side = f"（{sel['side']}の側）" if sel.get("side") else ""
            blocks.append(
                f"- 候補ID：{cid}{side}\n"
                f"  出典の記号：{{{{source:{cid}}}}}\n"
                f"  当てはめる悩み：{sel.get('worry_id', '')}\n"
                f"  選んだ理由：{sel.get('reason', '')}\n"
                f"  一節：{c['quote_text']}\n"
                f"  前後の文脈：{c['context_text']}"
            )
        return "\n".join(blocks)

    def _works(self, data: EpisodeData) -> str:
        """オープニング用：この回で読む作品と、タイトル案で名前を入れるときの記号。"""
        lines, seen = [], set()
        for s in data.plan["selection"]:
            c = data.candidates.get(s["candidate_id"])
            if c is None or c["work_id"] in seen:
                continue
            seen.add(c["work_id"])
            cid = s["candidate_id"]
            authors = author_label(c)
            lines.append(
                f"- {{{{author:{cid}}}}}＝{authors}、{{{{title:{cid}}}}}＝『{c['title']}』"
            )
        return "この回で読む作品（名前を出すときは記号で書く）：\n" + "\n".join(lines)

    def _facts(self, data: EpisodeData, ids: list[str]) -> str:
        """章に渡す背景の事実。差し込んだあとの値を見せ、記号の書き方を示す。"""
        blocks = []
        for fid in ids:
            f = data.facts.get(fid)
            if f is None:
                continue
            items = []
            for name in f.slots:
                items.append(f"{{{{fact:{fid}.{name}}}}}＝{f.slot_text(name)}")
                if name.startswith("year"):
                    try:
                        items.append(
                            f"{{{{fact:{fid}.{name}_wareki}}}}＝{f.slot_text(name + '_wareki')}"
                        )
                    except (KeyError, ValueError):
                        pass
            scope = {"original_work": "原著について", "translation": "この訳について"}.get(
                f.scope, ""
            )
            blocks.append(
                f"- 事実ID：{fid}{('（' + scope + '）') if scope else ''}\n"
                f"  確認済みの文：{f.text_ja}（文のまま言うときは {{{{fact:{fid}}}}}）\n"
                f"  項目の記号：{'、'.join(items)}"
            )
        return "\n".join(blocks) or "（なし）"

    def _refs(self, data: EpisodeData, ids: list[str]) -> str:
        """章に渡す昔の辞書の語釈・時代の解釈。差し込んだあとの値と、記号の書き方を示す。"""
        roles = {"original": "原文", "kundoku": "訓読", "translation": "訳", "commentary": "注釈"}
        blocks = []
        for rid in ids:
            r = data.refs.get(rid)
            if r is None:
                continue
            if rid.startswith("J-"):
                sense = f"（第{r['sense']}義）" if r.get("sense") else ""
                head = f"辞書の語釈：見出し「{r['headword']}」{sense}"
                body, src = f"{{{{def:{rid}}}}}", f"{{{{def_src:{rid}}}}}"
            else:
                head = f"時代の解釈（{roles.get(r['role'], r['role'])}）：一節 {r['passage_key']}"
                body, src = f"{{{{interp:{rid}}}}}", f"{{{{interp_src:{rid}}}}}"
            blocks.append(
                f"- ID：{rid}　{head}\n"
                f"  本文の記号：{body}＝{ref_text(r)}\n"
                f"  出典の記号：{src}＝{ref_source_label(r)}"
            )
        return "\n".join(blocks) or "（なし）"

    def _stats(self, data: EpisodeData, ids: list[str]) -> str:
        if not ids:
            return "（なし）"
        return "\n".join(
            f"- 統計ID：{sid}（{data.stats[sid]['label']}。数字は書かず {{{{stat:{sid}}}}} を使う）"
            for sid in ids
            if sid in data.stats
        )

    def write_chapter(
        self,
        data: EpisodeData,
        chapter: dict[str, Any],
        feedback: list[str] | None = None,
        *,
        use_facts: bool = True,
        use_refs: bool = True,
    ) -> ChapterDraft:
        plan = data.plan
        guide = ROLE_GUIDE.get(chapter["role"], "")
        if chapter["role"] == "compare" and plan.get("question"):
            guide += f"問いかけの例：{plan['question']}"
        if chapter["role"] == "opening":
            guide += "\n" + self._works(data)
        worries = "\n".join(f"- {w['worry_id']}：{w['text']}" for w in plan["worries"])
        fb = ""
        if feedback:
            fb = "\n前回の台本の問題（必ず直す）：\n" + "\n".join(f"- {x}" for x in feedback)
        user = fill(
            self.user_tpl,
            format_name=f"{plan['format']} {plan['format_name']}",
            worries=worries,
            outline=self._outline(plan),
            chapter_id=chapter["chapter_id"],
            role=ROLE_NAMES.get(chapter["role"], chapter["role"]),
            target_chars=int(chapter["target_sec"] * self.cfg.chars_per_min / 60),
            target_sec=chapter["target_sec"],
            guide=guide,
            passages=self._passages(data, chapter["candidate_ids"]),
            stats=self._stats(data, chapter.get("stat_ids", [])),
            facts=self._facts(data, chapter.get("fact_ids", []) if use_facts else []),
            refs=self._refs(data, chapter.get("ref_ids", []) if use_refs else []),
            feedback=fb,
        )
        raw: dict[str, Any] | None = None
        for _ in range(3):
            try:
                raw = parse_json_object(self.llm.complete(self.system, user, CHAPTER_SCHEMA))
                if isinstance(raw.get("segments"), list):
                    break
            except ValueError:
                raw = None
        if raw is None or not isinstance(raw.get("segments"), list):
            raw = {"title": ROLE_NAMES.get(chapter["role"], ""), "segments": []}
            return ChapterDraft(self.build(data, chapter, raw), issues=["LLM の出力を読めなかった"])
        draft = ChapterDraft(
            self.build(data, chapter, raw),
            episode_title=str(raw.get("episode_title") or "").strip(),
            comment_prompt=str(raw.get("comment_prompt") or "").strip(),
        )
        draft.issues = self.shape_issues(data, chapter, raw)
        return draft

    def build(
        self, data: EpisodeData, chapter: dict[str, Any], raw: dict[str, Any]
    ) -> dict[str, Any]:
        """LLM の段落の並びに、ID・画面の種類・読み方を付けて章にする。"""
        cid = chapter["chapter_id"]
        title = _cut(str(raw.get("title") or ROLE_NAMES.get(chapter["role"], "")).strip(), 40)
        title = title or "章"
        segments: list[dict[str, Any]] = []
        if cid != "ch0":
            segments.append(
                {"kind": "transition", "text": title, "visual": {"type": "title_card", "ref": None}}
            )
        for s in raw.get("segments") or []:
            if not isinstance(s, dict):
                continue
            kind, text = s.get("kind"), str(s.get("text") or "").strip()
            if not text or kind not in ("narration", "quote", "stat"):
                continue
            seg: dict[str, Any] = {"kind": kind, "text": text}
            if kind == "quote" and (m := QUOTE_ONLY.match(text)):
                ref = m.group(1).strip()
                seg["visual"] = {"type": "quote_card", "ref": ref}
                seg["read_as"] = read_as(data.candidates.get(ref, {}))
            elif kind == "stat" and (m := STAT_ONLY.match(text)):
                seg["visual"] = {"type": "chart", "ref": m.group(1).strip()}
            else:
                # 形の崩れた quote・stat は語りとして残し、G1 で不合格にする
                seg["kind"] = "narration" if kind != "narration" else kind
                seg["visual"] = {"type": "photo", "ref": None}
                facts = list(dict.fromkeys(FACT_REF.findall(text)))
                refs = list(dict.fromkeys(REF_REF.findall(text)))
                if refs:
                    seg["refs"] = refs  # 語釈・解釈の段落（画面は資料のまま・出典・ページ。M6）
                    seg["visual"] = {"type": "ref_card", "ref": refs[0]}
                if facts:
                    seg["facts"] = facts  # 背景の段落（画面は事実のカード。M6）
                    seg["visual"] = {"type": "fact_card", "ref": facts[0]}
            segments.append(seg)
        for i, seg in enumerate(segments, start=1):
            seg["seg_id"] = f"{cid}-s{i}"
        out = {
            "chapter_id": cid,
            "role": chapter["role"],
            "title": title,
            "target_sec": int(chapter["target_sec"]),
            "segments": [
                {
                    k: seg[k]
                    for k in ("seg_id", "kind", "text", "read_as", "visual", "facts", "refs")
                    if k in seg
                }
                for seg in segments
            ],
        }
        if chapter["candidate_ids"]:
            out["sources"] = list(chapter["candidate_ids"])
        used = list(dict.fromkeys(f for seg in segments for f in seg.get("facts", [])))
        if used:
            out["facts"] = used
        used_refs = list(dict.fromkeys(r for seg in segments for r in seg.get("refs", [])))
        if used_refs:
            out["refs"] = used_refs
        return out

    @staticmethod
    def shape_issues(data: EpisodeData, chapter: dict[str, Any], raw: dict[str, Any]) -> list[str]:
        issues = []
        want = list(chapter["candidate_ids"])
        got: list[str] = []
        for s in raw.get("segments") or []:
            if not isinstance(s, dict):
                continue
            text = str(s.get("text") or "").strip()
            if s.get("kind") == "quote":
                m = QUOTE_ONLY.match(text)
                if not m:
                    issues.append(f"一節の段落に記号以外が書かれている：{text[:30]}")
                elif m.group(1).strip() not in want:
                    issues.append(f"この章に割り当てていない一節：{m.group(1)}")
                else:
                    got.append(m.group(1).strip())
            elif s.get("kind") == "stat":
                m = STAT_ONLY.match(text)
                if not m:
                    issues.append(f"統計の段落に記号以外が書かれている：{text[:30]}")
                elif m.group(1).strip() not in chapter.get("stat_ids", []):
                    issues.append(f"この章に割り当てていない統計：{m.group(1)}")
        for cid in want:
            if got.count(cid) != 1:
                issues.append(f"割り当てた一節 {cid} を、一節の段落でちょうど1回出していない")
        if not any(
            isinstance(s, dict) and s.get("kind") == "narration" for s in raw.get("segments") or []
        ):
            issues.append("語りの段落が無い")
        return issues

    def assemble(
        self,
        data: EpisodeData,
        drafts: list[ChapterDraft],
    ) -> dict[str, Any]:
        plan = data.plan
        human = next((c for c in plan["chapters"] if c.get("human_take")), plan["chapters"][-1])
        # 動画のタイトル案はオープニングの章のものだけ（判定役が一緒に確かめている）
        title = next((d.episode_title for d in drafts if d.chapter["role"] == "opening"), "")
        if not title:
            title = f"{plan['worries'][0]['text']}――古典の一節で考える"
        comment = plan.get("question") or next(
            (d.comment_prompt for d in reversed(drafts) if d.comment_prompt), ""
        )
        books, seen = [], set()
        for s in plan["selection"]:
            if s["work_id"] in seen:
                continue
            seen.add(s["work_id"])
            c = data.candidates.get(s["candidate_id"], {})
            authors = author_label(c) or "・".join(s.get("authors") or [])
            books.append(
                {
                    "work_id": s["work_id"],
                    "query": f"{authors} {c.get('title', s.get('title', ''))}",
                }
            )
        return {
            "schema_version": SCRIPT_SCHEMA_VERSION,
            "episode_id": data.episode_id,
            "format": plan["format"],
            "title": title,
            "theme": plan["theme"],
            "worries": [{"worry_id": w["worry_id"], "text": w["text"]} for w in plan["worries"]],
            "chapters": [d.chapter for d in drafts],
            "human_take": {"chapter_id": human["chapter_id"], "text": None, "audio_path": None},
            "comment_prompt": comment,
            "featured_books": books,
            "ad_break_candidates_sec": None,
            "review_required": bool(plan.get("review_required")),
            "verification": None,
            "timings": None,
        }


def _cut(text: str, n: int) -> str:
    """n 字で切る。記号（{{…}}）の途中では切らない。"""
    if len(text) <= n:
        return text
    cut = text[:n]
    start = cut.rfind("{{")
    if start >= 0 and "}}" not in cut[start:]:
        cut = cut[:start]
    return cut.rstrip()


def load_schema() -> dict[str, Any]:
    return json.loads((Path(__file__).parent / "schema.json").read_text("utf-8"))
