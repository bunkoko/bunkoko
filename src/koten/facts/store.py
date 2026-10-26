"""背景の事実を SSD の SQLite に作り直す（koten facts build）。

入力は3つ。どれかがおかしければ1件も入れ替えない（統計の stats.csv と同じ扱い）。
1. HDD の書誌（clean/*/works.parquet・persons.parquet）→ persons・works_meta と、自動の事実
   （生没年・訳者・初出・原題・出てから○年。origin=auto）
2. config/facts.csv：人が出典で確かめた事実（origin=curated・status=verified。checked_by が必須）
3. config/fact_sources.csv：その出典（確かめた出典 role=confirm が、ウィキペディア以外に1つ以上）

台本づくり（企画・台本・ゲート）は SSD のこの表だけを読む（HDD は読まない）。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from koten.facts.dates import years_since
from koten.facts.model import Fact
from koten.license import is_usable
from koten.parquet import data_files, read_rows

FACT_COLUMNS = [
    "fact_id",
    "subject_type",
    "subject_id",
    "scope",
    "kind",
    "text_ja",
    "slots",
    "date",
    "sensitive",
    "interest",
    "checked_by",
    "checked_at",
    "links",
]
SOURCE_COLUMNS = [
    "fact_id",
    "seq",
    "kind",
    "role",
    "title",
    "url",
    "locator",
    "excerpt",
    "license",
    "credit",
]
# 確かめた出典（role=confirm）に使える種類。ウィキペディアは手がかり（lead）だけ（R2・D-013）
CONFIRM_KINDS = ("aozora_csv", "aozora_text", "ndl", "wikidata_ref", "book", "web", "museum")
LEAD_KINDS = ("wikipedia", "wikidata", "aozora_card")
AOZORA_CREDIT = "青空文庫 作家別作品一覧（CC BY 4.0）"
AOZORA_LICENSE = "CC BY 4.0"
AOZORA_SITE = "https://www.aozora.gr.jp/"
FIRST_PUB = re.compile(r"^「([^」]+)」\s*(\d{4})（[^）]*）年(\d{1,2})月")


class FactsError(ValueError):
    pass


@dataclass
class Source:
    fact_id: str
    seq: int
    kind: str
    role: str
    title: str = ""
    url: str = ""
    locator: str = ""
    excerpt: str = ""
    license: str = ""
    credit: str = ""

    def row(self) -> dict[str, Any]:
        return {c: getattr(self, c) for c in SOURCE_COLUMNS}


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _pid_num(person_id: str) -> str:
    return person_id.rsplit(":", 1)[-1]


# --- 自動の事実（青空文庫の書誌から） ---------------------------------------


def derive_auto(
    works: list[dict[str, Any]], persons: list[dict[str, Any]], today: date
) -> tuple[list[Fact], list[Source]]:
    facts: list[Fact] = []
    sources: list[Source] = []

    def add(fact: Fact, url: str) -> None:
        facts.append(fact)
        sources.append(
            Source(
                fact.fact_id,
                1,
                "aozora_csv",
                "confirm",
                title="青空文庫 作家別作品一覧（作品CSV）",
                url=url,
                license=AOZORA_LICENSE,
                credit=AOZORA_CREDIT,
            )
        )

    by_id = {p["person_id"]: p for p in persons}
    for p in persons:
        if p["person_id"] != p.get("canonical_id", p["person_id"]):
            continue  # 別名は代表の人物の事実にまとめる
        by, dy = _year(p.get("birth_date")), _year(p.get("death_date"))
        if not (by and dy and p.get("name")):
            continue
        num = _pid_num(p["person_id"])
        add(
            Fact(
                fact_id=f"A-p{num}-lifespan",
                subject_type="person",
                subject_id=p["person_id"],
                scope="person",
                kind="lifespan",
                text_ja=f"{p['name']}は{by}年に生まれ、{dy}年に亡くなりました。",
                slots={"person": p["name"], "year": by, "year2": dy},
                date=p["birth_date"] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p["birth_date"]) else "",
                origin="auto",
                status="auto",
                interest=1,
            ),
            f"{AOZORA_SITE}index_pages/person{int(num) if num.isdigit() else num}.html",
        )

    for w in works:
        if not is_usable(w.get("license") or ""):
            continue
        wid, title, url = w["work_id"], w["title"], w.get("url") or ""
        num = wid.rsplit(":", 1)[-1]
        translators = [
            by_id[t]["name"] for t in (w.get("translator_ids") or []) if t in by_id
        ] or list(w.get("translator_names") or [])
        if translators:
            slots: dict[str, Any] = {"title": title}
            for i, name in enumerate(translators):
                slots["person" if i == 0 else f"person{i + 1}"] = name
            add(
                Fact(
                    fact_id=f"A-w{num}-translation",
                    subject_type="work",
                    subject_id=wid,
                    scope="translation",
                    kind="translation",
                    text_ja=f"ここで読む『{title}』は、{'・'.join(translators)}による日本語訳です。",
                    slots=slots,
                    origin="auto",
                    status="auto",
                    interest=2,
                ),
                url,
            )
        orig = (w.get("original_title") or "").strip()
        if orig and orig != title and "『" not in orig and "」" not in orig:
            add(
                Fact(
                    fact_id=f"A-w{num}-original_title",
                    subject_type="work",
                    subject_id=wid,
                    scope="original_work" if translators else "work",
                    kind="original_title",
                    text_ja=f"『{title}』のもとの題は『{orig}』です。",
                    slots={"title": title, "title2": orig},
                    origin="auto",
                    status="auto",
                    interest=2,
                ),
                url,
            )
        m = FIRST_PUB.match((w.get("first_pub") or "").strip())
        if m and not translators:  # 訳書の初出は訳と原著のどちらか分からないので使わない
            periodical, year, month = m.group(1), m.group(2), str(int(m.group(3)))
            serial = "～" in w["first_pub"] or "〜" in w["first_pub"]
            text = (
                f"『{title}』は、{year}年{month}月から「{periodical}」に載りました。"
                if serial
                else f"『{title}』が最初に載ったのは、{year}年{month}月の「{periodical}」です。"
            )
            add(
                Fact(
                    fact_id=f"A-w{num}-first_pub",
                    subject_type="work",
                    subject_id=wid,
                    scope="work",
                    kind="first_pub",
                    text_ja=text,
                    slots={"title": title, "year": year, "month": month, "periodical": periodical},
                    date=f"{year}-{int(month):02d}",
                    origin="auto",
                    status="auto",
                    interest=2,
                ),
                url,
            )
            n = years_since(int(year), today)
            if n:
                add(
                    Fact(
                        fact_id=f"A-w{num}-years_since",
                        subject_type="work",
                        subject_id=wid,
                        scope="work",
                        kind="years_since",
                        text_ja=f"『{title}』が最初に世に出てから、{n}年あまりがたちます。",
                        slots={"title": title, "number": f"{n}年あまり"},
                        origin="auto",
                        status="auto",
                        interest=1,
                    ),
                    url,
                )
    return facts, sources


def _year(d: str | None) -> str | None:
    m = re.match(r"(\d{4})", d or "")
    return m.group(1) if m else None


# --- 人が確かめた事実（config の CSV） --------------------------------------


def read_curated(
    config_dir: Path,
    known_subjects: set[str],
    blocked: Callable[[str], tuple[str, str] | None] = lambda _: None,
) -> tuple[list[Fact], list[Source]]:
    """config/facts.csv と fact_sources.csv を読み、全部を確かめる。1つでも問題があれば止める。"""
    fpath, spath = config_dir / "facts.csv", config_dir / "fact_sources.csv"
    rows = _read_csv(fpath)
    srows = _read_csv(spath)
    problems: list[str] = []
    facts: list[Fact] = []
    seen: set[str] = set()
    for i, r in enumerate(rows, start=2):
        where = f"facts.csv {i}行目（{r.get('fact_id', '')}）"
        try:
            slots = json.loads(r.get("slots") or "{}")
            links = json.loads(r.get("links") or "[]")
        except json.JSONDecodeError as e:
            problems.append(f"{where}：slots・links が JSON ではない（{e}）")
            continue
        f = Fact(
            fact_id=(r.get("fact_id") or "").strip(),
            subject_type=(r.get("subject_type") or "").strip(),
            subject_id=(r.get("subject_id") or "").strip(),
            scope=(r.get("scope") or "").strip(),
            kind=(r.get("kind") or "").strip(),
            text_ja=(r.get("text_ja") or "").strip(),
            slots=slots,
            date=(r.get("date") or "").strip(),
            origin="curated",
            status="verified",
            sensitive=(r.get("sensitive") or "none").strip(),
            interest=int(r.get("interest") or 2),
            checked_by=(r.get("checked_by") or "").strip(),
            checked_at=(r.get("checked_at") or "").strip(),
            links=links,
        )
        if not f.fact_id.startswith("F-"):
            problems.append(f"{where}：人が確かめた事実の ID は F- で始める")
        if f.fact_id in seen:
            problems.append(f"{where}：fact_id が重複")
        seen.add(f.fact_id)
        if f.subject_type not in ("person", "work", "era"):
            problems.append(f"{where}：subject_type は person・work・era")
        elif f.subject_type != "era" and f.subject_id not in known_subjects:
            problems.append(f"{where}：subject_id {f.subject_id} が書誌に無い")
        if not f.slots:
            problems.append(f"{where}：項目（slots）が1つも無い（名前か年を項目にする）")
        problems += [f"{where}：{p}" for p in f.problems()]
        if hit := blocked(f.text_ja):
            problems.append(f"{where}：除外カテゴリ「{hit[0]}」の語（{hit[1]}）")
        facts.append(f)
    sources: list[Source] = []
    for i, r in enumerate(srows, start=2):
        where = f"fact_sources.csv {i}行目（{r.get('fact_id', '')}）"
        try:
            s = Source(
                fact_id=(r.get("fact_id") or "").strip(),
                seq=int(r.get("seq") or 0),
                kind=(r.get("kind") or "").strip(),
                role=(r.get("role") or "").strip(),
                title=(r.get("title") or "").strip(),
                url=(r.get("url") or "").strip(),
                locator=(r.get("locator") or "").strip(),
                excerpt=(r.get("excerpt") or "").strip(),
                license=(r.get("license") or "").strip(),
                credit=(r.get("credit") or "").strip(),
            )
        except ValueError:
            problems.append(f"{where}：seq が数字ではない")
            continue
        if s.fact_id not in seen:
            problems.append(f"{where}：facts.csv に無い事実")
        if s.role not in ("confirm", "lead"):
            problems.append(f"{where}：role は confirm か lead")
        if s.role == "confirm" and s.kind not in CONFIRM_KINDS:
            problems.append(
                f"{where}：確かめた出典に {s.kind} は使えない（{'・'.join(CONFIRM_KINDS)}）"
            )
        if s.role == "lead" and s.kind not in CONFIRM_KINDS + LEAD_KINDS:
            problems.append(f"{where}：kind {s.kind} は使えない")
        if not (s.url or s.locator):
            problems.append(f"{where}：url か locator（本の題とページなど）が要る")
        if not s.license:
            problems.append(f"{where}：license が空")
        if "CC BY" in s.license.upper() and not s.credit:
            problems.append(f"{where}：CC BY には出典表記（credit）が要る")
        sources.append(s)
    confirmed = {s.fact_id for s in sources if s.role == "confirm" and s.kind in CONFIRM_KINDS}
    for f in facts:
        if f.fact_id not in confirmed:
            problems.append(
                f"facts.csv（{f.fact_id}）：確かめた出典（role=confirm、ウィキペディア以外）が無い"
            )
    if problems:
        raise FactsError("背景の事実に問題があります：\n" + "\n".join(f"- {p}" for p in problems))
    return facts, sources


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --- 作り直す --------------------------------------------------------------


@dataclass
class BuildResult:
    persons: int
    works: int
    auto: int
    curated: int


def build_facts(
    conn: sqlite3.Connection,
    hdd_root: Path,
    config_dir: Path,
    *,
    today: date | None = None,
    blocked: Callable[[str], tuple[str, str] | None] = lambda _: None,
) -> BuildResult:
    today = today or date.today()
    works: list[dict[str, Any]] = []
    persons: list[dict[str, Any]] = []
    for p in data_files(hdd_root / "clean", "*/works.parquet"):
        works += read_rows(p)
    for p in data_files(hdd_root / "clean", "*/persons.parquet"):
        persons += read_rows(p)
    if not works:
        raise FactsError(f"書誌がありません（{hdd_root}/clean/*/works.parquet）。koten ingest が先")
    for p in persons:
        p["canonical_id"] = p.get("canonical_id") or p["person_id"]
    canon = {p["person_id"]: p["canonical_id"] for p in persons}
    auto, auto_src = derive_auto(works, persons, today)
    known = {p["person_id"] for p in persons} | {p["canonical_id"] for p in persons}
    known |= {w["work_id"] for w in works}
    curated, cur_src = read_curated(config_dir, known, blocked)
    for f in curated:
        if f.subject_type == "person":
            f.subject_id = canon.get(f.subject_id, f.subject_id)  # 別名で書いても代表の人物に付ける
    clash = {f.fact_id for f in auto} & {f.fact_id for f in curated}
    if clash:
        raise FactsError(f"自動の事実と ID が重なる：{sorted(clash)}")
    with conn:
        for table in ("persons", "works_meta", "facts", "fact_sources"):
            conn.execute(f"DELETE FROM {table}")
        conn.executemany(
            "INSERT OR REPLACE INTO persons(person_id, canonical_id, name, name_yomi, birth_date, "
            "death_date) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    p["person_id"],
                    p["canonical_id"],
                    p.get("name") or "",
                    p.get("name_yomi"),
                    p.get("birth_date"),
                    p.get("death_date"),
                )
                for p in persons
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO works_meta(work_id, title, title_yomi, original_title, "
            "first_pub, base_book, parent_book, url, author_ids, translator_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    w["work_id"],
                    w["title"],
                    w.get("title_yomi"),
                    w.get("original_title"),
                    w.get("first_pub"),
                    w.get("base_book"),
                    w.get("parent_book"),
                    w.get("url"),
                    json.dumps(
                        [canon.get(a, a) for a in w.get("author_ids") or []], ensure_ascii=False
                    ),
                    json.dumps(list(w.get("translator_ids") or []), ensure_ascii=False),
                )
                for w in works
            ],
        )
        cols = list(Fact("x", "work", "", "work", "k", "t。").to_row()) + ["text_sha"]
        conn.executemany(
            f"INSERT INTO facts({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [[*f.to_row().values(), text_sha(f.text_ja)] for f in auto + curated],
        )
        conn.executemany(
            f"INSERT INTO fact_sources({', '.join(SOURCE_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in SOURCE_COLUMNS)})",
            [list(s.row().values()) for s in auto_src + cur_src],
        )
    return BuildResult(len(persons), len(works), len(auto), len(curated))


# --- 読む（企画・台本から。SSD だけ） ---------------------------------------


def load_facts(conn: sqlite3.Connection, fact_ids: list[str]) -> dict[str, Fact]:
    if not fact_ids:
        return {}
    marks = ",".join("?" for _ in fact_ids)
    return {
        r["fact_id"]: Fact.from_row(dict(r))
        for r in conn.execute(f"SELECT * FROM facts WHERE fact_id IN ({marks})", fact_ids)
    }


def load_sources(conn: sqlite3.Connection, fact_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not fact_ids:
        return {}
    marks = ",".join("?" for _ in fact_ids)
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
        f"SELECT * FROM fact_sources WHERE fact_id IN ({marks}) ORDER BY fact_id, seq", fact_ids
    ):
        out.setdefault(r["fact_id"], []).append(dict(r))
    return out


def fact_shas(conn: sqlite3.Connection, fact_ids: list[str]) -> dict[str, str]:
    if not fact_ids:
        return {}
    marks = ",".join("?" for _ in fact_ids)
    return {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT fact_id, text_sha FROM facts WHERE fact_id IN ({marks})", fact_ids
        )
    }
