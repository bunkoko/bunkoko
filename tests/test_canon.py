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
    # 作り直して要旨が変わった候補は、このモデルで埋め込み直す
    rows = (kenv.cfg / "renderings.csv").read_text("utf-8").replace(ORIG, ORIG + "也")
    (kenv.cfg / "renderings.csv").write_text(rows, "utf-8")
    build_refs(connect(kenv.ssd_root), kenv.cfg)
    llm = CanonLLM()
    llm.complete = lambda *a, **k: json.dumps(  # 要旨が変わる
        {"summary_ja": "新しい要旨", "themes": ["孤独"], "worries": ["一人がつらい"],
         "quotability": 0.5, "caution": False, "caution_reason": ""}, ensure_ascii=False)
    canon_build(kenv, llm)
    r = run_embed(hdd, adir, HashEncoder(name=MODEL), passage_prefix="passage: ")
    assert r.embedded == 1  # 変わった候補だけを、このモデルで埋め込み直す
    assert "canon:lunyu:1.1" in embedded_ids(emb_dir(hdd, MODEL))
    # 別のモデルの埋め込みも、それぞれ埋め込み直す（印を取り合わない）
    r2 = run_embed(hdd, adir, HashEncoder(name="other-model"), passage_prefix="passage: ")
    assert r2.embedded > 1
    r3 = run_embed(hdd, adir, HashEncoder(name=MODEL), passage_prefix="passage: ")
    assert r3.embedded == 0


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


def test_screen_text_follows_the_reading(kenv):
    """画面の一節は読みの元と同じ本文：古い日本語訳だけならその訳、自前の訳はその訳の元。"""
    from koten.shiyaku.flow import FlowConfig, run_flow
    from koten.shiyaku.material import gather
    from koten.shiyaku.store import approve, save
    from tests.test_shiyaku import Drafter, Judge, ok_answer

    rows = (kenv.cfg / "renderings.csv").read_text("utf-8").rstrip("\n")
    rows += (
        "\nI-shimo-1.3,lunyu:1.3,,R-shimo,translation,ja,巧みな言葉は。,,たくみなことばは,"
        "5ページ,verified,owner,2026-10-29,"
        "\nI-legge-1.3,lunyu:1.3,,R-legge,translation,en,Fine words.,,,Book I ch. 3,"
        "verified,owner,2026-10-29,\n"
    )
    (kenv.cfg / "renderings.csv").write_text(rows, "utf-8")
    conn = connect(kenv.ssd_root)
    build_refs(conn, kenv.cfg)
    m = gather(conn, load_canon(CFG), "meditations:2.p1")
    sy = run_flow(m, Drafter("朝に自分に言え。"), Judge(ok_answer("I-long-2.p1", n=1)), PROMPTS,
                  FlowConfig(), shiyaku_id="S-meditations-2.p1-1")
    save(conn, sy)
    approve(conn, sy, by="owner")
    # 自前の訳のあとで、もっと古い英訳が入っても、画面は訳の元（Long）のまま
    rows = (kenv.cfg / "renderings.csv").read_text("utf-8").rstrip("\n")
    rows += ("\nI-old-2.p1,meditations:2.p1,,R-legge,translation,en,Betimes in the morning.,,,"
             "x,verified,owner,2026-10-29,\n")
    (kenv.cfg / "renderings.csv").write_text(rows, "utf-8")
    build_refs(conn, kenv.cfg)
    canon_build(kenv)
    got = {r["candidate_id"]: r for r in iter_canon(kenv.require_hdd())}
    assert got["canon:lunyu:1.3"]["source_id"] == "I-shimo-1.3"  # 英訳は訳の元でないので出さない
    assert got["canon:lunyu:1.3"]["quote_lang"] == "ja"
    assert got["canon:meditations:2.p1"]["source_id"] == "I-long-2.p1"


def test_unchecked_numbers_show_only_the_book():
    canon = load_canon(CFG)
    assert heading(canon, "meditations:2.1", numbered=False) == "第2巻"
    assert heading(canon, "lunyu:1.1", numbered=False) == ""


def test_classic_authors_are_persons(kenv):
    """古典の作者も人物の書誌に入る（生没年は入れない）。青空文庫にいる作者は同じ ID にできる。"""
    from koten.canon.build import persons_path

    canon = load_canon(CFG)
    canon["hojoki"] = {**canon["hojoki"], "author_id": "aozora:person:000196"}
    build_canon(connect(kenv.ssd_root), kenv.require_hdd(), canon, CanonLLM(), PROMPTS, VOCAB,
                AugmentConfig())
    people = {p["person_id"]: p for p in read_rows(persons_path(kenv.require_hdd()))}
    assert people["canon:person:孔子"]["name"] == "孔子"
    assert people["canon:person:孔子"]["death_date"] is None
    works = {w["work_id"]: w for w in read_rows(works_path(kenv.require_hdd()))}
    assert works["canon:hojoki"]["author_ids"] == ["aozora:person:000196"]
    assert "aozora:person:000196" not in people  # 青空文庫の人物の書誌は向こうにある
