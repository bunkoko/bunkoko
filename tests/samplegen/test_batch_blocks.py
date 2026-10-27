"""バッチの各行のブロックを、仕様表の行と突き合わせる（期待する文面はここに独立に書く）。"""

import pytest

from tools.samplegen.codeframes import QUESTIONS

STYLE_DESC = {
    "丁寧語": "です・ます調",
    "常体・体言止め": "だ・である調、または体言止め",
    "口語": "話し言葉に近いくだけた書き方",
    "箇条書き": "「・」で始まる短い項目を2〜4行（改行は \\n）",
    "強い口調": "語気が強く言い切る書き方（暴言は書かない）",
    "絵文字・顔文字": "文中か文末に絵文字または顔文字を1〜2個",
}
TRAP_NAME = {
    "keyword_mismatch": "キーワードはあるが別カテゴリ",
    "negation": "否定",
    "vague": "曖昧",
    "sarcasm": "皮肉",
    "weak_mix": "2話題の弱い混在",
    "off_question": "設問違い",
}
KEYWORD_TAIL = {
    "Q1": "についての不満や要望は書かない（語が別の意味で出るだけ）",
    "Q2": "をよかった点としては挙げない（語が別の意味で出るだけ）",
    "Q3": "についての要望・意見・感想・評価は書かない（語が別の意味で出るだけ）",
}
NEGATION_PHRASE = {
    "Q1": "問題ない・困っていない",
    "Q2": "特によいとは思わない",
    "Q3": "特に言うことはない",
}
OFF_QUESTION = {
    "Q1": "改善してほしいとは書かず、困っている現状や出来事の報告だけを書く",
    "Q2": "よかったことを、会社への要望（これからも続けてほしい、なくさないでほしい）の形で書く",
    "Q3": "会社への意見の形にせず、自分の身の回りの出来事や感想として書く",
}
WEAK_MIX = "2つの話題を1つの流れの中で混ぜ、どちらもはっきり言い切らずに書く（どちらの話題も読めば分かる程度に）"
EXTRA_NOTE = {
    "negation": "ほかの話題は、下の「難しい回答」で指定された一言のほかは足さない",
    "keyword_mismatch": "ほかの話題は、下の「難しい回答」で指定された語のほかは足さない",
}


@pytest.fixture(scope="module")
def blocks(batches):
    """row_id → (バッチ名, ブロックの行のリスト)"""
    out = {}
    for name, text in batches.items():
        for block in text.split("\n### ")[1:]:
            lines = block.rstrip("\n").split("\n")
            out[lines[0].strip()] = (name, lines[1:])
    return out


def _field(lines, prefix):
    hits = [ln for ln in lines if ln.startswith(prefix)]
    assert len(hits) == 1, prefix
    return hits[0][len(prefix) :]


def test_every_block_matches_its_spec_row(spec_rows, respondents, blocks, emotion_desc):
    by_resp = {r["respondent_id"]: r for r in respondents}
    content = [r for r in spec_rows if r["kind"] == "content"]
    assert len(blocks) == len(content) == 1950
    for r in content:
        name, lines = blocks[r["row_id"]]
        qid = r["question"]
        q = QUESTIONS[qid]
        assert name.startswith(qid.lower())
        resp = by_resp[r["respondent_id"]]

        # 回答者と人物設定
        want = (
            f"部署={resp['dept']}／年代={resp['age']}／勤続年数={resp['tenure']}／雇用形態={resp['emp']}"
            f"／役職={resp['pos']}／総合満足度={resp['sat']}（5段階。1が不満、5が満足）"
        )
        assert _field(lines, "- 回答者：") == want
        persona = _field(lines, "- 人物設定：")
        traits = resp["traits"].split("|")
        assert persona.count(" — ") == len(traits)
        for t in traits:
            assert t + " — " in persona

        # 必ず触れる話題
        must = _field(lines, "- 必ず触れる話題：")
        topics = r["must_topics"].split("|")
        for t in topics:
            assert t in must
        trap = r["trap_type"]
        if trap == "weak_mix":
            assert must.startswith("どれも同じ重さで：")
            assert must.endswith("（この範囲だけを書く）")
        elif len(topics) == 1:
            assert must.startswith(topics[0] + "（この話題だけを書く。")
            if trap in EXTRA_NOTE:
                assert must.endswith(EXTRA_NOTE[trap] + "）")
            else:
                assert must.endswith("ほかの話題は足さない）")
        else:
            assert must.startswith("中心：" + topics[0] + " ／ あわせて軽く：")
            if trap in EXTRA_NOTE:
                assert must.endswith(EXTRA_NOTE[trap] + "）")
            else:
                assert must.endswith("（この範囲だけを書く）")
        assert not any(n in must for k, n in EXTRA_NOTE.items() if k != trap)
        assert (" ／ ".join(topics[1:]) in must) or len(topics) == 1

        # 触れてはいけない話題：その行のラベルの言い方が入っていない
        avoid = r["avoid_topics"].split("|")
        assert _field(lines, "- 触れてはいけない話題：") == " ／ ".join(avoid)
        own_shorts = {q.by_name(n).short for n in r["label_names"].split("|")}
        assert not own_shorts & set(avoid)

        # 文字数・文体・感情
        assert _field(lines, "- 文字数：") == f"{r['len_min']}〜{r['len_max']}字"
        assert _field(lines, "- 文体：") == f"{r['style']} — {STYLE_DESC[r['style']]}"
        assert _field(lines, "- 感情：") == f"{r['emotion']} — {emotion_desc[qid][r['emotion']]}"

        # 難しい回答
        hard = _field(lines, "- 難しい回答：")
        if r["difficulty"] == "normal":
            assert hard == "なし（ふつうに書く）"
            continue
        assert hard.startswith(TRAP_NAME[trap] + "：")
        decoy = next((lb for lb in q.labels if lb.code == r["decoy_code"]), None)
        if trap == "keyword_mismatch":
            meaning = dict(q.by_name(decoy.name).trap_keywords)[r["trap_keyword"]]
            assert f"文中に「{r['trap_keyword']}」という語を、{meaning}の意味で入れる" in hard
            assert f"（「{decoy.short}」の話はしない）。「{decoy.short}」{KEYWORD_TAIL[qid]}" in hard
            assert "一言" not in hard
        elif trap == "negation":
            assert f"「{decoy.short}」については「{NEGATION_PHRASE[qid]}」という趣旨で" in hard
        elif trap == "off_question":
            assert hard.endswith(OFF_QUESTION[qid])
        elif trap == "weak_mix":
            assert hard.endswith(WEAK_MIX)
            assert decoy is None and len(topics) == 2
        elif trap == "sarcasm":
            assert "皮肉や反語" in hard and "本心は不満・怒り" in hard
        else:
            assert "遠回しで曖昧" in hard
        if decoy is None:
            assert "「" not in hard  # 別カテゴリの話題を名指ししない罠には、かぎ括弧の指定がない


@pytest.fixture(scope="module")
def emotion_desc():
    return {
        "Q1": {
            "要望": "改善を落ち着いて求める、前向きな要望の調子",
            "不満": "不満や困りごとをはっきり述べる",
            "強い怒り": "怒りや憤りがにじむ強い調子（暴言・差別語・個人攻撃は書かない）",
            "肯定まじり": "現状のよい面にも一言触れつつ（同じ話題の範囲で）、改善を求める",
        },
        "Q2": {
            "素直な肯定": "素直に喜びや感謝を述べる",
            "控えめな肯定": "「悪くない」「それなりに」程度の控えめな肯定",
            "淡々": "感情をあまり出さず、事実を淡々と述べる（否定的にはしない）",
        },
        "Q3": {
            "要望": "会社への要望を落ち着いて述べる",
            "不満": "不満や疑問をはっきり述べる",
            "強い怒り": "怒りや憤りがにじむ強い調子（暴言・差別語・個人攻撃は書かない）",
            "淡々": "感情をあまり出さず、淡々と述べる",
            "感謝": "感謝や応援の気持ちを素直に述べる",
        },
    }


def test_negation_and_keyword_text_differs_by_question(blocks):
    seen = {"negation": set(), "keyword_mismatch": set()}
    for _name, lines in blocks.values():
        hard = _field(lines, "- 難しい回答：")
        for trap, label in (("negation", "否定："), ("keyword_mismatch", "キーワードはあるが別カテゴリ：")):
            if hard.startswith(label):
                for qid in ("Q1", "Q2", "Q3"):
                    tail = NEGATION_PHRASE[qid] if trap == "negation" else KEYWORD_TAIL[qid]
                    if tail in hard:
                        seen[trap].add(qid)
    assert seen["negation"] == {"Q1", "Q2", "Q3"}
    assert seen["keyword_mismatch"] == {"Q1", "Q2", "Q3"}
