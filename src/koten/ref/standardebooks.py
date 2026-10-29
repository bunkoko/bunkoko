"""Standard Ebooks の古い英訳を取り、一節ごとに分ける（M8a）。

- 取得は net.py を通す（1秒1件・控え）。GitHub の raw ファイル（content.opf と本文の xhtml）を
  1つずつ取る。git clone は使わない（CLAUDE.md）
- 取る前に、資料の台帳で使える（PD_JP など）と判定されていることを確かめる（R2）。
  Standard Ebooks が足した手入れは CC0。元の訳者・原著者の没年は台帳で判定する
- 分けた本文は HDD の clean/ref/<ref_id>.parquet に置き、`koten ref build` で SSD に入れる。
  status は imported（電子版から機械で分けた）。台本で引用するには、人が確かめて
  config/renderings.csv に verified として書く
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pyarrow as pa

from koten.net import Fetcher
from koten.parquet import data_files, read_rows, write_rows
from koten.ref.registry import RefWork
from koten.ref.texts import IMPORTED, RENDERING_COLUMNS, Rendering

XHTML = "{http://www.w3.org/1999/xhtml}"
EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"
OPF = "{http://www.idpf.org/2007/opf}"
SCHEMA = pa.schema([(c, pa.string()) for c in RENDERING_COLUMNS])


class EditionError(ValueError):
    pass


@dataclass
class Edition:
    ref_id: str
    repo: str
    canon: str
    split: str  # analects / meditations


@dataclass
class Unit:
    number: str  # canon の番号（1.1、2.p3 など）
    text: str
    locator: str


def editions(conf: dict[str, Any]) -> dict[str, Edition]:
    """sources.yaml の standardebooks.editions（ref_id ごとの repo・canon・split）。"""
    out = {}
    for ref_id, e in (conf.get("editions") or {}).items():
        if e.get("split") not in SPLITTERS:
            raise EditionError(f"{ref_id}：split は {'・'.join(SPLITTERS)} のどれか")
        out[ref_id] = Edition(ref_id, e["repo"], e["canon"], e["split"])
    return out


def _parse(data: bytes) -> ET.Element:
    if b"<!ENTITY" in data:
        raise EditionError("ENTITY 宣言のある XML は読まない")
    return ET.fromstring(data)


def spine_texts(opf: bytes) -> list[str]:
    """content.opf の読む順（spine）の本文ファイル（text/….xhtml）。"""
    root = _parse(opf)
    hrefs = {i.get("id"): i.get("href") for i in root.iter(f"{OPF}item")}
    return [
        hrefs[r.get("idref")]
        for r in root.iter(f"{OPF}itemref")
        if hrefs.get(r.get("idref"), "").startswith("text/")
    ]


def _text(el: ET.Element) -> str:
    """要素の文字だけ。注の番号（noteref）は除き、空白をまとめる。"""

    def walk(e: ET.Element) -> str:
        if e.tag == f"{XHTML}a" and "noteref" in (e.get(EPUB_TYPE) or ""):
            return e.tail or ""
        inner = (e.text or "") + "".join(walk(c) for c in e)
        return inner + (e.tail or "") if e is not el else inner

    s = walk(el).replace("⁠", "")  # 語をつなぐ見えない文字（⁠—）
    return " ".join(s.split())


def split_analects(files: list[tuple[str, bytes]]) -> list[Unit]:
    """Legge の論語：chapter-篇.xhtml の section#chapter-篇-章 を1つの一節にする（lunyu:篇.章）。"""
    units = []
    for href, data in files:
        root = _parse(data)
        for sec in root.iter(f"{XHTML}section"):
            m = re.fullmatch(r"chapter-(\d+)-(\d+)", sec.get("id") or "")
            if not m:
                continue
            paras = [_text(p) for p in sec.iter(f"{XHTML}p")]
            text = "\n".join(p for p in paras if p)
            if text:
                units.append(Unit(f"{m.group(1)}.{m.group(2)}", text, f"{href}#{sec.get('id')}"))
    return units


def split_meditations(files: list[tuple[str, bytes]]) -> list[Unit]:
    """Long の自省録：book-巻.xhtml を段落ごとに分ける（meditations:巻.p段落）。

    Standard Ebooks 版には節の番号が無く、段落と節は一致しない（第11巻の「九つの心得」は
    1つの節が10段落に分かれるなど）。節の番号を作らないよう、段落の順で番号を振る。
    引用（blockquote）と、それをつなぐ短い段落（「And—」など）は前の段落にまとめる。
    巻の終わりの場所の書き添え（「This in Carnuntum.」など）は一節にしない。
    """
    units = []
    for href, data in files:
        m = re.search(r"book-(\d+)\.xhtml$", href)
        if not m:
            continue
        book = m.group(1)
        root = _parse(data)
        section = next(root.iter(f"{XHTML}section"), None)
        if section is None:
            continue
        blocks = [
            (c.tag, _text(c)) for c in section if c.tag in (f"{XHTML}p", f"{XHTML}blockquote")
        ]
        if blocks and len(blocks[-1][1]) < 40 and re.match(r"(This|Among)\b", blocks[-1][1]):
            blocks.pop()  # 巻の終わりの場所の書き添え
        paras: list[str] = []
        joined = False
        for tag, text in blocks:
            if not text:
                continue
            follows = tag == f"{XHTML}blockquote" or joined
            short_link = paras and len(text) < 50 and re.match(r"And\b", text)
            if paras and (follows or short_link):
                paras[-1] += "\n" + text
            else:
                paras.append(text)
            joined = tag == f"{XHTML}p" and text.endswith(("—", ":"))  # 次に続く段落
        for i, text in enumerate(paras, start=1):
            units.append(Unit(f"{book}.p{i}", text, f"{href} 第{i}段落"))
    return units


SPLITTERS: dict[str, Callable[[list[tuple[str, bytes]]], list[Unit]]] = {
    "analects": split_analects,
    "meditations": split_meditations,
}
FILES = {"analects": r"text/chapter-\d+\.xhtml", "meditations": r"text/book-\d+\.xhtml"}


@dataclass
class ImportResult:
    ref_id: str
    files: int
    fetched: int
    units: int
    path: Path


def import_edition(
    hdd_root: Path,
    conf: dict[str, Any],
    edition: Edition,
    work: RefWork,
    fetcher: Fetcher,
    *,
    log: Callable[[str], None] = lambda _: None,
) -> ImportResult:
    if not work.usable:
        # R2：使えない資料の本文は取らない
        raise EditionError(
            f"{work.ref_id} は {work.license}（{work.computed_reason}）。本文を取れない"
        )
    if work.source != "standardebooks":
        raise EditionError(f"{work.ref_id} の source が standardebooks ではない（{work.source}）")
    base = f"{conf['raw_base'].rstrip('/')}/{edition.repo}/{conf.get('branch', 'master')}/src/epub/"
    lic = str(work.license)
    opf, _ = fetcher.fetch(base + "content.opf", f"{edition.repo}/content.opf", license=lic)
    hrefs = [h for h in spine_texts(opf.read_bytes()) if re.fullmatch(FILES[edition.split], h)]
    if not hrefs:
        raise EditionError(f"{edition.repo} の content.opf に本文のファイルが見つからない")
    files: list[tuple[str, bytes]] = []
    fetched = 0
    for href in hrefs:
        path, got = fetcher.fetch(base + href, f"{edition.repo}/{href}", license=lic)
        fetched += got
        if got:
            log(f"取得：{href}")
        files.append((href, path.read_bytes()))
    units = SPLITTERS[edition.split](files)
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
            note=f"Standard Ebooks（{edition.repo}）から機械で分けた",
        ).row()
        for u in units
    ]
    out = imported_path(hdd_root, work.ref_id)
    write_rows(out, [{c: r[c] for c in RENDERING_COLUMNS} for r in rows], SCHEMA)
    return ImportResult(work.ref_id, len(hrefs), fetched, len(units), out)


def imported_path(hdd_root: Path, ref_id: str) -> Path:
    return hdd_root / "clean" / "ref" / f"{ref_id}.parquet"


def load_imported(hdd_root: Path) -> list[Rendering]:
    out = []
    for path in data_files(hdd_root / "clean" / "ref", "*.parquet"):
        for r in read_rows(path):
            out.append(Rendering(**{c: r.get(c) or "" for c in RENDERING_COLUMNS}))
    return out


def forget_unusable(
    hdd_root: Path,
    works: dict[str, RefWork],
    eds: dict[str, Edition],
    restricted: set[str] | None = None,
) -> list[str]:
    """台帳で使えない（台帳に無い）資料の本文と画像を HDD から消す（R2）。

    - 取り込んだ本文（clean/ref/<ref_id>.parquet）
    - Standard Ebooks の原本（raw/standardebooks/<repo>）。使える版が指していない repo は全部
    - 国会図書館のページの画像（raw/ndl/<PID>/images）。使える NDL の資料が指していない PID は全部
    restricted は、台帳の外の事情（マニフェストの公開範囲）で使えない ref_id。
    """
    restricted = restricted or set()

    def usable(ref_id: str) -> bool:
        w = works.get(ref_id)
        return w is not None and w.usable and ref_id not in restricted

    def why(ref_id: str) -> str:
        w = works.get(ref_id)
        if w is None:
            return "台帳に無い"
        return "公開範囲" if ref_id in restricted else str(w.license)

    removed = []
    for path in data_files(hdd_root / "clean" / "ref", "*.parquet"):
        if not usable(path.stem):
            path.unlink()
            removed.append(f"{path.stem} の取り込んだ本文（{why(path.stem)}）")
    keep_repos = {ed.repo for rid, ed in eds.items() if usable(rid)}
    raw_se = hdd_root / "raw" / "standardebooks"
    for d in sorted(raw_se.iterdir()) if raw_se.exists() else []:
        if d.is_dir() and not d.name.startswith(".") and d.name not in keep_repos:
            shutil.rmtree(d)
            owner = next((rid for rid, ed in eds.items() if ed.repo == d.name), "")
            removed.append(
                f"Standard Ebooks の原本 {d.name}（{why(owner) if owner else '台帳に無い'}）"
            )
    keep_pids = {w.pid for w in works.values() if w.source == "ndl" and w.pid and usable(w.ref_id)}
    raw_ndl = hdd_root / "raw" / "ndl"
    for d in sorted(raw_ndl.iterdir()) if raw_ndl.exists() else []:
        images = d / "images"
        if d.is_dir() and d.name not in keep_pids and images.exists():
            shutil.rmtree(images)
            owner = next((w.ref_id for w in works.values() if w.pid == d.name), "")
            removed.append(
                f"国会図書館の画像 PID {d.name}（{why(owner) if owner else '台帳に無い'}）"
            )
    return removed
