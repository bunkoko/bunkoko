"""売買の対象を商品以外（FX・株価指数・国債・個別株）に広げたら良くなるかを確かめる（./cfd universe）。

    python scripts/universe_study.py --fetch     # 初回: Yahoo から 57 市場の日足を data/universe/ に取得して検証
    python scripts/universe_study.py             # 取得済みのデータで検証だけ

今の戦略（turtle55_d1_ext.json）を、市場ごとに調整せずに当てる。結果を見て一番良かった市場を選ぶと
偶然の当たりを選びやすい（選択バイアス）ので、次の 3 つを確かめる（判定の条件は docs/universe.md）:
  1. 前半（〜2013）に良かった市場は、後半（2014〜）も良いか
  2. どの資産クラスで効くか（クラスごとにまとめた成績）
  3. 今の 4 商品に別の資産クラスを足すと、まとめた成績が良くなるか
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

from cfdbot.backtest import Sleeve, run_backtest  # noqa: E402
from cfdbot.context import fetch_yahoo, yahoo_csv_frame, yfinance_available  # noqa: E402
from cfdbot.stats import expected_max_z, moments, psr  # noqa: E402
from cfdbot.strategies import make_strategy  # noqa: E402
from cfdbot.study import clean, load_picks  # noqa: E402
from cfdbot.train.config import load_train_config  # noqa: E402
from cfdbot.train.pipeline import _account_config  # noqa: E402
from cfdbot.universe import (GROUPS, UNIVERSE, at_vol, data_path, day_returns, group_portfolio,  # noqa: E402
                             market_instrument, portfolio, selection_test, sharpe, with_costs)


def fetch(markets, root: Path, since: str, refresh: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if not yfinance_available():
        print("⚠ yfinance が入っていない（./cfd update で入る）。Yahoo に直接取りに行くので、断られることがある")
    fails = 0
    for m in markets:
        p = data_path(root, m)
        if p.exists() and not refresh:
            continue
        if fails >= 5:
            print(f"  – {m.label}（{m.ticker}）: Yahoo の失敗が続いたので省いた（時間をおいて --fetch をやり直す）")
            continue
        try:
            df = fetch_yahoo(m.ticker, since)
            df.to_csv(p, index=False)
            print(f"  ✓ {m.label}（{m.ticker}）: {df['date'].iloc[0]}〜{df['date'].iloc[-1]}（{len(df):,} 行）", flush=True)
            fails = 0
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ✗ {m.label}（{m.ticker}）: {str(e)[:120]}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--final", default="config/baselines/turtle55_d1_ext.json", help="戦略（今の構成の 1 つ目の銘柄の設定を使う）")
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--data", default="data/universe", help="日足の置き場")
    p.add_argument("--fetch", action="store_true", help="無い市場の日足を Yahoo から取得してから検証する")
    p.add_argument("--refresh", action="store_true", help="--fetch で、ある市場も取り直す")
    p.add_argument("--since", default="2000-01-01", help="取得の開始日（指標の準備に 1 年使う）")
    p.add_argument("--start", default="2001-01-01", help="成績を数え始める日")
    p.add_argument("--split", default="2014-01-01", help="前半と後半の境")
    p.add_argument("--equity", type=float, default=10_000_000, help="1 市場あたりの資金（円。率で比べるので結論は変わらない）")
    p.add_argument("--out", default="output/universe")
    args = p.parse_args()

    root = Path(args.data)
    if args.fetch or not root.exists():
        print("取得中: Yahoo Finance（57 市場。数分かかる）…", flush=True)
        fetch(UNIVERSE, root, args.since, args.refresh)

    pk0 = load_picks(args.final)[0]
    cfg = load_train_config(args.config)
    start = pd.Timestamp(clean(args.start), tz="UTC")
    split = pd.Timestamp(clean(args.split))
    bt = _account_config(cfg, float(cfg.data.fx), [])
    bt = replace(bt, initial_equity=args.equity, trade_start=start,
                 risk=replace(bt.risk, max_drawdown_halt=1.0),
                 filters=replace(bt.filters, max_spread_mult=float("inf"), extra_events=()))

    rets, info = {}, {}
    for m in UNIVERSE:
        path = data_path(root, m)
        if not path.exists():
            continue
        frame = yahoo_csv_frame(path, repair=True)
        if frame.empty or frame.index[-1] - max(frame.index[0], start) < pd.Timedelta(days=3 * 365):
            print(f"  – {m.label}: データが 3 年分に足りないので省く")
            continue
        print(f"計算: {m.label}", flush=True)
        sleeve = Sleeve(m.key, make_strategy(pk0.strategy, **pk0.params), dict(pk0.exit))
        res = run_backtest({m.key: with_costs(frame, m)}, {m.key: market_instrument(m)}, [sleeve], bt)
        r = day_returns(res.equity, start)
        if len(r) < 250:
            continue
        rets[m.key] = r
        t = res.trades
        info[m.key] = {"m": m, "first": r.index[0], "trades": len(t),
                       "avg_r": float(t["r_multiple"].mean()) if len(t) else np.nan}
    if len(rets) < 8:
        raise SystemExit(f"検証できる市場が {len(rets)} しかない。--fetch で日足を取得する（data/universe/）")

    returns = pd.DataFrame(rets).sort_index()
    h1, h2 = returns[returns.index < split], returns[returns.index >= split]
    sr_full = pd.Series({k: sharpe(returns[k]) for k in returns})
    sr1 = pd.Series({k: sharpe(h1[k]) for k in returns})
    sr2 = pd.Series({k: sharpe(h2[k]) for k in returns})
    keys = list(returns.columns)
    n_mk = len(keys)

    # 市場ごとの PSR と、市場の数で割り引いた DSR（市場の数だけ試したのと同じなので）
    mom = {k: moments(returns[k].dropna().to_numpy()) for k in keys}
    sr_d = np.array([mom[k][0] for k in keys])
    sr_star = float(np.nanstd(sr_d, ddof=1) * expected_max_z(n_mk))
    psr_v = {k: psr(*mom[k]) for k in keys}
    dsr_v = {k: psr(*mom[k], sr_star=sr_star) for k in keys}

    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    say(f"# 売買の対象を広げる検証（{Path(args.final).name} を市場ごとに調整せずに当てる）")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　判定の条件: docs/universe.md（結果を見る前に決めたもの）")
    say(f"データ: Yahoo の日足 {n_mk} 市場、{args.start}〜。前半 〜{split - pd.Timedelta(days=1):%Y-%m}、"
        f"後半 {split:%Y-%m}〜。コストは資産クラスごとの目安（下の表）")
    say("\n| 資産クラス | 市場の数 | 往復コスト | 保有コスト（年率） |")
    say("|---|---|---|---|")
    for g, spec in GROUPS.items():
        n = sum(1 for k in keys if info[k]["m"].group == g)
        say(f"| {spec.label} | {n} | {spec.cost:.2%} | {spec.financing:.0%} |")

    # ---------------- 1. 過去の成績で選ぶ意味があるか
    say("\n## 1. 前半に良かった市場は、後半も良いか")
    sel = selection_test(sr1, h2, sr2)
    if sel is None:
        say("比べられる市場が少なすぎる")
    else:
        say(f"前半と後半の両方にデータがある {sel.n} 市場で比べた。")
        say(f"- 前半と後半のシャープの順位相関: {sel.rho:+.2f}（順位を混ぜた偶然でこれ以上になる割合 {sel.rho_p:.0%}）")
        say(f"- 前半の上位 {sel.k} 市場（{', '.join(info[k]['m'].label for k in sel.picked)}）を後半にまとめて持つと"
            f" シャープ {sel.sharpe_picked:.2f}。全 {sel.n} 市場をまとめて持つと {sel.sharpe_all:.2f}。"
            f"でたらめに {sel.k} 市場を選んだ場合より良かった割合 {sel.pct_vs_random:.0%}")
        ok = sel.rho_p <= 0.05 and sel.pct_vs_random >= 0.95
        say(f"- 判定: **{'過去の成績で選ぶ意味がある' if ok else '過去の成績で選んでも、その後は当たらない'}**"
            "（条件: 順位相関の偶然の割合 5% 以下、かつ上位を選んだ場合がでたらめより良い割合 95% 以上）")

    # ---------------- 2. 資産クラスごと
    groups = {g: [k for k in keys if info[k]["m"].group == g] for g in GROUPS}
    say("\n## 2. 資産クラスごと（クラスの中の市場を同じリスクでまとめて持った場合）")
    say("年率・最大DD は、年率の変動を 10% にそろえた場合（クラスどうしを同じ条件で比べるため）")
    say("\n| 資産クラス | 市場 | シャープ 前半 | 後半 | 全期間 | PSR | 年率 | 最大DD | 市場ごとのシャープの平均 | 判定 |")
    say("|---|---|---|---|---|---|---|---|---|---|")
    works = {}
    for g, members in groups.items():
        if not members:
            continue
        pr = portfolio(returns, members)
        s1, s2, sf = sharpe(pr[pr.index < split]), sharpe(pr[pr.index >= split]), sharpe(pr)
        ps = psr(*moments(pr.to_numpy()))
        cagr, mdd = at_vol(pr)
        works[g] = bool(np.isfinite(s1) and np.isfinite(s2) and s1 > 0.3 and s2 > 0.3 and ps >= 0.95)
        say(f"| {GROUPS[g].label} | {len(members)} | {s1:.2f} | {s2:.2f} | {sf:.2f} | {ps:.0%} | {cagr:.1%} | {mdd:.1%} | "
            f"{np.nanmean([sr_full[k] for k in members]):.2f} | {'効いている' if works[g] else '–'} |")
    say("（判定「効いている」: 前半・後半ともシャープ 0.3 超、かつ PSR 95% 以上）")

    # ---------------- 3. 今の 4 商品に足す
    say("\n## 3. 今の 4 商品に、別の資産クラスを足した場合（クラスごとに同じリスク）")
    say("\n| 組み合わせ | シャープ 前半 | 後半 | 全期間 | 年率 | 最大DD | 判定 |")
    say("|---|---|---|---|---|---|---|")
    base = group_portfolio(returns, groups, ["core"])
    b1, b2 = sharpe(base[base.index < split]), sharpe(base[base.index >= split])
    combos = [("今の 4 商品だけ", ["core"])] + [(f"＋{GROUPS[g].label}", ["core", g]) for g in GROUPS
                                                if g != "core" and groups.get(g)]
    combos.append(("全部のクラス", [g for g in GROUPS if groups.get(g)]))
    candidates = []
    for name, names in combos:
        pr = group_portfolio(returns, groups, names)
        s1, s2, sf = sharpe(pr[pr.index < split]), sharpe(pr[pr.index >= split]), sharpe(pr)
        cagr, mdd = at_vol(pr)
        if names == ["core"]:
            v = "（基準）"
        elif len(names) == 2:
            v = "足す候補" if works.get(names[1]) and s1 > b1 and s2 > b2 else "–"
            if v == "足す候補":
                candidates.append(names[1])
        else:
            v = "–"
        say(f"| {name} | {s1:.2f} | {s2:.2f} | {sf:.2f} | {cagr:.1%} | {mdd:.1%} | {v} |")
    if len(candidates) >= 2:
        pr = group_portfolio(returns, groups, ["core"] + candidates)
        cagr, mdd = at_vol(pr)
        say(f"| ＋足す候補のすべて（{'・'.join(GROUPS[g].label for g in candidates)}） | "
            f"{sharpe(pr[pr.index < split]):.2f} | {sharpe(pr[pr.index >= split]):.2f} | {sharpe(pr):.2f} | "
            f"{cagr:.1%} | {mdd:.1%} | – |")
    say("（判定「足す候補」: 2 章で効いているクラスで、前半・後半とも今の 4 商品だけより良い）")
    say("足す候補: " + ("、".join(GROUPS[g].label for g in candidates) if candidates else "なし（今の 4 商品のまま）"))

    # ---------------- 4. 市場ごと
    say(f"\n## 4. 市場ごと（参考。{n_mk} 市場を試したので、一番良い市場も偶然の当たりの可能性が高い）")
    say(f"DSR: {n_mk} 市場の中で一番良く見える偶然を超えている確率。95% 以上なら、選び出しても偶然とは言いにくい")
    say("\n| 資産クラス | 市場 | データ | 取引 | 1 回あたりの R | シャープ 前半 | 後半 | 全期間 | PSR | DSR |")
    say("|---|---|---|---|---|---|---|---|---|---|")
    for g in GROUPS:
        for k in sorted(groups.get(g, []), key=lambda k: -np.nan_to_num(sr_full[k], nan=-9)):
            it = info[k]
            say(f"| {GROUPS[g].label} | {it['m'].label} | {it['first']:%Y}〜 | {it['trades']} | {it['avg_r']:+.2f} | "
                f"{sr1[k]:.2f} | {sr2[k]:.2f} | {sr_full[k]:.2f} | {psr_v[k]:.0%} | {dsr_v[k]:.0%} |")
    n_dsr = sum(1 for k in keys if dsr_v[k] >= 0.95)
    say(f"\nDSR 95% 以上の市場: {n_dsr} / {n_mk}")

    say("\n## 読み方・限界")
    say("- 実際に売買できるかは別の問題。今のフィリップの MT5 口座で売買できるのは金・銀・WTI・ブレント・プラチナ・ドル円だけ")
    say("- 個別株は「2001 年ごろの大型株」を選んだが、倒産・上場廃止した会社は Yahoo にデータが無く入れられない（生存者バイアス）。"
        "配当は入っていない")
    say("- 為替の金利差（スワップ）は入れず、年率 1% の保有コストで代えている。キャリーの大きい通貨では実際と差が出る")
    say("- 先物（商品・国債）はつなぎ足で、限月の乗り換えで値が飛ぶ。コストは資産クラスごとの目安で、証券会社によって違う")

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    returns.to_csv(out / "daily_returns.csv")
    print(f"\n保存先: {out}/report.md（この内容をそのまま送ってください）")


if __name__ == "__main__":
    main()
