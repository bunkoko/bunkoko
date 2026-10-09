"""機械学習でポジションを決めると、決まったルールより良くなるかを確かめる（./cfd ml）。

    python scripts/ml_study.py            # data/universe の 59 市場で（先に ./cfd universe --fetch）
    python scripts/ml_study.py --no-mlp   # 線形のモデルだけ（速い）

59 市場をまとめて 1 つのモデルを学び（市場ごとに標準化した入力）、コストを引いたシャープを直接大きくする。
2 年ごとに、それより前のデータだけで学び直す（ウォークフォワード）。2010 年以降は学習に使っていない期間の成績。
判定の条件は docs/ml.md（結果を見る前に決めたもの）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot.context import yahoo_csv_frame  # noqa: E402
from cfdbot.events import trading_day_keys  # noqa: E402
from cfdbot.ml import (FEATURES, RULES, ann_sharpe, build_panel, daily_portfolio, fit_policy,  # noqa: E402
                       rule_positions, shift_targets, split_markets, strategy_returns, walk_forward)
from cfdbot.stats import expected_max_z, moments, psr  # noqa: E402
from cfdbot.universe import GROUPS, UNIVERSE, at_vol, data_path  # noqa: E402

PRIOR_TRIALS = 18     # これまでに試した数（docs/meta_labeling.md）


def f2(x: float) -> str:
    return f"{x:.2f}" if np.isfinite(x) else "–"


def pc(x: float, digits: int = 0) -> str:
    return f"{x:.{digits}%}" if np.isfinite(x) else "–"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data/universe", help="日足の置き場（./cfd universe --fetch の保存先）")
    p.add_argument("--first-test", type=int, default=2010, help="学習に使っていない期間の始まり（年）")
    p.add_argument("--split", default="2018-01-01", help="学習に使っていない期間の前半と後半の境")
    p.add_argument("--step", type=int, default=2, help="何年ごとに学び直すか")
    p.add_argument("--hidden", type=int, default=16, help="小さなニューラルネットの中間の数")
    p.add_argument("--placebo", type=int, default=20, help="偶然との比較の回数（線形のモデル）")
    p.add_argument("--no-mlp", action="store_true", help="ニューラルネットを省く（速い）")
    p.add_argument("--out", default="output/ml")
    args = p.parse_args()

    root = Path(args.data)
    closes, costs, groups = {}, {}, {}
    for m in UNIVERSE:
        path = data_path(root, m)
        if not path.exists():
            continue
        f = yahoo_csv_frame(path, repair=True)
        if len(f) < 600:
            continue
        keys = trading_day_keys(f.index + pd.Timedelta(days=1))    # 足の終わりの日付
        closes[m.key] = pd.Series(f["close"].to_numpy(), index=keys).groupby(level=0).last()
        g = GROUPS[m.group]
        costs[m.key] = (g.cost, g.financing)
        groups[m.key] = m.group
    if len(closes) < 10:
        raise SystemExit("市場のデータが足りない。先に ./cfd universe --fetch を実行する（data/universe/）")
    panel = build_panel(closes, costs)
    last_year = int(str(panel.dates.max())[:4])
    test_start = np.datetime64(f"{args.first_test}-01-01")
    split = pd.Timestamp(args.split)
    group_of_row = np.array([groups[panel.keys[i]] for i in panel.market])

    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    def log(s: str) -> None:
        print(s, flush=True)

    # ---------------- 位置（ポジション）を作る
    positions: dict[str, np.ndarray] = {f"rule:{k}": rule_positions(panel, k) for k in RULES}
    log("計算: 線形のモデル（2 年ごとに学び直す）")
    positions["ml:linear"] = walk_forward(panel, args.first_test, last_year, args.step, hidden=0, iters=300, log=log)
    if not args.no_mlp:
        log(f"計算: 小さなニューラルネット（中間 {args.hidden}。時間がかかる）")
        positions["ml:mlp"] = walk_forward(panel, args.first_test, last_year, args.step, hidden=args.hidden,
                                           iters=200, log=log)
    oos = (panel.dates >= test_start) & np.isfinite(positions["ml:linear"])

    def evaluate(pos: np.ndarray, rows: np.ndarray, pnl_panel=None) -> dict:
        pp = pnl_panel or panel
        sub = pp.take(rows)
        r = daily_portfolio(strategy_returns(np.nan_to_num(pos[rows]), sub), sub)
        h1, h2 = r[r.index < split], r[r.index >= split]
        cagr, mdd = at_vol(r)
        return {"r": r, "s": ann_sharpe(r), "s1": ann_sharpe(h1), "s2": ann_sharpe(h2), "cagr": cagr, "mdd": mdd,
                "psr": psr(*moments(r.to_numpy()))}

    names = {f"rule:{k}": v for k, v in RULES.items()}
    names.update({"ml:linear": "機械学習: 線形", "ml:mlp": f"機械学習: ニューラルネット（中間 {args.hidden}）"})
    res = {k: evaluate(v, oos) for k, v in positions.items()}
    core = oos & (group_of_row == "core")
    res_core = {k: evaluate(v, core) for k, v in positions.items()}

    # ---------------- 偶然との比較（線形のモデル）
    rng = np.random.default_rng(20261009)
    null = []
    for i in range(args.placebo):
        log(f"計算: 偶然との比較 {i + 1}/{args.placebo}（翌日のリターンを時期だけずらして学び直す）")
        fake = shift_targets(panel, rng)
        pos = walk_forward(fake, args.first_test, last_year, args.step, hidden=0, iters=300)
        null.append(evaluate(pos, oos & np.isfinite(pos), fake)["s"])
    null = np.array(null)

    # ---------------- 学習に使っていない市場
    half_a, half_b = split_markets(list(panel.keys), groups)
    in_a = np.array([panel.keys[i] in half_a for i in panel.market])
    held = {}
    for k, hidden, iters in [("ml:linear", 0, 300)] + ([] if args.no_mlp else [("ml:mlp", args.hidden, 200)]):
        log(f"計算: {names[k]}（市場を半分に分け、もう半分で学んだモデルを当てる）")
        pos = np.full(len(panel.x), np.nan)
        for tr, te in ((in_a, ~in_a), (~in_a, in_a)):
            q = walk_forward(panel, args.first_test, last_year, args.step, hidden=hidden, iters=iters,
                             train_mask=tr, test_mask=te)
            pos[te] = q[te]
        held[k] = evaluate(pos, oos & np.isfinite(pos))
    held_rules = {k: evaluate(positions[k], oos) for k in positions if k.startswith("rule:")}

    # ---------------- 表
    say("# 機械学習でポジションを決める検証")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　判定の条件: docs/ml.md（結果を見る前に決めたもの）")
    say(f"データ: {len(panel.keys)} 市場（Yahoo の日足）、{panel.x.shape[0]:,} 行。入力 {len(FEATURES)} 個: {', '.join(FEATURES)}")
    say(f"{args.first_test} 年から {args.step} 年ごとに、それより前のデータだけで学び直した（直前 3 年は止めどころを決める確認用）。"
        f"成績は {args.first_test} 年以降（学習に使っていない期間）、コスト込み。どの市場も年率 15% の値動きにそろえ、市場の平均を見る")

    def table(title, rows_res, keys):
        say(f"\n{title}")
        say("\n| やり方 | シャープ 全体 | 前半 | 後半 | PSR | 年率（変動 10%） | 最大DD |")
        say("|---|---|---|---|---|---|---|")
        for k in keys:
            r = rows_res[k]
            say(f"| {names[k]} | {f2(r['s'])} | {f2(r['s1'])} | {f2(r['s2'])} | {pc(r['psr'])} | "
                f"{pc(r['cagr'], 1)} | {pc(r['mdd'], 1)} |")

    order = [k for k in positions if k.startswith("rule:")] + [k for k in positions if k.startswith("ml:")]
    table(f"## 1. 全市場（{args.first_test}〜、前半 〜{split - pd.Timedelta(days=1):%Y}・後半 {split:%Y}〜）", res, order)
    table("## 2. 今の 4 商品だけ（今の口座で売買できるもの）", res_core, order)

    say("\n## 3. 確かめ")
    n_trials = PRIOR_TRIALS + (1 if args.no_mlp else 2)
    thr = float(null.mean() + null.std(ddof=1) * expected_max_z(n_trials)) if len(null) > 1 else float("nan")
    say(f"- 偶然との比較（線形）: 翌日のリターンを時期だけずらして学び直した {len(null)} 回のシャープは "
        f"{f2(np.percentile(null, 5)) if len(null) else '–'}〜{f2(np.percentile(null, 95)) if len(null) else '–'}（90% の範囲）。"
        f"本物 {f2(res['ml:linear']['s'])} がそれより良かった割合 {np.mean(null < res['ml:linear']['s']):.0%}"
        if len(null) else "- 偶然との比較: 省いた")
    say(f"- 試した数（{n_trials}）で割り引いた基準: シャープ {f2(thr)}（偶然の平均 + 標準偏差 × {expected_max_z(n_trials):.2f}）")
    say("\n### 学習に使っていない市場での成績（市場を資産クラスごとに半分に分け、もう半分で学んだモデルを当てる）")
    say("\n| やり方 | シャープ 全体 | 前半 | 後半 |")
    say("|---|---|---|---|")
    for k, r in list(held_rules.items()) + list(held.items()):
        say(f"| {names[k]} | {f2(r['s'])} | {f2(r['s1'])} | {f2(r['s2'])} |")

    say("\n### 資産クラスごとのシャープ（" + f"{args.first_test}" + "〜）")
    cls = [g for g in GROUPS if (group_of_row == g).any()]
    say("\n| やり方 | " + " | ".join(GROUPS[g].label for g in cls) + " |")
    say("|---|" + "---|" * len(cls))
    for k in order:
        cells = [f2(evaluate(positions[k], oos & (group_of_row == g))["s"]) for g in cls]
        say(f"| {names[k]} | " + " | ".join(cells) + " |")

    # ---------------- 判定
    say("\n## 4. 判定（docs/ml.md 4 章の条件）")
    rules = [k for k in positions if k.startswith("rule:") and k != "rule:long"]
    best_rule = max(rules, key=lambda k: res[k]["s"])
    say(f"比べる相手: 決まったルールの中で全体のシャープが一番良い「{names[best_rule]}」")
    adopted = []
    for k in [k for k in positions if k.startswith("ml:")]:
        r, h = res[k], held.get(k)
        checks = [
            ("前半・後半とも一番良いルールよりシャープが高い",
             r["s1"] > res[best_rule]["s1"] and r["s2"] > res[best_rule]["s2"]),
            ("偶然より良い割合 95% 以上", bool(len(null)) and float(np.mean(null < r["s"])) >= 0.95),
            ("試した数で割り引いた基準を超える", np.isfinite(thr) and r["s"] > thr),
            ("学習に使っていない市場でも一番良いルールより高い",
             h is not None and h["s"] > held_rules[best_rule]["s"]),
            ("今の 4 商品でタートルの向き以上", res_core[k]["s"] >= res_core["rule:turtle"]["s"]),
        ]
        ok = all(c for _, c in checks)
        if ok:
            adopted.append(k)
        say(f"- {names[k]}: **{'採用候補' if ok else '採用しない'}**（" +
            "、".join(f"{t} {'○' if c else '×'}" for t, c in checks) + "）")
    if not adopted:
        say("\n→ 機械学習は条件を満たさない。今のルール（タートル＋行き過ぎフィルタ）のまま進める。強化学習（段階 2）にも進まない。")
    else:
        say("\n→ 採用候補あり。今の口座の MT5 のデータ・コストで同じ確かめをし、EA に入れる方法（係数か ONNX）を決める。")

    say("\n## 読み方")
    say("- ここでの「タートルの向き」は終値だけの簡易版（損切りなし・量は値動きで調整）。EA の成績とは一致しない。"
        "同じ条件で機械学習と比べるためのもの")
    say("- 機械学習は 2 年ごとに学び直しているので、本番でも定期的に学び直す前提")

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # 最新のデータまでで学んだ線形のモデル（採用候補になったときに EA に入れる係数の元）
    end = panel.dates.max()
    vcut = end - np.timedelta64(3 * 365, "D")
    final = fit_policy(panel.take(panel.dates < vcut), panel.take(panel.dates >= vcut), hidden=0, iters=300)
    (out / "linear_model.json").write_text(json.dumps(
        {"features": list(FEATURES), "w": [float(v) for v in final.params["w"]], "b": float(final.params["b"][0])},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n保存先: {out}/report.md（この内容をそのまま送ってください）")


if __name__ == "__main__":
    main()
