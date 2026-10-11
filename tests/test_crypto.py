import json

import numpy as np
import pandas as pd

from cfdbot.crypto import (LOG_COLUMNS, PaperStore, decide, frame, klines_url, paper_report, paper_run, parse_klines,
                           parse_ticker, symbol_returns, ticker_url)


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
