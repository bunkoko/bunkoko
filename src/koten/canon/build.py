"""古典の一節の候補を作る（koten canon build）と、訳の足りなさを数える（koten canon coverage）。"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa

from koten.augment.extract import CANDIDATES_SCHEMA, AugmentConfig
from koten.ingest.aozora import PERSONS_SCHEMA, WORKS_SCHEMA
from koten.llm import ChatLLM, LLMOutputError, fill, load_prompt, parse_json_object
from koten.parquet import read_rows, write_rows
from koten.ref.store import renderings_for_passage
from koten.ref.texts import USABLE_STATUS, passage_problem, passage_sort_key
from koten.shiyaku.material import gather
from koten.shiyaku.store import ShiyakuError, reading_for, single

CANON_SCHEMA = pa.schema(
    list(CANDIDATES_SCHEMA)
    + [
        ("passage_key", pa.string()),
        ("quote_lang", pa.string()),
        ("review_required", pa.bool_()),  # NDL の資料の本文（人が画像と照らす）
        ("source_id", pa.string()),  # 一節にした本文（renderings の I-…）
        ("material_sha", pa.string()),  # 一節と読みの指紋（変われば作り直す）
    ]
)
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_ja": {"type": "string"},
        "themes": {"type": "array", "items": {"type": "string"}},
        "worries": {"type": "array", "items": {"type": "string"}},
        "quotability": {"type": "number"},
        "caution": {"type": "boolean"},
        "caution_reason": {"type": "string"},
    },
    "required": ["summary_ja", "themes", "worries", "quotability", "caution", "caution_reason"],
    "additionalProperties": False,
}
CANON_LICENSE_REASON = "古典の原典（保護期間なし）。一節の本文は、台帳で使えると判定した資料のもの"


def canon_dir(hdd_root: Path) -> Path:
    return hdd_root / "canon"


def candidates_path(hdd_root: Path) -> Path:
    return canon_dir(hdd_root) / "candidates.parquet"


def works_path(hdd_root: Path) -> Path:
    return hdd_root / "clean" / "canon" / "works.parquet"


def persons_path(hdd_root: Path) -> Path:
    return hdd_root / "clean" / "canon" / "persons.parquet"


AOZORA_PERSON = re.compile(r"^aozora:person:\d+$")


def author_id(w: dict[str, Any]) -> str:
    """作者の人物 ID。青空文庫の人物がいれば、その ID（canon.yaml の author_id）を使い、同じ人を
    1人として数える（直近の回の作者・背景の事実）。いなければ canon:person:<名前>。"""
    given = str(w.get("author_id") or "")
    if AOZORA_PERSON.match(given):
        return given
    author = w.get("author") or ""
    return f"canon:person:{author}" if author else ""


def iter_canon(hdd_root: Path) -> Iterator[dict[str, Any]]:
    path = candidates_path(hdd_root)
    if path.exists():
        yield from read_rows(path)


def heading(canon: dict[str, dict[str, Any]], passage_key: str, *, numbered: bool = True) -> str:
    """出典に出す章の見出し（canon.yaml の heading。無ければ番号のまま）。

    numbered が False（その版の番号で、標準と合うか確かめていない）なら、巻の見出しだけにする。
    """
    work, _, num = passage_key.partition(":")
    w = canon.get(work) or {}
    if not numbered and not w.get("heading_p"):
        return ""
    by_para = ".p" in f".{num}" or not numbered
    tpl = w.get("heading_p") if by_para and w.get("heading_p") else w.get("heading")
    parts = re.findall(r"\d+[ab]?", num)
    if not tpl:
        return num
    try:
        return tpl.format(*parts)
    except (IndexError, KeyError):
        return num


def passage_keys(conn: sqlite3.Connection) -> list[str]:
    """SSD の資料の本文がある一節（台帳で使える資料のものだけ）。"""
    rows = conn.execute(
        "SELECT DISTINCT t.passage_key FROM renderings t JOIN ref_works w ON w.ref_id = t.ref_id "
        "WHERE w.license IN ('PD_JP', 'CC0', 'CC_BY')"
    ).fetchall()

    def key(k: str) -> tuple:
        work, _, num = k.partition(":")
        return (work, passage_sort_key(num))

    return sorted((r[0] for r in rows), key=key)


@dataclass
class Plan:
    """1つの一節の候補の材料。"""

    passage_key: str
    source: dict[str, Any]  # 一節にする本文
    quote_lang: str
    readings: list[str]  # LLM に見せる読み（日本語を先に）
    ready: str  # 台本で日本語を出せる理由（pd・approved・draftable）
    review_required: bool
    numbered: bool = True  # 節の番号を見出しに出せるか（確かめていない番号なら巻だけ）

    def sha(self) -> str:
        body = "\n".join([self.source["text_sha"], *self.readings])
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def plan_passage(
    conn: sqlite3.Connection,
    canon: dict[str, dict[str, Any]],
    passage_key: str,
    *,
    min_versions: int = 2,
) -> Plan | str:
    """候補にできるなら Plan、できなければ理由の文。"""
    if problem := passage_problem(passage_key, canon):
        return problem
    lang = canon[passage_key.partition(":")[0]].get("lang") or "ja"
    rows = renderings_for_passage(conn, passage_key, verified_only=False)
    reading = reading_for(conn, passage_key)
    material = None
    if reading is None:
        try:
            material = gather(conn, canon, passage_key, min_versions=min_versions)
        except ShiyakuError as e:
            return f"台本で日本語を出せない（{e}）"
    # 画面の一節は、読み（日本語）の元と同じ本文にする（表示の出典と画面の本文が食い違わない）：
    # これから訳を作る一節は訳の元、自前の訳はその訳の元、古い日本語訳は人が確かめた原文か、
    # 無ければその訳そのもの。ほかの外国語の訳・取り込んだだけの本文は、訳の元でなければ出さない
    by_id = {r["rendering_id"]: r for r in rows}
    originals = [
        r
        for r in rows
        if r["role"] == "original"
        and r["lang"] == lang
        and r["status"] == USABLE_STATUS
        and single(r, passage_key)
    ]
    if reading is None:
        source = material.source
    elif reading.kind == "pd":
        source = originals[0] if originals else by_id.get(reading.ref)
    else:
        source = by_id.get(reading.source_id)
    if source is None:
        return "一節にする本文が無い"
    texts = [reading.text] if reading is not None and reading.ref != source["rendering_id"] else []
    texts += [
        r.get("display_text") or r["text"]
        for r in sorted(rows, key=lambda r: r["lang"] != "ja")
        if r["rendering_id"] != source["rendering_id"]
        and r["role"] in ("kundoku", "translation", "commentary")
        and r["status"] in (USABLE_STATUS, "imported")
    ]
    ready = "draftable" if reading is None else ("pd" if reading.kind == "pd" else "approved")
    review = source.get("ref_source") == "ndl"
    # 電子版から機械で分けた Gutenberg の節の番号は、その版のもの（標準と合うか未確認）
    numbered = not (source.get("ref_source") == "gutenberg" and source["status"] != USABLE_STATUS)
    return Plan(passage_key, source, source["lang"], texts[:4], ready, review, numbered)


@dataclass
class CanonResult:
    candidates: int = 0
    made: int = 0
    kept: int = 0
    failed: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


def _check(data: dict[str, Any], vocab: set[str], cfg: AugmentConfig) -> dict[str, Any]:
    summary = str(data.get("summary_ja") or "").strip()
    if not summary or len(summary) > cfg.summary_max:
        raise ValueError(f"summary_ja が空か{cfg.summary_max}字を超える")
    themes = [t for t in data.get("themes") or [] if t in vocab][:3]
    if not themes:
        raise ValueError("themes に統制語彙が無い")
    worries = [str(w).strip()[: cfg.worry_max] for w in data.get("worries") or [] if str(w).strip()]
    if not worries:
        raise ValueError("worries が空")
    return {
        "summary_ja": summary,
        "themes": themes,
        "worries": worries[:3],
        "quotability": float(min(max(float(data.get("quotability") or 0), 0.0), 1.0)),
        "caution": bool(data.get("caution")),
        "caution_reason": str(data.get("caution_reason") or "") if data.get("caution") else "",
    }


def build_canon(
    conn: sqlite3.Connection,
    hdd_root: Path,
    canon: dict[str, dict[str, Any]],
    llm: ChatLLM,
    prompts_dir: Path,
    vocab: list[str],
    cfg: AugmentConfig,
    *,
    prompt: str = "canon_augment_v1",
    min_versions: int = 2,
    log: Callable[[str], None] = lambda _: None,
) -> CanonResult:
    """候補を作り直す（一節と読みが変わらない候補は、前の付加情報を使う）。"""
    old = {r["candidate_id"]: r for r in iter_canon(hdd_root)}
    system, user_tpl = load_prompt(prompts_dir, prompt)
    vocab_set = set(vocab)
    res = CanonResult()
    rows: list[dict[str, Any]] = []
    works: dict[str, dict[str, Any]] = {}
    for key in passage_keys(conn):
        p = plan_passage(conn, canon, key, min_versions=min_versions)
        if isinstance(p, str):
            res.skipped[key] = p
            continue
        work, _, _num = key.partition(":")
        w = canon[work]
        cid = f"canon:{key}"
        sha = p.sha()
        prev = old.get(cid)
        if prev is not None and prev.get("material_sha") == sha:
            info = {
                k: prev[k]
                for k in (
                    "summary_ja",
                    "themes",
                    "worries",
                    "quotability",
                    "caution",
                    "caution_reason",
                    "llm",
                    "prompt_ver",
                )
            }
            res.kept += 1
        else:
            user = fill(
                user_tpl,
                title=w["title"],
                author=w.get("author") or "作者不詳",
                heading=f"「{heading(canon, key, numbered=p.numbered)}」",
                text=p.source.get("display_text") or p.source["text"],
                readings="\n".join(f"- {t}" for t in p.readings) or "（なし）",
            )
            system_f = fill(
                system,
                summary_max=cfg.summary_max,
                worry_max=cfg.worry_max,
                themes="・".join(vocab),
            )
            info = None
            for _ in range(cfg.retries + 1):
                try:
                    info = _check(
                        parse_json_object(llm.complete(system_f, user, RESPONSE_SCHEMA)),
                        vocab_set,
                        cfg,
                    )
                    break
                except (ValueError, LLMOutputError):
                    continue
            if info is None:
                res.failed.append(key)
                log(f"  {key}：付加情報を作れませんでした")
                continue
            info.update(llm=llm.name, prompt_ver=prompt)
            res.made += 1
        text = p.source.get("display_text") or p.source["text"]
        rows.append(
            {
                "candidate_id": cid,
                "chunk_id": "",
                "work_id": f"canon:{work}",
                "section": heading(canon, key, numbered=p.numbered),
                "quote_text": text,
                "quote_start": 0,
                "quote_end": len(text),
                "context_text": "",
                **info,
                "passage_key": key,
                "quote_lang": p.quote_lang,
                "review_required": p.review_required,
                "source_id": p.source["rendering_id"],
                "material_sha": sha,
            }
        )
        author = w.get("author") or ""
        aid = author_id(w)
        works[f"canon:{work}"] = {
            "work_id": f"canon:{work}",
            "source": "canon",
            "source_work_id": work,
            "title": w["title"],
            "subtitle": "",
            "author_ids": [aid] if aid else [],
            "author_names": [author] if author else [],
            "author_display": [author] if author else [],
            "translator_ids": [],
            "translator_names": [],
            "ndc": "",
            "kana_type": "",
            "lang": w.get("lang") or "ja",
            "base_book": "",
            "first_pub": "",
            "original_title": "",
            "title_yomi": "",
            "parent_book": "",
            "url": "",
            "text_url": "",
            "license": "PD_JP",
            "license_reason": CANON_LICENSE_REASON,
            "review_required": False,
            "raw_path": None,
        }
    write_rows(candidates_path(hdd_root), rows, CANON_SCHEMA)
    write_rows(works_path(hdd_root), list(works.values()), WORKS_SCHEMA)
    # 青空文庫にいない作者（孔子など）も人物の書誌に入れる（背景の事実を人が書いて付けられる）。
    # 生没年は入れない（自動の事実を作らない。年は人が別の資料で確かめて facts.csv に書く）
    persons = {
        w["author_ids"][0]: {
            "person_id": w["author_ids"][0],
            "name": w["author_names"][0],
            "name_yomi": None,
            "birth_date": None,
            "death_date": None,
            "nationality": "",
            "allied": False,
            "canonical_id": w["author_ids"][0],
        }
        for w in works.values()
        if w["author_ids"] and w["author_ids"][0].startswith("canon:person:")
    }
    write_rows(persons_path(hdd_root), list(persons.values()), PERSONS_SCHEMA)
    gone = [cid for cid in old if cid not in {r["candidate_id"] for r in rows}]
    res.candidates = len(rows)
    if gone:
        log(f"  候補から外した一節 {len(gone)} 件（訳の材料が無くなったなど）")
    return res


@dataclass
class Coverage:
    work: str
    title: str
    passages: int = 0
    pd: int = 0  # 古い日本語訳がある（訳を作らない。D-014）
    approved: int = 0  # 承認済みの自前の訳がある
    draftable: int = 0  # 訳の材料がそろっている（koten shiyaku draft で作れる）
    missing: int = 0  # 訳の材料が足りない

    @property
    def without_pd(self) -> float:
        return (self.passages - self.pd) / self.passages if self.passages else 0.0


def coverage(
    conn: sqlite3.Connection, canon: dict[str, dict[str, Any]], *, min_versions: int = 2
) -> list[Coverage]:
    """作品ごとに、古い日本語訳がある一節と、自前の訳が要る一節の数（D-023 の見積もりの確かめ）。"""
    out: dict[str, Coverage] = {}
    for key in passage_keys(conn):
        work = key.partition(":")[0]
        if work not in canon:
            continue
        c = out.setdefault(work, Coverage(work, canon[work]["title"]))
        c.passages += 1
        p = plan_passage(conn, canon, key, min_versions=min_versions)
        if isinstance(p, str):
            c.missing += 1
        elif p.ready == "pd":
            c.pd += 1
        elif p.ready == "approved":
            c.approved += 1
        else:
            c.draftable += 1
    return list(out.values())
