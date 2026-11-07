"""上がる確率の順位表（docs/chance.md、./cfd agents rank）の試験。作ったデータだけを使う。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

from cfdbot import stocks_short as H  # noqa: E402
from cfdbot.agents import chance as C  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAST = dict(max_iter=20, learning_rate=0.1, max_leaf_nodes=15, min_samples_leaf=200, early_stopping=False, random_state=0)


def _script():
    spec = importlib.util.spec_from_file_location("agents_rank", ROOT / "scripts" / "agents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _prices(n_stocks=60, start="2016-01-04", end="2019-12-27", seed=0, drift=0.0):
    """寄りと引け。drift > 0 なら、株ごとにずっと同じ向きの癖（過去の値動きから分かる）を付ける。"""
    idx = pd.bdate_range(start, end)
    rng = np.random.default_rng(seed)
    cols = [f"{1000 + k}.T" for k in range(n_stocks)]
    mu = rng.normal(0, drift, n_stocks) if drift else np.zeros(n_stocks)
    night = rng.normal(0.0002, 0.008, (len(idx), n_stocks)) + mu / 2
    day = rng.normal(0, 0.012, (len(idx), n_stocks)) + mu / 2
    close = np.empty_like(night)
    open_ = np.empty_like(night)
    prev = np.full(n_stocks, 1000.0)
    for t in range(len(idx)):
        open_[t] = prev * (1 + night[t])
        close[t] = open_[t] * (1 + day[t])
        prev = close[t]
    return pd.DataFrame(open_, idx, cols), pd.DataFrame(close, idx, cols)


def test_the_answer_is_bought_at_the_next_close_and_sold_n_days_later():
    idx = pd.bdate_range("2020-01-01", periods=8)
    close = pd.DataFrame({"A.T": [10, 11, 12, 11, 13, 9, 9, 15], "B.T": [5, 5, 5, 5, 5, 5, 5, np.nan]}, index=idx, dtype=float)
    up, ret = C.answers(close, days=2)
    # t=0: 買い t=1（11）→ 売り t=3（11）: 同じなので 0。t=1: 12 → 13 で 1。t=2: 11 → 9 で 0。t=3: 13 → 9 で 0。t=4: 9 → 15 で 1
    assert list(up["A.T"].iloc[:5]) == [0, 1, 0, 0, 1] and up["A.T"].iloc[5:].isna().all()
    assert ret["A.T"].iloc[1] == pytest.approx(np.log(13 / 12)) and up["B.T"].iloc[0] == 0 and np.isnan(up["B.T"].iloc[4])
    assert C.embargo(5) == 7 and C.embargo(20) == 22


def test_the_probabilities_never_use_the_answer_of_the_year_they_score():
    """判定の年の最初の日の引け（学んだ行の答えより後）を大きく変えても、その日の確率は変わらない。あける日が足りなければ変わる。"""
    open_, close = _prices()
    first = int(np.flatnonzero(close.index.year == 2019)[0])
    bent = close.copy()
    bent.iloc[first] *= 3.0                                         # その日の引けだけ（その日の確率の入力は前の日の引けまで）
    days = 5
    _, x = H.features(open_, close)
    _, xb = H.features(open_, bent)
    assert np.allclose(x[first], xb[first], equal_nan=True)         # 入力は同じ
    tradable = C.tradable_of(open_, close).to_numpy()

    def prob(c, xx):
        up, _ = C.answers(c, days)
        p, _ = C.walk_forward(xx, up, tradable, days, first_year=2019, params=FAST, log=lambda s: None)
        return p.iloc[first].to_numpy()

    assert np.array_equal(prob(close, x), prob(bent, xb))
    import unittest.mock as um

    with um.patch.object(C, "embargo", lambda d, delay=1: d):      # 足りない: 学ぶ行の答えが、その日の引けを使う
        assert not np.array_equal(prob(close, x), prob(bent, xb))


def test_a_planted_tendency_is_found_and_plain_noise_is_not():
    open_, close = _prices(drift=0.004, seed=1)
    r = C.probabilities(open_, close, 5, params=FAST, log=lambda s: None)
    hits = C.hit_rates(r["prob"], r["up"], r["tradable"])
    assert hits["top"].mean() - hits["all"].mean() > 0.10           # 上がる癖のある株が上位に来る
    v = C.evaluate(r["prob"], open_, close, 5, trials=89, placebo=20)
    assert v["mean"] > 0 and v["placebo"] == 1.0

    open_, close = _prices(seed=2)
    r = C.probabilities(open_, close, 5, params=FAST, log=lambda s: None)
    hits = C.hit_rates(r["prob"], r["up"], r["tradable"])
    assert abs(hits["top"].mean() - hits["all"].mean()) < 0.03      # 癖の無い値動きでは、上位も全体と同じくらい
    p = r["prob"].to_numpy()
    assert np.nanmin(p) >= 0 and np.nanmax(p) <= 1 and 0.4 < np.nanmean(p) < 0.6


def test_calibration_bins_and_the_readable_line():
    idx = pd.bdate_range("2017-01-02", "2019-12-31")
    rng = np.random.default_rng(3)
    p = pd.DataFrame(rng.uniform(0.35, 0.70, (len(idx), 80)), index=idx)
    up = pd.DataFrame((rng.random(p.shape) < p.to_numpy()).astype(float), index=idx)
    ret = up * 0.01 - 0.005
    info = {y: {"base": 0.5} for y in (2017, 2018, 2019)}
    h = C.halves(idx)
    cal = {k: C.calibration(p, up, ret, m) for k, m in h.items()}
    scores = {k: C.brier(p, up, info, m) for k, m in h.items()}
    g = cal["全期間"]
    assert g["rows"].sum() == p.size and list(g["label"]) == list(C.BIN_LABELS) and g.loc[0, "rows"] > 0
    assert ((g["up"] - g["prob"]).abs()[g["share"] > 0.01] < 0.02).all()
    ok, why = C.readable(cal, scores)
    assert ok and not why                                            # 確率どおりに上がる作り物は「そのまま読んでよい」
    worse = (p + 0.10).clip(upper=0.99)                              # 10 ポイント高く言う
    cal2 = {k: C.calibration(worse, up, ret, m) for k, m in h.items()}
    scores2 = {k: C.brier(worse, up, info, m) for k, m in h.items()}
    ok2, why2 = C.readable(cal2, scores2)
    assert not ok2 and any("前半の" in w for w in why2) and any("後半の" in w for w in why2)


def test_rank_command_writes_the_table_and_the_test_writes_both_verdicts(tmp_path, monkeypatch, capsys):
    open_, close = _prices(drift=0.002, seed=4)
    data = tmp_path / "stocks"
    data.mkdir()
    open_.to_csv(data / "jp_open.csv")
    close.to_csv(data / "jp_close.csv")
    pd.DataFrame({"ticker": close.columns, "name": [f"会社{k}" for k in range(close.shape[1])]}).to_csv(data / "universe_jp.csv", index=False)
    mod = _script()
    monkeypatch.setattr(H, "GBM_PARAMS", FAST)
    base = ["--root", str(tmp_path / "agents"), "--data", str(data), "--out", str(tmp_path / "out")]
    monkeypatch.setattr("sys.argv", ["agents.py", "rank", *base, "--days", "5", "--top", "7"])
    mod.main()
    out = capsys.readouterr().out
    path = Path(out.split("保存先: ")[-1].split("（")[0].strip())
    text = path.read_text(encoding="utf-8")
    assert "決めた日: **2019-12-27**" in text and "12-30 ごろ）の引けで買い、5 営業日後（01-06 ごろ）" in text
    assert "## 1. 上がる確率の上位 7 銘柄（60 銘柄から）" in text and "| 1 | 会社" in text and "| 8 |" not in text
    assert "判定: RK5" in text and "毎日の上位 10 銘柄が 5 日で上がった割合" in text and "| **全期間** |" in text
    table = pd.read_csv(path.parent / "rank_5.csv")
    assert len(table) == 60 and table["prob"].is_monotonic_decreasing and table["prob"].between(0, 1).all()

    monkeypatch.setattr("sys.argv", ["agents.py", "rank", *base, "--days", "3"])
    mod.main()
    assert "判定しない参考の表" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["agents.py", "rank", *base, "--days", "61"])
    with pytest.raises(SystemExit):
        mod.main()

    monkeypatch.setattr("sys.argv", ["agents.py", "rank", *base, "--test", "--placebo", "5"])
    mod.main()
    out = capsys.readouterr().out
    text = Path(out.split("保存先: ")[-1].strip()).read_text(encoding="utf-8")
    assert "| RK5 | 上位 10 を 5 日持つ |" in text and "| RK20 | 上位 10 を 20 日持つ |" in text
    assert "| 2.49 |" in text and "### 20 日（RK20）" in text and "（試した数 90）" in text
