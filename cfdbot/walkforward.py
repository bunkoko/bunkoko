"""ウォークフォワード最適化（過剰最適化の検出）。

学習期間でパラメータをグリッドサーチ → 直後の検証期間（未使用データ）で成績を測る、
を期間をずらしながら繰り返し、検証期間の成績だけをつなげて評価する。

- plateau=True: 各パラメータの点数を「自分と隣接グリッドの平均」で評価し、
  周りも良い「台地」を選ぶ（1点だけ尖った最適値を避ける）
- n_jobs: 並列数（Mac のマルチコアを使う）。macOS では呼び出し側を
  if __name__ == "__main__": で囲むこと
"""

from __future__ import annotations

import itertools
import math
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Callable

import numpy as np
import pandas as pd

from .backtest import BacktestConfig, Sleeve, run_backtest
from .instruments import Instrument
from .strategies import make_strategy

Objective = Callable[[dict[str, float]], float]


def objective_mar(m: dict[str, float]) -> float:
    """CAGR / 最大DD（取引回数が少ないものは除外）。"""
    mdd = max(m.get("max_drawdown", 0.0), 0.02)  # DD が極小のときに無限大にならないよう下限
    return m.get("cagr", 0.0) / mdd


def objective_sharpe(m: dict[str, float]) -> float:
    return m.get("sharpe", 0.0)


OBJECTIVES: dict[str, Objective] = {"mar": objective_mar, "sharpe": objective_sharpe}


@dataclass
class WFWindow:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_end: pd.Timestamp


@dataclass
class WalkForwardResult:
    windows: pd.DataFrame           # 期間ごとの採用パラメータと学習/検証の成績
    oos_equity: pd.Series           # 検証期間だけをつなげた資産曲線
    oos_trades: pd.DataFrame
    grid_scores: list[pd.DataFrame]  # 期間ごとの全パラメータの点数

    def efficiency(self) -> float:
        """ウォークフォワード効率 = 検証期間の年率 / 学習期間の年率（0.5 以上が目安）。"""
        w = self.windows
        is_ = w["train_cagr"].mean()
        return float(w["test_cagr"].mean() / is_) if is_ > 0 else float("nan")


def make_windows(
    index: pd.DatetimeIndex, train_months: int, test_months: int, anchored: bool = False
) -> list[WFWindow]:
    start, end = index[0], index[-1]
    out = []
    t0 = start
    train_end = start + pd.DateOffset(months=train_months)
    while train_end < end:
        test_end = min(train_end + pd.DateOffset(months=test_months), end)
        out.append(WFWindow(start if anchored else t0, train_end, test_end))
        t0 = t0 + pd.DateOffset(months=test_months)
        train_end = train_end + pd.DateOffset(months=test_months)
    return out


# --------------------------------------------------------------------------- 並列実行
_G: dict[str, Any] = {}


def _init(data, instruments, symbol, strategy_name, fixed, sleeve_overrides, config):
    _G.update(data=data, instruments=instruments, symbol=symbol, strategy=strategy_name,
              fixed=fixed, overrides=sleeve_overrides, config=config)


def _slice(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, warmup: int) -> pd.DataFrame:
    i0 = max(df.index.searchsorted(start) - warmup, 0)
    i1 = df.index.searchsorted(end)
    return df.iloc[i0:i1]


def _run_one(params: dict, start, end) -> tuple[dict[str, float], Any]:
    strat = make_strategy(_G["strategy"], **{**_G["fixed"], **params})
    df = _G["data"][_G["symbol"]]
    warm = strat.warmup_bars() + 5
    sliced = {_G["symbol"]: _slice(df, start, end, warm)}
    cfg = replace(_G["config"], trade_start=start, trade_end=end)
    res = run_backtest(sliced, _G["instruments"],
                       [Sleeve(_G["symbol"], strat, dict(_G["overrides"]))], cfg)
    return res.metrics(), res


def _grid(param_grid: dict[str, list]) -> tuple[list[str], list[tuple[int, ...]]]:
    keys = list(param_grid)
    idx = list(itertools.product(*[range(len(param_grid[k])) for k in keys]))
    return keys, idx


def _neighbor_mean(scores: dict[tuple[int, ...], float], dims: list[int]) -> dict[tuple[int, ...], float]:
    out = {}
    for pos, s in scores.items():
        vals = [s]
        for d, size in enumerate(dims):
            for step in (-1, 1):
                q = list(pos)
                q[d] += step
                if 0 <= q[d] < size and tuple(q) in scores:
                    vals.append(scores[tuple(q)])
        out[pos] = float(np.mean(vals))
    return out


def walk_forward(
    data: dict[str, pd.DataFrame],
    instruments: dict[str, Instrument],
    symbol: str,
    strategy: str,
    param_grid: dict[str, list],
    *,
    fixed_params: dict[str, Any] | None = None,
    sleeve_exit_overrides: dict[str, Any] | None = None,
    config: BacktestConfig | None = None,
    train_months: int = 24,
    test_months: int = 6,
    anchored: bool = False,
    objective: str | Objective = "mar",
    min_trades: int = 20,
    plateau: bool = True,
    n_jobs: int | None = None,
) -> WalkForwardResult:
    """1銘柄 × 1戦略のウォークフォワード最適化。

    param_grid のキーは戦略パラメータ名。出口設定（trail_atr など）を最適化したい場合は
    キーを "exit." で始める（例: {"exit.trail_atr": [2.5, 3, 3.5]}）。
    """
    config = config or BacktestConfig()
    fixed_params = fixed_params or {}
    sleeve_exit_overrides = sleeve_exit_overrides or {}
    obj = OBJECTIVES[objective] if isinstance(objective, str) else objective
    keys, grid_idx = _grid(param_grid)
    dims = [len(param_grid[k]) for k in keys]
    combos = [{k: param_grid[k][j] for k, j in zip(keys, pos)} for pos in grid_idx]
    windows = make_windows(data[symbol].index, train_months, test_months, anchored)
    if not windows:
        raise ValueError("データが短すぎて学習期間を確保できない")

    def split(params: dict) -> tuple[dict, dict]:
        strat = {k: v for k, v in params.items() if not k.startswith("exit.")}
        ex = {k[5:]: v for k, v in params.items() if k.startswith("exit.")}
        return strat, ex

    # 出口パラメータは sleeve の上書きとして渡すため、組み合わせごとにタスクを作る
    def to_task(params, start, end):
        strat, ex = split(params)
        return ({**strat, "__exit__": ex}, start, end)

    n_jobs = n_jobs or max((os.cpu_count() or 2) - 1, 1)
    initargs = (data, instruments, symbol, strategy, fixed_params, sleeve_exit_overrides, config)
    rows, oos_eq, oos_trades, grid_scores = [], [], [], []
    _init(*initargs)  # 検証期間の実行はメインプロセスで行う
    executor = ProcessPoolExecutor(n_jobs, initializer=_init, initargs=initargs) if n_jobs > 1 else None
    try:
        for w in windows:
            tasks = [to_task(c, w.train_start, w.train_end) for c in combos]
            mapper = executor.map if executor else map
            metrics = list(mapper(_score_task_exit, tasks))
            raw = {}
            for pos, m in zip(grid_idx, metrics):
                ok = m.get("trades", 0) >= min_trades
                raw[pos] = obj(m) if ok else -math.inf
            finite = {k: v for k, v in raw.items() if math.isfinite(v)}
            smoothed = _neighbor_mean(finite, dims) if plateau and finite else finite
            gs = pd.DataFrame(combos)
            gs["score"] = [raw[p] for p in grid_idx]
            gs["score_plateau"] = [smoothed.get(p, -math.inf) for p in grid_idx]
            gs["trades"] = [m.get("trades", 0) for m in metrics]
            grid_scores.append(gs)
            if smoothed:
                best_pos = max(smoothed, key=smoothed.get)
                best = combos[grid_idx.index(best_pos)]
                best_m = metrics[grid_idx.index(best_pos)]
                fallback = False
            else:  # どれも取引回数不足 → 既定値
                best, best_m, fallback = {}, {}, True
            strat_p, ex_p = split(best)
            test_m, res = _run_exit(strat_p, ex_p, w.train_end, w.test_end)
            rows.append({
                "train_start": w.train_start, "train_end": w.train_end, "test_end": w.test_end,
                **{f"p.{k}": v for k, v in best.items()},
                "fallback_default": fallback,
                "train_score": obj(best_m) if best_m else np.nan,
                "train_cagr": best_m.get("cagr", np.nan),
                "test_cagr": test_m.get("cagr", np.nan),
                "test_mdd": test_m.get("max_drawdown", np.nan),
                "test_trades": test_m.get("trades", 0),
                "test_pf": test_m.get("profit_factor", np.nan),
            })
            if not res.equity.empty:
                oos_eq.append(res.equity / config.initial_equity)
            if not res.trades.empty:
                oos_trades.append(res.trades)
    finally:
        if executor:
            executor.shutdown()

    # 検証期間の資産曲線をリターンで連結
    chained = []
    level = 1.0
    for eq in oos_eq:
        chained.append(eq * level)
        level = float((eq * level).iloc[-1])
    oos = pd.concat(chained) * config.initial_equity if chained else pd.Series(dtype=float)
    trades = pd.concat(oos_trades, ignore_index=True) if oos_trades else pd.DataFrame()
    return WalkForwardResult(pd.DataFrame(rows), oos, trades, grid_scores)


def _run_exit(strat_params: dict, exit_params: dict, start, end):
    saved = _G["overrides"]
    _G["overrides"] = {**saved, **exit_params}
    try:
        return _run_one(strat_params, start, end)
    finally:
        _G["overrides"] = saved


def _score_task_exit(args) -> dict[str, float]:
    params, start, end = args
    params = dict(params)
    ex = params.pop("__exit__", {})
    m, _ = _run_exit(params, ex, start, end)
    return m
