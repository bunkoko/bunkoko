"""難しい回答（15%）の罠が、その行の感情・文字数帯・ラベル数・文体と両立していること。"""

import re
from collections import Counter

import pytest

from tests.samplegen.conftest import content_rows
from tools.samplegen.answers import TRAP_TYPES, allowed_traps, decoy_candidates
from tools.samplegen.codeframes import OTHER, QUESTIONS
from tools.samplegen.sampling import round_half_up

QIDS = ["Q1", "Q2", "Q3"]
LONG = {"31-80", "81-200", "201-400"}
# 設計の取り決めを、実装の定数とは別にここに書く
Q3_NEGATION_FORBIDDEN = {"組織風土", "本調査への意見", "感謝・応援", OTHER}
Q3_OFFQ_FORBIDDEN = {"本調査への意見", "感謝・応援", OTHER}
REQUEST_WORDS = re.compile(r"ほしい|欲しい|望む|望み|もらいたい|希望|要望|求め|願|べき|してくれ|意見|提案|期待|声")


def _decoy_name(qid, code):
    return next(lb.name for lb in QUESTIONS[qid].labels if lb.code == code)


@pytest.mark.parametrize("qid", QIDS)
def test_hard_count_and_normal_rows(spec_rows, qid):
    rows = content_rows(spec_rows, qid)
    hard = [r for r in rows if r["difficulty"] == "hard"]
    assert len(hard) == round_half_up(0.15 * len(rows))
    assert {r["difficulty"] for r in rows} <= {"normal", "hard"}
    for r in rows:
        if r["difficulty"] == "normal":
            assert r["trap_type"] == r["decoy_code"] == r["trap_keyword"] == ""
        else:
            assert r["trap_type"] in TRAP_TYPES


@pytest.mark.parametrize("qid", QIDS)
def test_no_hard_row_in_shortest_band(spec_rows, qid):
    rows = content_rows(spec_rows, qid)
    assert not [r for r in rows if r["difficulty"] == "hard" and r["length_band"] == "1-10"]


@pytest.mark.parametrize("qid", QIDS)
def test_every_trap_is_compatible_with_its_row(spec_rows, qid):
    q = QUESTIONS[qid]
    for r in content_rows(spec_rows, qid):
        if r["difficulty"] != "hard":
            continue
        t, band, emo, style = r["trap_type"], r["length_band"], r["emotion"], r["style"]
        names = r["label_names"].split("|")
        codes = r["label_codes"].split("|")
        if t in {"keyword_mismatch", "negation"}:
            assert r["decoy_code"] and r["decoy_code"] not in codes  # 触れさせた話題はラベルに入れない
            decoy = _decoy_name(qid, r["decoy_code"])
            assert decoy != OTHER
            assert band != "1-10"
            if t == "keyword_mismatch":
                # 語は、そのカテゴリが別の意味で使える語として表に載っているものだけ
                assert r["trap_keyword"] in [w for w, _ in q.by_name(decoy).trap_keywords]
                # 関連ペアのカテゴリは decoy にしない
                assert not {decoy} & {nb for n in names for nb in q.neighbors(n)}
            else:
                assert r["trap_keyword"] == ""
                if qid == "Q3":
                    assert decoy not in Q3_NEGATION_FORBIDDEN
        else:
            assert r["decoy_code"] == "" and r["trap_keyword"] == ""
        if t == "vague":
            assert len(names) == 1 or band in LONG
            assert style != "強い口調"  # 「言い切らず」と「語気が強く言い切る」が食い違う
        if t == "sarcasm":
            assert qid != "Q2"
            assert emo in {"不満", "強い怒り"}
            assert "感謝・応援" not in names
        if t == "weak_mix":
            assert len(names) == 2 and band in LONG  # 正解はその行のラベル全部（ちょうど2つ）
            assert OTHER not in names
            assert style not in {"強い口調", "箇条書き"}
        if t == "off_question":
            assert band != "1-10"
            allowed = {"Q1": {"不満", "強い怒り"}, "Q2": {"控えめな肯定", "淡々"}, "Q3": {"淡々", "不満"}}
            assert emo in allowed[qid]
            if qid == "Q3":
                assert not set(names) & Q3_OFFQ_FORBIDDEN
            if qid in {"Q1", "Q3"}:  # 要望・意見・提案の形でない言い換えだけを使う
                assert not [x for x in r["must_topics"].split("|") if REQUEST_WORDS.search(x)]


@pytest.mark.parametrize("qid", QIDS)
def test_trap_types_are_balanced_and_q2_has_no_sarcasm(spec_rows, qid):
    hard = [r for r in content_rows(spec_rows, qid) if r["difficulty"] == "hard"]
    counts = Counter(r["trap_type"] for r in hard)
    assert set(counts) == set(TRAP_TYPES) - ({"sarcasm"} if qid == "Q2" else set())
    assert max(counts.values()) - min(counts.values()) <= 2


def test_pairs_left_as_they_are_still_occur_or_are_allowed():
    """控えめな肯定×強い口調と、Q2 の設問違い×淡々は、そのままでよい（許可されたまま）。"""
    q2 = QUESTIONS["Q2"]
    assert "off_question" in allowed_traps(q2, ("同僚・人間関係",), "81-200", "淡々", "常体・体言止め")
    assert "off_question" in allowed_traps(q2, ("同僚・人間関係",), "81-200", "淡々", "丁寧語")
    assert "keyword_mismatch" in allowed_traps(q2, ("同僚・人間関係",), "81-200", "控えめな肯定", "強い口調")
    assert "off_question" in allowed_traps(q2, ("同僚・人間関係",), "81-200", "控えめな肯定", "強い口調")


def test_allowed_traps_unit():
    q1 = QUESTIONS["Q1"]
    one = ("人員不足・業務量",)
    two = ("人員不足・業務量", "残業・労働時間")
    three = ("人員不足・業務量", "残業・労働時間", "休暇の取りやすさ")
    assert allowed_traps(q1, one, "1-10", "不満") == []
    assert "weak_mix" not in allowed_traps(q1, one, "81-200", "不満")  # ラベルが1つ
    assert "weak_mix" in allowed_traps(q1, two, "81-200", "不満")
    assert "weak_mix" not in allowed_traps(q1, three, "81-200", "不満")  # ちょうど2つの行だけ
    assert "weak_mix" not in allowed_traps(q1, two, "11-30", "不満")
    assert "vague" not in allowed_traps(q1, two, "11-30", "不満")
    assert "vague" in allowed_traps(q1, one, "11-30", "不満")
    assert "sarcasm" in allowed_traps(q1, one, "11-30", "強い怒り")
    assert "sarcasm" not in allowed_traps(q1, one, "11-30", "要望")
    assert "off_question" not in allowed_traps(q1, one, "11-30", "要望")
    assert "off_question" in allowed_traps(q1, one, "11-30", "不満")
    assert allowed_traps(q1, (OTHER,), "81-200", "不満") == [
        "sarcasm",
        "off_question",
        "negation",
        "keyword_mismatch",
        "vague",
    ]  # 条件のきつい順
    # 文体：強い口調には曖昧・弱い混在を付けない。箇条書きには弱い混在を付けない
    assert "vague" not in allowed_traps(q1, one, "81-200", "不満", "強い口調")
    assert "weak_mix" not in allowed_traps(q1, two, "81-200", "不満", "強い口調")
    assert "weak_mix" not in allowed_traps(q1, two, "81-200", "不満", "箇条書き")
    assert "vague" in allowed_traps(q1, one, "81-200", "不満", "箇条書き")
    assert "weak_mix" in allowed_traps(q1, two, "81-200", "不満", "丁寧語")
    q2 = QUESTIONS["Q2"]
    assert "sarcasm" not in allowed_traps(q2, ("同僚・人間関係",), "81-200", "控えめな肯定")
    assert "off_question" not in allowed_traps(q2, ("同僚・人間関係",), "81-200", "素直な肯定")
    assert "off_question" in allowed_traps(q2, ("同僚・人間関係",), "81-200", "淡々")
    q3 = QUESTIONS["Q3"]
    thanks = ("感謝・応援",)
    assert "sarcasm" not in allowed_traps(q3, thanks + ("組織風土",), "81-200", "不満")
    assert "off_question" not in allowed_traps(q3, thanks, "81-200", "淡々")
    assert "off_question" not in allowed_traps(q3, ("本調査への意見",), "81-200", "不満")
    assert "off_question" not in allowed_traps(q3, (OTHER,), "81-200", "不満")
    assert "off_question" in allowed_traps(q3, ("待遇改善",), "81-200", "不満")
    assert "off_question" not in allowed_traps(q3, ("待遇改善",), "81-200", "要望")
    # 打ち消せる話題は要望型のカテゴリだけ。それしか残らない行なら打ち消しは選べない
    only_decoys = ("経営・ビジョンへの要望", "待遇改善", "人材育成・採用", "働き方・制度", "設備・IT投資")
    assert "negation" not in allowed_traps(q3, only_decoys, "81-200", "不満")


def test_keyword_decoy_is_never_a_neighbour_of_the_rows_labels():
    # 関連ペアのカテゴリは、キーワード違いの decoy の候補から外れる
    q1 = QUESTIONS["Q1"]
    labels = ("残業・労働時間",)
    linked = set(q1.neighbors("残業・労働時間"))
    assert linked
    cands = decoy_candidates(q1, labels, "keyword_mismatch")
    assert not set(cands) & linked and "残業・労働時間" not in cands and OTHER not in cands
    # 打ち消しの候補には関連ペアも含まれる（こちらは近いカテゴリを優先する）
    assert linked <= set(decoy_candidates(q1, labels, "negation"))
