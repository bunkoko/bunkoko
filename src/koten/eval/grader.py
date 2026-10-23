"""検索の評価の grader（2値）と、人の点との一致の確かめ。

- `grade_file`：評価セットの CSV の llm_grade 列を、LLM（既定は Claude Code の Haiku）で埋める。
  悩み1つぶんの一節をまとめて1回で判定させ、呼び出しの回数を抑える
- `agreement`：人の点（grade）と LLM の点（llm_grade）が両方ある行で、一致率・コーエンのκ・
  食い違いの一覧を出す。κ がおよそ 0.6 以上なら、LLM の点を広い評価に使ってよい目安
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from koten.eval.retrieval import parse_grade, read_rows, write_eval
from koten.llm import ChatLLM, fill, load_prompt, parse_json_object

SCHEMA = {
    "type": "object",
    "properties": {
        "grades": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "relevant": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["candidate_id", "relevant", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["grades"],
    "additionalProperties": False,
}


@dataclass
class GradeResult:
    worries: int = 0
    graded: int = 0
    skipped: int = 0  # すでに点があった行
    missing: list[str] = field(default_factory=list)  # 判定が返ってこなかった候補


def _items_text(rows: list[dict[str, str]]) -> str:
    return "\n".join(
        f"- candidate_id: {r['candidate_id']}\n"
        f"  作品：{r['title']}（{r['authors']}）{r['section']}\n"
        f"  quote：{r['quote_text']}\n"
        f"  summary：{r['summary_ja']}"
        for r in rows
    )


def grade_file(
    path: Path,
    llm: ChatLLM,
    prompts_dir: Path,
    prompt: str = "grader_v1",
    *,
    redo: bool = False,
    worry_ids: set[str] | None = None,
    retries: int = 2,
    log: Callable[[str], None] = lambda _: None,
) -> GradeResult:
    """llm_grade が空の行を、悩みごとにまとめて LLM に判定させる。悩み1つ終わるごとに保存する。"""
    rows = read_rows(path)
    system, user_tpl = load_prompt(prompts_dir, prompt)
    result = GradeResult()
    order: list[str] = []
    for r in rows:
        if r["worry_id"] not in order:
            order.append(r["worry_id"])
    for wid in order:
        if worry_ids and wid not in worry_ids:
            continue
        group = [r for r in rows if r["worry_id"] == wid]
        todo = [r for r in group if redo or parse_grade(r["llm_grade"]) is None]
        result.skipped += len(group) - len(todo)
        if not todo:
            continue
        user = fill(user_tpl, worry=todo[0]["worry_text"], items=_items_text(todo))
        wanted = {r["candidate_id"] for r in todo}
        got: dict[str, dict] = {}
        for _ in range(retries + 1):
            try:
                data = parse_json_object(llm.complete(system, user, SCHEMA))
            except ValueError:
                continue
            for g in data.get("grades") or []:
                if isinstance(g, dict) and g.get("candidate_id") in wanted:
                    if isinstance(g.get("relevant"), bool):
                        got[g["candidate_id"]] = g
            if wanted <= set(got):
                break
        for r in todo:
            g = got.get(r["candidate_id"])
            if g is None:
                result.missing.append(f"{wid} {r['candidate_id']}")
                continue
            r["llm_grade"] = "1" if g["relevant"] else "0"
            r["llm_reason"] = str(g.get("reason") or "").strip()[:200]
            r["llm_grader"] = f"{llm.name}/{prompt}"
            result.graded += 1
        write_eval(path, rows)  # 悩み1つごとに保存（途中で止めても判定済みは残る）
        result.worries += 1
        n_rel = sum(1 for r in todo if r["llm_grade"] == "1")
        log(f"  {wid}：{len(todo)} 件を判定（合う {n_rel} 件）")
    return result


@dataclass
class Agreement:
    n: int
    accuracy: float
    kappa: float | None
    both_yes: int
    both_no: int
    human_only: int  # 人は合う、LLM は合わない
    llm_only: int  # 人は合わない、LLM は合う
    disagreements: list[dict[str, str]]

    @property
    def llm_precision(self) -> float | None:
        """LLM が「合う」とした中で、人も「合う」とした割合。"""
        d = self.both_yes + self.llm_only
        return self.both_yes / d if d else None

    @property
    def llm_recall(self) -> float | None:
        """人が「合う」とした中で、LLM も「合う」とした割合。"""
        d = self.both_yes + self.human_only
        return self.both_yes / d if d else None


def cohen_kappa(pairs: list[tuple[int, int]]) -> float | None:
    """2値のコーエンのκ：偶然でも一致する分を差し引いた一致の度合い。

    1 が完全一致、0 が偶然と同じくらい。
    """
    n = len(pairs)
    if n == 0:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    pa = sum(a for a, _ in pairs) / n
    pb = sum(b for _, b in pairs) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    if pe == 1:
        return 1.0 if po == 1 else None
    return (po - pe) / (1 - pe)


def agreement(path: Path) -> Agreement:
    pairs: list[tuple[int, int]] = []
    disagreements = []
    counts = {"yy": 0, "nn": 0, "yn": 0, "ny": 0}
    for r in read_rows(path):
        h, m = parse_grade(r["grade"]), parse_grade(r["llm_grade"])
        if h is None or m is None:
            continue
        pairs.append((h, m))
        counts[("y" if h else "n") + ("y" if m else "n")] += 1
        if h != m:
            disagreements.append(r)
    n = len(pairs)
    return Agreement(
        n=n,
        accuracy=sum(1 for a, b in pairs if a == b) / n if n else 0.0,
        kappa=cohen_kappa(pairs),
        both_yes=counts["yy"],
        both_no=counts["nn"],
        human_only=counts["yn"],
        llm_only=counts["ny"],
        disagreements=disagreements,
    )
