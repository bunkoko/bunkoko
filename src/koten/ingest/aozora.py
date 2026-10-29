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
from koten.ingest.review import ANONYMOUS_NAMES, AozoraReview
from koten.license import (
    LAST_EXPIRED_ANONYMOUS_PUB_YEAR,
    LAST_EXPIRED_DEATH_YEAR,
    License,
    Person,
    judge_anonymous_work,
    judge_public_domain,
    may_store_text,
)
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
        ("author_display", pa.list_(pa.string())),  # 同じ人の別名をまとめた表示名（authors.py）
        ("translator_ids", pa.list_(pa.string())),
        ("translator_names", pa.list_(pa.string())),
        ("ndc", pa.string()),
        ("kana_type", pa.string()),
        ("lang", pa.string()),
        ("base_book", pa.string()),
        # 背景の事実に使う書誌（作品CSVの列。無ければ空）
        ("first_pub", pa.string()),  # 初出
        ("original_title", pa.string()),  # 原題
        ("title_yomi", pa.string()),  # 作品名読み
        ("parent_book", pa.string()),  # 底本の親本
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
        ("canonical_id", pa.string()),  # 同じ人の別名をまとめた代表の人物ID
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
    first_pub: str = ""
    original_title: str = ""
    title_yomi: str = ""
    parent_book: str = ""
    # 初出・底本・底本の親本の年のうち、いちばん早い年（この作品が公表済みだった証拠。D-029）
    published_by: int | None = None
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
                first_pub=row.get("初出", "").strip(),
                original_title=row.get("原題", "").strip(),
                title_yomi=row.get("作品名読み", "").strip(),
                parent_book="、".join(
                    v
                    for v in (
                        row.get("底本の親本名1", ""),
                        row.get("底本の親本出版社名1", ""),
                        row.get("底本の親本初版発行年1", ""),
                    )
                    if v
                ),
                published_by=_earliest_year(row, PUBLISHED_COLUMNS),
            )
        w.persons.append(
            {
                "person_id": row["人物ID"].strip(),
                "name": row.get("姓", "").strip() + row.get("名", "").strip(),
                "surname": row.get("姓", "").strip(),
                "name_yomi": row.get("姓読み", "").strip() + row.get("名読み", "").strip(),
                "role": ROLE_MAP.get(row.get("役割フラグ", "").strip(), "other"),
                "birth_date": row.get("生年月日", "").strip(),
                "death_date": row.get("没年月日", "").strip(),
                "copyright": row.get("人物著作権フラグ", "").strip(),
            }
        )
    return works


# この作品（訳・版を含む）が公表済みだったことを示す年の列
PUBLISHED_COLUMNS = (
    "初出",
    "底本初版発行年1",
    "底本の親本初版発行年1",
    "底本初版発行年2",
    "底本の親本初版発行年2",
)


def _earliest_year(row: dict[str, str], columns: tuple[str, ...]) -> int | None:
    years = [int(y) for c in columns for y in re.findall(r"(?<!\d)(\d{4})(?!\d)", row.get(c) or "")]
    return min(years) if years else None


def ndc_codes(ndc: str) -> list[str]:
    """ "NDC 159 914" や "NDC K913" から ["159", "914"]、["913"] を取り出す。"""
    return [c.lstrip("K") for c in re.findall(r"K?\d+", ndc.replace("NDC", ""))]


def matches_ndc(ndc: str, prefixes: list[str]) -> bool:
    return any(code.startswith(p) for code in ndc_codes(ndc) for p in prefixes)


def _death_year(date: str) -> int | None:
    """作品CSVの没年月日から年を取る。3桁の年（紀貫之の 945 など）も読む。

    読めない形（前347 のような紀元前・空）は None にし、判定不能のままにする。
    """
    s = (date or "").strip()
    m = re.match(r"(\d{4})", s) or re.match(r"(\d{3})(?:-|$)", s)
    return int(m.group(1)) if m else None


# 人の一生の最長の記録（122年）。この年より前に生まれた人は、1967年より後まで生きていない
MAX_LIFESPAN = 122
# 利用者の決まり（D-029）：没年が空でも、1900年より前に名前が出ている人は使ってよい
EARLY_NAME_BEFORE = 1900


def _blank_death(
    p: dict[str, str], w: AozoraWork, review: AozoraReview
) -> tuple[str, int | None, str]:
    """作品CSVの没年が空か読めない人の扱い（D-029）。

    戻り値は (扱い, 年, 理由)。扱いは
    year（この年を没年として判定する）・skip（この人は満了として没年の判定から外す）・
    copyrighted（作者不詳で1968年からの公表）・unknown（判定不能のまま）。
    台帳（人が確かめた行）を先に見て、次に作品CSVの生年・公表の年から決める。
    """
    name = p["name"]
    if name in ANONYMOUS_NAMES:
        if p["role"] != "author":
            return "unknown", None, ""
        fact = review.anonymous.get((name, w.title))
        if fact is not None:
            lic, why = judge_anonymous_work(fact.year)
            kind = "skip" if lic is License.PD_JP else "copyrighted"
            return kind, None, f"{why}（人が確かめた：{fact.basis}）"
        if w.published_by is not None and w.published_by <= LAST_EXPIRED_ANONYMOUS_PUB_YEAR:
            # 訳が公表されれば、元の作品も公表されたとみなす（著作権法4条4項）
            return (
                "skip",
                None,
                (
                    f"作者不詳で、この作品（訳・版を含む）は{w.published_by}年までに公表（52条。満了）"
                ),
            )
        return "unknown", None, ""
    if (fact := review.deaths.get(name)) is not None:
        return "year", fact.year, f"{name}の没年{fact.year}は人が確かめた（{fact.basis}）"
    if (fact := review.early.get(name)) is not None:
        return "skip", None, f"{name}は{fact.year}年に名前が出ている（1900年より前。{fact.basis}）"
    birth = _death_year(p.get("birth_date", ""))
    if birth is not None and birth + MAX_LIFESPAN <= LAST_EXPIRED_DEATH_YEAR:
        latest = birth + MAX_LIFESPAN
        return "year", latest, f"{name}は{birth}年生まれで、遅くとも{latest}年までに没"
    if w.published_by is not None and w.published_by < EARLY_NAME_BEFORE:
        return (
            "skip",
            None,
            (f"{name}の作品は{w.published_by}年までに公表（1900年より前に名前が出ている。D-029）"),
        )
    return "unknown", None, ""


def judge_license(w: AozoraWork, review: AozoraReview | None = None) -> tuple[License, str]:
    """青空文庫の作品の権利判定。

    1. 作品・人物の著作権フラグが1つでも「あり」なら保護中
    2. 著者・訳者の没年で R3・R4 を判定する（フラグが「なし」でも没年が保護中なら保護中とする）
       訳者がいる作品の原著者は外国人とみなし、国籍不明として R4 を確かめる
    3. 作品CSVの没年が空か読めない人は、_blank_death で決める（D-029）。決まらなければ判定不能
    """
    flags = [w.work_copyright] + [p["copyright"] for p in w.persons]
    if any(f == "あり" for f in flags):
        return License.COPYRIGHTED, "青空文庫の著作権フラグが「あり」"
    if any(f != "なし" for f in flags):
        return License.UNKNOWN, "青空文庫の著作権フラグが不明"
    review = review or AozoraReview()
    translated = any(p["role"] == "translator" for p in w.persons)
    persons: list[Person] = []
    notes: list[str] = []
    for p in w.persons:
        if p["role"] not in JUDGED_ROLES:
            continue
        year = _death_year(p["death_date"])
        if year is None:
            kind, filled, note = _blank_death(p, w, review)
            if kind == "copyrighted":
                return License.COPYRIGHTED, note
            if note:
                notes.append(note)
            if kind == "skip":
                continue  # この人は満了として、没年では判定しない
            year = filled
        persons.append(
            Person(
                name=p["name"],
                death_year=year,
                role=p["role"],
                allied=None if (translated and p["role"] == "author") else False,
            )
        )
    if not persons and notes:
        lic, reason = License.PD_JP, "；".join(notes)
    else:
        lic, reason = judge_public_domain(persons)
        reason = "；".join([reason, *notes])
    return lic, f"青空文庫の著作権フラグはすべて「なし」；{reason}"


def unknown_people(w: AozoraWork, review: AozoraReview | None = None) -> list[str]:
    """没年が空か読めず、台帳でも作品CSVの生年・公表の年でも決まらない人（著者・訳者）。"""
    review = review or AozoraReview()
    return [
        p["name"]
        for p in w.persons
        if p["role"] in JUDGED_ROLES
        and _death_year(p["death_date"]) is None
        and _blank_death(p, w, review)[0] == "unknown"
    ]


def select_works(works: dict[str, AozoraWork], conf: dict[str, Any]) -> list[AozoraWork]:
    """sources.yaml の条件で対象を選び、文字遣い違いの重複を除く。

    include_works で指定した作品を先頭に並べる（--limit を付けても先に取得されるように）。
    """
    prefixes = [str(p) for p in conf.get("ndc_prefixes", [])]
    include = {(i["title"], i["author"]) for i in conf.get("include_works", [])}
    exclude = {str(i) for i in conf.get("exclude_work_ids", [])}
    # 有名な作家の詩歌・小説など（D-025）。作家名と NDC の両方に合う作品だけ
    authors_in = {str(a) for a in conf.get("include_authors", [])}
    author_ndc = [str(p) for p in conf.get("include_authors_ndc", [])]
    # 古典の訳（晶子訳『源氏物語』など）。訳者の名前で選ぶ（NDC は問わない。D-029）
    translators_in = {str(a) for a in conf.get("include_translators", [])}
    picked = []
    listed: set[str] = set()
    for w in works.values():
        if w.work_id in exclude:
            continue
        authors = w.names("author")
        by_list = any((w.title, a) in include for a in authors)
        if by_list:
            listed.add(w.work_id)
        by_author = bool(authors_in & set(authors)) and matches_ndc(w.ndc, author_ndc)
        by_translator = bool(translators_in & set(w.names("translator")))
        if by_list or by_author or by_translator or matches_ndc(w.ndc, prefixes):
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


def person_aliases(works: dict[str, AozoraWork]) -> dict[str, str]:
    """同じ人の別名の人物IDを、作品の多いほうの人物IDにまとめる対応表（人物ID→代表の人物ID）。

    姓・生年月日・没年月日がすべて同じ人物を同じ人とみなす（どれかが空なら、まとめない）。
    """
    count: dict[str, int] = defaultdict(int)
    groups: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for w in works.values():
        for p in w.persons:
            count[p["person_id"]] += 1
            key = (p.get("surname", ""), p.get("birth_date", ""), p.get("death_date", ""))
            if all(key):
                groups[key].add(p["person_id"])
    aliases = {}
    for members in groups.values():
        if len(members) > 1:
            head = min(members, key=lambda m: (-count[m], m))
            aliases.update({m: head for m in members})
    return aliases


def _authors(w: AozoraWork, aliases: dict[str, str]) -> tuple[list[str], list[str]]:
    """作品の著者を同じ人ごとにまとめ、(人物IDの並び, 表示名の並び) を返す。

    表示名は、この作品に載っている名前のうち、作品の多い名前を先にして「主な名前（別名）」にする。
    """
    groups: dict[str, list[dict[str, str]]] = {}
    for p in w.persons:
        if p["role"] == "author":
            groups.setdefault(aliases.get(p["person_id"], p["person_id"]), []).append(p)
    ids_, names = [], []
    for head, members in groups.items():
        members = sorted(members, key=lambda p: p["person_id"] != head)  # 代表の名前を先に
        ids_.append(f"{SOURCE}:person:{head}")
        rest = [p["name"] for p in members[1:] if p["name"] != members[0]["name"]]
        names.append(members[0]["name"] + (f"（{'・'.join(rest)}）" if rest else ""))
    return ids_, names


def work_row(
    w: AozoraWork,
    lic: License,
    reason: str,
    raw_path: str | None,
    aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    pid = lambda p: f"{SOURCE}:person:{p['person_id']}"  # noqa: E731
    author_ids, author_display = _authors(w, aliases or {})
    return {
        "work_id": ids.work_id(SOURCE, w.work_id),
        "source": SOURCE,
        "source_work_id": w.work_id,
        "title": w.title,
        "subtitle": w.subtitle,
        "author_ids": author_ids,  # 同じ人の別名は代表の人物IDにまとめる（再使用間隔のため）
        "author_names": w.names("author"),  # 載っている名前すべて（G1 で直書きを見つけるため）
        "author_display": author_display,
        "translator_ids": [pid(p) for p in w.persons if p["role"] == "translator"],
        "translator_names": w.names("translator"),
        "ndc": w.ndc,
        "kana_type": w.kana_type,
        "lang": "ja",
        "base_book": w.base_book,
        "first_pub": w.first_pub,
        "original_title": w.original_title,
        "title_yomi": w.title_yomi,
        "parent_book": w.parent_book,
        "url": w.card_url,
        "text_url": w.text_url,
        "license": str(lic),
        "license_reason": reason,
        "review_required": lic == License.NEEDS_REVIEW,
        "raw_path": raw_path,
    }


def person_rows(
    works: list[AozoraWork],
    aliases: dict[str, str] | None = None,
    listed: dict[str, AozoraWork] | None = None,
) -> list[dict[str, Any]]:
    """作品に載る人物。別名の代表の人物が対象の作品に載っていなくても、作品CSV全体（listed）
    から代表の人物を足す（代表の人物に事実を付けるため）。"""
    aliases = aliases or {}
    seen: dict[str, dict[str, Any]] = {}
    heads = {aliases[p["person_id"]] for w in works for p in w.persons if p["person_id"] in aliases}
    extra = [w for w in (listed or {}).values() if any(p["person_id"] in heads for p in w.persons)]
    for w in list(works) + extra:
        translated = any(p["role"] == "translator" for p in w.persons)
        for p in w.persons:
            if w in extra and p["person_id"] not in heads:
                continue
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
                    "canonical_id": f"{SOURCE}:person:"
                    + aliases.get(p["person_id"], p["person_id"]),
                },
            )
    return list(seen.values())


@dataclass
class IngestPlan:
    """取得の前の見積もり（koten ingest aozora --dry-run）。"""

    selected: int
    storable: int
    stored: int  # もう取ってある
    new: int  # これから取る
    by_reason: dict[str, int]
    missing_authors: list[str]  # include_authors のうち、作品CSVに見つからない作家
    missing_works: list[str]  # include_works のうち、見つからない作品
    # 没年が空で、台帳（config/aozora_review.csv）でも埋まっていない人 → 作品の数（D-029）
    unknown_people: dict[str, int]
    chunks_per_work: float | None  # 今の取得済みの作品から数えた、1作品あたりのチャンク数
    est_chunks: int | None
    est_hours: float | None  # 付加情報づけ（augment）の時間の目安


AUGMENT_SEC_PER_CHUNK = 4.9  # gemma4:26b で測った1チャンクあたりの秒数（D-005）


def plan_ingest(
    hdd_root: Path, conf: dict[str, Any], fetcher: Fetcher, review: AozoraReview | None = None
) -> IngestPlan:
    """作品CSVだけを見て、対象の作品数と、付加情報づけにかかる時間の目安を出す（本文は取らない）。"""
    list_path, _ = fetcher.fetch(conf["list_url"], LIST_FILE)
    listed = read_work_list(list_path.read_bytes())
    works = select_works(listed, conf)
    prefixes = [str(p) for p in conf.get("ndc_prefixes", [])]
    include = {(i["title"], i["author"]) for i in conf.get("include_works", [])}
    authors_in = {str(a) for a in conf.get("include_authors", [])}
    author_ndc = [str(p) for p in conf.get("include_authors_ndc", [])]
    translators_in = {str(a) for a in conf.get("include_translators", [])}
    reasons: dict[str, int] = defaultdict(int)
    unknown: dict[str, int] = defaultdict(int)
    storable = stored = 0
    for w in works:
        names = set(w.names("author"))
        if any((w.title, a) in include for a in names):
            reasons["作品の指定（include_works）"] += 1
        elif matches_ndc(w.ndc, prefixes):
            reasons["NDC（ndc_prefixes）"] += 1
        elif names & authors_in and matches_ndc(w.ndc, author_ndc):
            reasons["有名な作家（include_authors）"] += 1
        elif translators_in & set(w.names("translator")):
            reasons["古典の訳者（include_translators）"] += 1
        rel = text_rel_path(w.text_url)
        lic = judge_license(w, review)[0]
        if may_store_text(lic) and rel:
            storable += 1
            stored += (fetcher.raw_dir / rel).exists()
        elif lic is License.UNKNOWN:
            for name in unknown_people(w, review):
                unknown[name] += 1
    every_author = {a for w in listed.values() for a in w.names("author")}
    every_translator = {a for w in listed.values() for a in w.names("translator")}
    missing_authors = sorted(authors_in - every_author)
    missing_authors += sorted(f"{a}（訳者）" for a in translators_in - every_translator)
    found_works = {(w.title, a) for w in listed.values() for a in w.names("author")}
    missing_works = sorted(f"{t}（{a}）" for t, a in include - found_works)
    per_work = None
    chunks_path = hdd_root / "chunks" / "aozora.parquet"
    works_path = hdd_root / "clean" / "aozora" / "texts.parquet"
    if chunks_path.exists() and works_path.exists():
        import pyarrow.parquet as pq

        n_chunks = pq.read_metadata(chunks_path).num_rows
        n_works = pq.read_metadata(works_path).num_rows
        per_work = round(n_chunks / n_works, 1) if n_works else None
    new = storable - stored
    est_chunks = int(new * per_work) if per_work is not None else None
    est_hours = (
        round(est_chunks * AUGMENT_SEC_PER_CHUNK / 3600, 1) if est_chunks is not None else None
    )
    return IngestPlan(
        selected=len(works),
        storable=storable,
        stored=stored,
        new=new,
        by_reason=dict(reasons),
        missing_authors=missing_authors,
        missing_works=missing_works,
        unknown_people=dict(sorted(unknown.items(), key=lambda kv: -kv[1])),
        chunks_per_work=per_work,
        est_chunks=est_chunks,
        est_hours=est_hours,
    )


@dataclass
class IngestResult:
    selected: int
    usable: int
    fetched: int
    skipped: int
    blocked: dict[str, int]
    errors: list[str] = field(default_factory=list)
    # 前は保存できたが、判定が厳しくなって保存できなくなった作品（本文を消した）
    removed: list[str] = field(default_factory=list)


def ingest(
    hdd_root: Path,
    conf: dict[str, Any],
    fetcher: Fetcher,
    *,
    limit: int | None = None,
    refresh_list: bool = False,
    log: Callable[[str], None] = lambda _: None,
    review: AozoraReview | None = None,
) -> IngestResult:
    """作品CSVを取り、対象作品のテキストzipを raw/aozora/ に保存する。

    中断しても、再実行で続きから進む。log には進み具合の1行ずつが渡される。
    """
    log(f"作品CSVを確認中：{conf['list_url']}")
    list_path, got = fetcher.fetch(conf["list_url"], LIST_FILE, refresh=refresh_list)
    log("作品CSVを取得しました" if got else "作品CSVは取得済みのものを使います")
    listed = read_work_list(list_path.read_bytes())
    aliases = person_aliases(listed)
    works = select_works(listed, conf)
    storable = [
        rel
        for w in works
        if may_store_text(judge_license(w, review)[0]) and (rel := text_rel_path(w.text_url))
    ]
    remaining = sum(1 for rel in storable if not (fetcher.raw_dir / rel).exists())
    planned = remaining if limit is None else min(limit, remaining)
    log(f"対象 {len(works)} 作品。本文を保存できるのは {len(storable)} 作品")
    log(f"未取得 {remaining} 件のうち、今回は {planned} 件を取得します（1秒に1件まで）")

    rows = []
    blocked: dict[str, int] = defaultdict(int)
    fetched = skipped = usable = 0
    errors: list[str] = []
    removed: list[str] = []
    base_url = conf.get("text_base_url") or AOZORA_SITE
    for w in works:
        lic, reason = judge_license(w, review)
        rel = text_rel_path(w.text_url)
        raw_path = None
        if not may_store_text(lic):
            # R2：本文は保存しない（書誌情報のみ）
            blocked[str(lic)] += 1
            if rel and (old := fetcher.raw_dir / rel).exists():
                old.unlink()  # 前の判定で保存した本文も残さない
                removed.append(f"{SOURCE}:{w.work_id} {w.title}（{lic}）")
                log(f"    本文を消しました（{lic}）：{w.title}")
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
        rows.append(work_row(w, lic, reason, raw_path, aliases))

    clean_dir = hdd_root / "clean" / SOURCE
    log(f"書誌と判定結果を書き込み中：{clean_dir}")
    write_rows(clean_dir / "works.parquet", rows, WORKS_SCHEMA)
    write_rows(clean_dir / "persons.parquet", person_rows(works, aliases, listed), PERSONS_SCHEMA)
    return IngestResult(len(works), usable, fetched, skipped, dict(blocked), errors, removed)
