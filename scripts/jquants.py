"""J-Quants（JPX 総研の公式 API）の確認（./cfd jquants）。docs/survivorship.md の段階 1。

    python scripts/jquants.py key            # API キーを保存する（入力を求める。~/.bunkoko/jquants.json、権限 600。このリポジトリの外）
    python scripts/jquants.py check          # 無料プランで、過去の一覧に後で消えた会社が入るか・その値段が取れるか などを確かめる（数分〜20 分。ページ分けしだい）

キーは J-Quants のダッシュボードの設定（Settings → API Key）で発行する（登録は利用者が行う）。
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot import jquants as J  # noqa: E402


def main(argv: list[str] | None = None, getter=None, sleep=None) -> Path | None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["check", "key"])
    p.add_argument("--today", help="今日として扱う日（試験用）")
    p.add_argument("--interval", type=float, default=J.FREE_INTERVAL, help="呼び出しの間隔（秒。Free は 13）")
    p.add_argument("--out", default="output/jquants")
    args = p.parse_args(argv)

    if args.command == "key":
        key = getpass.getpass("J-Quants の API キー（画面には出ません）: ").strip()
        if not key:
            raise SystemExit("キーが空だった。保存していない")
        print(f"保存した: {J.save_key(key)}（権限 600。このリポジトリの外）")
        return None

    if getter is None:
        key = J.load_key()
        if not key:
            raise SystemExit("J-Quants の API キーが無い。J-Quants に無料で登録し、ダッシュボードの設定 → API Key で発行して、"
                             "./cfd jquants key で保存する（docs/survivorship.md 3.1）")
        getter = J.http_getter(key)
    today = pd.Timestamp(args.today) if args.today else pd.Timestamp.now(tz="Asia/Tokyo").tz_localize(None)
    client = J.Client(getter, interval=args.interval, **({"sleep": sleep} if sleep else {}))
    try:
        res = J.run_check(client, today)
    except J.Stop as e:
        raise SystemExit(f"止めた: {e}")
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
