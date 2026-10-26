"""背景の事実の下書き（koten facts draft）と、人が確かめた下書きの取り込み（koten facts accept）。

1. 作者・作品ごとに、Wikidata の項目と、ウィキペディアの記事（手がかり）を net.py で取る
2. AI（llm.facts）が、資料に書いてあることだけから、事実の文と項目を下書きする
3. コードで形を確かめ、Wikidata の出典つきの記述と年が合えば「確かめる候補」に書き込む
4. 下書きを HDD の exports/facts/ に CSV で出す（status は確かめ待ち）
5. 人が別の資料で確かめ、確かめた出典・確認者・accept を書いた行だけを、
   koten facts accept が config/facts.csv・fact_sources.csv に足す（全部確かめてから書く）
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from koten.facts import wikimedia as wm
from koten.facts.model import Fact
from koten.facts.store import FACT_COLUMNS, SOURCE_COLUMNS, FactsError, read_curated
from koten.llm import ChatLLM, fill, load_prompt, parse_json_object
from koten.net import Fetcher

REVIEW_COLUMNS = [
    "accept",
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
    "problems",
    "lead_title",
    "lead_url",
    "lead_ref",
    "confirm_hint",
    "confirm_kind",
    "confirm_title",
    "confirm_url",
    "confirm_locator",
    "confirm_excerpt",
    "confirm_license",
    "confirm_credit",
    "checked_by",
    "checked_at",
]
ACCEPT = {"1", "yes", "y", "はい", "○", "◯", "ok"}
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text_ja": {"type": "string"},
                    "kind": {"type": "string"},
                    "scope": {"type": "string"},
                    "slots": {"type": "object", "additionalProperties": {"type": "string"}},
                    "date": {"type": "string"},
                    "sensitive": {"type": "string"},
                    "interest": {"type": "integer"},
                    "wikipedia_ref": {"type": "string"},
                    "confirm_hint": {"type": "string"},
                },
                "required": [
                    "text_ja",
                    "kind",
                    "scope",
                    "slots",
                    "date",
                    "sensitive",
                    "interest",
                    "wikipedia_ref",
                    "confirm_hint",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["facts"],
    "additionalProperties": False,
}


@dataclass
class DraftConfig:
    draft_prompt: str = "facts_draft_v1"
    max_facts: int = 8
    wikitext_max_chars: int = 30000


@dataclass
class Subject:
    subject_type: str  # person / work
    subject_id: str  # 代表の人物ID か作品ID
    name: str  # 人名か作品名
    aozora: str  # 青空文庫の書誌（LLM に渡す要約）
    wikidata_prop: str
    wikidata_value: str | None
    authors: list[str] = field(default_factory=list)  # 作品のとき：著者（代表の人物ID）


def subjects_for_episode(conn: sqlite3.Connection, plan: dict[str, Any]) -> list[Subject]:
    """回で読む作品と、その作者（代表の人物）。"""
    from koten.facts.select import canonical

    out: list[Subject] = []
    seen: set[str] = set()
    for sel in plan["selection"]:
        wid = sel["work_id"]
        if wid not in seen:
            seen.add(wid)
            out.append(work_subject(conn, wid))
        for pid in canonical(conn, sel.get("author_ids") or []):
            if pid not in seen:
                seen.add(pid)
                out.append(person_subject(conn, pid))
    return [s for s in out if s.name]


def work_subject(conn: sqlite3.Connection, work_id: str) -> Subject:
    w = conn.execute("SELECT * FROM works_meta WHERE work_id = ?", (work_id,)).fetchone()
    if w is None:
        return Subject("work", work_id, "", "", wm.AOZORA_WORK, None)
    people = json.loads(w["author_ids"] or "[]") + json.loads(w["translator_ids"] or "[]")
    names = _names(conn, people)
    lines = [
        f"作品名：『{w['title']}』",
        f"著者・訳者：{'・'.join(names) or '不明'}",
        f"原題：{w['original_title'] or 'なし'}",
        f"初出：{w['first_pub'] or '記載なし'}",
        f"底本：{w['base_book'] or '記載なし'}",
        f"図書カード：{w['url'] or ''}",
    ]
    return Subject(
        "work",
        work_id,
        w["title"],
        "\n".join(lines),
        wm.AOZORA_WORK,
        wm.aozora_work_value(w["url"] or ""),
        authors=json.loads(w["author_ids"] or "[]"),
    )


def person_subject(conn: sqlite3.Connection, person_id: str) -> Subject:
    p = conn.execute("SELECT * FROM persons WHERE person_id = ?", (person_id,)).fetchone()
    if p is None:
        return Subject("person", person_id, "", "", wm.AOZORA_AUTHOR, None)
    aliases = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM persons WHERE canonical_id = ? AND person_id != ?",
            (person_id, person_id),
        )
    ]
    lines = [
        f"人名：{p['name']}" + (f"（別名：{'・'.join(aliases)}）" if aliases else ""),
        f"生年月日：{p['birth_date'] or '不明'}",
        f"没年月日：{p['death_date'] or '不明'}",
    ]
    return Subject(
        "person",
        person_id,
        p["name"],
        "\n".join(lines),
        wm.AOZORA_AUTHOR,
        wm.aozora_author_value(person_id),
    )


def _names(conn: sqlite3.Connection, ids: list[str]) -> list[str]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    found = {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT person_id, name FROM persons WHERE person_id IN ({marks})", ids
        )
    }
    return [found[i] for i in ids if i in found]


def _id_prefix(subject: Subject) -> str:
    num = subject.subject_id.rsplit(":", 1)[-1]
    return f"F-{'p' if subject.subject_type == 'person' else 'w'}{num}-"


def next_ids(prefix: str, taken: set[str], n: int) -> list[str]:
    used = [int(m.group(1)) for t in taken if (m := re.fullmatch(re.escape(prefix) + r"(\d+)", t))]
    start = max(used, default=0) + 1
    return [f"{prefix}{i:03d}" for i in range(start, start + n)]


def draft_subject(
    subject: Subject,
    *,
    wikidata: Fetcher,
    wikipedia: Fetcher,
    llm: ChatLLM,
    prompts_dir: Path,
    cfg: DraftConfig,
    taken_ids: set[str],
    log: Callable[[str], None] = lambda _: None,
) -> list[dict[str, Any]]:
    """1人（1作品）の下書きの行（REVIEW_COLUMNS）。手がかりが無ければ空。"""
    qid = None
    if subject.wikidata_value is not None:
        qid = wm.find_qid(wikidata, subject.wikidata_prop, subject.wikidata_value)
    if qid is None and subject.subject_type == "work":
        # 青空文庫の作品IDが Wikidata に無ければ、著者（P7311 で探す）と題名で探す
        for author in subject.authors:
            aq = wm.find_qid(wikidata, wm.AOZORA_AUTHOR, wm.aozora_author_value(author))
            if aq and (qid := wm.find_work_by_author(wikidata, aq, subject.name)):
                break
    if qid is None:
        log(f"  {subject.name}：Wikidata の項目が見つからない（{subject.wikidata_prop}）")
        return []
    e = wm.entity(wikidata, qid)
    props = wm.PERSON_PROPS if subject.subject_type == "person" else wm.WORK_PROPS
    found = wm.claims(e, props)
    names = wm.labels(wikidata, [c.value for c in found if re.fullmatch(r"Q\d+", c.value)])
    wd_lines = [
        f"- {c.label}（{c.prop}）：{names.get(c.value, c.value)}"
        f"{'（出典あり）' if c.referenced else ''}"
        for c in found
    ] or ["（記述なし）"]
    title = wm.ja_title(e)
    page = wm.wikipedia_wikitext(wikipedia, title) if title else None
    if page is None:
        log(f"  {subject.name}：ウィキペディアの記事が無い（Wikidata {qid}）")
        return []
    revid, wikitext = page
    system, user_tpl = load_prompt(prompts_dir, cfg.draft_prompt)
    user = fill(
        user_tpl,
        subject=f"{subject.name}（{'作者' if subject.subject_type == 'person' else '作品'}）",
        aozora=subject.aozora,
        wikidata="\n".join(wd_lines),
        title=title,
        revid=revid,
        wikitext=wikitext[: cfg.wikitext_max_chars],
    )
    system = system.replace("{max_facts}", str(cfg.max_facts))
    items: list[dict[str, Any]] = []
    for _ in range(3):
        try:
            d = parse_json_object(llm.complete(system, user, DRAFT_SCHEMA))
        except ValueError:
            continue
        if isinstance(d.get("facts"), list):
            items = [x for x in d["facts"] if isinstance(x, dict)][: cfg.max_facts]
            break
    ids = next_ids(_id_prefix(subject), taken_ids, len(items))
    taken_ids.update(ids)
    rows = []
    lead_url = wm.wikipedia_url(title, revid)
    for fid, x in zip(ids, items, strict=True):
        slots = {str(k): str(v) for k, v in (x.get("slots") or {}).items()}
        f = Fact(
            fact_id=fid,
            subject_type=subject.subject_type,
            subject_id=subject.subject_id,
            scope=str(x.get("scope") or ""),
            kind=str(x.get("kind") or ""),
            text_ja=str(x.get("text_ja") or "").strip(),
            slots=slots,
            date=str(x.get("date") or ""),
            sensitive=str(x.get("sensitive") or "none"),
            interest=int(x.get("interest") or 2) if str(x.get("interest") or "2").isdigit() else 2,
            status="needs_review",
        )
        try:
            problems = [p for p in f.problems() if "checked_by" not in p]
        except (ValueError, TypeError) as err:
            problems = [str(err)]
        row = {c: "" for c in REVIEW_COLUMNS}
        row.update(
            {
                k: v
                for k, v in f.to_row().items()
                if k in REVIEW_COLUMNS and k not in ("checked_by", "checked_at")
            }
        )
        row.update(
            {
                "problems": "；".join(problems),
                "lead_title": f"ウィキペディア日本語版「{title}」",
                "lead_url": lead_url,
                "lead_ref": str(x.get("wikipedia_ref") or ""),
                "confirm_hint": str(x.get("confirm_hint") or ""),
            }
        )
        _prefill_wikidata(row, f, found, qid)
        rows.append(row)
    log(f"  {subject.name}：下書き {len(rows)} 件（Wikidata {qid}・ウィキペディア 版{revid}）")
    return rows


def _prefill_wikidata(row: dict[str, Any], f: Fact, found: list[wm.Claim], qid: str) -> None:
    """年が、出典つきの Wikidata の日付（生没年・刊行日）と合えば、確かめる手がかりに書き足す。

    確かめた出典（confirm_*）には書かない。年が合っても、文の主張まで確かめたことにはならない
    ため。確かめた出典は人が書く。
    """
    year = str(f.slots.get("year") or "")
    if not year:
        return
    for c in found:
        if c.prop in ("P569", "P570", "P577") and c.referenced and c.value.startswith(year):
            refs = "、".join(c.refs) or "あり"
            hint = (
                f"Wikidata {qid}#{c.prop}（{c.label}）={c.value}（出典：{refs}）："
                "年だけ合う。文の主張は別の資料で確かめる"
            )
            row["confirm_hint"] = "；".join(x for x in (row.get("confirm_hint"), hint) if x)
            return


def write_review(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:  # Excel でも文字化けしない
        w = csv.DictWriter(f, fieldnames=REVIEW_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def config_fact_ids(config_dir: Path, review_dir: Path | None = None) -> set[str]:
    """使い済みの事実ID（config/facts.csv と、まだ取り込んでいない下書きの CSV）。"""
    paths = [config_dir / "facts.csv"]
    if review_dir is not None and review_dir.exists():
        paths += sorted(review_dir.glob("review_*.csv"))
    ids: set[str] = set()
    for path in paths:
        if path.exists():
            with path.open(encoding="utf-8-sig") as f:
                ids |= {r["fact_id"] for r in csv.DictReader(f) if r.get("fact_id")}
    return ids


# --- 取り込み ---------------------------------------------------------------


def accept_review(
    review: Path,
    config_dir: Path,
    known_subjects: set[str],
    *,
    today: date | None = None,
    blocked: Callable[[str], tuple[str, str] | None] = lambda _: None,
) -> int:
    """accept に印のある行を config の CSV に足す。1行でもおかしければ何も書かない。"""
    today = today or date.today()
    with review.open(encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("accept") or "").strip().lower() in ACCEPT]
    if not rows:
        return 0
    facts_path, src_path = config_dir / "facts.csv", config_dir / "fact_sources.csv"
    old_facts = _read(facts_path)
    old_srcs = _read(src_path)
    new_facts, new_srcs = [], []
    for r in rows:
        new_facts.append(
            {
                **{c: (r.get(c) or "").strip() for c in FACT_COLUMNS},
                "checked_at": (r.get("checked_at") or "").strip() or today.isoformat(),
                "links": "[]",
            }
        )
        new_srcs.append(
            {
                "fact_id": r["fact_id"],
                "seq": "1",
                "kind": (r.get("confirm_kind") or "").strip(),
                "role": "confirm",
                "title": (r.get("confirm_title") or "").strip(),
                "url": (r.get("confirm_url") or "").strip(),
                "locator": (r.get("confirm_locator") or "").strip(),
                "excerpt": (r.get("confirm_excerpt") or "").strip(),
                "license": (r.get("confirm_license") or "").strip(),
                "credit": (r.get("confirm_credit") or "").strip(),
            }
        )
        if r.get("lead_url"):
            lead_title = (r.get("lead_title") or "ウィキペディア").strip()
            new_srcs.append(
                {
                    "fact_id": r["fact_id"],
                    "seq": "2",
                    "kind": "wikipedia",
                    "role": "lead",
                    "title": lead_title,
                    "url": r["lead_url"].strip(),
                    "locator": "",
                    "excerpt": (r.get("lead_ref") or "").strip()[:100],
                    "license": "CC BY-SA 4.0",
                    "credit": f"{lead_title}（CC BY-SA 4.0）",
                }
            )
    # 足したあとの全体を、読み込みと同じ決まりで確かめてから書く
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        _write(t / "facts.csv", FACT_COLUMNS, old_facts + new_facts)
        _write(t / "fact_sources.csv", SOURCE_COLUMNS, old_srcs + new_srcs)
        read_curated(t, known_subjects, blocked)  # FactsError ならここで止まる
    _write(facts_path, FACT_COLUMNS, old_facts + new_facts)
    _write(src_path, SOURCE_COLUMNS, old_srcs + new_srcs)
    return len(new_facts)


def _read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _write(path: Path, cols: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


__all__ = ["FactsError", "accept_review", "draft_subject", "subjects_for_episode"]
