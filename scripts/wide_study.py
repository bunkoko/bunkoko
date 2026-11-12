"""広げた候補 7 つ（W1〜W7）と、その組み合わせ（WX）の検証（docs/wide.md）。判定は 1 回だけ。

    python scripts/wide_study.py --fetch                  # W1 の ETF、W7 の FRED、W2 の公式の日程のページを取る（数分）
    python scripts/wide_study.py --fetch-edinet           # W6 の EDINET の一覧を 10 年分（1 時間半ほど。止めても続きから）
    python scripts/wide_study.py --check                  # ルールごとに期間・件数・形だけを見る（リターンは出さない。何度でもよい）
    python scripts/wide_study.py --fomc-checked "<何と見比べたか>"   # W2 の日付を公式と見比べた印（自動で見比べられなかったとき）
    python scripts/wide_study.py [--without W6 ...]       # 判定（1 回だけ。点検を通らなかったルールだけ外せる）

1 回計算したら、報告の保存先に関係なく data/wide/W_DONE.json の印で止まる（動かし直さない）。
"""

from __future__ import annotations

import argparse
import importlib
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot.wide import core as C  # noqa: E402
from cfdbot.wide.base import NotReady  # noqa: E402

FETCH = ("W1", "W7", "W2")          # --fetch で取るルール（W6 は時間がかかるので別）


def rule(key: str):
    return importlib.import_module(f"cfdbot.wide.{key.lower()}")


def stop_if_done(marker: Path) -> None:
    rec = C.done(marker)
    if rec is not None:
        v = "・".join(f"{k} {x}" for k, x in (rec.get("verdicts") or {}).items())
        sys.exit(f"判定はもう 1 回計算してある（{rec.get('date')}: {v or rec.get('detail', '')}）。"
                 "1 回だけの決まり（docs/wide.md 3 章）なので、動かし直さない")


def run_checks(data: Path) -> dict:
    res = {}
    for k in C.RULES:
        try:
            res[k] = rule(k).check(data)
        except Exception as e:  # noqa: BLE001  点検そのものが落ちても、ほかのルールは見る
            from cfdbot.wide.base import Check
            res[k] = Check(key=k, ok=False, why=f"点検が落ちた（{type(e).__name__}: {str(e)[:160]}）")
    return res


def print_checks(res: dict) -> None:
    for k in C.RULES:
        c = res[k]
        print(f"\n{k} {C.NAMES[k]}: {'通った' if c.ok else '通らない'}" + ("" if c.ok else f"（{c.why}）"), flush=True)
        for t in c.lines:
            print(f"  - {t}", flush=True)


def main(argv: list[str] | None = None, root: Path | None = None) -> Path | None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fetch", action="store_true", help="W1・W7・W2 のデータを取る")
    p.add_argument("--fetch-edinet", action="store_true", help="W6 の EDINET の一覧を取る（続きから）")
    p.add_argument("--check", action="store_true", help="期間・件数・形だけ見る（リターンは出さない）")
    p.add_argument("--fomc-checked", metavar="NOTE", help="W2 の日付を公式と見比べた印を付ける")
    p.add_argument("--without", nargs="*", default=[], help="点検を通らなかったルール（例 W6）を外す")
    p.add_argument("--out", default=None, help="報告の保存先（既定 output/wide）")
    args = p.parse_args(argv)
    root = Path(root) if root else ROOT
    data = root / "data"
    marker = root / C.MARKER                       # 印の場所は --out に関係なく決まっている

    if args.fetch or args.fetch_edinet:
        for k in (FETCH if args.fetch else ()) + (("W6",) if args.fetch_edinet else ()):
            print(f"{k} {C.NAMES[k]} のデータを取る…", flush=True)
            try:
                rule(k).fetch(data)
            except Exception as e:  # noqa: BLE001  1 つ取れなくても、ほかは取る
                print(f"  {k} は取れなかった（{type(e).__name__}: {str(e)[:200]}）。時間をおいて同じコマンドで", flush=True)
        print("\n次は --check（期間・件数・形だけを見る）", flush=True)
        return None
    if args.fomc_checked is not None:
        note = args.fomc_checked.strip()
        if not note:
            sys.exit("何と見比べたかを書く（例: federalreserve.gov の 2000〜2026 年の日程のページ）")
        rule("W2").mark_checked(data, note)
        print("W2 の日付を公式と見比べた印を付けた", flush=True)
        return None

    if not args.check:
        stop_if_done(marker)
    res = run_checks(data)
    print_checks(res)
    if args.check:
        print("\nリターンはまだ 1 つも計算していない。判定は引数なしで 1 回だけ（通らないルールは --without に書く）", flush=True)
        return None

    without = sorted({w.upper() for w in args.without})
    bad = [w for w in without if w not in C.RULES]
    if bad:
        sys.exit(f"--without に知らない記号がある: {bad}（W1〜W7）")
    passed = [w for w in without if res[w].ok]
    if passed:
        sys.exit(f"点検を通ったルールは外せない: {passed}（docs/wide.md 3 章）。何も計算していない")
    failing = [k for k in C.RULES if not res[k].ok and k not in without]
    if failing:
        sys.exit(f"点検を通らないルールがある: {failing}。データを取り直すか、計算せずに閉じるなら --without {' '.join(failing)}"
                 "（理由を docs/wide.md 10 章に書く）。何も計算していない")

    streams, skipped = {}, {w: res[w].why or "点検を通らない" for w in without}
    for k in C.RULES:
        if k in skipped:
            continue
        try:
            streams[k] = rule(k).compute(data)
        except NotReady as e:
            sys.exit(f"{k} の計算の前に止まった: {e}。何も判定していない（印も書いていない）")
    single = {k: C.judge(s) for k, s in streams.items()}
    wx = C.judge_combos(single, streams)
    made = f"{datetime.now():%Y-%m-%d %H:%M}"
    lines = C.report_lines(single, wx, streams, skipped, made)
    C.mark(marker, single, wx, lines, made)        # 先に印（報告の中身も入る）。この後で止まっても、動かし直さない
    out = (Path(args.out) if args.out else root / "output" / "wide") / f"{datetime.now():%Y%m%d-%H%M}"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for t in lines[: next(i for i, x in enumerate(lines) if x.startswith("## 2."))]:
        print(t, flush=True)
    print(f"\n報告: {path}（docs/wide.md 10 章に書き写す。cfdbot/wide/core.py の RESULT にも書く）", flush=True)
    return path


if __name__ == "__main__":
    main()
