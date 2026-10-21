"""案 2-US の計算と判定（cfdbot/us_lazy.py。docs/us_text.md 9 章）の試験。作ったデータだけ（本物の 10-K の文は使わない）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from cfdbot import us_lazy as U

N_FIRMS, N_SECTORS = 350, 4
MONTHS = pd.date_range("2019-01-31", "2026-09-30", freq="ME")


def _world(alpha: float, seed: int = 0):
    """350 社・4 業種。毎年 2 月に 10-K（期末は前年の 12 月末）。前年から書き換えた割合 c が小さい会社ほど、次の 1 年に上がる（alpha）。"""
    rng = np.random.default_rng(seed)
    letters = np.array(list("abcdefghijklmnopqrstuvwxyz"))
    vocab = np.array(["".join(rng.choice(letters, size=rng.integers(5, 10))) for _ in range(3000)])
    years = range(2019, 2026)                                        # 期末の年
    change = rng.uniform(0, 0.5, (N_FIRMS, len(years)))
    text, rows = {}, []
    for i in range(N_FIRMS):
        words = rng.integers(0, len(vocab), 400)
        for j, y in enumerate(years):
            if j:
                pos = rng.random(len(words)) < change[i, j]
                words = np.where(pos, rng.integers(0, len(vocab), len(words)), words)
            acc = f"{i:04d}-{y}"
            text[acc] = f"Firm{i} Holdings Inc. faces {y} risks. " + " ".join(vocab[words])
            rows.append((1000 + i, acc, "10-K", pd.Timestamp(f"{y + 1}-02-{10 + i % 15}"), pd.Timestamp(f"{y}-12-31"),
                         pd.Timestamp(f"{y + 1}-02-{10 + i % 15} 16:05"), "a.htm"))
    filings = pd.DataFrame(rows, columns=["cik", "accession", "form", "filing_date", "report_date", "acceptance", "primary"])
    tick = [f"T{i:03d}" for i in range(N_FIRMS)]
    ret = rng.normal(0.008, 0.04, (len(MONTHS), 1)) + rng.normal(0, 0.05, (len(MONTHS), N_FIRMS))
    for t, h in enumerate(MONTHS[1:], start=1):
        m = MONTHS[t - 1]                                             # 選ぶ月末。2 月末から次の 1 月末までは、その年の 2 月の 10-K
        j = (m.year - 1 if m.month >= 2 else m.year - 2) - 2019
        if j >= 1:
            ret[t] += alpha * (0.25 - change[:, j]) * 4
    mclose = pd.DataFrame(100 * np.cumprod(1 + ret, axis=0), index=MONTHS, columns=tick)
    members = pd.DataFrame([(m, tick[i], f"Firm{i} Holdings Inc.", f"S{i % N_SECTORS}", 1000 + i, tick[i])
                            for m in MONTHS for i in range(N_FIRMS)],
                           columns=["month", "ticker", "name", "sector", "cik", "symbol"])
    p = U.pairs(filings, lambda a, k: text[a], lambda c: [f"Firm{c - 1000} Holdings Inc."])
    return members, U.signals(p, MONTHS), mclose, p


def test_clean_text_counts_and_cosine():
    t = U.clean_text("The Coca-Cola Company said Coca-Cola sold 1,250 cases in 2023; pineapple Apple Inc. apple",
                     ["The Coca-Cola Company", "Apple Inc."])
    assert "coca-cola" not in t and "apple inc" not in t and " apple" not in t and "pineapple" in t
    assert "0,0 cases in 0" in t and t == t.lower()
    a = U.word_counts("risk risk supply chain")
    assert a["risk"] == 2 and abs(U.cosine(a, U.word_counts("risk supply risk chain")) - 1) < 1e-12
    assert np.isnan(U.cosine(a, U.word_counts("0 0 ,")))


def _f(cik, acc, form, rep, accepted):
    return (cik, acc, form, pd.Timestamp(accepted).normalize(), pd.Timestamp(rep), pd.Timestamp(accepted), "x.htm")


def test_pairs_forms_gap_and_min_chars():
    long = " ".join(f"word{chr(97 + k % 26)}{chr(97 + k // 26 % 26)}" for k in range(300))
    rows = [_f(1, "a1", "10-K", "2019-12-31", "2020-02-20 10:00"), _f(1, "a2", "10-K", "2020-12-31", "2021-02-20 18:30"),
            _f(1, "a2b", "10-K", "2020-12-31", "2021-03-01 09:00"),      # 同じ期末の後の 10-K は使わない
            _f(1, "a2c", "10-K/A", "2020-12-31", "2021-02-01 09:00"),    # 訂正は使わない
            _f(2, "b1", "10-K", "2020-12-31", "2021-02-20"), _f(2, "b2", "10-KT", "2021-06-30", "2021-08-20"),
            _f(2, "b3", "10-K", "2022-06-30", "2022-08-20"),              # 決算期を変えた年（期末の間 546 日）は組にしない
            _f(3, "c1", "10-K", "2020-12-26", "2021-02-20"), _f(3, "c2", "10-K", "2022-01-01", "2022-02-20"),   # 53 週の年（371 日）
            _f(4, "d1", "10-K", "2020-12-31", "2021-02-20"), _f(4, "d2", "10-K", "2021-12-31", "2022-02-20")]
    fil = pd.DataFrame(rows, columns=["cik", "accession", "form", "filing_date", "report_date", "acceptance", "primary"])
    first = U.first_filings(fil)
    assert set(first["accession"]) == {"a1", "a2", "b1", "b3", "c1", "c2", "d1", "d2"}
    short = "Megacorp " * 150 + "risk " * 100                         # 社名を消すと 1,000 字に届かない
    text = {a: (short if a.startswith("d") else long + (" extra" if a.endswith("2") else "")) for a in fil["accession"]}
    p = U.pairs(fil, lambda a, k: text[a], lambda c: ["Megacorp"])
    assert p["accession"].tolist() == ["a2", "c2"] and p["cik"].tolist() == [1, 3]
    assert p.loc[0, "accepted"] == pd.Timestamp("2021-02-20") and bool(p.loc[0, "in_window"]) and not bool(p.loc[1, "in_window"])
    assert 0.99 < p.loc[0, "sim"] < 1


def test_signal_timing_and_latest_pair():
    p = pd.DataFrame({"cik": [1, 1], "accession": ["old", "new"], "accepted": pd.to_datetime(["2021-03-01", "2022-01-31"]),
                      "sim": [0.5, 0.9], "in_window": [True, False]})
    ends = pd.to_datetime(["2022-01-31", "2022-02-28", "2023-01-31", "2023-02-28"])
    s = U.signals(p, ends)
    assert s[ends[0]].loc[1, "sim"] == 0.5           # 受付の日の月末には、まだ使わない（前の組のまま）
    assert s[ends[1]].loc[1, "sim"] == 0.9           # 翌月末から使う
    assert s[ends[2]].loc[1, "sim"] == 0.9           # 受付から 365 日目まで
    assert ends[3] not in s                          # 366 日を過ぎたら使わない


def _small():
    """3 業種（10・7・4 社）と、同じ CIK の 2 つの記号。翌月の値段が無い株と、翌月が最後の値段の株。"""
    m0, m1, m2 = pd.to_datetime(["2022-01-31", "2022-02-28", "2022-03-31"])
    sec = ["A"] * 10 + ["B"] * 7 + ["C"] * 4
    tick = [f"X{i}" for i in range(21)]
    mem = pd.DataFrame([(m, t, t, s, i + 1, t) for m in (m0, m1) for i, (t, s) in enumerate(zip(tick, sec))],
                       columns=["month", "ticker", "name", "sector", "cik", "symbol"])
    dup = pd.DataFrame([(m0, "X0B", "X0", "A", 1, "X0B")], columns=mem.columns)
    mem = pd.concat([mem, dup], ignore_index=True)
    close = pd.DataFrame(100.0, index=[m0, m1, m2], columns=tick + ["X0B"])
    close.loc[m1, tick] = [100 * (1 + (i + 1) / 100) for i in range(21)]   # 翌月のリターン = (i+1)%
    close.loc[m1, "X9"] = np.nan                                            # 値段がまったく無くなった
    close.loc[m2, "X8"] = np.nan                                            # 2 月末が最後の値段
    sim = pd.DataFrame({"sim": np.linspace(0, 1, 21), "in_window": [i % 2 == 0 for i in range(21)]}, index=range(1, 22))
    return mem, {m0: sim, m1: sim}, close, (m0, m1, m2)


def test_sector_neutral_selection_delisted_and_duplicates():
    mem, sig, close, (m0, m1, m2) = _small()
    b = U.book(mem, sig, close, start=m0)
    r0 = b.loc[m1]
    assert r0["n"] == 3 and r0["firms"] == 21          # 業種 A 2 社・B 1 社・C 0 社。同じ CIK の 2 つの記号は 1 社
    assert r0["missing"] == 1                          # X9（業種 A の一番上）は翌月の値段が無い → 最後の値段で手仕舞い、その後は 0（3 章）
    assert np.isclose(r0["port"], np.mean([0.0, 0.09, 0.17]))   # X9（0）と X8（A の 2 番目）と X16（B の一番上）
    assert np.isclose(r0["mkt"], np.mean([(i + 1) / 100 if i != 9 else 0.0 for i in range(21)]))
    assert r0["turnover"] == 1.0 and np.isclose(r0["window_share"], 2 / 3)
    assert np.isclose(r0["d"], r0["gross"] - U.COST)
    assert U.book(mem, sig, close, start=m0, by_sector=False).loc[m1, "n"] == 4
    low = U.book(mem, sig, close, start=m0, highest=False).loc[m1]
    assert low["n"] == 3 and np.isclose(low["port"], np.mean([0.01, 0.02, 0.11]))
    r1 = b.loc[m2]                                     # 3 月: X9 は値段が無く一覧から外れる。X8 は翌月の値段が無い
    assert r1["firms"] == 20 and r1["missing"] == 1
    assert U.first_month(sig, mem, close, start=m0) is None   # 300 社に届かない


def test_placebo_deterministic_per_sector():
    mem, sig, close, (m0, *_) = _small()
    a = U.placebo(mem, sig, close, m0, 30, np.random.default_rng(5))
    b = U.placebo(mem, sig, close, m0, 30, np.random.default_rng(5))
    assert len(a) == 30 and np.array_equal(a, b) and np.isfinite(a).all()
    assert not np.array_equal(a, U.placebo(mem, sig, close, m0, 30, np.random.default_rng(6)))


def test_caps_and_final_verdict():
    idx = pd.date_range("2022-01-31", periods=48, freq="ME")
    b = pd.DataFrame({"gross": 0.004, "turnover": 0.1, "window_share": 0.0, "turnover_rebal": 0.2}, index=idx)
    b.loc[idx[:2], "window_share"] = 1.0
    b["d"] = b["gross"] - b["turnover"] * U.COST
    c = U.caps(b)
    assert c["cost_1pct"] and c["window_excluded"] and c["years"] and c["years_total"] == 4
    b.loc[idx[2:], "gross"] = -0.0005
    b.loc[idx[:2], "gross"] = 0.05                     # 改正の年の組だけで稼いでいる
    b["d"] = b["gross"] - b["turnover"] * U.COST
    c = U.caps(b)
    assert b["d"].mean() > 0 and not c["window_excluded"] and not c["years"] and c["years_positive"] == 1
    assert not U.caps(b.assign(turnover=1.0, d=b["gross"] - U.COST))["cost_1pct"]
    assert U.final_verdict("使えそう", c) == "まだ判断できない" and U.final_verdict("使えない", c) == "使えない"
    assert abs(U.newey_west_t(b["d"], lags=0) - b["d"].mean() / (b["d"].std(ddof=0) / np.sqrt(len(b)))) < 1e-9


def test_effect_world_is_found_and_report():
    mem, sig, close, p = _world(alpha=0.01)
    assert len(p) == N_FIRMS * 6 and p["in_window"].sum() == N_FIRMS     # 2021 年 2 月の組だけが改正の年
    r = U.evaluate(mem, sig, close, np.random.default_rng(1), sig_mdna=sig, placebo_n=40)
    assert r["start"] == pd.Timestamp("2021-12-31")
    b = r["book"]
    assert b.index[0] == pd.Timestamp("2022-01-31") and b.index[-1] == MONTHS[-1] and len(b) == 57
    assert (b["n"] == 4 * int(N_FIRMS / N_SECTORS * U.QUANTILE)).all()   # 87・88 社の業種から 17 社ずつ
    assert (b.loc[:"2022-02-28", "window_share"] == 1).all() and (b.loc["2022-03-31":, "window_share"] == 0).all()
    assert r["judge"]["verdict"] == "使えそう" and r["verdict"] == "使えそう" and r["judge"]["placebo"] == 1.0
    assert all(r["caps"][k] for k in ("cost_1pct", "window_excluded", "years"))
    assert r["refs"]["changed"]["d"].mean() < 0 < r["refs"]["not_sector"]["d"].mean()
    assert r["refs"]["current_only"]["d"].mean() > 0 and len(r["refs"]["yearly"]) == 5
    text = "\n".join(U.report_lines(r, {"made": "2026-10-21 12:00", "filings": len(p), "pairs": len(p), "sim_median": 0.8}))
    for key in ("docs/us_text.md 9 章", "## 1. 判定", "## 2. 参考", "## 3. 読み方・限界", "2.485", "この長さでは見えなかった",
                "決まり 1", "決まり 2", "決まり 3", "**使えそう**"):
        assert key in text


def test_null_world_is_not_found():
    mem, sig, close, _ = _world(alpha=0.0, seed=3)
    r = U.evaluate(mem, sig, close, np.random.default_rng(2), placebo_n=40)
    assert r["start"] == pd.Timestamp("2021-12-31") and r["verdict"] != "使えそう"
    assert abs(r["judge"]["t"]) < U.S.expected_max_z(U.TRIALS)
    none = U.evaluate(mem[mem["ticker"] < "T100"], sig, close, np.random.default_rng(2), placebo_n=5)
    assert none["start"] is None and "判定しない" in "\n".join(U.report_lines(none, {}))


def test_signal_is_not_used_at_a_month_end_priced_before_the_filing():
    p = pd.DataFrame({"cik": [1], "accession": ["a"], "accepted": [pd.Timestamp("2026-02-27")], "sim": [0.9], "in_window": [False]})
    feb, mar = pd.Timestamp("2026-02-28"), pd.Timestamp("2026-03-31")          # 2026-02-28 は土曜。値段は 2-27（金）の終値
    assert feb not in U.signals(p, [feb], trade_days={feb: pd.Timestamp("2026-02-27")})
    assert feb not in U.signals(p, [feb])                                      # 渡さなければ月末より前の平日で比べる
    assert mar in U.signals(p, [mar], trade_days={mar: pd.Timestamp("2026-03-31")})


def test_no_placebo_means_condition_four_fails_and_low_coverage_stops():
    mem, sig, close, (m0, *_) = _small()
    res = U.evaluate(mem, {}, close, np.random.default_rng(1))
    assert res["verdict"] == "判定しない" and "点検 3" in res["reason"]
    assert any("点検 3" in x for x in U.report_lines(res, {}))
