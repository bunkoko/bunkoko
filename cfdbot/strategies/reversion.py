"""D. レンジ逆張り（補助）— 銀のレンジ局面専用。

ADX が低い（トレンドが弱い）ときだけ、価格が移動平均から 2σ 以上離れ、
かつ RSI(2) が極端な値になったら平均回帰を狙って逆張りする。
トレンド局面では大きく負けるため、単独ではなく RegimeSwitch の
「レンジ」枠で使うことを想定。窓開けに弱いので週末・指標前は手仕舞う。
原油は指標・地政学で一方向に飛びやすく、既定の構成では使わない。
"""

from __future__ import annotations

from .. import indicators as ind
from .base import Strategy, signal_frame


class RangeReversion(Strategy):
    name = "reversion"
    ea_prefix = "mr"
    default_params = {
        "period": 20,
        "z_entry": 2.0,
        "exit_z": 0.0,           # 平均（z=0）まで戻ったら手仕舞い
        "rsi_period": 2,
        "rsi_low": 10.0,
        "rsi_high": 90.0,
        "adx_period": 14,
        "adx_max": 20.0,
    }
    default_exit_overrides = {
        "init_stop_atr": 2.0,
        "trail_atr": 0.0,              # 平均回帰なのでトレーリングしない
        "breakeven_trigger_atr": 0.0,
        "max_hold_bars": 10,
        "flatten_before_weekend": True,
        "flatten_before_events": True,
    }

    def warmup_bars(self) -> int:
        return max(self.params["period"], self.params["adx_period"] * 5) + 20

    def _generate(self, df, atr):
        p = self.params
        close = df["close"]
        mid = ind.sma(close, p["period"])
        sd = ind.stdev(close, p["period"])
        z = (close - mid) / sd.where(sd > 0)
        r = ind.rsi(close, p["rsi_period"])
        ranging = ind.adx(df, p["adx_period"]) < p["adx_max"]
        long_ok = ranging & (z < -p["z_entry"]) & (r < p["rsi_low"])
        short_ok = ranging & (z > p["z_entry"]) & (r > p["rsi_high"])
        return signal_frame(
            df.index, long_ok, short_ok,
            exit_long=z >= p["exit_z"],
            exit_short=z <= -p["exit_z"],
        )
