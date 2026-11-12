"""W7 金利の高い通貨（docs/wide.md 4 章）。作ったデータだけで確かめる（FRED には問い合わせない）。"""

import numpy as np
import pandas as pd
import pytest

from cfdbot.wide import base
from cfdbot.wide import w7 as W

CCY = list(W.FX)


def _fred(data, sid: str, dates, values, header: str = "observation_date") -> None:
    """FRED と同じ形の CSV（欠けは「.」）。"""
    path = data / W.DIR / f"{sid}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [f"{pd.Timestamp(d):%Y-%m-%d},{'.' if pd.isna(v) else repr(float(v))}" for d, v in zip(dates, values)]
    path.write_text(f"{header},{sid}\n" + "\n".join(rows) + "\n")


def make(data, seed: int = 0, end: str = "2025-03-14", rate_end: str = "2025-02-01", stop: dict | None = None):
    """19 の系列を作って書く。為替は日ごと（逆数の形の通貨は「1 ドル = いくら」で書く）、金利は月ごと（月初の日付）。"""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("1998-10-01", end)
    months = pd.date_range("1998-10-01", rate_end, freq="MS")
    for i, (c, (sid, inv)) in enumerate(W.FX.items()):
        usd = np.exp(np.cumsum(rng.normal(0, 0.006, len(days))))   # 1 単位 = 何ドル
        _fred(data, sid, days, 1 / usd if inv else usd, header="DATE" if i % 2 else "observation_date")
        lvl = 1.0 + i * 0.6 + np.cumsum(rng.normal(0, 0.15, len(months)))
        if stop and c in stop:
            lvl = np.where(months > pd.Timestamp(stop[c]), np.nan, lvl)
        _fred(data, W.RATE[c], months, lvl)
    _fred(data, W.US, months, 3.0 + np.cumsum(rng.normal(0, 0.1, len(months))))
    return data


def _tables(fx_rows: dict, diff_rows: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    idx = pd.DatetimeIndex(list(fx_rows))
    fx = pd.DataFrame([{c: v.get(c, 1.0) for c in CCY} for v in fx_rows.values()], index=idx)
    diff = pd.DataFrame([{c: v.get(c) for c in CCY} for v in diff_rows.values()], index=idx, dtype=float)
    return fx, diff


# --------------------------------------------------------------------------- 手で計算した小さな例
def test_build_by_hand():
    t0, t1, t2, t3 = "1998-12-31", "1999-01-31", "1999-02-28", "1999-03-31"
    r0 = {"AUD": 4, "CAD": 1, "CHF": -2, "EUR": 0, "GBP": 3, "JPY": -3, "NOK": 2, "NZD": 5, "SEK": 0}
    r1 = dict(r0, EUR=0.5, GBP=1.5)
    r2 = dict(r1, NZD=np.nan)                             # 2 月は NZD の金利が無い → 8 通貨で作る
    f1 = {"NZD": 1.01, "AUD": 1.02, "GBP": 0.99, "CHF": 1.03, "JPY": 0.98}
    f3 = dict(f1, AUD=1.02 * 1.1)
    fx, diff = _tables({t0: {}, t1: f1, t2: f1, t3: f3}, {t0: r0, t1: r1, t2: r2, t3: r1})
    out, info = W._build(fx, diff)
    assert list(out.index) == [pd.Timestamp(t) for t in (t1, t2, t3)]
    # 1 月: 買い NZD・AUD・GBP、売り SEK・CHF・JPY（EUR と SEK は同じ 0 で、記号の順で EUR が上）。最初の月は入れ替え 100%
    x = {"NZD": 0.01 + 5 / 1200, "AUD": 0.02 + 4 / 1200, "GBP": -0.01 + 3 / 1200, "SEK": 0.0, "CHF": 0.03 - 2 / 1200, "JPY": -0.02 - 3 / 1200}
    g1 = ((x["NZD"] + x["AUD"] + x["GBP"]) / 3 - (x["SEK"] + x["CHF"] + x["JPY"]) / 3) / 2
    # 2 月: 為替は動かず、金利の差だけ。買い NZD・AUD・NOK（GBP → NOK の 1 つが入れ替わり）、売りは同じ
    g2 = ((5 + 4 + 2) / 1200 / 3 - (0 - 2 - 3) / 1200 / 3) / 2
    # 3 月: NZD を外した 8 通貨。買い AUD・NOK・GBP（NZD → GBP の 1 つ）、AUD が 10% 上がる
    g3 = ((0.1 + 4 / 1200 + 2 / 1200 + 1.5 / 1200) / 3 - (0 - 2 / 1200 - 3 / 1200) / 3) / 2
    assert np.allclose(out["gross"], [g1, g2, g3])
    assert np.allclose(out["turn"], [1.0, 1 / 6, 1 / 6])
    assert list(out["n_ccy"]) == [9, 9, 8]
    assert info["no_fx"] == 0
    d = out["gross"] - out["turn"] * W.COST
    assert d.iloc[0] == pytest.approx(g1 - 0.0005) and d.iloc[1] == pytest.approx(g2 - 0.0005 / 6)


def test_month_with_7_currencies_is_not_made():
    """2 通貨の金利が無い月（7 通貨）は作らない（0 にもしない）。次の月は前の持ち物が無いので入れ替え 100%。"""
    ms = ["1999-01-31", "1999-02-28", "1999-03-31", "1999-04-30"]
    r = {c: float(i) for i, c in enumerate(CCY)}
    r7 = dict(r, AUD=np.nan, CAD=np.nan)
    fx, diff = _tables({m: {} for m in ms}, {ms[0]: r, ms[1]: r7, ms[2]: r, ms[3]: r})
    out, _ = W._build(fx, diff)
    assert list(out.index) == [pd.Timestamp("1999-02-28"), pd.Timestamp("1999-04-30")]
    assert list(out["turn"]) == [1.0, 1.0]
    # 為替が動かないので、得るのは金利の差だけ: 買い SEK・NZD・NOK（8・7・6）、売り CHF・CAD・AUD（2・1・0）
    assert out["gross"].iloc[0] == pytest.approx(((8 + 7 + 6) / 3 - (2 + 1 + 0) / 3) / 1200 / 2)


def test_start_and_missing_next_fx():
    """1998-11 末に決めた分（1998-12 の d）は入れない。持った通貨の翌月の為替が無い月は使わない。"""
    ms = ["1998-11-30", "1998-12-31", "1999-01-31", "1999-02-28"]
    r = {c: float(i) for i, c in enumerate(CCY)}
    fx, diff = _tables({ms[0]: {}, ms[1]: {}, ms[2]: {}, ms[3]: {"SEK": np.nan}}, {m: r for m in ms})
    out, info = W._build(fx, diff)
    assert list(out.index) == [pd.Timestamp("1999-01-31")]  # 1999-02 は SEK（買い）の為替が無い
    assert out["turn"].iloc[0] == 1.0 and info["no_fx"] == 1


# --------------------------------------------------------------------------- ファイルから
def test_read_both_headers_dots_and_inverse(tmp_path):
    make(tmp_path)
    days = pd.to_datetime(["1999-03-30", "1999-03-31", "1999-04-01"])
    _fred(tmp_path, "DEXJPUS", days, [120.0, np.nan, 125.0], header="DATE")   # 3 月の最後の日は「.」
    _fred(tmp_path, "DEXUSAL", days, [0.6, 0.62, 0.61])
    fx, diff, _, span = W._tables(tmp_path)
    assert fx.at[pd.Timestamp("1999-03-31"), "JPY"] == pytest.approx(1 / 120)   # 「.」を飛ばした月末の最後の値の逆数
    assert fx.at[pd.Timestamp("1999-03-31"), "AUD"] == pytest.approx(0.62)
    assert pd.isna(fx.at[pd.Timestamp("1999-04-30"), "JPY"])                   # 最後の日の月（途中かもしれない）は使わない
    rate = pd.read_csv(tmp_path / W.DIR / f"{W.RATE['GBP']}.csv")
    us = pd.read_csv(tmp_path / W.DIR / f"{W.US}.csv")
    assert diff.at[pd.Timestamp("1999-01-31"), "GBP"] == pytest.approx(rate.iloc[3, 1] - us.iloc[3, 1])   # 1999-01-01 の行
    assert span["DEXJPUS"] == (pd.Timestamp("1999-03-30"), pd.Timestamp("1999-04-01"))


def test_compute_and_check(tmp_path):
    data = make(tmp_path)
    st = W.compute(data)
    assert st.key == "W7" and st.events is None
    assert st.d.index[0] == pd.Timestamp("1999-01-31")
    assert st.d.index[-1] == pd.Timestamp("2025-02-28")   # 為替は 3 月の途中まで。金利は 2 月まで
    assert len(st.d) == 26 * 12 + 2 and st.d.notna().all()
    assert (st.d.index == st.d.index.normalize()).all()
    assert np.isfinite(st.gross) and any("1999-01" in n for n in st.notes)
    ck = W.check(data)
    assert ck.ok and ck.why == ""
    text = "\n".join(ck.lines)
    assert "DEXSDUS" in text and "2025-02" in text and "%" not in text


def test_no_look_ahead(tmp_path):
    """2010-06-10 より後の為替と 2010-06 からの金利を変えても、2010-05 までの d は変わらない。"""
    data = make(tmp_path, seed=4)
    before = W.compute(data).d
    rng = np.random.default_rng(1)
    for sid, _ in W.FX.values():
        df = pd.read_csv(data / W.DIR / f"{sid}.csv")
        later = pd.to_datetime(df.iloc[:, 0]) > pd.Timestamp("2010-06-10")
        df.loc[later, sid] = df.loc[later, sid] * rng.uniform(0.7, 1.3, int(later.sum()))
        df.to_csv(data / W.DIR / f"{sid}.csv", index=False)
    for sid in [*W.RATE.values(), W.US]:
        df = pd.read_csv(data / W.DIR / f"{sid}.csv")
        later = pd.to_datetime(df.iloc[:, 0]) >= pd.Timestamp("2010-06-01")
        df.loc[later, sid] = rng.normal(0, 5, int(later.sum()))
        df.to_csv(data / W.DIR / f"{sid}.csv", index=False)
    after = W.compute(data).d
    cut = pd.Timestamp("2010-05-31")
    pd.testing.assert_series_equal(after[after.index <= cut], before[before.index <= cut], check_freq=False)
    assert not np.allclose(after[after.index > cut], before[before.index > cut])


# --------------------------------------------------------------------------- 止める決まりと NotReady
def test_stale_rates_stop(tmp_path):
    """2 通貨の金利が 2024-05 で止まると 7 通貨になり、d の最後の月が 2024-06（2024-12 より前）→ 計算しない。"""
    data = make(tmp_path, stop={"AUD": "2024-05-01", "CHF": "2024-05-01"})
    with pytest.raises(base.NotReady, match="2024-06"):
        W.compute(data)
    ck = W.check(data)
    assert not ck.ok and "2024-06" in ck.why and "2024-12" in ck.why
    assert any("AUD 2024-05" in line for line in ck.lines)


def test_one_stale_rate_is_fine(tmp_path):
    """1 通貨だけ止まっても 8 通貨あるので続く。"""
    data = make(tmp_path, stop={"CHF": "2020-12-01"})
    st = W.compute(data)
    assert st.d.index[-1] == pd.Timestamp("2025-02-28")
    assert any("8 つだけの月" in n for n in st.notes)


def test_missing_and_malformed(tmp_path):
    data = make(tmp_path)
    (data / W.DIR / "DEXSZUS.csv").unlink()
    with pytest.raises(base.NotReady, match="DEXSZUS"):
        W.compute(data)
    ck = W.check(data)
    assert not ck.ok and "DEXSZUS" in ck.why
    (data / W.DIR / "DEXSZUS.csv").write_text("DATE,VALUE\n2020-01-01,1.0\n")
    with pytest.raises(base.NotReady, match="形が違う"):
        W.compute(data)
    (data / W.DIR / "DEXSZUS.csv").write_text("DATE,DEXSZUS\n2020-01-01,abc\n")
    with pytest.raises(base.NotReady, match="数でない"):
        W.compute(data)
    (data / W.DIR / "DEXSZUS.csv").write_text("<html>Not Found</html>\n")
    assert not W.check(data).ok


def test_real_world_csv_quirks(tmp_path):
    """BOM・欠けが空欄（「.」でない新しい形）・CRLF は読める。空のファイル・見出しだけは NotReady（点検は落ちない）。"""
    data = make(tmp_path)
    base_d = W.compute(data).d
    p = data / W.DIR / "DEXUSAL.csv"
    orig = p.read_text()
    p.write_bytes(("﻿" + orig.replace(",.\n", ",\n")).replace("\n", "\r\n").encode("utf-8"))
    pd.testing.assert_series_equal(W.compute(data).d, base_d)
    for txt, why in (("", "読めない"), ("observation_date,DEXUSAL\n", "1 つも無い")):
        p.write_text(txt)
        with pytest.raises(base.NotReady, match=why):
            W.compute(data)
        ck = W.check(data)
        assert not ck.ok and why in ck.why


def test_rate_must_be_monthly(tmp_path):
    data = make(tmp_path)
    _fred(data, W.US, pd.to_datetime(["2020-01-01", "2020-01-15"]), [1.0, 1.1])
    with pytest.raises(base.NotReady, match="月ごと"):
        W.compute(data)


# --------------------------------------------------------------------------- 取得（FRED の代わりを差し込む）
def test_fetch_politely(tmp_path, monkeypatch):
    urls, waits = [], []

    def get(url, timeout=60.0):
        urls.append(url)
        sid = url.split("id=")[1]
        if sid == "DEXJPUS":
            raise OSError("回線の不調")
        if sid == "DEXUSUK":
            return b"<html>error</html>"
        return f"observation_date,{sid}\n2024-01-02,1.5\n2024-01-03,.\n".encode()

    monkeypatch.setattr(W, "_get", get)
    monkeypatch.setattr(W, "_sleep", waits.append)
    with pytest.raises(RuntimeError, match="DEXUSUK、DEXJPUS"):
        W.fetch(tmp_path, log=lambda *_: None)
    assert all(u.startswith("https://fred.stlouisfed.org/graph/fredgraph.csv?id=") for u in urls)
    assert len(urls) == len(W.SERIES) + 4                  # 取れない 2 つは 3 回ずつ
    assert len(waits) == len(urls) - 1 and min(waits) >= 1.0
    saved = sorted(p.stem for p in (tmp_path / W.DIR).glob("*.csv"))
    assert saved == sorted(set(W.SERIES) - {"DEXJPUS", "DEXUSUK"})
    assert not list((tmp_path / W.DIR).glob("*.tmp"))
    assert len(W.SERIES) == 19 and len(set(W.SERIES)) == 19
