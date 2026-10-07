"""メタラベリング（2 次モデルで今の戦略の取引を選ぶ）で成績が良くなるかを確かめる（./cfd meta）。

    python scripts/meta_study.py                  # 今の構成（タートル 55/20 日足＋行き過ぎフィルタ）で

設定と採用の条件は docs/meta_labeling.md（結果を見る前に決めたもの）:
  1. 期間A（先物 2001〜2021-05）で、パージング・エンバーゴ付きの組み合わせ交差検証（CPCV）
  2. 期間A の全部で学習したモデルを固定し、期間B（フィリップ）と期間C（使っていない市場）に当てる
  3. 時期をずらした同じ見送り・量の並び（偶然）と比べ、試した数（既定 18）で割り引いた基準も見る

先に ./cfd context で先物のデータを取得しておく（期間A・C に使う）。
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

from cfdbot.meta import (META_LABELS, MODES, classification, cpcv, feature_columns, fit_logistic,  # noqa: E402
                         meta_feature_frame, meta_gates, meta_rows, meta_weights)
from cfdbot.metrics import daily_equity  # noqa: E402
from cfdbot.stats import deflated_threshold, expected_max_z, hhi, longest_underwater_days, psr_of  # noqa: E402
from cfdbot.study import build_period_c, build_setup, shifted, summarize  # noqa: E402

ALL_TIME = (np.iinfo(np.int64).min, np.iinfo(np.int64).max)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--final", default="config/baselines/turtle55_d1_ext.json", help="構成（1 次モデル）")
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--context", default="data/context", help="外部データの置き場（./cfd context の保存先）")
    p.add_argument("--equity", type=float, default=10_000_000, help="比べるときの資金（円）")
    p.add_argument("--long-start", default="2001-01-01", help="期間A の開始日")
    p.add_argument("--split", default="2021-06-01", help="期間A と期間B の境（期間B の開始日）")
    p.add_argument("--end", help="期間B の終了日（省略時はデータの最後まで）")
    p.add_argument("--placebo", type=int, default=30, help="偶然との比較で時期をずらす回数")
    p.add_argument("--groups", type=int, default=6, help="CPCV のグループ数")
    p.add_argument("--test-groups", type=int, default=2, help="CPCV で検証に使うグループ数")
    p.add_argument("--embargo", type=float, default=0.01, help="エンバーゴ（期間A の長さに対する割合）")
    p.add_argument("--trials", type=int, default=18, help="これまでに試した数（割り引きに使う）")
    p.add_argument("--fine", action="store_true", help="期間B で M5 の約定の再現を使う（遅い）")
    p.add_argument("--out", default="output/meta")
    args = p.parse_args()

    setup = build_setup(args.final, args.config, args.context, args.equity, args.long_start, args.split,
                        args.end, fine=args.fine)
    if setup.period_a is None:
        raise SystemExit("先物の長期データ（期間A）が無い。先に ./cfd context を実行する")
    pa, pb = setup.period_a, setup.period_b
    runs = {"A": (pa, setup.picks, setup.symbols), "B": (pb, setup.picks, setup.symbols)}
    built_c = build_period_c(setup)
    if built_c is not None:
        pc, picks_c, syms_c = built_c
        runs["C"] = (pc, picks_c, syms_c)
    else:
        print("⚠ 使っていない市場（期間C）のデータが無い。採用の判定はできない")

    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    say(f"# メタラベリングの検証（{Path(args.final).name}、資金 {args.equity:,.0f} 円）")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　設定と採用の条件: docs/meta_labeling.md（結果を見る前に決めたもの）")
    for name, (per, _, syms) in runs.items():
        say(f"- {per.label}（{', '.join(syms)}）")

    # ---------------- 基準と入力
    base_res, base, feats = {}, {}, {}
    for name, (per, picks, syms) in runs.items():
        print(f"計算: 基準 / {per.label}", flush=True)
        base_res[name] = per.run(picks)
        base[name] = summarize(base_res[name], args.equity, syms)
        feats[name] = {s: meta_feature_frame(s, per.frames[s], per.tfs[s], per.resolve("partner", s))
                       for s in syms if s in per.frames}

    rows_a = meta_rows(base_res["A"]["trades"], feats["A"])
    if len(rows_a) < 100:
        raise SystemExit(f"期間A の取引が少なすぎる（{len(rows_a)} 回）")
    cols = feature_columns()
    y_a = (rows_a["r"] > 0).to_numpy(float)
    w_a = meta_weights(rows_a["r"].to_numpy(), rows_a["decide_ns"].to_numpy(), rows_a["exit_ns"].to_numpy())

    # ---------------- 1. 期間A の CPCV
    span = int(rows_a["decide_ns"].max() - rows_a["decide_ns"].min())
    cv = cpcv(rows_a["decide_ns"].to_numpy(), rows_a["exit_ns"].to_numpy(), args.groups, args.test_groups,
              embargo_ns=int(span * args.embargo))
    models = [fit_logistic(rows_a.loc[idx, cols], y_a[idx], w_a[idx]) if len(idx) >= 50 else None
              for idx in cv.train]
    paths = cv.paths()
    oof = np.full((len(rows_a), len(paths)), np.nan)
    for j, path in enumerate(paths):
        for g, ci in path.items():
            m = models[ci]
            if m is not None:
                at = np.flatnonzero(cv.groups == g)
                oof[at, j] = m.prob(rows_a.loc[at, cols])
    cpcv_res = {mode: [] for mode in MODES}
    for j, path in enumerate(paths):
        segments = [(cv.bounds[g][0], cv.bounds[g][1], models[ci]) for g, ci in path.items()]
        for mode in MODES:
            print(f"計算: CPCV の経路 {j + 1}/{len(paths)}・{mode} / {pa.label}", flush=True)
            r = summarize(pa.run(setup.picks, meta_gates(feats["A"], segments, mode)), args.equity, setup.symbols)
            cpcv_res[mode].append(r)

    # ---------------- 2. 期間A で学習したモデルを固定して B・C に当てる
    final = fit_logistic(rows_a[cols], y_a, w_a)
    rng = np.random.default_rng(20261007)
    fracs = rng.uniform(0.15, 0.85, args.placebo)
    fixed = {}
    for name in [k for k in ("B", "C") if k in runs]:
        per, picks, syms = runs[name]
        for mode in MODES:
            gates = meta_gates(feats[name], [(*ALL_TIME, final)], mode)
            print(f"計算: {mode} / {per.label}（偶然との比較 {len(fracs)} 回を含む）", flush=True)
            res = per.run(picks, gates)
            r = summarize(res, args.equity, syms)
            null = np.array([summarize(per.run(picks, shifted(gates, fr)), args.equity, syms)["sharpe"]
                             for fr in fracs]) - base[name]["sharpe"]
            d = r["sharpe"] - base[name]["sharpe"]
            fixed[(name, mode)] = {"sum": r, "res": res, "d": d, "null": null,
                                   "pct": float(np.mean(null < d)) if len(null) else None,
                                   "thr": deflated_threshold(null, args.trials)}

    # ---------------- 表: 成績
    say("\n## 1. 成績（シャープレシオ。括弧は基準との差）")
    say("\n### 期間A: CPCV（学習に使わなかったグループだけで判定した 5 本の経路）")
    say(f"取引 {len(rows_a)} 回を時間順に {args.groups} グループに分け、{args.test_groups} グループずつ検証（{len(cv.combos)} 通り）。"
        f"学習側から検証期間と重なる取引を除き、直後 {args.embargo:.0%}（約 {span * args.embargo / 86400e9:.0f} 日）も除いた")
    say(f"\n基準: シャープ {base['A']['sharpe']:.2f}、年率 {base['A']['cagr']:.1%}、最大DD {base['A']['mdd']:.1%}、"
        f"取引 {base['A']['trades']}")
    say("\n| 使い方 | " + " | ".join(f"経路{j + 1}" for j in range(len(paths))) + " | 中央値 | 良くなった経路 |")
    say("|---|" + "---|" * (len(paths) + 2))
    cpcv_ok = {}
    for mode, label in MODES.items():
        ds = np.array([r["sharpe"] - base["A"]["sharpe"] for r in cpcv_res[mode]])
        better = int((ds > 0).sum())
        cpcv_ok[mode] = bool(len(ds) and np.median(ds) > 0 and better >= 0.8 * len(ds))
        say(f"| {label} | " + " | ".join(f"{r['sharpe']:.2f}（{d:+.2f}）" for r, d in zip(cpcv_res[mode], ds))
            + f" | {np.median(ds):+.2f} | {better}/{len(ds)} |")

    zmax = expected_max_z(args.trials)
    say("\n### 期間A で学習したモデルを固定して、学習に使っていない期間に当てる")
    say(f"偶然より良い: 同じ見送り・量の並びを時期だけずらして {len(fracs)} 回試した結果より、差が大きかった割合。"
        f"割り引いた基準: これまで {args.trials} 通り試したので、偶然の平均 + 標準偏差 × {zmax:.2f}（{args.trials} 回試したときの一番良い偶然）")
    say("\n| 期間 | 使い方 | シャープ | 年率 | 最大DD | 取引 | 偶然より良い | 割り引いた基準 |")
    say("|---|---|---|---|---|---|---|---|")
    for name in [k for k in ("B", "C") if k in runs]:
        per = runs[name][0]
        b = base[name]
        say(f"| {per.short} | 基準 | {b['sharpe']:.2f} | {b['cagr']:.1%} | {b['mdd']:.1%} | {b['trades']} | – | – |")
        for mode, label in MODES.items():
            f = fixed[(name, mode)]
            r = f["sum"]
            thr = f"{f['thr']:+.2f}（{'超えた' if f['d'] > f['thr'] else '届かない'}）" if np.isfinite(f["thr"]) else "–"
            say(f"| {per.short} | {label} | {r['sharpe']:.2f}（{f['d']:+.2f}） | {r['cagr']:.1%} | {r['mdd']:.1%} | "
                f"{r['trades']} | {f['pct']:.0%} | {thr} |")

    # ---------------- 表: 精度
    say("\n## 2. 勝ちの当て方（取引ごと。勝ち = R がプラス）")
    say("適合率: 2 次モデルが「勝ちやすい」とした取引のうち、本当に勝った割合（1 次モデルだけなら勝率と同じ）。"
        "再現率: 勝った取引のうち、見送らずに残せた割合。AUC: 0.5 なら当て推量と同じ")
    say("\n| 期間 | 取引 | 勝率（1 次モデルだけ） | 残した割合 | 適合率 | 再現率 | 残した取引の R | 見送った取引の R | AUC |")
    say("|---|---|---|---|---|---|---|---|---|")
    seen = np.isfinite(oof).sum(axis=1)
    oof_mean = np.where(seen > 0, np.nansum(oof, axis=1) / np.maximum(seen, 1), np.nan)
    cls_rows = [("A（CPCV の平均）", oof_mean, rows_a["r"].to_numpy())]
    for name in [k for k in ("B", "C") if k in runs]:
        rows = meta_rows(base_res[name]["trades"], feats[name])
        cls_rows.append((runs[name][0].short, final.prob(rows[cols]) if len(rows) else np.zeros(0),
                         rows["r"].to_numpy(float)))
    for label, prob, r in cls_rows:
        c = classification(prob, r)
        say(f"| {label} | {c['n']} | {c['win_rate']:.0%} | {c['accepted']:.0%} | {c['precision']:.0%} | "
            f"{c['recall']:.0%} | {c['r_accepted']:+.2f} | {c['r_rejected']:+.2f} | {c['auc']:.2f} |")

    # ---------------- 表: 統計
    say("\n## 3. 統計（AFML 14 章）")
    say("PSR: シャープが本当はプラスである確率（日数・リターンの歪み・裾の厚さを考える）。"
        "集中度: 勝った取引の利益がどれだけ少数に偏るか（0 = 均等、1 = 1 回だけ）。水面下: 最高値を下回っていた最長の日数")
    say("\n| 期間 | 使い方 | PSR | 集中度（勝ち） | 最長の水面下 |")
    say("|---|---|---|---|---|")

    def stat_row(label_p, label_m, res):
        eq = res["equity"]
        rets = daily_equity(eq).pct_change().dropna() if len(eq) else pd.Series(dtype=float)
        rr = res["trades"]["r_multiple"].to_numpy(float) if not res["trades"].empty else np.zeros(0)
        say(f"| {label_p} | {label_m} | {psr_of(rets):.0%} | {hhi(rr[rr > 0]):.3f} | "
            f"{longest_underwater_days(eq):.0f} 日 |")

    for name in runs:
        stat_row(runs[name][0].short, "基準", base_res[name])
        for mode, label in MODES.items():
            if (name, mode) in fixed:
                stat_row(runs[name][0].short, label, fixed[(name, mode)]["res"])

    # ---------------- モデル
    say("\n## 4. 2 次モデル（期間A の全部で学習。係数は標準化した入力 1 つあたり。プラスなら勝ちやすい）")
    say(f"学習: {final.n} 取引、重み付きの勝ちの割合 {final.base_rate:.0%}"
        "（重み = |R|（5 で頭打ち）× 独自性。50% を超えていれば、モデルが何も分からなくても全部入る）")
    say("\n| 入力 | 係数 |")
    say("|---|---|")
    for k, v in final.importance.items():
        say(f"| {META_LABELS.get(k, k)}（{k}） | {v:+.3f} |")

    # ---------------- 判定
    say("\n## 5. 判定（docs/meta_labeling.md 5 章の条件）")
    adopted = []
    for mode, label in MODES.items():
        fb, fc = fixed.get(("B", mode)), fixed.get(("C", mode))
        checks = [
            ("期間A の CPCV で中央値が良く、8 割以上の経路で良い", cpcv_ok[mode]),
            ("期間B で偶然より良い割合 95% 以上", bool(fb and fb["pct"] is not None and fb["pct"] >= 0.95)),
            ("期間C で偶然より良い割合 95% 以上", bool(fc and fc["pct"] is not None and fc["pct"] >= 0.95)),
            ("期間B の改善が割り引いた基準を超える", bool(fb and np.isfinite(fb["thr"]) and fb["d"] > fb["thr"])),
        ]
        ok = all(c for _, c in checks)
        if ok:
            adopted.append(mode)
        say(f"- {label}: **{'採用候補' if ok else '採用しない'}**（" +
            "、".join(f"{t} {'○' if c else '×'}" for t, c in checks) + "）")
    if not adopted:
        say("\n→ どちらも条件を満たさないので、今の構成（2 次モデルなし）のまま進める。")
    else:
        say("\n→ 採用候補あり。期間A＋B で学び直した係数（model_ab.json）を EA に入れ、テスターで Python と突き合わせてからデモに入れる。")

    say("\n## 読み方")
    say("- CPCV は学習に「後の時期」も使うので、本番の再現ではなく「学習に使わなかったデータでの成績のばらつき」を見るもの。"
        "本番に近い確かめは、固定したモデルを期間B・C に当てた結果")
    say("- 期間C は銅・プラチナ・天然ガス。相方の銘柄が無いので、相方の入力は平均（0）として扱う")
    say("- 「半分」は 1 回の損失を減らすだけなので、年率は下がりやすい。シャープ（リスクあたりの成績）で比べる")

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "model_a.json").write_text(json.dumps(final.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    rows_b = meta_rows(base_res["B"]["trades"], feats["B"])
    if len(rows_b):
        rows_ab = pd.concat([rows_a, rows_b], ignore_index=True)
        w_ab = meta_weights(rows_ab["r"].to_numpy(), rows_ab["decide_ns"].to_numpy(), rows_ab["exit_ns"].to_numpy())
        model_ab = fit_logistic(rows_ab[cols], (rows_ab["r"] > 0).to_numpy(float), w_ab)
        (out / "model_ab.json").write_text(json.dumps(model_ab.to_dict(), ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    print(f"\n保存先: {out}/report.md（この内容をそのまま送ってください）")


if __name__ == "__main__":
    main()
