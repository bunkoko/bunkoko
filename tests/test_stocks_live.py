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
                memo=None, void=None, import_csv=None, report=False, no_fetch=True, sells_only=False)
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
    assert (b["shares"] == (10_000 // prev[b.index]).astype(int)).all()                   # 1 銘柄 = 5 万円 ÷ 5 = 1 万円、前の終値で切り捨て
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
    mod.cmd_orders(_ns(tmp_path, setup=True, deadline="14:00"))
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
    mod.cmd_orders(_ns(tmp_path, fill=[real["ticker"].iloc[2], "買い", "40", "2000"]))    # 8 万円 > 5 万円 × 1.5 → すぐ STOP
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
