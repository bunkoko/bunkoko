"""回答者1,000人の属性と人物設定（design.md 6.1）。

部署・年代・雇用形態・総合満足度は、割合どおりの人数を最大剰余法で決めて並べ替える。
役職は「課長以上」「主任・係長」の順に、年代と雇用形態で重みを付けた非復元抽出で割り付ける。
勤続年数は年代（と役職）ごとの条件付き分布から引く。
"""

from dataclasses import dataclass

import numpy as np

from tools.samplegen.sampling import (
    largest_remainder,
    rng_for,
    to_permille,
    weighted_sample,
)

N_RESPONDENTS = 1000

DEPTS = ("製造", "営業", "開発・技術", "管理部門", "物流", "CS")
DEPT_SHARES = to_permille([35, 20, 12, 13, 10, 10])
AGES = ("20代以下", "30代", "40代", "50代", "60代以上")
AGE_SHARES = to_permille([18, 24, 27, 23, 8])
EMPS = ("正社員", "契約社員", "パート・アルバイト", "派遣社員")
EMP_SHARES = to_permille([72, 12, 10, 6])
POSS = ("一般", "主任・係長", "課長以上")
POS_SHARES = to_permille([70, 20, 10])
SATS = (1, 2, 3, 4, 5)
SAT_SHARES = to_permille([8, 17, 35, 30, 10])

TENURES = ("1年未満", "1〜3年未満", "3〜5年未満", "5〜10年未満", "10〜20年未満", "20年以上")
# 年代ごとの勤続年数の分布（%）。20代以下は10年以上が0。
TENURE_BY_AGE = {
    "20代以下": (25, 35, 25, 15, 0, 0),
    "30代": (10, 18, 20, 32, 20, 0),
    "40代": (5, 10, 12, 23, 35, 15),
    "50代": (3, 6, 8, 15, 33, 35),
    "60代以上": (4, 6, 6, 14, 30, 40),
}

# 役職の重み（年代と相関）。パート・派遣は課長以上が0。
_KACHO_BY_AGE = {"20代以下": 0.02, "30代": 0.8, "40代": 2.5, "50代": 3.5, "60代以上": 2.0}
_SHUNIN_BY_AGE = {"20代以下": 0.3, "30代": 1.5, "40代": 2.0, "50代": 1.8, "60代以上": 1.0}
_KACHO_BY_EMP = {"正社員": 1.0, "契約社員": 0.3, "パート・アルバイト": 0.0, "派遣社員": 0.0}
_SHUNIN_BY_EMP = {"正社員": 1.0, "契約社員": 0.5, "パート・アルバイト": 0.1, "派遣社員": 0.05}

TRAITS = (
    "子育て中",
    "介護中",
    "夜勤あり",
    "中途入社",
    "通勤90分",
    "育休明け",
    "外国籍（日本語やや不慣れ）",
)
TRAIT_DESCRIPTIONS = {
    "子育て中": "小さな子どもを育てながら働いている",
    "介護中": "家族の介護をしながら働いている",
    "夜勤あり": "夜勤を含む勤務がある",
    "中途入社": "他の会社から途中で入社した",
    "通勤90分": "通勤に片道90分ほどかかる",
    "育休明け": "育児休業から復帰して間もない",
    "外国籍（日本語やや不慣れ）": ("外国籍で日本語にやや不慣れ（言い回しに素朴なぎこちなさが少しある。誤字は避ける）"),
}


@dataclass(frozen=True, slots=True)
class Respondent:
    respondent_id: str
    dept: str
    age: str
    tenure: str
    emp: str
    pos: str
    sat: int
    traits: tuple[str, ...]

    @property
    def is_non_regular(self) -> bool:
        return self.emp != "正社員"


def _exact_shuffle(rng: np.random.Generator, values, shares, n: int) -> list:
    counts = largest_remainder(n, shares)
    pool = [v for v, c in zip(values, counts, strict=True) for _ in range(c)]
    return [pool[i] for i in rng.permutation(len(pool))]


def _trait_weights(dept: str, age: str, tenure: str) -> list[float]:
    # 子育て中は60代以上では0。育休明けは50代以上と、勤続1年未満では0
    kosodate = {"20代以下": 0.3, "30代": 3.0, "40代": 3.0, "50代": 0.3, "60代以上": 0.0}[age]
    kaigo = {"20代以下": 0.05, "30代": 0.4, "40代": 2.0, "50代": 3.0, "60代以上": 2.0}[age]
    yakin = {"製造": 1.5, "物流": 1.5, "CS": 0.4}.get(dept, 0.0)
    chuto = 2.0 if tenure in TENURES[:3] else 0.6
    ikukyu = {"20代以下": 0.3, "30代": 1.5, "40代": 0.4, "50代": 0.0, "60代以上": 0.0}[age]
    if tenure == TENURES[0]:
        ikukyu = 0.0
    return [kosodate, kaigo, yakin, chuto, 0.8, ikukyu, 0.3]


def generate_respondents(n: int = N_RESPONDENTS) -> list[Respondent]:
    rng = rng_for("attributes")
    dept = _exact_shuffle(rng, DEPTS, DEPT_SHARES, n)
    age = _exact_shuffle(rng, AGES, AGE_SHARES, n)
    emp = _exact_shuffle(rng, EMPS, EMP_SHARES, n)
    sat = _exact_shuffle(rng, SATS, SAT_SHARES, n)

    # 役職：課長以上 → 主任・係長 → 残りは一般
    n_pos = dict(zip(POSS, largest_remainder(n, POS_SHARES), strict=True))
    pos = ["一般"] * n
    w_kacho = [_KACHO_BY_AGE[a] * _KACHO_BY_EMP[e] for a, e in zip(age, emp, strict=True)]
    kacho = set(weighted_sample(rng, w_kacho, n_pos["課長以上"]).tolist())
    for i in kacho:
        pos[i] = "課長以上"
    w_shunin = [0.0 if i in kacho else _SHUNIN_BY_AGE[age[i]] * _SHUNIN_BY_EMP[emp[i]] for i in range(n)]
    for i in weighted_sample(rng, w_shunin, n_pos["主任・係長"]).tolist():
        pos[i] = "主任・係長"

    # 勤続年数：年代ごとの条件付き分布。課長以上は3年未満、主任・係長は1年未満、派遣社員は10年以上を除く。
    tenure = []
    for a, p, e in zip(age, pos, emp, strict=True):
        probs = np.array(TENURE_BY_AGE[a], dtype=float)
        if p == "課長以上":
            probs[:2] = 0.0
        elif p == "主任・係長":
            probs[0] = 0.0
        if e == "派遣社員":
            probs[4:] = 0.0
        probs /= probs.sum()
        tenure.append(TENURES[int(rng.choice(len(TENURES), p=probs))])

    # 人物設定：特徴を1〜2個（1個が60%）
    rng_t = rng_for("traits")
    out = []
    for i in range(n):
        k = 1 if rng_t.random() < 0.6 else 2
        w = _trait_weights(dept[i], age[i], tenure[i])
        picked = weighted_sample(rng_t, w, k)
        out.append(
            Respondent(
                respondent_id=f"R{i + 1:04d}",
                dept=dept[i],
                age=age[i],
                tenure=tenure[i],
                emp=emp[i],
                pos=pos[i],
                sat=sat[i],
                traits=tuple(TRAITS[j] for j in picked),
            )
        )
    return out
