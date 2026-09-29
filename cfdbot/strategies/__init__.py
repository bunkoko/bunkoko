"""戦略の登録簿。新しいアルゴリズムは Strategy を継承して STRATEGIES に追加するだけ。"""

from __future__ import annotations

from typing import Any

from .base import Strategy, finalize_signals, signal_frame
from .donchian import DonchianBreakout
from .pullback import TrendPullback
from .regime import RegimeSwitch
from .reversion import RangeReversion
from .squeeze import SqueezeBreakout

STRATEGIES: dict[str, type[Strategy]] = {
    cls.name: cls
    for cls in (DonchianBreakout, SqueezeBreakout, TrendPullback, RangeReversion, RegimeSwitch)
}


def make_strategy(name: str, **params: Any) -> Strategy:
    try:
        cls = STRATEGIES[name]
    except KeyError:
        raise ValueError(f"unknown strategy '{name}' (choose from {list(STRATEGIES)})") from None
    return cls(**params)


__all__ = [
    "STRATEGIES",
    "Strategy",
    "DonchianBreakout",
    "SqueezeBreakout",
    "TrendPullback",
    "RangeReversion",
    "RegimeSwitch",
    "make_strategy",
    "finalize_signals",
    "signal_frame",
]
