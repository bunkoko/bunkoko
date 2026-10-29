"""台本の昔の辞書の語釈・時代の解釈（D-015・M8a）：企画で付ける・記号で差し込む・G1・判定・外す流れ。"""

# ruff: noqa: E501

import json
import re
import shutil

import pytest
from typer.testing import CliRunner

from koten.cli import app
from koten.config import DEFAULT_CONFIG_DIR, load_settings
from koten.db import connect
from koten.ref.store import build_refs
from koten.script import gates
from koten.script.data import EpisodeData
from koten.script.run import render, schema_errors
from tests.m4_corpus import build
from tests.ref_fixture import entry, person, rendering, work, write, write_texts
from tests.test_script import PROMPTS as PROMPTS_DIR
from tests.test_script import FakeScriptLLM, planned, run

REF_LINE = re.compile(r"^- ID：((?:J|I)-\S+)", re.M)
DEF = "J-dict-sunao-1"
INTERP = "I-kun-lunyu-1.1"


def write_refs(cfg, *, ndl=True, def_text="ココロノ マガリナキコト。"):
    shutil.copy(DEFAULT_CONFIG_DIR / "canon.yaml", cfg / "canon.yaml")
    src = (
        {
            "source": "ndl",
            "pid": "000001",
            "ndl_access": "internet_pd",
            "ndl_checked_at": "2026-10-29",
        }
        if ndl
        else {}
    )
    write(
        cfg,
        [
            work(
                "R-dict",
                title="架空辞書",
                volume="第4冊",
                pub_year="1891",
                credit="大槻架空『架空辞書』第4冊（1891）",
                **src,
            ),
            work(
                "R-kun",
                kind="kundoku",
                title="架空漢文大系",
                pub_year="1909",
                credit="『架空漢文大系』（1909）",
            ),
        ],
        [
            person("R-dict", 1, "大槻架空", "author", 1928),
            person("R-kun", 1, "服部架空", "kundoku", 1939),
        ],
    )
    write_texts(
        cfg,
        [
            rendering(
                INTERP,
                "lunyu:1.1",
                "R-kun",
                "学ビテ時ニ之ヲ習フ、亦説バシカラズヤ",
                reading="まなびてときにこれをならう、またよろこばしからずや",
            )
        ],
        [
            entry(
                DEF,
                "R-dict",
                "素直",
                def_text,
                headword_kana="すなほ",
                reading="こころのまがりなきこと",
                display_text="こころのまがりなきこと。",
                locator="コマ800",
            ),
            # 確かめ中のものは企画に付かない
            entry(
                "J-dict-shoushin-1",
                "R-dict",
                "昇進",
                "クラヰノノボルコト。",
                reading="くらいののぼること",
                status="transcribed",
                checked_by="",
                checked_at="",
            ),
        ],
    )


@pytest.fixture
def env(settings_file, tmp_path):
    s = load_settings(settings_file)
    build(s.require_hdd(), s.ssd_root, DEFAULT_CONFIG_DIR)
    cfg = tmp_path / "ref_config"
    cfg.mkdir()
    write_refs(cfg)
    build_refs(connect(s.ssd_root), cfg)
    s.ref_cfg = cfg
    return s


class RefWriter(FakeScriptLLM):
    """渡された語釈・解釈を、記号で1段落ずつ紹介する偽の台本作家。

    mode："ok"（決まりどおり）・"unquoted"（「」に入れない）・"nosrc"（出典の記号が無い）・
    "wrong"（正しい意味だと言う。偽の判定役が落とす）。times 回だけその mode にし、あとは ok。
    """

    def __init__(self, mode="ok", times=0, **kw):
        super().__init__(**kw)
        self.mode, self.times = mode, times

    def complete(self, system, user, schema=None):
        out = json.loads(super().complete(system, user, schema))
        for rid in REF_LINE.findall(user):
            mode = self.mode if self.times > 0 else "ok"
            body, src = ("def", "def_src") if rid.startswith("J-") else ("interp", "interp_src")
            text = f"{{{{{src}:{rid}}}}}は、「{{{{{body}:{rid}}}}}」と説明しています。"
            if mode == "unquoted":
                text = f"{{{{{src}:{rid}}}}}は、{{{{{body}:{rid}}}}}と説明しています。"
            elif mode == "nosrc":
                text = f"昔の辞書は、「{{{{{body}:{rid}}}}}」と説明しています。"
            elif mode == "wrong":
                text += "これが今も正しい意味です。"
            out["segments"].insert(1, {"kind": "narration", "text": text})
        if self.times > 0 and REF_LINE.search(user):
            self.times -= 1
        return json.dumps(out, ensure_ascii=False)


class RefJudge:
    """1文ずつの判定（judge_v3）に答える偽の判定役。〔J-…〕〔I-…〕の印の文は reference。"""

    name = "fake-ref-judge"

    def __init__(self):
        self.users: list[str] = []

    def complete(self, system, user, schema=None):
        self.users.append(user)
        if "番号つきの文" not in user:
            return json.dumps({"faithful": True, "problems": []})
        sentences = []
        for n, sent in re.findall(r"^S(\d+)（[^）]+）：(.*)$", user, flags=re.M):
            markers = re.findall(r"〔([^〔〕]+)〕", sent)
            bad = "正しい意味" in sent
            ref = any(m.startswith(("J-", "I-")) for m in markers) or bad
            sentences.append(
                {
                    "n": int(n),
                    "type": "reference" if ref else ("background" if markers else "connective"),
                    "supported_by": markers,
                    "ok": not bad,
                    "problem": "昔の説明を今の正しい意味と言っている" if bad else "",
                }
            )
        return json.dumps({"sentences": sentences, "passage_problems": []}, ensure_ascii=False)


def plan_of(s, eid):
    row = (
        connect(s.ssd_root)
        .execute("SELECT plan_json FROM episodes WHERE episode_id = ?", (eid,))
        .fetchone()
    )
    return json.loads(row[0])


def test_plan_attaches_definition_of_worry_word(env):
    eid = planned(env, "A", worry_id="W0001")  # 同期の昇進を素直に喜べない
    plan = plan_of(env, eid)
    assert plan["schema_version"] == "1.2"
    context = next(c for c in plan["chapters"] if c["role"] == "context")
    # 確かめ済みの「素直」だけ。確かめ中の「昇進」は付かない
    assert context["ref_ids"] == [DEF]
    assert set(plan["refs"]) == {DEF}
    assert all(c["ref_ids"] == [] for c in plan["chapters"] if c["role"] != "context")


def test_script_quotes_definition_with_source(env):
    eid = planned(env, "A", worry_id="W0001")
    judge = RefJudge()
    r, logs = run(env, eid, writer_llm=RefWriter(), judge_llm=judge)
    assert r.state == "script_done", r.state_msg
    assert not schema_errors(r.script) and r.script["schema_version"] == "1.2"
    context = next(c for c in r.script["chapters"] if c["role"] == "context")
    assert context["refs"] == [DEF]
    seg = next(s for s in context["segments"] if s.get("refs"))
    assert seg["visual"] == {"type": "ref_card", "ref": DEF}
    data = EpisodeData.load(connect(env.ssd_root), env.ssd_root, eid)
    resolved = gates.resolver_for(data).resolve(seg["text"])
    # 出典は刷りの年（和暦つき）と書名。本文は表記を改めた画面用の文
    assert (
        resolved
        == "1891年（明治24年）の『架空辞書』は、「こころのまがりなきこと。」と説明しています。"
    )
    # 判定役は語釈の本文と出典を見て、〔ID〕の印のある文を確かめている
    v3 = next(u for u in judge.users if "DBの語釈・解釈" in u and DEF in u)
    assert f"こころのまがりなきこと。〔{DEF}〕" in v3
    rec = r.verification["refs"]["used"][0]
    assert rec["ref"] == DEF and rec["locator"] == "コマ800" and rec["notation_changed"] is True
    # NDL の資料の本文は、画面のページ画像と照らし合わせる（G6）
    assert r.verification["G6"]["review_required"]
    assert any("NDL の資料の本文" in x for x in r.verification["G6"]["reasons"])
    text = "\n".join(render(r.script, gates.resolver_for(data)))
    assert f"【資料｜{DEF}】1891年（明治24年）の『架空辞書』は" in text
    assert "「ココロノ マガリナキコト。」（大槻架空『架空辞書』第4冊（1891）　コマ800）" in text
    assert "画面の表記：「こころのまがりなきこと。」（表記を改めた）" in text
    assert "読み上げ：こころのまがりなきこと" in text


@pytest.mark.parametrize(
    "mode,feedback",
    [
        ("unquoted", "「」の中に記号だけを書く"),
        ("nosrc", "出典の記号"),
        ("wrong", "正しい意味と言っている"),
    ],
)
def test_bad_reference_use_is_rewritten(env, mode, feedback):
    eid = planned(env, "A", worry_id="W0001")
    writer = RefWriter(mode=mode, times=1)
    r, logs = run(env, eid, writer_llm=writer, judge_llm=RefJudge())
    assert r.state == "script_done", r.state_msg
    assert any(feedback in u for u in writer.users)
    context = next(c for c in r.script["chapters"] if c["role"] == "context")
    assert context["refs"] == [DEF]  # 書き直して決まりどおりに紹介できた


def test_reference_dropped_when_it_never_passes(env):
    eid = planned(env, "A", worry_id="W0001")
    r, logs = run(env, eid, writer_llm=RefWriter(mode="unquoted", times=3), judge_llm=RefJudge())
    assert r.state == "script_done", r.state_msg
    assert any("語釈・解釈を外して書き直します" in x for x in logs)
    assert any("語釈・解釈を外した" in x for x in r.verification["viewing"]["issues"])
    assert not any(ch.get("refs") for ch in r.script["chapters"])


def chapter_with(*texts, kind="narration", ref_ids=(DEF,)):
    return {
        "chapter_id": "ch1",
        "role": "context",
        "title": "t",
        "ref_ids": list(ref_ids),
        "segments": [
            {"seg_id": f"ch1-s{i}", "kind": kind, "text": t} for i, t in enumerate(texts, 1)
        ],
    }


@pytest.mark.parametrize(
    "text,issue",
    [
        (f"{{{{def_src:{DEF}}}}}は、{{{{def:{DEF}}}}}と説明しています。", "「」の中に記号だけ"),
        (f"昔の辞書は「{{{{def:{DEF}}}}}」と説明しています。", "出典の記号"),
        ("『架空辞書』では、素直をまっすぐな心とします。", "書名『架空辞書』を直接書いている"),
        (
            "辞書では、素直とはまっすぐな心のことです。",
            "「辞書」は、渡した語釈・解釈の記号と同じ文のときだけ",
        ),
        ("素直の語源をたどると、意外な姿が見えます。", "「語源」は"),
        ("素直の本当の意味を考えます。", "「本当の意味」は根拠を示せない"),
        ("通説では、素直は美徳です。", "「通説」は根拠を示せない"),
        ("広辞苑を開くと、素直の説明があります。", "名前「広辞苑」"),
        ("明治の人は、素直を美徳と考えました。", "時代の人全体の考えにしない"),
        ("辞書で調べると、素直とは心がまっすぐなことです。", "「辞書」は"),
        ("国語辞典によれば、素直とは人に逆らわないことです。", "「辞典」は"),
        ("架空辞書によると、素直とは人に逆らわないことです。", "書名「架空辞書」は出典の記号"),
        ("『字通』によれば、素直は美徳です。", "名前「字通」"),
        (
            f"{{{{def_src:{DEF}}}}}は、「素直」を「心がまっすぐなこと」と説明しています。",
            "資料の言葉のように「」に入れない",
        ),
        (
            f"{{{{interp_src:{INTERP}}}}}では「{{{{interp:{INTERP}}}}}」と読みます。",
            "この章に渡していない語釈・解釈",
        ),
    ],
)
def test_g1_reference_rules(env, text, issue):
    eid = planned(env, "A", worry_id="W0001")
    conn = connect(env.ssd_root)
    plan = plan_of(env, eid)
    plan["chapters"][2]["ref_ids"] = [INTERP]  # 別の章に解釈を付ける（この章には渡していない）
    with conn:
        conn.execute(
            "UPDATE episodes SET plan_json = ? WHERE episode_id = ?",
            (json.dumps(plan, ensure_ascii=False), eid),
        )
    data = EpisodeData.load(conn, env.ssd_root, eid)
    issues = gates.g1_chapter(chapter_with(text), data, [], set(), set(), {DEF}, {DEF})
    assert any(issue in x for x in issues), issues


def test_g1_accepts_quoted_reference(env):
    eid = planned(env, "A", worry_id="W0001")
    data = EpisodeData.load(connect(env.ssd_root), env.ssd_root, eid)
    ok = f"{{{{def_src:{DEF}}}}}は、「素直」を「{{{{def:{DEF}}}}}」と説明しています。辞書を引くと、{{{{def_src:{DEF}}}}}の説明が出てきます。"
    assert gates.g1_chapter(chapter_with(ok), data, [], set(), set(), {DEF}, {DEF}) == []
    title = chapter_with(f"{{{{def_src:{DEF}}}}}を読む", kind="transition")
    assert any(
        "章タイトルに語釈・解釈の記号を使わない" in x
        for x in gates.g1_chapter(title, data, [], set(), set(), {DEF}, {DEF})
    )


def test_title_reference_bans(env):
    eid = planned(env, "A", worry_id="W0001")
    data = EpisodeData.load(connect(env.ssd_root), env.ssd_root, eid)
    assert any(
        "「本当の意味」を使わない" in x for x in gates.title_issues("素直の本当の意味とは", data)
    )
    assert any("広辞苑" in x for x in gates.title_issues("広辞苑で読む素直", data))
    assert any(
        "author・title・worry だけ" in x for x in gates.title_issues(f"{{{{def:{DEF}}}}}とは", data)
    )


def test_changed_or_unverified_reference_goes_to_review(env):
    eid = planned(env, "A", worry_id="W0001")
    # 企画のあとで語釈の本文を直し、もう1つの語釈は確かめ中に戻した
    write_refs(env.ref_cfg, def_text="ココロノ マガリナキ コト。")
    build_refs(connect(env.ssd_root), env.ref_cfg)
    conn = connect(env.ssd_root)
    data = EpisodeData.load(conn, env.ssd_root, eid)
    assert any("企画のあとで本文が変わった" in x for x in data.ref_issues)
    plan = plan_of(env, eid)
    plan["chapters"][1]["ref_ids"].append("J-dict-shoushin-1")
    with conn:
        conn.execute(
            "UPDATE episodes SET plan_json = ? WHERE episode_id = ?",
            (json.dumps(plan, ensure_ascii=False), eid),
        )
    data = EpisodeData.load(conn, env.ssd_root, eid)
    assert any("J-dict-shoushin-1：まだ画像と照らし合わせていない" in x for x in data.ref_issues)
    assert "J-dict-shoushin-1" not in data.refs
    g6 = gates.g6_review(data, {"chapters": []})
    assert g6["review_required"] and any("語釈・解釈" in x for x in g6["reasons"])


def test_ref_attach_command(env, settings_file):
    eid = planned(env, "A", worry_id="W0001")
    st = ["--settings", str(settings_file)]
    ch = next(c for c in plan_of(env, eid)["chapters"] if c["role"] == "classic")
    before = ch["target_sec"]
    r = CliRunner().invoke(
        app, ["ref", "attach", "--episode", eid, "--chapter", ch["chapter_id"], "--id", INTERP, *st]
    )
    assert r.exit_code == 0, r.output
    after = next(c for c in plan_of(env, eid)["chapters"] if c["chapter_id"] == ch["chapter_id"])
    assert after["ref_ids"] == [INTERP] and after["target_sec"] == before + 25
    assert INTERP in plan_of(env, eid)["refs"]
    # 確かめ中のものは付けられない
    r = CliRunner().invoke(
        app,
        [
            "ref",
            "attach",
            "--episode",
            eid,
            "--chapter",
            ch["chapter_id"],
            "--id",
            "J-dict-shoushin-1",
            *st,
        ],
    )
    assert r.exit_code == 2 and "transcribed" in r.output
    # 台本では、付けた章で解釈を紹介できる
    res, _ = run(env, eid, writer_llm=RefWriter(), judge_llm=RefJudge())
    assert res.state == "script_done", res.state_msg
    got = next(c for c in res.script["chapters"] if c["chapter_id"] == ch["chapter_id"])
    assert got["refs"] == [INTERP]
    r = CliRunner().invoke(
        app, ["ref", "attach", "--episode", eid, "--chapter", ch["chapter_id"], "--id", INTERP, *st]
    )
    assert r.exit_code == 2 and "--force" in r.output
    r = CliRunner().invoke(
        app,
        [
            "ref",
            "attach",
            "--episode",
            eid,
            "--chapter",
            ch["chapter_id"],
            "--id",
            INTERP,
            "--remove",
            "--force",
            *st,
        ],
    )
    assert r.exit_code == 0, r.output
    assert (
        next(c for c in plan_of(env, eid)["chapters"] if c["chapter_id"] == ch["chapter_id"])[
            "ref_ids"
        ]
        == []
    )


@pytest.mark.parametrize(
    "title,issue",
    [
        ("広辞苑で引く「素直」", "名前「広辞苑」"),
        ("「素直」の本当の意味", "「本当の意味」は根拠を示せない"),
        ("「素直」の語源をたどる", "「語源」は"),
        ("明治の人は素直を嫌った", "時代の人全体の考えにしない"),
    ],
)
def test_g1_chapter_title_reference_rules(env, title, issue):
    eid = planned(env, "A", worry_id="W0001")
    data = EpisodeData.load(connect(env.ssd_root), env.ssd_root, eid)
    issues = gates.g1_chapter(
        chapter_with(title, kind="transition"), data, [], set(), set(), {DEF}, {DEF}
    )
    assert any(issue in x for x in issues), issues


def test_s4_book_titles_are_never_named(env):
    from koten.check.books import BOOK_COLUMNS
    from tests.ref_fixture import write_csv

    write_csv(
        env.ref_cfg / "ref_books.csv",
        BOOK_COLUMNS,
        [
            {
                "book_id": "B-gendai",
                "title": "現代語訳の論語",
                "people": "今野",
                "publisher": "架空書房",
                "year": "2001",
                "kind": "translation",
                "medium": "paper_owned",
            }
        ],
    )
    build_refs(connect(env.ssd_root), env.ref_cfg)
    eid = planned(env, "A", worry_id="W0001")
    data = EpisodeData.load(connect(env.ssd_root), env.ssd_root, eid)
    assert data.s4_titles == ["現代語訳の論語"]
    issues = gates.g1_chapter(
        chapter_with("現代語訳の論語を読むと、素直は美徳です。"),
        data,
        [],
        set(),
        set(),
        {DEF},
        {DEF},
    )
    assert any("照合に使う本の名前" in x for x in issues), issues
    assert any("現代語訳の論語" in x for x in gates.title_issues("現代語訳の論語で読む素直", data))


def test_judge_ignores_brackets_inside_dictionary_text(env):
    """昔の辞書の本文にある〔語源の注〕を、事実の印と取り違えない。"""
    eid = planned(env, "A", worry_id="W0001")
    write_refs(env.ref_cfg, def_text="〔素ハ白、直ハ真直〕ココロノ マガリナキコト。")
    build_refs(connect(env.ssd_root), env.ref_cfg)
    data = EpisodeData.load(connect(env.ssd_root), env.ssd_root, eid)
    ch = chapter_with(f"{{{{def_src:{DEF}}}}}は、「素直」を「{{{{def:{DEF}}}}}」と説明しています。")
    ch["refs"] = [DEF]
    judge = gates.FaithfulnessJudge(RefJudge(), PROMPTS_DIR)
    assert judge.judge(ch, data) == []
