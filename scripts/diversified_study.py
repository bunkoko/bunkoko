"""市場を分散した論文どおりのトレンドフォロー（TSMOM）を確かめる（./cfd diversify）。

    python scripts/diversified_study.py     # data/universe の日足（無い市場は Yahoo から取る）

作り方と判定の条件は docs/diversified.md（結果を見る前に決めたもの）。
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
from cfdbot.diversified import (EXTRA_MARKETS, MIN_HISTORY_DAYS, STOCK_GROUPS, placebo_sharpes,  # noqa: E402
                                portfolio_returns, positions, sharpe)
from cfdbot.spread import load_close  # noqa: E402
from cfdbot.stats import psr_of  # noqa: E402
from cfdbot.study import clean  # noqa: E402
from cfdbot.universe import GROUPS, UNIVERSE, data_path  # noqa: E402

PRIOR_TRIALS, NEW_TRIALS = 37, 1
CORE = ("GOLD", "SILVER", "WTI", "BRENT")


def f2(x: float) -> str:
    return f"{x:.2f}" if np.isfinite(x) else "–"


def pct(x: float, digits: int = 0) -> str:
    return f"{x:.{digits}%}" if np.isfinite(x) else "–"


def stats(r: pd.Series) -> dict:
    eq = r.cumsum()
    return {"sharpe": sharpe(r), "ann": float(r.mean() * 252), "vol": float(r.std() * np.sqrt(252)),
            "mdd": float((eq - eq.cummax()).min()) if len(eq) else np.nan}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data/universe")
    p.add_argument("--since", default="2000-01-01")
    p.add_argument("--start", default="2001-01-01")
    p.add_argument("--split", default="2014-01-01")
    p.add_argument("--placebo", type=int, default=200)
    p.add_argument("--out", default="output/diversify")
    args = p.parse_args()

    root = Path(args.data)
    markets = [m for m in UNIVERSE if m.group not in STOCK_GROUPS] + list(EXTRA_MARKETS)
    closes, groups, labels, skipped = {}, {}, {}, []
    for m in markets:
        path = data_path(root, m)
        if not path.exists():
            root.mkdir(parents=True, exist_ok=True)
            try:
                fetch_yahoo(m.ticker, args.since).to_csv(path, index=False)
            except Exception as e:  # noqa: BLE001
                skipped.append(f"{m.label}: 取れない（{str(e)[:60]}）")
                continue
        s = load_close(path)
        if len(s) < MIN_HISTORY_DAYS:
            skipped.append(f"{m.label}: {len(s)} 日しか無い")
            continue
        closes[m.key], groups[m.key], labels[m.key] = s, m.group, m.label
    close = pd.DataFrame(closes).sort_index()
    close = close[close.index >= pd.Timestamp(clean(args.start), tz="UTC") - pd.Timedelta(days=400)]
    close = close.ffill(limit=5)                                     # 市場ごとの休日は前日の値で埋める（5 日まで）
    start = pd.Timestamp(clean(args.start), tz="UTC")
    split = pd.Timestamp(clean(args.split), tz="UTC")

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    say("# 市場を分散した論文どおりのトレンドフォロー（TSMOM）")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　判定の条件: docs/diversified.md 3 章（結果を見る前に決めたもの）")
    say(f"作り方: 1・3・12 か月の値動きの符号の平均 × 0.40 ÷ 年率の変動（60 日）、全市場 1/N、月末に見直し。"
        f"コストはクラスごと（docs/universe.md）。試した数: {PRIOR_TRIALS} ＋ {NEW_TRIALS} ＝ {PRIOR_TRIALS + NEW_TRIALS}")
    say(f"市場: {len(close.columns)}（" + "、".join(f"{GROUPS[g].label} {sum(1 for k in close if groups[k] == g)}"
                                              for g in ("core", "commodity", "fx", "index", "bond", "crypto")
                                              if any(groups[k] == g for k in close)) + "）"
        + (f"。省いた: {'; '.join(skipped)}" if skipped else ""))

    def run(keys, name) -> tuple[pd.Series, pd.DataFrame, pd.DataFrame]:
        c = close[list(keys)]
        pos = positions(c)
        res = portfolio_returns(c, pos, {k: groups[k] for k in keys}, financing="先物なら" not in name)
        m = res.index >= start
        return res["ret"][m], pos[m], res[m]

    non_crypto = [k for k in close if groups[k] != "crypto"]
    sets = {
        "判定: 暗号資産を除く全市場": non_crypto,
        "参考: 暗号資産を含む全市場": list(close.columns),
        "参考: 今の 4 商品だけ（同じ作り方）": [k for k in CORE if k in close],
        "参考: 先物なら（保有コストなし）全市場": non_crypto,
        "参考: 先物なら（保有コストなし）今の 4 商品": [k for k in CORE if k in close],
    }
    for g in ("core", "commodity", "fx", "index", "bond"):
        ks = [k for k in close if groups[k] == g]
        if ks and g != "core":
            sets[f"参考: {GROUPS[g].label}だけ"] = ks

    say("\n| まとめ方 | 市場 | シャープ 全期間 | 前半 | 後半 | 2009〜2019 | 年率 | 年率の変動 | 最大DD | PSR | 名目÷資金 平均 | 月の売買（名目÷資金） |")
    say("|---|---|---|---|---|---|---|---|---|---|---|---|")
    results = {}
    for name, keys in sets.items():
        if len(keys) < 2 and "4 商品" not in name:
            continue
        r, pos, res = run(keys, name)
        h1, h2 = r[r.index < split], r[r.index >= split]
        winter = r[(r.index >= "2009-01-01") & (r.index < "2020-01-01")]
        st = stats(r)
        results[name] = {"r": r, "s": st["sharpe"], "s1": sharpe(h1), "s2": sharpe(h2), "psr": psr_of(r.to_numpy()),
                         "keys": keys, "pos": pos}
        say(f"| {name} | {len(keys)} | {f2(st['sharpe'])} | {f2(sharpe(h1))} | {f2(sharpe(h2))} | {f2(sharpe(winter))} | "
            f"{pct(st['ann'], 1)} | {pct(st['vol'], 1)} | {pct(st['mdd'], 1)} | {pct(results[name]['psr'])} | "
            f"{res['gross'].mean():.2f} | {res['turnover'].resample('ME').sum().mean():.2f} |")
        r.to_csv(out / f"returns_{name.split(':')[0]}_{len(keys)}.csv")

    main_name = "判定: 暗号資産を除く全市場"
    main = results[main_name]
    say("")
    print("偶然との比較を計算中…", flush=True)
    keys = main["keys"]
    c = close[keys]
    pos_full = positions(c)
    mask = np.asarray(pos_full.index >= start)
    pl = placebo_sharpes(c, pos_full, {k: groups[k] for k in keys}, mask, args.placebo) if args.placebo else np.array([])
    pct_better = float(np.mean(pl < main["s"])) if len(pl) else np.nan
    say(f"- 偶然（市場ごとの向きの並びをずらしたもの {len(pl)} 回）のシャープ: 平均 {f2(float(pl.mean()) if len(pl) else np.nan)}、"
        f"本物が上回った割合 {pct(pct_better)}")

    # クラスごとの寄与（判定の組の中で、クラスの市場だけの損益の合計）
    say("\n| クラス | 市場 | 寄与（年率、資金に対して） | そのクラスだけのシャープ |")
    say("|---|---|---|---|")
    pos_m = main["pos"]
    r_all = (pos_m * close[keys].pipe(lambda x: (x.diff() / x.shift(1).abs())).reindex(pos_m.index).fillna(0.0))
    for g in ("core", "commodity", "fx", "index", "bond"):
        ks = [k for k in keys if groups[k] == g]
        if not ks:
            continue
        contrib = float(r_all[ks].sum(axis=1).mean() * 252)
        s_only = results.get(f"参考: {GROUPS[g].label}だけ", {}).get("s", np.nan)
        say(f"| {GROUPS[g].label} | {len(ks)} | {pct(contrib, 1)} | {f2(s_only)} |")

    ok = main["s"] >= 0.5 and main["s1"] > 0.3 and main["s2"] > 0.3 and main["psr"] >= 0.95 and pct_better >= 0.95
    say("\n## 判定\n")
    say(f"- 暗号資産を除く全市場: シャープ {f2(main['s'])}（前半 {f2(main['s1'])}・後半 {f2(main['s2'])}）、PSR {pct(main['psr'])}、"
        f"偶然より良い割合 {pct(pct_better)}")
    say(f"- **{'採用候補（口座を選ぶ段階に進む）' if ok else '条件に届かない。今の構成のまま'}**"
        "（条件: 全期間 0.5 以上、前半・後半とも 0.3 超、PSR 95% 以上、偶然より良い割合 95% 以上）")
    core = results.get("参考: 今の 4 商品だけ（同じ作り方）")
    if core:
        say(f"- 比べる相手: 今の 4 商品だけを同じ作り方で持つと {f2(core['s'])}（前半 {f2(core['s1'])}・後半 {f2(core['s2'])}）。"
            f"分散の効果 = {f2(main['s'] - core['s'])}")

    say("\n## 読み方・限界")
    say("- Yahoo の日足。先物はつなぎ足（限月の乗り換えで値が飛ぶ）。株価指数は現物の指数で、先物の金利差を含まない")
    say("- コストはクラスごとの目安。業者が決まったら、その業者の実測で確かめ直す")
    say("- 1/N・月次見直し・40% の変動目標は論文のまま。市場の数が増えると 1 市場の量は小さくなり、最小の取引単位に当たりうる（4 章）")
    say("- 2009〜2019 年は論文でもトレンドフォローが弱かった期間。この期間だけで判断しないが、この期間を含めて 0.5 以上かを見る")
    say("- 「先物なら」の行は、名目に対する保有コストを 0 にしたもの（先物は証拠金だけで、金利は価格に織り込まれている）。"
        "CFD と先物のどちらで売買するかの判断材料で、判定には使わない")

    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {out}/report.md")


if __name__ == "__main__":
    main()
