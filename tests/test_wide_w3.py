"""W3 1 年の高値に近い株（docs/wide.md 4 章）。作ったデータだけで確かめる。"""

import numpy as np
import pandas as pd
import pytest

from cfdbot.wide import base
from cfdbot.wide import w3 as W

A, B, C, D, E = "1001.T", "1002.T", "1003.T", "1004.T", "1005.T"
NAN = np.nan


def _write(tmp_path, close: pd.DataFrame):
    path = tmp_path / W.FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    close = close.copy()
    close.index.name = "Date"
    close.to_csv(path)
    return tmp_path


def _hand() -> pd.DataFrame:
    """手で計算する小さな表（5 銘柄・10 日）。直近 3 日・3 日とも値段のある株・最低 2 銘柄で使う。"""
    days = pd.to_datetime(["2020-01-30", "2020-01-31", "2020-02-27", "2020-02-28", "2020-03-30", "2020-03-31",
                           "2020-04-29", "2020-04-30", "2020-05-29", "2020-06-01"])
    rows = [
        [100, 100, 100, 100, NAN],
        [110, 90, 100, 105, NAN],
        [105, 95, 120, NAN, NAN],
        [108, 99, 110, 100, NAN],
        [100, 110, 121, 110, 100],
        [120, 99, 121, 110, 110],
        [132, 90, 133.1, 99, NAN],
        [145.2, 81, 121, 99, NAN],
        [130.68, 121.5, 121, 108.9, NAN],     # B は +50% → リターンは +30% にそろえる
        [130.68, 109.35, 125, 119.79, NAN],
    ]
    return pd.DataFrame(rows, index=days, columns=[A, B, C, D, E], dtype=float)


def _random(n_stocks: int = 40, start: str = "2006-01-04", end: str = "2010-03-10", seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, end)
    px = 1000 * np.exp(np.cumsum(rng.normal(0.0002, 0.02, (len(idx), n_stocks)), axis=0))
    df = pd.DataFrame(px, index=idx, columns=[f"{1300 + i}.T" for i in range(n_stocks)])
    df.iloc[:400, 0] = np.nan                                  # 途中から上場した株
    df.iloc[rng.integers(0, len(idx), 200), 1] = np.nan        # ところどころ値段の無い株
    return df


# --------------------------------------------------------------------------- 手で計算した小さな例
def test_series_by_hand():
    s = W._series(_hand(), window=3, min_days=3, top_pct=10, min_hold=2)
    assert list(s.index) == list(pd.to_datetime(["2020-03-31", "2020-04-30", "2020-05-31", "2020-06-30"]))
    # 2 月末: D は 2/27 に値段が無く 3 日そろわない、E は値段が無い → A・B・C の 3 銘柄。点数 B 1.0 > A 108/110 > C 110/120
    # 3 月末: A・C・D が 1.0（E は 2 日しかない）。直近 3 日（2/28・3/30・3/31）の日ごとのリターンを掛け合わせると
    #   A 108/105 × 100/108 × 120/100 = 120/105、D 0%（2/27 に値段が無い）× 1.1 × 1.0 = 1.1、C 110/120 × 1.1 × 1.0 = 121/120 → A・D
    # 4 月末: A 1.0 > C 121/133.1 > D 99/110 > B 81/99
    # 5 月末: B と D が 1.0。B 90/99 × 0.9 × 1.3（+50% は +30% に）= 1.064 > D 0.9 × 1.0 × 1.1 = 0.99
    assert list(s["eligible"]) == [3, 4, 4, 4]
    assert list(s["held"]) == [2, 2, 2, 2]
    turn = [1.0, 0.5, 0.5, 1.0]                                # {B,A} → {A,D} → {A,C} → {B,D}
    assert list(s["turn"]) == turn
    mkt = [(120 / 108 - 1 + 0 + 0.1 + 0.1 + 0.1) / 5,          # 3 月: E も 3/31 にリターンがあるので入る
           (0.21 + (81 / 99 - 1) + 0 - 0.1) / 4,              # 4 月: E は値段が無いので入らない
           (-0.1 + 0.3 + 0 + 0.1) / 4,
           (0 - 0.1 + (125 / 121 - 1) + 0.1) / 4]
    g = [(0 + (120 / 108 - 1)) / 2, (0.21 - 0.1) / 2, (-0.1 + 0) / 2, (-0.1 + 0.1) / 2]
    assert np.allclose(s["mkt"], mkt)
    assert np.allclose(s["g"], g)
    gross = [g[i] - mkt[i] for i in range(4)]
    assert np.allclose(s["gross"], gross)
    assert np.allclose(s["d"], [gross[i] - 0.001 * turn[i] for i in range(4)])


def test_picks_order_and_eligibility():
    p = W._picks(_hand(), window=3, min_days=3, top_pct=10, min_hold=2)
    assert list(p.index) == list(pd.to_datetime(["2020-02-28", "2020-03-31", "2020-04-30", "2020-05-29", "2020-06-01"]))
    assert p["held"].tolist()[:4] == [[B, A], [A, D], [A, C], [B, D]]
    # 2 日でよければ D（2 月末）・E（3 月末）も入る。t の値段が無い株は入らない
    q = W._picks(_hand(), window=3, min_days=2, top_pct=10, min_hold=2)
    assert list(q["eligible"])[:2] == [4, 5]
    one = _hand()
    one.loc["2020-03-31", A] = NAN
    assert A not in W._picks(one, window=3, min_days=2, top_pct=10, min_hold=2)["held"].iloc[1]


def test_tie_break_uses_every_day_of_the_window():
    """同点の順の「直近 3 営業日のリターン」は 3 日分の日ごとのリターン（窓の最初の日の分も入れる）。それも同じなら番号の順。"""
    days = pd.to_datetime(["2020-02-25", "2020-02-26", "2020-02-27", "2020-02-28"])
    x, y, y2 = "1003.T", "1002.T", "1001.T"
    px = pd.DataFrame({x: [100, 125, 120, 125], y: [100, 100, 105, 110], y2: [100, 100, 105, 110]}, index=days, dtype=float)
    # 点数はどれも 1.0。x = 1.25 × 0.96 × 125/120 = 1.25、y・y2 = 1.0 × 1.05 × 110/105 = 1.10
    # （窓の最初の日 2/26 の分を抜くと x 1.0 < y 1.10 になり、順が入れ替わる）
    p = W._picks(px, window=3, min_days=3, top_pct=10, min_hold=1)
    assert p["held"].iloc[0] == [x]
    p = W._picks(px, window=3, min_days=3, top_pct=10, min_hold=2)
    assert p["held"].iloc[0] == [x, y2]                        # y と y2 は同じ → 番号の順で 1001.T


def test_top_ten_percent_floor_and_minimum():
    rng = np.random.default_rng(1)
    days = pd.bdate_range("2020-01-27", "2020-02-28")
    for n, k in ((125, 12), (99, 10), (200, 20), (12, 10)):
        px = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.01, (len(days), n)), axis=0)), index=days,
                          columns=[f"{2000 + i}.T" for i in range(n)])
        p = W._picks(px, window=3, min_days=3, top_pct=10, min_hold=10)
        assert [len(h) for h in p["held"]] == [k, k]
    p = W._picks(px.iloc[:, :9], window=3, min_days=3, top_pct=10, min_hold=10)
    assert list(p["eligible"]) == [9, 9] and p["held"].map(len).tolist() == [0, 0]   # 10 銘柄に届かない月末は持たない


def test_empty_month_gives_zero_and_full_turnover():
    """基準を満たす株が足りない月は持たず d = 0。その次の月は全部が新しく入った（100%）。"""
    close = _hand()
    close.loc["2020-03-31", [A, B, C]] = NAN                   # 3 月末・4 月末は D だけ 3 日そろう → 持たない
    s = W._series(close, window=3, min_days=3, top_pct=10, min_hold=2)
    assert list(s["held"]) == [2, 0, 0, 2]
    for m in ("2020-04-30", "2020-05-31"):
        assert s.loc[m, "d"] == 0.0 and s.loc[m, "gross"] == 0.0 and s.loc[m, "turn"] == 0.0
    # 5 月末は {B, D}。2 月末の {B, A} と B が重なるが、間に持たない月があるので全部が新しく入った
    assert s.loc["2020-06-30", "turn"] == 1.0


def test_compute_small_with_patched_constants(tmp_path, monkeypatch):
    for k, v in (("WINDOW", 3), ("MIN_DAYS", 3), ("MIN_HOLD", 2)):
        monkeypatch.setattr(W, k, v)
    st = W.compute(_write(tmp_path, _hand()))
    full = W._series(_hand(), 3, 3, 10, 2)
    assert st.key == "W3" and st.events is None
    assert list(st.d.index) == list(pd.to_datetime(["2020-03-31", "2020-04-30", "2020-05-31"]))   # 6 月は終わっていない
    assert np.allclose(st.d, full["d"].iloc[:3])
    assert st.gross == pytest.approx(full["gross"].iloc[:3].mean())
    assert any("2020-03" in n for n in st.notes)


# --------------------------------------------------------------------------- 大きめの作ったデータ
def test_compute_random_and_closed_day(tmp_path):
    df = _random()
    st = W.compute(_write(tmp_path, df))
    assert st.d.index[0] == pd.Timestamp("2007-01-31")         # 250 営業日そろう最初の月末（2006-12）の翌月
    assert st.d.index[-1] == pd.Timestamp("2010-02-28")        # 2010-03 は途中
    assert st.d.notna().all() and len(st.d) == 38
    assert (st.d.index == st.d.index.normalize()).all()
    # 休場日なのに前の日の値のまま入っている日は落とすので、足しても d は同じ
    sat = df.loc[["2008-05-02"]].set_axis([pd.Timestamp("2008-05-03")])   # 土曜に金曜の値のままの行
    st2 = W.compute(_write(tmp_path, pd.concat([df, sat]).sort_index()))
    pd.testing.assert_series_equal(st2.d, st.d)
    assert any("1 日を落とした" in n for n in st2.notes)


def test_rows_without_any_price_are_dropped(tmp_path):
    """値の 1 つも無い行（取り直しで消えた銘柄の日付だけが残ったもの）は落とす。残すと次の日のリターンが全部の株で消え、
    月末（土曜の 2008-05-31）にあると、その月末に持つ株を決められない。値のある日と同じ日付の空の行（04-14）も、同じ日が 2 回とはしない。"""
    df = _random(seed=4)
    st = W.compute(_write(tmp_path, df))
    empty = pd.DataFrame(np.nan, index=pd.to_datetime(["2008-04-12", "2008-04-14", "2008-05-31"]), columns=df.columns)
    st2 = W.compute(_write(tmp_path, pd.concat([df, empty]).sort_index()))
    pd.testing.assert_series_equal(st2.d, st.d)
    assert any("合わせて 3 日を落とした" in n for n in st2.notes)
    ck = W.check(tmp_path)
    assert ck.ok and "合わせて 3 日" in ck.lines[0]


def test_timezone_dates(tmp_path):
    """時差の付いた日付（全部・混ざったもの）は、その土地の日付のまま。落ちずに同じ d になる。"""
    df = _random(n_stocks=15, seed=6)
    a = W.compute(_write(tmp_path, df)).d
    path = tmp_path / W.FILE
    lines = path.read_text().splitlines()
    tz = [lines[0]] + [ln[:10] + " 00:00:00+09:00" + ln[10:] for ln in lines[1:]]
    path.write_text("\n".join(tz) + "\n")
    pd.testing.assert_series_equal(W.compute(tmp_path).d, a)
    assert W.check(tmp_path).ok
    mixed = [lines[0]] + [ln[:10] + (" 00:00:00+09:00" if k % 2 else "") + ln[10:] for k, ln in enumerate(lines[1:])]
    path.write_text("\n".join(mixed) + "\n")
    pd.testing.assert_series_equal(W.compute(tmp_path).d, a)
    assert W.check(tmp_path).ok


def test_no_look_ahead(tmp_path):
    """2008-06-10 より後の値を変えても、2008-05 までの d は変わらない（6 月からは変わってよい）。"""
    df = _random(seed=3)
    base_d = W.compute(_write(tmp_path, df)).d
    df2 = df.copy()
    later = df2.index > pd.Timestamp("2008-06-10")
    rng = np.random.default_rng(9)
    df2.loc[later] = df2.loc[later].to_numpy() * rng.uniform(0.8, 1.25, (int(later.sum()), df2.shape[1]))
    new_d = W.compute(_write(tmp_path, df2)).d
    cut = pd.Timestamp("2008-05-31")
    pd.testing.assert_series_equal(new_d[new_d.index <= cut], base_d[base_d.index <= cut], check_freq=False)
    assert not np.allclose(new_d[new_d.index > cut], base_d[base_d.index > cut])
    short = W.compute(_write(tmp_path, df[df.index <= pd.Timestamp("2008-06-20")])).d
    assert short.index[-1] == cut
    pd.testing.assert_series_equal(short, base_d[base_d.index <= cut], check_freq=False)


# --------------------------------------------------------------------------- 点検と NotReady
def test_check_ok_has_no_return_numbers(tmp_path):
    ck = W.check(_write(tmp_path, _random()))
    assert ck.ok and ck.key == "W3" and ck.why == ""
    text = "\n".join(ck.lines)
    assert "2006-12" in text and "2007-01" in text and "40 銘柄" in text
    assert "%" not in text


def test_missing_file(tmp_path):
    with pytest.raises(base.NotReady, match="無い"):
        W.compute(tmp_path)
    ck = W.check(tmp_path)
    assert not ck.ok and "無い" in ck.why


@pytest.mark.parametrize("how, match", [
    ("column", "7203.T"), ("text", "数でない"), ("date", "日付"), ("dup", "2 回"), ("zero", "0 以下"), ("gap", "2008-03"),
    ("inf", "無限"),
])
def test_bad_shapes(tmp_path, how, match):
    df = _random(n_stocks=12)
    if how == "column":
        df = df.rename(columns={"1300.T": "SPY"})
    elif how == "zero":
        df.iloc[500, 3] = 0.0
    elif how == "inf":
        df.iloc[500, 3] = np.inf
    elif how == "gap":
        df = df[(df.index < "2008-03-01") | (df.index > "2008-03-31")]
    data = _write(tmp_path, df)
    path = tmp_path / W.FILE
    if how in ("text", "date", "dup"):
        lines = path.read_text().splitlines()
        if how == "text":
            lines[300] = lines[300].replace(",", ",abc,", 1).rsplit(",", 1)[0]
        elif how == "date":
            lines[300] = "2008-13-45" + lines[300][10:]
        else:
            lines.insert(300, lines[300])
        path.write_text("\n".join(lines) + "\n")
    with pytest.raises(base.NotReady, match=match):
        W.compute(data)
    ck = W.check(data)
    assert not ck.ok and ck.why


def test_too_short(tmp_path):
    data = _write(tmp_path, _random(start="2006-01-04", end="2006-12-20"))
    with pytest.raises(base.NotReady, match="終わった月が無い"):
        W.compute(data)
    assert not W.check(data).ok
