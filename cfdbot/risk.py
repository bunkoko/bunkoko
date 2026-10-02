"""リスク管理: ATR ベースの数量計算と、発注前チェック（上限・証拠金・停止条件）。

数量 = 許容損失額 / (損切り幅 × 1単位あたりの損益 × 為替)
荒れた相場では ATR が広がり損切り幅が広がるので、自動的に数量が縮む。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .instruments import Instrument


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade: float = 0.01          # 1回の損失 = 資金の 1%
    max_total_risk: float = 0.03          # 全ポジションの損切り時損失の合計上限
    cluster_max_risk: dict[str, float] = field(
        default_factory=lambda: {"energy": 0.015, "metals": 0.015}
    )                                     # 相関の高いグループごとの上限（WTI+ブレント など）
    max_positions_per_symbol: int = 1     # ネッティング口座・EA と挙動を揃えるため 1
    daily_loss_limit: float = 0.03        # 取引日の損失がこれを超えたら当日は新規停止
    max_drawdown_halt: float = 0.25       # 高値からの下落がこれを超えたら新規停止（手動で解除）
    max_margin_utilization: float = 0.5   # 必要証拠金の合計 ≤ 資産 × これ
    # レバレッジ上限（名目建玉 ÷ 資産、0 で無効）。損切りが効かない窓開けへの備え
    max_leverage_symbol: float = 1.0      # 1銘柄あたり
    max_leverage_total: float = 2.0       # 全銘柄の合計
    min_lot_overshoot: float = 1.0        # 最小単位に切り上げたとき許容するリスク超過倍率
    max_qty: dict[str, float] = field(default_factory=dict)  # 銘柄ごとの自主上限（単位）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SizeResult:
    qty: float
    reason: str | None = None  # 0 のときの理由


def position_size(
    equity: float,
    stop_dist: float,
    inst: Instrument,
    fx: float,
    cfg: RiskConfig,
    risk_budget: float | None = None,
    risk_mult: float = 1.0,
) -> SizeResult:
    """1回のリスクが equity × risk_per_trade × risk_mult（または risk_budget）に収まる数量を返す。"""
    if stop_dist <= 0 or equity <= 0:
        return SizeResult(0.0, "invalid")
    target = equity * cfg.risk_per_trade * risk_mult
    if risk_budget is not None:
        target = min(target, risk_budget)
    loss_per_unit = stop_dist * fx
    qty = inst.round_qty_down(target / loss_per_unit)
    cap = min(inst.max_qty, cfg.max_qty.get(inst.symbol, float("inf")))
    qty = min(qty, inst.round_qty_down(cap))
    if qty >= inst.min_qty:
        return SizeResult(qty)
    # 最小単位でもリスクが大きすぎる場合は見送る（資金不足）
    min_risk = inst.min_qty * loss_per_unit
    if min_risk <= target * cfg.min_lot_overshoot and inst.min_qty <= cap:
        return SizeResult(inst.min_qty)
    return SizeResult(0.0, "min_lot")


def required_margin(qty: float, price: float, inst: Instrument, fx: float) -> float:
    return qty * price * inst.margin_rate * fx


def notional(qty: float, price: float, fx: float) -> float:
    """名目建玉（口座通貨）。"""
    return qty * price * fx


def leverage_room(equity: float, cfg: RiskConfig, symbol_notional: float, total_notional: float) -> float:
    """レバレッジ上限まであといくら（口座通貨）の名目建玉を持てるか。上限なしなら inf。"""
    room = float("inf")
    if cfg.max_leverage_symbol > 0:
        room = min(room, cfg.max_leverage_symbol * equity - symbol_notional)
    if cfg.max_leverage_total > 0:
        room = min(room, cfg.max_leverage_total * equity - total_notional)
    return room


def open_risk(side: int, qty: float, entry: float, stop: float, fx: float) -> float:
    """現在の逆指値で決済された場合の損失額（建値より有利なら 0）。"""
    return max(0.0, side * (entry - stop)) * qty * fx
