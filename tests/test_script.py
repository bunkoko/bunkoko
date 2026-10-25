"""台本と検証ゲート（M5）：差し込み・G1〜G6・書き直し・状態・スキーマ。"""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from koten.cli import app
from koten.config import DEFAULT_CONFIG_DIR, REPO_ROOT, load_settings
from koten.db import connect
from koten.script import gates
from koten.script.data import EpisodeData
from koten.script.placeholders import Resolver, format_stat
from koten.script.run import render, run_script, schema_errors
from koten.script.writer import ScriptConfig, ScriptWriter
from koten.worries.master import load_blocked_keywords
from tests.m4_corpus import FakePlanLLM, build
from tests.test_plan import make_planner

PROMPTS = REPO_ROOT / "prompts"
CATEGORIES = load_blocked_keywords(DEFAULT_CONFIG_DIR)
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


class FakeScriptLLM:
    """章の指示を読み、決まりを守った段落を返す偽の台本作家。

    bad：その回数だけ、作り話の引用・著者名の直書き・統計の数字を混ぜる（G1 で落ちる）。
    """

    name = "fake-script"

    def __init__(self, bad: int = 0):
        self.bad = bad
        self.calls = 0
        self.users: list[str] = []

    def complete(self, system, user, schema=None):
        self.calls += 1
        self.users.append(user)
        role = re.search(r"これから書く章：ch\d+（(.+?)）", user).group(1)
        cands = re.findall(r"候補ID：(\S+?)(?:（|$)", user, flags=re.M)
        stats = re.findall(r"統計ID：(\S+?)（", user)
        worry = re.findall(r"^- (W\d{4})：", user, flags=re.M)[0]
        segs = [
            {"kind": "narration", "text": f"今日は「{{{{worry:{worry}}}}}」という悩みを考えます。"}
        ]
        for sid in stats:
            segs.append({"kind": "stat", "text": f"{{{{stat:{sid}}}}}"})
        for cid in cands:
            segs += [
                {"kind": "narration", "text": f"{{{{source:{cid}}}}}に、こうあります。"},
                {"kind": "quote", "text": f"{{{{quote:{cid}}}}}"},
                {
                    "kind": "narration",
                    "text": "比べる心とどう付き合うかを、この一節は教えてくれます。",
                },
            ]
        if self.bad > 0:
            self.bad -= 1
            segs.append(
                {
                    "kind": "narration",
                    "text": "甲は「人は誰しも他人と比べてしまう生き物なのです」と言い、"
                    "55614件もの相談がある。",
                }
            )
        out = {
            "title": f"{role}の章",
            "segments": segs,
            "episode_title": "同期の昇進を喜べないとき、古典は" if role == "オープニング" else "",
            "comment_prompt": "あなたが比べてしまうのはどんなときですか"
            if role == "エンディング"
            else "",
        }
        return json.dumps(out, ensure_ascii=False)


class FakeJudge:
    name = "fake-judge"

    def __init__(self, fail: int = 0):
        self.fail = fail
        self.calls = 0

    def complete(self, system, user, schema=None):
        self.calls += 1
        if self.fail > 0:
            self.fail -= 1
            return json.dumps(
                {"faithful": False, "problems": ["一節が言っていないことを書いている"]},
                ensure_ascii=False,
            )
        return json.dumps({"faithful": True, "problems": []})


@pytest.fixture
def env(settings_file):
    s = load_settings(settings_file)
    build(s.require_hdd(), s.ssd_root, DEFAULT_CONFIG_DIR)
    return s


def planned(s, fmt="A", when=NOW, **kw):
    p = make_planner(s, llm=FakePlanLLM())
    plan = p.plan(fmt, now=when, **kw)
    return p.save(plan, now=when)


def run(s, episode_id, writer_llm=None, judge_llm=None, **kw):
    cfg = ScriptConfig()
    writer = ScriptWriter(writer_llm or FakeScriptLLM(), PROMPTS, cfg)
    judge = gates.FaithfulnessJudge(judge_llm or FakeJudge(), PROMPTS)
    logs: list[str] = []
    r = run_script(
        connect(s.ssd_root),
        s.ssd_root,
        episode_id,
        writer,
        judge,
        CATEGORIES,
        cfg,
        log=logs.append,
        **kw,
    )
    return r, logs


def test_resolver():
    c = {
        "quote_text": "一節",
        "author_names": ["三木清"],
        "title": "人生論ノート",
        "section": "嫉妬について",
    }
    st = {
        "label": "対人関係を挙げた人",
        "value": "32.9",
        "unit": "%",
        "survey": "労働安全衛生調査",
        "year": "2025",
    }
    r = Resolver({"c1": c}, {"s1": st}, {"W1": "同期の昇進を喜べない"})
    assert (
        r.resolve("{{source:c1}}より「{{quote:c1}}」")
        == "三木清『人生論ノート』「嫉妬について」より「一節」"
    )
    assert r.resolve("{{author:c1}}の{{title:c1}}") == "三木清の『人生論ノート』"
    assert r.resolve("{{stat:s1}}") == "対人関係を挙げた人は32.9%（労働安全衛生調査、2025年）"
    assert r.resolve("{{worry:W1}}") == "同期の昇進を喜べない"
    assert (
        r.problems("{{quote:x}}{{stat:y}}{{name:z}}")
        and len(r.problems("{{quote:x}}{{stat:y}}{{name:z}}")) == 3
    )
    with pytest.raises(KeyError):
        r.resolve("{{quote:unknown}}")
    assert (
        format_stat({"label": "x", "value": "7割超", "unit": None, "survey": "", "year": None})
        == "xは7割超"
    )


def test_three_episodes_pass_all_gates(settings_file):
    """受け入れ条件：青空文庫だけで3回分の台本が、スキーマ検証と G1〜G6 を全て通る。"""
    from tests.m4_corpus import EXTRA_WORKS, WORKS

    env = load_settings(settings_file)
    build(env.require_hdd(), env.ssd_root, DEFAULT_CONFIG_DIR, WORKS + EXTRA_WORKS)
    ids = [
        planned(env, "D", NOW, worry_id="W0003"),
        planned(env, "A", NOW + timedelta(hours=1), worry_id="W0001"),
        planned(env, "C", NOW + timedelta(hours=2), theme="孤独"),
    ]
    conn = connect(env.ssd_root)
    for eid in ids:
        r, _ = run(env, eid)
        assert r.state == "script_done", (eid, r.state_msg)
        v = r.verification
        assert all(v[g]["ok"] for g in ("G1", "G2", "G3", "G4", "G5", "G6")), v
        assert v["schema"]["ok"] and not schema_errors(r.script)
        script = r.script
        data = EpisodeData.load(conn, env.ssd_root, eid)
        # プレースホルダが全て解決し、出典とライセンスが入っている
        text = "\n".join(render(script, gates.resolver_for(data)))
        assert "{{" not in text and "解決できない" not in text
        for s in data.plan["selection"]:
            c = data.candidates[s["candidate_id"]]
            assert c["quote_text"] in text and f"『{c['title']}』" in text
            assert c["license"] == "PD_JP"
        assert sorted(cid for ch in script["chapters"] for cid in ch.get("sources", [])) == sorted(
            s["candidate_id"] for s in data.plan["selection"]
        )
        quotes = [g for ch in script["chapters"] for g in ch["segments"] if g["kind"] == "quote"]
        assert all(
            g["visual"]["type"] == "quote_card" and g["read_as"] == "original" for g in quotes
        )
        assert script["human_take"]["chapter_id"] in {ch["chapter_id"] for ch in script["chapters"]}
        assert script["featured_books"] and script["comment_prompt"]
        row = conn.execute(
            "SELECT state, script_json, verification_json FROM episodes WHERE episode_id = ?",
            (eid,),
        ).fetchone()
        assert row["state"] == "script_done" and json.loads(row["verification_json"])["G1"]["ok"]
        assert v["length_estimate"]["chars"] > 0


def test_g1_catches_fabrication_and_rewrites(env):
    eid = planned(env, "A", worry_id="W0001")
    llm = FakeScriptLLM(bad=1)
    r, logs = run(env, eid, writer_llm=llm)
    assert r.state == "script_done"
    assert any("書き直し" in x for x in logs)
    retry_prompt = llm.users[1]
    assert "前回の台本の問題" in retry_prompt
    assert "一節でも悩み文でもない" in retry_prompt
    assert "著者名・作品名「甲」" not in retry_prompt  # 1文字の名前は誤検出を避けて見ない
    assert "統計の数字" not in retry_prompt  # この章には統計が無い


def test_g1_unfixable_makes_error(env):
    eid = planned(env, "A", worry_id="W0001")
    llm = FakeScriptLLM(bad=99)
    r, _ = run(env, eid, writer_llm=llm)
    assert r.state == "error" and r.state_msg.startswith("G1：")
    assert not r.verification["G1"]["ok"]
    assert llm.calls == 3  # 最初の章で3回（1回＋書き直し2回）書いて止める


def test_g1_detects_names_and_stat_numbers(env):
    eid = planned(env, "C", theme="孤独")
    data = EpisodeData.load(connect(env.ssd_root), env.ssd_root, eid)
    cid = data.plan["selection"][0]["candidate_id"]
    title = data.candidates[cid]["title"]
    stat = next(iter(data.stats.values()))
    chapter = {
        "chapter_id": "ch9",
        "segments": [
            {"seg_id": "ch9-s1", "kind": "narration", "text": f"『{title}』という本があります。"},
            {"seg_id": "ch9-s2", "kind": "narration", "text": "「孤独」は誰にでもあります。"},
            {
                "seg_id": "ch9-s3",
                "kind": "narration",
                "text": f"「{data.candidates[cid]['quote_text'][:12]}」と書かれています。",
            },
            {"seg_id": "ch9-s4", "kind": "narration", "text": "孤独を感じる人は多いのです。"},
        ],
    }
    issues = gates.g1_chapter(chapter, data, [])
    assert any("作品名" in i for i in issues)
    assert not any("ch9-s2" in i or "ch9-s3" in i for i in issues)  # 短い強調と一節の一部は許す
    if any(ch.isdigit() for ch in str(stat["value"])):
        chapter["segments"].append(
            {"seg_id": "ch9-s5", "kind": "narration", "text": f"{stat['value']}だそうです。"}
        )
        assert any("統計の数字" in i for i in gates.g1_chapter(chapter, data, []))


def test_g3_rewrite_then_human_review(env):
    eid = planned(env, "A", worry_id="W0001")
    judge = FakeJudge(fail=1)
    r, logs = run(env, eid, judge_llm=judge)
    assert r.state == "script_done" and r.verification["G3"]["ok"] and not r.review_required
    eid2 = planned(env, "A", NOW + timedelta(hours=1), worry_id="W0003")
    r2, _ = run(env, eid2, judge_llm=FakeJudge(fail=999))
    # G3 が2回の書き直しでも直らなければ、人の確認へ（回は止めない）
    assert r2.state == "script_done" and r2.review_required
    assert r2.verification["G3"]["needs_human"] and "G3 人の確認へ" in r2.state_msg


def test_g5_stops_before_llm(env):
    eid = planned(env, "A", worry_id="W0001")
    conn = connect(env.ssd_root)
    with conn:
        conn.execute(
            "UPDATE worries SET status = 'blocked', blocked_reason = 'x' WHERE worry_id = 'W0001'"
        )
    llm = FakeScriptLLM()
    r, _ = run(env, eid, writer_llm=llm)
    assert r.state == "error" and r.state_msg.startswith("回ごと中止") and llm.calls == 0


def test_g4_detects_overlap(env):
    eid = planned(env, "A", worry_id="W0001")
    conn = connect(env.ssd_root)
    work = json.loads(conn.execute("SELECT plan_json FROM episodes").fetchone()[0])["selection"][0][
        "work_id"
    ]
    with conn:
        conn.execute(
            "INSERT INTO episodes(episode_id, format, worry_ids, state, created_at) "
            "VALUES ('E20261031-01', 'B', '[]', 'planned', '2026-10-31 00:00:00')"
        )
        conn.execute("INSERT INTO usage VALUES ('E20261031-01', 'x', ?, NULL, 'main')", (work,))
    r, _ = run(env, eid)
    assert r.state == "error" and "G4" in r.state_msg and "koten plan" in r.state_msg


def test_g2_and_g6_units(env):
    eid = planned(env, "A", worry_id="W0001")
    data = EpisodeData.load(connect(env.ssd_root), env.ssd_root, eid)
    assert gates.g2_license(data)["ok"]
    cid = next(iter(data.candidates))
    data.candidates[cid] = {
        **data.candidates[cid],
        "license": "COPYRIGHTED",
        "review_required": True,
    }
    assert not gates.g2_license(data)["ok"]
    g6 = gates.g6_review(data, {"chapters": []})
    assert g6["ok"] and g6["review_required"]


def test_state_guard_and_cli(env, settings_file, monkeypatch):
    eid = planned(env, "A", worry_id="W0001")

    def fake_llm(conf, model=None):
        return FakeJudge() if "haiku" in conf.get("model", "") else FakeScriptLLM()

    monkeypatch.setattr("koten.llm.make_llm", fake_llm)
    st = ["--settings", str(settings_file)]
    r = CliRunner().invoke(app, ["script", "--episode", eid, *st])
    assert r.exit_code == 0, r.output
    assert "script_done" in r.output and "読み上げの見積もり" in r.output
    r = CliRunner().invoke(app, ["script", "--episode", eid, "--show", *st])
    assert r.exit_code == 0 and "【一節｜原文を読む】" in r.output and "{{" not in r.output
    r = CliRunner().invoke(app, ["script", "--episode", eid, *st])
    assert r.exit_code == 2 and "--force" in r.output
    r = CliRunner().invoke(app, ["script", "--episode", eid, "--force", *st])
    assert r.exit_code == 0


def test_episode_list_and_discard(env, settings_file):
    eid = planned(env, "A", worry_id="W0001")
    st = ["--settings", str(settings_file)]
    r = CliRunner().invoke(app, ["episode", "list", *st])
    assert r.exit_code == 0 and eid in r.output and "planned" in r.output
    r = CliRunner().invoke(app, ["episode", "discard", "--episode", eid, *st])
    assert r.exit_code == 0 and "取り消しました" in r.output
    # 取り消した回は再使用間隔に数えないので、同じ作品でもう一度企画できる
    p = make_planner(env, llm=FakePlanLLM())
    again = p.plan("A", worry_id="W0003", now=NOW)
    first = json.loads(
        connect(env.ssd_root)
        .execute("SELECT plan_json FROM episodes WHERE episode_id = ?", (eid,))
        .fetchone()[0]
    )
    assert {s["work_id"] for s in again["selection"]} & {s["work_id"] for s in first["selection"]}


def test_source_section_is_capped():
    from koten.script.placeholders import format_source

    c = {
        "author_names": ["高神覚昇"],
        "title": "般若心経講義",
        "section": "第十一講 / " + "本文" * 50,
    }
    out = format_source(c)
    assert out.startswith("高神覚昇『般若心経講義』「第十一講") and out.endswith("…」")
    assert len(out) < 50


def test_chapter_length_issue():
    from koten.script.run import chapter_length_issue

    cfg = ScriptConfig()  # 1分300字・±30%
    r = Resolver(
        {"c": {"quote_text": "一" * 20, "author_names": [], "title": "t", "section": ""}}, {}, {}
    )

    def ch(chars, quote=False):
        segs = [{"seg_id": "ch1-s1", "kind": "narration", "text": "あ" * chars}]
        if quote:
            segs.append({"seg_id": "ch1-s2", "kind": "quote", "text": "{{quote:c}}"})
        return {"chapter_id": "ch1", "target_sec": 60, "segments": segs}  # 目安300字

    assert chapter_length_issue(ch(300), r, cfg) is None
    # 一節を含めて380字なら上限（390字）の内側、400字なら外側
    assert chapter_length_issue(ch(360, quote=True), r, cfg) is None
    assert chapter_length_issue(ch(380, quote=True), r, cfg).startswith("長すぎる：約400字")
    assert chapter_length_issue(ch(500), r, cfg).startswith("長すぎる：約500字")
    assert chapter_length_issue(ch(100), r, cfg).startswith("短すぎる")


def test_long_chapter_is_rewritten(env):
    class Wordy(FakeScriptLLM):
        def complete(self, system, user, schema=None):
            out = json.loads(super().complete(system, user, schema))
            if self.calls == 1:
                out["segments"].append({"kind": "narration", "text": "長い語り。" * 400})
            return json.dumps(out, ensure_ascii=False)

    eid = planned(env, "A", worry_id="W0001")
    llm = Wordy()
    r, logs = run(env, eid, writer_llm=llm)
    assert r.state == "script_done"
    assert "長すぎる" in llm.users[1]  # 2回目の指示に、長さの直し方が入る


def test_opening_and_title_checks(env):
    """視聴の確かめ：冒頭30秒で悩みを言う・挨拶しない・タイトル案の決まり。"""
    eid = planned(env, "A", worry_id="W0001")
    data = EpisodeData.load(connect(env.ssd_root), env.ssd_root, eid)
    r = gates.resolver_for(data)
    cid = data.plan["selection"][0]["candidate_id"]

    def opening(*texts):
        segs = [
            {"seg_id": f"ch0-s{i}", "kind": "narration", "text": t} for i, t in enumerate(texts)
        ]
        return {"chapter_id": "ch0", "segments": segs}

    assert gates.opening_issues(opening("{{worry:W0001}}。今日はこれを考えます。"), r, 300) == []
    late = gates.opening_issues(opening("前置き。" * 40, "{{worry:W0001}}"), r, 300)
    assert len(late) == 1 and "160字目まで出てこない" in late[0]
    assert "言っていない" in gates.opening_issues(opening("今日は悩みの話です。"), r, 300)[0]
    hello = gates.opening_issues(
        opening("こんにちは。{{worry:W0001}}をチャンネルで考えます。"), r, 300
    )
    assert len(hello) == 1 and "「こんにちは」「チャンネル」" in hello[0]

    assert gates.title_issues(f"同期の昇進を喜べない｜{{{{author:{cid}}}}}の言葉", data) == []
    assert "無い" in gates.title_issues("", data)[0]
    assert "直接書いている" in gates.title_issues("『比べる心』で考える", data)[0]
    assert "author・title・worry だけ" in gates.title_issues(f"{{{{quote:{cid}}}}}", data)[0]
    assert "長い" in gates.title_issues("あ" * 41, data)[0]
    assert "約束できない" in gates.title_issues("これで人生が変わる", data)[0]


def test_bad_title_is_rewritten_then_replaced(env):
    class BadTitle(FakeScriptLLM):
        def __init__(self, bad_titles):
            super().__init__()
            self.bad_titles = bad_titles

        def complete(self, system, user, schema=None):
            out = json.loads(super().complete(system, user, schema))
            if out["episode_title"] and self.bad_titles > 0:
                self.bad_titles -= 1
                out["episode_title"] = "『比べる心』で絶対に楽になる"
            return json.dumps(out, ensure_ascii=False)

    eid = planned(env, "A", worry_id="W0001")
    llm = BadTitle(1)
    r, _ = run(env, eid, writer_llm=llm)
    assert r.script["title"] == "同期の昇進を喜べないとき、古典は"
    assert "直接書いている" in llm.users[1] and "約束できない" in llm.users[1]
    assert "この回で読む作品" in llm.users[0] and "{{author:" in llm.users[0]

    eid2 = planned(env, "A", NOW + timedelta(days=40), worry_id="W0004")
    r2, _ = run(env, eid2, writer_llm=BadTitle(99))
    assert r2.state == "script_done"  # タイトルでは止めない
    assert r2.script["title"].endswith("――古典の一節で考える")  # 悩みから作る仮のタイトル
    assert any("仮のタイトル" in x for x in r2.verification["viewing"]["issues"])


def test_total_length_estimate_is_flagged(env):
    eid = planned(env, "A", worry_id="W0001")
    r, _ = run(env, eid)  # 偽の台本作家は短い章しか書かない
    v = r.verification["viewing"]
    assert not v["ok"] and any(x.startswith("尺の見積もり") for x in v["issues"])
    assert "視聴の注意" in r.state_msg
