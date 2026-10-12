"""暗号資産を、国内の業者の実際の手数料の仕組みで確かめ直す（./cfd crypto）。

    python scripts/crypto_study.py     # data/universe の日足を使う（無い市場だけ Yahoo から取る）

ビットコイン・イーサリアムに今の戦略（turtle55_d1_ext.json）を調整せずに当て、業者ごとのコスト
（往復のコスト・建玉の保有コスト）で計算する。変えるのはコストだけで、やり方は docs/universe.md 5 章と同じ。
判定の条件は docs/crypto.md 3 章（結果を見る前に決めたもの）。
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
from cfdbot.context import fetch_yahoo, yahoo_csv_frame  # noqa: E402
from cfdbot.stats import moments, psr  # noqa: E402
from cfdbot.strategies import make_strategy  # noqa: E402
from cfdbot.study import clean, load_picks  # noqa: E402
from cfdbot.train.config import load_train_config  # noqa: E402
from cfdbot.train.pipeline import _account_config  # noqa: E402
from cfdbot.universe import (CRYPTO_JUDGED, CRYPTO_SPLIT, CRYPTO_VENUES, GROUPS, UNIVERSE, at_vol,  # noqa: E402
                             data_path, day_returns, group_portfolio, hold_returns, market_instrument, portfolio,
                             sharpe, with_costs)


def f2(x: float) -> str:
    return f"{x:.2f}" if np.isfinite(x) else "–"


def pct(x: float, digits: int = 0) -> str:
    return f"{x:.{digits}%}" if np.isfinite(x) else "–"


def add_check_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--final", default="config/baselines/turtle55_d1_ext.json", help="戦略（今の構成の 1 つ目の銘柄の設定を使う）")
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--data", default="data/universe", help="日足の置き場（./cfd universe と同じ）")
    p.add_argument("--since", default="2000-01-01", help="取得の開始日")
    p.add_argument("--start", default="2001-01-01", help="成績を数え始める日")
    p.add_argument("--equity", type=float, default=10_000_000, help="1 市場あたりの資金（円。率で比べるので結論は変わらない）")
    p.add_argument("--out", default="output/crypto")


def cmd_check(args) -> None:
    """3 章の確かめ直し: 業者ごとのコストで今の戦略を当てる。"""
    root = Path(args.data)
    markets = [m for m in UNIVERSE if m.group in ("core", "crypto")]
    for m in markets:
        path = data_path(root, m)
        if path.exists():
            continue
        root.mkdir(parents=True, exist_ok=True)
        print(f"取得中: {m.label}（{m.ticker}）", flush=True)
        try:
            fetch_yahoo(m.ticker, args.since).to_csv(path, index=False)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {m.label}: {str(e)[:120]}")

    pk0 = load_picks(args.final)[0]
    cfg = load_train_config(args.config)
    start = pd.Timestamp(clean(args.start), tz="UTC")
    bt = _account_config(cfg, float(cfg.data.fx), [])
    bt = replace(bt, initial_equity=args.equity, trade_start=start,
                 risk=replace(bt.risk, max_drawdown_halt=1.0),
                 filters=replace(bt.filters, max_spread_mult=float("inf"), extra_events=()))
    bt_weekend = replace(bt, filters=replace(bt.filters, no_entry_after_fri_et=None))   # 土日も動く市場

    def run_market(m, frame, g):
        sleeve = Sleeve(m.key, make_strategy(pk0.strategy, **pk0.params), dict(pk0.exit))
        return run_backtest({m.key: with_costs(frame, m, g)}, {m.key: market_instrument(m, g)}, [sleeve],
                            bt_weekend if g.weekend else bt)

    frames = {}
    for m in markets:
        path = data_path(root, m)
        if path.exists():
            frame = yahoo_csv_frame(path, repair=True)
            if not frame.empty:
                frames[m.key] = (m, frame)
    core = [k for k, (m, _) in frames.items() if m.group == "core"]
    crypto = [k for k, (m, _) in frames.items() if m.group == "crypto"]
    if not crypto or len(core) < 4:
        raise SystemExit("日足が足りない（ビットコイン・イーサリアムと今の 4 商品）。./cfd universe --fetch で取得する")

    core_r = {}
    for k in core:
        m, frame = frames[k]
        print(f"計算: {m.label}", flush=True)
        core_r[k] = day_returns(run_market(m, frame, GROUPS["core"]).equity, start)

    by_venue, trades = {}, {}
    for v, g in CRYPTO_VENUES.items():
        by_venue[v] = {}
        for k in crypto:
            m, frame = frames[k]
            print(f"計算: {m.label}（{g.label}）", flush=True)
            res = run_market(m, frame, g)
            by_venue[v][k] = day_returns(res.equity, start)
            if v == "gmo":
                trades[k] = len(res.trades)

    c_split = pd.Timestamp(CRYPTO_SPLIT)
    c_start = min(r.index[0] for r in by_venue["guide"].values())

    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    def halves(r: pd.Series) -> tuple[float, float, float, float]:
        r = r[r.index >= c_start].dropna()
        return (sharpe(r[r.index < c_split]), sharpe(r[r.index >= c_split]), sharpe(r), psr(*moments(r.to_numpy())))

    def row(name: str, r: pd.Series) -> str:
        r = r[r.index >= c_start]
        s1, s2, sf, ps = halves(r)
        cagr, mdd = at_vol(r)
        return f"| {name} | {f2(s1)} | {f2(s2)} | {f2(sf)} | {pct(ps)} | {pct(cagr, 1)} | {pct(mdd, 1)} |"

    say("# 暗号資産を実際の業者のコストで確かめ直す")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　判定の条件: docs/crypto.md 3 章（結果を見る前に決めたもの）")
    say(f"戦略: {Path(args.final).name} の 1 つ目の設定を調整せずに当てる。データ: Yahoo の日足、{c_start:%Y-%m}〜。"
        f"前半 〜{c_split - pd.Timedelta(days=1):%Y-%m}・後半 {c_split:%Y-%m}〜。土日も動くので、金曜〜週末の新規停止は使わない")
    say("年率・最大DD は年率の変動を 10% にそろえた場合（業者どうし・持っているだけと同じ条件で比べるため）")
    say("\n| 業者 | 往復コスト | 保有コスト（年率） |")
    say("|---|---|---|")
    for g in CRYPTO_VENUES.values():
        say(f"| {g.label} | {g.cost:.2%} | {g.financing:.2%} |")

    core_keys = {"core": core}
    say("\n## 1. 市場ごと")
    say("\n| 市場・業者 | シャープ 前半 | 後半 | 全期間 | PSR | 年率 | 最大DD |")
    say("|---|---|---|---|---|---|---|")
    for k in crypto:
        m, frame = frames[k]
        for v, g in CRYPTO_VENUES.items():
            say(row(f"{m.label}: {g.label}", by_venue[v][k]))
        say(row(f"{m.label}: 比べる相手 持っているだけ（コストなし）", hold_returns(frame)))
    say("\n売買の回数: " + "、".join(f"{frames[k][0].label} {trades[k]} 回" for k in crypto))

    say("\n## 2. まとめた成績と判定")
    say("\n| 業者 | まとめ方 | シャープ 前半 | 後半 | 全期間 | PSR | 年率 | 最大DD |")
    say("|---|---|---|---|---|---|---|---|")
    core_only = group_portfolio(pd.DataFrame(core_r).sort_index(), core_keys, ["core"])
    say("| – " + row("今の 4 商品だけ", core_only))
    c0 = halves(core_only)
    verdicts = {}
    for v, g in CRYPTO_VENUES.items():
        returns = pd.DataFrame({**core_r, **by_venue[v]}).sort_index()
        cls = portfolio(returns, crypto)
        both = group_portfolio(returns, {"core": core, "crypto": crypto}, ["core", "crypto"])
        say(f"| {g.label} " + row("暗号資産だけ", cls))
        say(f"| {g.label} " + row("今の 4 商品＋暗号資産", both))
        s1, s2, sf, ps = halves(cls)
        b1, b2, _, _ = halves(both)
        cls_ok = bool(np.isfinite(s1) and np.isfinite(s2) and s1 > 0.3 and s2 > 0.3 and ps >= 0.95)
        better = bool(b1 > c0[0] and b2 > c0[1])
        verdicts[v] = (cls_ok and better, sf, cls_ok, better)

    say("")
    for v in CRYPTO_JUDGED:
        ok, sf, cls_ok, better = verdicts[v]
        say(f"- {CRYPTO_VENUES[v].label}: **{'足す候補' if ok else '足さない'}**"
            f"（暗号資産だけで前半・後半ともシャープ 0.3 超かつ PSR 95% 以上: {'○' if cls_ok else '×'}、"
            f"今の 4 商品に足すと前半・後半とも良くなる: {'○' if better else '×'}）")
    cands = [v for v in CRYPTO_JUDGED if verdicts[v][0]]
    if cands:
        best = max(cands, key=lambda v: verdicts[v][1])
        say(f"- 選ぶ業者: **{CRYPTO_VENUES[best].label}**（「足す候補」の中で、暗号資産だけの全期間のシャープが一番高い）")
    else:
        say("- 選ぶ業者: **なし**（どの業者のコストでも条件に届かない。暗号資産は足さない）")

    say("\n## 読み方・限界")
    say("- 値段は Yahoo のドル建ての値（世界の取引所の値をまとめたもの）。国内の業者の円建ての値とは少し違う。"
        "日足のトレンドフォローでは差は小さいはず")
    say("- GMOコインの板の売り買いの差は、往復 0.1% と控えめに置いた。実際の差は、口座を開く前に業者の公開 API で測れる")
    say("- bitFlyer のファンディングレートは、いつも払う側として置いた（控えめ）。実際は受け取る日もある")
    say("- 試した数: docs/universe.md の 59 市場に、この 2 つ（GMOコイン・bitFlyer）が増えた。偶然の当たりの可能性"
        "（前回の DSR 55〜59%）は、コストを変えても消えない")

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {out}/report.md")


# --------------------------------------------------------------------------- 記録だけの売買（docs/crypto.md 6 章）
LABEL = "com.bunkoko.crypto"


def cmd_paper(args) -> None:
    from cfdbot.crypto import PaperStore, paper_report, paper_run

    store = PaperStore(args.root)
    if not args.report:
        paper_run(store, equity=args.equity)
    lines, info = paper_report(store)
    print("\n".join(lines))
    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_live(args) -> None:
    """docs/crypto.md 8 章: 記録の直後に、GMOコインの建玉を記録の向きにそろえる（既定は空運転）。"""
    from cfdbot.crypto import PaperStore, paper_run
    from cfdbot.crypto_live import GmoClient, LiveStore, live_run, load_keys

    paper = PaperStore(args.root)
    live = LiveStore(Path(args.root) / "live")
    if not args.no_paper:
        paper_run(paper, equity=args.equity)                 # まず記録（取得・向きの決定・追記）
    client = None
    if not args.offline:
        keys = load_keys()
        client = GmoClient(keys["api_key"], keys["api_secret"])
    rows = live_run(paper, live, client, real=args.real)
    mode = "本物" if args.real and client is not None else "空運転"
    print(f"{mode}: {len(rows)} 件を記録 → {live.log_path}")


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def make_plist(root: Path, log: Path, live: bool = False, real: bool = False) -> dict:
    args = [str(root / "cfd"), "crypto"] + ((["live"] + (["--real"] if real else [])) if live else ["paper"])
    return {"Label": LABEL, "ProgramArguments": args, "WorkingDirectory": str(root),
            "StartCalendarInterval": {"Hour": 6, "Minute": 30}, "StandardOutPath": str(log),
            "StandardErrorPath": str(log), "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"}}


def cmd_schedule(args) -> None:
    import os
    import plistlib
    import subprocess

    if sys.platform != "darwin":
        raise SystemExit("毎朝の記録の登録は Mac だけ（launchd）。他では cron などで毎朝 6:30 に ./cfd crypto paper を")
    uid, p = os.getuid(), plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    if args.off:
        p.unlink(missing_ok=True)
        print("毎朝の記録を外した")
        return
    log = ROOT / "data" / "crypto" / "paper" / "run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        plistlib.dump(make_plist(ROOT, log, live=args.live, real=args.real), f)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(p)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"launchctl に断られた: {r.stderr.strip()}")
    what = ("crypto live" + (" --real（本物の注文）" if args.real else "（空運転）")) if args.live else "crypto paper"
    print(f"毎朝 6:30 に ./cfd {what} を動かすよう登録した（記録: {log}）。Mac が眠っていると動かないので、"
          "電源につないで docs/operations.md 10 章の設定にしておく")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("check", help="業者ごとのコストで今の戦略を当てる（3 章）")
    add_check_args(c)
    add_check_args(p)                                    # サブコマンド無しは check
    q = sub.add_parser("paper", help="記録だけの売買（6 章）。毎朝 1 回。--report で途中経過だけ")
    q.add_argument("--root", default="data/crypto")
    q.add_argument("--equity", type=float, default=1_000_000, help="記録上の資金（円）")
    q.add_argument("--out", default="output/crypto/paper")
    q.add_argument("--report", action="store_true", help="取りに行かず、記録から成績だけ出す")
    sch = sub.add_parser("schedule", help="毎朝 6:30 の記録（--live で少額の本番）を launchd に登録する（Mac）")
    sch.add_argument("--off", action="store_true")
    sch.add_argument("--live", action="store_true", help="paper の代わりに live を動かす")
    sch.add_argument("--real", action="store_true", help="--live と一緒に: 本物の注文を出す")
    lv = sub.add_parser("live", help="少額の本番（8 章）。記録の直後に建玉を記録の向きにそろえる。既定は空運転")
    lv.add_argument("--root", default="data/crypto")
    lv.add_argument("--equity", type=float, default=1_000_000, help="記録上の資金（円）")
    lv.add_argument("--real", action="store_true", help="本物の注文を出す（無ければ空運転）")
    lv.add_argument("--no-paper", action="store_true", help="記録の取得を省く（すでに今日の分がある時）")
    lv.add_argument("--offline", action="store_true", help="API に触らない（キー無しで仕組みを確かめる）")
    return p


def main() -> None:
    args = build_parser().parse_args()
    {"paper": cmd_paper, "schedule": cmd_schedule, "live": cmd_live}.get(args.cmd, cmd_check)(args)


if __name__ == "__main__":
    main()
