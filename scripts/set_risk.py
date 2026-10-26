"""1 回の損失（リスク）と資金を、この Mac の設定（config/local.toml）に保存する。

    python scripts/set_risk.py --risk 0.015                    # 1 回の損失 1.5%
    python scripts/set_risk.py --risk 0.015 --equity 1000000   # 資金も
    python scripts/set_risk.py --risk 0.02 --dd-halt 0.40      # 最大の下落での停止の線も（docs/leverage.md）

同時に持てる合計（1 回の損失 × 4）とグループ（原油 2 つ・貴金属 2 つ: 1 回の損失 × 2）の上限も同じ比率にする
（scripts/risk_table.py の表と同じ考え方）。replay・risk_table・EA のプリセット（./cfd preset）がこの値を使う。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cfdbot.train.config import load_train_config, local_config_path, set_local_option  # noqa: E402

TOTAL_PER_TRADE = 4   # 4 銘柄分
CLUSTER_PER_TRADE = 2  # 1 グループ 2 銘柄分


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--risk", type=float, required=True, help="1 回の損失（資金比。1.5%% なら 0.015）")
    p.add_argument("--equity", type=float, help="資金（円）")
    p.add_argument("--dd-halt", type=float, help="最大の下落での新規停止の線（資産の高値からの下落。40%% なら 0.40）")
    p.add_argument("--config", default="config/train.toml")
    args = p.parse_args()
    if not 0.001 <= args.risk <= 0.03:
        raise SystemExit("1 回の損失は 0.001〜0.03（0.1%〜3%）の範囲で指定する（例: 1.5% なら 0.015）")
    if args.dd_halt is not None and not 0.10 <= args.dd_halt <= 0.50:
        raise SystemExit("停止の線は 0.10〜0.50（10%〜50%）の範囲で指定する（例: 40% なら 0.40）")

    r = args.risk
    clusters = load_train_config(args.config).account.cluster_max_risk
    set_local_option(args.config, "account", "base_risk", r)
    set_local_option(args.config, "account", "max_total_risk", round(r * TOTAL_PER_TRADE, 6))
    set_local_option(args.config, "account", "cluster_max_risk",
                     {k: round(r * CLUSTER_PER_TRADE, 6) for k in clusters})
    if args.equity:
        set_local_option(args.config, "account", "equity", float(args.equity))
    if args.dd_halt is not None:
        set_local_option(args.config, "account", "max_drawdown_halt", float(args.dd_halt))
    acc = load_train_config(args.config).account
    print(f"{local_config_path(args.config)} に保存した:")
    print(f"  資金 {acc.equity:,.0f} 円 / 1回の損失 {acc.base_risk:.1%} / 同時に持てる合計 {acc.max_total_risk:.1%} / "
          + " / ".join(f"{k} {v:.1%}" for k, v in acc.cluster_max_risk.items())
          + f" / 最大の下落での停止 {acc.max_drawdown_halt:.0%}")
    print("次: ./cfd preset config/baselines/turtle55_d1.json（EA のプリセットを作って MT5 に置く）")


if __name__ == "__main__":
    main()
