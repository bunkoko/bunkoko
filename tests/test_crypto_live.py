import json

import numpy as np
import pandas as pd
import pytest

from cfdbot.crypto import PaperStore
from cfdbot.crypto_live import (GmoClient, Held, LiveStore, held_from_summary, live_run, plan_actions, round_size,
                                sign, target_size)
from cfdbot.crypto import Decision


def test_sign_matches_a_known_answer():
    s = sign("secret", "1700000000000", "GET", "/v1/account/margin", "")
    assert s == sign("secret", "1700000000000", "GET", "/v1/account/margin")
    assert len(s) == 64 and s != sign("other", "1700000000000", "GET", "/v1/account/margin")
    assert sign("k", "1", "POST", "/v1/order", '{"a":1}') != sign("k", "1", "POST", "/v1/order", "")


def test_held_and_plan():
    assert held_from_summary([]) == Held(0, 0.0)
    assert held_from_summary([{"side": "BUY", "sumPositionQuantity": "0.03"}]) == Held(1, 0.03)
    assert held_from_summary([{"side": "BUY", "sumPositionQuantity": "0.03"},
                              {"side": "SELL", "sumPositionQuantity": "0.05"}]) == Held(-1, 0.02)
    assert plan_actions("BTC_JPY", Held(0, 0), 1, 0.02) == [{"action": "open", "side": "BUY", "size": 0.02}]
    assert plan_actions("BTC_JPY", Held(1, 0.02), 1, 0.03) == []                      # 同じ向き: 量の差は追わない
    assert plan_actions("BTC_JPY", Held(1, 0.02), 0, 0.0) == [{"action": "close", "side": "SELL", "size": 0.02}]
    assert plan_actions("BTC_JPY", Held(1, 0.02), -1, 0.01) == [{"action": "close", "side": "SELL", "size": 0.02},
                                                                 {"action": "open", "side": "SELL", "size": 0.01}]
    assert round_size("BTC_JPY", 0.0299) == pytest.approx(0.02) and round_size("ETH_JPY", 0.37) == pytest.approx(0.3)
    d = Decision("BTC_JPY", pd.Timestamp("2026-10-01", tz="UTC"), 10_000_000.0, 1, 0.5)
    assert target_size("BTC_JPY", d, {"max_notional_jpy": 1_000_000}) == pytest.approx(0.1)   # 名目の上限で抑える
    assert target_size("BTC_JPY", Decision("BTC_JPY", d.bar_time, d.close, 0, 0.5), {"max_notional_jpy": 1e9}) == 0.0


class FakeRequest:
    """非公開 API の代わり。建玉と注文を覚える。"""

    def __init__(self, status="OPEN", positions=None):
        self.status, self.positions, self.orders, self.calls = status, positions or {}, [], []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        if "/public/v1/status" in url:
            return {"status": 0, "data": {"status": self.status}}
        assert headers["API-KEY"] == "k" and len(headers["API-SIGN"]) == 64
        if "/v1/positionSummary" in url:
            sym = url.split("symbol=")[1]
            return {"status": 0, "data": {"list": self.positions.get(sym, [])}}
        if url.endswith("/v1/order") or url.endswith("/v1/closeBulkOrder"):
            self.orders.append((url.rsplit("/", 1)[1], json.loads(body)))
            return {"status": 0, "data": str(100 + len(self.orders))}
        return {"status": 1, "messages": [{"message_string": "unknown"}]}


def _store_with_trend(tmp_path, n=400, drift=0.004):
    rng = np.random.default_rng(1)
    st = PaperStore(tmp_path / "paper")
    for sym in ("BTC_JPY", "ETH_JPY"):
        t = pd.date_range(pd.Timestamp("2025-01-01 21:00", tz="UTC"), periods=n, freq="D")
        close = (400_000 if sym == "BTC_JPY" else 30_000) * np.exp(np.cumsum(rng.normal(drift, 0.02, n)))
        st.save_klines(sym, pd.DataFrame({"time": t, "open": close, "high": close * 1.02, "low": close * 0.98,
                                          "close": close, "volume": 1.0}))
    return st, t[-1] + pd.Timedelta(days=1, hours=1)


def test_live_run_dry_logs_orders_without_sending(tmp_path):
    paper, now = _store_with_trend(tmp_path)
    live = LiveStore(tmp_path / "live")
    req = FakeRequest()
    client = GmoClient("k", "s", request=req, clock=lambda: 1.7e9)
    rows = live_run(paper, live, client, real=False, now=now, log=lambda m: None)
    assert any(r["action"] == "open" and r["mode"] == "dry" for r in rows)
    assert req.orders == []                                                  # 空運転: 注文は出ていない
    assert live.config_path.exists() and live.load_log().shape[0] == len(rows)
    assert all(m == "GET" for m, _ in req.calls)                            # 読むだけ


def test_live_run_real_opens_closes_and_respects_stop(tmp_path):
    paper, now = _store_with_trend(tmp_path)
    live = LiveStore(tmp_path / "live")
    live.config()
    live.config_path.write_text(json.dumps({"equity_jpy": 1_000_000, "max_notional_jpy": 2_000_000}), encoding="utf-8")
    req = FakeRequest(positions={"BTC_JPY": [{"side": "SELL", "sumPositionQuantity": "0.02"}]})   # 売りを持っている
    client = GmoClient("k", "s", request=req, clock=lambda: 1.7e9)
    rows = live_run(paper, live, client, real=True, now=now, log=lambda m: None)
    btc = [r for r in rows if r["symbol"] == "BTC_JPY"]
    assert [r["action"] for r in btc][:2] == ["close", "open"] and btc[0]["side"] == "BUY" and btc[1]["side"] == "BUY"
    assert req.orders[0][0] == "closeBulkOrder" and req.orders[0][1]["size"] == "0.02"
    assert req.orders[1][0] == "order" and req.orders[1][1]["executionType"] == "MARKET"
    assert all(r["mode"] == "real" for r in rows)
    # 同じ向きをすでに持っていれば何もしない（記録の向きに合わせた建玉を用意する）
    from cfdbot.crypto import decide, frame

    dec = decide({s: frame(paper.load_klines(s), now) for s in ("BTC_JPY", "ETH_JPY")}, 1_000_000)
    held = {s: ([{"side": "BUY" if d.position > 0 else "SELL", "sumPositionQuantity": "0.5"}] if d.position else [])
            for s, d in dec.items()}
    req2 = FakeRequest(positions=held)
    rows2 = live_run(paper, live, GmoClient("k", "s", request=req2, clock=lambda: 1.7e9), real=True, now=now,
                     log=lambda m: None)
    assert all(r["action"] == "hold" for r in rows2) and req2.orders == []
    # STOP があれば何もしない。取引所が OPEN でなくても何もしない
    live.stop_file.write_text("")
    rows3 = live_run(paper, live, client, real=True, now=now, log=lambda m: None)
    assert rows3[0]["action"] == "skip" and "STOP" in rows3[0]["note"]
    live.stop_file.unlink()
    req4 = FakeRequest(status="MAINTENANCE")
    rows4 = live_run(paper, live, GmoClient("k", "s", request=req4, clock=lambda: 1.7e9), real=True, now=now,
                     log=lambda m: None)
    assert rows4[0]["action"] == "skip" and req4.orders == []


def _paper_log(store, sym, start, n, position, qty):
    t = pd.date_range(start, periods=n, freq="D")
    store.append_log([{"logged_at": b + pd.Timedelta(days=1), "symbol": sym, "bar_time": b, "close": 0.0,
                       "position": position, "qty": qty, "bid": 0.0, "ask": 0.0, "spread_pct": 0.001} for b in t])


def test_paper_equity_and_drawdown(tmp_path):
    from cfdbot.crypto import drawdown, paper_equity

    paper = PaperStore(tmp_path / "paper")
    assert paper_equity(paper).empty and drawdown(paper_equity(paper)) == 0.0
    t = pd.date_range(pd.Timestamp("2026-10-01 21:00", tz="UTC"), periods=4, freq="D")
    paper.save_klines("BTC_JPY", pd.DataFrame({"time": t, "open": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0,
                                               "close": [10_000_000.0, 11_000_000.0, 9_000_000.0, 9_000_000.0]}))
    _paper_log(paper, "BTC_JPY", t[0], 4, 1, 0.1)                           # 0.1 BTC の買いを持ち続ける
    eq = paper_equity(paper, 1_000_000)
    # 初日は建てた分のスプレッドの半分（0.1 × 1,000 万 × 0.05% = 500 円）。+10 万円、−20 万円（保有コストを少し引く）
    assert eq.iloc[0] == pytest.approx(1_000_000 - 500)
    assert eq.iloc[1] - eq.iloc[0] == pytest.approx(100_000 - 0.1 * 10_000_000 * 0.0004)
    assert eq.iloc[2] - eq.iloc[1] == pytest.approx(-200_000 - 0.1 * 11_000_000 * 0.0004)
    assert drawdown(eq) == pytest.approx(1 - eq.iloc[-1] / eq.iloc[1])
    assert drawdown(pd.Series([100.0, 120.0, 90.0])) == pytest.approx(0.25)
    assert drawdown(pd.Series([100.0, 120.0])) == 0.0


def test_live_run_places_stop_after_paper_drawdown(tmp_path):
    paper, now = _store_with_trend(tmp_path)
    live = LiveStore(tmp_path / "live")
    k = paper.load_klines("BTC_JPY")
    _paper_log(paper, "BTC_JPY", k["time"].iloc[-30], 30, -1, 0.0)          # 数量 0: 資産は動かない
    rows = live_run(paper, live, None, now=now, log=lambda m: None)
    assert not live.stop_file.exists() and all(r["action"] != "stop" for r in rows)

    paper2, now2 = _store_with_trend(tmp_path / "b")                        # 上げ続ける相場で売りを持ち続けた記録
    k2 = paper2.load_klines("BTC_JPY")
    _paper_log(paper2, "BTC_JPY", k2["time"].iloc[-200], 200, -1, 0.5)
    live2 = LiveStore(tmp_path / "b" / "live")
    req = FakeRequest()
    rows2 = live_run(paper2, live2, GmoClient("k", "s", request=req, clock=lambda: 1.7e9), real=True, now=now2,
                     log=lambda m: None)
    assert [r["action"] for r in rows2] == ["stop"] and live2.stop_file.exists() and req.orders == []
    assert req.calls == []                                                  # API にも触らない
    rows3 = live_run(paper2, live2, None, now=now2, log=lambda m: None)    # 次の日からは STOP で何もしない
    assert rows3[0]["action"] == "skip" and "STOP" in rows3[0]["note"]
    # 上限を緩めれば止まらない（config.json の max_drawdown_stop）
    live2.stop_file.unlink()
    live2.config_path.write_text(json.dumps({"max_drawdown_stop": 1.0}), encoding="utf-8")
    rows4 = live_run(paper2, live2, None, now=now2, log=lambda m: None)
    assert all(r["action"] != "stop" for r in rows4) and not live2.stop_file.exists()
