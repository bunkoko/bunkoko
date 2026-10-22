"""引用候補の抽出と付加情報（SPEC §8.3）。

チャンクごとにLLMへ一節を抜き出させ、原文の中で位置を確かめてから保存する。
原文の中に見つからない一節は捨てる。保存する quote_text は原文から切り出した文字列なので、
保存済みの引用は必ず原文と一致する。

出力は <HDD>/augment/<llm>/<prompt版>/ に、まとめて書く：
- part-NNNNN.parquet：引用候補
- done-NNNNN.parquet：処理済みのチャンク（候補0件や失敗も含む）。再開時はここにあるチャンクを飛ばす
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from koten.llm import ChatLLM, LLMError, fill, load_prompt, parse_json_object
from koten.parquet import read_rows, write_rows
from koten.textnorm import locate, normalize_for_match

CANDIDATES_SCHEMA = pa.schema(
    [
        ("candidate_id", pa.string()),
        ("chunk_id", pa.string()),
        ("work_id", pa.string()),
        ("section", pa.string()),
        ("quote_text", pa.string()),
        ("quote_start", pa.int32()),
        ("quote_end", pa.int32()),
        ("context_text", pa.string()),
        ("summary_ja", pa.string()),
        ("themes", pa.list_(pa.string())),
        ("worries", pa.list_(pa.string())),
        ("quotability", pa.float32()),
        ("caution", pa.bool_()),
        ("caution_reason", pa.string()),
        ("llm", pa.string()),
        ("prompt_ver", pa.string()),
    ]
)

DONE_SCHEMA = pa.schema(
    [
        ("chunk_id", pa.string()),
        ("status", pa.string()),  # ok / failed
        ("n_candidates", pa.int32()),
        ("dropped", pa.string()),  # JSON: {理由: 件数}
        ("error", pa.string()),
    ]
)


class OutputBroken(ValueError):
    """LLM の出力が JSON として壊れている、または形が違う（再試行の対象）。"""


@dataclass
class AugmentConfig:
    prompt: str = "augment_v1"
    quote_min: int = 20
    quote_max: int = 120
    max_quotes: int = 3
    summary_max: int = 60
    worry_max: int = 20
    retries: int = 2
    batch_size: int = 200
    context_chars: int = 200

    @classmethod
    def from_settings(cls, section: dict[str, Any]) -> AugmentConfig:
        return cls(**{k: v for k, v in section.items() if k in cls.__dataclass_fields__})


def response_schema(cfg: AugmentConfig) -> dict[str, Any]:
    """LLM に渡す JSON スキーマ（OpenAI 互換の response_format 用）。"""
    quote = {
        "type": "object",
        "properties": {
            "quote_text": {"type": "string"},
            "summary_ja": {"type": "string"},
            "themes": {"type": "array", "items": {"type": "string"}},
            "worries": {"type": "array", "items": {"type": "string"}},
            "quotability": {"type": "number"},
            "caution": {"type": "boolean"},
            "caution_reason": {"type": "string"},
        },
        "required": [
            "quote_text",
            "summary_ja",
            "themes",
            "worries",
            "quotability",
            "caution",
            "caution_reason",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"quotes": {"type": "array", "items": quote, "maxItems": cfg.max_quotes}},
        "required": ["quotes"],
        "additionalProperties": False,
    }


def load_themes(config_dir: Path) -> list[str]:
    return yaml.safe_load((config_dir / "themes.yaml").read_text("utf-8"))["themes"]


def _check_shape(data: dict[str, Any]) -> list[dict[str, Any]]:
    quotes = data.get("quotes")
    if not isinstance(quotes, list):
        raise OutputBroken("quotes が配列ではない")
    for q in quotes:
        if not isinstance(q, dict):
            raise OutputBroken("quotes の要素がオブジェクトではない")
        if not isinstance(q.get("quote_text"), str) or not isinstance(q.get("summary_ja"), str):
            raise OutputBroken("quote_text か summary_ja が文字列ではない")
        if not isinstance(q.get("themes"), list) or not isinstance(q.get("worries"), list):
            raise OutputBroken("themes か worries が配列ではない")
    return quotes


def validate_quotes(
    data: dict[str, Any],
    chunk: dict[str, Any],
    work_text: str,
    vocab: set[str],
    cfg: AugmentConfig,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """LLM の出力から、条件を満たす一節だけを候補にする。戻り値は（候補, 捨てた理由の件数）。"""
    dropped: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for q in _check_shape(data)[: cfg.max_quotes]:
        span = locate(q["quote_text"], chunk["text_orig"])
        if span is None:
            dropped["原文に無い"] += 1
            continue
        local_start, local_end = span
        quote_text = chunk["text_orig"][local_start:local_end]
        length = len(normalize_for_match(quote_text))
        if not cfg.quote_min <= length <= cfg.quote_max:
            dropped["長さが範囲外"] += 1
            continue
        if span in seen:
            dropped["重複"] += 1
            continue
        summary = q["summary_ja"].strip()
        if not summary or len(summary) > cfg.summary_max:
            dropped["要旨の長さ"] += 1
            continue
        themes = [t for t in q["themes"] if isinstance(t, str) and t in vocab][:3]
        if not themes:
            dropped["統制語彙のテーマが無い"] += 1
            continue
        worries = [
            w.strip()
            for w in q["worries"]
            if isinstance(w, str) and w.strip() and len(w.strip()) <= cfg.worry_max
        ][:3]
        if not worries:
            dropped["悩みが無い"] += 1
            continue
        seen.add(span)
        try:
            quotability = min(max(float(q.get("quotability", 0)), 0.0), 1.0)
        except (TypeError, ValueError):
            quotability = 0.0
        start = chunk["char_start"] + local_start
        end = chunk["char_start"] + local_end
        rows.append(
            {
                "quote_text": quote_text,
                "quote_start": start,
                "quote_end": end,
                "context_text": work_text[
                    max(0, start - cfg.context_chars) : end + cfg.context_chars
                ],
                "summary_ja": summary,
                "themes": themes,
                "worries": worries,
                "quotability": quotability,
                "caution": q.get("caution") is True,
                "caution_reason": str(q.get("caution_reason") or ""),
            }
        )
    return rows, dropped


def llm_slug(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name).strip("_") or "llm"


@dataclass
class AugmentResult:
    processed: int = 0
    candidates: int = 0
    failed: int = 0
    skipped: int = 0
    dropped: Counter[str] = field(default_factory=Counter)
    out_dir: Path | None = None


class _Writer:
    def __init__(self, out_dir: Path, batch_size: int):
        self.out_dir = out_dir
        self.batch_size = batch_size
        self.cands: list[dict[str, Any]] = []
        self.done: list[dict[str, Any]] = []
        self.seq = len(list(out_dir.glob("done-*.parquet"))) if out_dir.exists() else 0

    def add(self, cands: list[dict[str, Any]], done: dict[str, Any]) -> None:
        self.cands.extend(cands)
        self.done.append(done)
        if len(self.done) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self.done:
            return
        self.seq += 1
        # 候補を先に書き、処理済みの記録は後に書く（途中で落ちても未処理として残る）
        write_rows(self.out_dir / f"part-{self.seq:05d}.parquet", self.cands, CANDIDATES_SCHEMA)
        write_rows(self.out_dir / f"done-{self.seq:05d}.parquet", self.done, DONE_SCHEMA)
        self.cands, self.done = [], []


def done_chunk_ids(out_dir: Path) -> set[str]:
    ids: set[str] = set()
    for p in sorted(out_dir.glob("done-*.parquet")):
        ids.update(pq.read_table(p, columns=["chunk_id"]).column(0).to_pylist())
    return ids


def iter_candidates(out_dir: Path) -> Iterator[dict[str, Any]]:
    """part-*.parquet の候補を、対応する done があるものだけ返す。"""
    for p in sorted(out_dir.glob("part-*.parquet")):
        if (out_dir / p.name.replace("part-", "done-")).exists():
            yield from read_rows(p)


def _chunks(hdd_root: Path) -> Iterator[dict[str, Any]]:
    for p in sorted((hdd_root / "chunks").glob("*.parquet")):
        yield from read_rows(p)


def _works_and_texts(hdd_root: Path) -> tuple[dict[str, dict], dict[str, str]]:
    works: dict[str, dict] = {}
    texts: dict[str, str] = {}
    for src in sorted((hdd_root / "clean").iterdir()) if (hdd_root / "clean").exists() else []:
        if (src / "works.parquet").exists():
            works.update({w["work_id"]: w for w in read_rows(src / "works.parquet")})
        if (src / "texts.parquet").exists():
            t = pq.read_table(src / "texts.parquet", columns=["work_id", "text_orig"])
            texts.update(zip(t.column(0).to_pylist(), t.column(1).to_pylist(), strict=True))
    return works, texts


def augment_chunk(
    llm: ChatLLM,
    system: str,
    user: str,
    chunk: dict[str, Any],
    work_text: str,
    vocab: set[str],
    cfg: AugmentConfig,
    schema: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """1チャンクを処理する。出力が壊れていれば retries 回まで再試行し、だめなら failed にする。"""
    error = ""
    for _ in range(cfg.retries + 1):
        try:
            data = parse_json_object(llm.complete(system, user, schema))
            rows, dropped = validate_quotes(data, chunk, work_text, vocab, cfg)
        except (ValueError, OutputBroken) as e:  # JSONDecodeError は ValueError
            error = f"{type(e).__name__}: {e}"
            continue
        for i, r in enumerate(rows, start=1):
            r.update(
                candidate_id=f"{chunk['chunk_id']}/q{i}",
                chunk_id=chunk["chunk_id"],
                work_id=chunk["work_id"],
                section=chunk["section"],
                llm=llm.name,
                prompt_ver=cfg.prompt,
            )
        done = {
            "chunk_id": chunk["chunk_id"],
            "status": "ok",
            "n_candidates": len(rows),
            "dropped": json.dumps(dropped, ensure_ascii=False),
            "error": "",
        }
        return rows, done
    done = {
        "chunk_id": chunk["chunk_id"],
        "status": "failed",
        "n_candidates": 0,
        "dropped": "{}",
        "error": error[:500],
    }
    return [], done


def run_augment(
    hdd_root: Path,
    prompts_dir: Path,
    vocab: list[str],
    llm: ChatLLM,
    cfg: AugmentConfig,
    *,
    limit: int | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> AugmentResult:
    """未処理のチャンクに付加情報を付ける。中断しても、再実行で続きから進む。"""
    out_dir = hdd_root / "augment" / llm_slug(llm.name) / cfg.prompt
    out_dir.mkdir(parents=True, exist_ok=True)
    system_tpl, user_tpl = load_prompt(prompts_dir, cfg.prompt)
    system = fill(
        system_tpl,
        quote_min=cfg.quote_min,
        quote_max=cfg.quote_max,
        max_quotes=cfg.max_quotes,
        summary_max=cfg.summary_max,
        worry_max=cfg.worry_max,
        themes="、".join(vocab),
    )
    schema = response_schema(cfg)
    vocab_set = set(vocab)
    done = done_chunk_ids(out_dir)
    works, texts = _works_and_texts(hdd_root)
    result = AugmentResult(skipped=len(done), out_dir=out_dir)
    log(f"出力先：{out_dir}（処理済み {len(done)} チャンクは飛ばします）")

    writer = _Writer(out_dir, cfg.batch_size)
    try:
        for chunk in _chunks(hdd_root):
            if chunk["chunk_id"] in done:
                continue
            if limit is not None and result.processed >= limit:
                break
            work = works.get(chunk["work_id"], {})
            user = fill(
                user_tpl,
                title=work.get("title", ""),
                author="・".join(work.get("author_names") or []),
                section=chunk["section"] or "（なし）",
                text=chunk["text_orig"],
            )
            rows, done_row = augment_chunk(
                llm, system, user, chunk, texts.get(chunk["work_id"], ""), vocab_set, cfg, schema
            )
            writer.add(rows, done_row)
            result.processed += 1
            result.candidates += len(rows)
            result.dropped.update(json.loads(done_row["dropped"]))
            if done_row["status"] == "failed":
                result.failed += 1
            if result.processed % 10 == 0:
                log(f"  {result.processed} チャンク処理、候補 {result.candidates} 件")
    except LLMError:
        log("LLM の呼び出しに失敗したので止めます。ここまでの結果は保存します")
        raise
    finally:
        writer.flush()
    return result
