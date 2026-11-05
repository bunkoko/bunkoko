"""作り話の調査・研究・数字を止める G1 と、コメントへの問いかけの検査（D-035）。"""

import json

from koten.db import connect
from koten.script import gates
from koten.script.data import EpisodeData
from tests.test_script import (
    CATEGORIES,
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
