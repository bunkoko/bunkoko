"""相方の行き過ぎフィルタを、使っていない商品の組で確かめる（./cfd partner）。docs/cross_asset.md 7 章。

今の構成（タートル 55/20 日足＋自分の行き過ぎ 1.5 倍・20 日）を基準にし、「相方が直近 20 日に、取引の向きに
ふだんの 1.5 倍を超えて動いていたら新規に入らない」を足した分（増分）を測る。判定は、これまで相方の行き過ぎに
一度も使っていない 5 組（10 市場）だけで行う。金↔銀・WTI↔ブレント（期間A・B）は見つけた期間なので参考。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .backtest import BacktestConfig, Sleeve, run_backtest
from .context import CSeries, at_times, change, fetch_yahoo, from_bars, yahoo_csv_frame
from .diversified import EXTRA_MARKETS
from .features import Hypothesis, decide_ns, hypothesis_gate
from .instruments import Instrument
from .study import shifted
from .universe import BY_KEY, GROUPS, Market, data_path, day_returns, market_instrument, sharpe, with_costs

N_DAYS, THRESHOLD = 20, 1.5          # cross_asset.md 6 章の相方の行き過ぎと同じ（結果を見て動かさない）
JUMP_SIGMA, WINDOW = 4.0, 252        # 主の判定: 相方の 1 日の変化がそれまでの 252 日の標準偏差の 4 倍を超えた日は、つなぎ足の乗り換えの飛びとみなして 0 にする
MIN_YEARS = 3                        # これより短い市場の組は使わない
MIN_PAIRS = 3                        # 判定に残る組がこれより少なければ判定しない
PLACEBO = 200                        # 時期をずらした偶然の回数
PRIOR_TRIALS = 79                    # docs/review.md の 77 ＋ desk の D1・D2
FEEDER = Market("FEEDER", "GF=F", "commodity", "素牛（フィーダーキャトル）")
MARKETS: dict[str, Market] = {**BY_KEY, **{m.key: m for m in EXTRA_MARKETS}, FEEDER.key: FEEDER}
# 判定に使う組（経済的につながりが強いものを、結果を見る前に決めた。1 つの市場の相方は 1 つだけ。EA と同じ形）
PAIRS: tuple[tuple[str, str], ...] = (
    ("PLATINUM", "PALLADIUM"),       # 白金族の金属（同じ鉱山・同じ需要）
    ("HEATOIL", "GASOLINE"),         # 原油から作る 2 つの製品
    ("WHEAT", "KCWHEAT"),            # 小麦の 2 つの品種（シカゴ・カンザス）
    ("SOYBEAN", "SOYMEAL"),          # 大豆と、大豆を搾った粕
    ("CATTLE", "FEEDER"),            # 肥育した牛と、肥育前の牛
)
CORE_PAIRS: tuple[tuple[str, str], ...] = (("GOLD", "SILVER"), ("WTI", "BRENT"))   # 参考（見つけた期間）


def partner_map(pairs) -> dict[str, str]:
    out: dict[str, str] = {}
    for a, b in pairs:
        out[a], out[b] = b, a
    return out


def clean_zchange(cs: CSeries, n: int = N_DAYS, window: int = WINDOW, jump: float = JUMP_SIGMA) -> tuple[np.ndarray, np.ndarray]:
    """飛びを除いた n 本の変化 ÷ ふだんの大きさ。飛び = 1 本の変化の絶対値が、その前の window 本の標準偏差の jump 倍を超えた日（0 にする）。

    つなぎ足の限月の乗り換えの飛び（docs/cross_asset.md 7.2）が、相方の「行き過ぎ」に入らないようにするため。戻り値の 2 つ目は飛びとした日。
    """
    one = pd.Series(change(cs, 1))
    sd = one.rolling(window, min_periods=window // 2).std().shift(1)        # その日より前だけ
    is_jump = (one.abs() > jump * sd).fillna(False).to_numpy()
    cleaned = one.where(~is_jump, 0.0)
    scale = cleaned.rolling(window, min_periods=window // 2).std() * np.sqrt(n)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (cleaned.rolling(n, min_periods=n).sum() / scale).to_numpy(dtype=float, copy=True)
    z[~np.isfinite(z)] = np.nan
    return z, is_jump


def partner_gates(frames: dict[str, pd.DataFrame], pmap: dict[str, str], tf: pd.Timedelta = pd.Timedelta("1D"),
                  n: int = N_DAYS, threshold: float = THRESHOLD, clean: bool = True) -> dict[str, pd.DataFrame]:
    """相方の直近 n 本の変化が、ふだんの threshold 倍（σ）を超えた向きの新規を止める絞り込み（列 long / short）。

    clean=True（主の判定）: 相方の変化から飛びの日を除く（clean_zchange）。clean=False（参考）: cross_asset.md 6 章の extension_partner と同じ
    （`features.hypothesis_gate`、`context.zchange`）。どちらも、相方の値は自分の足の終わりの時点で終わっている相方の足まで（先読みしない）。
    """
    if clean:
        out = {}
        for s, f in frames.items():
            other = pmap.get(s)
            if other not in frames:
                continue
            cs = from_bars(other, frames[other], tf)
            z = at_times(cs, clean_zchange(cs, n)[0], decide_ns(f.index, tf))
            out[s] = pd.DataFrame({"long": ~(z > threshold), "short": ~(z < -threshold)}, index=f.index)
        return out
    h = Hypothesis("extension_partner", "相方の行き過ぎ", {s: "partner" for s in frames if pmap.get(s) in frames},
                   kind="extension", n=n, threshold=threshold)

    def resolve(key: str, sym: str):
        other = pmap.get(sym)
        return from_bars(other, frames[other], tf) if key == "partner" and other in frames else None

    out = {}
    for s, f in frames.items():
        g = hypothesis_gate(h, s, f.index, tf, resolve)
        if g is not None:
            out[s] = g
    return out


@dataclass
class Group:
    """1 つのまとまり（判定の 5 組、または参考の 2 組）のデータと設定。"""
    name: str
    label: str
    frames: dict[str, pd.DataFrame]
    instruments: dict[str, Instrument]
    bt: BacktestConfig
    pmap: dict[str, str]
    start: pd.Timestamp
    dropped: list[str]


def load_group(name: str, label: str, pairs, root: Path, bt: BacktestConfig, start: pd.Timestamp, end: pd.Timestamp | None,
               since: str, fetch: bool = True, log: Callable[[str], None] = print) -> Group:
    """Yahoo の日足（data/universe/<名前>.csv。無ければ取る）で組を作る。どちらかの市場が無い・短い組は外す。"""
    frames, insts, dropped, use = {}, {}, [], []
    for a, b in pairs:
        got = {}
        for k in (a, b):
            m = MARKETS[k]
            p = data_path(root, m)
            if not p.exists() and fetch:
                root.mkdir(parents=True, exist_ok=True)
                try:
                    fetch_yahoo(m.ticker, since).to_csv(p, index=False)
                except Exception as e:  # noqa: BLE001
                    log(f"  ✗ {m.label}（{m.ticker}）: {str(e)[:100]}")
            if not p.exists():
                break
            f = yahoo_csv_frame(p, repair=True)
            if end is not None:
                f = f[f.index < end]
            if len(f) < MIN_YEARS * 252:
                break
            g = GROUPS[m.group]
            got[k] = (with_costs(f, m, g), market_instrument(m, g))
        if len(got) == 2:
            use.append((a, b))
            for k, (f, inst) in got.items():
                frames[k], insts[k] = f, inst
        else:
            dropped.append(f"{MARKETS[a].label}↔{MARKETS[b].label}")
    return Group(name, label, frames, insts, replace(bt, trade_start=start), partner_map(use), start, dropped)


def run(group: Group, pk0, gates: dict[str, pd.DataFrame] | None = None) -> dict:
    """今の構成（自分の行き過ぎ入り）を全市場に同じリスクで当てる。gates があれば相方の行き過ぎも足す。"""
    sleeves = [Sleeve(s, _strategy(pk0), dict(pk0.exit), entry_gate=(gates or {}).get(s)) for s in group.frames]
    res = run_backtest(group.frames, group.instruments, sleeves, group.bt)
    r = day_returns(res.equity, group.start)
    t = res.trades
    by = {s: (int((t["symbol"] == s).sum()), float(t.loc[t["symbol"] == s, "r_multiple"].mean()) if (t["symbol"] == s).any() else np.nan)
          for s in group.frames} if not t.empty else {s: (0, np.nan) for s in group.frames}
    return {"returns": r, "sharpe": sharpe(r), "trades": int(len(t)), "by_symbol": by}


def _strategy(pk0):
    from .strategies import make_strategy

    return make_strategy(pk0.strategy, **pk0.params)


def improved_share(base: dict, test: dict) -> float | None:
    """1 回あたりの R が良くなった市場の割合（両方で 5 回以上取引した市場だけ）。"""
    hits = []
    for s, (n0, r0) in base["by_symbol"].items():
        n1, r1 = test["by_symbol"].get(s, (0, np.nan))
        if n0 >= 5 and n1 >= 5 and np.isfinite(r0) and np.isfinite(r1):
            hits.append(r1 > r0)
    return float(np.mean(hits)) if hits else None


def placebo_diffs(group: Group, pk0, gates: dict[str, pd.DataFrame], base_sharpe: float, n: int, seed: int = 20261015,
                  log: Callable[[str], None] = print) -> np.ndarray:
    """絞り込みの並びを時間方向にずらした偶然（止める割合・続き方は同じ）の、基準とのシャープの差。"""
    fracs = np.random.default_rng(seed).uniform(0.15, 0.85, n)
    out = np.empty(n)
    for i, fr in enumerate(fracs):
        out[i] = run(group, pk0, shifted(gates, fr))["sharpe"] - base_sharpe
        if (i + 1) % 20 == 0:
            log(f"  偶然 {i + 1}/{n}")
    return out


def jump_share(frames: dict[str, pd.DataFrame], tf: pd.Timedelta = pd.Timedelta("1D")) -> dict[str, float]:
    """市場ごとの、飛びとした日の割合（報告用）。"""
    return {s: float(clean_zchange(from_bars(s, f, tf))[1].mean()) for s, f in frames.items()}


def verdict(diff: float, pct: float | None, share: float | None, n_pairs: int | None = None) -> str:
    """docs/cross_asset.md 7.3 の判定。"""
    if n_pairs is not None and n_pairs < MIN_PAIRS:
        return f"判定できない（判定に残った組が {n_pairs} 組。{MIN_PAIRS} 組未満）"
    if not np.isfinite(diff) or pct is None:
        return "判定できない（データ不足）"
    ok = diff > 0 and pct >= 0.95 and (share or 0.0) >= 0.5
    return "採用候補（デモの区切りで EA に足す候補）" if ok else "採らない（相方の行き過ぎは閉じる）"
