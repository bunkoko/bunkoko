"""時刻・イベント関連（EIA/API 在庫統計、マクロ指標、週末）。

内部の時刻はすべて UTC（tz-aware）。市場の区切りは米東部時間 (ET) で判定する。
CME の商品先物は 日〜金 18:00 ET 開始 / 17:00 ET 終了（毎日 17:00-18:00 ET が休止）。

- API 週間在庫統計: 毎週火曜 16:30 ET（日本時間 水曜 5:30 / 冬時間 6:30）
- EIA 週間石油在庫統計: 毎週水曜 10:30 ET（日本時間 23:30 / 冬時間 0:30）
  米国の祝日週は発表日がずれるので、その週は CSV で追加指定すること。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
TRADING_DAY_ROLL_ET = 17  # 17:00 ET で取引日が切り替わる

# (曜日 0=月, 時刻 ET, 名前, タグ)
RECURRING_OIL_EVENTS = (
    (1, time(16, 30), "API", "oil"),
    (2, time(10, 30), "EIA", "oil"),
)


@dataclass(frozen=True)
class Event:
    time: pd.Timestamp   # UTC
    name: str
    tag: str             # Instrument.event_tags と照合する（例: "oil", "usd_macro"）または銘柄キー


def recurring_oil_events(start: pd.Timestamp, end: pd.Timestamp) -> list[Event]:
    """期間内の API/EIA 定例発表時刻を UTC で列挙する。"""
    start_et = pd.Timestamp(start).tz_convert(ET).normalize() - pd.Timedelta(days=1)
    end_et = pd.Timestamp(end).tz_convert(ET).normalize() + pd.Timedelta(days=1)
    out: list[Event] = []
    for day in pd.date_range(start_et.tz_localize(None), end_et.tz_localize(None), freq="D"):
        for weekday, t, name, tag in RECURRING_OIL_EVENTS:
            if day.weekday() == weekday:
                ts = pd.Timestamp.combine(day.date(), t).tz_localize(ET).tz_convert(UTC)
                out.append(Event(ts, name, tag))
    return out


def load_events_csv(path: str | Path) -> list[Event]:
    """イベント CSV を読む。列: time_utc, name, tag（例: 2026-10-28 18:00, FOMC, usd_macro）。

    time_utc は UTC。日本時間で書きたい場合は列名を time_jst にする。
    """
    df = pd.read_csv(path, comment="#", skipinitialspace=True)
    if "time_utc" in df:
        ts = pd.to_datetime(df["time_utc"]).dt.tz_localize(UTC)
    elif "time_jst" in df:
        ts = pd.to_datetime(df["time_jst"]).dt.tz_localize("Asia/Tokyo").dt.tz_convert(UTC)
    else:
        raise ValueError("events CSV には time_utc または time_jst 列が必要")
    return [Event(t, str(n), str(g).strip()) for t, n, g in zip(ts, df["name"], df["tag"])]


class EventIndex:
    """タグごとのイベント時刻を保持し、時間窓の判定を高速に行う。"""

    def __init__(self, events: list[Event]):
        by_tag: dict[str, list[int]] = {}
        for e in events:
            by_tag.setdefault(e.tag, []).append(pd.Timestamp(e.time).value)
        self._by_tag = {k: np.array(sorted(v), dtype=np.int64) for k, v in by_tag.items()}

    def _arrays(self, tags) -> list[np.ndarray]:
        return [self._by_tag[t] for t in tags if t in self._by_tag]

    def in_window(self, t: pd.Timestamp, tags, before: pd.Timedelta, after: pd.Timedelta) -> bool:
        """時刻 t が「イベントの before 前 〜 after 後」の範囲に入っているか。"""
        tv = pd.Timestamp(t).value
        lo, hi = tv - after.value, tv + before.value
        for arr in self._arrays(tags):
            i = np.searchsorted(arr, lo, side="left")
            if i < len(arr) and arr[i] <= hi:
                return True
        return False

    def upcoming(self, t: pd.Timestamp, tags, within: pd.Timedelta) -> bool:
        """(t, t+within] にイベントがあるか。"""
        tv = pd.Timestamp(t).value
        for arr in self._arrays(tags):
            i = np.searchsorted(arr, tv, side="right")
            if i < len(arr) and arr[i] <= tv + within.value:
                return True
        return False


def to_et(t: pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(t).tz_convert(ET)


def trading_day(t: pd.Timestamp) -> pd.Timestamp:
    """17:00 ET 区切りの取引日（日付）。"""
    et = to_et(t)
    return (et + pd.Timedelta(hours=24 - TRADING_DAY_ROLL_ET)).normalize().tz_localize(None)


def friday_cutoff_passed(t: pd.Timestamp, cutoff_hour_et: float) -> bool:
    """t が「金曜 cutoff_hour_et 以降 〜 日曜の再開前」に入っているか。"""
    et = to_et(t)
    wd = et.weekday()
    hour = et.hour + et.minute / 60.0
    if wd == 4:
        return hour >= cutoff_hour_et
    if wd == 5:
        return True
    if wd == 6:
        return hour < 18.0
    return False


def server_to_utc(index: pd.DatetimeIndex, server_tz: str | int | float) -> pd.DatetimeIndex:
    """MT5 サーバー時刻（tz なし）を UTC に変換する。

    server_tz:
        "ny_close" : サーバー時刻 = 米東部時間 + 7時間（NY 17:00 = サーバー 0:00。
                     冬 GMT+2 / 夏 GMT+3 の業者に多い方式）
        数値        : 固定オフセット（時間）。例: 9 → 日本時間
        "Area/City": IANA タイムゾーン名
    証券会社のサーバー時刻の方式は必ず確認すること。
    """
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        return idx.tz_convert(UTC)
    if isinstance(server_tz, (int, float)):
        return (idx - timedelta(hours=float(server_tz))).tz_localize(UTC)
    if server_tz == "ny_close":
        shifted = idx - timedelta(hours=7)
        return shifted.tz_localize(ET, ambiguous="NaT", nonexistent="shift_forward").tz_convert(UTC)
    return idx.tz_localize(server_tz, ambiguous="NaT", nonexistent="shift_forward").tz_convert(UTC)
