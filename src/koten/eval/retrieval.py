"""検索の評価（SPEC §12）：評価セットで nDCG@10 と Recall@20 を出す。

評価セットは tests/eval/retrieval.csv（悩み × 一節 × 点）。
点は 1＝合う、0＝合わない の2値（人がぶれずに付けられるように）。空欄は未判定で、
計算では 0 と同じに扱う。前の版の 2（合う）は 1 として読む。
- grade：人が付けた点
- llm_grade・llm_reason・llm_grader：LLM の grader（Claude Haiku など）が付けた点と理由

1. `koten eval export`：悩みごとに、いくつかの検索のやり方の上位を合わせて（プーリング）
   印付け用の CSV を書き出す。すでに付けた点は残す
2. 人が grade 列に点を付ける。`koten eval grade` で LLM も点を付ける
3. `koten eval agreement`：人と LLM の点がどれだけ一致するかを見る
4. `koten eval retrieval`：やり方ごとに指標を出して比べる（--by llm で LLM の点を使う）
"""

from __future__ import annotations

import csv
import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from koten.authors import author_label
from koten.index.search import Filter

EVAL_COLUMNS = [
    "worry_id",
    "worry_text",
    "candidate_id",
    "title",
    "authors",
    "section",
    "quote_text",
    "summary_ja",
    "grade",
    "llm_grade",
    "llm_reason",
    "llm_grader",
]
KEEP_COLUMNS = ["grade", "llm_grade", "llm_reason", "llm_grader"]  # 書き出し直しても残す列


def parse_grade(value: str | None) -> int | None:
    """「1」「0」を点にする。前の版の「2」は 1、空欄や読めない値は None（未判定）。"""
    v = (value or "").strip()
    if v in ("1", "2"):
        return 1
    if v == "0":
        return 0
    return None


@dataclass(frozen=True)
class EvalConfig:
    name: str
    queries: tuple[str, ...]
    retrievers: tuple[str, ...]
    rerank: bool = False


CONFIGS = [
    EvalConfig("密ベクトル（悩みだけ）", ("text",), ("dense",)),
    EvalConfig("BM25（悩みだけ）", ("text",), ("bm25",)),
    EvalConfig("ハイブリッド（悩み＋言い換え）", ("text", "paraphrase"), ("dense", "bm25")),
    EvalConfig("ハイブリッド＋HyDE", ("text", "paraphrase", "hyde"), ("dense", "bm25")),
    EvalConfig(
        "ハイブリッド＋HyDE＋リランク", ("text", "paraphrase", "hyde"), ("dense", "bm25"), True
    ),
]


def dcg(gains: list[float]) -> float:
    return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(ranked: list[str], grades: dict[str, int], k: int = 10) -> float:
    ideal = dcg(sorted(grades.values(), reverse=True)[:k])
    if ideal == 0:
        return 0.0
    return dcg([grades.get(c, 0) for c in ranked[:k]]) / ideal


def recall_at_k(ranked: list[str], grades: dict[str, int], k: int = 20) -> float | None:
    relevant = {c for c, g in grades.items() if g >= 1}
    if not relevant:
        return None
    return len(relevant & set(ranked[:k])) / len(relevant)


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as f:
        return [{c: r.get(c) or "" for c in EVAL_COLUMNS} for r in csv.DictReader(f)]


def write_eval(path: Path, rows: list[dict[str, str]]) -> None:
    """Excel・Numbers で開けるよう BOM 付きで、書き終えてから置き換える。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EVAL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def read_eval(path: Path, column: str = "grade") -> dict[str, dict[str, int]]:
    """{worry_id: {candidate_id: 点}}。未判定は読まない。

    column は grade（人の点）か llm_grade（LLM の点）。
    """
    out: dict[str, dict[str, int]] = {}
    for r in read_rows(path):
        g = parse_grade(r.get(column))
        if g is not None:
            out.setdefault(r["worry_id"], {})[r["candidate_id"]] = g
    return out


Run = Callable[[str, EvalConfig, int], list[str]]  # (worry_id, やり方, 件数) → 候補IDの順位


def export_pool(
    path: Path,
    worries: list[dict],
    run: Run,
    rows_of: Callable[[str], dict],
    configs: list[EvalConfig],
    *,
    k: int = 10,
    seed: int = 0,
) -> tuple[int, int]:
    """印付け用の CSV を書く。戻り値は（行の数, そのうち点が付いている数）。"""
    existing: dict[tuple[str, str], dict[str, str]] = {}
    if path.exists():
        for r in read_rows(path):
            existing[(r["worry_id"], r["candidate_id"])] = {c: r[c] for c in KEEP_COLUMNS}
    rows = []
    rng = random.Random(seed)
    for w in worries:
        pool: list[str] = []
        for cfg in configs:
            for cid in run(w["worry_id"], cfg, k):
                if cid not in pool:
                    pool.append(cid)
        rng.shuffle(pool)  # どのやり方で出たかが分からない順にして、印の偏りを防ぐ
        for cid in pool:
            r = rows_of(cid)
            rows.append(
                {
                    "worry_id": w["worry_id"],
                    "worry_text": w["text_general"],
                    "candidate_id": cid,
                    "title": r["title"],
                    "authors": author_label(r),
                    "section": r["section"] or "",
                    "quote_text": r["quote_text"],
                    "summary_ja": r["summary_ja"],
                    **{c: "" for c in KEEP_COLUMNS},
                    **existing.get((w["worry_id"], cid), {}),
                }
            )
    write_eval(path, rows)
    return len(rows), sum(1 for r in rows if parse_grade(r["grade"]) is not None)


@dataclass
class EvalResult:
    name: str
    ndcg10: float
    recall20: float | None
    unjudged10: float  # 上位10件のうち点が付いていない割合（大きいと評価が甘くなる）
    worries: int


def evaluate(
    grades: dict[str, dict[str, int]], run: Run, configs: list[EvalConfig]
) -> list[EvalResult]:
    results = []
    for cfg in configs:
        ndcgs, recalls, unjudged = [], [], []
        for wid, g in grades.items():
            ranked = run(wid, cfg, 20)
            ndcgs.append(ndcg_at_k(ranked, g, 10))
            r = recall_at_k(ranked, g, 20)
            if r is not None:
                recalls.append(r)
            top = ranked[:10]
            unjudged.append(sum(1 for c in top if c not in g) / len(top) if top else 0.0)
        n = len(ndcgs)
        results.append(
            EvalResult(
                cfg.name,
                sum(ndcgs) / n if n else 0.0,
                sum(recalls) / len(recalls) if recalls else None,
                sum(unjudged) / n if n else 0.0,
                n,
            )
        )
    return results


def report(results: list[EvalResult]) -> str:
    lines = [
        "| やり方 | nDCG@10 | Recall@20 | 上位10件の未判定 | 悩み |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        recall = f"{r.recall20:.3f}" if r.recall20 is not None else "—"
        lines.append(f"| {r.name} | {r.ndcg10:.3f} | {recall} | {r.unjudged10:.0%} | {r.worries} |")
    return "\n".join(lines)


EVAL_FILTER = Filter(exclude_caution=True)  # 評価では再使用間隔をかけない
