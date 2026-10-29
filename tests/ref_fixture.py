"""資料の台帳と本文の CSV を、テスト用に小さく作る（M8a。資料・人・本文は架空）。"""

import csv

from koten.ref.registry import PEOPLE_COLUMNS, WORK_COLUMNS
from koten.ref.texts import ENTRY_COLUMNS, RENDERING_COLUMNS

AUTH = "https://id.ndl.go.jp/auth/ndlna/00000000"  # 架空の典拠 URL


def work(ref_id, **kw):
    base = {
        "ref_id": ref_id,
        "source_class": "S2",
        "kind": "dictionary",
        "title": "架空辞書",
        "pub_year": "1891",
        "source": "owned",
        "credit": "架空辞書（1891）",
        "holder_allied": "no",
    }
    return {**base, **kw}


def person(ref_id, seq, name, role, death, **kw):
    base = {
        "ref_id": ref_id,
        "seq": str(seq),
        "name": name,
        "role": role,
        "death_year": str(death) if death is not None else "",
        "country": "日本",
        "death_source": AUTH if death is not None else "",
    }
    return {**base, **kw}


def write(dirpath, works, people):
    write_csv(dirpath / "ref_works.csv", WORK_COLUMNS, works)
    write_csv(dirpath / "ref_people.csv", PEOPLE_COLUMNS, people)


def write_csv(path, cols, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def rendering(rid, passage_key, ref_id, text, **kw):
    base = {
        "rendering_id": rid,
        "passage_key": passage_key,
        "ref_id": ref_id,
        "role": "kundoku",
        "text": text,
        "reading": "まなびてときにこれをならう",
        "locator": "コマ12",
        "status": "verified",
        "checked_by": "owner",
        "checked_at": "2026-10-29",
    }
    return {**base, **kw}


def entry(eid, ref_id, headword, text, **kw):
    base = {
        "entry_id": eid,
        "ref_id": ref_id,
        "headword": headword,
        "headword_kana": "ふつう",
        "sense": "1",
        "text": text,
        "reading": "よのつね",
        "locator": "コマ800",
        "status": "verified",
        "checked_by": "owner",
        "checked_at": "2026-10-29",
    }
    return {**base, **kw}


def write_texts(dirpath, renderings=(), entries=()):
    write_csv(dirpath / "renderings.csv", RENDERING_COLUMNS, list(renderings))
    write_csv(dirpath / "dict_entries.csv", ENTRY_COLUMNS, list(entries))
