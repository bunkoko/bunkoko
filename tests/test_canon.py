"""古典の一節を検索の候補にする（M8b。koten canon build・coverage）。"""

import json

import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from koten.augment.extract import AugmentConfig
from koten.canon.build import (
    build_canon,
    candidates_path,
    coverage,
    heading,
    iter_canon,
    reembed_path,
    works_path,
)
from koten.cli import app
from koten.config import load_settings
from koten.db import connect
from koten.embed.encoder import HashEncoder
from koten.embed.run import emb_dir, embedded_ids, run_embed
from koten.index.build import build_index, cache_path
from koten.parquet import read_rows
from koten.ref.store import build_refs
from koten.ref.texts import load_canon
from tests.m4_corpus import MODEL, build
from tests.test_shiyaku import DEFAULT_CONFIG_DIR as CFG
from tests.test_shiyaku import ORIG, ledger

PROMPTS = CFG.parent / "prompts"
VOCAB = ["嫉妬・比較", "孤独", "怠け・習慣"]


class CanonLLM:
    name = "fake-canon"

    def __init__(self, bad=0):
        self.calls = 0
        self.users: list[str] = []
        self.bad = bad

    def complete(self, system, user, schema=None):
        self.calls += 1
        self.users.append(user)
        if self.bad > 0:
            self.bad -= 1
            return json.dumps({"summary_ja": "", "themes": ["無い語"], "worries": []})
        return json.dumps(
            {
                "summary_ja": "学んで身につける喜び",
                "themes": ["怠け・習慣", "知らない語"],
                "worries": ["勉強が続かない", "比べてしまう", "x" * 40],
                "quotability": 1.7,
                "caution": False,
                "caution_reason": "理由",
            },
            ensure_ascii=False,
        )


@pytest.fixture
def kenv(settings_file, tmp_path):
    s = load_settings(settings_file)
    build(s.require_hdd(), s.ssd_root, CFG)
    cfg = tmp_path / "ref_config"
    cfg.mkdir()
    ledger(cfg)
    build_refs(connect(s.ssd_root), cfg)
    s.cfg = cfg
    return s


def canon_build(s, llm=None, **kw):
    return build_canon(
        connect(s.ssd_root),
        s.require_hdd(),
        load_canon(CFG),
        llm or CanonLLM(),
        PROMPTS,
        VOCAB,
        AugmentConfig(),
        **kw,
    )


def test_heading():
    canon = load_canon(CFG)
    assert heading(canon, "lunyu:1.1") == "第1篇 第1章"
    assert heading(canon, "meditations:2.p3") == "第2巻"  # 節の番号を確かめるまで巻だけ
    assert heading(canon, "meditations:2.3") == "第2巻 第3節"
    assert heading(canon, "hojoki:1") == "1"


def test_build_makes_candidates_from_the_sources(kenv):
    llm = CanonLLM()
    res = canon_build(kenv, llm)
    rows = {r["candidate_id"]: r for r in iter_canon(kenv.require_hdd())}
    assert set(rows) == {
        "canon:lunyu:1.1",
        "canon:lunyu:1.2",
        "canon:meditations:2.p1",
        "canon:hojoki:1",
    }
    assert res.made == 4 and res.kept == 0 and not res.failed
    r = rows["canon:lunyu:1.1"]
    # 一節は資料の本文そのまま（AI に書き写させない）。要旨などは AI。統制語彙に無いテーマは外す
    assert r["quote_text"] == ORIG and r["quote_lang"] == "zh" and r["section"] == "第1篇 第1章"
    assert r["themes"] == ["怠け・習慣"] and r["worries"][2] == "x" * 20
    assert r["quotability"] == 1.0 and r["caution_reason"] == ""
    assert r["work_id"] == "canon:lunyu" and r["source_id"] == "I-orig-1.1"
    # 英訳から訳す一節は、英訳を一節にする
    m = rows["canon:meditations:2.p1"]
    assert m["quote_lang"] == "en" and m["source_id"] == "I-long-2.p1"
    # 読みとして、日本語の訓読と英訳を見せる
    prompt = next(u for u in llm.users if ORIG in u)
    assert "学ビテ時ニ" in prompt and "The Master said" in prompt
    works = {w["work_id"]: w for w in read_rows(works_path(kenv.require_hdd()))}
    assert works["canon:lunyu"]["author_names"] == ["孔子"] and works["canon:lunyu"]["lang"] == "zh"
    assert works["canon:lunyu"]["license"] == "PD_JP"


def test_rebuild_keeps_or_remakes(kenv):
    canon_build(kenv)
    llm = CanonLLM()
    res = canon_build(kenv, llm)
    assert llm.calls == 0 and res.kept == 4  # 一節と読みが変わらなければ作り直さない
    # 一節の本文が変わったら作り直し、埋め込み直す印を残す
    rows = (kenv.cfg / "renderings.csv").read_text("utf-8").replace(ORIG, ORIG + "也")
    (kenv.cfg / "renderings.csv").write_text(rows, "utf-8")
    build_refs(connect(kenv.ssd_root), kenv.cfg)
    res = canon_build(kenv, CanonLLM())
    assert res.made == 1
    redo = json.loads(reembed_path(kenv.require_hdd()).read_text("utf-8"))
    assert redo == ["canon:lunyu:1.1"]


def test_only_passages_that_can_have_japanese(kenv):
    """訳の材料が足りず、古い日本語訳も承認済みの訳も無い一節は候補にしない。"""
    res = canon_build(kenv, min_versions=3)
    rows = {r["candidate_id"] for r in iter_canon(kenv.require_hdd())}
    assert "canon:lunyu:1.1" not in rows and "lunyu:1.1" in res.skipped
    assert "canon:lunyu:1.2" in rows  # 古い日本語訳がある一節は候補にする
    assert "canon:meditations:2.p1" in rows  # 英訳からの訳は版の数を問わない


def test_failed_llm_output_is_reported(kenv):
    res = canon_build(kenv, CanonLLM(bad=99))
    assert len(res.failed) == 4 and res.candidates == 0


def test_candidates_reach_the_search_index(kenv):
    canon_build(kenv)
    hdd = kenv.require_hdd()
    adir = hdd / "augment/fake/augment_v1"
    run_embed(hdd, adir, HashEncoder(name=MODEL), passage_prefix="passage: ")
    build_index(hdd, kenv.ssd_root, adir, MODEL, tokenizer="bigram")
    cache = {r["candidate_id"]: r for r in pq.read_table(cache_path(kenv.ssd_root)).to_pylist()}
    c = cache["canon:lunyu:1.1"]
    assert c["passage_key"] == "lunyu:1.1" and c["lang"] == "zh" and c["title"] == "論語"
    assert c["author_names"] == ["孔子"] and c["license"] == "PD_JP"
    assert any(cid.startswith("aozora:") for cid in cache)  # 青空文庫の候補と一緒
    # 企画は、古典の一節の候補が混ざっても作れる（論語が選ばれれば、台本の前に訳を作る）
    from datetime import timedelta

    from tests.test_script import NOW, planned

    for i, fmt in enumerate(("A", "C", "D")):  # B はこの小さな作品集では一節が足りない
        kw = {"theme": "孤独"} if fmt == "C" else {"worry_id": "W0001"}
        assert planned(kenv, fmt, when=NOW + timedelta(days=40 * i), **kw)
    # 作り直した候補は埋め込み直し、印を消す
    rows = (kenv.cfg / "renderings.csv").read_text("utf-8").replace(ORIG, ORIG + "也")
    (kenv.cfg / "renderings.csv").write_text(rows, "utf-8")
    build_refs(connect(kenv.ssd_root), kenv.cfg)
    canon_build(kenv)
    r = run_embed(hdd, adir, HashEncoder(name=MODEL), passage_prefix="passage: ")
    assert r.embedded == 1 and not reembed_path(hdd).exists()
    assert "canon:lunyu:1.1" in embedded_ids(emb_dir(hdd, MODEL))


def test_coverage(kenv):
    rows = {c.work: c for c in coverage(connect(kenv.ssd_root), load_canon(CFG))}
    lunyu = rows["lunyu"]
    assert (lunyu.passages, lunyu.pd, lunyu.draftable, lunyu.missing) == (2, 1, 1, 0)
    assert lunyu.without_pd == 0.5
    assert rows["meditations"].draftable == 1


def test_cli(kenv, settings_file, monkeypatch):
    monkeypatch.setattr("koten.llm.make_llm", lambda conf, model=None: CanonLLM())
    st = ["--settings", str(settings_file)]
    r = CliRunner().invoke(app, ["canon", "build", *st])
    assert r.exit_code == 0 and "古典の一節の候補 4 件" in r.output, r.output
    assert candidates_path(kenv.require_hdd()).exists()
    r = CliRunner().invoke(app, ["canon", "coverage", *st])
    assert r.exit_code == 0 and "論語（lunyu）：一節 2" in r.output, r.output
    assert "古い日本語訳の無い割合 50%" in r.output
