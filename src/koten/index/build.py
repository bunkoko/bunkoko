"""索引づくり（SPEC §5・§8.4）。HDD の候補・埋め込みから、検索時に読むものを SSD に書く。

<SSD_ROOT>/koten-hot/
  cache/candidates.parquet   検索時に使う引用候補表
                             （使用可のライセンスの作品だけ。作品名・著者・権利つき）
  index/<embed_model>/       dense.npy（float16・長さ1）、ids.json、
                             bm25.npz、bm25_vocab.json、meta.json

密ベクトルは100万件以下なので内積の全探索（FAISS の IndexFlatIP と同じ結果）を numpy で行う。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa

from koten.augment.extract import CANDIDATES_SCHEMA, iter_candidates, llm_slug
from koten.embed.encoder import passage_text
from koten.embed.run import emb_dir, load_embeddings
from koten.index.bm25 import BM25
from koten.index.tokenize import get_tokenizer
from koten.license import is_usable
from koten.parquet import read_rows, write_rows

WORK_FIELDS = [
    ("title", pa.string()),
    ("author_ids", pa.list_(pa.string())),
    ("author_names", pa.list_(pa.string())),
    ("license", pa.string()),
    ("review_required", pa.bool_()),
    ("kana_type", pa.string()),
    ("lang", pa.string()),
]
CACHE_SCHEMA = pa.schema(list(CANDIDATES_SCHEMA) + [pa.field(n, t) for n, t in WORK_FIELDS])


def cache_path(ssd_root: Path) -> Path:
    return ssd_root / "cache" / "candidates.parquet"


def index_dir(ssd_root: Path, model: str) -> Path:
    return ssd_root / "index" / llm_slug(model)


def _works(hdd_root: Path) -> dict[str, dict]:
    works: dict[str, dict] = {}
    for p in sorted((hdd_root / "clean").glob("*/works.parquet")):
        works.update({w["work_id"]: w for w in read_rows(p)})
    return works


def build_index(
    hdd_root: Path,
    ssd_root: Path,
    augment_dir: Path,
    model: str,
    *,
    tokenizer: str = "sudachi",
    log: Callable[[str], None] = lambda _: None,
) -> dict:
    works = _works(hdd_root)
    ids, vecs = load_embeddings(emb_dir(hdd_root, model))
    row_of = {cid: i for i, cid in enumerate(ids)}

    cache: list[dict] = []
    dropped_license = missing_emb = 0
    for c in iter_candidates(augment_dir):
        w = works.get(c["work_id"])
        if w is None or not is_usable(w["license"]):
            dropped_license += 1  # R1：使用可でない作品の候補は検索に出さない
            continue
        if c["candidate_id"] not in row_of:
            missing_emb += 1
            continue
        cache.append({**c, **{n: w.get(n) for n, _ in WORK_FIELDS}})
    log(f"候補 {len(cache)} 件（権利で除外 {dropped_license}、埋め込み未作成 {missing_emb}）")

    order = [row_of[c["candidate_id"]] for c in cache]
    dense = vecs[order] if order else np.zeros((0, vecs.shape[1] if vecs.ndim == 2 else 0))
    tok = get_tokenizer(tokenizer)
    bm25 = BM25.build([tok(passage_text(c)) for c in cache])

    write_rows(cache_path(ssd_root), cache, CACHE_SCHEMA)
    out = index_dir(ssd_root, model)
    out.mkdir(parents=True, exist_ok=True)
    np.save(out / "dense.npy", dense.astype(np.float16))
    (out / "ids.json").write_text(json.dumps([c["candidate_id"] for c in cache]), "utf-8")
    bm25.save(out)
    meta = {
        "model": model,
        "dim": int(dense.shape[1]) if dense.ndim == 2 else 0,
        "count": len(cache),
        "tokenizer": tokenizer,
        "augment": str(augment_dir.relative_to(hdd_root)),
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    log(f"索引を書きました：{out}")
    return meta
