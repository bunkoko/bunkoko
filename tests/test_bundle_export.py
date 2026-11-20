"""柱ごとの系列の書き出し（cfdbot/bundle_export.py）。元の道具の出力と同じ形のファイルを作って、拾い方・そろえ方・書き方を確かめる。"""

import sys

import pandas as pd
import pytest

from cfdbot import bundle as B
from cfdbot import bundle_export as X


def ts(*days):
    return pd.to_datetime(list(days))


def daily_csv(path, column, days, values, tz="UTC", extra=None):
    """道具が書く日ごとの表（1 列目が日付。時刻の帯つき）。"""
    idx = pd.DatetimeIndex(ts(*days))
    idx = idx.tz_localize(tz) if tz else idx
    df = pd.DataFrame({column: values, **(extra or {})}, index=idx)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path)


def d_csv(path, days, values):
    """株の道具が書く d_<記号>.csv（1 列目が日付、列 d）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.Series(values, index=ts(*days)).to_csv(path, header=["d"])


def ym(s):
    return [f"{x:%Y-%m}" for x in s.index]


# --------------------------------------------------------------------------- そろえる・書く
def test_write_series_round_trips_through_load(tmp_path):
    d = pd.Series([0.01, -0.02], index=ts("2024-01-31", "2024-02-29"))
    X.write_series(tmp_path, "EA", d)
    got, _ = B.load_series(tmp_path)
    assert got["EA"].tolist() == [0.01, -0.02] and list(got["EA"].index) == list(d.index)


def test_monthly_moves_dates_to_month_ends_and_drops_the_unfinished_month():
    d = pd.Series([0.01, 0.02, 0.03], index=ts("2024-01-31", "2024-02-28", "2024-03-29"))
    m = X.monthly(d, last_day="2024-03-15")                      # データは 3 月の途中まで → 3 月は使わない
    assert list(m.index) == list(ts("2024-01-31", "2024-02-29")) and m.tolist() == [0.01, 0.02]
    assert len(X.monthly(d)) == 3                                 # 最後の日を渡さなければ、月はそのまま


def test_monthly_rejects_two_values_in_one_month():
    with pytest.raises(X.ExportError, match="同じ月"):
        X.monthly(pd.Series([0.01, 0.02], index=ts("2024-01-10", "2024-01-31")))


# --------------------------------------------------------------------------- 道具の出力を拾う
def test_trend_joins_period_a_and_b(tmp_path):
    daily_csv(tmp_path / "returns_A.csv", "base", ["2021-04-29", "2021-05-28", "2021-05-31"], [0.0, 0.01, 0.02], extra={"multi": 9.0})
    daily_csv(tmp_path / "returns_B.csv", "base", ["2021-06-01", "2021-06-30", "2021-07-01"], [0.03, 0.04, 0.5])
    got = X.pick_trend(tmp_path)
    assert set(got) == {"EA"} and ym(got["EA"]) == ["2021-05", "2021-06"]         # 4 月は始まる前（0）、7 月は終わっていない
    assert got["EA"].tolist() == pytest.approx([0.03, 0.07])


def test_trend_needs_period_b(tmp_path):
    daily_csv(tmp_path / "returns_A.csv", "base", ["2021-05-28", "2021-05-31"], [0.01, 0.02])
    with pytest.raises(X.ExportError, match="期間B"):
        X.pick_trend(tmp_path)


def test_trend_needs_period_a(tmp_path):
    """期間A（Yahoo の先物）の成績が空なら、黙って期間B だけの短い系列にしない（20 年分が消える）。"""
    daily_csv(tmp_path / "returns_A.csv", "base", ["2021-05-28", "2021-05-31"], [float("nan"), float("nan")])
    daily_csv(tmp_path / "returns_B.csv", "base", ["2021-06-01", "2021-06-30", "2021-07-01"], [0.03, 0.04, 0.5])
    with pytest.raises(X.ExportError, match="期間A"):
        X.pick_trend(tmp_path)


def test_carry_reads_the_ret_columns_of_the_cfd_files(tmp_path):
    for name, v in (("carry", 0.01), ("combo", 0.02), ("trend", 0.03)):
        daily_csv(tmp_path / f"{name}_cfd.csv", "ret", ["2020-01-06", "2020-01-31", "2020-02-03"], [v, v, 0.5], tz=None, extra={"pos": 1})
        daily_csv(tmp_path / f"{name}_futures.csv", "ret", ["2020-01-06", "2020-01-31", "2020-02-03"], [9.0, 9.0, 9.0], tz=None)
    got = X.pick_carry(tmp_path)
    assert set(got) == {"carry", "combo"}                         # trend（12 か月の向きだけ）は柱ではない。先物の版も使わない
    assert got["carry"].tolist() == pytest.approx([0.02]) and got["combo"].tolist() == pytest.approx([0.04])


def test_crypto_reads_the_gmo_series(tmp_path):
    daily_csv(tmp_path / "returns_crypto_gmo.csv", "ret", ["2020-01-06", "2020-01-31", "2020-02-03"], [0.01, 0.02, 0.5])
    daily_csv(tmp_path / "returns_crypto_bitflyer.csv", "ret", ["2020-01-06", "2020-01-31", "2020-02-03"], [9.0, 9.0, 9.0])
    got = X.pick_crypto(tmp_path)
    assert set(got) == {"C2"} and got["C2"].tolist() == pytest.approx([0.03])


def test_stocks_reads_three_monthly_files(tmp_path):
    for k, v in (("C3", 0.01), ("C1", 0.02), ("S1", 0.03), ("T1", 9.0)):
        d_csv(tmp_path / f"d_{k}.csv", ["2020-01-31", "2020-02-29"], [v, v])
    got = X.pick_stocks(tmp_path)
    assert set(got) == {"C3", "C1", "S1"} and got["C1"].tolist() == [0.02, 0.02] and ym(got["C3"]) == ["2020-01", "2020-02"]


def test_shortterm_drops_the_month_that_is_not_finished(tmp_path):
    for k in ("M2", "M1"):
        d_csv(tmp_path / f"d_{k}.csv", ["2026-09-30", "2026-10-31", "2026-11-30"], [0.01, 0.02, 0.5])
    got = X.pick_shortterm(tmp_path, last_day="2026-11-18")       # 値段は 11 月の途中まで
    assert set(got) == {"M2", "M1"} and ym(got["M2"]) == ["2026-09", "2026-10"]


def test_agents_reads_the_daily_files_that_exist(tmp_path):
    for k in ("M2a", "M2b", "E1", "J7"):                          # E2 は作れなかった回
        daily_csv(tmp_path / f"d_{k}.csv", "d", ["2020-01-06", "2020-01-31", "2020-02-03"], [0.01, 0.02, 0.5], tz=None)
    got = X.pick_agents(tmp_path)
    assert set(got) == {"M2a", "M2b", "E1", "J7"} and got["J7"].tolist() == pytest.approx([0.03])


def test_text_reads_the_monthly_book(tmp_path):
    d_csv(tmp_path / "d_1C.csv", ["2026-09-30", "2026-10-30", "2026-11-28"], [0.01, 0.02, 0.5])
    got = X.pick_text(tmp_path, last_day="2026-11-18")
    assert set(got) == {"1C"} and ym(got["1C"]) == ["2026-09", "2026-10"]


# --------------------------------------------------------------------------- 道具を動かす
def test_run_tool_returns_the_new_output_folder_and_keeps_the_log(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "fake.py").write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--out') + 1]) / '20261120-0900'\n"
        "out.mkdir(parents=True)\n(out / 'x.csv').write_text('a')\nprint('成績 12.3%')\n", encoding="utf-8")
    folder = X.run_tool(tmp_path, "fake.py", [], tmp_path / "o", tmp_path / "fake.log")
    assert (folder / "x.csv").read_text() == "a" and "成績 12.3%" in (tmp_path / "fake.log").read_text(encoding="utf-8")


def test_run_tool_reports_a_failed_run_with_its_last_line(tmp_path):
    """止まった理由（道具の最後の行）を返す。記録を開かなくても分かるように（記録には、作り直した成績の表が入っている）。"""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "bad.py").write_text("import sys\nprint('成績 12.3%')\nsys.exit('値段のデータが無い')\n", encoding="utf-8")
    with pytest.raises(X.ExportError, match="bad.py") as e:
        X.run_tool(tmp_path, "bad.py", [], tmp_path / "o", tmp_path / "bad.log")
    assert "値段のデータが無い" in str(e.value) and "12.3" not in str(e.value)


def test_outputs_of_an_earlier_export_are_cleared_before_a_tool_runs(tmp_path):
    """前の書き出しの出力が残っていると、今回の道具が何も書かなかったときに古い数字を拾ってしまう。"""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "quiet.py").write_text("print('何も書かない')\n", encoding="utf-8")
    old = tmp_path / "work" / "quiet" / "20261101-0900"
    old.mkdir(parents=True)
    (old / "x.csv").write_text("古い")
    ctx = X.Ctx(root=tmp_path, work=tmp_path / "work", log=lambda s: None)
    with pytest.raises(X.ExportError, match="出力"):
        ctx.tool("quiet", "quiet.py")
    assert not old.exists()


def test_the_last_price_day_is_read_before_the_tool_runs(tmp_path, monkeypatch):
    """道具が動いている間に値段の表が新しくなっても、「終わっていない月」を古い側で決める（終わった月に半端な値を入れない）。"""
    calls = []
    monkeypatch.setattr(X, "jp_last_day", lambda root: calls.append("最後の日") or pd.Timestamp("2026-11-18"))
    monkeypatch.setattr(X.Ctx, "tool", lambda self, name, script, args=None: calls.append("道具") or tmp_path)
    for k in ("M2", "M1", "1C"):
        d_csv(tmp_path / f"d_{k}.csv", ["2026-09-30", "2026-10-31", "2026-11-30"], [0.01, 0.02, 0.5])
    ctx = X.Ctx(root=tmp_path, work=tmp_path / "work", log=lambda s: None)
    for name in ("shortterm", "edinet"):
        calls.clear()
        got = X.SOURCES[name](ctx)
        assert calls == ["最後の日", "道具"] and all(ym(s) == ["2026-09", "2026-10"] for s in got.values())


# --------------------------------------------------------------------------- まとめて書き出す
def test_export_writes_what_it_can_and_tells_what_failed(tmp_path):
    good = pd.Series([0.0123, -0.0045], index=ts("2024-01-31", "2024-02-29"))

    def fine(ctx):
        return {"EA": good, "ZZ": good}                           # ZZ は登録に無い名前

    def missing(ctx):
        raise X.ExportError("値段のファイルが無い")

    def broken(ctx):
        return 1 / 0

    logs = []
    status, changed = X.export(tmp_path, sources={"trend": fine, "carry": missing, "crypto": broken, "agents": lambda ctx: {"M2a": good}},
                               log=logs.append, needs={})
    series, _ = B.load_series(tmp_path / B.SERIES_DIR)
    assert set(series) == {"EA", "M2a"} and series["EA"].tolist() == pytest.approx(good.tolist()) and changed == []
    assert list(status) == list(B.SERIES_NAMES)                   # 登録の 16 本すべての様子を、登録の順で返す
    assert status["EA"].startswith("書いた") and "2024-01〜2024-02" in status["EA"] and "2 か月" in status["EA"]
    assert all("値段のファイルが無い" in status[k] for k in ("carry", "combo")) and "ZeroDivisionError" in status["C2"]
    assert all(status[k].startswith("作れなかった") for k in B.SERIES_NAMES if k not in ("EA", "M2a"))
    assert all("書かなかった" in status[k] for k in ("M2b", "E1", "E2", "J7"))     # 道具は動いたが、その系列は出てこなかった
    assert "動かしていない" in status["C3"]                        # 今回は道具を動かしていない系列
    text = "\n".join(logs) + "\n".join(status.values())
    assert "0.0123" not in text and "1.23" not in text            # 成績の数字は、画面にも記録にも出さない
    assert not any("ZZ" in k for k in status)


def test_export_passes_on_why_the_series_tool_skipped_a_series(tmp_path):
    """系列だけを書く道具（agents・edinet の series）は、作れなかった理由を 1 行で言う。記録を開かなくても分かるように、様子に入れる。"""
    good = pd.Series([0.01, 0.02], index=ts("2024-01-31", "2024-02-29"))

    def agents(ctx):
        ctx.work.mkdir(parents=True, exist_ok=True)
        (ctx.work / "agents.log").write_text("  M2a: 2010-01-04〜2026-11-17（4,100 日）を書いた\n  E1・E2: 作れない（decisions_numbers.csv か ir_numbers.csv が無い）\n"
                                             "  J7: 作れない（使える決算が無い）\n保存先: x\n", encoding="utf-8")
        return {"M2a": good, "M2b": good}

    status, _ = X.export(tmp_path, sources={"agents": agents, "stocks": lambda ctx: {"C3": good}}, log=lambda s: None, needs={})
    assert "decisions_numbers.csv" in status["E1"] and "decisions_numbers.csv" in status["E2"] and "使える決算が無い" in status["J7"]
    assert all(status[k].startswith("作れなかった") for k in ("E1", "E2", "J7")) and status["M2a"].startswith("書いた")
    assert "書かなかった" in status["C1"]                         # 判定の表を出す道具の記録は読まない（成績が入っている）


def test_the_sources_make_each_registered_series_exactly_once():
    made = [k for keys in X.MAKES.values() for k in keys]
    assert sorted(made) == sorted(B.SERIES_NAMES) and set(X.MAKES) == set(X.SOURCES)
    assert set(X.NEEDS) <= set(X.SOURCES)


def test_export_marks_the_end_only_when_it_ran_to_the_end(tmp_path):
    """途中で止まった書き出しの後で、柱の減ったまま判定に進まないように、最後まで終わったときだけ印を書く。"""
    good = pd.Series([0.01, 0.02], index=ts("2024-01-31", "2024-02-29"))
    folder = tmp_path / B.SERIES_DIR
    status, _ = X.export(tmp_path, sources={"trend": lambda ctx: {"EA": good}}, log=lambda s: None, made="2026-11-21 09:00", needs={})
    rec, problem = B.read_seal(folder)
    assert problem is None and rec["date"] == "2026-11-21 09:00" and rec["series"] == status and set(rec["sha256"]) == {"EA"}

    def stopped(ctx):
        raise KeyboardInterrupt                                    # 端末を閉じた・Ctrl-C

    with pytest.raises(KeyboardInterrupt):
        X.export(tmp_path, sources={"trend": lambda ctx: {"EA": good}, "carry": stopped}, log=lambda s: None, needs={})
    assert (folder / "EA.csv").exists() and B.read_seal(folder)[0] is None        # 前の回の印も消えている
    assert not B.check(folder)[0]


def test_export_does_not_mark_the_end_when_an_input_was_rewritten_meanwhile(tmp_path):
    """毎日の自動実行（値段・点数の書き換え）と重なったら、系列どうしの元データが食い違う。印を書かずに、やり直しを求める。"""
    prices = tmp_path / "data" / "stocks" / "jp_close.csv"
    prices.parent.mkdir(parents=True)
    prices.write_text("a\n1\n")
    good = pd.Series([0.01, 0.02], index=ts("2024-01-31", "2024-02-29"))

    def overlapped(ctx):
        prices.write_text("a\n1\n2\n")                          # 道具が動いている間に、値段の表に 1 日足された
        (tmp_path / "data" / "stocks" / "new.csv").write_text("x")  # 新しくできたファイルは数えない（書き換えではない）
        return {"EA": good}

    status, changed = X.export(tmp_path, sources={"trend": overlapped}, log=lambda s: None, needs={})
    assert changed == ["data/stocks/jp_close.csv"] and B.read_seal(tmp_path / B.SERIES_DIR)[0] is None
    assert status["EA"].startswith("書いた")                      # 系列は書くが、終わった印は書かない
    _, changed = X.export(tmp_path, sources={"trend": lambda ctx: {"EA": good}}, log=lambda s: None, needs={})
    assert changed == [] and B.read_seal(tmp_path / B.SERIES_DIR)[0] is not None


def test_export_does_not_run_a_tool_that_would_fetch_its_missing_input(tmp_path):
    """元の道具には、要るファイルが無いと自分で取り直すものがある（株の研究は銘柄の一覧と値段を全部取り直す）。書き出しでは取り直さない。"""
    called = []
    (tmp_path / "data" / "stocks").mkdir(parents=True)
    (tmp_path / "data" / "stocks" / "jp_close.csv").write_text("a\n1\n")
    status, _ = X.export(tmp_path, sources={"stocks": lambda ctx: called.append(1) or {}}, log=lambda s: None)
    assert called == [] and all("us_close.csv" in status[k] and "動かしていない" in status[k] for k in ("C3", "C1", "S1"))


def test_export_runs_under_the_python_that_is_running_the_tests():
    assert X.PYTHON == sys.executable


def test_export_starts_from_an_empty_series_folder(tmp_path):
    """前の回の系列が残っていると、作れなかった柱に古い数字が混ざる。毎回、登録した名前のファイルを消してから書く。"""
    folder = tmp_path / B.SERIES_DIR
    X.write_series(folder, "C2", pd.Series([0.5], index=ts("2020-01-31")))        # 前の回の残り
    (folder / "memo.txt").write_text("消さない")
    good = pd.Series([0.01, 0.02], index=ts("2024-01-31", "2024-02-29"))
    X.export(tmp_path, sources={"trend": lambda ctx: {"EA": good}}, log=lambda s: None, needs={})
    assert not (folder / "C2.csv").exists() and (folder / "EA.csv").exists() and (folder / "memo.txt").exists()


def test_run_tool_twice_in_the_same_minute_still_finds_the_output(tmp_path):
    """道具の出力のフォルダは「日時（分まで）」の名前。同じ分にもう一度動かすと同じフォルダに書くので、それを拾う。"""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "fake.py").write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--out') + 1]) / '20261120-0900'\n"
        "out.mkdir(parents=True, exist_ok=True)\n(out / 'x.csv').write_text('a')\n", encoding="utf-8")
    first = X.run_tool(tmp_path, "fake.py", [], tmp_path / "o", tmp_path / "fake.log")
    again = X.run_tool(tmp_path, "fake.py", [], tmp_path / "o", tmp_path / "fake.log")
    assert first == again and (again / "x.csv").exists()
