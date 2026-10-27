"""本文書き係に渡すバッチ（work/prompts/）の検査。"""

import re
from collections import Counter

from tests.samplegen.conftest import ROW_ID_RE
from tools.samplegen import prompts
from tools.samplegen.codeframes import QUESTIONS


def _batch_rows(batches):
    return {name: ROW_ID_RE.findall(text) for name, text in batches.items()}


def test_every_content_row_appears_exactly_once(spec_rows, batches):
    content = [r["row_id"] for r in spec_rows if r["kind"] == "content"]
    assert len(content) == 1950  # 800 + 700 + 450
    seen = Counter(rid for ids in _batch_rows(batches).values() for rid in ids)
    assert set(seen) == set(content)
    assert set(seen.values()) == {1}


def test_batches_have_about_20_rows(batches):
    sizes = [len(ids) for ids in _batch_rows(batches).values()]
    assert len(sizes) == 98  # 約100ファイル
    assert min(sizes) >= 19
    assert max(sizes) <= 20
    assert sum(sizes) == 1950
    for name, text in batches.items():
        assert f"- 行数：{len(_batch_rows({name: text})[name])}" in text


def test_batch_names_and_questions(batches):
    counts = Counter(name.split("_")[0] for name in batches)
    assert counts == {"q1": 40, "q2": 35, "q3": 23}
    for name, ids in _batch_rows(batches).items():
        qid = name.split("_")[0].upper()
        assert {i.split("_")[1] for i in ids} == {qid}
        assert QUESTIONS[qid].text in batches[name]
        assert f"raw_llm/{name}.json" in batches[name]


def test_batch_has_no_code_numbers(batches):
    code_like = re.compile(r"(?<![A-Za-z0-9])c\d{2}(?![A-Za-z0-9])|Q\d[-_ ]?c\d{2}", re.IGNORECASE)
    for name, text in batches.items():
        assert not code_like.search(text), name
        assert "label_code" not in text
        assert "decoy_code" not in text
        assert "trap_type" not in text


def test_batch_does_not_show_label_names(batches):
    """ラベル名をそのまま正解として見せない（その設問のラベル名が本文に現れない）。"""
    for name, text in batches.items():
        q = QUESTIONS[name.split("_")[0].upper()]
        for lb in q.labels:
            assert lb.name not in text, (name, lb.name)


def test_batch_has_required_fields_for_every_row(batches):
    fields = (
        "- 回答者：",
        "- 人物設定：",
        "- 必ず触れる話題：",
        "- 触れてはいけない話題：",
        "- 文字数：",
        "- 文体：",
        "- 感情：",
        "- 難しい回答：",
    )
    for name, text in batches.items():
        n = len(ROW_ID_RE.findall(text))
        for f in fields:
            assert text.count(f) == n, (name, f)


def test_batch_has_placeholders_for_human_samples_and_banned_phrases(batches):
    for text in batches.values():
        assert text.count(prompts.HUMAN_SAMPLES_BEGIN) == 1
        assert text.count(prompts.HUMAN_SAMPLES_END) == 1
        assert text.count(prompts.BANNED_BEGIN) == 1
        assert text.count(prompts.BANNED_END) == 1
        assert prompts.HUMAN_SAMPLES_EMPTY in text
        assert prompts.BANNED_EMPTY in text


def test_batch_rows_are_mixed_not_sequential(batches):
    """同じ人・同じ話題が続かないよう、行の並びは乱数で混ぜてある。"""
    ids = _batch_rows(batches)["q1_001"]
    numbers = [int(i[1:5]) for i in ids]
    assert numbers != sorted(numbers) or len(set(numbers)) == 1
    assert numbers != sorted(numbers)


def test_topic_phrases_have_no_separator_char():
    for q in QUESTIONS.values():
        for lb in q.labels:
            assert all("|" not in t for t in lb.topics)
            assert "|" not in lb.short


def test_split_batches():
    assert prompts.split_batches(800) == [20] * 40
    assert sorted(prompts.split_batches(450)) == [19] * 10 + [20] * 13
    assert sorted(prompts.split_batches(700)) == [20] * 35
    assert prompts.split_batches(3) == [3]
    assert sum(prompts.split_batches(1234)) == 1234
