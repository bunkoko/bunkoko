"""案 2-US 米国の 10-K のリスク要因（docs/us_text.md）。

    python scripts/edgar.py universe   # 点検 1: 時点ごとの S&P500（Wikipedia の月末ごとの古い版）と Yahoo の値段のそろい方（数分。SEC は使わない）

    python scripts/edgar.py key        # SEC に名乗る名前と連絡先を保存（~/.bunkoko/sec.json、権限 600）
    python scripts/edgar.py check      # 点検 2: くじで選んだ 60 件の 10-K のリスク要因の始まりと終わり（指令役が読んで判定する）
    python scripts/edgar.py fetch      # 全部の 10-K（1 秒に 1 件。止めても続きから）
    python scripts/edgar.py status     # 点検 3: 近さのある割合
    python scripts/edgar.py lazy       # 判定（1 回だけ）

取ったものは data/edgar/（コミットしない）。判定の決まりは docs/us_text.md 9 章。
"""

from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from cfdbot import edgar as EG  # noqa: E402
from cfdbot import sec as SEC  # noqa: E402
from cfdbot import us_lazy as U  # noqa: E402


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


# --------------------------------------------------------------------------- SEC（点検 2・3 と判定。docs/us_text.md 9 章）
def keep_awake() -> None:
    """取得が終わるまで Mac を眠らせない（ふたを閉じると眠る）。"""
    if sys.platform == "darwin":
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])


def members_frame(args) -> pd.DataFrame:
    path = Path(args.root) / "universe" / "members.csv"
    if not path.exists():
        sys.exit("会社の一覧が無い。先に ./cfd edgar universe（点検 1）を動かす")
    m = pd.read_csv(path, dtype={"ticker": str, "symbol": str})
    m["month"] = pd.to_datetime(m["month"])
    m["cik"] = pd.to_numeric(m["cik"], errors="coerce").astype("Int64")
    return m[m["month"] >= EG.WINDOW_START].reset_index(drop=True)


def sec_client(args, getter=None, sleep=None) -> SEC.Client:
    if getter is None:
        ua = SEC.load_user_agent()
        if not ua:
            sys.exit("SEC に名乗る名前と連絡先が無い。先に ./cfd edgar key で保存する（リポジトリの外に置く）")
        getter = SEC.http_getter(ua)
    kw = {"sleep": sleep} if sleep else {}
    return SEC.Client(getter, log=lambda t: print(t, flush=True), **kw)


def all_filings(args, client: SEC.Client, members: pd.DataFrame) -> pd.DataFrame:
    """2021-12〜の月末に一度でもいた会社の 10-K の一覧（期末が 2019-01-01 以降）。提出の一覧は data/edgar/sec/submissions に残して使い回す。"""
    store = SEC.Store(Path(args.root) / "sec")
    ciks = sorted({int(c) for c in members["cik"].dropna()})
    todo = [c for c in ciks if not store.load_sub(c)]
    if todo:
        print(f"SEC の提出の一覧: {len(todo):,} 社（全部で {len(ciks):,} 社。1 秒に 1 件）…", flush=True)
    for k, c in enumerate(todo):
        sub = SEC.submissions(client, c)
        if sub:
            store.save_sub(c, sub)
        if (k + 1) % 50 == 0:
            print(f"  {k + 1}/{len(todo)}", flush=True)
    parts = [SEC.tenk_filings(store.load_sub(c), c) for c in ciks if store.load_sub(c)]
    f = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=SEC.COLUMNS)
    f.to_csv(Path(args.root) / "sec" / "filings.csv", index=False)
    return f


def cmd_key(args) -> None:
    name = getpass.getpass("SEC に名乗る名前（個人名か屋号。画面には出ません）: ").strip()
    mail = getpass.getpass("連絡先のメール（画面には出ません）: ").strip()
    if not name or "@" not in mail:
        sys.exit("名前とメールの両方が要る。保存していない")
    print(f"保存した: {SEC.save_user_agent(f'{name} {mail}')}（権限 600。このリポジトリの外。記録・報告には出さない）")


def cmd_check(args, getter=None, sleep=None) -> Path:
    members = members_frame(args)
    client = sec_client(args, getter, sleep)
    store = SEC.Store(Path(args.root) / "sec")
    filings = all_filings(args, client, members)
    sample = SEC.sample_for_check(filings, SEC.CHECK_PER_YEAR)
    print(f"点検 2: くじで選んだ 10-K {len(sample)} 件を取る…", flush=True)
    SEC.fetch_docs(client, store, sample, pd.Timestamp.now(), log=lambda t: print(t, flush=True))
    rows = SEC.check_rows(store, sample)
    lines = ["# 案 2-US の点検 2: リスク要因を取り出せるか", "",
             f"作成: {datetime.now():%Y-%m-%d %H:%M}　決まり: docs/us_text.md 9.1（くじ: 2020 年 6 件・2021〜2026 年 各 9 件。決まった種）",
             f"- 10-K の一覧: {len(filings):,} 件・{filings['cik'].nunique() if len(filings) else 0:,} 社。くじ {len(rows)} 件",
             "- 通る条件: 57 件以上で、始まりが本文の「Item 1A. Risk Factors」の見出し、終わりが Item 1B・1C・2 の前。どの年も失敗が 2 割未満。"
             "**判定は指令役が、下の始まりと終わりの文を読んで行う**（コードの自己判定ではない）", "",
             "| # | 年 | CIK | 受付番号 | 文字数 | 状態 |", "|---|---|---|---|---|---|"]
    lines += [f"| {i + 1} | {r['year']} | {r['cik']} | {r['accession']} | {r['chars']:,} | {r['status']} |" for i, r in enumerate(rows)]
    lines += ["", "## 始まりと終わり（各 300 字）", ""]
    for i, r in enumerate(rows):
        lines += [f"### {i + 1}（{r['year']} 年・{r['chars']:,} 字）", "", "始まり: " + r["head"].replace("\n", " / "), "",
                  "終わり: " + r["tail"].replace("\n", " / "), ""]
    return write(Path(args.out), "check.md", lines)


def cmd_fetch(args, getter=None, sleep=None) -> None:
    members = members_frame(args)
    store = SEC.Store(Path(args.root) / "sec")
    if not store.lock():
        sys.exit("ほかの取得が動いている（同時に 1 本だけ）")
    keep_awake()
    try:
        client = sec_client(args, getter, sleep)
        filings = all_filings(args, client, members)
        print(f"10-K の本文: {len(filings):,} 件のうち、まだ取っていないもの（1 秒に 1 件。止めても続きから）…", flush=True)
        got = SEC.fetch_docs(client, store, filings, pd.Timestamp.now(), log=lambda t: print(t, flush=True))
        print(f"10-K: 取った {got['docs']:,}・リスク要因が無い {got['empty']:,}・取れない {got['missing']:,}・残り {got['left']:,}", flush=True)
    except SEC.Stop as e:
        raise SystemExit(f"止めた: {e}（同じコマンドで続きから）")
    finally:
        store.unlock()


def lazy_inputs(args) -> dict:
    """判定に使うもの（取得が終わっていて、動いていないときだけ）。"""
    members = members_frame(args)
    store = SEC.Store(Path(args.root) / "sec")
    fpath = Path(args.root) / "sec" / "filings.csv"
    if not fpath.exists():
        sys.exit("10-K の一覧が無い。先に ./cfd edgar check → fetch")
    lock = store.root / ".lock"
    if lock.exists():
        try:
            os.kill(int(lock.read_text().strip() or 0), 0)
            sys.exit("10-K の取得がまだ動いている。終わってから動かす")
        except (OSError, ValueError):
            pass
    filings = pd.read_csv(fpath, parse_dates=["filing_date", "report_date", "acceptance"])
    ix = store.index()
    left = filings[~filings["accession"].astype(str).isin(set(ix["accession"]))]
    if len(left):
        sys.exit(f"10-K の本文の取得がまだ終わっていない（{len(filings):,} 件のうち残り {len(left):,} 件）。先に ./cfd edgar fetch を終える")
    cik_of = dict(zip(filings["accession"], filings["cik"]))
    cache: dict[str, dict] = {}

    def text_of(acc: str, kind: str) -> str:
        if acc not in cache:
            cache[acc] = store.load_doc(int(cik_of[acc]), acc)
        return str(cache[acc].get(kind, ""))

    names = members.dropna(subset=["cik"]).groupby("cik")["name"].agg(lambda s: sorted(set(map(str, s))))
    edgar_names = {int(c): str(store.load_sub(int(c)).get("name", "")) for c in members["cik"].dropna().unique()}

    def names_of(cik: int) -> list[str]:
        return [n for n in names.get(cik, []) + [edgar_names.get(int(cik), "")] if n]

    close = pd.read_csv(Path(args.root) / "us_close.csv", index_col=0, parse_dates=True)
    mclose = EG.monthly_last(close)
    trade_days = {m: close.index[close.index <= m].max() for m in mclose.index}
    return {"members": members, "filings": filings, "index": ix, "text_of": text_of, "names_of": names_of,
            "mclose": mclose, "trade_days": trade_days}


def cmd_status(args) -> None:
    d = lazy_inputs(args)
    ix = d["index"]
    pr = U.pairs(d["filings"], d["text_of"], d["names_of"], "risks")
    sig = U.signals(pr, sorted(d["members"]["month"].unique()), d["trade_days"])
    cov = U.coverage(d["members"], sig, EG.WINDOW_START)
    print(f"10-K: {len(d['filings']):,} 件（取った {int((ix['status'] == 'ok').sum()):,}・リスク要因が無い {int((ix['status'] == 'empty').sum()):,}・"
          f"取れない {int((ix['status'] == 'missing').sum()):,}）。前年と組にできた「リスク要因」{len(pr):,} 組")
    print(f"点検 3: 会社 × 月のうち、近さのある割合 {cov:.1%}（線は {U.COVERAGE_MIN:.0%}）→ **{'通った' if cov >= U.COVERAGE_MIN else '通らなかった'}**")


def cmd_lazy(args) -> Path:
    done = sorted(Path(args.out).glob("*/lazy.md"))
    if done and not args.again:
        sys.exit(f"判定はもう 1 回計算してある（{done[-1]}）。1 回だけの決まり（docs/us_text.md 5 章）なので、動かし直さない")
    d = lazy_inputs(args)
    pr = U.pairs(d["filings"], d["text_of"], d["names_of"], "risks")
    pm = U.pairs(d["filings"], d["text_of"], d["names_of"], "mdna")
    ends = sorted(d["members"]["month"].unique())
    sig, sigm = U.signals(pr, ends, d["trade_days"]), U.signals(pm, ends, d["trade_days"])
    today = pd.Timestamp(args.today) if args.today else pd.Timestamp.now(tz="America/New_York").tz_localize(None)
    print(f"判定を計算する（偶然との比べ {U.PLACEBO} 回。数分）…", flush=True)
    res = U.evaluate(d["members"], sig, d["mclose"], np.random.default_rng(20261021), sig_mdna=sigm, today=today)
    meta = {"made": f"{datetime.now():%Y-%m-%d %H:%M}", "filings": len(d["filings"]), "firms": int(d["filings"]["cik"].nunique()),
            "pairs": len(pr), "pairs_mdna": len(pm), "sim_median": float(pr["sim"].median()) if len(pr) else float("nan"),
            "no_cik": int(d["members"].loc[d["members"]["cik"].isna(), "ticker"].nunique())}
    lines = U.report_lines(res, meta)
    for t in lines:
        print(t, flush=True)
    return write(Path(args.out), "lazy.md", lines)


def main(argv: list[str] | None = None, api=None, prices=None, refetch=None, getter=None, sleep=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["universe", "key", "check", "fetch", "status", "lazy"])
    p.add_argument("--again", action="store_true", help="lazy: 試験用（判定をもう一度。本番では使わない）")
    p.add_argument("--root", default="data/edgar")
    p.add_argument("--out", default="output/edgar")
    p.add_argument("--today", help="今日として扱う日（試験用）")
    args = p.parse_args(argv)
    if args.command == "universe":
        return cmd_universe(args, api, prices, refetch)
    if args.command == "key":
        return cmd_key(args)
    if args.command == "check":
        return cmd_check(args, getter, sleep)
    if args.command == "fetch":
        return cmd_fetch(args, getter, sleep)
    if args.command == "status":
        return cmd_status(args)
    return cmd_lazy(args)


if __name__ == "__main__":
    main()
