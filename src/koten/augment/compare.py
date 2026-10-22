"""ローカルLLMのモデルを、同じチャンクの結果で比べる（D-005 のモデル選び用）。

どのモデルも処理済みのチャンクだけを、チャンクの並び順に先頭から n 件使う。
集計表を返し、チャンクごとの並べ表示を Markdown にして <HDD>/exports/ に書く。
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from koten.augment.extract import _chunks, iter_candidates, llm_slug
from koten.parquet import read_rows


@dataclass
class ModelSummary:
    model: str
    chunks: int = 0
    candidates: int = 0
    failed: int = 0
    dropped: Counter[str] = field(default_factory=Counter)
    quotability: float = 0.0
    caution: int = 0
    theme_counts: Counter[str] = field(default_factory=Counter)


def _done_rows(out_dir: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for p in sorted(out_dir.glob("done-*.parquet")):
        rows.update({r["chunk_id"]: r for r in read_rows(p)})
    return rows


def compare_models(
    hdd_root: Path, prompt: str, models: list[str], n: int = 10
) -> tuple[list[ModelSummary], str, list[str]]:
    """戻り値は（モデルごとの集計, 並べ表示の Markdown, 比べたチャンクのID）。"""
    dirs = {m: hdd_root / "augment" / llm_slug(m) / prompt for m in models}
    missing = [m for m, d in dirs.items() if not d.exists()]
    if missing:
        raise FileNotFoundError(f"まだ augment していないモデル：{'、'.join(missing)}")
    done = {m: _done_rows(d) for m, d in dirs.items()}
    common = set.intersection(*(set(d) for d in done.values()))
    chunks = [c for c in _chunks(hdd_root) if c["chunk_id"] in common][:n]
    picked = {c["chunk_id"] for c in chunks}

    by_chunk: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    summaries = []
    for m, d in dirs.items():
        s = ModelSummary(model=m, chunks=len(chunks))
        for cid in picked:
            row = done[m][cid]
            s.failed += row["status"] == "failed"
            s.dropped.update(json.loads(row["dropped"] or "{}"))
        q_total = 0.0
        for c in iter_candidates(d):
            if c["chunk_id"] in picked:
                by_chunk[c["chunk_id"]][m].append(c)
                s.candidates += 1
                q_total += c["quotability"]
                s.caution += bool(c["caution"])
                s.theme_counts.update(c["themes"])
        s.quotability = q_total / s.candidates if s.candidates else 0.0
        summaries.append(s)

    lines = [
        f"# ローカルLLMの比較（{datetime.now():%Y-%m-%d %H:%M}）",
        "",
        f"どのモデルも処理したチャンクのうち、先頭の {len(chunks)} 件で比べた：",
        "、".join(c["chunk_id"] for c in chunks),
        "",
        summary_table(summaries),
    ]
    for c in chunks:
        head = c["text_orig"].strip().replace("\n", " ")
        lines += ["", f"## {c['chunk_id']}（{c['section'] or '章なし'}）", "", f"> {head}", ""]
        for m in models:
            quotes = by_chunk[c["chunk_id"]].get(m, [])
            dropped = json.loads(done[m][c["chunk_id"]]["dropped"] or "{}")
            note = f"（捨てた一節：{dropped}）" if dropped else ""
            lines.append(f"### {m}：{len(quotes)}件{note}")
            for q in quotes:
                flag = f" ※注意：{q['caution_reason']}" if q["caution"] else ""
                lines += [
                    f"- 「{q['quote_text']}」（引用映え {q['quotability']:.1f}）{flag}",
                    f"  - 要旨：{q['summary_ja']}",
                    f"  - テーマ：{'、'.join(q['themes'])}",
                    f"  - 悩み：{' / '.join(q['worries'])}",
                ]
            lines.append("")
    return summaries, "\n".join(lines), [c["chunk_id"] for c in chunks]


def summary_table(summaries: list[ModelSummary]) -> str:
    rows = [
        "| モデル | 候補 | 1チャンクあたり | 捨てた一節 | 失敗 | 引用映えの平均 | 注意 |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        per = s.candidates / s.chunks if s.chunks else 0.0
        dropped = "、".join(f"{k} {v}" for k, v in s.dropped.most_common()) or "なし"
        rows.append(
            f"| {s.model} | {s.candidates} | {per:.1f} | {dropped} | {s.failed} | "
            f"{s.quotability:.2f} | {s.caution} |"
        )
    return "\n".join(rows)


def write_report(hdd_root: Path, markdown: str) -> Path:
    out = hdd_root / "exports" / f"augment-compare-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown, "utf-8")
    return out
