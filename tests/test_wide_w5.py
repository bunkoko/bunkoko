"""W5 公募増資の値決めの後（docs/wide.md 4 章）。作ったデータだけで確かめる（通信はしない）。"""

import numpy as np
import pandas as pd
import pytest

from cfdbot.wide import base
from cfdbot.wide import w5 as W

COLS = ["id", "time", "ticker", "company", "title", "url", "xbrl"]
TITLE = "公募による新株式発行及び株式売出しに係る発行価格及び売出価格等の決定に関するお知らせ"


def _close(days, tickers, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0005, 0.02, (len(days), len(tickers)))
    return pd.DataFrame(100 * np.exp(np.cumsum(r, axis=0)), index=days, columns=tickers)


def _write(data, close: pd.DataFrame | None, rows: list | None, where: str = "ir"):
    if close is not None:
        (data / "stocks").mkdir(parents=True, exist_ok=True)
        close.to_csv(data / "stocks" / "jp_close.csv")
    if rows is not None:
        path = data / "agents" / where / "disclosures.csv" if where else data / "agents" / "disclosures.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([(f"x{k}", t, c, "会社", title, "", "") for k, (t, c, title) in enumerate(rows)], columns=COLS).to_csv(path, index=False)
    return data


def _expected(close: pd.DataFrame, ticker: str, day, hold: int) -> tuple[pd.Timestamp, float]:
    """1 日ずつ掛け合わせて、事象の値を手で数える（部品とは別の書き方）。"""
    idx = close.index
    b = idx.get_loc(idx[idx > pd.Timestamp(day)][0])
    r = close.pct_change(fill_method=None).clip(-0.3, 0.3)
    stock = market = 1.0
    for k in range(b + 1, b + hold + 1):
        stock *= 1 + r[ticker].iloc[k]
        market *= 1 + r.iloc[k].mean()
    return idx[b], (stock - 1) - (market - 1) - 0.001


# --------------------------------------------------------------------------- 題名・同じ会社の 2 回目
def test_is_offering():
    yes = [TITLE, "自己株式の処分価格等の決定に関するお知らせ", "株式売出しに関する売出価格等の決定のお知らせ", "発行価格決定"]
    no = ["発行価格等の決定に関するお知らせ（訂正）", "新株予約権の発行価格の決定", "無担保社債の発行価格の決定", "投資口の発行価格等の決定",
          "第三者割当による新株式の発行価格の決定", "ストック・オプションとしての新株予約権の発行価格の決定", "譲渡制限付株式としての自己株式の処分の決定",
          "ｽﾄｯｸｵﾌﾟｼｮﾝの処分価格の決定", "発行価格の仮条件のお知らせ", "決算短信"]
    assert all(W.is_offering(t) for t in yes)
    assert not any(W.is_offering(t) for t in no)


def test_first_only_within_10_trading_days():
    idx = pd.bdate_range("2020-01-06", periods=40)
    pos = [0, 5, 10, 11, 22]                        # 0 を残す → 5・10 は 0 から 10 営業日のうち → 11 を残す → 22 は 11 から 11 営業日 → 残す
    ev = pd.DataFrame({"ticker": "A.T", "when": [idx[p] + pd.Timedelta(hours=15) for p in pos]})
    ev["day"] = ev["when"].dt.normalize()
    other = pd.DataFrame({"ticker": ["B.T"], "when": [idx[5]], "day": [idx[5]]})   # 別の会社は別に数える
    out = W.first_only(pd.concat([ev, other]), idx)
    assert list(out.loc[out["ticker"] == "A.T", "day"]) == [idx[0], idx[11], idx[22]]
    assert list(out.loc[out["ticker"] == "B.T", "day"]) == [idx[5]]


# --------------------------------------------------------------------------- 買って持つ部品（手で計算した小さな例）
def test_hold_events_by_hand():
    idx = pd.bdate_range("2024-01-08", periods=8)   # 1/8〜1/12、1/15〜1/17
    a = [100, 110, 99, 138.6, 138.6, 145.53, 142.6194, 144.045594]   # +10% −10% +40%（→ +30%） 0 +5% −2% +1%
    b = [50, 51, 52, np.nan, 57.2, 57.2, 60.06, 54.054]              # 1/11 は値段が無い
    c = [20, 20, 21, 21, 21, 22.05, 22.05, 22.05]
    close = pd.DataFrame({"A.T": a, "B.T": b, "C.T": c}, index=idx)
    # 全銘柄の平均（その日にリターンのある株）。B は 1/11（値段なし）と 1/12（前の日に値段なし）にリターンが無い
    m = [None, (0.10 + 0.02 + 0) / 3, (-0.10 + 52 / 51 - 1 + 0.05) / 3, (0.30 + 0) / 2, 0.0, (0.05 + 0 + 0.05) / 3,
         (-0.02 + 0.05 + 0) / 3, (0.01 - 0.10 + 0) / 3]
    ev = pd.DataFrame({"ticker": ["A.T", "B.T", "B.T", "B.T", "C.T", "A.T", "Z.T", "C.T"],
                       "day": pd.to_datetime(["2024-01-08", "2024-01-09", "2024-01-13", "2024-01-10", "2024-01-15",
                                              "2024-01-05", "2024-01-09", "2024-01-12"])})
    used, drop = W.hold_events(close, ev, hold=2)
    assert drop == {"no_ticker": 1, "before": 1, "pending": 1, "no_price": 1}
    assert list(used["buy"]) == [idx[1], idx[2], idx[5], idx[5]]     # 1/13（土）の知らせは 1/15 に買う。1/12（金）も 1/15
    assert list(used["sell"]) == [idx[3], idx[4], idx[7], idx[7]]
    st = [0.9 * 1.3 - 1,                     # A: −10% と +30%
          57.2 / 52 - 1,                     # B: 1/11 は 0%、1/12 に 52 → 57.2 の動きが入る
          1.05 * 0.9 - 1,                    # B: +5% −10%
          0.0]                               # C: 動かない
    mk = [(1 + m[2]) * (1 + m[3]) - 1, (1 + m[3]) * (1 + m[4]) - 1, (1 + m[6]) * (1 + m[7]) - 1, (1 + m[6]) * (1 + m[7]) - 1]
    assert np.allclose(used["stock"], st)
    assert np.allclose(used["market"], mk)
    assert np.allclose(used["value"], np.array(st) - np.array(mk) - 0.001)
    assert np.allclose(used["gross"], np.array(st) - np.array(mk))


def test_span_end():
    idx = pd.bdate_range("2016-01-04", "2016-06-30")
    # 値段: 6/17 に買うと売りの日が表の外 → 6 月は終わっていない → 5 月まで
    assert W.span_end(idx, 10, "2016-07-15") == pd.Timestamp("2016-05-31")
    # 開示が 6/1 までしか無い → 6 月は分からない → 5 月まで。4/30 なら 3 月まで
    assert W.span_end(idx, 10, "2016-06-01 15:00") == pd.Timestamp("2016-05-31")
    assert W.span_end(idx, 10, "2016-04-30") == pd.Timestamp("2016-03-31")
    # 20 営業日持つと 6/2 の買いから表の外 → 5 月まで。6/1 が 20 営業日目より前なら 5 月まで
    assert W.span_end(idx, 20, "2017-01-01") == pd.Timestamp("2016-05-31")
    idx2 = pd.bdate_range("2016-01-04", "2016-07-08")    # 7/8 まで: 6/9 の買いから外 → 5 月まで
    assert W.span_end(idx2, 20, "2017-01-01") == pd.Timestamp("2016-05-31")


def test_load_close_drops_closed_days(tmp_path):
    days = pd.bdate_range("2020-01-06", periods=30)
    close = _close(days, [f"{1000 + k}.T" for k in range(6)])
    close.iloc[10] = close.iloc[9]                   # 休場日なのに前日の値のまま
    close.iloc[20] = np.nan                          # 値の 1 つも無い日
    _write(tmp_path, close, None)
    got = W.load_close(tmp_path)
    assert len(got) == 28 and days[10] not in got.index and days[20] not in got.index


# --------------------------------------------------------------------------- 通しで
def _data(tmp_path, seed: int = 1):
    days = pd.bdate_range("2015-01-01", "2016-06-30")
    close = _close(days, [f"{1001 + k}.T" for k in range(8)], seed)
    rows = [
        ("2015-01-05 10:00:00", "1008.T", "決算短信"),                                     # 一覧の最初（期間の始まり）
        ("2015-03-10 15:30:00", "1001.T", TITLE),                                          # 使う
        ("2015-03-12 15:30:00", "1001.T", TITLE),                                          # 10 営業日のうちの 2 回目
        ("2015-03-20 15:00:00", "1002.T", "自己株式の処分価格等の決定に関するお知らせ"),   # 使う
        ("2015-04-01 15:00:00", "1003.T", "発行価格等の決定に関するお知らせ（訂正）"),
        ("2015-04-02 15:00:00", "9999.T", TITLE),                                          # 490 銘柄に無い
        ("2015-05-01 15:00:00", "1004.T", "新株予約権の発行価格の決定"),
        ("2015-07-04 09:00:00", "1005.T", TITLE),                                          # 土曜 → 月曜に買う
        ("2015-09-01 15:00:00", "10060", TITLE),                                           # 5 桁の会社コード → 1006.T
        ("2016-06-10 15:00:00", "1007.T", TITLE),                                          # 6 月は終わっていない → 使わない
        ("2016-06-20 12:00:00", "1008.T", "決算短信"),                                     # 一覧の最後
    ]
    return _write(tmp_path, close, rows), close


def test_compute_by_hand(tmp_path):
    data, close = _data(tmp_path)
    s = W.compute(data)
    d = s.d
    assert d.index[0] == pd.Timestamp("2015-01-31") and d.index[-1] == pd.Timestamp("2016-05-31") and len(d) == 17
    want = {}
    for t, day in [("1001.T", "2015-03-10"), ("1002.T", "2015-03-20"), ("1005.T", "2015-07-04"), ("1006.T", "2015-09-01")]:
        buy, v = _expected(close, t, day, 10)
        want.setdefault(base.month_end(buy), []).append(v)
    assert set(want) == {pd.Timestamp("2015-03-31"), pd.Timestamp("2015-07-31"), pd.Timestamp("2015-09-30")}
    for mth, vs in want.items():
        assert d[mth] == pytest.approx(np.mean(vs), abs=1e-12)
    assert (d.drop(list(want)) == 0).all()          # 事象の無い月は 0
    assert s.events == 4
    assert s.gross == pytest.approx(sum(np.mean(vs) + 0.001 for vs in want.values()) / 17, abs=1e-12)
    joined = "\n".join(s.notes)
    assert "使った 4 件" in joined and "期間の外（終わっていない月など） 1 件" in joined
    assert "題名が条件に合う開示 7 件" in joined and "490 銘柄のもの 6 件" in joined


def test_no_lookahead(tmp_path):
    data, close = _data(tmp_path / "a")
    full = W.compute(data).d
    # 12/15 より後の値段を入れ替え、後の日付の開示を足しても、11 月までの d は変わらない
    cut = pd.Timestamp("2015-12-15")
    later = close.index > cut
    noisy = close.copy()
    noisy.loc[later] = noisy.loc[later] * np.random.default_rng(9).uniform(0.7, 1.3, noisy.loc[later].shape)
    disc = pd.read_csv(data / "agents" / "ir" / "disclosures.csv", dtype=str)
    rows = list(zip(disc["time"], disc["ticker"], disc["title"])) + [("2015-12-16 15:00:00", "1003.T", TITLE)]
    other = _write(tmp_path / "b", noisy, rows)
    d2 = W.compute(other).d
    upto = full.index <= pd.Timestamp("2015-11-30")
    assert np.allclose(full[upto], d2[d2.index <= pd.Timestamp("2015-11-30")])
    # 12/15 で切ったデータ（値段も開示も）で計算した月は、全部のデータで計算した同じ月と同じ
    short = close[close.index <= cut]
    kept = [r for r in zip(disc["time"], disc["ticker"], disc["title"]) if pd.Timestamp(r[0]) <= cut]
    kept.append(("2015-12-14 10:00:00", "1008.T", "決算短信"))      # 切った日までの一覧
    d3 = W.compute(_write(tmp_path / "c", short, kept)).d
    assert d3.index[-1] == pd.Timestamp("2015-11-30")
    assert np.allclose(d3, full.reindex(d3.index))


def test_check_has_counts_but_no_returns(tmp_path):
    data, _ = _data(tmp_path)
    c = W.check(data)
    assert c.ok and c.key == "W5"
    text = "\n".join(c.lines)
    assert "題名が条件に合う開示 7 件" in text and "490 銘柄のもの 6 件" in text and "除いて 5 件" in text
    assert "d を作れる月 2015-01〜2016-05（17 か月）。その中で買う事象 4 件" in text
    assert "%" not in text                          # リターンの数字は出さない


def test_old_place_of_disclosures(tmp_path):
    days = pd.bdate_range("2015-01-01", "2015-06-30")
    _write(tmp_path, _close(days, ["1001.T", "1002.T"]), [("2015-01-05 10:00:00", "1001.T", "決算短信"),
                                                           ("2015-02-10 15:00:00", "1001.T", TITLE),
                                                           ("2015-06-20 10:00:00", "1001.T", "決算短信")], where="")
    s = W.compute(tmp_path)
    assert s.events == 1 and s.d.index[0] == pd.Timestamp("2015-01-31")


def test_no_event_in_490_gives_zero_months(tmp_path):
    days = pd.bdate_range("2015-01-01", "2015-06-30")
    _write(tmp_path, _close(days, ["1001.T", "1002.T"]), [("2015-01-05 10:00:00", "1001.T", "決算短信"),
                                                           ("2015-02-10 15:00:00", "9999.T", TITLE),
                                                           ("2015-06-20 10:00:00", "1001.T", "決算短信")])
    s = W.compute(tmp_path)
    assert s.events == 0 and len(s.d) == 5 and (s.d == 0).all() and s.gross == 0
    assert W.check(tmp_path).ok


# --------------------------------------------------------------------------- データが無い・形が違う
def test_not_ready(tmp_path):
    days = pd.bdate_range("2015-01-01", "2015-06-30")
    close = _close(days, ["1001.T"])
    rows = [("2015-02-10 15:00:00", "1001.T", TITLE)]
    cases = {
        "no_prices": lambda p: _write(p, None, rows),
        "no_disclosures": lambda p: _write(p, close, None),
        "no_title_column": lambda p: (_write(p, close, rows),
                                      pd.read_csv(p / "agents" / "ir" / "disclosures.csv").drop(columns="title")
                                      .to_csv(p / "agents" / "ir" / "disclosures.csv", index=False)),
        "bad_dates": lambda p: (_write(p, None, rows), (p / "stocks").mkdir(parents=True, exist_ok=True),
                                (p / "stocks" / "jp_close.csv").write_text(",1001.T\nきのう,100\nきょう,101\n")),
        "empty_prices": lambda p: (_write(p, None, rows), (p / "stocks").mkdir(parents=True, exist_ok=True),
                                   (p / "stocks" / "jp_close.csv").write_text("")),
        "no_matching_title": lambda p: _write(p, close, [("2015-02-10 15:00:00", "1001.T", "決算短信")]),
        "no_readable_time": lambda p: _write(p, close, [("いつか", "1001.T", TITLE)]),
        "too_short": lambda p: _write(p, close.iloc[:8], rows),
    }
    for name, make in cases.items():
        path = tmp_path / name
        path.mkdir()
        make(path)
        with pytest.raises(base.NotReady):
            W.compute(path)
        c = W.check(path)
        assert not c.ok and c.why, name


# --------------------------------------------------------------------------- 実際のファイルの癖（点検役が足した）
def test_tz_aware_price_dates_give_same_d(tmp_path):
    """値段の日付が時差つき（2015-01-05 00:00:00+09:00）でも落ちず、時差の無い日付と同じ d になる。"""
    data, close = _data(tmp_path / "a")
    want = W.compute(data).d
    tz = close.copy()
    tz.index = tz.index.tz_localize("Asia/Tokyo")
    tz.to_csv(data / "stocks" / "jp_close.csv")
    got = W.compute(data).d
    assert got.index.equals(want.index) and np.allclose(got, want)
    assert W.check(data).ok


def test_mixed_time_formats_are_not_dropped(tmp_path):
    """開示の日時の書き方が行ごとに違っても（秒なし・T つき・時差つき）、黙って落とさない。読めない行は数を出す。"""
    data, _ = _data(tmp_path)
    want = W.compute(data)
    path = data / "agents" / "ir" / "disclosures.csv"
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.loc[3, "time"] = "2015-03-20 15:00"                       # 1002.T の事象（秒なし）
    df.loc[7, "time"] = "2015-07-04T09:00:00+09:00"              # 1005.T の事象（時差つき。ほかの行は時差なし）
    df.loc[8, "time"] = "2015-09-01T06:00:00+00:00"              # 1006.T の事象（世界時 → 日本の 15:00）
    df.to_csv(path, index=False)
    got = W.compute(data)
    assert got.events == want.events == 4 and np.allclose(got.d, want.d)
    df.loc[4, "time"] = "いつか"                                  # 読めない行（題名は条件に合うが訂正なので使わない行）
    df.to_csv(path, index=False)
    assert "日時の読めない行 1 件は使わない" in "\n".join(W.compute(data).notes)
    text = "\n".join(W.check(data).lines)
    assert "日時の読めない行 1 件は使わない" in text and "%" not in text


def test_non_positive_price_is_missing(tmp_path):
    """0 以下の値段は値段の無い日として扱う（買いの日なら、その事象は使わない）。"""
    data, close = _data(tmp_path)
    bad = close.copy()
    buy = close.index[close.index > pd.Timestamp("2015-03-20")][0]
    bad.loc[buy, "1002.T"] = 0.0
    bad.loc[pd.Timestamp("2015-05-05") <= bad.index, "1003.T"] = -1.0
    bad.to_csv(data / "stocks" / "jp_close.csv")
    s = W.compute(data)
    assert s.events == 3 and "買いか売りの日に値段が無い 1 件" in "\n".join(s.notes)
    assert np.isfinite(s.d).all()
