"""デモ口座（本番）で動く EA のログを、同じデータ・同じ構成の Python と 1 日ずつ照らし合わせる。

毎週の確認（docs/operations.md 12 章）で、MT5 から書き出して ./cfd data の後に:

    ./cfd demo-check --start 2026-10-06 --equity 10000000

- --start: その口座で EA が判断した最初の足の日付（Python の再現もこの日から。./cfd replay と同じ）
- --count-from: 直した後の確かめ直しで、条件を数え始める日（Python の再現は --start から続ける）
- EA のログ: MT5 のデータフォルダの MQL5/Logs/<YYYYMMDD>.log（自動で探す。--logs でフォルダかファイルを指定もできる。
  VPS に移した後は、移した日までの Mac のログと VPS のログ（日付ごとのファイルのまま）を 1 つのフォルダに入れて渡す）
- 構成: ./cfd preset で最後に置いたもの（./cfd replay と同じ）
- 結果: 画面と output/demo_check/<日時>/report.md・days.csv
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from compare_all import chart_key  # noqa: E402
from compare_signals import signals_for_sleeve  # noqa: E402
from replay import clean_date, replay  # noqa: E402

from cfdbot.demo_check import (compare_symbol, python_daily, read_logs, reasons, server_dates,  # noqa: E402
                               shifted_close_match)
from cfdbot.events import server_to_utc  # noqa: E402
from cfdbot.mt5files import choose_terminal, find_terminals  # noqa: E402
from cfdbot.mt5specs import ACCOUNT_FILE, load_account  # noqa: E402
from cfdbot.train.config import TIMEFRAMES, load_train_config  # noqa: E402
from cfdbot.train.dataset import load_dataset  # noqa: E402

SIG = {1: "買い", -1: "売り", 0: "なし"}
# 本番に進む条件（docs/operations.md 12 章に事前登録。変えるときは結果を見る前に docs と一緒に）
MIN_CALENDAR_DAYS = 28
MIN_COMPARED_DAYS = 20
RECHECK_CALENDAR_DAYS = 14    # 直した後の確かめ直し（--count-from）は 2 週間
RECHECK_COMPARED_DAYS = 10
MAX_MISSING_SHARE = 0.10
LAST_DAYS_NO_GAP = 5
MIN_ENTRIES = 2
SERIOUS_SEVERITY = "要確認"
EXPORT_MARGIN = pd.Timedelta(minutes=15)
_DATED_LOG = re.compile(r"^\d{8}\.log$")


def log_files(args) -> tuple[list[Path], str]:
    if args.logs:
        files: list[Path] = []
        for s in args.logs:
            p = Path(s).expanduser()
            files += sorted(p.glob("*.log")) if p.is_dir() else [p]
        return files, ", ".join(args.logs)
    found = find_terminals()
    term = choose_terminal(found, args.terminal, args.mql5)
    d = term.mql5 / "Logs"
    files = sorted(f for f in d.glob("*.log") if _DATED_LOG.match(f.name)) if d.is_dir() else []
    return files, str(d)


def exported_at(data_dir: str, server_tz) -> pd.Timestamp | None:
    """書き出した時刻（UTC）。CfdExportBars が account_info.txt に書く exported=（サーバー時刻）。"""
    v = load_account(Path(data_dir) / ACCOUNT_FILE).get("exported")
    if not v:
        return None
    try:
        t = pd.Timestamp(datetime.strptime(v, "%Y.%m.%d %H:%M"))
    except ValueError:
        return None
    return server_to_utc(pd.DatetimeIndex([t]), server_tz)[0]


def excused_held(days: pd.DataFrame, recheck_from: date | None) -> list[bool]:
    """保有数の差を数えない足か（条件 4）。抜けた足の後、次に一致する足まで。

    EA は止まっていた間の足を処理しないので、その間の売買が Python とずれる。確かめ直し（recheck_from）では、
    数え始める日より前から続いているずれ（直す前の失敗で建玉がずれたまま）も、次に一致する足まで数えない。
    days は開始日からの表（抜けが数え始める日より前でも効くように）。
    """
    out, flag, entered = [], False, recheck_from is None
    for d, st in zip(days["bar_date"], days["status"]):
        if not entered and d >= recheck_from:
            flag, entered = True, True
        out.append(flag)
        if st == "抜け":
            flag = True
        elif st == "一致":
            flag = False
    return out


def fmt(v, nd=2) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{v:+.{nd}f}" if nd else f"{int(v)}"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--final", required=True, help="構成の final.json（./cfd demo-check なら自動）")
    p.add_argument("--start", required=True, help="その口座で EA が判断した最初の足の日付（例: 2026-10-06）")
    p.add_argument("--count-from", help="条件を数え始める日（直した後の確かめ直し。省略時は --start）")
    p.add_argument("--end", help="終わりの日（省略時はデータの最後まで）")
    p.add_argument("--equity", type=float, help="口座の開始時の資産（円）。./cfd replay と同じ")
    p.add_argument("--risk-scale", type=float, default=1.0, help="EA の ea_risk_scale と同じ値")
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--data", help="データフォルダ（設定より優先）")
    p.add_argument("--logs", nargs="*", help="EA のログ（フォルダか .log ファイル）。省略時は MT5 の MQL5/Logs")
    p.add_argument("--terminal", type=int, help="MT5 が複数あるときの番号（./cfd where の順）")
    p.add_argument("--mql5", help="MT5 の MQL5 フォルダを直接指定")
    p.add_argument("--out", default="output/demo_check")
    args = p.parse_args()

    out_lines: list[str] = []

    def say(s: str = "") -> None:
        print(s)
        out_lines.append(s)

    files, where = log_files(args)
    if not files:
        raise SystemExit(f"EA のログが見つからない: {where}（MT5 → ファイル → データフォルダを開く → MQL5/Logs）")
    log = read_logs(files)

    cfg = load_train_config(args.config)
    if args.data:
        cfg.data.dir = args.data
    if args.equity:
        cfg.account.equity = args.equity
    final = json.loads(Path(args.final).read_text(encoding="utf-8"))
    ds = load_dataset(cfg.data)
    res, picks, _, _ = replay(cfg, final, args.start, args.end, args.risk_scale, ds=ds)
    trades, rej = res["trades"], res.get("rejections")
    start = pd.Timestamp(clean_date(args.start)).date()
    count_from = max(start, pd.Timestamp(clean_date(args.count_from)).date()) if args.count_from else start
    end = pd.Timestamp(clean_date(args.end)).date() if args.end else None
    recheck = count_from > start
    smap = cfg.data.symbol_map
    tz = cfg.data.server_tz
    if recheck:   # 足の無い日（日曜など）に直したら、その前の足から数える（土曜の足は月曜の朝、直した後に判断される）
        bar_days = {d for pk in picks for d in server_dates(ds.signal_frame(pk.symbol, pk.timeframe).index, tz)}
        count_from = max((d for d in bar_days if start <= d <= count_from), default=count_from)
    exported = exported_at(cfg.data.dir, tz)

    # 数える範囲: 足は count_from〜end。他の行（要確認など）は、その足を判断した日まで（end の足は翌日以降に判断される）
    j, ev = log.judgments, log.events
    j = j.assign(key=[chart_key(c, smap) for c in j["chart"]])
    ev = ev.assign(key=[chart_key(c, smap) if c else None for c in ev["chart"]])
    log_end = log.last_date
    if end is not None:
        judged = j.loc[j["bar_date"] <= end, "log_date"].dropna()
        cap = max(judged.max(), end + timedelta(days=1)) if len(judged) else end + timedelta(days=1)
        log_end = min(log.last_date, cap) if log.last_date else cap

    # 他の行は、数える最初の足を EA が判断した日から（その日の 0 時より前の行は、前の足・直す前・前の口座のもの）
    first_judged = j.loc[j["bar_date"] >= count_from, "log_date"].dropna()
    line_from = first_judged.min() if len(first_judged) else count_from + timedelta(days=1)

    def in_range(d) -> bool:   # 書いた日が分からない行は数える（見落とさないため）
        return d is None or pd.isna(d) or (d >= line_from and (log_end is None or d <= log_end))

    ev = ev[ev["log_date"].map(in_range).astype(bool)] if len(ev) else ev
    unattributed = [d for d in log.unattributed if in_range(d)]

    say("# デモの照らし合わせ（EA のログ ⇔ Python）")
    say()
    say(f"- 実行: {datetime.now():%Y-%m-%d %H:%M}、構成: {args.final}")
    say(f"- 開始日（Python の再現の始まり）: {start}" + (f"、条件を数え始める日: {count_from}" if recheck else "")
        + (f"、終わりの日: {end}" if end else "") + f"、資金 {cfg.account.equity:,.0f} 円、リスク倍率 {args.risk_scale}")
    say(f"- EA のログ: {where}（{len(files)} 個、{log.first_date}〜{log.last_date}）")
    say(f"- 書き出した時刻: {exported.tz_convert('Asia/Tokyo'):%Y-%m-%d %H:%M}（日本時間。これより後に終わる足は比べない）"
        if exported is not None else "- 書き出した時刻が分からない（account_info.txt が無い）。データの最後の足は、"
        "書き出したときに途中だったかもしれないので比べない")
    if unattributed:
        say(f"- ⚠ 発信元（CfdCommodityEA (銘柄,時間足)）が読めない判断の行が {len(unattributed)} 行。実物の数行を送ってください")
    if log.unparsed:
        say(f"- ⚠ 形が読めない判断の行がある（例: `{log.unparsed[0][:160]}`）。実物の数行を送ってください")
    if log.undated:
        say(f"- ⚠ 書いた日が分からないログ: {', '.join(log.undated)}（名前が YYYYMMDD.log のファイルのまま渡す）。"
            "抜けと期間を数えられない")
    first_bar = j["bar_date"].min() if len(j) else None
    if first_bar is not None:
        say(f"- ログで EA が判断した最初の足: {first_bar}"
            + ("（--start より前。EA を貼った直後に判断した足なら、--start をこの日にする）"
               if first_bar < start and not j["log_date"].dropna().lt(start).any() else ""))
    unknown = sorted(set(j.loc[j["key"].isna(), "chart"]))
    if unknown:
        say(f"- ⚠ 銘柄キーに変換できないチャート: {', '.join(unknown)}（config の symbol_map に追加）")

    all_days, summary, verdicts = [], [], []
    for pk, sl in zip(picks, final["sleeves"]):
        sym, tf = pk.symbol, pk.timeframe
        say()
        say(f"## {sym}（{sl['strategy']} @{tf}）")
        ea = j[j["key"] == sym]
        if not ea.empty and set(ea["tf"]) - {tf}:
            say(f"- ⚠ EA のチャートの時間足が {sorted(set(ea['tf']))}（{tf} のはず）")
            ea = ea[ea["tf"] == tf]
        sig = signals_for_sleeve(ds, sl)
        bar = pd.Timedelta(TIMEFRAMES[tf])
        py = python_daily(sig, tz, bar, trades, sym)
        # 書き出したときに途中だった足は、EA が見た確定の値と違うので比べない
        # （exported= は書き出しの終わりに書かれるので、その直前に終わった足も念のため次の書き出しに回す）
        done = py[py.index + bar <= exported - EXPORT_MARGIN] if exported is not None else py.iloc[:-1]
        upto = done["bar_date"].iloc[-1] if len(done) else date.min
        days = compare_symbol(ea, py, start, log.last_date if not log.undated else None, upto)
        if end is not None:
            days = days[days["bar_date"] <= end]
        days = days.assign(after_gap=excused_held(days, count_from if recheck else None))
        days = days[days["bar_date"] >= count_from].reset_index(drop=True)
        days.insert(0, "symbol", sym)
        all_days.append(days)
        if ea.empty:
            say("- EA の判断の行が 1 つも無い（このチャートに EA を貼っていない・ログの場所が違う・symbol_map に無い）")
            verdicts.append("記録なし")
            summary.append((sym, 0, 0, int((days["status"] == "抜け").sum()), 0, 0))
            continue
        n = days["status"].value_counts()
        ok, bad, gap, wait = (int(n.get(k, 0)) for k in ("一致", "差あり", "抜け", "判断待ち"))
        nopy = int(n.get("Python のデータに無い", 0))
        # 保有数だけの差で、抜けの後（確かめ直しでは数え始める前から続くずれ）のものは条件 4 で数えない
        excused = (days["status"] == "差あり") & (days["diffs"] == "保有数") & days["after_gap"].astype(bool)
        n_excused = int(excused.sum())
        bad -= n_excused
        say(f"- 一致 {ok} 日、差あり {bad} 日、EA の記録の抜け {gap} 日、判断待ち {wait} 日"
            + (f"、数えない保有数の差 {n_excused} 日（抜けの後{'・確かめ直しの前から続くずれ' if recheck else ''}）"
               if n_excused else "")
            + (f"、Python のデータに無い {nopy} 日（書き出しより後の足。次の書き出しで比べる）" if nopy else ""))
        steps = days["ea_held"].dropna().diff()   # EA が判断した日どうしの保有数の増減（抜けた日は飛ばす）
        entries, exits = int((steps > 0).sum()), int((steps < 0).sum())
        py_trades = trades[(trades["symbol"] == sym)] if not trades.empty else trades
        say(f"- EA の保有数の増減: 増えた {entries} 回・減った {exits} 回 / Python の再現の取引: {len(py_trades)} 回")
        if gap:
            say(f"- 抜けた日: {', '.join(str(d) for d in days.loc[days['status'] == '抜け', 'bar_date'])}"
                "（Mac のスリープ・MT5 の停止・EA が外れたなど）")
        twice_days = days.loc[days["twice"].fillna(False).astype(bool), "bar_date"]
        if len(twice_days):
            say(f"- ⚠ 同じ足を 2 回判断した日: {', '.join(str(d) for d in twice_days)}"
                "（同じ銘柄のチャートに EA が 2 つ・再起動で記録が消えた）")
        if bad or n_excused:
            say()
            say("| 足の日付 | 差 | 終値 EA / Py | シグナル EA / Py | 行き過ぎ EA / Py | 保有 EA / Py |")
            say("|---|---|---|---|---|---|")
            rows = days[days["status"] == "差あり"].assign(ex=excused[days["status"] == "差あり"])
            rows = pd.concat([rows[~rows["ex"]], rows[rows["ex"]]])   # 数える差を先に
            for _, r in rows.head(20).iterrows():
                ex, nd = bool(r["ex"]), int(r["close_dec"])
                say(f"| {r['bar_date']} | {r['diffs']}{'（数えない）' if ex else ''} | "
                    f"{r['ea_close']:.{nd}f} / {r['py_close']:.{nd}f} | "
                    f"{SIG.get(r['ea_signal'], '?')} / {SIG.get(r['py_signal'], '?')} | "
                    f"{fmt(r['ea_ext'])} / {fmt(r['py_ext'])} | {fmt(r['ea_held'], 0)} / {fmt(r['py_held'], 0)} |")
            if len(rows) > 20:
                say(f"\n（ほか {len(rows) - 20} 日は days.csv）")
        sym_rej = None
        if rej is not None and not rej.empty:
            sym_rej = rej[rej["sleeve"].astype(str).str.split(":").str[0] == sym]
        why = reasons(sym, days, ev[ev["key"] == sym], shifted_close_match(ea, py), sym_rej)
        if why:
            say()
            say("差の理由の候補:")
            for w in why:
                say(f"- {w}")
        verdicts.append("差あり" if bad else ("一致" if ok or n_excused else ("抜けのみ" if gap else "照らし合わせた日なし")))
        summary.append((sym, ok, bad, gap, wait, entries))

    say()
    say(f"## EA の他の行（要確認・注意。{line_from} 以降に書かれた行）")
    serious = ev[ev["severity"] == SERIOUS_SEVERITY]
    notes = ev[ev["severity"] == "注意"]
    if serious.empty and notes.empty:
        say("- なし")
    for _, e in pd.concat([serious, notes]).head(40).iterrows():
        say(f"- [{e['severity']}] {e['log_date']} {e['time']} {e['chart'] or '?'}: {e['kind']} — `{e['text'][:140]}`")
    info = ev[ev["severity"] == "情報"]["kind"].value_counts()
    if len(info):
        say("- 情報: " + "、".join(f"{k} {v} 回" for k, v in info.items()))
    if rej is not None and not rej.empty:
        when = server_dates(pd.DatetimeIndex(rej["time"]), tz)
        keep = [d >= count_from and (end is None or d <= end + timedelta(days=1)) for d in when]
        r = rej[(rej["reason"] != "halt_triggered").to_numpy() & pd.Series(keep).to_numpy()]
        if len(r):
            say("- Python 側の見送り: " + "、".join(
                f"{t:%Y-%m-%d} {str(s).split(':')[0]} {x}" for t, s, x in zip(r["time"], r["sleeve"], r["reason"])))

    days_all = pd.concat(all_days, ignore_index=True) if all_days else pd.DataFrame()
    twice_n = int(days_all["twice"].fillna(False).astype(bool).sum()) if len(days_all) else 0
    compared = days_all[days_all["status"].isin(["一致", "差あり"])] if len(days_all) else days_all
    nopy_all = days_all.loc[days_all["status"] == "Python のデータに無い", "bar_date"].nunique() if len(days_all) else 0
    gap_all = days_all.loc[days_all["status"] == "抜け", "bar_date"].nunique() if len(days_all) else 0
    say()
    say("## 結果")
    extra = [x for x in (f"要確認の行 {len(serious)} 件" if len(serious) else "",
                         f"同じ足の二重判断 {twice_n} 件" if twice_n else "",
                         f"発信元が読めない判断の行 {len(unattributed)} 件" if unattributed else "") if x]
    if {"差あり", "記録なし", "抜けのみ"} & set(verdicts) or extra:
        word = "差あり"
    elif compared.empty:
        word = "まだ比べられない"   # 最初の足がまだ判断されていない・書き出しが古い
    else:
        word = "一致"
    say(f"**{word}**（" + "、".join([f"{s[0]} {v}" for s, v in zip(summary, verdicts)] + extra) + "）"
        + (f"。EA の記録の抜け {gap_all} 日（条件 2）" if gap_all else "")
        + (f"。書き出しより後の {nopy_all} 日分は、次の書き出しで比べる" if nopy_all else ""))

    # 本番に進む条件のうち、ログで数えられるもの（docs/operations.md 12 章）
    say()
    say("## 本番に進む条件（docs/operations.md 12 章。数えられる分）")
    if days_all.empty or compared.empty:
        say("- 照らし合わせた日が無い（最初の足をまだ EA が判断していない・書き出しが古い・ログの場所が違う）")
    else:
        min_days, min_cmp = (RECHECK_CALENDAR_DAYS, RECHECK_COMPARED_DAYS) if recheck else \
            (MIN_CALENDAR_DAYS, MIN_COMPARED_DAYS)
        cdays = sorted(set(compared["bar_date"]))
        gaps = days_all[days_all["status"] == "抜け"]
        share = len(gaps) / max(1, len(gaps) + len(compared))
        last_days = sorted(set(days_all.loc[days_all["status"].isin(["一致", "差あり", "抜け"]), "bar_date"]))[
            -LAST_DAYS_NO_GAP:]
        span = (log_end - count_from).days if log_end else 0
        dated = not log.undated
        held_bad = compared[compared["diffs"].fillna("").str.contains("保有数")]
        held_bad_unexplained = held_bad[~held_bad["after_gap"].astype(bool)]
        other_bad = compared[compared["diffs"].fillna("").str.contains("終値|シグナル|行き過ぎ")]
        entries = sum(s[5] for s in summary)
        checks = [
            (f"期間: {count_from} から {min_days} 日以上・照らし合わせた日 {min_cmp} 日以上",
             dated and span >= min_days and len(cdays) >= min_cmp,
             f"{span} 日・{len(cdays)} 日" if dated else "書いた日が分からないログがあり数えられない"),
            (f"抜け: {MAX_MISSING_SHARE:.0%} 以下（全銘柄）、かつ最後の {LAST_DAYS_NO_GAP} 日に無い",
             dated and share <= MAX_MISSING_SHARE and not (set(gaps["bar_date"]) & set(last_days)),
             f"{share:.1%}" if dated else "書いた日が分からないログがあり数えられない"),
            ("終値・シグナル・行き過ぎ: すべて一致", other_bad.empty, f"差 {len(other_bad)} 件"),
            ("保有数: 抜けの直後を除いてすべて一致", held_bad_unexplained.empty,
             f"差 {len(held_bad)} 件（抜けの直後でないもの {len(held_bad_unexplained)} 件）"),
            ("要確認の行・同じ足の二重判断: 0 件", serious.empty and not twice_n,
             f"要確認 {len(serious)} 件・二重判断 {twice_n} 件"),
            (f"取引: EA のエントリーが全銘柄で {MIN_ENTRIES} 回以上", entries >= MIN_ENTRIES, f"{entries} 回"),
        ]
        for name, ok, val in checks:
            say(f"- {'✓' if ok else '✗'} {name}（{val}）")
        say("- 目で確認: 口座履歴と ./cfd replay の取引（向き・決済理由・価格の差がスプレッド程度）／"
            "再起動・週末と指標前の停止・通知（各 1 回）")
        n_ok = sum(ok for _, ok, _ in checks)
        say(f"- 数えられる条件: {n_ok}/{len(checks)} を満たす")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    if not days_all.empty:
        days_all.to_csv(out / "days.csv", index=False)
    if not ev.empty:
        ev.to_csv(out / "ea_lines.csv", index=False)
    print(f"\n保存先: {out}/report.md")



if __name__ == "__main__":
    main()
