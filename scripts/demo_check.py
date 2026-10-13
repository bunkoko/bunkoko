"""デモ口座（本番）で動く EA のログを、同じデータ・同じ構成の Python と 1 日ずつ照らし合わせる。

毎週の確認（docs/operations.md 12 章）で、MT5 から書き出して ./cfd data の後に:

    ./cfd demo-check --start 2026-10-06 --equity 10000000

- EA のログ: MT5 のデータフォルダの MQL5/Logs/<YYYYMMDD>.log（自動で探す。--logs でフォルダかファイルを指定もできる。
  VPS に移した後は、VPS のジャーナルを保存したファイルを --logs に渡す）
- 構成: ./cfd preset で最後に置いたもの（./cfd replay と同じ）
- 結果: 画面と output/demo_check/<日時>/report.md・days.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from compare_all import chart_key  # noqa: E402
from compare_signals import signals_for_sleeve  # noqa: E402
from replay import clean_date, replay  # noqa: E402

from cfdbot.demo_check import compare_symbol, python_daily, read_logs, reasons, shifted_close_match  # noqa: E402
from cfdbot.mt5files import choose_terminal, find_terminals  # noqa: E402
from cfdbot.train.config import TIMEFRAMES, load_train_config  # noqa: E402
from cfdbot.train.dataset import load_dataset  # noqa: E402

SIG = {1: "買い", -1: "売り", 0: "なし"}
# 本番に進む条件（docs/operations.md 12 章に事前登録。変えるときは結果を見る前に docs と一緒に）
MIN_CALENDAR_DAYS = 28
MIN_COMPARED_DAYS = 20
MAX_MISSING_SHARE = 0.10
LAST_DAYS_NO_GAP = 5
MIN_ENTRIES = 2
SERIOUS_SEVERITY = "要確認"


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
    return sorted(d.glob("*.log")) if d.is_dir() else [], str(d)


def fmt(v, nd=2) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{v:+.{nd}f}" if nd else f"{int(v)}"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--final", required=True, help="構成の final.json（./cfd demo-check なら自動）")
    p.add_argument("--start", required=True, help="EA を動かし始めた日（例: 2026-10-06）")
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
    res, picks, t0, t1 = replay(cfg, final, args.start, args.end, args.risk_scale, ds=ds)
    trades, rej = res["trades"], res.get("rejections")
    start = pd.Timestamp(clean_date(args.start)).date()
    smap = cfg.data.symbol_map
    tz = cfg.data.server_tz

    say("# デモの照らし合わせ（EA のログ ⇔ Python）")
    say()
    say(f"- 実行: {datetime.now():%Y-%m-%d %H:%M}、構成: {args.final}、開始日: {start}、資金 {cfg.account.equity:,.0f} 円、"
        f"リスク倍率 {args.risk_scale}")
    say(f"- EA のログ: {where}（{len(files)} 個、{log.first_date}〜{log.last_date}）")
    if log.unattributed:
        say(f"- ⚠ 発信元（CfdCommodityEA (銘柄,時間足)）が読めない判断の行が {log.unattributed} 行。実物の数行を送ってください")
    if log.unparsed:
        say(f"- ⚠ 形が読めない判断の行がある（例: `{log.unparsed[0][:160]}`）。実物の数行を送ってください")

    j, ev = log.judgments, log.events
    j = j.assign(key=[chart_key(c, smap) for c in j["chart"]])
    ev = ev.assign(key=[chart_key(c, smap) if c else None for c in ev["chart"]])
    unknown = sorted(set(j.loc[j["key"].isna(), "chart"]))
    if unknown:
        say(f"- ⚠ 銘柄キーに変換できないチャート: {', '.join(unknown)}（config の symbol_map に追加）")

    all_days, summary, verdicts = [], [], []
    for pk, sl in zip(picks, final["sleeves"]):
        sym, tf = pk.symbol, pk.timeframe
        say()
        say(f"## {sym}（{sl['strategy']} @{tf}）")
        ea = j[(j["key"] == sym)]
        if not ea.empty and set(ea["tf"]) - {tf}:
            say(f"- ⚠ EA のチャートの時間足が {sorted(set(ea['tf']))}（{tf} のはず）")
            ea = ea[ea["tf"] == tf]
        ea = ea[ea["bar_date"] >= start - timedelta(days=7)]   # 貼った日に、直前の足を 1 回判断する
        if ea.empty:
            say("- EA の判断の行が 1 つも無い（このチャートに EA を貼っていない・ログの場所が違う）")
            verdicts.append("記録なし")
            summary.append((sym, 0, 0, 0, 0, 0))
            continue
        sig = signals_for_sleeve(ds, sl)
        bar = pd.Timedelta(TIMEFRAMES[tf])
        py = python_daily(sig, tz, bar, trades, sym)
        py = py[py["bar_date"] >= start - timedelta(days=7)]
        if args.end:
            py = py[py["bar_date"] <= pd.Timestamp(clean_date(args.end)).date()]
        days = compare_symbol(ea, py, start, log.last_date)
        days.insert(0, "symbol", sym)
        all_days.append(days)
        n = days["status"].value_counts()
        ok, bad, gap, wait = (int(n.get(k, 0)) for k in ("一致", "差あり", "抜け", "判断待ち"))
        nopy = int(n.get("Python のデータに無い", 0))
        say(f"- 一致 {ok} 日、差あり {bad} 日、EA の記録の抜け {gap} 日、判断待ち {wait} 日"
            + (f"、Python のデータに無い {nopy} 日（書き出しが古い）" if nopy else ""))
        steps = days["ea_held"].dropna().diff()   # EA が判断した日どうしの保有数の増減（抜けた日は飛ばす）
        entries, exits = int((steps > 0).sum()), int((steps < 0).sum())
        say(f"- EA の保有数の増減: 増えた {entries} 回・減った {exits} 回 / Python の再現の取引: "
            f"{int((trades['symbol'] == sym).sum()) if not trades.empty else 0} 回")
        if gap:
            say(f"- 抜けた日: {', '.join(str(d) for d in days.loc[days['status'] == '抜け', 'bar_date'])}"
                "（Mac のスリープ・MT5 の停止など）")
        if bad:
            say()
            say("| 足の日付 | 差 | 終値 EA / Py | シグナル EA / Py | 行き過ぎ EA / Py | 保有 EA / Py |")
            say("|---|---|---|---|---|---|")
            for _, r in days[days["status"] == "差あり"].head(20).iterrows():
                say(f"| {r['bar_date']} | {r['diffs']} | {r['ea_close']} / {r['py_close']} | "
                    f"{SIG.get(r['ea_signal'], '?')} / {SIG.get(r['py_signal'], '?')} | {fmt(r['ea_ext'])} / {fmt(r['py_ext'])} | "
                    f"{fmt(r['ea_held'], 0)} / {fmt(r['py_held'], 0)} |")
        sym_rej = None
        if rej is not None and not rej.empty:
            sym_rej = rej[rej["sleeve"].astype(str).str.split(":").str[0] == sym]
        why = reasons(sym, days, ev[ev["key"] == sym], shifted_close_match(ea, py), sym_rej)
        if why:
            say()
            say("差の理由の候補:")
            for w in why:
                say(f"- {w}")
        verdicts.append("差あり" if bad else "一致")
        summary.append((sym, ok, bad, gap, wait, entries))

    say()
    say("## EA の他の行（要確認・注意）")
    serious = ev[ev["severity"] == SERIOUS_SEVERITY]
    notes = ev[ev["severity"] == "注意"]
    if serious.empty and notes.empty:
        say("- なし")
    for _, e in pd.concat([serious, notes]).head(40).iterrows():
        say(f"- [{e['severity']}] {e['file_date']} {e['time']} {e['chart'] or '?'}: {e['kind']} — `{e['text'][:140]}`")
    info = ev[ev["severity"] == "情報"]["kind"].value_counts()
    if len(info):
        say("- 情報: " + "、".join(f"{k} {v} 回" for k, v in info.items()))
    if rej is not None and not rej.empty:
        r = rej[rej["reason"] != "halt_triggered"]
        if len(r):
            say("- Python 側の見送り: " + "、".join(
                f"{t:%Y-%m-%d} {str(s).split(':')[0]} {x}" for t, s, x in zip(r["time"], r["sleeve"], r["reason"])))

    days_all = pd.concat(all_days, ignore_index=True) if all_days else pd.DataFrame()
    twice = bool(len(days_all) and days_all["twice"].fillna(False).astype(bool).any())
    total_bad = sum(s[2] for s in summary)
    say()
    say("## 結果")
    final_word = "一致" if total_bad == 0 and serious.empty and not twice and "記録なし" not in verdicts \
        and not log.unattributed else "差あり"
    say(f"**{final_word}**（" + "、".join(f"{s[0]} {v}" for s, v in zip(summary, verdicts)) + "）")

    # 本番に進む条件のうち、ログで数えられるもの（docs/operations.md 12 章）
    say()
    say("## 本番に進む条件（docs/operations.md 12 章。数えられる分）")
    if days_all.empty:
        say("- 照らし合わせた日が無い")
    else:
        compared = days_all[days_all["status"].isin(["一致", "差あり"])]
        cdays = sorted(set(compared["bar_date"]))
        gaps = days_all[days_all["status"] == "抜け"]
        share = len(gaps) / max(1, len(gaps) + len(compared))
        last_days = sorted(set(days_all.loc[days_all["status"].isin(["一致", "差あり", "抜け"]), "bar_date"]))[
            -LAST_DAYS_NO_GAP:]
        span = (log.last_date - start).days if log.last_date else 0
        held_bad = compared[compared["diffs"].fillna("").str.contains("保有数")]
        # 抜けた日の後は、次に一致する日までの保有数の差を数えない（EA は止まっていた間の足を処理しないため）
        held_bad_unexplained = []
        for sym, g in days_all.groupby("symbol"):
            after_gap = False
            for _, r in g.sort_values("bar_date").iterrows():
                if r["status"] == "抜け":
                    after_gap = True
                elif r["status"] == "一致":
                    after_gap = False
                elif r["status"] == "差あり" and "保有数" in str(r["diffs"]) and not after_gap:
                    held_bad_unexplained.append((sym, r["bar_date"]))
        other_bad = compared[compared["diffs"].fillna("").str.contains("終値|シグナル|行き過ぎ")]
        entries = sum(s[5] for s in summary)
        checks = [
            (f"期間: 開始から {MIN_CALENDAR_DAYS} 日以上・照らし合わせた日 {MIN_COMPARED_DAYS} 日以上",
             span >= MIN_CALENDAR_DAYS and len(cdays) >= MIN_COMPARED_DAYS, f"{span} 日・{len(cdays)} 日"),
            (f"抜け: {MAX_MISSING_SHARE:.0%} 以下、かつ最後の {LAST_DAYS_NO_GAP} 日に無い",
             share <= MAX_MISSING_SHARE and not (set(gaps["bar_date"]) & set(last_days)), f"{share:.1%}"),
            ("終値・シグナル・行き過ぎ: すべて一致", other_bad.empty, f"差 {len(other_bad)} 件"),
            ("保有数: 抜けの直後を除いてすべて一致", not held_bad_unexplained,
             f"差 {len(held_bad)} 件（抜けの直後でないもの {len(held_bad_unexplained)} 件）"),
            ("要確認の行・同じ足の二重判断: 0 件", serious.empty and not twice, f"{len(serious)} 件"),
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
