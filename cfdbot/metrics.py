"""成績指標。"""

from __future__ import annotations

import numpy as np
import pandas as pd


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float(((equity - peak) / peak).min() * -1)


def daily_equity(equity: pd.Series) -> pd.Series:
    """17:00 ET 区切りの取引日ごとの終値ベース資産。"""
    et = equity.index.tz_convert("America/New_York")
    key = (et + pd.Timedelta(hours=7)).normalize().tz_localize(None)
    return equity.groupby(key).last()


def trade_stats(trades: pd.DataFrame) -> dict[str, float]:
    if trades.empty:
        return {"trades": 0}
    pnl = trades["pnl"]
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    gross_loss = -losses.sum()
    return {
        "trades": int(len(trades)),
        "win_rate": float(len(wins) / len(trades)),
        "profit_factor": float(wins.sum() / gross_loss) if gross_loss > 0 else float("inf"),
        "avg_r": float(trades["r_multiple"].mean()),
        "avg_win_r": float(trades.loc[pnl > 0, "r_multiple"].mean()) if len(wins) else 0.0,
        "avg_loss_r": float(trades.loc[pnl <= 0, "r_multiple"].mean()) if len(losses) else 0.0,
        "net_pnl": float(pnl.sum()),
        "avg_bars_held": float(trades["bars_held"].mean()),
    }


def compute_metrics(equity: pd.Series, trades: pd.DataFrame, initial: float) -> dict[str, float]:
    out: dict[str, float] = {}
    if equity.empty:
        return {"trades": 0}
    final = float(equity.iloc[-1])
    years = max((equity.index[-1] - equity.index[0]).total_seconds() / (365.25 * 86400), 1e-9)
    total = final / initial - 1
    cagr = (final / initial) ** (1 / years) - 1 if final > 0 else -1.0
    daily = daily_equity(equity)
    rets = daily.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if len(rets) > 1 and rets.std() > 0 else 0.0
    downside = rets[rets < 0]
    sortino = (
        float(rets.mean() / downside.std() * np.sqrt(252)) if len(downside) > 1 and downside.std() > 0 else 0.0
    )
    mdd = max_drawdown(equity)
    out.update({
        "final_equity": final,
        "total_return": total,
        "cagr": cagr,
        "max_drawdown": mdd,
        "mar": cagr / mdd if mdd > 0 else float("inf") if cagr > 0 else 0.0,
        "sharpe": sharpe,
        "sortino": sortino,
        "years": years,
    })
    out.update(trade_stats(trades))
    return out


def format_metrics(m: dict[str, float]) -> str:
    pct = {"total_return", "cagr", "max_drawdown", "win_rate"}
    lines = []
    for k, v in m.items():
        if isinstance(v, float):
            lines.append(f"{k:>16}: {v:.2%}" if k in pct else f"{k:>16}: {v:,.3f}")
        else:
            lines.append(f"{k:>16}: {v}")
    return "\n".join(lines)
