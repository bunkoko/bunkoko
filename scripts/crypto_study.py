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



# --------------------------------------------------------------------------- 他の通貨の過去の検証（docs/crypto.md 9.3）
def cmd_extra(args) -> None:
    """9.3: BTC・ETH 以外の 9 通貨に今の戦略を調整せずに当て、BTC・ETH の組に足すと良くなるかを見る。"""
    from cfdbot.crypto import EXTRA, EXTRA_COST, EXTRA_FROM, EXTRA_SPLIT, EXTRA_TRIALS, extra_judge
    from cfdbot.universe import BY_KEY, Group, Market

    root = Path(args.data)
    base_m = [BY_KEY["BTC"], BY_KEY["ETH"]]
    extra_m = [Market(v[0], v[3], "crypto", v[1]) for v in EXTRA.values()]
    count_from = {v[0]: pd.Timestamp(v[2], tz="UTC") for v in EXTRA.values()}
    t0 = pd.Timestamp(EXTRA_FROM, tz="UTC")
    for m in base_m + extra_m:
        path = data_path(root, m)
        if path.exists() and not args.fetch:
            continue
        root.mkdir(parents=True, exist_ok=True)
        print(f"取得中: {m.label}（{m.ticker}）", flush=True)
        try:
            fetch_yahoo(m.ticker, args.since).to_csv(path, index=False)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {m.label}: {str(e)[:120]}")

    pk0 = load_picks(args.final)[0]
    cfg = load_train_config(args.config)
    bt0 = _account_config(cfg, float(cfg.data.fx), [])
    bt0 = replace(bt0, initial_equity=args.equity, risk=replace(bt0.risk, max_drawdown_halt=1.0),
                  filters=replace(bt0.filters, max_spread_mult=float("inf"), extra_events=(), no_entry_after_fri_et=None))
    g_base = CRYPTO_VENUES["gmo"]
    g_extra = Group(f"GMOコイン 他の通貨（往復 {EXTRA_COST:.1%}）", EXTRA_COST, g_base.financing, g_base.margin, weekend=True)

    frames = {}
    for m in base_m + extra_m:
        path = data_path(root, m)
        f = yahoo_csv_frame(path, repair=True) if path.exists() else pd.DataFrame()
        if not f.empty:
            frames[m.key] = (m, f)
    if any(m.key not in frames for m in base_m):
        raise SystemExit("ビットコイン・イーサリアムの日足が無い（ネットにつないで ./cfd crypto extra --fetch）")
    lacking = [m.label for m in extra_m if m.key not in frames]

    def run(m, g, start):
        frame = frames[m.key][1]
        sleeve = Sleeve(m.key, make_strategy(pk0.strategy, **pk0.params), dict(pk0.exit))
        res = run_backtest({m.key: with_costs(frame, m, g)}, {m.key: market_instrument(m, g)}, [sleeve],
                           replace(bt0, trade_start=start))
        return day_returns(res.equity, start), len(res.trades)

    def ready(m, start):                     # 指標の準備（130 本）より前からは数えない
        first = frames[m.key][1].index[0]
        first = first.tz_localize("UTC") if first.tzinfo is None else first
        return max(start, first + pd.Timedelta(days=130))

    base, extra, ref_cost, ref_all, trades = {}, {}, {}, {}, {}
    for m in base_m:
        print(f"計算: {m.label}", flush=True)
        base[m.key], trades[m.key] = run(m, g_base, ready(m, t0))
    for m in extra_m:
        if m.key not in frames:
            continue
        print(f"計算: {m.label}", flush=True)
        st = ready(m, count_from[m.key])
        extra[m.key], trades[m.key] = run(m, g_extra, st)
        ref_cost[m.key], _ = run(m, g_base, st)
        ref_all[m.key], _ = run(m, g_extra, ready(m, t0))
    main = extra_judge(base, extra)
    r_cost = extra_judge(base, ref_cost)
    r_all = extra_judge(base, ref_all)

    lines: list[str] = []

    def say(t: str = "") -> None:
        print(t, flush=True)
        lines.append(t)

    def row(name: str, h: dict) -> str:
        return f"| {name} | {f2(h['first'])} | {f2(h['second'])} | {f2(h['full'])} | {pct(h['psr'])} | {f2(h['t'])} |"

    say("# 他の通貨（C3）の過去の検証")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　判定の条件: docs/crypto.md 9.3（結果を見る前に決めたもの）")
    say(f"戦略: {Path(args.final).name} の 1 つ目の設定を調整せずに当てる。Yahoo のドル建ての日足。{EXTRA_FROM} から数える"
        f"（新しい 6 通貨は GMO の扱いの始まった 2024-04-14 から）。前半 〜{EXTRA_SPLIT}・後半 {EXTRA_SPLIT}〜")
    say(f"コスト: BTC・ETH 往復 {g_base.cost:.1%}、他の通貨 往復 {EXTRA_COST:.1%}。保有はどれも {g_base.financing:.1%}/年")
    if lacking:
        say("")
        say(f"**⚠ 日足が取れなかった通貨: {'・'.join(lacking)}。そろってからもう一度（この結果は判定に使わない）**")
    say("")
    say("## 1. 判定")
    say("")
    say("| 組 | シャープ 前半 | 後半 | 全期間 | PSR | t 値 |")
    say("|---|---|---|---|---|---|")
    say(row("BTC・ETH の組", main["btc_eth"]))
    say(row("他の通貨の組", main["extra"]))
    say(row("BTC・ETH の組＋他の通貨の組（同じリスク）", main["both"]))
    say("")
    say(f"- (1) 他の通貨の組だけで前半・後半ともシャープ 0.3 超、かつ PSR 95% 以上: {'○' if main['c1'] else '×'}")
    say(f"- (2) BTC・ETH の組に足すと、前半・後半ともシャープが良くなる: {'○' if main['c2'] else '×'}")
    say(f"- (3) 他の通貨の組の t 値が、試した数 {EXTRA_TRIALS} の基準 {main['bar']:.2f} 以上: {'○' if main['c3'] else '×'}")
    verdict = main["verdict"] if not lacking else f"判定しない（通貨が欠けている。計算上は {main['verdict']}）"
    say(f"- **判定: {verdict}**")
    say("")
    say("## 2. 参考（判定しない）")
    say("")
    say("| 場合 | 他の通貨の組 前半 | 後半 | 全期間 | 足した組 前半 | 後半 | 全期間 |")
    say("|---|---|---|---|---|---|---|")
    for name, r in (("他の通貨も往復 0.1%", r_cost), ("扱いの始まる前も数える（新しい 6 通貨も 2019-02 から）", r_all)):
        say(f"| {name} | {f2(r['extra']['first'])} | {f2(r['extra']['second'])} | {f2(r['extra']['full'])} | "
            f"{f2(r['both']['first'])} | {f2(r['both']['second'])} | {f2(r['both']['full'])} |")
    say("")
    say("### 通貨ごと（数え始めから。持っているだけはコストなし）")
    say("")
    say("| 通貨 | 数え始め | 売買の回数 | シャープ 前半 | 後半 | 全期間 | 持っているだけ 全期間 |")
    say("|---|---|---|---|---|---|---|")
    cut = pd.Timestamp(EXTRA_SPLIT)
    for m in base_m + extra_m:
        r = base.get(m.key, extra.get(m.key))
        if r is None or not len(r):
            continue
        h = hold_returns(frames[m.key][1])
        h = h[h.index >= r.index[0]]
        say(f"| {m.label} | {r.index[0]:%Y-%m-%d} | {trades[m.key]} | {f2(sharpe(r[r.index < cut]))} | "
            f"{f2(sharpe(r[r.index >= cut]))} | {f2(sharpe(r))} | {f2(sharpe(h))} |")
    say("")
    say("## 3. 読み方・限界（docs/crypto.md 9.4）")
    say("")
    say("- 9 通貨は 2026 年の今も GMO が扱っている通貨（生存者）。扱いの始まった日から数えて「扱う前の値上がり」は除いたが、"
        "扱いをやめた通貨・値を失った通貨は最初から入っていない。過去の成績は良く見えやすい")
    say("- 新しい 6 通貨は約 2.5 年だけ。前半の他の通貨の組は 3 通貨だけ。暗号資産どうしは値動きが似ていて、通貨の数ほどは分散しない")
    say("- 値段は Yahoo のドル建て。GMO の円建ての板とは違う。他の通貨の板の売り買いの差は、先の記録（9.2）で測る")
    say(f"- 試した数: {EXTRA_TRIALS}（81 ＋ 事前登録済みの 1C・案 2 ＋ この検証）")
    out = Path(args.out) / "extra" / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    path = out / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("")
    print(f"保存先: {path}")

# --------------------------------------------------------------------------- 記録だけの売買（docs/crypto.md 6 章）
LABEL = "com.bunkoko.crypto"


def run_extra(store, equity: float) -> None:
    """他の通貨の記録（9.2）。BTC・ETH の記録の後に動かし、失敗しても BTC・ETH の記録と本番には広げない。"""
    from cfdbot.crypto import paper_extra_run

    try:
        info = paper_extra_run(store, equity=equity)
        print(f"他の通貨: {info['logged']} 件を記録 → {store.extra_log_path}")
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 他の通貨の記録に失敗（BTC・ETH の記録には影響なし）: {str(e)[:200]}")


def cmd_paper(args) -> None:
    from cfdbot.crypto import PaperStore, paper_extra_report, paper_report, paper_run

    store = PaperStore(args.root)
    if not args.report:
        paper_run(store, equity=args.equity)
        if not args.no_extra:
            run_extra(store, args.equity)
    lines, info = paper_report(store)
    lines += paper_extra_report(store)[0]
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
    if not args.no_paper and not args.offline:
        run_extra(paper, args.equity)                        # 他の通貨の記録（9.2。本番の後に。本番には使わない）


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
    q.add_argument("--no-extra", action="store_true", help="他の通貨（9 章）の記録を省く")
    x = sub.add_parser("extra", help="他の通貨の過去の検証（9.3）。Yahoo の日足")
    add_check_args(x)
    x.add_argument("--fetch", action="store_true", help="手元にあっても Yahoo から取り直す")
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
    {"paper": cmd_paper, "schedule": cmd_schedule, "live": cmd_live, "extra": cmd_extra}.get(args.cmd, cmd_check)(args)


if __name__ == "__main__":
    main()
