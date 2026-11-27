"""読む一覧（cfdbot/agents/digest.py・./cfd agents digest）: その会社の「ふだん」と違う開示を、短い一覧にする。

人が読む量を減らす道具。値段は使わない・判定しない（docs/ir_text.md 2 章）。「ふだん」は、その開示より前の 3 年の、同じ会社の開示から作る。
"""

import os
from argparse import Namespace
from datetime import datetime, timedelta

import pandas as pd
import pytest

from cfdbot.agents import collector as C
from cfdbot.agents import digest as D
from tests.test_text_history import load_script

BUYBACK = "自己株式の取得状況に関するお知らせ"
ROWS = [("t1", "2026-09-01 15:00:00", "7203.T", "トヨタ", BUYBACK, "", ""),
        ("t2", "2026-10-01 15:00:00", "7203.T", "トヨタ", BUYBACK, "", ""),
        ("t3", "2026-11-02 15:00:00", "7203.T", "トヨタ", BUYBACK, "", ""),                                     # いつもどおり
        ("t4", "2026-11-02 16:00:00", "7203.T", "トヨタ", "特別調査委員会の設置に関するお知らせ", "", ""),      # この会社では初めての題名
        ("n1", "2026-08-05 15:00:00", "1332.T", "ニッスイ", "業績予想の修正に関するお知らせ", "", ""),
        ("n2", "2026-11-02 15:30:00", "1332.T", "ニッスイ", "業績予想の修正に関するお知らせ", "", ""),          # いつもの題名だが、中身が違う
        ("n3", "2026-11-02 15:40:00", "1332.T", "ニッスイ", "Notice of Revision of Forecast", "", ""),           # 英文の版
        ("n4", "2026-11-02 15:50:00", "1332.T", "ニッスイ", "人事異動のお知らせ", "", ""),                       # 本文がまだ無い
        ("n5", "2026-11-02 15:55:00", "1332.T", "ニッスイ", "組織変更のお知らせ", "", ""),                       # 本文が読めていない（短い）
        ("s1", "2026-06-01 15:00:00", "6758.T", "ソニー", "特別調査委員会の設置に関するお知らせ", "", ""),      # ほかの会社の、同じ題名
        ("f1", "2026-11-03 09:00:00", "7203.T", "トヨタ", "特別調査委員会の設置に関するお知らせ", "", "")]      # 先の日の開示（見ない）
TEXTS = {"t1": "当社は自己株式を取得しました。取得した株式の総数は 100 株です。" * 10,
         "t2": "当社は自己株式を取得しました。取得した株式の総数は 2,300 株です。" * 10,
         "t3": "当社は自己株式を取得しました。取得した株式の総数は 45,000 株です。" * 10,
         "t4": "当社は特別調査委員会を設置しました。不適切な会計処理の疑いについて調査します。" * 8,
         "n1": "業績予想を修正します。理由は原材料価格の上昇です。" * 12,
         "n2": "業績予想を修正します。理由は海外子会社における減損損失の計上です。" * 12,
         "n3": "Notice of revision of the consolidated earnings forecast. " * 8,
         "n5": "組織を変更します。",
         "f1": "先の日の本文。" * 40}
DAY = "2026-11-02"


def disc():
    return pd.DataFrame(ROWS, columns=C.COLUMNS)


def table():
    return D.describe(disc(), TEXTS.get, DAY).set_index("id")


def test_shingles_ignore_spaces_and_the_size_of_numbers():
    assert D.shingles("売上高 12 億円") == D.shingles("売上高　3,456.7億円")       # 数字だけが変わった書類は、同じに見る
    assert D.similarity(D.shingles("あいうえお"), D.shingles("あいうえお")) == 1.0
    assert D.similarity(D.shingles("あいうえお"), D.shingles("かきくけこ")) == 0.0
    assert D.similarity(D.shingles(""), D.shingles("")) == 1.0


def test_words_are_runs_of_kanji_or_katakana():
    assert D.words("当社は特別調査委員会を設置し、リコールを実施します") == {"当社", "特別調査委員会", "設置", "リコール", "実施"}


def test_describe_looks_only_at_the_day_and_only_at_the_past():
    t = table()
    assert sorted(t.index) == ["n2", "n3", "n4", "n5", "t3", "t4"]                 # その日の開示だけ（先の日の f1 は入らない）
    assert t.loc["t4", "same"] == 0 and t.loc["t4", "rare"] == 1                   # 先の日の f1 は数えない。ほかの会社の s1 は「珍しさ」に数える


def test_a_routine_notice_with_only_new_numbers_is_usual():
    t = table()
    assert t.loc["t3", "same"] == 2 and t.loc["t3", "diff"] < 0.1 and t.loc["t3", "why"] == ""
    assert 0.9 < t.loc["t3", "ratio"] < 1.2


def test_a_title_the_company_has_not_used_is_listed_with_its_new_words():
    t = table()
    assert t.loc["t4", "why"].startswith("初めての題名") and pd.isna(t.loc["t4", "diff"])
    assert {"特別調査委員会", "会計処理"} <= set(t.loc["t4", "new"].split("・"))
    assert "自己株式" not in t.loc["t4", "new"] and "当社" not in t.loc["t4", "new"]      # その会社の過去の本文に出ている言葉は「初めて」でない


def test_a_usual_title_with_different_content_is_listed():
    t = table()
    assert t.loc["n2", "same"] == 1 and t.loc["n2", "diff"] >= D.DIFF_LINE
    assert t.loc["n2", "why"].startswith("いつもの題名だが") and "減損損失" in t.loc["n2", "new"]


def test_what_cannot_be_judged_is_not_listed_and_says_why():
    t = table()
    assert t.loc["n3", "why"] == "" and t.loc["n3", "note"] == "日本語が少ない"
    assert t.loc["n4", "why"] == "" and t.loc["n4", "note"] == "本文がまだ無い"
    assert t.loc["n5", "why"] == "" and t.loc["n5", "note"] == "本文が読めていない"


def test_render_shows_two_short_lists_and_says_what_it_is_not():
    text = D.render(D.describe(disc(), TEXTS.get, DAY), DAY, TEXTS.get, top=5)
    assert "珍しさの順" in text and "値段は使っていない" in text
    assert "この日の開示 6 件" in text and "初めての題名 1 件" in text and "中身が大きく違う 1 件" in text and "いつもどおり 1 件" in text
    first, second, bodies = text.index("## 1. 初めての題名"), text.index("## 2. いつもの題名だが"), text.index("## 4. 一覧に出た開示の本文")
    assert first < text.index("特別調査委員会の設置に関するお知らせ") < second < text.index("業績予想の修正に関するお知らせ") < bodies
    assert BUYBACK not in text                                                     # いつもどおりの開示は、数だけ
    assert TEXTS["t4"] in text[bodies:] and TEXTS["n2"] in text[bodies:]           # 一覧に出た開示の本文は、下に付ける（生成 AI に渡す分）


def test_render_keeps_each_list_short(monkeypatch):
    rows = [(f"x{k}", f"2026-11-02 1{k}:00:00", f"{2000 + k}.T", f"会社{k}", f"めずらしい知らせ{'あいうえおかきくけこ'[k]}のお知らせ", "", "") for k in range(8)]
    texts = {f"x{k}": "めずらしい知らせの本文です。" * 20 for k in range(8)}
    text = D.render(D.describe(pd.DataFrame(rows, columns=C.COLUMNS), texts.get, DAY), DAY, texts.get, top=3)
    assert text.count("### 1-") == 3 and "ほかに 5 件（題名だけ" in text
    rest = text[text.index("ほかに 5 件"):text.index("## 2. ")]
    assert rest.count("\n- ") == 5 and "会社7（2007）" in rest and "のお知らせ」" in rest          # くわしく出さなかった分も、題名は見える
    assert text[text.index("## 3. "):].count("### ") == 3                                          # 本文を付けるのは、くわしく出した分だけ


def test_render_stops_the_title_only_part_too(monkeypatch):
    monkeypatch.setattr(D, "REST_SHOWN", 2)
    rows = [(f"x{k}", f"2026-11-02 1{k}:00:00", f"{2000 + k}.T", f"会社{k}", f"めずらしい知らせ{'あいうえおかきくけこ'[k]}のお知らせ", "", "") for k in range(8)]
    texts = {f"x{k}": "めずらしい知らせの本文です。" * 20 for k in range(8)}
    text = D.render(D.describe(pd.DataFrame(rows, columns=C.COLUMNS), texts.get, DAY), DAY, texts.get, top=3)
    rest = text[text.index("ほかに 5 件"):text.index("## 2. ")]
    assert rest.count("\n- ") == 2 and "さらに 3 件" in rest


def test_render_names_what_it_could_not_read():
    """本文が無い・読めていない開示は、比べられない。黙って落とさずに、題名を並べる（英文の版は数だけ）。"""
    text = D.render(D.describe(disc(), TEXTS.get, DAY), DAY, TEXTS.get, top=5)
    part = text[text.index("## 3. 本文が無くて"):text.index("## 4. ")]
    assert "人事異動のお知らせ" in part and "本文がまだ無い" in part and "組織変更のお知らせ" in part and "本文が読めていない" in part
    assert "Notice of Revision" not in part and "日本語が少ない 1 件" in text
    assert "## 4. 一覧に出た開示の本文" in text


def test_render_says_nothing_about_unread_ones_when_all_were_read():
    rows = [r for r in ROWS if r[0] in ("t1", "t2", "t3", "t4")]
    text = D.render(D.describe(pd.DataFrame(rows, columns=C.COLUMNS), TEXTS.get, DAY), DAY, TEXTS.get)
    part = text[text.index("## 3. 本文が無くて"):text.index("## 4. ")]
    assert "（無い）" in part


def test_render_carries_a_warning_line():
    text = D.render(D.describe(disc(), TEXTS.get, DAY), DAY, TEXTS.get, note="⚠ この日は、まだ途中")
    assert text.index("⚠ この日は、まだ途中") < text.index("## 1. ")


def world(tmp_path):
    root = tmp_path / "agents"
    ir = root / "ir"
    ir.mkdir(parents=True)
    disc().to_csv(ir / "disclosures.csv", index=False)
    for r in disc().itertuples(index=False):
        if r.id in TEXTS:
            path = C.text_path(ir, r)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(TEXTS[r.id], encoding="utf-8")
    return root


def test_tool_writes_the_list_for_the_latest_day_with_texts(tmp_path, capsys):
    root = world(tmp_path)
    m = load_script("agents_digest")
    out = m.cmd_digest(Namespace(root=str(root), date=DAY, each=5))
    said = capsys.readouterr().out
    assert out == root / "digest" / f"{DAY}.md" and out.exists() and str(out) in said
    assert "初めての題名 1 件" in said and "特別調査委員会の設置に関するお知らせ" in said and TEXTS["t4"] not in said      # 画面には一覧だけ（本文はファイルに）
    assert TEXTS["t4"] in out.read_text(encoding="utf-8")
    m.cmd_digest(Namespace(root=str(root), date=None, each=5))                      # 日付を言わなければ、開示のあるいちばん新しい日
    assert (root / "digest" / "2026-11-03.md").exists()


def test_tool_says_when_there_is_nothing_for_the_day(tmp_path):
    root = world(tmp_path)
    with pytest.raises(SystemExit) as stop:
        load_script("agents_digest").cmd_digest(Namespace(root=str(root), date="2026-01-01", each=5))
    assert "開示が無い" in str(stop.value)


def refreshed(root, when):
    C.note_list_refreshed(root / "ir" / "disclosures.csv", pd.Timestamp(when))


def test_tool_shows_the_latest_finished_day_unless_told(tmp_path, capsys):
    """夕方の開示は、次の朝の収集で一覧にそろう。一覧を最後にそろえた日の分は、まだ途中なので、言われなければ、その前の日を出す。"""
    root = world(tmp_path)
    refreshed(root, "2026-11-03 13:05")
    out = load_script("agents_digest").cmd_digest(Namespace(root=str(root), date=None, each=5))
    said = capsys.readouterr().out
    assert out.name == "2026-11-02.md"
    assert "開示の一覧を最後にそろえたのは 11-03 13:05" in said and "11-02（月）の開示はそろっている" in said
    assert "11-03（火）の開示が 1 件" in said and "まだ途中" in said and "--date 2026-11-03" in said
    assert "⚠" not in said


def test_tool_warns_about_a_day_that_is_not_finished(tmp_path, capsys):
    root = world(tmp_path)
    refreshed(root, "2026-11-03 13:05")
    out = load_script("agents_digest").cmd_digest(Namespace(root=str(root), date="2026-11-03", each=5))
    said = capsys.readouterr().out
    line = "⚠ この日（11-03）は、まだ途中"
    assert line in said and "11-03 13:05" in said and line in out.read_text(encoding="utf-8")


def test_tool_uses_the_time_of_the_list_file_when_there_is_no_note(tmp_path, capsys):
    """控えが無い間（道具を新しくしてから最初の収集まで）は、一覧のファイルの時刻で見る。"""
    root = world(tmp_path)
    when = datetime(2026, 11, 3, 8, 5).timestamp()
    os.utime(root / "ir" / "disclosures.csv", (when, when))
    out = load_script("agents_digest").cmd_digest(Namespace(root=str(root), date=None, each=5))
    assert out.name == "2026-11-02.md" and "11-03 08:05" in capsys.readouterr().out


def test_tool_lists_the_days_around(tmp_path, capsys):
    root = world(tmp_path)
    refreshed(root, "2026-11-04 08:00")
    m = load_script("agents_digest")
    m.cmd_digest(Namespace(root=str(root), date="2026-11-02", each=5))
    capsys.readouterr()
    m.cmd_digest(Namespace(root=str(root), date=None, each=5))                      # 11-03（そろっている、いちばん新しい日）
    said = capsys.readouterr().out
    assert "ほかの日" in said and "11-02（月）6 件" in said and "10-01（木）1 件＊" in said and "＊ は、まだ一覧を作っていない日" in said


def test_tool_warns_when_many_texts_of_the_day_are_missing(tmp_path, capsys):
    root = world(tmp_path)
    for doc in ("t3", "t4", "n2"):
        C.text_path(root / "ir", next(r for r in disc().itertuples(index=False) if r.id == doc)).unlink()
    load_script("agents_digest").cmd_digest(Namespace(root=str(root), date=DAY, each=5))
    said = capsys.readouterr().out
    assert "⚠ この日の開示 6 件のうち 4 件は、本文がまだ無い" in said and "./cfd agents status" in said


def collect_ns(root, **kw):
    base = {"root": str(root), "data": str(root.parent / "stocks"), "text_history": False, "text_history_stop": False, "backfill": False,
            "force": False, "days": 5, "no_embed": True, "docs": False, "xbrl_history": False}
    return Namespace(**{**base, **kw})


def collect_world(tmp_path, monkeypatch):
    root = world(tmp_path)
    (root.parent / "stocks").mkdir()
    pd.DataFrame({"ticker": ["7203.T", "1332.T", "6758.T"], "name": ["トヨタ", "ニッスイ", "ソニー"]}).to_csv(root.parent / "stocks" / "universe_jp.csv", index=False)
    m = load_script("agents_digest_collect")
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    return root, m, root / "ir" / "disclosures.csv"


def test_collection_notes_when_it_refreshed_the_list(tmp_path, monkeypatch):
    root, m, path = collect_world(tmp_path, monkeypatch)
    asked = []
    monkeypatch.setattr(C, "fetch_day", lambda day: asked.append(f"{day:%Y-%m-%d}") or pd.DataFrame(columns=C.COLUMNS))
    m.cmd_collect(collect_ns(root))
    assert len(asked) == 5 and path.with_name(C.LIST_STAMP).exists()
    assert abs(C.list_refreshed(path) - datetime.now()) < timedelta(hours=10)       # 日本の時刻で控える（試験の機械の時刻とは、ずれてよい）


def test_collection_keeps_the_old_note_when_a_day_could_not_be_fetched(tmp_path, monkeypatch, capsys):
    """一覧を取れなかった日があれば、「そろえた」とは控えない（ファイルを書き直しても、読む一覧が、途中の日を「そろった」と見ないように）。"""
    root, m, path = collect_world(tmp_path, monkeypatch)
    when = datetime(2026, 11, 3, 8, 5).timestamp()
    os.utime(path, (when, when))

    def broken(day):
        raise OSError("つながらない")

    monkeypatch.setattr(C, "fetch_day", broken)
    m.cmd_collect(collect_ns(root))
    assert "一覧が取れない" in capsys.readouterr().out
    assert C.list_refreshed(path) == datetime(2026, 11, 3, 8, 5)                     # 控えが無かったときは、前のファイルの時刻を控える
    refreshed(root, "2026-11-04 08:00")
    m.cmd_collect(collect_ns(root))
    assert C.list_refreshed(path) == datetime(2026, 11, 4, 8, 0)


def test_collection_looks_further_back_after_a_gap(tmp_path, monkeypatch, capsys):
    """前にそろえてから間があいていたら（Mac を何日も切っていた など）、その間の日の一覧も取る（31 日まで）。"""
    root, m, path = collect_world(tmp_path, monkeypatch)
    asked = []
    monkeypatch.setattr(C, "fetch_day", lambda day: asked.append(f"{day:%Y-%m-%d}") or pd.DataFrame(columns=C.COLUMNS))
    C.note_list_refreshed(path, datetime.now() - timedelta(days=9))
    m.cmd_collect(collect_ns(root))
    assert 10 <= len(asked) <= 12 and "間があいたので" in capsys.readouterr().out      # 日本の時刻との差で 1 日ずれてもよい
    asked.clear()
    C.note_list_refreshed(path, datetime.now() - timedelta(days=200))
    m.cmd_collect(collect_ns(root))
    assert len(asked) == C.LIST_DAYS_MAX
    asked.clear()
    m.cmd_collect(collect_ns(root, days=0))                                         # 0 と言われたら、取りに行かない（控えも動かさない）
    assert asked == []


def test_list_days():
    today = pd.Timestamp("2026-11-27")
    assert C.list_days(5, None, today) == 5
    assert C.list_days(5, datetime(2026, 11, 26, 13, 5), today) == 5
    assert C.list_days(5, datetime(2026, 11, 20, 8, 0), today) == 8                 # 11-20 の分から（その日は途中だった）
    assert C.list_days(5, datetime(2025, 1, 1, 8, 0), today) == C.LIST_DAYS_MAX
    assert C.list_days(0, datetime(2026, 11, 20, 8, 0), today) == 0


# --------------------------------------------------------------------------- 読む一覧づくりを自動で（2026-11-28 利用者の決め）
def auto_ns(root, **kw):
    return Namespace(**{"root": str(root), "date": None, "each": None, "auto": True, **kw})


def auto_tool(monkeypatch, name="agents_digest_auto"):
    m = load_script(name)
    sent = []
    monkeypatch.setattr(m, "digest_notice", lambda folder, title, text: sent.append((title, text)) or [("imessage", True, "送った")])
    return m, sent


def test_auto_makes_the_lists_of_the_finished_days_that_have_none(tmp_path, capsys, monkeypatch):
    root = world(tmp_path)
    refreshed(root, "2026-11-04 08:00")
    m, sent = auto_tool(monkeypatch)
    made = m.cmd_digest(auto_ns(root))
    assert [q.name for q in made] == ["2026-11-02.md", "2026-11-03.md"]                    # そろっている日のうち、直近 7 日の分（10-01 より前は作らない）
    said = capsys.readouterr().out
    assert "11-02（月）の分を作った" in said and "開示 6 件" in said and "初めての題名 1 件" in said and "中身が大きく違う 1 件" in said
    assert "### 1-1" not in said                                                           # 毎朝の記録には、数だけ（一覧はファイルに）
    assert [t for t, _ in sent] == ["読む一覧 11-03（火）"]                                # 知らせは、いちばん新しい日の分を 1 回だけ
    assert m.cmd_digest(auto_ns(root)) == []
    assert "新しく作る日は無い" in capsys.readouterr().out and len(sent) == 1


def test_auto_never_makes_a_day_that_is_not_finished(tmp_path, capsys, monkeypatch):
    root = world(tmp_path)
    refreshed(root, "2026-11-03 13:05")
    m, sent = auto_tool(monkeypatch)
    assert [q.name for q in m.cmd_digest(auto_ns(root))] == ["2026-11-02.md"]
    assert [t for t, _ in sent] == ["読む一覧 11-02（月）"]


def test_auto_makes_a_list_again_once_the_missing_texts_have_come(tmp_path, capsys, monkeypatch):
    root = world(tmp_path)
    refreshed(root, "2026-11-03 08:00")
    paths = {doc: C.text_path(root / "ir", next(r for r in disc().itertuples(index=False) if r.id == doc)) for doc in ("t3", "t4", "n2")}
    for q in paths.values():
        q.unlink()
    m, sent = auto_tool(monkeypatch)
    first = m.cmd_digest(auto_ns(root))[0]
    assert "⚠ この日の開示 6 件のうち 4 件は、本文がまだ無い" in first.read_text(encoding="utf-8")
    assert "⚠" in capsys.readouterr().out and "本文がまだ無い" in sent[0][1]
    for doc, q in paths.items():
        q.write_text(TEXTS[doc], encoding="utf-8")
    again = m.cmd_digest(auto_ns(root))                                                    # 次の朝: 本文が入ったので、作り直す
    assert [q.name for q in again] == ["2026-11-02.md"] and "⚠" not in again[0].read_text(encoding="utf-8").split("## 1. ")[0]
    assert len(sent) == 1                                                                  # 作り直しでは、もう知らせない


def test_auto_can_be_switched_off(tmp_path, capsys, monkeypatch):
    root = world(tmp_path)
    refreshed(root, "2026-11-04 08:00")
    (root / "digest").mkdir()
    (root / "digest" / "STOP").write_text("", encoding="utf-8")
    m, sent = auto_tool(monkeypatch)
    assert m.cmd_digest(auto_ns(root)) == [] and sent == []
    assert "自動は止めてある" in capsys.readouterr().out
    out = m.cmd_digest(Namespace(root=str(root), date=None, each=5))                       # 頼まれた分は、止めてあっても作る
    assert out.name == "2026-11-03.md"


def test_auto_says_when_the_disk_is_away(tmp_path, monkeypatch):
    root = world(tmp_path)
    import shutil

    shutil.rmtree(root / "ir" / "text")
    (root / "ir" / "text").symlink_to(tmp_path / "nowhere")
    m, _ = auto_tool(monkeypatch)
    with pytest.raises(SystemExit) as stop:
        m.cmd_digest(auto_ns(root))
    assert "本文の置き場所が見えない" in str(stop.value)


def test_notice_is_short_and_names_the_top_ones():
    table = D.describe(disc(), TEXTS.get, DAY)
    title, text = D.notice(table, "11-02（月）", ["⚠ 注意の行"])
    assert title == "読む一覧 11-02（月）"
    assert "開示 6 件" in text and "初めての題名 1 件" in text and "⚠ 注意の行" in text
    assert "トヨタ「特別調査委員会の設置に関するお知らせ」" in text and "ニッスイ「業績予想の修正に関するお知らせ」" in text
    assert "勧めではない" in text and BUYBACK not in text and TEXTS["t4"][:30] not in text       # 本文は入れない（題名だけ）


def test_notice_stays_within_the_limit():
    rows = [(f"x{k:02d}", f"2026-11-02 1{k % 10}:00:00", f"{2000 + k}.T", f"とても長い名前の会社{k}", "めずらしい知らせ" + "あ" * 80 + f"{'あいうえおかきくけこ'[k % 10]}のお知らせ", "", "")
            for k in range(12)]
    texts = {f"x{k:02d}": "めずらしい知らせの本文です。" * 20 for k in range(12)}
    _, text = D.notice(D.describe(pd.DataFrame(rows, columns=C.COLUMNS), texts.get, DAY), "11-02（月）", [])
    assert len(text) <= D.NOTICE_MAX and "ほかに" in text


class Done:
    def __init__(self, code=0, out="読む一覧: 11-02（月）の分を作った\n", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_collection_runs_the_list_as_a_separate_step_with_a_time_limit(tmp_path, capsys):
    """読む一覧は、別の道具として動かして、時間を切る（読む一覧が止まっても、毎朝の収集は終わる）。"""
    m = load_script("agents_digest_after")
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        return Done()

    m.digest_after_collect(Namespace(root=str(tmp_path / "agents"), data="data/stocks"), run=run)
    assert seen["argv"][1].endswith("scripts/agents.py") and seen["argv"][2:] == ["digest", "--auto", "--root", str(tmp_path / "agents"), "--data", "data/stocks"]
    assert seen["timeout"] == m.DIGEST_TIMEOUT and "11-02（月）の分を作った" in capsys.readouterr().out


def test_collection_goes_on_when_the_list_fails_or_hangs(tmp_path, capsys):
    import subprocess

    m = load_script("agents_digest_after2")
    args = Namespace(root=str(tmp_path / "agents"), data="data/stocks")

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    m.digest_after_collect(args, run=slow)
    said = capsys.readouterr().out
    assert "読む一覧: ⚠" in said and "終わらないので、やめた" in said and "収集は済んでいる" in said
    m.digest_after_collect(args, run=lambda argv, **kw: Done(1, "", "読む一覧: ⚠ 本文の置き場所が見えない"))
    assert "本文の置き場所が見えない" in capsys.readouterr().out

    def broken(argv, **kw):
        raise OSError("boom")

    m.digest_after_collect(args, run=broken)
    assert "読む一覧: ⚠ 作れなかった" in capsys.readouterr().out
