"""データを入れてすぐ学習する。

    python scripts/train.py                 # data/ の CSV と config/train.toml で学習
    python scripts/train.py --demo          # 合成データで一通り動かす（動作確認用。結果は output/train/demo/）
    python scripts/train.py --listen 0.0.0.0   # iPad など他の端末も計算に参加させる

結果は output/train/<日時>/ に出る（report.md・EA 用ファイル・検証期間の成績）。
同じデータ・設定の計算結果はキャッシュされ、途中で止めても続きから再開する。
macOS では並列計算のため、このスクリプトを直接実行すること（import して使わない）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cfdbot.train.config import load_train_config  # noqa: E402
from cfdbot.train.pipeline import run_training  # noqa: E402


def make_demo_data(dest: Path) -> None:
    """合成データを MT5 の書き出し形式で作る（成績は無意味）。"""
    import pandas as pd

    from cfdbot.data import synthetic_ohlc

    dest.mkdir(parents=True, exist_ok=True)
    specs = [("XTIUSD", "oil", 1), ("XBRUSD", "oil", 2), ("XAGUSD", "silver", 3), ("XAUUSD", "gold", 4)]
    for name, kind, seed in specs:
        path = dest / f"{name}_H1.csv"
        if path.exists():
            continue
        df = synthetic_ohlc(kind, start="2020-12-06", end="2026-09-25", hours=1, seed=seed)
        srv = (df.index.tz_convert("America/New_York") + pd.Timedelta(hours=7)).tz_localize(None)
        pd.DataFrame({
            "<DATE>": srv.strftime("%Y.%m.%d"), "<TIME>": srv.strftime("%H:%M:%S"),
            "<OPEN>": df["open"], "<HIGH>": df["high"], "<LOW>": df["low"], "<CLOSE>": df["close"],
            "<TICKVOL>": 1000, "<VOL>": 0, "<SPREAD>": df["spread"].astype(int),
        }).to_csv(path, sep="\t", index=False)
        print(f"合成データを作成: {path}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--data", help="データフォルダ（設定より優先）")
    p.add_argument("--budget-hours", type=float, help="計算時間の上限（設定より優先）")
    p.add_argument("--workers", type=int, help="この Mac で使う並列数（0 = CPU数-1）")
    p.add_argument("--listen", help='"0.0.0.0" で iPad など他の端末の参加を受け付ける')
    p.add_argument("--port", type=int)
    p.add_argument("--demo", action="store_true", help="合成データで動作確認（成績は無意味）")
    args = p.parse_args()

    cfg = load_train_config(args.config)
    if args.demo:
        cfg.output_dir = str(Path(cfg.output_dir) / "demo")   # 本番用の学習結果と混ざらないように分ける
        demo_dir = Path(cfg.output_dir) / "demo_data"
        make_demo_data(demo_dir)
        cfg.data.dir = str(demo_dir)
        cfg.data.server_tz = "ny_close"    # 合成データは米東部+7時間の形式で書いている
        cfg.data.events = ""
        cfg.data.instruments = ""
        print("※ 合成データです。レポートの成績は戦略の良し悪しを示しません。")
    if args.data:
        cfg.data.dir = args.data
    if args.budget_hours is not None:
        cfg.compute.time_budget_hours = args.budget_hours
    if args.workers is not None:
        cfg.compute.workers = args.workers
    if args.listen:
        cfg.compute.listen = args.listen
    if args.port:
        cfg.compute.port = args.port
    run_training(cfg, log=lambda m: print(m, flush=True), config_path=args.config)


if __name__ == "__main__":
    main()
