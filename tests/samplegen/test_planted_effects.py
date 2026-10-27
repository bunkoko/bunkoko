"""仕込んだ属性効果：設計書 6.1 との一致、記録された実現値、集計で見えること。"""

import copy

import numpy as np
import pytest
import yaml

from tests.samplegen.conftest import content_rows
from tools.samplegen import answers as ans
from tools.samplegen.attributes import generate_respondents
from tools.samplegen.codeframes import QUESTIONS
from tools.samplegen.sampling import ipf, largest_remainder

NON_REGULAR = ["契約社員", "パート・アルバイト", "派遣社員"]
# 設計書 6.1 の13個（yaml から読まずに、ここに直接書く）：(when, ラベル, 乗数)
SPEC_EFFECTS = [
    ({"dept": ["製造"]}, "職場環境・設備", 3.0),
    ({"dept": ["営業"]}, "残業・労働時間", 2.0),
    ({"dept": ["営業"]}, "人員不足・業務量", 1.5),
    ({"age": ["20代以下"]}, "教育・研修・キャリア", 2.5),
    ({"age": ["20代以下"]}, "給与・賞与", 1.5),
    ({"age": ["40代", "50代"]}, "評価制度・昇進", 1.8),
    ({"emp": NON_REGULAR}, "福利厚生・手当", 2.5),
    ({"emp": NON_REGULAR}, "経営方針・情報共有", 2.0),
    ({"age": ["30代"]}, "柔軟な働き方", 2.0),
    ({"dept": ["管理部門"]}, "ITツール・業務効率化", 2.5),
    ({"sat": [1, 2]}, "上司・マネジメント", 2.0),
    ({"sat": [1, 2]}, "ハラスメント・人間関係", 3.0),
    ({"tenure": ["1年未満"]}, "教育・研修・キャリア", 2.0),
]
STRONG_MULTIPLIER = 2.5  # 乗数がこれ以上の効果は、はっきり出ていること
# 種を20通り変えて確かめた最小値（主ラベル 2.23、全ラベル 1.77）より下に置いた
STRONG_PRIMARY_MIN = 2.0
STRONG_ANY_MIN = 1.6
PRIMARY_VS_EXPECTED = 0.2  # 実現した主ラベルのリフトは、IPF の期待リフトの ±20% 以内（種を変えた20通りで -10%〜+10%）


def _normalize(effects):
    return sorted(
        (sorted((k, sorted(v)) for k, v in when.items()), label, float(mult)) for when, label, mult in effects
    )


def _src():
    return yaml.safe_load(ans.EFFECTS_PATH.read_text(encoding="utf-8"))


def _out(built):
    return yaml.safe_load(built["ground_truth/planted_effects.yaml"])


def test_source_yaml_matches_design_6_1():
    data = _src()
    got = [(e["when"], e["label"], e["multiplier"]) for e in data["effects"]]
    assert len(got) == 13
    assert _normalize(got) == _normalize(SPEC_EFFECTS)
    assert data["question"] == "Q1"


def test_output_yaml_matches_design_6_1(built):
    data = _out(built)
    assert [e["id"] for e in data["effects"]] == [f"E{i:02d}" for i in range(1, 14)]
    got = [(e["when"], e["label"], e["multiplier"]) for e in data["effects"]]
    assert _normalize(got) == _normalize(SPEC_EFFECTS)
    q = QUESTIONS[data["question"]]
    for e in data["effects"]:
        assert e["code"] == q.by_name(e["label"]).code


def _recompute(rows, effect, code):
    def matches(r):
        return all(r[a] in {str(v) for v in vs} for a, vs in effect["when"].items())

    inside = [r for r in rows if matches(r)]
    outside = [r for r in rows if not matches(r)]

    def lift(pred):
        rate_in = sum(pred(r) for r in inside) / len(inside)
        rate_out = sum(pred(r) for r in outside) / len(outside)
        return rate_in / rate_out

    prim = lift(lambda r: r["label_codes"].split("|")[0] == code)
    anyl = lift(lambda r: code in r["label_codes"].split("|"))
    return prim, anyl, len(inside), len(outside)


def test_recorded_realized_values_are_true(built, gt_rows):
    """yaml に書いた実現リフトと内・外の件数は、rows_gt.csv から数え直した値と一致する。"""
    data = _out(built)
    rows = content_rows(gt_rows, data["question"])
    for e in data["effects"]:
        prim, anyl, n_in, n_out = _recompute(rows, e, e["code"])
        assert e["n_in"] == n_in and e["n_out"] == n_out
        assert e["realized_lift_primary"] == pytest.approx(prim, abs=1e-4)
        assert e["realized_lift_any"] == pytest.approx(anyl, abs=1e-4)
        assert e["n_in"] + e["n_out"] == len(rows)


def test_primary_lift_is_close_to_ipf_expectation(built):
    for e in _out(built)["effects"]:
        ratio = e["realized_lift_primary"] / e["ipf_expected_lift"]
        assert abs(ratio - 1) <= PRIMARY_VS_EXPECTED, e["id"]
        assert e["ipf_expected_lift"] > 1


def test_all_label_lift_exceeds_one_and_strong_ones_are_clear(built):
    for e in _out(built)["effects"]:
        assert e["realized_lift_any"] > 1, e["id"]
        if e["multiplier"] >= STRONG_MULTIPLIER:
            assert e["realized_lift_any"] >= STRONG_ANY_MIN, e["id"]
            assert e["realized_lift_primary"] >= STRONG_PRIMARY_MIN, e["id"]


def test_manufacturing_facility_effect_headline(gt_rows):
    """製造×職場環境・設備：他部署の2倍以上。"""
    rows = content_rows(gt_rows, "Q1")
    code = QUESTIONS["Q1"].by_name("職場環境・設備").code

    def rate(sub):
        return sum(r["label_codes"].split("|")[0] == code for r in sub) / len(sub)

    seizo = [r for r in rows if r["dept"] == "製造"]
    other = [r for r in rows if r["dept"] != "製造"]
    assert rate(seizo) >= 2 * rate(other)


def test_effects_are_not_applied_to_q2(gt_rows):
    # Q2 には効果を掛けていないので、非正規×福利厚生の偏りは出ない
    rows = content_rows(gt_rows, "Q2")
    code = QUESTIONS["Q2"].by_name("福利厚生").code
    non_regular = [r for r in rows if r["emp"] != "正社員"]
    regular = [r for r in rows if r["emp"] == "正社員"]

    def rate(sub):
        return sum(r["label_codes"].split("|")[0] == code for r in sub) / len(sub)

    assert rate(non_regular) < 2 * rate(regular)


def test_effect_stats_rejects_empty_group():
    respondents = generate_respondents()
    q = QUESTIONS["Q1"]
    ghost = ans.Effect(id="X1", when={"dept": ("存在しない部署",)}, label="給与・賞与", multiplier=2.0)
    rows, p, content = ans.build_question_rows(q, respondents, [])
    with pytest.raises(ValueError):
        ans.effect_stats(q, respondents, rows, p, content, [ghost])


# --- yaml の検査（属性の値を語彙と照らす） ---


def _write(tmp_path, mutate):
    data = copy.deepcopy(_src())
    mutate(data)
    p = tmp_path / "effects.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def test_load_effects_accepts_the_real_file():
    qid, effects = ans.load_effects()
    assert qid == "Q1" and len(effects) == 13


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["effects"][6]["when"].update(emp=["派遣"]),  # 値の綴り違い（派遣社員）
        lambda d: d["effects"][10]["when"].update(sat=["1", "2"]),  # 満足度は整数
        lambda d: d["effects"][10]["when"].update(sat=[1, 6]),
        lambda d: d["effects"][0]["when"].update(dept=["製造部"]),
        lambda d: d["effects"][0]["when"].update(depts=["製造"]),  # 属性名の綴り違い
        lambda d: d["effects"][0].update(when={}),
        lambda d: d["effects"][0]["when"].update(dept=[]),
        lambda d: d["effects"][0]["when"].update(dept="製造"),  # リストでない
        lambda d: d["effects"][5].update(multiplier=-1.8),
        lambda d: d["effects"][5].update(multiplier=0),
        lambda d: d["effects"][5].update(multiplier="1.8"),
        lambda d: d["effects"][5].update(multiplier=float("nan")),
        lambda d: d["effects"][0].update(label="職場環境"),
        lambda d: d["effects"][1].update(id="E01"),  # id の重複
    ],
)
def test_load_effects_rejects_typos(tmp_path, mutate):
    with pytest.raises(ValueError):
        ans.load_effects(_write(tmp_path, mutate))


# --- 効果の強さを固定する（乗数を弱めたり、副ラベルから外したりすると落ちる） ---


def _matches(row, when):
    return all(row[a] in {str(v) for v in vs} for a, vs in when.items())


def test_ipf_expected_lift_is_recomputed_from_the_design_multipliers(built, gt_rows):
    """設計書 6.1 の乗数から IPF を計算し直し、記録された期待リフトと一致する。"""
    q = QUESTIONS["Q1"]
    rows = content_rows(gt_rows, "Q1")
    col = {lb.name: i for i, lb in enumerate(q.labels)}
    w = np.ones((len(rows), len(q.labels)))
    for when, label, mult in SPEC_EFFECTS:
        for j, r in enumerate(rows):
            if _matches(r, when):
                w[j, col[label]] *= mult
    p = ipf(w, largest_remainder(len(rows), q.label_shares))
    recorded = {(e["label"], e["multiplier"], str(sorted(e["when"].items()))): e for e in _out(built)["effects"]}
    for when, label, mult in SPEC_EFFECTS:
        inside = np.array([_matches(r, when) for r in rows])
        c = col[label]
        lift = p[inside, c].mean() / p[~inside, c].mean()
        e = recorded[(label, mult, str(sorted(when.items())))]
        assert e["ipf_expected_lift"] == pytest.approx(lift, abs=1e-3), e["id"]


def test_secondary_labels_keep_the_effect(built):
    """全ラベルのリフトが、主ラベルのリフトの一定割合以上残っている（副ラベルにも同じ乗数を掛けている）。"""
    for e in _out(built)["effects"]:
        floor = 1 + ans.SECONDARY_RETAIN * (e["realized_lift_primary"] - 1)
        assert e["realized_lift_any"] >= floor - 1e-3, e["id"]  # 記録は小数第4位までに丸めてある


def test_primary_hits_in_group_are_within_one_of_the_ipf_expected_count(built):
    """主ラベルの、対象の中の件数は、IPF の期待件数の±1以内（制御丸め＋局所探索の効果）。"""
    for e in _out(built)["effects"]:
        lift, n_in, n_out = e["ipf_expected_lift"], e["n_in"], e["n_out"]
        total = e["primary_hits_in"] + e["primary_hits_out"]
        expected_in = lift * n_in * total / (lift * n_in + n_out)
        assert abs(e["primary_hits_in"] - expected_in) <= 1.0, (e["id"], expected_in)
