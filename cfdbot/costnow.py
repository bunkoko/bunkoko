"""今のスプレッドで計算し直す（docs/costnow.md）の部品。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators as ind

RECENT_DAYS = 90        # 今の水準を測る長さ（./cfd data の点検と同じ）
STOP_ATR = 2.5          # 損切り幅（./cfd data の「往復コスト ÷ 損切り幅」と同じ）
MAX_COST_RATIO = 0.10   # これを超えると「不向き」


def recent_levels(fine: pd.DataFrame, daily: pd.DataFrame, point: float, slippage: float,
                  days: int = RECENT_DAYS) -> dict:
    """書き出しの最後から days 日の、スプレッド・値段・値動き・コスト比率。"""
    t0 = fine.index[-1] - pd.Timedelta(days=days)
    recent = fine[fine.index >= t0]
    spread = float(np.median(recent["spread"].to_numpy(float) * point))
    price = float(np.median(recent["close"].to_numpy(float)))
    atr = ind.atr(daily, 20)
    atr_now = float(atr[atr.index >= daily.index[-1] - pd.Timedelta(days=days)].median())
    return {"spread": spread, "price": price, "rel": spread / price, "atr": atr_now,
            "cost_ratio": (spread + 2 * slippage) / (STOP_ATR * atr_now)}


def floor_spread(df: pd.DataFrame, rel: float, point: float) -> pd.DataFrame:
    """スプレッドを「各足の値段 × rel」以上にする（実績の方が広ければ実績）。列はポイント単位。"""
    out = df.copy()
    floor = rel * out["close"].to_numpy(float) / point
    out["spread"] = np.maximum(out["spread"].to_numpy(float), floor) if "spread" in out else floor
    return out


def verdict(sharpe_with: dict[str, float], sharpe_without: dict[str, float], cost_ratio: float) -> str:
    """docs/costnow.md 3 章の判定。sharpe_*: 期間名 → シャープ（② と ③）。期間A・B の両方が要る。"""
    if not {"A", "B"} <= set(sharpe_with) or not {"A", "B"} <= set(sharpe_without):
        return "判定できない（期間A・期間B の両方が要る。先物の日足が無ければ ./cfd context）"
    better = all(sharpe_with[k] >= sharpe_without[k] for k in ("A", "B"))
    if better and cost_ratio <= MAX_COST_RATIO:
        return "銀を今のスプレッドでも入れる"
    why = []
    if not better:
        why.append("銀を入れた方が良いのが両方の期間ではない")
    if cost_ratio > MAX_COST_RATIO:
        why.append(f"今のコスト比率 {cost_ratio:.1%} が 10% を超える")
    return "今のまま（" + "・".join(why) + "）"
