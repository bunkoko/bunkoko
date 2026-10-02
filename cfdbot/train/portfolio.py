"""ポートフォリオ学習の中身（純粋な計算部分）。

1. 戦略ごと: 学習期間の日次リターンでパラメータを選ぶ（隣接平均で「台地」を選ぶ）
2. 銘柄ごと: 評価値が基準を満たす戦略のうち上位を採用（既定 1 銘柄 1 戦略）
3. 配分: 採用した戦略のリスク寄与が均等になるよう配分（ERC）し、
   学習期間の年率ボラティリティが目標に合うよう全体を拡大・縮小、最大DDが上限を超えるなら縮小
4. 配分は「1回の損失の倍率」（基準 1% × 倍率）として EA に渡す

すべて学習期間のデータだけで決め、直後の検証期間で成績を測る（ウォークフォワード）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import PortfolioConfig, WalkForwardConfig

ANN = 252.0


@dataclass
class SleeveCandidates:
    """1銘柄 × 1戦略の全パラメータの結果。"""

    symbol: str
    strategy: str
    params: list[dict]           # パラメータ（戦略の引数）
    exits: list[dict]            # 出口設定の上書き
    pos: list[tuple[int, ...]]   # グリッド上の位置
    dims: tuple[int, ...]        # グリッドの各軸の大きさ
    returns: np.ndarray          # [パラメータ数, 日数] の日次リターン（基準リスク）
    entries: list[np.ndarray]    # 取引ごとのエントリー日（日数の添字）
    timeframe: str = "H1"        # 売買の足
    htfs: list[dict] | None = None  # パラメータごとの上位足フィルタ

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.strategy}@{self.timeframe}"

    def trade_counts(self, a: int, b: int) -> np.ndarray:
        return np.array([np.count_nonzero((e >= a) & (e < b)) for e in self.entries])


@dataclass
class Pick:
    symbol: str
    strategy: str
    params: dict
    exit: dict
    index: int            # SleeveCandidates 内の添字
    score: float          # 学習期間の評価値（隣接平均後）
    raw_score: float
    trades: int
    multiplier: float = 0.0   # 1回の損失の倍率（基準リスク × これ）
    timeframe: str = "H1"
    htf: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.strategy}@{self.timeframe}"

    @property
    def label(self) -> str:
        htf = f"+{self.htf['timeframe']}EMA{self.htf['ema']}" if self.htf else ""
        return f"{self.symbol}:{self.strategy}@{self.timeframe}{htf}"


@dataclass
class WindowResult:
    train: tuple[pd.Timestamp, pd.Timestamp]
    test: tuple[pd.Timestamp, pd.Timestamp] | None
    picks: list[Pick] = field(default_factory=list)
    train_vol: float = 0.0
    train_dd: float = 0.0
    oos: pd.Series | None = None      # 検証期間の日次リターン（近似）


# --------------------------------------------------------------------------- 評価値
def max_drawdown(ret: np.ndarray) -> float:
    eq = np.cumprod(1.0 + ret)
    peak = np.maximum.accumulate(np.maximum(eq, 1.0))
    return float(np.max(1.0 - eq / peak)) if len(eq) else 0.0


def score_matrix(r: np.ndarray, objective: str) -> np.ndarray:
    """r: [n, T] の日次リターン → 各行の評価値。"""
    if r.shape[1] < 2:
        return np.zeros(r.shape[0])
    mu = r.mean(axis=1)
    if objective == "sharpe":
        sd = r.std(axis=1)
        return np.where(sd > 0, mu / np.where(sd > 0, sd, 1) * math.sqrt(ANN), 0.0)
    if objective == "sortino":
        down = np.sqrt(np.mean(np.minimum(r, 0.0) ** 2, axis=1))
        return np.where(down > 0, mu / np.where(down > 0, down, 1) * math.sqrt(ANN), 0.0)
    if objective == "mar":
        years = r.shape[1] / ANN
        growth = np.prod(1.0 + r, axis=1)
        cagr = np.where(growth > 0, growth ** (1 / years) - 1, -1.0)
        dd = np.array([max_drawdown(x) for x in r])
        return cagr / np.maximum(dd, 0.02)
    raise ValueError(f"unknown objective '{objective}'")


def neighbor_mean(scores: dict[tuple[int, ...], float], dims: tuple[int, ...]) -> dict[tuple[int, ...], float]:
    out = {}
    for pos, s in scores.items():
        vals = [s]
        for d in range(len(dims)):
            for step in (-1, 1):
                q = list(pos)
                q[d] += step
                t = tuple(q)
                if t in scores:
                    vals.append(scores[t])
        out[pos] = float(np.mean(vals))
    return out


def select_params(c: SleeveCandidates, a: int, b: int, wf: WalkForwardConfig) -> Pick | None:
    """学習期間 [a, b) で最良のパラメータを選ぶ。条件を満たすものが無ければ None。"""
    scores = score_matrix(c.returns[:, a:b], wf.objective)
    counts = c.trade_counts(a, b)
    ok = {c.pos[i]: float(scores[i]) for i in range(len(scores)) if counts[i] >= wf.min_trades}
    if not ok:
        return None
    smooth = neighbor_mean(ok, c.dims) if wf.plateau else ok
    best_pos = max(smooth, key=smooth.get)
    i = c.pos.index(best_pos)
    return Pick(c.symbol, c.strategy, c.params[i], c.exits[i], i, smooth[best_pos], float(scores[i]),
                int(counts[i]), timeframe=c.timeframe, htf=dict(c.htfs[i]) if c.htfs else {})


# --------------------------------------------------------------------------- 配分
def shrink_cov(r: np.ndarray, shrinkage: float) -> np.ndarray:
    cov = np.atleast_2d(np.cov(r)) * ANN
    diag = np.diag(np.diag(cov))
    return (1 - shrinkage) * cov + shrinkage * diag


def erc_weights(cov: np.ndarray, iters: int = 1000, tol: float = 1e-10) -> np.ndarray:
    """リスク寄与（w_i × (Σw)_i）が全員同じになる重み。"""
    vol = np.sqrt(np.diag(cov))
    w = 1.0 / np.where(vol > 0, vol, np.inf)
    if not np.isfinite(w).any() or w.sum() == 0:
        return np.ones(len(cov)) / len(cov)
    w = w / w.sum()
    for _ in range(iters):
        rc = w * (cov @ w)
        if np.any(rc <= 0):
            return w  # 強い負の相関などで解けない → 逆ボラティリティのまま
        new = w * np.sqrt(rc.mean() / rc)
        new /= new.sum()
        if np.max(np.abs(new - w)) < tol:
            return new
        w = new
    return w


def allocate(r: np.ndarray, scores: np.ndarray, pc: PortfolioConfig,
             min_mult: float, max_mult: float) -> tuple[np.ndarray, float, float]:
    """r: [n, T]（基準リスクでの日次リターン）→ 倍率、学習期間の年率ボラ、最大DD。"""
    n = r.shape[0]
    cov = shrink_cov(r, pc.shrinkage) if n > 1 else np.array([[np.var(r) * ANN]])
    vol = np.sqrt(np.diag(cov))
    if pc.allocation == "equal":
        w = np.ones(n)
    elif pc.allocation == "inverse_vol":
        w = 1.0 / np.maximum(vol, 1e-12)
    elif pc.allocation == "sharpe_tilt":
        tilt = np.maximum(scores, 0.0)
        w = (tilt if tilt.sum() > 0 else np.ones(n)) / np.maximum(vol, 1e-12)
    elif pc.allocation == "erc":
        w = erc_weights(cov) if n > 1 else np.ones(1)
    else:
        raise ValueError(f"unknown allocation '{pc.allocation}'")
    w = w / w.sum()
    port_vol = float(np.sqrt(w @ cov @ w))
    m = w * (pc.target_vol / port_vol) if port_vol > 0 else w
    m = np.clip(m, min_mult, max_mult)
    dd = max_drawdown(m @ r)
    if dd > pc.max_train_dd:
        m = np.clip(m * pc.max_train_dd / dd, min_mult, max_mult)
    port = m @ r
    return m, float(np.std(port) * math.sqrt(ANN)), max_drawdown(port)


def build_portfolio(cands: list[SleeveCandidates], a: int, b: int, wf: WalkForwardConfig,
                    pc: PortfolioConfig, min_mult: float, max_mult: float) -> tuple[list[Pick], float, float]:
    """学習期間 [a, b) で戦略・パラメータ・配分を決める。"""
    picks = [p for c in cands if (p := select_params(c, a, b, wf)) is not None and p.score >= pc.min_score]
    by_symbol: dict[str, list[Pick]] = {}
    for p in sorted(picks, key=lambda p: -p.score):
        lst = by_symbol.setdefault(p.symbol, [])
        # 同じ銘柄に複数の戦略を使う場合も、時間足は 1 つに揃える（EA・バックテストとも 1 銘柄 1 時間足）
        if len(lst) < pc.max_strategies_per_symbol and all(x.timeframe == p.timeframe for x in lst):
            lst.append(p)
    chosen = [p for lst in by_symbol.values() for p in lst]
    if not chosen:
        return [], 0.0, 0.0
    lookup = {c.key: c for c in cands}
    r = np.vstack([lookup[p.key].returns[p.index, a:b] for p in chosen])
    mult, vol, dd = allocate(r, np.array([p.score for p in chosen]), pc, min_mult, max_mult)
    for p, m in zip(chosen, mult):
        p.multiplier = float(m)
    return chosen, vol, dd


def portfolio_returns(cands: list[SleeveCandidates], picks: list[Pick], a: int, b: int) -> np.ndarray:
    lookup = {c.key: c for c in cands}
    out = np.zeros(b - a)
    for p in picks:
        out += p.multiplier * lookup[p.key].returns[p.index, a:b]
    return out


def make_windows(calendar: pd.DatetimeIndex, start: pd.Timestamp, train_months: int,
                 test_months: int) -> list[tuple[int, int, int]]:
    """[学習開始, 学習終了=検証開始, 検証終了) の添字の組。"""
    out = []
    t0 = start
    while True:
        t1 = t0 + pd.DateOffset(months=train_months)
        t2 = t1 + pd.DateOffset(months=test_months)
        if t1 >= calendar[-1]:
            break
        a, b, c = (int(calendar.searchsorted(x)) for x in (t0, t1, min(t2, calendar[-1] + pd.Timedelta(days=1))))
        if c - b >= 20:  # 検証期間が短すぎる最後の端は使わない
            out.append((a, b, c))
        t0 = t0 + pd.DateOffset(months=test_months)
    return out
