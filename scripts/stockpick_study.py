"""日本の個別株をルールで選ぶ検証（./cfd stockpick）。

    python scripts/stockpick_study.py --fetch   # Yahoo から約 230 銘柄の日足を取る（初回・更新）
    python scripts/stockpick_study.py           # 検証と判定（有望なら今月の上位 20 も表示）

ルールと判定の条件は docs/stockpick.md（結果を見る前に決めたもの）。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot.context import fetch_yahoo  # noqa: E402
from cfdbot.stats import psr_of  # noqa: E402
from cfdbot.stockpick import (COST, TICKERS, TOP_N, US_TICKERS, basket_returns, load_closes, monthly_picks,  # noqa: E402
                           monthly_returns, random_pick_excess, scores, sharpe_monthly, stock_path, universe_returns)

MARKETS = {   # 名前 → (ティッカー, 既定の置き場, 表示, 事前登録の文書, これまでの試した数)
    "jp": (TICKERS, "data/stockpick", "日本の個別株", "docs/stockpick.md 3 章", 41),
    "us": (US_TICKERS, "data/stockpick_us", "米国の個別株", "docs/us_equity.md 3 章", 42),
}


def f2(x: float) -> str:
    return f"{x:.2f}" if np.isfinite(x) else "–"


def pct(x: float, digits: int = 0) -> str:
    return f"{x:.{digits}%}" if np.isfinite(x) else "–"


def fetch_all(root: Path, tickers, log=print) -> None:
    root.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    for i, t in enumerate(tickers):
        p = stock_path(root, t)
        if p.exists():
            ok += 1
            continue
        try:
            df = fetch_yahoo(t, "2000-01-01")
            if len(df) < 100:
                raise RuntimeError(f"{len(df)} 行しか無い")
            df.to_csv(p, index=False)
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            log(f"  ✗ {t}: {str(e)[:80]}")
        time.sleep(0.3)
        if (i + 1) % 50 == 0:
            log(f"  {i + 1}/{len(tickers)} 銘柄（取れた {ok}・取れない {fail}）")
    log(f"取得: {ok} 銘柄（取れない {fail}）→ {root}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--market", default="jp", choices=list(MARKETS), help="jp: 日本株（docs/stockpick.md）、us: 米国株（docs/us_equity.md）")
    p.add_argument("--root", default=None)
    p.add_argument("--fetch", action="store_true")
    p.add_argument("--start", default="2006-01-01")
    p.add_argument("--placebo", type=int, default=200)
    p.add_argument("--out", default="output/stockpick")
    args = p.parse_args()

    tickers, default_root, label, rule_doc, prior = MARKETS[args.market]
    root = Path(args.root or default_root)
    if args.fetch or not root.exists() or not any(root.glob("*.csv")):
        fetch_all(root, tickers)
    notes: list[str] = []
    close = load_closes(root, tickers, log=notes.append)
    if close.shape[1] < 2 * TOP_N:
        raise SystemExit(f"銘柄が足りない（{close.shape[1]}。./cfd stockpick --fetch）")
    start = pd.Timestamp(args.start, tz="UTC")

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    mret = monthly_returns(close)
    picks = monthly_picks(close)
    top = basket_returns(picks, mret)
    bench = universe_returns(close, mret)
    m = (top.index >= start) & top.notna() & bench.notna() & mret.reindex(top.index).notna().any(axis=1)
    top, bench = top[m], bench[m]
    diff = (top - bench).dropna()
    half = len(diff) // 2
    s_all, s1, s2 = sharpe_monthly(diff), sharpe_monthly(diff.iloc[:half]), sharpe_monthly(diff.iloc[half:])
    ps = psr_of(diff.to_numpy()) if len(diff) > 24 else np.nan

    say(f"# {label}をルールで選ぶ（モメンタム × 低ボラティリティ、上位 20 を同じ金額で）")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　判定の条件: {rule_doc}（結果を見る前に決めたもの）。"
        f"試した数: {prior} ＋ 1 ＝ {prior + 1}")
    say(f"対象: {close.shape[1]} 銘柄（Yahoo の日足。今の銘柄だけ = 生存者バイアスあり。比べる相手も同じ銘柄群）。"
        f"期間 {diff.index[0]:%Y-%m}〜{diff.index[-1]:%Y-%m}（{len(diff)} か月）。コスト 往復 {COST:.1%}")
    if notes:
        say("- 値段の尺度の飛び（1 日で ×2 超・÷2 未満。再上場・持ち株会社化）より前を省いた銘柄: " + "; ".join(n.strip() for n in notes))

    def row(name, r):
        r = r.dropna()
        eq = (1 + r).cumprod()
        dd = float((eq / eq.cummax() - 1).min()) if len(eq) else np.nan
        return (f"| {name} | {f2(sharpe_monthly(r))} | {f2(sharpe_monthly(r.iloc[:len(r) // 2]))} | "
                f"{f2(sharpe_monthly(r.iloc[len(r) // 2:]))} | {pct(float(r.mean() * 12), 1)} | {pct(dd, 1)} |")

    say("\n| | シャープ 全期間 | 前半 | 後半 | 年率 | 最大DD |")
    say("|---|---|---|---|---|---|")
    say(row("上位 20 銘柄", top))
    say(row("比べる相手（全銘柄を同じ金額で）", bench))
    say(row("差（選ぶことの上乗せ）", diff))
    pl = random_pick_excess(close, mret, bench, args.placebo) if args.placebo else np.array([])
    pl = pl[np.isfinite(pl)]
    pb = float(np.mean(pl < s_all)) if len(pl) else np.nan
    say(f"\n- 差の PSR {pct(ps)}、でたらめに 20 銘柄を選んだ {len(pl)} 回より良い割合 {pct(pb)}"
        f"（でたらめの差のシャープの平均 {f2(float(pl.mean())) if len(pl) else '–'}）")
    yearly = (1 + diff).groupby(diff.index.year).prod() - 1
    say("- 年ごとの差: " + "、".join(f"{y} {v:+.1%}" for y, v in yearly.items()))

    ok = s_all > 0.3 and s1 > 0 and s2 > 0 and ps >= 0.95 and pb >= 0.95
    say("\n## 判定\n")
    say(f"- 差のシャープ {f2(s_all)}（前半 {f2(s1)}・後半 {f2(s2)}）、PSR {pct(ps)}、でたらめより良い割合 {pct(pb)}")
    say(f"- **{'有望（記録だけの売買を始める）' if ok else '有望ではない。' + label + 'のルール選びは閉じる'}**"
        "（条件: 0.3 超、前半・後半ともプラス、PSR 95%、でたらめより良い 95%）")

    if ok:
        sc = scores(close).iloc[-1].dropna().sort_values(ascending=False)
        say(f"\n## 今の上位 {TOP_N}（{close.index[-1]:%Y-%m-%d} の終値まで。記録だけ。買う根拠ではない）\n")
        say("| 順位 | 銘柄 | 総合点 | 12-1 か月の値上がり | 60 日の変動（年率） |")
        say("|---|---|---|---|---|")
        mom = (close.shift(21) / close.shift(252) - 1).iloc[-1]
        vol = close.pct_change().rolling(60).std().iloc[-1] * np.sqrt(252)
        for i, (t, v) in enumerate(sc.head(TOP_N).items(), 1):
            say(f"| {i} | {t} | {v:.2f} | {mom[t]:+.1%} | {vol[t]:.0%} |")
    say("\n## 読み方・限界")
    say("- 今の銘柄だけで作った対象なので、途中で消えた会社が入っていない。選ぶ側と相手の両方に同じ偏りがあるので「差」で見る")
    say("- 配当を含めていない。低ボラティリティの銘柄は配当が高めなことが多く、実際の差は少し大きい方向にずれる")
    say("- 1 銘柄の予測ではない。20 銘柄の束の、月ごとの小さな偏りの話。1 銘柄ずつの当たり外れは偶然")
    pd.DataFrame({"top20": top, "universe": bench, "diff": diff}).to_csv(out / "monthly.csv")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {out}/report.md")


if __name__ == "__main__":
    main()
