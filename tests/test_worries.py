"""悩みマスター（M3）：シード30件の正規化・除外・クラスタ・優先度と、統計の読み込み。"""

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from typer.testing import CliRunner

from koten.augment.extract import load_themes
from koten.cli import app
from koten.config import DEFAULT_CONFIG_DIR, REPO_ROOT
from koten.db import StatsError, connect, load_stats
from koten.worries.master import (
    WorriesConfig,
    cluster_worries,
    import_csv,
    keyword_block,
    load_blocked_keywords,
    load_theme_weights,
    priority_score,
    ranked_worries,
    run_normalize,
)

SEED = DEFAULT_CONFIG_DIR / "worries_seed.csv"
PROMPTS = REPO_ROOT / "prompts"
VOCAB = load_themes(DEFAULT_CONFIG_DIR)
CATEGORIES = load_blocked_keywords(DEFAULT_CONFIG_DIR)

THEME_RULES = [
    (("同期", "出世", "SNS", "インスタ", "比べ"), "嫉妬・比較"),
    (("休日", "休み", "量"), "仕事の量"),
    (("ミス", "叱"), "失敗・責任"),
    (("職場", "上司"), "職場の人間関係"),
    (("転職", "向いて"), "転職の迷い"),
    (("将来",), "将来への不安"),
    (("孤独", "一人", "友達"), "孤独"),
]


class FakeWorryLLM:
    """悩みの文から決まった形の正規化を返す。「追い出したい」は攻撃として除外する。"""

    name = "fake-worry-llm"

    def __init__(self, broken: int = 0):
        self.calls = 0
        self.broken = broken
        self.texts: list[str] = []

    def complete(self, system, user, schema=None):
        self.calls += 1
        text = user.split("悩み：", 1)[1].strip()
        self.texts.append(text)
        if self.calls <= self.broken:
            return '{"text_general": "' + "長" * 50 + '", "themes": ["孤独"]}'
        theme = next((t for keys, t in THEME_RULES if any(k in text for k in keys)), "自信のなさ")
        attack = "追い出したい" in text
        return json.dumps(
            {
                "text_general": text[:40],
                "situation": "職場",
                "emotion": "不安",
                "themes": [theme, "存在しないテーマ"],
                "audience": "新社会人" if "上司" in text else "宇宙人",
                "excluded": attack,
                "excluded_category": "特定の個人や集団への攻撃" if attack else "",
                "reason": "攻撃" if attack else "",
            },
            ensure_ascii=False,
        )


class FakeEncoder:
    """言い換えのペアだけを同じ向きにし、ほかはばらばらにする埋め込み。"""

    name = "fake-encoder"
    dim = 128
    GROUPS = [("同期",), ("SNS", "インスタ"), ("休日も仕事", "休みの日も仕事")]

    def __init__(self):
        self._slots: dict[str, int] = {}

    def encode(self, texts):
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            g = next((k for k, words in enumerate(self.GROUPS) if any(w in t for w in words)), None)
            idx = g if g is not None else self._slots.setdefault(t, 10 + len(self._slots))
            out[i, idx] = 1.0
        return out


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "ssd")


def _normalized(conn, llm=None):
    import_csv(conn, SEED)
    llm = llm or FakeWorryLLM()
    r = run_normalize(conn, llm, PROMPTS, VOCAB, CATEGORIES, WorriesConfig())
    return r, llm


def test_seed_has_30_and_import_skips_duplicates(conn):
    first = import_csv(conn, SEED)
    assert first.added == 30 and not first.skipped
    again = import_csv(conn, SEED)
    assert again.added == 0 and again.duplicated == 30
    ids = [r[0] for r in conn.execute("SELECT worry_id FROM worries ORDER BY worry_id")]
    assert ids[0] == "W0001" and ids[-1] == "W0030"


def test_normalize_and_block(conn):
    r, llm = _normalized(conn)
    # 受け入れ条件：除外カテゴリが blocked になる（キーワードで止め、LLM には送らない）
    assert r.blocked == 4 and r.active == 26 and r.errors == 0
    assert llm.calls == 26
    assert set(r.blocked_by) == {"医療・診断・服薬", "お金の判断", "法律相談", "自傷・自殺・摂食"}
    sent = " ".join(llm.texts)
    for word in ("睡眠薬", "借金", "訴え", "消えてしまいたい"):
        assert word not in sent
    w = conn.execute("SELECT * FROM worries WHERE worry_id = 'W0001'").fetchone()
    assert w["status"] == "active" and w["text_general"] == "同期が先に昇進して、素直に喜べない"
    assert json.loads(w["themes"]) == ["嫉妬・比較"]  # 統制語彙に無いテーマは外す
    assert w["audience"] == "全般"  # 決まった対象層でなければ「全般」
    assert w["llm"] == "fake-worry-llm" and w["prompt_ver"] == "worries_v1"


def test_trailing_period_removed():
    from koten.worries.master import validate

    out = validate(
        {"text_general": "働く意味を見失ってしまう。", "themes": ["働く意味"]},
        set(VOCAB),
        WorriesConfig(),
    )
    assert out["text_general"] == "働く意味を見失ってしまう"


def test_llm_exclusion_also_blocks(tmp_path, conn):
    csv = tmp_path / "w.csv"
    csv.write_text(
        "text,source_type,source_ref,demand\n隣の部署の人たちを追い出したい,manual,,10\n"
    )
    import_csv(conn, csv)
    r = run_normalize(conn, FakeWorryLLM(), PROMPTS, VOCAB, CATEGORIES, WorriesConfig())
    assert r.blocked == 1 and r.blocked_by == {"特定の個人や集団への攻撃": 1}
    row = conn.execute("SELECT status, blocked_reason FROM worries").fetchone()
    assert row["status"] == "blocked" and row["blocked_reason"].startswith("LLM：")


def test_broken_output_becomes_error_then_retried(conn):
    import_csv(conn, SEED)
    r = run_normalize(
        conn, FakeWorryLLM(broken=99), PROMPTS, VOCAB, CATEGORIES, WorriesConfig(), limit=1
    )
    assert r.errors == 1
    row = conn.execute("SELECT status, error FROM worries WHERE worry_id = 'W0001'").fetchone()
    assert row["status"] == "error" and "40字" in row["error"]
    r2 = run_normalize(conn, FakeWorryLLM(), PROMPTS, VOCAB, CATEGORIES, WorriesConfig(), limit=1)
    assert r2.active == 1  # error のものは次の実行でやり直す


def test_keyword_block():
    assert keyword_block("睡眠薬を飲まないと眠れない", CATEGORIES) == ("医療・診断・服薬", "薬")
    assert keyword_block("同期が先に昇進した", CATEGORIES) is None


def test_clusters_similar_worries(conn):
    _normalized(conn)
    n = cluster_worries(conn, FakeEncoder(), threshold=0.92)
    # 受け入れ条件：似た悩みがクラスタにまとまる（言い換え3組 → 26件が23クラスタ）
    assert n == 23
    pair = conn.execute(
        "SELECT worry_id, cluster_id, is_representative FROM worries "
        "WHERE worry_id IN ('W0001', 'W0002') ORDER BY worry_id"
    ).fetchall()
    assert [tuple(p) for p in pair] == [("W0001", "W0001", 1), ("W0002", "W0001", 0)]
    blocked = conn.execute(
        "SELECT COUNT(*) FROM worries WHERE status = 'blocked' AND cluster_id IS NOT NULL"
    ).fetchone()[0]
    assert blocked == 0


def test_ranked_only_representatives(conn):
    _normalized(conn)
    cluster_worries(conn, FakeEncoder(), threshold=0.92)
    weights = load_theme_weights(DEFAULT_CONFIG_DIR)
    ranked = ranked_worries(conn, weights, 90)
    assert len(ranked) == 23
    assert all(a["score"] >= b["score"] for a, b in zip(ranked, ranked[1:], strict=False))
    ids = {r["worry_id"] for r in ranked}
    assert "W0002" not in ids and "W0027" not in ids  # 代表でない・除外
    conn.execute("UPDATE worries SET enabled = 0 WHERE worry_id = 'W0001'")
    assert "W0001" not in {r["worry_id"] for r in ranked_worries(conn, weights, 90)}


def test_priority_score():
    now = datetime(2026, 10, 23, tzinfo=UTC)
    w = {"職場の人間関係": 1.3}
    base = priority_score(["職場の人間関係"], 999, None, w, 90, now)
    assert base == pytest.approx(1.3 * (1 + 3.0))
    used = (now - timedelta(days=45)).isoformat()
    assert priority_score(["職場の人間関係"], 999, used, w, 90, now) == pytest.approx(base / 2)
    assert priority_score(["孤独"], 0, None, w, 90, now) == pytest.approx(1.0)


def test_load_stats(conn, tmp_path):
    # 受け入れ条件：stats.csv が読み込まれる
    assert load_stats(conn, DEFAULT_CONFIG_DIR / "stats.csv") == 7
    row = conn.execute("SELECT * FROM stats WHERE stat_id = 'rodo_r7_relations'").fetchone()
    assert row["value"] == "32.9" and row["url"].startswith("https://")
    bad = tmp_path / "stats.csv"
    bad.write_text("stat_id,theme,label,value,unit,survey,year,url\nx,孤独,ラベル,1,%,調査,2025,\n")
    with pytest.raises(StatsError, match="url"):
        load_stats(conn, bad)
    assert conn.execute("SELECT COUNT(*) FROM stats").fetchone()[0] == 7  # 壊れたら入れ替えない


def test_cli_flow(settings_file, monkeypatch):
    monkeypatch.setattr("koten.llm.make_llm", lambda conf, model=None: FakeWorryLLM())
    monkeypatch.setattr("koten.embed.encoder.make_encoder", lambda *a, **k: FakeEncoder())
    st = ["--settings", str(settings_file)]
    run = CliRunner().invoke
    r = run(app, ["db", "init", *st])
    assert r.exit_code == 0 and "統計 7 件" in r.output
    r = run(app, ["worries", "import", "--from", str(SEED), *st])
    assert r.exit_code == 0 and "取り込み 30 件" in r.output
    r = run(app, ["worries", "normalize", *st])
    assert r.exit_code == 0, r.output
    assert "正規化 26 件、除外 4 件" in r.output and "クラスタ 23 個" in r.output
    r = run(app, ["worries", "list", *st])
    assert r.exit_code == 0 and "W0001" in r.output and "似た悩み 1 件" in r.output
    assert "嫉妬・比較／" in r.output  # テーマの区切りは「、」（テーマ名の「・」と混ざらない）
    r = run(app, ["worries", "list", "--status", "blocked", *st])
    assert r.output.count("blocked") == 4
    r = run(app, ["worries", "import", "--from", "comments", *st])
    assert r.exit_code == 2 and "M9" in r.output
