"""運用データの SQLite（SPEC §5・§7）。<SSD_ROOT>/db/koten.sqlite に置き、HDD には置かない。

悩み（worries）・統計（stats）・回（episodes）・使用履歴（usage）・検索文の控え（query_cache）。
背景の事実（facts・fact_sources・fact_usage）と、その元になる人物・作品の書誌（persons・works_meta）。
資料の台帳（ref_works）と、時代の解釈（renderings）・昔の辞書の語釈（dict_entries）。
今の本（S4）での照合の判定（checks。本の文は持たない）。自前の訳（shiyaku。M8b）。
"""

from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 9

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
    -- yet / planned / script_done / voiced / rendered / reviewed / published / error / discarded
    state TEXT NOT NULL,
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

-- 回で使った素材（M6）：自分の写真は同じものを直近5回で使わない（SPEC §8.10）
CREATE TABLE IF NOT EXISTS media_usage (
    episode_id TEXT NOT NULL,
    kind TEXT NOT NULL,                  -- photo
    path TEXT NOT NULL,                  -- photos_dir からの相対パス
    PRIMARY KEY (episode_id, kind, path)
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
    url TEXT NOT NULL,
    trend TEXT,                          -- 増加・減少（同じ調査・同じ設問で人が確かめた）
    trend_note TEXT,                     -- 移り変わりの根拠（年の範囲と、同じ設問であること）
    status TEXT,                         -- 空は使う。保留は企画に出さない（規約を確かめるまで）
    checked_by TEXT,                     -- オーナー（人が元の資料で確かめた）か AI
    checked_at TEXT                      -- 確かめた日
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

-- 資料の台帳と、その本文（koten ref build が config の CSV から作り直す。M8a）
CREATE TABLE IF NOT EXISTS ref_works (
    ref_id TEXT PRIMARY KEY,             -- R-…
    source_class TEXT NOT NULL,          -- S1 原文 / S2 時代の解釈
    kind TEXT NOT NULL,                  -- original・dictionary・kundoku・translation・commentary
    title TEXT NOT NULL,
    volume TEXT,
    edition TEXT,
    pub_year INTEGER NOT NULL,           -- 使う版（取り込んだ刷り）の刊行年
    first_pub_year INTEGER,
    publisher TEXT,
    credited_as TEXT,                    -- 表紙・奥付の名義
    lang TEXT NOT NULL,
    source TEXT NOT NULL,                -- ndl・standardebooks・gutenberg・aozora・archive・owned
    pid TEXT,
    url TEXT,
    ndl_access TEXT,
    ndl_checked_at TEXT,
    tradition TEXT,                      -- 注の流れ（古注・新注・徂徠 など）
    based_on TEXT,                       -- JSON 配列（元にした資料の ref_id）
    credit TEXT NOT NULL,                -- 画面・説明欄の出典表記
    license TEXT NOT NULL,               -- 人の確認のあとの判定
    license_reason TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    people TEXT NOT NULL                 -- JSON 配列（name・role・death_year）
);

CREATE TABLE IF NOT EXISTS renderings (
    rendering_id TEXT PRIMARY KEY,       -- I-…
    passage_key TEXT NOT NULL,           -- lunyu:1.1 など（config/canon.yaml）
    passage_to TEXT,                     -- 範囲で結び付けるときの終わり
    ref_id TEXT NOT NULL,
    role TEXT NOT NULL,                  -- original / kundoku / translation / commentary
    lang TEXT NOT NULL,
    text TEXT NOT NULL,                  -- 資料のままの本文
    display_text TEXT,                   -- 表記を改めて画面に出す本文
    reading TEXT,                        -- 読み上げの読み（人が確かめた）
    locator TEXT NOT NULL,               -- コマ・ページ・章など
    status TEXT NOT NULL,                -- verified / transcribed / imported（使えるのは verified）
    checked_by TEXT,
    checked_at TEXT,
    note TEXT,
    text_sha TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS renderings_passage ON renderings(passage_key);

CREATE TABLE IF NOT EXISTS dict_entries (
    entry_id TEXT PRIMARY KEY,           -- J-…
    ref_id TEXT NOT NULL,
    headword TEXT NOT NULL,              -- 今の表記の見出し語（普通）
    headword_kana TEXT NOT NULL,         -- 辞書の見出しのかな
    sense TEXT,                          -- 語義番号
    text TEXT NOT NULL,                  -- 資料のままの語釈
    display_text TEXT,
    reading TEXT,
    locator TEXT NOT NULL,
    status TEXT NOT NULL,                -- verified / transcribed（使えるのは verified）
    checked_by TEXT,
    checked_at TEXT,
    note TEXT,
    text_sha TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dict_entries_headword ON dict_entries(headword);

-- 今の本（S4）での照合の判定（koten check run。SPEC R8・D-017）。本の文の列は作らない
CREATE TABLE IF NOT EXISTS checks (
    check_id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id TEXT NOT NULL,
    claim_key TEXT NOT NULL,             -- ch2-s3#1（段落の ID と、段落の中の文の順番）
    claim_sha TEXT NOT NULL,             -- 照合した台本の文（差し込んだあと）の指紋
    claim_text TEXT NOT NULL,            -- 照合した台本の文（自分たちの文）
    book_id TEXT NOT NULL,               -- config/ref_books.csv
    book_label TEXT NOT NULL,
    locator TEXT NOT NULL,               -- ページなど
    verdict TEXT NOT NULL,               -- same / differs / not_covered / resolved
    close_wording INTEGER NOT NULL DEFAULT 0,  -- 言い回しが近い（台本の文が本の言い回しに近い）
    run_len INTEGER,                     -- 共通する最長の並びの字数
    share REAL,                          -- 台本の文の4字の並びのうち、本にあるものの割合
    method TEXT NOT NULL,                -- ollama:モデル名 / human
    checked_by TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    note TEXT,                           -- 自分の言葉で60字まで（本の文は書かない）
    shiyaku_id TEXT                      -- 自前の訳の照合のとき（episode_id は空。M8b）
);
CREATE INDEX IF NOT EXISTS checks_episode ON checks(episode_id);

-- 照合に使う今の本（S4）の書名（koten ref build が config/ref_books.csv から写す。語りで出さない）
CREATE TABLE IF NOT EXISTS s4_titles (
    title TEXT PRIMARY KEY
);

-- 自前の訳（M8b。koten shiyaku）。AI の下書き → コードの確かめ → 判定役（下書きと別の LLM。
-- D-023）→ 人の承認。台本で使えるのは approved だけ。今の本（S4）の文は入れない
CREATE TABLE IF NOT EXISTS shiyaku (
    shiyaku_id TEXT PRIMARY KEY,         -- S-lunyu-1.1-1
    passage_key TEXT NOT NULL,           -- lunyu:1.1 など（config/canon.yaml）
    kind TEXT NOT NULL,                  -- shiyaku（外国語の原文から。「試訳」と出す）
                                         -- relay（保護期間の切れた外国語の訳から。出典だけ。D-024）
                                         -- modern（日本の古文を今の言葉に。表示なし。D-023）
    text TEXT NOT NULL,                  -- 自前の日本語の訳
    source_id TEXT NOT NULL,             -- 訳した元の本文（renderings の I-…）
    source_sha TEXT NOT NULL,            -- そのときの元の本文の指紋（変わったら使わない）
    basis_ids TEXT NOT NULL,             -- JSON 配列。下書きと判定に見せた時代の解釈（I-…）
    label TEXT NOT NULL,                 -- 画面と説明欄の表示（試訳 / ロングの英訳（1862）/ 空）
    status TEXT NOT NULL,                -- draft / flagged / judged / approved / rejected
    issues TEXT NOT NULL,                -- JSON 配列。コードの確かめと判定役の問題
    judge_json TEXT,                     -- 判定役の1文ずつの答え
    divided INTEGER NOT NULL DEFAULT 0,  -- 読み方が分かれる（人が承認のときに確かめた）
    drafter TEXT NOT NULL,
    judge TEXT,
    created_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    note TEXT,
    text_sha TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS shiyaku_passage ON shiyaku(passage_key);

-- 国会図書館のマニフェストで見た公開範囲（koten ref ndl-snapshot。HDD が無くても ref build が見る）
CREATE TABLE IF NOT EXISTS ndl_checks (
    ref_id TEXT PRIMARY KEY,
    pid TEXT NOT NULL,
    access TEXT NOT NULL,                -- internet_pd / unknown
    access_value TEXT,                   -- マニフェストの値（PDM など）
    access_checked_at TEXT,              -- マニフェストを取った日時
    recorded_at TEXT NOT NULL
);

-- 統計の系列（複数年の数の並び。koten db init が config の CSV から作り直す）
CREATE TABLE IF NOT EXISTS stat_series (
    series_id TEXT PRIMARY KEY,
    themes TEXT NOT NULL,                -- JSON の配列
    label TEXT NOT NULL,
    unit TEXT,
    survey TEXT,
    publisher TEXT,
    source_kind TEXT,
    period_kind TEXT,
    selector TEXT,                       -- JSON
    table_title TEXT,
    url TEXT,
    latest_known TEXT,
    breaks TEXT,                         -- JSON
    trend TEXT,
    trend_from TEXT,
    trend_to TEXT,
    trend_note TEXT,
    status TEXT,
    checked_by TEXT,
    checked_at TEXT,
    checked_sha TEXT,
    "where" TEXT,
    note TEXT,
    check_sha TEXT,                      -- 今の数・名前・区切りの指紋
    owner_ok INTEGER,                    -- 人の確認が今の指紋と合っている
    latest_period TEXT,                  -- 数のある最新の年（無ければ NULL）
    stale INTEGER,                       -- 元の資料の最新年より古い
    trend_ok INTEGER                     -- trend が数で言える（trend なしは 0）
);

CREATE TABLE IF NOT EXISTS stat_points (
    series_id TEXT NOT NULL,
    period TEXT NOT NULL,
    value TEXT,                          -- 元の文字のまま
    mark TEXT,
    part TEXT,
    src_label TEXT,
    src_unit TEXT,
    fetched_at TEXT,
    src_sha TEXT,
    value_num REAL,                      -- 数として読めるときだけ（特殊文字は NULL）
    segment INTEGER,                     -- 調べ方の区切りの番号
    period_label TEXT,
    PRIMARY KEY (series_id, period)
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
    _add_missing_columns(
        conn,
        "stats",
        {
            "trend": "TEXT",
            "trend_note": "TEXT",
            "status": "TEXT",
            "checked_by": "TEXT",
            "checked_at": "TEXT",
        },
    )
    _add_missing_columns(conn, "checks", {"shiyaku_id": "TEXT"})
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
    )
    conn.commit()
    return conn


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """前の版で作った表に、あとから足した列を足す（CREATE TABLE IF NOT EXISTS は列を足さない）。"""
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, kind in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


class StatsError(ValueError):
    pass


STATS_COLUMNS = [
    "stat_id",
    "theme",
    "label",
    "value",
    "unit",
    "survey",
    "year",
    "url",
    "trend",
    "trend_note",
    "status",
    "checked_by",
    "checked_at",
]
TRENDS = ("増加", "減少")
# 人が元の資料で確かめた行の checked_by。それ以外（AI が確かめた行・書いた人の記録が無い行）は、
# 回で使ったら承認の前に人が確かめる（koten sources todo）
HUMAN_CHECKER = "オーナー"
AI_CHECKER = "AI（調べ役と見直し役）"


def human_checked(row: dict[str, Any] | sqlite3.Row) -> bool:
    """統計の行を、人が元の資料で確かめたか。"""
    value = row["checked_by"] if "checked_by" in row.keys() else ""
    return (value or "").strip() == HUMAN_CHECKER


STAT_STATUSES = ("", "保留")  # 空は使う。保留は読み込むが企画に出さない


def load_stats(conn: sqlite3.Connection, csv_path: Path) -> int:
    """config/stats.csv を stats 表に読み込む（全件を入れ替える）。

    数値と出典URLは必ず残す（SPEC §9）。欠けた行があれば1件も入れずに止める。
    """
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    problems = stats_problems(rows)
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


def stats_problems(rows: list[dict[str, str]]) -> list[str]:
    """stats.csv の行の問題（何行目の何か）。空なら読み込める。"""
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
        trend = (r.get("trend") or "").strip()
        if trend and trend not in TRENDS:
            problems.append(f"{i}行目：trend は {'・'.join(TRENDS)} か空にする（{trend}）")
        if trend and not (r.get("trend_note") or "").strip():
            # 「増え続けています」と語らせる根拠。年の範囲と、同じ調査・同じ設問であることを人が書く
            problems.append(f"{i}行目：trend を書いたら trend_note（根拠）も書く")
        if (r.get("status") or "").strip() not in STAT_STATUSES:
            problems.append(f"{i}行目：status は空（使う）か 保留 にする")
        if (r.get("checked_by") or "").strip() and not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", (r.get("checked_at") or "").strip()
        ):
            problems.append(f"{i}行目：checked_by を書いたら checked_at（2026-11-21 の形）も書く")
        if r.get("stat_id") in seen:
            problems.append(f"{i}行目：stat_id {r['stat_id']} が重複")
        seen.add(r.get("stat_id"))
    return problems


def load_series(conn: sqlite3.Connection, config_dir: Path, meta_dir: Path) -> tuple[int, int]:
    """config/stat_series.csv・stat_points.csv を stat_series・stat_points に読み込む。

    問題が1つでもあれば StatsError で止めて、何も書かない（全部か無しか）。
    成功したら両表を1回の取引で入れ替え、(系列の数, 点の数) を返す。
    """
    import json

    import yaml

    from koten.stats import series as ss
    from koten.worries.master import load_blocked_keywords

    config_dir = Path(config_dir)
    series_rows = ss.read_series_rows(config_dir)
    point_rows = ss.read_point_rows(config_dir)
    themes_path = config_dir / "themes.yaml"
    themes = (
        set(yaml.safe_load(themes_path.read_text("utf-8")).get("themes") or [])
        if themes_path.exists()
        else set()
    )
    stats_csv = config_dir / "stats.csv"
    legacy_ids: set[str] = set()
    if stats_csv.exists():
        with stats_csv.open(encoding="utf-8-sig", newline="") as f:
            legacy_ids = {(r.get("stat_id") or "").strip() for r in csv.DictReader(f)}
    metas: dict[str, dict] = {}
    try:
        for r in series_rows:
            if r.get("source_kind", "").strip() != "estat_api":
                continue
            try:
                sel = json.loads(r.get("selector", ""))
            except ValueError:
                continue
            sdid = sel.get("statsDataId") if isinstance(sel, dict) else None
            if isinstance(sdid, str) and sdid.isascii() and sdid.isdigit() and sdid not in metas:
                meta = ss.load_meta(meta_dir, sdid)
                if meta is not None:
                    metas[sdid] = meta
    except ss.SeriesError as e:
        raise StatsError(str(e)) from e
    problems = ss.series_problems(
        series_rows,
        point_rows,
        themes=themes,
        legacy_ids=legacy_ids,
        blocked=load_blocked_keywords(config_dir),
        metas=metas,
    )
    if problems:
        raise StatsError(
            f"{config_dir / ss.SERIES_FILE}・{config_dir / ss.POINTS_FILE} に問題があります："
            + "、".join(problems)
        )

    all_points = ss.parse_points(point_rows)
    by_series: dict[str, list[ss.Point]] = {}
    for p in all_points:
        by_series.setdefault(p.series_id, []).append(p)
    series_out: list[list[Any]] = []
    points_out: list[list[Any]] = []
    for s in ss.parse_series(series_rows):
        pts = by_series.get(s.series_id, [])
        meta = None
        if s.source_kind == "estat_api":
            meta = metas.get(s.selector.get("statsDataId", ""))
        sha = ss.check_sha(s, pts, meta)
        numeric = [p.period for p in pts if ss.value_num(p) is not None]
        trend_ok = ss.verify_trend(s, pts)[0] if s.trend else False
        raw = {
            **{c: getattr(s, c) for c in ss.SERIES_COLUMNS},
            "themes": json.dumps(list(s.themes), ensure_ascii=False),
            "selector": json.dumps(s.selector, ensure_ascii=False),
            "breaks": json.dumps(
                [{"before": b.before, "note": b.note} for b in s.breaks], ensure_ascii=False
            ),
        }
        series_out.append(
            [raw[c] for c in ss.SERIES_COLUMNS]
            + [
                sha,
                int(ss.owner_ok(s, sha)),
                max(numeric) if numeric else None,
                int(ss.is_stale(s, pts)),
                int(trend_ok),
            ]
        )
        seg = ss.segments(s, pts)
        for p in pts:
            num = ss.value_num(p)
            points_out.append(
                [getattr(p, c) for c in ss.POINTS_COLUMNS]
                + [
                    float(num) if num is not None else None,
                    seg[p.period],
                    ss.period_label(p.period, s.period_kind),
                ]
            )
    s_cols = [f'"{c}"' for c in ss.SERIES_COLUMNS] + [
        "check_sha", "owner_ok", "latest_period", "stale", "trend_ok",
    ]  # fmt: skip
    p_cols = [*ss.POINTS_COLUMNS, "value_num", "segment", "period_label"]
    with conn:
        conn.execute("DELETE FROM stat_series")
        conn.execute("DELETE FROM stat_points")
        conn.executemany(
            f"INSERT INTO stat_series({', '.join(s_cols)}) "
            f"VALUES ({', '.join('?' for _ in s_cols)})",
            series_out,
        )
        conn.executemany(
            f"INSERT INTO stat_points({', '.join(p_cols)}) "
            f"VALUES ({', '.join('?' for _ in p_cols)})",
            points_out,
        )
    return len(series_out), len(points_out)
