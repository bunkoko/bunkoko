"""テスターのシグナル記録をまとめて取り込み、構成の全銘柄について EA と Python を突き合わせる。

    python scripts/compare_all.py --final output/live/turtle55_d1/final.json

記録のファイル名の magic（銘柄ごとに固定）から銘柄を見分けるので、テスターは銘柄ごとに 1 回ずつ
（テスター用プリセットを読み込んで）実行しておく。まだ実行していない銘柄は「記録なし」と表示する。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot.mt5files import choose_terminal, fetch, find_terminals, magic_for  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--final", required=True)
    p.add_argument("--dest", default="output/compare")
    p.add_argument("--magic-base", type=int, default=2609000)
    p.add_argument("--config", default="config/train.toml")
    args = p.parse_args()

    dest = Path(args.dest)
    found = find_terminals()
    if found:
        term = choose_terminal(found)
        if term.common is not None:
            fetch(term.common, "cfdbot_signals_*.csv", dest)
    sleeves = json.loads(Path(args.final).read_text(encoding="utf-8"))["sleeves"]
    summary = []
    for s in sleeves:
        magic = magic_for(s["symbol"], args.magic_base)
        logs = sorted(dest.glob(f"cfdbot_signals_*_{magic}.csv"))
        if not logs:
            summary.append((s["symbol"], "記録なし（テスターで未実行）"))
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
