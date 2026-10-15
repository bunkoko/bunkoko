"""日本株の予行演習（docs/stocks_live.md、./cfd agents orders）の試験。作ったデータだけを使う。注文は出さない。"""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdbot.agents import decider as D
from cfdbot.agents import live as L

ROOT = Path(__file__).resolve().parents[1]
TODAY = pd.Timestamp("2026-11-04")          # 水曜


def _script():
    spec = importlib.util.spec_from_file_location("agents_live", ROOT / "scripts" / "agents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ns(tmp_path, **kw):
    base = dict(root=str(tmp_path / "agents"), data=str(tmp_path / "stocks"), out=str(tmp_path / "out"), now=f"{TODAY:%Y-%m-%d} 13:10",
                setup=False, equity=None, deadline=None, real=False, fill=None, fee=0.0, status="約定", at="引け", on=None, error=None,
                memo=None, void=None, import_csv=None, report=False, no_fetch=True, sells_only=False, notify=False, notify_test=False,
                exclude=None)
    base.update(kw)
    return Namespace(**base)


def _index():
    idx = pd.bdate_range("2026-08-03", TODAY)
    return idx[idx != pd.Timestamp("2026-10-12")]          # スポーツの日（値段の無い平日）


def _prices(tmp_path, levels=None):
    idx = _index()
    cols = [f"{1000 + i}.T" for i in range(30)]
    lv = np.full(30, 2000.0) if levels is None else np.asarray(levels, dtype=float)
    rng = np.random.default_rng(0)
    close = pd.DataFrame(lv * np.exp(np.cumsum(rng.normal(0, 0.005, (len(idx), 30)), axis=0)), index=idx, columns=cols)
    d = tmp_path / "stocks"
    d.mkdir(parents=True, exist_ok=True)
    close.to_csv(d / "jp_close.csv")
    close.shift(1).bfill().to_csv(d / "jp_open.csv")
    return idx, cols, close


def _ledger_extra(tmp_path, days_tickers: dict, decided="13:05:30"):
    rows = []
    for day, tickers in days_tickers.items():
        rows.append((f"{day:%Y-%m-%d} {decided}", f"{day:%Y-%m-%d}", "1029.T", "random", 0.0, np.nan, np.nan, "会社R", "でたらめ"))
        for t in tickers:
            rows.append((f"{day:%Y-%m-%d} {decided}", f"{day:%Y-%m-%d}", t, "e1", 0.0, np.nan, np.nan, f"会社{t[:4]}",
                         "通期の純利益の会社予想 +12%（E1: 5% 以上の引き上げ）"))
    root = tmp_path / "agents"
    root.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=["decided_at", "date", "ticker", "book", "selector", "ir", "combined", "name", "reason"]).to_csv(
        root / "ledger_extra.csv", index=False)


def _e1(tickers):
    return pd.DataFrame({"ticker": tickers, "name": [f"会社{t}" for t in tickers], "reason": "E1"})


def test_business_days_follow_the_price_data_and_skip_holidays():
    idx = _index()
    cal = L.calendar(idx, TODAY)
    assert L.bday_add(cal, pd.Timestamp("2026-10-09"), 1) == pd.Timestamp("2026-10-13")    # 10-12 は値段が無い（祝日）→ 飛ばす
    assert L.bday_add(cal, pd.Timestamp("2026-10-30"), 5) == pd.Timestamp("2026-11-06")    # 金曜に買えば翌週の金曜（土日を数えない）
    assert L.bday_add(cal, TODAY, 5) == pd.Timestamp("2026-11-11")                         # 値段の無い先は平日で数える（目安）
    assert L.bdays_after(cal, pd.Timestamp("2026-10-09"), pd.Timestamp("2026-10-14")) == 2
    assert L.bdays_after(cal, TODAY, TODAY) == 0


def test_plan_picks_five_by_date_seed_and_skips_expensive_cheap_and_held():
    idx = _index()
    cal = L.calendar(idx, TODAY)
    cfg = {"equity_jpy": 300_000, "mode": "real", "deadline": "14:00"}
    now = TODAY + pd.Timedelta(hours=13, minutes=10)
    prev = pd.Series({"A": 2000.0, "B": 3000.0, "C": 4000.0, "D": 1500.0, "E": 2500.0, "F": 900.0, "G": 5000.0})
    close = pd.DataFrame([prev, prev * 1.01], index=[idx[-2], TODAY])
    rows = L.make_plan(TODAY, now, cfg, _e1(list("ABCDEFG")), "x", close, cal, {}, 300_000, None)
    df = pd.DataFrame(rows)
    chosen = D.random_pick(list("ABCDEFG"), 5, TODAY)
    assert sorted(df.loc[df["side"] == "買い", "ticker"]) == chosen                       # 7 社 → 日付を種にしたでたらめで 5 社
    assert sorted(df.loc[df["note"].str.contains("でたらめで外した", na=False), "ticker"]) == sorted(set("ABCDEFG") - set(chosen))
    b = df[df["side"] == "買い"].set_index("ticker")
    per = L.daily_budget(300_000) // 5
    assert L.daily_budget(300_000) == 42_857 and per == 8_571                            # D = X ÷ 7、1 銘柄 = D ÷ 5
    assert (b["shares"] == (per // prev[b.index]).astype(int)).all()                      # 前の終値で切り捨て
    assert (b["due"] == pd.Timestamp("2026-11-11")).all() and (b["margin_min"] == 50.0).all()

    prev2 = pd.Series({"X": 60_000.0, "Y": 120.0, "Z": 2000.0, "H": 2000.0})          # 高い・安い（100 株以上）・持っている株・ふつう
    close2 = pd.DataFrame([prev2, prev2], index=[idx[-2], TODAY])
    held = {"Z": L.Lot("Z", 5, idx[-6], idx[-6], 10_000.0, due=TODAY), "H": L.Lot("H", 4, idx[-6], idx[-6], 8_000.0, due=TODAY)}
    df2 = pd.DataFrame(L.make_plan(TODAY, now, cfg, _e1(["X", "Y", "Z"]), "x", close2, cal, held, 300_000, None)).set_index("ticker")
    assert "高くて買えない" in df2.loc["X", "note"] and "99 株を超える" in df2.loc["Y", "note"]
    assert df2.loc["Z", "side"] == "延長" and df2.loc["Z", "due"] == pd.Timestamp("2026-11-11") and df2.loc["Z", "shares"] == 5   # 売らず・買い足さず
    assert df2.loc["H", "side"] == "売り" and df2.loc["H", "shares"] == 4                  # 5 営業日たった株は売る
    assert not (df2["side"] == "買い").any()


def test_plan_without_buys_when_stopped_late_or_not_judged():
    idx = _index()
    cal = L.calendar(idx, TODAY)
    cfg = {"equity_jpy": 300_000, "mode": "real", "deadline": "14:00"}
    prev = pd.Series({"A": 2000.0})
    close = pd.DataFrame([prev, prev], index=[idx[-2], TODAY])
    lot = {"S": L.Lot("S", 3, idx[-6], idx[-6], 6_000.0, due=TODAY)}
    early, late = TODAY + pd.Timedelta(hours=13, minutes=10), TODAY + pd.Timedelta(hours=13, minutes=40)
    stopped = pd.DataFrame(L.make_plan(TODAY, early, cfg, _e1(["A"]), "x", close, cal, lot, 300_000, "STOP（手で置いた）"))
    assert stopped.set_index("ticker").loc["A", "side"] == "買わない" and "STOP" in stopped.set_index("ticker").loc["A", "note"]
    assert stopped.set_index("ticker").loc["S", "side"] == "売り"                          # STOP でも、持っている株は予定の日に売る
    tardy = pd.DataFrame(L.make_plan(TODAY, late, cfg, _e1(["A"]), "x", close, cal, {}, 300_000, None))
    assert tardy["note"].iloc[0].startswith("間に合わない（受付の終わりまで 20 分")
    none = pd.DataFrame(L.make_plan(TODAY, early, cfg, None, None, close, cal, {}, 300_000, None))
    assert none["side"].tolist() == ["なし"] and "判断が無い日" in none["reason"].iloc[0]


def test_stop_rules_drawdown_mistakes_unsold_and_missing_prices():
    idx = _index()
    cal = L.calendar(idx, TODAY)
    cfg = {"equity_jpy": 300_000}
    close = pd.DataFrame({"A": 1000.0, "B": 1000.0}, index=idx)
    assert L.stop_checks(cfg, (0, 241_000), [], {}, close, cal, TODAY, None) == []
    assert "80% を下回った" in L.stop_checks(cfg, (0, 239_000), [], {}, close, cal, TODAY, None)[0]
    errs = [(idx[-3], "x"), (idx[-25], "古い誤り")]
    assert L.stop_checks(cfg, (0, 300_000), errs, {}, close, cal, TODAY, None) == []      # 20 営業日より前の誤りは数えない
    errs.append((idx[-1], "y"))
    assert "誤りが 2 回" in L.stop_checks(cfg, (0, 300_000), errs, {}, close, cal, TODAY, None)[0]
    lot = {"A": L.Lot("A", 3, idx[-12], idx[-12], 3_000.0, due=idx[-7])}                   # 予定から 6 営業日売れていない
    assert "6 営業日売れていない" in L.stop_checks(cfg, (0, 300_000), [], lot, close, cal, TODAY, None)[0]
    close.loc[idx[-2]:, "B"] = np.nan
    lot = {"B": L.Lot("B", 3, idx[-3], idx[-3], 3_000.0, due=TODAY + pd.Timedelta(days=7))}
    assert "売買停止・上場廃止の恐れ" in L.stop_checks(cfg, (0, 300_000), [], lot, close, cal, TODAY, None)[0]
    assert "E1 の帳が閉じた" in L.stop_checks(cfg, (0, 300_000), [], {}, close, cal, TODAY, "閉じる（250 日）")[0]


def test_orders_command_dry_run_then_real_with_mistakes_and_stop(tmp_path, capsys):
    idx, cols, _ = _prices(tmp_path)
    mod = _script()
    with pytest.raises(SystemExit):
        mod.cmd_orders(_ns(tmp_path))                                                     # 設定が無ければ何もしない
    mod.cmd_orders(_ns(tmp_path, setup=True, equity=300_000))
    files = L.LiveFiles(tmp_path / "agents" / "live")
    assert files.config()["mode"] == "dry" and files.config()["deadline"] is None
    with pytest.raises(SystemExit):
        mod.cmd_orders(_ns(tmp_path, setup=True, equity=900_000))                         # 50 万円を超える金額は断る
    mod.cmd_orders(_ns(tmp_path, import_csv="x.csv"))
    assert "未対応" in capsys.readouterr().out

    days = list(idx[-11:])
    mod.cmd_orders(_ns(tmp_path, now=f"{days[0]:%Y-%m-%d} 13:10"))                       # 判断がまだ → 書かない
    assert not files.plan_path.exists()
    _ledger_extra(tmp_path, {d: cols[:7] if d == days[-1] else cols[10:12] for d in days})
    mod.cmd_orders(_ns(tmp_path, setup=True, deadline="14:00"))                          # 公式の締め 14:00 は通る（注意も出ない）
    assert "13:35 より前" not in capsys.readouterr().out and files.config()["deadline"] == "14:00"
    with pytest.raises(SystemExit):
        mod.cmd_orders(_ns(tmp_path, setup=True, deadline="14:30"))                       # 14:00 より後は翌日の寄りになるので断る
    mod.cmd_orders(_ns(tmp_path, setup=True, deadline="13:50"))                           # 決まりは 13:50（docs/stocks_live.md 4.1）
    with pytest.raises(SystemExit):
        mod.cmd_orders(_ns(tmp_path, setup=True, real=True))                              # 空運転が足りない
    for d in days[:-1]:
        mod.cmd_orders(_ns(tmp_path, now=f"{d:%Y-%m-%d} 13:10"))
    n = len(files.plan())
    mod.cmd_orders(_ns(tmp_path, now=f"{days[-2]:%Y-%m-%d} 13:20"))                      # 同じ日は二度書かない
    assert len(files.plan()) == n
    with pytest.raises(SystemExit):
        mod.cmd_orders(_ns(tmp_path, fill=["1010", "買い", "5", "2000"]))                 # 空運転の間は約定を書かない
    st = L.dry_run_status(files.plan(), files.config())
    assert st["days"] == 10 and st["e1_rows"] == 20 and st["margin_share"] == 1.0 and st["ok"]
    mod.cmd_orders(_ns(tmp_path, setup=True, real=True, now=f"{days[-1]:%Y-%m-%d} 09:00"))
    assert files.config()["mode"] == "real" and files.config()["started"] == f"{days[-1]:%Y-%m-%d}"
    with pytest.raises(SystemExit):
        mod.cmd_orders(_ns(tmp_path, setup=True, equity=200_000))                         # 始めた後は金額を変えない

    capsys.readouterr()
    mod.cmd_orders(_ns(tmp_path))                                                         # 本物の 1 日目: 7 社 → 5 社を買う
    out = capsys.readouterr().out
    plan = files.plan()
    real = plan[(plan["mode"] == "real") & (plan["side"] == "買い")]
    assert len(real) == 5 and "預り区分は「特定」" in out and "株**" in out
    t0, s0 = real["ticker"].iloc[0], int(real["shares"].iloc[0])
    mod.cmd_orders(_ns(tmp_path, fill=[t0, "買い", str(s0), "2000"]))
    assert files.fills()["error"].isna().all()                                           # 計画どおりなら誤りではない
    t1 = real["ticker"].iloc[1]
    mod.cmd_orders(_ns(tmp_path, fill=[t1, "買い", "7", "2000"]))                         # 株数が計画と違う → 誤り
    assert "株数が計画" in str(files.fills()["error"].iloc[-1])
    mod.cmd_orders(_ns(tmp_path, fill=["1029", "買い", "3", "2000"]))                     # 計画に無い買い → 誤り（2 回目）
    assert not files.stop_path.exists()
    mod.cmd_orders(_ns(tmp_path, now=f"{TODAY:%Y-%m-%d} 15:40"))                         # 次に開いたとき、2 回で STOP を置く
    assert files.stop_path.exists() and "誤りが 2 回" in files.stop_reason()

    files.stop_path.unlink()
    stamp = str(files.fills()["id"].iloc[-1])
    mod.cmd_orders(_ns(tmp_path, void=stamp))                                             # 取り消しは行を足すだけ（元の行は残る）
    assert len(files.fills()) == 2 and len(files.fills(include_voided=True)) == 4
    mod.cmd_orders(_ns(tmp_path, fill=[real["ticker"].iloc[2], "買い", "40", "2000"]))    # 8 万円 > 42,857 円 × 1.5 → すぐ STOP
    assert files.stop_path.exists() and "大きすぎる約定" in files.stop_reason()
    capsys.readouterr()
    mod.cmd_orders(_ns(tmp_path, report=True))
    text = next((tmp_path / "out" / "live").glob("*.md")).read_text(encoding="utf-8")
    assert "## 3. 空運転の進み具合" in text and "出した買いの注文 3 回" in text and "まだ決めない" in text


def test_sell_day_and_mistakes_for_late_or_early_sells():
    idx = _index()
    cal = L.calendar(idx, TODAY)
    buy_day = pd.Timestamp("2026-10-09")                                                  # 金曜。10-12 は祝日
    fills = pd.DataFrame([{"id": "1", "recorded_at": "a", "mode": "real", "date": buy_day, "ticker": "A", "side": "買い", "shares": 4, "price": 1000.0,
                           "fee_yen": 0.0, "status": "約定", "at": "引け", "error": np.nan, "voids": np.nan, "memo": np.nan}])
    ev = L.events("real", pd.DataFrame(columns=L.PLAN_COLUMNS), fills, pd.DataFrame())
    lots = L.open_lots(ev, pd.DataFrame(columns=L.PLAN_COLUMNS), fills, "real", cal)
    assert lots["A"].due == pd.Timestamp("2026-10-19")                                    # 祝日を飛ばして 5 営業日
    cfg = {"equity_jpy": 300_000}
    early = L.check_fill(cfg, pd.DataFrame(columns=L.PLAN_COLUMNS), lots, pd.Timestamp("2026-10-16"), "A", "売り", 4, 1000.0, "約定", "引け", None)
    assert "より前の売り" in early[0]
    late = L.check_fill(cfg, pd.DataFrame(columns=L.PLAN_COLUMNS), lots, pd.Timestamp("2026-10-20"), "A", "売り", 4, 1000.0, "約定", "引け", None)
    assert "売りを出さなかった" in late[0]
    ok = L.check_fill(cfg, pd.DataFrame(columns=L.PLAN_COLUMNS), lots, pd.Timestamp("2026-10-19"), "A", "売り", 4, 1000.0, "約定", "寄り", None)
    assert ok == ("", False)                                                              # 予定の日の朝の売りは誤りではない
    assert "株を超える" not in ok[0]
    big = L.check_fill(cfg, pd.DataFrame(columns=L.PLAN_COLUMNS), lots, pd.Timestamp("2026-10-19"), "A", "売り", 120, 1000.0, "約定", "引け", None)
    assert "S 株は 99 株まで" in big[0]
    errs = L.mistakes("real", pd.DataFrame(columns=L.PLAN_COLUMNS), fills, lots, cal, pd.Timestamp("2026-10-21"))
    assert len(errs) == 1 and "売りの出し忘れ" in errs[0][1]                               # 予定の翌々営業日になっても記録が無い


def _rt(n, c=0.0, t=0.0, status="約定", m=np.nan, done=True, excluded=""):
    return pd.DataFrame({"date": pd.bdate_range("2026-11-02", periods=n), "ticker": "A", "status": status, "done": done,
                         "c": c if status == "約定" and not excluded else np.nan, "t": t if status == "約定" and not excluded else np.nan,
                         "m": m, "excluded": excluded})


def test_c_star_fixes_once_at_sixty_orders_with_floor_nonfills_and_give_up():
    assert not L.c_star(_rt(59))["fixed"]
    cs = L.c_star(_rt(70))
    assert cs["fixed"] and cs["k"] == 60 and cs["c_star"] == L.C_FLOOR and cs["ok6"]     # 手数料 0・公式の値段どおり → 下限 0.02%
    rt = pd.concat([_rt(57, c=0.0003), _rt(3, status="不成立", m=0.04)], ignore_index=True)
    cs = L.c_star(rt)
    assert cs["p_nf"] == 0.05 and "不成立の株" in cs["rule"] and cs["c_star"] > 0.001        # 5% 以上 → 不成立の株の紙の上の差を足す
    rt = pd.concat([_rt(58, c=0.0003), _rt(2, status="不成立", m=0.04)], ignore_index=True)
    cs = L.c_star(rt)
    assert "約定した往復" in cs["rule"] and abs(cs["c_star"] - 0.0003) < 1e-9
    cs = L.c_star(_rt(60, c=0.0, t=0.002))
    assert cs["c6"] > L.LIMIT_6 and not cs["ok6"]                                         # 6 章の判定は、コスト＋時刻のずれ
    few = pd.concat([_rt(25), _rt(95, excluded="後場始値")], ignore_index=True)             # 往復が 30 回に届かない
    cs = L.c_star(few)
    assert not cs["fixed"] and cs["gave_up"]
    wait = _rt(60)
    wait.loc[10, "done"] = False
    assert not L.c_star(wait)["fixed"]                                                    # 最初の 60 回が片づくまで決めない


def test_round_trip_cost_and_timing_against_unadjusted_reference_prices(tmp_path):
    idx = _index()
    cal = L.calendar(idx, TODAY)
    d0, d5 = idx[-12], idx[-7]
    base = {"mode": "real", "fee_yen": 0.0, "error": np.nan, "voids": np.nan, "memo": np.nan}
    fills = pd.DataFrame([
        {**base, "recorded_at": "1", "date": d0, "ticker": "A", "side": "買い", "shares": 4, "price": 1000.0, "status": "約定", "at": "引け"},
        {**base, "recorded_at": "2", "date": d5, "ticker": "A", "side": "売り", "shares": 4, "price": 1010.0, "status": "約定", "at": "寄り",
         "fee_yen": 11.0},
        {**base, "recorded_at": "3", "date": d0, "ticker": "B", "side": "買い", "shares": 2, "price": 0.0, "status": "不成立", "at": "引け"},
    ])
    refs = pd.DataFrame([{"date": d0, "ticker": "A", "open": 990.0, "close": 1000.0, "fetched_at": "x", "source": "yahoo"},
                         {"date": d5, "ticker": "A", "open": 1010.0, "close": 1020.0, "fetched_at": "x", "source": "yahoo"}])
    close = pd.DataFrame({"A": 100.0, "B": 100.0, "C": 100.0}, index=idx)
    close.loc[d5:, "B"] = 110.0                                                           # 買えなかった B は、その後 5 日で +10%
    rt = L.round_trips(fills, pd.DataFrame(columns=L.PLAN_COLUMNS), refs, close, cal).set_index("ticker")
    assert np.isclose(rt.loc["A", "c"], 0.0 + 0.0 + 11.0 / (4 * 1000.0))                   # 公式の値段どおり → 手数料の分だけ
    assert np.isclose(rt.loc["A", "t"], (1020.0 - 1010.0) / 1020.0)                       # 朝に売った分の時刻のずれ（c には入れない）
    assert np.isclose(rt.loc["B", "m"], 0.10 - 0.10 / 3) and rt.loc["B", "done"]          # 株 − 全銘柄の平均

    files = L.LiveFiles(tmp_path / "live")
    calls = []

    def fake(tickers, start, end):
        calls.append((tuple(tickers), start, end))
        return pd.DataFrame([{"date": d0, "ticker": "A", "open": 990.0, "close": 1000.0}, {"date": d5, "ticker": "A", "open": 1010.0, "close": 1020.0}])

    msgs = L.update_refs(files, fills, pd.DataFrame(columns=L.PLAN_COLUMNS), TODAY, TODAY + pd.Timedelta(hours=16), cal, fetch=fake)
    got = files.refs()
    assert len(got) == 2 and calls[0][0] == ("A",) and "6 営業日後に取った" in got["source"].iloc[1]
    assert any("3 営業日以内の決まり" in m for m in msgs)
    assert L.update_refs(files, fills, pd.DataFrame(columns=L.PLAN_COLUMNS), TODAY, TODAY, cal, fetch=fake) == []   # 取ってあれば取らない


def test_sells_only_duplicate_buy_early_cost_and_early_deadline(tmp_path, capsys):
    idx, cols, _ = _prices(tmp_path)
    mod = _script()
    mod.cmd_orders(_ns(tmp_path, setup=True, equity=300_000, deadline="13:30"))
    assert "13:35 より前" in capsys.readouterr().out                                     # 13:05 の判断の後に 30 分の余裕が取れない
    files = L.LiveFiles(tmp_path / "agents" / "live")
    mod.cmd_orders(_ns(tmp_path, sells_only=True))                                        # 判断が無い日は、売りだけの一覧を書く
    plan = files.plan()
    assert plan["side"].tolist() == ["なし"] and "判断が無い日" in plan["reason"].iloc[0]
    mod.cmd_orders(_ns(tmp_path, now=f"{TODAY:%Y-%m-%d} 13:20"))                         # 同じ日は後から買いを足さない
    assert len(files.plan()) == 1

    cfg = {"equity_jpy": 300_000}
    plan = pd.DataFrame([{"mode": "real", "date": TODAY, "ticker": "A", "side": "買い", "shares": 4}])
    before = pd.DataFrame([{"mode": "real", "date": TODAY, "ticker": "A", "side": "買い", "status": "約定"}])
    err, _ = L.check_fill(cfg, plan, {}, TODAY, "A", "買い", 4, 2000.0, "約定", "引け", None, before)
    assert "二度" in err

    rt = pd.DataFrame({"c": [0.002] * 10 + [0.0] * 5, "status": "約定", "done": True})
    assert "最初の 10 往復" in L.early_cost(rt)                                           # 差が大きければ早めに止める
    assert L.early_cost(rt.iloc[:9]) is None and L.early_cost(pd.DataFrame({"c": [0.0005] * 10})) is None
    cal = L.calendar(idx, TODAY)
    hits = L.stop_checks(cfg, (0, 300_000), [], {}, pd.DataFrame(index=idx), cal, TODAY, None, rt)
    assert hits and "4.5 の 3" in hits[0]


def test_price_limits_reserve_buying_power_and_skip_outside_holdings():
    assert L.price_limit(150) == 50 and L.price_limit(2_500) == 500 and L.price_limit(9_999) == 1_500 and L.price_limit(200_000) == 60_000
    assert L.reserve_price(2_500) == 3_000
    idx = _index()
    cal = L.calendar(idx, TODAY)
    now = TODAY + pd.Timedelta(hours=13, minutes=10)
    prev = pd.Series({"A": 2000.0, "B": 2000.0})
    close = pd.DataFrame([prev, prev], index=[idx[-2], TODAY])
    cfg = {"equity_jpy": 300_000, "mode": "real", "deadline": "13:50", "exclude": ["1001"]}
    df = pd.DataFrame(L.make_plan(TODAY, now, cfg, _e1(["A", "B"]), "x", close, cal, {}, 30_000, None)).set_index("ticker")
    assert df.loc["A", "shares"] == 10 and df.loc["A", "side"] == "買い"                    # 10 株 × 2,500 円 = 25,000 円を拘束
    assert df.loc["B", "side"] == "買わない" and "買付余力" in df.loc["B", "note"]          # 残り 5,000 円では足りない
    df2 = pd.DataFrame(L.make_plan(TODAY, now, cfg, _e1(["1001.T", "A"]), "x",
                                   pd.DataFrame([{"1001.T": 2000.0, "A": 2000.0}] * 2, index=[idx[-2], TODAY]), cal, {}, 300_000, None)).set_index("ticker")
    assert df2.loc["1001.T", "side"] == "買わない" and "すでに持っている株" in df2.loc["1001.T", "note"]
    assert 5 * L.daily_budget(300_000) + L.daily_budget(300_000) * 1.5 <= 300_000           # 5 日分を持ったまま、値幅 5 割の拘束でも足りる


def test_order_stop_is_neither_mistake_nor_nonfill():
    idx = _index()
    cal = L.calendar(idx, TODAY)
    day = idx[-12]
    base = {"mode": "real", "fee_yen": 0.0, "error": np.nan, "voids": np.nan, "memo": np.nan, "at": "引け"}
    plan = pd.DataFrame([{"mode": "real", "date": day, "ticker": t, "side": "買い", "shares": 4} for t in ("A", "B")])
    fills = pd.DataFrame([{**base, "id": "1", "recorded_at": "1", "date": day, "ticker": "A", "side": "買い", "shares": 4, "price": 1000.0, "status": "約定"},
                          {**base, "id": "2", "recorded_at": "2", "date": day, "ticker": "B", "side": "買い", "shares": 4, "price": 0.0, "status": "受注停止"}])
    ev = L.events("real", plan, fills, pd.DataFrame())
    lots = L.open_lots(ev, plan, fills, "real", cal)
    due = lots["A"].due
    fills2 = pd.concat([fills, pd.DataFrame([{**base, "id": "3", "recorded_at": "3", "date": due, "ticker": "A", "side": "売り", "shares": 4,
                                              "price": 0.0, "status": "受注停止"}])], ignore_index=True)
    lots2 = L.open_lots(ev, plan, fills2, "real", cal)
    assert L.mistakes("real", plan, fills2, lots2, cal, TODAY) == []                    # 受注停止は、一部だけ出さなかった・出し忘れにならない
    nxt = L.bday_add(cal, due, 1)
    assert L.check_fill({"equity_jpy": 300_000}, plan, lots2, nxt, "A", "売り", 4, 1000.0, "約定", "寄り", None) == ("", False)
    rt = L.round_trips(fills2, plan, pd.DataFrame(columns=L.REF_COLUMNS), pd.DataFrame({"A": 1.0}, index=idx), cal)
    assert rt["status"].tolist() == ["約定"]                                              # 出した買い・不成立に数えない


def test_e1_preview_is_the_same_as_the_ledger_e1_book():
    idx = _index()
    cols = [f"{2000 + i}.T" for i in range(8)]
    rng = np.random.default_rng(4)
    close = pd.DataFrame(1000 * np.exp(np.cumsum(rng.normal(0, 0.01, (len(idx), 8)), axis=0)), index=idx, columns=cols)
    open_ = close.shift(1)
    close.loc[TODAY, cols[1]] = np.nan                                                    # 今日の終値が無い株
    open_.loc[TODAY, cols[2]] = np.nan                                                    # 今日の寄りが無い株
    rev = pd.DataFrame({"date": TODAY, "ticker": cols[:4], "fc_rev": [0.2, 0.3, 0.1, 0.01], "kind": 5})
    day, e1 = D.e1_preview(open_, close, rev, {cols[0]: "会社0"})
    assert day == TODAY and e1["ticker"].tolist() == [cols[0]] and e1["name"].iloc[0] == "会社0"
    from cfdbot import stocks_short as H

    tradable = close.notna() & H.parts(open_, close)["night"].notna()                     # 銘柄選定が点数を付ける所
    sel_row = pd.Series(np.where(tradable.loc[TODAY], 0.0, np.nan), index=cols)
    rows = D.extra_rows(TODAY, sel_row, 2, rev, {})
    assert rows.loc[rows["book"] == "e1", "ticker"].tolist() == e1["ticker"].tolist()


def test_notify_sends_nothing_without_settings_and_retries_once(tmp_path):
    from cfdbot import notify as N

    assert N.notify("t", "x", path=tmp_path / "none.json")[0][1] is False
    bad = tmp_path / "bad.json"
    bad.write_text('{"channels": [{"type": "ntfy", "topic": "x"}]}', encoding="utf-8")
    assert N.load_settings(bad)[0] is None                                                # ntfy の公開サーバーは使わない
    calls, waits = [], []

    class R:
        def __init__(self, code):
            self.returncode, self.stderr, self.stdout = code, "error -1743", ""

    def flaky(args, **kw):
        calls.append(args)
        return R(1 if len(calls) == 1 else 0)

    s = {"channels": [{"type": "imessage", "to": "me@example.com"}, {"type": "mailapp", "to": "me@example.com"}]}
    rec = tmp_path / "notify.log"
    out = N.notify("題", "本文", settings=s, runner=flaky, sleep=waits.append, record=rec)
    assert [o[1] for o in out] == [True, True] and len(calls) == 3 and waits == [N.RETRY_WAIT]   # 1 回目に失敗 → 1 回だけやり直す
    assert calls[0][0] == "osascript" and calls[0][-2:] == ["題\n本文", "me@example.com"] and calls[2][-3:] == ["題", "本文", "me@example.com"]
    assert len(rec.read_text(encoding="utf-8").splitlines()) == 2

    def hang(args, **kw):
        import subprocess

        raise subprocess.TimeoutExpired(args, kw.get("timeout"))

    out = N.notify("題", "本文", settings={"channels": [{"type": "imessage", "to": "x"}]}, runner=hang, sleep=lambda s: None)
    assert out[0][1] is False and "TimeoutExpired" in out[0][2]                         # 止まっても例外を出さない

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = []

    def opener(req, timeout):
        seen.append((req.full_url, req.data))
        return Resp()

    out = N.notify("題", "本文", settings={"channels": [{"type": "pushover", "token": "T", "user": "U"}]}, opener=opener)
    assert out[0][1] and seen[0][0].startswith("https://api.pushover.net/") and b"token=T" in seen[0][1]


def test_notice_text_marks_dry_run_and_lists_orders():
    rows = [{"ticker": "1001.T", "name": "会社A", "side": "買い", "shares": 4, "prev_close": 2000.0, "note": ""},
            {"ticker": "1002.T", "name": "会社B", "side": "売り", "shares": 3, "prev_close": np.nan, "note": ""},
            {"ticker": "1003.T", "name": "会社C", "side": "買わない", "shares": 0, "prev_close": np.nan, "note": "高くて買えない"}]
    title, text, has = L.notice_text(rows, {"mode": "dry", "deadline": "13:50"}, TODAY + pd.Timedelta(hours=13))
    assert "空運転: 出すはずの注文" in title and has and "買い 1001 会社A 4 株（約 8,000 円）" in text and "売り 1002 会社B 3 株" in text
    assert "13:30 までを目安" in text and "「特定」" in text and "買わない 1 社" in text
    title, text, has = L.notice_text([{"ticker": "", "name": "", "side": "なし", "shares": 0, "prev_close": np.nan, "note": ""}],
                                     {"mode": "real"}, TODAY)
    assert title.startswith("株の予行演習（今日の注文）") and not has and "注文なし" in text


def test_run_writes_the_list_before_the_slow_steps_and_ledger_survives_failures(tmp_path, monkeypatch, capsys):
    idx, cols, close = _prices(tmp_path)
    mod = _script()
    order = []
    for name in ("cmd_numbers", "cmd_collect", "cmd_ir", "cmd_select", "cmd_decide", "cmd_evaluate"):
        monkeypatch.setattr(mod, name, lambda a, n=name: order.append(n))

    def boom(a):
        order.append("live_early")
        raise RuntimeError("作り物の失敗")

    monkeypatch.setattr(mod, "live_early", boom)
    monkeypatch.setattr(mod, "live_after_decide", lambda a: order.append("live_after"))
    mod.cmd_run(_ns(tmp_path, prices=False))
    assert order == ["cmd_numbers", "live_early", "cmd_collect", "cmd_ir", "cmd_select", "cmd_decide", "live_after", "cmd_evaluate"]
    out = capsys.readouterr().out
    assert "先に作る一覧は作れなかった（RuntimeError" in out                               # 失敗しても台帳（判断）まで進む
    log = tmp_path / "run.log"
    log.write_text("古い行\n[2026-11-03 13:05:00] 自動実行: 始め\n[2026-11-04 13:05:01] 自動実行: 始め\n"
                   "[2026-11-04 13:07:31] 自動実行: 値段\n[2026-11-04 13:08:01] 自動実行: E1 の一覧と注文の一覧\n"
                   "[2026-11-04 13:20:01] 自動実行: 判断（台帳）\n", encoding="utf-8")
    steps = mod.run_timeline(log)
    assert [s[0] for s in steps] == ["始め", "値段", "E1 の一覧と注文の一覧", "判断（台帳）"]       # 最後の実行だけ
    assert "E1 の一覧と注文の一覧 3.0 分（13:08）" in mod.timeline_text(steps) and "判断（台帳） 15.0 分" in mod.timeline_text(steps)


def test_live_early_writes_the_plan_notifies_and_checks_against_the_ledger(tmp_path, monkeypatch):
    from cfdbot import notify as N

    idx, cols, close = _prices(tmp_path)
    pd.DataFrame({"ticker": cols, "name": [f"会社{c[:4]}" for c in cols], "sector": "x"}).to_csv(tmp_path / "stocks" / "universe_jp.csv", index=False)
    root = tmp_path / "agents"
    root.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": [TODAY] * 3, "ticker": cols[:3], "fc_rev": [0.2, 0.06, 0.01], "kind": 5}).to_csv(root / "revisions_recent.csv", index=False)
    mod = _script()
    assert mod.live_early(_ns(tmp_path)) is False                                         # 設定が無ければ何もしない
    mod.cmd_orders(_ns(tmp_path, setup=True, equity=300_000, deadline="13:50"))
    conf = tmp_path / "notify.json"
    conf.write_text('{"channels": [{"type": "imessage", "to": "me@example.com"}]}', encoding="utf-8")
    monkeypatch.setattr(N, "NOTIFY_FILE", conf)
    sent = []
    monkeypatch.setattr(N, "notify", lambda title, text, **kw: sent.append((title, text)) or [("imessage", True, "送った")])
    assert mod.live_early(_ns(tmp_path, now=f"{TODAY:%Y-%m-%d} 13:08"))
    files = L.LiveFiles(root / "live")
    plan = files.plan()
    assert set(plan.loc[plan["side"].isin(L.E1_SIDES), "ticker"]) == set(cols[:2]) and (plan["source"] == "先に作った E1 の一覧").all()
    assert len(sent) == 1 and "空運転: 出すはずの注文" in sent[0][0]
    _ledger_extra(tmp_path, {TODAY: cols[:2]})
    mod.live_after_decide(_ns(tmp_path, now=f"{TODAY:%Y-%m-%d} 13:20"))                  # 台帳の E1 と同じ → 知らせない
    assert len(sent) == 1 and len(files.plan()) == len(plan)
    _ledger_extra(tmp_path, {TODAY: cols[:3]})
    mod.live_after_decide(_ns(tmp_path, now=f"{TODAY:%Y-%m-%d} 13:20"))                  # 違えば、知らせて記録に残す
    assert len(sent) == 2 and "違いました" in sent[1][1] and cols[2] in sent[1][1]


def test_notify_test_works_before_setup_and_sends_nothing_without_settings(tmp_path, monkeypatch, capsys):
    from cfdbot import notify as N

    _prices(tmp_path)
    monkeypatch.setattr(N, "NOTIFY_FILE", tmp_path / "no_such.json")
    _script().cmd_orders(_ns(tmp_path, notify_test=True))
    out = capsys.readouterr().out
    assert "設定が無いので送らない" in out and not (tmp_path / "agents" / "live" / "notify.log").exists()   # 何も送らず、何も書かない
