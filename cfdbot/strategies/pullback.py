"""C. トレンド押し目買い / 戻り売り — 原油・銀の両方（副戦略）。

EMA(短) > EMA(長)、長期 EMA が上向き、ADX が閾値以上 = 上昇トレンド。
その中で安値が短期 EMA 付近まで押した後、前の足の高値を終値で上抜けたら買う。
原油の「強いトレンドと急反転」に対して、高値掴みしやすいブレイク買いより
有利な価格・狭い損切りで入れる。損切りは押し安値の少し下。
"""

from __future__ import annotations

from .. import indicators as ind
from .base import Strategy, signal_frame


class TrendPullback(Strategy):
    name = "pullback"
    ea_prefix = "pb"
    default_params = {
        "fast_ema": 20,
        "slow_ema": 100,
        "slope_bars": 5,         # 長期 EMA の傾き判定（n 本前との比較）
        "adx_period": 14,
        "adx_min": 20.0,
        "touch_atr": 0.5,        # 安値が 短期EMA + ATR×touch_atr 以下なら「押した」
        "setup_bars": 3,         # 押しを探す直近本数（現在足を含む）
        "swing_bars": 5,         # 損切りの基準にする直近安値/高値の本数
        "stop_buffer_atr": 0.5,
    }

    def validate(self) -> None:
        if self.params["fast_ema"] >= self.params["slow_ema"]:
            raise ValueError("fast_ema は slow_ema より短くする")

    def warmup_bars(self) -> int:
        return self.params["slow_ema"] * 3

    def _generate(self, df, atr):
        p = self.params
        close, high, low = df["close"], df["high"], df["low"]
        fast = ind.ema(close, p["fast_ema"])
        slow = ind.ema(close, p["slow_ema"])
        strength = ind.adx(df, p["adx_period"]) >= p["adx_min"]
        up = (fast > slow) & (slow > slow.shift(p["slope_bars"])) & (close > slow) & strength
        dn = (fast < slow) & (slow < slow.shift(p["slope_bars"])) & (close < slow) & strength

        touched_long = (low <= fast + p["touch_atr"] * atr).astype(float)
        touched_short = (high >= fast - p["touch_atr"] * atr).astype(float)
        setup_long = touched_long.rolling(p["setup_bars"]).max() > 0
        setup_short = touched_short.rolling(p["setup_bars"]).max() > 0

        long_ok = up & setup_long & (close > high.shift(1))
        short_ok = dn & setup_short & (close < low.shift(1))

        swing_low = low.rolling(p["swing_bars"]).min()
        swing_high = high.rolling(p["swing_bars"]).max()
        stop_long = close - (swing_low - p["stop_buffer_atr"] * atr)
        stop_short = (swing_high + p["stop_buffer_atr"] * atr) - close
        return signal_frame(
            df.index, long_ok, short_ok, stop_long, stop_short,
            exit_long=fast < slow,
            exit_short=fast > slow,
        )
