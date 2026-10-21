"""案 2-US 米国の 10-K のリスク要因（docs/us_text.md）。

    python scripts/edgar.py universe   # 点検 1: 時点ごとの S&P500（Wikipedia）と Yahoo の値段のそろい方（数分。SEC は使わない）

点検 1 を通った後に、key・check・fetch・status・lazy を足す。取ったものは data/edgar/（コミットしない）。
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from cfdbot import edgar as EG  # noqa: E402


def pct(x: float, d: int = 1) -> str:
    return f"{x:.{d}%}"


def write(out_dir: Path, name: str, lines: list[str]) -> Path:
    out = out_dir / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {path}")
    return path


def wiki_tables(root: Path | None = None) -> list[pd.DataFrame]:
    import stocks_study as M

    html = M.http_get(EG.WIKI_SP500).decode("utf-8")
    if root is not None:
        (root / "wiki.html").write_text(html, encoding="utf-8")          # 表の形が変わったときに見直すため（コミットしない）
    return pd.read_html(io.StringIO(html))


def fetch_prices(tickers: list[str], since: str) -> pd.DataFrame:
    import stocks_study as M

    return M.fetch_daily(tickers, since)[1]


def cmd_universe(args, tables=None, prices=None) -> Path:
    root = Path(args.root) / "universe"
    root.mkdir(parents=True, exist_ok=True)
    today = pd.Timestamp(args.today) if args.today else pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
    last = (today.to_period("M") - 1).to_timestamp(how="end").normalize()          # 終わった最後の月
    print("Wikipedia の S&P500 の一覧と変更の表…", flush=True)
    try:
        current, changes = EG.parse_wiki(tables if tables is not None else wiki_tables(root))
    except ValueError as e:
        sys.exit(f"止めた: {e}\n（この文を Claude に貼ってください。ページは {root / 'wiki.html'} に保存した）")
    current.to_csv(root / "wiki_current.csv", index=False)
    changes.to_csv(root / "wiki_changes.csv", index=False)
    ends = EG.month_ends(EG.START, last)
    members = EG.membership(current, changes, ends)
    rows = [{"month": m, "ticker": t} for m, ts in members.items() for t in sorted(ts)]
    pd.DataFrame(rows).to_csv(root / "members.csv", index=False)
    ever = sorted({t for ts in members.values() for t in ts})
    print(f"値段（Yahoo）: これまでに S&P500 にいた {len(ever)} 社…", flush=True)
    close = prices(ever, "2014-01-01") if prices is not None else fetch_prices(ever, "2014-01-01")
    close.to_csv(Path(args.root) / "us_close.csv", float_format="%.6g")
    mclose = EG.monthly_last(close)
    cov = EG.coverage(members, mclose)
    miss = EG.missing_names(members, mclose)
    tab = cov["table"]

    lines: list[str] = []

    def say(t: str = "") -> None:
        print(t, flush=True)
        lines.append(t)

    say("# 案 2-US の点検 1: 時点ごとの S&P500 と値段のそろい方")
    say("")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　決まり: docs/us_text.md 4 章（データを取る前に決めたもの）。SEC は使っていない")
    say(f"- Wikipedia: 今の一覧 {len(current)} 社・変更の表 {len(changes)} 行（{changes['date'].min():%Y-%m-%d}〜{changes['date'].max():%Y-%m-%d}）")
    say(f"- 月末: {ends[0]:%Y-%m}〜{ends[-1]:%Y-%m}（{len(ends)} か月）。これまでにいた会社 {len(ever)} 社、Yahoo の値段のある会社 {int(close.notna().any().sum())} 社")
    say("")
    say("## 1. 判定")
    say("")
    say("| 点検 | 条件 | 結果 | 通るか |")
    say("|---|---|---|---|")
    say(f"| (a) 毎月末の会社の数 | どの月も {EG.COUNT_RANGE[0]}〜{EG.COUNT_RANGE[1]} 社（外れる月が {EG.MAX_BAD_MONTHS} か月まで） | "
        f"最少 {tab['members'].min()}・最多 {tab['members'].max()}、外れた月 {len(cov['bad_months'])} | {'○' if cov['ok_count'] else '×'} |")
    say(f"| (b) 値段の無い割合 | 会社 × 月の {pct(EG.MAX_MISSING, 0)} 以下 | {pct(cov['missing'])}（{int(tab['members'].sum() - tab['with_price'].sum()):,} / {int(tab['members'].sum()):,}） | "
        f"{'○' if cov['ok_price'] else '×'} |")
    say("")
    say(f"- **点検 1: {'通った' if cov['ok'] else '通らなかった'}**" + ("（次は ./cfd edgar key → check）" if cov["ok"] else
        "（無料のデータでは正直に確かめられない。計算しない・試した数も足さない。有料のデータで組むかは利用者に聞く）"))
    say("")
    say("## 2. 年ごと")
    say("")
    say("| 年 | 会社の数（最少〜最多） | 値段の無い割合 |")
    say("|---|---|---|")
    for y, g in tab.groupby(tab.index.year):
        say(f"| {y} | {g['members'].min()}〜{g['members'].max()} | {pct(1 - g['with_price'].sum() / g['members'].sum())} |")
    if len(cov["bad_months"]):
        say("")
        say("- 数が外れた月: " + "、".join(f"{m:%Y-%m}（{n}）" for m, n in cov["bad_months"]["members"].items()))
    say("")
    say(f"## 3. 値段の無い会社（多い順に 30。全部で {len(miss)} 社）")
    say("")
    say("、".join(f"{t}（{n} か月）" for t, n in miss.head(30).items()) if len(miss) else "無し")
    say("")
    say("## 4. 読み方・限界")
    say("")
    say("- Wikipedia の変更の表は抜けや記号の変更（会社の名前・記号が変わった）がありうる。数が外れる月はその印")
    say("- 値段の無い会社の多くは、買収・上場廃止で Yahoo から値段が消えた会社。記号を別の会社が使い回していると、別の会社の値段が入ることがある")
    say("- データは data/edgar/（コミットしない）。この一覧（members.csv）を後の計算でもそのまま使う（取り直さない）")
    return write(Path(args.out), "universe.md", lines)


def main(argv: list[str] | None = None, tables=None, prices=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["universe"])
    p.add_argument("--root", default="data/edgar")
    p.add_argument("--out", default="output/edgar")
    p.add_argument("--today", help="今日として扱う日（試験用）")
    args = p.parse_args(argv)
    if args.command == "universe":
        return cmd_universe(args, tables, prices)
    return None


if __name__ == "__main__":
    main()
