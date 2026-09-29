from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cfdbot.data import synthetic_ohlc  # noqa: E402
from cfdbot.strategies.base import Strategy  # noqa: E402


class FixedSignals(Strategy):
    """テスト用: 指定した足で指定の方向にシグナルを出す。"""

    name = "fixed"
    default_params = {"entries": {}, "stop_dist": None, "exits": {}}

    def warmup_bars(self) -> int:
        return 0

    def _generate(self, df, atr):
        n = len(df)
        entry = np.zeros(n, dtype=np.int8)
        for i, side in self.params["entries"].items():
            entry[i] = side
        stop = np.full(n, np.nan if self.params["stop_dist"] is None else self.params["stop_dist"])
        ex_l = np.zeros(n, bool)
        ex_s = np.zeros(n, bool)
        for i, side in self.params["exits"].items():
            (ex_l if side > 0 else ex_s)[i] = True
        return pd.DataFrame(
            {"entry": entry, "stop_dist": stop, "exit_long": ex_l, "exit_short": ex_s}, index=df.index
        )


def make_bars(rows, start="2026-09-08 01:00", hours=4) -> pd.DataFrame:
    """rows: [(open, high, low, close), ...] を火曜 01:00 UTC から H4 で並べる。"""
    idx = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=len(rows), freq=f"{hours}h")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)


@pytest.fixture(scope="session")
def oil_df() -> pd.DataFrame:
    return synthetic_ohlc("oil", start="2022-01-02", end="2024-12-31", seed=3)


@pytest.fixture(scope="session")
def silver_df() -> pd.DataFrame:
    return synthetic_ohlc("silver", start="2022-01-02", end="2024-12-31", seed=4)
