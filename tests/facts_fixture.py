"""背景の事実のテスト用：m4_corpus の作品に付ける、人が確かめた事実（中身は架空）と人物の書誌。"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from koten.ingest.aozora import PERSONS_SCHEMA
from koten.parquet import write_rows

FACTS = [
    {
        "fact_id": "F-w001-001",
        "subject_type": "work",
        "subject_id": "aozora:001",
        "scope": "work",
        "kind": "work_origin",
        "text_ja": "『比べる心』は、1906年に架空の町で書かれました。",
        "slots": {"title": "比べる心", "year": "1906", "place": "架空の町"},
        "date": "1906",
        "interest": 3,
    },
    {
        "fact_id": "F-p1-001",
        "subject_type": "person",
        "subject_id": "aozora:person:p1",
        "scope": "person",
        "kind": "life_event",
        "text_ja": "甲野一郎は、1905年に架空の国の作家を訪ねました。",
        "slots": {"person": "甲野一郎", "year": "1905", "person2": "架空の国の作家"},
        "date": "1905",
        "interest": 3,
    },
]


def write_curated(config_dir: Path, facts=None, sources=None) -> None:
    facts = FACTS if facts is None else facts
    with (config_dir / "facts.csv").open("w", encoding="utf-8", newline="") as f:
        cols = [
            "fact_id", "subject_type", "subject_id", "scope", "kind", "text_ja", "slots",
            "date", "sensitive", "interest", "checked_by", "checked_at", "links",
        ]  # fmt: skip
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for x in facts:
            w.writerow(
                {
                    "sensitive": "none",
                    "checked_by": "テスト",
                    "checked_at": "2026-10-26",
                    "links": "[]",
                    **x,
                    "slots": json.dumps(x["slots"], ensure_ascii=False),
                }
            )
    if sources is None:
        sources = [
            {
                "fact_id": x["fact_id"],
                "seq": 1,
                "kind": "book",
                "role": "confirm",
                "title": "架空の伝記",
                "url": "",
                "locator": "架空書房、12ページ",
                "excerpt": "",
                "license": "事実のみ（文は自前）",
                "credit": "",
            }
            for x in facts
        ]
    with (config_dir / "fact_sources.csv").open("w", encoding="utf-8", newline="") as f:
        cols = ["fact_id", "seq", "kind", "role", "title", "url", "locator", "excerpt",
                "license", "credit"]  # fmt: skip
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(sources)


def write_persons(hdd: Path) -> None:
    rows = [
        {
            "person_id": "aozora:person:p1",
            "name": "甲野一郎",
            "name_yomi": "",
            "birth_date": "1880-02-03",
            "death_date": "1940-05-06",
            "nationality": "JP",
            "allied": False,
            "canonical_id": "aozora:person:p1",
        }
    ]
    write_rows(hdd / "clean/aozora/persons.parquet", rows, PERSONS_SCHEMA)
