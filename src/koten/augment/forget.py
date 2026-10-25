"""作品の付加情報を忘れる（正規化を直して本文が変わった作品を、作り直すため）。

augment/<llm>/<prompt版>/ の候補（part）と処理済みの記録（done）、emb/*/ の埋め込みから、
指定した作品の行を取り除く。そのあと `koten augment` と `koten embed` が、その作品だけを
作り直す。候補の ID は作り直しても同じになりうるので、その作品の一節を使っている回が
残っていれば止める（回の引用が別の一節にすり替わらないように）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from koten.augment.extract import CANDIDATES_SCHEMA, DONE_SCHEMA
from koten.embed.run import IDS_SCHEMA
from koten.parquet import data_files, read_rows, write_rows


@dataclass
class ForgetResult:
    candidates: int = 0
    done_chunks: int = 0
    embeddings: int = 0
    files: int = 0
    episodes: list[str] = field(default_factory=list)  # その作品の一節を使っている回


def _work_of(item_id: str) -> str:
    """chunk_id（作品ID#連番）や candidate_id（…/q1）から作品IDを取り出す。"""
    return item_id.split("#", 1)[0]


def episodes_using(conn: sqlite3.Connection, work_ids: set[str]) -> list[str]:
    if not work_ids:
        return []
    marks = ",".join("?" for _ in work_ids)
    rows = conn.execute(
        f"SELECT DISTINCT u.episode_id FROM usage u JOIN episodes e ON e.episode_id = u.episode_id "
        f"WHERE e.state != 'discarded' AND u.work_id IN ({marks}) ORDER BY u.episode_id",
        sorted(work_ids),
    ).fetchall()
    return [r[0] for r in rows]


def forget_works(
    hdd_root: Path,
    augment_dir: Path,
    work_ids: set[str],
    *,
    apply: bool = False,
) -> ForgetResult:
    """apply=False なら数えるだけ。apply=True で取り除いたファイルに書き換える。"""
    result = ForgetResult()
    if not work_ids:
        return result
    for p in data_files(augment_dir, "part-*.parquet"):
        rows = read_rows(p)
        keep = [r for r in rows if r["work_id"] not in work_ids]
        if len(keep) != len(rows):
            result.candidates += len(rows) - len(keep)
            result.files += 1
            if apply:
                write_rows(p, keep, CANDIDATES_SCHEMA)
    for p in data_files(augment_dir, "done-*.parquet"):
        rows = read_rows(p)
        keep = [r for r in rows if _work_of(r["chunk_id"]) not in work_ids]
        if len(keep) != len(rows):
            result.done_chunks += len(rows) - len(keep)
            result.files += 1
            if apply:
                write_rows(p, keep, DONE_SCHEMA)
    for ids_path in data_files(hdd_root / "emb", "*/shard-*.ids.parquet"):
        npy = ids_path.with_name(ids_path.name.replace(".ids.parquet", ".npy"))
        if not npy.exists():
            continue
        ids = pq.read_table(ids_path).column(0).to_pylist()
        mask = [_work_of(c) not in work_ids for c in ids]
        if all(mask):
            continue
        result.embeddings += mask.count(False)
        result.files += 1
        if apply:
            vecs = np.load(npy)[np.array(mask, dtype=bool)]
            tmp = npy.with_name(npy.name.replace(".npy", ".tmp.npy"))
            np.save(tmp, vecs)
            write_rows(
                ids_path,
                [{"candidate_id": c} for c, k in zip(ids, mask, strict=True) if k],
                IDS_SCHEMA,
            )
            tmp.replace(npy)
    return result


def read_changed(path: Path) -> set[str]:
    return set(json.loads(path.read_text("utf-8"))["work_ids"]) if path.exists() else set()
