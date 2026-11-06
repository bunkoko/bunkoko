"""自前の訳（M8b）：材料の集め方（D-014・D-023・D-024）・コードの確かめ・判定役・承認・照合。"""

import json
import shutil

import pytest
from typer.testing import CliRunner

from koten.check.claims import text_claims
from koten.check.store import holds, record
from koten.cli import app
from koten.config import DEFAULT_CONFIG_DIR, load_settings
from koten.db import connect
from koten.ref.store import build_refs
from koten.ref.texts import load_canon
from koten.shiyaku.flow import FlowConfig, ShiyakuLLMError, judge_verdict, run_flow
from koten.shiyaku.machine import machine_issues
from koten.shiyaku.material import gather, independent_versions
from koten.shiyaku.store import (
    ShiyakuError,
    approve,
    approved_for,
    get,
    next_id,
    reading_for,
    reject,
    save,
)
from tests.ref_fixture import person, rendering, work, write, write_texts

PROMPTS = DEFAULT_CONFIG_DIR.parent / "prompts"
KUN = "子曰ク、学ビテ時ニ之ヲ習フ、亦説バシカラズヤ"
ORIG = "子曰學而時習之不亦説乎"


def ledger(cfg, *, tradition_kun="", tradition_legge="", long_status="verified"):
    shutil.copy(DEFAULT_CONFIG_DIR / "canon.yaml", cfg / "canon.yaml")
    write(
        cfg,
        [
            work(
                "R-hakubun",
                source_class="S1",
                kind="kundoku",
                title="架空漢文大系",
                pub_year="1909",
                lang="ja",
                credit="『架空漢文大系』（1909）",
                tradition=tradition_kun,
            ),
            work(
                "R-legge",
                kind="translation",
                title="Confucian Analects",
                pub_year="1861",
                lang="en",
                credit="レッグの英訳（1861）",
                holder_allied="yes",
                tradition=tradition_legge,
            ),
            work(
                "R-long",
                kind="translation",
                title="Meditations",
                pub_year="1862",
                lang="en",
                credit="ロングの英訳（1862）",
                holder_allied="yes",
            ),
            work(
                "R-shimo",
                kind="translation",
                title="架空現代訳論語",
                pub_year="1954",
                lang="ja",
                credit="下村架空『架空現代訳論語』（1954）",
            ),
            work("R-hojo", source_class="S1", kind="commentary", title="架空方丈記詳解",
                 pub_year="1900", lang="ja", credit="『架空方丈記詳解』（1900）"),
            work("R-hojo2", kind="commentary", title="架空方丈記新釈", pub_year="1910",
                 lang="ja", credit="『架空方丈記新釈』（1910）"),
        ],
        [
            person("R-hakubun", 1, "服部架空", "editor", 1939),
            person("R-legge", 1, "James Legge", "translator", 1897, country="イギリス"),
            person("R-long", 1, "George Long", "translator", 1879, country="イギリス"),
            person("R-shimo", 1, "下村架空", "translator", 1955),
            person("R-hojo", 1, "甲野架空", "annotator", 1930),
            person("R-hojo2", 1, "乙野架空", "annotator", 1940),
        ],
    )
    write_texts(
        cfg,
        [
            rendering("I-orig-1.1", "lunyu:1.1", "R-hakubun", ORIG, role="original", lang="zh",
                      reading=""),
            rendering("I-kun-1.1", "lunyu:1.1", "R-hakubun", KUN,
                      reading="しいわく、まなびてときにこれをならう、またよろこばしからずや"),
            rendering("I-legge-1.1", "lunyu:1.1", "R-legge",
                      "The Master said, Is it not pleasant to learn with a constant perseverance?",
                      role="translation", lang="en", reading="", locator="Book I, ch. 1"),
            rendering("I-long-2.p1", "meditations:2.p1", "R-long",
                      "Begin the morning by saying to thyself, I shall meet with the busy-body.",
                      role="translation", lang="en", reading="", locator="Book II, p1",
                      status=long_status, **({} if long_status == "verified"
                                             else {"checked_by": "", "checked_at": ""})),
            rendering("I-shimo-1.2", "lunyu:1.2", "R-shimo", "有子が言った。",
                      role="translation", reading="ゆうしがいった", locator="3ページ"),
            rendering("I-orig-1.2", "lunyu:1.2", "R-hakubun", "有子曰", role="original",
                      lang="zh", reading=""),
            rendering("I-hojo-1", "hojoki:1", "R-hojo", "ゆく河の流れは絶えずして", role="original",
                      reading="ゆくかわのながれはたえずして", locator="1ページ"),
            rendering("I-hojo-c1", "hojoki:1", "R-hojo", "河ノ水ノ絶エヌ譬ナリ",
                      role="commentary", reading="かわのみずのたえぬたとえなり", locator="2ページ"),
            rendering("I-hojo2-c1", "hojoki:1", "R-hojo2", "流レ去ル水ニ世ヲ譬フ",
                      role="commentary", reading="ながれさるみずによをたとう", locator="5ページ"),
        ],
    )


@pytest.fixture
def senv(settings_file, tmp_path):
    s = load_settings(settings_file)
    cfg = tmp_path / "ref_config"
    cfg.mkdir()
    ledger(cfg)
    conn = connect(s.ssd_root)
    build_refs(conn, cfg)
    s.conn, s.canon = conn, load_canon(DEFAULT_CONFIG_DIR)
    return s


class Drafter:
    name = "claude-cli:opus@xhigh"

    def __init__(self, *texts):
        self.texts = list(texts)
        self.users: list[str] = []

    def complete(self, system, user, schema=None):
        self.users.append(user)
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return json.dumps({"text": text}, ensure_ascii=False)


class Judge:
    name = "claude-cli:sonnet@high"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.users: list[str] = []

    def complete(self, system, user, schema=None):
        self.users.append(user)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if callable(a):
            a = a(user)
        return json.dumps(a, ensure_ascii=False)


def ok_answer(*ids, n=2, verdict="supported"):
    return {
        "sentences": [
            {"n": i, "verdict": verdict, "supported_by": list(ids), "problem": ""}
            for i in range(1, n + 1)
        ],
        "omissions": [],
    }


GOOD = "先生は言った。「学んだことを折にふれて復習するのは、うれしいことではないか。」"


def test_gather_decides_the_kind_and_label(senv):
    m = gather(senv.conn, senv.canon, "lunyu:1.1")
    assert (m.kind, m.label, m.source["rendering_id"]) == ("shiyaku", "試訳", "I-orig-1.1")
    assert {r["rendering_id"] for r in m.basis} == {"I-kun-1.1", "I-legge-1.1"}
    m = gather(senv.conn, senv.canon, "meditations:2.p1")
    assert (m.kind, m.label) == ("relay", "ロングの英訳（1862）")  # 「試訳」と書かない（D-024）
    m = gather(senv.conn, senv.canon, "hojoki:1")
    assert (m.kind, m.label) == ("modern", "")  # 古文の今の言葉には何も出さない（D-023）
    with pytest.raises(ShiyakuError, match="D-014"):  # 古い日本語訳があれば作らない
        gather(senv.conn, senv.canon, "lunyu:1.2")
    with pytest.raises(ShiyakuError, match="独立した版で3つ"):
        gather(senv.conn, senv.canon, "lunyu:1.1", min_versions=3)
    with pytest.raises(ShiyakuError, match="canon.yaml"):
        gather(senv.conn, senv.canon, "zhuangzi:1")


def test_independent_versions_follow_tradition_and_based_on():
    rows = [
        {"ref_id": "R-a", "ref_tradition": "新注", "ref_based_on": "[]"},
        {"ref_id": "R-b", "ref_tradition": "新注", "ref_based_on": "[]"},
        {"ref_id": "R-c", "ref_tradition": "", "ref_based_on": '["R-d"]'},
        {"ref_id": "R-d", "ref_tradition": "", "ref_based_on": "[]"},
        {"ref_id": "R-e", "ref_tradition": "", "ref_based_on": "[]"},
    ]
    assert independent_versions(rows) == 3  # 新注の2冊・元にした資料でつながる2冊は1つと数える


def test_same_tradition_is_one_version(settings_file, tmp_path):
    s = load_settings(settings_file)
    cfg = tmp_path / "c2"
    cfg.mkdir()
    ledger(cfg, tradition_kun="新注", tradition_legge="新注")
    conn = connect(s.ssd_root)
    build_refs(conn, cfg)
    with pytest.raises(ShiyakuError, match="今は1つ"):
        gather(conn, load_canon(DEFAULT_CONFIG_DIR), "lunyu:1.1")


def test_relay_from_imported_digital_edition_only(settings_file, tmp_path):
    """電子版の取り込んだまま（imported）の英訳は元にできる。人が起こし中（transcribed）は不可。"""
    s = load_settings(settings_file)
    cfg = tmp_path / "c3"
    cfg.mkdir()
    ledger(cfg, long_status="transcribed")
    conn = connect(s.ssd_root)
    build_refs(conn, cfg)
    with pytest.raises(ShiyakuError, match="訳の元にできる本文がありません"):
        gather(conn, load_canon(DEFAULT_CONFIG_DIR), "meditations:2.p1")


def test_machine_checks(senv):
    m = gather(senv.conn, senv.canon, "lunyu:1.1")
    assert machine_issues(GOOD, m.source, m.basis) == []
    copied = "子曰ク、学ビテ時ニ之ヲ習フ、亦説バシカラズヤ、という。"
    assert any("言い回しがほぼ同じ" in x for x in machine_issues(copied, m.source, m.basis))
    assert any("「試訳」" in x for x in machine_issues("試訳：" + GOOD, m.source, m.basis))
    assert any("{{" in x for x in machine_issues("{{quote:x}}" + GOOD, m.source, m.basis))
    assert any("数「3」" in x for x in machine_issues(GOOD + "3度も。", m.source, m.basis))
    assert any("長すぎる" in x for x in machine_issues("あ" * 300, m.source, m.basis))
    s4 = machine_issues("架空論語新訳によれば" + GOOD, m.source, m.basis, ["架空論語新訳"])
    assert any("今の本の書名" in x for x in s4)
    assert machine_issues("", m.source, m.basis) == ["訳が空"]


def test_flow_passes_and_records(senv):
    m = gather(senv.conn, senv.canon, "lunyu:1.1")
    judge = Judge(ok_answer("I-kun-1.1", "I-orig-1.1"))
    sy = run_flow(m, Drafter(GOOD), judge, PROMPTS, FlowConfig(), shiyaku_id="S-lunyu-1.1-1")
    assert sy.status == "judged" and sy.issues == [] and sy.label == "試訳"
    assert sy.judge == "claude-cli:sonnet@high" and sy.source_sha == m.source["text_sha"]
    # 判定役には元の本文・時代の解釈・番号つきの文を見せる（今の本は見せない）
    assert ORIG in judge.users[0] and KUN in judge.users[0] and "S2：「学んだ" in judge.users[0]


def test_flow_refuses_the_same_model(senv):
    m = gather(senv.conn, senv.canon, "lunyu:1.1")
    same = Judge(ok_answer("I-kun-1.1"))
    same.name = "claude-cli:opus@high"
    with pytest.raises(ShiyakuLLMError, match="D-023"):
        run_flow(m, Drafter(GOOD), same, PROMPTS, FlowConfig(), shiyaku_id="S-x-1")


def test_flow_rewrites_then_flags(senv):
    m = gather(senv.conn, senv.canon, "lunyu:1.1")
    bad = ok_answer("I-kun-1.1", verdict="unsupported")
    drafter = Drafter("試訳：" + GOOD, GOOD)
    sy = run_flow(m, drafter, Judge(bad), PROMPTS, FlowConfig(retries=2), shiyaku_id="S-x-1")
    assert sy.status == "flagged" and any("支えが無い" in x for x in sy.issues)
    assert "「試訳」を書かない" in drafter.users[1]  # 書き直しに指摘を渡す
    assert len(drafter.users) == 3
    # 原文だけで、時代の解釈の支えが無い文は不合格（shiyaku・modern）
    only_src = ok_answer("I-orig-1.1")
    problems, _ = judge_verdict(m, ["一。", "二。"], only_src)
    assert any("時代の解釈" in x for x in problems)
    # 見せていない版・落とした所・答えの無い文
    weird = {"sentences": [{"n": 1, "verdict": "supported", "supported_by": ["I-zzz"],
                            "problem": ""}], "omissions": ["不亦説乎"]}
    problems, _ = judge_verdict(m, ["一。", "二。"], weird)
    assert any("見せていない版" in x for x in problems)
    assert any("落ちている" in x for x in problems)
    assert any("答えが無い" in x for x in problems)


def test_relay_needs_the_source_translation(senv):
    m = gather(senv.conn, senv.canon, "meditations:2.p1")
    problems, _ = judge_verdict(m, ["朝に。"], ok_answer("I-legge-1.1", n=1))
    assert any("見せていない版" in x for x in problems)
    problems, _ = judge_verdict(m, ["朝に。"], ok_answer("I-long-2.p1", n=1))
    assert problems == []


def test_divided_is_noted(senv):
    m = gather(senv.conn, senv.canon, "lunyu:1.1")
    sy = run_flow(
        m,
        Drafter(GOOD),
        Judge(ok_answer("I-kun-1.1", verdict="divided")),
        PROMPTS,
        FlowConfig(),
        shiyaku_id="S-x-1",
    )
    assert sy.status == "judged" and any("読みが分かれる" in x for x in sy.issues)


def test_approve_and_reading(senv):
    conn = senv.conn
    m = gather(conn, senv.canon, "lunyu:1.1")
    sy = run_flow(
        m, Drafter(GOOD), Judge(ok_answer("I-kun-1.1")), PROMPTS, FlowConfig(),
        shiyaku_id=next_id(conn, "lunyu:1.1"),
    )
    save(conn, sy)
    assert sy.shiyaku_id == "S-lunyu-1.1-1" and next_id(conn, "lunyu:1.1") == "S-lunyu-1.1-2"
    assert reading_for(conn, "lunyu:1.1") is None  # 承認するまでは使えない
    approve(conn, get(conn, sy.shiyaku_id), by="owner")
    r = reading_for(conn, "lunyu:1.1")
    assert (r.kind, r.label, r.text, r.ref) == ("shiyaku", "試訳", GOOD, "S-lunyu-1.1-1")
    # 古い日本語訳がある一節は、その訳（D-014）
    pd = reading_for(conn, "lunyu:1.2")
    want = ("pd", "I-shimo-1.2", "下村架空『架空現代訳論語』（1954）")
    assert (pd.kind, pd.ref, pd.label) == want
    # 使わないことにすると、台本でも使えない
    reject(conn, get(conn, sy.shiyaku_id), note="読みを直す")
    assert reading_for(conn, "lunyu:1.1") is None


def test_flagged_needs_a_note_and_holds_block(senv):
    conn = senv.conn
    m = gather(conn, senv.canon, "lunyu:1.1")
    sy = run_flow(
        m, Drafter(GOOD), Judge(ok_answer("I-kun-1.1", verdict="unsupported")), PROMPTS,
        FlowConfig(retries=0), shiyaku_id="S-lunyu-1.1-1",
    )
    save(conn, sy)
    with pytest.raises(ShiyakuError, match="--note"):
        approve(conn, sy, by="owner")
    claims = text_claims(sy.shiyaku_id, sy.text)
    record(conn, "", claims[1], book_id="B-x", book_label="架空の本", locator="3ページ",
           verdict="differs", close_wording=False, run_len=None, share=None, method="human",
           checked_by="owner", shiyaku_id=sy.shiyaku_id)
    held = holds(conn, "", claims, shiyaku_id=sy.shiyaku_id)
    assert held and "T2" in held[0].reason
    assert holds(conn, "E-none", claims) == []  # 回の照合とは混ざらない
    with pytest.raises(ShiyakuError, match="照合"):
        approve(conn, sy, by="owner", note="読んだ", holds=[h.reason for h in held])


def test_source_change_makes_the_translation_unusable(settings_file, tmp_path, senv):
    conn = senv.conn
    m = gather(conn, senv.canon, "lunyu:1.1")
    sy = run_flow(m, Drafter(GOOD), Judge(ok_answer("I-kun-1.1")), PROMPTS, FlowConfig(),
                  shiyaku_id="S-lunyu-1.1-1")
    save(conn, sy)
    approve(conn, sy, by="owner")
    cfg = tmp_path / "c4"
    cfg.mkdir()
    ledger(cfg)
    rows = (cfg / "renderings.csv").read_text("utf-8").replace(ORIG, ORIG + "也")
    (cfg / "renderings.csv").write_text(rows, "utf-8")
    build_refs(conn, cfg)
    assert approved_for(conn, "lunyu:1.1") is None


def test_cli_flow(senv, settings_file, monkeypatch):
    llms = {"opus": Drafter(GOOD), "sonnet": Judge(ok_answer("I-kun-1.1"))}
    calls = iter([llms["opus"], llms["sonnet"]] * 4)  # 下書き・判定役の順に作る
    monkeypatch.setattr("koten.llm.make_llm", lambda conf, model=None: next(calls))
    runner = CliRunner()
    base = ["--settings", str(settings_file)]
    r = runner.invoke(app, ["shiyaku", "draft", "--passage", "lunyu:1.1", *base])
    assert r.exit_code == 0, r.output
    assert "S-lunyu-1.1-1：判定役を通った" in r.output
    r = runner.invoke(app, ["shiyaku", "draft", "--passage", "lunyu:1.2", *base])
    assert r.exit_code == 1 and "D-014" in r.output
    r = runner.invoke(app, ["shiyaku", "show", "--id", "S-lunyu-1.1-1", *base])
    assert r.exit_code == 0 and "○ T1" in r.output and "試訳" in r.output, r.output
    # 照合（任意）：訳の文に番号が付き、人の判定で食い違うと承認できない
    r = runner.invoke(app, ["check", "list", "--shiyaku", "S-lunyu-1.1-1", *base])
    assert r.exit_code == 0 and "T1" in r.output, r.output
    r = runner.invoke(app, ["shiyaku", "approve", "--id", "S-lunyu-1.1-1", *base])
    assert r.exit_code == 0, r.output
    assert reading_for(connect(senv.ssd_root), "lunyu:1.1").ref == "S-lunyu-1.1-1"
    r = runner.invoke(app, ["shiyaku", "list", "--status", "approved", *base])
    assert "S-lunyu-1.1-1" in r.output and "「試訳」と表示" in r.output
