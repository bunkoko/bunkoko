"""本文書き係（.claude/agents/sample-writer.md）に渡すバッチファイルを作る（design.md 6.2 手順3の入力）。

中身のある回答だけを、設問ごとに約20行ずつ。ラベル名・コード番号は見せず、
話題の言い換え（codeframes.py の topics）だけを「必ず触れる話題」として渡す。
人手の見本と禁止フレーズは、あとで差し込める欄だけ用意しておく。
"""

import math

from tools.samplegen.answers import EMOTIONS, STYLES, TRAP_TYPES, RowSpec
from tools.samplegen.attributes import TRAIT_DESCRIPTIONS, Respondent
from tools.samplegen.codeframes import QUESTIONS, Question
from tools.samplegen.sampling import rng_for

BATCH_SIZE = 20

HUMAN_SAMPLES_BEGIN = "<!-- HUMAN_SAMPLES:BEGIN -->"
HUMAN_SAMPLES_END = "<!-- HUMAN_SAMPLES:END -->"
BANNED_BEGIN = "<!-- BANNED_PHRASES:BEGIN -->"
BANNED_END = "<!-- BANNED_PHRASES:END -->"
HUMAN_SAMPLES_EMPTY = "（人手の見本は未設定。入ったら、このバッチ向けの3件をここに差し込む）"
BANNED_EMPTY = "（なし。全体の2%以上の回答に出た言い回しを、ここに足していく）"

_OFF_QUESTION = {
    "Q1": "改善してほしいとは書かず、困っている現状や出来事の報告だけを書く",
    "Q2": "よかったことを、会社への要望（これからも続けてほしい、なくさないでほしい）の形で書く",
    "Q3": "会社への意見の形にせず、自分の身の回りの出来事や感想として書く",
}
# 語だけ出す罠（キーワード違い）と一言だけ触れる罠（打ち消し）の文面は設問ごとに変える
_KEYWORD_TAIL = {
    "Q1": "についての不満や要望は書かない（語が別の意味で出るだけ）",
    "Q2": "をよかった点としては挙げない（語が別の意味で出るだけ）",
    "Q3": "についての要望・意見・感想・評価は書かない（語が別の意味で出るだけ）",
}
_NEGATION_PHRASE = {
    "Q1": "問題ない・困っていない",
    "Q2": "特によいとは思わない",
    "Q3": "特に言うことはない",
}
EXTRA_TOPIC_NOTE = "ほかの話題は、下の「難しい回答」で指定された一言のほかは足さない"  # 打ち消し
EXTRA_WORD_NOTE = "ほかの話題は、下の「難しい回答」で指定された語のほかは足さない"  # キーワード違い
_PROFILE_SAT = "5段階。1が不満、5が満足"


def split_batches(n: int, size: int = BATCH_SIZE) -> list[int]:
    """n 行を約 size 行ずつに、行数がそろうように分ける（各バッチの行数を返す）。"""
    k = max(1, math.ceil(n / size))
    base, extra = divmod(n, k)
    return [base + 1 if i < extra else base for i in range(k)]


def batch_plan(rows_by_q: dict[str, list[RowSpec]]) -> list[tuple[str, list[RowSpec]]]:
    """(バッチ名, 行のリスト) を設問順・番号順に返す。行の並びは乱数の種で固定。"""
    plan = []
    for qid in QUESTIONS:
        content = [r for r in rows_by_q[qid] if r.kind == "content"]
        order = rng_for(f"{qid}:batch-order").permutation(len(content))
        shuffled = [content[i] for i in order]
        pos = 0
        for b, size in enumerate(split_batches(len(shuffled)), start=1):
            plan.append((f"{qid.lower()}_{b:03d}", shuffled[pos : pos + size]))
            pos += size
    return plan


def _trap_text(q: Question, row: RowSpec) -> str:
    if row.difficulty != "hard":
        return "なし（ふつうに書く）"
    name = TRAP_TYPES[row.trap_type]
    decoy = q.by_name(row.decoy).short if row.decoy else ""
    t = row.trap_type
    if t == "keyword_mismatch":
        meaning = q.trap_meaning(row.decoy, row.trap_keyword)
        how = (
            f"文中に「{row.trap_keyword}」という語を、{meaning}の意味で入れる（「{decoy}」の話はしない）。"
            f"「{decoy}」{_KEYWORD_TAIL[q.qid]}"
        )
    elif t == "negation":
        how = (
            f"「{decoy}」については「{_NEGATION_PHRASE[q.qid]}」という趣旨で、一言だけ触れる。"
            "本当に言いたいのは「必ず触れる話題」"
        )
    elif t == "vague":
        how = "話題を直接言い切らず、遠回しで曖昧な言い方にする（読めば何の話かは分かる程度に）"
    elif t == "sarcasm":
        how = "皮肉や反語の言い方にする（言葉どおりではなく、裏の意味が本心。本心は不満・怒り）"
    elif t == "weak_mix":
        how = "2つの話題を1つの流れの中で混ぜ、どちらもはっきり言い切らずに書く（どちらの話題も読めば分かる程度に）"
    else:
        how = _OFF_QUESTION[q.qid]
    return f"{name}：{how}"


def must_text(row: RowSpec) -> str:
    """「必ず触れる話題」の行の中身。罠の種類で末尾の注意書きが変わる。"""
    topics = row.must_topics
    note = {"keyword_mismatch": EXTRA_WORD_NOTE, "negation": EXTRA_TOPIC_NOTE}.get(row.trap_type)
    if row.trap_type == "weak_mix":
        return f"どれも同じ重さで：{' ／ '.join(topics)}（この範囲だけを書く）"
    if len(topics) == 1:
        tail = f"（この話題だけを書く。{note}）" if note else "（この話題だけを書く。ほかの話題は足さない）"
        return f"{topics[0]}{tail}"
    rest = " ／ ".join(topics[1:])
    tail = f"（この範囲だけを書く。{note}）" if note else "（この範囲だけを書く）"
    return f"中心：{topics[0]} ／ あわせて軽く：{rest}{tail}"


def _row_block(q: Question, row: RowSpec, r: Respondent) -> str:
    emo_desc = EMOTIONS[q.qid][row.emotion][0]
    must = must_text(row)
    traits = "／".join(f"{t} — {TRAIT_DESCRIPTIONS[t]}" for t in r.traits)
    lines = [
        f"### {row.row_id}",
        f"- 回答者：部署={r.dept}／年代={r.age}／勤続年数={r.tenure}／雇用形態={r.emp}"
        f"／役職={r.pos}／総合満足度={r.sat}（{_PROFILE_SAT}）",
        f"- 人物設定：{traits}",
        f"- 必ず触れる話題：{must}",
        f"- 触れてはいけない話題：{' ／ '.join(row.avoid_topics)}",
        f"- 文字数：{row.len_min}〜{row.len_max}字",
        f"- 文体：{row.style} — {STYLES[row.style][0]}",
        f"- 感情：{row.emotion} — {emo_desc}",
        f"- 難しい回答：{_trap_text(q, row)}",
    ]
    return "\n".join(lines)


def render_batch(name: str, q: Question, rows: list[RowSpec], respondents: dict[str, Respondent]) -> str:
    parts = [
        f"# 本文書き係へのバッチ {name}",
        "",
        f"- 出力先：`raw_llm/{name}.json`（このデータ名のフォルダの下）。"
        '形式は `{"items": [{"row_id": "...", "text": "..."}]}`。',
        "- 行は下の並びのまま、追加・削除・並べ替えをしない。",
        f"- 行数：{len(rows)}",
        "",
        "## 設問",
        "",
        q.text,
        "",
        "## 共通の注意",
        "",
        "- すべて架空の回答。実在の人名・企業名（社名が必要なら「A社」）・商品名・地名・連絡先は書かない。",
        "- 「必ず触れる話題」は内容の指定。この言い換えの文面をそのまま写さず、"
        "回答者らしい言葉で書く。分類名やコード番号のような言い方もしない。",
        "- 「触れてはいけない話題」には触れない。人物設定は語り口の背景にとどめ、新しい話題にしない。",
        "- 文字数・文体・感情を守る。書き出しを行ごとに変え、同じ言い回しを繰り返さない。",
        "- text には回答の本文だけを書く（見出し・説明・かぎ括弧で囲むことはしない）。",
        "",
        "## 人手の見本（文体の参考。真似しない）",
        "",
        HUMAN_SAMPLES_BEGIN,
        HUMAN_SAMPLES_EMPTY,
        HUMAN_SAMPLES_END,
        "",
        "## 禁止フレーズ",
        "",
        BANNED_BEGIN,
        BANNED_EMPTY,
        BANNED_END,
        "",
        "## 行",
        "",
        "",
    ]
    blocks = [_row_block(q, row, respondents[row.respondent_id]) for row in rows]
    return "\n".join(parts) + "\n\n".join(blocks) + "\n"
