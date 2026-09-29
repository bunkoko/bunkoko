"""合成データで一通りの流れを動かすデモ（成績は無意味。動作確認用）。

    python scripts/demo.py

1. 推奨構成（原油: ドンチャン+押し目 / 銀: スクイーズ+押し目）でバックテスト
2. コストのストレステスト（スプレッド・スリッページ2倍、証拠金率20%）
3. 原油ドンチャンのウォークフォワード
4. EA 用パラメータファイルの書き出し
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cfdbot.backtest import CostModel, run_backtest  # noqa: E402
from cfdbot.cli import print_result, save_result  # noqa: E402
from cfdbot.data import synthetic_ohlc  # noqa: E402
from cfdbot.export import export_sleeve  # noqa: E402
from cfdbot.instruments import get_instruments  # noqa: E402
from cfdbot.profiles import default_config, default_sleeves, silver_regime_sleeves  # noqa: E402
from cfdbot.walkforward import walk_forward  # noqa: E402

OUT = Path("output/demo")


def main() -> None:
    print("※ 合成データです。ここでの成績は戦略の良し悪しを一切示しません。")
    data = {
        "WTI": synthetic_ohlc("oil", seed=11),
        "SILVER": synthetic_ohlc("silver", seed=12),
    }
    inst = get_instruments("phillip")

    res = run_backtest(data, inst, default_sleeves(), default_config())
    print_result(res, "推奨構成（合成データ）")
    save_result(res, OUT, "default")

    # ストレステスト（比較しやすいよう最大DDでの停止は無効にする）
    no_halt = replace(default_config().risk, max_drawdown_halt=1.0)
    base = run_backtest(data, inst, default_sleeves(), default_config(risk=no_halt))
    stressed = run_backtest(
        data, inst, default_sleeves(),
        default_config(risk=no_halt, costs=CostModel(spread_mult=2.0, slippage_mult=2.0)),
    )
    inst_margin = {k: replace(v, margin_rate=0.20) for k, v in inst.items()}
    margin = run_backtest(data, inst_margin, default_sleeves(), default_config(risk=no_halt))
    print("\n=== ストレステスト（停止条件なし） ===")
    for label, r in (("基準", base), ("コスト2倍", stressed), ("証拠金率20%", margin)):
        m = r.metrics()
        rej = r.rejections["reason"].value_counts().get("margin", 0)
        print(f"{label:<8} 最終資産 {m['final_equity']:>12,.0f}円  CAGR {m['cagr']:>6.1%}  "
              f"最大DD {m['max_drawdown']:>5.1%}  取引 {m['trades']:>4}  証拠金不足 {rej}")

    regime = run_backtest({"SILVER": data["SILVER"]}, inst, silver_regime_sleeves(), default_config())
    print_result(regime, "銀: レジーム切り替え（Python 専用の発展形）")

    print("\n=== ウォークフォワード: 原油ドンチャン ===")
    wf = walk_forward(
        {"WTI": data["WTI"]}, inst, "WTI", "donchian",
        {"entry_period": [30, 40, 55], "exit_period": [10, 20], "exit.trail_atr": [2.5, 3.0, 3.5]},
        train_months=24, test_months=6,
    )
    cols = ["train_end", "p.entry_period", "p.exit_period", "p.exit.trail_atr", "train_cagr", "test_cagr", "test_trades"]
    print(wf.windows[cols].to_string(index=False))
    print(f"ウォークフォワード効率: {wf.efficiency():.2f}（0.5 以上が目安）")

    for sleeve in default_sleeves():
        paths = export_sleeve(sleeve, inst[sleeve.symbol], default_config(), OUT / "ea")
        print("EA 用に書き出し:", *[str(p) for p in paths])


if __name__ == "__main__":
    main()
