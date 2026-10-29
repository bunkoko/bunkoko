"""照合に使う今の本の台帳（config/ref_books.csv。S4）。

書誌だけを持ち、本文の列は作らない（R2・R8）。照合の結果と、説明欄の「参考にした本」にだけ使う。
語りでは書名を出さない（D-027）。手に取れる紙の本（自分の本・図書館で借りた本）だけにする。
電子書籍（コピー防止つき）や有料の辞書サービスは、規約で写しを禁じていることが多いので使わない。
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

BOOK_COLUMNS = [
    "book_id",
    "title",
    "people",
    "publisher",
    "year",
    "edition",
    "isbn",
    "kind",
    "medium",
    "covers",
    "show_in_description",
    "affiliate_query",
    "note",
]
KINDS = {
    "translation": "今の訳",
    "annotation": "注釈書",
    "dictionary": "今の辞書",
    "kundoku": "書き下し",
    "other": "そのほか",
}
MEDIUMS = {"paper_owned": "自分の紙の本", "paper_library": "図書館で借りた紙の本"}
BOOK_ID = re.compile(r"^B-[A-Za-z0-9][A-Za-z0-9_.-]*$")
NOTE_MAX = 60  # メモは自分の言葉で短く（本の文を書く場所にしない）


class BooksError(ValueError):
    pass


@dataclass(frozen=True)
class Book:
    book_id: str
    title: str
    people: str
    publisher: str
    year: int
    kind: str
    medium: str
    edition: str = ""
    isbn: str = ""
    covers: str = ""
    show_in_description: bool = True
    affiliate_query: str = ""
    note: str = ""

    def label(self) -> str:
        who = f"{self.people}、" if self.people else ""
        return f"{who}『{self.title}』（{self.publisher}、{self.year}年）"


def read_books(config_dir: Path) -> dict[str, Book]:
    path = config_dir / "ref_books.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        extra = [c for c in reader.fieldnames or [] if c not in BOOK_COLUMNS]
        rows = list(reader)
    problems: list[str] = []
    if extra:
        # 本文などを書く列を足させない（表の形の上で、S4 の文を持てないようにする）
        problems.append(f"ref_books.csv に決まっていない列がある（{'、'.join(extra)}）")
    books: dict[str, Book] = {}
    for i, r in enumerate(rows, start=2):
        g = {c: (r.get(c) or "").strip() for c in BOOK_COLUMNS}
        where = f"ref_books.csv {i}行目（{g['book_id']}）"
        bad = []
        if not BOOK_ID.match(g["book_id"]):
            bad.append("book_id は B- で始め、英数字と - _ . だけにする")
        elif g["book_id"] in books:
            bad.append("book_id が重複")
        for col in ("title", "publisher"):
            if not g[col]:
                bad.append(f"{col} が空")
        if not re.fullmatch(r"\d{4}", g["year"]):
            bad.append("year は刊行年（4桁の数字）")
        if g["kind"] not in KINDS:
            bad.append(f"kind は {'・'.join(KINDS)} のどれか")
        if g["medium"] not in MEDIUMS:
            bad.append(
                "medium は paper_owned（自分の紙の本）か paper_library（図書館の紙の本）。"
                "電子書籍や有料の辞書サービスは使わない"
            )
        if g["show_in_description"] not in ("", "yes", "no"):
            bad.append("show_in_description は yes か no")
        if len(g["note"]) > NOTE_MAX:
            bad.append(f"note は{NOTE_MAX}字まで（本の文を書かない）")
        if bad:
            problems += [f"{where}：{b}" for b in bad]
            continue
        books[g["book_id"]] = Book(
            book_id=g["book_id"],
            title=g["title"],
            people=g["people"],
            publisher=g["publisher"],
            year=int(g["year"]),
            kind=g["kind"],
            medium=g["medium"],
            edition=g["edition"],
            isbn=g["isbn"],
            covers=g["covers"],
            show_in_description=g["show_in_description"] != "no",
            affiliate_query=g["affiliate_query"],
            note=g["note"],
        )
    if problems:
        raise BooksError(
            "照合に使う本の台帳に問題があります：\n" + "\n".join(f"- {p}" for p in problems)
        )
    return books
