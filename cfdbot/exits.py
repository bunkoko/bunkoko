"""出口管理（初期損切り・建値ストップ・シャンデリア型トレーリング・分割利確・時間ストップ）。

EA と同じルールで動くよう、状態更新は「足の確定時」にだけ行う純粋な関数にしてある。
損切りラインは証券会社側の逆指値として置く前提（EA は足確定時に逆指値を書き換える）。
ラインは有利な方向にしか動かさない。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from typing import Any


@dataclass(frozen=True)
class ExitConfig:
    atr_period: int = 20
    init_stop_atr: float = 2.5        # 戦略が損切り位置を出さない場合の初期損切り（ATR倍）
    min_stop_atr: float = 1.0         # 戦略の損切り幅をこの範囲にクリップ
    max_stop_atr: float = 4.0
    breakeven_trigger_atr: float = 1.0  # 終値ベースの含み益がエントリー時ATR×この値で建値へ（0=無効）
    breakeven_offset_atr: float = 0.1   # 建値 + ATR×offset に置く（コスト分）
    trail_atr: float = 3.0            # シャンデリア: 保有中の最高値 - ATR×trail（0=無効）
    min_update_atr: float = 0.1       # これ未満の移動では逆指値を書き換えない
    partial_tp_r: float = 0.0         # +xR で一部利確（0=無効）
    partial_fraction: float = 0.5
    time_stop_bars: int = 0           # n 本後に +time_stop_min_r 未満なら撤退（0=無効）
    time_stop_min_r: float = 0.5
    max_hold_bars: int = 0            # 最大保有本数（0=無効）
    flatten_before_weekend: bool = False
    flatten_before_events: bool = False

    def with_overrides(self, overrides: dict[str, Any] | None) -> "ExitConfig":
        if not overrides:
            return self
        valid = {f.name for f in fields(self)}
        unknown = set(overrides) - valid
        if unknown:
            raise ValueError(f"unknown exit params {sorted(unknown)}")
        return replace(self, **overrides)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def clip_stop(self, stop_dist: float | None, atr: float) -> float:
        """戦略の損切り幅（NaN なら既定の ATR 倍）を [min, max]×ATR に収める。"""
        if stop_dist is None or not stop_dist == stop_dist or stop_dist <= 0:  # NaN 判定
            stop_dist = self.init_stop_atr * atr
        return min(max(stop_dist, self.min_stop_atr * atr), self.max_stop_atr * atr)


@dataclass
class ExitState:
    side: int            # +1 買い / -1 売り
    entry: float         # 約定価格
    stop: float          # 現在の逆指値
    r_dist: float        # 初期損切り幅（1R の値幅）
    atr_entry: float
    extreme: float       # 保有中の最高値（買い）/最安値（売り）
    bars_held: int = 0
    breakeven_done: bool = False


@dataclass(frozen=True)
class BarUpdate:
    new_stop: float | None   # 逆指値を書き換える場合の新しい値
    exit_now: str | None     # 次の始値で手仕舞う理由（"time_stop" など）


def on_bar_close(
    st: ExitState,
    cfg: ExitConfig,
    high: float,
    low: float,
    close: float,
    atr: float,
    spread: float,
    stop_level: float = 0.0,
) -> BarUpdate:
    """確定足ごとに呼ぶ。st の extreme / bars_held / breakeven_done を更新する。

    価格はチャート表示と同じ Bid 基準。売りポジションの逆指値は Ask で約定するので
    スプレッド分だけ上にずらす。
    """
    side = st.side
    st.bars_held += 1
    st.extreme = max(st.extreme, high) if side > 0 else min(st.extreme, low)

    # 時間系の撤退
    if cfg.max_hold_bars and st.bars_held >= cfg.max_hold_bars:
        return BarUpdate(None, "max_hold")
    if cfg.time_stop_bars and st.bars_held >= cfg.time_stop_bars:
        mark = close if side > 0 else close + spread
        progress_r = side * (mark - st.entry) / st.r_dist
        if progress_r < cfg.time_stop_min_r:
            return BarUpdate(None, "time_stop")

    candidate = st.stop
    # 建値ストップ（終値ベースで判定。ヒゲだけで建値に上げない）
    if cfg.breakeven_trigger_atr > 0 and not st.breakeven_done:
        mark = close if side > 0 else close + spread
        if side * (mark - st.entry) >= cfg.breakeven_trigger_atr * st.atr_entry:
            be = st.entry + side * cfg.breakeven_offset_atr * st.atr_entry
            candidate = _better(side, candidate, be)
            st.breakeven_done = True
    # シャンデリア型トレーリング（現在の ATR を使う）
    if cfg.trail_atr > 0:
        if side > 0:
            trail = st.extreme - cfg.trail_atr * atr
        else:
            trail = st.extreme + cfg.trail_atr * atr + spread
        candidate = _better(side, candidate, trail)

    if candidate == st.stop:
        return BarUpdate(None, None)
    # 現在値に近すぎる（ストップレベル内）なら逆指値は置けない → 次の始値で成行決済
    if side > 0 and candidate >= close - stop_level:
        return BarUpdate(None, "stop_level")
    if side < 0 and candidate <= close + spread + stop_level:
        return BarUpdate(None, "stop_level")
    moved_be = st.breakeven_done and side * (candidate - st.entry) >= 0 > side * (st.stop - st.entry)
    if abs(candidate - st.stop) < cfg.min_update_atr * atr and not moved_be:
        return BarUpdate(None, None)
    return BarUpdate(candidate, None)


def _better(side: int, a: float, b: float) -> float:
    """有利な方（買いなら高い方、売りなら低い方）の逆指値を返す。"""
    return max(a, b) if side > 0 else min(a, b)
