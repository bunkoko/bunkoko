"""トレンドフォローの改良（複数の期間・量の管理・出口）を 3 つの期間で確かめる（./cfd trend）。

    python scripts/trend_study.py            # data/universe の日足（無ければ Yahoo から取る）。期間B はフィリップのデータがある Mac で

今の構成（turtle55_d1_ext.json）を基準に、5 つの改良を同じ期間・同じコストで比べる。
試すもの・判定の条件は docs/trend.md（結果を見る前に決めたもの）。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot.backtest import BacktestConfig, Sleeve, run_backtest  # noqa: E402
from cfdbot.context import fetch_yahoo, yahoo_csv_frame  # noqa: E402
from cfdbot.instruments import Instrument  # noqa: E402
from cfdbot.stats import psr_of  # noqa: E402
from cfdbot.strategies import make_strategy  # noqa: E402
from cfdbot.study import clean, load_picks  # noqa: E402
from cfdbot.train.config import load_train_config  # noqa: E402
from cfdbot.train.dataset import load_dataset  # noqa: E402
from cfdbot.train.pipeline import _account_config, _eval_settings  # noqa: E402
from cfdbot.universe import BY_KEY, GROUPS, data_path, day_returns, market_instrument, sharpe, with_costs  # noqa: E402

CORE = ("GOLD", "SILVER", "WTI", "BRENT")
OTHER = ("COPPER", "PLATINUM", "NATGAS")          # 期間C: これまで使っていない市場（docs/cross_asset.md と同じ）
HORIZONS = ((20, 10), (55, 20), (120, 40))       # multi: 入る日数 / 手仕舞いの日数（docs/trend.md 1 章）
VOLCAP_FLOOR, VOLCAP_SHORT, VOLCAP_SCALE, VOLCAP_MIN_HISTORY = 0.5, 20, 60, 250
PRIOR_TRIALS, NEW_TRIALS = 28, 5
VARIANTS = {    # 名前 → (表示, 出口の上書き)。multi・volcap は出口ではなく組み立てが違う
    "base": ("基準（タートル 55/20＋行き過ぎフィルタ）", {}),
    "multi": ("multi: 20/10・55/20・120/40 を 3 分の 1 ずつ", {}),
    "volcap": ("volcap: かごの変動が高い時は新規の量を減らす", {}),
    "no_be": ("no_be: 建値への移動をやめる", {"breakeven_trigger_atr": 0.0}),
    "trail5": ("trail5: 追いかける損切りを 5 ATR に", {"trail_atr": 5.0}),
    "tp2r": ("tp2r: +2R で半分を利確", {"partial_tp_r": 2.0, "partial_fraction": 0.5}),
}


@dataclass
class Period:
    name: str
    label: str
    frames: dict[str, pd.DataFrame]
    instruments: dict[str, Instrument]
    bt: BacktestConfig
    start: pd.Timestamp


def f2(x: float) -> str:
    return f"{x:.2f}" if np.isfinite(x) else "–"


def pct(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}%}" if np.isfinite(x) else "–"


# --------------------------------------------------------------------------- 期間
def base_bt(cfg, equity: float, start: pd.Timestamp) -> BacktestConfig:
    bt = _account_config(cfg, float(cfg.data.fx), [])
    return replace(bt, initial_equity=equity, trade_start=start, risk=replace(bt.risk, max_drawdown_halt=1.0),
                   filters=replace(bt.filters, max_spread_mult=float("inf"), extra_events=()))


def period_from_yahoo(name: str, label: str, keys, root: Path, cfg, equity: float, start: pd.Timestamp,
                      end: pd.Timestamp | None, since: str) -> Period | None:
    frames, insts = {}, {}
    for k in keys:
        m = BY_KEY[k]
        p = data_path(root, m)
        if not p.exists():
            root.mkdir(parents=True, exist_ok=True)
            try:
                fetch_yahoo(m.ticker, since).to_csv(p, index=False)
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ {m.label}: {str(e)[:120]}")
                continue
        f = yahoo_csv_frame(p, repair=True)
        if end is not None:
            f = f[f.index < end]
        if f.empty or len(f) < 400:
            continue
        g = GROUPS[m.group]
        frames[k] = with_costs(f, m, g)
        insts[k] = market_instrument(m, g)
    if len(frames) < len(keys):
        return None
    return Period(name, label, frames, insts, base_bt(cfg, equity, start), start)


def period_from_phillip(cfg, equity: float, start: pd.Timestamp) -> Period | None:
    """期間B: フィリップの MT5 の日足（data/）。無ければ None（Mac でだけ計算できる）。"""
    try:
        ds = load_dataset(cfg.data)
    except FileNotFoundError:
        return None
    frames = {}
    for s in CORE:
        if s not in ds.symbols:
            continue
        f = ds.signal_frame(s, "D1")
        if f is not None and len(f) >= 300:
            frames[s] = f
    if len(frames) < 2:
        return None
    settings = _eval_settings(cfg, ds)
    fx = ds.fx if ds.fx is not None else float(cfg.data.fx)
    bt = _account_config(cfg, fx, settings.event_list())
    bt = replace(bt, initial_equity=equity, trade_start=start, risk=replace(bt.risk, max_drawdown_halt=1.0))
    insts = {s: v for s, v in settings.instrument_map().items() if s in frames}
    last = max(f.index[-1] for f in frames.values())
    return Period("B", f"期間B フィリップ {start:%Y-%m}〜{last:%Y-%m}", frames, insts, bt, start)


# --------------------------------------------------------------------------- 改良の組み立て
def volcap_gates(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """かご（同じリスクで持った全銘柄）の直近の変動が、それまでの中央値より高いとき、新規の量を減らす倍率。"""
    closes = pd.DataFrame({s: f["close"].astype(float) for s, f in frames.items()}).sort_index()
    r = closes.pct_change()
    scaled = r / r.rolling(VOLCAP_SCALE, min_periods=40).std().shift(1)
    basket = scaled.mean(axis=1, skipna=True)
    bv = basket.rolling(VOLCAP_SHORT, min_periods=15).std()
    med = bv.expanding(min_periods=VOLCAP_MIN_HISTORY).median()
    mult = (med / bv).clip(VOLCAP_FLOOR, 1.0).fillna(1.0)
    out = {}
    for s, f in frames.items():
        m = mult.reindex(f.index).ffill().fillna(1.0).to_numpy()
        out[s] = pd.DataFrame({"long_size": m, "short_size": m}, index=f.index)
    return out


def build_sleeves(name: str, period: Period, pk0) -> tuple[list[Sleeve], int]:
    """改良ごとの運用単位。戻り値の 2 つ目は 1 銘柄に同時に持てる数。"""
    exit_cfg = dict(pk0.exit) | VARIANTS[name][1]
    if name == "multi":
        sleeves = [Sleeve(s, make_strategy(pk0.strategy, **(pk0.params | {"entry_period": e, "exit_period": x})),
                          exit_cfg, name=f"{s}:dc{e}", risk_weight=1.0 / len(HORIZONS))
                   for s in period.frames for e, x in HORIZONS]
        return sleeves, len(HORIZONS)
    gates = volcap_gates(period.frames) if name == "volcap" else {}
    return [Sleeve(s, make_strategy(pk0.strategy, **pk0.params), exit_cfg, entry_gate=gates.get(s))
            for s in period.frames], 1


def run_variant(name: str, period: Period, pk0) -> tuple[pd.Series, int]:
    sleeves, per_symbol = build_sleeves(name, period, pk0)
    cfg = replace(period.bt, risk=replace(period.bt.risk, max_positions_per_symbol=per_symbol))
    res = run_backtest(period.frames, period.instruments, sleeves, cfg)
    return day_returns(res.equity, period.start), int(len(res.trades))


# --------------------------------------------------------------------------- 集計
def cagr_mdd(r: pd.Series) -> tuple[float, float]:
    r = r.dropna()
    if len(r) < 60:
        return float("nan"), float("nan")
    eq = (1 + r).cumprod()
    years = len(r) / 252
    cagr = eq.iloc[-1] ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0
    return float(cagr), float((eq / eq.cummax() - 1).min())


def halves(r: pd.Series) -> tuple[float, float]:
    r = r.dropna()
    if len(r) < 120:
        return float("nan"), float("nan")
    mid = len(r) // 2
    return sharpe(r.iloc[:mid]), sharpe(r.iloc[mid:])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--final", default="config/baselines/turtle55_d1_ext.json", help="基準の構成")
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--data", default="data/universe", help="先物の日足の置き場（./cfd universe と同じ）")
    p.add_argument("--since", default="2000-01-01")
    p.add_argument("--long-start", default="2001-01-01", help="期間A・C の開始日")
    p.add_argument("--split", default="2021-06-01", help="期間A の終わり・期間B の始まり")
    p.add_argument("--equity", type=float, default=10_000_000)
    p.add_argument("--out", default="output/trend")
    args = p.parse_args()

    cfg = load_train_config(args.config)
    pk0 = load_picks(args.final)[0]
    root = Path(args.data)
    long_start = pd.Timestamp(clean(args.long_start), tz="UTC")
    split = pd.Timestamp(clean(args.split), tz="UTC")

    periods = []
    a = period_from_yahoo("A", f"期間A 先物 {long_start:%Y-%m}〜{split - pd.Timedelta(days=1):%Y-%m}", CORE, root, cfg,
                          args.equity, long_start, split, args.since)
    if a is None:
        raise SystemExit("期間A の日足が足りない（data/universe/。./cfd universe --fetch）")
    periods.append(a)
    b = period_from_phillip(cfg, args.equity, split)
    if b is not None:
        periods.append(b)
    c = period_from_yahoo("C", f"期間C 他の商品 {long_start:%Y-%m}〜", OTHER, root, cfg, args.equity, long_start, None,
                          args.since)
    if c is not None:
        periods.append(c)

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    say("# トレンドフォローの改良を 3 つの期間で確かめる")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　判定の条件: docs/trend.md 3 章（結果を見る前に決めたもの）")
    say(f"基準: {Path(args.final).name}。資金 {args.equity:,.0f} 円・1 回の損失 {periods[0].bt.risk.risk_per_trade:.1%}。"
        f"試した数: {PRIOR_TRIALS} ＋ {NEW_TRIALS} ＝ {PRIOR_TRIALS + NEW_TRIALS}")
    if b is None:
        say("- 期間B（フィリップのデータ）はこの環境に無い。判定は Mac で `./cfd trend` を実行して完成させる")

    results: dict[str, dict[str, dict]] = {}   # 期間 → 改良 → 数字
    for per in periods:
        say(f"\n## {per.label}（{', '.join(BY_KEY[s].label if s in BY_KEY else s for s in per.frames)}）\n")
        say("| 改良 | シャープ | 前半 | 後半 | 年率 | 最大DD | 取引 | 基準との差 | 差の PSR |")
        say("|---|---|---|---|---|---|---|---|---|")
        rets = {}
        results[per.name] = {}
        for name, (label, _) in VARIANTS.items():
            print(f"計算: {per.name} {name}", flush=True)
            r, n = run_variant(name, per, pk0)
            rets[name] = r
            s, (h1, h2), (cg, dd) = sharpe(r), halves(r), cagr_mdd(r)
            d = (r - rets["base"]).dropna()
            diff = s - sharpe(rets["base"]) if name != "base" else float("nan")
            ps = psr_of(d.to_numpy()) if name != "base" and len(d) > 60 else float("nan")
            results[per.name][name] = {"sharpe": s, "diff": diff, "psr": ps, "trades": n}
            say(f"| {label} | {f2(s)} | {f2(h1)} | {f2(h2)} | {pct(cg)} | {pct(dd)} | {n} | "
                f"{'–' if name == 'base' else f'{diff:+.2f}'} | {pct(ps, 0)} |")
        pd.DataFrame(rets).to_csv(out / f"returns_{per.name}.csv")

    say("\n## 判定\n")
    say("採用候補 = 期間 A・B・C のすべてでシャープが基準より良く、かつ期間A の差の PSR 95% 以上。参考 = 3 期間のうち 2 つで良い")
    say("\n| 改良 | " + " | ".join(f"期間{per.name}" for per in periods) + " | 期間A の差の PSR | 判定 |")
    say("|---|" + "---|" * len(periods) + "---|---|")
    verdicts = {}
    for name, (label, _) in VARIANTS.items():
        if name == "base":
            continue
        better = {per.name: results[per.name][name]["diff"] > 0 for per in periods}
        ps = results["A"][name]["psr"]
        n_better = sum(better.values())
        if b is None:                                   # 期間B が無い: 期間A の条件だけで決まる所は決める
            if better["A"] and ps >= 0.95 and better.get("C", True):
                v = "途中（期間B 待ち。A・C は条件を満たす）"
            elif not better["A"] or ps < 0.95:
                v = "採らない（期間A で条件に届かない）" + ("。参考になりうる" if n_better >= 1 else "")
            else:
                v = "参考になりうる（期間B 待ち）"
        elif n_better == 3 and ps >= 0.95:
            v = "**採用候補**"
        elif n_better >= 2:
            v = "参考"
        else:
            v = "採らない"
        verdicts[name] = (v, ps)
        say(f"| {label} | " + " | ".join(f"{results[per.name][name]['diff']:+.2f}" for per in periods)
            + f" | {pct(ps, 0)} | {v} |")
    cands = [n for n, (v, _) in verdicts.items() if v == "**採用候補**"]
    if cands:
        order = sorted(cands, key=lambda n: -verdicts[n][1])
        say(f"\n- EA に入れる順番（期間A の差の PSR が高い順。デモの区切りで 1 回に 1 つ）: {', '.join(order)}")
    elif b is not None:
        say("\n- 採用候補は無い。今の構成のまま")

    say("\n## 読み方・限界")
    say("- 期間A・C のコストは目安（往復 0.08%・年 3%）。期間B はフィリップの実測スプレッド")
    say("- multi は取引回数が約 3 倍になる。期間B のコストで成り立つかは期間B の行で見る")
    say("- volcap は量を減らすだけなので、シャープが上がっても年率は下がりうる（最大DD と合わせて読む）")
    say("- 5 つを同じデータで試したので、1 つが偶然 95% に届く確率は 5 × 5% ≈ 23%。3 期間すべてで良いことも条件にして割り引く")

    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {out}/report.md")


if __name__ == "__main__":
    main()
