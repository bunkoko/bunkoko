"""引用候補の抽出（M2）：原文一致100%・再試行・中断からの再開。"""

import pytest

from koten.augment.extract import (
    AugmentConfig,
    done_chunk_ids,
    iter_candidates,
    load_themes,
    run_augment,
    validate_quotes,
)
from koten.config import DEFAULT_CONFIG_DIR, REPO_ROOT
from koten.llm import LLMError, fill, load_prompt, parse_json_object
from koten.parquet import read_rows
from tests.conftest import FakeLLM

PROMPTS = REPO_ROOT / "prompts"
VOCAB = load_themes(DEFAULT_CONFIG_DIR)
CHUNK = {
    "chunk_id": "aozora:1#1",
    "work_id": "aozora:1",
    "section": "嫉妬について",
    "char_start": 10,
    "text_orig": (
        "　他人の幸福を見て、自分の不足を数えるとき、心は落ち着きを失う。\n　比べる癖だけが残る。"
    ),
}
WORK_TEXT = "x" * 10 + CHUNK["text_orig"] + "y" * 10


def q(text, **kw):
    base = {
        "quote_text": text,
        "summary_ja": "比べると心が乱れる",
        "themes": ["嫉妬・比較"],
        "worries": ["同期と比べてしまう"],
        "quotability": 0.5,
        "caution": False,
        "caution_reason": "",
    }
    return {**base, **kw}


def test_exact_quote_kept_with_positions():
    quote = "他人の幸福を見て、自分の不足を数えるとき、心は落ち着きを失う。"
    rows, dropped = validate_quotes(
        {"quotes": [q(quote)]}, CHUNK, WORK_TEXT, set(VOCAB), AugmentConfig()
    )
    assert not dropped
    (r,) = rows
    assert r["quote_text"] == quote
    assert WORK_TEXT[r["quote_start"] : r["quote_end"]] == quote
    assert quote in r["context_text"] and r["context_text"].startswith("x")


def test_whitespace_difference_is_stored_as_original():
    quote = "心は落ち着きを失う。比べる癖だけが残る。"  # 原文は改行と字下げを挟む
    rows, _ = validate_quotes({"quotes": [q(quote)]}, CHUNK, WORK_TEXT, set(VOCAB), AugmentConfig())
    assert rows[0]["quote_text"] == "心は落ち着きを失う。\n　比べる癖だけが残る。"
    assert WORK_TEXT[rows[0]["quote_start"] : rows[0]["quote_end"]] == rows[0]["quote_text"]


@pytest.mark.parametrize(
    "item,reason",
    [
        (q("他人の不幸を見て、自分の幸福を数えるとき、心は落ち着く。"), "原文に無い"),
        (q("比べる癖だけが残る。"), "長さが範囲外"),
        (
            q(
                "他人の幸福を見て、自分の不足を数えるとき、心は落ち着きを失う。",
                summary_ja="あ" * 61,
            ),
            "要旨の長さ",
        ),
        (
            q(
                "他人の幸福を見て、自分の不足を数えるとき、心は落ち着きを失う。",
                themes=["恋愛相談"],
            ),
            "統制語彙のテーマが無い",
        ),
        (
            q(
                "他人の幸福を見て、自分の不足を数えるとき、心は落ち着きを失う。",
                worries=["あ" * 21],
            ),
            "悩みが無い",
        ),
    ],
)
def test_invalid_quotes_dropped(item, reason):
    rows, dropped = validate_quotes(
        {"quotes": [item]}, CHUNK, WORK_TEXT, set(VOCAB), AugmentConfig()
    )
    assert rows == [] and dropped == {reason: 1}


def test_prompt_template_fills_placeholders():
    system, user = load_prompt(PROMPTS, "augment_v1")
    s = fill(
        system,
        quote_min=20,
        quote_max=120,
        max_quotes=3,
        summary_max=60,
        worry_max=20,
        themes="、".join(VOCAB),
    )
    assert "{" not in s.replace('{"quotes"', "").replace('{"quote_text', "")
    assert "嫉妬・比較" in s
    assert "{text}" in user


def test_parse_json_object_variants():
    assert parse_json_object('```json\n{"quotes": []}\n```') == {"quotes": []}
    assert parse_json_object('結果です：{"quotes": []} 以上') == {"quotes": []}
    with pytest.raises(ValueError):
        parse_json_object("JSONではない")


def _all_chunks(hdd):
    return read_rows(hdd / "chunks/aozora.parquet")


def _texts(hdd):
    return {t["work_id"]: t["text_orig"] for t in read_rows(hdd / "clean/aozora/texts.parquet")}


def test_run_augment_resume_and_exact_match(hdd_with_chunks):
    _, hdd = hdd_with_chunks
    cfg = AugmentConfig(batch_size=2)
    n_chunks = len(_all_chunks(hdd))

    first = run_augment(hdd, PROMPTS, VOCAB, FakeLLM(), cfg, limit=3)
    assert first.processed == 3
    second = run_augment(hdd, PROMPTS, VOCAB, FakeLLM(), cfg)
    assert second.skipped == 3 and first.processed + second.processed == n_chunks
    third = run_augment(hdd, PROMPTS, VOCAB, FakeLLM(), cfg)
    assert third.processed == 0

    out = hdd / "augment/fake-llm/augment_v1"
    assert len(done_chunk_ids(out)) == n_chunks
    cands = list(iter_candidates(out))
    assert cands, "候補が1件もない"
    assert len({c["candidate_id"] for c in cands}) == len(cands)
    texts = _texts(hdd)
    # 受け入れ条件：抜き出した一節の原文一致率が100%
    for c in cands:
        assert texts[c["work_id"]][c["quote_start"] : c["quote_end"]] == c["quote_text"]
        assert c["themes"] == ["嫉妬・比較"]
        assert len(c["worries"]) == 2 and c["quotability"] == 1.0
    # 作り話の一節は1件も残らない
    assert not any("真理" in c["quote_text"] for c in cands)
    assert first.dropped["原文に無い"] + second.dropped["原文に無い"] == n_chunks


def test_broken_output_retried_then_failed(hdd_with_chunks):
    _, hdd = hdd_with_chunks
    ok = run_augment(hdd, PROMPTS, VOCAB, FakeLLM(broken_first=2), AugmentConfig(), limit=1)
    assert ok.processed == 1 and ok.failed == 0
    ng = run_augment(hdd, PROMPTS, VOCAB, FakeLLM(broken_first=99), AugmentConfig(), limit=1)
    assert ng.processed == 1 and ng.failed == 1
    done = read_rows(sorted((hdd / "augment/fake-llm/augment_v1").glob("done-*.parquet"))[-1])
    assert done[0]["status"] == "failed"
    assert done[0]["error"].startswith("JSONDecodeError")


def test_interrupt_keeps_finished_chunks(hdd_with_chunks):
    _, hdd = hdd_with_chunks
    with pytest.raises(KeyboardInterrupt):
        run_augment(hdd, PROMPTS, VOCAB, FakeLLM(interrupt_at=3), AugmentConfig(batch_size=100))
    out = hdd / "augment/fake-llm/augment_v1"
    assert len(done_chunk_ids(out)) == 2  # 止める前に終わった2チャンクは保存済み
    rest = run_augment(hdd, PROMPTS, VOCAB, FakeLLM(), AugmentConfig())
    assert rest.skipped == 2 and len(done_chunk_ids(out)) == len(_all_chunks(hdd))


def test_llm_down_stops_and_keeps_results(hdd_with_chunks):
    _, hdd = hdd_with_chunks

    class Down(FakeLLM):
        def complete(self, system, user, schema=None):
            if self.calls == 1:  # 2回目の呼び出しで接続が切れる
                raise LLMError("接続できない")
            return super().complete(system, user, schema)

    with pytest.raises(LLMError):
        run_augment(hdd, PROMPTS, VOCAB, Down(), AugmentConfig())
    assert len(done_chunk_ids(hdd / "augment/fake-llm/augment_v1")) == 1
