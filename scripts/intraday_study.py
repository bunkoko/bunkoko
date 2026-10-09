"""5 分足〜1 時間足の短い売買で、成り立つやり方があるかを探す（./cfd intraday）。

    python scripts/intraday_study.py --fetch                 # Dukascopy の 1 分足を取って 5 分足にし、検証（初回は時間がかかる）
    python scripts/intraday_study.py --source mt5            # Mac のフィリップの M5（./cfd data の data/）で検証

まずコストの小さい場合で候補を探し（Dukascopy なら取引所に近いスプレッド、フィリップのデータならスプレッドの 1 割）、
候補だけを実際のコスト（フィリップのスプレッド）でも見る。あわせて「損益が 0 になる往復コスト」を出す。
判定の条件は docs/intraday.md（結果を見る前に決めたもの）。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot.context import NotFound, _get  # noqa: E402
from cfdbot.intraday import (MARKETS, breakeven_bps, duka_url, evaluate, fetch_days, load_m5, m5_path,  # noqa: E402
                             parse_candles, positions, resample, rules, sharpe, to_m5)
from cfdbot.stats import expected_max_z  # noqa: E402


def f2(x: float) -> str:
    return f"{x:.2f}" if np.isfinite(x) else "–"


def polite_get(url: str, waits=(30, 60, 120, 240)) -> bytes | None:
    """相手に負担をかけないよう 1 件ずつ取り、断られたら長めに待ってやり直す。取れなければ None。"""
    for i in range(len(waits) + 1):
        try:
            raw = _get(url, timeout=20, tries=1, use_curl=False)
            time.sleep(0.2)
            return raw
        except NotFound:
            return b""
        except Exception:  # noqa: BLE001
            if i < len(waits):
                time.sleep(waits[i])
    return None


def fetch_month(m, month_start: date, month_end: date, root: Path, workers: int = 1) -> tuple[int, int]:
    """1 か月分を取って 5 分足にして保存する。売値は毎日、買値（スプレッド用）は水曜だけ。戻り値は (本数, 取れなかった日数)。"""
    parts, missing = [], 0
    for day in fetch_days(month_start, month_end, weekend=(m.key == "BTC")):
        raw = polite_get(duka_url(m.duka, day, "BID"))
        if raw is None:
            missing += 1
            continue
        bid = parse_candles(raw, day, m.divisor)
        ask = None
        if day.weekday() == 2 and not bid.empty:
            raw_a = polite_get(duka_url(m.duka, day, "ASK"))
            ask = parse_candles(raw_a, day, m.divisor) if raw_a else None
        df = to_m5(bid, ask)
        if not df.empty:
            parts.append(df)
    if not parts:
        return 0, missing
    df = pd.concat(parts).sort_index()
    path = m5_path(root, m.key, f"{month_start:%Y-%m}")
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, compression="gzip")
    return len(df), missing


def fetch(markets, root: Path, start: date, end: date, workers: int) -> None:
    for m in markets:
        month = date(start.year, start.month, 1)
        total, fails = 0, 0
        while month <= end:
            nxt = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
            last = min(end, nxt - pd.Timedelta(days=1).to_pytimedelta())
            path = m5_path(root, m.key, f"{month:%Y-%m}")
            complete = nxt <= date.today()
            if not (path.exists() and complete):
                try:
                    n, miss = fetch_month(m, month, last, root, workers)
                    total += n
                    if miss:
                        print(f"  ⚠ {m.label} {month:%Y-%m}: {miss} 日分を取れなかった", flush=True)
                    fails = 0
                except Exception as e:  # noqa: BLE001
                    fails += 1
                    print(f"  ✗ {m.label} {month:%Y-%m}: {str(e)[:100]}", flush=True)
                    if fails >= 3:
                        print(f"  – {m.label}: 失敗が続いたので省いた", flush=True)
                        break
            month = nxt
        print(f"  ✓ {m.label}（{m.duka}）: 新しく {total:,} 本", flush=True)


def load_mt5_markets(config: str) -> dict[str, pd.DataFrame]:
    """フィリップの M5（data/ の CSV）→ 中値ではなく売値の OHLC と、スプレッド（価格）。"""
    from cfdbot.train.config import load_train_config
    from cfdbot.train.dataset import load_dataset
    from cfdbot.train.pipeline import _eval_settings

    cfg = load_train_config(config)
    ds = load_dataset(cfg.data)
    inst = _eval_settings(cfg, ds).instrument_map()
    out = {}
    for sym in ds.symbols:
        f = ds.frames.get(sym, {}).get("M5")
        if f is None or "spread" not in f:
            continue
        df = f[["open", "high", "low", "close"]].copy()
        df["spread"] = f["spread"].to_numpy(float) * inst[sym].point_size
        out[sym] = df
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=("dukascopy", "mt5"), default="dukascopy")
    p.add_argument("--data", default="data/intraday", help="Dukascopy から作った 5 分足の置き場")
    p.add_argument("--config", default="config/train.toml", help="--source mt5 のとき")
    p.add_argument("--fetch", action="store_true")
    p.add_argument("--start", default="2020-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--markets", default="", help="カンマ区切り（例 GOLD,WTI）。省略で全部")
    p.add_argument("--split", default="2023-06-01", help="前半と後半の境")
    p.add_argument("--workers", type=int, default=1, help="（未使用。Dukascopy には 1 件ずつ取りに行く）")
    p.add_argument("--out", default="output/intraday")
    args = p.parse_args()

    only = {s.strip().upper() for s in args.markets.split(",") if s.strip()}
    markets = [m for m in MARKETS if not only or m.key in only]
    root = Path(args.data)
    if args.source == "dukascopy":
        if args.fetch:
            end = pd.Timestamp(args.end).date() if args.end else date.today() - pd.Timedelta(days=1).to_pytimedelta()
            print(f"取得中: Dukascopy の 1 分足（{len(markets)} 市場、{args.start}〜{end}。初回は数十分）", flush=True)
            fetch(markets, root, pd.Timestamp(args.start).date(), end, args.workers)
        data = {m.key: load_m5(root, m.key) for m in markets}
        low_mult = 1.0
    else:
        data = load_mt5_markets(args.config)
        low_mult = 0.1
    data = {k: v[v.index >= pd.Timestamp(args.start, tz="UTC")] for k, v in data.items() if len(v)}
    data = {k: v for k, v in data.items() if len(v) > 288 * 250}
    if not data:
        raise SystemExit("5 分足のデータが無い（--fetch で取得するか、--source mt5 で data/ の M5 を使う）")
    labels = {m.key: m.label for m in MARKETS}
    phillip = {m.key: m.phillip_spread for m in MARKETS}
    split = pd.Timestamp(args.split)

    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    span = {k: (v.index[0], v.index[-1]) for k, v in data.items()}
    years = np.mean([(b - a).days / 365.25 for a, b in span.values()])
    rl = rules()
    thr = expected_max_z(len(rl)) / np.sqrt(years)
    say("# 5 分足〜1 時間足の短い売買の検証")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　判定の条件: docs/intraday.md（結果を見る前に決めたもの）")
    say(f"データ: {'Dukascopy の 1 分足（売値・買値）から作った 5 分足' if args.source == 'dukascopy' else 'フィリップの M5'}、"
        + "、".join(f"{labels.get(k, k)} {a:%Y-%m}〜{b:%Y-%m}" for k, (a, b) in span.items()))
    low_txt = "データのスプレッド（取引所に近い）" if args.source == "dukascopy" else "フィリップのスプレッドの 1 割"
    say(f"コストの小さい場合: {low_txt}。足の終値で向きを決め、次の足の値動きを取る（量は市場ごとに年率 15% の値動きにそろえる）")
    say(f"判定「候補」: コストの小さい場合に、全市場をまとめた成績が前半（〜{split - pd.Timedelta(days=1):%Y-%m}）・後半ともプラスで、"
        f"全期間のシャープが {thr:.2f}（{len(rl)} 通り試したときの偶然の一番良い値の目安）を超える")

    rows, cand = [], []
    per_market = {}
    for rule in rl:
        print(f"計算: {rule.label}", flush=True)
        nets, nets_real, gross_daily = {}, {}, {}
        be, tpy = {}, {}
        for k, df0 in data.items():
            df = resample(df0, rule.timeframe)
            pos = positions(rule, df)
            sp = df["spread"].to_numpy(float)
            low = evaluate(df, pos, sp * low_mult)
            nets[k] = low.daily_net
            gross_daily[k] = low.daily_gross
            be[k] = breakeven_bps(low)
            tpy[k] = low.trades_per_year
            real_sp = sp if args.source == "mt5" else (np.maximum(sp, phillip[k]) if phillip.get(k) else None)
            if real_sp is not None:
                nets_real[k] = evaluate(df, pos, real_sp).daily_net
        port = pd.DataFrame(nets).mean(axis=1)
        s1, s2, sf = sharpe(port[port.index < split.tz_localize("UTC")]), sharpe(port[port.index >= split.tz_localize("UTC")]), sharpe(port)
        gross = sharpe(pd.DataFrame(gross_daily).mean(axis=1))
        real = sharpe(pd.DataFrame(nets_real).mean(axis=1)) if nets_real else float("nan")
        ok = bool(np.isfinite(s1) and np.isfinite(s2) and s1 > 0 and s2 > 0 and sf > thr)
        if ok:
            cand.append(rule)
        per_market[rule.key] = {k: (sharpe(nets[k]), sharpe(nets_real[k]) if k in nets_real else float("nan"),
                                    be[k], tpy[k]) for k in nets}
        rows.append(f"| {rule.label} | {f2(gross)} | {f2(s1)} | {f2(s2)} | {f2(sf)} | {f2(real)} | "
                    f"{np.nanmedian(list(tpy.values())):.0f} | {'候補' if ok else '–'} |")

    say("\n## 1. やり方ごと（全市場をまとめた成績。シャープ）")
    say("\n| やり方 | コスト無し | 小さいコスト 前半 | 後半 | 全期間 | 実際のコスト | 1 市場の年間の取引 | 判定 |")
    say("|---|---|---|---|---|---|---|---|")
    for r in rows:
        say(r)
    say("（実際のコスト: フィリップのスプレッド。Dukascopy のデータでは金・銀・WTI・ブレントだけ）")

    say("\n## 2. 市場ごとの損益分岐（往復コスト、価格に対する bp。1 bp = 0.01%）")
    say("この値よりコストが安い売買先なら、コスト込みでもプラス。フィリップの往復コストの目安: "
        + "、".join(f"{labels[k]} {phillip[k] / float(data[k]['close'].iloc[-2000:].median()) * 1e4:.1f}"
                   for k in data if phillip.get(k)))
    keys = list(data)
    say("\n| やり方 | " + " | ".join(labels.get(k, k) for k in keys) + " |")
    say("|---|" + "---|" * len(keys))
    for rule in rl:
        say(f"| {rule.label} | " + " | ".join(f"{per_market[rule.key][k][2]:.1f}" if np.isfinite(per_market[rule.key][k][2])
                                               else "–" for k in keys) + " |")

    say("\n## 3. 判定")
    if not cand:
        say("コストの小さい場合でも、条件を満たすやり方は無い。短い時間足は候補にしない。")
    for rule in cand:
        say(f"- **候補: {rule.label}**")
        for k in keys:
            s_low, s_real, b, t = per_market[rule.key][k]
            say(f"  - {labels.get(k, k)}: 小さいコスト {f2(s_low)}、実際のコスト {f2(s_real)}、損益分岐 {b:.1f} bp、年 {t:.0f} 回")
    say("\n## 読み方")
    say("- ここは「速く多くを比べる」ための簡単な計算（足の終値で入って次の足から損益。損切りの細かい動き・約定の遅れは入れない）。"
        "候補は本番と同じバックテストで確かめ直す")
    say("- 短い時間足は売買の回数が多いので、コストで結果が大きく変わる。損益分岐（2 章）と、実際に使う口座のコストを比べる")

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {out}/report.md")


if __name__ == "__main__":
    main()
