"""W4 配当の権利落ちの日（docs/wide.md 4 章）。作ったデータだけで確かめる。"""

import numpy as np
import pandas as pd
import pytest

from cfdbot.wide import base, w3
from cfdbot.wide import w4 as W

T = ["1001.T", "1002.T", "1003.T", "1004.T", "1005.T"]
NAN = np.nan


def _write(tmp_path, close: pd.DataFrame, div: pd.DataFrame | None):
    p = tmp_path / w3.FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    close = close.copy()
    close.index.name = "Date"
    close.to_csv(p)
    if div is not None:
        div.to_csv(tmp_path / W.FILE, index=False)
    return tmp_path


def _hand_close() -> pd.DataFrame:
    """5 銘柄・6 日。値段はリターンから作る（NaN の日は値段が無い）。"""
    days = pd.to_datetime(["2020-01-30", "2020-01-31", "2020-02-03", "2020-02-04", "2020-03-02", "2020-04-01"])
    ret = [
        [0, 0, 0, 0, 0],
        [-0.02, -0.01, 0.01, 0.02, 0.03],
        [0.01, 0.02, -0.01, 0.05, 0.0],
        [0.04, -0.02, 0.40, 0.01, 0.03],      # 1003 は +40% → +30% にそろえる
        [0.05, 0.01, NAN, NAN, NAN],
        [0.01, 0.02, NAN, NAN, NAN],
    ]
    px = 100 * np.cumprod(1 + np.nan_to_num(np.array(ret, dtype=float)), axis=0)
    px[4:, 2:] = NAN
    px[5, 2:] = 100.0                           # 値段が戻っても、前の日が無いのでリターンは無い
    return pd.DataFrame(px, index=days, columns=T)


def _hand_div() -> pd.DataFrame:
    rows = [("2020-01-31", "1001.T"), ("2020-01-31", "1002.T"), ("2020-01-31", "1001.T"),   # 重なり 1
            ("2020-02-03", "1004.T"), ("2020-02-04", "1003.T"),
            ("2020-02-04", "9999.T"),           # 値段の表に無い銘柄
            ("2020-02-01", "1004.T"),           # 土曜（営業日でない）
            ("2020-01-30", "1005.T"),           # 最初の日（リターンが無い）
            ("2020-03-02", "1001.T"),           # 権利落ちでない株でリターンのあるのは 1002 だけ
            ("2020-04-01", "1002.T"),           # 終わっていない月
            ("2020-05-01", "1003.T"),           # 値段の最後の日より後
            ("2019-12-30", "1001.T")]           # 値段の最初の日より前
    return pd.DataFrame([(d, t, 10.0) for d, t in rows], columns=["date", "ticker", "amount"])


def _random(n_stocks: int = 40, start: str = "2006-01-04", end: str = "2010-03-10", seed: int = 0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, end)
    px = 1000 * np.exp(np.cumsum(rng.normal(0.0002, 0.02, (len(idx), n_stocks)), axis=0))
    cols = [f"{1300 + i}.T" for i in range(n_stocks)]
    close = pd.DataFrame(px, index=idx, columns=cols)
    close.iloc[:300, 0] = np.nan
    ev = [(idx[k], cols[c]) for k, c in zip(rng.integers(1, len(idx), 400), rng.integers(0, n_stocks, 400))]
    div = pd.DataFrame([(f"{d:%Y-%m-%d}", t, 12.5) for d, t in ev], columns=["date", "ticker", "amount"])
    return close, div


# --------------------------------------------------------------------------- 手で計算した小さな例
def test_events_by_hand(tmp_path):
    ev, dup = W.read_div(_write(tmp_path, _hand_close(), _hand_div()))
    assert dup == 1 and len(ev) == 11
    e = W._events(_hand_close(), ev, min_ctrl=2)
    st = dict(zip(zip(e["date"].dt.strftime("%m-%d"), e["ticker"]), e["state"]))
    assert st == {("01-31", "1001.T"): "used", ("01-31", "1002.T"): "used", ("02-03", "1004.T"): "used",
                  ("02-04", "1003.T"): "used", ("02-04", "9999.T"): "no_ticker", ("02-01", "1004.T"): "holiday",
                  ("01-30", "1005.T"): "no_return", ("03-02", "1001.T"): "few_ctrl", ("04-01", "1002.T"): "unfinished",
                  ("05-01", "1003.T"): "after", ("12-30", "1001.T"): "before"}
    u = e[e["state"] == "used"].set_index("ticker")
    assert u.loc["1001.T", "n_ctrl"] == 3 and u.loc["1001.T", "ctrl"] == pytest.approx(0.02)   # 1003・1004・1005
    assert u.loc["1004.T", "n_ctrl"] == 4 and u.loc["1004.T", "ctrl"] == pytest.approx(0.005)
    assert u.loc["1003.T", "ret"] == pytest.approx(0.30)                                        # +40% → +30%
    assert u.loc["1003.T", "ctrl"] == pytest.approx(0.015)


def test_compute_by_hand(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "MIN_CTRL", 2)
    st = W.compute(_write(tmp_path, _hand_close(), _hand_div()))
    assert st.key == "W4" and st.events == 4
    assert list(st.d.index) == list(pd.to_datetime(["2020-01-31", "2020-02-29", "2020-03-31"]))   # 4 月は終わっていない
    jan = ((-0.02 - 0.02 - 0.001) + (-0.01 - 0.02 - 0.001)) / 2              # 1 日に 2 件 → 平均
    feb = (0.05 - 0.005 - 0.001) + (0.30 - 0.015 - 0.001)                    # 2 日 → 足す
    assert np.allclose(st.d, [jan, feb, 0.0])                                # 3 月は使える事象が無い → 0
    assert st.gross == pytest.approx((jan + 0.001 + feb + 0.002 + 0.0) / 3)
    text = "\n".join(st.notes)
    for word in ("重なり 1 件", "値段の表に無い銘柄の事象 1 件", "営業日でない日の事象 1 件", "終わっていない月の事象 1 件",
                 "届かない日（1 日）の事象 1 件", "使った事象 4（3 日）", "2020-01〜2020-03"):
        assert word in text


def test_thirty_controls_needed(tmp_path):
    """40 銘柄: 権利落ちが 11 銘柄の日は相手が 29 で使わない。10 銘柄の日は 30 で使う。"""
    close, _ = _random(seed=5)
    cols = list(close.columns)
    d11, d10 = pd.Timestamp("2008-03-12"), pd.Timestamp("2008-04-15")
    div = pd.DataFrame([(f"{d11:%Y-%m-%d}", t, 1.0) for t in cols[1:12]] + [(f"{d10:%Y-%m-%d}", t, 1.0) for t in cols[1:11]],
                       columns=["date", "ticker", "amount"])
    e = W._events(close, W.read_div(_write(tmp_path, close, div))[0], W.MIN_CTRL)
    assert (e.loc[e["date"] == d11, "state"] == "few_ctrl").all() and (e.loc[e["date"] == d11, "n_ctrl"] == 29).all()
    assert (e.loc[e["date"] == d10, "state"] == "used").all() and (e.loc[e["date"] == d10, "n_ctrl"] == 30).all()
    st = W.compute(tmp_path)
    assert st.events == 10 and st.d.index[0] == pd.Timestamp("2008-04-30")
    r = close.pct_change(fill_method=None).loc[d10]
    want = (r[cols[1:11]] - r.drop(cols[1:11]).mean()).mean() - 0.001
    assert st.d.loc["2008-04-30"] == pytest.approx(want)
    assert (st.d.drop(pd.Timestamp("2008-04-30")) == 0).all() and st.d.index[-1] == pd.Timestamp("2010-02-28")


def test_no_look_ahead(tmp_path):
    """2008-06-10 より後の値段・権利落ちの日を変えても、2008-05 までの d は変わらない。"""
    close, div = _random(seed=3)
    base_d = W.compute(_write(tmp_path, close, div)).d
    later = close.index > pd.Timestamp("2008-06-10")
    c2 = close.copy()
    c2.loc[later] = c2.loc[later].to_numpy() * np.random.default_rng(9).uniform(0.8, 1.25, (int(later.sum()), c2.shape[1]))
    d2 = div[pd.to_datetime(div["date"]) <= "2008-06-10"]
    d2 = pd.concat([d2, pd.DataFrame([("2008-07-01", c, 1.0) for c in close.columns[1:6]], columns=div.columns)])
    new_d = W.compute(_write(tmp_path, c2, d2)).d
    cut = pd.Timestamp("2008-05-31")
    pd.testing.assert_series_equal(new_d[new_d.index <= cut], base_d[base_d.index <= cut], check_freq=False)
    assert not np.allclose(new_d[new_d.index > cut], base_d[base_d.index > cut])
    short = W.compute(_write(tmp_path, close[close.index <= "2008-06-20"], div)).d
    assert short.index[-1] == cut
    pd.testing.assert_series_equal(short, base_d[base_d.index <= cut], check_freq=False)


# --------------------------------------------------------------------------- 点検と NotReady
def test_check_ok_has_no_return_numbers(tmp_path):
    close, div = _random()
    ck = W.check(_write(tmp_path, close, div))
    assert ck.ok and ck.key == "W4" and ck.why == ""
    text = "\n".join(ck.lines)
    assert "使う事象" in text and "40 銘柄" in text and "2010-02" in text
    assert "%" not in text


def test_check_lists_reasons(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "MIN_CTRL", 2)
    ck = W.check(_write(tmp_path, _hand_close(), _hand_div()))
    assert ck.ok
    text = "\n".join(ck.lines)
    assert "使う事象 4（3 日）" in text and "値段の表に無い銘柄 1 件" in text and "重なり 1 件" in text


def test_missing_files(tmp_path):
    close, div = _random(n_stocks=12)
    data = _write(tmp_path, close, None)
    with pytest.raises(base.NotReady, match="jp_div.csv が無い"):
        W.compute(data)
    assert not W.check(data).ok
    (tmp_path / w3.FILE).unlink()
    div.to_csv(tmp_path / W.FILE, index=False)
    with pytest.raises(base.NotReady, match="jp_close.csv が無い"):
        W.compute(tmp_path)
    ck = W.check(tmp_path)
    assert not ck.ok and "無い" in ck.why


@pytest.mark.parametrize("text, match", [
    ("day,ticker\n2020-01-31,1301.T\n", "列が無い: date"),
    ("date,ticker,amount\n", "1 行も無い"),
    ("", "読めない"),
    ("date,ticker\n2020-01-31,1301.T\nnot-a-date,1302.T\n", "日付"),
    ("date,ticker\n2020-01-31,\n", "銘柄の空いた行"),
    ("date,ticker\n2008-01-31,7203.T\n", "使える事象が無い"),
])
def test_bad_div(tmp_path, text, match):
    close, _ = _random(n_stocks=12)
    data = _write(tmp_path, close, None)
    (tmp_path / W.FILE).write_text(text)
    with pytest.raises(base.NotReady, match=match):
        W.compute(data)
    ck = W.check(data)
    assert not ck.ok and ck.why


def test_timezone_dates(tmp_path):
    close, div = _random(n_stocks=35)
    div2 = div.assign(date=div["date"] + " 00:00:00+09:00")
    a = W.compute(_write(tmp_path, close, div)).d
    b = W.compute(_write(tmp_path, close, div2)).d
    pd.testing.assert_series_equal(a, b)
    # 時差の付いた日付と付かない日付が混ざっていても落ちない（その土地の日付のまま）
    div3 = div.assign(date=[x + (" 00:00:00+09:00" if k % 2 else "") for k, x in enumerate(div["date"])])
    pd.testing.assert_series_equal(W.compute(_write(tmp_path, close, div3)).d, a)
    assert W.check(tmp_path).ok


def test_row_without_any_price_does_not_hide_the_next_day(tmp_path):
    """値の 1 つも無い行（土曜 2008-04-12）は落とす。残すと、次の営業日（04-14）の権利落ちの事象がリターン無しになる。"""
    close, _ = _random(seed=7)
    cols = list(close.columns)
    div = pd.DataFrame([("2008-04-14", t, 1.0) for t in cols[1:4]], columns=["date", "ticker", "amount"])
    a = W.compute(_write(tmp_path, close, div))
    empty = pd.DataFrame(np.nan, index=[pd.Timestamp("2008-04-12")], columns=close.columns)
    b = W.compute(_write(tmp_path, pd.concat([close, empty]).sort_index(), div))
    assert a.events == b.events == 3
    pd.testing.assert_series_equal(b.d, a.d)
    r = close.pct_change(fill_method=None).loc["2008-04-14"]
    want = (r[cols[1:4]] - r.drop(cols[1:4]).mean()).mean() - 0.001
    assert b.d.loc["2008-04-30"] == pytest.approx(want)
    assert any("合わせて 1 日を落とした" in n for n in b.notes)
