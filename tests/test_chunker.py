from koten.chunk.chunker import Span, chunk_section, chunk_work, sentences
from koten.normalize.aozora import parse
from tests.conftest import SAMPLE_TEXT


def test_chunk_text_matches_offsets_and_sections():
    t = parse(SAMPLE_TEXT)
    rows = chunk_work("aozora:1", t.text_orig, t.sections, min_chars=20, max_chars=80)
    assert rows
    for r in rows:
        assert t.text_orig[r["char_start"] : r["char_end"]] == r["text_orig"]
        assert r["chunk_id"] == f"aozora:1#{r['seq']}"
        assert r["section"] in {"嫉妬について", "孤独について"}
        assert "　" not in r["text_norm"] and "\n" not in r["text_norm"]
    # 章をまたがない
    assert not any("嫉妬" in r["text_orig"] and "孤独は山" in r["text_orig"] for r in rows)


def test_paragraphs_packed_up_to_max():
    text = "\n".join("　" + "あ" * 50 + "。" for _ in range(6))
    chunks = chunk_section(text, 0, len(text), min_chars=100, max_chars=200)
    assert all(len(c) <= 200 for c in chunks)
    assert len(chunks) == 2  # 51字の段落を3つずつ（改行・字下げを含めて約157字）


def test_long_paragraph_split_at_sentence_end():
    para = "　" + "".join(f"{'い' * 40}{i}。" for i in range(10))
    chunks = chunk_section(para, 0, len(para), min_chars=50, max_chars=100)
    for c in chunks:
        assert len(c) <= 100
        assert para[c.end - 1] == "。"


def test_sentence_includes_closing_bracket():
    text = "「行け。」と言った。次。"
    spans = sentences(text, Span(0, len(text)))
    assert [text[s.start : s.end] for s in spans] == ["「行け。」", "と言った。", "次。"]


def test_short_tail_merged_into_previous():
    text = "あ" * 70 + "。\n" + "い" * 9 + "。"
    chunks = chunk_section(text, 0, len(text), min_chars=20, max_chars=100)
    assert len(chunks) == 1


def _check(text, chunks, min_chars, max_chars):
    for c in chunks:
        assert min_chars <= len(c) <= max_chars
        assert text[c.end - 1] == "。"  # 文の途中で切らない


def test_short_paragraph_before_long_one_is_rebalanced():
    # 16字の段落の後に590字の段落。つなぐと600字を超えるので、文の切れ目で分け直す
    text = "　" + "あ" * 15 + "。\n　" + ("い" * 58 + "。") * 10
    chunks = chunk_section(text, 0, len(text), min_chars=200, max_chars=600)
    assert len(chunks) == 2
    _check(text, chunks, 200, 600)
    assert text[chunks[0].start :].startswith("あ")


def test_short_paragraph_in_middle_joins_next():
    text = "　" + "う" * 299 + "。\n　" + "え" * 9 + "。\n　" + "お" * 299 + "。"
    chunks = chunk_section(text, 0, len(text), min_chars=200, max_chars=600)
    _check(text, chunks, 200, 600)
    assert (
        sum(len(text[c.start : c.end].replace("\n", "").replace("　", "")) for c in chunks) == 610
    )


def test_whole_short_section_is_kept():
    text = "　短い後記。"
    chunks = chunk_section(text, 0, len(text), min_chars=200, max_chars=600)
    assert [text[c.start : c.end] for c in chunks] == ["短い後記。"]
