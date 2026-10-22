"""青空文庫の取得（SPEC §6・§8.1）。

作品CSV（list_person_all_extended_utf8.zip）で対象を選び、権利を判定してから、
使用可（R1）の作品だけテキストzipを解凍せずに raw/aozora/ へ保存する。
作品の書誌と判定結果は clean/aozora/works.parquet、人物は clean/aozora/persons.parquet に書く。
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa

from koten import ids
from koten.license import License, Person, judge_public_domain, may_store_text
from koten.net import Fetcher
from koten.parquet import write_rows

SOURCE = "aozora"
AOZORA_SITE = "https://www.aozora.gr.jp/"
LIST_FILE = "list_person_all_extended_utf8.zip"

# 同じ作品に複数の文字遣いがあれば、新字新仮名を優先する（SPEC §6）
KANA_PRIORITY = {"新字新仮名": 0, "新字旧仮名": 1, "旧字新仮名": 2, "旧字旧仮名": 3}

ROLE_MAP = {"著者": "author", "翻訳者": "translator", "編者": "editor", "校訂者": "reviser"}
# 没年で R3/R4 を判定する役割。それ以外の役割は青空文庫の人物著作権フラグで判定する
JUDGED_ROLES = {"author", "translator"}

WORKS_SCHEMA = pa.schema(
    [
        ("work_id", pa.string()),
        ("source", pa.string()),
        ("source_work_id", pa.string()),
        ("title", pa.string()),
        ("subtitle", pa.string()),
        ("author_ids", pa.list_(pa.string())),
        ("author_names", pa.list_(pa.string())),
        ("translator_ids", pa.list_(pa.string())),
        ("ndc", pa.string()),
        ("kana_type", pa.string()),
        ("lang", pa.string()),
        ("base_book", pa.string()),
        ("url", pa.string()),
        ("text_url", pa.string()),
        ("license", pa.string()),
        ("license_reason", pa.string()),
        ("review_required", pa.bool_()),
        ("raw_path", pa.string()),
    ]
)

PERSONS_SCHEMA = pa.schema(
    [
        ("person_id", pa.string()),
        ("name", pa.string()),
        ("name_yomi", pa.string()),
        ("birth_date", pa.string()),
        ("death_date", pa.string()),
        ("nationality", pa.string()),
        ("allied", pa.bool_()),
    ]
)


@dataclass
class AozoraWork:
    work_id: str
    title: str
    subtitle: str
    ndc: str
    kana_type: str
    work_copyright: str
    card_url: str
    text_url: str
    base_book: str
    persons: list[dict[str, str]] = field(default_factory=list)

    def names(self, role: str) -> list[str]:
        return [p["name"] for p in self.persons if p["role"] == role]


def read_work_list(zip_bytes: bytes) -> dict[str, AozoraWork]:
    """作品CSVを作品ID→作品にまとめる（1行は作品×人物）。"""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".csv"))
        text = zf.read(name).decode("utf-8-sig")
    works: dict[str, AozoraWork] = {}
    for row in csv.DictReader(io.StringIO(text)):
        wid = row["作品ID"].strip()
        w = works.get(wid)
        if w is None:
            base = "、".join(
                v
                for v in (
                    row.get("底本名1", ""),
                    row.get("底本出版社名1", ""),
                    row.get("底本初版発行年1", ""),
                )
                if v
            )
            w = works[wid] = AozoraWork(
                work_id=wid,
                title=row["作品名"].strip(),
                subtitle=row.get("副題", "").strip(),
                ndc=row.get("分類番号", "").strip(),
                kana_type=row.get("文字遣い種別", "").strip(),
                work_copyright=row.get("作品著作権フラグ", "").strip(),
                card_url=row.get("図書カードURL", "").strip(),
                text_url=row.get("テキストファイルURL", "").strip(),
                base_book=base,
            )
        w.persons.append(
            {
                "person_id": row["人物ID"].strip(),
                "name": row.get("姓", "").strip() + row.get("名", "").strip(),
                "name_yomi": row.get("姓読み", "").strip() + row.get("名読み", "").strip(),
                "role": ROLE_MAP.get(row.get("役割フラグ", "").strip(), "other"),
                "birth_date": row.get("生年月日", "").strip(),
                "death_date": row.get("没年月日", "").strip(),
                "copyright": row.get("人物著作権フラグ", "").strip(),
            }
        )
    return works


def ndc_codes(ndc: str) -> list[str]:
    """ "NDC 159 914" や "NDC K913" から ["159", "914"]、["913"] を取り出す。"""
    return [c.lstrip("K") for c in re.findall(r"K?\d+", ndc.replace("NDC", ""))]


def matches_ndc(ndc: str, prefixes: list[str]) -> bool:
    return any(code.startswith(p) for code in ndc_codes(ndc) for p in prefixes)


def _death_year(date: str) -> int | None:
    m = re.match(r"(\d{4})", date or "")
    return int(m.group(1)) if m else None


def judge_license(w: AozoraWork) -> tuple[License, str]:
    """青空文庫の作品の権利判定。

    1. 作品・人物の著作権フラグが1つでも「あり」なら保護中
    2. 著者・訳者の没年で R3・R4 を判定する（フラグが「なし」でも没年が保護中なら保護中とする）
       訳者がいる作品の原著者は外国人とみなし、国籍不明として R4 を確かめる
    """
    flags = [w.work_copyright] + [p["copyright"] for p in w.persons]
    if any(f == "あり" for f in flags):
        return License.COPYRIGHTED, "青空文庫の著作権フラグが「あり」"
    if any(f != "なし" for f in flags):
        return License.UNKNOWN, "青空文庫の著作権フラグが不明"
    translated = any(p["role"] == "translator" for p in w.persons)
    persons = [
        Person(
            name=p["name"],
            death_year=_death_year(p["death_date"]),
            role=p["role"],
            allied=None if (translated and p["role"] == "author") else False,
        )
        for p in w.persons
        if p["role"] in JUDGED_ROLES
    ]
    lic, reason = judge_public_domain(persons)
    return lic, f"青空文庫の著作権フラグはすべて「なし」；{reason}"


def select_works(works: dict[str, AozoraWork], conf: dict[str, Any]) -> list[AozoraWork]:
    """sources.yaml の条件で対象を選び、文字遣い違いの重複を除く。

    include_works で指定した作品を先頭に並べる（--limit を付けても先に取得されるように）。
    """
    prefixes = [str(p) for p in conf.get("ndc_prefixes", [])]
    include = {(i["title"], i["author"]) for i in conf.get("include_works", [])}
    exclude = {str(i) for i in conf.get("exclude_work_ids", [])}
    picked = []
    listed: set[str] = set()
    for w in works.values():
        if w.work_id in exclude:
            continue
        authors = w.names("author")
        by_list = any((w.title, a) in include for a in authors)
        if by_list:
            listed.add(w.work_id)
        if by_list or matches_ndc(w.ndc, prefixes):
            picked.append(w)
    best: dict[tuple[str, str, tuple[str, ...]], AozoraWork] = {}
    for w in sorted(picked, key=lambda w: int(w.work_id) if w.work_id.isdigit() else 0):
        key = (w.title, w.subtitle, tuple(sorted(w.names("author"))))
        cur = best.get(key)
        if cur is None or KANA_PRIORITY.get(w.kana_type, 9) < KANA_PRIORITY.get(cur.kana_type, 9):
            best[key] = w
    return sorted(
        best.values(),
        key=lambda w: (w.work_id not in listed, int(w.work_id) if w.work_id.isdigit() else 0),
    )


def text_rel_path(text_url: str) -> str | None:
    """テキストファイルURLを raw/aozora/ からの相対パスにする（例：cards/000218/files/x.zip）。"""
    if not text_url.startswith(AOZORA_SITE) or not text_url.endswith(".zip"):
        return None
    return text_url[len(AOZORA_SITE) :]


def mirror_url(text_url: str, base_url: str) -> str:
    return base_url.rstrip("/") + "/" + text_url[len(AOZORA_SITE) :]


def work_row(w: AozoraWork, lic: License, reason: str, raw_path: str | None) -> dict[str, Any]:
    pid = lambda p: f"{SOURCE}:person:{p['person_id']}"  # noqa: E731
    return {
        "work_id": ids.work_id(SOURCE, w.work_id),
        "source": SOURCE,
        "source_work_id": w.work_id,
        "title": w.title,
        "subtitle": w.subtitle,
        "author_ids": [pid(p) for p in w.persons if p["role"] == "author"],
        "author_names": w.names("author"),
        "translator_ids": [pid(p) for p in w.persons if p["role"] == "translator"],
        "ndc": w.ndc,
        "kana_type": w.kana_type,
        "lang": "ja",
        "base_book": w.base_book,
        "url": w.card_url,
        "text_url": w.text_url,
        "license": str(lic),
        "license_reason": reason,
        "review_required": lic == License.NEEDS_REVIEW,
        "raw_path": raw_path,
    }


def person_rows(works: list[AozoraWork]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for w in works:
        translated = any(p["role"] == "translator" for p in w.persons)
        for p in w.persons:
            key = f"{SOURCE}:person:{p['person_id']}"
            foreign = translated and p["role"] == "author"
            seen.setdefault(
                key,
                {
                    "person_id": key,
                    "name": p["name"],
                    "name_yomi": p["name_yomi"],
                    "birth_date": p["birth_date"],
                    "death_date": p["death_date"],
                    "nationality": None if foreign else "JP",
                    "allied": None if foreign else False,
                },
            )
    return list(seen.values())


@dataclass
class IngestResult:
    selected: int
    usable: int
    fetched: int
    skipped: int
    blocked: dict[str, int]
    errors: list[str] = field(default_factory=list)


def ingest(
    hdd_root: Path,
    conf: dict[str, Any],
    fetcher: Fetcher,
    *,
    limit: int | None = None,
    refresh_list: bool = False,
    log: Callable[[str], None] = lambda _: None,
) -> IngestResult:
    """作品CSVを取り、対象作品のテキストzipを raw/aozora/ に保存する。

    中断しても、再実行で続きから進む。log には進み具合の1行ずつが渡される。
    """
    log(f"作品CSVを確認中：{conf['list_url']}")
    list_path, got = fetcher.fetch(conf["list_url"], LIST_FILE, refresh=refresh_list)
    log("作品CSVを取得しました" if got else "作品CSVは取得済みのものを使います")
    works = select_works(read_work_list(list_path.read_bytes()), conf)
    storable = [
        rel
        for w in works
        if may_store_text(judge_license(w)[0]) and (rel := text_rel_path(w.text_url))
    ]
    remaining = sum(1 for rel in storable if not (fetcher.raw_dir / rel).exists())
    planned = remaining if limit is None else min(limit, remaining)
    log(f"対象 {len(works)} 作品。本文を保存できるのは {len(storable)} 作品")
    log(f"未取得 {remaining} 件のうち、今回は {planned} 件を取得します（1秒に1件まで）")

    rows = []
    blocked: dict[str, int] = defaultdict(int)
    fetched = skipped = usable = 0
    errors: list[str] = []
    base_url = conf.get("text_base_url") or AOZORA_SITE
    for w in works:
        lic, reason = judge_license(w)
        rel = text_rel_path(w.text_url)
        raw_path = None
        if not may_store_text(lic):
            # R2：本文は保存しない（書誌情報のみ）
            blocked[str(lic)] += 1
        elif rel is None:
            blocked["NO_TEXT_ZIP"] += 1
        else:
            usable += 1
            dest = fetcher.raw_dir / rel
            if dest.exists():
                raw_path = rel
                skipped += 1
            elif limit is None or fetched + len(errors) < limit:
                url = mirror_url(w.text_url, base_url)
                n = fetched + len(errors) + 1
                log(f"[{n}/{planned}] {w.title}（{'・'.join(w.names('author'))}）")
                try:
                    fetcher.fetch(url, rel, license=str(lic))
                except httpx.HTTPError as e:
                    # 1件の失敗で夜間バッチ全体を止めない。次の実行で取り直す
                    msg = str(e).splitlines()[0] if str(e) else type(e).__name__
                    errors.append(f"{w.work_id} {w.title}: {msg}")
                    log(f"    取得失敗：{msg}")
                else:
                    raw_path = rel
                    fetched += 1
        rows.append(work_row(w, lic, reason, raw_path))

    clean_dir = hdd_root / "clean" / SOURCE
    log(f"書誌と判定結果を書き込み中：{clean_dir}")
    write_rows(clean_dir / "works.parquet", rows, WORKS_SCHEMA)
    write_rows(clean_dir / "persons.parquet", person_rows(works), PERSONS_SCHEMA)
    return IngestResult(len(works), usable, fetched, skipped, dict(blocked), errors)
