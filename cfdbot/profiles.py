"""推奨構成（docs/algorithms.md の「結論」に対応）。

パラメータは H4 足を想定した **出発点** であり、実データのウォークフォワードで調整すること。
"""

from __future__ import annotations

from .backtest import BacktestConfig, FilterConfig, Sleeve
from .exits import ExitConfig
from .risk import RiskConfig
from .strategies import (
    DonchianBreakout,
    RegimeSwitch,
    SqueezeBreakout,
    TrendPullback,
)


def oil_sleeves(symbol: str = "WTI") -> list[Sleeve]:
    """原油: 主力 = ドンチャン・ブレイクアウト、副 = 押し目/戻り。"""
    return [
        Sleeve(symbol, DonchianBreakout(entry_period=40, exit_period=20, trend_ema=200)),
        Sleeve(symbol, TrendPullback(fast_ema=20, slow_ema=100, adx_min=20)),
    ]


def silver_sleeves(symbol: str = "SILVER") -> list[Sleeve]:
    """銀: 主力 = スクイーズ・ブレイクアウト、副 = 押し目/戻り。"""
    return [
        Sleeve(symbol, SqueezeBreakout()),
        Sleeve(symbol, TrendPullback(fast_ema=20, slow_ema=100, adx_min=20)),
    ]


def silver_regime_sleeves(symbol: str = "SILVER") -> list[Sleeve]:
    """銀（発展形）: ADX で局面を判定し、トレンド/中間/レンジで戦略を切り替える。Python 専用。"""
    return [Sleeve(symbol, RegimeSwitch())]


def default_sleeves() -> list[Sleeve]:
    return oil_sleeves("WTI") + silver_sleeves("SILVER")


def default_config(**kwargs) -> BacktestConfig:
    cfg = BacktestConfig(
        exit=ExitConfig(),
        risk=RiskConfig(
            risk_per_trade=0.01,
            max_total_risk=0.03,
            cluster_max_risk={"energy": 0.015, "metals": 0.015},
        ),
        filters=FilterConfig(),
    )
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg
