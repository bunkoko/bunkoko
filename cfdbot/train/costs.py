"""時間足ごとのコスト診断（スプレッド・スリッページが値動きに対してどれだけ重いか）。

往復コスト（スプレッド + スリッページ×2）を、損切り幅（2.5 ATR）に対する割合で表す。
これは「1 回の取引で、損切り幅 1 つ分（1R）のうち何割がコストで消えるか」に当たる。
トレンドフォローの 1 回あたりの期待値は一般に 0.1〜0.3R 程度なので、
5% 未満なら良好、5〜10% は注意、10% を超える足は不向き。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..instruments import Instrument
from .config import tf_minutes

CHECK_TIMEFRAMES = ["M1", "M5", "M10", "M15", "M30", "H1", "H4", "D1"]


def _verdict(x: float) -> str:
    return "良好" if x < 0.05 else "注意" if x < 0.10 else "不向き"


def timeframe_costs(ds, instruments: dict[str, Instrument], stop_atr: float = 2.5,
                    atr_period: int = 20) -> pd.DataFrame:
    """ファイルがある足と、そこから作れる足のすべてについてコストを計算する。"""
    rows = []
    for sym in ds.symbols:
        inst = instruments[sym]
        finest = min(ds.file_timeframes(sym), key=tf_minutes)
        for tf in CHECK_TIMEFRAMES:
            if tf_minutes(tf) < tf_minutes(finest):
                continue
            df = ds.signal_frame(sym, tf)
            if df is None or len(df) < atr_period * 5:
                continue
            spread = np.full(len(df), inst.spread)
            if "spread" in df:
                spread = np.maximum(spread, df["spread"].to_numpy(float) * inst.point_size)
            atr = ind.atr(df, atr_period).to_numpy()[atr_period * 3:]
            med_spread = float(np.median(spread))
            med_atr = float(np.median(atr))
            cost = med_spread + 2 * inst.slippage
            per_r = cost / (stop_atr * med_atr) if med_atr > 0 else float("inf")
            rows.append({
                "symbol": sym, "timeframe": tf, "from_file": tf in ds.frames[sym],
                "spread": med_spread, "atr": med_atr, "spread_atr": med_spread / med_atr if med_atr else np.nan,
                "cost_per_r": per_r, "verdict": _verdict(per_r),
            })
    return pd.DataFrame(rows)
