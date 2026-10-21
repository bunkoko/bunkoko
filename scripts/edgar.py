"""案 2-US 米国の 10-K のリスク要因（docs/us_text.md）。

    python scripts/edgar.py universe   # 点検 1: 時点ごとの S&P500（Wikipedia の月末ごとの古い版）と Yahoo の値段のそろい方（数分。SEC は使わない）

点検 1 を通った後に、key・check・fetch・status・lazy を足す。取ったものは data/edgar/（コミットしない）。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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


UA = "bunkoko-research/1.0 (https://github.com/bunkoko/bunkoko; personal research)"   # Wikipedia の決まりで、取る人を名乗る（個人の連絡先は入れない）
WAITS = (10.0, 30.0, 60.0)


def wiki_api(params: dict, sleep=time.sleep) -> dict:
    """Wikipedia の API を 1 回呼ぶ（1 秒に 1 件。断られたら 10・30・60 秒待つ）。"""
    url = EG.WIKI_API + "?" + urlencode({**params, "format": "json", "formatversion": 2})
    for k in range(len(WAITS) + 1):
        sleep(1.0)
        try:
            with urlopen(Request(url, headers={"User-Agent": UA}), timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as e:
            if k == len(WAITS):
                raise SystemExit(f"Wikipedia に断られ続けた（{type(e).__name__}）。時間をおいて同じコマンドで続きから")
            sleep(WAITS[k])
    return {}


def snapshot(month: pd.Timestamp, root: Path, api=wiki_api) -> pd.DataFrame:
    """その月末の時点のページの版（月末の翌日 0 時 UTC より前の、一番新しい版）の一覧。取った版は data/edgar/universe/snapshots/ に残し、次は使い回す。"""
    d = root / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{month:%Y-%m}.html"
    if not path.exists():
        t = (month + pd.Timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
        q = api({"action": "query", "prop": "revisions", "titles": EG.WIKI_TITLE, "rvlimit": 1, "rvdir": "older",
                 "rvstart": t, "rvprop": "ids|timestamp"})
        rev = q["query"]["pages"][0]["revisions"][0]
        html = api({"action": "parse", "oldid": rev["revid"], "prop": "text"})["parse"]["text"]
        path.write_text(html, encoding="utf-8")
        with (d / "revisions.csv").open("a", encoding="utf-8") as f:
            f.write(f"{month:%Y-%m-%d},{rev['revid']},{rev['timestamp']}\n")
    return EG.parse_constituents(pd.read_html(io.StringIO(path.read_text(encoding="utf-8"))))


def fetch_prices(tickers: list[str], since: str) -> pd.DataFrame:
    import stocks_study as M

    return M.fetch_daily(tickers, since)[1]


def refetch_prices(symbols: list[str], since: str, sleep=time.sleep) -> pd.DataFrame:
    """まとめての取得で値段の無かった記号を、1 つずつもう 1 回だけ取る（9 章。待ちのやり直しはしない）。"""
    import yfinance as yf  # type: ignore

    got = {}
    for s in symbols:
        sleep(1.0)
        try:
            d = yf.download([s], start=since, auto_adjust=True, progress=False, threads=False, group_by="column")
        except Exception:  # noqa: BLE001
            continue
        if d is None or d.empty or "Close" not in d:
            continue
        c = d["Close"]
        c = c.iloc[:, 0] if isinstance(c, pd.DataFrame) else c
        if c.index.tz is not None:
            c.index = c.index.tz_localize(None)
        if c.notna().any():
            got[s] = c
    return pd.DataFrame(got)


def cmd_universe(args, api=None, prices=None, refetch=None) -> Path:
    root = Path(args.root) / "universe"
    root.mkdir(parents=True, exist_ok=True)
    today = pd.Timestamp(args.today) if args.today else pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
    last = (today.to_period("M") - 1).to_timestamp(how="end").normalize()          # 終わった最後の月
    ends = EG.month_ends(EG.START, last)
    print(f"Wikipedia の S&P500 の一覧の、月末ごとの古い版 {len(ends)} 個（1 秒に 1 件。取った版は使い回す）…", flush=True)
    snaps = {}
    for k, m in enumerate(ends):
        try:
            snaps[m] = snapshot(m, root, api or wiki_api)
        except ValueError as e:
            sys.exit(f"止めた: {m:%Y-%m} の版: {e}\n（この文を Claude に貼ってください）")
        if (k + 1) % 24 == 0:
            print(f"  {k + 1}/{len(ends)}（{m:%Y-%m}: {len(snaps[m])} 社）", flush=True)
    _, info = EG.membership_from_snapshots(snaps)
    info = EG.link_symbols(info)
    info.to_csv(root / "members.csv", index=False)
    linked = info[info["symbol"] != info["ticker"]].drop_duplicates("ticker")
    members_all = EG.members_by_symbol(info)
    members = EG.members_by_symbol(info, EG.WINDOW_START)
    ever = sorted({t for ts in members_all.values() for t in ts})
    print(f"値段（Yahoo）: これまでに S&P500 にいた {len(ever)} 社（記号は CIK でつないだもの）…", flush=True)
    close = prices(ever, "2014-01-01") if prices is not None else fetch_prices(ever, "2014-01-01")
    lost = [t for t in ever if t not in close.columns or not close[t].notna().any()]
    print(f"値段の無かった {len(lost)} 社を、1 つずつもう 1 回だけ取る…", flush=True)
    again = (refetch or refetch_prices)(lost, "2014-01-01") if lost else pd.DataFrame()
    if len(again.columns):
        close = pd.concat([close.drop(columns=[c for c in again.columns if c in close.columns]), again], axis=1).sort_index()
    close.to_csv(Path(args.root) / "us_close.csv", float_format="%.6g")
    mclose = EG.monthly_last(close)
    cov = EG.coverage(members, mclose)
    cov_all = EG.coverage(members_all, mclose)
    miss = EG.missing_names(members, mclose)
    tab = cov["table"]

    lines: list[str] = []

    def say(t: str = "") -> None:
        print(t, flush=True)
        lines.append(t)

    say("# 案 2-US の点検 1: 時点ごとの S&P500 と値段のそろい方")
    say("")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　決まり: docs/us_text.md 9 章（判定の版: {EG.WINDOW_START:%Y-%m} 末からの月末。成績を見る前に決めたもの）。SEC は使っていない")
    say(f"- Wikipedia: 「{EG.WIKI_TITLE}」の月末ごとの古い版 {len(snaps)} 個（版の番号は data/edgar/universe/snapshots/revisions.csv）。業種はその時点の版の GICS")
    say(f"- 判定の版の月末: {EG.WINDOW_START:%Y-%m}〜{ends[-1]:%Y-%m}（{len(members)} か月）。これまでにいた会社 {len(ever)} 社、Yahoo の値段のある会社 {int(close.notna().any().sum())} 社")
    say(f"- 記号を CIK でつないだ会社 {len(linked)} 社（例: " + "、".join(f"{r.ticker}→{r.symbol}" for r in linked.head(8).itertuples()) + "）。"
        f"まとめての取得で値段の無かった {len(lost)} 社のうち、取り直して取れた {len(again.columns)} 社")
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
    say(f"- 参考（判定しない）: 2015 年からの版（2〜5 章。2026-10-21 に止めた）を同じ直しで数えると、値段の無い割合 {pct(cov_all['missing'])}")
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
    say("- Wikipedia の古い版は、その時点の編集者の書いた一覧（抜け・遅れ・いたずらの版がありうる）。数が外れる月はその印")
    say("- 値段の無い会社の多くは、買収・上場廃止で Yahoo から値段が消えた会社。記号を別の会社が使い回していると、別の会社の値段が入ることがある")
    say("- データは data/edgar/（コミットしない）。この一覧（members.csv）を後の計算でもそのまま使う（取り直さない）")
    return write(Path(args.out), "universe.md", lines)


def main(argv: list[str] | None = None, api=None, prices=None, refetch=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["universe"])
    p.add_argument("--root", default="data/edgar")
    p.add_argument("--out", default="output/edgar")
    p.add_argument("--today", help="今日として扱う日（試験用）")
    args = p.parse_args(argv)
    if args.command == "universe":
        return cmd_universe(args, api, prices, refetch)
    return None


if __name__ == "__main__":
    main()
