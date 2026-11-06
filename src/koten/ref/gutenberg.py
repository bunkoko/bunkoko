"""Project Gutenberg の古い英訳を、ミラーから取り、一節ごとに分ける（M8b。D-037）。

- 本体のサイト（www.gutenberg.org）からは自動で取らない（robot の方針。SPEC §6）。オーナーが
  sources.yaml の gutenberg.mirror に、公式のミラーの一覧から選んだミラーを書く（要確認）
- 取得は net.py を通す（1秒1件・控え）。1冊ずつ、プレーンテキストのファイルを1つ取る
- 取る前に、資料の台帳で使える（PD_JP など）と判定されていることを確かめる（R2）
- 本文の前後の「*** START OF … PROJECT GUTENBERG EBOOK」「*** END OF …」の外側（ライセンス文など）
  は捨てる
- 「Project Gutenberg」の名前は、規約を確かめる（D-004）まで画面・説明欄に出さない。出典は台帳の
  credit（「カソーボンの英訳（1634）」など）だけを使う
- 分けた本文は HDD の clean/ref/<ref_id>.parquet に置き、`koten ref build` で SSD に入れる。
  status は imported。節の番号はその版のもの（標準の番号と合うかは人が確かめる）。p の付いた
  段落の番号（Standard Ebooks 版の番号）は作らない（別の版の段落と取り違えないため）
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from koten.net import Fetcher
from koten.parquet import write_rows
from koten.ref.registry import RefWork
from koten.ref.standardebooks import SCHEMA, ImportResult, Unit, imported_path
from koten.ref.texts import IMPORTED, RENDERING_COLUMNS, Rendering

START = re.compile(r"^\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*$", re.I | re.M)
END = re.compile(r"^\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*$", re.I | re.M)
# 本体のサイト。robot の方針で、プログラムからは取らない
MAIN_HOSTS = {"www.gutenberg.org", "gutenberg.org"}
ORDINALS = {
    w: i
    for i, w in enumerate(
        "FIRST SECOND THIRD FOURTH FIFTH SIXTH SEVENTH EIGHTH NINTH TENTH ELEVENTH TWELFTH".split(),
        start=1,
    )
}
ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


class GutenbergError(ValueError):
    pass


@dataclass
class GutenbergEdition:
    ref_id: str
    ebook: int
    file: str  # ミラーのファイル名（2680-0.txt など）
    canon: str
    section_start: str  # 節の始まりの正規表現（グループ1が節の番号。ローマ数字か数字）
    book_heading: str = ""  # 巻の見出しの正規表現（グループ1が巻の番号か FIRST などの語）


def editions(conf: dict[str, Any]) -> dict[str, GutenbergEdition]:
    """sources.yaml の gutenberg.editions。"""
    out = {}
    for ref_id, e in (conf.get("editions") or {}).items():
        for key in ("ebook", "file", "canon", "section_start"):
            if not e.get(key):
                raise GutenbergError(f"{ref_id}：gutenberg.editions に {key} が要る")
        for key in ("section_start", "book_heading"):
            if e.get(key):
                try:
                    re.compile(e[key])
                except re.error as err:
                    raise GutenbergError(f"{ref_id}：{key} の正規表現が読めない（{err}）") from err
        out[ref_id] = GutenbergEdition(
            ref_id,
            int(e["ebook"]),
            str(e["file"]),
            str(e["canon"]),
            str(e["section_start"]),
            str(e.get("book_heading") or ""),
        )
    return out


def mirror_url(mirror: str, ebook: int, file: str) -> str:
    """ミラーのファイルの URL。

    /2/6/8/2680/2680-0.txt の形（番号の最後の桁を除いた各桁のフォルダ。1桁の番号は /0/）。
    """
    if not mirror:
        raise GutenbergError(
            "sources.yaml の gutenberg.mirror に、公式のミラーの一覧から選んだミラーの URL を書く"
            "（本体のサイトからは取らない）"
        )
    host = urlparse(mirror).hostname or ""
    if host in MAIN_HOSTS:
        raise GutenbergError(
            f"{host} は本体のサイトです。プログラムからはミラーを使う（Gutenberg の robot の方針）"
        )
    digits = str(ebook)
    folders = "/".join(digits[:-1]) if len(digits) > 1 else "0"
    return f"{mirror.rstrip('/')}/{folders}/{ebook}/{file}"


def body(text: str) -> str:
    """前後のライセンス文を捨てた本文。印が無ければ止める（ライセンス文を本文として持たない）。"""
    s, e = START.search(text), END.search(text)
    if not s or not e or e.start() <= s.end():
        raise GutenbergError("本文の始まりと終わりの印（*** START / *** END）が見つからない")
    return text[s.end() : e.start()].strip("\n")


def _number(word: str) -> int | None:
    w = word.strip().upper()
    if w.isdigit():
        return int(w)
    if w in ORDINALS:
        return ORDINALS[w]
    if re.fullmatch(r"[IVXLC]+", w):
        total = 0
        for i, ch in enumerate(w):
            v = ROMAN[ch]
            total += -v if i + 1 < len(w) and ROMAN[w[i + 1]] > v else v
        return total
    return None


def split_sections(text: str, ed: GutenbergEdition) -> list[Unit]:
    """巻の見出しと節の始まりで分ける（巻.節、巻が無ければ節）。番号の読めない所は捨てる。"""
    book_re = re.compile(ed.book_heading, re.M) if ed.book_heading else None
    sec_re = re.compile(ed.section_start)
    units: list[Unit] = []
    book: int | None = None if book_re else 0
    cur: tuple[str, list[str]] | None = None

    def close() -> None:
        nonlocal cur
        if cur is not None:
            t = " ".join(" ".join(cur[1]).split())
            if t:
                units.append(Unit(cur[0], t, f"Gutenberg #{ed.ebook} {cur[0]}"))
        cur = None

    for para in re.split(r"\n\s*\n", text):
        line = " ".join(para.split())
        if not line:
            continue
        if book_re and (m := book_re.match(line)):
            close()
            book = _number(m.group(1))
            continue
        m = sec_re.match(line)
        if m and book is not None and (n := _number(m.group(1))) is not None:
            close()
            number = f"{book}.{n}" if book_re else str(n)
            cur = (number, [line[m.end() :]])
        elif cur is not None:
            cur[1].append(line)
    close()
    seen: set[str] = set()
    out = []
    for u in units:  # 同じ番号が2度出たら（目次など）、最初のものを残す
        if u.number not in seen:
            seen.add(u.number)
            out.append(u)
    return out


def import_edition(
    hdd_root: Path,
    conf: dict[str, Any],
    edition: GutenbergEdition,
    work: RefWork,
    fetcher: Fetcher,
    *,
    log: Callable[[str], None] = lambda _: None,
) -> ImportResult:
    if not work.usable:
        raise GutenbergError(
            f"{work.ref_id} は {work.license}（{work.computed_reason}）。本文を取れない"
        )
    if work.source != "gutenberg":
        raise GutenbergError(f"{work.ref_id} の source が gutenberg ではない（{work.source}）")
    url = mirror_url(str(conf.get("mirror") or ""), edition.ebook, edition.file)
    path, got = fetcher.fetch(url, f"{edition.ebook}/{edition.file}", license=str(work.license))
    if got:
        log(f"取得：{url}")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    units = split_sections(body(text), edition)
    if not units:
        raise GutenbergError(f"{edition.ebook}：節の始まり（section_start）に合う所が無い")
    slug = work.ref_id.removeprefix("R-")
    rows = [
        Rendering(
            rendering_id=f"I-{slug}-{u.number}",
            passage_key=f"{edition.canon}:{u.number}",
            ref_id=work.ref_id,
            role="translation",
            lang=work.lang,
            text=u.text,
            locator=u.locator,
            status=IMPORTED,
            note="電子テキストから機械で分けた。節の番号はこの版のもの（標準の番号と合うかは人が確かめる）",
        ).row()
        for u in units
    ]
    out = imported_path(hdd_root, work.ref_id)
    write_rows(out, [{c: r[c] for c in RENDERING_COLUMNS} for r in rows], SCHEMA)
    return ImportResult(work.ref_id, 1, int(got), len(units), out)


def forget_unusable_raw(
    hdd_root: Path, works: dict[str, RefWork], eds: dict[str, GutenbergEdition]
) -> list[str]:
    """台帳で使えない（台帳に無い）版の原本（raw/gutenberg/<番号>）を HDD から消す（R2）。"""
    keep = {
        str(ed.ebook) for rid, ed in eds.items() if (w := works.get(rid)) is not None and w.usable
    }
    removed = []
    raw = hdd_root / "raw" / "gutenberg"
    for d in sorted(raw.iterdir()) if raw.exists() else []:
        if d.is_dir() and not d.name.startswith(".") and d.name not in keep:
            shutil.rmtree(d)
            removed.append(f"Gutenberg の原本 {d.name}（台帳で使えない）")
    return removed
