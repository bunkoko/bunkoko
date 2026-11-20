"""リスクとお金の置き方の点検（cfdbot/riskcheck.py）。docs/risk_plan.md 8 章。作ったデータだけ。期待する数字は手で出したもの。"""

import numpy as np
import pandas as pd
import pytest

from cfdbot import riskcheck as R


def months(n, start="2000-01-31"):
    return pd.date_range(start, periods=n, freq="ME")


# --------------------------------------------------------------------------- 月末の終値
def test_month_ends_takes_the_last_close_of_each_finished_month():
    daily = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0],
                      index=pd.to_datetime(["2024-01-30", "2024-01-31", "2024-02-28", "2024-02-29", "2024-03-01"]))
    m = R.month_ends(daily)
    assert list(m.index) == [pd.Timestamp("2024-01-31"), pd.Timestamp("2024-02-29")]     # 3 月は終わっていないので使わない
    assert m.tolist() == [11.0, 13.0]


# --------------------------------------------------------------------------- 持つ年数ごとの元本割れ（8.1）
def test_lump_is_the_end_value_over_the_start_value():
    m = pd.Series([100.0] * 12 + [50.0] + [200.0] * 12, index=months(25))
    r = R.lump(m, years=1)
    assert len(r) == 13                                           # 25 か月あれば、1 年持てる始め方は 13 通り
    assert r.iloc[0] == pytest.approx(0.5) and r.iloc[1] == pytest.approx(2.0) and r.iloc[12] == pytest.approx(4.0)


def test_saving_every_month_buys_more_when_the_price_is_low():
    m = pd.Series([100.0] * 6 + [50.0] * 6 + [100.0], index=months(13))
    r = R.saving(m, years=1)
    # 100 円で 6 回・50 円で 6 回、1 ずつ入れる → 口数は 6/100 + 6/50 = 0.18。次の月末の値 100 を掛けて 18。入れた合計は 12 → 1.5
    assert len(r) == 1 and r.iloc[0] == pytest.approx(1.5)


def test_saving_is_one_when_the_price_never_moves():
    assert R.saving(pd.Series([80.0] * 40, index=months(40)), years=3).tolist() == pytest.approx([1.0] * 4)


def test_shortfall_counts_the_starts_that_ended_below_what_was_put_in():
    ratio = pd.Series([0.9, 1.1, 1.0, 0.5, 2.0], index=months(5))
    s = R.shortfall(ratio)
    assert s["n"] == 5 and s["share"] == pytest.approx(0.4) and s["worst"] == pytest.approx(0.5) and s["median"] == pytest.approx(1.0)
    assert s["worst_start"] == months(5)[3]


def test_shortfall_of_nothing_is_empty():
    assert R.shortfall(pd.Series(dtype=float))["n"] == 0


# --------------------------------------------------------------------------- 戻し方（8.2）
def test_left_alone_the_stock_share_drifts():
    idx = pd.to_datetime(["2001-11-30", "2001-12-31", "2002-01-31"])
    value, share = R.mix_path(pd.Series([1.0, -0.5, 0.0], index=idx), 0.5, "drift")
    # 株 0.5・現金 0.5 から。+100% で株 1.0（全体 1.5、株は 2/3）。−50% で株 0.5（全体 1.0）
    assert value.tolist() == pytest.approx([1.5, 1.0, 1.0]) and share.tolist() == pytest.approx([2 / 3, 0.5, 0.5])


def test_yearly_goes_back_to_the_base_share_at_the_end_of_december():
    idx = pd.to_datetime(["2001-12-31", "2002-01-31", "2002-02-28"])
    value, share = R.mix_path(pd.Series([1.0, -0.5, 0.0], index=idx), 0.5, "yearly")
    # 12 月末: 全体 1.5 を 0.75 ずつに戻す。1 月: 株が半分になって 0.375 ＋ 現金 0.75 = 1.125
    assert value.tolist() == pytest.approx([1.5, 1.125, 1.125]) and share.iloc[0] == pytest.approx(0.5) and share.iloc[1] == pytest.approx(1 / 3)


def test_band_goes_back_only_when_the_share_is_more_than_ten_points_off():
    dec = pd.to_datetime(["2001-12-31", "2002-12-31"])
    _, small = R.mix_path(pd.Series([0.2, 0.0], index=dec), 0.5, "band")          # +20%: 株 0.6／1.1 = 54.5% → 10 ポイント以内。戻さない
    _, large = R.mix_path(pd.Series([1.0, 0.0], index=dec), 0.5, "band")          # +100%: 株 2/3 → 16.7 ポイント外れ。戻す
    assert small.iloc[0] == pytest.approx(0.6 / 1.1) and large.iloc[0] == pytest.approx(0.5)
    mid = pd.to_datetime(["2001-06-30", "2001-07-31"])
    _, june = R.mix_path(pd.Series([1.0, 0.0], index=mid), 0.5, "band")           # 12 月でなければ、外れていても戻さない
    assert june.iloc[0] == pytest.approx(2 / 3)


def test_mix_path_rejects_an_unknown_way():
    with pytest.raises(ValueError):
        R.mix_path(pd.Series([0.1], index=months(1)), 0.5, "毎日")


def test_windows_start_each_january_and_run_twenty_years():
    m = pd.Series(100.0 * 1.005 ** np.arange(12 * 23 + 6), index=months(12 * 23 + 6, "1999-07-31"))     # 1999-07〜2022-12。毎月 +0.5%
    w = R.windows(m, share=0.5, how="yearly", years=20)
    # 始められるのは、前の年の 12 月末の値があって、20 年後の 12 月末まである年: 2000・2001・2002・2003
    assert list(w.index) == [2000, 2001, 2002, 2003]
    assert (w["max_dd"] == 0).all()                               # 下がった月が無いので、下げは 0
    assert w["end_share"].tolist() == pytest.approx([0.5] * 4)    # 12 月末に戻すので、終わりは決めた割合
    grow = 0.5 * 1.005 ** 12 + 0.5                                # 1 年ぶん: 株の半分だけが増える
    assert w["annual"].iloc[0] == pytest.approx(grow - 1)


def test_drawdown_of_a_window_is_measured_from_the_peak():
    r = pd.Series([0.0] * 11 + [1.0] + [-0.5] + [0.0] * 227, index=months(240, "2000-01-31"))
    m = pd.concat([pd.Series([100.0], index=[pd.Timestamp("1999-12-31")]), 100.0 * (1 + r).cumprod()])
    w = R.windows(m, share=1.0, how="drift", years=20)
    assert list(w.index) == [2000] and w["max_dd"].iloc[0] == pytest.approx(0.5) and w["end_share"].iloc[0] == pytest.approx(1.0)


def test_summary_gives_the_middle_and_the_worst():
    w = pd.DataFrame({"max_dd": [0.1, 0.3, 0.2], "end_share": [0.4, 0.9, 0.6], "annual": [0.01, 0.03, 0.02]}, index=[2000, 2001, 2002])
    s = R.summary(w)
    assert s["n"] == 3 and s["dd_median"] == pytest.approx(0.2) and s["dd_worst"] == pytest.approx(0.3) and s["dd_worst_start"] == 2001
    assert s["share_min"] == pytest.approx(0.4) and s["share_max"] == pytest.approx(0.9) and s["annual_median"] == pytest.approx(0.02)


# --------------------------------------------------------------------------- 通しで（作った指数）
def misc_csv(root, tickers=("^GSPC", "^N225"), end="2026-11-18"):
    """./cfd stocks --fetch が書く形（日付 × 記号。時刻と時差つきの日付）。"""
    idx = pd.bdate_range("1949-01-03", end)
    rng = np.random.default_rng(0)
    cols = {t: 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, len(idx)))) for t in tickers}
    folder = root / "data" / "stocks"
    folder.mkdir(parents=True)
    pd.DataFrame(cols, index=[f"{d:%Y-%m-%d} 00:00:00-05:00" for d in idx]).to_csv(folder / "misc_close.csv")
    return root / "data"


def fred_csv(data, end="2026-11-13"):
    """./cfd wide --fetch が書く形（FRED の CSV。休みの日は「.」）。1 ドル = 何円。"""
    idx = pd.bdate_range("1971-01-04", end)
    rng = np.random.default_rng(1)
    rate = 300 * np.exp(np.cumsum(rng.normal(-0.00005, 0.006, len(idx))))
    text = [f"{v:.2f}" for v in rate]
    for k in range(7, len(text), 251):
        text[k] = "."                                             # 祝日
    folder = data / "wide" / "fred"
    folder.mkdir(parents=True)
    pd.DataFrame({"observation_date": idx.strftime("%Y-%m-%d"), "DEXJPUS": text}).to_csv(folder / "DEXJPUS.csv", index=False)
    return data


def test_the_registered_shares_and_years():
    """決め方（docs/risk_plan.md 8 章）。変えるときは、文書を先に直す。"""
    assert R.SHARES == (0.3, 0.5, 0.7, 0.8, 0.9, 1.0) and R.YEARS == (1, 3, 5, 10, 15, 20)
    assert (R.WINDOW_YEARS, R.BAND) == (20, 0.10) and list(R.WAYS) == ["drift", "yearly", "band"]


def test_in_yen_multiplies_the_month_end_values():
    m = pd.Series([100.0, 110.0, 120.0], index=months(3, "2024-01-31"))
    rate = pd.Series([151.0, 150.0, 140.0, 139.0], index=pd.to_datetime(["2024-01-29", "2024-01-30", "2024-02-27", "2024-03-04"]))
    yen = R.in_yen(m, rate)                                       # 1 月は最後の値 150、2 月は 140。3 月のドル円は終わっていない月なので使わない
    assert list(yen.index) == list(months(2, "2024-01-31")) and yen.tolist() == pytest.approx([15000.0, 15400.0])


def test_usdjpy_reads_the_saved_file_and_never_fetches(tmp_path):
    rate = R.read_usdjpy(fred_csv(tmp_path / "data"))
    assert rate.index[0] == pd.Timestamp("1971-01-04") and rate.index.is_monotonic_increasing and (rate > 0).all()
    assert pd.Timestamp("1971-01-13") not in rate.index            # 「.」の日は値が無い
    with pytest.raises(R.base.NotReady, match="wide --fetch"):
        R.read_usdjpy(tmp_path / "nothing")


def test_run_counts_the_indexes_and_the_three_ways_in_dollars_and_in_yen(tmp_path):
    res = R.run(fred_csv(misc_csv(tmp_path)))
    assert len(res["hold"]) == 3 and not res["skipped"]
    us = res["hold"][R.INDEXES["^GSPC"][0]]
    assert f"{us['start']:%Y-%m}" == "1950-01" and f"{us['end']:%Y-%m}" == "2026-10"       # 11 月は終わっていない
    assert set(us["rows"]) == set(R.YEARS) and us["rows"][20]["lump"]["n"] == 922 - 240       # 1950-01〜2026-10 は 922 か月
    assert us["rows"][20]["saving"]["n"] == 922 - 240
    assert f"{res['hold'][R.INDEXES['^N225'][0]]['start']:%Y-%m}" == "1965-01"
    yen = res["hold"][R.YEN]
    assert f"{yen['start']:%Y-%m}" == "1971-01" and f"{yen['end']:%Y-%m}" == "2026-10"
    assert list(res["mix"]) == [R.VIEWS["yen"], R.VIEWS["usd"]]   # 円で見た版を先に（利用者は円で暮らしている）
    rows = res["mix"][R.VIEWS["usd"]]["rows"]
    assert set(rows) == set(R.SHARES) and all(set(v) == set(R.WAYS) for v in rows.values())
    assert rows[0.5]["yearly"]["n"] == 56                         # 1951 年〜2006 年に始めた回（1950-12 末の値から。20 年後の 12 月末は 2025-12 まで）
    assert rows[0.5]["yearly"]["share_min"] == pytest.approx(0.5) and rows[0.5]["yearly"]["share_max"] == pytest.approx(0.5)
    assert rows[0.5]["drift"]["share_max"] > 0.5 > rows[0.5]["yearly"]["dd_worst"] - 1
    assert rows[1.0]["drift"] == rows[1.0]["yearly"] == rows[1.0]["band"]      # 株だけなら、戻す先が無いので 3 つとも同じ
    assert rows[1.0]["drift"]["dd_worst"] >= rows[0.9]["yearly"]["dd_worst"] >= rows[0.8]["yearly"]["dd_worst"] >= rows[0.7]["yearly"]["dd_worst"]
    assert res["mix"][R.VIEWS["yen"]]["rows"][0.5]["yearly"]["n"] == 35         # 円で見た版は 1971-12 末の値から: 1972 年〜2006 年に始めた回


def test_run_goes_on_without_the_yen_rate(tmp_path):
    res = R.run(misc_csv(tmp_path))                               # ドル円のファイルが無い
    assert len(res["hold"]) == 2 and "wide --fetch" in res["skipped"][R.YEN] and list(res["mix"]) == [R.VIEWS["usd"]]


def test_run_goes_on_when_one_index_is_missing(tmp_path):
    res = R.run(fred_csv(misc_csv(tmp_path, tickers=("^GSPC",))))
    assert len(res["hold"]) == 2 and "^N225" in res["skipped"][R.INDEXES["^N225"][0]] and len(res["mix"]) == 2


def test_report_shows_the_tables_and_judges_nothing(tmp_path):
    lines = R.report_lines(R.run(fred_csv(misc_csv(tmp_path))), "2026-11-21 09:00")
    text = "\n".join(lines)
    assert "元本を割って終わった割合" in text and "毎月積み立てる" in text and "戻し方" in text and "判定はしない" in text
    assert text.index("円で見た場合") < text.index("ドルで見た場合") and "ドル円" in text
    assert sum("株だけ（戻す先が無い）" in x for x in lines) == 2      # 株 100% は、円・ドルそれぞれ 1 行だけ
    assert sum(x.startswith("| 90% |") for x in lines) == 6        # 90% は、戻し方 3 通り × 円・ドル
    assert "使えそう" not in text and "使えない" not in text and "配当" in text and "円高" in text


def test_report_shows_gains_and_losses_with_a_sign():
    assert R._change(1.91) == "+91%" and R._change(0.55) == "−45%" and R._change(1.0) == "+0%"


def load_script():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "risk_check.py"
    spec = importlib.util.spec_from_file_location("risk_check_under_test", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_tool_writes_the_report(tmp_path, capsys):
    data = misc_csv(tmp_path)
    path = load_script().main(["--data", str(data), "--out", str(tmp_path / "out")])
    assert path.name == "report.md" and "## 1. 持つ年数ごとの元本割れ" in path.read_text(encoding="utf-8")
    assert "元本を割って終わった割合" in capsys.readouterr().out


def test_tool_stops_when_there_is_no_data(tmp_path):
    with pytest.raises(SystemExit, match="stocks --fetch"):
        load_script().main(["--data", str(tmp_path / "data"), "--out", str(tmp_path / "out")])


def test_a_share_that_rounds_to_zero_is_not_shown_as_zero():
    """682 回のうち 1 回だけ元本を割った、のような割合を「0%」と出すと、「いちばん悪かった回 −6%」と食い違って見える。"""
    one = R.shortfall(pd.Series([0.94] + [1.5] * 681, index=months(682)))
    assert one["below"] == 1 and R._share(one) == "1% 未満（1 回）"
    none = R.shortfall(pd.Series([1.2] * 10, index=months(10)))
    assert none["below"] == 0 and R._share(none) == "0%"
    some = R.shortfall(pd.Series([0.9] * 3 + [1.2] * 7, index=months(10)))
    assert R._share(some) == "30%"
