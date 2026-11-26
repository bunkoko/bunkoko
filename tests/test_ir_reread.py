"""保護つきの PDF を読む・空の本文を原本から読み直す（cfdbot/agents/collector.py・./cfd agents reread）。

2026-11-26 の棚卸しで、取ってある本文の 15%（9,422 件）が空だと分かった。編集を止める保護（AES の暗号）つきの PDF は、
読む部品（cryptography）が無いと読めず、道具はそれを黙って空の本文として残していた（docs/agents.md 10.7）。
"""

import io
import os
from argparse import Namespace
from datetime import datetime

import pandas as pd
import pytest

from cfdbot.agents import collector as C
from tests.test_agents import DISC3, _pdf, _xbrl_zip
from tests.test_text_history import hdd, load_script, run, written

WHEN = datetime(2026, 11, 20, 8, 5)
NO_TEXT = "文字が入っていない（画像だけの PDF など）"


def protected(text, algorithm="AES-128", user=""):
    """編集を止める保護つきの PDF（開けるパスワードは空 ＝ だれでも読める。上場会社の書類によくある形）。"""
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter()
    w.append(PdfReader(io.BytesIO(_pdf(text))))
    w.encrypt(user_password=user, owner_password="owner", algorithm=algorithm)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_text_of_a_protected_pdf_is_read():
    assert C.crypto_ready()                                               # 保護を解く部品は、入れておく決まり（pyproject.toml）
    for algorithm in ("AES-128", "AES-256", "RC4-128"):
        assert C.pdf_text(protected("hello", algorithm)) == "hello"


def test_reading_tells_why_there_is_no_text():
    assert C.pdf_text_why(_pdf("hello")) == ("hello", "")
    assert C.pdf_text_why(_pdf("")) == ("", NO_TEXT)
    assert C.pdf_text_why(protected("x", user="secret")) == ("", "開くのにパスワードが要る")
    text, why = C.pdf_text_why(b"%PDF-1.4 broken")
    assert text == "" and why.startswith("読めない作り（")


def test_a_missing_part_is_never_mistaken_for_an_empty_document(monkeypatch):
    """読む部品が無いときは、空の本文を返さずに、はっきり止まる（黙って空の本文をためない）。"""
    from pypdf.errors import DependencyError

    def broken(blob):
        raise DependencyError("cryptography>=3.1 is required for AES algorithm")

    monkeypatch.setattr(C, "_open_pdf", broken)
    with pytest.raises(C.PdfPartMissing):
        C.pdf_text(_pdf("hello"))
    with pytest.raises(C.PdfPartMissing):
        C.pdf_text_why(_pdf("hello"))


def test_daily_collection_takes_no_text_while_the_part_is_missing(tmp_path, monkeypatch):
    """部品が無い朝は、本文を取りに行かない（取っても空になる）。数字は取る。部品が戻れば、次の朝に本文も取る。"""
    disc = pd.DataFrame(DISC3, columns=C.COLUMNS)
    logs, calls = [], []

    def get(url):
        calls.append(url.rsplit("/", 1)[1])
        return _xbrl_zip() if url.endswith(".zip") else _pdf("a")

    monkeypatch.setattr(C, "crypto_ready", lambda: False)
    got = C.collect_docs(disc, tmp_path, interval=0, get=get, log=logs.append)
    assert calls == ["x1.zip", "x3.zip"] and got["xbrl"] == 2 and got["text"] == 0 and got["no_pdf_part"] == 1
    assert sum(s.strip().startswith("⚠") and "部品" in s and "./cfd update" in s for s in logs) == 1
    monkeypatch.setattr(C, "crypto_ready", lambda: True)
    again = C.collect_docs(disc, tmp_path, interval=0, get=get, log=logs.append)
    assert again["text"] == 3 and "no_pdf_part" not in again


def test_history_does_not_start_while_the_part_is_missing(tmp_path, monkeypatch):
    root, _ = hdd(tmp_path)
    calls = []
    monkeypatch.setattr(C, "crypto_ready", lambda: False)
    got = run(root, lambda url: calls.append(url) or _pdf("a"))
    assert got["stopped"] == "no_pdf_part" and calls == [] and got["left"] == 3


def empties(tmp_path):
    """空の本文が 3 つ: e1 は保護つきの原本あり・e2 は文字の無い原本あり・e3 は原本なし。ok は文字のある本文（原本もある）。"""
    root, disk = hdd(tmp_path)
    for name, text in (("e1", ""), ("e2", ""), ("e3", ""), ("ok", "もとの本文")):
        written(disk / "ir_text" / "2026-11" / f"{name}.txt", WHEN, text)
    (disk / "ir_pdf" / "2026-11").mkdir(parents=True)
    for name, blob in (("e1", protected("now readable")), ("e2", _pdf("")), ("ok", _pdf("different"))):
        (disk / "ir_pdf" / "2026-11" / f"{name}.pdf").write_bytes(blob)
    return root, disk / "ir_text" / "2026-11"


def test_reread_fills_empty_texts_from_the_kept_originals(tmp_path):
    root, texts = empties(tmp_path)
    got = C.reread_empty(root, log=lambda s: None)
    assert (got["empty"], got["no_pdf"], got["tried"], got["fixed"], got["still"]) == (3, 1, 2, 1, 1) and got["why"] == {NO_TEXT: 1}
    assert (texts / "e1.txt").read_text(encoding="utf-8") == "now readable"
    assert (texts / "ok.txt").read_text(encoding="utf-8") == "もとの本文"            # 文字のある本文には触らない
    assert (texts / "e2.txt").read_text(encoding="utf-8") == "" and not list(texts.glob("*.part"))
    again = C.reread_empty(root, log=lambda s: None)
    assert (again["empty"], again["fixed"]) == (2, 0)                                 # 2 回目に残るのは、読み直せなかった分だけ


def test_reread_keeps_the_time_each_text_was_written(tmp_path):
    """断られた後の日数の数え（clean_days）は、本文のファイルが書かれた日を見る。読み直しを「書類を受け取れた朝」の証拠にしない。"""
    root, texts = empties(tmp_path)
    C.note_refused(root, 503, datetime(2026, 11, 25, 20, 15))
    C.reread_empty(root, log=lambda s: None)
    assert datetime.fromtimestamp(os.stat(texts / "e1.txt").st_mtime) == WHEN
    assert C.clean_days(root, datetime(2027, 1, 1)) == []


def test_reread_can_only_look(tmp_path):
    """--check: 読み直せば取れる数と、取れない理由を数えるだけ。何も書かない。"""
    root, texts = empties(tmp_path)
    got = C.reread_empty(root, write=False, log=lambda s: None)
    assert (got["empty"], got["fixed"], got["still"]) == (3, 1, 1)
    assert (texts / "e1.txt").read_text(encoding="utf-8") == ""


def test_reread_takes_a_spread_sample_when_asked(tmp_path):
    root, disk = hdd(tmp_path)
    for k in range(10):
        month = f"2025-{k + 1:02d}"
        written(disk / "ir_text" / month / f"s{k}.txt", WHEN, "")
        (disk / "ir_pdf" / month).mkdir(parents=True)
        (disk / "ir_pdf" / month / f"s{k}.pdf").write_bytes(_pdf(f"t{k}"))
    got = C.reread_empty(root, write=False, limit=3, log=lambda s: None)
    assert got["empty"] == 10 and got["tried"] == 3 and got["fixed"] == 3            # 全部を数えたうえで、一部だけ読んでみる


def test_reread_needs_the_part_and_the_disk(tmp_path, monkeypatch):
    root, texts = empties(tmp_path)
    monkeypatch.setattr(C, "crypto_ready", lambda: False)
    assert C.reread_empty(root, log=lambda s: None)["stopped"] == "no_pdf_part" and (texts / "e1.txt").read_text(encoding="utf-8") == ""
    monkeypatch.setattr(C, "crypto_ready", lambda: True)
    gone = tmp_path / "gone"
    gone.mkdir()
    (gone / "text").symlink_to(tmp_path / "nowhere")
    assert C.reread_empty(gone, log=lambda s: None)["stopped"] == "no_text_place"


def test_tool_rereads_and_tells_what_it_did(tmp_path, capsys):
    root, texts = empties(tmp_path)                                                   # root は data/agents/ir に当たる
    m = load_script("agents_reread")
    m.cmd_reread(Namespace(root=str(root.parent), check=True))
    out = capsys.readouterr().out
    assert "見るだけ" in out and "空の本文 3 件" in out and "原本が無い 1 件" in out and "文字が取れた 1 件" in out
    assert (texts / "e1.txt").read_text(encoding="utf-8") == ""
    m.cmd_reread(Namespace(root=str(root.parent), check=False))
    out = capsys.readouterr().out
    assert "書き直した 1 件" in out and NO_TEXT + " 1 件" in out and "サイトには取りに行っていない" in out
    assert (texts / "e1.txt").read_text(encoding="utf-8") == "now readable"


def test_tool_stops_when_the_part_is_missing(tmp_path, monkeypatch):
    root, _ = empties(tmp_path)
    monkeypatch.setattr(C, "crypto_ready", lambda: False)
    with pytest.raises(SystemExit) as stop:
        load_script("agents_reread").cmd_reread(Namespace(root=str(root.parent), check=False))
    assert "部品" in str(stop.value) and "./cfd update" in str(stop.value)
