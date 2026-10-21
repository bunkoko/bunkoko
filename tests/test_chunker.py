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
