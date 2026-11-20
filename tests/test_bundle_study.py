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


def months(n, start="2008-01-31"):
    return pd.date_range(start, periods=n, freq="ME")


def write_series(folder, keys, n=200, drift=0.004, seed=0):
    """登録した名前の系列を、乱数で作って書く（平均 drift、月のばらつき 2%）。返すのは書いた系列。"""
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    out = {}
    for k in keys:
        d = pd.Series(drift + 0.02 * rng.standard_normal(n), index=months(n))
        pd.DataFrame({"month": [f"{x:%Y-%m-%d}" for x in d.index], "d": d.to_numpy()}).to_csv(folder / f"{k}.csv", index=False)
        out[k] = d
    return out


ALL = [m for ms in B.FAMILIES.values() for m in ms]


def test_the_registered_list_is_eleven_families_and_sixteen_series():
    assert len(B.FAMILIES) == 11 and len(ALL) == 16 and len(set(ALL)) == 16
    assert set(B.FAMILY_NAMES) == set(B.FAMILIES) and set(B.SERIES_NAMES) == set(ALL)
    for sub in B.SUBS.values():                                  # 小さな束は、登録した系列だけで作る
        assert all(f in B.FAMILIES and set(ms) <= set(B.FAMILIES[f]) for f, ms in sub.items())
    assert len(B.SUBS) == 3


def test_load_series_reads_the_registered_files_and_tells_which_are_missing(tmp_path):
    wrote = write_series(tmp_path, ["EA", "C3"], n=30)
    (tmp_path / "ZZ.csv").write_text("month,d\n2020-01-31,9\n")   # 登録に無い名前のファイルは読まない
    series, missing = B.load_series(tmp_path)
    assert set(series) == {"EA", "C3"} and set(missing) == set(ALL) - {"EA", "C3"}
    assert series["EA"].tolist() == pytest.approx(wrote["EA"].tolist()) and series["EA"].index.equals(wrote["EA"].index)


def test_load_series_rejects_a_file_that_is_not_month_ends(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "EA.csv").write_text("month,d\n2020-01-15,0.01\n2020-02-29,0.01\n")
    with pytest.raises(NotReady, match="月末"):
        B.load_series(tmp_path)


def test_check_shows_shapes_but_never_a_return(tmp_path):
    wrote = write_series(tmp_path, ALL, n=200)
    ok, lines = B.check(tmp_path)
    text = "\n".join(lines)
    assert ok and "11 家族" in text and "200 か月" in text and "2008-01〜2024-08" in text
    assert "%" not in text
    for d in wrote.values():                                      # 成績の数字（平均）は 1 つも出さない
        assert f"{d.mean():.4f}" not in text and f"{d.mean() * 100:.2f}" not in text


def test_check_fails_with_fewer_than_eight_families(tmp_path):
    write_series(tmp_path, ["EA", "C2", "carry", "M2", "E1", "J7", "C3"], n=200)     # 7 家族
    ok, lines = B.check(tmp_path)
    assert not ok and "8" in "\n".join(lines)


def test_run_refuses_fewer_than_eight_families(tmp_path):
    series = write_series(tmp_path, ["EA", "C2", "carry", "M2", "E1", "J7", "C3"], n=200)
    with pytest.raises(NotReady, match="家族"):
        B.run(series, n_worlds=200)


def test_run_judges_one_bundle_and_adds_the_checks(tmp_path):
    series = write_series(tmp_path, ALL, n=200, drift=0.004)
    res = B.run(series, n_worlds=300)
    assert res["verdict"] in (B.BEYOND, B.LOOKS_GOOD, B.USELESS)
    assert res["stats"]["n"] == 188                               # 200 か月のうち、最初の 12 か月は値動きの大きさを測るのに使う
    assert res["yard"]["n_months"] == 188 and res["yard"]["q95"] > res["yard"]["q50"] > 0
    assert list(res["corr"].index) == list(B.FAMILIES) and res["loo"].shape[0] == 11 and set(res["subs"]) == set(B.SUBS)
    assert res["best_family"] in B.FAMILIES and isinstance(res["lift"], bool)
    assert res["trial"] == 101


def test_a_strong_bundle_is_beyond_chance_and_a_dead_one_is_useless(tmp_path):
    strong = B.run(write_series(tmp_path / "a", ALL, n=200, drift=0.02), n_worlds=300)      # どの柱も月 2%（ばらつき 2%）
    dead = B.run(write_series(tmp_path / "b", ALL, n=200, drift=-0.01), n_worlds=300)
    assert strong["verdict"] == B.BEYOND and dead["verdict"] == B.USELESS


def test_report_warns_that_the_members_were_picked(tmp_path):
    res = B.run(write_series(tmp_path, ALL, n=200), n_worlds=300)
    lines = B.report_lines(res, "2026-12-01 10:00")
    text = "\n".join(lines)
    assert res["verdict"] in text and "物差し" in text and "でたらめでも" in text
    assert "相関" in text and "抜いた" in text and all(name in text for name in B.SUBS)
    assert any(x.startswith("## 2.") for x in lines)


def test_mark_then_done(tmp_path):
    res = B.run(write_series(tmp_path / "s", ALL, n=200), n_worlds=300)
    marker = tmp_path / "B1_DONE.json"
    assert B.done(marker) is None
    B.mark(marker, res, ["x"], "2026-12-01 10:00")
    rec = B.done(marker)
    assert rec["verdict"] == res["verdict"] and rec["trial"] == 101 and json.loads(marker.read_text(encoding="utf-8"))["report"] == ["x"]


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
    monkeypatch.setattr(B, "N_WORLDS", 300)                      # 試験では物差しの世界の数を減らす（速さのため。決まりは 2 万）


def test_report_shows_whole_numbers_of_months(tmp_path):
    res = B.run(write_series(tmp_path, ALL, n=200), n_worlds=300)
    assert "188.0" not in "\n".join(B.report_lines(res, "2026-12-01 10:00"))


def test_tool_check_prints_shapes_and_computes_nothing(tmp_path, capsys):
    write_series(tmp_path / "data" / "bundle" / "series", ALL, n=200)
    m = load_script()
    assert m.main(["--check"], root=tmp_path) is None
    out = capsys.readouterr().out
    assert "11 家族" in out and "何も計算していない" in out and not (tmp_path / B.MARKER).exists()


def test_tool_runs_once_and_then_refuses(tmp_path, capsys, few_worlds):
    write_series(tmp_path / "data" / "bundle" / "series", ALL, n=200)
    m = load_script()
    path = m.main(["--out", str(tmp_path / "out")], root=tmp_path)
    rec = json.loads((tmp_path / B.MARKER).read_text(encoding="utf-8"))
    assert path.read_text(encoding="utf-8").startswith("# 束ねる検証 B1") and rec["verdict"] in (B.BEYOND, B.LOOKS_GOOD, B.USELESS)
    assert rec["verdict"] in capsys.readouterr().out
    with pytest.raises(SystemExit, match="もう 1 回計算してある"):
        m.main([], root=tmp_path)


def test_tool_does_nothing_when_families_are_missing(tmp_path, few_worlds):
    write_series(tmp_path / "data" / "bundle" / "series", ["EA", "C2", "carry", "M2", "E1", "J7", "C3"], n=200)
    m = load_script()
    with pytest.raises(SystemExit, match="何も計算していない"):
        m.main([], root=tmp_path)
    assert not (tmp_path / B.MARKER).exists()


def test_tool_refuses_when_the_result_is_already_written_in_the_program(tmp_path, monkeypatch, few_worlds):
    write_series(tmp_path / "data" / "bundle" / "series", ALL, n=200)
    monkeypatch.setattr(B, "RESULT", {"date": "2026-12-01", "verdict": B.LOOKS_GOOD, "trial": 101})
    with pytest.raises(SystemExit, match="もう 1 回計算してある"):
        load_script().main([], root=tmp_path)


def test_tool_export_writes_series_and_judges_nothing(tmp_path, capsys, monkeypatch):
    from cfdbot import bundle_export as X

    good = pd.Series([0.0123, -0.0045], index=pd.to_datetime(["2024-01-31", "2024-02-29"]))
    monkeypatch.setattr(X, "SOURCES", {"trend": lambda ctx: {"EA": good}, "carry": lambda ctx: (_ for _ in ()).throw(X.ExportError("データが無い"))})
    m = load_script()
    assert m.main(["--export"], root=tmp_path) is None
    out = capsys.readouterr().out
    assert (tmp_path / B.SERIES_DIR / "EA.csv").exists() and not (tmp_path / B.MARKER).exists()
    assert "EA: 書いた" in out and "carry: 作れなかった" in out and "--check" in out
    assert "0.0123" not in out and "1.23" not in out


def test_tool_export_is_refused_after_the_judgement(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "RESULT", {"date": "2026-12-01", "verdict": B.LOOKS_GOOD, "trial": 101})
    with pytest.raises(SystemExit, match="もう 1 回計算してある"):
        load_script().main(["--export"], root=tmp_path)
