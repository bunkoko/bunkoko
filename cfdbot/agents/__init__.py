"""役割の違う 5 つの AI（docs/agents.md）。それぞれ別のモジュール・別のコマンドで、ファイルだけでやりとりする。

    collector（情報収集）→ ir_scorer（IR 評価）┐
    selector（銘柄選定）───────────────────────┴→ decider（判断）→ evaluator（判断の評価）

evaluator はほかの 4 つのモジュールを読み込まない（記録と値段だけで採点する）。
"""

from __future__ import annotations

from pathlib import Path


def paths(root: str | Path = "data/agents") -> dict[str, Path]:
    r = Path(root)
    return {"root": r, "disclosures": r / "ir" / "disclosures.csv", "titles": r / "ir" / "titles.npz",
            "ir_scores": r / "ir_scores.csv", "selector": r / "selector_scores.csv", "decisions": r / "decisions.csv",
            "ledger": r / "ledger.csv", "ir_numbers": r / "ir_numbers.csv", "decisions_numbers": r / "decisions_numbers.csv"}
