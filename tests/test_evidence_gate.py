"""作り話の調査・研究・数字を止める G1 と、コメントへの問いかけの検査（D-035）。"""

import json

from koten.db import connect
from koten.script import gates
from koten.script.data import EpisodeData
from tests.test_script import (
    CATEGORIES,
    FakeJudge,
    FakeScriptLLM,
    env,  # noqa: F401  フィクスチャ
    planned,
    run,
)

# 調べ役が見つけた、今の G1 を通っていた作り話（2026-11-05）
INVENTED = [
    "ある調査では、日本の会社員の7割がこの悩みを抱えています。",
    "研究によると、比べる心は誰にでもあることが分かっています。",
    "3人に1人が同じ悩みを持っています。",
    "約半数の人が、そう感じていると言われています。",
    "心理学の実験で、科学的に証明されています。",
    "ハーバード大学の研究では、孤独はたばこ15本分の害があるそうです。",
    "職場のいじめの相談は増え続けています。",
    "こうした悩みは年々増えています。",
    "多くの若者がこの悩みを抱えています。",
    "最新の調査では、若い人ほど悩んでいます。",
    "専門家は、比べるのをやめるよう勧めています。",
    "九割の人が、そう答えました。",
    # 2026-11-05 の見直しで見つかった、すり抜け
    "ハーバード大学の調べでは、孤独はたばこ15本分の害があるそうです。",
    "ある調査結果によると、若い人ほど悩んでいます。",
    "比べる人ほど落ち込みやすいことが知られています。",
    "比べる人ほど落ち込みやすいというデータがあります。",
    "厚生労働省によると、仕事の悩みを抱える人は多いそうです。",
    "比べる心については、面白い研究があります。",
    "心理学の世界では、これを社会的比較と呼びます。",
    "これはバーナム効果と呼ばれる現象です。",
    "日本の会社員の七十パーセントが、この悩みを抱えています。",
    "十人中八人が、同じ悩みを持っています。",
    "会社員の3分の1が、同じ悩みを持っています。",
    "毎年、およそ5万人が仕事を辞めています。",
    "多くの方が、同じ悩みを抱えています。",
    "若者の多くは、同期と比べてしまいます。",
    "同期と比べて落ち込む若者が増えています。",
    "日本は、世界で最も自己肯定感が低い国です。",
    "日本人は、人の目を気にしすぎると言われます。",
    "医師によると、眠りが足りないと人と比べやすくなるそうです。",
    "Z世代は、ほかの世代よりも比べやすいのです。",
    "職場の悩みの第1位は、人間関係です。",
    # 見直しで見つかった言い方（研究で分かった、の過去形・言い伝え・機関の名前）
    "比べる人ほど落ち込みやすいことが分かりました。",
    "比べる人ほど落ち込みやすいことが明らかになりました。",
    "比べる人ほど落ち込みやすいことが判明しました。",
    "比べる人ほど落ち込みやすいことが分かってきました。",
    "比べる人ほど落ち込みやすいことが、明らかになっています。",
    "比べる人ほど落ち込みやすいことが確かめられています。",
    "比べる人ほど落ち込みやすいと考えられています。",
    "比べる人ほど落ち込みやすいと指摘されています。",
    "比べる人ほど落ち込みやすいと報告されています。",
    "比べる人ほど落ち込みやすいという説があります。",
    "比べる人ほど落ち込みやすいとも言われています。",
    "比べる人ほど落ち込みやすいと言われることもあります。",
    "ある研究は、比べる人ほど不幸だと示しています。",
    "研究によって、比べる人ほど不幸だと分かっています。",
    "ある研究から、比べる人ほど不幸だと分かりました。",
    "多くの研究は、比べる人ほど不幸だと示しています。",
    "ある調査は、比べる人ほど不幸だと示しています。",
    "人と比べると幸福感が下がる傾向が見られます。",
    "誰かに話すだけで、心が軽くなると言われています。",
    "周りに合わせる人ほど疲れやすいと言われています。",
    "友人に囲まれている人ほど、長生きしやすいそうです。",
    "上司に「まだか」と言われていますが、比べる人ほど不幸になると言われています。",
    "ハーバード大学によれば、人は比べるほど不幸になります。",
    "ハーバード大学の教授は、比べる心を手放せと説きます。",
    "スタンフォード大学の発表によると、比べる人ほど疲れます。",
    "海外の論文にも、似た話があります。",
    "人は比べることで不幸になる、というのが定説です。",
    "これは確証バイアスと呼ばれる心の働きです。",
    "単純接触効果という言葉があります。",
    "これは認知的不協和の一つです。",
    "学習性無力感に近い状態です。",
    "社会的比較理論で説明できます。",
    "インポスター症候群に悩む人もいます。",
    "人と比べると、脳の中ではストレスホルモンが出ています。",
    "人間の脳は、もともと他人と比べるようにできています。",
    "ハーバード大学の教授によると、比べる心は消せません。",
    "ある学者は、比べる心を手放せと言いました。",
    "フロイトによれば、比べる心は幼いころに育ちます。",
    "毎年およそ2000人が、仕事を辞めています。",
    "数万人の若者が、同じ悩みを持っています。",
    "何万人もの人が、同じ悩みを持っています。",
    "日本の若者の自己肯定感は、ほかの国より低いのです。",
    "人は比べるほど不幸になることが分かっています。",
    "あなたは知らないかもしれませんが、比べるほど不幸になることが分かっています。",
    "私たちは、比べるほど不幸になることが知られています。",
    "あなたの周りでも、こうした悩みは年々増えています。",
    "あなたの職場でも悩みは年々増えています。",
    "うつで休職する社員が増えています。",
    "孤独死は年々増えています。",
    "「比べる人は不幸になる」と言われます。",
    "「人と比べるな」とも言われています。",
]
# 普通の文（止めない）
PLAIN = [
    "自分の役割を果たそうとして、疲れてしまうことがあります。",
    "人一倍がんばってきた人ほど、そう感じるのかもしれません。",
    "割り切れない気持ちが残ります。",
    "二つの見方を比べてみましょう。",
    "自分の気持ちを、少し調べてみましょう。",
    "孤独を感じる人は少なくないのかもしれません。",
    "この一節は、比べる心とどう付き合うかを教えてくれます。",
    # 2026-11-05 の見直しで見つかった、止めすぎ
    "大勢の人の中にいても、ひとりぼっちだと感じることがあります。",
    "この一節は、長いあいだ多くの人に読み継がれてきました。",
    "比べても意味がないことが分かっていても、つい比べてしまいます。",
    "やるべきことが分かっているのに、動けない日もあります。",
    "「真面目だね」と、よく言われませんか。",
    "上司からは、毎日のように「まだか」と言われています。",
    "多くの方法があります。",
    "仕事の量が増えていくと、誰でも疲れてしまいます。",
    "頭では、比べても仕方ないと分かっています。",
    "この一節を読むと、孔子も同じことで悩んでいたことが分かります。",
    "あなたも、真面目すぎると言われることがあるかもしれません。",
    "上司からは、遅いとも言われています。",
    "自分のことが少しずつ分かってきました。",
    "人は、自分を責める傾向があります。",
    "上司にそう言われています。",
    "大学では、友だちがなかなかできませんでした。",
    "卒業論文に追われる毎日です。",
    "大学の先輩から、よく励まされました。",
    "台風のような自然現象には、誰も逆らえません。",
    "頭脳が冴える朝もあります。",
    "あの日の言葉が、脳裏に焼きついています。",
    "百人一首の歌に、こんな一首があります。",
    "10人の同期のうち、昇進したのは1人だけでした。",
    "同期が100人いる会社です。",
    "無力感に襲われる夜もあります。",
    "理論上は、休めば治るはずです。",
    "頑張りすぎると、かえって逆効果です。",
    "ほかの人より遅れている気がします。",
    "数人の同期と、昼ごはんを食べます。",
    "何人かの先輩に、相談してみました。",
    "{{author:C1}}には、人の心が移ろいやすいことが分かっていました。",
    "孔子には、自分のなすべきことが分かっていたのでしょう。",
    "自分に足りないことが分かっているからこそ、人は学び続けます。",
    "本当に大切なことが分かっていれば、比べずにすみます。",
    "やるべきことが分かっているなら、あとは一歩だけです。",
    "あなたはもう、自分が頑張ってきたことが分かっているはずです。",
    "あなたは、自分が悪くないことが分かっています。",
    "あなたの仕事が増えてきて、息をつく暇もありません。",
    "私の白髪は年々増えていきます。",
    "仲間と一緒なら、喜びは倍増します。",
    "上司に「まだか」と言われます。",
]


def chapter(*texts, stat_after=None):
    segs = [{"seg_id": f"ch9-s{i}", "kind": "narration", "text": t} for i, t in enumerate(texts)]
    if stat_after is not None:
        segs.insert(
            stat_after + 1,
            {"seg_id": "ch9-st", "kind": "stat", "text": "{{stat:rodo_r7_amount}}"},
        )
    return {"chapter_id": "ch9", "role": "context", "segments": segs}


def load(env):  # noqa: F811
    eid = planned(env, "C", theme="孤独")
    conn = connect(env.ssd_root)
    data = EpisodeData.load(conn, env.ssd_root, eid)
    row = conn.execute("SELECT * FROM stats WHERE stat_id = 'rodo_r7_amount'").fetchone()
    data.stats["rodo_r7_amount"] = dict(row)
    return data


def evidence(issues):
    return [i for i in issues if gates.EVIDENCE in i]


def test_invented_evidence_fails_g1(env):  # noqa: F811
    data = load(env)
    for text in INVENTED:
        assert evidence(gates.g1_chapter(chapter(text), data, [])), text


def test_plain_sentences_pass(env):  # noqa: F811
    data = load(env)
    for text in PLAIN:
        assert evidence(gates.g1_chapter(chapter(text), data, [])) == [], text


def test_survey_words_only_next_to_a_stat(env):  # noqa: F811
    data = load(env)
    lead = "国の調査では、強いストレスの中身がこうなっています。"
    many = "多くの人が、同じ悩みを抱えています。"
    for text in (lead, many):
        assert evidence(gates.g1_chapter(chapter(text), data, []))
        # 統計の段落のすぐ前・すぐあとなら言える
        assert evidence(gates.g1_chapter(chapter(text, stat_after=0), data, [])) == []
        ch = chapter("前置きです。", text, stat_after=0)  # 統計のすぐあと
        assert evidence(gates.g1_chapter(ch, data, [])) == []
        far = chapter(text, "あいだの段落です。", "もうひとつ。", stat_after=2)
        assert evidence(gates.g1_chapter(far, data, []))
    # 研究・数字・「最新の調査」は、統計の隣でも言えない
    for text in ("研究では、そう分かっています。", "4割の人です。", "最新の調査を見ます。"):
        assert evidence(gates.g1_chapter(chapter(text, stat_after=0), data, [])), text


def test_hearsay_needs_a_record(env):  # noqa: F811
    data = load(env)
    assert evidence(gates.g1_chapter(chapter("昔からそう言われています。"), data, []))
    assert evidence(gates.g1_chapter(chapter("よく言われることですが。"), data, []))
    asked = "「もっと頑張れ」と言われていませんか。上司にそう言われていた日のことです。"
    assert evidence(gates.g1_chapter(chapter(asked), data, [])) == []
    fid = "F-X"
    text = f"{{{{fact:{fid}}}}}と言われています。"
    issues = gates.evidence_issues(text, "", False)
    assert issues == []  # 事実の記号と同じ文なら言える（事実のほうは別に確かめる）


def test_words_in_the_passage_are_allowed():
    assert gates.evidence_issues("半数の者は去った。", "半数の者は去った", False) == []


def test_titles_and_chapter_titles(env):  # noqa: F811
    data = load(env)
    assert any("研究" in x for x in gates.title_issues("研究で分かった、比べる心の正体", data))
    assert any("7割" in x for x in gates.title_issues("7割が悩む同期の昇進", data))
    ch = {
        "chapter_id": "ch9",
        "segments": [{"seg_id": "ch9-s0", "kind": "transition", "text": "調査では何が分かるか"}],
    }
    assert evidence(gates.g1_chapter(ch, data, []))


def test_question_checks(env):  # noqa: F811
    data = load(env)
    ok = "あなたが比べてしまうのは、どんなときですか。"
    assert gates.question_issues(ok, data, CATEGORIES) == []
    assert gates.question_issues("", data, CATEGORIES) == []
    assert gates.question_issues("あなたの勤め先ではどうですか。", data, CATEGORIES)
    assert gates.question_issues("多くの人が悩む今、あなたは？", data, CATEGORIES)
    assert gates.question_issues("あ" * 61, data, CATEGORIES)
    blocked = next(w for words in CATEGORIES.values() for w in words if len(w) >= 2)
    assert gates.question_issues(f"{blocked}について教えてください。", data, CATEGORIES)


def test_bad_question_is_rewritten_then_replaced(env):  # noqa: F811
    class BadQuestion(FakeScriptLLM):
        def __init__(self, bad):
            super().__init__()
            self.bad = 0
            self.bad_questions = bad

        def complete(self, system, user, schema=None):
            out = json.loads(super().complete(system, user, schema))
            if out["comment_prompt"] and self.bad_questions > 0:
                self.bad_questions -= 1
                out["comment_prompt"] = "あなたの勤め先の名前と、ストレスの原因を教えてください"
            return json.dumps(out, ensure_ascii=False)

    eid = planned(env, "A", worry_id="W0001")
    llm = BadQuestion(1)
    r, _ = run(env, eid, writer_llm=llm)
    assert r.script["comment_prompt"] == "あなたが比べてしまうのはどんなときですか"
    assert any("勤め先" in u for u in llm.users)  # 書き直しの指示に入った

    eid2 = planned(env, "A", NOW_PLUS(), worry_id="W0004")
    r2, _ = run(env, eid2, writer_llm=BadQuestion(99))
    assert r2.state == "script_done"  # 問いかけでは止めない
    assert r2.script["comment_prompt"] == gates.FALLBACK_QUESTION
    assert any("決まった問い" in x for x in r2.verification["viewing"]["issues"])


def NOW_PLUS():
    from datetime import timedelta

    from tests.test_script import NOW

    return NOW + timedelta(days=40)


def test_trend_only_next_to_a_checked_trend(env):  # noqa: F811
    """「増え続けています」は、同じ調査で増え方を人が確かめた統計（trend）の隣でだけ言える。"""
    data = load(env)
    text = "こうした相談は、増え続けています。"
    ch = chapter(text, stat_after=0)
    assert evidence(gates.g1_chapter(ch, data, []))  # trend の無い統計の隣
    data.stats["rodo_r7_amount"]["trend"] = "増加"
    assert evidence(gates.g1_chapter(ch, data, [])) == []
    down = chapter("こうした相談は、減り続けています。", stat_after=0)
    assert evidence(gates.g1_chapter(down, data, []))  # 向きが違う
    assert evidence(gates.g1_chapter(chapter(text), data, []))  # 統計の隣でない


def test_stats_trend_columns_and_old_db(tmp_path):
    """trend には根拠が要る。前の版の DB（trend の列が無い）にも読み込める。"""
    import sqlite3

    import pytest

    from koten.db import StatsError, connect, db_path, load_stats

    path = db_path(tmp_path)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as old:  # 前の版の stats 表
        old.execute(
            "CREATE TABLE stats (stat_id TEXT PRIMARY KEY, theme TEXT NOT NULL,"
            " label TEXT NOT NULL, value TEXT NOT NULL, unit TEXT, survey TEXT, year TEXT,"
            " url TEXT NOT NULL)"
        )
    conn = connect(tmp_path)
    head = "stat_id,theme,label,value,unit,survey,year,url,trend,trend_note\n"
    row = "s1,孤独,ラベル,10,%,調査,2025,https://example.go.jp/a"
    csv = tmp_path / "stats.csv"
    csv.write_text(head + row + ",増加,令和元年から令和7年まで毎年（同じ設問）\n", "utf-8")
    assert load_stats(conn, csv) == 1
    got = conn.execute("SELECT trend, trend_note FROM stats").fetchone()
    assert tuple(got) == ("増加", "令和元年から令和7年まで毎年（同じ設問）")
    csv.write_text(head + row + ",増加,\n", "utf-8")
    with pytest.raises(StatsError, match="trend_note"):
        load_stats(conn, csv)
    csv.write_text(head + row + ",横ばい,x\n", "utf-8")
    with pytest.raises(StatsError, match="trend"):
        load_stats(conn, csv)
    old_csv = tmp_path / "old.csv"  # trend の列の無い前の stats.csv も読める
    old_csv.write_text("stat_id,theme,label,value,unit,survey,year,url\n" + row + "\n", "utf-8")
    assert load_stats(conn, old_csv) == 1


def test_review_fixes_headlines_passage_and_ch0(env):  # noqa: F811
    data = load(env)
    for t in (
        "脳科学が教える、比べる心の正体",
        "10人中7人が悩む同期の昇進",
        "調査で判明、若者の悩み",
    ):
        assert any(gates.EVIDENCE in x for x in gates.title_issues(t, data)), t
    # 一節にある言葉でも、今の人の話に使えば止める（一節を「」で引くのはかまわない）
    passage = "世の中の多くの人が、名を求めて走る。半数の者は去った。"
    claim = "日本の会社員の半数が、同じ悩みを抱えています。"
    assert gates.evidence_issues(claim, passage, False)
    assert gates.evidence_issues("「半数の者は去った」と書かれています。", passage, False) == []
    # オープニングの章タイトル（段落にならない）も確かめる
    ch = {"chapter_id": "ch0", "title": "研究で分かった比べる心", "segments": []}
    assert any("章タイトル" in x and gates.EVIDENCE in x for x in gates.g1_chapter(ch, data, []))


def test_fact_text_grounds_expert_words(env):  # noqa: F811
    """確認済みの事実の文にある言葉（作者が「精神科医」）は、その事実の記号のある段落では止めない。"""
    from types import SimpleNamespace

    data = load(env)
    data.facts["F-X"] = SimpleNamespace(text_ja="甲野一郎は歌人で、精神科医でもあった。", slots={})
    text = "{{fact:F-X}}彼は、精神科医でもありました。"
    ch = {
        "chapter_id": "ch9",
        "segments": [{"seg_id": "ch9-s1", "kind": "narration", "text": text}],
    }
    found = gates.g1_chapter(ch, data, [], allowed_facts={"F-X"}, offered_facts={"F-X"})
    assert evidence(found) == []
    # 事実の言葉を、別の文の主語・話し手にして専門家の話を作るのは止める
    for bad in (
        "{{fact:F-X}}彼は精神科医でもありました。精神科医は、比べる心が心の病の始まりだと"
        "考えています。",
        "{{fact:F-X}}精神科医の多くは、比べる心を病気の入り口と見ています。",
        "{{fact:F-X}}彼は精神科医でもありました。精神科医によると、比べる心は心の病の始まりです。",
    ):
        ch["segments"][0]["text"] = bad
        found = gates.g1_chapter(ch, data, [], allowed_facts={"F-X"}, offered_facts={"F-X"})
        assert evidence(found), bad


def test_passage_words_pass_only_when_quoted():
    """一節の数や言葉は、一節の言い方のまま「」で引けば通る。言い換えると止まり、引き方を伝える。"""
    passage = "すべて都のうち、三分が一に及べりとぞ"
    quoted = "長明は、都の「三分が一」が焼けたと書いています。"
    assert gates.evidence_issues(quoted, passage, False) == []
    (msg,) = gates.evidence_issues("長明は、都の三分の一が焼けたと書いています。", passage, False)
    assert "「」で引く" in msg
    passage = "科学者は自然を恋人とせよ"
    quoted = "寅彦の言う「科学者」とは、確かめる人のことです。"
    assert gates.evidence_issues(quoted, passage, False) == []


def test_rewrite_keeps_the_draft_that_passed(env):  # noqa: F811
    """問いかけだけが悪い版のあと、書き直しで G1 が悪くなったら、通った版に戻す。"""

    class Worse(FakeScriptLLM):
        def __init__(self):
            super().__init__()
            self.ending = 0

        def complete(self, system, user, schema=None):
            out = json.loads(super().complete(system, user, schema))
            if out["comment_prompt"]:
                self.ending += 1
                if self.ending == 1:
                    out["comment_prompt"] = "あなたの勤め先の名前を教えてください"
                else:
                    out["segments"].append(
                        {"kind": "narration", "text": "多くの人がこの悩みを抱えています。"}
                    )
            return json.dumps(out, ensure_ascii=False)

    eid = planned(env, "A", worry_id="W0001")
    r, _ = run(env, eid, writer_llm=Worse())
    assert r.state == "script_done", r.state_msg
    assert r.script["comment_prompt"] == gates.FALLBACK_QUESTION


def test_empty_question_gets_the_fixed_one(env):  # noqa: F811
    class Quiet(FakeScriptLLM):
        def complete(self, system, user, schema=None):
            out = json.loads(super().complete(system, user, schema))
            out["comment_prompt"] = ""
            return json.dumps(out, ensure_ascii=False)

    eid = planned(env, "A", worry_id="W0001")
    r, _ = run(env, eid, writer_llm=Quiet())
    assert r.script["comment_prompt"] == gates.FALLBACK_QUESTION
    assert any("問いかけが無かった" in x for x in r.verification["viewing"]["issues"])


def test_bad_plan_question_is_not_used(env):  # noqa: F811
    from koten.db import connect as _connect

    eid = planned(env, "D", worry_id="W0003")
    conn = _connect(env.ssd_root)
    plan = json.loads(
        conn.execute("SELECT plan_json FROM episodes WHERE episode_id = ?", (eid,)).fetchone()[0]
    )
    plan["question"] = "あなたの勤め先では、どちらの考えの人が多いですか"
    with conn:
        conn.execute(
            "UPDATE episodes SET plan_json = ? WHERE episode_id = ?",
            (json.dumps(plan, ensure_ascii=False), eid),
        )
    llm = FakeScriptLLM()
    r, _ = run(env, eid, writer_llm=llm)
    assert "勤め先" not in r.script["comment_prompt"]
    assert not any("勤め先" in u for u in llm.users)  # 比べる章の手本にも使わない
    assert any("企画の問いを使わなかった" in x for x in r.verification["viewing"]["issues"])


def test_stat_held_after_planning_is_not_used(env):  # noqa: F811
    """企画のあとで保留にした統計は、台本で使わず、人の確認（G6）を立てる（D-035）。"""
    conn = connect(env.ssd_root)
    row = dict(conn.execute("SELECT * FROM stats WHERE stat_id = 'rodo_r7_amount'").fetchone())
    row.update(stat_id="nf_x", theme="嫉妬・比較")
    cols = ", ".join(row)
    with conn:
        conn.execute(
            f"INSERT INTO stats({cols}) VALUES ({', '.join('?' for _ in row)})", list(row.values())
        )
    eid = planned(env, "A", worry_id="W0001")
    data = EpisodeData.load(conn, env.ssd_root, eid)
    assert "nf_x" in data.stats  # 企画には付いた
    with conn:
        conn.execute("UPDATE stats SET status = '保留' WHERE stat_id = 'nf_x'")
    data = EpisodeData.load(conn, env.ssd_root, eid)
    assert "nf_x" not in data.stats and any("保留" in x for x in data.stat_issues)
    r, _ = run(env, eid)
    assert r.state == "script_done", r.state_msg
    texts = [s["text"] for c in r.script["chapters"] for s in c["segments"]]
    assert not any("nf_x" in t for t in texts)
    assert any("統計 nf_x" in x for x in r.verification["G6"]["reasons"])


def test_title_is_checked_when_the_opening_never_passes_g3(env):  # noqa: F811
    """オープニングが G3 を通らないままでも、タイトル案の作り話は仮のタイトルに替える。"""

    class InventedTitle(FakeScriptLLM):
        def complete(self, system, user, schema=None):
            out = json.loads(super().complete(system, user, schema))
            if out["episode_title"]:
                out["episode_title"] = "最新の研究で判明！10人中7人が悩む嫉妬"
            return json.dumps(out, ensure_ascii=False)

    class OpeningFails(FakeJudge):
        def complete(self, system, user, schema=None):
            out = json.loads(super().complete(system, user, schema))
            if "sentences" in out and "（ch0-" in user:
                for x in out["sentences"]:
                    x["ok"], x["problem"] = False, "一節に無いことを言っている"
            return json.dumps(out, ensure_ascii=False)

    eid = planned(env, "A", worry_id="W0001")
    r, _ = run(env, eid, writer_llm=InventedTitle(), judge_llm=OpeningFails())
    assert r.script is not None and r.verification["G3"]["issues"]
    assert "研究" not in r.script["title"] and r.script["title"].endswith("――古典の一節で考える")
    assert any("仮のタイトル" in x for x in r.verification["viewing"]["issues"])
