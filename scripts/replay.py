"""学習で決めた本番用の構成（final.json）を、指定した期間でバックテストする。

デモ口座・本番で EA を動かしている期間を再現して、実際の取引と比べるのに使う
（毎週、MT5 から最新のデータを書き出してから実行する）。

    python scripts/replay.py --final output/train/<日時>/final.json --start 2026-11-01
    python scripts/replay.py --final ... --start 2026-11-01 --end 2026-11-30 --risk-scale 0.5

資金・ルールは config/train.toml の [account]、約定は M5 で再現する（M5 があれば）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cfdbot.metrics import format_metrics  # noqa: E402
from cfdbot.train.config import load_train_config  # noqa: E402
from cfdbot.train.dataset import load_dataset  # noqa: E402
from cfdbot.train.pipeline import _account_config, _eval_settings, validate_window  # noqa: E402
from cfdbot.train.portfolio import Pick  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--final", required=True, help="学習結果の final.json")
    p.add_argument("--start", required=True, help="開始日（例: 2026-11-01）")
    p.add_argument("--end", help="終了日（省略時はデータの最後まで）")
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--data", help="データフォルダ（設定より優先）")
    p.add_argument("--equity", type=float, help="資金（円）。省略時は config の [account] equity")
    p.add_argument("--risk-scale", type=float, default=1.0, help="EA の ea_risk_scale と同じ値にする")
    p.add_argument("--out", default="output/replay")
    args = p.parse_args()

    cfg = load_train_config(args.config)
    if args.data:
        cfg.data.dir = args.data
    if args.equity:
        cfg.account.equity = args.equity
    final = json.loads(Path(args.final).read_text(encoding="utf-8"))
    picks = [Pick(s["symbol"], s["strategy"], s["params"], s["exit"], 0, s["train_score"], s["train_score"],
                  s["train_trades"], s["multiplier"] * args.risk_scale, s["timeframe"], s.get("htf") or {})
             for s in final["sleeves"]]
    if not picks:
        raise SystemExit("final.json に構成が無い（学習で全銘柄見送り）")

    ds = load_dataset(cfg.data)
    settings = _eval_settings(cfg, ds)
    fx = ds.fx if ds.fx is not None else cfg.data.fx
    bt_cfg = _account_config(cfg, fx, settings.event_list())
    start = pd.Timestamp(args.start, tz="UTC")
    last = max(ds.signal_frame(p.symbol, p.timeframe).index[-1] for p in picks)
    end = pd.Timestamp(args.end, tz="UTC") + pd.Timedelta(days=1) if args.end else last + pd.Timedelta(days=1)
    res = validate_window(ds, settings.instrument_map(), picks, bt_cfg, start, end)

    from cfdbot.metrics import compute_metrics

    m = compute_metrics(res["equity"], res["trades"], cfg.account.equity, res["leverage"])
    print(f"期間 {start:%Y-%m-%d} 〜 {(end - pd.Timedelta(days=1)):%Y-%m-%d}、資金 {cfg.account.equity:,.0f} 円、"
          f"リスク倍率 {args.risk_scale}")
    for pk in picks:
        print(f"  {pk.label}: 1回の損失 {cfg.account.base_risk * pk.multiplier:.2%}")
    print(format_metrics(m))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    name = f"{start:%Y%m%d}-{(end - pd.Timedelta(days=1)):%Y%m%d}"
    if not res["trades"].empty:
        cols = ["symbol", "side", "qty", "entry_time", "entry_price", "exit_time", "exit_price", "reason", "pnl"]
        t = res["trades"][cols].copy()
        for c in ("entry_time", "exit_time"):
            t[c] = t[c].dt.tz_convert("Asia/Tokyo").dt.strftime("%Y-%m-%d %H:%M")
        print("\n取引（時刻は日本時間。MT5 の口座履歴と比べる）:")
        print(t.to_string(index=False))
        res["trades"].to_csv(out / f"{name}_trades.csv", index=False)
    else:
        print("\nこの期間の取引は無い")
    res["equity"].rename("equity").to_csv(out / f"{name}_equity.csv")
    print(f"\n保存先: {out}/{name}_*.csv")


if __name__ == "__main__":
    main()
