"""研究の台帳（D-034）：読み込みの決まり・SSD の表・企画・記号・G1・台本。"""

import json
import re

import pytest

from koten.db import connect
from koten.findings import (
    COLUMNS,
    FindingsError,
    build_findings,
    finding_shas,
    findings_for,
    load_findings,
    read_findings,
)
from koten.script import gates
from koten.script.data import EpisodeData
from tests.test_script import (
    FakeScriptLLM,
    env,  # noqa: F401  フィクスチャ
    planned,
    run,
)

GOOD = {
    "finding_id": "FN-test-1",
    "themes": "嫉妬・比較",
    "claim_ja": "人と比べる時間が長い人ほど、自分の暮らしに満足しにくい傾向がありました",
    "limits_ja": "アメリカの大学生を調べたもので、働く人にも当てはまるかは分かっていません",
    "effect_ja": "小さい",
    "design": "survey",
    "population_ja": "アメリカの大学生300人",
    "country": "US",
    "affiliation_ja": "ミシガン大学",
    "authors_ja": "",
    "authors": "Doe, J.; Roe, R.",
    "year": "2019",
    "title": "Social comparison and life satisfaction",
    "journal": "Journal of Examples",
    "volume": "12",
    "issue": "3",
    "pages": "45-67",
    "doi": "10.0000/example.1",
    "url": "",
    "license": "COPYRIGHTED",
    "replication": "mixed",
    "replication_note": "後のメタ分析では効果はまちまち",
    "retraction_checked_at": "2026-11-05",
    "verified_from": "full_text",
    "checked_by": "オーナー",
    "checked_at": "2026-11-05",
    "status": "verified",
    "note": "",
}


def write(config_dir, *rows):
    import csv

    (config_dir / "themes.yaml").write_text("themes:\n  - 嫉妬・比較\n  - 孤独\n", "utf-8")
    with (config_dir / "findings.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_read_findings_rules(tmp_path):
    assert read_findings(tmp_path) == []  # 台帳が無ければ空
    write(tmp_path, GOOD)
    (f,) = read_findings(tmp_path)
    assert f.usable and f.source_label() == "ミシガン大学の調査（2019年）"
    assert f.citation().startswith("Doe, J.; Roe, R. (2019). Social comparison")
    assert "12(3), 45-67" in f.citation() and "https://doi.org/10.0000/example.1" in f.citation()
    bad_cases = [
        ({"verified_from": "abstract"}, "本文"),  # 要旨だけでは verified にしない
        ({"replication": "failed"}, "再現しなかった"),
        ({"limits_ja": ""}, "limits_ja"),  # まちまちなら限界を書く
        ({"themes": "未知"}, "themes.yaml"),
        ({"claim_ja": "比べる心は必ず不幸を生むと証明された"}, "言いすぎ"),
        ({"affiliation_ja": "", "authors_ja": ""}, "affiliation_ja"),
        ({"retraction_checked_at": ""}, "取り下げ"),
        ({"design": "anecdote"}, "design"),
        ({"finding_id": "X-1"}, "FN-"),
        # 見直しで足した決まり
        ({"claim_ja": "比べる人ほど満足しにくい。働く人も同じでした"}, "1文"),
        ({"claim_ja": "誰もが例外なく、比べると満足しにくくなります"}, "言いすぎ"),
        ({"claim_ja": "人と比べることが、暮らしへの不満の原因になります"}, "原因"),
        ({"limits_ja": "大学生だけで、効果は証明されていません"}, "limits_ja"),
        ({"doi": "see library"}, "doi"),
        ({"doi": "", "url": "図書館で読んだ"}, "url"),
        ({"license": "CC BY-NC-ND 4.0"}, "license"),
        ({"note": "x" * 201}, "note"),
        ({"claim_ja": "People who compare themselves more are less satisfied with life"}, "日本語"),
        ({"replication_note": ""}, "replication_note"),
        ({"year": "２０１９"}, "year"),
        ({"retraction_checked_at": "2026-13-45"}, "retraction_checked_at"),
        ({"retraction_checked_at": "2099-01-01"}, "retraction_checked_at"),
        ({"retraction_checked_at": "2018-01-01"}, "論文の年より前"),
        ({"checked_at": "2026-02-30"}, "checked_at"),
    ]
    for change, word in bad_cases:
        write(tmp_path, {**GOOD, **change})
        with pytest.raises(FindingsError, match=word):
            read_findings(tmp_path)
    # 限界の「必ずしも」は打ち消しなので通す。実験なら原因と書ける
    write(tmp_path, {**GOOD, "limits_ja": "働く人に必ずしも当てはまるとは限りません"})
    assert read_findings(tmp_path)[0].usable
    caused = {"design": "experiment", "claim_ja": "比べる場面が不満の原因になりました"}
    write(tmp_path, {**GOOD, **caused})
    assert read_findings(tmp_path)[0].usable
    # 一文の終わりの「。」は外す。DOI は https://doi.org/ や doi: を外す
    write(tmp_path, {**GOOD, "claim_ja": GOOD["claim_ja"] + "。", "doi": "https://doi.org/10.1002/x"})
    (f,) = read_findings(tmp_path)
    assert f.claim_ja == GOOD["claim_ja"] and f.citation().endswith("https://doi.org/10.1002/x")
    write(tmp_path, {**GOOD, "doi": "doi:10.1002/job.635"})
    assert read_findings(tmp_path)[0].doi == "10.1002/job.635"
    # 考え方・メタ分析は、大学の公式の考えや、全部その大学の研究に聞こえないように言う
    labels = {
        d: (write(tmp_path, {**GOOD, "design": d}), read_findings(tmp_path)[0].source_label())[1]
        for d in ("theory", "meta_analysis", "review")
    }
    assert labels == {
        "theory": "ミシガン大学が示した考え方（2019年）",
        "meta_analysis": "ミシガン大学による、多くの研究をまとめた分析（2019年）",
        "review": "ミシガン大学による研究のまとめ（2019年）",
    }
    # 下書き（要旨だけで確かめた）は読み込むが、台本では使わない
    write(tmp_path, {**GOOD, "status": "draft", "verified_from": "abstract"})
    (d,) = read_findings(tmp_path)
    assert not d.usable


def test_build_and_select(tmp_path):
    write(
        tmp_path,
        GOOD,
        {**GOOD, "finding_id": "FN-test-2", "themes": "孤独"},
        {**GOOD, "finding_id": "FN-test-3", "status": "draft", "verified_from": "abstract"},
    )
    conn = connect(tmp_path / "ssd")
    assert build_findings(conn, read_findings(tmp_path)) == 3
    assert findings_for(conn, ["嫉妬・比較"]) == ["FN-test-1"]  # 下書きは出さない
    assert findings_for(conn, ["孤独", "嫉妬・比較"]) == ["FN-test-1", "FN-test-2"]
    got = load_findings(conn, ["FN-test-1"])["FN-test-1"]
    assert got["source_label"] == "ミシガン大学の調査（2019年）" and got["usable"] == 1
    assert set(finding_shas(conn, ["FN-test-1"])) == {"FN-test-1"}
    # 台帳から消した行は、表からも消える
    write(tmp_path, GOOD)
    build_findings(conn, read_findings(tmp_path))
    assert load_findings(conn, ["FN-test-2"]) == {}


@pytest.fixture
def fenv(env, tmp_path):  # noqa: F811
    # テストの設定は本物の config/ を指すので、台帳だけ tmp に置いて SSD の表を作る
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    write(cfg, GOOD)
    build_findings(connect(env.ssd_root), read_findings(cfg))
    return env


def test_planner_attaches_findings(fenv):
    eid = planned(fenv, "A", worry_id="W0001")
    data = EpisodeData.load(connect(fenv.ssd_root), fenv.ssd_root, eid)
    roles = {c["role"]: c.get("finding_ids", []) for c in data.plan["chapters"]}
    assert "FN-test-1" in sum(roles.values(), [])
    assert all(not ids for role, ids in roles.items() if role == "classic")  # 古典の章には渡さない
    assert set(data.plan["findings"]) == {"FN-test-1"} and "FN-test-1" in data.findings
    r = gates.resolver_for(data)
    assert r.resolve("{{finding_src:FN-test-1}}") == "ミシガン大学の調査（2019年）"
    assert r.resolve("{{finding:FN-test-1}}").startswith("人と比べる時間が長い人ほど")


def test_g1_finding_rules(fenv):
    eid = planned(fenv, "A", worry_id="W0001")
    data = EpisodeData.load(connect(fenv.ssd_root), fenv.ssd_root, eid)

    def issues(text, allowed=frozenset({"FN-test-1"})):
        seg = {"seg_id": "ch9-s1", "kind": "narration", "text": text}
        ch = {"chapter_id": "ch9", "segments": [seg]}
        return gates.g1_chapter(ch, data, [], allowed_findings=set(allowed))

    ok = (
        "{{finding_src:FN-test-1}}では、{{finding:FN-test-1}}。ただ、これは大学生を調べたものです。"
    )
    assert issues(ok) == []
    # 研究の言葉も、研究の記号と同じ文なら言える
    assert issues("{{finding_src:FN-test-1}}の研究によると、{{finding:FN-test-1}}。") == []
    assert any("渡していない研究" in x for x in issues(ok, allowed=frozenset()))
    assert any("出典の記号" in x for x in issues("{{finding:FN-test-1}}。"))
    assert any("「ミシガン大学」を直接書いている" in x for x in issues("ミシガン大学の調べです。"))
    assert any(gates.EVIDENCE in x for x in issues("ある大学の研究では、そう分かっています。"))
    # 限界は研究の言葉を使わずに書く（次の文の「この研究では」も止め、研究を外して書き直す）
    first = "{{finding_src:FN-test-1}}では、{{finding:FN-test-1}}。"
    assert issues(first + "ただ、調べたのはアメリカの大学生だけです。") == []
    assert issues(first + "ただ、この研究では効果が大きかったそうです。")
    assert issues("{{finding_src:FN-test-1}}の研究チームによると、{{finding:FN-test-1}}。") == []
    # 出典の記号に作った結果を付けない。研究の記号の隣でも言いすぎ・ほかの研究・専門家は止める
    smuggled = [
        "{{finding_src:FN-test-1}}では、人と比べる人は必ず不幸になると科学的に証明されています。",
        "{{finding_src:FN-test-1}}では、人と比べる人は必ず不幸になります。",
        "{{finding_src:FN-test-1}}など、多くの研究者や精神科医がエビデンスを示しています。",
        "{{finding_src:FN-test-1}}では、{{finding:FN-test-1}}と、科学的に証明されました。",
        "{{finding_src:FN-test-1}}では、{{finding:FN-test-1}}、ハーバード大学の研究でも同じことが"
        "分かっていて、専門家も比べるのをやめるよう勧めています。",
        "{{finding_src:FN-test-1}}では、{{finding:FN-test-1}}（ほかの多くの研究でも同じ結果です。"
        "医師も、比べるのをやめるよう勧めています）。",
        "{{finding_src:FN-test-1}}など多くの研究で、{{finding:FN-test-1}}。",
        ok + "研究チームの医師も、比べるのをやめるよう勧めています。",
    ]
    for text in smuggled:
        assert issues(text), text


class FindingWriter(FakeScriptLLM):
    """渡された研究の一文を、出典の記号と一緒に使う偽の台本作家。"""

    def complete(self, system, user, schema=None):
        out = json.loads(super().complete(system, user, schema))
        for fid in re.findall(r"研究ID：(FN-\S+)", user):
            out["segments"].append(
                {
                    "kind": "narration",
                    "text": f"{{{{finding_src:{fid}}}}}では、{{{{finding:{fid}}}}}。"
                    "ただ、これは大学生を調べたものです。",
                }
            )
        return json.dumps(out, ensure_ascii=False)


def test_script_uses_findings(fenv):
    eid = planned(fenv, "A", worry_id="W0001")
    r, _ = run(fenv, eid, writer_llm=FindingWriter())
    assert r.state == "script_done", r.state_msg
    used = [c for c in r.script["chapters"] if c.get("findings")]
    assert used and used[0]["findings"] == ["FN-test-1"]
    seg = next(s for c in used for s in c["segments"] if s.get("findings"))
    assert seg["visual"] == {"type": "finding_card", "ref": "FN-test-1"}
    record = r.verification["findings"]["used"]
    assert record[0]["source_label"] == "ミシガン大学の調査（2019年）"
    assert record[0]["citation"].startswith("Doe, J.")


def test_changed_finding_needs_review(fenv, tmp_path):
    eid = planned(fenv, "A", worry_id="W0001")
    cfg = tmp_path / "cfg2"
    cfg.mkdir()
    write(cfg, {**GOOD, "claim_ja": "人と比べる時間が長い人ほど、満足しにくいことがありました"})
    build_findings(connect(fenv.ssd_root), read_findings(cfg))
    data = EpisodeData.load(connect(fenv.ssd_root), fenv.ssd_root, eid)
    assert any("変わった" in x for x in data.finding_issues)
    write(cfg, {**GOOD, "status": "draft", "verified_from": "abstract"})
    build_findings(connect(fenv.ssd_root), read_findings(cfg))
    data = EpisodeData.load(connect(fenv.ssd_root), fenv.ssd_root, eid)
    assert any("使えない" in x for x in data.finding_issues) and not data.findings


class ClassicFindingWriter(FindingWriter):
    """研究の記号を、古典の章にも書いてしまう偽の台本作家。"""

    def complete(self, system, user, schema=None):
        out = json.loads(FakeScriptLLM.complete(self, system, user, schema))
        fids = re.findall(r"研究ID：(FN-\S+)", user) or (
            ["FN-test-1"] if "（classic）" in user or "古典の一節" in user else []
        )
        for fid in fids:
            out["segments"].append(
                {
                    "kind": "narration",
                    "text": f"{{{{finding_src:{fid}}}}}では、{{{{finding:{fid}}}}}。",
                }
            )
        return json.dumps(out, ensure_ascii=False)


def test_classic_chapters_cannot_use_findings(fenv):
    eid = planned(fenv, "A", worry_id="W0001")
    r, _ = run(fenv, eid, writer_llm=ClassicFindingWriter())
    # 前の章（悩みを見る章）で使った研究でも、古典の章では指せない
    assert r.state == "error" and "渡していない研究" in r.state_msg, r.state_msg


class InventedLimitsWriter(FakeScriptLLM):
    """研究の一文のあとに、研究の言葉で作った話を足す偽の台本作家（研究を外せば通る）。"""

    def complete(self, system, user, schema=None):
        out = json.loads(super().complete(system, user, schema))
        for fid in re.findall(r"研究ID：(FN-\S+)", user):
            out["segments"].append(
                {
                    "kind": "narration",
                    "text": f"{{{{finding_src:{fid}}}}}では、{{{{finding:{fid}}}}}。"
                    "ほかの研究でも、同じ結果が出ています。",
                }
            )
        return json.dumps(out, ensure_ascii=False)


def test_research_word_issue_drops_the_finding(fenv):
    """研究を使った版が「ほかの研究でも」で止まったら、回を止めずに研究を外して書き直す。"""
    eid = planned(fenv, "A", worry_id="W0001")
    r, _ = run(fenv, eid, writer_llm=InventedLimitsWriter())
    assert r.state == "script_done", r.state_msg
    assert not any(c.get("findings") for c in r.script["chapters"])
    assert any("研究を外した" in x for x in r.verification["viewing"]["issues"])


def test_research_chapter_and_render(fenv, tmp_path):
    """研究が3つあれば研究の章ができ、名前と書き方が付く。確認画面に研究と限界が出る。"""
    cfg = tmp_path / "cfg3"
    cfg.mkdir()
    write(cfg, *[{**GOOD, "finding_id": f"FN-test-{i}"} for i in (1, 2, 3)])
    build_findings(connect(fenv.ssd_root), read_findings(cfg))
    eid = planned(fenv, "A", worry_id="W0001")
    data = EpisodeData.load(connect(fenv.ssd_root), fenv.ssd_root, eid)
    research = [c for c in data.plan["chapters"] if c["role"] == "research"]
    assert research and research[0]["finding_ids"] == ["FN-test-3"]
    from koten.script.writer import ROLE_GUIDE, ROLE_NAMES

    assert ROLE_NAMES["research"] and "finding_src" in ROLE_GUIDE["research"]
    assert not gates.title_issues(ROLE_NAMES["research"], data)  # 仮の章タイトルでも止まらない
    r, _ = run(fenv, eid, writer_llm=FindingWriter())
    assert r.state == "script_done", r.state_msg
    from koten.script.run import render

    text = "\n".join(render(r.script, gates.resolver_for(data)))
    assert "【研究｜FN-test-" in text and "限界：アメリカの大学生" in text
    assert "書誌：Doe, J." in text


def test_judge_schema_has_every_sentence_type():
    enum = gates.JUDGE_SENTENCES_SCHEMA["properties"]["sentences"]["items"]["properties"]["type"]
    assert set(enum["enum"]) == gates.SENTENCE_TYPES and "evidence" in enum["enum"]
    sentences = [("ch1-s1", "多くの人が悩んでいます。", set())]
    got = {1: {"n": 1, "type": "evidence", "supported_by": [], "ok": True, "problem": ""}}
    (v,) = gates.sentence_verdict(sentences, got, set(), [])
    assert v.startswith(gates.EVIDENCE)


class InventedResearchChapter(FindingWriter):
    """研究の章でだけ、渡していない研究の話を作る偽の台本作家。"""

    def complete(self, system, user, schema=None):
        out = json.loads(super().complete(system, user, schema))
        if "（別の角度から考える）" in user:
            out["segments"].append(
                {"kind": "narration", "text": "ある大学の研究でも、同じことが分かっています。"}
            )
        return json.dumps(out, ensure_ascii=False)


def test_failing_research_chapter_is_dropped(fenv, tmp_path):
    """研究の章が直らなければ、回を止めずに章ごと外す。"""
    cfg = tmp_path / "cfg4"
    cfg.mkdir()
    write(cfg, *[{**GOOD, "finding_id": f"FN-test-{i}"} for i in (1, 2, 3)])
    build_findings(connect(fenv.ssd_root), read_findings(cfg))
    eid = planned(fenv, "A", worry_id="W0001")
    r, logs = run(fenv, eid, writer_llm=InventedResearchChapter())
    assert r.state == "script_done", r.state_msg
    assert not any(c["role"] == "research" for c in r.script["chapters"])
    assert any("研究の章を外した" in x for x in r.verification["viewing"]["issues"])
