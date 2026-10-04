"""EA のシグナル記録と Python のシグナルを突き合わせる（EA の移植が正しいかの確認）。

手順:
1. MT5 のストラテジーテスターで EA を ea_log_signals=true にして実行
   （scripts/mt5_files.py install が置くテスター用プリセットはオンになっている）
   → MT5 の共通フォルダ（Common\\Files）に cfdbot_signals_<MT5の銘柄名>_<magic>.csv ができる
2. python scripts/mt5_files.py fetch-logs で output/compare/ に取り込む
3. 学習結果（final.json）を指定して比べる（データは config/train.toml の [data] dir から読む）:

    python scripts/compare_signals.py --final output/train/<日時>/final.json --symbol SILVER \\
        --ea-log output/compare/cfdbot_signals_XAGUSD_2609001.csv

   学習結果を使わず、戦略とパラメータを直接指定することもできる:

    python scripts/compare_signals.py --csv data/XAGUSD_H1.csv --strategy squeeze --params '{}' \\
        --ea-log output/compare/cfdbot_signals_XAGUSD_2609001.csv

EA は直近 ea_calc_bars 本だけで指標を計算するため、長い EMA では記録の最初の方に
僅かな差が出ることがある（ea_calc_bars を増やすと解消）。
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
from cfdbot.backtest import htf_trend, infer_timeframe  # noqa: E402
from cfdbot.data import load_mt5_csv, resample_ohlc  # noqa: E402
from cfdbot.exits import ExitConfig  # noqa: E402
from cfdbot.strategies import STRATEGIES, make_strategy  # noqa: E402
from cfdbot.train.config import TIMEFRAMES, load_train_config  # noqa: E402
from cfdbot.train.dataset import load_dataset  # noqa: E402


def python_signals_from_final(final_path: str, symbol: str, config: str, data: str | None = None) -> pd.DataFrame:
    final = json.loads(Path(final_path).read_text(encoding="utf-8"))
    sleeve = next((s for s in final["sleeves"] if s["symbol"] == symbol.upper()), None)
    if sleeve is None:
        raise SystemExit(f"{final_path} に {symbol} の構成が無い（{[s['symbol'] for s in final['sleeves']]}）")
    cfg = load_train_config(config)
    if data:
        cfg.data.dir = data
    ds = load_dataset(cfg.data)
    tf = sleeve["timeframe"]
    frame = ds.signal_frame(sleeve["symbol"], tf)
    strat = make_strategy(sleeve["strategy"], **sleeve["params"])
    atr_period = ExitConfig().with_overrides(strat.exit_overrides()).with_overrides(sleeve["exit"]).atr_period
    atr = ind.atr(frame, atr_period)
    sig = strat.generate(frame, atr)
    if sleeve.get("htf"):
        htf = sleeve["htf"]
        tr, _ = htf_trend(frame.index, pd.Timedelta(TIMEFRAMES[tf]), ds.context_frame(sleeve["symbol"], htf["timeframe"]),
                          int(htf["ema"]))
        e = sig["entry"].to_numpy()
        sig["entry"] = np.where(((e > 0) & (tr <= 0)) | ((e < 0) & (tr >= 0)), 0, e)
    sig["atr"] = atr
    print(f"比較する構成: {symbol} {sleeve['strategy']} @{tf}" + (f" + 上位足 {sleeve['htf']}" if sleeve.get("htf") else ""))
    return sig


def python_signals_from_csv(csv: str, server_tz, strategy: str, params: dict, timeframe: str | None,
                            atr_period: int) -> pd.DataFrame:
    df = load_mt5_csv(csv, server_tz=server_tz)
    if timeframe and infer_timeframe(df.index) != pd.Timedelta(TIMEFRAMES[timeframe]):
        df = resample_ohlc(df, TIMEFRAMES[timeframe], server_tz)  # 細かい足のファイルから作る（MT5 と同じ区切り）
    atr = ind.atr(df, atr_period)
    sig = make_strategy(strategy, **params).generate(df, atr)
    sig["atr"] = atr
    return sig


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ea-log", required=True, help="EA が書き出した cfdbot_signals_*.csv")
    p.add_argument("--final", help="学習結果の final.json")
    p.add_argument("--symbol", help="--final のどの銘柄と比べるか（例: SILVER）")
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--data", help="データフォルダ（設定より優先）")
    p.add_argument("--csv", help="（直接指定する場合）MT5 から書き出したバー CSV")
    p.add_argument("--timeframe", help="（直接指定する場合）売買の足。CSV より粗ければ CSV から作る")
    p.add_argument("--server-tz", default="ny_close")
    p.add_argument("--strategy", choices=[s for s in STRATEGIES if s != "regime"])
    p.add_argument("--params", default="{}")
    p.add_argument("--atr-period", type=int, default=20)
    args = p.parse_args()

    if args.final:
        if not args.symbol:
            raise SystemExit("--final を使うときは --symbol も指定する")
        sig = python_signals_from_final(args.final, args.symbol, args.config, args.data)
    else:
        if not (args.csv and args.strategy):
            raise SystemExit("--final か、--csv と --strategy のどちらかを指定する")
        tz = args.server_tz
        try:
            tz = float(tz)
        except ValueError:
            pass
        sig = python_signals_from_csv(args.csv, tz, args.strategy, json.loads(args.params), args.timeframe,
                                      args.atr_period)

    ea = pd.read_csv(args.ea_log)
    ea.index = pd.to_datetime(ea["time_utc"], format="%Y.%m.%d %H:%M").dt.tz_localize("UTC")
    ea = ea[~ea.index.duplicated(keep="last")]
    ea["stop_dist"] = ea["stop_dist"].where(ea["stop_dist"] > 0)
    common = ea.index.intersection(sig.index)
    if common.empty:
        raise SystemExit("時刻が 1 本も一致しない。時間足と [data] server_tz を確認すること")
    e, s = ea.loc[common], sig.loc[common]

    atr_diff = (e["atr"] - s["atr"]).abs() / s["atr"]
    entry_mismatch = common[(e["entry"].to_numpy() != s["entry"].to_numpy())]
    exits = ["exit_long", "exit_short"]
    exit_mismatch = common[(e[exits].astype(bool).to_numpy() != s[exits].to_numpy()).any(axis=1)]
    stop_diff = (e["stop_dist"] - s["stop_dist"]).abs().dropna()

    print(f"比較した足: {len(common)} 本（EA {len(ea)} / Python {len(sig)}）")
    print(f"ATR の最大相対誤差: {atr_diff.max():.2e}")
    print(f"エントリー不一致: {len(entry_mismatch)} 本（EA のエントリー {int((e['entry'] != 0).sum())} 回）")
    print(f"手仕舞いシグナル不一致: {len(exit_mismatch)} 本")
    if len(stop_diff):
        print(f"損切り幅の最大差: {stop_diff.max():.6f}")
    if len(entry_mismatch):
        print("\n不一致の例（最初の 10 本）:")
        show = pd.DataFrame({"ea": e.loc[entry_mismatch[:10], "entry"], "python": s.loc[entry_mismatch[:10], "entry"]})
        print(show.to_string())
    ok = len(entry_mismatch) == 0 and len(exit_mismatch) == 0 and np.nan_to_num(atr_diff.max()) < 1e-6
    print("\n結果:", "一致" if ok else "差分あり（上記を確認。記録の最初の方だけなら ea_calc_bars 不足）")


if __name__ == "__main__":
    main()
