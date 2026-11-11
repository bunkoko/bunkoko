"""質（収益性の高い会社）に傾ける検証 Q1（docs/quality.md）。1 回だけ計算する。

    python scripts/quality_study.py --check             # ファイルを取って形だけ見る（差は計算しない。何度でもよい）
    python scripts/quality_study.py --trials <数>       # 判定（1 回だけ。数は docs/quality.md 5 章の決め方で）

フレンチ教授のデータ集から 2 つのファイルを取る。取れない・形が登録と違うときは、何も計算せずに止まる。
1 回計算したら、報告の保存先に関係なく data/quality/Q1_DONE.json の印で止まる（動かし直さない）。
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot import quality as Q  # noqa: E402

FRENCH = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"


def http_get(url: str, tries: int = 3) -> bytes:
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh)"})
            return urllib.request.urlopen(req, timeout=60).read()
        except Exception:  # noqa: BLE001
            if k == tries - 1:
                raise
            time.sleep(5 * (k + 1))
    raise RuntimeError(url)


def stop_if_done(marker: Path) -> None:
    rec = Q.done(marker)
    if rec is not None:
        sys.exit(f"判定はもう 1 回計算してある（{rec.get('date')}: {rec.get('verdict')}。{rec.get('detail', '')}）。"
                 "1 回だけの決まり（docs/quality.md 5 章）なので、動かし直さない")


def main(argv: list[str] | None = None, getter=None, root: Path | None = None) -> Path | None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="ファイルを取って形だけ見る（差は計算しない）")
    p.add_argument("--trials", type=int, help=f"Q1 が何個目の検証か（{Q.PRIOR_TRIALS + 1} 以上。docs/quality.md 5 章）")
    p.add_argument("--out", default=None, help="報告の保存先（既定 output/quality）")
    args = p.parse_args(argv)
    root = Path(root) if root else ROOT
    marker = root / Q.MARKER                       # 印の場所は --out に関係なく決まっている
    getter = getter or http_get

    if not args.check:
        stop_if_done(marker)                       # 取りに行く前に見る
        if args.trials is None or not Q.PRIOR_TRIALS < args.trials <= Q.MAX_TRIALS:
            sys.exit(f"--trials に、Q1 が何個目の検証かを渡す（{Q.PRIOR_TRIALS + 1} 以上。今の数は docs/review.md の最後の「試した数」に 1 を足す。"
                     "docs/quality.md 5 章）。形だけ見るなら --check")

    blobs = {}
    for name in (Q.FILE_PORT, Q.FILE_MKT):
        try:
            blobs[name] = getter(f"{FRENCH}{name}_CSV.zip")
        except Exception as e:  # noqa: BLE001
            sys.exit(f"{name} を取れなかった（{type(e).__name__}: {str(e)[:120]}）。何も計算していない。"
                     "ファイルが無い（404）なら、この検証は計算せずに閉じる（docs/quality.md 4 章）。回線の不調なら、時間をおいてもう一度")
    try:
        port, mkt, info = Q.load(blobs[Q.FILE_PORT], blobs[Q.FILE_MKT])
    except Q.QualityError as e:
        sys.exit(f"形が登録と違う: {e}。何も計算していない（docs/quality.md 4 章）")
    print(f"形の点検は通った: {Q.FILE_PORT} の「{Q.COLUMN}」と {Q.FILE_MKT}、{info['start']}〜{info['end']} の {info['months']} か月"
          f"（列: {'、'.join(info['columns'])}。組と市場の相関 {info['corr']:.2f}）", flush=True)
    if args.check:
        print("差はまだ計算していない。判定は --trials <数> で 1 回だけ", flush=True)
        return None

    made = f"{datetime.now():%Y-%m-%d %H:%M}"
    try:
        j = Q.q1(port, mkt, args.trials)
    except Q.QualityError as e:
        sys.exit(str(e))
    lines = Q.report_lines(j, info, made)
    Q.mark(marker, j, lines, made)                 # 先に印（報告の中身も入る）。この後で止まっても、動かし直さない
    out = (Path(args.out) if args.out else root / "output" / "quality") / f"{datetime.now():%Y%m%d-%H%M}"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for t in lines:
        print(t, flush=True)
    print(f"\n報告: {path}（docs/quality.md 9 章に書き写す。cfdbot/quality.py の RESULT にも書く）", flush=True)
    return path


if __name__ == "__main__":
    main()
