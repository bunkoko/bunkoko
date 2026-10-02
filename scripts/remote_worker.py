"""別の端末（iPad・別の Mac など）から学習の計算に参加するワーカー。

このファイル 1 つを端末にコピーして実行するだけでよい。計算に必要なコードとデータは
Mac（scripts/train.py --listen 0.0.0.0 で起動）から自動でダウンロードする。

    python remote_worker.py http://192.168.1.10:8765 <トークン>

必要なもの: Python 3.9 以上、numpy、pandas（タイムゾーン情報が無い環境では tzdata も）。
iPad では Python が動くアプリ（a-Shell、Pyto、Juno など）を使う。iPadOS はアプリが
裏に回ると処理を止めるので、計算中は画面を点けたまま前面に出しておく
（止まっても、そのタスクは時間切れで Mac 側に回されるので結果は失われない）。
"""

from __future__ import annotations

import socket
import sys
import tempfile
import urllib.request
from pathlib import Path


def _check_env() -> None:
    missing = []
    for mod in ("numpy", "pandas"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        sys.exit(f"{', '.join(missing)} が入っていない（pip install {' '.join(missing)}）")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001
        sys.exit("タイムゾーン情報が無い（pip install tzdata）")


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    server, token = sys.argv[1].rstrip("/"), sys.argv[2]
    _check_env()
    req = urllib.request.Request(server + "/package.zip", headers={"X-Token": token})
    with urllib.request.urlopen(req, timeout=60) as resp:
        package = resp.read()
    path = Path(tempfile.mkdtemp(prefix="cfdbot_")) / "cfdbot.zip"
    path.write_bytes(package)
    sys.path.insert(0, str(path))
    from cfdbot.train.distributed import run_worker

    name = f"remote-{socket.gethostname().split('.')[0]}"
    print(f"{name}: {server} に接続しました。計算を始めます（終わると自動で止まる）")
    n = run_worker(server, token, name, allow_local=False, log=print)
    print(f"{name}: 完了（{n} 件）")


if __name__ == "__main__":
    main()
