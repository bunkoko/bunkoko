"""過去の本文をまとめて取る（cfdbot/agents/collector.py の collect_text_history・./cfd agents collect --text-history）。

決まり: 東証の上場会社情報サービスから、1 秒に 1 件・同時に 1 本だけ。毎朝 8:00 の収集（決算の数字を取る回）には場所をゆずる。
無かった書類は覚えておいて、もう取りに行かない。断られたらすぐ止める。置き場所は外付けのディスク（2026-11-20 利用者の決め）。
"""

import time
import urllib.error
from datetime import datetime, timedelta

import pandas as pd
import pytest

from cfdbot.agents import collector as C
from tests.test_agents import _pdf

ROWS = [("p1", "2019-05-10 15:00:00", "7203.T", "トヨタ", "決算短信", "", "x1.zip"),
        ("p2", "2021-08-06 15:00:00", "1332.T", "ニッスイ", "業績予想の修正", "", ""),
        ("p3", "2024-11-08 15:00:00", "1332.T", "ニッスイ", "お知らせ", "", ""),
        ("p0", "2015-02-01 15:00:00", "7203.T", "トヨタ", "古い開示", "", "")]
SINCE = "2016-10-01"


def disc(rows=ROWS):
    return pd.DataFrame(rows, columns=C.COLUMNS)


def hdd(tmp_path):
    """本文の置き場所を、外付けのディスクへのリンクにした形。返すのは data/agents/ir に当たるフォルダと、ディスクの側のフォルダ。"""
    root, disk = tmp_path / "ir", tmp_path / "disk"
    root.mkdir(parents=True)
    (disk / "ir_text").mkdir(parents=True)
    (root / "text").symlink_to(disk / "ir_text")
    return root, disk


class Clock:
    """作り物の時計。sleep で進む。"""

    def __init__(self, start):
        self.t, self.slept = start, []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += timedelta(seconds=seconds)


def noon():
    return Clock(datetime(2026, 11, 25, 12, 0))


def http_error(url, code):
    return urllib.error.HTTPError(url, code, "x", {}, None)


def run(root, get, clock=None, **kw):
    clock = clock or noon()
    kw.setdefault("free", lambda p: 500 * 2 ** 30)
    return C.collect_text_history(disc(kw.pop("rows", ROWS)), root, since=SINCE, interval=0, get=get, log=kw.pop("log", lambda s: None),
                                  now=clock.now, sleep=clock.sleep, **kw)


def test_history_takes_the_newest_first_and_keeps_the_text_and_the_original(tmp_path):
    root, disk = hdd(tmp_path)
    calls = []

    def get(url):
        calls.append(url)
        return _pdf(url[-6:-4])

    got = run(root, get)
    assert [u.rsplit("/", 1)[1] for u in calls] == ["p3.pdf", "p2.pdf", "p1.pdf"]         # 新しい順。2016-10 より前（p0）と数字（zip）は取らない
    assert got == {"text": 3, "missing": 0, "failed": 0, "left": 0, "stopped": None}
    assert (root / "text" / "2024-11" / "p3.txt").read_text(encoding="utf-8") == "p3"     # 毎日の収集と同じ場所・同じ形
    assert (disk / "ir_pdf" / "2019-05" / "p1.pdf").read_bytes() == _pdf("p1")            # 原本も残す（読み方を変えるときに取り直さない）
    again = run(root, get)
    assert again["text"] == 0 and len(calls) == 3                                         # 取ってあるものは飛ばす


def test_history_can_leave_the_originals_out(tmp_path):
    root, disk = hdd(tmp_path)
    assert run(root, lambda url: _pdf("a"), keep_pdf=False)["text"] == 3 and not (disk / "ir_pdf").exists()


def test_history_remembers_what_the_site_does_not_have(tmp_path):
    root, _ = hdd(tmp_path)
    calls = []

    def get(url):
        calls.append(url.rsplit("/", 1)[1])
        if "p2" in url:
            raise http_error(url, 404)                                                    # サイトに無い
        if "p1" in url:
            raise OSError("回線が切れた")                                                # たまたま取れなかった
        return _pdf("a")

    got = run(root, get)
    assert got["text"] == 1 and got["missing"] == 1 and got["failed"] == 1 and got["left"] == 1     # 残りは、取れなかった p1 だけ
    assert C.load_missing(root) == {"p2"}
    calls.clear()
    run(root, get)
    assert calls == ["p1.pdf"]                                                            # 無かった p2 には、もう取りに行かない。p1 はもう一度試す


def test_history_does_not_keep_what_is_not_a_pdf(tmp_path):
    """エラーのページ（HTML）が「成功」として返ってくることがある。空の本文として残すと、取り直せなくなる。"""
    root, disk = hdd(tmp_path)
    got = run(root, lambda url: b"<html>error</html>" if "p2" in url else _pdf("a"))
    assert got["text"] == 2 and got["failed"] == 1 and got["left"] == 1
    assert not (root / "text" / "2021-08" / "p2.txt").exists() and not (disk / "ir_pdf" / "2021-08").exists()
    assert C.load_missing(root) == set()


def test_history_takes_only_as_many_as_asked_and_goes_on_next_time(tmp_path):
    root, _ = hdd(tmp_path)
    calls = []

    def get(url):
        calls.append(url.rsplit("/", 1)[1])
        return _pdf("a")

    first = run(root, get, max_docs=2)
    assert first["text"] == 2 and first["left"] == 1 and calls == ["p3.pdf", "p2.pdf"]
    assert run(root, get, max_docs=2)["left"] == 0 and calls[-1] == "p1.pdf"


def test_history_yields_to_the_morning_collection(tmp_path):
    """毎朝 8:00 の収集は、取得が動いていると書類を取らずに終わる（決算の数字が抜けて、その日の E1 の記録が傷む）。7:50〜8:45 は休む。"""
    root, _ = hdd(tmp_path)
    clock = Clock(datetime(2026, 11, 25, 7, 55))
    seen = []

    def get(url):
        seen.append(clock.now())
        return _pdf("a")

    free_while_waiting = []
    real_sleep = clock.sleep

    def sleep(seconds):
        free_while_waiting.append(C.docs_lock(root))             # 休んでいる間は、錠を持っていない（朝の収集が取れる）
        C.docs_unlock(root)
        real_sleep(seconds)

    clock.sleep = sleep
    logs = []
    got = run(root, get, clock=clock, log=logs.append)
    assert got["text"] == 3 and all(t >= datetime(2026, 11, 25, 8, 45) for t in seen)
    assert free_while_waiting and all(free_while_waiting) and any("8:00" in x for x in logs)


def test_history_stops_in_the_middle_when_the_morning_window_begins(tmp_path):
    root, _ = hdd(tmp_path)
    clock = Clock(datetime(2026, 11, 25, 7, 49, 59))
    seen = []

    def get(url):
        seen.append(clock.now())
        clock.t += timedelta(seconds=2)                          # 1 件取る間に 7:50 を過ぎる
        return _pdf("a")

    assert run(root, get, clock=clock)["text"] == 3
    assert seen[0] < datetime(2026, 11, 25, 7, 50) and all(t >= datetime(2026, 11, 25, 8, 45) for t in seen[1:])


@pytest.mark.parametrize("code", [403, 429, 503])
def test_history_stops_at_once_when_the_site_refuses(tmp_path, code):
    root, _ = hdd(tmp_path)
    calls = []

    def get(url):
        calls.append(url)
        raise http_error(url, code)

    got = run(root, get)
    assert got["stopped"] == "refused" and len(calls) == 1 and got["left"] == 3            # 1 回断られたら、続けて取りに行かない
    assert C.load_missing(root) == set()                                                   # 「無かった」とは覚えない


def test_history_stops_after_many_failures_in_a_row(tmp_path):
    root, _ = hdd(tmp_path)
    rows = [(f"q{k:02d}", f"2024-01-{k + 1:02d} 15:00:00", "7203.T", "トヨタ", "お知らせ", "", "") for k in range(25)]
    calls = []

    def get(url):
        calls.append(url)
        raise OSError("回線が切れた")

    got = run(root, get, rows=rows)
    assert got["stopped"] == "network" and len(calls) == C.MAX_FAILS and got["left"] == 25


def test_history_stops_when_the_disk_goes_away(tmp_path):
    root, disk = hdd(tmp_path)
    calls = []

    def get(url):
        calls.append(url)
        if len(calls) == 2:
            (disk / "ir_text").rename(disk / "gone")              # 2 件目を取っている間に、ディスクが外れた
        return _pdf("a")

    got = run(root, get, keep_pdf=False)
    assert got["stopped"] == "disk" and got["text"] == 1 and len(calls) == 2


def test_history_needs_the_external_disk(tmp_path):
    plain = tmp_path / "ir"                                       # リンクにしていない（内蔵のまま）
    plain.mkdir()
    called = []
    got = run(plain, lambda url: called.append(url) or _pdf("a"))
    assert got["stopped"] == "not_external" and called == []
    root, disk = hdd(tmp_path / "x")
    (disk / "ir_text").rmdir()                                    # リンクの行き先が無い（つながっていない）
    assert run(root, lambda url: called.append(url) or _pdf("a"))["stopped"] == "no_text_place" and called == []


def test_history_checks_the_free_space_before_keeping_the_originals(tmp_path):
    root, _ = hdd(tmp_path)
    called = []
    small = run(root, lambda url: called.append(url) or _pdf("a"), free=lambda p: 4 * 2 ** 30)          # 残しておく 5GB にも届かない
    assert small["stopped"] == "no_space" and called == [] and round(small["free_gb"]) == 4 and small["need_gb"] > 5
    assert run(root, lambda url: called.append(url) or _pdf("a"), free=lambda p: 4 * 2 ** 30, keep_pdf=False)["text"] == 3


def test_history_asks_for_space_only_for_what_is_left(tmp_path):
    """要る空きは、これから取る件数に合わせる。途中で止めて続きから取るとき、残りが少しなのに「60GB 要る」と止まらないように。"""
    assert 55 < C.pdf_need(92_000) / 2 ** 30 < 62                 # 全部（9 万件ほど）なら 60GB ほど
    assert C.pdf_need(20) / 2 ** 30 < 5.1                         # 試しの 20 件なら、残しておく分とほぼ同じ
    root, _ = hdd(tmp_path)
    assert run(root, lambda url: _pdf("a"), free=lambda p: 10 * 2 ** 30)["text"] == 3


def test_history_stops_before_the_disk_is_full(tmp_path):
    """原本の大きさは見当なので、取っている間も空きを見る。HDD をいっぱいにしない（毎日の収集の本文も、同じ HDD に書く）。"""
    root, _ = hdd(tmp_path)
    space, calls = [100 * 2 ** 30], []

    def get(url):
        calls.append(url)
        space[0] = 4 * 2 ** 30                                    # 1 件取ったところで、空きが 5GB を切った
        return _pdf("a")

    got = run(root, get, free=lambda p: space[0])
    assert got["stopped"] == "no_space" and got["text"] == 1 and got["left"] == 2 and len(calls) == 1
    assert round(got["free_gb"]) == 4
    assert run(root, get, free=lambda p: space[0], keep_pdf=False)["text"] == 2          # 原本を残さなければ、続きを取れる


def test_history_stops_plainly_when_the_disk_cannot_even_be_measured(tmp_path):
    """書類と書類の間でディスクが外れると、空きを見る所で分かる。道具ごと落ちずに、「書けなくなった」として止まる。"""
    root, _ = hdd(tmp_path)
    asked, calls = [], []

    def free(path):
        asked.append(path)
        if len(asked) >= 3:                                       # 始めの確かめと 1 件目の前は見えた。2 件目の前には、もう見えない
            raise FileNotFoundError(path)
        return 100 * 2 ** 30

    got = run(root, lambda url: calls.append(url) or _pdf("a"), free=free)
    assert got["stopped"] == "disk" and got["text"] == 1 and len(calls) == 1 and not (root / ".docs.lock").exists()


def test_history_does_not_break_when_the_mark_file_vanishes(tmp_path, monkeypatch):
    root = tmp_path / "ir"
    root.mkdir()
    (root / C.HISTORY_LOCK).write_text("1")
    real = open

    def gone(path, *a, **kw):
        if str(path).endswith(C.HISTORY_LOCK):
            raise FileNotFoundError(path)                         # 見た直後に消えた
        return real(path, *a, **kw)

    monkeypatch.setattr("builtins.open", gone)
    assert C.history_running(root) is None


def test_history_tells_the_free_space_as_it_goes(tmp_path, monkeypatch):
    root, _ = hdd(tmp_path)
    monkeypatch.setattr(C, "HISTORY_CHUNK", 2)
    logs = []
    run(root, lambda url: _pdf("a"), free=lambda p: 123 * 2 ** 30, log=logs.append)
    assert any("過去の本文 2/3" in s and "HDD の空き 123GB" in s for s in logs)


def test_history_waits_while_another_fetch_holds_the_lock(tmp_path):
    root, _ = hdd(tmp_path)
    assert C.docs_lock(root)                                      # ほかの取得（毎朝の収集など）が動いている
    clock = noon()
    real_sleep = clock.sleep

    def sleep(seconds):
        C.docs_unlock(root)                                       # 待っている間に、ほかの取得が終わった
        real_sleep(seconds)

    clock.sleep = sleep
    got = run(root, lambda url: _pdf("a"), clock=clock)
    assert got["text"] == 3 and clock.slept and not (root / ".docs.lock").exists()


def test_history_keeps_the_pace(tmp_path):
    root, _ = hdd(tmp_path)
    started = time.monotonic()
    C.collect_text_history(disc(), root, since=SINCE, interval=0.2, get=lambda url: _pdf("a"), log=lambda s: None, free=lambda p: 500 * 2 ** 30,
                           now=lambda: datetime(2026, 11, 25, 12, 0))
    assert time.monotonic() - started >= 0.4                      # 3 件なら、間が 2 回


def test_quiet_window_is_ten_minutes_before_eight_to_a_quarter_to_nine():
    assert not C.in_quiet(datetime(2026, 11, 25, 7, 49)) and C.in_quiet(datetime(2026, 11, 25, 7, 50))
    assert C.in_quiet(datetime(2026, 11, 25, 8, 44)) and not C.in_quiet(datetime(2026, 11, 25, 8, 45))
    assert C.INTERVAL == 1.0                                      # 1 秒に 1 件（2026-10-13 利用者の決め）。速くしない


# --------------------------------------------------------------------------- 道具（./cfd agents collect --text-history）を通しで
def load_script(name="agents_text_history"):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "scripts" / "agents.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def world(tmp_path, link=True):
    """data/agents の形（開示の一覧つき）。link なら、本文の置き場所は外付けのディスクへのリンク。"""
    root = tmp_path / "agents"
    ir = root / "ir"
    ir.mkdir(parents=True)
    disc().to_csv(ir / "disclosures.csv", index=False)
    disk = tmp_path / "disk"
    if link:
        (disk / "ir_text").mkdir(parents=True)
        (ir / "text").symlink_to(disk / "ir_text")
    return root, ir, disk


def ns(root, **kw):
    from argparse import Namespace

    base = {"root": str(root), "data": str(root.parent / "stocks"), "text_history": True, "no_pdf": False, "history_max": 0}
    return Namespace(**{**base, **kw})


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(C, "INTERVAL", 0.0)                       # 試験では待たない（決まりは 1 秒に 1 件）
    monkeypatch.setattr(C, "PDF_EACH_MB", 0.0, raising=False)     # 試験の置き場所の空きは問わない
    monkeypatch.setattr(C, "PDF_KEEP_FREE_GB", 0, raising=False)
    monkeypatch.setattr(C, "in_quiet", lambda t: False)           # 試験を朝に流しても休まない
    monkeypatch.setattr(C, "FAIL_WAIT", 0.0, raising=False)       # 取れなかったときの間（本物は 15 秒から）を、試験では待たない


def test_tool_takes_the_past_texts_and_tells_what_it_did(tmp_path, capsys, monkeypatch, fast):
    root, ir, disk = world(tmp_path)
    monkeypatch.setattr(C, "http_get", lambda url, tries=1: _pdf("本文"))
    load_script().cmd_collect(ns(root))
    out = capsys.readouterr().out
    assert "まだ本文の無い 3 件" in out and "新しい順" in out and "1 秒に 1 件" not in out      # 待ち時間は、その時の設定のまま出す（試験では 0 秒）
    assert "取れた +3" in out and "残り 0" in out and "終わった" in out
    assert (ir / "text" / "2024-11" / "p3.txt").exists() and (disk / "ir_pdf" / "2024-11" / "p3.pdf").exists()
    assert "2015" not in "".join(str(p) for p in (disk / "ir_text").rglob("*"))               # 2016-10 より前は取らない


def test_tool_refuses_to_fill_the_internal_disk(tmp_path, capsys, monkeypatch, fast):
    root, ir, _ = world(tmp_path, link=False)
    called = []
    monkeypatch.setattr(C, "http_get", lambda url, tries=1: called.append(url) or _pdf("a"))
    with pytest.raises(SystemExit, match="HDD"):
        load_script().cmd_collect(ns(root))
    assert called == [] and not list((ir / "text").glob("*/*.txt"))


def test_tool_says_why_it_stopped(tmp_path, capsys, monkeypatch, fast):
    root, _, _ = world(tmp_path)

    def refuse(url, tries=1):
        raise http_error(url, 429)

    monkeypatch.setattr(C, "http_get", refuse)
    with pytest.raises(SystemExit, match="断られた"):
        load_script().cmd_collect(ns(root))
    assert "残り 3" in capsys.readouterr().out


def test_tool_says_how_much_space_is_missing(tmp_path, capsys, monkeypatch, fast):
    root, _, _ = world(tmp_path)
    monkeypatch.setattr(C, "PDF_KEEP_FREE_GB", 10 ** 7, raising=False)          # どの機械でも足りない大きさ
    called = []
    monkeypatch.setattr(C, "http_get", lambda url, tries=1: called.append(url) or _pdf("a"))
    with pytest.raises(SystemExit) as stop:
        load_script().cmd_collect(ns(root))
    said = str(stop.value)
    assert "空きが足りない" in said and "今の空き" in said and "--no-pdf" in said and called == []


def test_tool_can_do_a_small_trial_first(tmp_path, capsys, monkeypatch, fast):
    root, ir, disk = world(tmp_path)
    monkeypatch.setattr(C, "http_get", lambda url, tries=1: _pdf("a"))
    load_script().cmd_collect(ns(root, history_max=1, no_pdf=True))
    out = capsys.readouterr().out
    assert "取れた +1" in out and "残り 2" in out and "続きは同じコマンドで" in out
    assert len(list((ir / "text").glob("*/*.txt"))) == 1 and not (disk / "ir_pdf").exists()


def test_status_tells_how_far_the_past_texts_are(tmp_path, capsys, monkeypatch, fast):
    root, ir, _ = world(tmp_path)
    monkeypatch.setattr(C, "http_get", lambda url, tries=1: _pdf("a"))
    m = load_script()
    m.cmd_collect(ns(root, history_max=1))
    (ir / C.MISS_FILE).write_text("p2\n", encoding="utf-8")
    capsys.readouterr()
    m.cmd_status(ns(root, out=str(tmp_path / "out")))
    out = capsys.readouterr().out
    assert "過去の本文（2016-10〜）: 取ってある 1 件・サイトに無い 1 件・残り 1 件" in out


def test_daily_collection_tells_each_morning_whether_the_disk_can_be_written(tmp_path, capsys, monkeypatch, fast):
    """毎朝の収集は、新しく取る本文が 0 件の日も、外付けのディスクに書けるかを実際に確かめて、記録に 1 行残す。

    2026-11-24: 置き場所を HDD に移した日の試しは「新しく取る本文が 0 件」で、自動実行から書けるかが分からなかった。
    """
    from pathlib import Path

    root, _, disk = world(tmp_path)
    stocks = tmp_path / "stocks"
    stocks.mkdir()
    pd.DataFrame({"ticker": ["7203.T"], "name": ["トヨタ"]}).to_csv(stocks / "universe_jp.csv", index=False)
    m = load_script()
    args = ns(root, text_history=False, backfill=False, force=False, days=0, no_embed=True, docs=True, xbrl_history=False, docs_days=7, max_docs=300)
    m.cmd_collect(args)
    out = capsys.readouterr().out
    assert "本文 +0" in out and "情報収集: 本文の置き場所" in out and "外付けのディスクへのリンク。つながっている。書ける" in out and "⚠" not in out
    assert list((disk / "ir_text").iterdir()) == []                                  # 確かめに書いたファイルは残さない
    real = Path.write_bytes

    def blocked(self, data):                                                         # macOS が、自動実行から外付けへの書き込みを止めている
        if disk in self.resolve().parents:
            raise PermissionError(1, "Operation not permitted")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", blocked)
    m.cmd_collect(args)
    out = capsys.readouterr().out
    assert "情報収集: ⚠ 本文の置き場所" in out and "つながっているが、書けない" in out and "本文 +0" in out
    assert out.count("つながっているが、書けない") == 1                                # 同じ知らせを 2 度出さない


def test_history_tool_says_when_the_disk_is_there_but_cannot_be_written(tmp_path, capsys, monkeypatch, fast):
    from pathlib import Path

    root, _, disk = world(tmp_path)
    called = []
    monkeypatch.setattr(C, "http_get", lambda url, tries=1: called.append(url) or _pdf("a"))
    real = Path.write_bytes

    def blocked(self, data):
        if disk in self.resolve().parents:
            raise PermissionError(1, "Operation not permitted")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", blocked)
    with pytest.raises(SystemExit, match="書けない"):
        load_script().cmd_collect(ns(root))
    assert called == [] and "つながっているが、書けない" in capsys.readouterr().out


# --------------------------------------------------------------------------- 毎日の収集を先に通す（時間に関係なく）
def test_history_leaves_the_last_week_to_the_daily_collection(tmp_path):
    """直近の開示は、まだサイトに載っていないことがある（翌日から載る）。「無かった」と覚えてしまわないよう、過去分では触らない。"""
    root, _ = hdd(tmp_path)
    rows = [*ROWS, ("p9", "2026-11-22 15:00:00", "7203.T", "トヨタ", "3 日前の開示", "", "")]
    calls = []
    run(root, lambda url: calls.append(url.rsplit("/", 1)[1]) or _pdf("a"), rows=rows)       # 時計は 2026-11-25 の昼
    assert "p9.pdf" not in calls and len(calls) == 3


def test_history_gives_way_when_the_daily_collection_asks(tmp_path):
    """Mac が眠っていて 8:00 を過ぎると、毎朝の収集は起きた時に動く（何時でも）。その時も、過去分の取得は錠を放して先に通す。"""
    root, _ = hdd(tmp_path)
    clock = noon()
    calls, waited = [], []

    def get(url):
        calls.append(url.rsplit("/", 1)[1])
        if len(calls) == 1:
            (root / C.WANT_FILE).write_text(str(__import__("os").getpid()))      # 1 件目の途中で、毎日の収集が「先に取らせて」と頼んだ
        return _pdf("a")

    real_sleep = clock.sleep

    def sleep(seconds):
        waited.append((len(calls), C.docs_lock(root)))            # 待っている間は、錠が空いている
        C.docs_unlock(root)
        (root / C.WANT_FILE).unlink(missing_ok=True)              # 毎日の収集が終わった
        real_sleep(seconds)

    clock.sleep = sleep
    got = run(root, get, clock=clock)
    assert got["text"] == 3 and waited and waited[0] == (1, True)  # 1 件取ったところで手を止めて、ゆずった


def test_history_ignores_a_request_left_by_a_dead_process(tmp_path):
    root, _ = hdd(tmp_path)
    (root / C.WANT_FILE).write_text("999999999")                  # もう動いていない道具が残した印
    clock = noon()
    assert run(root, lambda url: _pdf("a"), clock=clock)["text"] == 3 and clock.slept == []


def test_daily_collection_waits_for_the_lock_and_asks_the_history_to_yield(tmp_path):
    disc2 = disc([ROWS[2]])
    root = tmp_path / "ir"
    root.mkdir()
    assert C.docs_lock(root)                                      # 過去分の取得が錠を持っている
    seen = []

    def sleep(seconds):
        seen.append((root / C.WANT_FILE).exists())                # 待つ間、「先に取らせて」の印を置いている
        C.docs_unlock(root)                                       # 過去分の取得が、印を見て錠を放した

    got = C.collect_docs(disc2, root, xbrl=False, interval=0, get=lambda url: _pdf("a"), log=lambda s: None, wait=600, sleep=sleep)
    assert got["text"] == 1 and "busy" not in got and seen == [True] and not (root / C.WANT_FILE).exists()


def test_daily_collection_gives_up_after_waiting_long_enough(tmp_path):
    root = tmp_path / "ir"
    root.mkdir()
    assert C.docs_lock(root)
    slept = []
    got = C.collect_docs(disc([ROWS[2]]), root, interval=0, get=lambda url: _pdf("a"), log=lambda s: None, wait=20, sleep=slept.append)
    assert got.get("busy") == 1 and sum(slept) >= 20 and not (root / C.WANT_FILE).exists()


def test_a_killed_run_leaves_no_half_written_text(tmp_path, monkeypatch):
    """本文のファイルがあることが「取った」印。書いている途中で止まっても、半端なファイルを「取った」ことにしない。"""
    root, _ = hdd(tmp_path)
    real = C.Path.replace

    def boom(self, target):
        raise KeyboardInterrupt                                   # 書き終わる直前に止められた

    monkeypatch.setattr(C.Path, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        run(root, lambda url: _pdf("a"), keep_pdf=False)
    monkeypatch.setattr(C.Path, "replace", real)
    assert not list((root / "text").glob("*/*.txt")) and not (root / ".docs.lock").exists()
    assert run(root, lambda url: _pdf("a"), keep_pdf=False)["text"] == 3


# --------------------------------------------------------------------------- 途中で止める・続きから取る・2 本目は始めない
def test_only_one_history_fetch_can_hold_the_mark(tmp_path):
    """過去分の取得は 1 本だけ。動いている間ずっと印を持ち、終わると（止められても）OS が外す。"""
    root = tmp_path / "ir"
    assert C.history_running(root) is None
    hold = C.history_hold(root)
    assert hold is not None and C.history_hold(root) is None                    # 2 本目は持てない
    assert C.history_running(root) == __import__("os").getpid()
    C.history_release(hold)
    assert C.history_running(root) is None
    again = C.history_hold(root)                                                # 外れたら、また持てる
    assert again is not None
    C.history_release(again)


def test_a_mark_left_by_a_dead_fetch_does_not_count(tmp_path):
    """止められた・機械が落ちた後に、番号だけ残っていても「動いている」と数えない（別の道具が同じ番号になっていても、取り違えない）。"""
    root = tmp_path / "ir"
    root.mkdir()
    (root / C.HISTORY_LOCK).write_text(str(__import__("os").getpid()))          # 今動いている別の道具（この試験）の番号が残っている
    assert C.history_running(root) is None
    hold = C.history_hold(root)
    assert hold is not None
    C.history_release(hold)


def test_tool_does_not_start_a_second_copy(tmp_path, capsys, monkeypatch, fast):
    """2 本目が動くと、同じ書類を 2 度ずつ取りに行ってしまう（錠が守るのは「同時に 1 本」だけ）。動いている間は、始めない。"""
    root, ir, _ = world(tmp_path)
    called = []
    monkeypatch.setattr(C, "http_get", lambda url, tries=1: called.append(url) or _pdf("a"))
    hold = C.history_hold(ir)                                                   # 1 本目が動いている
    try:
        with pytest.raises(SystemExit, match="もう動いている"):
            load_script().cmd_collect(ns(root))
        assert called == [] and C.history_running(ir) is not None               # 1 本目の印はそのまま
    finally:
        C.history_release(hold)
    load_script().cmd_collect(ns(root))                                         # 1 本目が終われば、始められる
    assert "終わった" in capsys.readouterr().out and C.history_running(ir) is None


def test_tool_stops_when_asked_and_goes_on_next_time(tmp_path, capsys, monkeypatch, fast):
    """長い取得は、途中で止められる（Mac を持ち出す・HDD を外す前に）。止める合図で止まり、取った分は残り、次は続きから。"""
    import os
    import signal

    root, ir, _ = world(tmp_path)
    outer = []

    def outside(*_):                                                            # 道具が合図を受け取らなければ、ここに来る（試験ごと止まらないように）
        outer.append("outer")

    before = signal.signal(signal.SIGTERM, outside)
    try:
        calls = []

        def get(url, tries=1):
            calls.append(url.rsplit("/", 1)[1])
            if len(calls) == 2:
                os.kill(os.getpid(), signal.SIGTERM)                            # 2 件目を取っている途中で「止めて」
            return _pdf("a")

        monkeypatch.setattr(C, "http_get", get)
        m = load_script()
        m.cmd_collect(ns(root))
        out = capsys.readouterr().out
        assert outer == [] and "頼まれて止めた" in out and "続きは" in out and "⚠" not in out
        assert 1 <= len(list((ir / "text").glob("*/*.txt"))) <= 2                # 取った分は残っている
        assert not (ir / ".docs.lock").exists() and C.history_running(ir) is None
        assert signal.getsignal(signal.SIGTERM) is outside                      # 合図の受け取り方を、元に戻している
        m.cmd_collect(ns(root))                                                 # 同じコマンドで続きから
        assert "終わった" in capsys.readouterr().out and len(list((ir / "text").glob("*/*.txt"))) == 3
        assert len(calls) <= 4                                                  # 取ってあるものは取り直さない
    finally:
        signal.signal(signal.SIGTERM, before)


def test_stop_command_stops_the_running_fetch_and_nothing_else(tmp_path, capsys, fast):
    import os
    import subprocess
    import sys

    root, ir, _ = world(tmp_path)
    m = load_script()
    stop = ns(root, text_history=False, text_history_stop=True)
    m.cmd_collect(stop)
    assert "動いていない" in capsys.readouterr().out                             # 止めるものが無い
    (ir / C.HISTORY_LOCK).write_text(str(os.getpid()))                          # 前の取得が残した番号と、たまたま同じ番号の別の道具（この試験）
    m.cmd_collect(stop)
    assert "動いていない" in capsys.readouterr().out                             # 取り違えて止めない（止めていたら、この試験ごと落ちる）
    code = ("import sys, time; from pathlib import Path; from cfdbot.agents import collector as C; "
            "hold = C.history_hold(Path(sys.argv[1])); print('ready', flush=True); time.sleep(60)")
    fetch = subprocess.Popen([sys.executable, "-c", code, str(ir)], stdout=subprocess.PIPE, text=True,
                             cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    try:
        assert fetch.stdout.readline().strip() == "ready" and C.history_running(ir) == fetch.pid
        m.cmd_collect(stop)
        out = capsys.readouterr().out
        assert "止めた" in out and "続きは" in out and "取り出して" in out
        assert fetch.wait(timeout=10) is not None and C.history_running(ir) is None
    finally:
        fetch.kill()
        fetch.wait()


def test_status_tells_whether_the_history_fetch_is_running(tmp_path, capsys, monkeypatch, fast):
    root, ir, _ = world(tmp_path)
    m = load_script()
    m.cmd_status(ns(root, out=str(tmp_path / "out")))
    assert "取得は今: 止まっている" in capsys.readouterr().out
    hold = C.history_hold(ir)
    try:
        m.cmd_status(ns(root, out=str(tmp_path / "out")))
        assert "取得は今: 動いている" in capsys.readouterr().out
    finally:
        C.history_release(hold)


# --------------------------------------------------------------------------- 速さと、使う場所を測る（2026-11-24: 見込みより 4 倍遅く、30 倍大きかった）
def test_disk_use_counts_both_the_content_and_the_room_on_the_disk(tmp_path):
    """中身の大きさと、ディスクの上で使っている場所は違う（形式によっては、小さなファイルも 1 つで何 MB も使う）。両方を数える。"""
    import os

    d = tmp_path / "x"
    (d / "2026-11").mkdir(parents=True)
    (d / "2026-10").mkdir()
    (d / "2026-11" / "a.pdf").write_bytes(b"1" * 3000)
    (d / "2026-10" / "b.pdf").write_bytes(b"1" * 10)
    (d / "2026-10" / "c.txt").write_text("x")
    (d / "2026-10" / "d.part").write_text("x")
    got = C.disk_use(d, ".pdf", big=1000)
    assert got["n"] == 2 and got["size"] == 3010 and got["biggest"] == 3000
    assert got["big_n"] == 1 and got["big_size"] == 3000                       # 大きいもの（ここでは 1000 バイトより上）が、いくつで、どれだけを占めるか
    assert got["used"] == sum(os.stat(f).st_blocks * 512 for f in d.rglob("*.pdf"))
    assert len(got["times"]) == 2 and got["times"] == sorted(got["times"])
    assert C.disk_use(d, ".txt")["n"] == 1
    assert C.disk_use(tmp_path / "none", ".pdf") == {"n": 0, "size": 0, "used": 0, "biggest": 0, "big_n": 0, "big_size": 0, "times": []}


def test_pace_is_the_usual_gap_between_files():
    times = [1000.0 + 4 * k for k in range(100)]                 # 4 秒に 1 件
    assert C.pace_seconds(times) == 4
    paused = times[:50] + [x + 3600 for x in times[50:]]         # 途中で 1 時間休んでも（毎朝の休み・止めていた間）、ふだんの間隔は変わらない
    assert C.pace_seconds(paused) == 4
    assert C.pace_seconds(times[:5]) is None                     # 少なすぎて分からない


def test_span_reads_like_a_person_would_say_it():
    assert C.span_text(50 * 60) == "50 分" and C.span_text(26.5 * 3600) == "26.5 時間" and C.span_text(4.3 * 86400) == "4.3 日"


def test_status_tells_the_speed_and_the_room_the_past_texts_take(tmp_path, capsys, monkeypatch, fast):
    """進み具合を聞かれたら、今の速さ・残りの時間・実際に使っている場所・この先の見当も答える（決めるための数字）。"""
    root, _, disk = world(tmp_path)
    (disk / "ir_pdf").mkdir()
    gb, mb = 2 ** 30, 2 ** 20
    use = {".pdf": {"n": 1000, "size": 6 * gb, "used": 10 * gb, "biggest": 48 * mb, "big_n": 150, "big_size": 4 * gb,
                    "times": [1000.0 + 4 * k for k in range(500)]},
           ".txt": {"n": 2821, "size": 15 * mb, "used": 11 * gb, "biggest": 24000, "big_n": 0, "big_size": 0, "times": []}}
    monkeypatch.setattr(C, "disk_use", lambda folder, suffix: use[suffix])
    load_script().cmd_status(ns(root, out=str(tmp_path / "out")))
    out = capsys.readouterr().out
    assert "残り 3 件" in out
    assert "速さ: 1 件 4.0 秒" in out and "1 時間に 900 件" in out and "この速さで、あと" in out
    assert "原本の PDF: 1,000 件・中身 6.0GB（1 件 6.1MB）・HDD の上で使っている場所 10.0GB（1 件 10MB）・いちばん大きいもの 48MB" in out
    assert "10MB を超えるものは 150 件（15%）で、中身の 67% を占める" in out          # 大きい書類を飛ばすかどうかを決めるための数字
    assert "本文: 2,821 件・中身 15MB（1 件 5KB）・HDD の上で使っている場所 11.0GB（1 件 4.0MB）" in out
    assert "残り 3 件で、あと 43MB ほど使う見当" in out                    # 3 × (10.2MB + 4.0MB)
    assert "HDD の空き" in out


def test_status_stays_short_when_nothing_was_taken_yet(tmp_path, capsys, fast):
    root, _, _ = world(tmp_path)
    load_script().cmd_status(ns(root, out=str(tmp_path / "out")))
    out = capsys.readouterr().out
    assert "過去の本文（2016-10〜）: 取ってある 0 件" in out and "原本の PDF" not in out and "速さ" not in out


def test_history_log_tells_the_real_pace(tmp_path, monkeypatch):
    """始めに出す時間は「いちばん速くて」の数字。区切りごとに、実際の速さと残りの時間を出す。"""
    root, _ = hdd(tmp_path)
    monkeypatch.setattr(C, "HISTORY_CHUNK", 2)
    logs = []
    run(root, lambda url: _pdf("a"), log=logs.append)
    line = next(s for s in logs if "過去の本文 2/3" in s)
    assert "1 件" in line and "秒（受け取り" in line and "この速さで、あと" in line


def test_pdf_reader_does_not_fill_the_log_with_notes_about_odd_files(caplog):
    """PDF の作りについての注意書き（Multiple definitions in dictionary…）が記録を埋めると、最後の行で様子が分からなくなる。"""
    import logging

    assert C.pdf_text(_pdf("hello")) == "hello"
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("pypdf._reader").warning("Multiple definitions in dictionary at byte 0x1 for key /Info")
    assert "Multiple definitions" not in caplog.text


# --------------------------------------------------------------------------- 取れなかった理由を残す・続けて取れないときは間をあける
# 2026-11-24: 実物で「続けて取れなかった」で止まったが、記録に理由が無く、サイトに断られたのか回線なのかが分からなかった
MANY = [(f"q{k:02d}", f"2024-01-{k + 1:02d} 15:00:00", "7203.T", "トヨタ", "お知らせ", "", "") for k in range(25)]


def test_history_waits_longer_after_each_failure_and_tells_why(tmp_path):
    """続けて取れないときは、間をあけてから次を試す（サイトが困っているときに、たたみかけない）。止まるときは、理由を伝える。"""
    root, _ = hdd(tmp_path)
    clock, at, logs = noon(), [], []

    def get(url):
        at.append(clock.t)
        if len(at) <= 6:
            raise http_error(url, 502)
        raise TimeoutError("timed out")

    got = run(root, get, rows=MANY, clock=clock, log=logs.append)
    assert got["stopped"] == "network" and len(at) == C.MAX_FAILS and got["left"] == 25
    assert [(at[k + 1] - at[k]).total_seconds() for k in range(len(at) - 1)] == [15, 30, 60, 120, 240, 300, 300, 300, 300]
    assert got["why"] == "HTTP 502 ×6、時間切れ ×4"
    assert sum("取れなかった（HTTP 502）" in s for s in logs) == 6 and not (root / ".docs.lock").exists()


def test_history_gets_through_a_short_outage(tmp_path):
    """少しの間の不調（Mac が眠りから覚めた直後・回線の一瞬の切れ）では止まらない。取れなかった分は、次の回にもう一度試す。"""
    root, _ = hdd(tmp_path)
    calls = []

    def get(url):
        calls.append(url)
        if 2 <= len(calls) <= 4:
            raise OSError("Network is unreachable")
        return _pdf("a")

    got = run(root, get, rows=MANY[:8])
    assert got["stopped"] is None and got["text"] == 5 and got["failed"] == 3 and got["left"] == 3 and "why" not in got


def test_history_gives_way_to_the_daily_collection_even_while_waiting(tmp_path):
    """間をあけて待っている最中でも、毎日の収集が来たら、すぐ錠を放して先に通す（決算の数字を取る回を待たせない）。"""
    import os

    root, _ = hdd(tmp_path)
    clock = noon()
    calls, seen = [], []

    def get(url):
        calls.append(url)
        if len(calls) <= 5:
            raise OSError("Network is unreachable")                 # 5 回続けて取れない → 次は 4 分待つところ
        return _pdf("a")

    real_sleep = clock.sleep

    def sleep(seconds):
        if len(calls) == 5 and not seen:
            (root / C.WANT_FILE).write_text(str(os.getpid()))       # 待っている最中に、毎日の収集が「先に取らせて」と頼んだ
            seen.append("asked")
        elif seen == ["asked"]:
            seen.append((seconds, C.docs_lock(root)))               # 次の待ちは「先に通す」の 30 秒で、その間は錠が空いている
            C.docs_unlock(root)
            (root / C.WANT_FILE).unlink(missing_ok=True)
        real_sleep(seconds)

    clock.sleep = sleep
    started = clock.t
    got = run(root, get, rows=MANY[:8], clock=clock)
    assert seen[1] == (30.0, True) and got["text"] == 3
    assert (clock.t - started).total_seconds() < 15 + 30 + 60 + 120 + 240       # 4 分の待ちを、最後まで待たずに切り上げた


def test_history_says_what_came_back_instead_of_a_pdf(tmp_path):
    root, _ = hdd(tmp_path)
    logs = []
    got = run(root, lambda url: b"<html><title>Access Denied</title>" if "p2" in url else _pdf("a"), log=logs.append)
    assert got["failed"] == 1 and any("PDF ではない" in s and "Access Denied" in s for s in logs)


def test_tool_tells_why_the_fetch_could_not_go_on(tmp_path, capsys, monkeypatch, fast):
    root, _, _ = world(tmp_path)
    monkeypatch.setattr(C, "MAX_FAILS", 2)

    def get(url, tries=1):
        raise http_error(url, 500)

    monkeypatch.setattr(C, "http_get", get)
    with pytest.raises(SystemExit) as stop:
        load_script().cmd_collect(ns(root))
    assert "続けて取れなかった" in str(stop.value) and "HTTP 500 ×2" in str(stop.value)
