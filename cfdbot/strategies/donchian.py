"""A. ドンチャン・ブレイクアウト（ATR トレンドフォロー）— 原油の主力。

直前 N 本の高値を終値で上抜けたら買い、安値を下抜けたら売り。
長期 EMA の向きと一致する方向だけに絞り、保ち合い中の往復ビンタを減らす。
手仕舞いは M 本チャネルの逆側ブレイク（M < N）と、出口管理のトレーリング。
"""

from __future__ import annotations

from .. import indicators as ind
from .base import Strategy, signal_frame


class DonchianBreakout(Strategy):
    name = "donchian"
    ea_prefix = "dc"
    default_params = {
        "entry_period": 40,   # H4 で約1.5週間
        "exit_period": 20,
        "trend_ema": 200,     # 0 でフィルタ無効
        "buffer_atr": 0.0,    # チャネルを ATR×buffer 以上抜けたときだけ採用
    }

    def validate(self) -> None:
        p = self.params
        if p["exit_period"] >= p["entry_period"]:
            raise ValueError("exit_period は entry_period より短くする")

    def warmup_bars(self) -> int:
        return max(self.params["entry_period"], self.params["trend_ema"] * 3, 60)

    def _generate(self, df, atr):
        p = self.params
        close = df["close"]
        upper, lower = ind.donchian(df, p["entry_period"])
        ex_upper, ex_lower = ind.donchian(df, p["exit_period"])
        buf = p["buffer_atr"] * atr
        long_ok = close > upper + buf
        short_ok = close < lower - buf
        if p["trend_ema"] > 0:
            trend = ind.ema(close, p["trend_ema"])
            long_ok &= close > trend
            short_ok &= close < trend
        return signal_frame(
            df.index, long_ok, short_ok,
            exit_long=close < ex_lower,
            exit_short=close > ex_upper,
        )
