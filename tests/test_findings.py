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
    ]
    for change, word in bad_cases:
        write(tmp_path, {**GOOD, **change})
        with pytest.raises(FindingsError, match=word):
            read_findings(tmp_path)
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
