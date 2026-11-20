"""束ねる検証 B1（docs/bundle.md）。残っている方法 11 家族を一緒に持つと、偶然を超えるか。判定は 1 回だけ。

    python scripts/bundle_study.py --check     # 柱ごとの系列の期間・月の数・欠けだけを見る（成績は出さない。何度でもよい）
    python scripts/bundle_study.py             # 判定（1 回だけ）

柱ごとの月の系列は data/bundle/series/<名前>.csv（列 month・d）。1 回計算したら、data/bundle/B1_DONE.json の印で止まる（動かし直さない）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot import bundle as B  # noqa: E402
from cfdbot.wide.base import NotReady  # noqa: E402


def stop_if_done(marker: Path) -> None:
    rec = B.done(marker)
    if rec is not None:
        sys.exit(f"判定はもう 1 回計算してある（{rec.get('date')}: {rec.get('verdict')}）。1 回だけの決まり（docs/bundle.md 4.4）なので、動かし直さない")


def main(argv: list[str] | None = None, root: Path | None = None) -> Path | None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="期間・月の数・欠けだけ見る（成績は出さない）")
    p.add_argument("--out", default=None, help="報告の保存先（既定 output/bundle）")
    args = p.parse_args(argv)
    root = Path(root) if root else ROOT
    folder, marker = root / B.SERIES_DIR, root / B.MARKER       # 印の場所は --out に関係なく決まっている

    if not args.check:
        stop_if_done(marker)
    ok, lines = B.check(folder)
    for t in lines:
        print(f"- {t}", flush=True)
    if args.check:
        print("\n成績はまだ何も計算していない。" + ("判定は引数なしで 1 回だけ" if ok else "このままでは判定に進めない"), flush=True)
        return None
    if not ok:
        sys.exit("柱の系列がそろっていない（上の行）。何も計算していない")
    try:
        series, _ = B.load_series(folder)
        res = B.run(series)
    except NotReady as e:
        sys.exit(f"計算の前に止まった: {e}。何も計算していない（印も書いていない）")
    made = f"{datetime.now():%Y-%m-%d %H:%M}"
    lines = B.report_lines(res, made)
    B.mark(marker, res, lines, made)                            # 先に印（報告の中身も入る）。この後で止まっても、動かし直さない
    out = (Path(args.out) if args.out else root / "output" / "bundle") / f"{datetime.now():%Y%m%d-%H%M}"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    for t in lines[: next(i for i, x in enumerate(lines) if x.startswith("## 2."))]:
        print(t, flush=True)
    print(f"\n報告: {path}（docs/bundle.md 8 章に書き写す。cfdbot/bundle.py の RESULT にも書く）", flush=True)
    return path


if __name__ == "__main__":
    main()
