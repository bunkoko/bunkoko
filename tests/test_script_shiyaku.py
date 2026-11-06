"""台本で古典の一節の日本語を使う（M8b）：{{quote_ja:}}・表示はコード・G1・G2・G3・G6・照合の保留。"""

import json
import re

import pyarrow.parquet as pq
import pytest

from koten.check.claims import text_claims
from koten.check.store import record, translation_holds
from koten.config import load_settings
from koten.db import connect
from koten.index.build import CACHE_SCHEMA, cache_path
from koten.parquet import write_rows
from koten.ref.store import build_refs
from koten.ref.texts import load_canon
from koten.script import gates
from koten.script.data import EpisodeData
from koten.script.run import render, schema_errors
from koten.shiyaku.flow import FlowConfig, run_flow
from koten.shiyaku.material import gather
from koten.shiyaku.store import approve, save
from tests.m4_corpus import build
from tests.test_script import FakeScriptLLM, planned, run
from tests.test_shiyaku import DEFAULT_CONFIG_DIR as CFG
from tests.test_shiyaku import GOOD, ORIG, PROMPTS, Drafter, Judge, ledger, ok_answer

CID = "canon:lunyu:1.1"
PD_CID = "canon:lunyu:1.2"


def add_canon_candidates(ssd):
    """候補表に古典の一節の候補を足す（検索の候補づくりは別のテスト。ここでは台本の側を見る）。"""
    rows = pq.read_table(cache_path(ssd)).to_pylist()
    base = {k: None for k in CACHE_SCHEMA.names}
    for cid, pk, text, sid in (
        (CID, "lunyu:1.1", ORIG, "I-orig-1.1"),
        (PD_CID, "lunyu:1.2", "有子曰", "I-orig-1.2"),
    ):
        rows.append(
            {
                **base,
                "candidate_id": cid,
                "chunk_id": "",
                "work_id": "canon:lunyu",
                "section": pk.partition(":")[2],
                "quote_text": text,
                "quote_start": 0,
                "quote_end": len(text),
                "context_text": "",
                "summary_ja": "学んで復習する喜び",
                "themes": ["嫉妬・比較"],
                "worries": ["比べてしまう"],
                "quotability": 0.9,
                "caution": False,
                "caution_reason": "",
                "title": "論語",
                "author_ids": ["canon:person:kongzi"],
                "author_names": ["孔子"],
                "author_display": ["孔子"],
                "translator_ids": [],
                "translator_names": [],
                "license": "PD_JP",
                "review_required": False,
                "lang": "zh",
                "passage_key": pk,
                "quote_lang": "zh",
                "source_id": sid,
            }
        )
    write_rows(cache_path(ssd), rows, CACHE_SCHEMA)


def use_canon(s, eid, cid=CID):
    """企画の最初の古典の章の一節を、古典の一節に差し替える。"""
    conn = connect(s.ssd_root)
    plan = json.loads(
        conn.execute("SELECT plan_json FROM episodes WHERE episode_id = ?", (eid,)).fetchone()[0]
    )
    ch = next(c for c in plan["chapters"] if c["role"] == "classic")
    old = ch["candidate_ids"][0]
    ch["candidate_ids"][0] = cid
    for sel in plan["selection"]:
        if sel["candidate_id"] == old:
            sel.update(candidate_id=cid, work_id="canon:lunyu", title="論語",
                       author_ids=["canon:person:kongzi"])
    with conn:
        conn.execute(
            "UPDATE episodes SET plan_json = ? WHERE episode_id = ?",
            (json.dumps(plan, ensure_ascii=False), eid),
        )
    return ch["chapter_id"]


def approved_shiyaku(s, divided=False):
    conn = connect(s.ssd_root)
    m = gather(conn, load_canon(CFG), "lunyu:1.1")
    sy = run_flow(m, Drafter(GOOD), Judge(ok_answer("I-kun-1.1")), PROMPTS, FlowConfig(),
                  shiyaku_id="S-lunyu-1.1-1")
    save(conn, sy)
    approve(conn, sy, by="owner", divided=divided)
    return sy


@pytest.fixture
def cenv(settings_file, tmp_path):
    s = load_settings(settings_file)
    build(s.require_hdd(), s.ssd_root, CFG)
    add_canon_candidates(s.ssd_root)
    cfg = tmp_path / "ref_config"
    cfg.mkdir()
    ledger(cfg)
    build_refs(connect(s.ssd_root), cfg)
    return s


class CanonWriter(FakeScriptLLM):
    """古典の一節は {{quote_ja:}} で出し、say を語りに足す偽の台本作家。"""

    def __init__(self, say="", ja=True):
        super().__init__()
        self.say, self.ja = say, ja

    def complete(self, system, user, schema=None):
        out = json.loads(super().complete(system, user, schema))
        for seg in out["segments"]:
            m = re.fullmatch(r"\{\{quote:(canon:[^{}]+)\}\}", seg["text"])
            if m and self.ja and f"{{{{quote_ja:{m.group(1)}}}}}" in user:
                seg["text"] = f"{{{{quote_ja:{m.group(1)}}}}}"
        if self.say and "{{quote_ja:" in user:
            out["segments"].append({"kind": "narration", "text": self.say})
        return json.dumps(out, ensure_ascii=False)


def test_no_translation_stops_before_writing(cenv):
    eid = planned(cenv, "A", worry_id="W0001")
    use_canon(cenv, eid)
    r, _ = run(cenv, eid, writer_llm=CanonWriter())
    assert r.state == "error" and "G2" in r.state_msg and "shiyaku draft" in r.state_msg


def test_script_uses_the_approved_translation(cenv):
    approved_shiyaku(cenv)
    eid = planned(cenv, "A", worry_id="W0001")
    chid = use_canon(cenv, eid)
    llm = CanonWriter()
    r, _ = run(cenv, eid, writer_llm=llm)
    assert r.state == "script_done", r.state_msg
    assert not schema_errors(r.script) and r.script["schema_version"] == "1.4"
    ch = next(c for c in r.script["chapters"] if c["chapter_id"] == chid)
    seg = next(s for s in ch["segments"] if s["kind"] == "quote" and CID in s["text"])
    tr = {k: v for k, v in seg["translation"].items() if k != "sha"}
    assert tr == {"ref": "S-lunyu-1.1-1", "kind": "shiyaku", "label": "試訳"}
    assert seg["translation"]["sha"]  # 書いたときの訳の指紋（あとで変われば書き直す）
    assert seg["read_as"] == "modern" and seg["visual"] == {"type": "quote_card", "ref": CID}
    # 書き手には原文と日本語と表示を見せる
    prompt = next(u for u in llm.users if f"{{{{quote_ja:{CID}}}}}" in u)
    assert ORIG in prompt and GOOD in prompt and "「試訳」と出る" in prompt
    # 自前の訳は人の確認（G6）。記録と確認画面に出る
    assert any("S-lunyu-1.1-1" in x for x in r.verification["G6"]["reasons"])
    used = r.verification["readings"]["used"][CID]
    assert used["kind"] == "shiyaku" and used["passage_key"] == "lunyu:1.1"
    data = EpisodeData.load(connect(cenv.ssd_root), cenv.ssd_root, eid)
    text = "\n".join(render(r.script, gates.resolver_for(data)))
    assert f"表示：試訳｜S-lunyu-1.1-1】「{GOOD}」" in text and f"原文：{ORIG}" in text


def test_pd_translation_is_used_without_review(cenv):
    eid = planned(cenv, "A", worry_id="W0001")
    use_canon(cenv, eid, PD_CID)
    r, _ = run(cenv, eid, writer_llm=CanonWriter())
    assert r.state == "script_done", r.state_msg
    seg = next(
        s for c in r.script["chapters"] for s in c["segments"] if s.get("translation")
    )
    assert seg["translation"]["kind"] == "pd" and seg["translation"]["ref"] == "I-shimo-1.2"
    assert not any(PD_CID in x for x in r.verification["G6"]["reasons"])


def test_canon_passage_must_use_quote_ja(cenv):
    approved_shiyaku(cenv)
    eid = planned(cenv, "A", worry_id="W0001")
    use_canon(cenv, eid)
    r, _ = run(cenv, eid, writer_llm=CanonWriter(ja=False))
    assert r.state == "error" and "quote_ja" in r.state_msg


@pytest.mark.parametrize(
    "say, divided, ok",
    [
        ("ここからは当チャンネルの試訳です。", False, True),  # 原文から訳した一節の章なら言える
        ("この一節は AI が訳しました。", False, False),
        ("この一節は、機械翻訳したものです。", False, False),
        ("この一節は、読み方が分かれています。", False, False),
        ("この一節は、読み方が分かれています。", True, True),  # 人が確かめた「分かれる」
        ("この一節は、AIによる翻訳です。", False, False),
        ("この一節は、AIを使い日本語にしました。", False, False),
        ("日本語は人工知能の力を借りて訳しました。", False, False),
        ("この日本語は、ChatGPTで訳したものです。", False, False),
        ("解釈は、人によって分かれます。", False, False),
        ("この一節には諸説あります。", False, False),
        ("言い訳はいりません。", False, True),
    ],
)
def test_translation_words(cenv, say, divided, ok):
    approved_shiyaku(cenv, divided=divided)
    eid = planned(cenv, "A", worry_id="W0001")
    use_canon(cenv, eid)
    data = EpisodeData.load(connect(cenv.ssd_root), cenv.ssd_root, eid)
    ch = {"chapter_id": "ch9", "sources": [CID], "segments": [
        {"seg_id": "ch9-s1", "kind": "narration", "text": say}]}
    issues = gates.g1_chapter(ch, data, [])
    assert (issues == []) is ok, issues
    # 古典の一節の日本語は「」で引ける
    said = "「学んだことを折にふれて復習する」のです。"
    quoted = {"chapter_id": "ch9", "sources": [CID], "segments": [
        {"seg_id": "ch9-s1", "kind": "narration", "text": said}]}
    assert gates.g1_chapter(quoted, data, []) == []


def test_relay_translation_cannot_say_shiyaku(cenv):
    """英訳からの訳（relay）の章では「試訳」とも言わない（D-024）。"""
    conn = connect(cenv.ssd_root)
    m = gather(conn, load_canon(CFG), "meditations:2.p1")
    sy = run_flow(m, Drafter("朝のうちに、自分にこう言い聞かせよ。"),
                  Judge(ok_answer("I-long-2.p1", n=1)), PROMPTS, FlowConfig(),
                  shiyaku_id="S-meditations-2.p1-1")
    save(conn, sy)
    approve(conn, sy, by="owner")
    eid = planned(cenv, "A", worry_id="W0001")
    rows = pq.read_table(cache_path(cenv.ssd_root)).to_pylist()
    for r in rows:
        if r["candidate_id"] == CID:
            r.update(
                passage_key="meditations:2.p1",
                lang="grc",
                quote_lang="en",
                source_id="I-long-2.p1",
                quote_text="Begin the morning by saying to thyself, "
                "I shall meet with the busy-body.",
            )
    write_rows(cache_path(cenv.ssd_root), rows, CACHE_SCHEMA)
    use_canon(cenv, eid)
    data = EpisodeData.load(conn, cenv.ssd_root, eid)
    assert data.readings[CID].label == "ロングの英訳（1862）"
    ch = {"chapter_id": "ch9", "sources": [CID], "segments": [
        {"seg_id": "ch9-s1", "kind": "narration", "text": "ここからは当チャンネルの試訳です。"}]}
    assert any("試訳" in x for x in gates.g1_chapter(ch, data, []))


def test_held_translation_holds_the_episode(cenv):
    sy = approved_shiyaku(cenv)
    eid = planned(cenv, "A", worry_id="W0001")
    use_canon(cenv, eid)
    r, _ = run(cenv, eid, writer_llm=CanonWriter())
    conn = connect(cenv.ssd_root)
    assert translation_holds(conn, r.script) == []
    claims = text_claims(sy.shiyaku_id, sy.text)
    record(conn, "", claims[0], book_id="B-x", book_label="架空の本", locator="3ページ",
           verdict="same", close_wording=True, run_len=14, share=0.5, method="human",
           checked_by="owner", shiyaku_id=sy.shiyaku_id)
    held = translation_holds(conn, r.script)
    assert held and "S-lunyu-1.1-1" in held[0].reason


def _approve_second(s, text):
    conn = connect(s.ssd_root)
    m = gather(conn, load_canon(CFG), "lunyu:1.1")
    sy = run_flow(m, Drafter(text), Judge(ok_answer("I-kun-1.1", n=2)), PROMPTS, FlowConfig(),
                  shiyaku_id="S-lunyu-1.1-2")
    save(conn, sy)
    approve(conn, sy, by="owner")
    return sy


def test_script_is_pinned_to_its_translation(cenv):
    """台本を書いたあとで訳が変わったら、別の訳を差し込まず、書き直すまで公開しない。"""
    from koten.shiyaku.store import reading_drift
    from koten.shiyaku.store import reject as reject_sy

    sy = approved_shiyaku(cenv)
    eid = planned(cenv, "A", worry_id="W0001")
    use_canon(cenv, eid)
    r, _ = run(cenv, eid, writer_llm=CanonWriter())
    conn = connect(cenv.ssd_root)
    data = EpisodeData.load(conn, cenv.ssd_root, eid)
    assert reading_drift(r.script, data.readings) == []
    # 新しい訳を承認すると、今の一節の日本語が変わる
    _approve_second(cenv, "先生が言った。学んで時々おさらいするのは、楽しいことだ。")
    data = EpisodeData.load(conn, cenv.ssd_root, eid)
    assert reading_drift(r.script, data.readings)
    text = "\n".join(render(r.script, gates.resolver_for(data, r.script)))
    assert "台本のあとで一節の日本語が変わった" in text and "おさらいする" not in text
    assert any("書き直す" in h.reason for h in translation_holds(conn, r.script, data.readings))
    assert gates.g6_review(data, r.script)["review_required"]
    # 書いたときの訳を使わないことにしても保留
    reject_sy(conn, sy, note="直す")
    assert any("使えなくなった" in h.reason for h in translation_holds(conn, r.script))


def test_close_sentence_holds_a_redraft(cenv):
    """照合で本に近いと出た文は、訳し直して別の ID になっても、同じ文なら承認できない（R8）。"""
    from koten.check.store import holds

    conn = connect(cenv.ssd_root)
    m = gather(conn, load_canon(CFG), "lunyu:1.1")
    first = run_flow(m, Drafter(GOOD), Judge(ok_answer("I-kun-1.1")), PROMPTS, FlowConfig(),
                     shiyaku_id="S-lunyu-1.1-1")
    save(conn, first)
    record(conn, "", text_claims(first.shiyaku_id, first.text)[1], book_id="B-x",
           book_label="架空の本", locator="3ページ", verdict="same", close_wording=True,
           run_len=20, share=0.8, method="human", checked_by="owner",
           shiyaku_id=first.shiyaku_id)
    again = run_flow(m, Drafter(GOOD), Judge(ok_answer("I-kun-1.1")), PROMPTS, FlowConfig(),
                     shiyaku_id="S-lunyu-1.1-2")
    save(conn, again)
    held = holds(conn, "", text_claims(again.shiyaku_id, again.text), shiyaku_id=again.shiyaku_id)
    assert held


def test_narration_may_quote_the_translation_placeholder(cenv):
    """語りの段落の {{quote_ja:}} はスキーマで止めない（translation は一節の段落だけ）。"""
    approved_shiyaku(cenv)
    eid = planned(cenv, "A", worry_id="W0001")
    use_canon(cenv, eid)
    r, _ = run(cenv, eid, writer_llm=CanonWriter(say=f"「{{{{quote_ja:{CID}}}}}」と言います。"))
    assert r.state == "script_done", r.state_msg


def test_translation_words_in_titles(cenv):
    approved_shiyaku(cenv)
    eid = planned(cenv, "A", worry_id="W0001")
    use_canon(cenv, eid)
    data = EpisodeData.load(connect(cenv.ssd_root), cenv.ssd_root, eid)
    ch = {"chapter_id": "ch3", "sources": [CID], "segments": [
        {"seg_id": "ch3-s1", "kind": "transition", "text": "AIが訳した古典の言葉"}]}
    assert gates.g1_chapter(ch, data, [])
    assert any("訳の表示" in x for x in gates.title_issues("試訳で読む、古い言葉", data))
    q = "AIが訳したこの一節を、あなたならどう読みますか。"
    assert any("訳の表示" in x for x in gates.question_issues(q, data, {}))


def test_canon_text_must_stay_usable(cenv, tmp_path):
    """画面の一節の本文の資料が使えなくなったら、台本を書かせない（R2）。"""
    eid = planned(cenv, "A", worry_id="W0001")
    use_canon(cenv, eid, PD_CID)
    cfg = tmp_path / "tight"
    cfg.mkdir()
    ledger(cfg)
    people = (cfg / "ref_people.csv").read_text("utf-8")
    (cfg / "ref_people.csv").write_text(people.replace(",editor,1939", ",editor,1990"), "utf-8")
    from koten.ref.texts import TextsError

    with pytest.raises(TextsError):  # 本文の行が残っていると止まるが、SSD は先に厳しくなる
        build_refs(connect(cenv.ssd_root), cfg)
    r, _ = run(cenv, eid, writer_llm=CanonWriter())
    assert r.state == "error" and "使えない資料" in r.state_msg and "canon build" in r.state_msg
