"""検索の評価を、スマホ（Claude のアプリで開くページ）で付けるための部品。

流れ：Mac で `koten eval export` → 評価セット（tests/eval/retrieval.csv）を GitHub に入れる →
Claude が `koten eval page` でページを作って載せる → 人がスマホで「合う／合わない」を押す
（ページのデータベースに残る）→ Claude がデータベースの点を読み、`koten eval merge` で
grade 列に書いて GitHub に入れる → Mac で取り込み、`koten eval grade` などに進む。

- ページに入れるのは、悩み・作品名・著者・一節・前後の文・要旨と、すでに付いた人の点だけ。
  LLM の点と理由は入れない（人の点が引きずられないように）
- 一節は保護期間の切れた作品のもの（索引は権利で使えるものだけ）。今の本（S4）や有料の論文の
  本文は評価セットに無い
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from koten.eval.retrieval import parse_grade, read_rows, write_eval

TEMPLATE = Path(__file__).with_name("phone_page.html")
ITEMS_MARK = "/*__ITEMS__*/[]"
SAMPLE_MARK = "/*__SAMPLE__*/false"


def row_key(worry_id: str, candidate_id: str) -> str:
    """ページのデータベースの文書名（英数字と - だけ）。悩みと一節の組で決まる。"""
    digest = hashlib.sha1(f"{worry_id}\t{candidate_id}".encode()).hexdigest()[:12]
    head = "".join(ch for ch in worry_id if ch.isascii() and ch.isalnum()) or "W"
    return f"{head}-{digest}"


def page_items(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        g = parse_grade(r.get("grade"))
        out.append(
            {
                "k": row_key(r["worry_id"], r["candidate_id"]),
                "w": r["worry_id"],
                "wt": r["worry_text"],
                "t": r["title"],
                "a": r["authors"],
                "s": r["section"],
                "q": r["quote_text"],
                "c": r.get("context_text") or "",
                "m": r["summary_ja"],
                "g": "" if g is None else str(g),
            }
        )
    return out


def _script_json(value: Any) -> str:
    # <script> の中に置くので、</script> などで抜け出せないようにする
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def build_page(rows: list[dict[str, str]], *, sample: bool = False) -> str:
    html = TEMPLATE.read_text("utf-8")
    if ITEMS_MARK not in html or SAMPLE_MARK not in html:
        raise ValueError(f"{TEMPLATE.name} に差し込む印がない")
    return html.replace(ITEMS_MARK, _script_json(page_items(rows))).replace(
        SAMPLE_MARK, "true" if sample else "false"
    )


def merge_grades(path: Path, grades: dict[str, str]) -> tuple[int, int, list[str]]:
    """ページで付けた点を grade 列に書く。

    戻り値は（変わった行, 合った点の数, 評価セットに無いキー）。

    ページの点は人が付けたいちばん新しい点なので、CSV の点と違えばページの点にする。
    「1」「0」以外の値は読まない。
    """
    rows = read_rows(path)
    by_key = {row_key(r["worry_id"], r["candidate_id"]): r for r in rows}
    changed, matched, unknown = 0, 0, []
    for key, value in grades.items():
        g = str(value).strip()
        if g not in ("1", "0"):
            continue
        r = by_key.get(key)
        if r is None:
            unknown.append(key)
            continue
        matched += 1
        if r["grade"].strip() != g:
            r["grade"] = g
            changed += 1
    if changed:
        write_eval(path, rows)
    return changed, matched, unknown
