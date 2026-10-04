"""学習の分散実行（この Mac の全コア + iPad など同じネットワークの端末）。

Mac 側でコーディネーター（小さな HTTP サーバー）が計算タスクを配り、ワーカーが
「タスクを借りる → 計算する → 結果を返す」を繰り返す。この Mac のワーカーも同じ仕組みで動く。

- 結果は届いた順に SQLite に保存するので、途中で止めても次回は続きから再開できる
- 借りたまま戻ってこないタスク（iPad がスリープした等）は一定時間後に他のワーカーへ回す
- 外部端末用には、このパッケージ自体を zip で配る（端末側のコードを揃える手間が要らない）
- 通信はトークン付き。同じ LAN 内でも他人のアクセスは受け付けない
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

PACKAGE_DIR = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- 結果の保存
class ResultStore:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS results (id TEXT PRIMARY KEY, body TEXT)")
        self._db.commit()
        self._lock = threading.Lock()

    def ids(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT id FROM results")}

    def put(self, task_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO results VALUES (?, ?)", (task_id, json.dumps(result)))
            self._db.commit()

    def get_many(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        out = {}
        with self._lock:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                q = f"SELECT id, body FROM results WHERE id IN ({','.join('?' * len(chunk))})"
                out.update({k: json.loads(v) for k, v in self._db.execute(q, chunk)})
        return out

    def close(self) -> None:
        self._db.close()


def build_package_zip() -> bytes:
    """cfdbot パッケージ（.py のみ）を zip にする。外部ワーカーはこれを import する。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(PACKAGE_DIR.rglob("*.py")):
            z.write(path, Path("cfdbot") / path.relative_to(PACKAGE_DIR))
    return buf.getvalue()


# --------------------------------------------------------------------------- コーディネーター
class Coordinator:
    def __init__(
        self,
        tasks: list[dict[str, Any]],
        meta: dict[str, Any],
        files: dict[str, Path],
        store: ResultStore,
        token: str,
        host: str = "127.0.0.1",
        port: int = 8765,
        lease_seconds: float = 600.0,
    ):
        self.tasks = {t["id"]: t for t in tasks}
        self.pending: deque[str] = deque(t["id"] for t in tasks)
        self.leased: dict[str, tuple[float, str]] = {}
        self.done: set[str] = set()
        self.failed: dict[str, str] = {}
        self.meta = meta
        self.files = files
        self.store = store
        self.token = token
        self.lease_seconds = lease_seconds
        self.workers: dict[str, float] = {}       # ワーカー名 → 最終通信時刻
        self.completed_by: dict[str, int] = {}
        self.package = build_package_zip()
        self._lock = threading.Lock()
        self._durations: deque[float] = deque(maxlen=200)
        self.server = ThreadingHTTPServer((host, port), _make_handler(self))
        self.server.daemon_threads = True
        self.address = self.server.server_address
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # -- ライフサイクル
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.address[1]}"

    # -- タスク管理
    def lease(self, worker: str, n: int) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self.workers[worker] = now
            for tid, (deadline, _) in list(self.leased.items()):
                if deadline < now:  # 期限切れ → 再配布
                    del self.leased[tid]
                    self.pending.appendleft(tid)
            out = []
            while self.pending and len(out) < n:
                tid = self.pending.popleft()
                if tid in self.done:
                    continue
                self.leased[tid] = (now + self._lease_timeout(), worker)
                out.append(self.tasks[tid])
            return {"tasks": out, "done": self.is_done()}

    def complete(self, worker: str, result: dict[str, Any]) -> None:
        tid = result.get("id")
        if tid not in self.tasks:
            return
        with self._lock:
            self.workers[worker] = time.time()
            if tid in self.done:
                return
            self.done.add(tid)
            self.leased.pop(tid, None)
            self.completed_by[worker] = self.completed_by.get(worker, 0) + 1
            if result.get("ok"):
                self._durations.append(float(result.get("secs", 0.0)))
            else:
                self.failed[tid] = str(result.get("error", ""))
        self.store.put(tid, result)

    def _lease_timeout(self) -> float:
        if self._durations:
            med = sorted(self._durations)[len(self._durations) // 2]
            return max(self.lease_seconds, med * 30)
        return self.lease_seconds

    def is_done(self) -> bool:
        return len(self.done) >= len(self.tasks)

    def status(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            active = {w: c for w, c in self.completed_by.items() if now - self.workers.get(w, 0) < 120}
            return {
                "total": len(self.tasks), "done": len(self.done), "failed": len(self.failed),
                "leased": len(self.leased), "workers": sorted(w for w, t in self.workers.items() if now - t < 120),
                "completed_by": dict(self.completed_by), "active": active,
                "errors": list(self.failed.values())[:3],
            }

    def wait(self, on_progress: Callable[[dict[str, Any]], None] | None = None, interval: float = 10.0) -> None:
        last = 0.0
        while not self.is_done():
            time.sleep(0.5)
            if on_progress and time.time() - last >= interval:
                last = time.time()
                on_progress(self.status())
        if on_progress:
            on_progress(self.status())


def _make_handler(coord: Coordinator):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 標準のアクセスログは出さない
            pass

        def _auth(self) -> bool:
            if self.headers.get("X-Token") != coord.token:
                self.send_error(403, "bad token")
                return False
            return True

        def _send(self, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any) -> None:
            self._send(json.dumps(obj).encode())

        def _read_json(self) -> Any:
            n = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if not self._auth():
                return
            if self.path == "/meta":
                self._json(coord.meta)
            elif self.path == "/status":
                self._json(coord.status())
            elif self.path == "/package.zip":
                self._send(coord.package, "application/zip")
            elif self.path.startswith("/data/"):
                key = urllib.parse.unquote(self.path[len("/data/"):])
                if key not in coord.files:
                    self.send_error(404)
                    return
                self._send(coord.files[key].read_bytes(), "text/csv")
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._auth():
                return
            body = self._read_json()
            worker = str(body.get("worker", "?"))[:64]
            if self.path == "/lease":
                self._json(coord.lease(worker, max(1, min(int(body.get("n", 1)), 16))))
            elif self.path == "/result":
                coord.complete(worker, body.get("result", {}))
                self._json({"ok": True})
            else:
                self.send_error(404)

    return Handler


# --------------------------------------------------------------------------- ワーカー
class _Client:
    def __init__(self, server: str, token: str, timeout: float = 60.0):
        self.server = server.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, path: str, data: bytes | None = None) -> bytes:
        req = urllib.request.Request(self.server + path, data=data, headers={
            "X-Token": self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()

    def get_json(self, path: str) -> Any:
        return json.loads(self._req(path))

    def post_json(self, path: str, obj: Any) -> Any:
        return json.loads(self._req(path, json.dumps(obj).encode()))

    def get_bytes(self, path: str) -> bytes:
        return self._req(path)


def _load_dataset(client: _Client, meta: dict[str, Any], allow_local: bool):
    """データを読む。同じ Mac ならローカルのファイル、外部端末ならダウンロードしたもの。"""
    import hashlib

    from ..data import load_mt5_csv
    from .dataset import dataset_from_frames

    loaded = {}
    tmp = Path(tempfile.mkdtemp(prefix="cfdbot_"))
    for key, info in meta["files"].items():
        path = Path(info["path"])
        ok = allow_local and path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == info["sha256"]
        if not ok:
            raw = client.get_bytes("/data/" + urllib.parse.quote(key, safe=""))
            if hashlib.sha256(raw).hexdigest() != info["sha256"]:
                raise RuntimeError(f"{key}: ダウンロードしたデータが壊れている")
            path = tmp / info["name"]
            path.write_bytes(raw)
        loaded[key] = load_mt5_csv(path, server_tz=meta["server_tz"])
    return dataset_from_frames(meta["files"], loaded, meta["signal_timeframes"], meta["fill_timeframe"],
                               meta["server_tz"])


def run_worker(server: str, token: str, name: str, allow_local: bool = True, batch: int = 1,
               log: Callable[[str], None] | None = None) -> int:
    """タスクが無くなるまで計算を続ける。処理した件数を返す。"""
    from .evaluate import EvalSettings, Evaluator, Task

    client = _Client(server, token)
    meta = client.get_json("/meta")
    ds = _load_dataset(client, meta, allow_local)
    ev = Evaluator(ds, EvalSettings.from_dict(meta["settings"]))
    if log:
        log(f"[{name}] 準備完了（{len(ds.symbols)} 銘柄、{len(meta['files'])} ファイル）")
    count = 0
    idle = 0
    while True:
        try:
            resp = client.post_json("/lease", {"worker": name, "n": batch})
        except OSError:
            idle += 1
            if idle > 30:  # サーバーが終了した
                return count
            time.sleep(2)
            continue
        idle = 0
        if not resp["tasks"]:
            if resp["done"]:
                return count
            time.sleep(1.0)
            continue
        for t in resp["tasks"]:
            try:
                result = ev.run(Task.from_dict(t))
            except Exception as e:  # パラメータの組み合わせが不正など。記録して次へ
                result = {"id": t["id"], "ok": False, "error": repr(e)}
            client.post_json("/result", {"worker": name, "result": result})
            count += 1
            if log and count % 50 == 0:
                log(f"[{name}] {count} 件")


def _local_worker_entry(server: str, token: str, name: str) -> None:
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    run_worker(server, token, name, allow_local=True)
