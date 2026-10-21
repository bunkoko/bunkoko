"""Parquet の読み書き（行グループでまとめて書き、小さなファイルを大量に作らない）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


def write_rows(path: Path, rows: list[dict[str, Any]], schema: pa.Schema) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=schema)
    tmp = path.with_suffix(path.suffix + ".part")
    pq.write_table(table, tmp, row_group_size=10_000)
    tmp.replace(path)


def read_rows(path: Path) -> list[dict[str, Any]]:
    return pq.read_table(path).to_pylist()
