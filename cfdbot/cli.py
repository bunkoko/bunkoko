"""scripts/ から使う共通処理（引数の解釈・結果の保存）。"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import pandas as pd

from .backtest import BacktestConfig, BacktestResult, CostModel
from .data import load_mt5_csv
from .events import load_events_csv
from .instruments import Instrument, get_instruments, load_instruments
from .metrics import format_metrics


def add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--csv", action="append", default=[], metavar="SYMBOL=PATH",
                   help="銘柄キーと MT5 書き出し CSV（例: WTI=data/WTI_H4.csv）。複数指定可")
    p.add_argument("--server-tz", default="ny_close",
                   help="MT5 サーバー時刻: ny_close / 数値(UTCからの時差) / IANA名（既定 ny_close）")
    p.add_argument("--broker", default="phillip", choices=["phillip", "saxo"])
    p.add_argument("--instruments", help="銘柄仕様 JSON（実測スプレッド等で上書き）")
    p.add_argument("--events", help="指標イベント CSV（FOMC/CPI/祝日でずれた EIA など）")
    p.add_argument("--equity", type=float, default=1_000_000, help="初期資金（円）")
    p.add_argument("--fx", type=float, default=150.0, help="USD/JPY")
    p.add_argument("--spread-mult", type=float, default=1.0)
    p.add_argument("--slippage-mult", type=float, default=1.0)
    p.add_argument("--start", help="売買開始日（それ以前は指標計算のみ）")
    p.add_argument("--end", help="売買終了日")
    p.add_argument("--out", default="output", help="結果の保存先")


def parse_csv_args(items: list[str], server_tz) -> dict[str, pd.DataFrame]:
    try:
        server_tz = float(server_tz)
    except (TypeError, ValueError):
        pass
    data = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--csv は SYMBOL=PATH 形式で指定: {item}")
        sym, path = item.split("=", 1)
        data[sym.upper()] = load_mt5_csv(path, server_tz=server_tz)
    if not data:
        raise SystemExit("--csv を1つ以上指定すること")
    return data


def instruments_from_args(args) -> dict[str, Instrument]:
    inst = get_instruments(args.broker)
    if args.instruments:
        inst.update(load_instruments(args.instruments))
    return inst


def config_from_args(args, base: BacktestConfig | None = None) -> BacktestConfig:
    cfg = base or BacktestConfig()
    cfg.initial_equity = args.equity
    cfg.fx_rate = args.fx
    cfg.costs = replace(cfg.costs, spread_mult=args.spread_mult, slippage_mult=args.slippage_mult)
    if args.events:
        cfg.filters = replace(cfg.filters, extra_events=tuple(load_events_csv(args.events)))
    if args.start:
        cfg.trade_start = pd.Timestamp(args.start, tz="UTC")
    if args.end:
        cfg.trade_end = pd.Timestamp(args.end, tz="UTC")
    return cfg


def save_result(res: BacktestResult, out: str | Path, prefix: str = "backtest") -> Path:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    res.trades.to_csv(out / f"{prefix}_trades.csv", index=False)
    res.equity.to_csv(out / f"{prefix}_equity.csv")
    res.rejections.to_csv(out / f"{prefix}_rejections.csv", index=False)
    (out / f"{prefix}_metrics.json").write_text(
        json.dumps(res.metrics(), indent=2, default=float), encoding="utf-8"
    )
    return out


def print_result(res: BacktestResult, title: str = "") -> None:
    if title:
        print(f"\n=== {title} ===")
    print(format_metrics(res.metrics()))
    by = res.summary_by("sleeve")
    if not by.empty:
        cols = ["trades", "win_rate", "profit_factor", "avg_r", "net_pnl"]
        print("\n--- 戦略別 ---")
        print(by[cols].to_string(float_format=lambda v: f"{v:,.2f}"))
    if not res.trades.empty:
        print("\n--- 決済理由 ---")
        print(res.trades["reason"].value_counts().to_string())
    if not res.rejections.empty:
        print("\n--- 見送り理由 ---")
        print(res.rejections["reason"].value_counts().to_string())
