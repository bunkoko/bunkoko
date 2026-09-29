"""EA のシグナル記録と Python のシグナルを突き合わせる（移植の検証用）。

手順:
1. MT5 のストラテジーテスターで EA を ea_log_signals=true で実行
   → Common/Files/cfdbot_signals_<銘柄>_<magic>.csv ができる
2. 同じ銘柄・時間足のバーを CSV に書き出す
3. python scripts/compare_signals.py --csv data/XAGUSD_H4.csv \
       --ea-log cfdbot_signals_XAGUSD_2609001.csv --strategy squeeze --params '{}'

EA は直近 ea_calc_bars 本だけで指標を計算するため、EMA が長い戦略では
計算開始直後に僅かな差が出ることがある（本数を増やすと解消）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cfdbot import indicators as ind  # noqa: E402
from cfdbot.data import load_mt5_csv  # noqa: E402
from cfdbot.strategies import STRATEGIES, make_strategy  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", required=True, help="MT5 から書き出したバー CSV")
    p.add_argument("--server-tz", default="ny_close")
    p.add_argument("--ea-log", required=True)
    p.add_argument("--strategy", required=True, choices=[s for s in STRATEGIES if s != "regime"])
    p.add_argument("--params", default="{}")
    p.add_argument("--atr-period", type=int, default=20)
    args = p.parse_args()

    tz = args.server_tz
    try:
        tz = float(tz)
    except ValueError:
        pass
    df = load_mt5_csv(args.csv, server_tz=tz)
    atr = ind.atr(df, args.atr_period)
    sig = make_strategy(args.strategy, **json.loads(args.params)).generate(df, atr)
    sig["atr"] = atr

    ea = pd.read_csv(args.ea_log)
    ea.index = pd.to_datetime(ea["time_utc"], format="%Y.%m.%d %H:%M").dt.tz_localize("UTC")
    ea["stop_dist"] = ea["stop_dist"].where(ea["stop_dist"] > 0)
    common = ea.index.intersection(sig.index)
    if common.empty:
        raise SystemExit("時刻が1本も一致しない。--server-tz を確認すること")
    e, s = ea.loc[common], sig.loc[common]

    atr_diff = (e["atr"] - s["atr"]).abs() / s["atr"]
    entry_mismatch = common[(e["entry"].to_numpy() != s["entry"].to_numpy())]
    exits = ["exit_long", "exit_short"]
    exit_mismatch = common[(e[exits].astype(bool).to_numpy() != s[exits].to_numpy()).any(axis=1)]
    stop_diff = (e["stop_dist"] - s["stop_dist"]).abs().dropna()

    print(f"比較した足: {len(common)} 本（EA {len(ea)} / Python {len(sig)}）")
    print(f"ATR の最大相対誤差: {atr_diff.max():.2e}")
    print(f"エントリー不一致: {len(entry_mismatch)} 本")
    print(f"手仕舞いシグナル不一致: {len(exit_mismatch)} 本")
    if len(stop_diff):
        print(f"損切り幅の最大差: {stop_diff.max():.6f}")
    if len(entry_mismatch):
        print("\n不一致の例:")
        show = pd.DataFrame({"ea": e.loc[entry_mismatch[:10], "entry"], "python": s.loc[entry_mismatch[:10], "entry"]})
        print(show.to_string())
    ok = len(entry_mismatch) == 0 and len(exit_mismatch) == 0 and np.nan_to_num(atr_diff.max()) < 1e-6
    print("\n結果:", "一致" if ok else "差分あり（上記を確認）")


if __name__ == "__main__":
    main()
