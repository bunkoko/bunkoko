"""台本の背景の事実（D-012）：記号での差し込み・G1 の直書き・1文ずつの判定・背景を外す流れ。"""

import json
import re
from datetime import date

import pytest

from koten.config import DEFAULT_CONFIG_DIR, load_settings
from koten.db import connect
from koten.facts.store import build_facts
from koten.script import gates
from koten.script.data import EpisodeData
from koten.script.run import render, schema_errors
from tests.facts_fixture import write_curated, write_persons
from tests.m4_corpus import build
from tests.test_script import FakeScriptLLM, planned, run

FACT_LINE = re.compile(r"事実ID：(\S+?)(?:（|\n|$)", re.M)


class FactWriter(FakeScriptLLM):
    """渡された背景の事実を、記号で1段落ずつ語る偽の台本作家。

    leak：その回数だけ、事実の年と場所を直接書く（G1 で落ちる）。
    claim：その回数だけ、事実に無い主張を足す（偽の判定役が落とす）。
    """

    def __init__(self, leak: int = 0, claim: int = 0, **kw):
        super().__init__(**kw)
        self.leak = leak
        self.claim = claim

    def complete(self, system, user, schema=None):
        out = json.loads(super().complete(system, user, schema))
        for fid in FACT_LINE.findall(user):
            text = f"少し背景にふれます。{{{{fact:{fid}}}}}"
            if self.leak > 0 and fid == "F-w001-001":
                self.leak -= 1
                text = "少し背景にふれます。1906年、架空の町でのことです。"
            if self.claim > 0:
                self.claim -= 1
                text += "そのため作者は深く悩みました。"
            out["segments"].insert(1, {"kind": "narration", "text": text})
        return json.dumps(out, ensure_ascii=False)


class SentenceJudge:
    """1文ずつの判定（judge_v2）にも答える偽の判定役。「そのため」の文は根拠なしにする。"""

    name = "fake-sentence-judge"

    def __init__(self):
        self.calls = {"v1": 0, "v2": 0}
        self.users: list[str] = []

    def complete(self, system, user, schema=None):
        self.users.append(user)
        if "番号つきの文" not in user:
            self.calls["v1"] += 1
            return json.dumps({"faithful": True, "problems": []})
        self.calls["v2"] += 1
        sentences = []
        for n, sent in re.findall(r"^S(\d+)（[^）]+）：(.*)$", user, flags=re.M):
            markers = re.findall(r"〔([^〔〕]+)〕", sent)
            bad = "そのため" in sent
            sentences.append(
                {
                    "n": int(n),
                    "type": "background" if markers or bad else "connective",
                    "supported_by": markers,
                    "ok": not bad,
                    "problem": "事実に無い因果を作っている" if bad else "",
                }
            )
        return json.dumps({"sentences": sentences, "passage_problems": []}, ensure_ascii=False)


@pytest.fixture
def env(settings_file, tmp_path):
    s = load_settings(settings_file)
    build(s.require_hdd(), s.ssd_root, DEFAULT_CONFIG_DIR)
    write_persons(s.require_hdd())
    cfg = tmp_path / "facts_config"
    cfg.mkdir()
    write_curated(cfg)
    build_facts(connect(s.ssd_root), s.require_hdd(), cfg, today=date(2026, 10, 26))
    return s


def test_script_tells_background_with_facts(env):
    eid = planned(env, "A", worry_id="W0001")
    judge = SentenceJudge()
    r, _ = run(env, eid, writer_llm=FactWriter(), judge_llm=judge)
    assert r.state == "script_done", r.state_msg
    assert not schema_errors(r.script) and r.script["schema_version"] == "1.1"
    used = [f for ch in r.script["chapters"] for f in ch.get("facts", [])]
    assert "F-w001-001" in used and judge.calls["v2"] >= 1
    seg = next(
        g for ch in r.script["chapters"] for g in ch["segments"] if g.get("facts") == ["F-w001-001"]
    )
    assert seg["visual"] == {"type": "fact_card", "ref": "F-w001-001"}
    rec = r.verification["facts"]
    item = next(x for x in rec["used"] if x["fact_id"] == "F-w001-001")
    assert item["checked_by"] == "テスト" and item["sources"][0]["kind"] == "book"
    conn = connect(env.ssd_root)
    rows = conn.execute("SELECT fact_id FROM fact_usage WHERE episode_id = ?", (eid,)).fetchall()
    assert {x[0] for x in rows} == set(used)
    data = EpisodeData.load(conn, env.ssd_root, eid)
    text = "\n".join(render(r.script, gates.resolver_for(data)))
    assert (
        "【背景｜F-w001-001】少し背景にふれます。『比べる心』は、1906年に架空の町で書かれました。"
        in text
    )
    assert "背景の事実（確認済みの文" in text
    # 使った事実は、次の企画では付かない（直近10回）
    again = planned(env, "A", worry_id="W0003")
    plan = json.loads(
        conn.execute("SELECT plan_json FROM episodes WHERE episode_id = ?", (again,)).fetchone()[0]
    )
    assert not {f for c in plan["chapters"] for f in c.get("fact_ids", [])} & set(used)


def test_raw_year_and_place_are_rewritten(env):
    eid = planned(env, "A", worry_id="W0001")
    llm = FactWriter(leak=1)
    r, logs = run(env, eid, writer_llm=llm, judge_llm=SentenceJudge())
    assert r.state == "script_done"
    feedback = next(u for u in llm.users if "1906年」を直接" in u)
    assert "年「1906年」を直接書いている" in feedback and "「架空の町」を直接書いている" in feedback
    assert any(ch.get("facts") for ch in r.script["chapters"])  # 書き直して記号で語れた


def test_unsupported_background_drops_facts(env):
    eid = planned(env, "A", worry_id="W0001")
    r, logs = run(env, eid, writer_llm=FactWriter(claim=99), judge_llm=SentenceJudge())
    assert r.state == "script_done", r.state_msg
    assert any("背景を外した" in x for x in r.verification["viewing"]["issues"])
    assert any("背景の事実を外して書き直します" in x for x in logs)
    assert not any(ch.get("facts") for ch in r.script["chapters"])


def test_g1_fact_rules(env):
    eid = planned(env, "A", worry_id="W0001")
    data = EpisodeData.load(connect(env.ssd_root), env.ssd_root, eid)
    assert data.facts, "企画に事実が付いていない"
    fid = next(iter(data.facts))

    def ch(text):
        return {
            "chapter_id": "ch9",
            "fact_ids": [],
            "segments": [{"seg_id": "ch9-s1", "kind": "narration", "text": text}],
        }

    issues = gates.g1_chapter(ch(f"{{{{fact:{fid}}}}}"), data, [], allowed_facts=set())
    assert any("渡していない事実" in i for i in issues)
    assert gates.g1_chapter(ch(f"{{{{fact:{fid}}}}}"), data, [], allowed_facts={fid}) == []
    for raw in ("明治三十九年のこと", "十九世紀の話", "百年前の言葉", "甲野一郎という人"):
        assert gates.g1_chapter(ch(raw), data, [], allowed_facts=set()), raw
    assert gates.g1_chapter(ch("入社して3年目の春"), data, [], allowed_facts=set()) == []
    assert any(
        "解決できない" in i
        for i in gates.g1_chapter(ch("{{fact:F-なし.year}}"), data, [], allowed_facts=set())
    )


def test_sentence_verdict_rules():
    sents = [
        ("ch1-s1", "1906年〔F-1〕に書かれました。", {"F-1"}),
        ("ch1-s2", "つなぎです。", set()),
    ]

    def got(**kw):
        base = {
            1: {"n": 1, "type": "background", "supported_by": ["F-1"], "ok": True, "problem": ""},
            2: {"n": 2, "type": "connective", "supported_by": [], "ok": True, "problem": ""},
        }
        base[1].update(kw)
        return base

    assert gates.sentence_verdict(sents, got(), {"F-1"}, []) == []
    assert "根拠の事実が無い" in gates.sentence_verdict(sents, got(supported_by=[]), {"F-1"}, [])[0]
    assert (
        "根拠と合わない"
        in gates.sentence_verdict(sents, got(supported_by=["F-2"]), {"F-1", "F-2"}, [])[0]
    )
    assert (
        "見せていない事実"
        in gates.sentence_verdict(sents, got(supported_by=["F-1", "F-9"]), {"F-1"}, [])[0]
    )
    assert (
        "作った"
        in gates.sentence_verdict(sents, got(ok=False, problem="因果を作った"), {"F-1"}, [])[0]
    )


def test_changed_fact_needs_review(env):
    eid = planned(env, "A", worry_id="W0001")
    conn = connect(env.ssd_root)
    with conn:
        conn.execute("UPDATE facts SET text_sha = 'changed' WHERE fact_id = 'F-w001-001'")
        plan = json.loads(
            conn.execute("SELECT plan_json FROM episodes WHERE episode_id = ?", (eid,)).fetchone()[
                0
            ]
        )
        plan["facts"]["F-w001-001"] = "0000000000000000"
        conn.execute(
            "UPDATE episodes SET plan_json = ? WHERE episode_id = ?",
            (json.dumps(plan, ensure_ascii=False), eid),
        )
    r, _ = run(env, eid, writer_llm=FactWriter(), judge_llm=SentenceJudge())
    assert r.verification["G6"]["review_required"] and r.review_required
    assert any("文が変わった" in x for x in r.verification["G6"]["reasons"])
