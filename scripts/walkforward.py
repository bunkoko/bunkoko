"""ウォークフォワード最適化して、最新期間のパラメータを EA 用に書き出す。

例:
    python scripts/walkforward.py --csv WTI=data/WTI_H4.csv --symbol WTI --strategy donchian \
        --grid '{"entry_period": [30, 40, 55], "exit_period": [10, 20], "exit.trail_atr": [2.5, 3, 3.5]}' \
        --train 24 --test 6 --export

--export を付けると output/ea/ に .set（MT5 のパラメータ読み込み用）と
.txt（EA が定期的に読み直す用。MT5 の Common/Files に置く）を書き出す。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cfdbot.backtest import Sleeve  # noqa: E402
from cfdbot.cli import add_common_args, config_from_args, instruments_from_args, parse_csv_args  # noqa: E402
from cfdbot.export import export_sleeve  # noqa: E402
from cfdbot.profiles import default_config  # noqa: E402
from cfdbot.strategies import STRATEGIES, make_strategy  # noqa: E402
from cfdbot.walkforward import walk_forward  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(p)
    p.add_argument("--symbol", required=True)
    p.add_argument("--strategy", required=True, choices=list(STRATEGIES))
    p.add_argument("--grid", required=True, help='探索範囲（JSON）。出口設定は "exit.trail_atr" のように指定')
    p.add_argument("--fixed", default="{}", help="固定する戦略パラメータ（JSON）")
    p.add_argument("--train", type=int, default=24, help="学習期間（月）")
    p.add_argument("--test", type=int, default=6, help="検証期間（月）")
    p.add_argument("--anchored", action="store_true", help="学習開始日を固定（拡大ウィンドウ）")
    p.add_argument("--objective", default="mar", choices=["mar", "sharpe"])
    p.add_argument("--min-trades", type=int, default=20)
    p.add_argument("--no-plateau", action="store_true", help="隣接平均を使わず単点の最良値を採用")
    p.add_argument("--jobs", type=int, default=None, help="並列数（既定: CPU数-1）")
    p.add_argument("--export", action="store_true", help="最新期間の採用パラメータを EA 用に書き出す")
    args = p.parse_args()

    data = parse_csv_args(args.csv, args.server_tz)
    sym = args.symbol.upper()
    inst = instruments_from_args(args)
    cfg = config_from_args(args, default_config())
    fixed = json.loads(args.fixed)
    wf = walk_forward(
        data, inst, sym, args.strategy, json.loads(args.grid),
        fixed_params=fixed, config=cfg, train_months=args.train, test_months=args.test,
        anchored=args.anchored, objective=args.objective, min_trades=args.min_trades,
        plateau=not args.no_plateau, n_jobs=args.jobs,
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wf.windows.to_csv(out / f"wf_{sym}_{args.strategy}_windows.csv", index=False)
    wf.oos_equity.to_csv(out / f"wf_{sym}_{args.strategy}_oos_equity.csv")
    wf.oos_trades.to_csv(out / f"wf_{sym}_{args.strategy}_oos_trades.csv", index=False)
    print(wf.windows.to_string(index=False))
    print(f"\nウォークフォワード効率: {wf.efficiency():.2f}（0.5 以上が目安）")
    if not wf.oos_equity.empty:
        total = wf.oos_equity.iloc[-1] / cfg.initial_equity - 1
        print(f"検証期間のみの累積リターン: {total:.1%}（取引 {len(wf.oos_trades)} 回）")

    if args.export:
        last = wf.windows.iloc[-1]
        chosen = {k[2:]: v for k, v in last.items() if k.startswith("p.")}
        strat_p = {k: _py(v) for k, v in chosen.items() if not k.startswith("exit.")}
        exit_p = {k[5:]: _py(v) for k, v in chosen.items() if k.startswith("exit.")}
        sleeve = Sleeve(sym, make_strategy(args.strategy, **{**fixed, **strat_p}), exit_p)
        paths = export_sleeve(sleeve, inst[sym], cfg, out / "ea")
        print("EA 用に書き出し:", *[str(x) for x in paths])


def _py(v):
    """pandas/numpy の値を JSON 互換の Python 値に戻す。"""
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


if __name__ == "__main__":
    main()
