"""フィリップと同じ形の作ったデータで、毎週の確認（./cfd demo-check）を通しで確かめる。

- 4 銘柄（XAUUSD.ps01・XAGUSD.ps01・XTIUSD.ps01・XBRUSD.ps01）の 1 時間足。サーバー時刻は日本時間。
  月曜の朝（ブレントは 9 時）から土曜の朝 6 時まで。毎朝 6 時台は休み。日足は土曜の短い足もできる
- EA のログは実物（2026-10-14 のエキスパートタブ）と同じ形: `CfdCommodityEA (XAUUSD.ps01,Daily)`、UTF-16、
  平日の足は翌日 0 時、金曜の足は土曜 0 時、土曜の足は月曜の朝（ブレントは 9 時すぎ）に判断する
- 書き出しは 2026-10-14（水）15:32。その日の足は途中なので比べない
正しく動く EA のログなら「一致」、Mac のスリープ・発注拒否・二重判断・終値の差は、それぞれ正しく知らせることを確かめる。
"""

import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdbot.demo_check import python_daily

ROOT = Path(__file__).resolve().parents[1]
EXPORT = pd.Timestamp("2026-10-14 15:32")          # 書き出した時刻（サーバー時刻 = 日本時間）
UNTIL = datetime(2026, 10, 14, 15, 40)             # ログを集めた時刻
START = date(2026, 7, 1)                           # 売買と建玉が期間に入るよう、デモより前から
# MT5 の銘柄名: (銘柄キー, 値, 1 日の動き, 桁, 月曜に開く時刻, 乱数の種)
SYMS = {
    "XAUUSD.ps01": ("GOLD", 2600.0, 0.011, 2, 7, 11),
    "XAGUSD.ps01": ("SILVER", 45.0, 0.020, 4, 7, 12),
    "XTIUSD.ps01": ("WTI", 70.0, 0.022, 3, 7, 13),
    "XBRUSD.ps01": ("BRENT", 74.0, 0.021, 3, 9, 14),
}
WHAT = {1: "買いのシグナル", -1: "売りのシグナル", 0: "シグナルなし"}


def _hours(monday_open: int) -> pd.DatetimeIndex:
    t = pd.date_range("2024-09-30 07:00", EXPORT.floor("h"), freq="1h")
    wd, h = t.weekday, t.hour
    keep = (((wd <= 4) & (h != 6)) | ((wd == 5) & (h < 6))) & ~((wd == 0) & (h < monday_open))
    return t[keep]


def _write_h1(path: Path, monday_open: int, price: float, dvol: float, digits: int, seed: int) -> None:
    rng = np.random.default_rng(seed)
    t = _hours(monday_open)
    n = len(t)
    sig = dvol / np.sqrt(22)
    reg = rng.integers(22 * 20, 22 * 80, size=n // (22 * 20) + 2)       # 20〜80 日続く向き（ブレイクアウトが起きる）
    drift = np.repeat(rng.normal(0, dvol * 0.3 / 22, size=len(reg)), reg)[:n]
    c = np.exp(np.log(price) + np.cumsum(drift + sig * rng.standard_normal(n)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, sig * 0.5, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, sig * 0.5, n)))
    q = 0.0005 if digits == 4 else 10.0 ** -digits    # 銀は 4 桁で、刻みは 0.0005
    o, h, lo, c = (np.round(np.round(x / q) * q, digits) for x in (o, h, lo, c))
    h, lo = np.maximum.reduce([h, o, c]), np.minimum.reduce([lo, o, c])
    f = f"%.{digits}f"
    with open(path, "w", newline="") as fh:
        fh.write("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\r\n")
        for ts, a, b, x, y in zip(t, o, h, lo, c):
            fh.write(f"{ts:%Y.%m.%d}\t{ts:%H:%M:%S}\t{f % a}\t{f % b}\t{f % x}\t{f % y}\t100\t0\t30\r\n")


@pytest.fixture(scope="module")
def phillip(tmp_path_factory):
    root = tmp_path_factory.mktemp("phillip")
    data = root / "data"
    data.mkdir()
    for name, (_, price, dvol, digits, mon, seed) in SYMS.items():
        _write_h1(data / f"{name}_H1.csv", mon, price, dvol, digits, seed)
    (data / "account_info.txt").write_text(f"exported={EXPORT:%Y.%m.%d %H:%M}\n", encoding="utf-8")
    cfg = root / "train.toml"
    cfg.write_text(f'[data]\ndir = "{data}"\nserver_tz = 9\nsignal_timeframes = ["D1"]\nevents = ""\n', encoding="utf-8")
    fpath = ROOT / "config" / "baselines" / "turtle55_d1_ext.json"
    final = json.loads(fpath.read_text(encoding="utf-8"))

    sys.path.insert(0, str(ROOT / "scripts"))
    from compare_signals import signals_for_sleeve
    from replay import replay

    from cfdbot.train.config import load_train_config
    from cfdbot.train.dataset import load_dataset

    c = load_train_config(cfg)
    c.account.equity = 10_000_000
    ds = load_dataset(c.data)
    res, *_ = replay(c, final, START.isoformat(), ds=ds)
    truth, signals, first_hour = {}, {}, {}
    for sl in final["sleeves"]:
        sym = sl["symbol"]
        sig = signals_for_sleeve(ds, sl)
        signals[sym] = sig
        truth[sym] = python_daily(sig, 9, pd.Timedelta(days=1), res["trades"], sym).reset_index()
        h1 = ds.frames[sym]["H1"].index.tz_convert("Asia/Tokyo").tz_localize(None)   # 足の始まり（日本時間）
        first_hour[sym] = pd.Series(h1, index=h1.date).groupby(level=0).min()
    return {"root": root, "cfg": cfg, "final": fpath, "truth": truth, "signals": signals, "trades": res["trades"],
            "first_hour": first_hour}


def _judge_time(fx, sym: str, k: int) -> datetime | None:
    """足 k を EA が判断する時刻（次の足の最初の値動き）。まだなら None。"""
    tr = fx["truth"][sym]
    if k + 1 >= len(tr):
        return None
    nxt = fx["first_hour"][sym][tr.loc[k + 1, "bar_date"]].to_pydatetime()
    t = nxt + (timedelta(milliseconds=300) if nxt.hour == 0 else timedelta(minutes=1, milliseconds=200))
    return t if t <= UNTIL else None


def _write_logs(fx, name: str, sleep=None, reject=None, twice=None, close=None) -> Path:
    """正しく動く EA のログ。sleep=(始まり, 終わり) の間は書かない（起きたら最新の確定足だけ判断する）。"""
    out = fx["root"] / name
    out.mkdir()
    lines: dict[date, list[str]] = {}

    def emit(t: datetime, chart: str, msg: str) -> None:
        lines.setdefault(t.date(), []).append(
            (t, f"KO\t0\t{t:%H:%M:%S}.{t.microsecond // 1000:03d}\tCfdCommodityEA ({chart},Daily)\t{msg}"))

    for chart, (sym, *_rest) in SYMS.items():
        dig = SYMS[chart][3]
        tr = fx["truth"][sym]
        refused = False   # 発注拒否で建玉を持てず、Python の取引が終わるまで EA は 0
        for k in tr.index[tr["bar_date"] >= START]:
            t = _judge_time(fx, sym, k)
            if t is None:
                continue
            if sleep and sleep[0] <= t < sleep[1]:
                wake = sleep[1]
                if k + 2 < len(tr) and (_judge_time(fx, sym, k + 1) or UNTIL) <= wake:
                    continue                               # 寝ている間に次の足も終わった → この足は判断されない
                t = wake + timedelta(seconds=37)
            r = tr.loc[k]
            d = r["bar_date"]
            cl = close.get((sym, d)) if close else None
            ext = "" if pd.isna(r["py_ext"]) else f"、直近 20 本の動き {r['py_ext']:+.2f} 倍（1.5 倍超の向きには入らない）"
            refused = refused and int(r["py_held"]) > 0
            held = 0 if refused else int(r["py_held"])
            msg = (f"{d:%Y.%m.%d} の足を判断: 終値 {cl or format(r['py_close'], f'.{dig}f')} → "
                   f"{WHAT[int(r['py_signal'])]}{ext}、保有 {held}")
            emit(t, chart, msg)
            if twice == (sym, d):
                emit(t + timedelta(milliseconds=5), chart, msg)
            if reject == (sym, d):   # この足のシグナルで入るはずの注文が断られた
                emit(t + timedelta(seconds=6), chart, "order failed rc=10019 No money")
                emit(t + timedelta(seconds=6), chart, f"{chart} 発注拒否 rc=10019 (No money) lots=0.07")
                refused = True
    for d, ls in lines.items():
        body = "\r\n".join(x for _, x in sorted(ls)) + "\r\n"
        (out / f"{d:%Y%m%d}.log").write_bytes(b"\xff\xfe" + body.encode("utf-16-le"))
    return out


def _run(fx, logs: Path) -> tuple[str, pd.DataFrame]:
    out_dir = fx["root"] / f"out_{logs.name}"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "demo_check.py"), "--final", str(fx["final"]),
                        "--start", START.isoformat(), "--equity", "10000000", "--config", str(fx["cfg"]),
                        "--logs", str(logs), "--out", str(out_dir)], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-3000:]
    days = pd.read_csv(next(out_dir.glob("*/days.csv")))
    return r.stdout, days


def test_replay_trades_follow_full_history_signals(phillip):
    """Python の再現の売買が、全部の過去で計算したシグナルどおり（EA は 4000 本で計算する）。"""
    for _, t in phillip["trades"].iterrows():
        sig = phillip["signals"][t["symbol"]]["entry"]
        assert sig[sig.index < t["entry_time"]].iloc[-1] == (1 if t["side"] > 0 else -1)


def _entry_bar(fx, sym: str) -> date:
    """期間の中で Python が建玉を持ち始める足の、1 本前（シグナルの足）。"""
    tr = fx["truth"][sym]
    k = tr.index[(tr["bar_date"] >= START) & (tr["py_held"] > 0) & (tr["py_held"].shift(1) == 0)][0]
    return tr.loc[k - 1, "bar_date"]


def test_correct_ea_matches(phillip):
    out, days = _run(phillip, _write_logs(phillip, "perfect"))
    assert "**一致**（GOLD 一致、SILVER 一致、WTI 一致、BRENT 一致）" in out
    assert days["ea_held"].max() == 1 and "増えた 4 回" in out            # 建玉を持つ日も比べている
    assert "書き出しより後" not in out and "抜け" not in out.split("## 結果")[1].split("\n")[1]
    sat = days[days["bar_date"] == "2026-10-10"]                 # 土曜の短い足（月曜の朝に判断）も比べている
    assert len(sat) == 4 and set(sat["status"]) == {"一致"}
    assert set(days["status"]) == {"一致"} and days["bar_date"].max() == "2026-10-13"   # 10/14 は書き出しのとき途中


def test_mac_asleep_is_a_gap(phillip):
    # 10/12 23:00 〜 10/14 07:40 にスリープ: 10/12 の足は判断されず、10/13 の足は起きたときに判断する
    logs = _write_logs(phillip, "asleep", sleep=(datetime(2026, 10, 12, 23), datetime(2026, 10, 14, 7, 40)))
    out, days = _run(phillip, logs)
    gap = days[days["status"] == "抜け"]
    assert set(gap["bar_date"]) == {"2026-10-12"} and len(gap) == 4
    assert "EA の記録の抜け 1 日（条件 2）" in out and "✗ 抜け" in out          # 最後の 5 日に抜けがある
    assert set(days.loc[days["bar_date"] == "2026-10-13", "status"]) == {"一致"}


def test_rejected_order_and_double_judgment(phillip):
    bar = _entry_bar(phillip, "GOLD")
    logs = _write_logs(phillip, "issues", reject=("GOLD", bar), twice=("SILVER", date(2026, 10, 12)))
    out, days = _run(phillip, logs)
    assert "**差あり**（GOLD 差あり" in out and "要確認の行 2 件" in out and "同じ足の二重判断 1 件" in out
    assert "XAUUSD.ps01: 発注拒否" in out and "同じ足を 2 回判断した日: 2026-10-12" in out
    gold = days[(days["symbol"] == "GOLD") & (days["status"] == "差あり")]
    assert set(gold["diffs"]) == {"保有数"} and set(gold["ea_held"]) == {0} and set(gold["py_held"]) == {1}
    assert "EA が発注できなかった" in out


def test_one_tick_close_difference(phillip):
    tr = phillip["truth"]["SILVER"].set_index("bar_date")
    d = date(2026, 10, 12)
    off = f"{tr.loc[d, 'py_close'] + 0.0005:.4f}"
    out, _ = _run(phillip, _write_logs(phillip, "close", close={("SILVER", d): off}))
    assert "**差あり**" in out
    assert f"| 2026-10-12 | 終値 | {off} / {tr.loc[d, 'py_close']:.4f} |" in out   # EA の桁のまま出す
