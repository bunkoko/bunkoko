from koten.normalize.aozora import GETA, parse
from tests.conftest import SAMPLE_TEXT


def test_ruby_and_annotations_removed():
    t = parse(SAMPLE_TEXT)
    for mark in ("《", "》", "｜", "［＃", "］"):
        assert mark not in t.text_orig
    assert "嫉妬は人を比べることから生まれる。" in t.text_orig


def test_header_and_colophon_split():
    t = parse(SAMPLE_TEXT)
    assert "テキスト中に現れる記号" not in t.text_orig
    assert "架空　太郎" not in t.text_orig
    assert t.colophon.startswith("底本：「架空の底本」架空書房")
    assert "入力：架空" in t.colophon
    assert "底本" not in t.text_orig


def test_gaiji_replaced_and_logged():
    t = parse(SAMPLE_TEXT)
    assert len(t.gaiji) == 2
    jis, uni = t.gaiji
    assert jis["replacement"] != GETA and len(jis["replacement"]) == 1
    assert uni["replacement"] == chr(0x23D7B)
    assert chr(0x23D7B) in t.text_orig
    assert "※" not in t.text_orig


def test_unknown_gaiji_becomes_geta():
    t = parse("題\n著者\n\n　あ※［＃「謎の字」］い\n")
    assert t.text_orig == "　あ〓い"
    assert t.gaiji == [{"note": "「謎の字」", "replacement": GETA}]


def test_kunoji():
    assert "書いた〳〵。" in parse(SAMPLE_TEXT).text_orig


def test_sections_from_headings():
    t = parse(SAMPLE_TEXT)
    titles = [s["title"] for s in t.sections]
    assert titles == ["嫉妬について", "孤独について"]
    first = t.text_orig[t.sections[0]["start"] : t.sections[0]["end"]]
    assert "嫉妬は人を" in first and "孤独は山に" not in first
    second = t.text_orig[t.sections[1]["start"] : t.sections[1]["end"]]
    assert second.strip().startswith("孤独は山に")
    assert "孤独について" not in second


def test_nested_headings_are_joined():
    text = (
        "題\n著者\n\n"
        "第一部［＃「第一部」は大見出し］\n"
        "朝［＃「朝」は中見出し］\n　本文一。\n"
        "夜［＃「夜」は中見出し］\n　本文二。\n"
    )
    titles = [s["title"] for s in parse(text).sections]
    assert titles == ["第一部 / 朝", "第一部 / 夜"]
