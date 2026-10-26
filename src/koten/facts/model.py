"""事実の1行と、その「項目」（台本に差し込む名前・年・数）。

事実は確認済みの1文（text_ja）と、項目（slots）を持つ。項目は、語りで言い換えるときに
LLM が直接書いてはいけないもの（人名・地名・作品名・年・年齢・数）で、台本では
{{fact:事実ID.項目名}} の記号で DB から差し込む（SPEC R6）。

項目名の頭で種類が決まる（後ろの数字は区別用。例：person2）。
- year・month・age：数字だけ。差し込むと「1906年」「6月」「38歳」
- number：数字を含む文字列（単位ごと。例「100年あまり」）
- person・place・org：そのまま
- title：『』を付ける。periodical（雑誌・新聞）：「」を付ける
year には「year_wareki」で和暦（例：明治39年）も差し込める（言えない年なら解決しない）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from koten.facts.dates import era_year

SLOT_TYPES = ("year", "month", "age", "number", "person", "place", "org", "title", "periodical")
NUMERIC = ("year", "month", "age")
SLOT_NAME = re.compile(r"^(year|month|age|number|person|place|org|title|periodical)\d*$")
STATUSES_USABLE = ("auto", "verified")
SCOPES = ("person", "work", "original_work", "translation", "era")
SENSITIVE = ("none", "health", "religion", "politics")
DIGITS = re.compile(r"[0-9０-９]+")


def slot_type(name: str) -> str:
    m = SLOT_NAME.match(name)
    if not m:
        raise ValueError(f"項目名 {name} は使えない（{'・'.join(SLOT_TYPES)} に数字を付けた名前）")
    return m.group(1)


def render_slot(name: str, value: Any) -> str:
    kind = slot_type(name)
    v = str(value).strip()
    if kind == "year":
        return f"{v}年"
    if kind == "month":
        return f"{v}月"
    if kind == "age":
        return f"{v}歳"
    if kind == "title":
        return f"『{v}』"
    if kind == "periodical":
        return f"「{v}」"
    return v


@dataclass
class Fact:
    fact_id: str
    subject_type: str  # person / work / era
    subject_id: str  # 代表の人物ID（aozora:person:000280）か作品ID（aozora:001704）。era は空
    scope: str  # person / work / original_work / translation / era
    kind: str  # lifespan / first_pub / translation / original_title / life_event / work_origin …
    text_ja: str  # 確認済みの1文（です・ます、「。」で終わる）
    slots: dict[str, Any] = field(default_factory=dict)
    date: str = ""  # YYYY / YYYY-MM / YYYY-MM-DD（分かる細かさまで）
    origin: str = "curated"  # auto / curated
    status: str = "verified"  # auto / verified / needs_review / disputed / rejected
    sensitive: str = "none"
    interest: int = 2  # 1〜3（話の面白さ。人が付ける）
    checked_by: str = ""
    checked_at: str = ""
    links: list[str] = field(default_factory=list)  # ほかに結び付ける作品・人物の ID

    @property
    def usable(self) -> bool:
        return self.status in STATUSES_USABLE and self.sensitive == "none"

    def slot_text(self, name: str) -> str:
        """{{fact:ID.name}} に差し込む文字列。言えなければ KeyError。"""
        if name.endswith("_wareki"):
            base = name.removesuffix("_wareki")
            if base not in self.slots or not base.startswith("year"):
                raise KeyError(f"{self.fact_id} に年の項目 {base} は無い")
            y = int(self.slots[base])
            m = d = None
            dy, dm, dd = _date_parts(self.date)
            if base == "year" and dy == y:
                m, d = dm, dd  # date の細かさまで使う（日まで分かれば改元の年でも決まる）
            wareki = era_year(y, m, d)
            if wareki is None:
                raise KeyError(f"{self.fact_id} の {base} に和暦は付けない（改暦前か改元の年）")
            return wareki
        if name not in self.slots:
            raise KeyError(f"{self.fact_id} に項目 {name} は無い")
        return render_slot(name, self.slots[name])

    def problems(self) -> list[str]:
        """読み込む前に確かめること（1つでもあれば読み込まない）。"""
        out = []
        if self.scope not in SCOPES:
            out.append(f"scope {self.scope} は使えない（{'・'.join(SCOPES)}）")
        if self.sensitive not in SENSITIVE:
            out.append(f"sensitive {self.sensitive} は使えない（{'・'.join(SENSITIVE)}）")
        if self.kind in ("death_cause", "cause_of_death", "suicide"):
            out.append("死因の事実は使わない")
        text = self.text_ja.strip()
        if not text.endswith("。"):
            out.append("text_ja は「。」で終える")
        if len(text) > 100:
            out.append(f"text_ja が長い（{len(text)}字。100字以内の1文）")
        periodicals = {str(v) for k, v in self.slots.items() if k.startswith("periodical")}
        for m in re.finditer(r"「([^「」]*)」", text):
            if m.group(1) not in periodicals:
                out.append("text_ja に「」の引用を入れない（雑誌・新聞名は periodical の項目に）")
                break
        covered = ""
        for name, value in self.slots.items():
            try:
                kind = slot_type(name)
            except ValueError as e:
                out.append(str(e))
                continue
            v = str(value).strip()
            if not v:
                out.append(f"項目 {name} が空")
                continue
            if kind in NUMERIC and not v.isdigit():
                out.append(f"項目 {name} は数字だけにする（{v}）")
            if kind == "number" and not DIGITS.search(v):
                out.append(f"項目 {name} に数字が無い（{v}）")
            if v not in text:
                out.append(f"項目 {name} の値「{v}」が text_ja に無い")
            covered += v + "\n"
        for m in DIGITS.finditer(text):
            if m.group(0) not in covered:
                out.append(f"text_ja の数字「{m.group(0)}」が項目に無い（年や数は項目にする）")
        if self.date and not re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", self.date):
            out.append(f"date は YYYY・YYYY-MM・YYYY-MM-DD（{self.date}）")
        if self.date and "year" in self.slots and not self.date.startswith(str(self.slots["year"])):
            out.append(f"date（{self.date}）と項目 year（{self.slots['year']}）が合わない")
        if self.origin == "curated" and self.status == "verified" and not self.checked_by:
            out.append("確認した人（checked_by）が無い")
        if not 1 <= int(self.interest or 0) <= 3:
            out.append("interest は1〜3")
        return out

    def to_row(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "scope": self.scope,
            "kind": self.kind,
            "text_ja": self.text_ja,
            "slots": json.dumps(self.slots, ensure_ascii=False),
            "date": self.date,
            "origin": self.origin,
            "status": self.status,
            "sensitive": self.sensitive,
            "interest": int(self.interest),
            "checked_by": self.checked_by,
            "checked_at": self.checked_at,
            "links": json.dumps(self.links, ensure_ascii=False),
        }

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> Fact:
        slots = r.get("slots") or "{}"
        links = r.get("links") or "[]"
        return cls(
            fact_id=r["fact_id"],
            subject_type=r["subject_type"],
            subject_id=r["subject_id"] or "",
            scope=r["scope"],
            kind=r["kind"],
            text_ja=r["text_ja"],
            slots=json.loads(slots) if isinstance(slots, str) else dict(slots),
            date=r.get("date") or "",
            origin=r.get("origin") or "curated",
            status=r.get("status") or "verified",
            sensitive=r.get("sensitive") or "none",
            interest=int(r.get("interest") or 2),
            checked_by=r.get("checked_by") or "",
            checked_at=r.get("checked_at") or "",
            links=json.loads(links) if isinstance(links, str) else list(links),
        )


def _date_parts(d: str) -> tuple[int | None, int | None, int | None]:
    parts = [int(x) for x in d.split("-")] if d else []
    parts += [None] * (3 - len(parts))
    return parts[0], parts[1], parts[2]  # type: ignore[return-value]
