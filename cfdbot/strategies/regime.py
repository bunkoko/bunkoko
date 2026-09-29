"""E. レジーム切り替え（複合戦略）。

ADX で相場を「トレンド / 中間 / レンジ」に分類し、局面ごとに使う子戦略を切り替える。
各局面の中では並び順が優先順位（最初にシグナルを出した子戦略を採用）。
ポジションはエントリーした子戦略の手仕舞いルール・出口設定で管理される。

既定の構成（銀向け）:
    トレンド (ADX>=25): pullback → donchian
    中間               : squeeze
    レンジ   (ADX<20)  : squeeze → reversion
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .. import indicators as ind
from .base import Strategy, finalize_signals

REGIMES = ("trend", "neutral", "range")


class RegimeSwitch(Strategy):
    name = "regime"
    ea_prefix = ""   # EA 未対応（Python 側のみ）
    default_params = {
        "adx_period": 14,
        "trend_adx": 25.0,
        "range_adx": 20.0,
        "trend": ("pullback", "donchian"),
        "neutral": ("squeeze",),
        "range": ("squeeze", "reversion"),
        "children": {},   # {"donchian": {...params}} 子戦略のパラメータ上書き
    }

    def validate(self) -> None:
        from . import STRATEGIES  # 循環 import 回避

        p = self.params
        if p["range_adx"] > p["trend_adx"]:
            raise ValueError("range_adx <= trend_adx にする")
        names: list[str] = []
        for regime in REGIMES:
            for n in p[regime]:
                if n not in names:
                    names.append(n)
        unknown = [n for n in names if n not in STRATEGIES or n == self.name]
        if unknown:
            raise ValueError(f"unknown child strategies: {unknown}")
        self.children: dict[str, Strategy] = {
            n: STRATEGIES[n](**p["children"].get(n, {})) for n in names
        }

    def warmup_bars(self) -> int:
        return max([c.warmup_bars() for c in self.children.values()] + [self.params["adx_period"] * 5])

    def exit_overrides(self, tag: str | None = None) -> dict[str, Any]:
        if tag in self.children:
            return self.children[tag].exit_overrides()
        return {}

    def regime(self, df: pd.DataFrame) -> pd.Series:
        a = ind.adx(df, self.params["adx_period"])
        out = pd.Series("neutral", index=df.index, dtype=object)
        out[a >= self.params["trend_adx"]] = "trend"
        out[a < self.params["range_adx"]] = "range"
        return out

    def _generate(self, df, atr):
        p = self.params
        regime = self.regime(df).to_numpy()
        child_sig = {n: c.generate(df, atr) for n, c in self.children.items()}
        n = len(df)
        entry = np.zeros(n, dtype=np.int8)
        stop = np.full(n, np.nan)
        tag = np.full(n, None, dtype=object)
        for r in REGIMES:
            mask = regime == r
            # 優先度の低い順に上書きしていき、最後に優先度の高いものが残るようにする
            for child in reversed(p[r]):
                s = child_sig[child]
                hit = mask & (s["entry"].to_numpy() != 0)
                entry[hit] = s["entry"].to_numpy()[hit]
                stop[hit] = s["stop_dist"].to_numpy()[hit]
                tag[hit] = child
        out = pd.DataFrame({"entry": entry, "stop_dist": stop, "tag": tag}, index=df.index)
        for child, s in child_sig.items():
            out[f"exit_long:{child}"] = s["exit_long"]
            out[f"exit_short:{child}"] = s["exit_short"]
        return finalize_signals(out, df.index)
