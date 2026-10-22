"""SSD の索引と候補表だけを読む検索（SPEC §5：検索・APIの処理中はHDDを読まない）。

ハイブリッド検索（RRF）・リランク・選択は M4 で足す。ここでは密ベクトルと BM25 を別々に引く。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from koten.index.bm25 import BM25, top_k
from koten.index.build import cache_path, index_dir
from koten.index.tokenize import Tokenizer, get_tokenizer
from koten.parquet import read_rows


@dataclass
class Hit:
    candidate_id: str
    score: float
    row: dict


class HotIndex:
    def __init__(self, ssd_root: Path, model: str):
        d = index_dir(ssd_root, model)
        if not (d / "meta.json").exists():
            raise FileNotFoundError(
                f"{d} に索引がありません。先に `koten index build` を実行してください"
            )
        self.meta = json.loads((d / "meta.json").read_text("utf-8"))
        self.ids: list[str] = json.loads((d / "ids.json").read_text("utf-8"))
        self.dense = np.load(d / "dense.npy")
        self.bm25 = BM25.load(d)
        rows = {r["candidate_id"]: r for r in read_rows(cache_path(ssd_root))}
        self.rows = [rows[cid] for cid in self.ids]
        self._tokenizer: Tokenizer | None = None

    def __len__(self) -> int:
        return len(self.ids)

    @property
    def tokenizer(self) -> Tokenizer:
        if self._tokenizer is None:
            self._tokenizer = get_tokenizer(self.meta["tokenizer"])
        return self._tokenizer

    def _hits(self, ranked: list[tuple[int, float]]) -> list[Hit]:
        return [Hit(self.ids[i], s, self.rows[i]) for i, s in ranked]

    def search_dense(self, query_vec: np.ndarray, k: int = 50) -> list[Hit]:
        q = np.asarray(query_vec, dtype=np.float32).ravel()
        scores = np.empty(len(self.ids), dtype=np.float32)
        step = 65536
        for start in range(0, len(self.ids), step):
            block = self.dense[start : start + step].astype(np.float32)
            scores[start : start + len(block)] = block @ q
        return self._hits(top_k(scores, k))

    def search_bm25(self, query: str, k: int = 50) -> list[Hit]:
        return self._hits(top_k(self.bm25.scores(self.tokenizer(query)), k, positive_only=True))
