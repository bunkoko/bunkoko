"""SSD の索引と候補表だけを読む検索（SPEC §5：検索・APIの処理中はHDDを読まない）。

ハイブリッド検索（RRF）・リランク・選択は M4 で足す。ここでは密ベクトルと BM25 を別々に引く。

絞り込み（注意フラグ・再使用間隔・1冊回の作品指定など）は、候補表の列から作った真偽の配列で
スコアを消してから上位を取る。全件を計算するので、絞り込みで取りこぼすことはない。
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from koten.index.bm25 import BM25, top_k
from koten.index.build import cache_path, index_dir
from koten.index.tokenize import Tokenizer, get_tokenizer
from koten.parquet import read_rows


@dataclass
class Filter:
    """検索の絞り込み（SPEC §3 安全ルール・量産判定対策、§8.6）。"""

    exclude_caution: bool = True  # caution の一節は自動選択しない
    exclude_work_ids: Collection[str] = ()  # 直近の回で主役にした作品
    exclude_author_ids: Collection[str] = ()  # 直近の回で主役にした著者
    only_work_ids: Collection[str] | None = None  # 形式B（1冊を読む）
    themes_any: Collection[str] | None = None  # どれかのテーマを持つもの
    allow_review_required: bool = True  # NDL 由来など、人の確認が要るもの


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
        # 検索のたびに変換しないよう、float32 でメモリに持つ（50万件・1024次元で約2GB）
        self.dense = self.dense.astype(np.float32)
        rows = {r["candidate_id"]: r for r in read_rows(cache_path(ssd_root))}
        self.rows = [rows[cid] for cid in self.ids]
        self._tokenizer: Tokenizer | None = None
        self._caution = np.array([bool(r["caution"]) for r in self.rows], dtype=bool)
        self._review = np.array([bool(r["review_required"]) for r in self.rows], dtype=bool)
        self._by_work = self._group(lambda r: [r["work_id"]])
        self._by_author = self._group(lambda r: r.get("author_ids") or [])
        self._by_theme = self._group(lambda r: r["themes"] or [])

    def __len__(self) -> int:
        return len(self.ids)

    @property
    def tokenizer(self) -> Tokenizer:
        if self._tokenizer is None:
            self._tokenizer = get_tokenizer(self.meta["tokenizer"])
        return self._tokenizer

    def _group(self, keys) -> dict[str, np.ndarray]:
        groups: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(self.rows):
            for key in keys(r):
                groups[key].append(i)
        return {k: np.array(v, dtype=np.int64) for k, v in groups.items()}

    def _rows_of(self, groups: dict[str, np.ndarray], keys: Collection[str]) -> np.ndarray:
        parts = [groups[k] for k in keys if k in groups]
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int64)

    def mask(self, f: Filter | None) -> np.ndarray:
        """絞り込みを満たす行を True にした配列。"""
        m = np.ones(len(self.ids), dtype=bool)
        if f is None:
            return m
        if f.exclude_caution:
            m &= ~self._caution
        if not f.allow_review_required:
            m &= ~self._review
        if f.only_work_ids is not None:
            keep = np.zeros_like(m)
            keep[self._rows_of(self._by_work, f.only_work_ids)] = True
            m &= keep
        if f.themes_any is not None:
            keep = np.zeros_like(m)
            keep[self._rows_of(self._by_theme, f.themes_any)] = True
            m &= keep
        m[self._rows_of(self._by_work, f.exclude_work_ids)] = False
        m[self._rows_of(self._by_author, f.exclude_author_ids)] = False
        return m

    def _hits(self, ranked: list[tuple[int, float]]) -> list[Hit]:
        return [Hit(self.ids[i], s, self.rows[i]) for i, s in ranked]

    def search_dense(
        self, query_vec: np.ndarray, k: int = 50, flt: Filter | None = None
    ) -> list[Hit]:
        scores = self.dense @ np.asarray(query_vec, dtype=np.float32).ravel()
        scores[~self.mask(flt)] = -np.inf
        return self._hits(top_k(scores, k))

    def search_bm25(self, query: str, k: int = 50, flt: Filter | None = None) -> list[Hit]:
        scores = self.bm25.scores(self.tokenizer(query))
        scores[~self.mask(flt)] = 0.0
        return self._hits(top_k(scores, k, positive_only=True))
