"""見出しの埋め込み（文章を数値のベクトルにする）と、日ごとの特徴（docs/news.md 7 章）。

① 話題の強さ: 先に決めた代表の文（TOPICS）への近さで、その日の見出しのうち話題に当てはまるものの割合
② 珍しさ: その日の原油（金）の見出しの平均の向きが、直近 30 日の平均からどれだけ離れたか

埋め込みは Mac の中だけで計算する（sentence-transformers。M5 なら GPU の MPS を使う。クラウドの費用はかからない）。
テストとクラウドでは、単語の出現だけを使う hash（AI を使わない比較用）で代わりに動かす。
"""

from __future__ import annotations

import re
import zlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import numpy as np
import pandas as pd

from .news import NewsStore, sample_like_backfill

FEATURE_SOURCE = "gkg"   # 特徴に使う見出し: GDELT の生データ（2019-10 から今まで同じ取り方。docs/news.md 6 章）


@dataclass(frozen=True)
class ModelSpec:
    key: str
    name: str                    # Hugging Face のモデル名（hash は空）
    prefix: str = ""             # 文の前に付ける決まりの文字（e5 は "query: "）
    prompt_name: str | None = None
    dim: int | None = None       # 次元を減らして保存する（対応しているモデルだけ）
    note: str = ""


MODELS: dict[str, ModelSpec] = {m.key: m for m in (
    ModelSpec("qwen3-0.6b", "Qwen/Qwen3-Embedding-0.6B", dim=256,
              note="既定。登録不要・約 1.2GB。日本語も扱える（2025-06 公開）"),
    ModelSpec("gemma-300m", "google/embeddinggemma-300m", prompt_name="STS", dim=256,
              note="軽い。Hugging Face で利用規約に同意してログインが要る（2025-09 公開）"),
    ModelSpec("e5-large", "intfloat/multilingual-e5-large", prefix="query: ",
              note="約 2GB。公開が古い（2023 年）ので、2024 年以降の検証で先読みの心配が小さい"),
    ModelSpec("qwen3-4b", "Qwen/Qwen3-Embedding-4B", dim=256, note="約 8GB。遅いが精度が高い"),
    ModelSpec("hash", "", dim=512, note="AI を使わない比較用（単語の出現だけ）"),
)}
DEFAULT_MODEL = "qwen3-0.6b"

# 代表の文（結果を見る前に決めたもの。変えない。docs/news.md 7 章）
TOPICS: dict[str, tuple[str, ...]] = {
    "geo": (
        "Missile strikes escalate the war in the Middle East",
        "Drone attack hits oil facilities",
        "New sanctions imposed on Russian oil exports",
        "Military conflict threatens shipping in the Strait of Hormuz",
        "Iran and Israel exchange attacks as tensions rise",
        "Russia launches a major offensive in Ukraine",
        "中東で軍事衝突が激化し、原油の供給不安が高まる",
        "ロシアへの追加制裁で原油の輸出が制限される",
    ),
    "oil": (
        "OPEC+ agrees to cut oil production",
        "US crude oil inventories fall more than expected",
        "Oil prices rise on supply concerns",
        "Global oil demand outlook weakens",
        "Saudi Arabia raises crude output",
        "OPECプラスが原油の減産で合意",
    ),
    "gold": (
        "Gold prices hit a record high as investors seek safe havens",
        "Central banks increase gold purchases",
        "Gold falls as the dollar strengthens",
        "Silver prices surge on industrial demand",
        "金価格が最高値を更新、安全資産への需要が高まる",
    ),
    "macro": (
        "The Federal Reserve raises interest rates to fight inflation",
        "Fed signals interest rate cuts",
        "US inflation comes in hotter than expected",
        "Recession fears grow as economic data weakens",
        "米連邦準備制度理事会が利上げを決定",
    ),
}
TOPIC_Q = 0.95        # 話題に当てはまる: 直近 60 日の見出しの近さの上位 5%
GROUP_Q = 0.80        # 珍しさに使う原油・金の見出し: 上位 20%
WINDOW = 60           # しきい値と急増の基準にする日数
MIN_DAYS = 20         # これより短い履歴では計算しない
NOVELTY_REF = 30      # 珍しさの比べる相手: 直近 30 日の平均の向き
MIN_HEADLINES = 10    # 1 日にこれより少なければ珍しさを計算しない
MIN_REF_DAYS = 10     # 比べる相手の日数がこれより少なければ計算しない


class Embedder(Protocol):
    key: str

    def encode(self, texts: list[str]) -> np.ndarray: ...


def _normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=1, keepdims=True)
    return (v / np.where(n > 0, n, 1.0)).astype(np.float32)


_STOP = set("a an the of to in on for and or as at by with from is are was were be has have had it its this that "
            "after over amid into up down out new says said will may could would vs".split())


def _tokens(text: str) -> list[str]:
    t = text.lower()
    words = [w for w in re.findall(r"[a-z0-9]+", t) if w not in _STOP]
    cjk = re.findall(r"[぀-ヿ一-鿿]+", t)
    grams = [c[i:i + 2] for c in cjk for i in range(max(1, len(c) - 1))]
    return words + [f"{a}_{b}" for a, b in zip(words, words[1:])] + grams


class HashEmbedder:
    """単語（と単語 2 つの並び、日本語は 2 文字ずつ）の出現を、決まった次元に振り分けて数えるだけ。AI は使わない。"""

    def __init__(self, dim: int = 512):
        self.key, self.dim = "hash", dim

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), np.float32)
        for i, t in enumerate(texts):
            for tok in _tokens(t):
                h = zlib.crc32(tok.encode("utf-8"))
                out[i, h % self.dim] += 1.0 if (h >> 16) & 1 else -1.0
        return _normalize(out)


class SentenceEmbedder:
    """sentence-transformers のモデル（初回はダウンロードする。以降は Mac の中だけで動く）。"""

    def __init__(self, spec: ModelSpec, batch_size: int = 64):
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise SystemExit("埋め込みの部品が無い。先に ./cfd news setup を実行する") from None
        device = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
        kw = {"truncate_dim": spec.dim} if spec.dim else {}
        self.model = SentenceTransformer(spec.name, device=device, **kw)
        prompts = getattr(self.model, "prompts", None) or {}
        self.prompt_name = spec.prompt_name if spec.prompt_name in prompts else None
        self.key, self.spec, self.device, self.batch_size = spec.key, spec, device, batch_size

    def encode(self, texts: list[str]) -> np.ndarray:
        v = self.model.encode([self.spec.prefix + t for t in texts], batch_size=self.batch_size,
                              prompt_name=self.prompt_name, convert_to_numpy=True, normalize_embeddings=True,
                              show_progress_bar=False)
        return _normalize(np.asarray(v, dtype=np.float32))   # 次元を減らした後にも長さを 1 にそろえる


def make_embedder(key: str) -> Embedder:
    if key not in MODELS:
        raise SystemExit(f"モデル {key} は無い。選べるもの: {', '.join(MODELS)}")
    spec = MODELS[key]
    return HashEmbedder(spec.dim or 512) if key == "hash" else SentenceEmbedder(spec)


# --------------------------------------------------------------------------- 埋め込みの保存
class EmbeddingCache:
    """UTC の日ごとに、見出しの ID とベクトル（float16）を data/news/emb/<モデル>/ に保存する。"""

    def __init__(self, store: NewsStore, model_key: str):
        self.store, self.dir = store, store.root / "emb" / model_key

    def path(self, day: pd.Timestamp) -> Path:
        return self.dir / f"{day:%Y-%m}" / f"{day:%Y-%m-%d}.npz"

    def load(self, day: pd.Timestamp, raw: bool = False) -> tuple[np.ndarray, np.ndarray]:
        p = self.path(day)
        if not p.exists():
            return np.array([], dtype=str), np.zeros((0, 0), np.float32)
        z = np.load(p, allow_pickle=False)
        return z["ids"].astype(str), (z["vecs"] if raw else z["vecs"].astype(np.float32))

    def update_day(self, day: pd.Timestamp, embedder: Embedder) -> int:
        """その日の見出しのうち、まだ埋め込んでいないものを足す。足した数を返す。"""
        df = self.store.load_day(day)
        ids, vecs = self.load(day)
        have = set(ids)
        new = df[~df["id"].isin(have)].drop_duplicates("id")
        if new.empty:
            return 0
        v = embedder.encode(new["title"].tolist())
        ids2 = np.concatenate([ids, new["id"].to_numpy(str)])
        vecs2 = v if not len(ids) else np.vstack([vecs, v])
        p = self.path(day)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.stem + ".tmp.npz")
        np.savez_compressed(tmp, ids=ids2, vecs=vecs2.astype(np.float16))
        tmp.replace(p)
        return len(new)


def embed_days(store: NewsStore, embedder: Embedder, until: pd.Timestamp,
               log: Callable[[str], None] = print) -> int:
    """until より前の（終わった）UTC の日を、まだの分だけ埋め込む。"""
    cache = EmbeddingCache(store, embedder.key)
    total = 0
    days = [d for d in store.days() if d < until]
    for i, d in enumerate(days):
        total += cache.update_day(d, embedder)
        if (i + 1) % 30 == 0 and total:
            log(f"  埋め込み: {d:%Y-%m-%d} まで（+{total}）")
    return total


# --------------------------------------------------------------------------- 日ごとの特徴
def anchor_vectors(embedder: Embedder) -> dict[str, np.ndarray]:
    return {k: embedder.encode(list(v)) for k, v in TOPICS.items()}


def topic_scores(vecs: np.ndarray, anchors: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """見出しごとに、各話題の代表の文との近さ（コサイン）の最大。"""
    if not len(vecs):
        return {k: np.zeros(0, np.float32) for k in anchors}
    return {k: (vecs @ a.T).max(axis=1) for k, a in anchors.items()}


def daily_features(days: list[tuple[pd.Timestamp, np.ndarray]], anchors: dict[str, np.ndarray],
                   groups: tuple[str, ...] = ("oil", "gold")) -> pd.DataFrame:
    """日ごとの特徴。days は (UTC の日, その日の見出しのベクトル) を古い順に。

    share_<話題>: その日の見出しのうち、直近 60 日の見出しの近さの上位 5% を超えたものの割合
    novelty_<原油・金>: その日の原油（金）の見出しの平均の向きと、直近 30 日の平均の向きの離れ方（1 − コサイン）
    しきい値と比べる相手は、その日より前の日だけで決める（先読みしない）。
    """
    hist: deque = deque()          # (日, {話題: 近さ}) 直近 WINDOW 日分
    cents: deque = deque()         # (日, {原油・金: 平均の向き})
    rows = []
    for day, vecs in days:
        vecs = np.asarray(vecs, dtype=np.float32)
        sc = topic_scores(vecs, anchors)
        while hist and (day - hist[0][0]).days > WINDOW:
            hist.popleft()
        while cents and (day - cents[0][0]).days > NOVELTY_REF:
            cents.popleft()
        row: dict = {"day": day, "n": len(vecs)}
        ready = len(hist) >= MIN_DAYS and len(vecs) > 0
        today_c = {}
        for k in anchors:
            if ready:
                pool = np.concatenate([h[1][k] for h in hist])
                row[f"share_{k}"] = float(np.mean(sc[k] > np.quantile(pool, TOPIC_Q)))
            if k in groups and ready:
                sel = sc[k] > np.quantile(np.concatenate([h[1][k] for h in hist]), GROUP_Q)
                if sel.sum() >= MIN_HEADLINES:
                    c = vecs[sel].mean(axis=0)
                    today_c[k] = c / max(np.linalg.norm(c), 1e-12)
                    past = [cc[1][k] for cc in cents if k in cc[1]]
                    if len(past) >= MIN_REF_DAYS:
                        ref = np.mean(past, axis=0)
                        ref = ref / max(np.linalg.norm(ref), 1e-12)
                        row[f"novelty_{k}"] = float(1.0 - today_c[k] @ ref)
        rows.append(row)
        if len(vecs):
            hist.append((day, sc))
        if today_c:
            cents.append((day, today_c))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).set_index("day")
    full = pd.date_range(df.index.min(), df.index.max(), freq="D", tz="UTC")
    return df.reindex(full)


def load_day_vectors(store: NewsStore, model_key: str, until: pd.Timestamp,
                     source: str = FEATURE_SOURCE) -> list[tuple[pd.Timestamp, np.ndarray]]:
    """特徴に使う見出しのベクトルを日ごとに（float16 のまま。メモリを節約する）。

    source="gdelt"（記事の一覧）は、さかのぼった期間と同じ取り方にそろえてから使う。
    """
    cache = EmbeddingCache(store, model_key)
    out = []
    for d in store.days(source):
        if d >= until:
            continue
        df = store.load_day(d, source)
        if source == "gdelt":
            df = sample_like_backfill(df)
        ids, vecs = cache.load(d, raw=True)
        if not len(ids):
            continue
        pos = {i: j for j, i in enumerate(ids)}
        idx = [pos[i] for i in df["id"] if i in pos]
        out.append((d, vecs[idx] if idx else np.zeros((0, vecs.shape[1]), np.float16)))
    return out
