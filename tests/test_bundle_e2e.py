"""束ねる検証の書き出しを、作ったデータだけの別の根で、本物の道具を通しで動かす（cfdbot/bundle_export.py の SOURCES の配線そのまま）。

道具の名前・引数・出力のファイルの形が変わったら、Mac で初めて「作れなかった」になる前に、ここで気づく。
動かすのは trend・crypto・carry・agents（数秒）。学び直しのある shortterm は 1 分ほどかかるので、BUNDLE_SLOW=1 のときだけ。
株の研究（C3・C1・S1）・為替（W7）・文章（1C）は、元データ（フレンチ教授のファイル・FRED・有報の本文）を作れないので動かさない。
"""

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdbot import bundle as B
from cfdbot import bundle_export as X
from tests.test_train import write_mt5_csv

REPO = Path(__file__).resolve().parents[1]


def yahoo_csv(path, index, vol, rng):
    """Yahoo の日足の形（date・open・high・low・close）。"""
    close = 100 * np.exp(np.cumsum(rng.normal(0.0003, vol, len(index))))
    pd.DataFrame({"date": index.strftime("%Y-%m-%d"), "open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
                  "volume": 1.0}).to_csv(path, index=False)


def make_world(root: Path, n_stocks: int = 60) -> Path:
    """道具が読むファイルを、乱数で作る。scripts・config は本物を指す。"""
    (root / "data").mkdir(parents=True)
    (root / "scripts").symlink_to(REPO / "scripts")
    (root / "config").symlink_to(REPO / "config")
    rng = np.random.default_rng(42)
    uni = root / "data" / "universe"
    uni.mkdir()
    for k in ("GOLD", "SILVER", "WTI", "BRENT", "COPPER", "PLATINUM", "NATGAS"):                # 先物（期間A・C）
        yahoo_csv(uni / f"{k}.csv", pd.bdate_range("2012-01-02", "2026-11-13"), 0.015, rng)
    for k, vol in (("BTC", 0.03), ("ETH", 0.035)):                                              # 暗号資産（土日もある）
        yahoo_csv(uni / f"{k}.csv", pd.date_range("2019-01-01", "2026-11-15", freq="D"), vol, rng)
    write_mt5_csv(root / "data" / "XTIUSD_H1.csv", "oil", 1, start="2021-01-02", end="2026-11-10")   # フィリップの形（期間B）
    write_mt5_csv(root / "data" / "XAGUSD_H1.csv", "silver", 2, start="2021-01-02", end="2026-11-10")
    (root / "data" / "carry").mkdir()
    idx = pd.bdate_range("2011-12-23", "2024-04-05")                                            # 限月の表は 2024-04 の初めまで
    c1 = 20 * np.exp(np.cumsum(rng.normal(0.0001, 0.02, len(idx))))
    c2 = c1 * np.exp(-(0.01 * np.sin(np.arange(len(idx)) / 300) + rng.normal(0, 0.002, len(idx))))
    curve = pd.DataFrame({"c1": c1, "c2": c2, "c3": c2, "c4": c2}, index=idx)
    curve.index.name = "date"
    curve.to_csv(root / "data" / "carry" / "wti_curve.csv")
    stocks = root / "data" / "stocks"
    stocks.mkdir()
    idx = pd.bdate_range("2008-01-02", "2026-11-18")                                            # 値段は 11 月の途中まで
    cols = [f"{1000 + i}.T" for i in range(n_stocks)]
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0.0002, 0.015, (len(idx), n_stocks)), axis=0)), index=idx, columns=cols)
    close.to_csv(stocks / "jp_close.csv")
    (close.shift(1).fillna(100.0) * np.exp(rng.normal(0, 0.004, close.shape))).to_csv(stocks / "jp_open.csv")
    pd.DataFrame({"ticker": cols, "name": cols, "sector": "x", "size": "TOPIX Mid400"}).to_csv(stocks / "universe_jp.csv", index=False)
    etf = pd.DataFrame({"1306.T": 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(idx))))}, index=idx)
    etf.to_csv(stocks / "misc_close.csv")
    etf.shift(1).fillna(100.0).to_csv(stocks / "misc_open.csv")
    (root / "data" / "agents").mkdir()
    score = pd.DataFrame(rng.normal(0, 1, close.shape), index=idx, columns=cols)
    score[(score.index >= "2010-01-01") & (score.index <= "2026-11-17")].to_csv(root / "data" / "agents" / "selector_scores.csv", float_format="%.5f")
    return root


def listing(root: Path) -> dict[str, int]:
    """元データのファイルと大きさ（書き出しの道具の置き場 data/bundle は除く）。"""
    return {p.relative_to(root).as_posix(): p.stat().st_size for p in sorted((root / "data").rglob("*"))
            if p.is_file() and "bundle" not in p.relative_to(root).parts}


def ym(ts) -> str:
    return f"{ts:%Y-%m}"


def test_the_real_tools_write_what_the_export_picks_up(tmp_path):
    root = make_world(tmp_path / "world")
    before = listing(root)
    lines: list[str] = []
    sources = {k: X.SOURCES[k] for k in ("trend", "crypto", "carry", "agents")}
    status, changed = X.export(root, sources=sources, log=lines.append, made="2026-11-21 09:00")
    for k in ("EA", "C2", "carry", "combo", "M2a", "M2b"):
        assert status[k].startswith("書いた"), f"{k}: {status[k]}"
    assert "decisions_numbers.csv" in status["E1"] and "decisions_numbers.csv" in status["E2"] and "disclosures.csv" in status["J7"]
    assert all(status[k].startswith("作れなかった") for k in ("E1", "E2", "J7"))   # 材料（決算の記録・開示の一覧）が無い → 道具は動くが、書かない
    assert changed == [] and listing(root) == before                              # 元データは書き換えない・取り直さない・増やさない
    series, _ = B.load_series(root / B.SERIES_DIR)
    assert set(series) == {"EA", "C2", "carry", "combo", "M2a", "M2b"}
    assert ym(series["EA"].index[0]) < "2013-06" and ym(series["EA"].index[-1]) == "2026-10"     # 期間A（2012 年〜）と期間B がつながっている
    assert len(series["EA"]) == len(pd.period_range(series["EA"].index[0], series["EA"].index[-1], freq="M"))
    assert ym(series["C2"].index[-1]) == "2026-10" and ym(series["carry"].index[-1]) == "2024-03" and ym(series["combo"].index[-1]) == "2024-03"
    assert ym(series["M2a"].index[0]) == "2010-01" and ym(series["M2a"].index[-1]) == "2026-10" and ym(series["M2b"].index[-1]) == "2026-10"
    text = "\n".join(lines) + "\n".join(status.values())
    assert "%" not in text                                                        # 画面に出るのは、名前と期間と月の数だけ
    rec, problem = B.read_seal(root / B.SERIES_DIR)
    assert problem is None and rec["series"] == status
    ok, shown = B.check(root / B.SERIES_DIR, without=tuple(k for k in B.SERIES_NAMES if k not in series))
    assert not ok and "家族" in shown[-1]                                         # 4 家族だけなので、判定には進めない


@pytest.mark.skipif(not os.environ.get("BUNDLE_SLOW"), reason="学び直しがあって 1 分かかる。BUNDLE_SLOW=1 のときだけ")
def test_the_shortterm_tool_writes_what_the_export_picks_up(tmp_path):
    root = make_world(tmp_path / "world")
    before = listing(root)
    status, changed = X.export(root, sources={"shortterm": X.SOURCES["shortterm"]}, log=lambda s: None)
    assert status["M2"].startswith("書いた") and status["M1"].startswith("書いた"), status
    assert changed == [] and listing(root) == before
    series, _ = B.load_series(root / B.SERIES_DIR)
    assert ym(series["M2"].index[-1]) == "2026-10" and ym(series["M1"].index[-1]) == "2026-10"   # 11 月は終わっていない
