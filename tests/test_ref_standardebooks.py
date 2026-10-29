"""古い英訳（Standard Ebooks）の取得と、一節への分け方（M8a）。本文は架空の短い英文。"""

# ruff: noqa: E501

import shutil

import httpx
import pytest
from typer.testing import CliRunner

from koten.cli import app
from koten.config import DEFAULT_CONFIG_DIR
from koten.db import connect
from koten.net import Fetcher
from koten.ref.registry import read_registry
from koten.ref.standardebooks import (
    EditionError,
    editions,
    forget_unusable,
    import_edition,
    imported_path,
    load_imported,
    split_analects,
    split_meditations,
)
from koten.ref.store import build_refs, renderings_for_passage
from tests.ref_fixture import person, work, write, write_texts

HEAD = '<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>x</title></head><body>'
TAIL = "</body></html>"

CHAPTER_1 = (
    HEAD
    + '<section id="chapter-1" epub:type="chapter"><h2>Book I</h2>'
    + '<section id="chapter-1-1" epub:type="z3998:subchapter"><h3>I</h3>'
    + "<p>The Teacher said, “Is it not pleasant to learn?</p><p>“Is it not delightful to have friends?”</p></section>"
    + '<section id="chapter-1-2" epub:type="z3998:subchapter"><h3>II</h3>'
    + '<p>The pupil said, “Root first.<a href="endnotes.xhtml#note-1" id="noteref-1" epub:type="noteref">1</a>⁠—so it grows.”</p></section>'
    + "</section>"
    + TAIL
)
CHAPTER_2 = (
    HEAD
    + '<section id="chapter-2" epub:type="chapter"><section id="chapter-2-1" epub:type="z3998:subchapter"><h3>I</h3>'
    + "<p>The Teacher said, “Govern by virtue.”</p></section></section>"
    + TAIL
)
BOOK_1 = (
    HEAD
    + '<section id="book-1" epub:type="chapter"><h2>Book I</h2>'
    + "<p>From my grandfather I learned good temper.</p>"
    + "<p>The poets say this:</p>"
    + "<blockquote><p>Me and my children if the gods neglect.</p></blockquote>"
    + "<p>And again—</p>"
    + "<blockquote><p>We must not chafe.</p></blockquote>"
    + "<p>And other things of the same kind.</p>"
    + "<p>Begin the morning by saying to thyself that all is opinion and that the day is short.</p>"
    + "<p>Among the Quadi at the Granua.</p>"
    + "</section>"
    + TAIL
)
OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest>
<item href="text/titlepage.xhtml" id="titlepage.xhtml" media-type="application/xhtml+xml"/>
<item href="text/chapter-1.xhtml" id="chapter-1.xhtml" media-type="application/xhtml+xml"/>
<item href="text/chapter-2.xhtml" id="chapter-2.xhtml" media-type="application/xhtml+xml"/>
<item href="text/endnotes.xhtml" id="endnotes.xhtml" media-type="application/xhtml+xml"/>
</manifest><spine><itemref idref="titlepage.xhtml"/><itemref idref="chapter-2.xhtml"/><itemref idref="chapter-1.xhtml"/><itemref idref="endnotes.xhtml"/></spine></package>"""

CONF = {
    "raw_base": "https://raw.example/standardebooks/",
    "branch": "master",
    "editions": {
        "R-legge-analects": {
            "repo": "confucius_analects_james-legge",
            "canon": "lunyu",
            "split": "analects",
        }
    },
}


def test_split_analects():
    units = split_analects([("text/chapter-1.xhtml", CHAPTER_1.encode())])
    assert [u.number for u in units] == ["1.1", "1.2"]
    assert (
        units[0].text
        == "The Teacher said, “Is it not pleasant to learn?\n“Is it not delightful to have friends?”"
    )
    assert (
        units[1].text == "The pupil said, “Root first.—so it grows.”"
    )  # 注の番号と見えない文字を除く
    assert units[1].locator == "text/chapter-1.xhtml#chapter-1-2"


def test_split_meditations_keeps_paragraph_order_not_sections():
    units = split_meditations([("text/book-1.xhtml", BOOK_1.encode())])
    assert [u.number for u in units] == ["1.p1", "1.p2", "1.p3"]
    assert units[0].text == "From my grandfather I learned good temper."
    # 引用と、それをつなぐ短い段落は前の段落にまとめる
    assert units[1].text.split("\n") == [
        "The poets say this:",
        "Me and my children if the gods neglect.",
        "And again—",
        "We must not chafe.",
        "And other things of the same kind.",
    ]
    assert units[2].text.startswith("Begin the morning")  # 巻の終わりの場所の書き添えは除く
    assert units[2].locator == "text/book-1.xhtml 第3段落"


def test_entities_are_refused():
    bad = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><html xmlns="http://www.w3.org/1999/xhtml"/>'
    with pytest.raises(EditionError, match="ENTITY"):
        split_analects([("text/chapter-1.xhtml", bad)])


class Server:
    def __init__(self):
        self.calls = []

    def __call__(self, request):
        url = str(request.url)
        self.calls.append(url)
        base = "https://raw.example/standardebooks/confucius_analects_james-legge/master/src/epub/"
        files = {
            "content.opf": OPF,
            "text/chapter-1.xhtml": CHAPTER_1,
            "text/chapter-2.xhtml": CHAPTER_2,
        }
        rel = url.removeprefix(base)
        if rel in files:
            return httpx.Response(200, content=files[rel].encode())
        return httpx.Response(404)


def fetcher(raw_dir, server):
    client = httpx.Client(transport=httpx.MockTransport(server))
    return Fetcher(raw_dir, user_agent="test-agent", per_host_per_sec=1000, client=client)


@pytest.fixture
def conf(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    shutil.copy(DEFAULT_CONFIG_DIR / "canon.yaml", d / "canon.yaml")
    write(
        d,
        [
            work(
                "R-legge-analects",
                kind="translation",
                title="The Analects",
                lang="en",
                source="standardebooks",
                pub_year="1893",
                credit="James Legge 訳『論語』（Standard Ebooks 版）",
            )
        ],
        [
            person("R-legge-analects", 1, "孔子", "author", -479, country=""),
            person(
                "R-legge-analects",
                2,
                "英国の訳者",
                "translator",
                1897,
                country="英国",
                allied="yes",
            ),
        ],
    )
    write_texts(d)
    return d


def test_import_and_build(conf, tmp_path):
    hdd = tmp_path / "hdd"
    works = {w.ref_id: w for w in read_registry(conf)}
    server = Server()
    raw = hdd / "raw" / "standardebooks"
    r = import_edition(
        hdd,
        CONF,
        editions(CONF)["R-legge-analects"],
        works["R-legge-analects"],
        fetcher(raw, server),
    )
    assert (r.files, r.fetched, r.units) == (2, 2, 3)
    assert len(server.calls) == 3  # content.opf と本文2つ。題扉や注は取らない
    # 2回目は控えを使い、取りに行かない
    server2 = Server()
    r2 = import_edition(
        hdd,
        CONF,
        editions(CONF)["R-legge-analects"],
        works["R-legge-analects"],
        fetcher(raw, server2),
    )
    assert r2.fetched == 0 and server2.calls == []

    imported = load_imported(hdd)
    assert [(i.rendering_id, i.passage_key, i.status) for i in imported] == [
        ("I-legge-analects-2.1", "lunyu:2.1", "imported"),  # 読む順（spine）のまま
        ("I-legge-analects-1.1", "lunyu:1.1", "imported"),
        ("I-legge-analects-1.2", "lunyu:1.2", "imported"),
    ]
    conn = connect(tmp_path / "ssd")
    b = build_refs(conn, conf, imported)
    assert (b.renderings, b.imported) == (0, 3)
    # 機械で分けたもの（imported）は、台本の引用には使わない
    assert renderings_for_passage(conn, "lunyu:1.1") == []
    got = renderings_for_passage(conn, "lunyu:1.1", verified_only=False)
    assert got[0]["text"].startswith("The Teacher said") and got[0]["role"] == "translation"


def test_unusable_work_is_not_fetched_and_is_forgotten(conf, tmp_path):
    hdd = tmp_path / "hdd"
    works = {w.ref_id: w for w in read_registry(conf)}
    raw = hdd / "raw" / "standardebooks"
    import_edition(
        hdd,
        CONF,
        editions(CONF)["R-legge-analects"],
        works["R-legge-analects"],
        fetcher(raw, Server()),
    )
    assert imported_path(hdd, "R-legge-analects").exists()
    # 訳者の国籍が分からず、1956年没と書き直した → 人の確認へ（使えない）
    write(
        conf,
        [
            work(
                "R-legge-analects",
                kind="translation",
                title="The Analects",
                lang="en",
                source="standardebooks",
                pub_year="1893",
                credit="x",
            )
        ],
        [person("R-legge-analects", 1, "訳者", "translator", 1956, country="", allied="")],
    )
    works = {w.ref_id: w for w in read_registry(conf)}
    server = Server()
    with pytest.raises(EditionError, match="本文を取れない"):
        import_edition(
            hdd,
            CONF,
            editions(CONF)["R-legge-analects"],
            works["R-legge-analects"],
            fetcher(raw, server),
        )
    assert server.calls == []
    removed = forget_unusable(hdd, works, editions(CONF))
    assert removed == ["R-legge-analects（NEEDS_REVIEW）"]
    assert not imported_path(hdd, "R-legge-analects").exists()
    assert not (raw / "confucius_analects_james-legge").exists()
    assert load_imported(hdd) == []


def test_ref_fetch_command(conf, tmp_path, monkeypatch):
    import koten.cli as cli

    shutil.copy(DEFAULT_CONFIG_DIR / "sources.yaml", conf / "sources.yaml")
    (conf / "settings.toml").write_text(
        f'[paths]\nhdd_root = "{tmp_path / "hdd"}"\nssd_root = "{tmp_path / "ssd"}"\n', "utf-8"
    )
    monkeypatch.setattr(cli, "_make_fetcher", lambda s, raw_dir: fetcher(raw_dir, Server()))
    import yaml

    sources = yaml.safe_load((conf / "sources.yaml").read_text("utf-8"))
    sources["standardebooks"] = CONF
    (conf / "sources.yaml").write_text(yaml.safe_dump(sources, allow_unicode=True), "utf-8")
    st = ["--settings", str(conf / "settings.toml")]
    r = CliRunner().invoke(app, ["ref", "fetch", *st])
    assert r.exit_code == 0, r.output
    assert "R-legge-analects：ファイル 2（今回取得 2）・一節 3" in r.output
    r = CliRunner().invoke(app, ["ref", "build", *st])
    assert r.exit_code == 0, r.output
    assert "取り込んだ本文 3" in r.output
    r = CliRunner().invoke(app, ["ref", "show", "--passage", "lunyu:1.2", "--all", *st])
    assert "The pupil said" in r.output and "[imported]" in r.output
    r = CliRunner().invoke(app, ["ref", "fetch", "--ref", "R-none", *st])
    assert r.exit_code == 2 and "standardebooks.editions に無い" in r.output


def test_build_without_hdd_keeps_imported_rows(conf, tmp_path):
    hdd = tmp_path / "hdd"
    works = {w.ref_id: w for w in read_registry(conf)}
    raw = hdd / "raw" / "standardebooks"
    import_edition(
        hdd,
        CONF,
        editions(CONF)["R-legge-analects"],
        works["R-legge-analects"],
        fetcher(raw, Server()),
    )
    conn = connect(tmp_path / "ssd")
    build_refs(conn, conf, load_imported(hdd))
    # HDD が未接続（imported=None）でも、取り込んだ本文は残る
    b = build_refs(conn, conf, None)
    assert b.imported == 3
    assert renderings_for_passage(conn, "lunyu:1.1", verified_only=False)[0]["status"] == "imported"
    # 資料が使えなくなったら、HDD が無くても SSD からは外す
    write(
        conf,
        [
            work(
                "R-legge-analects",
                kind="translation",
                title="The Analects",
                lang="en",
                source="standardebooks",
                pub_year="1893",
                credit="x",
            )
        ],
        [person("R-legge-analects", 1, "訳者", "translator", 1956, country="", allied="")],
    )
    assert build_refs(conn, conf, None).imported == 0
