"""デモの照らし合わせ（./cfd demo-check）のテスト。ログは実物を見ていないので、合成したもので書く。"""

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from cfdbot.demo_check import (compare_symbol, held_counts, parse_lines, python_daily, read_logs, reasons,
                               server_dates, shifted_close_match)

ROOT = Path(__file__).resolve().parents[1]


def judge_line(d: str, close: str, what: str, held: int, ext: str | None = "+0.53", chart="XAUUSD.ps01",
               tf="D1", time="00:00:05.123") -> str:
    ext_part = f"、直近 20 本の動き {ext} 倍（1.5 倍超の向きには入らない）" if ext is not None else ""
    return f"CS\t0\t{time}\tCfdCommodityEA ({chart},{tf})\t{d} の足を判断: 終値 {close} → {what}{ext_part}、保有 {held}"


def write_log(path: Path, lines: list[str]) -> None:
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-16"))   # MT5 と同じ BOM 付き UTF-16


# --------------------------------------------------------------------------- 読み取り
def test_parse_judgment_and_issue_lines():
    lines = [
        judge_line("2026.10.06", "2650.12", "買いのシグナル", 0),
        judge_line("2026.10.07", "31.205", "シグナルなし", 1, ext="-1.62", chart="XAGUSD.ps01"),
        judge_line("2026.10.08", "70.10", "売りのシグナル", 2, ext=None, chart="WTI.ps01"),
        "CS\t0\t00:00:06.000\tCfdCommodityEA (XAUUSD.ps01,D1)\tentry 1 rejected: heat",
        "CS\t0\t00:00:06.000\tCfdCommodityEA (XAUUSD.ps01,D1)\torder failed rc=10019 no money",
        "CS\t0\t00:00:06.000\tCfdCommodityEA (XAUUSD.ps01,D1)\tXAUUSD.ps01 発注拒否 rc=10019 (no money) lots=0.10",
        "CS\t0\t00:00:06.000\tCfdCommodityEA (XAUUSD.ps01,D1)\tstale bar, skip entry",
        "CS\t0\t09:00:00.000\tCfdCommodityEA (XAUUSD.ps01,D1)\tCfdCommodityEA start XAUUSD.ps01 strategy=1 magic=2609004",
        "CS\t0\t09:00:00.000\tOtherEA (XAUUSD.ps01,D1)\tentry rejected: something",     # 他の EA は数えない
        "2026.10.09 の足を判断: 終値 1.0 → シグナルなし、保有 0",                         # 発信元が無い
    ]
    j, ev, unattributed, unparsed = parse_lines(lines, date(2026, 10, 9), "20261009.log")
    assert len(j) == 3 and unattributed == 1 and not unparsed
    g = j[0]
    assert (g["chart"], g["tf"], g["bar_date"], g["close"], g["close_dec"], g["signal"], g["held"]) == \
        ("XAUUSD.ps01", "D1", date(2026, 10, 6), 2650.12, 2, 1, 0)
    assert g["ext"] == 0.53 and g["ext_period"] == 20 and g["ext_max"] == 1.5 and g["time"] == "00:00:05"
    assert j[1]["ext"] == -1.62 and j[1]["close_dec"] == 3
    assert j[2]["signal"] == -1 and np.isnan(j[2]["ext"])
    kinds = [(e["kind"], e["severity"]) for e in ev]
    assert kinds == [("エントリーの見送り", "注意"), ("発注の失敗", "要確認"), ("発注拒否", "要確認"),
                     ("遅れた足のため見送り", "注意"), ("EA の起動", "情報")]
    assert ev[1]["text"].startswith("order failed")


def test_parse_is_lenient_about_layout():
    # 区切りが空白・時刻に小数なし・発信元が後ろ、でも読める
    lines = ["OK 0 00:00:05 CfdCommodityEA (XAUUSD.ps01, Daily)   2026.10.06 の足を判断: 終値 2650 → シグナルなし、"
             "直近 20 本の動き -0.10 倍（1.5 倍超の向きには入らない）、保有 0"]
    j, _, _, _ = parse_lines(lines)
    assert j[0]["tf"] == "D1" and j[0]["close_dec"] == 0 and j[0]["ext"] == -0.10
    # 形が崩れた行は捨てずに知らせる
    _, _, _, bad = parse_lines(["CfdCommodityEA (XAUUSD,D1) 2026.10.06 の足を判断: ？？？"])
    assert len(bad) == 1


def test_read_logs_utf16_and_dates(tmp_path):
    write_log(tmp_path / "20261007.log", [judge_line("2026.10.06", "1.00", "シグナルなし", 0)])
    write_log(tmp_path / "20261008.log", [judge_line("2026.10.07", "1.10", "シグナルなし", 0)])
    (tmp_path / "20261009.log").write_text(judge_line("2026.10.08", "1.20", "シグナルなし", 0), encoding="utf-8")
    log = read_logs(sorted(tmp_path.glob("*.log")))
    assert list(log.judgments["bar_date"]) == [date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)]
    assert (log.first_date, log.last_date) == (date(2026, 10, 7), date(2026, 10, 9))


# --------------------------------------------------------------------------- Python 側
def test_server_dates_japan_and_ny_close():
    # フィリップ（日本時間）: 日足は日本時間 0 時 = 前日 15:00 UTC に始まる
    idx = pd.DatetimeIndex([pd.Timestamp("2026-10-05 15:00", tz="UTC")])
    assert server_dates(idx, 9) == [date(2026, 10, 6)]
    # NY クローズ方式: 米東部 17:00 = サーバー 0 時
    idx = pd.DatetimeIndex([pd.Timestamp("2026-10-05 21:00", tz="UTC")])   # 米東部 17:00（夏時間）
    assert server_dates(idx, "ny_close") == [date(2026, 10, 6)]


def test_held_counts_matches_ea_timing():
    idx = pd.date_range("2026-10-05 15:00", periods=5, freq="1D", tz="UTC")   # 10/06〜10/10 の足
    day = pd.Timedelta(days=1)
    trades = pd.DataFrame({
        "symbol": ["GOLD", "GOLD", "WTI"],
        # 10/06 の足のシグナル → 10/07 の始値で約定。10/08 の足の決済シグナル → 10/09 の始値で決済
        "entry_time": [idx[1], idx[3] + pd.Timedelta(hours=3), idx[0]],
        "exit_time": [idx[3], pd.NaT, idx[4]],
    })
    # 10/06: 0（約定前）, 10/07: 1, 10/08: 1（決済シグナルの足は EA が数えた後に決済）, 10/09: 1（足の途中で 2 本目）, 10/10: 1
    assert list(held_counts(idx, day, trades, "GOLD")) == [0, 1, 1, 1, 1]
    # 足の途中で損切り → その足の確定時には持っていない
    t2 = pd.DataFrame({"symbol": ["GOLD"], "entry_time": [idx[1]], "exit_time": [idx[2] + pd.Timedelta(hours=5)]})
    assert list(held_counts(idx, day, t2, "GOLD")) == [0, 1, 0, 0, 0]


# --------------------------------------------------------------------------- 照らし合わせ
def _py(dates, closes, signals, exts, held):
    return pd.DataFrame({"bar_date": dates, "py_close": closes, "py_signal": signals, "py_ext": exts,
                         "py_held": held})


def _ea(dates, closes, signals, exts, held, dec=2):
    return pd.DataFrame({"chart": "XAUUSD.ps01", "tf": "D1", "bar_date": dates, "close": closes,
                         "close_dec": dec, "signal": signals, "ext": exts, "held": held,
                         "file": "", "file_date": dates, "time": "00:00:05"})


D = [date(2026, 10, d) for d in (6, 7, 8, 9, 12, 13)]


def test_compare_symbol_statuses():
    py = _py(D, [10.0, 10.1, 10.2, 10.3, 10.4, 10.5], [0, 1, 0, 0, 0, 0], [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
             [0, 0, 1, 1, 1, 1])
    ea = _ea([D[0], D[1], D[3], D[4]], [10.0, 10.104, 10.3, 10.4], [0, 1, 0, -1], [0.1, 0.2049, 0.4, 0.5],
             [0, 0, 0, 1])
    days = compare_symbol(ea, py, D[0], last_log=date(2026, 10, 13))
    st = dict(zip(days["bar_date"], days["status"]))
    assert st[D[0]] == "一致" and st[D[1]] == "一致"      # 終値・行き過ぎは表示の丸め以内
    assert st[D[2]] == "抜け"                             # 次の足（10/09）がログの最後の日より前に始まっている
    assert st[D[3]] == "差あり" and days.set_index("bar_date").loc[D[3], "diffs"] == "保有数"
    assert days.set_index("bar_date").loc[D[4], "diffs"] == "シグナル"
    assert st[D[5]] == "判断待ち"                         # 最後の足は、次の足が来るまで判断されない


def test_compare_flags_close_ext_and_missing_ext():
    py = _py(D[:3], [10.0, 10.1, 10.2], [0, 0, 0], [0.1, 0.2, 0.3], [0, 0, 0])
    ea = _ea(D[:3], [10.0, 10.12, 10.2], [0, 0, 0], [0.1, 0.2, np.nan], [0, 0, 0])
    days = compare_symbol(ea, py, D[0], last_log=D[2]).set_index("bar_date")
    assert days.loc[D[1], "diffs"] == "終値"
    assert days.loc[D[2], "diffs"] == "行き過ぎ"
    why = reasons("GOLD", days.reset_index(), pd.DataFrame(columns=["kind"]), None)
    assert any("終値の差" in w for w in why) and any("ea_calc_bars" in w for w in why)


def test_duplicate_judgment_is_reported():
    py = _py(D[:2], [10.0, 10.1], [0, 0], [0.1, 0.2], [0, 0])
    ea = _ea([D[0], D[0], D[1]], [10.0, 10.0, 10.1], [0, 0, 0], [0.1, 0.1, 0.2], [0, 0, 0])
    days = compare_symbol(ea, py, D[0], last_log=D[1])
    assert bool(days["twice"].iloc[0])


def test_shifted_close_detects_date_mapping():
    py = _py(D, [1, 2, 3, 4, 5, 6], [0] * 6, [0.0] * 6, [0] * 6)
    ea = _ea(D[:5], [2, 3, 4, 5, 6], [0] * 5, [0.0] * 5, [0] * 5)   # EA の日付が 1 本前にずれている
    assert shifted_close_match(ea, py) == 1
    assert shifted_close_match(_ea(D[:5], [1, 2, 3, 4, 5], [0] * 5, [0.0] * 5, [0] * 5), py) is None


def test_python_daily_columns():
    idx = pd.date_range("2026-10-05 15:00", periods=3, freq="1D", tz="UTC")
    sig = pd.DataFrame({"close": [1.0, 2.0, 3.0], "entry": [0, 1, 0], "ext_z": [0.1, 0.2, 0.3]}, index=idx)
    out = python_daily(sig, 9, pd.Timedelta(days=1), pd.DataFrame(), "GOLD")
    assert list(out["bar_date"]) == [date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)]
    assert list(out["py_signal"]) == [0, 1, 0] and list(out["py_held"]) == [0, 0, 0]


# --------------------------------------------------------------------------- コマンド全体
def _write_d1(path: Path, seed: int, days: int = 500):
    """日本時間 0 時始まりの日足（平日だけ）を MT5 の書き出しの形で書く。"""
    rng = np.random.default_rng(seed)
    srv = pd.bdate_range(end="2026-11-06", periods=days)
    c = 2000 * np.exp(np.cumsum(rng.normal(0.001, 0.012, days)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + rng.uniform(0, 0.005, days))
    lo = np.minimum(o, c) * (1 - rng.uniform(0, 0.005, days))
    pd.DataFrame({
        "<DATE>": srv.strftime("%Y.%m.%d"), "<TIME>": "00:00:00",
        "<OPEN>": o.round(2), "<HIGH>": h.round(2), "<LOW>": lo.round(2), "<CLOSE>": c.round(2),
        "<TICKVOL>": 1, "<VOL>": 0, "<SPREAD>": 30,
    }).to_csv(path, sep="\t", index=False)


def _setup(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _write_d1(data / "XAUUSD.ps01_D1.csv", 1)
    cfg = tmp_path / "train.toml"
    cfg.write_text(f'[data]\ndir = "{data}"\nserver_tz = 9\nsignal_timeframes = ["D1"]\nfill_timeframe = "none"\n'
                   f'events = ""\n', encoding="utf-8")
    final = json.loads((ROOT / "config" / "baselines" / "turtle55_d1_ext.json").read_text(encoding="utf-8"))
    final["sleeves"] = [s for s in final["sleeves"] if s["symbol"] == "GOLD"]
    fpath = tmp_path / "final.json"
    fpath.write_text(json.dumps(final), encoding="utf-8")
    return cfg, fpath, final


def _python_truth(cfg, final, start):
    sys.path.insert(0, str(ROOT / "scripts"))
    from compare_signals import signals_for_sleeve
    from replay import replay

    from cfdbot.train.config import load_train_config
    from cfdbot.train.dataset import load_dataset

    c = load_train_config(cfg)
    c.account.equity = 10_000_000
    ds = load_dataset(c.data)
    res, *_ = replay(c, final, start, ds=ds)
    sig = signals_for_sleeve(ds, final["sleeves"][0])
    return python_daily(sig, 9, pd.Timedelta(days=1), res["trades"], "GOLD")


def _ea_logs(logs: Path, truth: pd.DataFrame, start: date, skip=(), flip=None):
    logs.mkdir(exist_ok=True)
    what = {1: "買いのシグナル", -1: "売りのシグナル", 0: "シグナルなし"}
    rows = truth[truth["bar_date"] >= start]
    nxt = list(rows["bar_date"].iloc[1:]) + [None]
    for (_, r), judged_on in zip(rows.iterrows(), nxt):
        if judged_on is None or r["bar_date"] in skip:
            continue
        sig = int(r["py_signal"]) if r["bar_date"] != flip else (1 if int(r["py_signal"]) != 1 else 0)
        line = judge_line(r["bar_date"].strftime("%Y.%m.%d"), f"{r['py_close']:.2f}", what[sig], int(r["py_held"]),
                          ext=f"{r['py_ext']:+.2f}")
        write_log(logs / f"{judged_on:%Y%m%d}.log", [line])


def _run(cfg, fpath, logs, tmp_path, start="2026-10-06"):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "demo_check.py"), "--final", str(fpath),
                        "--start", start, "--config", str(cfg), "--logs", str(logs), "--out", str(tmp_path / "out"),
                        "--equity", "10000000"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-3000:]
    return r.stdout


def test_demo_check_end_to_end(tmp_path):
    cfg, fpath, final = _setup(tmp_path)
    start = date(2026, 10, 6)
    truth = _python_truth(cfg, final, "2026-10-06")
    _ea_logs(tmp_path / "logs", truth, start)
    out = _run(cfg, fpath, tmp_path / "logs", tmp_path)
    assert "**一致**" in out and "差あり 0 日" in out and "抜け 0 日" in out
    assert "増えた 1 回" in out and "Python の再現の取引: 1 回" in out
    assert "✓ 終値・シグナル・行き過ぎ: すべて一致" in out
    assert list((tmp_path / "out").glob("*/report.md"))

    # 1 日抜け、1 日シグナルが違う → 差あり。抜けは数え、シグナルの差を表に出す
    bad = tmp_path / "logs_bad"
    d_gap, d_flip = date(2026, 10, 14), date(2026, 10, 20)
    _ea_logs(bad, truth, start, skip=(d_gap,), flip=d_flip)
    write_log(bad / "20261021.log", [judge_line("2026.10.20", "1.00", "シグナルなし", 0).replace(
        "2026.10.20 の足を判断: 終値 1.00 → シグナルなし、直近 20 本の動き +0.53 倍（1.5 倍超の向きには入らない）、保有 0",
        "決済失敗 (exit) rc=10018")] + [
        judge_line(d_flip.strftime("%Y.%m.%d"), f"{truth.set_index('bar_date').loc[d_flip, 'py_close']:.2f}",
                   "買いのシグナル" if int(truth.set_index('bar_date').loc[d_flip, 'py_signal']) != 1 else "シグナルなし",
                   int(truth.set_index('bar_date').loc[d_flip, 'py_held']),
                   ext=f"{truth.set_index('bar_date').loc[d_flip, 'py_ext']:+.2f}")])
    out = _run(cfg, fpath, bad, tmp_path)
    assert "**差あり**" in out and "抜け 1 日" in out and "2026-10-14" in out
    assert "| 2026-10-20 | シグナル |" in out
    assert "[要確認]" in out and "決済失敗" in out
