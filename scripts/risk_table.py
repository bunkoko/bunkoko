"""資金と 1 回の損失（リスク）を変えたときの成績を 1 つの表にする（どこまでリスクを取るかを決めるため）。

    python scripts/risk_table.py --final config/baselines/turtle55_d1.json --start 2021-06-01
    python scripts/risk_table.py --final ... --equity 1000000,3000000 --risk 0.01,0.015,0.02

1 回の損失を上げるときは、同時に持てる合計（4 銘柄分 = 1 回の損失 × 4）とグループ（原油 2 つ・貴金属 2 つ
= 1 回の損失 × 2）の上限も同じ比率で上げて比べる（上限を据え置くと、上げた分だけ同時に持てなくなるため）。
最大DD での停止（25%）は評価のため外しているので、表の最大DD が停止の水準に近い設定は本番では途中で止まる。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cfdbot.metrics import compute_metrics  # noqa: E402
from cfdbot.train.config import load_train_config  # noqa: E402
from cfdbot.train.dataset import load_dataset  # noqa: E402
from cfdbot.train.pipeline import _account_config, _eval_settings, validate_window  # noqa: E402
from cfdbot.train.portfolio import Pick  # noqa: E402


def _floats(text: str) -> list[float]:
    return [float(x) for x in text.replace("、", ",").split(",") if x.strip()]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--final", required=True, help="構成（final.json や config/baselines/*.json）")
    p.add_argument("--start", required=True)
    p.add_argument("--end")
    p.add_argument("--equity", default="1000000,2000000,3000000", help="資金（円、カンマ区切り）")
    p.add_argument("--risk", default="0.01,0.015,0.02", help="1 回の損失（資金比、カンマ区切り）")
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--data")
    args = p.parse_args()

    cfg = load_train_config(args.config)
    if args.data:
        cfg.data.dir = args.data
    final = json.loads(Path(args.final).read_text(encoding="utf-8"))
    ds = load_dataset(cfg.data)
    settings = _eval_settings(cfg, ds)
    fx = ds.fx if ds.fx is not None else cfg.data.fx
    base = _account_config(cfg, fx, settings.event_list())
    base_risk = cfg.account.base_risk
    start = pd.Timestamp(args.start.strip("「」 "), tz="UTC")
    last = max(ds.signal_frame(s["symbol"], s["timeframe"]).index[-1] for s in final["sleeves"])
    end = (pd.Timestamp(args.end.strip("「」 "), tz="UTC") if args.end else last) + pd.Timedelta(days=1)
    total_per_trade = base.risk.max_total_risk / base_risk          # 既定 4%÷1% = 4 銘柄分
    cluster_per_trade = {k: v / base_risk for k, v in base.risk.cluster_max_risk.items()}

    rows = []
    for equity in _floats(args.equity):
        for risk in _floats(args.risk):
            picks = [Pick(s["symbol"], s["strategy"], s["params"], s["exit"], 0, 0.0, 0.0, 0,
                          s["multiplier"] * risk / base_risk, s["timeframe"], s.get("htf") or {})
                     for s in final["sleeves"]]
            bt = replace(base, initial_equity=equity, risk=replace(
                base.risk, max_total_risk=total_per_trade * risk,
                cluster_max_risk={k: v * risk for k, v in cluster_per_trade.items()}))
            res = validate_window(ds, settings.instrument_map(), picks, bt, start, end)
            m = compute_metrics(res["equity"], res["trades"], equity, res["leverage"])
            monthly = res["equity"].resample("ME").last().pct_change().dropna()
            rej = res["rejections"]["reason"].value_counts() if not res["rejections"].empty else pd.Series(dtype=int)
            rows.append({
                "資金": f"{equity / 1e4:,.0f}万円", "1回の損失": f"{risk:.1%}",
                "年率": f"{m.get('cagr', 0):.1%}", "最大DD": f"{abs(m.get('max_drawdown', 0)):.1%}",
                "シャープ": f"{m.get('sharpe', 0):.2f}", "最悪の月": f"{monthly.min():.1%}" if len(monthly) else "–",
                "取引": int(m.get("trades", 0)), "最小単位で見送り": int(rej.get("min_lot", 0)),
                "上限で見送り": int(rej.get("heat", 0) + rej.get("cluster", 0)),
                "最大レバ": f"{m.get('max_leverage', 0):.2f}倍",
            })
            print(f"  計算: 資金 {equity:,.0f} 円 / 1回の損失 {risk:.1%}", flush=True)
    print(f"\n期間 {start:%Y-%m-%d} 〜 {end - pd.Timedelta(days=1):%Y-%m-%d}（{Path(args.final).name}）")
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"\n※ 最大DD が {cfg.account.max_drawdown_halt:.0%} に近い設定は、本番では途中で新規停止になる。"
          "シャープは資金が小さいと最小単位の見送りで下がる")


if __name__ == "__main__":
    main()
