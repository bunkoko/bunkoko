"""埋め込みモデル（SPEC §8.4）。

本番は sentence-transformers（uv sync --extra ml で入る）。
e5 は "passage: "・"query: " の接頭辞を付ける。
HashEncoder はモデル無しで動く簡易版で、テストと配線の確認に使う（検索の質は低い）。
"""

from __future__ import annotations

import hashlib
from typing import Protocol

import numpy as np


class Encoder(Protocol):
    name: str
    dim: int

    def encode(self, texts: list[str]) -> np.ndarray:
        """長さ1に正規化した float32 の行列（件数 × dim）を返す。"""
        ...


class SentenceTransformerEncoder:
    def __init__(self, model: str, *, device: str = "auto", batch_size: int = 32):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "sentence-transformers が入っていません。`uv sync --extra ml` を実行してください"
            ) from e
        if device == "auto":
            import torch

            device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.name = model
        self.batch_size = batch_size
        self._model = SentenceTransformer(model, device=device)
        # 版によってメソッド名が違うので、取れなければ1件埋め込んで次元を測る
        getter = getattr(self._model, "get_sentence_embedding_dimension", None) or getattr(
            self._model, "get_embedding_dimension", None
        )
        dim = getter() if getter else None
        self.dim = int(dim) if dim else int(self.encode(["次元の確認"]).shape[1])

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = self._model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.asarray(vecs, dtype=np.float32)


class HashEncoder:
    """文字の2-gramをハッシュして並べるだけの埋め込み。モデル不要で決定的。"""

    def __init__(self, dim: int = 256, name: str = "hash-bigram"):
        self.name = name
        self.dim = dim

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            grams = [t[j : j + 2] for j in range(max(len(t) - 1, 1))]
            for g in grams:
                h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "little")
                out[i, h % self.dim] += 1.0
            norm = np.linalg.norm(out[i])
            if norm > 0:
                out[i] /= norm
        return out


def make_encoder(model: str, *, device: str = "auto", batch_size: int = 32) -> Encoder:
    if model.startswith("hash"):
        return HashEncoder(name=model)
    return SentenceTransformerEncoder(model, device=device, batch_size=batch_size)


def passage_text(candidate: dict) -> str:
    """埋め込む文：現代語の要旨＋刺さりそうな悩み＋テーマ（原文そのものは埋め込まない）。"""
    return (
        f"{candidate['summary_ja']}\n"
        f"悩み：{'、'.join(candidate['worries'])}\n"
        f"テーマ：{'、'.join(candidate['themes'])}"
    )
