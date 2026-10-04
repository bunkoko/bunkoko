"""テスターのシグナル記録をまとめて取り込み、構成の全銘柄について EA と Python を突き合わせる。

    python scripts/compare_all.py --final output/live/turtle55_d1/final.json

記録のファイル名の magic（銘柄ごとに固定）から銘柄を見分けるので、テスターは銘柄ごとに 1 回ずつ
（テスター用プリセットを読み込んで）実行しておく。まだ実行していない銘柄は「記録なし」と表示する。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot.mt5files import MAGIC_SLOTS, choose_terminal, fetch, find_terminals, magic_for  # noqa: E402
from cfdbot.train.config import load_train_config  # noqa: E402

_LOG_NAME = re.compile(r"^cfdbot_signals_(.+)_(\d+)\.csv$")
_TF_BY_HOURS = {1: "H1", 4: "H4", 24: "D1"}


def chart_key(chart: str, symbol_map: dict[str, str]) -> str | None:
    head = chart.upper()
    return symbol_map.get(head) or next((v for k, v in symbol_map.items() if head.startswith(k)), None)


def diagnose(log: Path, expected: dict[str, str], symbol_map: dict[str, str], magic_base: int) -> list[str]:
    """テスターの記録 1 つについて、設定の間違いを探す。戻り値は注意の一覧（空なら問題なし）。"""
    m = _LOG_NAME.match(log.name)
    if not m:
        return ["ファイル名が想定と違う"]
    chart, magic = m.group(1), int(m.group(2))
    key = chart_key(chart, symbol_map)
    preset = {v: k for k, v in MAGIC_SLOTS.items()}.get(magic - magic_base)
    notes = []
    if preset and key and preset != key:
        notes.append(f"{chart} のチャートに {preset} のプリセットを読み込んでいる（{key} 用のプリセットにする）")
    df = pd.read_csv(log)
    t = pd.to_datetime(df["time_utc"], format="%Y.%m.%d %H:%M")
    restarts = int((t.diff() < pd.Timedelta(0)).sum())
    if restarts:
        notes.append(f"テストを {restarts + 1} 回実行した記録が混ざっている（最後の実行を使うので問題ない。"
                     "気になるなら ./cfd compare --clean で消してから実行し直す）")
        t = t[int(t.diff().lt(pd.Timedelta(0)).to_numpy().nonzero()[0][-1]):]   # 最後の実行の分
    step = t.diff().dropna()
    hours = int(round(step.mode().iloc[0].total_seconds() / 3600)) if len(step) else 0
    tf = _TF_BY_HOURS.get(hours, f"{hours} 時間")
    want = expected.get(key or preset or "")
    if want and tf != want:
        notes.append(f"時間足が {tf}（{want} のはず）")
    if len(t) and t.iloc[0] > pd.Timestamp("2021-12-31"):
        notes.append(f"期間の始まりが {t.iloc[0]:%Y-%m-%d}（2021-06-01 からにすると比べる本数が増える）")
    if len(t) < 100:
        notes.append(f"記録が {len(t)} 本しかない（期間が短い）")
    head = f"{log.name}: チャート {chart} / プリセット {preset or magic} / {tf} / {t.iloc[0]:%Y-%m-%d}〜{t.iloc[-1]:%Y-%m-%d}（{len(t)} 本）" \
        if len(t) else f"{log.name}: 記録が空"
    return [head] + ["  ⚠ " + n for n in notes]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--final", required=True)
    p.add_argument("--dest", default="output/compare")
    p.add_argument("--magic-base", type=int, default=2609000)
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--clean", action="store_true", help="これまでのテスターの記録を消す（テストをやり直す前に）")
    p.add_argument("--clean-wrong", action="store_true",
                   help="別の銘柄のプリセットで実行した記録だけを消す（正しい記録は残す）")
    args = p.parse_args()

    dest = Path(args.dest)
    found = find_terminals()
    term = choose_terminal(found) if found else None
    if args.clean:
        n = 0
        for d in [dest] + ([term.common] if term and term.common else []):
            for f in d.glob("cfdbot_signals_*.csv") if d.is_dir() else []:
                f.unlink()
                n += 1
        print(f"テスターの記録を {n} 個消した。テスターで各銘柄を実行してから、--clean を付けずにもう一度実行する")
        return
    cfg = load_train_config(args.config)
    if args.clean_wrong:
        reverse = {v: k for k, v in MAGIC_SLOTS.items()}
        n = 0
        for d in [dest] + ([term.common] if term and term.common else []):
            for f in sorted(d.glob("cfdbot_signals_*.csv")) if d.is_dir() else []:
                m = _LOG_NAME.match(f.name)
                preset = reverse.get(int(m.group(2)) - args.magic_base) if m else None
                if m and preset and chart_key(m.group(1), cfg.data.symbol_map) not in (None, preset):
                    f.unlink()
                    n += 1
        print(f"別の銘柄のプリセットで実行した記録を {n} 個消した")
    if term and term.common is not None:
        fetch(term.common, "cfdbot_signals_*.csv", dest)
    sleeves = json.loads(Path(args.final).read_text(encoding="utf-8"))["sleeves"]
    expected = {s["symbol"]: s["timeframe"] for s in sleeves}
    logs_all = sorted(dest.glob("cfdbot_signals_*.csv"))
    print("===== テスターの設定の点検")
    if not logs_all:
        print("  記録が 1 つも無い。テスターで「パラメータの入力」→「読み込み」でテスター用プリセット"
              "（MQL5/Profiles/Tester の cfdbot_*.set）を読み込んでから実行したか確認する")
    for log in logs_all:
        print("\n".join("  " + line for line in diagnose(log, expected, cfg.data.symbol_map, args.magic_base)))
    summary = []
    for s in sleeves:
        magic = magic_for(s["symbol"], args.magic_base)
        logs = [f for f in sorted(dest.glob(f"cfdbot_signals_*_{magic}.csv"))      # 正しいチャートの分だけ
                if (m := _LOG_NAME.match(f.name)) and chart_key(m.group(1), cfg.data.symbol_map) == s["symbol"]]
        if not logs:
            summary.append((s["symbol"], "記録なし（未実行か、テスター用プリセットを読み込んでいない）"))
            continue
        print(f"\n===== {s['symbol']}（{logs[-1].name}）")
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "compare_signals.py"), "--final", args.final,
                            "--symbol", s["symbol"], "--ea-log", str(logs[-1]), "--config", args.config],
                           capture_output=True, text=True)
        print(r.stdout.rstrip())
        if r.returncode != 0:
            print(r.stderr.rstrip()[-2000:])
        result = next((ln.split("結果:", 1)[1].strip() for ln in r.stdout.splitlines() if ln.startswith("結果:")),
                      "エラー（上の表示を送ってください）")
        summary.append((s["symbol"], result))
    print("\n===== まとめ")
    for sym, res in summary:
        print(f"  {sym:<7}{res}")


if __name__ == "__main__":
    main()
