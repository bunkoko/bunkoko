"""学習の設定（config/train.toml）。

ファイルに書かなかった項目は、ここの既定値が使われる。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.10 以前
    import tomli as tomllib  # type: ignore[no-redef]

#: MT5 のシンボル名 → このツールの銘柄キー（"FX" は円換算用の USD/JPY）
DEFAULT_SYMBOL_MAP = {
    "XAGUSD": "SILVER", "SILVER": "SILVER",
    "XAUUSD": "GOLD", "GOLD": "GOLD",
    "XTIUSD": "WTI", "USOIL": "WTI", "WTI": "WTI",
    "XBRUSD": "BRENT", "UKOIL": "BRENT", "BRENT": "BRENT",
    "USDJPY": "FX",
}

TIMEFRAMES = {"M15": "15min", "M30": "30min", "H1": "1h", "H4": "4h", "D1": "1D"}


@dataclass
class DataConfig:
    dir: str = "data"
    timeframe: str = "H1"
    server_tz: Any = "ny_close"
    broker: str = "phillip"
    instruments: str = ""        # 銘柄仕様の JSON（実測スプレッドなど）。空なら証券会社の既定値
    events: str = ""             # 指標イベント CSV。空なら API/EIA の定例のみ
    fx: float = 150.0            # USDJPY のファイルが無いときの円換算レート
    symbol_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SYMBOL_MAP))


@dataclass
class AccountConfig:
    equity: float = 1_000_000            # 実際の運用資金（円）。最終検証と EA 書き出しに使う
    base_risk: float = 0.01              # 学習時の 1回の損失（配分の基準）
    min_risk_per_trade: float = 0.0025   # 配分後の 1回の損失の下限
    max_risk_per_trade: float = 0.02     # 配分後の 1回の損失の上限
    max_total_risk: float = 0.04
    cluster_max_risk: dict[str, float] = field(default_factory=lambda: {"energy": 0.02, "metals": 0.02})
    max_leverage_symbol: float = 1.0
    max_leverage_total: float = 2.0
    daily_loss_limit: float = 0.03
    max_drawdown_halt: float = 0.25


@dataclass
class WalkForwardConfig:
    train_months: int = 24
    test_months: int = 6
    objective: str = "sharpe"   # sharpe / sortino / mar
    min_trades: int = 15        # 学習期間内の取引回数がこれ未満のパラメータは選ばない
    plateau: bool = True        # 隣り合うパラメータとの平均で評価（尖った最適値を避ける）


@dataclass
class PortfolioConfig:
    allocation: str = "erc"            # erc（リスク寄与を均等）/ inverse_vol / sharpe_tilt / equal
    target_vol: float = 0.12           # 学習期間での年率ボラティリティの目標
    max_train_dd: float = 0.15         # 学習期間での最大DDの上限（超えたら全体を縮小）
    min_score: float = 0.3             # 学習期間の評価値（シャープレシオ等）がこれ未満の戦略は使わない
    max_strategies_per_symbol: int = 1 # 1銘柄で同時に使う戦略の数（EA は 1 銘柄 1 ポジション）
    shrinkage: float = 0.3             # 共分散の縮小推定（0=標本そのまま、1=相関を無視）


@dataclass
class ComputeConfig:
    time_budget_hours: float = 12.0    # これを超えそうならパラメータの組み合わせを間引く
    workers: int = 0                   # この Mac で使う並列数（0 = CPU数-1）
    listen: str = "127.0.0.1"          # "0.0.0.0" にすると iPad など他の端末も参加できる
    port: int = 8765
    seed: int = 0


@dataclass
class StrategySearch:
    name: str
    symbols: list[str]
    grid: dict[str, list]
    fixed: dict[str, Any] = field(default_factory=dict)


def default_strategies() -> list[StrategySearch]:
    """H1 用の探索範囲（本数は H1 の足数。H4 の 4 倍が同じ時間幅）。"""
    trend = ["WTI", "BRENT", "SILVER", "GOLD"]
    return [
        StrategySearch("donchian", trend, {
            "entry_period": [60, 120, 180, 240],
            "exit_period": [30, 60, 90],
            "trend_ema": [0, 400, 800],
            "exit.trail_atr": [2.5, 3.5, 4.5],
        }),
        StrategySearch("squeeze", trend, {
            "box_period": [20, 40, 60],
            "min_squeeze_bars": [6, 12, 20],
            "kc_mult": [1.25, 1.5, 1.75],
            "stop_box_frac": [0.5, 1.0],
            "exit.time_stop_bars": [24, 48],
        }),
        StrategySearch("pullback", trend, {
            "fast_ema": [20, 40, 80],
            "slow_ema": [100, 200, 400],
            "adx_min": [15.0, 20.0, 25.0],
            "touch_atr": [0.5, 1.0],
            "exit.trail_atr": [2.5, 3.5],
        }),
        StrategySearch("reversion", ["SILVER", "GOLD"], {
            "period": [20, 40],
            "z_entry": [1.8, 2.2],
            "rsi_period": [2, 3],
            "adx_max": [15.0, 20.0, 25.0],
            "exit.max_hold_bars": [24, 48],
        }),
    ]


@dataclass
class TrainConfig:
    data: DataConfig = field(default_factory=DataConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    walkforward: WalkForwardConfig = field(default_factory=WalkForwardConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    compute: ComputeConfig = field(default_factory=ComputeConfig)
    strategies: list[StrategySearch] = field(default_factory=default_strategies)
    output_dir: str = "output/train"


def _fill(cls, raw: dict[str, Any], section: str):
    valid = {f.name for f in fields(cls)}
    unknown = set(raw) - valid
    if unknown:
        raise ValueError(f"[{section}] に不明な項目: {sorted(unknown)}")
    obj = cls()
    for k, v in raw.items():
        if isinstance(getattr(obj, k), dict) and isinstance(v, dict):
            v = {**getattr(obj, k), **v}
        setattr(obj, k, v)
    return obj


def load_train_config(path: str | Path | None) -> TrainConfig:
    cfg = TrainConfig()
    if path is None or not Path(path).exists():
        return cfg
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    sections = {"data": DataConfig, "account": AccountConfig, "walkforward": WalkForwardConfig,
                "portfolio": PortfolioConfig, "compute": ComputeConfig}
    for name, cls in sections.items():
        if name in raw:
            setattr(cfg, name, _fill(cls, raw[name], name))
    if "output_dir" in raw:
        cfg.output_dir = raw["output_dir"]
    if "strategies" in raw:
        cfg.strategies = [
            StrategySearch(name=name, symbols=list(spec.get("symbols", [])),
                           grid=dict(spec.get("grid", {})), fixed=dict(spec.get("fixed", {})))
            for name, spec in raw["strategies"].items()
            if spec.get("enabled", True)
        ]
    unknown = set(raw) - set(sections) - {"strategies", "output_dir"}
    if unknown:
        raise ValueError(f"不明なセクション: {sorted(unknown)}")
    return cfg
