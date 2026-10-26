"""今のスプレッドで計算し直す（docs/costnow.md。既定は銀）。

    ./cfd costnow                    # 銀
    ./cfd costnow --symbol SILVER --fine

① 今の決まり ② その銘柄を今のスプレッドで（見送りなし） ③ その銘柄を外す（見送りなし）を、
期間A（先物の長期データ）と期間B（フィリップ）で比べ、docs/costnow.md 3 章の判定を出す。
結果: output/costnow/<日時>/report.md
"""

from __future__ import annotations

import argparse
import copy
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cfdbot.costnow import RECENT_DAYS, floor_spread, recent_levels, verdict  # noqa: E402
from cfdbot.study import Period, build_setup, run_frames, summarize  # noqa: E402
from cfdbot.train.pipeline import validate_window  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", default="SILVER")
    p.add_argument("--final", default="config/baselines/turtle55_d1_ext.json")
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--context", default="data/context", help="先物の日足の置き場（./cfd context の保存先）")
    p.add_argument("--equity", type=float, default=10_000_000)
    p.add_argument("--long-start", default="2001-01-01")
    p.add_argument("--split", default="2021-06-01")
    p.add_argument("--end")
    p.add_argument("--fine", action="store_true", help="期間B で M5 の約定の再現を使う（遅い）")
    p.add_argument("--out", default="output/costnow")
    args = p.parse_args()
    sym = args.symbol.upper()

    setup = build_setup(args.final, args.config, args.context, args.equity, args.long_start, args.split, args.end,
                        fine=args.fine)
    ds, picks, symbols = setup.ds, setup.picks, setup.symbols
    if sym not in symbols:
        raise SystemExit(f"{args.final} に {sym} が無い（{symbols}）")
    pick = next(pk for pk in picks if pk.symbol == sym)
    others = [pk for pk in picks if pk.symbol != sym]
    pb = setup.period_b
    inst = pb.instruments[sym]
    fine_tf = ds.file_timeframes(sym)[0]
    lv = recent_levels(ds.frames[sym][fine_tf], ds.signal_frame(sym, "D1"), inst.point_size, inst.slippage)

    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s)
        lines.append(s)

    say(f"# 今のスプレッドで計算し直す（{sym}）")
    say()
    say(f"- 実行: {datetime.now():%Y-%m-%d %H:%M}、構成: {args.final}、資金 {args.equity:,.0f} 円（docs/costnow.md）")
    say(f"- 今の水準（書き出しの最後から {RECENT_DAYS} 日、{fine_tf}）: スプレッド {lv['spread']:.4g}、値段 {lv['price']:.4g}"
        f"（割合 {lv['rel']:.3%}）、日足の ATR(20) {lv['atr']:.4g}")
    say(f"- 銘柄仕様のスプレッド {inst.spread:.4g}（見送りの線はその 3 倍 = {inst.spread * pb.bt.filters.max_spread_mult:.4g}）"
        f"、スリッページ {inst.slippage:.4g}")
    say(f"- 今のコスト比率（往復コスト ÷ 損切り幅 2.5ATR）: **{lv['cost_ratio']:.1%}**（10% 超は不向き）")

    nofilter = replace(pb.bt, filters=replace(pb.bt.filters, max_spread_mult=float("inf")))
    rows: dict[str, dict[str, dict]] = {}

    # ---------------- 期間B: フィリップ
    def run_b(ds_, pk, bt):
        r = validate_window(ds_, pb.instruments, pk, bt, pb.start, pb.end, use_fine=args.fine,
                            signal_warm=pd.Timedelta(days=365 * 100))
        return {"equity": r["equity"], "trades": r["trades"], "signals": r["signals"]}

    ds_now = copy.copy(ds)
    ds_now.frames = {**ds.frames, sym: {tf: floor_spread(df, lv["rel"], inst.point_size)
                                        for tf, df in ds.frames[sym].items()}}
    ds_now._derived = {}
    rows[pb.name] = {
        "①": summarize(run_b(ds, picks, pb.bt), args.equity, symbols),
        "②": summarize(run_b(ds_now, picks, nofilter), args.equity, symbols),
        "③": summarize(run_b(ds, others, nofilter), args.equity, symbols),
    }
    labels = {pb.name: pb.label}

    # ---------------- 期間A: 先物の長期データ（コストは値段に比例。今の割合に置き換える）
    pa = setup.period_a
    if pa is not None and sym in pa.frames:
        ref = float(pb.frames[sym]["close"].iloc[-250:].median())
        rel_base = (inst.spread + 2 * inst.slippage) / ref
        rel_now = lv["rel"] + 2 * inst.slippage / ref
        frames_now = {**pa.frames, sym: pa.frames[sym].assign(spread=pa.frames[sym]["spread"] * rel_now / rel_base)}
        pa_now = Period(pa.name, pa.label, frames_now, pa.tfs, pa.instruments, pa.bt, pa.start, pa.end, pa.resolve,
                        run_frames)
        rows[pa.name] = {
            "①": summarize(pa.run(picks), args.equity, symbols),
            "②": summarize(pa_now.run(picks), args.equity, symbols),
            "③": summarize(pa.run(others), args.equity, symbols),
        }
        labels[pa.name] = pa.label
        say(f"- 期間A の銀のコスト（値段に対する割合）: 今の決まり {rel_base:.3%} → 今のスプレッド {rel_now:.3%}")
    else:
        say("- ⚠ 期間A（先物の長期データ）が無い。./cfd context で取ってから実行し直す")

    names = {"①": "今の決まり", "②": f"{sym} を今のスプレッドで", "③": f"{sym} を外す"}
    for key in sorted(rows):
        say()
        say(f"## {labels[key]}")
        say()
        say(f"| | シャープ | 年率 | 最大DD | 取引 | {sym} の取引 | {sym} の平均 R |")
        say("|---|---|---|---|---|---|---|")
        for k, s in rows[key].items():
            n, r = s["by_symbol"].get(sym, (0, float("nan")))
            say(f"| {k} {names[k]} | {s['sharpe']:.2f} | {s['cagr']:.1%} | {s['mdd']:.1%} | {s['trades']} | "
                f"{n} | {'—' if pd.isna(r) else f'{r:+.2f}'} |")

    result = verdict({k: v["②"]["sharpe"] for k, v in rows.items()}, {k: v["③"]["sharpe"] for k, v in rows.items()},
                     lv["cost_ratio"])
    say()
    say("## 判定（docs/costnow.md 3 章）")
    say()
    say(f"**{result}**")
    say()
    say("- 「入れる」でも設定はまだ変えていない。変えるのは利用者が決めてから、週末に（銘柄仕様の銀のスプレッドを今の水準にして"
        "プリセットを作り直す）")

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {out}/report.md")


if __name__ == "__main__":
    main()
