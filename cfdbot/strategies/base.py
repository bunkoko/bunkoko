"""戦略のインターフェース。アルゴリズムを差し替えるのはこの層だけ。

戦略は OHLC の DataFrame を受け取り、足ごとの売買シグナルを返す。
シグナルは「その足の終値で判断 → 次の足の始値で執行」という前提で、
各行はその足の終値までの情報しか使ってはいけない（tests で検査している）。

返す DataFrame の列:
    entry      : +1 買い / -1 売り / 0 なし
    stop_dist  : 初期損切りまでの距離（価格単位）。NaN なら出口設定の ATR 倍率を使う
    exit_long  : True なら保有中の買いを次の始値で手仕舞い
    exit_short : True なら保有中の売りを次の始値で手仕舞い
    tag        : （任意）複合戦略でどの子戦略のシグナルかを示す文字列
複合戦略は子戦略ごとの手仕舞い列 "exit_long:<tag>" / "exit_short:<tag>" も返す。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from .. import indicators as ind


class Strategy(ABC):
    name: ClassVar[str] = "base"
    #: EA のパラメータ名の接頭辞（例: "dc" → dc_entry_period）
    ea_prefix: ClassVar[str] = ""
    default_params: ClassVar[dict[str, Any]] = {}
    #: この戦略に合わせた出口設定の既定値（ExitConfig のフィールド名）
    default_exit_overrides: ClassVar[dict[str, Any]] = {}

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"{self.name}: unknown params {sorted(unknown)}")
        self.params: dict[str, Any] = {**self.default_params, **params}
        self.validate()

    def validate(self) -> None:  # サブクラスで必要に応じて上書き
        pass

    def __repr__(self) -> str:
        changed = {k: v for k, v in self.params.items() if self.default_params.get(k) != v}
        return f"{type(self).__name__}({', '.join(f'{k}={v!r}' for k, v in changed.items())})"

    @abstractmethod
    def warmup_bars(self) -> int:
        """指標が安定するまでに必要な本数の目安。"""

    @abstractmethod
    def _generate(self, df: pd.DataFrame, atr: pd.Series) -> pd.DataFrame:
        ...

    def generate(self, df: pd.DataFrame, atr: pd.Series | None = None) -> pd.DataFrame:
        """シグナルを計算する。atr は出口管理と同じ ATR を渡す（None なら ATR(20)）。"""
        if atr is None:
            atr = ind.atr(df, 20)
        out = self._generate(df, atr)
        return finalize_signals(out, df.index)

    def exit_overrides(self, tag: str | None = None) -> dict[str, Any]:
        """ポジションに適用する出口設定の上書き。複合戦略は tag ごとに返す。"""
        return dict(self.default_exit_overrides)

    def clone(self, **params: Any) -> "Strategy":
        return type(self)(**{**self.params, **params})


def finalize_signals(out: pd.DataFrame, index: pd.Index) -> pd.DataFrame:
    out = out.reindex(index)
    out["entry"] = out["entry"].fillna(0).astype(np.int8)
    if "stop_dist" not in out:
        out["stop_dist"] = np.nan
    out["stop_dist"] = out["stop_dist"].astype(float)
    for col in [c for c in out.columns if c.startswith("exit_")]:
        out[col] = out[col].fillna(False).astype(bool)
    for col in ("exit_long", "exit_short"):
        if col not in out:
            out[col] = False
    return out


def signal_frame(
    index: pd.Index,
    long_entry: pd.Series,
    short_entry: pd.Series,
    stop_long: pd.Series | None = None,
    stop_short: pd.Series | None = None,
    exit_long: pd.Series | None = None,
    exit_short: pd.Series | None = None,
) -> pd.DataFrame:
    """買い/売り条件から標準形式のシグナル表を組み立てるヘルパー。"""
    long_entry = long_entry.fillna(False).astype(bool)
    short_entry = short_entry.fillna(False).astype(bool)
    entry = pd.Series(0, index=index, dtype=np.int8)
    entry[long_entry & ~short_entry] = 1
    entry[short_entry & ~long_entry] = -1
    stop = pd.Series(np.nan, index=index)
    if stop_long is not None:
        stop = stop.mask(entry == 1, stop_long)
    if stop_short is not None:
        stop = stop.mask(entry == -1, stop_short)
    false = pd.Series(False, index=index)
    return pd.DataFrame(
        {
            "entry": entry,
            "stop_dist": stop,
            "exit_long": false if exit_long is None else exit_long.fillna(False).astype(bool),
            "exit_short": false if exit_short is None else exit_short.fillna(False).astype(bool),
        },
        index=index,
    )
