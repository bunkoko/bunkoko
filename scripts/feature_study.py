"""外部データ（金利・ドル・株価・他の商品・投機筋の建玉）で売買を絞り込むと成績が良くなるかを確かめる。

    python scripts/feature_study.py                      # 基準の構成（タートル 55/20 日足）で全部
    python scripts/feature_study.py --only usd,partner   # 仮説を絞る
    python scripts/feature_study.py --no-ml              # 全部の入力を使う予測を省く

2 つの期間で同じことを確かめ、両方で良くなったものだけを「有望」とする:
  期間A: 先物の長期データ（Yahoo）。フィリップのデータが始まる前（2001〜2021-05）
  期間B: フィリップの MT5 のデータ（2021-06〜）。期間A とは重ならないので、期間A で見つけたものの確認になる

先に scripts/fetch_context.py（./cfd context）で外部データを取得しておく。
資金は最小単位の影響を避けるため既定 1,000 万円で比べる（率で見るので結論は資金によらない）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot.backtest import Sleeve, run_backtest  # noqa: E402
from cfdbot.context import FUTURES_FOR, ContextStore, from_bars, yahoo_frame  # noqa: E402
from cfdbot.features import (HYPOTHESES, PARTNER, describe_inputs, feature_frame, hypothesis_gate,  # noqa: E402
                             ml_gates, trade_rows)
from cfdbot.metrics import compute_metrics  # noqa: E402
from cfdbot.strategies import make_strategy  # noqa: E402
from cfdbot.train.config import TIMEFRAMES, load_train_config  # noqa: E402
from cfdbot.train.dataset import load_dataset  # noqa: E402
from cfdbot.train.pipeline import _account_config, _eval_settings, validate_window  # noqa: E402
from cfdbot.train.portfolio import Pick  # noqa: E402


def _clean(v: str) -> str:
    return v.strip().strip("「」『』\"'`、。　 ")


class Period:
    """1 つの期間のデータと、そこでのバックテスト。"""

    def __init__(self, name, label, frames, tfs, instruments, bt, start, end, resolve, runner):
        self.name, self.label = name, label
        self.frames, self.tfs = frames, tfs
        self.instruments, self.bt = instruments, bt
        self.start, self.end = start, end
        self.resolve = resolve
        self._runner = runner

    def run(self, picks, gates=None, start=None):
        return self._runner(self, picks, gates or {}, start or self.start)


def run_frames(period: Period, picks, gates, start) -> dict:
    sleeves = [Sleeve(p.symbol, make_strategy(p.strategy, **p.params), dict(p.exit), risk_weight=p.multiplier,
                      entry_gate=gates.get(p.symbol)) for p in picks if p.symbol in period.frames]
    cfg = replace(period.bt, trade_start=start, trade_end=period.end,
                  risk=replace(period.bt.risk, max_drawdown_halt=1.0))
    res = run_backtest({s.symbol: period.frames[s.symbol] for s in sleeves}, period.instruments, sleeves, cfg)
    return {"equity": res.equity, "trades": res.trades, "signals": res.signals}


def summarize(res: dict, equity0: float, symbols) -> dict:
    m = compute_metrics(res["equity"], res["trades"], equity0)
    t = res["trades"]
    out = {"sharpe": m.get("sharpe", 0.0), "cagr": m.get("cagr", 0.0), "mdd": m.get("max_drawdown", 0.0),
           "trades": int(m.get("trades", 0)), "by_symbol": {}}
    for s in symbols:
        g = t[t["symbol"] == s] if not t.empty else t
        out["by_symbol"][s] = (len(g), float(g["r_multiple"].mean()) if len(g) else np.nan)
    eq = res["equity"]
    out["yearly"] = (eq.resample("YE").last() / eq.resample("YE").first() - 1) if len(eq) else pd.Series(dtype=float)
    return out


def verdict(pct_a: float | None, pct_b: float | None, sym_a: float | None, sym_b: float | None) -> str:
    """pct: 時期をずらした同じ絞り込み（偶然）より良かった割合。"""
    if pct_a is None or pct_b is None:
        return "データ不足"
    if pct_a >= 0.95 and pct_b >= 0.95 and (sym_a or 0) >= 0.5 and (sym_b or 0) >= 0.5:
        return "有望"
    if pct_a <= 0.05 and pct_b <= 0.05:
        return "逆効果"
    if pct_a >= 0.95 or pct_b >= 0.95:
        return "片方の期間だけ"
    return "偶然の範囲"


def shifted(gates: dict[str, pd.DataFrame], frac: float) -> dict[str, pd.DataFrame]:
    """絞り込みの並びを時間方向にずらす（買い・売りを止める割合と続き方はそのまま、時期だけが合わなくなる）。"""
    out = {}
    for sym, g in gates.items():
        k = int(len(g) * frac)
        out[sym] = pd.DataFrame({"long": np.roll(g["long"].to_numpy(bool), k),
                                 "short": np.roll(g["short"].to_numpy(bool), k)}, index=g.index)
    return out


def improved_share(base: dict, test: dict, symbols) -> float | None:
    hits = []
    for s in symbols:
        n0, r0 = base["by_symbol"].get(s, (0, np.nan))
        n1, r1 = test["by_symbol"].get(s, (0, np.nan))
        if n0 >= 5 and n1 >= 5 and np.isfinite(r0) and np.isfinite(r1):
            hits.append(r1 > r0)
    return float(np.mean(hits)) if hits else None


def years_better(base: dict, test: dict) -> str:
    a, b = base["yearly"], test["yearly"]
    common = a.index.intersection(b.index)
    if not len(common):
        return "–"
    return f"{int((b[common] > a[common] + 1e-9).sum())}/{len(common)}"


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 30:
        return np.nan
    rx = pd.Series(x[ok]).rank().to_numpy()
    ry = pd.Series(y[ok]).rank().to_numpy()
    return float(np.corrcoef(rx, ry)[0, 1])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--final", default="config/baselines/turtle55_d1.json", help="構成（基準はタートル 55/20 日足）")
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--context", default="data/context", help="外部データの置き場（./cfd context の保存先）")
    p.add_argument("--equity", type=float, default=10_000_000, help="比べるときの資金（円）")
    p.add_argument("--long-start", default="2001-01-01", help="期間A の開始日")
    p.add_argument("--split", default="2021-06-01", help="期間A と期間B の境（期間B の開始日）")
    p.add_argument("--end", help="期間B の終了日（省略時はデータの最後まで）")
    p.add_argument("--only", default="", help="調べる仮説（カンマ区切り。例: usd,partner）")
    p.add_argument("--no-ml", action="store_true", help="全部の入力を使う予測を省く")
    p.add_argument("--ml-alpha", type=float, default=0.25, help="予測の控えめさ（大きいほど係数を小さく）")
    p.add_argument("--placebo", type=int, default=30, help="偶然との比較で時期をずらす回数（0 で省く）")
    p.add_argument("--fine", action="store_true", help="期間B で M5 の約定の再現を使う（遅い。結論はほぼ同じ）")
    p.add_argument("--out", default="output/study")
    args = p.parse_args()

    cfg = load_train_config(args.config)
    final = json.loads(Path(args.final).read_text(encoding="utf-8"))
    picks = [Pick(s["symbol"], s["strategy"], s["params"], s.get("exit", {}), 0, 0.0, 0.0, 0,
                  float(s.get("multiplier", 1.0)), s["timeframe"], s.get("htf") or {}) for s in final["sleeves"]]
    if any(pk.htf for pk in picks):
        raise SystemExit("上位足フィルタ付きの構成は未対応（基準の構成で調べる）")
    symbols = [pk.symbol for pk in picks]
    tfs = {pk.symbol: pd.Timedelta(TIMEFRAMES[pk.timeframe]) for pk in picks}

    store = ContextStore(args.context)
    if not store.available():
        raise SystemExit(f"{args.context} に外部データが無い。先に ./cfd context（scripts/fetch_context.py）を実行する")

    ds = load_dataset(cfg.data)
    settings = _eval_settings(cfg, ds)
    fx = ds.fx if ds.fx is not None else cfg.data.fx
    bt = _account_config(cfg, fx, settings.event_list())
    bt = replace(bt, initial_equity=args.equity)
    instruments = settings.instrument_map()

    # ---------------- 期間B: フィリップのデータ
    split = pd.Timestamp(_clean(args.split), tz="UTC")
    frames_b = {pk.symbol: ds.signal_frame(pk.symbol, pk.timeframe) for pk in picks}
    last = max(f.index[-1] for f in frames_b.values())
    end_b = (pd.Timestamp(_clean(args.end), tz="UTC") if args.end else last) + pd.Timedelta(days=1)

    def resolve_b(key: str, sym: str):
        if key == "partner":
            other = PARTNER.get(sym)
            f = frames_b.get(other) if other else None
            if f is None and other:
                f = ds.signal_frame(other, "D1")
            return from_bars(other, f, pd.Timedelta("1D")) if f is not None else None
        return store.get(key)

    def run_b(period, pk, gates, start):
        r = validate_window(ds, period.instruments, pk, period.bt, start, period.end, gates, use_fine=args.fine)
        return {"equity": r["equity"], "trades": r["trades"], "signals": r["signals"]}

    period_b = Period("B", f"期間B フィリップ {split:%Y-%m}〜{end_b - pd.Timedelta(days=1):%Y-%m}", frames_b, tfs,
                      instruments, bt, split, end_b, resolve_b, run_b)

    # ---------------- 期間A: 先物の長期データ（コストは今のスプレッドを価格に比例させる）
    frames_a = {}
    for pk in picks:
        f = yahoo_frame(store, FUTURES_FOR[pk.symbol]) if pk.symbol in FUTURES_FOR else None
        if f is None or f.empty:
            continue
        inst = instruments[pk.symbol]
        ref = float(frames_b[pk.symbol]["close"].iloc[-250:].median())
        rel = (inst.spread + 2 * inst.slippage) / ref
        f = f[f.index < split].copy()
        f["spread"] = rel * f["close"] / inst.point_size
        frames_a[pk.symbol] = f
    inst_a = {k: replace(v, spread=0.0, slippage=0.0) for k, v in instruments.items()}
    bt_a = replace(bt, fx_rate=float(cfg.data.fx),
                   filters=replace(bt.filters, max_spread_mult=float("inf"), extra_events=()))

    def resolve_a(key: str, sym: str):
        if key == "partner":
            other = PARTNER.get(sym)
            f = frames_a.get(other) if other else None
            if f is None and other in FUTURES_FOR:
                f = yahoo_frame(store, FUTURES_FOR[other])
            return from_bars(other, f, pd.Timedelta("1D")) if f is not None and not f.empty else None
        return store.get(key)

    long_start = pd.Timestamp(_clean(args.long_start), tz="UTC")
    period_a = Period("A", f"期間A 先物の長期データ {long_start:%Y-%m}〜{split - pd.Timedelta(days=1):%Y-%m}",
                      frames_a, tfs, inst_a, bt_a, long_start, split, resolve_a, run_frames)
    periods = [period_a, period_b] if frames_a else [period_b]
    if not frames_a:
        print("⚠ 先物の長期データが無い（Yahoo の取得に失敗？）。期間B だけで比べる（結論は出しにくい）")

    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    say(f"# 外部データでの絞り込みの検証（{Path(args.final).name}、資金 {args.equity:,.0f} 円）")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}")
    for per in periods:
        say(f"- {per.label}（{', '.join(f'{s} {per.frames[s].index[0]:%Y-%m}〜' for s in per.frames)}）")
    say("\n使える外部データ:")
    for s in describe_inputs(resolve_b, symbols):
        say(f"  - {s}")

    # ---------------- 基準
    base = {}
    for per in periods:
        print(f"計算: 基準 / {per.label}", flush=True)
        base[per.name] = summarize(per.run(picks), args.equity, symbols)

    # ---------------- 仮説ごと（＋時期をずらした同じ絞り込みで「偶然でもこのくらいは出る」を測る）
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    rng = np.random.default_rng(20261004)
    fracs = rng.uniform(0.15, 0.85, args.placebo)
    results = []
    for h in HYPOTHESES:
        if only and h.name not in only:
            continue
        row = {"name": h.name, "label": h.label, "targets": [s for s in symbols if s in h.inputs]}
        for per in periods:
            gates = {}
            for s in row["targets"]:
                f = per.frames.get(s)
                g = hypothesis_gate(h, s, f.index, per.tfs[s], per.resolve) if f is not None else None
                if g is not None:
                    gates[s] = g
            in_period = [g[(g.index >= per.start) & (g.index < per.end)] for g in gates.values()]
            if not any((~w["long"]).any() or (~w["short"]).any() for w in in_period):
                row[per.name] = None    # この期間には外部データが無い（何も止めない）
                continue
            print(f"計算: {h.name} / {per.label}" + (f"（偶然との比較 {len(fracs)} 回を含む）" if len(fracs) else ""),
                  flush=True)
            row[per.name] = summarize(per.run(picks, gates), args.equity, symbols)
            row[per.name]["gated"] = sorted(gates)
            placebo = [summarize(per.run(picks, shifted(gates, fr)), args.equity, symbols)["sharpe"] for fr in fracs]
            d = row[per.name]["sharpe"] - base[per.name]["sharpe"]
            row[per.name]["placebo"] = np.array(placebo) - base[per.name]["sharpe"]
            row[per.name]["pct"] = float(np.mean(row[per.name]["placebo"] < d)) if len(placebo) else None
        results.append(row)

    # ---------------- 表
    def cell(per_name: str, r: dict | None) -> str:
        if r is None:
            return "データ無し"
        b = base[per_name]
        txt = f"{r['sharpe']:.2f}（{r['sharpe'] - b['sharpe']:+.2f}）取引{r['trades']}"
        if r.get("pct") is not None:
            txt += f" / 偶然より良い {r['pct']:.0%}"
        return txt

    say("\n## 1. 仮説ごとの結果（シャープレシオ。括弧は基準との差）")
    if len(fracs):
        say(f"偶然より良い: 同じ絞り込みを時期だけずらして {len(fracs)} 回試した結果（偶然）より、差が大きかった割合。")
        say("判定: 両方の期間で 95% 以上、かつ対象銘柄の半分以上で 1 回あたりの R も良くなったら「有望」")
    say("")
    hdr = ["仮説", "対象"] + [per.label.split(" ")[0] for per in periods] + (["期間A で良くなった年"] if frames_a else []) + ["判定"]
    say("| " + " | ".join(hdr) + " |")
    say("|" + "---|" * len(hdr))
    base_row = ["基準（絞り込みなし）", "全銘柄"] + [f"{base[per.name]['sharpe']:.2f} 取引{base[per.name]['trades']}"
                                             for per in periods] + (["–"] if frames_a else []) + ["–"]
    say("| " + " | ".join(base_row) + " |")
    table = []
    noise = {per.name: [] for per in periods}
    for row in results:
        d = {per.name: (row[per.name]["sharpe"] - base[per.name]["sharpe"]) if row.get(per.name) else None
             for per in periods}
        pct = {per.name: row[per.name].get("pct") if row.get(per.name) else None for per in periods}
        sh = {per.name: improved_share(base[per.name], row[per.name], row["targets"]) if row.get(per.name) else None
              for per in periods}
        for per in periods:
            if row.get(per.name) is not None and len(row[per.name]["placebo"]):
                noise[per.name].extend(row[per.name]["placebo"])
        if not len(fracs):
            v = "（偶然との比較なし）"
        elif frames_a:
            v = verdict(pct.get("A"), pct.get("B"), sh.get("A"), sh.get("B"))
        else:
            v = "期間B のみ（結論は出さない）"
        cells = [row["label"], "・".join(row["targets"])] + [cell(per.name, row.get(per.name)) for per in periods]
        if frames_a:
            cells.append(years_better(base["A"], row["A"]) if row.get("A") else "–")
        cells.append(v)
        say("| " + " | ".join(cells) + " |")
        table.append({"name": row["name"], "verdict": v, **{f"d_sharpe_{k}": x for k, x in d.items()},
                      **{f"pct_{k}": x for k, x in pct.items()}})
    for per in periods:
        if noise[per.name]:
            lo, hi = np.percentile(noise[per.name], [5, 95])
            say(f"\n{per.label.split(' ')[0]} で偶然でも出るシャープの差（時期をずらした絞り込みの 90% の範囲）: "
                f"{lo:+.2f} 〜 {hi:+.2f}")

    say("\n## 2. 銘柄ごとの 1 回あたりの R（取引数）")
    for per in periods:
        say(f"\n{per.label}")
        say("| 仮説 | " + " | ".join(symbols) + " |")
        say("|---|" + "---|" * len(symbols))
        say("| 基準 | " + " | ".join(f"{base[per.name]['by_symbol'][s][1]:+.2f}（{base[per.name]['by_symbol'][s][0]}）"
                                    for s in symbols) + " |")
        for row in results:
            r = row.get(per.name)
            if r is None:
                continue
            cells = []
            for s in symbols:
                n, ar = r["by_symbol"][s]
                cells.append(f"{ar:+.2f}（{n}）" if s in r.get("gated", []) else "–")
            say(f"| {row['name']} | " + " | ".join(cells) + " |")

    # ---------------- 全部の入力を使う予測
    if not args.no_ml:
        say("\n## 3. 全部の入力をまとめて使う予測（リッジ回帰。1 年ごとに、それより前の取引だけで学習）")
        feats = {per.name: {s: feature_frame(s, per.frames[s], per.tfs[s], per.resolve) for s in per.frames}
                 for per in periods}
        base_runs = {per.name: per.run(picks) for per in periods}
        pools = {per.name: trade_rows(base_runs[per.name]["trades"], feats[per.name]) for per in periods}

        # 各入力と取引の結果（R）の関係（順位相関。買いなら入力の変化そのまま、売りなら符号を反転）
        say("\n### 入力ごとの、取引の結果との相関（両方の期間で同じ向きのものが上）")
        cols = sorted(set().union(*[set(pools[k][0].columns) for k in pools if len(pools[k][0])]))
        corr_rows = []
        for c in cols:
            cs = {k: spearman(pools[k][0][c].to_numpy(float) if c in pools[k][0] else np.full(len(pools[k][1]), np.nan),
                              pools[k][1]) for k in pools}
            corr_rows.append((c, cs))
        def key_fn(item):
            vals = [v for v in item[1].values() if np.isfinite(v)]
            same = len(vals) == len(item[1]) and (all(v > 0 for v in vals) or all(v < 0 for v in vals))
            return (not same, -min(abs(v) for v in vals) if vals else 0)
        say("| 入力 | " + " | ".join(per.label.split(" ")[0] for per in periods) + " |")
        say("|---|" + "---|" * len(periods))
        for c, cs in sorted(corr_rows, key=key_fn):
            say(f"| {c} | " + " | ".join(f"{cs[per.name]:+.3f}" if np.isfinite(cs[per.name]) else "–"
                                       for per in periods) + " |")
        say("（相関 0.05 未満はほぼ無関係。取引数が数百なので ±0.1 程度は偶然でも出る）")

        ml_rows = []
        if "A" in pools and len(pools["A"][1]):
            xa, ya, ea = pools["A"]
            gates_a, models_a = ml_gates(feats["A"], xa, ya, ea, period_a.start, period_a.end,
                                         alpha_per_row=args.ml_alpha)
            first = next((y for y, m in models_a if m is not None), None)
            if first is not None:
                s0 = max(pd.Timestamp(f"{first}-01-01", tz="UTC"), period_a.start)
                print("計算: 予測 / 期間A", flush=True)
                b0 = summarize(period_a.run(picks, start=s0), args.equity, symbols)
                r0 = summarize(period_a.run(picks, gates_a, start=s0), args.equity, symbols)
                pl = [summarize(period_a.run(picks, shifted(gates_a, fr), start=s0), args.equity, symbols)["sharpe"]
                      for fr in fracs]
                ml_rows.append((f"期間A（{first}〜）", b0, r0, pl))
        if "B" in pools:
            parts = [pools[k] for k in ("A", "B") if k in pools and len(pools[k][1])]
            x = pd.concat([q[0] for q in parts], ignore_index=True)
            y = np.concatenate([q[1] for q in parts])
            e = np.concatenate([q[2] for q in parts])
            gates_b, models_b = ml_gates(feats["B"], x, y, e, period_b.start, period_b.end,
                                         min_trades=100, alpha_per_row=args.ml_alpha)
            if any(m is not None for _, m in models_b):
                print("計算: 予測 / 期間B", flush=True)
                r1 = summarize(period_b.run(picks, gates_b), args.equity, symbols)
                pl = [summarize(period_b.run(picks, shifted(gates_b, fr)), args.equity, symbols)["sharpe"] for fr in fracs]
                ml_rows.append(("期間B", base["B"], r1, pl))
            last_model = next((m for _, m in reversed(models_b) if m is not None), None)
        else:
            last_model = None
        if ml_rows:
            say("\n| 期間 | 基準 シャープ | 予測で絞り込み シャープ | 偶然より良い | 基準 年率 | 絞り込み 年率 | 基準 最大DD | "
                "絞り込み 最大DD | 取引 |")
            say("|---|---|---|---|---|---|---|---|---|")
            for name, b0, r0, pl in ml_rows:
                pct = f"{np.mean(np.array(pl) < r0['sharpe']):.0%}" if pl else "–"
                say(f"| {name} | {b0['sharpe']:.2f} | {r0['sharpe']:.2f}（{r0['sharpe'] - b0['sharpe']:+.2f}） | {pct} | "
                    f"{b0['cagr']:.1%} | {r0['cagr']:.1%} | {b0['mdd']:.1%} | {r0['mdd']:.1%} | "
                    f"{b0['trades']}→{r0['trades']} |")
        if last_model is not None:
            say(f"\n最新の予測モデル（{last_model.n} 取引で学習）の係数の大きい入力:")
            for k, v in list(last_model.importance.items())[:12]:
                say(f"  {k}: {v:+.3f}")
        else:
            say("（学習に使える取引が少なく、予測を作れなかった）")

    say("\n## 読み方")
    say("- 仮説は 13 個あるので、偶然 1 つくらいは良く見える。そこで、同じ絞り込みを時期だけずらしたもの（偶然）と比べ、"
        "両方の期間で偶然をはっきり上回ったものだけを候補にする")
    say("- 全部の入力を使う予測は、入力が多いほど過去にだけ合ってしまいやすい。期間B（学習に使っていない年）で"
        "良くなったかだけを見る")
    say("- 「有望」が出ても、すぐには本番に入れない。EA で同じデータを使えるか（MT5 の銘柄か、"
        "毎日外から取り込むか）を決めてから、デモで確かめる")
    say("- 期間A は先物（限月の乗り換えで値が飛ぶ）、判断は米東部 18 時、外部データは 1 日遅れで使うなど、"
        "期間B（本番と同じ条件）より不利に作ってある")

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    pd.DataFrame(table).to_csv(out / "hypotheses.csv", index=False)
    print(f"\n保存先: {out}/report.md（この内容をそのまま送ってください）")


if __name__ == "__main__":
    main()
