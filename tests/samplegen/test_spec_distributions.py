"""仕様表の件数・割合が design.md 6.1 のとおりか（最大剰余法の丸め＝期待値との差が1未満）。"""

from collections import Counter

import pytest

from tests.samplegen.conftest import content_rows
from tools.samplegen.answers import EMOTIONS, INVALID_TEXTS, NONE_TEXTS
from tools.samplegen.attributes import TRAIT_DESCRIPTIONS
from tools.samplegen.codeframes import OTHER, QUESTIONS
from tools.samplegen.sampling import largest_remainder, round_half_up

N = 1000

# 6.1 の数字をここに直接書く（実装側の定数とは別に確かめるため）
ATTR_PCT = {
    "dept": {"製造": 35, "営業": 20, "開発・技術": 12, "管理部門": 13, "物流": 10, "CS": 10},
    "age": {"20代以下": 18, "30代": 24, "40代": 27, "50代": 23, "60代以上": 8},
    "emp": {"正社員": 72, "契約社員": 12, "パート・アルバイト": 10, "派遣社員": 6},
    "pos": {"一般": 70, "主任・係長": 20, "課長以上": 10},
    "sat": {"1": 8, "2": 17, "3": 35, "4": 30, "5": 10},
}
KIND_PCT = {
    "Q1": {"content": 80, "none": 11, "blank": 6, "invalid": 3},
    "Q2": {"content": 70, "none": 15, "blank": 12, "invalid": 3},
    "Q3": {"content": 45, "none": 20, "blank": 33, "invalid": 2},
}
LABEL_PCT = {
    "Q1": [
        ("人員不足・業務量", 15),
        ("給与・賞与", 13),
        ("評価制度・昇進", 11),
        ("上司・マネジメント", 9),
        ("部署間連携・コミュニケーション", 8),
        ("残業・労働時間", 7),
        ("休暇の取りやすさ", 6),
        ("教育・研修・キャリア", 6),
        ("職場環境・設備", 5),
        ("ITツール・業務効率化", 5),
        ("福利厚生・手当", 4),
        ("柔軟な働き方", 3),
        ("経営方針・情報共有", 2.5),
        ("ハラスメント・人間関係", 2.5),
        ("その他", 3),
    ],
    "Q2": [
        ("同僚・人間関係", 20),
        ("仕事のやりがい", 15),
        ("休暇・WLB", 12),
        ("雇用の安定", 11),
        ("福利厚生", 9),
        ("成長・学び", 8),
        ("上司", 7),
        ("柔軟な働き方", 6),
        ("製品・社会貢献への誇り", 5),
        ("給与・待遇", 4),
        ("その他", 3),
    ],
    "Q3": [
        ("経営・ビジョンへの要望", 18),
        ("待遇改善", 16),
        ("人材育成・採用", 14),
        ("働き方・制度", 13),
        ("組織風土", 12),
        ("設備・IT投資", 8),
        ("本調査への意見", 8),
        ("感謝・応援", 7),
        ("その他", 4),
    ],
}
BAND_PCT = {
    "Q1": [12, 28, 35, 20, 5],
    "Q2": [16, 32, 34, 15, 3],
    "Q3": [8, 22, 35, 27, 8],
}
BAND_KEYS = ["1-10", "11-30", "31-80", "81-200", "201-400"]
STYLE_PCT = {
    "丁寧語": 40,
    "常体・体言止め": 28,
    "口語": 14,
    "箇条書き": 8,
    "強い口調": 6,
    "絵文字・顔文字": 4,
}
QIDS = ["Q1", "Q2", "Q3"]


def assert_within_rounding(counter, expected_pct: dict, total: int):
    assert sum(counter.values()) == total
    for key, pct in expected_pct.items():
        assert abs(counter[key] - total * pct / 100) < 1, key


@pytest.mark.parametrize("attr", list(ATTR_PCT))
def test_attribute_counts(respondents, attr):
    assert len(respondents) == N
    assert_within_rounding(Counter(r[attr] for r in respondents), ATTR_PCT[attr], N)


def test_respondent_ids_unique(respondents):
    assert [r["respondent_id"] for r in respondents] == [f"R{i:04d}" for i in range(1, N + 1)]


def test_forbidden_combinations_are_zero(respondents):
    long_tenure = {"10〜20年未満", "20年以上"}
    assert not [r for r in respondents if r["age"] == "20代以下" and r["tenure"] in long_tenure]
    part_or_dispatch = {"パート・アルバイト", "派遣社員"}
    assert not [r for r in respondents if r["emp"] in part_or_dispatch and r["pos"] == "課長以上"]


def test_position_correlates_with_age(respondents):
    def rate(age_set, pos):
        sub = [r for r in respondents if r["age"] in age_set]
        return sum(r["pos"] == pos for r in sub) / len(sub)

    for pos in ("主任・係長", "課長以上"):
        assert rate({"50代", "40代"}, pos) > 1.5 * rate({"20代以下"}, pos)  # 種を変えても1.9倍以上
    assert rate({"20代以下"}, "課長以上") < 0.03  # 種を変えても1.1%まで


def test_tenure_is_conditional_on_age(respondents):
    def long_share(age):
        sub = [r for r in respondents if r["age"] == age]
        return sum(r["tenure"] in {"10〜20年未満", "20年以上"} for r in sub) / len(sub)

    assert long_share("20代以下") == 0
    assert long_share("30代") < long_share("40代") < long_share("50代")


def test_manager_tenure_is_plausible(respondents):
    short = {"1年未満", "1〜3年未満"}
    assert not [r for r in respondents if r["pos"] == "課長以上" and r["tenure"] in short]


def test_traits_one_or_two_and_plausible(respondents):
    for r in respondents:
        traits = r["traits"].split("|")
        assert 1 <= len(traits) <= 2
        assert len(set(traits)) == len(traits)
        assert set(traits) <= set(TRAIT_DESCRIPTIONS)
        if "夜勤あり" in traits:
            assert r["dept"] in {"製造", "物流", "CS"}
        if "育休明け" in traits:
            assert r["age"] in {"20代以下", "30代", "40代"}
            assert r["tenure"] != "1年未満"  # 勤続1年未満で育休明けは不自然
        if "子育て中" in traits:
            assert r["age"] != "60代以上"


def test_dispatch_workers_have_no_long_tenure(respondents):
    long_tenure = {"10〜20年未満", "20年以上"}
    dispatch = [r for r in respondents if r["emp"] == "派遣社員"]
    assert len(dispatch) == 60
    assert not [r for r in dispatch if r["tenure"] in long_tenure]


@pytest.mark.parametrize("qid", QIDS)
def test_kind_counts(spec_rows, qid):
    rows = [r for r in spec_rows if r["question"] == qid]
    assert_within_rounding(Counter(r["kind"] for r in rows), KIND_PCT[qid], N)


@pytest.mark.parametrize("qid", QIDS)
def test_satisfaction_5_doubles_none_weight(spec_rows, respondents, qid):
    """満足度5の人の「特になし」は重み2倍。件数を先に決めるので、3問それぞれで比が出る。"""
    sat = {r["respondent_id"]: r["sat"] for r in respondents}
    rows = [r for r in spec_rows if r["question"] == qid]
    hi = [r for r in rows if sat[r["respondent_id"]] == "5"]
    lo = [r for r in rows if sat[r["respondent_id"]] != "5"]
    n_none = sum(r["kind"] == "none" for r in rows)
    n_hi = sum(r["kind"] == "none" for r in hi)
    # n_none × 2·n5 / (2·n5 + n_other) を四捨五入した件数
    assert n_hi == round_half_up(n_none * 2 * len(hi) / (2 * len(hi) + len(lo)))
    ratio = (n_hi / len(hi)) / ((n_none - n_hi) / len(lo))
    assert 1.5 <= ratio <= 2.5


def test_fixed_text_only_for_non_content(spec_rows):
    none_texts = {t for t, _ in NONE_TEXTS}
    for r in spec_rows:
        if r["kind"] in {"content", "blank"}:
            assert r["text_fixed"] == ""
        elif r["kind"] == "none":
            assert r["text_fixed"] in none_texts
        else:
            assert r["text_fixed"] in INVALID_TEXTS
    used = {r["text_fixed"] for r in spec_rows if r["kind"] == "none"}
    assert used >= {"特になし", "とくになし", "なし"}


@pytest.mark.parametrize("qid", QIDS)
def test_primary_label_counts(spec_rows, qid):
    rows = content_rows(spec_rows, qid)
    counter = Counter(r["label_names"].split("|")[0] for r in rows)
    assert_within_rounding(counter, dict(LABEL_PCT[qid]), len(rows))
    codes = {lb.name: lb.code for lb in QUESTIONS[qid].labels}
    for r in rows:
        names = r["label_names"].split("|")
        assert r["label_codes"].split("|") == [codes[n] for n in names]


def test_q1_smallest_category_has_at_least_20(spec_rows):
    rows = content_rows(spec_rows, "Q1")
    counter = Counter(r["label_names"].split("|")[0] for r in rows)
    assert len(counter) == 15
    assert min(counter.values()) >= 20


@pytest.mark.parametrize("qid", QIDS)
def test_multi_label_share_and_other_is_single(spec_rows, qid):
    rows = content_rows(spec_rows, qid)
    n = Counter(len(r["label_names"].split("|")) for r in rows)
    assert_within_rounding(n, {1: 65, 2: 27, 3: 8}, len(rows))
    for r in rows:
        names = r["label_names"].split("|")
        assert len(set(names)) == len(names)
        if OTHER in names:
            assert names == [OTHER]  # 「その他」は単独のみ（主でも副でも）
        if r["length_band"] == "1-10":
            assert len(names) == 1
        if r["length_band"] == "11-30":
            assert len(names) <= 2


@pytest.mark.parametrize("qid", QIDS)
def test_second_label_comes_from_pair_table_for_60_percent(spec_rows, qid):
    """複数ラベルの行のうち、2つ目が関連ペア表から来る行は60%（最大剰余法の件数どおり）。"""
    q = QUESTIONS[qid]
    labels = [r["label_names"].split("|") for r in content_rows(spec_rows, qid)]
    labels = [s for s in labels if len(s) >= 2]
    related = sum(s[1] in q.neighbors(s[0]) for s in labels)
    assert abs(related - round_half_up(0.6 * len(labels))) <= 1
    assert related == largest_remainder(len(labels), [600, 400])[0]
    for s in labels:  # 「その他」は選ばれない
        assert OTHER not in s


@pytest.mark.parametrize("qid", QIDS)
def test_band_counts_and_ranges(spec_rows, qid):
    rows = content_rows(spec_rows, qid)
    expected = dict(zip(BAND_KEYS, BAND_PCT[qid], strict=True))
    assert_within_rounding(Counter(r["length_band"] for r in rows), expected, len(rows))
    ranges = {
        "1-10": (1, 10),
        "11-30": (11, 30),
        "31-80": (31, 80),
        "81-200": (81, 200),
        "201-400": (201, 400),
    }
    for r in rows:
        assert (int(r["len_min"]), int(r["len_max"])) == ranges[r["length_band"]]


def test_q2_is_shorter_and_q3_is_longer_than_q1(spec_rows):
    def short_share(qid):
        rows = content_rows(spec_rows, qid)
        return sum(r["length_band"] in {"1-10", "11-30"} for r in rows) / len(rows)

    assert short_share("Q2") > short_share("Q1") > short_share("Q3")


@pytest.mark.parametrize("qid", QIDS)
def test_style_counts_and_constraints(spec_rows, qid):
    rows = content_rows(spec_rows, qid)
    assert_within_rounding(Counter(r["style"] for r in rows), STYLE_PCT, len(rows))
    calm = {"要望", "淡々"}  # 説明に「落ち着いて」とある感情と、淡々
    for r in rows:
        if r["style"] == "箇条書き":
            assert r["length_band"] not in {"1-10", "11-30", "201-400"}
        if r["style"] == "絵文字・顔文字":
            assert r["length_band"] != "201-400"
            assert r["emotion"] != "淡々"
        if r["style"] == "強い口調":
            assert r["emotion"] not in calm


def test_emotion_does_not_conflict_with_length(spec_rows):
    for r in content_rows(spec_rows, "Q1"):
        if r["length_band"] == "1-10":
            assert r["emotion"] != "肯定まじり"


def test_q1_emotion_counts(spec_rows):
    rows = content_rows(spec_rows, "Q1")
    pct = {"要望": 50, "不満": 38, "強い怒り": 5, "肯定まじり": 7}
    assert_within_rounding(Counter(r["emotion"] for r in rows), pct, len(rows))


def test_emotions_are_defined_and_anger_follows_dissatisfaction(spec_rows):
    for qid in QIDS:
        for r in content_rows(spec_rows, qid):
            assert r["emotion"] in EMOTIONS[qid]
    rows = content_rows(spec_rows, "Q1")
    low = [r for r in rows if r["sat"] in {"1", "2"}]
    high = [r for r in rows if r["sat"] in {"4", "5"}]
    low_rate = sum(r["emotion"] == "強い怒り" for r in low) / len(low)
    high_rate = sum(r["emotion"] == "強い怒り" for r in high) / len(high)
    assert low_rate > 2 * high_rate


def test_non_content_rows_have_no_content_fields(spec_rows):
    cols = (
        "label_codes",
        "label_names",
        "length_band",
        "style",
        "emotion",
        "difficulty",
        "trap_type",
        "must_topics",
        "avoid_topics",
    )
    for r in spec_rows:
        if r["kind"] != "content":
            for col in cols:
                assert r[col] == "", col


def test_avoid_topics_do_not_overlap_must_topics(spec_rows):
    for qid in QIDS:
        q = QUESTIONS[qid]
        shorts = {lb.name: lb.short for lb in q.labels}
        for r in content_rows(spec_rows, qid):
            own = {shorts[n] for n in r["label_names"].split("|")}
            avoid = r["avoid_topics"].split("|")
            assert len(avoid) == 3 and len(set(avoid)) == 3
            assert not own & set(avoid)
            if r["decoy_code"]:
                decoy = next(lb for lb in q.labels if lb.code == r["decoy_code"])
                assert decoy.short not in avoid
