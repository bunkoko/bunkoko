"""悩みマスター（SPEC §9）：取り込み・正規化・除外・重複排除・優先度。

- 除外はキーワード辞書と LLM の二重で判定し、どちらかに当たれば status=blocked にする
- 一般化した悩み文を埋め込み、コサイン類似度が閾値以上のものを同じクラスタにまとめ、代表1件を残す
- 優先度 = テーマの重み × 需要 × 新しさ
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from koten.embed.encoder import Encoder
from koten.llm import ChatLLM, fill, load_prompt, parse_json_object

AUDIENCES = ["学生", "新社会人", "中堅", "管理職", "全般"]
SOURCE_TYPES = {"search", "comment", "qa", "manual", "seed", "stats"}


@dataclass
class WorriesConfig:
    prompt: str = "worries_v1"
    text_max: int = 40
    cluster_threshold: float = 0.92
    novelty_days: int = 90
    retries: int = 2

    @classmethod
    def from_settings(cls, section: dict[str, Any]) -> WorriesConfig:
        return cls(**{k: v for k, v in section.items() if k in cls.__dataclass_fields__})


def load_blocked_keywords(config_dir: Path) -> dict[str, list[str]]:
    data = yaml.safe_load((config_dir / "blocked_keywords.yaml").read_text("utf-8"))
    return {cat: list(words or []) for cat, words in data["categories"].items()}


def load_theme_weights(config_dir: Path) -> dict[str, float]:
    data = yaml.safe_load((config_dir / "themes.yaml").read_text("utf-8"))
    return {k: float(v) for k, v in (data.get("weights") or {}).items()}


def keyword_block(text: str, categories: dict[str, list[str]]) -> tuple[str, str] | None:
    """除外カテゴリのキーワードに当たれば（カテゴリ, キーワード）を返す。"""
    for cat, words in categories.items():
        for w in words:
            if w and w in text:
                return cat, w
    return None


# --- 取り込み ---------------------------------------------------------------


@dataclass
class ImportResult:
    added: int = 0
    duplicated: int = 0
    skipped: list[str] = field(default_factory=list)


def _next_ids(conn: sqlite3.Connection):
    row = conn.execute("SELECT MAX(CAST(SUBSTR(worry_id, 2) AS INTEGER)) FROM worries").fetchone()
    n = (row[0] or 0) + 1
    while True:
        yield f"W{n:04d}"
        n += 1


def import_csv(conn: sqlite3.Connection, path: Path) -> ImportResult:
    """CSV（text, source_type, source_ref, demand）を status=new で取り込む。同じ文は入れない。"""
    result = ImportResult()
    with path.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    ids = _next_ids(conn)
    with conn:
        for i, r in enumerate(rows, start=2):
            text = (r.get("text") or "").strip()
            source = (r.get("source_type") or "manual").strip()
            if not text:
                result.skipped.append(f"{i}行目：text が空")
                continue
            if source not in SOURCE_TYPES:
                result.skipped.append(f"{i}行目：source_type「{source}」は使えない")
                continue
            try:
                demand = float(r.get("demand") or 0)
            except ValueError:
                result.skipped.append(f"{i}行目：demand が数ではない")
                continue
            exists = conn.execute("SELECT 1 FROM worries WHERE text_raw = ?", (text,)).fetchone()
            if exists:
                result.duplicated += 1
                continue
            conn.execute(
                "INSERT INTO worries(worry_id, text_raw, source_type, source_ref, demand_score) "
                "VALUES (?, ?, ?, ?, ?)",
                (next(ids), text, source, (r.get("source_ref") or "").strip() or None, demand),
            )
            result.added += 1
    return result


# --- 正規化と除外 -----------------------------------------------------------


class WorryOutputBroken(ValueError):
    pass


def response_schema() -> dict[str, Any]:
    props = {
        "text_general": {"type": "string"},
        "situation": {"type": "string"},
        "emotion": {"type": "string"},
        "themes": {"type": "array", "items": {"type": "string"}},
        "audience": {"type": "string"},
        "excluded": {"type": "boolean"},
        "excluded_category": {"type": "string"},
        "reason": {"type": "string"},
    }
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def validate(data: dict[str, Any], vocab: set[str], cfg: WorriesConfig) -> dict[str, Any]:
    text = data.get("text_general")
    if not isinstance(text, str) or not text.strip():
        raise WorryOutputBroken("text_general が無い")
    text = text.strip()
    if len(text) > cfg.text_max:
        raise WorryOutputBroken(f"text_general が{cfg.text_max}字を超える（{len(text)}字）")
    themes = data.get("themes")
    if not isinstance(themes, list):
        raise WorryOutputBroken("themes が配列ではない")
    themes = [t for t in themes if isinstance(t, str) and t in vocab][:3]
    excluded = data.get("excluded") is True
    if not themes and not excluded:
        raise WorryOutputBroken("統制語彙のテーマが無い")
    audience = data.get("audience")
    return {
        "text_general": text,
        "situation": str(data.get("situation") or "").strip(),
        "emotion": str(data.get("emotion") or "").strip(),
        "themes": themes,
        "audience": audience if audience in AUDIENCES else "全般",
        "excluded": excluded,
        "excluded_category": str(data.get("excluded_category") or "").strip(),
        "reason": str(data.get("reason") or "").strip(),
    }


@dataclass
class NormalizeResult:
    active: int = 0
    blocked: int = 0
    errors: int = 0
    blocked_by: dict[str, int] = field(default_factory=dict)


def run_normalize(
    conn: sqlite3.Connection,
    llm: ChatLLM,
    prompts_dir: Path,
    vocab: list[str],
    categories: dict[str, list[str]],
    cfg: WorriesConfig,
    *,
    limit: int | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> NormalizeResult:
    """status=new の悩みを正規化し、除外を判定する（error のものもやり直す）。"""
    system_tpl, user_tpl = load_prompt(prompts_dir, cfg.prompt)
    system = fill(
        system_tpl,
        text_max=cfg.text_max,
        themes="、".join(vocab),
        audiences="・".join(AUDIENCES),
        categories="、".join(categories),
    )
    schema = response_schema()
    vocab_set = set(vocab)
    rows = conn.execute(
        "SELECT worry_id, text_raw FROM worries WHERE status IN ('new', 'error') ORDER BY worry_id"
    ).fetchall()
    if limit is not None:
        rows = rows[:limit]
    result = NormalizeResult()
    blocked_by: dict[str, int] = {}

    def block(wid: str, reason: str, category: str, extra: dict[str, Any] | None = None) -> None:
        cols = {"status": "blocked", "blocked_reason": reason, "error": None, **(extra or {})}
        _update(conn, wid, cols)
        result.blocked += 1
        blocked_by[category] = blocked_by.get(category, 0) + 1
        log(f"  {wid} 除外：{reason}")

    for row in rows:
        wid, raw = row["worry_id"], row["text_raw"]
        # 1. キーワード辞書（LLM に送る前に止める）
        hit = keyword_block(raw, categories)
        if hit:
            block(wid, f"キーワード：{hit[0]}（{hit[1]}）", hit[0])
            continue
        # 2. LLM で正規化し、除外も判定させる
        user = fill(user_tpl, text=raw)
        parsed, error = None, ""
        for _ in range(cfg.retries + 1):
            try:
                parsed = validate(
                    parse_json_object(llm.complete(system, user, schema)), vocab_set, cfg
                )
                break
            except (ValueError, WorryOutputBroken) as e:
                error = f"{type(e).__name__}: {e}"
        if parsed is None:
            _update(conn, wid, {"status": "error", "error": error[:500]})
            result.errors += 1
            log(f"  {wid} 正規化できませんでした：{error}")
            continue
        fields = {
            "text_general": parsed["text_general"],
            "situation": parsed["situation"],
            "emotion": parsed["emotion"],
            "themes": json.dumps(parsed["themes"], ensure_ascii=False),
            "audience": parsed["audience"],
            "llm": llm.name,
            "prompt_ver": cfg.prompt,
        }
        if parsed["excluded"]:
            cat = (
                parsed["excluded_category"]
                if parsed["excluded_category"] in categories
                else (parsed["excluded_category"] or "その他")
            )
            block(wid, f"LLM：{cat}（{parsed['reason']}）", cat, fields)
            continue
        # 3. 一般化した文にもキーワード辞書を当てる（二重の確認）
        hit = keyword_block(parsed["text_general"], categories)
        if hit:
            block(wid, f"キーワード：{hit[0]}（{hit[1]}）", hit[0], fields)
            continue
        _update(conn, wid, {**fields, "status": "active", "blocked_reason": None, "error": None})
        result.active += 1
    result.blocked_by = blocked_by
    return result


def _update(conn: sqlite3.Connection, worry_id: str, cols: dict[str, Any]) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    with conn:
        conn.execute(
            f"UPDATE worries SET {sets}, updated_at = datetime('now') WHERE worry_id = ?",
            [*cols.values(), worry_id],
        )


# --- 重複排除（クラスタ） ---------------------------------------------------


def cluster_worries(
    conn: sqlite3.Connection, encoder: Encoder, threshold: float, *, query_prefix: str = ""
) -> int:
    """status=active の悩みをクラスタにまとめる。需要の大きいものから順に代表にする。

    すでにある代表との類似度が閾値以上ならそのクラスタに入れ、無ければ新しい代表にする。
    戻り値はクラスタの数。enabled（人が付ける使う・使わない）には触れない。
    """
    rows = conn.execute(
        "SELECT worry_id, text_general FROM worries WHERE status = 'active' "
        "ORDER BY demand_score DESC, worry_id"
    ).fetchall()
    if not rows:
        return 0
    vecs = encoder.encode([query_prefix + r["text_general"] for r in rows])
    reps: list[int] = []
    assign: dict[str, tuple[str, bool]] = {}
    for i, r in enumerate(rows):
        if reps:
            sims = vecs[reps] @ vecs[i]
            j = int(np.argmax(sims))
            if sims[j] >= threshold:
                assign[r["worry_id"]] = (rows[reps[j]]["worry_id"], False)
                continue
        reps.append(i)
        assign[r["worry_id"]] = (r["worry_id"], True)
    with conn:
        conn.execute(
            "UPDATE worries SET cluster_id = NULL, is_representative = 0 WHERE status != 'active'"
        )
        conn.executemany(
            "UPDATE worries SET cluster_id = ?, is_representative = ? WHERE worry_id = ?",
            [(cid, int(rep), wid) for wid, (cid, rep) in assign.items()],
        )
    return len(reps)


# --- 優先度 -----------------------------------------------------------------


def priority_score(
    themes: list[str],
    demand: float,
    last_used_at: str | None,
    weights: dict[str, float],
    novelty_days: int,
    now: datetime,
) -> float:
    """テーマの重み（最も重いもの）× 需要（1 + log10(1 + 需要)）× 新しさ（0〜1）。"""
    weight = max((weights.get(t, 1.0) for t in themes), default=1.0)
    demand_factor = 1.0 + math.log10(1.0 + max(demand, 0.0))
    if last_used_at:
        used = datetime.fromisoformat(last_used_at)
        if used.tzinfo is None:
            used = used.replace(tzinfo=UTC)
        days = max((now - used).total_seconds() / 86400, 0.0)
        novelty = min(days / novelty_days, 1.0) if novelty_days > 0 else 1.0
    else:
        novelty = 1.0
    return weight * demand_factor * novelty


def ranked_worries(
    conn: sqlite3.Connection,
    weights: dict[str, float],
    novelty_days: int,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """回の主役にできる悩み（active・代表・enabled）を優先度の高い順に返す。"""
    now = now or datetime.now(UTC)
    rows = conn.execute(
        "SELECT * FROM worries WHERE status = 'active' AND is_representative = 1 AND enabled = 1"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["themes"] = json.loads(d["themes"] or "[]")
        d["score"] = priority_score(
            d["themes"], d["demand_score"], d["last_used_at"], weights, novelty_days, now
        )
        out.append(d)
    return sorted(out, key=lambda d: (-d["score"], d["worry_id"]))
