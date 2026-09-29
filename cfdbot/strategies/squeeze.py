"""B. ボラティリティ・スクイーズ・ブレイクアウト — 銀の主力。

ボリンジャーバンド(2σ)がケルトナーチャネル(1.5ATR)の内側に収まる
「収縮」が一定本数続いた後、その収縮ボックスの高値/安値を終値で抜けたら順張り。
銀は「静かな期間 → 一気に走る」を繰り返しやすく、収縮中は ATR が小さいため
同じ許容損失でも損切り幅を狭く置ける。
失敗したブレイクは早く失敗しやすいので、時間ストップを既定で有効にしている。
"""

from __future__ import annotations

from .. import indicators as ind
from .base import Strategy, signal_frame


class SqueezeBreakout(Strategy):
    name = "squeeze"
    ea_prefix = "sq"
    default_params = {
        "bb_period": 20,
        "bb_mult": 2.0,
        "kc_mult": 1.5,
        "box_period": 20,        # 収縮を数える期間 = ボックスの期間
        "min_squeeze_bars": 6,   # 直前 box_period 本のうち収縮だった本数の下限
        "stop_box_frac": 0.5,    # 損切り位置: 0=ブレイク位置, 0.5=ボックス中央, 1=反対側
    }
    default_exit_overrides = {
        "time_stop_bars": 12,    # 12本たっても +0.5R に届かなければ撤退
        "time_stop_min_r": 0.5,
    }

    def validate(self) -> None:
        p = self.params
        if not 0 < p["min_squeeze_bars"] <= p["box_period"]:
            raise ValueError("min_squeeze_bars は 1..box_period")
        if not 0.0 <= p["stop_box_frac"] <= 1.0:
            raise ValueError("stop_box_frac は 0..1")

    def warmup_bars(self) -> int:
        return max(self.params["bb_period"], self.params["box_period"]) + 60

    def _generate(self, df, atr):
        p = self.params
        close = df["close"]
        sd = ind.stdev(close, p["bb_period"])
        squeeze_on = (p["bb_mult"] * sd) < (p["kc_mult"] * atr)
        squeeze_count = squeeze_on.astype(float).rolling(p["box_period"]).sum().shift(1)
        box_high, box_low = ind.donchian(df, p["box_period"])
        armed = squeeze_count >= p["min_squeeze_bars"]
        long_ok = armed & (close > box_high)
        short_ok = armed & (close < box_low)
        height = box_high - box_low
        stop_long = close - (box_high - p["stop_box_frac"] * height)
        stop_short = (box_low + p["stop_box_frac"] * height) - close
        return signal_frame(df.index, long_ok, short_ok, stop_long, stop_short)
