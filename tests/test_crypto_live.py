import json

import numpy as np
import pandas as pd
import pytest

from cfdbot.crypto import PaperStore
from cfdbot.crypto_live import (GmoClient, Held, LiveStore, held_from_summary, live_run, plan_actions, round_size,
                                sign, target_size)
from cfdbot.crypto import Decision, drawdown, paper_equity


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
    live2.config_path.write_text(json.dumps({"max_drawdown_stop": 0.99}), encoding="utf-8")
    rows4 = live_run(paper2, live2, None, now=now2, log=lambda m: None)
    assert all(r["action"] not in ("stop", "error") for r in rows4) and not live2.stop_file.exists()


def _klines(store, sym, t, close):
    store.save_klines(sym, pd.DataFrame({"time": t, "open": 1.0, "high": 1.0, "low": 1.0, "close": close, "volume": 1.0}))


def _rows(store, sym, rows, extra=False):
    """rows: (足の日時, 向き, 数量, スプレッド) の並び。"""
    store.append_log([{"logged_at": b + pd.Timedelta(days=1), "symbol": sym, "bar_time": b, "close": 0.0, "position": p,
                       "qty": q, "bid": 0.0, "ask": 0.0, "spread_pct": s} for b, p, q, s in rows], extra=extra)


def test_paper_equity_matches_a_hand_calculation(tmp_path):
    """手で検算できる小さな例: 円の損益・保有コスト（0.04%/日）・向きを変えた日のスプレッドの半分・記録の無い日は前のまま。"""
    paper = PaperStore(tmp_path / "paper")
    t = pd.date_range(pd.Timestamp("2026-10-01 21:00", tz="UTC"), periods=6, freq="D")
    _klines(paper, "BTC_JPY", t, [10_000_000.0, 10_500_000.0, 10_200_000.0, 9_800_000.0, 9_900_000.0, 10_100_000.0])
    # 1 日目の終値で 0.1 BTC の買い → 3 日目に 0.05 BTC の売りへ → 4 日目は記録なし（Mac が止まっていた）→ 5 日目から「なし」
    _rows(paper, "BTC_JPY", [(t[0], 1, 0.10, 0.001), (t[1], 1, 0.10, 0.001), (t[2], -1, 0.05, 0.002),
                             (t[4], 0, 0.0, 0.001), (t[5], 0, 0.0, 0.001)])
    eq = paper_equity(paper, 1_000_000)
    steps = [
        -0.10 * 10_000_000 * 0.001 / 2,                                     # 建てた日: スプレッドの半分 −500 円
        0.10 * 500_000 - 0.10 * 10_000_000 * 0.0004,                        # 買い 0.1 × +50 万円 − 保有 400 円 = +49,600
        0.10 * -300_000 - 0.10 * 10_500_000 * 0.0004 - 0.15 * 10_200_000 * 0.002 / 2,   # −30,000 − 420 − 向きを変えた 0.15 分 1,530
        -0.05 * -400_000 - 0.05 * 10_200_000 * 0.0004,                      # 記録なし: 売り 0.05 のまま。下げで +20,000 − 204
        -0.05 * 100_000 - 0.05 * 9_800_000 * 0.0004 - 0.05 * 9_900_000 * 0.001 / 2,     # −5,000 − 196 − 閉じた分 247.5
        0.0,                                                                 # 「なし」: 値段が動いても増減しない
    ]
    assert list(eq.index) == list(t)
    assert eq.diff().fillna(eq.iloc[0] - 1_000_000).to_numpy() == pytest.approx(steps)
    assert eq.to_numpy() == pytest.approx([999_500, 1_049_100, 1_017_150, 1_036_946, 1_031_502.5, 1_031_502.5])
    assert drawdown(eq) == pytest.approx(1 - 1_031_502.5 / 1_049_100)       # 最高値は 2 日目
    # ETH を足す（記録の始まりは 2 日目）: 銘柄ごとの円の損益を足す。他の通貨の記録（log_extra.csv）は数えない
    _klines(paper, "ETH_JPY", t, [500_000.0, 480_000.0, 480_000.0, 520_000.0, 520_000.0, 520_000.0])
    _rows(paper, "ETH_JPY", [(t[1], -1, 2.0, 0.001), (t[2], -1, 2.0, 0.001), (t[3], 0, 0.0, 0.001)])
    _rows(paper, "XRP_JPY", [(t[0], 1, 1e9, 0.001)], extra=True)
    eth = np.cumsum([0.0, -2.0 * 480_000 * 0.001 / 2, -2.0 * 480_000 * 0.0004,                # −480、−384
                     -2.0 * 40_000 - 2.0 * 480_000 * 0.0004 - 2.0 * 520_000 * 0.001 / 2, 0.0, 0.0])   # 売り 2 ETH × +4 万円 = −80,904
    both = paper_equity(paper, 1_000_000)
    assert both.to_numpy() == pytest.approx(eq.to_numpy() + eth)
    assert both.iloc[-1] == pytest.approx(949_734.5) and drawdown(both) == pytest.approx(1 - 949_734.5 / 1_048_620)
    assert (paper_equity(paper, 600_000) - both).to_numpy() == pytest.approx(-400_000)       # 資金を変えても円の損益は同じ


def test_paper_equity_does_not_hide_what_it_cannot_compute(tmp_path):
    t = pd.date_range(pd.Timestamp("2026-10-01 21:00", tz="UTC"), periods=4, freq="D")
    falling = [10_000_000.0, 9_000_000.0, 8_000_000.0, 8_000_000.0]
    # 記録がまだ無い: 空（下落 0）。記録が 1 行だけ: 建てた分のスプレッドの半分を引いた 1 点（下落 0）
    paper = PaperStore(tmp_path / "a")
    _klines(paper, "BTC_JPY", t, falling)
    assert paper_equity(paper).empty and drawdown(paper_equity(paper)) == 0.0
    _rows(paper, "BTC_JPY", [(t[3], 1, 0.1, 0.001)])
    one = paper_equity(paper)
    assert len(one) == 1 and one.iloc[0] == pytest.approx(1_000_000 - 400) and drawdown(one) == 0.0
    # 記録はあるのに日足が無い・日足が記録より古い: 「下落なし」にせず ValueError
    lost = PaperStore(tmp_path / "b")
    _rows(lost, "BTC_JPY", [(t[0], 1, 0.1, 0.001)])
    with pytest.raises(ValueError, match="日足が無い"):
        paper_equity(lost)
    _klines(lost, "BTC_JPY", t[:1] - pd.Timedelta(days=5), [10_000_000.0])
    with pytest.raises(ValueError, match="日足が無い"):
        paper_equity(lost)
    # 0.1 BTC の買いを持ち続けて 1,000 万 → 800 万円（−20 万円）。これを基準にする
    full = PaperStore(tmp_path / "c")
    _klines(full, "BTC_JPY", t, falling)
    _rows(full, "BTC_JPY", [(b, 1, 0.1, 0.001) for b in t])
    base = paper_equity(full)
    assert base.to_numpy() == pytest.approx([999_500, 899_100, 798_740, 798_420]) and drawdown(base) > 0.20
    # 終値の欠けた足: 前の終値のままにして、値動きは次の足で数える（損を消さない。違いは保有コストの 40 円だけ）
    gap = PaperStore(tmp_path / "d")
    _klines(gap, "BTC_JPY", t, [10_000_000.0, np.nan, 8_000_000.0, 8_000_000.0])
    _rows(gap, "BTC_JPY", [(b, 1, 0.1, 0.001) for b in t])
    assert paper_equity(gap).to_numpy() == pytest.approx([999_500, 999_100, 798_700, 798_380])
    # 日足の順番が乱れていても・同じ日が 2 行あっても同じ答え
    mixed = PaperStore(tmp_path / "e")
    _klines(mixed, "BTC_JPY", t, falling)
    _rows(mixed, "BTC_JPY", [(b, 1, 0.1, 0.001) for b in t])
    p = mixed.klines_path("BTC_JPY")
    head, *body = p.read_text(encoding="utf-8").splitlines()
    p.write_text("\n".join([head, *body[::-1], body[1]]) + "\n", encoding="utf-8")
    assert paper_equity(mixed).to_numpy() == pytest.approx(base.to_numpy())
    # 向き・数量の読めない行は記録の無い日と同じ（前のまま）。全部読めなければ ValueError
    odd = PaperStore(tmp_path / "f")
    _klines(odd, "BTC_JPY", t, falling)
    _rows(odd, "BTC_JPY", [(t[0], 1, 0.1, 0.001), (t[1], 1, np.nan, np.nan), (t[2], np.nan, 0.1, 0.001), (t[3], 1, 0.1, 0.001)])
    assert paper_equity(odd).to_numpy() == pytest.approx(base.to_numpy())
    bad = PaperStore(tmp_path / "g")
    _klines(bad, "BTC_JPY", t, falling)
    _rows(bad, "BTC_JPY", [(b, 1, np.nan, 0.001) for b in t])
    with pytest.raises(ValueError, match="読めない"):
        paper_equity(bad)
    # 数字でない値が混じった推移の下落は NaN（0 にしない）
    assert np.isnan(drawdown(pd.Series([100.0, np.nan, 90.0]))) and np.isnan(drawdown(pd.Series([100.0, 120.0, np.nan])))


def test_live_run_sends_nothing_when_paper_equity_cannot_be_computed(tmp_path):
    paper, now = _store_with_trend(tmp_path)
    k = paper.load_klines("BTC_JPY")
    _paper_log(paper, "BTC_JPY", k["time"].iloc[-30], 30, 1, 0.1)
    paper.klines_path("BTC_JPY").rename(tmp_path / "moved.csv")            # 記録はあるのに日足が無い
    live = LiveStore(tmp_path / "live")
    req = FakeRequest()
    client = GmoClient("k", "s", request=req, clock=lambda: 1.7e9)
    rows = live_run(paper, live, client, real=True, now=now, log=lambda m: None)
    assert [r["action"] for r in rows] == ["error"] and "計算できない" in rows[0]["note"]
    assert req.calls == [] and req.orders == [] and not live.stop_file.exists()   # 注文も問い合わせも出さない。STOP は置かない
    assert live.load_log()["action"].tolist() == ["error"]                 # 理由は記録に残る
    (tmp_path / "moved.csv").rename(paper.klines_path("BTC_JPY"))          # 日足が戻れば、次の朝はふだんどおり
    rows2 = live_run(paper, live, client, real=True, now=now, log=lambda m: None)
    assert any(r["action"] == "open" for r in rows2) and len(req.orders) >= 1
    # 設定の上限が数字でないときも同じ（止める決まりを確かめられないので、注文を出さない）
    live.config_path.write_text(json.dumps({"max_drawdown_stop": "二割"}), encoding="utf-8")
    req3 = FakeRequest()
    rows3 = live_run(paper, live, GmoClient("k", "s", request=req3, clock=lambda: 1.7e9), real=True, now=now,
                     log=lambda m: None)
    assert [r["action"] for r in rows3] == ["error"] and req3.calls == []


def test_live_run_stop_threshold_dry_run_and_the_mornings_after(tmp_path, monkeypatch):
    import cfdbot.crypto_live as cl

    paper, now = _store_with_trend(tmp_path)
    live = LiveStore(tmp_path / "live")
    # ちょうど 20% の下落では止めない（決まりは「20% より減ったら」）
    monkeypatch.setattr(cl, "paper_equity", lambda store, start: pd.Series([start, 1.25 * start, start]))
    rows = live_run(paper, live, None, now=now, log=lambda m: None)
    assert not live.stop_file.exists() and all(r["action"] not in ("stop", "error") for r in rows)
    # 1 円でも超えたら止める。空運転でも STOP を置く（予行も本番と同じに止まる）
    monkeypatch.setattr(cl, "paper_equity", lambda store, start: pd.Series([start, 1.25 * start, start - 1]))
    rows2 = live_run(paper, live, None, now=now, log=lambda m: None)
    assert [(r["mode"], r["action"]) for r in rows2] == [("dry", "stop")] and live.stop_file.exists()
    assert f"{cl.jst(now):%Y-%m-%d %H:%M}（日本時間）" in live.stop_file.read_text(encoding="utf-8")   # 止まった朝を、日本の日付で書く
    # 次の朝: STOP があるので何もしない（下落の計算もしない）
    rows3 = live_run(paper, live, None, now=now + pd.Timedelta(days=1), log=lambda m: None)
    assert [r["action"] for r in rows3] == ["skip"] and "STOP" in rows3[0]["note"]
    # STOP を手で消しても、記録上の資産が戻っていなければ、次の朝にまた置かれる
    live.stop_file.unlink()
    req = FakeRequest()
    rows4 = live_run(paper, live, GmoClient("k", "s", request=req, clock=lambda: 1.7e9), real=True,
                     now=now + pd.Timedelta(days=2), log=lambda m: None)
    assert [(r["mode"], r["action"]) for r in rows4] == [("real", "stop")] and live.stop_file.exists() and req.calls == []
    # 推移に数字でない値が混じった朝: 止める・止めないを決められないので、注文を出さずに理由を残す（STOP は置かない）
    live.stop_file.unlink()
    monkeypatch.setattr(cl, "paper_equity", lambda store, start: pd.Series([start, float("nan")]))
    req5 = FakeRequest()
    rows5 = live_run(paper, live, GmoClient("k", "s", request=req5, clock=lambda: 1.7e9), real=True, now=now,
                     log=lambda m: None)
    assert [r["action"] for r in rows5] == ["error"] and req5.calls == [] and not live.stop_file.exists()


def test_live_run_stop_boundary_peak_and_settings_are_exact(tmp_path, monkeypatch):
    """2026-11-10（点検の指摘）: 境目・最高値の取り方・設定の値の読み方を、ぴったりの数で確かめる。"""
    import cfdbot.crypto_live as cl

    paper, now = _store_with_trend(tmp_path)
    live = LiveStore(tmp_path / "live")
    live.root.mkdir(parents=True)
    live.config_path.write_text(json.dumps({"max_drawdown_stop": 0.25}), encoding="utf-8")
    # ちょうど上限（1 → 2 → 1.5 は、計算機の中でもぴったり 0.25）では止めない。「以上」と書き間違えると、ここで止まる
    monkeypatch.setattr(cl, "paper_equity", lambda store, start: pd.Series([start, 2 * start, 1.5 * start]))
    rows = live_run(paper, live, None, now=now, log=lambda m: None)
    assert not live.stop_file.exists() and all(r["action"] not in ("stop", "error") for r in rows)
    monkeypatch.setattr(cl, "paper_equity", lambda store, start: pd.Series([start, 2 * start, 1.5 * start - 1]))
    assert [r["action"] for r in live_run(paper, live, None, now=now, log=lambda m: None)] == ["stop"]
    live.stop_file.unlink()

    # 最高値には始めの資金も入る: 初日から下げ続けて、始めの資金から 20.02% 減った（最初の点 999,500 円からは 19.98%）
    live.config_path.write_text(json.dumps({"max_drawdown_stop": 0.20}), encoding="utf-8")
    monkeypatch.setattr(cl, "paper_equity", lambda store, start: pd.Series([999_500.0, 799_800.0]))
    assert [r["action"] for r in live_run(paper, live, None, now=now, log=lambda m: None)] == ["stop"]
    live.stop_file.unlink()
    monkeypatch.setattr(cl, "paper_equity", lambda store, start: pd.Series([999_500.0, 800_100.0]))   # 19.99% は止めない
    assert all(r["action"] not in ("stop", "error") for r in live_run(paper, live, None, now=now, log=lambda m: None))

    # 設定の上限が 0 と 1 の間の数でない（20 = 2000% のつもり違い・true・0・負）: 止める決まりを確かめられないので、注文を出さない
    monkeypatch.setattr(cl, "paper_equity", lambda store, start: pd.Series([start, 0.4 * start]))     # 60% 減っている
    for bad in (20, True, 0, -0.2, 1.0):
        live.config_path.write_text(json.dumps({"max_drawdown_stop": bad}), encoding="utf-8")
        req = FakeRequest()
        rows = live_run(paper, live, GmoClient("k", "s", request=req, clock=lambda: 1.7e9), real=True, now=now, log=lambda m: None)
        assert [r["action"] for r in rows] == ["error"] and "0 と 1 の間" in rows[0]["note"] and req.calls == [], bad
    assert not live.stop_file.exists()


def test_live_run_does_not_trust_old_prices(tmp_path):
    """2026-11-10（点検の指摘）: 記録のある通貨の日足が古いままだと、その後の損益が 0 と数えられる。計算できない朝と同じに扱う。"""
    paper, now = _store_with_trend(tmp_path)
    k = paper.load_klines("BTC_JPY")
    _paper_log(paper, "BTC_JPY", k["time"].iloc[-30], 30, 1, 0.1)
    live = LiveStore(tmp_path / "live")
    req = FakeRequest()
    client = GmoClient("k", "s", request=req, clock=lambda: 1.7e9)
    late = now + pd.Timedelta(days=5)                                       # 日足が 5 日前で止まっている
    rows = live_run(paper, live, client, real=True, now=late, log=lambda m: None)
    assert [r["action"] for r in rows] == ["error"] and "日足が古い" in rows[0]["note"] and req.calls == []
    assert not live.stop_file.exists()
    rows2 = live_run(paper, live, client, real=True, now=now + pd.Timedelta(days=1), log=lambda m: None)   # 最後の足から 3 日までは、ふだんどおり
    assert all(r["action"] != "error" for r in rows2)


def test_duplicate_rows_for_one_bar_keep_the_first_written(tmp_path):
    """2026-11-10（点検の指摘）: 同じ足の行が 2 つあれば、先に書いた行で計算する（行が多くても入れ替わらない）。"""
    from cfdbot.crypto import symbol_returns

    t = pd.date_range(pd.Timestamp("2026-01-01 21:00", tz="UTC"), periods=60, freq="D")
    close = 5_000_000.0 * (1 + 0.01 * np.sin(np.arange(60)))

    def store(name, dup):
        st = PaperStore(tmp_path / name)
        _klines(st, "BTC_JPY", t, close)
        _rows(st, "BTC_JPY", [(b, 1, 0.1, 0.001) for b in t[:50]])
        if dup:
            _rows(st, "BTC_JPY", [(b, -1, 0.3, 0.001) for b in t[10:40]])     # 後から、中身の違う行が同じ足に入った
        return st

    a, b = store("a", False), store("b", True)
    assert len(b.load_log()) == 80
    pd.testing.assert_series_equal(paper_equity(a), paper_equity(b))
    ra, rb = symbol_returns(a.load_log(), a.load_klines("BTC_JPY"))[0], symbol_returns(b.load_log(), b.load_klines("BTC_JPY"))[0]
    pd.testing.assert_series_equal(ra, rb)                                  # 6 章の成績の計算も同じ


def test_stop_morning_leaves_a_trail_and_never_crashes(tmp_path, monkeypatch):
    """2026-11-10（点検の指摘）: 止まる朝は閉じる注文も出さないので、記録の向きを書き残す。STOP を書けない・設定が壊れた朝も、落ちずに理由を残す。"""
    import cfdbot.crypto_live as cl

    paper, now = _store_with_trend(tmp_path)
    k = paper.load_klines("BTC_JPY")
    _paper_log(paper, "BTC_JPY", k["time"].iloc[-200], 199, -1, 0.5)        # 上げ相場で売りを持ち続けた記録（最後の朝は「なし」に変えた）
    _paper_log(paper, "BTC_JPY", k["time"].iloc[-1], 1, 0, 0.0)
    _paper_log(paper, "ETH_JPY", k["time"].iloc[-5], 5, 1, 0.3)
    live = LiveStore(tmp_path / "live")
    req = FakeRequest()
    client = GmoClient("k", "s", request=req, clock=lambda: 1.7e9)

    def boom(self, *a, **kw):
        raise OSError(28, "No space left on device")

    live.config()                                                           # 設定のファイルは先に作っておく（書けなくするのは STOP だけ）
    with monkeypatch.context() as m:                                        # STOP を書けない朝: 落ちずに、注文を出さないで理由を残す
        m.setattr(type(live.stop_file), "write_text", boom)
        rows = live_run(paper, live, client, real=True, now=now, log=lambda m_: None)
    assert [r["action"] for r in rows] == ["error"] and "STOP を置けなかった" in rows[0]["note"] and req.calls == []
    assert live.load_log()["action"].tolist() == ["error"] and not live.stop_file.exists()

    rows2 = live_run(paper, live, client, real=True, now=now, log=lambda m_: None)   # 書ける朝: STOP と、今朝の記録の向き
    text = live.stop_file.read_text(encoding="utf-8")
    assert [r["action"] for r in rows2] == ["stop"] and req.calls == []
    assert "今朝の記録の向き: BTC なし・ETH 買い 0.3" in text and "口座の建玉は閉じていない" in text
    assert "今朝の記録の向き: BTC なし・ETH 買い 0.3" in rows2[0]["note"]
    assert cl.recorded_sides(PaperStore(tmp_path / "none")) == "記録なし"

    live.stop_file.unlink()
    live.config_path.write_text("{ 壊れた", encoding="utf-8")                # 設定のファイルが読めない朝
    rows3 = live_run(paper, live, client, real=True, now=now, log=lambda m_: None)
    assert [r["action"] for r in rows3] == ["error"] and "config.json" in rows3[0]["note"] and req.calls == []
    assert not live.stop_file.exists()


def test_broken_closes_are_not_read_as_no_loss(tmp_path):
    """2026-11-10（点検の指摘）: 終値が数字でない足（NaN・無限大）は、欠けた足と同じ。最後のほうが全部そうなら「日足が古い」で注文を出さない。"""
    t = pd.date_range(pd.Timestamp("2026-01-01 21:00", tz="UTC"), periods=40, freq="D")
    close = np.linspace(10_000_000.0, 7_800_000.0, 40)                      # 買いで持ったまま 22% 下げる
    good, bad = PaperStore(tmp_path / "good"), PaperStore(tmp_path / "bad")
    _klines(good, "BTC_JPY", t, close)
    broken = close.copy()
    broken[20] = np.inf                                                     # 途中の 1 本が無限大: 前の終値のままにして、次の足でまとめて数える
    _klines(bad, "BTC_JPY", t, broken)
    for st in (good, bad):
        _rows(st, "BTC_JPY", [(b, 1, 0.1, 0.001) for b in t[:39]])
    a, b = paper_equity(good), paper_equity(bad)
    assert np.isfinite(b.to_numpy()).all() and b.iloc[-1] == pytest.approx(a.iloc[-1], abs=500) and drawdown(b) == pytest.approx(drawdown(a), abs=1e-3)

    tail = PaperStore(tmp_path / "tail")
    last = close.copy()
    last[30:] = np.nan                                                      # 最後の 10 本の終値が無い（時刻だけある）
    tail.klines_path("BTC_JPY").parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": 1.0, "high": 1.0, "low": 1.0, "close": last, "volume": 1.0}).to_csv(
        tail.klines_path("BTC_JPY"), index=False)
    _rows(tail, "BTC_JPY", [(b, 1, 0.1, 0.001) for b in t[:39]])
    live = LiveStore(tmp_path / "live")
    req = FakeRequest()
    rows = live_run(tail, live, GmoClient("k", "s", request=req, clock=lambda: 1.7e9), real=True, now=t[-1] + pd.Timedelta(hours=25),
                    log=lambda m: None)
    assert [r["action"] for r in rows] == ["error"] and "日足が古い" in rows[0]["note"] and req.calls == []

