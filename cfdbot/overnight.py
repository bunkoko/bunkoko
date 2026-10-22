"""J2 夜だけ持つ（記録だけの売買。docs/overnight.md）。お金は動かさず、値段の日足から後で計算する。

1306（TOPIX の ETF）を毎営業日の引け（板寄せ）で買ったことにし、翌営業日の寄り（板寄せ）で売ったことにする。
比べる相手は「1306 を持ち続ける」。値段は配当と分割で調整した始値・終値（夜に持つ側も、権利付最終日の引けで持っていれば配当を受け取る）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TICKER = "1306.T"
START = "2026-10-20"                 # この日の引けで最初に買ったことにする
COSTS = (0.0, 0.0002)                # 往復のコスト: 0 と 0.02%（判定は 0.02%）
JUDGE_COST = 0.0002
LOOKS = (250, 500, 750)              # 見る時点（夜の数）
T_CONTINUES = 1.0                    # 「続いている」: 差がプラスで、日ごとの差の t 値が 1.0 以上
TAX = 0.20315                        # 参考: 毎年の利益に当てる税
DAYS_PER_YEAR = 245
MAX_ABS = 0.30                       # 1 晩・1 日で 30% を超える動きは、データの誤りとして除く
REF_FROM = "2010-01-01"              # 参考（判定しない）: 過去の同じ計算


def fetch_adjusted(ticker: str = TICKER, since: str = "2009-01-01") -> pd.DataFrame:
    """Yahoo の日足（配当・分割で調整した始値・終値）。列: date / open / close。"""
    import yfinance as yf  # type: ignore

    h = yf.Ticker(ticker).history(start=since, interval="1d", auto_adjust=True, raise_errors=True, timeout=30)
    if h is None or h.empty:
        raise RuntimeError(f"Yahoo: {ticker} のデータが空")
    idx = h.index.tz_localize(None) if h.index.tz is not None else h.index
    return pd.DataFrame({"date": idx.strftime("%Y-%m-%d"), "open": h["Open"].to_numpy(float),
                         "close": h["Close"].to_numpy(float)}).dropna().drop_duplicates("date", keep="last")


def legs(prices: pd.DataFrame) -> pd.DataFrame:
    """買った日（t）ごとに: night = t の引け → t+1 の寄り、hold = t の引け → t+1 の引け、day = t+1 の寄り → 引け。"""
    p = prices.assign(date=pd.to_datetime(prices["date"])).set_index("date").sort_index()
    p = p[(p["open"] > 0) & (p["close"] > 0)]
    nxt_open, nxt_close = p["open"].shift(-1), p["close"].shift(-1)
    out = pd.DataFrame({"night": nxt_open / p["close"] - 1, "hold": nxt_close / p["close"] - 1, "day": nxt_close / nxt_open - 1},
                       index=p.index)
    out["sold_on"] = pd.Series(p.index, index=p.index).shift(-1)
    out = out.dropna()
    bad = (out[["night", "hold", "day"]].abs() > MAX_ABS).any(axis=1)
    return out[~bad]


def ann(r: pd.Series) -> float:
    r = r.dropna()
    if not len(r):
        return float("nan")
    return float(np.prod(1 + r.to_numpy()) ** (DAYS_PER_YEAR / len(r)) - 1)


def t_value(x: pd.Series) -> float:
    x = x.dropna()
    if len(x) < 3 or x.std(ddof=1) <= 0:
        return float("nan")
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


def after_tax_yearly(r: pd.Series, rate: float = TAX) -> float:
    """毎日売り買いする側: 暦年ごとの利益（プラスのときだけ）に税を当てた後の、全期間の増え方（倍率 − 1）。"""
    wealth = 1.0
    for _, g in r.groupby(r.index.year):
        gain = float(np.prod(1 + g.to_numpy()) - 1)
        wealth *= 1 + (gain * (1 - rate) if gain > 0 else gain)
    return wealth - 1


def after_tax_at_end(r: pd.Series, rate: float = TAX) -> float:
    """持ち続ける側: 最後に売って、全体の利益に一度だけ税を当てた場合。"""
    gain = float(np.prod(1 + r.dropna().to_numpy()) - 1)
    return gain * (1 - rate) if gain > 0 else gain


def summary(lg: pd.DataFrame, cost: float) -> dict:
    """夜だけ（コストを引いた後）と持ち続けの、年率・差・日ごとの差の t 値。"""
    night = lg["night"] - cost
    diff = night - lg["hold"]
    return {"nights": len(lg), "night_ann": ann(night), "hold_ann": ann(lg["hold"]), "day_ann": ann(lg["day"]),
            "diff_ann": ann(night) - ann(lg["hold"]), "t": t_value(diff),
            "night_after_tax": after_tax_yearly(night), "hold_after_tax": after_tax_at_end(lg["hold"]),
            "night_total": float(np.prod(1 + night.to_numpy()) - 1) if len(night) else float("nan"),
            "hold_total": float(np.prod(1 + lg["hold"].to_numpy()) - 1) if len(lg) else float("nan")}


def verdict(s: dict) -> str:
    """docs/overnight.md 3 章（判定は往復 0.02% で、250・500・750 夜の時点）。"""
    n = s["nights"]
    looks = [k for k in LOOKS if n >= k]
    if not looks:
        return f"途中経過（{n} 夜。{LOOKS[0]} 夜で 1 回目、あと {LOOKS[0] - n} 夜）"
    if not np.isfinite(s["diff_ann"]) or s["diff_ann"] <= 0:
        return f"続いていない（{looks[-1]} 夜の時点で、夜だけ − 持ち続け がプラスでない）"
    if np.isfinite(s["t"]) and s["t"] >= T_CONTINUES:
        return f"続いている（{looks[-1]} 夜の時点で、差がプラス・t 値 {s['t']:.2f}）"
    return f"まだ分からない（{looks[-1]} 夜の時点で、差はプラスだが t 値 {s['t']:.2f} が {T_CONTINUES} 未満）"


def forward(lg: pd.DataFrame, start: str = START, upto: int | None = None) -> pd.DataFrame:
    """始めた日からの分だけ（upto を渡せば最初の upto 夜まで。見る時点の判定に使う）。"""
    f = lg[lg.index >= pd.Timestamp(start)]
    return f.iloc[:upto] if upto else f
