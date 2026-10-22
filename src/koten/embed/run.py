"""引用候補の埋め込みを <HDD>/emb/<embed_model>/ に書く（SPEC §5・§8.4）。

shard-NNNNN.npy（float16）と shard-NNNNN.ids.parquet（candidate_id）の組で、まとめて書く。
再開時は書き終えた shard の candidate_id を飛ばす。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from koten.augment.extract import iter_candidates, llm_slug
from koten.embed.encoder import Encoder, passage_text
from koten.parquet import write_rows

IDS_SCHEMA = pa.schema([("candidate_id", pa.string())])


@dataclass
class EmbedResult:
    embedded: int
    skipped: int
    out_dir: Path


def emb_dir(hdd_root: Path, model: str) -> Path:
    return hdd_root / "emb" / llm_slug(model)


def embedded_ids(out_dir: Path) -> set[str]:
    ids: set[str] = set()
    for p in sorted(out_dir.glob("shard-*.ids.parquet")):
        if p.with_name(p.name.replace(".ids.parquet", ".npy")).exists():
            ids.update(pq.read_table(p).column(0).to_pylist())
    return ids


def load_embeddings(out_dir: Path) -> tuple[list[str], np.ndarray]:
    ids: list[str] = []
    mats: list[np.ndarray] = []
    for p in sorted(out_dir.glob("shard-*.ids.parquet")):
        npy = p.with_name(p.name.replace(".ids.parquet", ".npy"))
        if npy.exists():
            ids.extend(pq.read_table(p).column(0).to_pylist())
            mats.append(np.load(npy))
    dim = mats[0].shape[1] if mats else 0
    return ids, (np.vstack(mats) if mats else np.zeros((0, dim), dtype=np.float16))


def run_embed(
    hdd_root: Path,
    augment_dir: Path,
    encoder: Encoder,
    *,
    passage_prefix: str = "",
    shard_size: int = 5000,
    log: Callable[[str], None] = lambda _: None,
) -> EmbedResult:
    out_dir = emb_dir(hdd_root, encoder.name)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path = out_dir / "meta.json"
    source = str(augment_dir.relative_to(hdd_root))
    if meta_path.exists():
        meta = json.loads(meta_path.read_text("utf-8"))
        if meta.get("augment") != source:
            raise RuntimeError(
                f"{out_dir} は {meta.get('augment')} の候補で作られています（今回は {source}）。"
                "作り直すときはこのフォルダを消してから実行してください"
            )
    else:
        meta_path.write_text(
            json.dumps({"model": encoder.name, "dim": encoder.dim, "augment": source}), "utf-8"
        )

    done = embedded_ids(out_dir)
    seq = len(list(out_dir.glob("shard-*.npy")))
    pending: list[dict] = []
    embedded = 0

    def flush() -> None:
        nonlocal seq, pending, embedded
        if not pending:
            return
        vecs = encoder.encode([passage_prefix + passage_text(c) for c in pending])
        seq += 1
        # ID を先に書き、ベクトルは後に書く（npy がある shard だけを完了とみなす）
        write_rows(
            out_dir / f"shard-{seq:05d}.ids.parquet",
            [{"candidate_id": c["candidate_id"]} for c in pending],
            IDS_SCHEMA,
        )
        tmp = out_dir / f"shard-{seq:05d}.tmp.npy"
        np.save(tmp, vecs.astype(np.float16))
        tmp.replace(out_dir / f"shard-{seq:05d}.npy")
        embedded += len(pending)
        log(f"  {embedded} 件を埋め込みました")
        pending = []

    for c in iter_candidates(augment_dir):
        if c["candidate_id"] in done:
            continue
        pending.append(c)
        if len(pending) >= shard_size:
            flush()
    flush()
    return EmbedResult(embedded=embedded, skipped=len(done), out_dir=out_dir)
