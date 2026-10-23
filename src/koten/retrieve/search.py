"""検索・リランク（SPEC §8.6）。

悩みを「悩みそのもの」「言い換え」「理想の一節の要旨（HyDE）」の3つのクエリにし、
それぞれで BM25 と密ベクトルを引き、RRF で統合する。絞り込み（使用可・caution なし・
再使用間隔）は検索の段階でかける。リランクはクロスエンコーダ（モデルは設定で選ぶ）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from koten.embed.encoder import Encoder, hf_cache_dir
from koten.index.search import Filter, Hit, HotIndex
from koten.llm import ChatLLM, fill, load_prompt, parse_json_object

QUERY_LABELS = {"text": "悩み", "paraphrase": "言い換え", "hyde": "HyDE"}
RETRIEVER_LABELS = {"dense": "密ベクトル", "bm25": "BM25"}


# --- クエリの変換 -----------------------------------------------------------


@dataclass
class Queries:
    text: str
    paraphrase: str
    hyde: str
    themes: list[str]
    fallback: bool = False  # LLM が使えず、悩みの文をそのまま使った

    def get(self, name: str) -> str:
        return getattr(self, name)


def transform_query(
    conn: sqlite3.Connection,
    llm: ChatLLM,
    prompts_dir: Path,
    prompt: str,
    key: str,
    text: str,
    vocab: list[str],
    *,
    known_themes: Iterable[str] = (),
    retries: int = 2,
) -> Queries:
    """悩み（かテーマ）を3つのクエリにする。結果は query_cache に控え、次からは LLM を呼ばない。"""
    known = [t for t in known_themes if t in vocab]
    row = conn.execute(
        "SELECT queries FROM query_cache WHERE worry_key = ? AND source_text = ? "
        "AND llm = ? AND prompt_ver = ?",
        (key, text, llm.name, prompt),
    ).fetchone()
    if row:
        d = json.loads(row["queries"])
        return Queries(text, d["paraphrase"], d["hyde"], _merge_themes(known, d["themes"]))

    system_tpl, user_tpl = load_prompt(prompts_dir, prompt)
    system = fill(system_tpl, paraphrase_max=40, hyde_max=80, themes="、".join(vocab))
    user = fill(user_tpl, text=text)
    schema = {
        "type": "object",
        "properties": {
            "paraphrase": {"type": "string"},
            "hyde": {"type": "string"},
            "themes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["paraphrase", "hyde", "themes"],
        "additionalProperties": False,
    }
    for _ in range(retries + 1):
        try:
            d = parse_json_object(llm.complete(system, user, schema))
        except ValueError:
            continue
        para, hyde = str(d.get("paraphrase") or "").strip(), str(d.get("hyde") or "").strip()
        themes = [t for t in d.get("themes") or [] if isinstance(t, str) and t in vocab]
        if para and hyde:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO query_cache(worry_key, source_text, llm, prompt_ver, "
                    "queries) VALUES (?, ?, ?, ?, ?)",
                    (
                        key,
                        text,
                        llm.name,
                        prompt,
                        json.dumps(
                            {"paraphrase": para, "hyde": hyde, "themes": themes},
                            ensure_ascii=False,
                        ),
                    ),
                )
            return Queries(text, para, hyde, _merge_themes(known, themes))
    # LLM の出力が壊れ続けたら、悩みの文だけで検索する（控えには残さない）
    return Queries(text, text, text, known, fallback=True)


def _merge_themes(first: list[str], second: list[str]) -> list[str]:
    out: list[str] = []
    for t in [*first, *second]:
        if t not in out:
            out.append(t)
    return out[:3]


# --- ハイブリッド検索（RRF） ------------------------------------------------


@dataclass
class Scored:
    candidate_id: str
    row: dict[str, Any]
    rrf: float = 0.0
    ranks: dict[str, int] = field(default_factory=dict)  # 「悩み・密ベクトル」→ 順位
    rerank: float | None = None

    @property
    def score(self) -> float:
        return self.rerank if self.rerank is not None else self.rrf

    def provenance(self, limit: int = 3) -> str:
        best = sorted(self.ranks.items(), key=lambda kv: kv[1])[:limit]
        return "、".join(f"{k} {v}位" for k, v in best)


def rrf_fuse(lists: dict[str, list[Hit]], *, k: int = 60, top: int = 50) -> list[Scored]:
    """Reciprocal Rank Fusion：各リストの順位 r に 1/(k + r) を足し合わせる。"""
    fused: dict[str, Scored] = {}
    for label, hits in lists.items():
        for rank, h in enumerate(hits, start=1):
            s = fused.setdefault(h.candidate_id, Scored(h.candidate_id, h.row))
            s.rrf += 1.0 / (k + rank)
            s.ranks[label] = rank
    return sorted(fused.values(), key=lambda s: (-s.rrf, s.candidate_id))[:top]


def hybrid_search(
    index: HotIndex,
    encoder: Encoder,
    q: Queries,
    flt: Filter | None,
    *,
    query_prefix: str = "",
    passage_prefix: str = "",
    queries: Iterable[str] = ("text", "paraphrase", "hyde"),
    retrievers: Iterable[str] = ("dense", "bm25"),
    per_list: int = 100,
    rrf_k: int = 60,
    top: int = 50,
) -> list[Scored]:
    names = [n for n in queries if n in QUERY_LABELS]
    if q.fallback:
        names = ["text"]
    lists: dict[str, list[Hit]] = {}
    retrievers = list(retrievers)
    if "dense" in retrievers:
        # HyDE は「一節の要旨」なので、候補と同じ passage の接頭辞で埋め込む
        texts = [(passage_prefix if n == "hyde" else query_prefix) + q.get(n) for n in names]
        for n, vec in zip(names, encoder.encode(texts), strict=True):
            lists[f"{QUERY_LABELS[n]}・密ベクトル"] = index.search_dense(vec, per_list, flt)
    if "bm25" in retrievers:
        for n in names:
            lists[f"{QUERY_LABELS[n]}・BM25"] = index.search_bm25(q.get(n), per_list, flt)
    return rrf_fuse(lists, k=rrf_k, top=top)


# --- リランク ---------------------------------------------------------------


class Reranker(Protocol):
    name: str

    def score(self, query: str, docs: list[str]) -> list[float]: ...


class CrossEncoderReranker:
    def __init__(self, model: str, *, device: str = "auto", batch_size: int = 16):
        if hf_cache_dir(model).exists():
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as e:
            raise RuntimeError(
                "sentence-transformers が入っていません。`uv sync --extra ml` を実行してください"
            ) from e
        if device == "auto":
            import torch

            device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.name = model
        self.batch_size = batch_size
        self._model = CrossEncoder(model, device=device)

    def score(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        out = self._model.predict([(query, d) for d in docs], batch_size=self.batch_size)
        return [float(x) for x in out]


def make_reranker(model: str | None, *, device: str = "auto") -> Reranker | None:
    """モデルが決まっていなければ（"要確認" か空）リランクしない。"""
    if not model or model == "要確認":
        return None
    return CrossEncoderReranker(model, device=device)


def rerank_text(row: dict[str, Any]) -> str:
    return f"{row['quote_text']}\n（要旨）{row['summary_ja']}"


def apply_rerank(items: list[Scored], reranker: Reranker | None, query: str) -> list[Scored]:
    """全件にリランクの点を付けて並べ替える。リランクしないときは RRF の順のまま。"""
    if reranker is None or not items:
        return items
    scores = reranker.score(query, [rerank_text(s.row) for s in items])
    for s, v in zip(items, scores, strict=True):
        s.rerank = v
    return sorted(items, key=lambda s: (-(s.rerank or 0.0), s.candidate_id))


# --- 絞り込みの理由（--explain 用） -----------------------------------------


def filter_report(index: HotIndex, flt: Filter) -> dict[str, int]:
    """絞り込みで外れる候補の数を、理由ごとに数える。"""
    total = len(index)
    base = index.mask(Filter(exclude_caution=False))
    out = {"索引の候補": total}
    out["注意フラグ（caution）"] = int((base & ~index.mask(Filter(exclude_caution=True))).sum())
    if flt.exclude_work_ids:
        only = Filter(exclude_caution=False, exclude_work_ids=flt.exclude_work_ids)
        out["直近の回で使った作品"] = int((base & ~index.mask(only)).sum())
    if flt.exclude_author_ids:
        only = Filter(exclude_caution=False, exclude_author_ids=flt.exclude_author_ids)
        out["直近の回で使った著者"] = int((base & ~index.mask(only)).sum())
    out["検索の対象"] = int(index.mask(flt).sum())
    return out
