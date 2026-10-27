"""話題の言い換え（本文書き係に見せる文面）とラベルの対応、言い換えどうしの重なり。"""

import itertools
import re

import pytest

from tests.samplegen.conftest import content_rows
from tools.samplegen.answers import EMOTIONS
from tools.samplegen.codeframes import OTHER, QUESTIONS, REQUEST_WORDS, validate

QIDS = ["Q1", "Q2", "Q3"]


@pytest.mark.parametrize("qid", QIDS)
def test_must_topics_match_labels_in_order(spec_rows, qid):
    q = QUESTIONS[qid]
    for r in content_rows(spec_rows, qid):
        names = r["label_names"].split("|")
        topics = r["must_topics"].split("|")
        assert len(topics) == len(names)  # 先頭が主ラベル、以降は副ラベルの順
        for name, topic in zip(names, topics, strict=True):
            if name == OTHER:
                assert any(f"「{s}」" in topic for s in q.other_subjects)
                assert "どの決まった分類にも入らない個別の事柄として" in topic
            elif r["trap_type"] == "off_question" and qid in {"Q1", "Q3"}:
                assert topic in q.offq_topics(name)  # Q1 は要望の言い方なし、Q3 は出来事・感想の形
            else:
                assert topic in q.by_name(name).topics


def test_must_topics_never_point_at_another_label(spec_rows):
    """あるラベルの話題が、別のラベルの言い換えと同じ文面になっていない。"""
    for qid in QIDS:
        q = QUESTIONS[qid]
        owner = {t: lb.name for lb in q.labels for t in (*lb.topics, *lb.report_topics) if lb.name != OTHER}
        assert len(owner) == sum(len(lb.topics) + len(lb.report_topics) for lb in q.labels if lb.name != OTHER)
        for r in content_rows(spec_rows, qid):
            for name, topic in zip(r["label_names"].split("|"), r["must_topics"].split("|"), strict=True):
                if name != OTHER:
                    assert owner[topic] == name


@pytest.mark.parametrize("qid", QIDS)
def test_paraphrases_do_not_contain_other_labels_keywords(qid):
    """ラベル L の言い換え・短い言い方に、別のラベル M のキーワードが入っていない（全ペア）。"""
    q = QUESTIONS[qid]
    named = [lb for lb in q.labels if lb.name != OTHER]
    for lb, other in itertools.permutations(named, 2):
        for kw in other.keywords:
            for text in (*lb.topics, *lb.report_topics, lb.short):
                assert kw not in text, (qid, lb.name, other.name, kw)


@pytest.mark.parametrize("qid", QIDS)
def test_other_subjects_belong_to_no_category(qid):
    q = QUESTIONS[qid]
    for s in q.other_subjects:
        for lb in q.labels:
            if lb.name == OTHER:
                continue
            assert lb.name not in s
            for kw in lb.keywords:
                assert kw not in s, (s, lb.name, kw)
    banned_subjects = (
        "駐車場",
        "駐輪場",
        "制服",
        "作業着",
        "社内報",
        "来客対応",
        "ホームページ",
        "ロゴ",
        "創立記念日",
        "年賀状や贈答品",
        "作り方",
    )
    for s in q.other_subjects:
        assert not any(b in s for b in banned_subjects), s
        if qid in {"Q1", "Q3"}:  # Q2 には職場環境・設備に当たるカテゴリがないので残してある
            assert "社屋の外観" not in s


def test_overtime_paraphrase_is_about_time_only():
    for t in QUESTIONS["Q1"].by_name("残業・労働時間").topics:
        for word in ("仕事が多", "仕事量", "業務量", "人手", "欠員"):
            assert word not in t
    for t in QUESTIONS["Q1"].by_name("休暇の取りやすさ").topics:
        assert "連絡" not in t
    for t in QUESTIONS["Q3"].by_name("待遇改善").topics:
        assert "休暇" not in t
    assert "休暇" not in QUESTIONS["Q3"].by_name("待遇改善").definition


def test_every_q1_label_has_a_request_free_paraphrase():
    validate()
    q = QUESTIONS["Q1"]
    for lb in q.labels:
        if lb.name != OTHER:
            free = q.request_free_topics(lb.name)
            assert free and all(not REQUEST_WORDS.search(t) for t in free)


def test_request_words_regex_catches_the_obvious():
    for t in ("増員をしてほしい", "増員を望む話", "してもらいたい", "希望する"):
        assert REQUEST_WORDS.search(t)
    assert not REQUEST_WORDS.search("欠員が埋まらず手が回らない状況")


# --- 「その他」の話題（最終の一覧） ---

FINAL_OTHER_SUBJECTS = {
    "Q1": [
        "このアンケートの設問の多さや答えにくさ",
        "名刺のデザイン",
        "社用封筒や便箋のデザイン",
        "取引先への手土産の選び方",
        "社内で配られるカレンダーのデザイン",
        "会議室の名前の付け方",
    ],
    "Q2": [
        "会社の立地",
        "社用車が使いやすいこと",
        "名刺のデザインが気に入っていること",
        "社屋の外観や看板の見た目",
        "社内の観葉植物や季節の飾り付け",
    ],
    "Q3": [
        "取引先への手土産の選び方",
        "名刺のデザイン",
        "社用封筒や便箋のデザイン",
        "会議室の名前の付け方",
        "会社のマスコットキャラクター",
    ],
}


@pytest.mark.parametrize("qid", QIDS)
def test_other_subjects_are_the_agreed_list(qid):
    assert list(QUESTIONS[qid].other_subjects) == FINAL_OTHER_SUBJECTS[qid]


# --- 隣り合うカテゴリどうしの近い語 ---

# (設問, 持ち主のカテゴリ): その語は持ち主の言い換えにだけ出てよい（ほかのカテゴリの言い換え・短い言い方に出さない）
NEIGHBOUR_WORDS = {
    ("Q1", "教育・研修・キャリア"): ["道筋", "進路", "キャリア"],
    ("Q1", "部署間連携・コミュニケーション"): ["連絡", "受け渡し", "引き継ぎ"],
    ("Q1", "経営方針・情報共有"): ["説明", "公開"],
    ("Q2", "雇用の安定"): ["安心", "不安", "見通し"],
    ("Q1", "評価制度・昇進"): ["昇進", "昇格"],
    ("Q1", "残業・労働時間"): ["退勤", "定時"],
}


@pytest.mark.parametrize(("qid", "owner"), list(NEIGHBOUR_WORDS))
def test_neighbouring_categories_do_not_share_close_words(qid, owner):
    q = QUESTIONS[qid]
    for lb in q.labels:
        if lb.name in {owner, OTHER}:
            continue
        for word in NEIGHBOUR_WORDS[(qid, owner)]:
            for text in (*lb.topics, *lb.report_topics, lb.short):
                assert word not in text, (qid, lb.name, word)


def test_reworded_paraphrases():
    q1 = QUESTIONS["Q1"]
    assert "昇進・昇格の基準や、評価結果の伝えられ方" in q1.by_name("評価制度・昇進").topics
    assert "会社の業績や経営方針の変更が、社員に説明・公開されないこと" in q1.by_name("経営方針・情報共有").topics
    assert "給料の額で生活に困らないこと" in QUESTIONS["Q2"].by_name("給与・待遇").topics
    assert not any("道筋" in t for t in q1.by_name("評価制度・昇進").topics)


# --- キーワード違いの語（別の意味で使える語） ---


@pytest.mark.parametrize("qid", QIDS)
def test_trap_keywords_have_a_meaning_and_are_not_plain_core_words(qid):
    q = QUESTIONS[qid]
    for lb in q.labels:
        if lb.name == OTHER:
            assert not lb.trap_keywords
            continue
        words = [w for w, _ in lb.trap_keywords]
        assert 1 <= len(words) <= 3 and len(set(words)) == len(words)
        for word, meaning in lb.trap_keywords:
            assert word and len(meaning) >= 4
            # どのラベルのキーワード一覧にも入っている語は、必ず別の意味の説明が付いている
            assert "の意味" not in meaning  # 「…の意味で入れる」と続けるので重ねない
            assert lb.name not in meaning
        # 語そのものが、そのカテゴリの正解ラベル名になっていない
        assert all(w != lb.name for w in words)


def test_most_labels_have_two_or_more_trap_words():
    few = [
        (q.qid, lb.name)
        for q in QUESTIONS.values()
        for lb in q.labels
        if lb.name != OTHER and len(lb.trap_keywords) < 2
    ]
    assert few == []  # すべてのラベルが語を2つ以上持つ


# --- Q3 の出来事・感想の形の言い換え ---

Q3_REPORT_LABELS = [
    "経営・ビジョンへの要望",
    "待遇改善",
    "人材育成・採用",
    "働き方・制度",
    "組織風土",
    "設備・IT投資",
]


def test_q3_report_form_paraphrases():
    q = QUESTIONS["Q3"]
    have = [lb.name for lb in q.labels if lb.report_topics]
    assert have == Q3_REPORT_LABELS  # 本調査への意見・感謝・応援・その他は持たない（設問違いにしない）
    for name in have:
        topics = q.by_name(name).report_topics
        assert 2 <= len(topics)
        for t in topics:
            assert not REQUEST_WORDS.search(t), t
    assert (
        q.by_name("設備・IT投資").report_topics[0]
        == "機械や設備、システムが古く、止まったり手間がかかったりする日々の出来事"
    )
    assert QUESTIONS["Q1"].by_name("給与・賞与").report_topics == ()
    # 待遇改善の出来事の形は、お金が足りない・少ない・苦しいことが読み取れる形にする
    for t in q.by_name("待遇改善").report_topics:
        assert re.search("少な|足り|苦労", t), t


def test_q3_neutral_emotion_description_has_no_opinion_word():
    assert EMOTIONS["Q3"]["淡々"][0] == "感情をあまり出さず、淡々と述べる"


# --- キーワード違いの語と別の意味（確かめた表。意味を変えるときは、ここを意識して直す） ---

FINAL_TRAP_WORDS = [
    ("Q1", "人員不足・業務量", "定員", "観光バスや遊覧船の定員"),
    ("Q1", "人員不足・業務量", "人数", "世帯の人数（家族が何人いるか）"),
    ("Q1", "給与・賞与", "ボーナス", "ゲームの『ボーナスステージ』のような例え"),
    ("Q1", "給与・賞与", "額", "『額に汗して』という言い回し"),
    ("Q1", "評価制度・昇進", "評価", "お客様や取引先から受けた評価（口コミ）"),
    ("Q1", "評価制度・昇進", "査定", "中古車や保険の査定（見積もり）"),
    ("Q1", "上司・マネジメント", "管理", "マンションの管理組合や管理人"),
    ("Q1", "上司・マネジメント", "指示", "道案内や標識の指示"),
    ("Q1", "部署間連携・コミュニケーション", "連携", "サッカーやバスケットボールの連携プレー"),
    ("Q1", "部署間連携・コミュニケーション", "連絡", "家族や友人への私的な連絡"),
    ("Q1", "残業・労働時間", "長時間", "休日の長時間ドライブや映画"),
    ("Q1", "残業・労働時間", "定時", "電車やバスの定時運行"),
    ("Q1", "休暇の取りやすさ", "連休", "世間の連休（道路の混雑など）の話題"),
    ("Q1", "休暇の取りやすさ", "休み", "お店の定休日"),
    ("Q1", "教育・研修・キャリア", "教育", "子どもの学校教育"),
    ("Q1", "教育・研修・キャリア", "資格", "運転免許など、通勤に必要な資格"),
    ("Q1", "職場環境・設備", "設備", "自宅の設備"),
    ("Q1", "職場環境・設備", "空調", "自宅や車の空調"),
    ("Q1", "ITツール・業務効率化", "紙", "新聞や紙面の記事"),
    ("Q1", "ITツール・業務効率化", "システム", "『システムキッチン』のような家の設備"),
    ("Q1", "ITツール・業務効率化", "パソコン", "家庭で使う私物のパソコン"),
    ("Q1", "福利厚生・手当", "手当", "けがの手当（応急処置）"),
    ("Q1", "福利厚生・手当", "食堂", "旅先で入った食堂"),
    ("Q1", "柔軟な働き方", "在宅", "家にいること（在宅していた家族）"),
    ("Q1", "柔軟な働き方", "時差", "海外旅行の時差ぼけ"),
    ("Q1", "経営方針・情報共有", "方針", "自分の子育ての方針"),
    ("Q1", "経営方針・情報共有", "説明", "自宅の家電の取扱説明書"),
    ("Q1", "ハラスメント・人間関係", "パワー", "車のパワー（出力）"),
    ("Q1", "ハラスメント・人間関係", "関係", "親戚関係"),
    ("Q2", "同僚・人間関係", "先輩", "学生時代の部活の先輩"),
    ("Q2", "同僚・人間関係", "仲間", "趣味のサークルの仲間"),
    ("Q2", "仕事のやりがい", "達成感", "趣味のマラソンで味わう達成感"),
    ("Q2", "仕事のやりがい", "任され", "家庭で家事を任されること"),
    ("Q2", "休暇・WLB", "定時", "電車やバスの定時運行"),
    ("Q2", "休暇・WLB", "休み", "お店の定休日"),
    ("Q2", "雇用の安定", "安定", "机や脚立がぐらつかない安定"),
    ("Q2", "雇用の安定", "安心", "家の戸締まりをしたときの安心"),
    ("Q2", "福利厚生", "手当", "けがの手当（応急処置）"),
    ("Q2", "福利厚生", "食堂", "旅先で入った食堂"),
    ("Q2", "成長・学び", "スキル", "ゲームやスポーツの『スキル』"),
    ("Q2", "成長・学び", "資格", "運転免許など、通勤に必要な資格"),
    ("Q2", "上司", "相談", "家族や友人との私的な相談"),
    ("Q2", "上司", "管理", "マンションの管理組合や管理人"),
    ("Q2", "柔軟な働き方", "在宅", "家にいること（在宅していた家族）"),
    ("Q2", "柔軟な働き方", "時短", "料理の時短レシピ"),
    ("Q2", "製品・社会貢献への誇り", "製品", "店で買った他社の製品"),
    ("Q2", "製品・社会貢献への誇り", "社会", "学校の社会科の授業"),
    ("Q2", "給与・待遇", "ボーナス", "ゲームの『ボーナスステージ』のような例え"),
    ("Q2", "給与・待遇", "額", "『額に汗して』という言い回し"),
    ("Q3", "経営・ビジョンへの要望", "将来", "子どもなど、自分の家庭の将来"),
    ("Q3", "経営・ビジョンへの要望", "経営", "知り合いの個人商店の経営"),
    ("Q3", "待遇改善", "手当", "けがの手当（応急処置）"),
    ("Q3", "待遇改善", "還元", "買い物のポイント還元"),
    ("Q3", "人材育成・採用", "育成", "盆栽や野菜の育成（栽培）"),
    ("Q3", "人材育成・採用", "新人", "スポーツ界の新人（ルーキー）"),
    ("Q3", "働き方・制度", "ルール", "スポーツやゲームのルール"),
    ("Q3", "働き方・制度", "規則", "規則正しい生活リズム"),
    ("Q3", "組織風土", "雰囲気", "お店や街の雰囲気"),
    ("Q3", "組織風土", "風通し", "自宅の部屋の風通し（換気）"),
    ("Q3", "設備・IT投資", "設備", "自宅の設備"),
    ("Q3", "設備・IT投資", "システム", "『システムキッチン』のような家の設備"),
    ("Q3", "本調査への意見", "調査", "ニュースで見た世間の調査結果"),
    ("Q3", "本調査への意見", "匿名", "ネット上の匿名の書き込み"),
    ("Q3", "感謝・応援", "応援", "スポーツチームの応援"),
    ("Q3", "感謝・応援", "ありがとう", "買い物のときに店員さんへ言う『ありがとう』"),
]


def test_trap_keywords_are_the_vetted_table():
    got = [(q.qid, lb.name, w, m) for q in QUESTIONS.values() for lb in q.labels for w, m in lb.trap_keywords]
    assert got == FINAL_TRAP_WORDS


# --- 「その他」の話題と、読まれやすい近いカテゴリ ---

FINAL_OTHER_NEAR = {
    ("Q1", "このアンケートの設問の多さや答えにくさ"): ["経営方針・情報共有", "人員不足・業務量"],
    ("Q1", "名刺のデザイン"): ["ITツール・業務効率化", "経営方針・情報共有"],
    ("Q1", "社用封筒や便箋のデザイン"): ["ITツール・業務効率化", "経営方針・情報共有"],
    ("Q1", "取引先への手土産の選び方"): ["給与・賞与", "福利厚生・手当", "上司・マネジメント"],
    ("Q1", "社内で配られるカレンダーのデザイン"): ["経営方針・情報共有", "福利厚生・手当", "ITツール・業務効率化"],
    ("Q1", "会議室の名前の付け方"): ["職場環境・設備", "ITツール・業務効率化"],
    ("Q2", "会社の立地"): ["休暇・WLB", "柔軟な働き方"],
    ("Q2", "社用車が使いやすいこと"): ["福利厚生", "柔軟な働き方", "休暇・WLB"],
    ("Q2", "名刺のデザインが気に入っていること"): ["製品・社会貢献への誇り", "同僚・人間関係"],
    ("Q2", "社屋の外観や看板の見た目"): ["製品・社会貢献への誇り", "同僚・人間関係"],
    ("Q2", "社内の観葉植物や季節の飾り付け"): ["同僚・人間関係", "福利厚生"],
    ("Q3", "取引先への手土産の選び方"): ["働き方・制度", "待遇改善"],
    ("Q3", "名刺のデザイン"): ["組織風土", "経営・ビジョンへの要望", "設備・IT投資"],
    ("Q3", "社用封筒や便箋のデザイン"): ["組織風土", "経営・ビジョンへの要望", "設備・IT投資"],
    ("Q3", "会議室の名前の付け方"): ["組織風土", "経営・ビジョンへの要望", "設備・IT投資"],
    ("Q3", "会社のマスコットキャラクター"): ["組織風土", "経営・ビジョンへの要望", "人材育成・採用"],
}


def test_other_subjects_have_the_agreed_near_categories():
    got = {(q.qid, subj): list(names) for q in QUESTIONS.values() for subj, names in q.other_near}
    assert got == FINAL_OTHER_NEAR
    for (qid, subj), names in FINAL_OTHER_NEAR.items():
        assert subj in QUESTIONS[qid].other_subjects and 1 <= len(names) <= 3


@pytest.mark.parametrize("qid", QIDS)
def test_other_rows_forbid_their_subjects_near_categories(spec_rows, qid):
    """「その他」の行の「触れてはいけない話題」に、話題の近いカテゴリ（decoy を除いて最大3つ）が先に入っている。"""
    q = QUESTIONS[qid]
    rows = [r for r in content_rows(spec_rows, qid) if r["label_names"] == OTHER]
    assert rows
    for r in rows:
        topic = r["must_topics"]
        subject = next(s for s in q.other_subjects if f"「{s}」" in topic)
        decoy = next((lb.name for lb in q.labels if lb.code == r["decoy_code"]), None)
        want = [n for n in FINAL_OTHER_NEAR[(qid, subject)] if n != decoy][:3]
        avoid = r["avoid_topics"].split("|")
        assert [q.by_name(n).short for n in want] == avoid[: len(want)], r["row_id"]
        assert topic.endswith("理由も、手間・費用・職場の雰囲気・制度の話に広げない")
