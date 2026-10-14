import io
import zipfile

import numpy as np
import pandas as pd

from cfdbot import stocks as S


def _index_with_tom(n_years=20, seed=0, bump=0.004):
    """月替わりの 4 日だけ平均が高い指数。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2000-01-03", periods=252 * n_years)
    r = rng.normal(0.0, 0.01, len(idx))
    per = idx.to_period("M")
    first = np.flatnonzero(per[1:] != per[:-1]) + 1
    for b in first:
        r[b - 1:b + 3] += bump
    return pd.Series(100 * np.exp(np.cumsum(np.log1p(r))), index=idx)


def test_judge_needs_both_halves_late_t_and_deflated_t():
    idx = pd.date_range("2000-01-31", periods=240, freq="ME")
    rng = np.random.default_rng(1)
    strong = pd.Series(0.01 + rng.normal(0, 0.02, 240), index=idx)
    j = S.judge(strong, "2010-01-01")
    assert j["verdict"] == "使えそう" and j["t"] > j["threshold"] > 2.2 and j["n"] == 240
    assert S.judge(strong, "2010-01-01", placebo_pct=0.80)["verdict"] == "使えない"      # 偶然と区別できない
    faded = strong.copy()
    faded["2010":] = rng.normal(0, 0.02, len(faded["2010":]))                         # 論文の後に消えた
    assert S.judge(faded - faded["2010":].mean() - 0.001, "2010-01-01")["verdict"] == "使えない"
    z = rng.normal(0, 1, (2, 120))
    z = ((z - z.mean(axis=1, keepdims=True)) / z.std(axis=1, ddof=1, keepdims=True)).ravel()   # 前半・後半とも平均 0
    weak = pd.Series(0.02 * z + 0.02 * 2.0 / np.sqrt(240), index=idx)                 # 全期間の t 値が 2 くらい
    jw = S.judge(weak, "2010-01-01")
    assert jw["verdict"] == "まだ判断できない" and 1.9 < jw["t"] < 2.1 < jw["threshold"] and jw["t2"] < S.T_MIN
    assert S.judge(weak - 0.002, "2010-01-01")["verdict"] == "使えない"


def test_tom_diff_finds_the_window_and_placebo_does_not():
    c = _index_with_tom()
    d = S.tom_diff(c)
    assert 0.003 < d.mean() < 0.005 and S.t_value(d) > 5
    assert abs(S.tom_diff(c, 0.004).mean() - (d.mean() - 0.001)) < 1e-12              # コストは 4 日で割る
    assert d.index[0].day <= 3 and len(d) == len(c.index.to_period("M").unique()) - 2
    mask = S.tom_mask(c.index)
    assert mask.sum() == 4 * (len(c.index.to_period("M").unique()) - 1) and c.pct_change()[mask].mean() > c.pct_change()[~mask].mean() + 0.003
    null = S.tom_placebo(c, 200)
    assert np.mean(null < d.mean()) > 0.99 and abs(null.mean()) < 0.001
    flat = _index_with_tom(bump=0.0)
    assert abs(S.t_value(S.tom_diff(flat))) < 3


def test_season_blocks_and_diff():
    idx = pd.bdate_range("1990-01-01", "2010-12-31")
    month = idx.month
    r = np.where((month >= 11) | (month <= 4), 0.001, -0.0005)                         # 11〜4 月だけ上がる
    c = pd.Series(100 * np.exp(np.cumsum(r)), index=idx)
    b = S.season_blocks(c, 5)
    assert b.shape[1] == 12 and b.index[0] == pd.Timestamp("1990-05-01") and b.index[-1] == pd.Timestamp("2009-05-01")
    d = S.season_diff(b, tuple(range(6, 12)), 0.001)
    assert (d > 0.15).all() and abs(d.mean() - (S.season_diff(b, tuple(range(6, 12))).mean() - 0.001)) < 1e-12
    null = S.season_placebo(b, 6, 200)
    assert np.mean(null < d.mean()) == 1.0
    first_half = S.season_diff(S.season_blocks(c, 1), tuple(range(0, 6)))
    assert abs(first_half.mean()) < 0.1                                               # 1〜6 月と 7〜12 月は同じくらい


def test_night_day_split_and_cost():
    idx = pd.bdate_range("2020-01-01", periods=300)
    close = pd.DataFrame({"A": 100 * 1.001 ** np.arange(300)}, index=idx)
    open_ = close.shift(1) * 1.002                                                    # 夜に +0.2%、昼に −0.1%
    n, d = S.night_day(open_, close)
    assert abs(n["A"].iloc[5] - 0.002) < 1e-9 and abs(d["A"].iloc[5] + 0.000998) < 1e-5
    m = S.night_day_diff(n["A"], d["A"])
    assert m.iloc[1] > 0.05 and S.night_day_diff(n["A"], d["A"], 0.01).iloc[1] < 0   # 毎日のコストで負ける
    wild = close.copy()
    wild.iloc[10] = 1000.0
    assert S.night_day(open_, wild)[1]["A"].max() <= S.CLIP
    many = pd.DataFrame({k: 100.0 + np.arange(300) * (i + 1) for i, k in enumerate("ABCDE")}, index=idx)
    many.iloc[20] = many.iloc[19]                                                     # 休場日: 全銘柄が前日のまま
    many.iloc[40, :2] = many.iloc[39, :2]                                             # 2 銘柄だけ動かない日は残す
    o2, c2 = S.drop_closed_days(many, many)
    assert len(c2) == 299 and idx[20] not in c2.index and idx[40] in c2.index and o2.index.equals(c2.index)


def test_top_n_active_picks_winners_and_counts_turnover():
    idx = pd.date_range("2000-01-31", periods=120, freq="ME")
    rng = np.random.default_rng(3)
    drift = np.linspace(-0.01, 0.01, 12)                                              # 後ろの列ほど毎月よく上がる
    ret = pd.DataFrame(drift + rng.normal(0, 0.002, (120, 12)), index=idx, columns=[f"S{i}" for i in range(12)])
    res = S.top_n_active(ret, S.momentum_score(ret), 3, cost=0.01)
    assert res.index[0] == idx[6] and res["gross"].mean() > 0.006
    assert res["turnover"].iloc[0] == 1.0 and res["turnover"].iloc[1:].mean() < 0.2
    assert abs((res["gross"] - res["d"] - res["turnover"] * 0.01).abs().max()) < 1e-12
    null = S.top_n_placebo(ret, S.momentum_score(ret), 3, 200)
    assert abs(null.mean()) < 0.001 and np.mean(null < res["gross"].mean()) == 1.0
    assert list(S.top_n_now(S.momentum_score(ret), 3).index) == ["S11", "S10", "S9"]


def test_seasonal_score_uses_same_calendar_month_only():
    idx = pd.date_range("1990-01-31", periods=360, freq="ME")
    ret = pd.DataFrame({"JAN": np.where(idx.month == 1, 0.05, 0.0), "FLAT": 0.004}, index=idx)
    sc = S.seasonal_score(ret)
    dec = sc[sc.index.month == 12].dropna()                                           # 12 月末 → 翌月は 1 月
    assert np.allclose(dec["JAN"], 0.05) and abs(dec["FLAT"].iloc[0] - 0.004) < 1e-12
    assert np.allclose(sc[sc.index.month == 5]["JAN"].dropna(), 0.0)
    assert sc.iloc[:119]["JAN"].isna().all() and np.isfinite(sc["JAN"].iloc[119])     # 最低 10 年


def test_group_returns_and_monthly_returns():
    idx = pd.bdate_range("2020-01-01", "2020-04-30")
    close = pd.DataFrame({k: 100.0 + i + np.arange(len(idx)) for i, k in enumerate("ABCDEFG")}, index=idx)
    m = S.monthly_returns(close)
    assert len(m) == 4 and m.index[1] == pd.Timestamp("2020-02-29")
    assert abs(m["A"].iloc[1] - (close["A"]["2020-02"].iloc[-1] / close["A"]["2020-01"].iloc[-1] - 1)) < 1e-12
    bad = close.copy()
    bad.loc[idx[30], "A"] *= 1000                                                     # 1 日だけ 1,000 倍（データの誤り）
    assert abs(S.monthly_returns(bad)["A"].iloc[1]) < 0.2 and S.monthly_returns(bad)["B"].equals(m["B"])
    gap = close.copy()
    gap.loc["2020-02", "C"] = np.nan
    assert np.isnan(S.monthly_returns(gap)["C"].iloc[1])
    g = S.group_returns(m, {**{k: "X" for k in "ABCDE"}, "F": "Y", "G": "Y"})
    assert list(g.columns) == ["X"] and abs(g["X"].iloc[1] - m[list("ABCDE")].iloc[1].mean()) < 1e-12


def test_dividend_month_sets_diff_and_placebo():
    idx = pd.date_range("2010-01-31", periods=60, freq="ME")
    tick = [f"Q{i}" for i in range(30)] + [f"H{i}" for i in range(30)]
    rows = []
    for t in idx:
        for k in tick:
            pays = t.month % 3 == 0 if k.startswith("Q") else t.month % 3 == 1        # Q は 3・6・9・12 月、H は 1・4・7・10 月
            if pays:
                rows.append((t - pd.Timedelta(days=3), k, 1.0))
    div = pd.DataFrame(rows, columns=["date", "ticker", "amount"])
    ex = S.ex_month_flags(div, idx, tick)
    assert ex.loc["2010-03-31", "Q0"] and not ex.loc["2010-03-31", "H0"] and ex.loc["2010-04-30", "H0"]
    rng = np.random.default_rng(5)
    ret = pd.DataFrame(rng.normal(0, 0.01, (60, 60)), index=idx, columns=tick) + ex.to_numpy() * 0.02   # 配当の月に +2%
    treat, ctrl = S.dividend_month_sets(ret, ex)
    assert not treat[:12].any() and treat[14, :30].all() and ctrl[14, 30:].all() and not (treat & ctrl).any()
    res = S.dividend_month_diff(ret, ex, 0.003)
    assert res.index[0] == idx[12] and len(res) == 32 and 0.017 < res["gross"].mean() < 0.023   # 2・5・8・11 月は誰も配当が無い
    assert abs((res["gross"] - res["d"]).iloc[0] - 0.003) < 1e-12 and (res["n_treat"] == 30).all()
    null = S.dividend_month_placebo(ret, ex, 200)
    assert abs(null.mean()) < 0.002 and np.mean(null < res["gross"].mean()) == 1.0


def test_pead_events_holdings_and_active():
    idx = pd.bdate_range("2020-01-01", periods=400)
    n = 30
    rng = np.random.default_rng(7)
    r = rng.normal(0, 0.002, (400, n))
    events = []
    for j in range(n):
        for i0 in (50, 113, 176, 239):
            good = (j + i0) % 2 == 0
            r[i0 + 1, j] += 0.05 if good else -0.05                                   # 発表の次の日に反応
            r[i0 + 2:i0 + 62, j] += 0.001 if good else -0.001                         # その後 60 日のドリフト
            events.append((f"T{j}", idx[i0], 5.0 if good else -5.0))
    events.append(("T0", idx[395] + pd.Timedelta(days=30), np.nan))                   # 先の予定は使わない
    ret = pd.DataFrame(r, index=idx, columns=[f"T{j}" for j in range(n)])
    mkt = ret.mean(axis=1)
    ev = S.pead_events(ret, mkt, pd.DataFrame(events, columns=["ticker", "date", "surprise"]))
    assert len(ev) == 4 * n and ev["good"].mean() == 0.5 and (ev["i1"] - 1).isin([50, 113, 176, 239]).all()
    first = ev[(ev["j"] == 0) & (ev["i1"] == 51)].iloc[0]
    assert first["good"] and first["end"] == 111 and first["reaction"] > 0.03
    hold = S.pead_holdings(ev, ev["good"].to_numpy(), ret.shape)
    assert not hold[51, 0] and hold[52, 0] and hold[111, 0] and not hold[112, 0]      # 買った次の日から 60 日
    assert not hold[115:176, 0].any() and hold[178, 0]                                # 次が悪い決算なら持たない
    res = S.pead_active(ret, mkt, hold, cost=0.002)
    on = res["names"] >= S.MIN_NAMES
    assert on.sum() > 200 and 0.0007 < res["gross"][on].mean() < 0.0013
    assert res["turnover"].iloc[52] == 0.5 and res["d"].iloc[52] < res["gross"].iloc[52]   # 買い始めは片道
    assert (res["gross"][~on] == 0).all()
    null = S.pead_placebo(ret, mkt, ev, 30)
    assert np.mean(null < res["gross"].mean()) == 1.0
    d = S.month_sum(res["d"])
    assert d.iloc[0] != 0 and d.index[0] == pd.Timestamp("2020-03-31")


def test_parse_french_and_read_xlsx():
    text = ("This file was created by test\n\n  Average Value Weighted Returns -- Monthly\n,A ,B\n192607,   1.00, -99.99\n192608,  -2.50,   3.00\n\n"
            "  Average Equal Weighted Returns -- Monthly\n,A ,B\n192607,   9.00,   9.00\n\n  Annual\n,A ,B\n  1926,  5.00,  6.00\n")
    vw = S.parse_french(text, "Value Weight")
    assert list(vw.columns) == ["A", "B"] and vw.index[0] == pd.Timestamp("1926-07-31")
    assert abs(vw["A"].iloc[1] + 0.025) < 1e-12 and np.isnan(vw["B"].iloc[0]) and len(vw) == 2
    assert S.parse_french(text, "Equal Weight")["A"].iloc[0] == 0.09 and len(S.parse_french(text)) == 2
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", text)
    assert S.read_french_zip(buf.getvalue(), "Value Weight").equals(vw)

    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    book = io.BytesIO()
    with zipfile.ZipFile(book, "w") as z:
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}><si><t>コード</t></si><si><t>銘柄名</t></si><si><r><t>トヨ</t></r><r><t>タ</t></r></si></sst>")
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<worksheet {ns}><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                   '<row><c r="A2"><v>7203</v></c><c r="B2" t="s"><v>2</v></c></row></sheetData></worksheet>')
    df = S.read_xlsx(book.getvalue())
    assert df.to_dict("records") == [{"コード": "7203", "銘柄名": "トヨタ"}]


def test_hourly_returns_and_profile():
    days = pd.bdate_range("2025-01-06", periods=3)
    rows = []
    price = 100.0
    for d in days:
        price *= 1.01                                                                 # 夜に +1%
        for h in ("09:00", "10:00", "11:00", "12:00", "13:00"):
            o = price * (1.005 if h == "12:00" else 1.0)                              # 昼休みに +0.5%
            c = o * (0.998 if h == "09:00" else 1.0)                                  # 朝の 1 時間は −0.2%
            rows.append((pd.Timestamp(f"{d.date()} {h}"), o, c))
            price = c
    bars = pd.DataFrame(rows, columns=["time", "open", "close"]).set_index("time")
    hr = S.hourly_returns(bars)
    assert np.isnan(hr["夜"].iloc[0]) and abs(hr["夜"].iloc[1] - 0.01) < 1e-9
    assert abs(hr["昼休み"].iloc[0] - 0.005) < 1e-9 and abs(hr["09:00"].iloc[0] + 0.002) < 1e-9 and hr["13:00"].abs().max() < 1e-12
    div = pd.Series({days[1].date(): float(bars["close"].iloc[4]) * 0.02})            # 2 日目が権利落ち日
    assert abs(S.hourly_returns(bars, div)["夜"].iloc[1] - 0.03) < 1e-9
    prof = S.profile_table([hr, hr])
    assert abs(prof.loc["09:00", "mean"] + 0.002) < 1e-9 and prof.loc["夜", "days"] == 2
