"""訳の下書き → コードの確かめ → 判定役 → 書き直し（決まった回数まで）。合否はコードが決める。

判定役は1文ずつ、その文を支える版の ID（supported_by）と判定を返す。
- supported：支える版がある / divided：版によって読みが分かれる（どれか1つは支える）/
  unsupported：どの版にも支えが無い
- 外国語の原文・古文からの訳（shiyaku・modern）は、どの文も時代の解釈（S2）の版の ID が要る
- 外国語の訳からの訳（relay）は、どの文も元の訳（ロングの英訳など）の ID が要る（元の訳の出典を
  出すので、ほかの訳者の読みに寄せない）
- 落とした所（omissions）があれば不合格
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from koten.llm import ChatLLM, LLMError, fill, load_prompt, parse_json_object
from koten.script.gates import split_sentences
from koten.shiyaku.machine import machine_issues
from koten.shiyaku.material import Material, describe
from koten.shiyaku.store import KINDS, Shiyaku

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
VERDICTS = ("supported", "divided", "unsupported")
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "supported_by": {"type": "array", "items": {"type": "string"}},
                    "problem": {"type": "string"},
                },
                "required": ["n", "verdict", "supported_by", "problem"],
                "additionalProperties": False,
            },
        },
        "omissions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sentences", "omissions"],
    "additionalProperties": False,
}
KIND_RULES = {
    "shiyaku": "外国語の原文を、今の日本語に訳す。時代の解釈（訓読・古い訳・注釈）で読みを確かめ、"
    "どの版も支えない読みをしない。版によって読みが分かれる所は、どれか1つの読みに沿って訳す。",
    "relay": "保護期間の切れた外国語の訳（元の訳）を、今の日本語に訳す。元の訳に書いてあることだけ"
    "を訳し、ほかの訳者の読みや、自分の知っている原文の読みに寄せない"
    "（画面には元の訳の出典が出る）。",
    "modern": "日本の古文を、今の日本語にする。時代の解釈（注釈・古い訳）で語の意味を確かめ、"
    "どの版も支えない読みをしない。",
}


class ShiyakuLLMError(RuntimeError):
    pass


@dataclass
class FlowConfig:
    draft_prompt: str = "shiyaku_draft_v1"
    judge_prompt: str = "shiyaku_judge_v1"
    retries: int = 2
    min_versions: int = 2


def model_of(llm: ChatLLM) -> str:
    """モデルの名前（effort などを除く）。下書きと判定役が同じモデルかを見るのに使う。"""
    return llm.name.split("@")[0]


def _basis_block(m: Material) -> str:
    if not m.basis:
        return "（なし）"
    return "\n".join(f"- {describe(r)}\n  {r.get('display_text') or r['text']}" for r in m.basis)


def judge_verdict(
    m: Material, sentences: list[str], answer: dict[str, Any]
) -> tuple[list[str], list[int]]:
    """判定役の答えから、問題の一覧と、読みが分かれる文の番号を決める（コードが決める）。"""
    shown = {m.source["rendering_id"]} | {r["rendering_id"] for r in m.basis}
    basis_ids = {r["rendering_id"] for r in m.basis}
    got = {x.get("n"): x for x in answer.get("sentences") or [] if isinstance(x, dict)}
    problems: list[str] = []
    divided: list[int] = []
    for n, sent in enumerate(sentences, start=1):
        x = got.get(n)
        head = f"S{n}「{sent[:24]}」"
        if x is None:
            problems.append(f"{head}：判定役の答えが無い")
            continue
        sup = {str(i) for i in x.get("supported_by") or []}
        if x.get("verdict") == "unsupported":
            problems.append(f"{head}：どの版にも支えが無い（{x.get('problem') or '根拠なし'}）")
            continue
        if unknown := sup - shown:
            problems.append(f"{head}：見せていない版 {'・'.join(sorted(unknown))} を根拠にしている")
            continue
        if m.kind == "relay" and m.source["rendering_id"] not in sup:
            problems.append(f"{head}：元の訳 {m.source['rendering_id']} に支えが無い")
            continue
        if m.kind != "relay" and not (sup & basis_ids):
            problems.append(f"{head}：時代の解釈（訓読・古い訳・注釈）の支えが無い")
            continue
        if x.get("verdict") == "divided":
            divided.append(n)
    for o in answer.get("omissions") or []:
        if str(o).strip():
            problems.append(f"訳から落ちている所：{str(o)[:40]}")
    return problems, divided


def run_flow(
    m: Material,
    drafter: ChatLLM,
    judge: ChatLLM,
    prompts_dir: Path,
    cfg: FlowConfig,
    *,
    shiyaku_id: str,
    s4_titles: list[str] = (),
    log: Callable[[str], None] = lambda _: None,
) -> Shiyaku:
    if model_of(drafter) == model_of(judge):
        raise ShiyakuLLMError(
            f"下書きと判定役が同じモデル（{model_of(drafter)}）です。判定役は別のモデルにする"
            "（D-023。settings.toml の [llm.shiyaku_judge]）"
        )
    d_system, d_user = load_prompt(prompts_dir, cfg.draft_prompt)
    j_system, j_user = load_prompt(prompts_dir, cfg.judge_prompt)
    src = m.source
    common = {
        "title": m.title,
        "passage_key": m.passage_key,
        "kind_rule": KIND_RULES[m.kind],
        "source_head": describe(src),
        "source_text": src.get("display_text") or src["text"],
        "basis": _basis_block(m),
    }
    feedback: list[str] = []
    text = ""
    issues: list[str] = []
    answer: dict[str, Any] | None = None
    divided: list[int] = []
    status = "draft"
    for attempt in range(cfg.retries + 1):
        user = fill(
            d_user,
            **common,
            feedback="\n".join(f"- {x}" for x in feedback) or "（なし。初めての下書き）",
            previous=text or "（なし）",
        )
        try:
            raw = parse_json_object(drafter.complete(d_system, user, DRAFT_SCHEMA))
        except LLMError as e:
            raise ShiyakuLLMError(f"下書きの AI が答えませんでした（{e}）") from e
        text = str((raw or {}).get("text") or "").strip()
        issues = machine_issues(text, src, m.basis, s4_titles)
        answer, divided = None, []
        if issues:
            status = "draft"
        else:
            sentences = split_sentences(text)
            numbered = "\n".join(f"S{n}：{t}" for n, t in enumerate(sentences, start=1))
            try:
                answer = parse_json_object(
                    judge.complete(
                        j_system, fill(j_user, **common, sentences=numbered), JUDGE_SCHEMA
                    )
                )
            except LLMError as e:
                raise ShiyakuLLMError(f"判定役の AI が答えませんでした（{e}）") from e
            issues, divided = judge_verdict(m, sentences, answer or {})
            status = "flagged" if issues else "judged"
        if not issues:
            break
        feedback = issues
        log(f"  書き直し（{attempt + 1}回目）：{'、'.join(issues)[:120]}")
    if divided:
        issues = issues + [
            f"読みが分かれる文：{'・'.join(f'S{n}' for n in divided)}（承認のときに確かめる）"
        ]
    return Shiyaku(
        shiyaku_id=shiyaku_id,
        passage_key=m.passage_key,
        kind=m.kind,
        text=text,
        source_id=src["rendering_id"],
        source_sha=src["text_sha"],
        basis_ids=[r["rendering_id"] for r in m.basis],
        label=m.label,
        status=status,
        drafter=drafter.name,
        issues=issues,
        judge_json=(answer or {}).get("sentences") if answer else None,
        judge=judge.name if answer is not None else "",
    )


def explain_kind(kind: str) -> str:
    return KINDS.get(kind, kind)
