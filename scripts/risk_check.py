"""リスクとお金の置き方の点検（docs/risk_plan.md 8 章）。判定はしない・試した数に入れない。何度動かしてもよい。

    python scripts/risk_check.py    # 持つ年数ごとの元本割れ・戻し方 3 通りの比べ（数秒。値段は取りに行かない）

使うのは data/stocks/misc_close.csv の S&P500（^GSPC）と日経平均（^N225）。無ければ ./cfd stocks --fetch。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot import riskcheck as R  # noqa: E402


def main(argv: list[str] | None = None) -> Path:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data", help="データの置き場（既定 data）")
    p.add_argument("--out", default="output/riskcheck", help="報告の保存先")
    args = p.parse_args(argv)
    res = R.run(Path(args.data))
    if not res["hold"]:
        sys.exit("指数のデータが無い: " + "／".join(res["skipped"].values()))
    lines = R.report_lines(res, f"{datetime.now():%Y-%m-%d %H:%M}")
    out = Path(args.out) / f"{datetime.now():%Y%m%d-%H%M}"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for t in lines:
        print(t, flush=True)
    print(f"報告: {path}（docs/risk_plan.md 9 章に書き写す。判定はしない）", flush=True)
    return path


if __name__ == "__main__":
    main()
