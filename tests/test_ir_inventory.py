"""取ってある開示の本文の棚卸し（cfdbot/agents/inventory.py・./cfd agents inventory）。

見るだけ: 何が・いつから・どれだけ読めているかを数える。値段は使わない・判定しない（docs/agents.md 10.6）。
"""

from argparse import Namespace

import pandas as pd
import pytest

from cfdbot.agents import collector as C
from cfdbot.agents import inventory as V
from tests.test_text_history import load_script

ROWS = [("a1", "2019-05-10 15:00:00", "7203.T", "トヨタ", "2019年3月期 決算短信", "", "x1.zip"),
        ("a2", "2021-08-06 15:00:00", "7203.T", "トヨタ", "業績予想の修正に関するお知らせ", "", ""),
        ("a3", "2021-08-20 15:00:00", "1332.T", "ニッスイ", "人事異動のお知らせ", "", ""),
        ("a4", "2021-09-01 15:00:00", "1332.T", "ニッスイ", "2022年3月期 第1四半期決算短信", "", "x4.zip"),
        ("a5", "2026-11-24 15:00:00", "1332.T", "ニッスイ", "お知らせ", "", ""),
        ("a0", "2015-02-01 15:00:00", "7203.T", "トヨタ", "古い開示", "", "")]
WORDS, TABLE = "業績の説明です" * 40, "1 2 3 4 5 6 7 8 9 0 " * 400          # 文章（280 字）と、数字ばかりで 8,000 字に届いたもの


def put(folder, month, name, text):
    path = folder / month / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def docs_table():
    """a1 はサイトに無い・a2 は文章・a3 は空・a4 は数字の表（切れている）・a5 は直近でまだ。zz は一覧に無い本文。"""
    disc = pd.DataFrame(ROWS, columns=C.COLUMNS)
    texts = pd.DataFrame([{"id": "a2", **V.text_stats(WORDS)}, {"id": "a4", **V.text_stats(TABLE)},
                          {"id": "a3", **V.text_stats("")}, {"id": "zz", **V.text_stats("一覧に無い")}])
    return V.build(disc, texts, pdfs={"a2", "a4"}, missing={"a1"}, since="2016-10-01", recent_from="2026-11-18 00:00:00")


def test_text_stats_count_length_cut_japanese_and_digits():
    assert V.text_stats("売上高は 12 億円") == {"chars": 10, "cut": False, "jp": 0.75, "digit": 0.25}      # 割合は、空白を除いた 8 文字のうち
    assert V.text_stats("") == {"chars": 0, "cut": False, "jp": 0.0, "digit": 0.0}
    assert V.text_stats("あ" * C.TEXT_MAX)["cut"] is True                 # 8,000 字ちょうどで残っている ＝ 途中で切れている
    assert V.text_stats("１２３ ABC")["digit"] == 0.5                     # 全角の数字も数字
    assert V.text_stats("カタカナと漢字")["jp"] == 1.0


def test_scan_reads_every_text_once_and_skips_what_is_not_a_text(tmp_path):
    put(tmp_path, "2024-11", "p3.txt", "決算の説明" * 10)
    put(tmp_path, "2021-08", "p2.txt", "")
    put(tmp_path, "2021-08", "._p9.txt", "影")                            # Mac が作る影のファイル
    put(tmp_path, "2021-08", "p8.part", "書きかけ")
    (tmp_path / "memo.txt").write_text("置き場所の直下のファイルは見ない", encoding="utf-8")
    logs = []
    got = V.scan_texts(tmp_path, log=logs.append, every=1).set_index("id")
    assert sorted(got.index) == ["p2", "p3"] and got.loc["p3", "chars"] == 50 and got.loc["p2", "chars"] == 0
    assert len(logs) == 2 and "2 件" in logs[-1]                          # 長くかかるので、途中の数を知らせる
    assert list(V.scan_texts(tmp_path / "none").columns) == ["id", "chars", "cut", "jp", "digit"]       # 置き場所が無くても、空の表


def test_pdf_ids_are_the_documents_whose_original_is_kept(tmp_path):
    put(tmp_path, "2021-08", "a2.pdf", "%PDF")
    put(tmp_path, "2021-08", "._a9.pdf", "影")
    assert V.pdf_ids(tmp_path) == {"a2"} and V.pdf_ids(tmp_path / "none") == set()


def test_build_gives_one_row_per_listed_disclosure_with_the_state_of_its_text():
    d = docs_table().set_index("id")
    assert list(d.index) == ["a1", "a2", "a3", "a4", "a5"]                # 2016-10 より前（a0）と、一覧に無い本文（zz）は入らない
    assert list(d["state"]) == ["サイトに無い", "ある", "ある", "ある", "まだ（直近）"]
    assert d.loc["a2", "kind"].startswith("業績予想の修正") and d.loc["a4", "kind"] == "決算短信" and d.loc["a3", "kind"] == "その他"
    assert bool(d.loc["a2", "pdf"]) and not bool(d.loc["a3", "pdf"]) and bool(d.loc["a4", "cut"]) and not bool(d.loc["a2", "cut"])


def test_year_table_counts_what_is_listed_and_what_can_be_read():
    y = V.by_year(docs_table()).set_index("年")
    assert list(y.index) == ["2019", "2021", "2026"]
    assert y.loc["2021", ["一覧", "本文あり", "サイトに無い", "まだ", "原本あり"]].tolist() == [3, 3, 0, 0, 2]
    assert y.loc["2019", ["一覧", "本文あり", "サイトに無い"]].tolist() == [1, 0, 1]
    assert y.loc["2021", "ほとんど読めていない"] == 1 and y.loc["2021", "8,000 字で切れている"] == 1      # a3 は空、a4 は切れている
    assert y.loc["2021", "長さのまん中"] == 280 and y.loc["2026", "まだ"] == 1


def test_kind_table_tells_which_kinds_are_mostly_number_tables():
    k = V.by_kind(docs_table()).set_index("種類")
    assert k.loc["決算短信", ["一覧", "本文あり", "ほぼ数字の表", "日本語が少ない"]].tolist() == [2, 1, 1, 1]
    assert k.loc["その他", ["一覧", "本文あり", "ほとんど読めていない", "ほぼ数字の表"]].tolist() == [2, 1, 1, 0]      # a3（空）と a5（まだ）
    assert list(k["一覧"]) == sorted(k["一覧"], reverse=True)             # 多い種類から


def many(months, n=10):
    return [(f"{m}-{k:02d}", f"{m}-{k + 1:02d} 15:00:00", "7203.T", "トヨタ", "お知らせ", "", "") for m in months for k in range(n)]


def test_full_since_is_the_first_month_from_which_the_texts_are_complete():
    disc = pd.DataFrame(many([f"2020-{m:02d}" for m in range(1, 7)]), columns=C.COLUMNS)
    have = [i for i in disc["id"] if i[:7] >= "2020-04" and i != "2020-05-03"] + [f"2020-03-{k:02d}" for k in range(5)]
    texts = pd.DataFrame([{"id": i, **V.text_stats(WORDS)} for i in have])
    d = V.build(disc, texts, pdfs=set(), missing={"2020-05-03"}, since="2016-10-01", recent_from="2026-01-01")
    assert V.full_since(d) == "2020-04"                                   # 3 月は半分だけ。5 月の 1 件はサイトに無い（抜けに数えない）
    assert V.full_since(d[d["state"] != "ある"]) is None


def test_company_summary_counts_companies_with_enough_texts(monkeypatch):
    monkeypatch.setattr(V, "ENOUGH", 2)
    assert V.company_summary(docs_table()) == {"listed": 2, "with_text": 2, "median": 1.5, "least": 1, "most": 2, "enough": 1}


def test_report_says_it_only_looks_and_shows_the_tables():
    text = V.report(docs_table(), when="2026-11-25 22:00", place="/Volumes/X/bunkoko/ir_text", orphans=1)
    assert "見るだけ" in text and "値段は使っていない" in text and "判定はしていない" in text
    for head in ("## 1. 全体", "## 2. 年ごと", "## 3. 種類ごと", "## 4. 会社ごと", "## 5. 読み方"):
        assert head in text
    assert "本文がある: 3 件（60%）" in text and "サイトに無い: 1 件" in text and "原本の PDF がある: 2 件" in text
    assert "| 2021 | 3 | 3 | 100% |" in text and "一覧に無い本文: 1 件" in text
    assert "8,000 字で切れている 1 件（33%）" in text and "ほとんど読めていない（200 字未満）1 件（33%）" in text      # 全体は「件」を付けて言う
    assert "| 2019 | 1 | 0 | 0% | 1 | 0 | 0 | – | – | – |" in text        # 本文が 1 件も無い年は、長さや割合を 0 と書かない
    assert "続けてそろっているのは 2021-08 から 2021-09 までの 2 か月" in text


def world(tmp_path):
    root = tmp_path / "agents"
    ir, disk = root / "ir", tmp_path / "disk"
    ir.mkdir(parents=True)
    pd.DataFrame(ROWS, columns=C.COLUMNS).to_csv(ir / "disclosures.csv", index=False)
    (disk / "ir_text").mkdir(parents=True)
    (ir / "text").symlink_to(disk / "ir_text")
    return root, ir, disk


def test_tool_writes_the_inventory_and_tells_where(tmp_path, capsys):
    root, ir, disk = world(tmp_path)
    put(disk / "ir_text", "2021-08", "a2.txt", WORDS)
    put(disk / "ir_pdf", "2021-08", "a2.pdf", "%PDF")
    (ir / "text_missing.csv").write_text("a1\n", encoding="utf-8")
    out = load_script("agents_inventory").cmd_inventory(Namespace(root=str(root), out=str(tmp_path / "out")))
    said = capsys.readouterr().out
    assert "見るだけ" in said and "本文がある: 1 件" in said and str(out / "inventory.md") in said
    assert "## 2. 年ごと" in (out / "inventory.md").read_text(encoding="utf-8")
    docs = pd.read_csv(out / "inventory_docs.csv", dtype=str)
    assert set(docs["id"]) == {"a1", "a2", "a3", "a4", "a5"} and "title" not in docs.columns
    assert not list(disk.rglob(".write_test*")) and sorted(p.name for p in (disk / "ir_text").rglob("*") if p.is_file()) == ["a2.txt"]   # 置き場所には何も書かない


def test_tool_does_not_guess_when_the_disk_is_not_attached(tmp_path):
    root, _, disk = world(tmp_path)
    (disk / "ir_text").rmdir()                                            # 行き先の無いリンク（HDD がつながっていない）
    with pytest.raises(SystemExit) as stop:
        load_script("agents_inventory").cmd_inventory(Namespace(root=str(root), out=str(tmp_path / "out")))
    assert "HDD" in str(stop.value) and not (tmp_path / "out").exists()
