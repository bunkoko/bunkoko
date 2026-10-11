"""キャリー（原油の期間構造）を確かめる（./cfd carry）。

    python scripts/carry_study.py --fetch   # EIA から WTI の限月 1〜4 を取る（Mac で。クラウドからは届かない）
    python scripts/carry_study.py           # data/carry/wti_curve.csv で計算と判定

仮説と判定の条件は docs/carry.md（結果を見る前に決めたもの）。
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

from cfdbot.carry import (COST, FINANCING, MAX_LEV, ROLL_LEAD, VOL_TARGET, basis, curve_path, evaluate,  # noqa: E402
                          fetch_curve, front_returns, load_curve, placebo_sharpes, sharpe, signals)
from cfdbot.stats import psr_of  # noqa: E402
from cfdbot.study import clean  # noqa: E402

PRIOR_TRIALS, NEW_TRIALS = 38, 2


def f2(x: float) -> str:
    return f"{x:.2f}" if np.isfinite(x) else "–"


def pct(x: float, digits: int = 0) -> str:
    return f"{x:.{digits}%}" if np.isfinite(x) else "–"


def mdd(r: pd.Series) -> float:
    eq = r.cumsum()
    return float((eq - eq.cummax()).min()) if len(eq) else float("nan")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default="data/carry")
    p.add_argument("--fetch", action="store_true", help="EIA から取る（初回・更新）")
    p.add_argument("--start", default="1986-01-01")
    p.add_argument("--placebo", type=int, default=200)
    p.add_argument("--out", default="output/carry")
    args = p.parse_args()

    root = Path(args.root)
    if args.fetch or not curve_path(root).exists():
        print("取得中: EIA の WTI 先物 限月 1〜4（日次、1983 年〜）…", flush=True)
        try:
            curve = fetch_curve()
        except Exception as e:  # noqa: BLE001
            raise SystemExit(f"EIA から取れなかった（{str(e)[:160]}）。クラウドの環境からは届かないので Mac で ./cfd carry --fetch") from None
        root.mkdir(parents=True, exist_ok=True)
        curve.to_csv(curve_path(root))
        print(f"  ✓ {curve.index[0]:%Y-%m-%d}〜{curve.index[-1]:%Y-%m-%d}（{len(curve):,} 日）→ {curve_path(root)}")
    curve = load_curve(root)
    start = pd.Timestamp(clean(args.start))

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    fr = front_returns(curve)
    b = basis(curve)
    sig = signals(curve, fr)
    say("# キャリー（原油の期間構造）")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　判定の条件: docs/carry.md 4 章（結果を見る前に決めたもの）")
    say(f"データ: EIA の WTI 先物 限月 1〜4、{curve.index[0]:%Y-%m-%d}〜{curve.index[-1]:%Y-%m-%d}（{len(curve):,} 日）。"
        f"切り替わる {ROLL_LEAD} 営業日前に限月 2 へ乗り換え。切り替わる日をデータで見つけた月の割合 {pct(fr.attrs.get('by_data', np.nan))}"
        f"（残りは規則）。切り替わりの数 {int(fr['switch'].sum())}")
    say(f"決まり: 月末に向きと量を決めて翌月は固定。量は年率 {VOL_TARGET:.0%}（名目 ≤ {MAX_LEV:.0f} 倍）。"
        f"コスト 往復 {COST:.2%}・年 {FINANCING:.0%}（CFD）。試した数: {PRIOR_TRIALS} ＋ {NEW_TRIALS} ＝ {PRIOR_TRIALS + NEW_TRIALS}")
    m_all = curve.index >= start
    bb = b[m_all]
    say(f"キャリー: バックワーデーション（プラス）の日の割合 {pct((bb > 0).mean())}、中央値 {bb.median():+.3%}（月あたり）、"
        f"2001 年以降 {pct((bb[bb.index >= '2001-01-01'] > 0).mean())}")

    windows = [("全期間", start, None), ("参考 2001-01〜2021-05", pd.Timestamp("2001-01-01"), pd.Timestamp("2021-06-01")),
               ("参考 2021-06〜", pd.Timestamp("2021-06-01"), None)]
    results = {}
    for fin_label, fin in (("CFD（判定）", FINANCING), ("先物なら（保有コストなし。参考）", 0.0)):
        say(f"\n## {fin_label}\n")
        say("| 向き | 期間 | シャープ | 前半 | 後半 | PSR | 偶然より良い割合 | 年率 | 最大DD | 持っている日 | 名目÷資金 平均 |")
        say("|---|---|---|---|---|---|---|---|---|---|---|")
        for name in ("carry", "trend", "combo"):
            res = evaluate(fr, sig[name], financing=fin)
            for w_label, ws, we in windows:
                m = (res.index >= ws) & ((res.index < we) if we is not None else True)
                r = res["ret"][m]
                if len(r) < 250:
                    continue
                half = len(r) // 2
                s1, s2, sf = sharpe(r.iloc[:half]), sharpe(r.iloc[half:]), sharpe(r)
                ps = psr_of(r.to_numpy())
                pl = (placebo_sharpes(fr, sig[name], m.to_numpy() if hasattr(m, "to_numpy") else np.asarray(m),
                                      args.placebo, financing=fin)
                      if args.placebo and w_label == "全期間" and name != "trend" else np.array([]))
                pb = float(np.mean(pl < sf)) if len(pl) else np.nan
                in_mkt = float((sig[name][m] != 0).mean())
                if fin == FINANCING and w_label == "全期間":
                    results[name] = {"s": sf, "s1": s1, "s2": s2, "psr": ps, "pct": pb}
                span = f"{w_label} {r.index[0]:%Y-%m}〜{r.index[-1]:%Y-%m}" if w_label == "全期間" else w_label
                say(f"| {name} | {span} | {f2(sf)} | {f2(s1)} | {f2(s2)} | {pct(ps)} | "
                    f"{pct(pb)} | {pct(float(r.mean() * 252), 1)} | {pct(mdd(r), 1)} | {pct(in_mkt)} | {res['lev'][m].mean():.2f} |")
            res.assign(pos=sig[name]).to_csv(out / f"{name}_{'cfd' if fin else 'futures'}.csv")

    say("\n## 判定\n")
    c, t, k = results.get("carry"), results.get("trend"), results.get("combo")
    verdict = {}
    if c:
        ok = c["s"] > 0.3 and c["s1"] > 0 and c["s2"] > 0 and c["psr"] >= 0.95 and c["pct"] >= 0.95
        verdict["carry"] = "**有望**" if ok else "有望ではない"
        say(f"- carry: シャープ {f2(c['s'])}（前半 {f2(c['s1'])}・後半 {f2(c['s2'])}）、PSR {pct(c['psr'])}、偶然より良い割合 {pct(c['pct'])}"
            f" → {verdict['carry']}（条件: 0.3 超、前半・後半ともプラス、PSR 95%、偶然 95%）")
    if k and t:
        ok = (k["s"] > 0.3 and k["s1"] > 0 and k["s2"] > 0 and k["psr"] >= 0.95 and k["pct"] >= 0.95
              and k["s1"] > t["s1"] and k["s2"] > t["s2"])
        verdict["combo"] = "**有望**" if ok else "有望ではない"
        say(f"- combo: シャープ {f2(k['s'])}（前半 {f2(k['s1'])}・後半 {f2(k['s2'])}）、trend は {f2(t['s'])}（{f2(t['s1'])}・{f2(t['s2'])}）"
            f" → {verdict['combo']}（条件: carry と同じ、かつ前半・後半とも trend より良い）")
    if any(v.startswith("**") for v in verdict.values()):
        say("- 次の段階: 今のタートルの WTI・ブレントに「キャリーの向きと揃った時だけ入る」絞り込みを足す検証を、別に事前登録してから行う")
    else:
        say("- キャリーは閉じる（論文で一番強い要因も、この市場・この期間では使えなかった）")

    say("\n## 読み方・限界")
    say("- 限月の切り替えは EIA の限月 1 の系列から見つけた。見つけ方の割合が低い場合は、規則（翌月 25 日の 3 営業日前）で補っている")
    say("- CFD の保有コストは年 3% の目安。フィリップの WTI CFD の実際の金利調整額とは違いうる")
    say("- ブレントは限月の履歴が無いので対象外。金・銀のキャリーは金利そのもので対象外")

    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {out}/report.md")


if __name__ == "__main__":
    main()
