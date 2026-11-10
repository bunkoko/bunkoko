import json

import numpy as np
import pandas as pd

from cfdbot.crypto import (LOG_COLUMNS, PaperStore, ann_sharpe, decide, frame, klines_url, paper_report, paper_run,
                           parse_klines, parse_ticker, price_vol, same_risk, symbol_returns, ticker_url)


def _bars(start: str, days: int, seed: int, drift: float = 0.004) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t0 = pd.Timestamp(start)
    t0 = t0.tz_localize("UTC") if t0.tzinfo is None else t0
    t = pd.date_range(t0, periods=days, freq="D")                              # GMO: 21:00 UTC（朝 6 時 JST）区切り
    close = 5_000_000 * np.exp(np.cumsum(rng.normal(drift, 0.02, days)))
    return pd.DataFrame({"time": t, "open": close, "high": close * 1.02, "low": close * 0.98, "close": close,
                         "volume": 1.0})


def _klines_json(bars: pd.DataFrame, year: int) -> bytes:
    rows = [{"openTime": str(int(t.timestamp() * 1000)), "open": f"{o:.0f}", "high": f"{h:.0f}", "low": f"{lo:.0f}",
             "close": f"{c:.0f}", "volume": "1.0"}
            for t, o, h, lo, c in zip(bars["time"], bars["open"], bars["high"], bars["low"], bars["close"])
            if t.year == year]
    return json.dumps({"status": 0, "data": rows, "responsetime": "2026-10-11T00:00:00.000Z"}).encode()


def _ticker_json(mid: float) -> bytes:
    return json.dumps({"status": 0, "data": [{"ask": f"{mid * 1.0005:.0f}", "bid": f"{mid * 0.9995:.0f}",
                                              "high": "1", "last": f"{mid:.0f}", "low": "1", "symbol": "BTC_JPY",
                                              "timestamp": "2026-10-11T00:00:00.000Z", "volume": "1"}]}).encode()


class FakeGmo:
    """公開 API の代わり。年ごとの日足と、その時点の売り買いの値段を返す。"""

    def __init__(self, bars: dict[str, pd.DataFrame]):
        self.bars, self.calls = bars, []

    def __call__(self, url: str) -> tuple[int, bytes]:
        self.calls.append(url)
        sym = url.split("symbol=")[1].split("&")[0]
        if "/klines" in url:
            year = int(url.split("date=")[1])
            if year < self.bars[sym]["time"].iloc[0].year:            # 取り扱いの前の年は 404（ETH_JPY の 2018 年など）
                return 404, b'{"status":1,"messages":[{"message_code":"ERR-5003"}]}'
            return 200, _klines_json(self.bars[sym], year)
        return 200, _ticker_json(float(self.bars[sym]["close"].iloc[-1]))


def test_parsers_read_gmo_json():
    bars = _bars("2026-01-01 21:00", 3, 0)
    k = parse_klines(_klines_json(bars, 2026))
    assert list(k.columns) == ["time", "open", "high", "low", "close", "volume"] and len(k) == 3
    assert k["time"].iloc[0] == bars["time"].iloc[0] and k["time"].dt.tz is not None
    t = parse_ticker(_ticker_json(10_000_000))
    assert t["bid"] < t["ask"] and abs(t["spread_pct"] - 0.001) < 1e-6
    assert parse_klines(json.dumps({"status": 0, "data": []}).encode()).empty
    assert "date=2026" in klines_url("BTC_JPY", 2026) and "symbol=ETH_JPY" in ticker_url("ETH_JPY")


def test_decide_reads_the_direction_from_the_last_closed_bar():
    bars = _bars("2025-01-01 21:00", 400, 1, drift=0.006)                 # 強い上げ → 買いで持っている
    f = frame(bars, bars["time"].iloc[-1] + pd.Timedelta(days=1))
    assert len(f) == 400 and (f["spread"] > 0).all()
    d = decide({"BTC_JPY": f}, 1_000_000)["BTC_JPY"]
    assert d.position == 1 and d.qty >= 0.01 and d.bar_time == bars["time"].iloc[-1]
    assert decide({"BTC_JPY": frame(bars.iloc[:50], bars["time"].iloc[-1])}, 1_000_000) == {}   # 足が足りない
    # 確定していない足（終わる前）は使わない
    assert len(frame(bars, bars["time"].iloc[-1] + pd.Timedelta(hours=12))) == 399


def test_paper_run_appends_once_per_bar_and_resumes(tmp_path):
    bars = {"BTC_JPY": _bars("2025-01-01 21:00", 400, 2), "ETH_JPY": _bars("2025-01-01 21:00", 400, 3)}
    gmo = FakeGmo(bars)
    st = PaperStore(tmp_path)
    now = bars["BTC_JPY"]["time"].iloc[-1] + pd.Timedelta(days=1, hours=1)
    out = paper_run(st, get=gmo, now=now, log=lambda m: None)
    log = st.load_log()
    assert len(out) == 2 and list(log.columns) == LOG_COLUMNS and len(log) == 2
    assert set(log["position"]) <= {-1, 0, 1} and (log["spread_pct"] > 0).all()
    assert len(st.load_klines("BTC_JPY")) == 400
    paper_run(st, get=gmo, now=now, log=lambda m: None)                 # 同じ日にもう一度 → 増えない
    assert len(st.load_log()) == 2
    for sym in bars:                                                   # 翌日: 足が 1 本増える
        nxt = _bars(str(bars[sym]["time"].iloc[-1] + pd.Timedelta(days=1)), 1, 9)
        bars[sym] = pd.concat([bars[sym], nxt], ignore_index=True)
    assert any("date=2018" in u for u in gmo.calls)                   # 初回は 2018 年からさかのぼる
    gmo.calls = []
    paper_run(st, get=gmo, now=now + pd.Timedelta(days=1), log=lambda m: None)
    log = st.load_log()
    assert len(log) == 4 and len(st.load_klines("ETH_JPY")) == 401
    assert all("date=2025" in u or "date=2026" in u or "/ticker" in u for u in gmo.calls)   # 2 回目からは最近の年だけ


def test_symbol_returns_carry_the_position_over_missing_days_and_charge_costs():
    bars = _bars("2025-01-01 21:00", 10, 4, drift=0.0)
    bars["close"] = [100.0, 110.0, 121.0, 121.0, 121.0, 121.0, 121.0, 121.0, 121.0, 121.0]
    t = bars["time"]
    log = pd.DataFrame([
        {"logged_at": t[0], "symbol": "BTC_JPY", "bar_time": t[0], "close": 100.0, "position": 1, "qty": 0.01,
         "bid": 99.9, "ask": 100.1, "spread_pct": 0.002},
        {"logged_at": t[3], "symbol": "BTC_JPY", "bar_time": t[3], "close": 121.0, "position": 0, "qty": 0.0,
         "bid": 120.9, "ask": 121.1, "spread_pct": 0.002},
    ])
    r, gaps = symbol_returns(log, bars)
    assert gaps == 7                                                   # 記録したのは 2 日だけ
    assert abs(r.iloc[0] - (-0.001)) < 1e-9                            # 入ると決めた日: 片道のスプレッド
    assert abs(r.iloc[1] - (0.10 - 0.0004)) < 1e-9                     # 買いで +10%、保有コスト
    assert abs(r.iloc[2] - (0.10 - 0.0004)) < 1e-9                     # 記録の無い日も買いのまま
    assert abs(r.iloc[3] - (-0.0004 - 0.001)) < 1e-9                   # やめた日: その日まで持っていた分と片道
    assert (r.iloc[4:] == 0).all()


def test_paper_report_judges_only_after_enough_days(tmp_path):
    st = PaperStore(tmp_path)
    bars = _bars("2025-01-01 21:00", 320, 5, drift=0.003)
    st.save_klines("BTC_JPY", bars)
    rows = [{"logged_at": t, "symbol": "BTC_JPY", "bar_time": t, "close": c, "position": 1, "qty": 0.01,
             "bid": c * 0.9995, "ask": c * 1.0005, "spread_pct": 0.001}
            for t, c in zip(bars["time"], bars["close"])]
    st.append_log(rows[:100])
    lines, info = paper_report(st, now=bars["time"].iloc[100])
    assert "途中経過" in info["verdict"] and "あと 150 日" in info["verdict"]
    st.append_log(rows[100:])
    lines, info = paper_report(st, now=bars["time"].iloc[-1])
    assert info["both"]["days"] == 320 and info["both"]["sharpe"] > 0
    assert "2028-01 に少額で始めてよい" in info["verdict"]
    assert any("ビットコイン" in ln for ln in lines)


# --------------------------------------------------------------------------- まとめ（同じリスク）の採点（2026-11-10 の直し）
WARM, DAYS = 100, 300          # 記録の前からある日足（実物は 2018 年から）と、記録した日数（判定の 250 日以上）


def _vol_bars(seed: int, drift: float, sd: float = 0.02, days: int = WARM + DAYS) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = pd.date_range(pd.Timestamp("2025-01-01 21:00", tz="UTC"), periods=days, freq="D")
    close = 5_000_000 * np.exp(np.cumsum(rng.normal(drift, sd, days)))
    return pd.DataFrame({"time": t, "open": close, "high": close * 1.02, "low": close * 0.98, "close": close,
                         "volume": 1.0})


def _log(sym: str, bars: pd.DataFrame, position, warm: int = WARM, spread: float = 0.001) -> pd.DataFrame:
    """warm 本目の足から毎日記録した形。position は 1 つの向きか、日ごとの向きの並び。"""
    b = bars.iloc[warm:]
    pos = np.broadcast_to(np.asarray(position, float), (len(b),))
    return pd.DataFrame([{"logged_at": t, "symbol": sym, "bar_time": t, "close": c, "position": p, "qty": 0.01,
                          "bid": c * (1 - spread / 2), "ask": c * (1 + spread / 2), "spread_pct": spread}
                         for t, c, p in zip(b["time"], b["close"], pos)], columns=LOG_COLUMNS)


def _flat(first: int = 0, mid: int = 0, mid_from: int = 100) -> np.ndarray:
    """買いで持ち続ける中に、初めの first 日と、途中の mid 日の「建玉なし」をはさむ。"""
    pos = np.ones(DAYS)
    pos[:first] = 0.0
    pos[mid_from:mid_from + mid] = 0.0
    return pos


def _store(root, position, drift: float, seeds=(1, 2), sd=(0.02, 0.02)) -> tuple[PaperStore, dict[str, pd.DataFrame]]:
    st, bars = PaperStore(root), {}
    for sym, seed, s in zip(("BTC_JPY", "ETH_JPY"), seeds, sd):
        bars[sym] = _vol_bars(seed, drift, s)
        st.save_klines(sym, bars[sym])
        st.append_log(_log(sym, bars[sym], position).to_dict("records"))
    return st, bars


def _summary(bars: dict[str, pd.DataFrame], position, warm: int = WARM) -> pd.Series:
    rets = {sym: symbol_returns(_log(sym, b, position, warm), b)[0] for sym, b in bars.items()}
    return same_risk(rets, {sym: price_vol(b) for sym, b in bars.items()})


def test_price_vol_uses_only_earlier_bars_and_the_same_window_as_before():
    b = _vol_bars(7, 0.0, days=120)
    chg, vol = b["close"].pct_change(), price_vol(b)
    assert vol.index.equals(pd.DatetimeIndex(b["time"])) and vol.iloc[:21].isna().all()    # 前の足が 21 本に満たない日は出さない
    assert abs(vol.iloc[21] - chg.iloc[1:21].std()) < 1e-12                               # 最低 20 日分。その日の値動きは入れない
    assert abs(vol.iloc[100] - chg.iloc[40:100].std()) < 1e-12                            # 直近 60 日
    still = b.copy()
    still["close"] = 100.0                                                               # 値が動かない → 0 ではなく「出せない」
    assert price_vol(still).isna().all()
    # 動いた後で 60 日より長く止まった所も「出せない」。pandas の std は、ここで 0 の代わりにごく小さい正の数（1e-10 ほど）を
    # 返すことがある（下の種はどれもそうなる）。それを「変動」として割らない
    for seed, sd in ((3, 0.02), (13, 0.02), (22, 0.08), (3, 0.3)):
        b = _vol_bars(seed, 0.0, sd, days=300)
        b.loc[150:249, "close"] = b["close"].iloc[149]                                   # 150〜249 本目は値が止まる
        chg, vol = b["close"].pct_change(), price_vol(b)
        assert vol.iloc[210:251].isna().all(), (seed, sd)                                # 前の 60 日が全部止まっている日
        for day, moved in ((209, 149), (251, 250)):                                      # 1 日でも動いた日が窓に入れば出す
            assert np.isclose(vol.iloc[day], abs(chg.iloc[moved]) / np.sqrt(60), rtol=1e-6), (seed, sd, day)
        ok = vol.dropna()
        assert (ok > 1e-6).all() and len(ok) == 300 - 21 - 41, (seed, sd)                # ごく小さい正の数が残っていない


def test_same_risk_summary_survives_flat_stretches(tmp_path):
    """上げ相場を買いで持つ。建玉なしが何日続いた後に建てても、まとめは計算でき、プラスで、判定は「始めてよい」。
    直す前は、建玉なしが記録の初めから 20 日以上（途中なら 61 日以上）続くと、次に建てた日が「0 で割る」になり、
    中身に関係なく「始めない」に落ちた。"""
    sharpes = {}
    cases = {"初め 0 日": _flat(0), "初め 5 日": _flat(5), "初め 19 日": _flat(19), "初め 20 日": _flat(20),
             "初め 25 日": _flat(25), "途中 70 日": _flat(mid=70), "初め 25 日と途中 70 日": _flat(25, 70)}
    for i, (name, pos) in enumerate(cases.items()):
        st, bars = _store(tmp_path / str(i), pos, drift=0.004)
        lines, info = paper_report(st, now=bars["BTC_JPY"]["time"].iloc[-1])
        s = info["both"]["sharpe"]
        assert np.isfinite(s) and s > 0, name
        assert "2028-01 に少額で始めてよい" in info["verdict"], name
        assert info["both"]["days"] == DAYS and info["both"]["scored"] == DAYS, name       # 記録の最初の日から全部数える
        assert abs(s - ann_sharpe(_summary(bars, pos))) < 1e-9, name                       # 表の数字は same_risk そのもの
        assert any("まとめ（同じリスク）: シャープ" in ln for ln in lines), name
        sharpes[name] = s
    # 建玉なしが 19 日でも歪まない（直す前は 20 日目の小さなばらつきで割って、半分以下になった）
    assert abs(sharpes["初め 19 日"] - sharpes["初め 20 日"]) < 0.5 and sharpes["初め 19 日"] > 0.7 * sharpes["初め 0 日"]


def test_same_risk_summary_is_negative_when_long_in_a_falling_market(tmp_path):
    for i, pos in enumerate((_flat(0), _flat(25, 70))):
        st, bars = _store(tmp_path / str(i), pos, drift=-0.004)
        info = paper_report(st, now=bars["BTC_JPY"]["time"].iloc[-1])[1]
        assert np.isfinite(info["both"]["sharpe"]) and info["both"]["sharpe"] < 0
        assert "始めない。もう 1 年記録する" in info["verdict"]


def test_same_risk_series_is_finite_and_starts_on_the_first_recorded_day():
    bars = {"BTC_JPY": _vol_bars(1, 0.004), "ETH_JPY": _vol_bars(2, 0.004)}
    for pos in (_flat(0), _flat(25), _flat(mid=70)):
        both = _summary(bars, pos)
        assert np.isfinite(both.to_numpy()).all() and len(both) == DAYS
        assert both.index[0] == bars["BTC_JPY"]["time"].iloc[WARM]          # 直す前は最初の 20 日が落ちていた
        flat_days = both.index[np.flatnonzero(pos == 0)[1:]]                # 建玉なしの日（閉じた当日は除く）は 0 として数える
        assert (both[flat_days] == 0).all()
    # 記録より前の日足が無いときは、値段の変動が出せるまでの 21 日を数えない（無限大にも 0 にもしない）
    short = {sym: b.iloc[WARM:].reset_index(drop=True) for sym, b in bars.items()}
    both = _summary(short, 1.0, warm=0)
    assert np.isfinite(both.to_numpy()).all() and len(both) == DAYS - 21 and both.index[0] == short["BTC_JPY"]["time"].iloc[21]
    # 値が 60 日より長く動かなかった所（変動 0）は、持っていても数えない（保有コスト ÷ 0 を作らない）
    stale = {sym: b.copy() for sym, b in bars.items()}
    for b in stale.values():
        b.loc[WARM - 70:WARM + 9, "close"] = b["close"].iloc[WARM - 71]
    both = _summary(stale, 1.0)
    assert np.isfinite(both.to_numpy()).all() and len(both) == DAYS - 11
    assert both.index[0] == bars["BTC_JPY"]["time"].iloc[WARM + 11]
    # 持っている間に値が止まったままになっても、まとめが桁違いに膨らまない（この種は、最大・最小の確かめが無いと
    # 「保有コスト ÷ 1e-10」で 1 日の値が 50 万前後になる）。止まって 60 日を過ぎた日からは数えない
    halted = {"BTC_JPY": _vol_bars(13, 0.004), "ETH_JPY": _vol_bars(16, 0.004)}
    for b in halted.values():
        b.loc[150:, "close"] = b["close"].iloc[149]
    both = _summary(halted, 1.0)
    assert len(both) == 210 - WARM and both.index[-1] == halted["BTC_JPY"]["time"].iloc[209] and both.abs().max() < 10
    # 片方の通貨だけ変動が出せない日は、出せる通貨だけで数える
    one = {"BTC_JPY": bars["BTC_JPY"], "ETH_JPY": short["ETH_JPY"]}
    rets = {"BTC_JPY": symbol_returns(_log("BTC_JPY", bars["BTC_JPY"], 1.0), bars["BTC_JPY"])[0],
            "ETH_JPY": symbol_returns(_log("ETH_JPY", short["ETH_JPY"], 1.0, 0), short["ETH_JPY"])[0]}
    vols = {sym: price_vol(b) for sym, b in one.items()}
    both, btc = same_risk(rets, vols), same_risk({"BTC_JPY": rets["BTC_JPY"]}, vols)
    assert len(both) == DAYS and (both.iloc[:21] == btc.iloc[:21]).all() and (both.iloc[21:] != btc.iloc[21:]).any()


def test_same_risk_summary_does_not_look_ahead():
    bars = {"BTC_JPY": _vol_bars(1, 0.004), "ETH_JPY": _vol_bars(2, 0.004)}
    pos = _flat(25, 70)
    before = _summary(bars, pos)
    day = WARM + 200                                                    # 買いで持っている日
    moved = {sym: b.copy() for sym, b in bars.items()}
    for b in moved.values():
        b.loc[day, "close"] *= 1.5
    after = _summary(moved, pos)
    t = bars["BTC_JPY"]["time"].iloc[day]
    pd.testing.assert_series_equal(before[before.index < t], after[after.index < t])     # その日より前は 1 つも変わらない
    assert after[t] > before[t] + 15                                     # その日は +50% が入る（割る変動は前の日までの分）
    assert not np.allclose(before[before.index > t].iloc[:60], after[after.index > t].iloc[:60])


def test_same_risk_weighs_a_calm_coin_and_a_wild_coin_alike():
    bars = {"BTC_JPY": _vol_bars(1, 0.001, sd=0.02), "ETH_JPY": _vol_bars(2, 0.001, sd=0.06)}     # 値動きの大きさが 3 倍
    rets = {sym: symbol_returns(_log(sym, b, 1.0), b)[0] for sym, b in bars.items()}
    vols = {sym: price_vol(b) for sym, b in bars.items()}
    btc, eth = same_risk({"BTC_JPY": rets["BTC_JPY"]}, vols), same_risk({"ETH_JPY": rets["ETH_JPY"]}, vols)
    assert 2.5 < rets["ETH_JPY"].std() / rets["BTC_JPY"].std() < 3.5    # そろえる前は 3 倍
    assert 0.85 < eth.std() / btc.std() < 1.18                          # そろえた後は同じくらい
    both = same_risk(rets, vols)
    assert np.allclose(both.to_numpy(), ((btc + eth) / 2).to_numpy())   # まとめは、そろえた 2 つの平均
    assert 0.8 < np.corrcoef(both, btc)[0, 1] / np.corrcoef(both, eth)[0, 1] < 1.25      # 荒い方だけで決まらない（そろえないと 0.3）
