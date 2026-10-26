"""運用データの SQLite（SPEC §5・§7）。<SSD_ROOT>/db/koten.sqlite に置き、HDD には置かない。

悩み（worries）・統計（stats）・回（episodes）・使用履歴（usage）・検索文の控え（query_cache）。
背景の事実（facts・fact_sources・fact_usage）と、その元になる人物・作品の書誌（persons・works_meta）。
"""

from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

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

CREATE TABLE IF NOT EXISTS episodes (
    episode_id TEXT PRIMARY KEY,         -- E20261101-01
    format TEXT NOT NULL,                -- A / B / C / D
    theme TEXT,                          -- JSON 配列
    worry_ids TEXT NOT NULL,             -- JSON 配列
    state TEXT NOT NULL,                 -- yet / planned / script_done / ... / error / discarded
    state_msg TEXT,
    plan_json TEXT,
    script_json TEXT,
    verification_json TEXT,
    review_required INTEGER NOT NULL DEFAULT 0,
    reviewer_note TEXT,
    human_take TEXT,
    duration_sec REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS usage (
    episode_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    author_id TEXT,
    role TEXT NOT NULL                   -- main / sub
);
CREATE INDEX IF NOT EXISTS usage_episode ON usage(episode_id);

CREATE TABLE IF NOT EXISTS query_cache (
    worry_key TEXT NOT NULL,             -- worry_id か「theme:<テーマ>」
    source_text TEXT NOT NULL,
    llm TEXT NOT NULL,
    prompt_ver TEXT NOT NULL,
    queries TEXT NOT NULL,               -- JSON
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (worry_key, source_text, llm, prompt_ver)
);

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

-- 背景の事実（koten facts build が HDD の書誌と config の CSV から作り直す）
CREATE TABLE IF NOT EXISTS persons (
    person_id TEXT PRIMARY KEY,          -- aozora:person:000280
    canonical_id TEXT NOT NULL,          -- 同じ人の別名をまとめた代表の人物ID
    name TEXT NOT NULL,
    name_yomi TEXT,
    birth_date TEXT,
    death_date TEXT
);

CREATE TABLE IF NOT EXISTS works_meta (
    work_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    title_yomi TEXT,
    original_title TEXT,
    first_pub TEXT,                      -- 初出（作品CSVのまま）
    base_book TEXT,
    parent_book TEXT,
    url TEXT,                            -- 図書カード
    author_ids TEXT,                     -- JSON 配列（代表の人物ID）
    translator_ids TEXT                  -- JSON 配列
);

CREATE TABLE IF NOT EXISTS facts (
    fact_id TEXT PRIMARY KEY,            -- F-…（人が確かめた）／A-…（青空文庫のデータから自動）
    subject_type TEXT NOT NULL,          -- person / work / era
    subject_id TEXT,
    scope TEXT NOT NULL,                 -- person / work / original_work / translation / era
    kind TEXT NOT NULL,
    text_ja TEXT NOT NULL,               -- 確認済みの1文
    slots TEXT NOT NULL,                 -- JSON（台本で記号にする名前・年・数）
    date TEXT,
    origin TEXT NOT NULL,                -- auto / curated
    status TEXT NOT NULL,                -- auto / verified（使えるのはこの2つ）
    sensitive TEXT NOT NULL DEFAULT 'none',
    interest INTEGER NOT NULL DEFAULT 2,
    checked_by TEXT,
    checked_at TEXT,
    links TEXT,                          -- JSON 配列（ほかに結び付ける作品・人物の ID）
    text_sha TEXT NOT NULL               -- 文の指紋（企画のあとで文が変わったら気づくため）
);
CREATE INDEX IF NOT EXISTS facts_subject ON facts(subject_id);

CREATE TABLE IF NOT EXISTS fact_sources (
    fact_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,                  -- aozora_csv・ndl・wikidata_ref・book・wikipedia など
    role TEXT NOT NULL,                  -- confirm（確かめた出典）/ lead（見つけた手がかり）
    title TEXT,
    url TEXT,
    locator TEXT,                        -- 章・ページ・記録の番号など
    excerpt TEXT,                        -- どこに何とあったか（100字以内のメモ）
    license TEXT NOT NULL,
    credit TEXT,                         -- 説明欄に出す出典表記
    PRIMARY KEY (fact_id, seq)
);

CREATE TABLE IF NOT EXISTS fact_usage (
    episode_id TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    fact_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fact_usage_episode ON fact_usage(episode_id);
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
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
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
        if (r.get("value") or "").strip() and not re.search(r"\d", r["value"]):
            # 「報告書から転記」のような書きかけを、数字として語らせない
            problems.append(f"{i}行目：value に数字が無い（{r['value'].strip()}）")
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
