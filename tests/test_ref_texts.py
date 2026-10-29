"""時代の解釈（renderings.csv）・昔の辞書の語釈（dict_entries.csv）と、SSD への作り直し（M8a）。"""

# ruff: noqa: E501

import shutil

import pytest
from typer.testing import CliRunner

from koten.cli import app
from koten.config import DEFAULT_CONFIG_DIR
from koten.db import connect
from koten.ref.store import (
    build_refs,
    entries_for_words,
    load_entries,
    load_renderings,
    renderings_for_passage,
)
from koten.ref.texts import TextsError
from tests.ref_fixture import entry, person, rendering, work, write, write_texts


@pytest.fixture
def conf(tmp_path):
    """使える辞書・訓読本・英訳と、保護中の訓読本・人の確認待ちの辞書の台帳。"""
    d = tmp_path / "config"
    d.mkdir()
    shutil.copy(DEFAULT_CONFIG_DIR / "canon.yaml", d / "canon.yaml")
    write(
        d,
        [
            work(
                "R-dict",
                title="架空辞書",
                volume="第4冊",
                pub_year="1891",
                source="ndl",
                pid="000001",
                ndl_access="internet_pd",
                ndl_checked_at="2026-10-29",
                credit="大槻架空『架空辞書』第4冊（1891）",
            ),
            work(
                "R-kun",
                kind="kundoku",
                title="架空漢文大系",
                pub_year="1909",
                credit="『架空漢文大系』（1909）",
            ),
            work(
                "R-en",
                kind="translation",
                title="Analects",
                lang="en",
                source="standardebooks",
                pub_year="1861",
                credit="Legge 訳（1861）",
            ),
            work(
                "R-new",
                kind="kundoku",
                title="新しい訓読",
                pub_year="1990",
                credit="新しい訓読（1990）",
            ),
            work(
                "R-corp",
                corporate="yes",
                credited_as="架空書院",
                pub_year="1903",
                credit="架空書院『辞典』（1903）",
            ),
        ],
        [
            person("R-dict", 1, "大槻架空", "author", 1928),
            person("R-kun", 1, "服部架空", "kundoku", 1939),
            person("R-en", 1, "英国の訳者", "translator", 1897, country="英国", allied="yes"),
            person("R-new", 1, "今の学者", "kundoku", 2010),
        ],
    )
    return d


def good_texts(d, **extra):
    write_texts(
        d,
        [
            rendering("I-kun-1.1", "lunyu:1.1", "R-kun", "学ビテ時ニ之ヲ習フ、亦説バシカラズヤ"),
            rendering(
                "I-en-1.1",
                "lunyu:1.1",
                "R-en",
                "Is it not pleasant to learn with a constant perseverance?",
                role="translation",
                reading="",
                locator="chapter-1.xhtml#chapter-1-1",
            ),
            rendering(
                "I-kun-1.2",
                "lunyu:1.2",
                "R-kun",
                "其ノ人ト為リヤ孝弟ニシテ",
                status="transcribed",
                checked_by="",
                checked_at="",
            ),
            *extra.get("renderings", []),
        ],
        [
            entry(
                "J-dict-futsuu-1",
                "R-dict",
                "普通",
                "ヨノツネ。ナミ。",
                display_text="よのつね。なみ。",
                reading="よのつね。なみ。",
            ),
            *extra.get("entries", []),
        ],
    )


def test_build_and_read(conf, tmp_path):
    good_texts(conf)
    conn = connect(tmp_path / "ssd")
    r = build_refs(conn, conf)
    assert (r.works, r.usable, r.renderings, r.entries) == (5, 3, 3, 1)
    # 使えない資料も書誌は入る（本文は無い）
    lic = dict(conn.execute("SELECT ref_id, license FROM ref_works").fetchall())
    assert lic["R-new"] == "COPYRIGHTED" and lic["R-corp"] == "NEEDS_REVIEW"

    got = renderings_for_passage(conn, "lunyu:1.1")
    assert [g["rendering_id"] for g in got] == ["I-en-1.1", "I-kun-1.1"]  # 刊行年の古い順
    assert got[1]["ref_credit"] == "『架空漢文大系』（1909）" and got[1]["ref_pub_year"] == 1909
    # 確かめ中（transcribed）は台本では使わない
    assert renderings_for_passage(conn, "lunyu:1.2") == []
    assert [
        g["rendering_id"] for g in renderings_for_passage(conn, "lunyu:1.2", verified_only=False)
    ] == ["I-kun-1.2"]

    words = entries_for_words(conn, "日本の社会は普通を求める")
    assert [w["entry_id"] for w in words] == ["J-dict-futsuu-1"]
    assert words[0]["display_text"] == "よのつね。なみ。" and words[0]["ref_volume"] == "第4冊"
    assert entries_for_words(conn, "特別な日") == []
    assert set(load_entries(conn, ["J-dict-futsuu-1", "J-none"])) == {"J-dict-futsuu-1"}
    assert set(load_renderings(conn, ["I-kun-1.1"])) == {"I-kun-1.1"}


def test_rebuild_replaces_everything(conf, tmp_path):
    good_texts(conf)
    conn = connect(tmp_path / "ssd")
    build_refs(conn, conf)
    write_texts(conf, [], [])
    build_refs(conn, conf)
    assert conn.execute("SELECT COUNT(*) FROM renderings").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dict_entries").fetchone()[0] == 0


def test_range_rendering(conf, tmp_path):
    good_texts(
        conf,
        renderings=[
            rendering(
                "I-kun-com",
                "lunyu:1.3",
                "R-kun",
                "此ノ三章ハ一意ナリ",
                passage_to="lunyu:1.5",
                role="commentary",
                reading="このさんしょうはいちいなり",
            ),
        ],
    )
    conn = connect(tmp_path / "ssd")
    build_refs(conn, conf)
    assert [r["rendering_id"] for r in renderings_for_passage(conn, "lunyu:1.4")] == ["I-kun-com"]
    assert renderings_for_passage(conn, "lunyu:1.6") == []
    assert renderings_for_passage(conn, "lunyu:1.10") == []  # 1.10 は 1.5 の後（文字の順ではない）


def test_ref_reviewed_later_hides_texts(conf, tmp_path):
    """台帳で使えなくなった資料の本文は、作り直すまでも読み出さない。"""
    good_texts(conf)
    conn = connect(tmp_path / "ssd")
    build_refs(conn, conf)
    with conn:
        conn.execute("UPDATE ref_works SET license = 'NEEDS_REVIEW' WHERE ref_id = 'R-kun'")
    assert [r["rendering_id"] for r in renderings_for_passage(conn, "lunyu:1.1")] == ["I-en-1.1"]


@pytest.mark.parametrize(
    "extra,message",
    [
        # R2：使えない資料の本文は書けない
        (
            {"renderings": [rendering("I-new-1", "lunyu:1.1", "R-new", "新しい訓読の文")]},
            "本文を書けない",
        ),
        ({"entries": [entry("J-corp-1", "R-corp", "普通", "なみ")]}, "NEEDS_REVIEW"),
        (
            {"renderings": [rendering("I-x", "lunyu:1.1", "R-none", "文")]},
            "台帳（ref_works.csv）に無い",
        ),
        ({"renderings": [rendering("I-x", "lunyu:one", "R-kun", "文")]}, "付け方"),
        ({"renderings": [rendering("I-x", "rongo:1.1", "R-kun", "文")]}, "canon.yaml に無い"),
        ({"renderings": [rendering("I-x", "lunyu:1.1", "R-kun", "文", reading="")]}, "reading"),
        (
            {"renderings": [rendering("I-x", "lunyu:1.1", "R-kun", "文", reading="学びて")]},
            "かなと区切り",
        ),
        (
            {"renderings": [rendering("I-x", "lunyu:1.1", "R-kun", "文", checked_by="")]},
            "checked_by",
        ),
        ({"renderings": [rendering("I-x", "lunyu:1.1", "R-kun", "文", locator="")]}, "locator"),
        (
            {"renderings": [rendering("I-x", "lunyu:1.1", "R-kun", "文", role="translation")]},
            "role translation を書けない",
        ),
        ({"renderings": [rendering("I-x", "lunyu:1.1", "R-kun", "文" * 801)]}, "長すぎる"),
        ({"renderings": [rendering("I-kun-1.1", "lunyu:1.1", "R-kun", "文")]}, "重複"),
        (
            {"renderings": [rendering("I-x", "lunyu:1.1", "R-kun", "文", display_text="文")]},
            "表記を改めたときだけ",
        ),
        ({"renderings": [rendering("x", "lunyu:1.1", "R-kun", "文")]}, "I- で始め"),
        ({"entries": [entry("J-x", "R-dict", "普通", "なみ", locator="12ページ")]}, "コマ"),
        ({"entries": [entry("J-x", "R-kun", "普通", "なみ")]}, "辞書ではない"),
        ({"entries": [entry("J-x", "R-dict", "普通", "なみ", sense="一")]}, "語義番号"),
        ({"entries": [entry("J-x", "R-dict", "", "なみ")]}, "headword"),
    ],
)
def test_texts_problems_stop_everything(conf, tmp_path, extra, message):
    good_texts(conf, **extra)
    conn = connect(tmp_path / "ssd")
    with pytest.raises(TextsError, match=message):
        build_refs(conn, conf)
    assert conn.execute("SELECT COUNT(*) FROM ref_works").fetchone()[0] == 0


def test_ref_build_and_show_commands(conf, tmp_path):
    shutil.copy(DEFAULT_CONFIG_DIR / "sources.yaml", conf / "sources.yaml")
    (conf / "settings.toml").write_text(
        f'[paths]\nhdd_root = "{tmp_path / "hdd"}"\nssd_root = "{tmp_path / "ssd"}"\n', "utf-8"
    )
    good_texts(conf)
    st = ["--settings", str(conf / "settings.toml")]
    r = CliRunner().invoke(app, ["ref", "build", *st])
    assert r.exit_code == 0, r.output
    assert "資料 5（使える 3）・時代の解釈 3・取り込んだ本文 0・辞書の語釈 1" in r.output
    r = CliRunner().invoke(app, ["ref", "show", "--word", "普通", *st])
    assert "J-dict-futsuu-1  普通（ふつう）1  [verified]" in r.output
    assert (
        "よのつね。なみ。" in r.output
        and "出典：大槻架空『架空辞書』第4冊（1891）　コマ800" in r.output
    )
    r = CliRunner().invoke(app, ["ref", "show", "--passage", "lunyu:1.2", *st])
    assert "見つかりません" in r.output
    r = CliRunner().invoke(app, ["ref", "show", "--passage", "lunyu:1.2", "--all", *st])
    assert "I-kun-1.2" in r.output and "[transcribed]" in r.output
    good_texts(conf, renderings=[rendering("I-new-1", "lunyu:1.1", "R-new", "新しい訓読の文")])
    r = CliRunner().invoke(app, ["ref", "build", *st])
    assert r.exit_code == 2 and "本文を書けない" in r.output


def test_tightening_reaches_ssd_even_when_build_stops(conf, tmp_path):
    """資料が使えなくなったのに CSV に行が残っていて作り直しが止まっても、SSD では先に使えなくする。"""
    good_texts(conf)
    conn = connect(tmp_path / "ssd")
    build_refs(conn, conf)
    assert load_entries(conn, ["J-dict-futsuu-1"])
    # 辞書の校訂者が1990年没と分かった → 保護中
    write(
        conf,
        [
            work(
                "R-dict",
                title="架空辞書",
                volume="第4冊",
                pub_year="1891",
                source="ndl",
                pid="000001",
                ndl_access="internet_pd",
                ndl_checked_at="2026-10-29",
                credit="x",
            ),
            work("R-kun", kind="kundoku", title="架空漢文大系", pub_year="1909", credit="y"),
        ],
        [
            person("R-dict", 1, "大槻架空", "author", 1928),
            person("R-dict", 2, "校訂者", "reviser", 1990),
            person("R-kun", 1, "服部架空", "kundoku", 1939),
        ],
    )
    with pytest.raises(TextsError) as e:
        build_refs(conn, conf)
    assert sorted(e.value.tightened) == ["R-dict", "R-en"]  # R-en は台帳から消えた
    assert load_entries(conn, ["J-dict-futsuu-1"]) == {}
    assert entries_for_words(conn, "普通の毎日") == []
    lic = conn.execute("SELECT license FROM ref_works WHERE ref_id = 'R-dict'").fetchone()[0]
    assert lic == "COPYRIGHTED"
    # 台帳から消えた資料の本文も外す
    assert all(
        r["ref_id"] != "R-en"
        for r in renderings_for_passage(conn, "lunyu:1.1", verified_only=False)
    )
    assert conn.execute("SELECT COUNT(*) FROM ref_works WHERE ref_id = 'R-en'").fetchone()[0] == 0


def test_paragraph_ranges_compare_as_numbers(conf, tmp_path):
    good_texts(
        conf,
        renderings=[
            rendering(
                "I-en-med",
                "meditations:2.p3",
                "R-en",
                "A note on these paragraphs.",
                passage_to="meditations:2.p12",
                role="translation",
                reading="",
                locator="text/book-2.xhtml",
            ),
        ],
    )
    conn = connect(tmp_path / "ssd")
    build_refs(conn, conf)
    for key in ("meditations:2.p5", "meditations:2.p12", "meditations:2.p3"):
        assert [r["rendering_id"] for r in renderings_for_passage(conn, key)] == ["I-en-med"], key
    for key in ("meditations:2.p13", "meditations:2.p2", "meditations:2.5", "meditations:3.p5"):
        assert renderings_for_passage(conn, key) == [], key


@pytest.mark.parametrize(
    "start,end,message",
    [
        ("meditations:2.3", "meditations:2.p5", "同じ番号の付け方"),
        ("lunyu:1.5", "lunyu:1.3", "より前"),
    ],
)
def test_bad_ranges_are_refused(conf, tmp_path, start, end, message):
    good_texts(
        conf,
        renderings=[
            rendering(
                "I-bad",
                start,
                "R-en",
                "A note.",
                passage_to=end,
                role="translation",
                reading="",
                locator="x",
            ),
        ],
    )
    with pytest.raises(TextsError, match=message):
        build_refs(connect(tmp_path / "ssd"), conf)
