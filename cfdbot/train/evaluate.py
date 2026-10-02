"""学習の最小単位（1銘柄 × 1戦略 × 1パラメータの全期間バックテスト）。

ワーカー（この Mac の各コア、iPad など）はこれを実行して、日次リターンと取引の記録を返す。
全期間を 1 回だけ走らせ、ウォークフォワードの各期間の評価はその日次リターンを切り出して行う
（指標は先読みしないので、期間ごとに走らせ直すのと同じ結果になり、計算量が期間数分の 1 になる）。

学習用のバックテストは「戦略そのものの性質」を測るため、
- 資金を大きめ（1,000万円）にして最小単位の丸めの影響を消す
- 証券会社の建玉上限・レバレッジ上限・停止条件（日次損失・最大DD）を外す
  （これらがあると 1回の損失を倍にしても損益が倍にならず、配分の計算が狂う）
実際の資金・停止条件での確認は pipeline の最終検証で行う。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import zlib
from dataclasses import asdict, dataclass, field, replace
from typing import Any

import numpy as np
import pandas as pd

from ..backtest import BacktestConfig, CostModel, FilterConfig, Sleeve, run_backtest
from ..events import Event
from ..exits import ExitConfig
from ..instruments import Instrument
from ..metrics import daily_equity
from ..risk import RiskConfig
from ..strategies import make_strategy
from .dataset import trading_days

TRAIN_EQUITY = 10_000_000.0


@dataclass(frozen=True)
class Task:
    symbol: str
    strategy: str
    params: dict[str, Any]
    exit: dict[str, Any] = field(default_factory=dict)
    pos: tuple[int, ...] = ()       # グリッド上の位置（隣接平均に使う）

    @property
    def sleeve(self) -> str:
        return f"{self.symbol}:{self.strategy}"

    @property
    def id(self) -> str:
        key = json.dumps([self.symbol, self.strategy, self.params, self.exit], sort_keys=True, default=str)
        return hashlib.sha1(key.encode()).hexdigest()[:20]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pos"] = list(self.pos)
        d["id"] = self.id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        return cls(d["symbol"], d["strategy"], dict(d["params"]), dict(d.get("exit", {})), tuple(d.get("pos", ())))


def encode_array(a: np.ndarray, dtype: str) -> str:
    return base64.b64encode(zlib.compress(np.ascontiguousarray(a, dtype=dtype).tobytes(), 6)).decode()


def decode_array(s: str, dtype: str) -> np.ndarray:
    return np.frombuffer(zlib.decompress(base64.b64decode(s)), dtype=dtype)


@dataclass
class EvalSettings:
    """ワーカーへ渡す評価条件（JSON で送れる形）。"""

    instruments: dict[str, dict]
    events: list[dict]                   # {"time": ISO(UTC), "name", "tag"}
    fx_const: float
    base_risk: float = 0.01
    spread_mult: float = 1.0
    slippage_mult: float = 1.0
    exit: dict[str, Any] = field(default_factory=dict)      # ExitConfig の既定値の上書き
    filters: dict[str, Any] = field(default_factory=dict)   # FilterConfig の上書き（時間は分）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvalSettings":
        return cls(**d)

    def instrument_map(self, uncapped: bool = False) -> dict[str, Instrument]:
        out = {k: Instrument.from_dict({"symbol": k, **{x: y for x, y in v.items() if x != "symbol"}})
               for k, v in self.instruments.items()}
        if uncapped:
            out = {k: replace(v, max_qty=float("inf")) for k, v in out.items()}
        return out

    def event_list(self) -> list[Event]:
        return [Event(pd.Timestamp(e["time"]).tz_convert("UTC"), e["name"], e["tag"]) for e in self.events]

    def filter_config(self) -> FilterConfig:
        f = dict(self.filters)
        kw: dict[str, Any] = {"extra_events": tuple(self.event_list())}
        for k in ("event_block_before", "event_block_after"):
            if k + "_min" in f:
                kw[k] = pd.Timedelta(minutes=f.pop(k + "_min"))
        kw.update(f)
        return FilterConfig(**kw)

    def backtest_config(self, fx) -> BacktestConfig:
        return BacktestConfig(
            initial_equity=TRAIN_EQUITY,
            fx_rate=fx,
            exit=ExitConfig().with_overrides(self.exit),
            risk=RiskConfig(
                risk_per_trade=self.base_risk, max_total_risk=1.0, cluster_max_risk={},
                daily_loss_limit=1.0, max_drawdown_halt=1.0,
                max_leverage_symbol=0.0, max_leverage_total=0.0,
                max_margin_utilization=float("inf"),
            ),
            costs=CostModel(spread_mult=self.spread_mult, slippage_mult=self.slippage_mult),
            filters=self.filter_config(),
        )


class Evaluator:
    def __init__(self, prices: dict[str, pd.DataFrame], fx: pd.Series | None, settings: EvalSettings):
        self.prices = prices
        self.settings = settings
        self.instruments = settings.instrument_map(uncapped=True)
        tf = next(iter(prices.values())).index
        from ..backtest import infer_timeframe

        self.timeframe = infer_timeframe(tf)
        self.calendar = trading_days(prices, self.timeframe)
        self.cfg = settings.backtest_config(fx if fx is not None else settings.fx_const)

    def run(self, task: Task) -> dict[str, Any]:
        t0 = time.perf_counter()
        strat = make_strategy(task.strategy, **task.params)
        res = run_backtest(
            {task.symbol: self.prices[task.symbol]}, self.instruments,
            [Sleeve(task.symbol, strat, dict(task.exit))], self.cfg,
        )
        daily = daily_equity(res.equity)
        ret = daily.pct_change().reindex(self.calendar).fillna(0.0).to_numpy()
        if len(res.trades):
            et = res.trades["entry_time"].dt.tz_convert("America/New_York")
            days = (et + pd.Timedelta(hours=7)).dt.normalize().dt.tz_localize(None)
            entry_idx = np.searchsorted(self.calendar.values, days.values)
            r = res.trades["r_multiple"].to_numpy()
        else:
            entry_idx = np.zeros(0, dtype=np.int32)
            r = np.zeros(0)
        return {
            "id": task.id,
            "ok": True,
            "ret": encode_array(ret, "float32"),
            "entries": encode_array(entry_idx, "int32"),
            "r": encode_array(r, "float32"),
            "n": int(len(res.trades)),
            "secs": round(time.perf_counter() - t0, 3),
        }
