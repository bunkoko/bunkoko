"""5 分足〜1 時間足の短い売買の研究（./cfd intraday）。まずコストの小さい場合で候補を探し、実際のコストで確かめる。

データ（どちらか）:
  - Dukascopy の 1 分足（売値・買値）: こちらで取得して 5 分足とスプレッドにする（複数年、UTC）
  - MT5 の 5 分足（フィリップの M5。./cfd data の data/ の CSV。スプレッドの列つき）

速く多くを比べるため、ここでは「足の終値で向きを決め、次の足の値動きを取る」簡単な計算で見る（損切りの細かい動きは
入れない）。候補が見つかったら、本番と同じバックテスト（cfdbot/backtest.py）で確かめ直す。
設定と判定の条件は docs/intraday.md（結果を見る前に決めたもの）。
"""

from __future__ import annotations

import lzma
import struct
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ET = "America/New_York"
LONDON = "Europe/London"


@dataclass(frozen=True)
class Market:
    key: str
    duka: str            # Dukascopy の銘柄名
    divisor: float       # Dukascopy の価格の整数 → 価格
    label: str
    phillip_spread: float | None = None    # フィリップの実測スプレッドの中央値（2026-10 の ./cfd data）


MARKETS: tuple[Market, ...] = (
    Market("GOLD", "XAUUSD", 1000, "金", 0.56),
    Market("SILVER", "XAGUSD", 1000, "銀", 0.0451),
    Market("WTI", "LIGHTCMDUSD", 1000, "WTI 原油", 0.069),
    Market("BRENT", "BRENTCMDUSD", 1000, "ブレント原油", 0.08),
    Market("USDJPY", "USDJPY", 1000, "ドル円", None),
    Market("SP500", "USA500IDXUSD", 1000, "S&P500", None),
    Market("NIKKEI", "JPNIDXJPY", 1000, "日経平均", None),
    Market("BTC", "BTCUSD", 10, "ビットコイン", None),
)
MARKET_BY_KEY = {m.key: m for m in MARKETS}


# --------------------------------------------------------------------------- Dukascopy
def duka_url(symbol: str, day: date, side: str) -> str:
    return (f"https://datafeed.dukascopy.com/datafeed/{symbol}/{day.year}/{day.month - 1:02d}/{day.day:02d}/"
            f"{side}_candles_min_1.bi5")


def parse_candles(raw: bytes, day: date, divisor: float) -> pd.DataFrame:
    """1 日分の 1 分足（bi5: LZMA で圧縮した 24 バイトずつの記録。秒, 始値, 終値, 安値, 高値, 出来高）。"""
    cols = ["open", "high", "low", "close", "volume"]
    if not raw:
        return pd.DataFrame(columns=cols)
    data = lzma.decompress(raw)
    n = len(data) // 24
    if n == 0:
        return pd.DataFrame(columns=cols)
    rec = np.array(struct.unpack(">" + "IIIIIf" * n, data[: n * 24]), dtype=float).reshape(n, 6)
    idx = pd.Timestamp(day, tz="UTC") + pd.to_timedelta(rec[:, 0], unit="s")
    df = pd.DataFrame({"open": rec[:, 1] / divisor, "close": rec[:, 2] / divisor, "low": rec[:, 3] / divisor,
                       "high": rec[:, 4] / divisor, "volume": rec[:, 5]}, index=idx)
    df = df[df["volume"] > 0]                      # 取引の無い分（週末・休み）は除く
    return df[cols]


def to_m5(bid: pd.DataFrame, ask: pd.DataFrame) -> pd.DataFrame:
    """1 分足の売値・買値 → 5 分足（売値と買値の中値の OHLC、スプレッドは 5 分の平均）。"""
    if bid.empty or ask.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "spread"])
    j = bid.join(ask, rsuffix="_a", how="inner")
    mid = pd.DataFrame({c: (j[c] + j[f"{c}_a"]) / 2 for c in ("open", "high", "low", "close")})
    mid["spread"] = (j["close_a"] - j["close"]).clip(lower=0)
    agg = mid.resample("5min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "spread": "mean"}).dropna()
    return agg


def m5_path(root: str | Path, key: str, month: str) -> Path:
    return Path(root) / key / f"{month}.csv.gz"


def load_m5(root: str | Path, key: str) -> pd.DataFrame:
    """保存した 5 分足（UTC）をまとめて読む。"""
    files = sorted((Path(root) / key).glob("*.csv.gz"))
    if not files:
        return pd.DataFrame(columns=["open", "high", "low", "close", "spread"])
    df = pd.concat([pd.read_csv(f, index_col=0) for f in files])
    df.index = pd.DatetimeIndex(pd.to_datetime(df.index, utc=True))
    return df[~df.index.duplicated(keep="last")].sort_index()


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if rule == "5min":
        return df
    return df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "spread": "mean"}).dropna()


# --------------------------------------------------------------------------- 売買のルール（足ごとの向き −1/0/+1）
def donchian(df: pd.DataFrame, n: int, exit_n: int) -> np.ndarray:
    """直近 n 本の高値を終値で超えたら買い、exit_n 本の安値を割るまで持つ（売りは逆）。"""
    c = df["close"].to_numpy()
    hi = df["high"].rolling(n).max().shift(1).to_numpy()
    lo = df["low"].rolling(n).min().shift(1).to_numpy()
    xhi = df["high"].rolling(exit_n).max().shift(1).to_numpy()
    xlo = df["low"].rolling(exit_n).min().shift(1).to_numpy()
    pos = np.zeros(len(c))
    p = 0.0
    for i in range(len(c)):
        if p > 0 and c[i] < xlo[i]:
            p = 0.0
        elif p < 0 and c[i] > xhi[i]:
            p = 0.0
        if p == 0:
            if c[i] > hi[i]:
                p = 1.0
            elif c[i] < lo[i]:
                p = -1.0
        pos[i] = p
    return pos


def reversion(df: pd.DataFrame, n: int = 20, z_in: float = 2.0) -> np.ndarray:
    """ボリンジャーの逆張り: 平均から標準偏差の z_in 倍離れたら逆に入り、平均に戻ったら出る。"""
    c = df["close"]
    z = ((c - c.rolling(n).mean()) / c.rolling(n).std()).to_numpy()
    pos = np.zeros(len(z))
    p = 0.0
    for i in range(len(z)):
        if not np.isfinite(z[i]):
            pos[i] = p
            continue
        if p > 0 and z[i] >= 0:
            p = 0.0
        elif p < 0 and z[i] <= 0:
            p = 0.0
        if p == 0:
            if z[i] < -z_in:
                p = 1.0
            elif z[i] > z_in:
                p = -1.0
        pos[i] = p
    return pos


def opening_range(df: pd.DataFrame, tz: str, open_hm: tuple[int, int], close_hm: tuple[int, int],
                  minutes: int) -> np.ndarray:
    """寄り付きの値幅ブレイク: その市場の時刻で open_hm から minutes 分の高値・安値を、終値で抜けた向きに入る。

    反対側を終値で割ったら手仕舞い。close_hm の足で必ず手仕舞い（その日のうちに終える）。
    """
    local = df.index.tz_convert(tz)
    tmin = local.hour * 60 + local.minute
    o = open_hm[0] * 60 + open_hm[1]
    e = close_hm[0] * 60 + close_hm[1]
    day = np.asarray(local.normalize().asi8)
    c, h, lo_ = df["close"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
    pos = np.zeros(len(c))
    cur_day, rh, rl, p, done = None, -np.inf, np.inf, 0.0, False
    for i in range(len(c)):
        if day[i] != cur_day:
            cur_day, rh, rl, p, done = day[i], -np.inf, np.inf, 0.0, False
        t = tmin[i]
        if o <= t < o + minutes:
            rh, rl = max(rh, h[i]), min(rl, lo_[i])
        elif o + minutes <= t < e and np.isfinite(rh):
            if p > 0 and c[i] < rl:
                p, done = 0.0, True
            elif p < 0 and c[i] > rh:
                p, done = 0.0, True
            elif p == 0 and not done:
                if c[i] > rh:
                    p = 1.0
                elif c[i] < rl:
                    p = -1.0
        else:
            p = 0.0
        pos[i] = p if t < e - 5 else 0.0      # 終わりの足の前に手仕舞う
    return pos


def intraday_momentum(df: pd.DataFrame) -> np.ndarray:
    """日中のモメンタム（Gao ほか 2018 の考え方）: 米国 9:30 から 15:00 の向きに、15:00〜16:00 だけ持つ。"""
    local = df.index.tz_convert(ET)
    tmin = local.hour * 60 + local.minute
    day = np.asarray(local.normalize().asi8)
    c = df["close"].to_numpy()
    pos = np.zeros(len(c))
    cur_day, open_px, sgn = None, np.nan, 0.0
    for i in range(len(c)):
        if day[i] != cur_day:
            cur_day, open_px, sgn = day[i], np.nan, 0.0
        t = tmin[i]
        if t == 9 * 60 + 30 or (np.isnan(open_px) and 9 * 60 + 30 <= t < 10 * 60):
            open_px = df["open"].iat[i]
        if t == 15 * 60 - 5 and np.isfinite(open_px):           # 14:55 の足の終値 = 15:00 時点
            sgn = float(np.sign(c[i] / open_px - 1))
        pos[i] = sgn if 15 * 60 - 5 <= t < 16 * 60 - 5 else 0.0
    return pos


@dataclass(frozen=True)
class Rule:
    key: str
    label: str
    timeframe: str        # "5min" / "15min" / "1h"


def rules() -> list[Rule]:
    """結果を見る前に決めた 14 のやり方（docs/intraday.md）。"""
    out = []
    for tf, per_day in (("5min", 288), ("15min", 96), ("1h", 24)):
        out.append(Rule(f"dc_day_{tf}", f"ブレイク（1 日分の高値・安値）{tf}", tf))
        out.append(Rule(f"dc_week_{tf}", f"ブレイク（1 週間分の高値・安値）{tf}", tf))
        out.append(Rule(f"rev_{tf}", f"逆張り（ボリンジャー 20 本・2σ）{tf}", tf))
    out += [Rule("orb_ldn30", "ロンドン寄り付き 30 分の値幅ブレイク", "5min"),
            Rule("orb_ldn60", "ロンドン寄り付き 60 分の値幅ブレイク", "5min"),
            Rule("orb_ny30", "ニューヨーク寄り付き 30 分の値幅ブレイク", "5min"),
            Rule("orb_ny60", "ニューヨーク寄り付き 60 分の値幅ブレイク", "5min"),
            Rule("mom_ny", "日中モメンタム（9:30〜15:00 の向きに 15〜16 時）", "5min")]
    return out


def positions(rule: Rule, df: pd.DataFrame) -> np.ndarray:
    per_day = {"5min": 288, "15min": 96, "1h": 24}[rule.timeframe]
    if rule.key.startswith("dc_day"):
        return donchian(df, per_day, per_day // 2)
    if rule.key.startswith("dc_week"):
        return donchian(df, per_day * 5, per_day * 5 // 2)
    if rule.key.startswith("rev"):
        return reversion(df)
    if rule.key.startswith("orb_ldn"):
        return opening_range(df, LONDON, (8, 0), (16, 30), int(rule.key[-2:]))
    if rule.key.startswith("orb_ny"):
        return opening_range(df, ET, (9, 30), (16, 0), int(rule.key[-2:]))
    if rule.key == "mom_ny":
        return intraday_momentum(df)
    raise ValueError(rule.key)


# --------------------------------------------------------------------------- 成績
TARGET_DAILY_VOL = 0.15 / np.sqrt(252)


@dataclass
class RunResult:
    daily_net: pd.Series         # 日ごとの損益（値動きでそろえた率、コスト込み）
    daily_gross: pd.Series       # コスト無し
    turnover: float              # 売買した量の合計（そろえた名目）
    gross_total: float
    trades_per_year: float


def evaluate(df: pd.DataFrame, pos: np.ndarray, spread: np.ndarray) -> RunResult:
    """足の終値で決めた向き pos を、次の足の値動きに当てる。量はその市場の 1 日の値動きでそろえる（年率 15%）。

    spread: 足ごとのスプレッド（価格）。向きを 1 変えるごとに、スプレッドの半分 ÷ 価格 を払う。
    """
    c = df["close"].to_numpy(float)
    r_next = np.r_[c[1:] / c[:-1] - 1, 0.0]
    day_close = df["close"].resample("1D").last().dropna()
    dvol = day_close.pct_change().ewm(span=60, min_periods=20).std().shift(1)     # 前日までで決める
    lev_day = np.minimum(TARGET_DAILY_VOL / dvol, 10.0)
    lev = lev_day.reindex(df.index.floor("1D")).to_numpy()
    lev = np.where(np.isfinite(lev), lev, 0.0)
    u = pos * lev
    du = np.abs(np.diff(np.r_[0.0, u]))
    cost = du * (spread / 2) / c
    gross = u * r_next
    days = df.index.floor("1D")
    g = pd.Series(gross, index=days).groupby(level=0).sum()
    n = pd.Series(gross - np.nan_to_num(cost), index=days).groupby(level=0).sum()
    years = max((df.index[-1] - df.index[0]).days / 365.25, 1e-9)
    entries = int(np.sum((np.diff(np.r_[0.0, pos]) != 0) & (pos != 0)))
    return RunResult(n, g, float(du.sum()), float(gross.sum()), entries / years)


def sharpe(r: pd.Series) -> float:
    r = r.dropna()
    if len(r) < 60 or r.std() <= 0:
        return float("nan")
    return float(r.mean() / r.std() * np.sqrt(252))


def breakeven_bps(res: RunResult) -> float:
    """損益が 0 になる往復コスト（価格に対する bp）。これより安い売買先なら、コスト込みでもプラス。"""
    if res.turnover <= 0:
        return float("nan")
    return float(2 * res.gross_total / res.turnover * 1e4)


def fetch_days(start: date, end: date, weekend: bool) -> list[date]:
    """取得する日。週末に取引の無い市場は土曜を飛ばす（日曜の夕方から始まるので日曜は取る）。"""
    out, d = [], start
    while d <= end:
        if weekend or d.weekday() != 5:
            out.append(d)
        d += timedelta(days=1)
    return out
