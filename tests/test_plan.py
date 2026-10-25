"""検索と企画（M4）：RRF・クエリの控え・リランク・形式A〜Dの plan_json・再使用間隔・評価。"""

import re
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from koten.augment.extract import load_themes
from koten.cli import app
from koten.config import DEFAULT_CONFIG_DIR, REPO_ROOT, load_settings
from koten.db import connect
from koten.embed.encoder import HashEncoder
from koten.eval.retrieval import (
    EvalConfig,
    evaluate,
    export_pool,
    ndcg_at_k,
    read_eval,
    recall_at_k,
)
from koten.index.search import Hit, HotIndex
from koten.plan.planner import PlanError, Planner
from koten.retrieve.search import apply_rerank, rrf_fuse, transform_query
from tests.m4_corpus import MODEL, FakePlanLLM, build

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def env(settings_file):
    s = load_settings(settings_file)
    hdd = s.require_hdd()
    build(hdd, s.ssd_root, DEFAULT_CONFIG_DIR)
    return s


def make_planner(s, llm=None, reranker=None):
    return Planner(
        conn=connect(s.ssd_root),
        index=HotIndex(s.ssd_root, MODEL),
        encoder=HashEncoder(name=MODEL),
        llm=llm or FakePlanLLM(),
        reranker=reranker,
        prompts_dir=REPO_ROOT / "prompts",
        config_dir=DEFAULT_CONFIG_DIR,
        vocab=load_themes(DEFAULT_CONFIG_DIR),
    )


def test_rrf_fuse_order():
    def hits(*ids):
        return [Hit(i, 0.0, {"id": i}) for i in ids]

    fused = rrf_fuse({"x": hits("a", "b", "c"), "y": hits("b", "a")}, k=60)
    assert [s.candidate_id for s in fused] == ["a", "b", "c"] or fused[0].candidate_id == "b"
    a = next(s for s in fused if s.candidate_id == "a")
    assert a.rrf == pytest.approx(1 / 61 + 1 / 62) and a.ranks == {"x": 1, "y": 2}
    assert fused[-1].candidate_id == "c"


def test_query_transform_is_cached(env):
    conn = connect(env.ssd_root)
    llm = FakePlanLLM()
    vocab = load_themes(DEFAULT_CONFIG_DIR)
    args = (conn, llm, REPO_ROOT / "prompts", "query_v1", "W0001", "同期の昇進", vocab)
    q1 = transform_query(*args, known_themes=["孤独"])
    q2 = transform_query(*args, known_themes=["孤独"])
    assert llm.calls["query"] == 1
    assert q1 == q2 and q1.themes == ["孤独", "嫉妬・比較"]  # 統制語彙に無い「未知」は外す
    assert q1.hyde.endswith("和らげる")


def test_rerank_reorders():
    class Reverse:
        name = "reverse"

        def score(self, query, docs):
            return [float(i) for i in range(len(docs))]

    items = rrf_fuse({"x": [Hit(c, 0, {"quote_text": c, "summary_ja": c}) for c in "abc"]})
    out = apply_rerank(items, Reverse(), "q")
    assert [s.candidate_id for s in out] == ["c", "b", "a"] and out[0].rerank == 2.0


def _works(plan):
    return [s["work_id"] for s in plan["selection"]]


def _check_common(plan, fmt):
    assert plan["format"] == fmt and plan["schema_version"] == "1.0"
    assert 600 <= plan["target_total_sec"] <= 900
    assert sum(1 for c in plan["chapters"] if c.get("human_take")) == 1
    ids = [c["chapter_id"] for c in plan["chapters"]]
    assert ids == [f"ch{i}" for i in range(len(ids))]
    used = [cid for c in plan["chapters"] for cid in c["candidate_ids"]]
    assert sorted(used) == sorted(s["candidate_id"] for s in plan["selection"])
    assert plan["ad_break_after_chapters"] == ids[1:-1]


def test_plan_a(env):
    plan = make_planner(env).plan("A", worry_id="W0001", now=NOW)
    _check_common(plan, "A")
    assert 4 <= len(plan["selection"]) <= 6
    assert len(set(_works(plan))) == len(plan["selection"])  # 別々の作品
    assert plan["selected_by"] == "llm" and all(s["reason"] for s in plan["selection"])
    context = next(c for c in plan["chapters"] if c["role"] == "context")
    assert context["stat_ids"] == []  # 嫉妬・比較の統計は stats.csv に無い
    classic = [c for c in plan["chapters"] if c["role"] == "classic"]
    assert 3 <= len(classic) <= 5


def test_plan_b(env):
    plan = make_planner(env).plan("B", worry_id="W0001", now=NOW)
    _check_common(plan, "B")
    assert len(set(_works(plan))) == 1 and plan["work_id"] == _works(plan)[0]
    assert 4 <= len(plan["selection"]) <= 6
    assert len({w["worry_id"] for w in plan["worries"]}) >= 3


def test_plan_c(env):
    plan = make_planner(env).plan("C", theme="孤独", now=NOW)
    _check_common(plan, "C")
    assert plan["theme"] == ["孤独"] and len(plan["worries"]) == 3
    assert 4 <= len(plan["selection"]) <= 6
    assert len(set(_works(plan))) == len(plan["selection"])
    context = next(c for c in plan["chapters"] if c["role"] == "context")
    assert context["stat_ids"] == []  # 孤独の統計は、値を転記するまで stats.csv に無い
    assert make_planner(env)._stats_for(["仕事の量"]) == ["rodo_r7_amount"]


def test_plan_d(env):
    plan = make_planner(env).plan("D", worry_id="W0001", now=NOW)
    _check_common(plan, "D")
    sides = {s["side"]: s["authors"][0] for s in plan["selection"]}
    assert set(sides) == {"A", "B"} and sides["A"] != sides["B"]
    assert plan["question"]
    views = {c["role"]: c["candidate_ids"] for c in plan["chapters"]}
    assert views["view_a"] and views["view_b"]


def test_fallback_and_d_needs_llm(env):
    p = make_planner(env, llm=FakePlanLLM(broken_select=True))
    plan = p.plan("A", worry_id="W0001", now=NOW)
    assert plan["selected_by"] == "search_rank"
    assert all(s["reason"].startswith("検索の順位で選んだ") for s in plan["selection"])
    with pytest.raises(PlanError, match="2人の著者"):
        p.plan("D", worry_id="W0001", now=NOW)


def test_save_recency_and_rotation(env):
    p = make_planner(env)
    first = p.plan("A", worry_id="W0001", now=NOW)
    eid = p.save(first, now=NOW)
    assert eid == "E20261101-01"
    # 同じクラスタの悩みは30日以内に主役にしない
    with pytest.raises(PlanError, match="30日"):
        p.plan("A", worry_id="W0001", now=NOW + timedelta(days=10))
    assert p.plan("A", worry_id="W0001", force=True, now=NOW)  # --force で外せる
    # 直近10回の作品は使わない
    second = p.plan("A", worry_id="W0003", now=NOW + timedelta(hours=1))
    assert not set(_works(second)) & set(_works(first))
    p.save(second, now=NOW + timedelta(hours=1))
    row = p.conn.execute(
        "SELECT state, worry_ids FROM episodes WHERE episode_id = ?", (eid,)
    ).fetchone()
    assert row["state"] == "planned"
    used = p.conn.execute("SELECT COUNT(*) FROM usage WHERE episode_id = ?", (eid,)).fetchone()[0]
    assert used == len(first["selection"])


def test_allowed_formats_avoids_three_in_a_row(env):
    p = make_planner(env)
    assert p.allowed_formats()[0] in "ABCD"
    with p.conn:
        for i in range(2):
            p.conn.execute(
                "INSERT INTO episodes(episode_id, format, worry_ids, state, created_at) "
                "VALUES (?, 'A', '[]', 'planned', ?)",
                (f"E2026010{i + 1}-01", f"2026-01-0{i + 1} 00:00:00"),
            )
    allowed = p.allowed_formats()
    assert "A" not in allowed and allowed[0] in "BCD"


def test_shortage_raises(env):
    p = make_planner(env)
    with p.conn:
        for i, wid in enumerate(["001", "002", "003", "005", "006", "009", "010", "011"]):
            p.conn.execute(
                "INSERT INTO usage VALUES (?, 'x', ?, NULL, 'main')", (f"E0-{i}", f"aozora:{wid}")
            )
            p.conn.execute(
                "INSERT INTO episodes(episode_id, format, worry_ids, state) "
                "VALUES (?, 'B', '[]', 'planned')",
                (f"E0-{i}",),
            )
    with pytest.raises(PlanError, match="別々の作品"):
        p.plan("A", worry_id="W0003", now=NOW)


def test_blocked_worry_cannot_be_planned(env):
    p = make_planner(env)
    with p.conn:
        p.conn.execute(
            "UPDATE worries SET status = 'blocked', blocked_reason = 'x' WHERE worry_id = 'W0001'"
        )
    with pytest.raises(PlanError, match="blocked"):
        p.plan("A", worry_id="W0001", now=NOW)


def test_metrics():
    grades = {"a": 2, "b": 1, "c": 0, "d": 2}
    assert ndcg_at_k(["a", "d", "b"], grades) == pytest.approx(1.0)
    assert ndcg_at_k(["c", "x"], grades) == 0.0
    assert recall_at_k(["a", "x", "b"], grades, 20) == pytest.approx(2 / 3)
    assert recall_at_k(["a"], {"a": 0}, 20) is None


def test_eval_export_and_evaluate(tmp_path):
    from koten.eval.retrieval import read_rows, write_eval

    rows = {
        c: {"title": "t", "author_names": ["甲"], "section": "", "quote_text": c, "summary_ja": c}
        for c in "abcd"
    }
    worries = [{"worry_id": "W1", "text_general": "悩み"}]
    good = EvalConfig("良い", ("text",), ("dense",))
    bad = EvalConfig("悪い", ("text",), ("bm25",))

    def run(wid, cfg, k):
        return (["a", "b", "c"] if cfg is good else ["c", "d"])[:k]

    path = tmp_path / "eval.csv"
    total, judged = export_pool(path, worries, run, rows.__getitem__, [good, bad])
    assert (total, judged) == (4, 0)
    graded = read_rows(path)
    for r in graded:
        r["grade"] = "1" if r["candidate_id"] == "a" else "0"
        r["llm_grade"] = "1"
    write_eval(path, graded)
    assert export_pool(path, worries, run, rows.__getitem__, [good, bad]) == (4, 4)  # 点は残る
    assert all(r["llm_grade"] == "1" for r in read_rows(path))
    res = {r.name: r for r in evaluate(read_eval(path), run, [good, bad])}
    assert res["良い"].ndcg10 == pytest.approx(1.0) and res["悪い"].ndcg10 == 0.0
    assert res["良い"].recall20 == 1.0 and res["悪い"].unjudged10 == 0.0


def test_cli_retrieve_and_plan(env, settings_file, monkeypatch):
    with settings_file.open("a", encoding="utf-8") as f:
        f.write('\n[bm25]\ntokenizer = "bigram"\n')
    text = settings_file.read_text().replace(
        'model = "intfloat/multilingual-e5-large"', f'model = "{MODEL}"'
    )
    settings_file.write_text(text)
    monkeypatch.setattr("koten.llm.make_llm", lambda conf, model=None: FakePlanLLM())
    st = ["--settings", str(settings_file)]
    r = CliRunner().invoke(app, ["retrieve", "--worry", "W0001", "--explain", "--top", "3", *st])
    assert r.exit_code == 0, r.output
    assert "言い換え：" in r.output and "理由：RRF" in r.output and "注意フラグ" in r.output
    for fmt in "ABCD":
        args = ["plan", "--worry", "W0001", "--format", fmt, "--dry-run", *st]
        if fmt == "C":
            args = ["plan", "--theme", "孤独", "--format", "C", "--dry-run", *st]
        r = CliRunner().invoke(app, args)
        assert r.exit_code == 0, (fmt, r.output)
        assert f"形式{fmt}" in r.output and "（保存していません）" in r.output
    r = CliRunner().invoke(app, ["plan", "--worry", "W0001", *st])
    assert r.exit_code == 0 and r.output.startswith("E")
    # 悩みを省くと優先度の順に試す（使ったばかりの W0001 は優先度が下がる）
    r = CliRunner().invoke(app, ["plan", "--format", "D", *st])
    assert r.exit_code == 0, r.output
    assert re.match(r"E\d{8}-\d{2} 形式D", r.output)
    assert "悩み W0001：" not in r.output
