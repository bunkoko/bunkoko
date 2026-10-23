"""運用データの SQLite（SPEC §5・§7）。<SSD_ROOT>/db/koten.sqlite に置き、HDD には置かない。

M3 では悩み（worries）と統計（stats）を持つ。回・台本・使用履歴は M4 以降で足す。
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS worries (
    worry_id TEXT PRIMARY KEY,           -- W0001
    text_raw TEXT NOT NULL,              -- 取り込んだままの文（正規化の入力）
    text_general TEXT,                   -- 一般化した悩み文（40字以内）
    situation TEXT,
    emotion TEXT,
    themes TEXT,                         -- JSON 配列（統制語彙）
    audience TEXT,                       -- 学生・新社会人・中堅・管理職・全般
    source_type TEXT NOT NULL,           -- search / comment / qa / manual / seed
    source_ref TEXT,
    demand_score REAL NOT NULL DEFAULT 0,
    cluster_id TEXT,                     -- 代表の worry_id
    is_representative INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',  -- new / active / blocked / error
    blocked_reason TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_used_at TEXT,
    llm TEXT,
    prompt_ver TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS worries_text_raw ON worries(text_raw);

CREATE TABLE IF NOT EXISTS stats (
    stat_id TEXT PRIMARY KEY,
    theme TEXT NOT NULL,
    label TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT,
    survey TEXT,
    year TEXT,
    url TEXT NOT NULL
);
"""


def db_path(ssd_root: Path) -> Path:
    return ssd_root / "db" / "koten.sqlite"


def connect(ssd_root: Path) -> sqlite3.Connection:
    path = db_path(ssd_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
    )
    conn.commit()
    return conn


class StatsError(ValueError):
    pass


STATS_COLUMNS = ["stat_id", "theme", "label", "value", "unit", "survey", "year", "url"]


def load_stats(conn: sqlite3.Connection, csv_path: Path) -> int:
    """config/stats.csv を stats 表に読み込む（全件を入れ替える）。

    数値と出典URLは必ず残す（SPEC §9）。欠けた行があれば1件も入れずに止める。
    """
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    problems = []
    seen = set()
    for i, r in enumerate(rows, start=2):
        for col in ("stat_id", "theme", "label", "value", "url"):
            if not (r.get(col) or "").strip():
                problems.append(f"{i}行目：{col} が空")
        if not (r.get("url") or "").startswith(("http://", "https://")):
            problems.append(f"{i}行目：url が URL ではない")
        if r.get("stat_id") in seen:
            problems.append(f"{i}行目：stat_id {r['stat_id']} が重複")
        seen.add(r.get("stat_id"))
    if problems:
        raise StatsError(f"{csv_path} に問題があります：" + "、".join(problems))
    with conn:
        conn.execute("DELETE FROM stats")
        conn.executemany(
            f"INSERT INTO stats({', '.join(STATS_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in STATS_COLUMNS)})",
            [[(r.get(c) or "").strip() or None for c in STATS_COLUMNS] for r in rows],
        )
    return len(rows)
