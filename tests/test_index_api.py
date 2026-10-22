"""埋め込み・索引・API（M2）：索引が SSD にでき、API が HDD を読まずに起動する。"""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from koten.augment.extract import AugmentConfig, iter_candidates, load_themes, run_augment
from koten.config import DEFAULT_CONFIG_DIR, REPO_ROOT, Settings
from koten.embed.encoder import HashEncoder
from koten.embed.run import emb_dir, load_embeddings, run_embed
from koten.index.bm25 import BM25, top_k
from koten.index.build import build_index, cache_path, index_dir
from koten.index.search import HotIndex
from koten.index.tokenize import bigram_tokenize
from tests.conftest import FakeLLM

MODEL = "hash-bigram"


@pytest.fixture
def augmented(hdd_with_chunks):
    s, hdd = hdd_with_chunks
    run_augment(
        hdd, REPO_ROOT / "prompts", load_themes(DEFAULT_CONFIG_DIR), FakeLLM(), AugmentConfig()
    )
    return s, hdd, hdd / "augment/fake-llm/augment_v1"


def test_bm25_ranks_matching_doc_first():
    docs = [bigram_tokenize(t) for t in ("孤独は街にある", "嫉妬は比べることから", "旅について")]
    bm = BM25.build(docs)
    ranked = top_k(bm.scores(bigram_tokenize("嫉妬")), 3, positive_only=True)
    assert ranked[0][0] == 1 and len(ranked) == 1


def test_embed_resume_and_shards(augmented):
    _, hdd, adir = augmented
    enc = HashEncoder(name=MODEL)
    first = run_embed(hdd, adir, enc, passage_prefix="passage: ", shard_size=1)
    n = len(list(iter_candidates(adir)))
    assert first.embedded == n > 0
    again = run_embed(hdd, adir, enc, shard_size=1)
    assert again.embedded == 0 and again.skipped == n
    ids, vecs = load_embeddings(emb_dir(hdd, MODEL))
    assert len(ids) == n and vecs.dtype == np.float16 and vecs.shape == (n, enc.dim)


def test_embed_refuses_other_augment_source(augmented, tmp_path):
    _, hdd, adir = augmented
    run_embed(hdd, adir, HashEncoder(name=MODEL))
    other = hdd / "augment/other/augment_v2"
    other.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="作り直す"):
        run_embed(hdd, other, HashEncoder(name=MODEL))


def _build(s, hdd, adir):
    run_embed(hdd, adir, HashEncoder(name=MODEL))
    return build_index(hdd, s.ssd_root, adir, MODEL, tokenizer="bigram")


def test_index_built_on_ssd_and_searchable(augmented):
    s, hdd, adir = augmented
    meta = _build(s, hdd, adir)
    assert meta["count"] > 0
    assert cache_path(s.ssd_root).exists()
    for name in ("dense.npy", "ids.json", "bm25.npz", "bm25_vocab.json", "meta.json"):
        assert (index_dir(s.ssd_root, MODEL) / name).exists()
    assert str(s.hdd_root) not in str(index_dir(s.ssd_root, MODEL))

    idx = HotIndex(s.ssd_root, MODEL)
    assert len(idx) == meta["count"]
    row = idx.rows[0]
    assert row["license"] == "PD_JP" and row["title"] and row["author_names"]
    qvec = HashEncoder(name=MODEL).encode([row["summary_ja"]])[0]
    assert idx.search_dense(qvec, 3)[0].row["summary_ja"] == row["summary_ja"]
    assert idx.search_bm25("比べる", 3)


def test_api_starts_without_touching_hdd(augmented, monkeypatch):
    s, hdd, adir = augmented
    _build(s, hdd, adir)
    ssd = s.ssd_root

    def no_hdd(self):
        raise AssertionError("API が HDD のパスを読んだ")

    # 受け入れ条件：API が HDD を読まずに起動する（HDD を外した状態にし、パスを読んだら失敗させる）
    monkeypatch.setattr(Settings, "hdd_root", property(no_hdd))
    monkeypatch.setattr(Settings, "require_hdd", no_hdd)
    (hdd.parent).rename(hdd.parent.with_name("unplugged"))

    from koten.api.app import create_app

    with TestClient(create_app(ssd, MODEL)) as client:
        body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["index"]["candidates"] > 0 and body["index"]["model"] == MODEL


def test_api_reports_missing_index(tmp_path):
    from koten.api.app import create_app

    with TestClient(create_app(tmp_path / "ssd", MODEL)) as client:
        body = client.get("/healthz").json()
    assert body["status"] == "no_index" and "koten index build" in body["error"]


def test_filters_are_exact(augmented):
    from koten.index.search import Filter

    s, hdd, adir = augmented
    _build(s, hdd, adir)
    idx = HotIndex(s.ssd_root, MODEL)
    works = {r["work_id"] for r in idx.rows}
    assert len(works) == 2
    a, b = sorted(works)
    q = HashEncoder(name=MODEL).encode(["他人と比べると心が乱れる"])[0]
    n = len(idx)

    assert len(idx.search_dense(q, n)) == n
    only_a = idx.search_dense(q, n, Filter(only_work_ids=[a]))
    assert only_a and {h.row["work_id"] for h in only_a} == {a}
    not_a = idx.search_dense(q, n, Filter(exclude_work_ids=[a]))
    assert {h.row["work_id"] for h in not_a} == {b}
    authors_a = idx.rows[[r["work_id"] for r in idx.rows].index(a)]["author_ids"]
    assert {
        h.row["work_id"] for h in idx.search_bm25("比べる", n, Filter(exclude_author_ids=authors_a))
    } == {b}
    assert idx.search_dense(q, n, Filter(themes_any=["孤独"])) == []

    idx._caution[0] = True  # caution の一節は既定で外れる
    assert idx.ids[0] not in {h.candidate_id for h in idx.search_dense(q, n, Filter())}
    assert idx.ids[0] in {
        h.candidate_id for h in idx.search_dense(q, n, Filter(exclude_caution=False))
    }
