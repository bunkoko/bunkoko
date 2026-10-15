"""相方の行き過ぎフィルタを、使っていない商品の組で確かめる（./cfd partner）。仮説と判定の条件は docs/cross_asset.md 7 章。

    python scripts/partner_study.py               # data/universe の日足（無い市場は Yahoo から取る。Mac で。数十分）
    python scripts/partner_study.py --placebo 20  # 試しに速く（判定には 200 回を使う）

基準 = 今の構成（turtle55_d1_ext.json: タートル 55/20 日足＋自分の行き過ぎ）。候補 = 基準＋相方の行き過ぎ（相方が直近 20 日に
取引の向きにふだんの 1.5 倍を超えて動いていたら入らない）。判定は使っていない 5 組だけ。金↔銀・WTI↔ブレントは参考。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot import partner as P  # noqa: E402
from cfdbot.stats import psr_of  # noqa: E402
from cfdbot.study import clean, load_picks  # noqa: E402
from cfdbot.train.config import load_train_config  # noqa: E402
from cfdbot.train.pipeline import _account_config  # noqa: E402
from cfdbot.universe import sharpe  # noqa: E402


def f2(x: float) -> str:
    return f"{x:+.2f}" if x is not None and np.isfinite(x) else "–"


def pct(x: float | None, digits: int = 0) -> str:
    return f"{x:.{digits}%}" if x is not None and np.isfinite(x) else "–"


def base_bt(cfg, equity: float):
    bt = _account_config(cfg, float(cfg.data.fx), [])
    return replace(bt, initial_equity=equity, risk=replace(bt.risk, max_drawdown_halt=1.0),
                   filters=replace(bt.filters, max_spread_mult=float("inf"), extra_events=()))


def halves(r: pd.Series) -> tuple[float, float]:
    r = r.dropna()
    if len(r) < 120:
        return float("nan"), float("nan")
    mid = len(r) // 2
    return sharpe(r.iloc[:mid]), sharpe(r.iloc[mid:])


def study(group: P.Group, pk0, n_placebo: int, clean: bool = True, base: dict | None = None, log=print) -> dict:
    """基準・候補・偶然（時期をずらした同じ絞り込み）を計算する。clean=True が主の判定（相方の飛びの日を除く）。"""
    if base is None:
        log(f"計算: {group.label}（基準）")
        base = P.run(group, pk0)
    gates = P.partner_gates(group.frames, group.pmap, clean=clean)
    log(f"計算: {group.label}（相方の行き過ぎを足す）")
    cand = P.run(group, pk0, gates)
    diff = cand["sharpe"] - base["sharpe"]
    null = P.placebo_diffs(group, pk0, gates, base["sharpe"], n_placebo, log=log) if n_placebo else np.array([])
    d = (cand["returns"] - base["returns"]).dropna()
    stopped = {s: float(((~g["long"]) | (~g["short"])).mean()) for s, g in gates.items()}
    return {"base": base, "cand": cand, "diff": diff, "pct": float(np.mean(null < diff)) if len(null) else None,
            "share": P.improved_share(base, cand), "psr": psr_of(d.to_numpy()) if len(d) > 60 else float("nan"), "stopped": stopped,
            "null": null}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--final", default="config/baselines/turtle55_d1_ext.json", help="基準の構成（今の EA と同じ）")
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--data", default="data/universe", help="先物の日足の置き場（./cfd universe と同じ）")
    p.add_argument("--since", default="2000-01-01")
    p.add_argument("--start", default="2001-01-01", help="判定の期間の始まり")
    p.add_argument("--split", default="2021-06-01", help="参考: 期間A（金↔銀・WTI↔ブレント）の終わり")
    p.add_argument("--equity", type=float, default=10_000_000)
    p.add_argument("--placebo", type=int, default=P.PLACEBO, help="時期をずらした偶然の回数（判定は 200）")
    p.add_argument("--no-fetch", action="store_true", help="無い市場を Yahoo から取らない")
    p.add_argument("--prior-trials", type=int, default=P.PRIOR_TRIALS, help="これまでに試した数（docs/review.md。計算した時点の数）")
    p.add_argument("--out", default="output/partner")
    args = p.parse_args()

    cfg = load_train_config(args.config)
    pk0 = load_picks(args.final)[0]
    bt = base_bt(cfg, args.equity)
    start = pd.Timestamp(clean(args.start), tz="UTC")
    split = pd.Timestamp(clean(args.split), tz="UTC")
    root = Path(args.data)
    judged = P.load_group("D", f"判定: 使っていない組 {start:%Y-%m}〜", P.PAIRS, root, bt, start, None, args.since, fetch=not args.no_fetch)
    n_pairs = len(judged.pmap) // 2
    if n_pairs == 0:
        raise SystemExit(f"判定に使える組が無い（外した組: {', '.join(judged.dropped) or 'なし'}）。Mac で ./cfd partner（Yahoo から取る）")
    ref = P.load_group("A", f"参考: 期間A 金↔銀・WTI↔ブレント {start:%Y-%m}〜{split - pd.Timedelta(days=1):%Y-%m}", P.CORE_PAIRS, root, bt,
                       start, split, args.since, fetch=not args.no_fetch)

    res = study(judged, pk0, args.placebo)
    raw = study(judged, pk0, min(args.placebo, 30), clean=False, base=res["base"])       # 参考: 飛びを除かない（6 章と同じ作り方）
    res_ref = study(ref, pk0, min(args.placebo, 30)) if len(ref.pmap) == 4 else None
    jumps = P.jump_share(judged.frames)
    v = P.verdict(res["diff"], res["pct"], res["share"], n_pairs)
    if args.placebo < P.PLACEBO and v.startswith("採用"):
        v = f"途中（偶然の回数が {args.placebo} 回。判定は {P.PLACEBO} 回で）"

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    say("# 相方の行き過ぎフィルタを、使っていない商品の組で確かめる")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　仮説と判定の条件: docs/cross_asset.md 7 章（結果を見る前に決めたもの）")
    say(f"基準: {Path(args.final).name}（今の EA と同じ。自分の行き過ぎ入り）。資金 {args.equity:,.0f} 円・1 回の損失 {bt.risk.risk_per_trade:.1%}。"
        f"コスト: 往復 0.08%・建玉に年 3%（docs/universe.md の他の商品と同じ）。試した数: {args.prior_trials} ＋ 1 ＝ {args.prior_trials + 1}")
    say("\n## 1. 判定（使っていない組だけ）\n")
    say("| 組 | 市場 | 基準のシャープ | 足したシャープ | 差 | 偶然より良い割合 | 1 回あたりの R が良くなった市場 | 判定 |")
    say("|---|---|---|---|---|---|---|---|")
    pairs = sorted({tuple(sorted((a, b))) for a, b in judged.pmap.items()})
    say(f"| 判定の {len(pairs)} 組 | {len(judged.frames)} | {res['base']['sharpe']:.2f} | {res['cand']['sharpe']:.2f} | {f2(res['diff'])} | "
        f"{pct(res['pct'])}（{len(res['null'])} 回） | {pct(res['share'])} | **{v}** |")
    say(f"| 参考: 飛びを除かない（6 章と同じ作り方） | {len(judged.frames)} | {raw['base']['sharpe']:.2f} | {raw['cand']['sharpe']:.2f} | "
        f"{f2(raw['diff'])} | {pct(raw['pct'])}（{len(raw['null'])} 回） | {pct(raw['share'])} | 判定しない |")
    if judged.dropped:
        say(f"\n- データが足りず外した組: {', '.join(judged.dropped)}")
    say("\n判定の条件（7.3）: (1) 差がプラス、(2) 偶然（絞り込みの時期だけをずらした 200 回）より良い割合 95% 以上、"
        f"(3) 1 回あたりの R が良くなった市場が半分以上。3 つとも満たせば採用候補。判定に残る組が {P.MIN_PAIRS} 組未満なら判定しない。"
        "主の判定は、相方の 1 日の変化がそれまでの 252 日の標準偏差の 4 倍を超えた日（つなぎ足の乗り換えの飛びとみなす）を 0 にした変化で作る。"
        "**逆の結果（相方が行き過ぎていた方が良い）が出ても、向きを反転して「効く」としない**")

    say("\n## 2. 市場ごと（判定の組）\n")
    say("| 市場 | 相方 | 止めた足の割合 | 飛びとした日の割合 | 基準 取引・1 回の R | 足した 取引・1 回の R |")
    say("|---|---|---|---|---|---|")
    for s in judged.frames:
        n0, r0 = res["base"]["by_symbol"][s]
        n1, r1 = res["cand"]["by_symbol"][s]
        say(f"| {P.MARKETS[s].label} | {P.MARKETS[judged.pmap[s]].label} | {pct(res['stopped'].get(s), 1)} | {pct(jumps.get(s), 2)} | "
            f"{n0}・{f2(r0)} | {n1}・{f2(r1)} |")
    h0, h1 = halves(res["base"]["returns"]), halves(res["cand"]["returns"])
    say(f"\n- 参考（判定しない）: 前半・後半のシャープ 基準 {h0[0]:.2f}・{h0[1]:.2f}、足した {h1[0]:.2f}・{h1[1]:.2f}。"
        f"日ごとの差の PSR {pct(res['psr'])}。取引 {res['base']['trades']} → {res['cand']['trades']}")

    say("\n## 3. 参考: 見つけた期間（金↔銀・WTI↔ブレント）での、今の EA への増分\n")
    if res_ref is None:
        say("- 期間A の日足が足りない（data/universe/ の金・銀・WTI・ブレント）")
    else:
        say(f"- 期間A（Yahoo の先物）: 基準 {res_ref['base']['sharpe']:.2f} → 足した {res_ref['cand']['sharpe']:.2f}（差 {f2(res_ref['diff'])}、"
            f"偶然より良い割合 {pct(res_ref['pct'])}・{len(res_ref['null'])} 回）。cross_asset.md 6 章の +0.18 は「行き過ぎフィルタの無い基準」との差で、"
            "これは今の EA（自分の行き過ぎ入り）との差。判定には使わない")
    say("- 期間B（フィリップ）での増分は、採用候補になったときに EA の設計と合わせて別に確かめる")

    say("\n## 4. 読み方・限界\n")
    say("- 文献の向きは逆（関連する市場の動きは、自分が同じ向きに続く合図という研究が多い）。事前の見込みは低いが、判定には使わない。"
        "読み方は 1 章の 3 条件だけ（docs/cross_asset.md 7.1・7.3）")
    say("- Yahoo の先物はつなぎ足（限月の乗り換えの飛びが損益に入る）。コストは目安")
    say("- 相方の値は、自分の足の終わりの時点で終わっている相方の足まで（先読みしない）")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    pd.DataFrame({"base": res["base"]["returns"], "cand": res["cand"]["returns"]}).to_csv(out / "returns.csv")
    print(f"\n保存先: {out}/report.md")


if __name__ == "__main__":
    main()
