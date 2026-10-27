"""設問ごとの行の仕様（design.md 6.1・6.2 手順1）。

回答の種類 → 主ラベル → 文字数帯 → ラベル数 → 副ラベル → 感情 → 文体 → 難しさ の順に決める。
どの段階も「件数を最大剰余法で確定してから行に割り付ける」ので、割合は丸めの範囲でぴたりと合う。
先に決めた感情・文字数帯・ラベル数は、あとから変えない。難しい回答の罠は、その行と両立するものから選ぶ。
本文は決めない（LLM にラベルは書かせない）。
"""

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

from tools.samplegen.attributes import AGES, DEPTS, EMPS, POSS, SATS, TENURES, Respondent
from tools.samplegen.codeframes import BANDS, OTHER, QUESTIONS, Question
from tools.samplegen.sampling import (
    allocate,
    controlled_rounding,
    ipf,
    largest_remainder,
    rng_for,
    round_half_up,
    weighted_sample,
)

EFFECTS_PATH = Path(__file__).with_name("planted_effects.yaml")

KINDS = ("content", "none", "blank", "invalid")
# 中身のない回答の本文（LLM を使わず Python で入れる）：(本文, 重み)
NONE_TEXTS = (
    ("特になし", 50),
    ("とくになし", 15),
    ("なし", 15),
    ("特にありません", 8),
    ("特に無し", 5),
    ("ありません", 4),
    ("ない", 3),
)
INVALID_TEXTS = ("あ", "。", "test", "テスト", "asdf", "1", "???", "ああああ", "ー", "...", "x")

# 感情：(本文書き係への説明, 肯定的か)
EMOTIONS = {
    "Q1": {
        "要望": ("改善を落ち着いて求める、前向きな要望の調子", False),
        "不満": ("不満や困りごとをはっきり述べる", False),
        "強い怒り": ("怒りや憤りがにじむ強い調子（暴言・差別語・個人攻撃は書かない）", False),
        "肯定まじり": ("現状のよい面にも一言触れつつ（同じ話題の範囲で）、改善を求める", True),
    },
    "Q2": {
        "素直な肯定": ("素直に喜びや感謝を述べる", True),
        "控えめな肯定": ("「悪くない」「それなりに」程度の控えめな肯定", True),
        "淡々": ("感情をあまり出さず、事実を淡々と述べる（否定的にはしない）", True),
    },
    "Q3": {
        "要望": ("会社への要望を落ち着いて述べる", False),
        "不満": ("不満や疑問をはっきり述べる", False),
        "強い怒り": ("怒りや憤りがにじむ強い調子（暴言・差別語・個人攻撃は書かない）", False),
        "淡々": ("感情をあまり出さず、淡々と述べる", False),
        "感謝": ("感謝や応援の気持ちを素直に述べる", True),
    },
}
_Q3_EMOTION_P = {
    "default": {"要望": 0.45, "不満": 0.30, "強い怒り": 0.05, "淡々": 0.20},
    "感謝・応援": {"感謝": 1.0},
    "本調査への意見": {"淡々": 0.5, "要望": 0.3, "不満": 0.2},
}
_Q2_EMOTION_P = {"素直な肯定": 0.70, "控えめな肯定": 0.20, "淡々": 0.10}
_Q1_EMOTION_SHARES = {"要望": 500, "不満": 380, "強い怒り": 50, "肯定まじり": 70}
# Q1 の感情は満足度で重みを変える（満足度1〜5の順）
_Q1_EMOTION_BY_SAT = {
    "強い怒り": (3.0, 2.0, 1.0, 0.4, 0.2),
    "肯定まじり": (0.3, 0.6, 1.0, 2.0, 3.0),
    "不満": (1.5, 1.3, 1.0, 0.8, 0.6),
}

STYLES = {
    "丁寧語": ("です・ます調", 400),
    "常体・体言止め": ("だ・である調、または体言止め", 280),
    "口語": ("話し言葉に近いくだけた書き方", 140),
    "箇条書き": ("「・」で始まる短い項目を2〜4行（改行は \\n）", 80),
    "強い口調": ("語気が強く言い切る書き方（暴言は書かない）", 60),
    "絵文字・顔文字": ("文中か文末に絵文字または顔文字を1〜2個", 40),
}
# 文体の重み（感情ごと）。強い口調は「落ち着いて」の感情と淡々では0、絵文字は淡々では0
_STRONG_STYLE_BY_EMOTION = {"強い怒り": 5.0, "不満": 1.5}
_STRONG_STYLE_ZERO = {"要望", "淡々"}
_STRONG_STYLE_POSITIVE = 0.2
_BULLET_BY_BAND = (0.0, 0.0, 1.0, 2.0, 0.0)  # 201字以上は箇条書きにしない
_EMOJI_BY_BAND = (1.0, 1.0, 1.0, 0.5, 0.0)

TRAP_TYPES = {
    "keyword_mismatch": "キーワードはあるが別カテゴリ",
    "negation": "否定",
    "vague": "曖昧",
    "sarcasm": "皮肉",
    "weak_mix": "2話題の弱い混在",
    "off_question": "設問違い",
}
# 罠の割り付け順：条件のきつい種類から先に
TRAP_ORDER = ("weak_mix", "sarcasm", "off_question", "negation", "keyword_mismatch", "vague")
OFFQ_EMOTIONS = {
    "Q1": {"不満", "強い怒り"},
    "Q2": {"控えめな肯定", "淡々"},
    "Q3": {"淡々", "不満"},
}
SARCASM_EMOTIONS = {"不満", "強い怒り"}
LONG_BANDS = ("31-80", "81-200", "201-400")
HARD_RATE = 0.15
LABEL_COUNT_SHARES = (650, 270, 80)  # 1つ／2つ／3つ（0.1%単位）
# 「その他」の行の「必ず触れる話題」。読まれやすい別カテゴリの理由に広げさせない
OTHER_TOPIC = (
    "どの決まった分類にも入らない個別の事柄として「{subject}」についてだけ書く。"
    "理由も、手間・費用・職場の雰囲気・制度の話に広げない"
)
ROUNDING_TRIES = 200  # 制御丸めで試す丸め方の数
LABEL_ATTEMPTS = 200  # 副ラベルの引き直しの上限（種固定の別の乱数列で順に試す）
SECONDARY_RETAIN = 0.6  # 全ラベルのリフトが、主ラベルのリフト（-1）の何割以上残っていてほしいか
RELATED_SHARES = (600, 400)  # 複数ラベルの行のうち、2つ目が関連ペア表から来る行／来ない行
EFFECT_ATTRS = {
    "dept": DEPTS,
    "age": AGES,
    "tenure": TENURES,
    "emp": EMPS,
    "pos": POSS,
    "sat": SATS,
}


@dataclass(frozen=True, slots=True)
class Effect:
    id: str
    when: dict[str, tuple]
    label: str
    multiplier: float

    def matches(self, r: Respondent) -> bool:
        return all(getattr(r, attr) in values for attr, values in self.when.items())


@dataclass(slots=True)
class RowSpec:
    row_id: str
    respondent_id: str
    qid: str
    kind: str
    text_fixed: str = ""
    labels: tuple[str, ...] = ()
    band: str = ""
    len_min: int = 0
    len_max: int = 0
    style: str = ""
    emotion: str = ""
    difficulty: str = ""
    trap_type: str = ""
    decoy: str = ""  # 罠で触れさせる別カテゴリのラベル名（キーワード違い・打ち消しだけ）
    trap_keyword: str = ""
    must_topics: tuple[str, ...] = ()
    avoid_topics: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class BuildResult:
    rows_by_q: dict[str, list[RowSpec]]
    effect_stats: list[dict]  # 効果ごとの期待リフトと実現リフト（Q1）
    effect_question: str


def load_effects(path: Path = EFFECTS_PATH) -> tuple[str, list[Effect]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    qid = data["question"]
    names = {lb.name for lb in QUESTIONS[qid].labels}
    effects = []
    seen = set()
    for e in data["effects"]:
        eid = e["id"]
        if eid in seen:
            raise ValueError(f"{eid}: id が重複しています")
        seen.add(eid)
        if e["label"] not in names:
            raise ValueError(f"{eid}: ラベル名が表にありません")
        m = e["multiplier"]
        if isinstance(m, bool) or not isinstance(m, int | float) or not np.isfinite(m) or m <= 0:
            raise ValueError(f"{eid}: 乗数は正の数にしてください")
        if not e["when"] or not set(e["when"]) <= set(EFFECT_ATTRS):
            raise ValueError(f"{eid}: 属性名が不正です")
        for attr, values in e["when"].items():
            vocab = EFFECT_ATTRS[attr]
            if not isinstance(values, list) or not values:
                raise ValueError(f"{eid}: {attr} の値は1つ以上のリストにしてください")
            for v in values:
                # 型も合わせる（満足度は整数。"1" と書いても一致しないので誤りとして止める）
                if type(v) is not type(vocab[0]) or v not in vocab:
                    raise ValueError(f"{eid}: {attr} の値が属性の語彙にありません")
        effects.append(
            Effect(
                id=eid,
                when={k: tuple(v) for k, v in e["when"].items()},
                label=e["label"],
                multiplier=float(m),
            )
        )
    return qid, effects


def _pick(rng: np.random.Generator, seq):
    return seq[int(rng.integers(len(seq)))]


def _fixed_text(rng: np.random.Generator, kind: str) -> str:
    if kind == "none":
        texts, w = zip(*NONE_TEXTS, strict=True)
        return str(texts[int(rng.choice(len(texts), p=np.array(w) / sum(w)))])
    if kind == "invalid":
        return str(_pick(rng, INVALID_TEXTS))
    return ""


def _assign_kinds(q: Question, rs: list[Respondent]) -> np.ndarray:
    """回答の種類。満足度5の人の「特になし」は重み2倍で、件数は偶然に任せず先に決める。"""
    rng = rng_for(f"{q.qid}:kinds")
    n = len(rs)
    counts = largest_remainder(n, q.kind_shares)  # content, none, blank, invalid
    hi = np.array([r.sat == 5 for r in rs])
    n5, n_other = int(hi.sum()), int((~hi).sum())
    none_hi = min(round_half_up(counts[1] * 2 * n5 / (2 * n5 + n_other)), n5, counts[1])
    kinds = np.full(n, -1, dtype=int)
    for mask, k in ((hi, none_hi), (~hi, counts[1] - none_hi)):
        idx = np.flatnonzero(mask)
        kinds[idx[weighted_sample(rng, np.ones(len(idx)), k)]] = 1
    rest = np.flatnonzero(kinds < 0)
    sub = allocate(rng, [counts[0], counts[2], counts[3]], [1, 2, 0])
    kinds[rest] = np.array([0, 2, 3])[sub]
    return kinds


def _assign_primary(q: Question, rs: list[Respondent], effects: list[Effect]) -> tuple[list[str], np.ndarray]:
    """主ラベル。効果の乗数を掛けた重みで IPF の期待件数を出し、制御丸めで整数にして、セルの中で割り付ける。

    回答者を「当てはまる効果が同じ」セルに分け、セル×カテゴリの期待件数を、セルの人数と
    カテゴリの件数の両方を守って整数にする。最後のカテゴリだけ重みなしの残り、にはならない。
    戻り値は (主ラベル名のリスト, 回答者×カテゴリの IPF 確率)。
    """
    rng = rng_for(f"{q.qid}:primary")
    m, n_cat = len(rs), len(q.labels)
    counts = largest_remainder(m, q.label_shares)
    col = {lb.name: i for i, lb in enumerate(q.labels)}
    w = np.ones((m, n_cat))
    for e in effects:
        for j, r in enumerate(rs):
            if e.matches(r):
                w[j, col[e.label]] *= e.multiplier
    cells: dict[tuple, list[int]] = {}
    for j in range(m):
        cells.setdefault(tuple(w[j].tolist()), []).append(j)
    members = list(cells.values())
    n_cell = np.array([len(x) for x in members])
    cell_w = np.array([list(k) for k in cells])
    expected = ipf(cell_w, counts, n_cell)
    # 効果ごとの対象（セルの集まり）の合計が期待件数に近い丸めを、種固定の乱数で選び、入れ替えで磨く
    groups = []
    for e in effects:
        mask = np.array([e.matches(rs[idx[0]]) for idx in members])
        groups.append((mask, col[e.label], float(expected[mask, col[e.label]].sum())))
    k = controlled_rounding(expected, n_cell, counts, rng=rng, tries=ROUNDING_TRIES, groups=groups)
    out = np.empty(m, dtype=int)
    p = np.empty((m, n_cat))
    for ci, idx in enumerate(members):
        seq = np.repeat(np.arange(n_cat), k[ci])
        out[idx] = seq[rng.permutation(len(seq))]
        p[idx] = expected[ci] / n_cell[ci]
    return [q.labels[c].name for c in out], p


def _extend_labels(
    q: Question,
    rs: list[Respondent],
    primary: list[str],
    n_labels: np.ndarray,
    effects: list[Effect],
    attempt: int = 0,
) -> list[tuple[str, ...]]:
    """2つ目・3つ目のラベル。

    複数ラベルの行の60%（最大剰余法で件数を確定）は関連ペア表から、残りは関連でないラベルから選ぶ。
    候補の重みには、その回答者に当てはまる効果の乗数を掛ける。「その他」は選ばない。
    """
    rng = rng_for(f"{q.qid}:labels:{attempt}")
    names = [lb.name for lb in q.labels if lb.name != OTHER]
    share = {lb.name: s for lb, s in zip(q.labels, q.label_shares, strict=True)}
    multi = [j for j, k in enumerate(n_labels) if k >= 2]
    n_rel = largest_remainder(len(multi), RELATED_SHARES)[0]
    flags = allocate(rng, [n_rel, len(multi) - n_rel], [1, 0])  # 0 が「関連ペア表から」
    want_related = dict(zip(multi, (f == 0 for f in flags), strict=True))

    def mult(j: int, name: str) -> float:
        out = 1.0
        for e in effects:
            if e.label == name and e.matches(rs[j]):
                out *= e.multiplier
        return out

    out = []
    for j, (p, k) in enumerate(zip(primary, n_labels, strict=True)):
        chosen = [p]
        for _ in range(int(k) - 1):
            related = []
            for lab in chosen:
                for nb in q.neighbors(lab):
                    if nb not in chosen and nb not in related:
                        related.append(nb)
            linked = {nb for lab in chosen for nb in q.neighbors(lab)}
            unrelated = [n for n in names if n not in chosen and n not in linked]
            if want_related[j] and related:
                pool, base = related, [1.0] * len(related)
            elif unrelated:
                pool, base = unrelated, [share[n] for n in unrelated]
            else:  # 関連でない候補がないときだけ、関連ペア表から選ぶ
                pool, base = related, [1.0] * len(related)
            w = [b * mult(j, n) for b, n in zip(base, pool, strict=True)]
            prob = np.array(w, dtype=float)
            chosen.append(pool[int(rng.choice(len(pool), p=prob / prob.sum()))])
        out.append(tuple(chosen))
    return out


def _choose_labels(
    q: Question,
    rs: list[Respondent],
    primary: list[str],
    n_labels: np.ndarray,
    effects: list[Effect],
) -> list[tuple[str, ...]]:
    """副ラベルを引く。効果のあるラベルの全ラベルのリフトが、主ラベルのリフトの一定割合を保つ引き方を選ぶ。

    副ラベルは行ごとの乱数で決まるので、件数の少ないラベルでは効果が薄まりすぎることがある。
    種固定の乱数列を順に試し、基準を満たす最初の引き方を採る（なければ不足が最小のもの）。
    """
    checks = []
    for e in effects:
        inside = np.array([e.matches(r) for r in rs])
        hit_primary = np.array([p == e.label for p in primary])
        if inside.all() or not inside.any():
            continue
        checks.append((e.label, inside, hit_primary))

    def lift(hit: np.ndarray, inside: np.ndarray) -> float:
        out_rate = hit[~inside].mean()
        return float(hit[inside].mean() / out_rate) if out_rate > 0 else np.inf

    def shortfall(labels: list[tuple[str, ...]]) -> float:
        total = 0.0
        for label, inside, hit_primary in checks:
            hit_any = np.array([label in lab for lab in labels])
            want = 1 + SECONDARY_RETAIN * (lift(hit_primary, inside) - 1)
            total += max(0.0, want - lift(hit_any, inside)) ** 2
        return total

    best, best_score = None, np.inf
    for attempt in range(LABEL_ATTEMPTS if checks else 1):
        labels = _extend_labels(q, rs, primary, n_labels, effects, attempt)
        score = shortfall(labels) if checks else 0.0
        if score < best_score:
            best, best_score = labels, score
        if score == 0.0:
            break
    assert best is not None
    return best


def _assign_emotions(q: Question, bands: list[str], rs: list[Respondent], labels: list[tuple[str, ...]]) -> list[str]:
    rng = rng_for(f"{q.qid}:emotion")
    m = len(rs)
    if q.qid == "Q1":
        names = list(_Q1_EMOTION_SHARES)
        counts = largest_remainder(m, list(_Q1_EMOTION_SHARES.values()))
        w = np.ones((m, len(names)))
        for c, name in enumerate(names):
            if name in _Q1_EMOTION_BY_SAT:
                w[:, c] = [_Q1_EMOTION_BY_SAT[name][r.sat - 1] for r in rs]
        w[[j for j, b in enumerate(bands) if b == "1-10"], names.index("肯定まじり")] = 0.0
        order = [names.index(x) for x in ("強い怒り", "肯定まじり", "不満", "要望")]
        return [names[c] for c in allocate(rng, counts, order, w)]
    out = []
    for lab in labels:
        if q.qid == "Q2":
            probs = _Q2_EMOTION_P
        else:
            probs = _Q3_EMOTION_P.get(lab[0], _Q3_EMOTION_P["default"])
        names = list(probs)
        out.append(names[int(rng.choice(len(names), p=list(probs.values())))])
    return out


def _band_index(band: str) -> int:
    return [b[0] for b in BANDS].index(band)


def _assign_styles(q: Question, bands: list[str], emotions: list[str]) -> list[str]:
    rng = rng_for(f"{q.qid}:style")
    m = len(bands)
    names = list(STYLES)
    counts = largest_remainder(m, [STYLES[s][1] for s in names])
    w = np.ones((m, len(names)))
    c = {s: i for i, s in enumerate(names)}
    bi = [_band_index(b) for b in bands]
    w[:, c["箇条書き"]] = [_BULLET_BY_BAND[b] for b in bi]
    w[:, c["絵文字・顔文字"]] = [0.0 if e == "淡々" else _EMOJI_BY_BAND[b] for e, b in zip(emotions, bi, strict=True)]
    strong = []
    for e in emotions:
        if e in _STRONG_STYLE_ZERO:
            strong.append(0.0)
        else:
            strong.append(_STRONG_STYLE_BY_EMOTION.get(e, _STRONG_STYLE_POSITIVE))
    w[:, c["強い口調"]] = strong
    order = [c[s] for s in ("箇条書き", "絵文字・顔文字", "強い口調", "口語", "常体・体言止め", "丁寧語")]
    return [names[i] for i in allocate(rng, counts, order, w)]


def decoy_candidates(q: Question, labels: tuple[str, ...], trap: str) -> list[str]:
    """罠で触れさせる別カテゴリの候補（その行のラベルと「その他」は除く）。

    キーワード違いでは、その行のラベルと関連ペアにあたるカテゴリも除く（語が別の意味で出るだけでも、
    近いカテゴリだと本文がそのカテゴリへの言及に読まれやすいため）。打ち消しは要望型のカテゴリだけ。
    """
    out = [lb.name for lb in q.labels if lb.name != OTHER and lb.name not in labels]
    if trap == "keyword_mismatch":
        linked = {nb for lab in labels for nb in q.neighbors(lab)}
        out = [n for n in out if n not in linked]
    if trap == "negation":
        out = [n for n in out if n not in q.negation_exclude]
    return out


def allowed_traps(q: Question, labels: tuple[str, ...], band: str, emotion: str, style: str = "") -> list[str]:
    """その行と両立する罠の種類。1〜10字の行は難しい回答にしない。文体とも両立するものだけにする。"""
    if band == "1-10":
        return []
    out = []
    if decoy_candidates(q, labels, "keyword_mismatch"):
        out.append("keyword_mismatch")
    if decoy_candidates(q, labels, "negation"):
        out.append("negation")
    if (len(labels) == 1 or band in LONG_BANDS) and style != "強い口調":
        out.append("vague")
    if q.qid != "Q2" and emotion in SARCASM_EMOTIONS and not set(labels) & set(q.sarcasm_exclude):
        out.append("sarcasm")
    if len(labels) == 2 and band in LONG_BANDS and style not in ("強い口調", "箇条書き"):
        out.append("weak_mix")  # ラベルがちょうど2つの行だけ（OTHER は単独なので入らない）
    if (
        emotion in OFFQ_EMOTIONS[q.qid]
        and not set(labels) & set(q.offq_exclude)
        and all(q.offq_topics(n) for n in labels if n != OTHER)
    ):
        out.append("off_question")
    return [t for t in TRAP_ORDER if t in out]


def _assign_traps(rng: np.random.Generator, allowed: list[list[str]], n_hard: int) -> dict[int, str]:
    """難しい回答の行と罠の種類。種類ごとの目標件数を決め、条件のきつい種類から割り付ける。"""
    types = [t for t in TRAP_ORDER if any(t in a for a in allowed)]
    targets = dict(zip(types, largest_remainder(n_hard, [1] * len(types)), strict=True))
    assigned: dict[int, str] = {}
    for t in TRAP_ORDER:
        if t not in targets:
            continue
        cand = [j for j in range(len(allowed)) if j not in assigned and t in allowed[j]]
        for p in weighted_sample(rng, np.ones(len(cand)), min(targets[t], len(cand))):
            assigned[cand[int(p)]] = t
    short = n_hard - len(assigned)
    if short > 0:  # 条件のきつい種類で足りなかった分は、残りの行から両立する種類を選ぶ
        cand = [j for j in range(len(allowed)) if j not in assigned and allowed[j]]
        for p in weighted_sample(rng, np.ones(len(cand)), short):
            j = cand[int(p)]
            assigned[j] = _pick(rng, allowed[j])
    return assigned


def build_question_rows(
    q: Question, respondents: list[Respondent], effects: list[Effect] | None = None
) -> tuple[list[RowSpec], np.ndarray, list[int]]:
    """戻り値は (全回答者ぶんの行, 主ラベルの IPF 確率（中身のある行ぶん）, 中身のある行の添字)。"""
    effects = effects or []
    kinds = _assign_kinds(q, respondents)

    rng_fixed = rng_for(f"{q.qid}:fixed")
    rows = [
        RowSpec(
            row_id=f"{r.respondent_id}_{q.qid}",
            respondent_id=r.respondent_id,
            qid=q.qid,
            kind=KINDS[int(k)],
        )
        for r, k in zip(respondents, kinds, strict=True)
    ]
    for row in rows:
        row.text_fixed = _fixed_text(rng_fixed, row.kind)

    content = [i for i, row in enumerate(rows) if row.kind == "content"]
    rs = [respondents[i] for i in content]
    m = len(content)

    primary, p_matrix = _assign_primary(q, rs, effects)

    # 文字数帯
    band_counts = largest_remainder(m, q.band_shares)
    band_idx = allocate(rng_for(f"{q.qid}:band"), band_counts, list(range(len(BANDS))))
    bands = [BANDS[b][0] for b in band_idx]

    # ラベル数：3つ → 2つ → 1つ（残り）。1〜10字は1つ、11〜30字は2つまで。「その他」は単独のみ
    nl_counts = largest_remainder(m, LABEL_COUNT_SHARES)
    w = np.ones((m, 3))
    not_other = np.array([0.0 if p == OTHER else 1.0 for p in primary])
    w[:, 1] = not_other * [(0.0, 0.4, 1.0, 2.0, 2.0)[b] for b in band_idx]
    w[:, 2] = not_other * [(0.0, 0.0, 1.0, 3.0, 5.0)[b] for b in band_idx]
    n_labels = allocate(rng_for(f"{q.qid}:nlabels"), nl_counts, [2, 1, 0], w) + 1
    labels = _choose_labels(q, rs, primary, n_labels, effects)

    emotions = _assign_emotions(q, bands, rs, labels)
    styles = _assign_styles(q, bands, emotions)

    # 難しい回答：中身のある回答の15%。罠はその行と両立するものから選ぶ
    allowed = [allowed_traps(q, labels[j], bands[j], emotions[j], styles[j]) for j in range(m)]
    n_hard = round_half_up(HARD_RATE * m)
    traps = _assign_traps(rng_for(f"{q.qid}:hard"), allowed, n_hard)

    rng_t = rng_for(f"{q.qid}:topics")
    all_names = [lb.name for lb in q.labels if lb.name != OTHER]
    for j, i in enumerate(content):
        row = rows[i]
        row.labels = labels[j]
        row.band = bands[j]
        row.len_min, row.len_max = BANDS[band_idx[j]][1], BANDS[band_idx[j]][2]
        row.style = styles[j]
        row.emotion = emotions[j]
        row.difficulty = "normal"

        if j in traps:
            row.difficulty = "hard"
            row.trap_type = traps[j]
            if row.trap_type in ("keyword_mismatch", "negation"):
                cands = decoy_candidates(q, row.labels, row.trap_type)
                if row.trap_type == "negation":  # 打ち消しは関連の近いカテゴリを優先（70%）
                    near = [n for lab in row.labels for n in q.neighbors(lab) if n in cands]
                    use_near = rng_t.random() < 0.7
                    row.decoy = _pick(rng_t, near) if near and use_near else _pick(rng_t, cands)
                else:  # キーワード違いは、関連ペアでないカテゴリから無作為に
                    row.decoy = _pick(rng_t, cands)
                    row.trap_keyword = _pick(rng_t, q.by_name(row.decoy).trap_keywords)[0]

        # 必ず触れる話題（先頭が中心）。設問違いは Q1・Q3 で専用の言い換えを使う
        topics = []
        subject = None
        for name in row.labels:
            if name == OTHER:
                subject = _pick(rng_t, q.other_subjects)
                topics.append(OTHER_TOPIC.format(subject=subject))
                continue
            pool = q.by_name(name).topics
            if row.trap_type == "off_question":  # Q1 は要望の言い方なし、Q3 は出来事・感想の形
                pool = q.offq_topics(name)
            topics.append(_pick(rng_t, pool))
        row.must_topics = tuple(topics)

        # 触れてはいけない話題：関連の近いカテゴリ（「その他」は話題ごとの近いカテゴリ）を優先し、3つまで
        banned = [n for n in all_names if n not in row.labels and n != row.decoy]
        near_b = []
        for lab in row.labels:
            for nb in q.neighbors(lab):
                if nb in banned and nb not in near_b:
                    near_b.append(nb)
        picked = [near_b[k] for k in rng_t.permutation(len(near_b))[:3]]
        if subject is not None:  # 「その他」の行は、話題から読まれやすい近いカテゴリを先に入れる
            picked = [n for n in q.near_categories(subject) if n in banned][:3]
        rest = [n for n in banned if n not in picked]
        while len(picked) < 3 and rest:
            picked.append(rest.pop(int(rng_t.integers(len(rest)))))
        row.avoid_topics = tuple(q.by_name(n).short for n in picked)
    return rows, p_matrix, content


def effect_stats(
    q: Question,
    respondents: list[Respondent],
    rows: list[RowSpec],
    p_matrix: np.ndarray,
    content: list[int],
    effects: list[Effect],
) -> list[dict]:
    """効果ごとの期待リフト（IPF）と実現リフト（主ラベル・全ラベル）。内と外は中身のある回答者で数える。"""
    col = {lb.name: i for i, lb in enumerate(q.labels)}
    out = []
    for e in effects:
        inside = np.array([e.matches(respondents[i]) for i in content])
        n_in, n_out = int(inside.sum()), int((~inside).sum())
        if n_in == 0 or n_out == 0:
            raise ValueError(f"{e.id}: 対象の回答者がいません")
        hit_primary = np.array([rows[i].labels[0] == e.label for i in content])
        hit_any = np.array([e.label in rows[i].labels for i in content])
        c = col[e.label]
        expected = (p_matrix[inside, c].sum() / n_in) / (p_matrix[~inside, c].sum() / n_out)

        def lift(hit, inside=inside, n_in=n_in, n_out=n_out):
            return (hit[inside].sum() / n_in) / (hit[~inside].sum() / n_out)

        out.append(
            {
                "id": e.id,
                "multiplier": e.multiplier,
                "ipf_expected_lift": round(float(expected), 4),
                "realized_lift_primary": round(float(lift(hit_primary)), 4),
                "realized_lift_any": round(float(lift(hit_any)), 4),
                "n_in": n_in,
                "n_out": n_out,
                "primary_hits_in": int(hit_primary[inside].sum()),
                "primary_hits_out": int(hit_primary[~inside].sum()),
                "any_hits_in": int(hit_any[inside].sum()),
                "any_hits_out": int(hit_any[~inside].sum()),
            }
        )
    return out


def build_all_rows(respondents: list[Respondent]) -> BuildResult:
    eq, effects = load_effects()
    rows_by_q = {}
    stats: list[dict] = []
    for qid, q in QUESTIONS.items():
        rows, p_matrix, content = build_question_rows(q, respondents, effects if qid == eq else [])
        rows_by_q[qid] = rows
        if qid == eq:
            stats = effect_stats(q, respondents, rows, p_matrix, content, effects)
    return BuildResult(rows_by_q=rows_by_q, effect_stats=stats, effect_question=eq)
