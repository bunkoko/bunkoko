"""HDD の引っ越し（koten hdd-copy）と控え（koten backup）。"""

import os
from pathlib import Path

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


def _pair(tmp_path):
    src, dst = tmp_path / "sd", tmp_path / "hdd"
    make_tree(src)
    dst.mkdir()
    return src, dst


def test_copy_tree_skips_mac_files_and_resumes(tmp_path):
    src, dst = _pair(tmp_path)
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
    # augment か update が動いているあいだは写さない
    monkeypatch.setattr(runlock, "other_processes", lambda *a, **k: [(9, "koten augment")])
    r = CliRunner().invoke(app, ["hdd-copy", "--from", str(src), *st])
    assert r.exit_code == 2 and "Ctrl-C" in r.output and not hdd.exists()
    monkeypatch.setattr(runlock, "other_processes", lambda *a, **k: [])
    r = CliRunner().invoke(app, ["hdd-copy", "--from", str(src), *st])
    assert r.exit_code == 0, r.output
    assert (hdd / "chunks" / "aozora.parquet").exists() and "確かめました" in r.output
    # 写し終えたあとは、update が HDD で動いていても、もう写さないので止めない
    monkeypatch.setattr(runlock, "other_processes", lambda *a, **k: [(9, "koten update")])
    r = CliRunner().invoke(app, ["hdd-copy", "--from", str(src), *st])
    assert r.exit_code == 0 and "写し終えています" in r.output
    # HDD がつながっていなければ止める
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


def test_newer_file_on_hdd_is_not_overwritten(tmp_path):
    """HDD で動かし始めたあとに写し直しても、HDD のほうが新しいファイルは古い中身で戻さない。"""
    src, dst = _pair(tmp_path)
    copy_tree(src, dst)
    f = dst / "chunks" / "aozora.parquet"
    f.write_bytes(b"new")
    t = (src / "chunks" / "aozora.parquet").stat().st_mtime + 100
    os.utime(f, (t, t))
    r = copy_tree(src, dst)
    assert r.kept == ["chunks/aozora.parquet"] and r.problems == [] and r.copied == 0
    assert f.read_bytes() == b"new"


def _leftovers(root):
    return [p for p in root.rglob("*") if p.name.endswith(".copying")]


def test_copy_error_is_reported_and_others_continue(tmp_path, monkeypatch):
    from koten import hddcopy

    src, dst = _pair(tmp_path)
    real = hddcopy._copy_data

    def broken(s, tmp):
        if s.name == "aozora.parquet":
            tmp.write_bytes(b"ha")
            raise PermissionError(13, "Permission denied")
        return real(s, tmp)

    monkeypatch.setattr(hddcopy, "_copy_data", broken)
    r = copy_tree(src, dst)
    assert r.copied == 1 and r.problems == ["写せない：chunks/aozora.parquet（Permission denied）"]
    assert _leftovers(dst) == []


def test_unreadable_entry_and_vanished_source_do_not_crash(tmp_path, monkeypatch):
    """傷んだ SD カードの読めない項目や、途中で抜けた SD カードでも止まらずに一覧を出す。"""
    import errno
    import shutil

    from koten import hddcopy

    src, dst = _pair(tmp_path)
    (src / "chunks" / "bad.bin").write_bytes(b"b")
    real_lstat = Path.lstat

    def lstat(self):
        if self.name == "bad.bin":
            raise OSError(errno.EIO, "Input/output error")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", lstat)
    r = copy_tree(src, dst)
    assert r.copied == 2 and r.problems == ["読めない：chunks/bad.bin（Input/output error）"]
    monkeypatch.setattr(Path, "lstat", real_lstat)
    # 写したあとで元が消えても（SD カードが抜けた）、確かめるところで止まらない
    listing = hddcopy.list_files(src)
    real_copy = hddcopy._copy_one

    def then_pull_card(s, target, result):
        size = real_copy(s, target, result)
        s.unlink()
        return size

    monkeypatch.setattr(hddcopy, "_copy_one", then_pull_card)
    shutil.rmtree(dst)
    dst.mkdir()
    r = copy_tree(src, dst, listing=listing)
    assert r.copied == 3 and all(p.startswith("確かめられない") for p in r.problems)


def test_hdd_gone_midway_does_not_recreate_folders(tmp_path, monkeypatch):
    """途中で HDD が外れたら、上のフォルダをたどって作り直さない（Mac 本体に書かない）。"""
    import shutil

    from koten import hddcopy

    src = tmp_path / "sd"
    for d, name in (("a", "x"), ("b", "y")):
        (src / d).mkdir(parents=True)
        (src / d / name).write_bytes(b"1")
    (tmp_path / "top.txt").write_bytes(b"t")
    real = hddcopy._copy_data

    def vanish(s, tmp):
        ok = real(s, tmp)
        shutil.rmtree(tmp_path / "hdd")
        return ok

    monkeypatch.setattr(hddcopy, "_copy_data", vanish)
    # HDD のいちばん上へ写すときも（--whole-card のほかのもの）、いちばん上を作り直さない
    (tmp_path / "hdd").mkdir()
    r = copy_tree(src, tmp_path / "hdd")
    assert r.copied == 0 and "HDD が外れていないか" in r.problems[-1]
    assert not (tmp_path / "hdd").exists()
    r = copy_tree(src, tmp_path / "hdd")
    assert r.problems == [
        f"写す先（{tmp_path / 'hdd'}）がありません。HDD が外れていないか確かめてください"
    ]
    assert not (tmp_path / "hdd").exists()


def test_mac_copies_tags_or_falls_back_to_contents(tmp_path, monkeypatch):
    import shutil

    from koten import hddcopy

    src, dst = _pair(tmp_path)
    monkeypatch.setattr(hddcopy, "MAC", True)

    def cp(s, d):  # Mac の cp：付帯情報を写せないと 1 を返す（中身は写っていることがある）
        shutil.copyfile(s, d)
        d.chmod(0o444)
        return 1 if s.name == "aozora.parquet" else 0

    monkeypatch.setattr(hddcopy, "_cp", cp)
    folders = []
    monkeypatch.setattr(hddcopy, "_copy_dir_meta", lambda a, b: folders.append(b.name) or True)
    r = copy_tree(src, dst)
    assert (r.copied, r.meta_lost, r.problems) == (2, 1, [])
    assert (dst / "chunks" / "aozora.parquet").read_bytes() == b"x" * 1000
    assert sorted(folders) == ["augment", "chunks", "m", "p"]  # フォルダのタグも写す


def test_links_long_names_and_user_copying_files(tmp_path):
    """リンクはリンクのまま写す。長い名前（250字）と、利用者の .copying のファイルも写す。"""
    from koten.hddcopy import list_files, skip_card

    card, vol = tmp_path / "card", tmp_path / "vol"
    (card / "App.app" / "Versions" / "A").mkdir(parents=True)
    (card / "App.app" / "Versions" / "A" / "X").write_bytes(b"x")
    (card / "App.app" / "Versions" / "Current").symlink_to("A")
    (card / "App.app" / "X").symlink_to("Versions/Current/X")
    (card / "broken").symlink_to("nowhere")
    long = "あ" * 80 + "a" * 10  # 250 バイト（UTF-8）
    (card / long).write_bytes(b"l")
    (card / "keep.copying").write_bytes(b"k")
    vol.mkdir()
    listing = list_files(card, skip_card(Path("koten")))
    r = copy_tree(card, vol, listing=listing)
    assert r.problems == [] and r.copied == 6
    assert os.readlink(vol / "App.app" / "Versions" / "Current") == "A"
    assert os.readlink(vol / "App.app" / "X") == "Versions/Current/X"
    assert os.readlink(vol / "broken") == "nowhere"
    assert (vol / long).read_bytes() == b"l" and (vol / "keep.copying").exists()
    r = copy_tree(card, vol, listing=list_files(card, skip_card(Path("koten"))))
    assert (r.copied, r.skipped, r.problems) == (0, 6, [])


def test_skip_card_rules():
    import unicodedata

    from koten.hddcopy import skip_card

    skip = skip_card(Path("データ"), Path("koten-backup"))
    # 別に写す koten のフォルダ（Mac が濁点を分けて書いた名前でも同じとみなす）
    assert skip(Path(unicodedata.normalize("NFD", "データ")) / "chunks")
    assert skip(Path("koten-backup") / "hdd")
    assert skip(Path(".Trashes") / "501" / "old.txt")
    assert skip(Path(".Spotlight-V100"))
    assert skip(Path("DCIM") / "._a.jpg") and skip(Path("DCIM") / ".DS_Store")
    assert skip(Path("DCIM") / ".koten-0123456789ab.copying")  # hdd-copy の書きかけ
    assert not skip(Path("DCIM") / "a.jpg")
    assert not skip(Path("Downloads") / "movie.part")  # 利用者のファイルは写す
    assert not skip(Path("Docs") / ".Trashes")  # 直下のものだけが管理用


def _card(tmp_path, monkeypatch):
    """SD カード（tmp/SDカード）と HDD（tmp/HD-NRLD-A）。tmp を /Volumes とみなす。"""

    from koten import hddcopy

    card, vol = tmp_path / "SDカード", tmp_path / "HD-NRLD-A"
    make_tree(card / "古典×悩み")
    (card / "DCIM" / "100").mkdir(parents=True)
    (card / "DCIM" / "100" / "a.jpg").write_bytes(b"jpg")
    (card / "Docs").mkdir()
    (card / "Docs" / "report.part").write_bytes(b"p")
    (card / "empty").mkdir()
    (card / ".Trashes" / "501").mkdir(parents=True)
    (card / ".Trashes" / "501" / "old.txt").write_bytes(b"t")
    (card / "._DCIM").write_bytes(b"apple")
    vol.mkdir()

    def mount_root(path):
        p = Path(path).resolve()
        for root in (card, vol):
            if p == root or root in p.parents:
                return root
        return Path("/")

    monkeypatch.setattr(hddcopy, "VOLUMES", tmp_path)
    monkeypatch.setattr(hddcopy, "mount_root", mount_root)
    monkeypatch.setattr(runlock, "other_processes", lambda *a, **k: [])
    return card, vol, _settings(tmp_path, vol / "koten")


def test_hdd_copy_whole_card(tmp_path, monkeypatch):
    from koten.hddcopy import BACKUP_MARK, COPIED_MARK, COPYING_MARK

    card, vol, st = _card(tmp_path, monkeypatch)
    (card / "koten-backup" / "hdd").mkdir(parents=True)  # koten backup の控え
    (card / "koten-backup" / BACKUP_MARK).write_text("x", "utf-8")
    args = ["hdd-copy", "--from", str(card / "古典×悩み"), "--whole-card", *st]
    r = CliRunner().invoke(app, args)
    assert r.exit_code == 0, r.output
    # koten のフォルダは hdd_root（koten）へ、ほかのものは HDD のいちばん上へ同じ並びで
    assert (vol / "koten" / "chunks" / "aozora.parquet").read_bytes() == b"x" * 1000
    assert (vol / "DCIM" / "100" / "a.jpg").read_bytes() == b"jpg"
    assert (vol / "Docs" / "report.part").exists() and (vol / "empty").is_dir()
    for absent in ("古典×悩み", ".Trashes", "._DCIM", "koten-backup"):
        assert not (vol / absent).exists()
    assert (vol / "koten" / COPIED_MARK).exists() and not (vol / "koten" / COPYING_MARK).exists()
    assert r.output.index("数えています") < r.output.index("koten のフォルダ：")
    assert "DCIM、Docs、empty" in r.output and "ゴミ箱" in r.output and "控え" in r.output
    assert r.output.rstrip().splitlines()[-2] == "前の置き場所（SD カード）はそのまま残します"
    # 写し終えたあとに打つと、写さずに HDD にあるかだけを確かめる（HDD で消したり直したりした
    # ものを、SD カードの古いもので戻さない）
    (card / "古典×悩み" / "chunks" / "aozora.parquet").write_bytes(b"old")
    (vol / "DCIM" / "100" / "a.jpg").unlink()  # HDD で消した
    r = CliRunner().invoke(app, args)
    assert r.exit_code == 1 and "HDD に無い：DCIM/100/a.jpg" in r.output, r.output
    assert "そのまま残します" not in r.output and "Claude に貼って" in r.output
    assert not (vol / "DCIM" / "100" / "a.jpg").exists()
    assert (vol / "koten" / "chunks" / "aozora.parquet").read_bytes() == b"x" * 1000
    (card / "DCIM" / "100" / "a.jpg").unlink()
    (vol / "koten" / "chunks" / "aozora.parquet").unlink()  # HDD の koten から消えた
    r = CliRunner().invoke(app, args)
    assert r.exit_code == 1 and "HDD に無い：chunks/aozora.parquet" in r.output, r.output
    (card / "古典×悩み" / "chunks" / "aozora.parquet").unlink()
    r = CliRunner().invoke(app, args)
    assert r.exit_code == 0, r.output
    assert "写し終えています" in r.output and "全部 HDD にもある" in r.output
    assert "そのまま残します" in r.output


def test_hdd_copy_refuses_wrong_from(tmp_path, monkeypatch):
    """--from に SD カードのいちばん上や、koten のデータでないフォルダを渡したら写さない。"""
    card, vol, st = _card(tmp_path, monkeypatch)

    def run(src, *extra):
        return CliRunner().invoke(app, ["hdd-copy", "--from", str(src), *extra, *st])

    for src, extra in ((card, ()), (card, ("--whole-card",)), (card / "DCIM", ("--whole-card",))):
        r = run(src, *extra)
        assert r.exit_code == 2 and "koten のデータのフォルダ" in r.output, (src, extra)
        assert list(vol.iterdir()) == []


def test_hdd_copy_whole_card_refuses(tmp_path, monkeypatch):
    from koten import hddcopy

    card, vol, st = _card(tmp_path, monkeypatch)

    def run(src, *extra):
        return CliRunner().invoke(app, ["hdd-copy", "--from", str(src), *extra, *st])

    # SD カードにも koten という名前のものがあると、HDD の koten のフォルダと重なる
    (card / "koten").mkdir()
    r = run(card / "古典×悩み", "--whole-card")
    assert r.exit_code == 2 and "重なります" in r.output
    (card / "koten").rmdir()
    # Mac 本体の上のフォルダから --whole-card で写さない
    monkeypatch.setattr(
        hddcopy, "mount_root", lambda p: vol if str(p).startswith(str(vol)) else Path("/")
    )
    r = run(card / "古典×悩み", "--whole-card")
    assert r.exit_code == 2 and "Mac 本体" in r.output
    # /Volumes の下に残った、ただのフォルダ（HDD はつながっていない）には写さない
    monkeypatch.setattr(hddcopy, "mount_root", lambda p: tmp_path.parent)
    r = run(card / "古典×悩み")
    assert r.exit_code == 2 and "Mac 本体の中のフォルダ" in r.output and list(vol.iterdir()) == []


def test_hdd_copy_stops_when_hdd_goes_away(tmp_path, monkeypatch):
    """koten のフォルダを写すあいだに HDD が外れたら、ほかのものは写さずに止まる。"""
    import shutil

    from koten import hddcopy

    card, vol, st = _card(tmp_path, monkeypatch)
    real = hddcopy.copy_tree

    def then_unplug(src, dst, **kw):
        r = real(src, dst, **kw)
        shutil.rmtree(vol)
        return r

    monkeypatch.setattr(hddcopy, "copy_tree", then_unplug)
    r = CliRunner().invoke(
        app, ["hdd-copy", "--from", str(card / "古典×悩み"), "--whole-card", *st]
    )
    assert r.exit_code == 1 and "外れました" in r.output and "Traceback" not in r.output
    assert not vol.exists()


def test_hdd_copy_checks_format_and_space(tmp_path, monkeypatch):
    from koten import hddcopy

    card, vol, st = _card(tmp_path, monkeypatch)
    args = ["hdd-copy", "--from", str(card / "古典×悩み"), "--whole-card", *st]
    for fmt in ("ntfs", "ufsd_ntfs", "exfat"):
        monkeypatch.setattr(hddcopy, "volume_format", lambda p, fmt=fmt: fmt)
        r = CliRunner().invoke(app, args)
        assert r.exit_code == 2 and "APFS に消去" in r.output and not (vol / "koten").exists()
    monkeypatch.setattr(hddcopy, "volume_format", lambda p: "apfs")
    monkeypatch.setattr(hddcopy, "free_bytes", lambda p: 10)
    r = CliRunner().invoke(app, args)
    assert r.exit_code == 2 and "空きが足りません" in r.output and not (vol / "koten").exists()


def test_hdd_copy_runs_once_at_a_time(tmp_path, monkeypatch):
    from koten.runlock import hold, lock_path

    card, vol, st = _card(tmp_path, monkeypatch)
    with hold(lock_path(tmp_path / "ssd", "hdd-copy")):
        r = CliRunner().invoke(app, ["hdd-copy", "--from", str(card / "古典×悩み"), *st])
    assert r.exit_code == 2 and "ほかの窓で hdd-copy" in r.output and list(vol.iterdir()) == []


def test_doctor_warns_unsuited_format(tmp_path, monkeypatch):
    from koten import hddcopy
    from koten.config import load_settings
    from koten.doctor import WARN, check_hdd

    hdd = tmp_path / "HD-NRLD-A" / "koten"
    hdd.mkdir(parents=True)
    s = load_settings(_settings(tmp_path, hdd)[1])
    monkeypatch.setattr(hddcopy, "volume_format", lambda p: "exfat")
    c = check_hdd(s)
    assert c.status == WARN and "exFAT" in c.detail


def test_backup_stops_before_filling_the_card(tmp_path, monkeypatch):
    """控えの置き場所の空きが足りなければ写す前に止まる。控えのほうが新しいものは知らせる。"""
    from koten import hddcopy

    hdd = tmp_path / "hdd" / "koten"
    make_tree(hdd)
    st = _settings(tmp_path, hdd)
    (tmp_path / "SDカード").mkdir()
    out = tmp_path / "SDカード" / "koten-backup"
    monkeypatch.setattr(hddcopy, "free_bytes", lambda p: 10)
    r = CliRunner().invoke(app, ["backup", "--to", str(out), *st])
    assert r.exit_code == 2 and "空きが足りません" in r.output and not out.exists()
    monkeypatch.undo()
    r = CliRunner().invoke(app, ["backup", "--to", str(out), *st])
    assert r.exit_code == 0, r.output
    assert (out / hddcopy.BACKUP_MARK).exists()
    f = out / "hdd" / "augment" / "m" / "p" / "done-00001.parquet"
    f.write_bytes(b"newer")
    t = (hdd / "augment" / "m" / "p" / "done-00001.parquet").stat().st_mtime + 100
    os.utime(f, (t, t))
    r = CliRunner().invoke(app, ["backup", "--to", str(out), *st])
    assert r.exit_code == 1 and "控えのほうが新しい" in r.output


def test_rerun_keeps_folder_tags_made_on_the_hdd(tmp_path, monkeypatch):
    """2回目に打っても、前からある HDD のフォルダのタグなどを SD カードの古いもので戻さない。"""
    import shutil

    from koten import hddcopy

    src, dst = _pair(tmp_path)
    monkeypatch.setattr(hddcopy, "MAC", True)

    def cp(s, d):
        shutil.copyfile(s, d)
        return 0

    monkeypatch.setattr(hddcopy, "_cp", cp)
    calls = []
    monkeypatch.setattr(hddcopy, "_copy_dir_meta", lambda a, b: calls.append(b.name) or True)
    copy_tree(src, dst)
    calls.clear()
    copy_tree(src, dst)
    assert calls == []
    (src / "chunks" / "new").mkdir()
    copy_tree(src, dst)
    assert calls == ["new"]


def test_folder_dates_follow_the_card(tmp_path):
    """中に写したフォルダの更新日時を、SD カードのフォルダの日付に戻す。"""
    src, dst = _pair(tmp_path)
    for d in (src / "augment" / "m" / "p", src / "augment" / "m", src / "chunks"):
        os.utime(d, (1_600_000_000, 1_600_000_000))
    copy_tree(src, dst)
    for rel in ("augment/m/p", "augment/m", "chunks"):
        assert int((dst / rel).stat().st_mtime) == 1_600_000_000, rel


def test_from_needs_koten_layout(tmp_path, monkeypatch):
    """RAW フォルダ1つだけでは koten のデータとみなさない（exFAT は大文字小文字を区別しない）。"""
    card, vol, st = _card(tmp_path, monkeypatch)
    (card / "DCIM" / "raw").mkdir()
    r = CliRunner().invoke(app, ["hdd-copy", "--from", str(card / "DCIM"), *st])
    assert r.exit_code == 2 and "koten のデータのフォルダ" in r.output and list(vol.iterdir()) == []


def test_backup_refuses_same_drive_and_runs_once(tmp_path, monkeypatch):
    from koten import hddcopy
    from koten.runlock import hold, lock_path

    card, vol, st = _card(tmp_path, monkeypatch)
    (vol / "koten" / "chunks").mkdir(parents=True)
    r = CliRunner().invoke(app, ["backup", "--to", str(vol / "koten-backup"), *st])
    assert r.exit_code == 2 and "別のドライブ" in r.output
    with hold(lock_path(tmp_path / "ssd", "backup")):
        r = CliRunner().invoke(app, ["backup", "--to", str(card / "koten-backup"), *st])
    assert r.exit_code == 2 and "ほかの窓で backup" in r.output
    r = CliRunner().invoke(app, ["backup", "--to", str(card / "koten-backup"), *st])
    assert r.exit_code == 0, r.output
    assert (card / "koten-backup" / hddcopy.BACKUP_MARK).exists()


def test_check_mode_never_writes(tmp_path, monkeypatch):
    """--check は HDD に何も書かない。写しきれなかった回のあとでも、HDD で消したものを戻さない。"""
    from koten import hddcopy

    card, vol, st = _card(tmp_path, monkeypatch)
    args = ["hdd-copy", "--from", str(card / "古典×悩み"), "--whole-card"]
    # koten のフォルダを写し終える前は、確かめない
    r = CliRunner().invoke(app, [*args, "--check", *st])
    assert r.exit_code == 2 and "まだ写し終えていません" in r.output and list(vol.iterdir()) == []
    # 1回目：SD カードのほかのものの1つが読めず、写し終えた印が付かない
    real = hddcopy._copy_data

    def broken(s, tmp):
        if s.name == "report.part":
            raise OSError(5, "Input/output error")
        return real(s, tmp)

    monkeypatch.setattr(hddcopy, "_copy_data", broken)
    r = CliRunner().invoke(app, [*args, *st])
    assert r.exit_code == 1 and not (vol / hddcopy.CARD_COPIED_MARK).exists()
    monkeypatch.setattr(hddcopy, "_copy_data", real)
    (vol / "DCIM" / "100" / "a.jpg").unlink()  # 使っているあいだに HDD で消した

    def no_write(folder):
        raise AssertionError("--check で書き込みを試した")

    monkeypatch.setattr(hddcopy, "can_write", no_write)
    before = sorted(str(p) for p in vol.rglob("*"))
    r = CliRunner().invoke(app, [*args, "--check", *st])
    assert r.exit_code == 1 and "HDD に無い：DCIM/100/a.jpg" in r.output
    assert "HDD に無い：Docs/report.part" in r.output and "写し終えた印がありません" in r.output
    assert sorted(str(p) for p in vol.rglob("*")) == before  # 何も書いていない
    (card / "DCIM" / "100" / "a.jpg").unlink()
    (card / "Docs" / "report.part").unlink()
    r = CliRunner().invoke(app, [*args, "--check", *st])
    assert r.exit_code == 0 and "そのまま残します" in r.output, r.output


def test_folder_dates_fixed_after_an_interrupted_run(tmp_path, monkeypatch):
    from koten import hddcopy

    src, dst = _pair(tmp_path)
    for d in (src / "augment" / "m" / "p", src / "augment" / "m", src / "chunks"):
        os.utime(d, (1_600_000_000, 1_600_000_000))
    real = hddcopy._copy_one

    def stop_at_last(s, target, result):
        if s.name == "done-00001.parquet":
            raise KeyboardInterrupt
        return real(s, target, result)

    monkeypatch.setattr(hddcopy, "_copy_one", stop_at_last)
    try:
        copy_tree(src, dst)
    except KeyboardInterrupt:
        pass
    monkeypatch.setattr(hddcopy, "_copy_one", real)
    copy_tree(src, dst)
    for rel in ("augment/m/p", "augment/m", "chunks"):
        assert int((dst / rel).stat().st_mtime) == 1_600_000_000, rel


def test_backup_refuses_the_hdd_root(tmp_path, monkeypatch):
    card, vol, st = _card(tmp_path, monkeypatch)
    (vol / "koten" / "chunks").mkdir(parents=True)
    r = CliRunner().invoke(app, ["backup", "--to", str(vol), *st])
    assert r.exit_code == 2 and "別のドライブ" in r.output


def test_backup_includes_photos_and_bgm(tmp_path):
    """自分の写真と BGM は作り直せないので控えに入れる（D-032）。"""
    hdd = tmp_path / "hdd" / "koten"
    make_tree(hdd)
    (hdd / "media" / "bg").mkdir(parents=True)
    (hdd / "media" / "bg" / "sky.jpg").write_bytes(b"sky")
    (hdd / "media" / "bgm").mkdir()
    (hdd / "media" / "bgm" / "calm.mp3").write_bytes(b"mp3")
    p = tmp_path / "settings.toml"
    p.write_text(
        f'[paths]\nhdd_root = "{hdd}"\nssd_root = "{tmp_path / "ssd"}"\n'
        f'photos_dir = "{hdd / "media" / "bg"}"\nbgm_dir = "{hdd / "media" / "bgm"}"\n',
        "utf-8",
    )
    (tmp_path / "SDカード").mkdir()
    out = tmp_path / "SDカード" / "koten-backup"
    r = CliRunner().invoke(app, ["backup", "--to", str(out), "--settings", str(p)])
    assert r.exit_code == 0, r.output
    assert (out / "media" / "photos" / "sky.jpg").read_bytes() == b"sky"
    assert (out / "media" / "bgm" / "calm.mp3").read_bytes() == b"mp3"
    assert "写真：写した 1 ファイル" in r.output and "BGM：写した 1 ファイル" in r.output
