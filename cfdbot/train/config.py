"""学習の設定（config/train.toml）。

ファイルに書かなかった項目は、ここの既定値が使われる。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import pandas as pd

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

TIMEFRAMES = {
    "M1": "1min", "M5": "5min", "M10": "10min", "M15": "15min", "M30": "30min",
    "H1": "1h", "H4": "4h", "D1": "1D",
}


def tf_minutes(tf: str) -> int:
    return int(pd.Timedelta(TIMEFRAMES[tf]).total_seconds() // 60)


def tf_name(delta: pd.Timedelta) -> str:
    for k, v in TIMEFRAMES.items():
        if pd.Timedelta(v) == delta:
            return k
    raise ValueError(f"対応していない時間足: {delta}")


#: 時間足を変えるとき「同じ時間幅」になるよう本数を換算するパラメータ
#: （指標の形を決めるもの: adx_period, rsi_period, bb_period などは換算しない）
SCALED_KEYS = {
    "entry_period", "exit_period", "trend_ema", "box_period", "min_squeeze_bars",
    "fast_ema", "slow_ema", "period", "exit.time_stop_bars", "exit.max_hold_bars",
}


def scale_grid(grid: dict[str, list], from_tf: str, to_tf: str) -> dict[str, list]:
    """grid_timeframe の本数で書いた探索範囲を、別の時間足の本数に換算する。"""
    ratio = tf_minutes(from_tf) / tf_minutes(to_tf)
    if ratio == 1:
        return {k: list(v) for k, v in grid.items()}
    out = {}
    for k, values in grid.items():
        if k in SCALED_KEYS:
            scaled = [v if v == 0 else max(2, int(round(v * ratio))) for v in values]
            out[k] = list(dict.fromkeys(scaled))  # 重複を除く（順番は保つ）
        else:
            out[k] = list(values)
    return out


@dataclass
class DataConfig:
    dir: str = "data"
    #: 売買判断に使う時間足（複数可）。学習で銘柄ごとに最も良い時間足が選ばれる
    signal_timeframes: list[str] = field(default_factory=lambda: ["H1"])
    #: 約定の再現に使う細かい足。"auto" = 売買の足より細かいファイルのうち最も細かいもの、"none" = 使わない
    fill_timeframe: str = "auto"
    #: [strategies.*.grid] の本数がどの時間足の本数か（他の時間足では同じ時間幅に換算）
    grid_timeframe: str = "H1"
    timeframe: str = ""          # 旧形式（signal_timeframes = [timeframe] と同じ）
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
    peak_since: str = ""                # DD の基準（最高資産）を測り始める日。DD 停止から再開するときだけ入れる


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
    timeframes: list[str] | None = None            # None = data.signal_timeframes
    grids: dict[str, dict[str, list]] = field(default_factory=dict)  # 時間足ごとの個別指定（grid_H4 など）


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
        return normalize(cfg)
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    local = local_config_path(path)
    if local.exists():  # この Mac だけの設定（ツールが書く。git の更新とぶつからないよう別ファイル）
        raw = _merge(raw, tomllib.loads(local.read_text(encoding="utf-8")))
    sections = {"data": DataConfig, "account": AccountConfig, "walkforward": WalkForwardConfig,
                "portfolio": PortfolioConfig, "compute": ComputeConfig}
    for name, cls in sections.items():
        if name in raw:
            setattr(cfg, name, _fill(cls, raw[name], name))
    if "output_dir" in raw:
        cfg.output_dir = raw["output_dir"]
    if "strategies" in raw:
        cfg.strategies = []
        for name, spec in raw["strategies"].items():
            if not spec.get("enabled", True):
                continue
            grids = {k[len("grid_"):].upper(): dict(v) for k, v in spec.items() if k.startswith("grid_")}
            tfs = spec.get("timeframes")
            cfg.strategies.append(StrategySearch(
                name=name, symbols=list(spec.get("symbols", [])), grid=dict(spec.get("grid", {})),
                fixed=dict(spec.get("fixed", {})), timeframes=[t.upper() for t in tfs] if tfs else None,
                grids=grids,
            ))
    unknown = set(raw) - set(sections) - {"strategies", "output_dir"}
    if unknown:
        raise ValueError(f"不明なセクション: {sorted(unknown)}")
    normalize(cfg)
    return cfg


def normalize(cfg: TrainConfig) -> TrainConfig:
    d = cfg.data
    if d.timeframe:
        d.signal_timeframes = [d.timeframe]
        d.timeframe = ""
    d.signal_timeframes = [t.upper() for t in d.signal_timeframes]
    d.grid_timeframe = d.grid_timeframe.upper()
    d.fill_timeframe = d.fill_timeframe.upper() if d.fill_timeframe.lower() not in ("auto", "none") else d.fill_timeframe.lower()
    for t in [*d.signal_timeframes, d.grid_timeframe] + ([d.fill_timeframe] if d.fill_timeframe not in ("auto", "none") else []):
        if t not in TIMEFRAMES:
            raise ValueError(f"対応していない時間足: {t}（{list(TIMEFRAMES)}）")
    return cfg



def local_config_path(config_path: str | Path) -> Path:
    """config/train.toml と同じフォルダの local.toml。あれば train.toml より優先する（git では管理しない）。"""
    return Path(config_path).with_name("local.toml")


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _toml_value(v: Any) -> str:
    import json

    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(v, ensure_ascii=False)


def set_local_option(config_path: str | Path, section: str, key: str, value: Any) -> bool:
    """local.toml の [section] key を設定する。変えたら True。"""
    p = local_config_path(config_path)
    data = tomllib.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    if data.get(section, {}).get(key) == value:
        return False
    data.setdefault(section, {})[key] = value
    lines = ["# この Mac だけの設定（./cfd data などのツールが書く）。config/train.toml より優先される", ""]
    for sec, items in data.items():
        lines.append(f"[{sec}]")
        lines += [f"{k} = {_toml_value(v)}" for k, v in items.items()]
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")
    return True
