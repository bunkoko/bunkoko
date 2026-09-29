"""MT5 から書き出した CSV でバックテストする。

例（推奨構成）:
    python scripts/backtest.py --csv WTI=data/WTI_H4.csv --csv SILVER=data/XAGUSD_H4.csv

例（戦略を1つ指定）:
    python scripts/backtest.py --csv SILVER=data/XAGUSD_H4.csv \
        --strategy squeeze --params '{"box_period": 24}' --exit '{"trail_atr": 3.5}'

ストレステスト: --spread-mult 2 --slippage-mult 2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cfdbot.backtest import Sleeve, run_backtest  # noqa: E402
from cfdbot.cli import (  # noqa: E402
    add_common_args,
    config_from_args,
    instruments_from_args,
    parse_csv_args,
    print_result,
    save_result,
)
from cfdbot.profiles import default_config, oil_sleeves, silver_sleeves  # noqa: E402
from cfdbot.strategies import STRATEGIES, make_strategy  # noqa: E402

OIL = {"WTI", "BRENT"}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(p)
    p.add_argument("--strategy", choices=list(STRATEGIES), help="省略時は銘柄ごとの推奨構成")
    p.add_argument("--params", default="{}", help="戦略パラメータ（JSON）")
    p.add_argument("--exit", default="{}", help="出口設定の上書き（JSON）")
    args = p.parse_args()

    data = parse_csv_args(args.csv, args.server_tz)
    inst = instruments_from_args(args)
    unknown = set(data) - set(inst)
    if unknown:
        raise SystemExit(f"銘柄仕様が無い: {sorted(unknown)}（--instruments で追加）")
    cfg = config_from_args(args, default_config())

    sleeves = []
    for sym in data:
        if args.strategy:
            strat = make_strategy(args.strategy, **json.loads(args.params))
            sleeves.append(Sleeve(sym, strat, json.loads(args.exit)))
        else:
            sleeves += oil_sleeves(sym) if sym in OIL else silver_sleeves(sym)

    res = run_backtest(data, inst, sleeves, cfg)
    print_result(res, "バックテスト")
    out = save_result(res, args.out)
    print(f"\n保存先: {out}/")


if __name__ == "__main__":
    main()
