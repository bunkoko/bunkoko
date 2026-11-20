"""束ねる検証の道具を、作ったデータで通しで動かす（cfdbot/bundle.py の登録の部分・scripts/bundle_study.py）。docs/bundle.md。"""

import json

import numpy as np
import pandas as pd
import pytest

from cfdbot import bundle as B
from cfdbot.wide.base import NotReady


@pytest.fixture(autouse=True)
def _before_record(monkeypatch):
    """道具を通しで動かす試験は、記録する前の形（RESULT が空）で動かす。"""
    monkeypatch.setattr(B, "RESULT", {})


# 登録の表（docs/bundle.md 2.2）に合わせた期間。月ごとの元データ（G・H・I）と記録の終わり（E・F）は、1 か月早く終わる形にしてある
SPANS = {"EA": ("2001-01", "2026-10"), "C2": ("2014-11", "2026-10"), "carry": ("1986-03", "2024-03"), "combo": ("1987-01", "2024-03"),
         "M2": ("2010-01", "2026-10"), "M2a": ("2010-01", "2026-10"), "M2b": ("2010-01", "2026-10"), "E1": ("2016-10", "2026-09"),
         "E2": ("2019-01", "2026-09"), "J7": ("2009-10", "2026-09"), "C3": ("1990-07", "2026-09"), "C1": ("1927-01", "2026-09"),
         "S1": ("1927-01", "2026-09"), "W7": ("1999-08", "2026-09"), "1C": ("2017-07", "2026-10"), "M1": ("2010-01", "2026-10")}


def month_ends(start, end):
    return pd.period_range(start, end, freq="M").to_timestamp(how="end").normalize()


def write_series(folder, keys, drift=0.004, seed=0, spans=None, sealed=True):
    """登録した名前の系列を、乱数で作って書く（平均 drift、月のばらつき 2%）。返すのは書いた系列。sealed なら、書き出しの終わった印も書く。"""
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    out = {}
    for k in keys:
        idx = month_ends(*(spans or {}).get(k, SPANS[k]))
        d = pd.Series(drift + 0.02 * rng.standard_normal(len(idx)), index=idx)
        pd.DataFrame({"month": [f"{x:%Y-%m-%d}" for x in d.index], "d": d.to_numpy()}).to_csv(folder / f"{k}.csv", index=False)
        out[k] = d
    if sealed:
        B.seal(folder, made="2026-11-21 09:00")
    return out


ALL = [m for ms in B.FAMILIES.values() for m in ms]


def test_the_registered_list_is_eleven_families_and_sixteen_series():
    assert len(B.FAMILIES) == 11 and len(ALL) == 16 and len(set(ALL)) == 16
    assert set(B.FAMILY_NAMES) == set(B.FAMILIES) and set(B.SERIES_NAMES) == set(ALL)
    for sub in B.SUBS.values():                                  # 小さな束は、登録した系列だけで作る
        assert all(f in B.FAMILIES and set(ms) <= set(B.FAMILIES[f]) for f, ms in sub.items())
    assert len(B.SUBS) == 3


def test_load_series_reads_the_registered_files_and_tells_which_are_missing(tmp_path):
    wrote = write_series(tmp_path, ["EA", "C3"])
    (tmp_path / "ZZ.csv").write_text("month,d\n2020-01-31,9\n")   # 登録に無い名前のファイルは読まない
    series, missing = B.load_series(tmp_path)
    assert set(series) == {"EA", "C3"} and set(missing) == set(ALL) - {"EA", "C3"}
    assert series["EA"].tolist() == pytest.approx(wrote["EA"].tolist()) and series["EA"].index.equals(wrote["EA"].index)


def test_load_series_rejects_a_file_that_is_not_month_ends(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "EA.csv").write_text("month,d\n2020-01-15,0.01\n2020-02-29,0.01\n")
    with pytest.raises(NotReady, match="月末"):
        B.load_series(tmp_path)


@pytest.mark.parametrize("rows", ["2020-02-29,0.01\n2020-01-31,0.01\n", "2020-01-31,0.01\n2020-01-31,0.02\n"])
def test_load_series_rejects_months_out_of_order_or_twice(tmp_path, rows):
    (tmp_path / "EA.csv").write_text("month,d\n" + rows)
    with pytest.raises(NotReady, match="古い順"):
        B.load_series(tmp_path)


def test_load_series_uses_no_month_after_november_2026(tmp_path):
    """B1 は 2026-11 までの月だけ（いつ計算しても、B2 の月と重ならない）。"""
    write_series(tmp_path, ["EA"], spans={"EA": ("2001-01", "2027-03")})
    series, _ = B.load_series(tmp_path)
    assert series["EA"].index[-1] == pd.Timestamp("2026-11-30") and len(series["EA"]) == len(month_ends("2001-01", "2026-11"))


# --------------------------------------------------------------------------- 点検（--check）
def test_check_shows_shapes_but_never_a_return(tmp_path):
    wrote = write_series(tmp_path, ALL)
    ok, lines = B.check(tmp_path)
    text = "\n".join(lines)
    assert ok and "11 家族" in text and "EA: 2001-01〜2026-10（310 か月" in text and "2010-10〜2026-09（192 か月）" in text
    assert "%" not in text and not any(x.startswith("⚠") for x in lines)
    for d in wrote.values():                                      # 成績の数字（平均）は 1 つも出さない
        assert f"{d.mean():.4f}" not in text and f"{d.mean() * 100:.2f}" not in text


def test_check_needs_the_mark_that_the_export_finished(tmp_path):
    write_series(tmp_path, ALL, sealed=False)                     # 書き出しが途中で止まった形（印が無い）
    ok, lines = B.check(tmp_path)
    assert not ok and "書き出し" in "\n".join(lines)


def test_check_notices_a_series_file_changed_after_the_export(tmp_path):
    write_series(tmp_path, ALL)
    f = tmp_path / "EA.csv"
    f.write_text(f.read_text() + "2026-11-30,0.01\n")
    ok, lines = B.check(tmp_path)
    assert not ok and "EA" in "\n".join(lines) and "変わ" in "\n".join(lines)


def test_check_notices_a_series_file_that_appeared_after_the_export(tmp_path):
    write_series(tmp_path, [k for k in ALL if k != "W7"])         # 印には「W7 は作れなかった」と書いてある
    write_series(tmp_path, ["W7"], sealed=False)                  # 後から置かれたファイル
    ok, lines = B.check(tmp_path, without=("W7",))
    assert not ok and "W7" in "\n".join(lines)


def test_check_counts_the_idle_months(tmp_path):
    wrote = write_series(tmp_path, ALL, sealed=False)
    d = wrote["J7"].copy()
    d.iloc[-30:] = 0.0
    d.iloc[10:15] = 0.0
    pd.DataFrame({"month": [f"{x:%Y-%m-%d}" for x in d.index], "d": d.to_numpy()}).to_csv(tmp_path / "J7.csv", index=False)
    B.seal(tmp_path, made="2026-11-21 09:00")
    ok, lines = B.check(tmp_path)
    line = next(x for x in lines if "・J7:" in x)
    assert ok and "0 の月 35" in line and "最後の 36 か月に 30" in line
    assert "0 の月" not in next(x for x in lines if "・EA:" in x)  # 0 の月が無い系列には書かない


def test_check_warns_when_a_series_does_not_look_like_the_registered_one(tmp_path):
    write_series(tmp_path, ALL, spans={"EA": ("2021-06", "2026-10"), "E1": ("2016-10", "2026-03"), "carry": ("1986-03", "2022-01")})
    ok, lines = B.check(tmp_path)
    warns = [x for x in lines if x.startswith("⚠")]
    assert ok and len(warns) == 3                                 # ⚠ があっても形は通る（進めるかは、道具の --warnings-seen で決める）
    assert "EA" in warns[0] and "始まり" in warns[0] and "2001-01" in warns[0]
    assert "carry" in warns[1] and "終わり" in warns[1] and "2024-03" in warns[1]
    assert "E1" in warns[2] and "終わり" in warns[2]


def test_check_allows_the_usual_small_differences_from_the_registered_table(tmp_path):
    write_series(tmp_path, ALL, spans={"EA": ("2001-11", "2026-10"), "C2": ("2015-08", "2026-10"), "E1": ("2016-10", "2026-07"),
                                       "combo": ("1987-01", "2024-01")})
    ok, lines = B.check(tmp_path)
    assert ok and not any(x.startswith("⚠") for x in lines)       # 始まりは 12 か月、終わりは 3 か月までのずれは見逃す


def test_check_stops_at_missing_series_unless_they_are_named(tmp_path):
    write_series(tmp_path, [k for k in ALL if k not in ("W7", "1C")])
    ok, lines = B.check(tmp_path)
    assert not ok and "--without W7,1C" in "\n".join(lines)
    ok, lines = B.check(tmp_path, without=("1C", "W7"))
    assert ok and "9 家族" in "\n".join(lines)
    assert not B.check(tmp_path, without=("W7",))[0]              # 名指しが足りない
    assert not B.check(tmp_path, without=("W7", "1C", "EA"))[0]   # ある系列は外せない
    assert not B.check(tmp_path, without=("W7", "1C", "XX"))[0]   # 登録に無い名前


def test_check_fails_with_fewer_than_eight_families(tmp_path):
    keys = ["EA", "C2", "carry", "M2", "E1", "J7", "C3"]          # 7 家族
    write_series(tmp_path, keys)
    ok, lines = B.check(tmp_path, without=tuple(k for k in ALL if k not in keys))
    assert not ok and "8" in "\n".join(lines)


def test_check_fails_when_the_bundle_would_be_shorter_than_sixty_months(tmp_path):
    write_series(tmp_path, ALL, spans={k: ("2021-01", "2026-10") for k in ALL})      # どれも 70 か月 → 束は 58 か月
    ok, lines = B.check(tmp_path)
    assert not ok and "60" in "\n".join(lines)


# --------------------------------------------------------------------------- 計算（B.run）
@pytest.fixture(scope="module")
def res_all(tmp_path_factory):
    """登録の形の 16 本で 1 回だけ計算したもの（読むだけの試験で使い回す）。"""
    series = write_series(tmp_path_factory.mktemp("series"), ALL)
    return series, B.run(series, n_worlds=300, n_worlds_fam=20)


def test_run_refuses_fewer_than_eight_families(tmp_path):
    series = write_series(tmp_path, ["EA", "C2", "carry", "M2", "E1", "J7", "C3"])
    with pytest.raises(NotReady, match="家族"):
        B.run(series, n_worlds=200, n_worlds_fam=5)


def test_run_stops_before_computing_when_the_bundle_is_shorter_than_sixty_months(tmp_path):
    series = write_series(tmp_path, ALL, spans={k: ("2021-01", "2026-10") for k in ALL})
    with pytest.raises(NotReady, match="60"):
        B.run(series, n_worlds=100, n_worlds_fam=5)


def test_run_judges_one_bundle_and_adds_the_checks(res_all):
    _, res = res_all
    assert res["verdict"] in (B.BEYOND, B.LOOKS_GOOD, B.USELESS)
    assert res["stats"]["n"] == 192 and f"{res['start']:%Y-%m}" == "2010-10" and f"{res['end']:%Y-%m}" == "2026-09"
    assert res["yard"]["n_months"] == 192 and res["yard"]["q95"] > res["yard"]["q50"] > 0
    assert res["yard2"]["q95"] > res["yard2"]["q50"] > 0 and res["yard2"]["n_worlds"] == 20
    assert res["line"] == max(res["yard"]["q95"], res["yard2"]["q95"])
    assert list(res["corr"].index) == list(B.FAMILIES) and res["loo"].shape[0] == 11 and set(res["subs"]) == set(B.SUBS)
    assert res["best_family"] in B.FAMILIES and isinstance(res["lift"], bool)
    assert res["trial"] == 101 and res["n_families"] == 11 and res["missing"] == []


def test_the_checks_of_a_run_are_counted_on_the_months_of_the_bundle(res_all):
    _, res = res_all
    assert (res["loo"]["n"] == 192).all()
    assert res["subs"]["株以外の柱だけ"]["n"] == 192              # A・B・C・I の半分（2 家族）は、束のどの月にもそろっている（古い月は足さない）
    assert res["subs"]["株の柱だけ"]["n"] == 189                  # D・E・F・G・J・K の半分（3 家族）がそろうのは 2011-01 から
    assert all(st["n"] <= 192 for st in res["subs"].values())


def test_the_lift_compares_the_bundle_and_the_best_family_on_the_same_months(res_all):
    _, res = res_all
    best = res["best_family"]
    same = res["family_frame"][best].dropna().index
    assert res["families"][best]["n"] == len(same)
    assert res["lift_t"] == pytest.approx(B.stats(res["bundle"].reindex(same))["t"])
    assert res["lift"] == (res["lift_t"] > res["families"][best]["t"])


def test_run_keeps_what_is_needed_to_check_it_later(res_all):
    series, res = res_all
    assert len(res["bundle"]) == 192 and res["weights"].index.equals(res["bundle"].index) and res["usable"].index.equals(res["bundle"].index)
    assert np.allclose(res["weights"].sum(axis=1), 1.0) and res["usable"].min() >= 6
    d = pd.DataFrame(series).reindex(res["bundle"].index)[list(res["weights"].columns)]
    assert np.allclose((res["weights"] * d).sum(axis=1), res["bundle"])           # 束の値 ＝ 重さ × 系列の値 の合計
    assert set(res["sigma_b2"]) == set(ALL) and all(v > 0 for v in res["sigma_b2"].values())
    assert list(res["family_frame"].columns) == list(B.FAMILIES) and res["family_frame"].index.equals(res["bundle"].index)


def test_run_with_nine_families_needs_five_of_them(tmp_path):
    series = write_series(tmp_path, [k for k in ALL if k not in ("C3", "C1", "S1")])
    res = B.run(series, n_worlds=100, n_worlds_fam=5)
    assert res["n_families"] == 9 and res["missing"] == ["C3", "C1", "S1"]
    assert f"{res['start']:%Y-%m}" == "2011-01" and res["usable"].min() >= 5      # 9 家族の半分 = 5 家族がそろうのは 2011-01 から


def test_a_strong_bundle_is_beyond_chance_and_a_dead_one_is_useless(tmp_path):
    strong = B.run(write_series(tmp_path / "a", ALL, drift=0.02), n_worlds=300, n_worlds_fam=20)      # どの柱も月 2%（ばらつき 2%）
    dead = B.run(write_series(tmp_path / "b", ALL, drift=-0.01), n_worlds=300, n_worlds_fam=20)
    assert strong["verdict"] == B.BEYOND and dead["verdict"] == B.USELESS


def test_the_verdict_needs_both_yardsticks(tmp_path, monkeypatch, res_all):
    series, res = res_all
    t = res["stats"]["t"]
    assert res["stats"]["t2"] >= 1.65 and res["stats"]["mean1"] > 0   # 物差しのほかの条件は満たしている束
    y1, y2 = B.yardstick, B.yardstick_families

    def verdict_with(q1, q2):
        monkeypatch.setattr(B, "yardstick", lambda *a, **k: {**y1(*a, **k), "q95": q1})
        monkeypatch.setattr(B, "yardstick_families", lambda *a, **k: {**y2(*a, **k), "q95": q2})
        return B.run(series, n_worlds=100, n_worlds_fam=5)["verdict"]

    assert verdict_with(t - 0.1, t - 0.1) == B.BEYOND
    assert verdict_with(t + 0.1, t - 0.1) == B.LOOKS_GOOD         # 物差し 1 に届かない
    assert verdict_with(t - 0.1, t + 0.1) == B.LOOKS_GOOD         # 物差し 2 に届かない


def test_report_warns_that_the_members_were_picked(res_all):
    _, res = res_all
    lines = B.report_lines(res, "2026-12-01 10:00")
    text = "\n".join(lines)
    assert res["verdict"] in text and "物差し 1" in text and "物差し 2" in text and "でたらめ" in text
    assert "相関" in text and "抜いた" in text and all(name in text for name in B.SUBS)
    assert any(x.startswith("## 2.") for x in lines) and "192.0" not in text
    sigma = text[text.index("B2 で使う"):]
    assert all(f"| {k} |" in sigma for k in ALL)                  # B2 で固定する値動きの大きさは、16 本とも表に出す


def test_mark_then_done(tmp_path, res_all):
    _, res = res_all
    marker = tmp_path / "B1_DONE.json"
    assert B.done(marker) is None
    B.mark(marker, res, ["x"], "2026-12-01 10:00", inputs={"EA": "abc"}, without=["W7"])
    rec, raw = B.done(marker), json.loads(marker.read_text(encoding="utf-8"))
    assert rec["verdict"] == res["verdict"] and rec["trial"] == 101 and raw["report"] == ["x"]
    assert raw["inputs"] == {"EA": "abc"} and raw["without"] == ["W7"] and set(raw["sigma_b2"]) == set(ALL)


# --------------------------------------------------------------------------- 道具（scripts/bundle_study.py）を通しで
def load_script():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "bundle_study.py"
    spec = importlib.util.spec_from_file_location("bundle_study_under_test", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def few_worlds(monkeypatch):
    monkeypatch.setattr(B, "N_WORLDS", 300)                      # 試験では物差しの世界の数を減らす（速さのため。決まりは 2 万と 5,000）
    monkeypatch.setattr(B, "N_WORLDS_FAM", 20)


def series_dir(root):
    return root / "data" / "bundle" / "series"


def test_tool_check_prints_shapes_and_computes_nothing(tmp_path, capsys):
    write_series(series_dir(tmp_path), ALL)
    m = load_script()
    assert m.main(["--check"], root=tmp_path) is None
    out = capsys.readouterr().out
    assert "11 家族" in out and "何も計算していない" in out and not (tmp_path / B.MARKER).exists()


def test_tool_runs_once_and_then_refuses(tmp_path, capsys, few_worlds):
    write_series(series_dir(tmp_path), ALL)
    m = load_script()
    path = m.main(["--out", str(tmp_path / "out")], root=tmp_path)
    rec = json.loads((tmp_path / B.MARKER).read_text(encoding="utf-8"))
    assert path.read_text(encoding="utf-8").startswith("# 束ねる検証 B1") and rec["verdict"] in (B.BEYOND, B.LOOKS_GOOD, B.USELESS)
    assert rec["verdict"] in capsys.readouterr().out
    with pytest.raises(SystemExit, match="もう 1 回計算してある"):
        m.main([], root=tmp_path)


def test_tool_keeps_copies_of_what_it_computed(tmp_path, few_worlds):
    """1 回だけの計算なので、後で確かめられるように、束の月の系列・重さ・家族の系列・入力の写しを残す。"""
    wrote = write_series(series_dir(tmp_path), ALL)
    folder = load_script().main(["--out", str(tmp_path / "out")], root=tmp_path).parent
    b, w = pd.read_csv(folder / "bundle.csv"), pd.read_csv(folder / "weights.csv")
    f, s = pd.read_csv(folder / "families.csv"), pd.read_csv(folder / "series.csv")
    assert list(b.columns) == ["month", "d", "families"] and len(b) == 192 and b["families"].min() >= 6
    assert len(w) == 192 and set(w.columns) == {"month", *ALL} and set(f.columns) == {"month", *B.FAMILIES}
    assert set(s.columns) == {"month", *ALL} and s["EA"].dropna().tolist() == pytest.approx(wrote["EA"].tolist())
    rec = json.loads((tmp_path / B.MARKER).read_text(encoding="utf-8"))
    assert set(rec["inputs"]) == set(ALL) and all(len(v) == 64 for v in rec["inputs"].values())      # 入力のファイルの指紋
    assert set(rec["sigma_b2"]) == set(ALL) and rec["without"] == [] and rec["warnings"] == []


def test_tool_does_nothing_when_families_are_missing(tmp_path, few_worlds):
    keys = ["EA", "C2", "carry", "M2", "E1", "J7", "C3"]
    write_series(series_dir(tmp_path), keys)
    with pytest.raises(SystemExit, match="何も計算していない"):
        load_script().main(["--without", ",".join(k for k in ALL if k not in keys)], root=tmp_path)
    assert not (tmp_path / B.MARKER).exists()


def test_tool_stops_at_missing_series_unless_they_are_named(tmp_path, few_worlds):
    write_series(series_dir(tmp_path), [k for k in ALL if k not in ("W7", "1C")])
    m = load_script()
    with pytest.raises(SystemExit, match="何も計算していない"):
        m.main([], root=tmp_path)
    assert not (tmp_path / B.MARKER).exists()
    path = m.main(["--without", "W7,1C", "--out", str(tmp_path / "out")], root=tmp_path)
    rec = json.loads((tmp_path / B.MARKER).read_text(encoding="utf-8"))
    assert rec["without"] == ["W7", "1C"] and "9 家族" in path.read_text(encoding="utf-8")


def test_tool_stops_at_warnings_unless_they_were_seen(tmp_path, few_worlds):
    write_series(series_dir(tmp_path), ALL, spans={"EA": ("2021-06", "2026-10")})
    m = load_script()
    with pytest.raises(SystemExit, match="⚠"):
        m.main([], root=tmp_path)
    assert not (tmp_path / B.MARKER).exists()
    m.main(["--warnings-seen", "--out", str(tmp_path / "out")], root=tmp_path)
    rec = json.loads((tmp_path / B.MARKER).read_text(encoding="utf-8"))
    assert len(rec["warnings"]) == 1 and "EA" in rec["warnings"][0]


def test_tool_does_nothing_without_the_mark_that_the_export_finished(tmp_path, few_worlds):
    write_series(series_dir(tmp_path), ALL, sealed=False)
    with pytest.raises(SystemExit, match="何も計算していない"):
        load_script().main([], root=tmp_path)
    assert not (tmp_path / B.MARKER).exists()


def test_tool_refuses_when_the_result_is_already_written_in_the_program(tmp_path, monkeypatch, few_worlds):
    write_series(series_dir(tmp_path), ALL)
    monkeypatch.setattr(B, "RESULT", {"date": "2026-12-01", "verdict": B.LOOKS_GOOD, "trial": 101})
    with pytest.raises(SystemExit, match="もう 1 回計算してある"):
        load_script().main([], root=tmp_path)


def test_tool_export_writes_series_and_judges_nothing(tmp_path, capsys, monkeypatch):
    from cfdbot import bundle_export as X

    good = pd.Series([0.0123, -0.0045], index=pd.to_datetime(["2024-01-31", "2024-02-29"]))
    monkeypatch.setattr(X, "SOURCES", {"trend": lambda ctx: {"EA": good}, "carry": lambda ctx: (_ for _ in ()).throw(X.ExportError("データが無い"))})
    monkeypatch.setattr(X, "NEEDS", {})                          # 作った道具なので、元データのファイルは要らない
    m = load_script()
    assert m.main(["--export"], root=tmp_path) is None
    out = capsys.readouterr().out
    assert (tmp_path / B.SERIES_DIR / "EA.csv").exists() and not (tmp_path / B.MARKER).exists()
    assert "EA: 書いた" in out and "carry: 作れなかった" in out and "combo: 作れなかった" in out and "--check" in out
    assert sum(f"- {k}: " in out for k in ALL) == 16              # 16 本すべての様子を出す
    assert "0.0123" not in out and "1.23" not in out


def test_tool_export_asks_to_run_again_when_an_input_was_rewritten_meanwhile(tmp_path, monkeypatch, capsys):
    from cfdbot import bundle_export as X

    monkeypatch.setattr(X, "export", lambda root, **k: ({m: "書いた: 2024-01〜2024-02（2 か月）" for m in ALL}, ["data/stocks/jp_close.csv"]))
    with pytest.raises(SystemExit, match="やり直す"):
        load_script().main(["--export"], root=tmp_path)
    assert "data/stocks/jp_close.csv" in capsys.readouterr().out


def test_tool_export_is_refused_after_the_judgement(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "RESULT", {"date": "2026-12-01", "verdict": B.LOOKS_GOOD, "trial": 101})
    with pytest.raises(SystemExit, match="もう 1 回計算してある"):
        load_script().main(["--export"], root=tmp_path)
