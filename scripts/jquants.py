"""J-Quants（JPX 総研の公式 API）の確認と取得（./cfd jquants）。docs/survivorship.md の段階 1 と、段階 2 の取得の道具（8.6）。

    python scripts/jquants.py key            # API キーを保存する（入力を求める。~/.bunkoko/jquants.json、権限 600。このリポジトリの外）
    python scripts/jquants.py check          # 無料プランで、過去の一覧に後で消えた会社が入るか・その値段が取れるか などを確かめる（数分〜20 分。ページ分けしだい）
    python scripts/jquants.py fetch --from 2024-12-01 --to 2026-07-31             # 日足・財務・月末の一覧を日付ごとに取る（続きから取れる。Free は 13 秒に 1 件）
    python scripts/jquants.py fetch --from 2008-05-07 --to 2026-10-30 --plan premium   # 有料の 1 か月の間に（契約は利用者がはっきり頼んだときだけ）
    python scripts/jquants.py status         # 何を何日分・何行取ったか（ネットにはつながない）

キーは J-Quants のダッシュボードの設定（Settings → API Key）で発行する（登録は利用者が行う）。
取ったものは data/jquants/ に置く（コミットしない。規約: 個人の私的利用のみ・再配布は不可）。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot import jquants as J  # noqa: E402


def _lock() -> Path:
    """同時に 2 本動かさない（速さの決まりを守るため）。印はキーと同じ所に 1 つ（~/.bunkoko/jquants.lock）で、fetch も check も同じ印を見る
    （2026-11-11 点検の指摘で直した。前は置き場所ごと・fetch だけだった）。前の回が落ちて残った印は、その番号のプログラムがもう無ければ消す。"""
    f = J.LOCK_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                pid = int(f.read_text().strip() or 0)
            except (OSError, ValueError):
                pid = 0
            alive = False
            if pid > 0:
                try:
                    os.kill(pid, 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True
            else:                                        # 空・0 以下・読めない印は古い印。ただし、いま書いている途中かもしれない新しい印は消さない
                try:
                    alive = time.time() - f.stat().st_mtime < 10
                except OSError:
                    alive = False
            if alive:
                raise SystemExit(f"もう 1 本の J-Quants の呼び出し（fetch か check）が動いている（{f}、番号 {pid or '?'}）。終わるのを待つ")
            f.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w") as h:
            h.write(str(os.getpid()))
        return f
    raise SystemExit(f"印（{f}）を置けなかった。もう一度動かす")


def _fetch(args, getter, sleep, today: pd.Timestamp) -> Path:
    if not args.start or not args.end:
        raise SystemExit("--from と --to（YYYY-MM-DD）を付ける。例: ./cfd jquants fetch --from 2024-12-01 --to 2026-07-31")
    if args.interval is not None and args.interval < J.PLAN_INTERVAL[args.plan]:
        print(f"注意: --interval {args.interval:g} はプラン {args.plan} の間隔（{J.PLAN_INTERVAL[args.plan]:g} 秒）より速いので使わない（遅くする方にだけ効く）")
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    if start > end:
        raise SystemExit("--from が --to より後になっている")
    paths = {}
    for a in args.also:
        name, _, path = a.partition("=")
        if not name or not path.startswith("/") or name in J.FETCH_KINDS:
            raise SystemExit(f"--also は 名前=/入口 の形で書く（例: dividend=/…）。受け取ったもの: {a}")
        paths[name] = path
    kinds = [k.strip() for k in args.what.split(",") if k.strip()] + list(paths)
    interval = J.plan_interval(args.plan, args.interval)     # --interval は遅くする方にだけ効く
    root = Path(args.dir)
    if args.plan == "free":
        lo, hi = J.free_window(today)
        if start < lo or end > hi:
            print(f"注意: Free で取れるのは {lo:%Y-%m-%d}〜{hi:%Y-%m-%d} ごろ（直近 12 週間を除く 2 年）。外の日は断られ、3 回続くと止まる")
    allp = dict(J.FETCH_KINDS, **paths)
    unknown = [k for k in kinds if k not in allp]
    if unknown:
        raise SystemExit(f"知らない種類: {', '.join(unknown)}（使えるのは {', '.join(allp)}）")
    try:
        todo = J.fetch_plan(J.load_state(root), root, start, end, kinds, args.plan, today, args.retry_empty)
    except J.Stop as e:
        raise SystemExit(f"止めた: {e}")
    rest = {k: v for k, v in todo.items() if k != "master"}
    n = sum(len(v) for v in rest.values())
    secs = sum(len(v) * J.call_interval(interval, allp[k]) for k, v in rest.items())
    print(f"プラン {args.plan}、間隔 {interval:g} 秒。これから取る日: " + "、".join(f"{k} {len(v):,}" for k, v in rest.items())
          + ("、master は日足の後に月末の日を決める" if "master" in kinds else "")
          + f"。少なくとも {n:,} 回・{secs / 3600:.1f} 時間（1 日分が何ページかに分かれると、その分だけ増える）")
    if args.dry_run:
        return root
    if getter is None:
        getter = _getter()
    lock = _lock()
    try:
        client = J.Client(getter, interval=interval, path_interval=J.PATH_INTERVAL, **({"sleep": sleep} if sleep else {}))
        res = J.run_fetch(client, start, end, kinds, args.plan, root=root, now=today, max_calls=args.max_calls,
                          retry_empty=args.retry_empty, paths=paths)
    except J.Stop as e:
        raise SystemExit(f"止めた: {e}")
    finally:
        lock.unlink(missing_ok=True)
    for k, v in res["kinds"].items():
        print(f"{k}: 取った {v['saved']:,} 日（{v['rows']:,} 行）、行なし {v['empty']:,} 日、取れなかった {v['failed']:,} 日")
    if res["undecided_months"]:
        print(f"月末の一覧をまだ取っていない月 {len(res['undecided_months'])}（日足が月末まで取れてから）: " + "、".join(res["undecided_months"][:6]))
    print(f"呼び出し {res['calls']:,} 回。" + (f"止めた: {res['stopped']}" if res["stopped"] else "頼まれた分は終わった"))
    print("\n".join(J.status_lines(J.fetch_status(root))))
    return root


def _getter():
    key = J.load_key()
    if not key:
        raise SystemExit("J-Quants の API キーが無い。J-Quants に無料で登録し、ダッシュボードの設定 → API Key で発行して、"
                         "./cfd jquants key で保存する（docs/survivorship.md 3.1）")
    return J.http_getter(key)


def main(argv: list[str] | None = None, getter=None, sleep=None) -> Path | None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["check", "key", "fetch", "status"])
    p.add_argument("--today", help="今日として扱う日（試験用）")
    p.add_argument("--interval", type=float, default=None,
                   help="呼び出しの間隔（秒）。遅くする方にだけ効く（プランごとの値より速い数は使わない。Free は 13）")
    p.add_argument("--trading-days", default=None, metavar="CSV",
                   help="status: 別の出所の営業日（1 列目か Date の列が日付の CSV。Yahoo の 1306 の日足など）。行なしの平日と突き合わせる")
    p.add_argument("--out", default="output/jquants")
    p.add_argument("--from", dest="start", help="fetch: 始まりの日（YYYY-MM-DD）")
    p.add_argument("--to", dest="end", help="fetch: 終わりの日（YYYY-MM-DD）")
    p.add_argument("--plan", choices=sorted(J.PLAN_INTERVAL), default="free", help="fetch: 契約しているプラン（間隔が決まる。既定は free）")
    p.add_argument("--what", default="bars,fins,master", help="fetch: 取る種類（bars 日足・fins 財務・master 月末の一覧。カンマ区切り）")
    p.add_argument("--also", action="append", default=[], metavar="名前=/入口",
                   help="fetch: 日付 1 つで呼べるほかの入口を足す（入口の名前を公式の文書で確かめた後だけ。配当金など）")
    p.add_argument("--dir", default=str(J.DATA_DIR), help="fetch・status: 置き場所（既定 data/jquants。コミットしない）")
    p.add_argument("--max-calls", type=int, default=None, help="fetch: この回数まで呼んだら止める（試すとき用。続きは同じ命令で）")
    p.add_argument("--retry-empty", action="store_true", help="fetch: 「行なし」と覚えた日も、もう一度聞く")
    p.add_argument("--dry-run", action="store_true", help="fetch: 何日分を取るかだけ出す（つながない）")
    args = p.parse_args(argv)

    if args.command == "key":
        key = getpass.getpass("J-Quants の API キー（画面には出ません）: ").strip()
        if not key:
            raise SystemExit("キーが空だった。保存していない")
        print(f"保存した: {J.save_key(key)}（権限 600。このリポジトリの外）")
        return None

    today = pd.Timestamp(args.today) if args.today else pd.Timestamp.now(tz="Asia/Tokyo").tz_localize(None)
    if args.command == "status":
        try:
            td = J.read_trading_days(Path(args.trading_days)) if args.trading_days else None
            print("\n".join(J.status_lines(J.fetch_status(Path(args.dir), td))))
        except J.Stop as e:
            raise SystemExit(f"止めた: {e}")
        return Path(args.dir)
    if args.command == "fetch":
        return _fetch(args, getter, sleep, today)

    if getter is None:
        getter = _getter()
    client = J.Client(getter, interval=max(J.FREE_INTERVAL, args.interval or 0.0), **({"sleep": sleep} if sleep else {}))
    lock = _lock()                                       # 取得（fetch）が動いている間は、確認（check）も始めない
    try:
        res = J.run_check(client, today)
    except J.Stop as e:
        raise SystemExit(f"止めた: {e}")
    finally:
        lock.unlink(missing_ok=True)
    lines = J.report(res, today)
    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "check.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = {k: v for k, v in res.items() if k not in ("calls",)}
    (out / "check.json").write_text(json.dumps(summary, ensure_ascii=False, default=str, indent=1), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n保存先: {out}/check.md")
    return out


if __name__ == "__main__":
    main()
