"""青空文庫で没年・公表の年が空のところを、人が確かめた事実で埋める台帳（D-029）。

config/aozora_review.csv に1行ずつ書く。根拠・確かめた人・確かめた日の無い行は読み込みで止める。

- kind=death：名前のある人（紫式部・菅原孝標女など）の没年。作品CSVの没年が空か読めないときだけ
  使う。作品CSVに年があれば、そちらを使う（台帳で上書きはしない）。はっきりしないときは遅いほうの年
- kind=early：1900年より前に名前が出ている人（利用者の決まり）。year は名前が出ている年
  （1900年より前）。没年を調べなくても使ってよい
- kind=anonymous：作者不詳の作品の公表（成立）の年。著作権法52条で公表から数える。
  name は作品CSVの作者不詳の表記、title は作品CSVの作品名（同じ作品名の巻・章すべてに効く）

台帳が無くても、作品CSVの生年・初出・底本の年から決まる人・作品もある
（ingest/aozora.py の _blank_death）。

台帳は判定の入力を足すだけで、判定の決まり（license.py）は変えない。行を消せば、元の判定不能に戻る。
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

REVIEW_FILE = "aozora_review.csv"
REVIEW_COLUMNS = ["kind", "name", "title", "year", "basis", "checked_by", "checked_at", "note"]
KINDS = ("death", "early", "anonymous")
EARLY_BEFORE = 1900  # kind=early の year はこれより前
# 作者の名前が分からない作品（52条の無名の著作物）として扱ってよい表記
ANONYMOUS_NAMES = frozenset({"作者不詳", "作者未詳", "不詳", "よみ人しらず", "読人不知"})
DATE = re.compile(r"\d{4}-\d{2}-\d{2}$")
MAX_YEAR = 2100


class ReviewError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewedYear:
    year: int
    basis: str


@dataclass
class AozoraReview:
    deaths: dict[str, ReviewedYear] = field(default_factory=dict)
    early: dict[str, ReviewedYear] = field(default_factory=dict)  # 1900年より前に名前が出ている人
    anonymous: dict[tuple[str, str], ReviewedYear] = field(default_factory=dict)  # (名前, 作品名)


def _name(value: str) -> str:
    return re.sub(r"[\s　]+", "", value or "")


def read_review(path: Path) -> AozoraReview:
    out = AozoraReview()
    if not path.exists():
        return out
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = list(reader.fieldnames or [])
        if cols != REVIEW_COLUMNS:
            raise ReviewError(f"{path.name} の列は {','.join(REVIEW_COLUMNS)} にしてください")
        for line, row in enumerate(reader, start=2):
            where = f"{path.name} の {line} 行目"
            kind = (row.get("kind") or "").strip()
            if not kind and not any((v or "").strip() for v in row.values()):
                continue  # 空の行
            if kind not in KINDS:
                raise ReviewError(
                    f"{where}：kind は death・early・anonymous のどれかにしてください"
                )
            name, title = _name(row.get("name", "")), (row.get("title") or "").strip()
            year_text = (row.get("year") or "").strip()
            if not year_text.isdigit() or not 1 <= int(year_text) <= MAX_YEAR:
                raise ReviewError(f"{where}：year は西暦の年（数字だけ）で書いてください")
            for col in ("basis", "checked_by"):
                if not (row.get(col) or "").strip():
                    raise ReviewError(f"{where}：{col}（根拠・確かめた人）が空です")
            if not DATE.match((row.get("checked_at") or "").strip()):
                raise ReviewError(f"{where}：checked_at は 2026-11-01 のように書いてください")
            fact = ReviewedYear(int(year_text), row["basis"].strip())
            if not name:
                raise ReviewError(f"{where}：name が空です")
            if kind in ("death", "early"):
                if title:
                    raise ReviewError(f"{where}：{kind} の行に title は書きません（人ごとの年）")
                if name in ANONYMOUS_NAMES:
                    raise ReviewError(f"{where}：{name} は anonymous の行に書いてください")
                if name in out.deaths or name in out.early:
                    raise ReviewError(f"{where}：{name} の行が重なっています")
                if kind == "early" and fact.year >= EARLY_BEFORE:
                    raise ReviewError(f"{where}：early の year は {EARLY_BEFORE} 年より前にします")
                (out.deaths if kind == "death" else out.early)[name] = fact
            else:
                if name not in ANONYMOUS_NAMES:
                    raise ReviewError(
                        f"{where}：anonymous は作者不詳の作品だけです（name は "
                        f"{'・'.join(sorted(ANONYMOUS_NAMES))} のどれか）"
                    )
                if not title:
                    raise ReviewError(f"{where}：anonymous の行には title（作品名）が要ります")
                if (name, title) in out.anonymous:
                    raise ReviewError(f"{where}：{name}『{title}』の行が重なっています")
                out.anonymous[(name, title)] = fact
    return out
