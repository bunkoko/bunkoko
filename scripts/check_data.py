"""data/ に置いた CSV を点検する（MT5 から書き出したら、学習の前に必ず実行）。

    python scripts/check_data.py
    python scripts/check_data.py --write-instruments config/instruments_measured.json

--write-instruments を付けると、M5 のスプレッド列から測ったスプレッドを銘柄仕様ファイルに書き出す。
そのファイルを config/train.toml の [data] instruments に指定すると、学習が実測値を使う。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cfdbot.instruments import get_instruments, load_instruments  # noqa: E402
from cfdbot.train.check import run_check, write_measured  # noqa: E402
from cfdbot.train.config import load_train_config  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--data", help="データフォルダ（設定より優先）")
    p.add_argument("--server-tz", help="サーバー時刻の方式（設定より優先。ny_close / 数値）")
    p.add_argument("--write-instruments", metavar="PATH", help="実測スプレッドを入れた銘柄仕様 JSON を書き出す")
    args = p.parse_args()

    cfg = load_train_config(args.config)
    if args.data:
        cfg.data.dir = args.data
    if args.server_tz:
        try:
            cfg.data.server_tz = float(args.server_tz)
        except ValueError:
            cfg.data.server_tz = args.server_tz
    res = run_check(cfg)
    print("\n".join(res.lines))
    if res.costs is not None and not res.costs.empty:
        print("\n■ 時間足ごとのコスト（往復コスト ÷ 損切り幅 2.5ATR。5% 未満 良好 / 10% 超 不向き）")
        for _, r in res.costs.iterrows():
            made = "" if r["from_file"] else "（作成）"
            print(f"  {r['symbol']:<7}{r['timeframe']:>4}{made:<5} {r['cost_per_r']:6.1%}  {r['verdict']}")
    print(f"\n点検結果: 注意 {len(res.warnings)} 件" if res.warnings else "\n点検結果: 問題なし")
    if args.write_instruments:
        base = get_instruments(cfg.data.broker)
        if cfg.data.instruments:
            base.update(load_instruments(cfg.data.instruments))
        path = write_measured(res, base, args.write_instruments)
        print(f"実測スプレッドを書き出した: {path}（config/train.toml の [data] instruments に指定する）")


if __name__ == "__main__":
    main()
