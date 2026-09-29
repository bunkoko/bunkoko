"""データ取得: MT5 から書き出した CSV の読み込み、足の変換、動作確認用の合成データ。

バックテストには **本番と同じ証券会社のデータ** を使うこと（スプレッド・取引時間・
限月調整が会社ごとに違う）。MT5 では「表示 → 銘柄」（Ctrl+U）のバーのタブで
銘柄・時間足・期間を選んで CSV を書き出せる。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .events import ET, server_to_utc

_OHLC = ["open", "high", "low", "close"]


def load_mt5_csv(path: str | Path, server_tz: str | int | float = "ny_close") -> pd.DataFrame:
    """MT5 の「バー」書き出し CSV（<DATE> <TIME> <OPEN> ... <SPREAD>）を読み込む。

    ヘッダーが time/open/high/low/close 形式の一般的な CSV にも対応する。
    戻り値は UTC の DatetimeIndex（足の開始時刻）と open/high/low/close[/volume/spread] 列。
    """
    path = Path(path)
    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        header = f.readline()
    sep = "\t" if "\t" in header else ";" if header.count(";") > header.count(",") else ","
    df = pd.read_csv(path, sep=sep, encoding="utf-8-sig")
    df.columns = [c.strip().strip("<>").lower() for c in df.columns]

    if "date" in df and "time" in df:
        raw = df["date"].astype(str) + " " + df["time"].astype(str)
    elif "date" in df:
        raw = df["date"].astype(str)
    else:
        col = next((c for c in ("time", "datetime", "timestamp") if c in df), None)
        if col is None:
            raise ValueError(f"{path}: 日時の列が見つからない（columns={list(df.columns)}）")
        raw = df[col].astype(str)
    ts = _parse_datetime(raw)
    out = df[_OHLC].astype(float).copy()
    if "tickvol" in df:
        out["volume"] = df["tickvol"].astype(float)
    elif "volume" in df:
        out["volume"] = df["volume"].astype(float)
    if "spread" in df:
        out["spread"] = df["spread"].astype(float)
    out.index = server_to_utc(pd.DatetimeIndex(ts), server_tz)
    out = out[~out.index.isna()]
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out.index.name = "time"
    validate_ohlc(out)
    return out


def _parse_datetime(raw: pd.Series) -> pd.Series:
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y.%m.%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return pd.to_datetime(raw, format=fmt)
        except (ValueError, TypeError):
            continue
    return pd.to_datetime(raw)


def validate_ohlc(df: pd.DataFrame) -> None:
    bad = (df["high"] < df[["open", "close"]].max(axis=1)) | (df["low"] > df[["open", "close"]].min(axis=1))
    if bad.any():
        raise ValueError(f"OHLC が不正な足が {int(bad.sum())} 本ある（最初: {df.index[bad.argmax()]}）")


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """下位足から上位足を作る（例: H1 → H4）。17:00 ET 区切りに合わせる。"""
    et = df.tz_convert(ET)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df:
        agg["volume"] = "sum"
    if "spread" in df:
        agg["spread"] = "median"
    out = et.resample(rule, offset=pd.Timedelta(hours=17) if rule.upper() in ("1D", "D") else None).agg(agg)
    out = out.dropna(subset=["open"])
    out.index = out.index.tz_convert("UTC")
    return out


# ---------------------------------------------------------------------------
# 合成データ（テスト・デモ専用。成績評価には絶対に使わないこと）
# ---------------------------------------------------------------------------

_PROFILES = {
    # 1日あたりのボラティリティ、ジャンプ頻度、EIA 時のジャンプ、初値、スプレッド(point)
    "oil": dict(daily_vol=0.025, jump_p=0.004, jump_sigma=4.0, eia_sigma=2.0, price=90.0, spread_pts=4, point=0.01),
    "silver": dict(daily_vol=0.020, jump_p=0.003, jump_sigma=4.0, eia_sigma=0.0, price=70.0, spread_pts=30, point=0.001),
    "gold": dict(daily_vol=0.010, jump_p=0.002, jump_sigma=3.0, eia_sigma=0.0, price=2500.0, spread_pts=40, point=0.01),
}


def synthetic_ohlc(
    kind: str = "oil",
    start: str = "2021-01-03",
    end: str = "2026-09-25",
    hours: int = 4,
    seed: int = 0,
) -> pd.DataFrame:
    """レジーム切り替え + GARCH 型のボラティリティ集中 + ジャンプを持つ合成 OHLC。

    取引時間は CME に合わせて 日 17:00 ET 〜 金 17:00 ET、足は 17:00 ET 起点。
    """
    prof = _PROFILES[kind]
    rng = np.random.default_rng(seed)
    bars_per_day = 24 // hours
    # ET の壁時計で 17:00 起点の足を作り、週末を除外する（17:00 ET 以降は翌営業日扱い）
    first = pd.Timestamp(start).normalize() + pd.Timedelta(hours=17)
    grid = pd.date_range(first, pd.Timestamp(end), freq=f"{hours}h")
    grid = grid[(grid + pd.Timedelta(hours=7)).weekday < 5]
    idx = grid.tz_localize(ET, ambiguous="NaT", nonexistent="NaT")
    idx = idx[~idx.isna()]
    n = len(idx)

    base = prof["daily_vol"] / np.sqrt(bars_per_day)
    # GARCH(1,1) 風の分散
    omega, alpha, beta = base**2 * 0.03, 0.07, 0.90
    var = base**2
    # レジーム: 0=上昇, 1=下落, 2=レンジ
    stay = 0.994
    regime = 2
    home = anchor = np.log(prof["price"])
    logp = anchor
    o = np.empty(n); h = np.empty(n); l = np.empty(n); c = np.empty(n); sp = np.empty(n)
    prev_t = None
    start_min = idx.hour * 60 + idx.minute
    is_eia = (idx.weekday == 2) & (start_min <= 630) & (630 < start_min + hours * 60)  # 10:30 ET
    for k in range(n):
        if rng.random() > stay:
            regime = int(rng.integers(0, 3))
            anchor = logp
        sigma = np.sqrt(var)
        drift = {0: 0.12 * sigma, 1: -0.12 * sigma, 2: -0.03 * (logp - anchor)}[regime]
        drift -= 0.0015 * (logp - home)  # 長期的には初値近辺に戻す（価格が発散しないように）
        gap = 0.0
        if prev_t is not None and (idx[k] - prev_t) > pd.Timedelta(hours=hours * 3):
            gap = rng.normal(0, 1.5 * sigma)  # 週末の窓
        open_lp = logp + gap
        diffusive = rng.normal(0, sigma)
        shock = diffusive
        if rng.random() < prof["jump_p"]:
            shock += rng.normal(0, prof["jump_sigma"] * sigma)
        if is_eia[k] and prof["eia_sigma"] > 0:
            shock += rng.normal(0, prof["eia_sigma"] * sigma)
        steps = 12
        path = open_lp + np.cumsum(rng.normal(0, sigma / np.sqrt(steps), steps))
        path += np.linspace(0, drift + shock - (path[-1] - open_lp), steps)  # 終値を合わせる
        close_lp = path[-1]
        o[k] = np.exp(open_lp)
        c[k] = np.exp(close_lp)
        h[k] = np.exp(max(open_lp, path.max()))
        l[k] = np.exp(min(open_lp, path.min()))
        var = omega + alpha * diffusive**2 + beta * var  # ジャンプは分散の更新に含めない
        var = min(var, (base * 5) ** 2)
        wide = 3.0 if (prev_t is None or (idx[k] - prev_t) > pd.Timedelta(hours=hours)) else 1.0
        sp[k] = prof["spread_pts"] * wide * (1 + 0.3 * rng.random())
        logp = close_lp
        prev_t = idx[k]
    tick = prof["point"]
    df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx.tz_convert("UTC"))
    df = (df / tick).round() * tick
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)
    df["spread"] = np.round(sp)
    df.index.name = "time"
    return df
