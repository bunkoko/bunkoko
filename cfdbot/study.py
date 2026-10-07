"""研究用の検証（./cfd study・./cfd meta）で共通の、期間の組み立てと集計。

期間A: 先物の長期データ（Yahoo）。フィリップのデータが始まる前（既定 2001〜2021-05）
期間B: フィリップの MT5 のデータ（既定 2021-06〜）。本番と同じ条件
期間C: これまで使っていない市場（銅・プラチナ・天然ガスの先物、2001〜）。同じ戦略で確かめる
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from .backtest import BacktestConfig, Sleeve, run_backtest
from .context import FUTURES_FOR, SPECS, ContextStore, CSeries, from_bars, yahoo_frame
from .features import OTHER_MARKETS, PARTNER
from .instruments import Instrument
from .metrics import compute_metrics
from .strategies import make_strategy
from .train.config import TIMEFRAMES, load_train_config
from .train.dataset import load_dataset
from .train.pipeline import _account_config, _eval_settings, validate_window
from .train.portfolio import Pick


def clean(v: str) -> str:
    return v.strip().strip("「」『』\"'`、。　 ")


class Period:
    """1 つの期間のデータと、そこでのバックテスト。"""

    def __init__(self, name, label, frames, tfs, instruments, bt, start, end, resolve, runner):
        self.name, self.label = name, label
        self.frames, self.tfs = frames, tfs
        self.instruments, self.bt = instruments, bt
        self.start, self.end = start, end
        self.resolve: Callable[[str, str], CSeries | None] = resolve
        self._runner = runner

    @property
    def short(self) -> str:
        return self.label.split(" ")[0]

    def run(self, picks, gates=None, start=None):
        return self._runner(self, picks, gates or {}, start or self.start)


def run_frames(period: Period, picks, gates, start) -> dict:
    sleeves = [Sleeve(p.symbol, make_strategy(p.strategy, **p.params), dict(p.exit), risk_weight=p.multiplier,
                      entry_gate=gates.get(p.symbol)) for p in picks if p.symbol in period.frames]
    cfg = replace(period.bt, trade_start=start, trade_end=period.end,
                  risk=replace(period.bt.risk, max_drawdown_halt=1.0))
    res = run_backtest({s.symbol: period.frames[s.symbol] for s in sleeves}, period.instruments, sleeves, cfg)
    return {"equity": res.equity, "trades": res.trades, "signals": res.signals}


def summarize(res: dict, equity0: float, symbols) -> dict:
    m = compute_metrics(res["equity"], res["trades"], equity0)
    t = res["trades"]
    out = {"sharpe": m.get("sharpe", 0.0), "cagr": m.get("cagr", 0.0), "mdd": m.get("max_drawdown", 0.0),
           "trades": int(m.get("trades", 0)), "by_symbol": {}}
    for s in symbols:
        g = t[t["symbol"] == s] if not t.empty else t
        out["by_symbol"][s] = (len(g), float(g["r_multiple"].mean()) if len(g) else np.nan)
    eq = res["equity"]
    out["yearly"] = (eq.resample("YE").last() / eq.resample("YE").first() - 1) if len(eq) else pd.Series(dtype=float)
    return out


def shifted(gates: dict[str, pd.DataFrame], frac: float) -> dict[str, pd.DataFrame]:
    """絞り込みの並びを時間方向にずらす（止める割合・量の配分と続き方はそのまま、時期だけが合わなくなる）。"""
    out = {}
    for sym, g in gates.items():
        k = int(len(g) * frac)
        out[sym] = pd.DataFrame({c: np.roll(g[c].to_numpy(), k) for c in g.columns}, index=g.index)
    return out


def improved_share(base: dict, test: dict, symbols) -> float | None:
    hits = []
    for s in symbols:
        n0, r0 = base["by_symbol"].get(s, (0, np.nan))
        n1, r1 = test["by_symbol"].get(s, (0, np.nan))
        if n0 >= 5 and n1 >= 5 and np.isfinite(r0) and np.isfinite(r1):
            hits.append(r1 > r0)
    return float(np.mean(hits)) if hits else None


def years_better(base: dict, test: dict) -> str:
    a, b = base["yearly"], test["yearly"]
    common = a.index.intersection(b.index)
    if not len(common):
        return "–"
    return f"{int((b[common] > a[common] + 1e-9).sum())}/{len(common)}"


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 30:
        return np.nan
    rx = pd.Series(x[ok]).rank().to_numpy()
    ry = pd.Series(y[ok]).rank().to_numpy()
    return float(np.corrcoef(rx, ry)[0, 1])


def load_picks(path: str | Path) -> list[Pick]:
    final = json.loads(Path(path).read_text(encoding="utf-8"))
    picks = [Pick(s["symbol"], s["strategy"], s["params"], s.get("exit", {}), 0, 0.0, 0.0, 0,
                  float(s.get("multiplier", 1.0)), s["timeframe"], s.get("htf") or {}) for s in final["sleeves"]]
    if any(pk.htf for pk in picks):
        raise SystemExit("上位足フィルタ付きの構成は未対応（基準の構成で調べる）")
    return picks


@dataclass
class Setup:
    picks: list[Pick]
    symbols: list[str]
    tfs: dict[str, pd.Timedelta]
    store: ContextStore
    ds: Any
    period_a: Period | None
    period_b: Period
    rel_costs: list[float]
    long_start: pd.Timestamp
    bt_a: BacktestConfig          # 先物の期間（A・C）の設定（スプレッドはデータの列、指標の停止なし）

    @property
    def periods(self) -> list[Period]:
        return [self.period_a, self.period_b] if self.period_a is not None else [self.period_b]


def build_setup(final: str, config: str, context: str, equity: float, long_start: str, split: str,
                end: str | None = None, fine: bool = False) -> Setup:
    """期間A（先物）と期間B（フィリップ）を組み立てる。先物のデータが無ければ期間A は None。"""
    cfg = load_train_config(config)
    picks = load_picks(final)
    symbols = [pk.symbol for pk in picks]
    tfs = {pk.symbol: pd.Timedelta(TIMEFRAMES[pk.timeframe]) for pk in picks}

    store = ContextStore(context)
    if not store.available():
        raise SystemExit(f"{context} に外部データが無い。先に ./cfd context（scripts/fetch_context.py）を実行する")

    ds = load_dataset(cfg.data)
    settings = _eval_settings(cfg, ds)
    fx = ds.fx if ds.fx is not None else cfg.data.fx
    bt = _account_config(cfg, fx, settings.event_list())
    bt = replace(bt, initial_equity=equity)
    instruments = settings.instrument_map()

    # ---------------- 期間B: フィリップのデータ
    split_ts = pd.Timestamp(clean(split), tz="UTC")
    frames_b = {pk.symbol: ds.signal_frame(pk.symbol, pk.timeframe) for pk in picks}
    last = max(f.index[-1] for f in frames_b.values())
    end_b = (pd.Timestamp(clean(end), tz="UTC") if end else last) + pd.Timedelta(days=1)

    def resolve_b(key: str, sym: str):
        if key == "own":
            return from_bars(sym, frames_b[sym], tfs[sym]) if sym in frames_b else None
        if key == "partner":
            other = PARTNER.get(sym)
            f = frames_b.get(other) if other else None
            if f is None and other:
                f = ds.signal_frame(other, "D1")
            return from_bars(other, f, pd.Timedelta("1D")) if f is not None else None
        return store.get(key)

    def run_b(period, pk, gates, start):
        r = validate_window(ds, period.instruments, pk, period.bt, start, period.end, gates, use_fine=fine)
        return {"equity": r["equity"], "trades": r["trades"], "signals": r["signals"]}

    period_b = Period("B", f"期間B フィリップ {split_ts:%Y-%m}〜{end_b - pd.Timedelta(days=1):%Y-%m}", frames_b, tfs,
                      instruments, bt, split_ts, end_b, resolve_b, run_b)

    # ---------------- 期間A: 先物の長期データ（コストは今のスプレッドを価格に比例させる）
    frames_a = {}
    rel_costs: list[float] = []
    for pk in picks:
        f = yahoo_frame(store, FUTURES_FOR[pk.symbol]) if pk.symbol in FUTURES_FOR else None
        if f is None or f.empty:
            continue
        inst = instruments[pk.symbol]
        ref = float(frames_b[pk.symbol]["close"].iloc[-250:].median())
        rel = (inst.spread + 2 * inst.slippage) / ref
        rel_costs.append(rel)
        f = f[f.index < split_ts].copy()
        f["spread"] = rel * f["close"] / inst.point_size
        frames_a[pk.symbol] = f
    inst_a = {k: replace(v, spread=0.0, slippage=0.0) for k, v in instruments.items()}
    bt_a = replace(bt, fx_rate=float(cfg.data.fx),
                   filters=replace(bt.filters, max_spread_mult=float("inf"), extra_events=()))

    def resolve_a(key: str, sym: str):
        if key == "own":
            return from_bars(sym, frames_a[sym], tfs[sym]) if sym in frames_a else None
        if key == "partner":
            other = PARTNER.get(sym)
            f = frames_a.get(other) if other else None
            if f is None and other in FUTURES_FOR:
                f = yahoo_frame(store, FUTURES_FOR[other])
            return from_bars(other, f, pd.Timedelta("1D")) if f is not None and not f.empty else None
        return store.get(key)

    long_ts = pd.Timestamp(clean(long_start), tz="UTC")
    period_a = None
    if frames_a:
        period_a = Period("A", f"期間A 先物の長期データ {long_ts:%Y-%m}〜{split_ts - pd.Timedelta(days=1):%Y-%m}",
                          frames_a, tfs, inst_a, bt_a, long_ts, split_ts, resolve_a, run_frames)
    return Setup(picks, symbols, tfs, store, ds, period_a, period_b, rel_costs, long_ts, bt_a)


def build_period_c(setup: Setup) -> tuple[Period, list[Pick], list[str]] | None:
    """期間C: 使っていない市場（銅・プラチナ・天然ガス）に同じ戦略を当てる。データが無ければ None。"""
    frames_c = {}
    rel_c = float(np.median(setup.rel_costs)) if setup.rel_costs else 3e-4
    for sym, key in OTHER_MARKETS.items():
        f = yahoo_frame(setup.store, key)
        if f is None or f.empty:
            continue
        f = f[f.index >= setup.long_start - pd.Timedelta(days=200)].copy()
        f["spread"] = rel_c * f["close"] / 1e-4
        frames_c[sym] = f
    if not frames_c:
        return None
    tf1 = pd.Timedelta("1D")
    syms_c = list(frames_c)
    inst_c = {sym: Instrument(symbol=sym, description=SPECS[OTHER_MARKETS[sym]].label, unit="unit", min_qty=1,
                              qty_step=1, max_qty=1e12, margin_rate=0.05, spread=0.0, slippage=0.0, tick_size=1e-4,
                              cluster=sym) for sym in syms_c}
    pk0 = setup.picks[0]
    picks_c = [Pick(sym, pk0.strategy, pk0.params, pk0.exit, 0, 0.0, 0.0, 0, 1.0, pk0.timeframe, {})
               for sym in syms_c]
    def resolve_c(key: str, sym: str):
        if key == "own":
            return from_bars(sym, frames_c[sym], tf1) if sym in frames_c else None
        return None if key == "partner" else setup.store.get(key)

    end_c = max(f.index[-1] for f in frames_c.values()) + tf1
    period_c = Period("C", f"期間C 他の商品 {setup.long_start:%Y-%m}〜{end_c - tf1:%Y-%m}", frames_c,
                      {s: tf1 for s in syms_c}, inst_c, setup.bt_a, setup.long_start, end_c, resolve_c, run_frames)
    return period_c, picks_c, syms_c
