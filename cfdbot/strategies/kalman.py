"""F. なめらかなトレンド（カルマンフィルタ）— 物理のモデル版のトレンドフォロー（研究用。docs/trend.md 6 章）。

対数の価格を「流れ＋揺れ」で表し、流れの推定値がその不確かさの z_in 倍を超えたら買い（下回ったら売り）。
流れの符号が変わったら手仕舞い。パラメータ（流れの変わりやすさ・揺れの大きさ）はデータから最尤法で推定し、
refit_bars ごとに、その時点より前の値だけで推定し直す。EA には移していない。
"""

from __future__ import annotations

from .. import indicators as ind
from ..kalman import trend_z
from .base import Strategy, signal_frame


class KalmanTrend(Strategy):
    name = "kalman"
    ea_prefix = "kf"
    default_params = {
        "z_in": 1.0,             # 流れ ÷ 不確かさ がこれを超えたら入る
        "min_fit": 500,          # 最初の推定に要る本数
        "refit_bars": 250,       # 推定し直す間隔
        "fit_window": 2000,      # 推定に使う直近の本数
        "max_extension": 0.0,    # 行き過ぎフィルタ（ドンチャンと同じ。0 で無効）
        "extension_period": 20,
        "prior_close": None,     # パラメータの推定に使う別の系列（例: 先物の長い履歴）。None なら自分の終値
    }

    def warmup_bars(self) -> int:
        return int(self.params["min_fit"])

    def _generate(self, df, atr):
        p = self.params
        close = df["close"]
        z = trend_z(close, p["prior_close"], int(p["min_fit"]), int(p["refit_bars"]), int(p["fit_window"]))
        long_ok = z > p["z_in"]
        short_ok = z < -p["z_in"]
        if p["max_extension"] > 0:
            ext = ind.extension_z(close, p["extension_period"])
            long_ok &= ~(ext > p["max_extension"])
            short_ok &= ~(ext < -p["max_extension"])
        return signal_frame(df.index, long_ok, short_ok, exit_long=z < 0, exit_short=z > 0)
