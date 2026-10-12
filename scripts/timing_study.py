"""S&P500 を持ち方で上回れるか（./cfd timing）: 変動で量を変える（vol_managed）・10 か月平均で持つか決める（sma_timing）。

    python scripts/timing_study.py --fetch   # Yahoo から ^GSPC（1950 年〜）を取る
    python scripts/timing_study.py           # 計算と判定

決まりと判定の条件は docs/us_equity.md（結果を見る前に決めたもの）。
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
from cfdbot.stats import psr_of  # noqa: E402
from cfdbot.timing import (COST, SMA_MONTHS, VOL_CAP, VOL_WINDOW, load_index, max_drawdown, placebo_sharpes,  # noqa: E402
                           sharpe, sma_weight, strategy_returns, vol_managed_weight)

PRIOR_TRIALS, NEW_TRIALS = 43, 2        # us_select の後


def f2(x: float) -> str:
    return f"{x:.2f}" if np.isfinite(x) else "–"


def pct(x: float, digits: int = 0) -> str:
    return f"{x:.{digits}%}" if np.isfinite(x) else "–"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default="data/timing")
    p.add_argument("--fetch", action="store_true")
    p.add_argument("--start", default="1951-01-01")
    p.add_argument("--placebo", type=int, default=200)
    p.add_argument("--out", default="output/timing")
    args = p.parse_args()

    root = Path(args.root)
    path = root / "GSPC.csv"
    if args.fetch or not path.exists():
        root.mkdir(parents=True, exist_ok=True)
        fetch_yahoo("^GSPC", "1950-01-01").to_csv(path, index=False)
    close = load_index(path)
    start = pd.Timestamp(args.start, tz="UTC")

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    hold_w = pd.Series(1.0, index=close.index)
    weights = {"持っているだけ（比べる相手）": hold_w,
               f"vol_managed（変動で量を変える。上限 {VOL_CAP} 倍）": vol_managed_weight(close),
               f"sma_timing（{SMA_MONTHS} か月平均の上なら持つ）": sma_weight(close)}
    rets = {k: strategy_returns(close, w) for k, w in weights.items()}
    mask = np.asarray(close.index >= start)
    hold = rets["持っているだけ（比べる相手）"][mask]

    say("# S&P500 を持ち方で上回れるか（vol_managed・sma_timing）")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　判定の条件: docs/us_equity.md 3 章（結果を見る前に決めたもの）。"
        f"試した数: {PRIOR_TRIALS} ＋ {NEW_TRIALS} ＝ {PRIOR_TRIALS + NEW_TRIALS}")
    say(f"データ: Yahoo の ^GSPC {close.index[0]:%Y-%m-%d}〜{close.index[-1]:%Y-%m-%d}（配当なし）。"
        f"成績は {start:%Y-%m}〜。コスト: 量の変化分に往復 {COST:.2%}。現金の金利は 0。vol_managed の変動は直近 {VOL_WINDOW} 日")
    say("\n| 持ち方 | シャープ 全期間 | 前半 | 後半 | 2001〜 | 年率 | 最大DD | 持っている量の平均 | 差の PSR | 偶然より良い割合 | 判定 |")
    say("|---|---|---|---|---|---|---|---|---|---|---|")
    half = int(mask.sum()) // 2
    verdicts = {}
    for name, r_all in rets.items():
        r = r_all[mask]
        s_all, s1, s2 = sharpe(r), sharpe(r.iloc[:half]), sharpe(r.iloc[half:])
        s01 = sharpe(r[r.index >= "2001-01-01"])
        base = name.startswith("持っている")
        d = (r - hold).dropna()
        ps = psr_of(d.to_numpy()) if not base else np.nan
        pl = placebo_sharpes(close, weights[name], mask, args.placebo) if (not base and args.placebo) else np.array([])
        pb = float(np.mean(pl < s_all)) if len(pl) else np.nan
        h1, h2 = sharpe(hold.iloc[:half]), sharpe(hold.iloc[half:])
        if base:
            v = "–"
        else:
            ok = s1 > h1 and s2 > h2 and ps >= 0.95 and pb >= 0.95
            v = "**有望**" if ok else "有望ではない"
            verdicts[name] = ok
        say(f"| {name} | {f2(s_all)} | {f2(s1)} | {f2(s2)} | {f2(s01)} | {pct(float(r.mean() * 252), 1)} | "
            f"{pct(max_drawdown(r), 1)} | {weights[name][mask].mean():.2f} | {pct(ps)} | {pct(pb)} | {v} |")
    pd.DataFrame({k: v[mask] for k, v in rets.items()}).to_csv(out / "daily.csv")

    say("\n## 判定\n")
    for name, ok in verdicts.items():
        say(f"- {name}: {'**有望**' if ok else '有望ではない'}（条件: 前半・後半とも持っているだけよりシャープが高く、差の PSR 95%、偶然 95%）")
    if not any(verdicts.values()):
        say("- 持ち方の工夫で S&P500 を上回るものは無い。株での結論は「低コストで（NISA で）指数を持つ」")
    say("\n## 読み方・限界")
    say("- 配当を含めていない（相手にも含めていない）。sma_timing は持たない期間の配当を取り逃すので、実際はここより少し不利")
    say("- 最大DD が浅くなっても、判定には使わない。下げを浅くするだけなら、量を減らして持ち続ければ同じことができる")
    say("- 1950 年以降の 1 本の道筋。前半（〜1988 年ごろ）と後半で分けて見る")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {out}/report.md")


if __name__ == "__main__":
    main()
