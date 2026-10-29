"""資料の台帳（config/ref_works.csv・ref_people.csv）を読み、1冊ずつ権利を判定する（M8a）。

台帳に載せるのは、保護期間の切れた資料（S1 原文・S2 時代の解釈：昔の辞書・訓読本・注釈書・
古い訳）だけ。著作権のある今の本（S4）は載せない（本文を持たないので、別の表で扱う）。

判定は license.py の関数を組み合わせ、いちばん厳しい結果を採る：
- 関わった人全員の没年（著者・編者・校訂者・訓読者・注釈者・序文や解説を書いた人・訳者など。R3・R4）
- 団体名義だけで公表されたもの（53条。D-018 案1：人が確かめるまで NEEDS_REVIEW）
- 使う版の刊行年（1968年以降の復刻・増補は、足された部分を人が確かめる）
- NDL の資料は、NDL の公開範囲の表示（「インターネット公開（保護期間満了）」だけ）
人の確認（reviewed_license）で使えるようにできるのは NEEDS_REVIEW だけ。
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from koten.license import (
    License,
    Person,
    ReviewError,
    apply_review,
    is_usable,
    judge_corporate_work,
    judge_edition,
    judge_ndl_access,
    judge_public_domain,
    stricter,
)

WORK_COLUMNS = [
    "ref_id",
    "source_class",
    "kind",
    "title",
    "volume",
    "edition",
    "pub_year",
    "first_pub_year",
    "publisher",
    "credited_as",
    "corporate",
    "lang",
    "source",
    "pid",
    "url",
    "ndl_access",
    "ndl_checked_at",
    "holder_allied",
    "tradition",
    "based_on",
    "credit",
    "reviewed_license",
    "reviewed_by",
    "reviewed_at",
    "review_note",
    "note",
]
PEOPLE_COLUMNS = [
    "ref_id",
    "seq",
    "name",
    "role",
    "death_year",
    "allied",
    "country",
    "death_source",
]

SOURCE_CLASSES = ("S1", "S2")  # S1 原文・S2 時代の解釈（S4 の今の本は台帳に載せない）
KINDS = {
    "original": "原文",
    "dictionary": "辞書",
    "kundoku": "訓読",
    "translation": "訳",
    "commentary": "注釈",
}
SOURCES = ("ndl", "standardebooks", "gutenberg", "aozora", "archive", "owned")
# 判定する人の役割（全員の没年を見る）
ROLES = {
    "author": "著者",
    "editor": "編者",
    "compiler": "編纂",
    "reviser": "校訂",
    "kundoku": "訓読",
    "kunten": "訓点",
    "annotator": "注釈",
    "lecturer": "講述",
    "recorder": "筆録",
    "supervisor": "監修",
    "preface": "序文",
    "commentary": "解説",
    "supplement": "増補",
    "translator": "訳者",
    "illustrator": "挿絵",
}
YES_NO = {"yes": True, "no": False, "unknown": None, "": None}
REF_ID = re.compile(r"^R-[A-Za-z0-9][A-Za-z0-9_.-]*$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class RefError(ValueError):
    pass


@dataclass
class RefPerson:
    ref_id: str
    seq: int
    name: str
    role: str
    death_year: int | None
    allied: bool | None
    country: str = ""
    death_source: str = ""

    def person(self) -> Person:
        return Person(self.name, self.death_year, ROLES[self.role], allied=self.allied)


@dataclass
class RefWork:
    ref_id: str
    source_class: str
    kind: str
    title: str
    pub_year: int
    credit: str
    volume: str = ""
    edition: str = ""
    first_pub_year: int | None = None
    publisher: str = ""
    credited_as: str = ""
    corporate: bool = False
    lang: str = "ja"
    source: str = ""
    pid: str = ""
    url: str = ""
    ndl_access: str = ""
    ndl_checked_at: str = ""
    holder_allied: bool | None = None
    tradition: str = ""
    based_on: list[str] = field(default_factory=list)
    reviewed_license: str = ""
    reviewed_by: str = ""
    reviewed_at: str = ""
    review_note: str = ""
    note: str = ""
    people: list[RefPerson] = field(default_factory=list)
    computed: License = License.UNKNOWN
    computed_reason: str = ""
    license: License = License.UNKNOWN

    @property
    def usable(self) -> bool:
        return is_usable(self.license)

    def label(self) -> str:
        parts = [f"『{self.title}』"]
        if self.volume:
            parts.append(self.volume)
        if self.edition:
            parts.append(self.edition)
        return " ".join(parts) + f"（{self.pub_year}）"


def judge(work: RefWork) -> tuple[License, str]:
    """1冊の判定（人の確認の前）。いちばん厳しい結果を採る。"""
    results: list[tuple[License, str]] = []
    if work.people:
        # R4 の「戦前・戦中の作品か」は台帳からは決めない（書かれた時期が分からないため）。
        # 連合国籍・国籍不明の人が1955年以降に亡くなっていれば、人の確認へ回る
        results.append(
            judge_public_domain(
                [p.person() for p in work.people], rights_holder_allied=work.holder_allied
            )
        )
    elif not work.corporate:
        results.append((License.UNKNOWN, "関わった人が台帳に無い"))
    if work.corporate:
        results.append(
            judge_corporate_work(
                work.credited_as or work.publisher, work.first_pub_year or work.pub_year
            )
        )
    results.append(judge_edition(work.pub_year))
    if work.source == "ndl":
        results.append(judge_ndl_access(work.ndl_access))
    return stricter(*results)


def read_registry(config_dir: Path) -> list[RefWork]:
    """台帳を読み、全部を確かめて判定する。1つでも問題があれば止める（RefError）。"""
    wrows = _read_csv(config_dir / "ref_works.csv")
    prows = _read_csv(config_dir / "ref_people.csv")
    problems: list[str] = []
    works: dict[str, RefWork] = {}
    broken: set[str] = set()  # 行に問題があった資料（人の行で「台帳に無い」と重ねて言わない）
    for i, r in enumerate(wrows, start=2):
        g = {c: (r.get(c) or "").strip() for c in WORK_COLUMNS}
        where = f"ref_works.csv {i}行目（{g['ref_id']}）"
        bad: list[str] = []
        if not REF_ID.match(g["ref_id"]):
            bad.append("ref_id は R- で始め、英数字と - _ . だけにする")
        elif g["ref_id"] in works:
            bad.append("ref_id が重複")
        if g["source_class"] not in SOURCE_CLASSES:
            bad.append(
                "source_class は S1（原文）か S2（時代の解釈）。今の本（S4）は台帳に載せない"
            )
        if g["kind"] not in KINDS:
            bad.append(f"kind は {'・'.join(KINDS)} のどれか")
        for col in ("title", "credit"):
            if not g[col]:
                bad.append(f"{col} が空")
        pub_year = _int(g["pub_year"])
        if pub_year is None:
            bad.append("pub_year（使う版の刊行年）は数字で書く")
        first = _int(g["first_pub_year"]) if g["first_pub_year"] else None
        if g["first_pub_year"] and first is None:
            bad.append("first_pub_year は数字で書く（分からなければ空）")
        elif first is not None and pub_year is not None and first > pub_year:
            bad.append("first_pub_year が pub_year より後")
        if g["corporate"] not in ("", "yes", "no"):
            bad.append("corporate は yes か no")
        if g["corporate"] == "yes" and not (g["credited_as"] or g["publisher"]):
            bad.append("団体名義なら credited_as（名義）を書く")
        if g["source"] not in SOURCES:
            bad.append(f"source は {'・'.join(SOURCES)} のどれか")
        if g["pid"] and g["source"] != "ndl":
            bad.append("pid（NDL の資料）があるなら source は ndl にする（公開範囲も判定する）")
        if g["source"] == "ndl":
            if not g["pid"]:
                bad.append("NDL の資料は pid が要る")
            if not DATE.match(g["ndl_checked_at"]):
                bad.append("NDL の資料は ndl_checked_at（公開範囲を見た日。YYYY-MM-DD）が要る")
        if g["holder_allied"] not in YES_NO:
            bad.append("holder_allied は yes・no・unknown（空は unknown）")
        if g["reviewed_license"]:
            if g["reviewed_license"] not in License.__members__:
                bad.append(f"reviewed_license {g['reviewed_license']} は license の値ではない")
            if not (g["reviewed_by"] and DATE.match(g["reviewed_at"]) and g["review_note"]):
                bad.append(
                    "人の確認には reviewed_by・reviewed_at（YYYY-MM-DD）・review_note が要る"
                )
        if bad:
            problems += [f"{where}：{b}" for b in bad]
            broken.add(g["ref_id"])
            continue
        works[g["ref_id"]] = RefWork(
            ref_id=g["ref_id"],
            source_class=g["source_class"],
            kind=g["kind"],
            title=g["title"],
            pub_year=pub_year,  # type: ignore[arg-type]
            credit=g["credit"],
            volume=g["volume"],
            edition=g["edition"],
            first_pub_year=first,
            publisher=g["publisher"],
            credited_as=g["credited_as"],
            corporate=g["corporate"] == "yes",
            lang=g["lang"] or "ja",
            source=g["source"],
            pid=g["pid"],
            url=g["url"],
            ndl_access=g["ndl_access"],
            ndl_checked_at=g["ndl_checked_at"],
            holder_allied=YES_NO[g["holder_allied"]],
            tradition=g["tradition"],
            based_on=[b.strip() for b in g["based_on"].split(";") if b.strip()],
            reviewed_license=g["reviewed_license"],
            reviewed_by=g["reviewed_by"],
            reviewed_at=g["reviewed_at"],
            review_note=g["review_note"],
            note=g["note"],
        )
    seen: set[tuple[str, int]] = set()
    for i, r in enumerate(prows, start=2):
        g = {c: (r.get(c) or "").strip() for c in PEOPLE_COLUMNS}
        where = f"ref_people.csv {i}行目（{g['ref_id']} {g['name']}）"
        bad = []
        seq = _int(g["seq"])
        if seq is None:
            bad.append("seq は数字")
        elif (g["ref_id"], seq) in seen:
            bad.append("ref_id と seq の組が重複")
        seen.add((g["ref_id"], seq or 0))
        if g["ref_id"] not in works and g["ref_id"] not in broken:
            bad.append("ref_works.csv に無い資料")
        if not g["name"]:
            bad.append("name が空（名前の分からない人は「（不明）」と書き、没年を空にする）")
        if g["role"] not in ROLES:
            bad.append(f"role は {'・'.join(ROLES)} のどれか")
        death = _int(g["death_year"]) if g["death_year"] else None
        if g["death_year"] and death is None:
            bad.append("death_year は数字（紀元前は -479 のように負の数。分からなければ空）")
        if death is not None and not g["death_source"]:
            bad.append("death_source（没年を確かめた資料。Web NDL Authorities の URL など）が要る")
        if g["allied"] not in YES_NO:
            bad.append("allied は yes・no・unknown")
        if bad:
            problems += [f"{where}：{b}" for b in bad]
            continue
        allied = YES_NO[g["allied"]]
        if not g["allied"] and g["country"] == "日本":
            allied = False
        if g["ref_id"] in works:
            works[g["ref_id"]].people.append(
                RefPerson(
                    ref_id=g["ref_id"],
                    seq=seq,  # type: ignore[arg-type]
                    name=g["name"],
                    role=g["role"],
                    death_year=death,
                    allied=allied,
                    country=g["country"],
                    death_source=g["death_source"],
                )
            )
    for w in works.values():
        w.people.sort(key=lambda p: p.seq)
        for b in w.based_on:
            if b not in works:
                problems.append(f"ref_works.csv（{w.ref_id}）：based_on の {b} が台帳に無い")
        w.computed, w.computed_reason = judge(w)
        try:
            w.license = apply_review(w.computed, w.reviewed_license or None)
        except ReviewError as e:
            problems.append(f"ref_works.csv（{w.ref_id}）：{e}（判定の理由：{w.computed_reason}）")
    if problems:
        raise RefError("資料の台帳に問題があります：\n" + "\n".join(f"- {p}" for p in problems))
    return list(works.values())


def _int(v: str) -> int | None:
    return int(v) if re.fullmatch(r"-?\d+", v or "") else None


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as f:  # Excel で保存した BOM 付きでも読む
        return list(csv.DictReader(f))
