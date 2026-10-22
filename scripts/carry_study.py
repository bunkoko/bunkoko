"""キャリー（原油の期間構造）を確かめる（./cfd carry）。

    python scripts/carry_study.py --fetch   # EIA から WTI の限月 1〜4 を取る（Mac で。クラウドからは届かない）
    python scripts/carry_study.py           # data/carry/wti_curve.csv で計算と判定
    python scripts/carry_study.py --paper   # 記録だけの売買（8 章。Yahoo の限月ごとの WTI。月に 1 回）

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

import importlib.util  # noqa: E402

from cfdbot.carry import (COST, CURVES, FINANCING, MAX_LEV, ROLL_LEAD, VOL_TARGET, basis, carry_gate, curve_path,  # noqa: E402
                          evaluate, fetch_curve, front_returns, load_curve, placebo_sharpes, sharpe, signals)
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


GATE_END = pd.Timestamp("2024-05-01", tz="UTC")       # EIA のデータの終わり（2024-04-05）の後は見ない


def _trend_module():
    spec = importlib.util.spec_from_file_location("trend_study", ROOT / "scripts" / "trend_study.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["trend_study"] = mod
    spec.loader.exec_module(mod)
    return mod


def gate_stage(args, root: Path) -> None:
    """docs/carry.md 7 章: タートルの WTI・ブレント（期間C は天然ガス）に、キャリーの向きと揃った新規だけ入る絞り込み。"""
    from dataclasses import replace

    from cfdbot.backtest import Sleeve, run_backtest
    from cfdbot.strategies import make_strategy
    from cfdbot.study import load_picks
    from cfdbot.train.config import load_train_config
    from cfdbot.universe import BY_KEY, day_returns

    ts = _trend_module()
    cfg = load_train_config(args.config)
    pk0 = load_picks(args.final)[0]
    data_root = Path(args.data)
    long_start = pd.Timestamp("2001-01-01", tz="UTC")
    split = pd.Timestamp("2021-06-01", tz="UTC")
    b_wti = basis(load_curve(root, "wti"))
    b_gas = basis(load_curve(root, "natgas")) if curve_path(root, "natgas").exists() else None

    periods = []
    a = ts.period_from_yahoo("A", f"期間A 先物 {long_start:%Y-%m}〜{split - pd.Timedelta(days=1):%Y-%m}", ts.CORE, data_root,
                             cfg, args.equity, long_start, split, "2000-01-01")
    if a is None:
        raise SystemExit("期間A の日足が足りない（data/universe/。./cfd universe --fetch）")
    periods.append((a, {"WTI": b_wti, "BRENT": b_wti}))
    b = ts.period_from_phillip(cfg, args.equity, split)
    if b is not None:
        b.frames = {k: f[f.index < GATE_END] for k, f in b.frames.items()}
        b.label = f"期間B フィリップ {split:%Y-%m}〜2024-04（EIA のデータの終わりまで）"
        periods.append((b, {"WTI": b_wti, "BRENT": b_wti}))
    if b_gas is not None:
        c = ts.period_from_yahoo("C", "期間C 天然ガス 2001-01〜2024-04（自分のキャリー）", ("NATGAS",), data_root, cfg,
                                 args.equity, long_start, GATE_END, "2000-01-01")
        if c is not None:
            periods.append((c, {"NATGAS": b_gas}))

    def run(period, gates):
        sleeves = [Sleeve(s, make_strategy(pk0.strategy, **pk0.params), dict(pk0.exit), entry_gate=gates.get(s))
                   for s in period.frames]
        bt = replace(period.bt, risk=replace(period.bt.risk, max_positions_per_symbol=1))
        res = run_backtest(period.frames, period.instruments, sleeves, bt)
        return day_returns(res.equity, period.start), int(len(res.trades))

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    say("# タートルにキャリーの絞り込みを足す（docs/carry.md 7 章）")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　判定の条件: 7 章（結果を見る前に決めたもの）。試した数: 40 ＋ 1 ＝ 41")
    say("絞り込み: 前日の EIA のキャリーがプラスなら買いだけ、マイナスなら売りだけ（WTI・ブレントは WTI の、天然ガスは自分のキャリー）。"
        "値が無い日は止めない。手仕舞いは今のまま")
    if b is None:
        say("- 期間B（フィリップのデータ）はこの環境に無い。判定は Mac で `./cfd carry --gate` を実行して完成させる")
    if b_gas is None:
        say("- 天然ガスの限月（EIA `RNGC1`〜`RNGC4`）が無いので期間C を省いた（`./cfd carry --fetch` で取る）")

    say("\n| 期間 | 市場 | 基準 シャープ | 絞り込み | 差 | 差の PSR | 偶然より良い割合 | 取引 基準 → 絞り込み | 止めた足 |")
    say("|---|---|---|---|---|---|---|---|---|")
    rows = {}
    rng = np.random.default_rng(23)
    for per, bmap in periods:
        gates = {s: carry_gate(per.frames[s].index, bmap[s]) for s in per.frames if s in bmap}
        r0, n0 = run(per, {})
        r1, n1 = run(per, gates)
        d = (r1 - r0).dropna()
        s0, s1 = sharpe(r0), sharpe(r1)
        ps = psr_of(d.to_numpy()) if len(d) > 60 else np.nan
        blocked = int(sum((~g["long"] | ~g["short"]).sum() for g in gates.values()))
        null = []
        for _ in range(args.gate_placebo if per.name == "A" else 0):
            sh = {}
            for s_, g in gates.items():
                k = int(rng.integers(1, len(g)))
                sh[s_] = pd.DataFrame({c_: np.roll(g[c_].to_numpy(), k) for c_ in g.columns}, index=g.index)
            null.append(sharpe(run(per, sh)[0]) - s0)
        pb = float(np.mean(np.array(null) < (s1 - s0))) if null else np.nan
        rows[per.name] = {"s0": s0, "s1": s1, "diff": s1 - s0, "psr": ps, "pct": pb}
        say(f"| {per.label} | {', '.join(BY_KEY[s].label for s in per.frames)} | {f2(s0)} | {f2(s1)} | {s1 - s0:+.2f} | "
            f"{pct(ps)} | {pct(pb)} | {n0} → {n1} | {blocked} |")
        pd.DataFrame({"base": r0, "gate": r1}).to_csv(out / f"gate_returns_{per.name}.csv")

    say("\n## 判定\n")
    have = list(rows)
    better = all(rows[k]["diff"] > 0 for k in have)
    psr_a = rows["A"]["psr"]
    if b is None or "C" not in rows:
        missing = "・".join(k for k, ok in (("B", b is not None), ("C", "C" in rows)) if not ok)
        if rows["A"]["diff"] > 0 and psr_a >= 0.95 and better:
            v = f"途中（期間{missing} 待ち。そろっている期間は条件を満たす）"
        elif rows["A"]["diff"] <= 0 or psr_a < 0.95:
            v = "採らない（期間A で条件に届かない）"
        else:
            v = "採らない（そろっている期間のどれかで悪い）"
    elif better and psr_a >= 0.95:
        v = "**採用候補**（EA に入れる設計を別に事前登録する）"
    elif sum(rows[k]["diff"] > 0 for k in have) >= 2:
        v = "参考（3 期間のうち 2 つで良い。採らない）"
    else:
        v = "採らない"
    say(f"- 期間A: 差 {rows['A']['diff']:+.2f}、差の PSR {pct(psr_a)}"
        + "".join(f"。期間{k}: 差 {rows[k]['diff']:+.2f}" for k in have if k != "A"))
    say(f"- 判定: {v}（条件: 3 期間すべてで基準より良く、期間A で差の PSR 95% 以上）")
    say("\n## 読み方・限界")
    say("- 期間A の WTI は Yahoo の日足の都合で 2020-04-16 まで（docs/review.md のデータの注意）")
    say("- ブレントを WTI のキャリーで代用している。本番でも同じ代用になる")
    say("- 期間B は 2024-04 までの約 3 年。EIA のデータの終わり以降は、上場中の限月から毎日ためる必要がある")
    (out / "gate.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {out}/gate.md")


def paper_stage(args, getter=None, now: pd.Timestamp | None = None, sleep=None) -> Path:
    """docs/carry.md 8 章: Yahoo の限月ごとの WTI で、月末のキャリーの向きを記録し、途中経過を出す。お金は動かさない。"""
    import time

    from cfdbot import carry_paper as P

    st = P.Store(args.root)
    now = now or pd.Timestamp.now(tz="Asia/Tokyo").tz_localize(None)
    since = pd.Timestamp(P.START) - pd.Timedelta(days=P.HISTORY_DAYS)
    tickers = P.needed(since, now + pd.Timedelta(days=40))
    if not args.no_fetch:
        if getter is None:
            from cfdbot.context import fetch_yahoo as getter
        wait = sleep or time.sleep
        for i, t in enumerate(tickers + [P.FRONT]):
            if i:
                wait(1.0)                                            # 1 秒に 1 件
            try:
                st.save(t, getter(t, f"{since:%Y-%m-%d}"))
            except Exception as e:  # noqa: BLE001
                have = len(st.load_close(t))
                print(f"  ⚠ {t}: 取れなかった（{str(e)[:100]}）。手元の {have} 日分で続ける", flush=True)
    closes = {t: s for t in tickers if len(s := st.load_close(t))}
    if not closes:
        raise SystemExit("限月の値段が 1 つも無い（ネットにつないで ./cfd carry --paper）")
    r = P.run(closes, st.load_close(P.FRONT), now)
    added = st.append(r["decisions"], now)
    log = st.load_log()
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    m = r["monthly"]
    say("# キャリーの記録だけの売買（C4）")
    say("")
    say(f"作成: {now:%Y-%m-%d %H:%M}　決まり: docs/carry.md 8 章（結果を見る前に決めたもの）。お金は動かしていない")
    say(f"- 毎月の最後の営業日の終値で キャリー ＝ ln(限月 1 ÷ 限月 2) を計算し、プラスなら翌月は買い・マイナスなら売り。{P.START} の終値から")
    say(f"- 値段: Yahoo の限月ごとの WTI（{tickers[0]}〜{tickers[-1]}）。今回 {added} か月分を記録に書き足した（{st.log_path}）")
    say("")
    say("## 1. 途中経過と判定（2026-11 から）")
    say("")
    say("| 形 | 月の数 | 合計 | 月ごとの t 値 | 年率 |")
    say("|---|---|---|---|---|")
    for key, label in (("cfd", "キャリーの向き（CFD のコスト。判定に使う）"), ("futures", "参考: 同じ向き・先物のコスト"),
                       ("long", "参考: いつも買い（CFD のコスト）")):
        x = m[key]
        say(f"| {label} | {len(x)} | {pct(float(x.sum()), 1) if len(x) else '–'} | {f2(P.t_value(x))} | "
            f"{pct(float(x.mean() * 12), 1) if len(x) else '–'} |")
    say("")
    looks = [k for k in P.LOOKS if len(m["cfd"]) >= k]
    for k in looks:
        say(f"- {k} か月の時点: **{P.verdict(m['cfd'].iloc[:k])}**")
    if not looks:
        say(f"- 判定: **{P.verdict(m['cfd'])}**（判定は {'・'.join(str(k) for k in P.LOOKS)} か月の時点だけ。間の数字を見て何も変えない）")
    say("")
    say("## 2. 月ごとの向き")
    say("")
    say("| 月末 | 限月 1 | 限月 2 | 終値 1 | 終値 2 | キャリー | 翌月の向き | 翌月の損益（CFD） | 記録 |")
    say("|---|---|---|---|---|---|---|---|---|")
    pnl = {p.to_period("M"): v for p, v in m["cfd"].items()}
    for row in log.itertuples():
        d = pd.Timestamp(row.month_end)
        nxt = pnl.get((d + pd.offsets.MonthBegin(1)).to_period("M"))
        word = {1: "買い", -1: "売り", 0: "なし"}.get(int(row.position), "?")
        say(f"| {d:%Y-%m-%d} | {row.c1} | {row.c2} | {float(row.c1_close):.2f} | {float(row.c2_close):.2f} | {float(row.carry):+.2%} | {word} | "
            f"{pct(nxt, 2) if nxt is not None else 'まだ'} | {row.logged_at}{'（' + row.late + '）' if isinstance(row.late, str) and row.late else ''} |")
    if log.empty:
        say("| まだ無い（10 月が終わった後に動かすと、2026-10-30 の向きが入る） | | | | | | | | |")
    say("")
    say("## 3. データの点検")
    say("")
    c = r["curve"]
    last = c.index[-1]
    front = st.load_close(P.FRONT)
    fc = front.get(last, np.nan) if len(front) else np.nan
    say(f"- 最後の日 {last:%Y-%m-%d}: 限月 1 {c.at[last, 't1']} {f2(c.at[last, 'c1'])}・限月 2 {c.at[last, 't2']} {f2(c.at[last, 'c2'])}、"
        f"{P.FRONT} {f2(fc)}（限月 1 と同じ限月なら、ほぼ同じ値になる）")
    gaps = c[(c.index > pd.Timestamp(P.START)) & (c["c1"].isna() | c["c2"].isna())]
    say(f"- 始まりの後で、限月 1・2 のどちらかの値が無い日: {len(gaps)} 日" + (f"（{', '.join(f'{d:%m-%d}' for d in gaps.index[:10])}…）" if len(gaps) else ""))
    say("")
    say("## 4. 読み方")
    say("")
    say("- 6 章のシャープ 0.34 は月ごとにすると 0.10。過去と同じ効きでも、t 値は 12 か月で 0.34、36 か月で 0.59 ほど。3 年でもほぼ「まだ分からない」")
    say("- この記録は「逆向きに変わっていないか」の見張り。お金を入れる判断は、これだけではしない")
    out = Path(args.out) / "paper" / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    path = out / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("")
    print(f"保存先: {path}")
    return path


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default="data/carry")
    p.add_argument("--fetch", action="store_true", help="EIA から取る（初回・更新）")
    p.add_argument("--start", default="1986-01-01")
    p.add_argument("--placebo", type=int, default=200)
    p.add_argument("--out", default="output/carry")
    p.add_argument("--gate", action="store_true", help="7 章: タートルにキャリーの絞り込みを足す検証（3 期間）")
    p.add_argument("--data", default="data/universe", help="先物の日足（./cfd universe と同じ）")
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--final", default="config/baselines/turtle55_d1_ext.json")
    p.add_argument("--equity", type=float, default=10_000_000)
    p.add_argument("--gate-placebo", type=int, default=30)
    p.add_argument("--paper", action="store_true", help="8 章: Yahoo の限月で、月末のキャリーの向きを記録して途中経過を出す")
    p.add_argument("--no-fetch", action="store_true", help="--paper: 値段を取り直さない")
    args = p.parse_args(argv)
    if args.paper:
        paper_stage(args)
        return

    root = Path(args.root)
    if args.fetch or not curve_path(root).exists():
        for name, codes in CURVES.items():
            print(f"取得中: EIA の {name} 先物 限月 1〜4（日次）…", flush=True)
            try:
                curve = fetch_curve(codes=codes)
            except Exception as e:  # noqa: BLE001
                if name == "wti":
                    raise SystemExit(f"EIA から取れなかった（{str(e)[:160]}）。クラウドの環境からは届かないので Mac で "
                                     "./cfd carry --fetch") from None
                print(f"  ✗ {name}: {str(e)[:120]}（期間C は省く）")
                continue
            root.mkdir(parents=True, exist_ok=True)
            curve.to_csv(curve_path(root, name))
            print(f"  ✓ {curve.index[0]:%Y-%m-%d}〜{curve.index[-1]:%Y-%m-%d}（{len(curve):,} 日）→ {curve_path(root, name)}")
    curve = load_curve(root)
    if args.gate:
        gate_stage(args, root)
        return
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
