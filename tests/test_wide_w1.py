"""W1 資産のトレンド（docs/wide.md 4 章）。作ったデータだけで確かめる（Yahoo には問い合わせない）。"""

import statistics
import sys
import types

import numpy as np
import pandas as pd
import pytest

from cfdbot.wide import base
from cfdbot.wide import w1 as W

COLS = [*W.ASSETS, W.CASH]


def _months(start: str, n: int) -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq="ME")


def _write(tmp_path, df: pd.DataFrame):
    path = tmp_path / W.FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    df.index.name = "date"
    df.to_csv(path)
    return tmp_path


def _daily(seed: int = 0, start: str = "2004-11-18", end: str = "2010-03-10") -> pd.DataFrame:
    """5 つの ETF（でたらめな歩み）と ^IRX の日足。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, end)
    px = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, (len(idx), 5)), axis=0))
    df = pd.DataFrame(px, index=idx, columns=list(W.ASSETS))
    df[W.CASH] = 2.0 + np.cumsum(rng.normal(0, 0.02, len(idx)))
    return df


# --------------------------------------------------------------------------- 手で計算した小さな例
def test_series_by_hand():
    """2 資産・平均 2 か月・倍率 3 か月（2 か月以上）で、G・B・c・入れ替え・k・d を手で計算した値と比べる。"""
    idx = _months("2005-01-31", 8)
    px = pd.DataFrame({"A": [100, 110, 99, 120, 120, 130, 117, 140], "B": [100, 90, 99, 90, 100, 100, 110, 99]},
                      index=idx, dtype=float)
    irx = pd.Series([12, 12, 24, 24, 12, 12, 12, 12], index=idx, dtype=float)
    s = W._series(px, irx, sma=2, win=3, min_win=2)
    # 月末の値が直前の月末より上なら「平均より上」（同じ値は上ではない）。決めた翌月に 0.5 ずつ持つ
    # 決めた重さ: 2 月 A / 3 月 B / 4 月 A / 5 月 B（A は 120 = 平均で持たない）/ 6 月 A（B は 100 = 平均）/ 7 月 B
    assert list(s.index) == list(idx[2:])
    rA = [99 / 110 - 1, 120 / 99 - 1, 0.0, 130 / 120 - 1, 117 / 130 - 1, 140 / 117 - 1]
    rB = [99 / 90 - 1, 90 / 99 - 1, 100 / 90 - 1, 0.0, 110 / 100 - 1, 99 / 110 - 1]
    c = [0.01, 0.02, 0.02, 0.01, 0.01, 0.01]             # 前の月末の ^IRX ÷ 100 ÷ 12
    turn = [0.5, 1.0, 1.0, 1.0, 1.0, 1.0]                # 最初の月は買い始めの 0.5
    hold_a = [True, False, True, False, True, False]
    g = [0.5 * (rA[i] if hold_a[i] else rB[i]) + 0.5 * c[i] - 0.0015 * turn[i] for i in range(6)]
    b = [(rA[i] + rB[i]) / 2 for i in range(6)]
    assert np.allclose(s["c"], c)
    assert np.allclose(s["turn"], turn)
    assert np.allclose(s["g"], g)
    assert np.allclose(s["b"], b)
    gx = [g[i] - c[i] for i in range(6)]
    bx = [b[i] - c[i] for i in range(6)]
    assert s["k"].iloc[:2].isna().all()                  # 前の月までが 2 か月そろうまでは作らない
    for i in range(2, 6):
        lo = max(0, i - 3)                               # 前の月までの直近 3 か月
        k = min(max(statistics.stdev(bx[lo:i]) / statistics.stdev(gx[lo:i]), 0.5), 3.0)
        assert s["k"].iloc[i] == pytest.approx(k)
        assert s["d"].iloc[i] == pytest.approx(k * gx[i] - bx[i])


def test_k_is_3_when_g_does_not_move():
    """どれも下がり続けると全部現金（G = c）。G の標準偏差が 0 なので k = 3.0、d = −（B − c）。"""
    idx = _months("2005-01-31", 7)
    px = pd.DataFrame({"A": [64.0, 32, 16, 8, 4, 2, 1], "B": [128.0, 64, 32, 16, 8, 4, 2]}, index=idx)
    irx = pd.Series(12.0, index=idx)
    s = W._series(px, irx, sma=2, win=3, min_win=2)
    assert (s["turn"] == 0).all()
    assert (s["g"] == s["c"]).all()
    assert (s["k"].dropna() == 3.0).all() and s["k"].notna().sum() == 3
    assert np.allclose(s["d"].dropna(), 0.51)            # B − c = −0.5 − 0.01


def test_k_clipped_both_ends():
    """倍率は 0.5〜3.0 に切る（手で比べた比が範囲の外になる 2 つの例）。"""
    idx = _months("2005-01-31", 8)
    up = [1.2, 1.2, 0.8, 1.2, 0.8, 0.8, 1.2]              # A と B は逆に動く → 5 つの平均（B）はほぼ動かず、G は大きく動く
    a = np.cumprod([100.0] + up)
    b = np.cumprod([100.0] + [2.0 - u for u in up])
    s = W._series(pd.DataFrame({"A": a, "B": b}, index=idx), pd.Series(0.0, index=idx), sma=2, win=3, min_win=2)
    gx, bx = list(s["g"] - s["c"]), list(s["b"] - s["c"])
    for i in range(2, len(s)):
        assert statistics.pstdev(bx[max(0, i - 3):i]) < 0.5 * statistics.stdev(gx[max(0, i - 3):i])
    assert (s["k"].dropna() == 0.5).all() and s["k"].notna().sum() == len(s) - 2
    # A はいつも下がり（持たない）、B は少しずつ上がる（いつも持つ）→ G はほぼ動かず、B（5 つの平均）は大きく動く
    a = np.cumprod([100.0, 0.7, 0.95, 0.7, 0.95, 0.7, 0.95, 0.7])
    b = np.cumprod([100.0, 1.01, 1.02, 1.01, 1.02, 1.01, 1.02, 1.01])
    s = W._series(pd.DataFrame({"A": a, "B": b}, index=idx), pd.Series(0.0, index=idx), sma=2, win=3, min_win=2)
    gx, bx = list(s["g"] - s["c"]), list(s["b"] - s["c"])
    for i in range(2, len(s)):
        assert statistics.stdev(bx[max(0, i - 3):i]) > 3.0 * statistics.stdev(gx[max(0, i - 3):i]) > 0
    assert (s["k"].dropna() == 3.0).all() and s["k"].notna().sum() == len(s) - 2


def test_compute_full_size_falling(tmp_path):
    """5 つとも毎月 10% 下がる: いつも全部現金 → k = 3.0、d = 0.1 + c。d は 5 つがそろう月から 22 か月後に始まる。"""
    idx = _months("2004-11-30", 38)                      # 2004-11〜2007-12 の月末
    df = pd.DataFrame({a: 100 * 0.9 ** np.arange(38) for a in W.ASSETS}, index=idx)
    df[W.CASH] = 12.0
    df.loc[pd.Timestamp("2008-01-15")] = df.iloc[-1]     # 終わっていない 1 月（使わない）
    st = W.compute(_write(tmp_path, df))
    assert st.key == "W1" and st.events is None
    assert st.d.index[0] == pd.Timestamp("2006-09-30") and st.d.index[-1] == pd.Timestamp("2007-12-31")
    assert len(st.d) == 16
    assert np.allclose(st.d, 0.11)
    assert st.gross == pytest.approx(0.11)               # 入れ替えが無いのでコストの前後で同じ
    assert any("2004-11" in n for n in st.notes)


def test_compute_uses_month_end_and_skips_unfinished_month(tmp_path):
    df = _daily()
    st = W.compute(_write(tmp_path, df))
    assert st.d.index[-1] == pd.Timestamp("2010-02-28")  # 2010-03 は途中なので入れない
    assert st.d.index[0] == pd.Timestamp("2006-09-30")   # 2004-11 + 22 か月
    assert st.d.notna().all() and st.d.index.is_monotonic_increasing
    assert (st.d.index == st.d.index.normalize()).all()
    # 日の途中の値を変えても、月末の値が同じなら d は変わらない
    df2 = df.copy()
    mid = df2.index[(df2.index.day == 15)]
    df2.loc[mid, "SPY"] *= 1.5
    pd.testing.assert_series_equal(W.compute(_write(tmp_path, df2)).d, st.d)


def test_no_look_ahead(tmp_path):
    """2008-06-10 より後の値を変えても、2008-05 までの d は変わらない（6 月の d は 6 月の値で変わってよい）。"""
    df = _daily(seed=3)
    base_d = W.compute(_write(tmp_path, df)).d
    df2 = df.copy()
    later = df2.index > pd.Timestamp("2008-06-10")
    rng = np.random.default_rng(9)
    df2.loc[later, list(W.ASSETS)] *= rng.uniform(0.5, 1.5, (int(later.sum()), 5))
    df2.loc[later, W.CASH] += 3.0
    new_d = W.compute(_write(tmp_path, df2)).d
    cut = pd.Timestamp("2008-05-31")
    pd.testing.assert_series_equal(new_d[new_d.index <= cut], base_d[base_d.index <= cut], check_freq=False)
    assert not np.allclose(new_d[new_d.index > cut], base_d[base_d.index > cut])
    # 途中で切ったデータでも、それより前の d は同じ
    short = W.compute(_write(tmp_path, df[df.index <= pd.Timestamp("2008-06-20")])).d
    assert short.index[-1] == cut
    pd.testing.assert_series_equal(short, base_d[base_d.index <= cut], check_freq=False)


# --------------------------------------------------------------------------- 点検と NotReady
def test_check_ok_has_no_return_numbers(tmp_path):
    ck = W.check(_write(tmp_path, _daily()))
    assert ck.ok and ck.key == "W1" and ck.why == ""
    text = "\n".join(ck.lines)
    assert "2004-11" in text and "2006-09" in text
    assert "%" not in text


def test_missing_file(tmp_path):
    with pytest.raises(base.NotReady, match="無い"):
        W.compute(tmp_path)
    ck = W.check(tmp_path)
    assert not ck.ok and "無い" in ck.why


def test_missing_column(tmp_path):
    data = _write(tmp_path, _daily().drop(columns=["GLD"]))
    with pytest.raises(base.NotReady, match="GLD"):
        W.compute(data)
    assert not W.check(data).ok


def test_month_gap(tmp_path):
    df = _daily()
    df.loc[(df.index >= "2007-03-01") & (df.index <= "2007-03-31"), "VNQ"] = np.nan
    data = _write(tmp_path, df)
    with pytest.raises(base.NotReady, match="VNQ 2007-03"):
        W.compute(data)
    assert not W.check(data).ok


def test_bad_dates_and_units(tmp_path):
    path = tmp_path / W.FILE
    path.parent.mkdir(parents=True)
    path.write_text("date," + ",".join(COLS) + "\nnot-a-date,1,1,1,1,1,1\n")
    with pytest.raises(base.NotReady):
        W.compute(tmp_path)
    df = _daily()
    df[W.CASH] = df[W.CASH] * 100                         # 年率 % でなく 1/100 % など
    with pytest.raises(base.NotReady, match="IRX"):
        W.compute(_write(tmp_path, df))


def test_whole_month_without_rows(tmp_path):
    """行が 1 つも無い月（どの列も無い）も欠けた月として止まる（飛ばして 2 か月分を 1 か月のリターンにしない）。"""
    df = _daily()
    df = df[~((df.index >= "2007-03-01") & (df.index <= "2007-03-31"))]
    data = _write(tmp_path, df)
    with pytest.raises(base.NotReady, match="2007-03"):
        W.compute(data)
    ck = W.check(data)
    assert not ck.ok and "2007-03" in ck.why


def test_too_short(tmp_path):
    df = _daily(start="2004-11-18", end="2006-03-10")
    data = _write(tmp_path, df)
    with pytest.raises(base.NotReady, match="k"):
        W.compute(data)
    ck = W.check(data)                                   # compute が止まるなら点検も通さない（通ったルールは外せないため）
    assert not ck.ok and "k" in ck.why


def test_check_and_compute_agree_on_range(tmp_path):
    data = _write(tmp_path, _daily(seed=5))
    st, ck = W.compute(data), W.check(data)
    assert ck.ok
    assert any(f"{st.d.index[0]:%Y-%m}〜{st.d.index[-1]:%Y-%m}（{len(st.d)} か月）" in line for line in ck.lines)


def test_duplicate_dates_keep_file_order(tmp_path):
    """同じ日付が 2 行あれば、ファイルで後の行を月末の値に使う（並べ替えで順が入れ替わらない）。"""
    df = _daily(seed=6)
    st = W.compute(_write(tmp_path, df))
    me = df.index[(df.index.year == 2008) & (df.index.month == 4)][-1]   # 2008-04 の月末の日
    dup = df.loc[[me]] * 0.5                                            # 先の行（違う値）
    df2 = pd.concat([dup, df.loc[[me]], df.drop(index=me)])            # ファイルの中で日付の順になっていない
    pd.testing.assert_series_equal(W.compute(_write(tmp_path, df2)).d, st.d)


# --------------------------------------------------------------------------- 取得（Yahoo の代わりを差し込む）
def test_fetch_one_at_a_time(tmp_path, monkeypatch):
    """1 つずつ取る（1 回に 1 本、間は 1 秒以上）。列が 2 段の版・1 段の版のどちらでも読む。同じ日付は 1 つにする。"""
    calls, events = [], []
    idx = pd.date_range("2024-01-02", periods=5, freq="B", tz="America/New_York")

    def download(tickers, **kw):
        calls.append((list(tickers), kw))
        events.append("get")
        t = tickers[0]
        vals = np.arange(1, 6, dtype=float) * (COLS.index(t) + 1)
        if t == "IEF":                                   # 古い版: 列が 1 段
            return pd.DataFrame({"Close": vals, "Open": vals}, index=idx)
        if t == W.CASH:                                  # 時刻のずれで同じ日付が 2 回（後の値を使う）
            ix = idx.append(pd.DatetimeIndex([idx[-1] + pd.Timedelta(hours=1)]))
            vals = np.append(vals, 99.0)
            cols = pd.MultiIndex.from_product([["Close"], [t]], names=["Price", "Ticker"])
            return pd.DataFrame(vals, index=ix, columns=cols)
        cols = pd.MultiIndex.from_product([["Close", "Open"], [t]], names=["Price", "Ticker"])
        return pd.DataFrame(np.c_[vals, vals], index=idx, columns=cols)

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=download))
    monkeypatch.setattr(W.time, "sleep", lambda s: events.append(s))
    W.fetch(tmp_path, log=lambda *_: None)
    got = pd.read_csv(tmp_path / W.FILE, index_col=0, parse_dates=True)
    assert list(got.columns) == COLS and len(got) == 5
    assert got.index[0] == pd.Timestamp("2024-01-02")
    assert got["IEF"].tolist() == [3.0, 6.0, 9.0, 12.0, 15.0] and got[W.CASH].iloc[-1] == 99.0
    assert [c[0] for c in calls] == [[t] for t in COLS]
    assert all(c[1]["auto_adjust"] is True and c[1]["threads"] is False for c in calls)
    # 問い合わせの間には必ず 1 秒以上の待ちがある
    gets = [i for i, e in enumerate(events) if e == "get"]
    assert all(isinstance(events[i - 1], float) and events[i - 1] >= 1.0 for i in gets[1:])


def test_fetch_retries_then_gives_up(tmp_path, monkeypatch):
    tries = {"n": 0}

    def download(tickers, **kw):
        tries["n"] += 1
        if tickers == ["SPY"] and tries["n"] == 1:
            raise OSError("回線の不調")                   # 1 回目だけ失敗 → やり直して取れる
        if tickers == ["GLD"]:
            return pd.DataFrame()                        # GLD は 3 回とも空
        idx = pd.date_range("2024-01-02", periods=3, freq="B")
        return pd.DataFrame({("Close", tickers[0]): [1.0, 2.0, 3.0]}, index=idx)

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=download))
    waits = []
    monkeypatch.setattr(W.time, "sleep", waits.append)
    with pytest.raises(RuntimeError, match="GLD"):
        W.fetch(tmp_path, log=lambda *_: None)
    assert waits == [30.0, 1.0, 1.0, 1.0, 30.0, 60.0]     # SPY のやり直し・EFA・IEF・GLD・GLD のやり直し 2 回
    assert tries["n"] == 2 + 2 + 3 and not (tmp_path / W.FILE).exists()
