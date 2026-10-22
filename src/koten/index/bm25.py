"""BM25（Okapi）。転置索引を CSR 形式の npz と語彙の JSON で保存する。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np


class BM25:
    def __init__(
        self,
        vocab: dict[str, int],
        indptr: np.ndarray,
        doc_ids: np.ndarray,
        tfs: np.ndarray,
        doc_len: np.ndarray,
        k1: float = 1.5,
        b: float = 0.75,
    ):
        self.vocab = vocab
        self.indptr = indptr
        self.doc_ids = doc_ids
        self.tfs = tfs
        self.doc_len = doc_len.astype(np.float32)
        self.avgdl = float(self.doc_len.mean()) if len(self.doc_len) else 0.0
        self.k1 = k1
        self.b = b

    @classmethod
    def build(cls, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> BM25:
        vocab: dict[str, int] = {}
        postings: list[list[tuple[int, int]]] = []
        for d, tokens in enumerate(docs):
            for term, tf in Counter(tokens).items():
                tid = vocab.setdefault(term, len(vocab))
                if tid == len(postings):
                    postings.append([])
                postings[tid].append((d, tf))
        indptr = np.zeros(len(postings) + 1, dtype=np.int64)
        for t, p in enumerate(postings):
            indptr[t + 1] = indptr[t] + len(p)
        doc_ids = np.fromiter((d for p in postings for d, _ in p), dtype=np.int32)
        tfs = np.fromiter((tf for p in postings for _, tf in p), dtype=np.float32)
        doc_len = np.array([len(t) for t in docs], dtype=np.float32)
        return cls(vocab, indptr, doc_ids, tfs, doc_len, k1, b)

    def scores(self, query_tokens: list[str]) -> np.ndarray:
        n = len(self.doc_len)
        out = np.zeros(n, dtype=np.float32)
        for term in set(query_tokens):
            tid = self.vocab.get(term)
            if tid is None:
                continue
            lo, hi = self.indptr[tid], self.indptr[tid + 1]
            docs, tf = self.doc_ids[lo:hi], self.tfs[lo:hi]
            df = hi - lo
            idf = np.log(1.0 + (n - df + 0.5) / (df + 0.5))
            norm = self.k1 * (1 - self.b + self.b * self.doc_len[docs] / max(self.avgdl, 1e-9))
            out[docs] += idf * tf * (self.k1 + 1) / (tf + norm)
        return out

    def save(self, directory: Path) -> None:
        np.savez(
            directory / "bm25.npz",
            indptr=self.indptr,
            doc_ids=self.doc_ids,
            tfs=self.tfs,
            doc_len=self.doc_len,
            params=np.array([self.k1, self.b], dtype=np.float32),
        )
        (directory / "bm25_vocab.json").write_text(json.dumps(self.vocab, ensure_ascii=False))

    @classmethod
    def load(cls, directory: Path) -> BM25:
        z = np.load(directory / "bm25.npz")
        vocab = json.loads((directory / "bm25_vocab.json").read_text("utf-8"))
        k1, b = (float(x) for x in z["params"])
        return cls(vocab, z["indptr"], z["doc_ids"], z["tfs"], z["doc_len"], k1, b)


def top_k(scores: np.ndarray, k: int, *, positive_only: bool = False) -> list[tuple[int, float]]:
    """スコアの高い順に（行番号, スコア）を返す。positive_only なら0以下を除く（BM25 用）。"""
    if len(scores) == 0:
        return []
    k = min(k, len(scores))
    idx = np.argpartition(-scores, k - 1)[:k]
    idx = idx[np.argsort(-scores[idx], kind="stable")]
    return [
        (int(i), float(scores[i]))
        for i in idx
        if np.isfinite(scores[i]) and (not positive_only or scores[i] > 0)
    ]
