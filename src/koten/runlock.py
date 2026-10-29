"""長い処理（augment・update）が2つ同時に動かないようにする。

augment は処理済みの記録に通し番号（done-00042.parquet）を付けるので、同じ出力先に2つが
同時に書くと番号がぶつかり、結果が上書きされて消える。ロックファイルは SSD に置く
（HDD の SD カードは exFAT で、ファイルのロックが効かないことがあるため）。
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# ロックを持たない古い版の augment も見つけるため、プロセスの一覧でも探す
AUGMENT_COMMAND = re.compile(r"\bkoten\s+augment\b")


class BusyError(RuntimeError):
    """ほかのプロセスがロックを持っている。"""


def lock_path(ssd_root: Path, name: str) -> Path:
    return Path(ssd_root) / "locks" / f"{name}.lock"


@contextmanager
def hold(path: Path) -> Iterator[None]:
    """ロックを取る。ほかのプロセスが持っていれば、待たずに BusyError を出す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise BusyError(str(path)) from e
        f.seek(0)
        f.truncate()
        f.write(f"{os.getpid()}\n")
        f.flush()
        yield
    finally:
        f.close()  # 閉じるとロックも外れる（プロセスが落ちたときも外れる）


def is_busy(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with hold(path):
            return False
    except BusyError:
        return True


def other_processes(
    pattern: re.Pattern[str], ps_output: str | None = None, me: int | None = None
) -> list[tuple[int, str]]:
    """pattern に合うコマンドで動いているプロセス（PID とコマンド）。

    自分と、自分を起動した親たち（caffeinate・uv など）は除く。ps が使えなければ止める
    （見落として2つの augment が同時に書くよりは、止まるほうがよい）。
    """
    if ps_output is None:
        ps_output = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, check=True
        ).stdout
    procs: dict[int, tuple[int, str]] = {}
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = (int(parts[1]), parts[2])
    mine: set[int] = set()
    pid = os.getpid() if me is None else me
    while pid in procs and pid not in mine:
        mine.add(pid)
        pid = procs[pid][0]
    return sorted(
        (p, cmd) for p, (_, cmd) in procs.items() if p not in mine and pattern.search(cmd)
    )
