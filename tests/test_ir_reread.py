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


# --------------------------------------------------------------------------- 古い日本語の文字の作り（2026-11-26 の読み直しの記録に 38 行出た）
def _build(objs):
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs):
        offs.append(len(out))
        out += f"{i + 1} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def old_japanese_pdf(text, cmap="90msp-RKSJ-H", codec="cp932"):
    """文字を、決まった表（cmap。たとえば Shift-JIS）の番号で書いた PDF。日本の会社の古い作りの書類にある形。"""
    stream = b"BT /F1 12 Tf 72 720 Td <" + text.encode(codec).hex().encode() + b"> Tj ET"
    return _build([
        b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type0 /BaseFont /MS-PMincho /Encoding /" + cmap.encode() + b" /DescendantFonts [6 0 R] >>",
        (b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /MS-PMincho /CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 2 >>"
         b" /FontDescriptor 7 0 R /DW 1000 >>"),
        (b"<< /Type /FontDescriptor /FontName /MS-PMincho /Flags 6 /FontBBox [0 -137 1000 859] /ItalicAngle 0 /Ascent 859 /Descent -137"
         b" /CapHeight 859 /StemV 80 >>")])


def test_old_japanese_encodings_are_read_as_japanese():
    """読む部品が知らない日本語の文字の作り（90msp-RKSJ-H など）を、道具が教える。知らないままだと、でたらめな漢字に化ける。"""
    text = "業績予想の修正に関するお知らせ"
    for cmap in ("90msp-RKSJ-H", "90msp-RKSJ-V", "90pv-RKSJ-H", "83pv-RKSJ-H", "Add-RKSJ-H", "Ext-RKSJ-H", "90ms-RKSJ-H"):
        assert C.pdf_text(old_japanese_pdf(text, cmap)) == text, cmap
    assert C.pdf_text(old_japanese_pdf(text, "EUC-H", "euc_jp")) == text
    assert C.pdf_text(old_japanese_pdf(text, "UniJIS2004-UTF16-H", "utf-16-be")) == text


def test_an_unknown_encoding_is_not_kept_as_garbled_text():
    """知らない文字の作りは、でたらめな文字に化ける。化けた本文は残さず、空にして理由を言う（原本から、後で読み直せる）。"""
    assert C.pdf_text_why(old_japanese_pdf("業績予想の修正", "Some-Unknown-H")) == ("", "知らない文字の作り（/Some-Unknown-H）")
    assert C.pdf_text_why(_pdf("hello")) == ("hello", "")                              # 次の書類に、前の書類の理由を持ち越さない


def test_reread_all_rewrites_what_reads_differently_now(tmp_path):
    """読み方を直したら、原本のある本文を全部読み直す。変わったものだけ書き直す（書かれた時刻は元のまま）。"""
    root, disk = hdd(tmp_path)
    texts, raw = disk / "ir_text" / "2026-11", disk / "ir_pdf" / "2026-11"
    raw.mkdir(parents=True)
    for name, old, blob in (("g1", "识郑靜酺", old_japanese_pdf("業績予想")),                       # 化けて残っていた。今は読める
                            ("g2", "苌轃邳", old_japanese_pdf("の修正", "Some-Unknown-H")),          # 今も読めない作り。化けた本文は空にする
                            ("s1", "same", _pdf("same")),                                            # 変わらない
                            ("k1", "前に読めた本文", b"%PDF-1.4 broken"),                            # 今は読めないが、前の本文は残す
                            ("e1", "", protected("now readable"))):                                  # 空だった。読める
        written(texts / f"{name}.txt", WHEN, old)
        (raw / f"{name}.pdf").write_bytes(blob)
    written(texts / "n1.txt", WHEN, "原本なし")
    got = C.reread_all(root, log=lambda s: None)
    assert (got["texts"], got["no_pdf"], got["tried"]) == (6, 1, 5)
    assert (got["changed"], got["blanked"], got["same"], got["kept"]) == (2, 1, 1, 1)
    assert {name: (texts / f"{name}.txt").read_text(encoding="utf-8") for name in ("g1", "g2", "s1", "k1", "e1", "n1")} == {
        "g1": "業績予想", "g2": "", "s1": "same", "k1": "前に読めた本文", "e1": "now readable", "n1": "原本なし"}
    assert got["why"]["知らない文字の作り（/Some-Unknown-H）"] == 1 and sum(got["why"].values()) == 2
    assert all(datetime.fromtimestamp(os.stat(p).st_mtime) == WHEN for p in texts.glob("*.txt"))
    again = C.reread_all(root, log=lambda s: None)
    assert (again["changed"], again["blanked"], again["same"], again["kept"]) == (0, 0, 4, 1)      # 2 回目は、もう変わらない（g2 は空のまま）


def test_reread_all_can_only_look(tmp_path):
    root, disk = hdd(tmp_path)
    (disk / "ir_pdf" / "2026-11").mkdir(parents=True)
    path = written(disk / "ir_text" / "2026-11" / "g1.txt", WHEN, "识郑靜酺")
    (disk / "ir_pdf" / "2026-11" / "g1.pdf").write_bytes(old_japanese_pdf("業績予想"))
    got = C.reread_all(root, write=False, log=lambda s: None)
    assert got["changed"] == 1 and path.read_text(encoding="utf-8") == "识郑靜酺"


def test_tool_rereads_everything_when_asked(tmp_path, capsys):
    root, disk = hdd(tmp_path)
    (disk / "ir_pdf" / "2026-11").mkdir(parents=True)
    path = written(disk / "ir_text" / "2026-11" / "g1.txt", WHEN, "识郑靜酺")
    (disk / "ir_pdf" / "2026-11" / "g1.pdf").write_bytes(old_japanese_pdf("業績予想"))
    m = load_script("agents_reread_all")
    m.cmd_reread(Namespace(root=str(root.parent), check=True, all=True, refetch=False))
    out = capsys.readouterr().out
    assert "見るだけ" in out and "読み直すと変わる 1 件" in out and path.read_text(encoding="utf-8") == "识郑靜酺"
    m.cmd_reread(Namespace(root=str(root.parent), check=False, all=True, refetch=False))
    out = capsys.readouterr().out
    assert "書き直した 1 件" in out and "pypdf" in out and path.read_text(encoding="utf-8") == "業績予想"


# --------------------------------------------------------------------------- 原本の無い空の本文を取り直しに回す（利用者の決め 2026-11-26）
def test_refetch_drops_only_empty_texts_without_an_original(tmp_path):
    """消すのは「空で、原本も無い」本文だけ。消すと、毎朝の収集（直近の分）か過去分の取得（それより前の分）が、決まりの速さで取り直す。"""
    root, texts = empties(tmp_path)                                                   # e1・e2 は原本あり、e3 は原本なし、ok は文字あり
    assert C.drop_lost_empties(root, write=False) == {"lost": 1, "dropped": 0, "stopped": None} and (texts / "e3.txt").exists()
    assert C.drop_lost_empties(root) == {"lost": 1, "dropped": 1, "stopped": None}
    assert sorted(p.name for p in texts.glob("*.txt")) == ["e1.txt", "e2.txt", "ok.txt"]
    assert C.drop_lost_empties(root)["lost"] == 0


def test_refetch_drops_nothing_when_the_originals_cannot_be_seen(tmp_path):
    """原本の置き場所が見えないと、どの空の本文も「原本なし」に見える。そのときは、何も消さない。"""
    root, disk = hdd(tmp_path)
    path = written(disk / "ir_text" / "2026-11" / "e1.txt", WHEN, "")
    assert C.drop_lost_empties(root) == {"lost": 0, "dropped": 0, "stopped": "no_pdf_place"} and path.exists()


def test_tool_drops_lost_empties_and_tells_who_fetches_them_again(tmp_path, capsys):
    root, texts = empties(tmp_path)
    m = load_script("agents_reread_refetch")
    m.cmd_reread(Namespace(root=str(root.parent), check=True, all=False, refetch=True))
    assert "見るだけ" in capsys.readouterr().out and (texts / "e3.txt").exists()
    m.cmd_reread(Namespace(root=str(root.parent), check=False, all=False, refetch=True))
    out = capsys.readouterr().out
    assert "1 件を消した" in out and "毎朝の収集" in out and "過去分の取得" in out and not (texts / "e3.txt").exists()


# --------------------------------------------------------------------------- 毎朝の収集でも原本を残す（利用者の決め 2026-11-26）
def test_daily_collection_keeps_the_original_when_texts_live_on_the_external_disk(tmp_path):
    """読み方を直したとき、取り直さずに読み直せるように、毎朝の収集でも原本の PDF を HDD に残す。"""
    root, disk = hdd(tmp_path)
    got = C.collect_docs(pd.DataFrame(DISC3, columns=C.COLUMNS), root, interval=0, log=lambda s: None,
                         get=lambda url: _xbrl_zip() if url.endswith(".zip") else _pdf("a"))
    assert got["text"] == 3 and (disk / "ir_pdf" / "2026-10" / "p1.pdf").read_bytes() == _pdf("a")
    assert sorted(p.name for p in (disk / "ir_pdf").rglob("*.pdf")) == ["p1.pdf", "p2.pdf", "p3.pdf"]
    assert (root / "text" / "2026-10" / "p1.txt").read_text(encoding="utf-8") == "a"


def test_daily_collection_keeps_no_original_on_the_internal_disk(tmp_path):
    """本文の置き場所が内蔵のまま（リンクでない）なら、原本は残さない（内蔵をいっぱいにしない）。"""
    got = C.collect_docs(pd.DataFrame(DISC3, columns=C.COLUMNS), tmp_path / "ir", interval=0, log=lambda s: None,
                         get=lambda url: _xbrl_zip() if url.endswith(".zip") else _pdf("a"))
    assert got["text"] == 3 and not list(tmp_path.rglob("*.pdf"))
