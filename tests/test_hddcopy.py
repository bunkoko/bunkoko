"""HDD の引っ越し（koten hdd-copy）と控え（koten backup）。"""

import os

from typer.testing import CliRunner

from koten import runlock
from koten.cli import app
from koten.db import connect, db_path
from koten.hddcopy import copy_tree, verify_tree


def make_tree(root):
    (root / "augment" / "m" / "p").mkdir(parents=True)
    (root / "augment" / "m" / "p" / "done-00001.parquet").write_bytes(b"done")
    (root / "augment" / "m" / "p" / "._done-00001.parquet").write_bytes(b"apple")
    (root / "augment" / "m" / "p" / "done-00002.parquet.part").write_bytes(b"half")
    (root / "chunks").mkdir()
    (root / "chunks" / "aozora.parquet").write_bytes(b"x" * 1000)
    (root / ".DS_Store").write_bytes(b"ds")


def test_copy_tree_skips_mac_files_and_resumes(tmp_path):
    src, dst = tmp_path / "sd", tmp_path / "hdd"
    make_tree(src)
    r = copy_tree(src, dst)
    assert (r.copied, r.skipped, r.problems) == (2, 0, [])
    assert (dst / "chunks" / "aozora.parquet").read_bytes() == b"x" * 1000
    assert not (dst / ".DS_Store").exists()
    assert not (dst / "augment" / "m" / "p" / "._done-00001.parquet").exists()
    assert not (dst / "augment" / "m" / "p" / "done-00002.parquet.part").exists()
    # 2回目は写し済みを飛ばす
    r = copy_tree(src, dst)
    assert (r.copied, r.skipped) == (0, 2)
    # 写した先で欠けたり大きさが違えば見つける
    (dst / "chunks" / "aozora.parquet").write_bytes(b"short")
    os.utime(dst / "chunks" / "aozora.parquet", (0, 0))
    assert verify_tree(src, dst) == ["大きさが違う：chunks/aozora.parquet"]
    assert copy_tree(src, dst).copied == 1  # 違うものは写し直す


def _settings(tmp_path, hdd):
    p = tmp_path / "settings.toml"
    p.write_text(f'[paths]\nhdd_root = "{hdd}"\nssd_root = "{tmp_path / "ssd"}"\n', "utf-8")
    return ["--settings", str(p)]


def test_hdd_copy_command(tmp_path, monkeypatch):
    src = tmp_path / "SDカード" / "古典×悩み"
    make_tree(src)
    (tmp_path / "koten-hdd").mkdir()
    hdd = tmp_path / "koten-hdd" / "koten"
    st = _settings(tmp_path, hdd)
    monkeypatch.setattr(runlock, "other_processes", lambda *a, **k: [])
    r = CliRunner().invoke(app, ["hdd-copy", "--from", str(src), *st])
    assert r.exit_code == 0, r.output
    assert (hdd / "chunks" / "aozora.parquet").exists() and "確かめました" in r.output
    # augment か update が動いているあいだは写さない
    monkeypatch.setattr(runlock, "other_processes", lambda *a, **k: [(9, "koten augment")])
    r = CliRunner().invoke(app, ["hdd-copy", "--from", str(src), *st])
    assert r.exit_code == 2 and "Ctrl-C" in r.output
    # HDD がつながっていなければ止める
    monkeypatch.setattr(runlock, "other_processes", lambda *a, **k: [])
    st = _settings(tmp_path, tmp_path / "none" / "koten")
    r = CliRunner().invoke(app, ["hdd-copy", "--from", str(src), *st])
    assert r.exit_code == 2 and "つながっていません" in r.output


def test_backup_command(tmp_path):
    hdd = tmp_path / "hdd" / "koten"
    make_tree(hdd)
    (hdd / "emb").mkdir()
    (hdd / "emb" / "vec.npy").write_bytes(b"v")
    st = _settings(tmp_path, hdd)
    conn = connect(tmp_path / "ssd")
    conn.execute("INSERT INTO meta(key, value) VALUES ('test', 'ok')")
    conn.commit()
    (tmp_path / "SDカード").mkdir()
    out = tmp_path / "SDカード" / "koten-backup"
    r = CliRunner().invoke(app, ["backup", "--to", str(out), *st])
    assert r.exit_code == 0, r.output
    assert (out / "hdd" / "augment" / "m" / "p" / "done-00001.parquet").exists()
    assert (out / "hdd" / "emb" / "vec.npy").exists()
    assert not (out / "hdd" / "chunks").exists()  # 作り直せるものは写さない
    import sqlite3

    with sqlite3.connect(out / "ssd" / db_path(tmp_path / "ssd").name) as c:
        assert c.execute("SELECT value FROM meta WHERE key = 'test'").fetchone() == ("ok",)
    assert (out / "config" / "settings.toml").exists()
    assert not (out / "config" / "hdd").exists()  # config の直下のファイルだけ
    # HDD の中には置かない
    r = CliRunner().invoke(app, ["backup", "--to", str(hdd / "bk"), *st])
    assert r.exit_code == 2


def test_half_copied_hdd_is_not_used(tmp_path, monkeypatch):
    from koten.config import HddNotConnectedError, load_settings
    from koten.hddcopy import COPYING_MARK

    hdd = tmp_path / "koten-hdd" / "koten"
    hdd.mkdir(parents=True)
    st = _settings(tmp_path, hdd)
    (hdd / COPYING_MARK).write_text("x", "utf-8")
    s = load_settings(st[1])
    try:
        s.require_hdd()
    except HddNotConnectedError as e:
        assert "写している途中" in str(e)
    else:
        raise AssertionError("写している途中なのに通った")
    # 写し終えると印が消える
    src = tmp_path / "SDカード" / "古典×悩み"
    make_tree(src)
    monkeypatch.setattr(runlock, "other_processes", lambda *a, **k: [])
    r = CliRunner().invoke(app, ["hdd-copy", "--from", str(src), *st])
    assert r.exit_code == 0, r.output
    assert not (hdd / COPYING_MARK).exists() and s.require_hdd() == hdd


def test_models_load_offline_only_when_cached(tmp_path, monkeypatch):
    """落としてあるモデルだけネットに問い合わせない。プロセス全体をオフラインにしない（あとのリランカーを落とせるように）。"""
    import sys
    import types

    seen = {}

    class Fake:
        def __init__(self, model, device=None, local_files_only=False):
            seen[model] = local_files_only

        def get_embedding_dimension(self):
            return 4

    fake = types.ModuleType("sentence_transformers")
    fake.SentenceTransformer = Fake
    fake.CrossEncoder = Fake
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    (tmp_path / "hf" / "hub" / "models--intfloat--multilingual-e5-large").mkdir(parents=True)
    from koten.embed.encoder import SentenceTransformerEncoder
    from koten.retrieve.search import CrossEncoderReranker

    SentenceTransformerEncoder("intfloat/multilingual-e5-large", device="cpu")
    CrossEncoderReranker("hotchpotch/japanese-reranker-small-v2", device="cpu")
    assert seen == {
        "intfloat/multilingual-e5-large": True,
        "hotchpotch/japanese-reranker-small-v2": False,
    }
    import os

    assert "HF_HUB_OFFLINE" not in os.environ
