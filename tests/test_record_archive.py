"""先の記録のための控え（docs/agents.md 12.7）の試験。作ったデータだけを使う。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

from cfdbot import stocks as S

ROOT = Path(__file__).resolve().parents[1]


def _agents():
    spec = importlib.util.spec_from_file_location("agents_archive", ROOT / "scripts" / "agents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _prices(idx, cols, base=100.0):
    rng = np.random.default_rng(len(idx) + len(cols))
    c = pd.DataFrame(base * np.exp(np.cumsum(rng.normal(0, 0.01, (len(idx), len(cols))), axis=0)), index=idx, columns=cols)
    return c.shift(1).fillna(base), c


def test_archive_keeps_vanished_tickers_and_prefers_new_values(tmp_path):
    d1 = pd.bdate_range("2026-10-01", "2026-10-20")
    o1, c1 = _prices(d1, ["A.T", "B.T", "C.T"])
    c1.to_csv(tmp_path / "jp_close.csv")
    o1.to_csv(tmp_path / "jp_open.csv")
    S.archive_prices(tmp_path, o1, c1, now=pd.Timestamp("2026-10-20 18:00"))
    # 次の日: C は上場廃止で取れない。A の過去の値は配当で調整し直されて少し変わる。新しい会社 D が入る
    d2 = pd.bdate_range("2026-10-01", "2026-10-21")
    o2, c2 = _prices(d2, ["A.T", "B.T", "D.T"], base=50.0)
    c2.to_csv(tmp_path / "jp_close.csv")
    o2.to_csv(tmp_path / "jp_open.csv")
    S.archive_prices(tmp_path, o2, c2, now=pd.Timestamp("2026-10-21 18:00"))
    arch = pd.read_csv(tmp_path / "archive" / "jp_close.csv", index_col=0, parse_dates=True)
    assert set(arch.columns) == {"A.T", "B.T", "C.T", "D.T"}
    assert np.allclose(arch.loc[d1, "C.T"], c1["C.T"], rtol=1e-5)               # 消えた銘柄の過去は残る
    assert np.isnan(arch.loc[d2[-1], "C.T"])
    assert np.allclose(arch.loc[d2, "A.T"], c2["A.T"], rtol=1e-5)               # 新しく取った値が優先
    o, c = S.record_prices(tmp_path)
    assert set(c.columns) == {"A.T", "B.T", "C.T", "D.T"} and c.index[-1] == d2[-1]
    assert np.allclose(c["C.T"].dropna(), c1["C.T"], rtol=1e-5) and np.allclose(c["A.T"], c2["A.T"], rtol=1e-5)
    # 控えが無ければ今の値段だけ
    (tmp_path / "archive" / "jp_close.csv").unlink()
    (tmp_path / "archive" / "jp_open.csv").unlink()
    assert set(S.record_prices(tmp_path)[1].columns) == {"A.T", "B.T", "D.T"}


def test_archive_skips_the_midday_row_and_stale_archive_rows(tmp_path):
    d = pd.bdate_range("2026-10-01", "2026-10-21")
    o, c = _prices(d, ["A.T", "C.T"])
    S.archive_prices(tmp_path, o, c, now=pd.Timestamp("2026-10-21 13:05"))        # 場中: 10-21 の行は控えに入れない
    arch = pd.read_csv(tmp_path / "archive" / "jp_close.csv", index_col=0, parse_dates=True)
    assert arch.index[-1] == pd.Timestamp("2026-10-20")
    # 控えに、今の値段に無い日（古い休場日の行）があっても、今の銘柄にも消えた銘柄にも足さない
    stale = arch.copy()
    stale.loc[pd.Timestamp("2026-10-10")] = stale.iloc[0]
    stale.sort_index().to_csv(tmp_path / "archive" / "jp_close.csv")
    o.drop(columns="C.T").to_csv(tmp_path / "jp_open.csv")
    c.drop(columns="C.T").to_csv(tmp_path / "jp_close.csv")
    _, rc = S.record_prices(tmp_path)
    assert pd.Timestamp("2026-10-10") not in rc.index and "C.T" in rc.columns
    assert np.allclose(rc["A.T"], c["A.T"].loc[rc.index], rtol=1e-6)                 # 今の銘柄は今の値段だけ
    assert np.isnan(rc.loc[pd.Timestamp("2026-10-21"), "C.T"])


def test_removed_disclosures_are_kept_aside(tmp_path):
    mod = _agents()
    cols = ["id", "time", "ticker", "company", "title", "url", "xbrl"]
    start = pd.DataFrame([["1", "2026-10-01 15:00:00", "A.T", "A", "決算短信", "", ""],
                          ["2", "2026-10-02 15:00:00", "C.T", "C", "決算短信", "", ""]], columns=cols)
    now = start[start["ticker"] != "C.T"]                                          # C が一覧から外れて落ちた
    path = tmp_path / "disclosures.csv"
    assert mod.save_disclosures(now, start, path) == 1
    assert mod.save_disclosures(now, start, path) == 0                              # 2 回目は増えない
    kept = pd.read_csv(tmp_path / "disclosures_removed.csv", dtype=str)
    assert kept["id"].tolist() == ["2"] and pd.read_csv(path, dtype=str)["id"].tolist() == ["1"]
    allr = mod.record_disclosures({"disclosures": path})
    assert sorted(allr["id"]) == ["1", "2"]
    assert mod.save_disclosures(start, start, tmp_path / "x.csv") == 0 and not (tmp_path / "x_removed.csv").exists()
