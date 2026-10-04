"""MT5（Mac 版は Wine の中）とのファイルのやり取り。Finder で深いフォルダを探さなくて済むようにする。

    python scripts/mt5_files.py where                      # MT5 のフォルダを表示
    python scripts/mt5_files.py install                    # EA とスクリプトだけ置く（最初のコンパイル用）
    python scripts/mt5_files.py install --run output/train/<日時>   # EA・プリセット等を MT5 に置く
    python scripts/mt5_files.py install --run ... --risk-scale 0.5  # 本番開始時（1回の損失を半分に）
    python scripts/mt5_files.py fetch-data                 # CfdExportBars の書き出しを data/ に取り込む
    python scripts/mt5_files.py fetch-logs                 # テスターのシグナル記録を output/compare/ に取り込む

install が置くもの:
- MQL5/Experts/CfdCommodityEA.mq5、MQL5/Scripts/CfdExportBars.mq5（MetaEditor でコンパイルする）
- MQL5/Presets/cfdbot_<銘柄>_<戦略>_<時間足>.set … チャートの EA に「読み込み」で入れる。
  magic・リスク倍率・指標の日時（ea_events_utc_*）まで入っているので、手で直す項目は無い。
  ファイルを使わないので MQL5 VPS に移してもそのまま動く
- MQL5/Profiles/Tester/ に同じ名前の .set（ストラテジーテスター用。シグナル記録がオン）
- 共通フォルダに cfdbot_events.csv（[data] events を設定している場合）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot.events import load_events_csv  # noqa: E402
from cfdbot.mt5files import (Terminal, build_presets, choose_terminal, fetch, find_terminals,  # noqa: E402
                             install)
from cfdbot.train.config import load_train_config  # noqa: E402
from cfdbot.train.dataset import _parse_name  # noqa: E402


def _terminal(args) -> Terminal:
    return choose_terminal(find_terminals(), args.terminal, args.mql5, args.common)


def _pad(text: str, width: int) -> str:
    """全角は 2 文字分として、表示幅 width までスペースで埋める。"""
    import unicodedata

    shown = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(1, width - shown)


def _mt5_name(key: str, symbol_map: dict[str, str], data_dir: Path) -> str:
    """チャートに使う MT5 の銘柄名。書き出したファイル名（XAGUSD.ps01_M5.csv など）があればその名前。"""
    if data_dir.is_dir():
        for p in sorted(data_dir.glob("*.csv")):
            if _parse_name(p, symbol_map)[0] == key:
                return p.name.split("_")[0]
    names = [k for k, v in symbol_map.items() if v == key and k != key]
    return (names or [key])[0]


def cmd_where(args) -> None:
    found = find_terminals()
    if not found:
        print("MT5 が見つからない。MT5 を一度起動してから再実行する")
        return
    for i, t in enumerate(found):
        mark = "（既定）" if i == 0 else ""
        print(f"[{i}]{mark} データフォルダ: {t.data_dir}")
        print(f"     共通フォルダ  : {t.common or '見つからない（--common で指定）'}")
    if len(found) > 1:
        print("\n既定は最近使ったもの。別のものは --terminal 番号 で選ぶ"
              "（MT5 → ファイル →「データフォルダを開く」で開く場所と同じものを選ぶ）")


def _short(path: str, term: Terminal) -> str:
    for base, label in ((term.data_dir, "データフォルダ"), (term.common, "共通フォルダ")):
        if base is not None and Path(path).is_relative_to(base):
            return f"{label}/{Path(path).relative_to(base).as_posix()}"
    return path


def cmd_install(args) -> None:
    cfg = load_train_config(args.config)
    events_path = Path(args.events or cfg.data.events) if (args.events or cfg.data.events) else None
    events = load_events_csv(events_path) if events_path else []
    now = pd.Timestamp.now(tz="UTC")
    peak_since = args.peak_since if args.peak_since is not None else cfg.account.peak_since
    presets = []
    if args.run:
        run = Path(args.run)
        run = run.parent if run.is_file() else run
        ea_dir = run / "ea"
        if not ea_dir.is_dir():
            raise SystemExit(f"{ea_dir} が無い（--run には学習結果のフォルダ output/train/<日時> を指定）")
        presets = build_presets(ea_dir, events, now - pd.Timedelta(days=1), now + pd.Timedelta(days=args.event_days),
                                args.magic_base, args.risk_scale, peak_since, cfg.data.server_tz)
        if not presets:
            raise SystemExit(f"{ea_dir} に cfdbot_*.set が無い（学習で全銘柄見送り？）")
    term = _terminal(args)
    placed = install(term, presets, ROOT, events_path)
    print(f"MT5 のデータフォルダ: {term.data_dir}")
    for p in placed:
        print(f"  置いた: {_short(p, term)}")
    if not presets:
        print("\nEA とスクリプトだけ置いた（学習後に --run を付けて実行するとプリセットも置く）。"
              "次: MetaEditor で CfdCommodityEA と CfdExportBars をコンパイル（F7）")
        return
    print("\nチャートと EA の対応（気配値表示の銘柄でチャートを開き、時間足を合わせて EA を貼る）:")
    charts = {p.name: _mt5_name(p.symbol, cfg.data.symbol_map, Path(cfg.data.dir)) for p in presets}
    w_name = max(len(n) for n in charts) + 6
    w_chart = max(len(c) for c in charts.values()) + 2
    print("  " + _pad("プリセット", w_name) + _pad("チャート", w_chart) + _pad("時間足", 6) + _pad("magic", 9) + "指標")
    for p in presets:
        cut = f"（〜{p.events_cut:%Y-%m-%d} まで。以降は次の更新で）" if p.events_cut is not None else ""
        print(f"  {p.name + '.set':<{w_name}}{charts[p.name]:<{w_chart}}{p.timeframe:<6}{p.params['ea_magic']:<9}"
              f"{p.events} 件{cut}")
    print(f"\nリスク倍率 ea_risk_scale = {args.risk_scale:g}（デモは 1、本番の最初は 0.25〜0.5）")
    if peak_since:
        print(f"DD の基準は {peak_since} 以降の最高資産から測る（[account] peak_since）")
    if not events_path:
        print("⚠ 指標カレンダーが未設定（config/train.toml の [data] events）。原油の EIA・API の定例だけで止まる")
    elif events:
        last = max(e.time for e in events)
        if last < now + pd.Timedelta(days=35):
            print(f"⚠ {events_path} の最後の予定が {last:%Y-%m-%d}。翌月以降の FOMC・CPI・雇用統計を追加する")
    print("\n次: テスターかチャートの EA の「パラメータの入力」→「読み込み」でプリセットを選ぶ（docs/operations.md 手順 9・11）。"
          "EA を更新したときだけ MetaEditor でコンパイルし直す（F7）")


def cmd_fetch_data(args) -> None:
    term = _terminal(args)
    if term.common is None:
        raise SystemExit("共通フォルダが見つからない（--common で指定）")
    cfg = load_train_config(args.config)
    dest = Path(args.dest or cfg.data.dir)
    got = fetch(term.common / args.folder, "*.csv", dest)
    if not got:
        raise SystemExit(f"{term.common / args.folder} に CSV が無い。MT5 で CfdExportBars を実行したか確認する")
    for p in got:
        print(f"  {p}（{p.stat().st_size / 1e6:.1f} MB）")
    meta = fetch(term.common / args.folder, "*.txt", dest)   # 銘柄仕様・口座情報
    for p in meta:
        print(f"  {p}")
    print(f"{len(got)} ファイルを {dest}/ に取り込んだ")


def cmd_fetch_logs(args) -> None:
    term = _terminal(args)
    if term.common is None:
        raise SystemExit("共通フォルダが見つからない（--common で指定）")
    got = fetch(term.common, "cfdbot_signals_*.csv", Path(args.dest))
    if not got:
        raise SystemExit("シグナル記録が無い。テスターで ea_log_signals=true にして実行したか確認する")
    for p in got:
        print(f"  {p}")
    print("次: python scripts/compare_signals.py --final output/train/<日時>/final.json --symbol <銘柄> "
          f"--ea-log {got[0]}")


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--terminal", type=int, help="MT5 が複数あるときの番号（where で表示）")
    common.add_argument("--mql5", help="MT5 の MQL5 フォルダを直接指定")
    common.add_argument("--common", help="共通フォルダ（.../MetaQuotes/Terminal/Common/Files）を直接指定")
    common.add_argument("--config", default="config/train.toml")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("where", parents=[common], help="MT5 のフォルダを表示")
    s = sub.add_parser("install", parents=[common], help="EA・プリセット・指標カレンダーを MT5 に置く")
    s.add_argument("--run", help="学習結果のフォルダ（output/train/<日時>）か final.json。省略時は EA とスクリプトだけ")
    s.add_argument("--risk-scale", type=float, default=1.0, help="ea_risk_scale（本番の最初は 0.25〜0.5）")
    s.add_argument("--magic-base", type=int, default=2609000, help="magic の上の桁（下 3 桁は銘柄ごとに固定）")
    s.add_argument("--peak-since", help="DD の基準を測り始める日（省略時は config の [account] peak_since）")
    s.add_argument("--event-days", type=int, default=120, help="何日先までの指標を EA に入れるか")
    s.add_argument("--events", help="指標カレンダー CSV（省略時は config の [data] events）")
    s = sub.add_parser("fetch-data", parents=[common], help="CfdExportBars が書き出したバーを data/ に取り込む")
    s.add_argument("--folder", default="cfdbot_data", help="共通フォルダの下のフォルダ名（スクリプトの out_folder）")
    s.add_argument("--dest", help="取り込み先（省略時は config の [data] dir）")
    s = sub.add_parser("fetch-logs", parents=[common], help="テスターのシグナル記録を取り込む")
    s.add_argument("--dest", default="output/compare")
    args = p.parse_args()
    {"where": cmd_where, "install": cmd_install, "fetch-data": cmd_fetch_data, "fetch-logs": cmd_fetch_logs}[args.cmd](args)


if __name__ == "__main__":
    main()
