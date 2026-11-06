"""古い英訳（Project Gutenberg のミラー。M8b・D-037）：ミラー・ライセンス文・節の分け方・取得。"""

import copy
import shutil

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from koten.cli import app
from koten.config import DEFAULT_CONFIG_DIR
from koten.db import connect
from koten.net import Fetcher
from koten.ref import gutenberg
from koten.ref.gutenberg import (
    GutenbergEdition,
    GutenbergError,
    body,
    editions,
    forget_unusable_raw,
    import_edition,
    mirror_url,
    split_sections,
)
from koten.ref.registry import read_registry
from koten.ref.standardebooks import imported_path, load_imported
from koten.ref.store import build_refs, renderings_for_passage
from tests.ref_fixture import person, work, write, write_texts

TEXT = """The Project Gutenberg eBook of Meditations (架空の短い本文)

This eBook is for the use of anyone anywhere. License text here.

*** START OF THE PROJECT GUTENBERG EBOOK MEDITATIONS ***

Produced by Someone

CONTENTS

THE FIRST BOOK

I. Of my grandfather I learned to be gentle
and meek.

II. Of my father, modesty.

THE SECOND BOOK

I. Betimes in the morning say to thyself,
this day I shall have to do with an idle man.

Some more of the same section.

II. Whatsoever I am, is either flesh or life.

*** END OF THE PROJECT GUTENBERG EBOOK MEDITATIONS ***

More license text after the end.
"""
ED = GutenbergEdition(
    "R-casaubon",
    2680,
    "2680-0.txt",
    "meditations",
    r"^([IVXLC]+)\. ",
    r"^THE ([A-Z]+) BOOK$",
)
CONF = {
    "mirror": "https://mirror.example/gutenberg/",
    "editions": {
        "R-casaubon": {
            "ebook": 2680,
            "file": "2680-0.txt",
            "canon": "meditations",
            "section_start": r"^([IVXLC]+)\. ",
            "book_heading": r"^THE ([A-Z]+) BOOK$",
        }
    },
}


def test_mirror_url():
    assert (
        mirror_url("https://mirror.example/gutenberg/", 2680, "2680-0.txt")
        == "https://mirror.example/gutenberg/2/6/8/2680/2680-0.txt"
    )
    assert mirror_url("https://m.example", 7, "7.txt") == "https://m.example/0/7/7.txt"
    with pytest.raises(GutenbergError, match="ミラー"):
        mirror_url("", 2680, "2680-0.txt")
    with pytest.raises(GutenbergError, match="本体のサイト"):  # 本体からは取らない
        mirror_url("https://www.gutenberg.org/files/", 2680, "2680-0.txt")


def test_body_drops_the_license_text():
    b = body(TEXT)
    assert b.startswith("Produced by") and "License text" not in b and "after the end" not in b
    with pytest.raises(GutenbergError, match="START"):
        body("no markers here")


def test_split_sections():
    units = {u.number: u for u in split_sections(body(TEXT), ED)}
    assert set(units) == {"1.1", "1.2", "2.1", "2.2"}
    assert units["1.1"].text == "Of my grandfather I learned to be gentle and meek."
    assert units["2.1"].text.endswith("Some more of the same section.")
    assert units["2.2"].locator == "Gutenberg #2680 2.2"
    # 巻の無い本（提要など）
    flat = GutenbergEdition("R-x", 1, "1.txt", "enchiridion", r"^([IVXLC]+)\. ")
    assert [u.number for u in split_sections("I. One.\n\nII. Two.", flat)] == ["1", "2"]


def test_editions_config():
    assert editions(CONF)["R-casaubon"] == ED
    with pytest.raises(GutenbergError, match="section_start"):
        editions({"editions": {"R-x": {"ebook": 1, "file": "1.txt", "canon": "meditations"}}})
    with pytest.raises(GutenbergError, match="正規表現"):
        editions({"editions": {"R-x": {"ebook": 1, "file": "1.txt", "canon": "m",
                                       "section_start": "("}}})


class Server:
    def __init__(self):
        self.calls = []

    def __call__(self, request):
        self.calls.append(str(request.url))
        if str(request.url) == "https://mirror.example/gutenberg/2/6/8/2680/2680-0.txt":
            return httpx.Response(200, content=TEXT.encode("utf-8"))
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
                "R-casaubon",
                kind="translation",
                title="Meditations",
                lang="en",
                source="gutenberg",
                pub_year="1634",
                credit="カソーボンの英訳（1634）",
            )
        ],
        [person("R-casaubon", 1, "Meric Casaubon", "translator", 1671, country="イギリス")],
    )
    write_texts(d)
    return d


def test_import_edition(conf, tmp_path):
    hdd = tmp_path / "hdd"
    works = {w.ref_id: w for w in read_registry(conf)}
    server = Server()
    r = import_edition(hdd, CONF, ED, works["R-casaubon"], fetcher(hdd / "raw/gutenberg", server))
    assert r.units == 4 and r.path == imported_path(hdd, "R-casaubon")
    assert server.calls == ["https://mirror.example/gutenberg/2/6/8/2680/2680-0.txt"]
    conn = connect(tmp_path / "ssd")
    build_refs(conn, conf, load_imported(hdd))
    rows = renderings_for_passage(conn, "meditations:2.1", verified_only=False)
    assert rows[0]["status"] == "imported" and rows[0]["ref_credit"] == "カソーボンの英訳（1634）"
    assert "Gutenberg" not in rows[0]["ref_credit"]  # 名前は D-004 まで出さない（出典は credit）


def test_unusable_work_is_not_fetched(conf, tmp_path):
    write(
        conf,
        [work("R-casaubon", kind="translation", title="M", lang="en", source="gutenberg",
              pub_year="1634", credit="c")],
        [person("R-casaubon", 1, "Someone", "translator", 1990, country="イギリス")],
    )
    works = {w.ref_id: w for w in read_registry(conf)}
    server = Server()
    with pytest.raises(GutenbergError, match="取れない"):
        import_edition(tmp_path / "hdd", CONF, ED, works["R-casaubon"],
                       fetcher(tmp_path / "raw", server))
    assert server.calls == []
    raw = tmp_path / "hdd" / "raw" / "gutenberg" / "2680"
    raw.mkdir(parents=True)
    assert forget_unusable_raw(tmp_path / "hdd", works, editions(CONF))
    assert not raw.exists()


def test_ref_fetch_command(conf, tmp_path, monkeypatch):
    import koten.cli as cli

    shutil.copy(DEFAULT_CONFIG_DIR / "sources.yaml", conf / "sources.yaml")
    (tmp_path / "hdd").mkdir(exist_ok=True)
    (conf / "settings.toml").write_text(
        f'[paths]\nhdd_root = "{tmp_path / "hdd"}"\nssd_root = "{tmp_path / "ssd"}"\n', "utf-8"
    )
    monkeypatch.setattr(cli, "_make_fetcher", lambda s, raw_dir: fetcher(raw_dir, Server()))
    sources = yaml.safe_load((conf / "sources.yaml").read_text("utf-8"))
    sources["gutenberg"] = copy.deepcopy(CONF)
    sources["standardebooks"] = {"raw_base": "https://raw.example/", "editions": {}}
    (conf / "sources.yaml").write_text(yaml.safe_dump(sources, allow_unicode=True), "utf-8")
    st = ["--settings", str(conf / "settings.toml")]
    r = CliRunner().invoke(app, ["ref", "fetch", *st])
    assert r.exit_code == 0, r.output
    assert "R-casaubon：ファイル 1（今回取得 1）・一節 4" in r.output
    r = CliRunner().invoke(app, ["ref", "build", *st])
    assert r.exit_code == 0 and "取り込んだ本文 4" in r.output, r.output
    # ミラーが空なら止める
    sources["gutenberg"]["mirror"] = ""
    (conf / "sources.yaml").write_text(yaml.safe_dump(sources, allow_unicode=True), "utf-8")
    r = CliRunner().invoke(app, ["ref", "fetch", *st])
    assert r.exit_code == 1 and "ミラー" in r.output
    assert gutenberg  # モジュールを読み込めている


def test_split_stops_instead_of_guessing():
    toc = (
        "CONTENTS\n\nI. Of things in our power\n\nII. Of desire\n\nTHE ENCHIRIDION\n\n"
        "I. There are things within our power.\n\nII. Remember that desire.\n"
    )
    flat = GutenbergEdition("R-x", 45109, "x.txt", "enchiridion", r"^([IVXLC]+)\. ")
    with pytest.raises(GutenbergError, match="2度出る"):  # 目次の番号を本文と取り違えない
        split_sections(toc, flat)
    with_start = GutenbergEdition("R-x", 45109, "x.txt", "enchiridion", r"^([IVXLC]+)\. ",
                                  body_start=r"^THE ENCHIRIDION$")
    units = split_sections(toc, with_start)
    assert [u.text for u in units] == [
        "There are things within our power.",
        "Remember that desire.",
    ]
    back = ("THE FIRST BOOK\n\nI. One.\n\nII. Two.\n\nGLOSSARY\n\nL. Verus, a man.\n\n"
            "C. Fronto, a teacher.\n")
    with pytest.raises(GutenbergError, match="飛ぶ"):  # 用語集の頭文字を節にしない
        split_sections(back, ED)
    ended = GutenbergEdition(**{**ED.__dict__, "end_heading": r"^(APPENDIX|NOTES|GLOSSARY)$"})
    assert [u.number for u in split_sections(back, ended)] == ["1.1", "1.2"]


def test_ref_build_tightens_before_reading_sources(conf, tmp_path, monkeypatch):
    """sources.yaml が壊れていても、厳しくなった台帳の判定は先に SSD に届く。"""
    import koten.cli as cli

    shutil.copy(DEFAULT_CONFIG_DIR / "sources.yaml", conf / "sources.yaml")
    (tmp_path / "hdd").mkdir(exist_ok=True)
    (conf / "settings.toml").write_text(
        f'[paths]\nhdd_root = "{tmp_path / "hdd"}"\nssd_root = "{tmp_path / "ssd"}"\n', "utf-8"
    )
    monkeypatch.setattr(cli, "_make_fetcher", lambda s, raw_dir: fetcher(raw_dir, Server()))
    sources = yaml.safe_load((conf / "sources.yaml").read_text("utf-8"))
    sources["gutenberg"] = copy.deepcopy(CONF)
    sources["standardebooks"] = {"raw_base": "https://raw.example/", "editions": {}}
    (conf / "sources.yaml").write_text(yaml.safe_dump(sources, allow_unicode=True), "utf-8")
    st = ["--settings", str(conf / "settings.toml")]
    assert CliRunner().invoke(app, ["ref", "fetch", *st]).exit_code == 0
    assert CliRunner().invoke(app, ["ref", "build", *st]).exit_code == 0
    write(
        conf,
        [work("R-casaubon", kind="translation", title="Meditations", lang="en",
              source="gutenberg", pub_year="1634", credit="カソーボンの英訳（1634）")],
        [person("R-casaubon", 1, "Meric Casaubon", "translator", 1990, country="イギリス")],
    )
    sources["gutenberg"]["editions"]["R-casaubon"]["section_start"] = "("
    (conf / "sources.yaml").write_text(yaml.safe_dump(sources, allow_unicode=True), "utf-8")
    r = CliRunner().invoke(app, ["ref", "build", *st])
    assert r.exit_code == 2 and "sources.yaml" in r.output
    row = connect(tmp_path / "ssd").execute(
        "SELECT license FROM ref_works WHERE ref_id = 'R-casaubon'"
    ).fetchone()
    assert row is None or row["license"] != "PD_JP"
