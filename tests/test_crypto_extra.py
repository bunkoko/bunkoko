"""他の通貨（docs/crypto.md 9 章）: 毎朝の記録・先の判定・過去の検証の判定と通しの計算。作ったデータだけで試す。"""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

from cfdbot.crypto import (EXTRA, LOG_COLUMNS, PaperStore, extra_available, extra_judge, paper_extra_report,
                           paper_extra_run, parse_symbols, symbols_url)

ROOT = Path(__file__).resolve().parents[1]


def _bars(start: str, days: int, seed: int, drift: float = 0.004, price: float = 50_000.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=days, freq="D")
    close = price * np.exp(np.cumsum(rng.normal(drift, 0.02, days)))
    return pd.DataFrame({"time": t, "open": close, "high": close * 1.02, "low": close * 0.98, "close": close,
                         "volume": 1.0})


def _klines_json(bars: pd.DataFrame, year: int) -> bytes:
    rows = [{"openTime": str(int(t.timestamp() * 1000)), "open": f"{o:.4f}", "high": f"{h:.4f}", "low": f"{lo:.4f}",
             "close": f"{c:.4f}", "volume": "1.0"}
            for t, o, h, lo, c in zip(bars["time"], bars["open"], bars["high"], bars["low"], bars["close"])
            if t.year == year]
    return json.dumps({"status": 0, "data": rows}).encode()


def _symbols_json(names) -> bytes:
    return json.dumps({"status": 0, "data": [{"symbol": n, "minOrderSize": "0.1", "sizeStep": "0.1"} for n in names]}).encode()


class FakeGmo:
    """公開 API の代わり。/symbols は渡した一覧、日足・値段は bars から。broken の記号は値段で 500 を返す。"""

    def __init__(self, bars: dict[str, pd.DataFrame], listed, broken=()):
        self.bars, self.listed, self.broken, self.calls = bars, list(listed), set(broken), []

    def __call__(self, url: str) -> tuple[int, bytes]:
        self.calls.append(url)
        if url == symbols_url():
            return 200, _symbols_json(self.listed)
        sym = url.split("symbol=")[1].split("&")[0]
        if "/klines" in url:
            year = int(url.split("date=")[1])
            if year < self.bars[sym]["time"].iloc[0].year:
                return 404, b'{"status":1}'
            return 200, _klines_json(self.bars[sym], year)
        if sym in self.broken:
            return 500, b"{}"
        mid = float(self.bars[sym]["close"].iloc[-1])
        return 200, json.dumps({"status": 0, "data": [{"ask": f"{mid * 1.001:.4f}", "bid": f"{mid * 0.999:.4f}",
                                                       "last": f"{mid:.4f}"}]}).encode()


def test_the_real_symbol_list_decides_what_is_recorded():
    have = parse_symbols(_symbols_json(["BTC_JPY", "ETH_JPY", "BCH_JPY", "XRP_JPY", "PEPE_JPY", "BTC"]))
    assert have["BCH_JPY"]["minOrderSize"] == 0.1
    a = extra_available(lambda url: (200, _symbols_json(["BTC_JPY", "ETH_JPY", "BCH_JPY", "XRP_JPY", "PEPE_JPY", "BTC"])))
    assert a["ok"] == ["BCH_JPY", "XRP_JPY"]                       # 9 章の表にあり、実物の一覧にもある
    assert "SOL_JPY" in a["missing"] and "BCH_JPY" not in a["missing"]
    assert a["new"] == ["PEPE_JPY"]                                # 表に無い新しい記号は記録しない（知らせるだけ）
    assert len(EXTRA) == 9 and "BTC_JPY" not in EXTRA


def test_extra_run_writes_its_own_log_once_and_isolates_failures(tmp_path):
    bars = {s: _bars("2025-01-01 21:00", 400, i + 1, drift=0.006) for i, s in enumerate(("BCH_JPY", "XRP_JPY", "SOL_JPY"))}
    gmo = FakeGmo(bars, ["BTC_JPY", "ETH_JPY", "BCH_JPY", "XRP_JPY", "SOL_JPY"], broken=["SOL_JPY"])
    st = PaperStore(tmp_path)
    now = bars["BCH_JPY"]["time"].iloc[-1] + pd.Timedelta(days=1, hours=1)
    waits, said = [], []
    info = paper_extra_run(st, get=gmo, now=now, log=said.append, sleep=waits.append)
    log = st.load_log(extra=True)
    assert list(log.columns) == LOG_COLUMNS and sorted(log["symbol"]) == ["BCH_JPY", "XRP_JPY"]
    assert info["logged"] == 2 and len(info["problems"]) == 1 and "SOL_JPY" in info["problems"][0]
    assert not st.log_path.exists()                                # BTC・ETH の記録（log.csv）には書かない
    assert waits and all(w > 0 for w in waits)                     # 問い合わせの間を空ける
    assert any("一覧に無いので飛ばす" in m for m in said)
    assert (log[log["symbol"] == "BCH_JPY"]["position"] == 1).all()  # 強い上げ → 買い（数量の最小単位で止められない）
    assert json.loads(st.extra_symbols_path.read_text(encoding="utf-8"))["ok"] == ["BCH_JPY", "XRP_JPY", "SOL_JPY"]
    paper_extra_run(st, get=gmo, now=now, log=lambda m: None, sleep=lambda s: None)   # 同じ日にもう一度 → 増えない
    assert len(st.load_log(extra=True)) == 2
    # 2019 年から扱いのある通貨は 2019 年から、2024 年からの通貨は 2024 年から日足を取りに行く
    assert any("symbol=BCH_JPY" in u and "date=2019" in u for u in gmo.calls)
    assert not any("symbol=SOL_JPY" in u and "date=2023" in u for u in gmo.calls)


def _rows(sym, bars, position, spread=0.001):
    return [{"logged_at": t, "symbol": sym, "bar_time": t, "close": c, "position": position, "qty": 1.0,
             "bid": c * (1 - spread / 2), "ask": c * (1 + spread / 2), "spread_pct": spread}
            for t, c in zip(bars["time"], bars["close"])]


def test_extra_report_waits_for_250_days_and_drops_wide_spreads(tmp_path):
    st = PaperStore(tmp_path)
    n = 320
    btc, eth = _bars("2026-01-01 21:00", n, 11, drift=0.0), _bars("2026-01-01 21:00", n, 12, drift=0.0)
    bch, xrp, doge = (_bars("2026-01-01 21:00", n, 13, drift=0.004), _bars("2026-01-01 21:00", n, 14, drift=0.004),
                      _bars("2026-01-01 21:00", n, 15, drift=-0.004))
    for sym, b in (("BTC_JPY", btc), ("ETH_JPY", eth), ("BCH_JPY", bch), ("XRP_JPY", xrp), ("DOGE_JPY", doge)):
        st.save_klines(sym, b)
    st.append_log(_rows("BTC_JPY", btc, 1) + _rows("ETH_JPY", eth, 1))
    st.append_log(_rows("BCH_JPY", bch, 1)[:100] + _rows("XRP_JPY", xrp, 1)[:100] + _rows("DOGE_JPY", doge, 1, 0.01)[:100],
                  extra=True)
    lines, info = paper_extra_report(st, now=bch["time"].iloc[100])
    assert "途中経過" in info["verdict"] and "あと 150 日" in info["verdict"]
    st.append_log(_rows("BCH_JPY", bch, 1)[100:] + _rows("XRP_JPY", xrp, 1)[100:] + _rows("DOGE_JPY", doge, 1, 0.01)[100:],
                  extra=True)
    lines, info = paper_extra_report(st, now=bch["time"].iloc[-1])
    assert info["groups"]["wide"] == ["DOGE_JPY"]                  # 往復 1% → 組から外す（下げている通貨でも）
    assert info["groups"]["extra"] > 0 and "満たす" in info["verdict"]
    assert any("ドージコイン" in ln for ln in lines) and any("外した" in ln for ln in lines)


def _daily(seed, mu, sd=0.02, start="2019-02-01", end="2026-10-01"):
    idx = pd.date_range(start, end, freq="D")
    return pd.Series(np.random.default_rng(seed).normal(mu, sd, len(idx)), index=idx)


def test_extra_judge_three_conditions():
    base = {"BTC": _daily(1, 0.0006), "ETH": _daily(2, 0.0006)}
    strong = {k: _daily(10 + i, 0.003) for i, k in enumerate(("BCH", "LTC", "XRP"))}
    strong["SOL"] = _daily(20, 0.003, start="2024-04-14")
    j = extra_judge(base, strong)
    assert j["c1"] and j["c2"] and j["c3"] and j["verdict"] == "足す候補"
    assert abs(j["bar"] - 2.469) < 0.01
    flat = {k: _daily(30 + i, 0.0) for i, k in enumerate(("BCH", "LTC", "XRP"))}
    assert extra_judge(base, flat)["verdict"] == "足さない"
    # 前半・後半とも良いが、t 値が割り引いた基準に届かない → まだ判断できない
    j2 = extra_judge(base, {"BCH": _daily(40, 0.0015)}, n_trials=10**9)
    assert j2["c1"] and j2["c2"] and not j2["c3"] and j2["verdict"] == "まだ判断できない"


def _write_yahoo(path: Path, start: str, end: str, seed: int, drift: float) -> None:
    idx = pd.date_range(start, end, freq="D")
    c = 100 * np.exp(np.cumsum(np.random.default_rng(seed).normal(drift, 0.03, len(idx))))
    pd.DataFrame({"date": idx.strftime("%Y-%m-%d"), "open": c, "high": c * 1.03, "low": c * 0.97, "close": c}).to_csv(
        path, index=False)


def test_extra_command_runs_end_to_end_on_made_up_data(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("crypto_study", ROOT / "scripts" / "crypto_study.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    data = tmp_path / "universe"
    data.mkdir()
    _write_yahoo(data / "BTC.csv", "2018-06-01", "2026-10-01", 1, 0.001)
    _write_yahoo(data / "ETH.csv", "2018-06-01", "2026-10-01", 2, 0.001)
    for i, (sym, v) in enumerate(EXTRA.items()):
        if v[0] == "LINK":
            continue                                               # 1 つ欠けている → 判定しない
        _write_yahoo(data / f"{v[0]}.csv", "2018-06-01" if v[2] < "2024" else "2023-06-01", "2026-10-01", 3 + i, 0.001)
    args = mod.build_parser().parse_args(["extra", "--data", str(data), "--out", str(tmp_path / "out"),
                                          "--final", str(ROOT / "config/baselines/turtle55_d1_ext.json"),
                                          "--config", str(ROOT / "config/train.toml")])
    args.fetch = False
    mod.fetch_yahoo = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("試験ではネットにつながない"))
    mod.cmd_extra(args)
    report = next((tmp_path / "out" / "extra").glob("*/report.md")).read_text(encoding="utf-8")
    assert "## 1. 判定" in report and "判定しない（通貨が欠けている" in report and "チェーンリンク" in report
    assert "| ビットコインキャッシュ | 2019-02-0" in report            # 2019-02-01 から（週末をはさむので最初の日は 02-04）
    assert "| ソラナ | 2024-04-1" in report
