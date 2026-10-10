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
    prompt_names: tuple[str, ...] = ()   # 使う指示（モデルにあるもののうち最初のもの。無ければ付けない）
    dim: int | None = None       # 次元を減らして保存する（対応しているモデルだけ）
    released: str = ""           # 公開の年月（判定の期間を決める）
    note: str = ""


MODELS: dict[str, ModelSpec] = {m.key: m for m in (
    ModelSpec("qwen3-0.6b", "Qwen/Qwen3-Embedding-0.6B", dim=256, released="2025-06",
              note="標準。登録不要・約 1.2GB。日本語も扱える"),
    ModelSpec("gemma2", "google/embeddinggemma-2", prompt_names=("STS", "Classification", "Clustering", "Document"),
              dim=256, released="2026-10", note="EmbeddingGemma 2（Gemma 4 がもと）。Apache 2.0"),
    ModelSpec("gemma-300m", "google/embeddinggemma-300m", prompt_names=("STS",), dim=256, released="2025-09",
              note="EmbeddingGemma（初代）。Hugging Face で利用規約に同意してログインが要る"),
    ModelSpec("e5-large", "intfloat/multilingual-e5-large", prefix="query: ", released="2023-06",
              note="約 2GB。公開が古いので先読みの心配が一番小さい"),
    ModelSpec("qwen3-4b", "Qwen/Qwen3-Embedding-4B", dim=256, released="2025-06",
              note="約 8GB。遅いが精度が高い（比べるときは --models に足す）"),
    ModelSpec("hash", "", dim=512, note="AI を使わない比較用（単語の出現だけ）"),
)}
DEFAULT_MODEL = "qwen3-0.6b"
# ./cfd news compare で比べる（＋hash）。EmbeddingGemma 初代はログインが要るので、比べるなら --models に足す
COMPARE_MODELS = ("qwen3-0.6b", "gemma2", "e5-large")

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


def pick_prompt(available: dict, preferred: tuple[str, ...]) -> str | None:
    return next((p for p in preferred if p in available), None)


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
        self.prompts = dict(getattr(self.model, "prompts", None) or {})
        self.prompt_name = pick_prompt(self.prompts, spec.prompt_names)
        self.key, self.spec, self.device, self.batch_size = spec.key, spec, device, batch_size

    def encode(self, texts: list[str]) -> np.ndarray:
        v = self.model.encode([self.spec.prefix + t for t in texts], batch_size=self.batch_size,
                              prompt_name=self.prompt_name, convert_to_numpy=True, normalize_embeddings=True,
                              show_progress_bar=False)
        if self.device == "mps":
            import torch

            torch.mps.empty_cache()   # 使い終えた GPU のメモリを返す（返さないと、日を重ねるうちに数十 GB までたまる）
        return _normalize(np.asarray(v, dtype=np.float32))   # 次元を減らした後にも長さを 1 にそろえる

    def release(self) -> None:
        """メモリを空ける（次のモデルを読む前に）。"""
        import gc

        import torch

        self.model = None
        gc.collect()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()


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
    from .news import STOP, Progress

    cache = EmbeddingCache(store, embedder.key)
    total = 0
    days = [d for d in store.days() if d < until]
    prog = Progress(f"埋め込み（{embedder.key}）", len(days), log)
    for i, d in enumerate(days):
        STOP.check()
        total += cache.update_day(d, embedder)
        if total:
            prog.tick(i + 1, f"{d:%Y-%m-%d} まで、+{total:,} 件")
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


# --------------------------------------------------------------------------- モデルの比べ方（価格は使わない）
# 答え: GDELT が本文全体から付けたテーマ。見出しを、その記事のテーマの話題に一番近いと判定できるかで比べる
THEME_KEYS: dict[str, frozenset[str]] = {
    "geo": frozenset({"ARMEDCONFLICT", "MILITARY", "SANCTIONS", "WB_2462_POLITICAL_VIOLENCE_AND_WAR"}),
    "oil": frozenset({"ECON_OILPRICE", "ENV_OIL"}),
    "gold": frozenset({"ECON_GOLDPRICE", "WB_2936_GOLD", "WB_2937_SILVER"}),
    "macro": frozenset({"ECON_INFLATION", "ECON_INTEREST_RATES", "EPU_POLICY_CENTRAL_BANK",
                        "EPU_POLICY_FEDERAL_RESERVE", "EPU_CATS_MONETARY_POLICY", "WB_1235_CENTRAL_BANKS"}),
}
COMPARE_MARGIN = 0.02      # 標準のモデルから替えるのは、点数がこれ以上良いときだけ
JUDGE_FROM = pd.Timestamp("2025-07-01", tz="UTC")


def theme_labels(themes: pd.Series) -> pd.DataFrame:
    """記事ごとに、各話題のテーマが付いているか。"""
    sets = themes.fillna("").astype(str).map(lambda s: set(s.split(";")))
    return pd.DataFrame({k: sets.map(lambda x, v=v: bool(x & v)).to_numpy() for k, v in THEME_KEYS.items()})


def topic_aucs(scores: dict[str, np.ndarray], labels: pd.DataFrame) -> dict[str, float]:
    """話題ごとの AUC（テーマの付いた見出しほど、その話題の代表の文に近いと判定できているか。0.5 = でたらめ、1 = 完全）。"""
    from .meta import auc

    return {k: auc(np.asarray(scores[k], dtype=float), labels[k].to_numpy()) for k in labels if k in scores}


def choose_model(scores: dict[str, float], default: str = DEFAULT_MODEL, margin: float = COMPARE_MARGIN) -> str:
    """標準のモデルより margin 以上良いモデルがあれば、その中で一番良いもの。無ければ標準（docs/news.md 7 章）。"""
    ok = {k: v for k, v in scores.items() if k != "hash" and v is not None and np.isfinite(v)}
    if not ok:
        return default
    best = max(ok, key=ok.get)
    if default not in ok:
        return best
    return best if ok[best] >= ok[default] + margin else default


def judge_from(model_key: str) -> pd.Timestamp:
    """判定の開始: 2025-07-01 か、モデルの公開の翌月 1 日の遅い方（モデルが知っている期間では判定しない）。"""
    rel = MODELS[model_key].released if model_key in MODELS else ""
    if not rel:
        return JUDGE_FROM
    return max(JUDGE_FROM, pd.Timestamp(f"{rel}-01", tz="UTC") + pd.offsets.MonthBegin(1))
