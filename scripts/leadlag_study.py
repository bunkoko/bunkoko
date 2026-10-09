"""先行指標（商品・為替・金利・米国株）で、日米のセクター株・個別株を予測できるかを確かめる（./cfd leadlag）。

    python scripts/leadlag_study.py --fetch     # 初回: Yahoo から先行指標 7・株 57 を data/leadlag/ に取得
    python scripts/leadlag_study.py             # 取得済みのデータで

部 A: 結果を見る前に決めた仮説（例: 原油高 → 化学株が後で下がる）を 1 つずつ、偶然と比べて確かめる
部 B: 機械学習（./cfd ml と同じ形）に先行指標の入力を足すと、株の予測が良くなるかを確かめる
判定の条件は docs/leadlag.md（結果を見る前に決めたもの）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot.context import fetch_yahoo  # noqa: E402
from cfdbot.leadlag import (DRIVERS, GROUP_COST, GROUP_LABELS, HORIZONS, HYPOTHESES, TARGETS,  # noqa: E402
                            DriverSeries, build_panel, driver_features, hypothesis_returns, read_yahoo,
                            shifted_driver, target_frame)
from cfdbot.ml import (FEATURES as OWN, ann_sharpe, daily_portfolio, rule_positions, strategy_returns,  # noqa: E402
                       walk_forward)
from cfdbot.stats import expected_max_z, moments, psr  # noqa: E402

PRIOR_TRIALS = 20    # これまでに試した数（docs/ml.md）


def f2(x: float) -> str:
    return f"{x:.2f}" if np.isfinite(x) else "–"


def fetch(root: Path, since: str, refresh: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    fails = 0
    for item in list(DRIVERS) + list(TARGETS):
        p = root / f"{item.key}.csv"
        if p.exists() and not refresh:
            continue
        if fails >= 5:
            print(f"  – {item.label}（{item.ticker}）: 失敗が続いたので省いた")
            continue
        try:
            df = fetch_yahoo(item.ticker, since)
            df.to_csv(p, index=False)
            print(f"  ✓ {item.label}（{item.ticker}）: {df['date'].iloc[0]}〜{df['date'].iloc[-1]}（{len(df):,} 行）",
                  flush=True)
            fails = 0
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ✗ {item.label}（{item.ticker}）: {str(e)[:120]}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data/leadlag")
    p.add_argument("--fetch", action="store_true")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--since", default="2000-01-01")
    p.add_argument("--split-a", default="2017-01-01", help="部 A の前半と後半の境")
    p.add_argument("--placebo-a", type=int, default=200, help="部 A の偶然との比較の回数（仮説ごと）")
    p.add_argument("--first-test", type=int, default=2013, help="部 B: 学習に使っていない期間の始まり（年）")
    p.add_argument("--split-b", default="2020-01-01", help="部 B の前半と後半の境")
    p.add_argument("--placebo-b", type=int, default=10, help="部 B の偶然との比較の回数（線形のモデル）")
    p.add_argument("--no-mlp", action="store_true")
    p.add_argument("--out", default="output/leadlag")
    args = p.parse_args()

    root = Path(args.data)
    if args.fetch or not root.exists():
        print(f"取得中: Yahoo Finance（先行指標 {len(DRIVERS)}・株 {len(TARGETS)}）…", flush=True)
        fetch(root, args.since, args.refresh)

    drivers = {}
    for d in DRIVERS:
        path = root / f"{d.key}.csv"
        if path.exists():
            s, tz = read_yahoo(path)
            drivers[d.key] = DriverSeries.from_series(d.key, d.kind, s, tz)
    targets, frames, groups = {}, {}, {}
    for t in TARGETS:
        path = root / f"{t.key}.csv"
        if not path.exists():
            continue
        s, tz = read_yahoo(path)
        if len(s) < 3 * 252:
            continue
        targets[t.key] = (s, tz)
        frames[t.key] = target_frame(s, tz, drivers)
        groups[t.key] = t.group
    if len(frames) < 10 or len(drivers) < 4:
        raise SystemExit("データが足りない。--fetch で取得する（data/leadlag/）")

    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    def log(s: str) -> None:
        print(s, flush=True)

    say("# 先行指標で株を予測できるかの検証")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　判定の条件: docs/leadlag.md（結果を見る前に決めたもの）")
    say(f"先行指標 {len(drivers)}: " + "、".join(d.label for d in DRIVERS if d.key in drivers))
    say(f"株 {len(frames)}: " + "、".join(f"{GROUP_LABELS[g]} {sum(1 for k in frames if groups[k] == g)}"
                                         for g in GROUP_LABELS))
    say("株の判断はその取引所の引け、先行指標はその時刻までに日付が終わったものだけを使う（東京の判断には前日の米国の値まで）。"
        "コスト込み、値動きの大きさでそろえた（年率 15%）成績")

    # ---------------- 部 A
    split_a = pd.Timestamp(args.split_a)
    n_tests = len(HYPOTHESES) * len(HORIZONS)
    zmax = expected_max_z(n_tests)
    rng = np.random.default_rng(20261009)
    fracs = rng.uniform(0.2, 0.8, args.placebo_a)
    say(f"\n## 部 A: 仮説ごと（{len(HYPOTHESES)} 個 × 期間 {len(HORIZONS)} 通り = {n_tests} 通り）")
    say(f"先行指標の直近 h 日の向き × 仮説の向きで、株を h 日持つ（h = {'・'.join(map(str, HORIZONS))} 日）。"
        f"前半 〜{split_a - pd.Timedelta(days=1):%Y}・後半 {split_a:%Y}〜。偶然 = 先行指標の並びを時期だけずらしたもの "
        f"{len(fracs)} 回。割り引いた基準 = 偶然の平均 + 標準偏差 × {zmax:.2f}（{n_tests} 通り試したため）")
    say("判定「有望」: 前半・後半ともシャープがプラス、偶然より良い割合 95% 以上、全期間が割り引いた基準を超える")
    say("\n| 仮説 | 期間 | シャープ 前半 | 後半 | 全期間 | 偶然より良い | 判定 |")
    say("|---|---|---|---|---|---|---|")
    promising, rows_a, n_done = [], [], 0
    for dkey, tkey, sign, label in HYPOTHESES:
        if dkey not in drivers or tkey not in frames:
            continue
        d, fr = drivers[dkey], frames[tkey]
        cost = GROUP_COST[groups[tkey]]
        for h in HORIZONS:
            r = hypothesis_returns(fr, d, sign, h, cost)
            s1, s2, sf = ann_sharpe(r[r.index < split_a]), ann_sharpe(r[r.index >= split_a]), ann_sharpe(r)
            null = np.array([ann_sharpe(hypothesis_returns(fr, shifted_driver(d, fr_), sign, h, cost)) for fr_ in fracs])
            null = null[np.isfinite(null)]
            pct = float(np.mean(null < sf)) if len(null) else float("nan")
            thr = float(null.mean() + null.std(ddof=1) * zmax) if len(null) > 1 else float("nan")
            ok = bool(s1 > 0 and s2 > 0 and pct >= 0.95 and sf > thr)
            n_done += 1
            if ok:
                promising.append((label, h))
            tname = next(t.label for t in TARGETS if t.key == tkey)
            dname = next(x.label for x in DRIVERS if x.key == dkey)
            rows_a.append((pct, f"| {dname}→{tname}（{label}） | {h} 日 | {f2(s1)} | {f2(s2)} | {f2(sf)} | "
                                f"{pct:.0%} | {'有望' if ok else '–'} |"))
    for _, row in rows_a:
        say(row)
    say(f"\n有望: {len(promising)} / {n_done}（すべて偶然でも、5% の基準なら {n_done * 0.05:.1f} 個くらいは"
        "「偶然より良い 95% 以上」になる）")
    if promising:
        for label, h in promising:
            say(f"- {label}（{h} 日）")

    # ---------------- 部 B
    log("\n計算: 部 B のデータを作る")
    drv_cols = driver_features(list(drivers))
    panel_own = build_panel(frames, groups, list(OWN), start="2005-01-01")
    panel_all = build_panel(frames, groups, list(OWN) + drv_cols, start="2005-01-01")
    last_year = int(str(panel_own.dates.max())[:4])
    split_b = pd.Timestamp(args.split_b)
    group_of_row = np.array([groups[panel_own.keys[i]] for i in panel_own.market])

    pos = {"rule:long": rule_positions(panel_own, "long"), "rule:tsmom": rule_positions(panel_own, "tsmom"),
           "rule:turtle": rule_positions(panel_own, "turtle")}
    models = [("ml:lin_own", panel_own, 0, 300), ("ml:lin_all", panel_all, 0, 300)]
    if not args.no_mlp:
        models += [("ml:mlp_own", panel_own, 16, 200), ("ml:mlp_all", panel_all, 16, 200)]
    for name, pn, hidden, iters in models:
        log(f"計算: {name}（{args.first_test} 年から 2 年ごとに学び直す）")
        pos[name] = walk_forward(pn, args.first_test, last_year, 2, hidden=hidden, iters=iters, log=log)
    oos = (panel_own.dates >= np.datetime64(f"{args.first_test}-01-01")) & np.isfinite(pos["ml:lin_own"])

    def ev(p_, rows, pn=panel_own):
        sub = pn.take(rows)
        r = daily_portfolio(strategy_returns(np.nan_to_num(p_[rows]), sub), sub)
        return {"s": ann_sharpe(r), "s1": ann_sharpe(r[r.index < split_b]), "s2": ann_sharpe(r[r.index >= split_b]),
                "psr": psr(*moments(r.to_numpy()))}

    res = {k: ev(v, oos) for k, v in pos.items()}

    null_imp = []
    for i in range(args.placebo_b):
        log(f"計算: 部 B の偶然との比較 {i + 1}/{args.placebo_b}（先行指標の並びを時期だけずらして学び直す）")
        fake_d = {k: shifted_driver(d, rng.uniform(0.2, 0.8)) for k, d in drivers.items()}
        fake_frames = {k: target_frame(s, tz, fake_d) for k, (s, tz) in targets.items()}
        pf = build_panel(fake_frames, groups, list(OWN) + drv_cols, start="2005-01-01")
        q = walk_forward(pf, args.first_test, last_year, 2, hidden=0, iters=300)
        null_imp.append(ev(q, oos & np.isfinite(q), pf)["s"] - res["ml:lin_own"]["s"])
    null_imp = np.array(null_imp)

    names = {"rule:long": "持っているだけ（買いのみ）", "rule:tsmom": "12 か月の向き", "rule:turtle": "タートルの向き",
             "ml:lin_own": "機械学習 線形: 自分の値動きだけ", "ml:lin_all": "機械学習 線形: ＋先行指標",
             "ml:mlp_own": "機械学習 ネット: 自分の値動きだけ", "ml:mlp_all": "機械学習 ネット: ＋先行指標"}
    say(f"\n## 部 B: 機械学習に先行指標を足す（{args.first_test} 年以降 = 学習に使っていない期間、"
        f"前半 〜{split_b - pd.Timedelta(days=1):%Y}・後半 {split_b:%Y}〜）")
    say(f"入力: 自分の値動き {len(OWN)} 個（./cfd ml と同じ）＋先行指標 {len(drv_cols)} 個"
        "（各先行指標の 1・5・21 日の変化と、それに株の連れて動く度合いを掛けたもの）")
    say("\n| やり方 | シャープ 全体 | 前半 | 後半 | PSR |")
    say("|---|---|---|---|---|")
    for k in pos:
        r = res[k]
        say(f"| {names[k]} | {f2(r['s'])} | {f2(r['s1'])} | {f2(r['s2'])} | {r['psr']:.0%} |")

    say("\n### グループごとのシャープ")
    gl = [g for g in GROUP_LABELS if (group_of_row == g).any()]
    say("\n| やり方 | " + " | ".join(GROUP_LABELS[g] for g in gl) + " |")
    say("|---|" + "---|" * len(gl))
    for k in pos:
        pn = panel_all if k.endswith("_all") else panel_own
        say(f"| {names[k]} | " + " | ".join(f2(ev(pos[k], oos & (group_of_row == g), pn)["s"]) for g in gl) + " |")

    imp = res["ml:lin_all"]["s"] - res["ml:lin_own"]["s"]
    imp1 = res["ml:lin_all"]["s1"] - res["ml:lin_own"]["s1"]
    imp2 = res["ml:lin_all"]["s2"] - res["ml:lin_own"]["s2"]
    n_trials = PRIOR_TRIALS + 1
    thr = float(null_imp.mean() + null_imp.std(ddof=1) * expected_max_z(n_trials)) if len(null_imp) > 1 else np.nan
    pct = float(np.mean(null_imp < imp)) if len(null_imp) else float("nan")
    say(f"\n先行指標を足した改善（線形）: 全体 {imp:+.2f}（前半 {imp1:+.2f}・後半 {imp2:+.2f}）。"
        f"先行指標の並びを時期だけずらした {len(null_imp)} 回の改善 "
        f"{f2(np.min(null_imp)) if len(null_imp) else '–'}〜{f2(np.max(null_imp)) if len(null_imp) else '–'}、"
        f"本物がそれより良かった割合 {pct:.0%}。試した数（{n_trials}）で割り引いた基準 {f2(thr)}")
    ok_b = bool(imp1 > 0 and imp2 > 0 and pct >= 0.95 and np.isfinite(thr) and imp > thr)
    say(f"- 判定: **{'先行指標は機械学習の役に立つ（採用候補）' if ok_b else '先行指標を足しても良くならない'}**"
        "（条件: 前半・後半とも改善、偶然より良い割合 95% 以上、割り引いた基準を超える。判定は線形で行い、ネットは参考）")

    say("\n## 読み方")
    say("- 部 A は「その関係が遅れて効くか」（売買できるか）を見ている。同じ日に一緒に動く関係（相関）があっても、"
        "株価がすぐに織り込めば、後から売買しても儲からない")
    say("- 株は今のフィリップの MT5 口座では売買できない。結果が良くても、株の CFD などを扱う口座が必要")
    say("- 個別株は今ある会社だけ（倒産・上場廃止した会社は入らない）。配当は入っていない")

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {out}/report.md")


if __name__ == "__main__":
    main()
