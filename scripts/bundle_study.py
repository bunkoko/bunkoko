"""束ねる検証 B1（docs/bundle.md）。残っている方法 11 家族を一緒に持つと、偶然を超えるか。判定は 1 回だけ。

    python scripts/bundle_study.py --export    # 柱ごとの月の系列を、元の道具を動かして作る（数十分。成績は画面に出さない）
    python scripts/bundle_study.py --check     # 柱ごとの系列の期間・月の数・欠け・0 の月の数だけを見る（成績は出さない。何度でもよい）
    python scripts/bundle_study.py             # 判定（1 回だけ。数分）

柱ごとの月の系列は data/bundle/series/<名前>.csv（列 month・d）。書き出しが最後まで終わると、同じ場所に印（EXPORT_DONE.json）ができる。
無い系列があるときは --without W7,1C のように名指ししたときだけ、⚠ の行があるときは --warnings-seen を付けたときだけ、判定に進む
（どちらも、理由を docs/bundle.md 8 章に書いてから）。1 回計算したら、data/bundle/B1_DONE.json の印で止まる（動かし直さない）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

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
    p.add_argument("--export", action="store_true", help="柱ごとの月の系列を作って data/bundle/series に書く（判定はしない）")
    p.add_argument("--check", action="store_true", help="期間・月の数・欠け・0 の月の数だけ見る（成績は出さない）")
    p.add_argument("--without", default="", help="無い系列を名指しで外す（例 W7,1C）。実際に無い系列とぴったり同じ名指しのときだけ進む")
    p.add_argument("--warnings-seen", action="store_true", help="⚠ の行を指令役が見て、進めてよいと docs/bundle.md 8 章に書いた後だけ付ける")
    p.add_argument("--out", default=None, help="報告の保存先（既定 output/bundle）")
    args = p.parse_args(argv)
    root = Path(root) if root else ROOT
    folder, marker = root / B.SERIES_DIR, root / B.MARKER       # 印の場所は --out に関係なく決まっている

    if not args.check:
        stop_if_done(marker)
    if args.export:
        from cfdbot import bundle_export as X

        print("柱ごとの系列を作る（元の道具を順に動かす。数十分。判定はやり直さない・成績は画面に出さない）", flush=True)
        status, changed = X.export(root, log=lambda t: print(f"  {t}", flush=True), made=f"{datetime.now():%Y-%m-%d %H:%M}")
        print()
        for k, v in status.items():
            print(f"- {k}: {v}", flush=True)
        if changed:
            print(f"\n書き出しの間に、元のデータが書き換わった: {'・'.join(changed)}", flush=True)
            sys.exit("毎日の自動実行（朝 6:30・8:00、平日 13:05）と重なった見込み。系列どうしの元データが食い違うので、終わった印を書いていない。"
                     "重ならない時間に --export をやり直す")
        print(f"\n成績はまだ何も計算していない。次は --check。{root / B.DIR / 'export'} の下の記録と報告は開かない"
              "（作り直した単独の成績の表が入っている。単独の判定はやり直さない決まり）", flush=True)
        return None
    without = tuple(x.strip() for x in args.without.split(",") if x.strip())
    ok, lines = B.check(folder, without)
    for t in lines:
        print(f"- {t}", flush=True)
    warns = [t for t in lines if t.startswith("⚠")]
    if args.check:
        if not ok:
            nxt = "このままでは判定に進めない（上の最後の行）"
        elif warns:
            nxt = "⚠ の行があるので、計算せずに利用者に伝える（指令役が見る。docs/bundle.md 4.4）"
        else:
            nxt = "判定は 1 回だけ（--check を外して、同じ引数で）"
        print(f"\n成績はまだ何も計算していない。{nxt}", flush=True)
        return None
    if not ok:
        sys.exit("柱の系列がそろっていない（上の最後の行）。何も計算していない")
    if warns and not args.warnings_seen:
        sys.exit("⚠ の行がある（登録の表と形の違う系列）。何も計算していない。利用者に伝えて、指令役が見てから（docs/bundle.md 4.4）")
    try:
        series, _ = B.load_series(folder)
        res = B.run(series)
    except NotReady as e:
        sys.exit(f"計算の前に止まった: {e}。何も計算していない（印も書いていない）")
    made = f"{datetime.now():%Y-%m-%d %H:%M}"
    lines = B.report_lines(res, made)
    sealed, _ = B.read_seal(folder)
    B.mark(marker, res, lines, made, inputs=sealed["sha256"], exported=sealed.get("date"),      # 先に印（報告の中身も入る）。この後で止まっても、動かし直さない
           without=[k for k in B.SERIES_NAMES if k in without], warnings=warns)
    out = (Path(args.out) if args.out else root / "output" / "bundle") / f"{datetime.now():%Y%m%d-%H%M}"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    save_copies(out, res, series)
    print()
    for t in lines[: next(i for i, x in enumerate(lines) if x.startswith("## 2."))]:
        print(t, flush=True)
    print(f"\n報告: {path}（docs/bundle.md 8 章に書き写す。3 章の表も。cfdbot/bundle.py の RESULT にも書く）", flush=True)
    return path


def save_copies(out: Path, res: dict, series: dict) -> None:
    """1 回だけの計算なので、後で確かめられるように控えを残す: 束の月の系列・月ごとの重さ・家族の系列・入力の系列の写し。"""
    def month_first(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df.insert(0, "month", [f"{x:%Y-%m-%d}" for x in df.index])
        return df

    month_first(pd.DataFrame({"d": res["bundle"], "families": res["usable"].astype(int)})).to_csv(out / "bundle.csv", index=False)
    month_first(res["weights"]).to_csv(out / "weights.csv", index=False)
    month_first(res["family_frame"]).to_csv(out / "families.csv", index=False)
    month_first(pd.DataFrame(series).sort_index()).to_csv(out / "series.csv", index=False)


if __name__ == "__main__":
    main()
