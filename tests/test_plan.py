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
    assert plan["format"] == fmt and plan["schema_version"] == "1.3"
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
    # 回の悩みは、一節を当てはめた悩みだけ（使わない悩みを30日の間隔で止めない）
    used = {s["worry_id"] for s in plan["selection"]}
    assert [w["worry_id"] for w in plan["worries"]] == [
        w for w in [w["worry_id"] for w in plan["worries"]] if w in used
    ]
    assert {w["worry_id"] for w in plan["worries"]} == used
    assert plan["worries"][0]["worry_id"] == "W0001"


def test_plan_b_keeps_only_used_worries(env, monkeypatch):
    """5つの悩みのうち3つにだけ一節を当てはめたら、残りは回にも使用日にも入れない。"""
    import json

    from koten.worries.master import load_theme_weights, ranked_worries

    p = make_planner(env)
    seen: list[str] = []
    # この小さな表では同じテーマの悩みが2つしかないので、テーマを問わず4つまで足す
    ranked = ranked_worries(p.conn, load_theme_weights(DEFAULT_CONFIG_DIR), 90)
    monkeypatch.setattr(
        p,
        "_worries_with_themes",
        lambda themes, *, exclude, limit, first=None: [
            w for w in ranked if w["worry_id"] not in exclude
        ][:limit],
    )

    def select(fmt, worries, shortlist, rules, check, fallback):
        ids = [w["worry_id"] for w in worries]
        seen.extend(ids)
        assert "W0001 は必ず使う" in rules and ids[0] == "W0001" and len(ids) >= 4
        assert check([{**f, "worry_id": ids[1 + i % 3]} for i, f in enumerate(fallback)]) == (
            "最初の悩み"
        )
        picks = [{**f, "worry_id": ids[i % 3]} for i, f in enumerate(fallback)]
        assert check(picks) is None
        return picks, "", True

    monkeypatch.setattr(p, "llm_select", select)
    plan = p.plan("B", worry_id="W0001", now=NOW)
    assert [w["worry_id"] for w in plan["worries"]] == seen[:3]
    episode_id = p.save(plan, now=NOW)
    row = p.conn.execute(
        "SELECT worry_ids FROM episodes WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    assert json.loads(row[0]) == seen[:3]
    unused = p.conn.execute(
        "SELECT last_used_at FROM worries WHERE worry_id = ?", (seen[3],)
    ).fetchone()
    assert unused[0] is None


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
    with pytest.raises(PlanError, match="別々の人"):
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
    # やり方を替えて書き出し直しても（リランカーの比べ）、前に付けた点の行は残る
    total, judged = export_pool(path, worries, run, rows.__getitem__, [good])
    assert (total, judged) == (4, 4)
    assert {r["candidate_id"] for r in read_rows(path)} == set("abcd")


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
    # 同じ悩みは30日の間隔に当たる。試しの回なら取り消すか --force で作れると案内する
    r = CliRunner().invoke(app, ["plan", "--worry", "W0001", "--format", "A", "--dry-run", *st])
    assert r.exit_code == 2 and "30日" in r.output
    assert "koten episode discard" in r.output and "--force" in r.output
    assert "後回し" not in r.output  # 何も記録しないので「後回しにします」とは言わない
    r = CliRunner().invoke(
        app, ["plan", "--worry", "W0001", "--format", "A", "--dry-run", "--force", *st]
    )
    assert r.exit_code == 0, r.output
    # 悩みを省くと優先度の順に試す（使ったばかりの W0001 は優先度が下がる）
    r = CliRunner().invoke(app, ["plan", "--format", "D", *st])
    assert r.exit_code == 0, r.output
    assert re.match(r"E\d{8}-\d{2} 形式D", r.output)
    assert "悩み W0001：" not in r.output


def _rewrite_cache(s, change):
    """候補表の行を書き換えて索引を読み直せるようにする（訳者・別名の試し用）。"""
    import pyarrow.parquet as pq

    from koten.index.build import cache_path

    path = cache_path(s.ssd_root)
    table = pq.read_table(path)
    rows = table.to_pylist()
    change(rows)
    pq.write_table(type(table).from_pylist(rows, schema=table.schema), path)
    return rows


def _two_people(rows):
    """別々の作品・別々の著者の2行。"""
    first = next(r for r in rows if r["author_ids"])
    second = next(
        r
        for r in rows
        if r["author_ids"]
        and r["work_id"] != first["work_id"]
        and not set(r["author_ids"]) & set(first["author_ids"])
    )
    return first, second


def test_one_person_once_per_episode(env, monkeypatch):
    """1回の中で同じ人は1度だけ（形式A・C）。訳者として出る人も、別名も同じ人とみる。"""
    from types import SimpleNamespace

    picked: dict[str, tuple] = {}

    def setup(rows):
        first, second = _two_people(rows)
        for r in rows:
            if r["work_id"] == first["work_id"]:
                r["translator_ids"] = [second["author_ids"][0]]  # first の訳者が second の著者
        picked["ids"] = (first["candidate_id"], second["candidate_id"])
        picked["author"] = second["author_ids"][0]

    _rewrite_cache(env, setup)
    p = make_planner(env)
    a, b = ({"candidate_id": cid} for cid in picked["ids"])
    assert picked["author"] in p.people(p._row(a))
    assert p._same_person([a, b])
    rows = [SimpleNamespace(row=p._row(a)), SimpleNamespace(row=p._row(b))]
    assert p.diverse(rows, 5, key="person") == rows[:1]
    assert p.diverse(rows, 5, key="work") == rows

    seen: dict[str, object] = {}

    def select(fmt, worries, shortlist, rules, check, fallback):
        seen[fmt] = check
        seen[fmt + "_worries"] = [w["worry_id"] for w in worries]
        assert "別々の人（著者か訳者）" in rules
        people = [x for s in shortlist for x in p.people(s.row)]
        if fmt == "A":  # A の候補は、はじめから別々の人
            assert len(people) == len(set(people))
        return fallback, "", False

    monkeypatch.setattr(p, "llm_select", select)
    p.plan("A", worry_id="W0001", now=NOW)
    p.plan("C", theme="孤独", now=NOW)
    others = [
        {"candidate_id": cid}
        for cid in p.index.ids
        if not set(p.people(p.index.rows[p.index.ids.index(cid)]))
        & (set(p.people(p._row(a))) | set(p.people(p._row(b))))
    ]
    distinct = []
    for o in others:
        if not any(set(p.people(p._row(o))) & set(p.people(p._row(d))) for d in distinct):
            distinct.append(o)
    assert seen["A"]([a, b, *distinct[:2]]) == "同じ人"
    assert seen["A"]([a, *distinct[:3]]) is None
    ws = seen["C_worries"]
    c_of = lambda xs: [  # noqa: E731
        {**x, "worry_id": w} for x, w in zip(xs, [ws[0], ws[1], ws[2], ws[2]], strict=True)
    ]
    assert seen["C"](c_of([a, b, *distinct[:2]])) == "同じ人"
    assert seen["C"](c_of([a, *distinct[:3]])) is None


def test_people_interval_counts_translators_and_aliases(env):
    """使った人（著者と訳者）は、別名も含めて次の回から間隔をあける。"""
    from koten.index.search import Filter

    plan = make_planner(env).plan("A", worry_id="W0001", now=NOW)
    used = {a for x in plan["selection"] for a in x["author_ids"]}
    head = plan["selection"][0]["author_ids"][0]  # この回で使う著者
    holder: dict[str, str] = {}

    def setup(rows):
        # この回で使わない作品の訳者を、この回の著者の別名にする
        other = next(r for r in rows if r["author_ids"] and not set(r["author_ids"]) & used)
        holder["work"] = other["work_id"]
        for r in rows:
            if r["work_id"] == other["work_id"]:
                r["translator_ids"] = ["aozora:person:999001"]

    _rewrite_cache(env, setup)
    p = make_planner(env)
    with p.conn:
        p.conn.executemany(
            "INSERT INTO persons(person_id, canonical_id, name) VALUES (?, ?, '別名')",
            [("aozora:person:999001", head), ("aozora:person:999002", "aozora:person:999003")],
        )
    row = next(r for r in p.index.rows if r["work_id"] == holder["work"])
    assert head in p.people(row) and "aozora:person:999001" not in p.people(row)
    rows_of_other = [i for i, r in enumerate(p.index.rows) if r["work_id"] == holder["work"]]
    assert p.base_filter().exclude_author_ids == []  # まだどの回も無い

    plan["selection"][1]["translator_ids"] = ["aozora:person:999002"]  # 訳者も別名で
    p.save(plan, now=NOW)
    recorded = {r[0] for r in p.conn.execute("SELECT author_id FROM usage WHERE role = 'main'")}
    assert "aozora:person:999003" in recorded  # 訳者も、代表の ID で記録する
    assert "aozora:person:999002" not in recorded
    excluded = set(make_planner(env).base_filter().exclude_author_ids)
    assert {head, "aozora:person:999001", "aozora:person:999002"} <= excluded
    assert p.index.mask(Filter(exclude_caution=False))[rows_of_other].all()
    only = Filter(exclude_caution=False, exclude_author_ids=sorted(excluded))
    assert not p.index.mask(only)[rows_of_other].any()  # 訳者が間隔の内の人なので外れる
