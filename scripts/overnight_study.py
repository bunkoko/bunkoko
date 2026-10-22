"""J2 夜だけ持つ（記録だけの売買）の途中経過（./cfd overnight）。決まりは docs/overnight.md（結果を見る前に決めたもの）。

    python scripts/overnight_study.py            # 1306 の日足を取り直して（古ければ）、2026-10-20 からの途中経過を出す
    python scripts/overnight_study.py --report   # 同じ（取り直さない場合は --no-fetch）

お金は動かさない。毎日の記録も要らない（値段の日足から、引けで買って翌朝の寄りで売ったことにして計算する）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot import overnight as O  # noqa: E402


def pct(x, d: int = 1) -> str:
    return "–" if x is None or not np.isfinite(x) else f"{x:.{d}%}"


def f2(x) -> str:
    return "–" if x is None or not np.isfinite(x) else f"{x:.2f}"


def load(path: Path, fetch: bool, now: pd.Timestamp, getter=None) -> pd.DataFrame:
    fresh = path.exists() and (now - pd.to_datetime(pd.read_csv(path)["date"]).max()).days <= 3
    if fetch and not fresh:
        try:
            df = (getter or O.fetch_adjusted)(O.TICKER, "2009-01-01")
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path, index=False)
        except Exception as e:  # noqa: BLE001
            print(f"⚠ {O.TICKER} の日足を取れなかった（{str(e)[:120]}）。手元のデータで続ける")
    if not path.exists():
        raise SystemExit(f"{O.TICKER} の日足が無い（ネットにつないでもう一度）")
    return pd.read_csv(path)


def row(label: str, s: dict) -> str:
    return (f"| {label} | {s['nights']} | {pct(s['night_ann'])} | {pct(s['hold_ann'])} | {pct(s['diff_ann'])} | {f2(s['t'])} | "
            f"{pct(s['night_after_tax'])} | {pct(s['hold_after_tax'])} |")


def main(argv: list[str] | None = None, getter=None) -> Path:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report", action="store_true", help="途中経過を出す（既定と同じ）")
    p.add_argument("--no-fetch", action="store_true", help="値段を取り直さない")
    p.add_argument("--data", default="data/overnight")
    p.add_argument("--out", default="output/overnight")
    p.add_argument("--today", help="今日として扱う日（試験用）")
    args = p.parse_args(argv)
    now = pd.Timestamp(args.today) if args.today else pd.Timestamp.now(tz="Asia/Tokyo").tz_localize(None)
    prices = load(Path(args.data) / "1306.csv", not args.no_fetch, now, getter)
    lg = O.legs(prices)
    fw = O.forward(lg)
    lines: list[str] = []

    def say(t: str = "") -> None:
        print(t, flush=True)
        lines.append(t)

    say("# J2 夜だけ持つ（記録だけの売買）")
    say("")
    say(f"作成: {now:%Y-%m-%d %H:%M}　決まり: docs/overnight.md（結果を見る前に決めたもの）。お金は動かしていない")
    say(f"- 1306 を毎営業日の引けで買ったことにし、翌営業日の寄りで売ったことにする。{O.START} の引けから。比べる相手は 1306 を持ち続ける")
    say(f"- 値段: Yahoo の日足（配当・分割で調整）。最後の日 {pd.to_datetime(prices['date']).max():%Y-%m-%d}")
    say("")
    say("## 1. 途中経過と判定（始めた日から）")
    say("")
    say("| コスト（往復） | 夜の数 | 夜だけ（年率） | 持ち続け（年率） | 差 | 日ごとの差の t 値 | 夜だけ・毎年の税の後（全体） | 持ち続け・最後に税（全体） |")
    say("|---|---|---|---|---|---|---|---|")
    for c in O.COSTS:
        say(row(f"{c:.2%}", O.summary(fw, c)))
    say("")
    s = O.summary(fw, O.JUDGE_COST)
    looks = [k for k in O.LOOKS if s["nights"] >= k]
    if looks:
        for k in looks:
            say(f"- {k} 夜の時点（往復 {O.JUDGE_COST:.2%}）: **{O.verdict(O.summary(O.forward(lg, upto=k), O.JUDGE_COST))}**")
    else:
        say(f"- 判定: **{O.verdict(s)}**（判定は往復 {O.JUDGE_COST:.2%} で、{'・'.join(str(k) for k in O.LOOKS)} 夜の時点だけ。間の数字を見て何も変えない）")
    say("")
    say("## 2. 参考（判定しない）: 過去の同じ計算（コスト 0）")
    say("")
    say("**docs/stocks.md で、往復 0.10% では「使えない」と判定した後の参考**（コスト 0 の数字は、結果を見た後に計算したもの）")
    say("")
    past = lg[(lg.index >= pd.Timestamp(O.REF_FROM)) & (lg.index < pd.Timestamp(O.START))]
    say("| 期間 | 夜の数 | 夜だけ（年率） | 持ち続け（年率） | 差 | 日ごとの差の t 値 | 夜だけ・毎年の税の後（全体） | 持ち続け・最後に税（全体） |")
    say("|---|---|---|---|---|---|---|---|")
    if len(past):
        mid = past.index[len(past) // 2]
        for label, part in (("全体", past), ("前半", past[past.index < mid]), ("後半", past[past.index >= mid])):
            say(row(f"{label} {part.index[0]:%Y-%m}〜{part.index[-1]:%Y-%m}", O.summary(part, 0.0)))
    say("")
    say("## 3. 読み方")
    say("")
    say("- 夜だけ − 持ち続け ＝ −（昼の値動き）。昼が下がりやすいほど、夜だけが勝つ")
    say("- 毎日 2 回の注文（引けで買い・翌朝の寄りで売る）が要る。注文を出せなかった日は、実際には持てない（記録は値段から計算するので、出せた日だけの数字ではない）")
    say("- 税: 夜だけは毎年の利益に税がかかる。持ち続けは売るまで税が先送りになる（ここでは最後に一度だけ当てた）")
    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    path = out / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {path}")
    return path


if __name__ == "__main__":
    main()
