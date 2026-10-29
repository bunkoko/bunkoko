"""外部サイトへのアクセスは必ずここを通す（SPEC §8.1）。

- 1ホストあたりの間隔を空ける（settings の rate_limit.per_host_per_sec 以下）
- 同じURLは再取得しない（manifest にあれば読み飛ばす。refresh 時は ETag で条件付き取得）
- 原本はそのまま raw/<source>/ に保存し、manifest に URL・取得日時・ハッシュ・ライセンスを書く
- 429・503 のときは Retry-After の秒数（上限120秒）だけ待って、3回まで取り直す（Wikimedia の決まり）
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx


class RateLimiter:
    """ホストごとに間隔を空ける。host_intervals は、ホスト名の末尾ごとの最小の間隔（秒）。

    例：{"ndl.go.jp": 3.0}（国会図書館は、続けて取ると止められた記録があるので3秒あける）。
    """

    def __init__(
        self,
        per_host_per_sec: float,
        clock=time.monotonic,
        sleep=time.sleep,
        host_intervals: dict[str, float] | None = None,
    ):
        self.min_interval = 1.0 / per_host_per_sec if per_host_per_sec > 0 else 0.0
        self.host_intervals = dict(host_intervals or {})
        self._last: dict[str, float] = {}
        self._clock = clock
        self._sleep = sleep

    def interval(self, host: str) -> float:
        name = host.split(":")[0]
        extra = [
            sec
            for suffix, sec in self.host_intervals.items()
            if name == suffix or name.endswith("." + suffix)
        ]
        return max([self.min_interval, *extra])

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last.get(host)
        if last is not None:
            remaining = self.interval(host) - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last[host] = self._clock()


@dataclass
class ManifestEntry:
    url: str
    path: str  # raw/<source>/ からの相対パス
    sha256: str
    fetched_at: str
    etag: str | None = None
    license: str | None = None


class Manifest:
    """raw/<source>/manifest.jsonl。追記のみで、同じURLは後の行が優先される。"""

    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, ManifestEntry] = {}
        if path.exists():
            for line in path.read_text("utf-8").splitlines():
                if line.strip():
                    e = ManifestEntry(**json.loads(line))
                    self.entries[e.url] = e

    def get(self, url: str) -> ManifestEntry | None:
        return self.entries.get(url)

    def add(self, entry: ManifestEntry) -> None:
        self.entries[entry.url] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")


class Fetcher:
    def __init__(
        self,
        raw_dir: Path,
        *,
        user_agent: str,
        per_host_per_sec: float = 1.0,
        timeout_sec: float = 30.0,
        client: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        max_retries: int = 3,
        sleep=time.sleep,
        host_intervals: dict[str, float] | None = None,
    ):
        self.raw_dir = raw_dir
        self.max_retries = max_retries
        self._sleep = sleep
        self.manifest = Manifest(raw_dir / "manifest.jsonl")
        self.limiter = limiter or RateLimiter(per_host_per_sec, host_intervals=host_intervals)
        self.client = client or httpx.Client(
            headers={"User-Agent": user_agent}, timeout=timeout_sec, follow_redirects=True
        )

    def fetch(
        self, url: str, rel_path: str, *, license: str | None = None, refresh: bool = False
    ) -> tuple[Path, bool]:
        """URLを raw_dir/rel_path に保存する。戻り値は（保存先, 今回取得したか）。"""
        dest = self.raw_dir / rel_path
        known = self.manifest.get(url)
        if known and dest.exists() and not refresh:
            return dest, False
        headers = {}
        if refresh and known and known.etag and dest.exists():
            headers["If-None-Match"] = known.etag
        for attempt in range(self.max_retries + 1):
            self.limiter.wait(url)
            resp = self.client.get(url, headers=headers)
            if resp.status_code in (429, 503) and attempt < self.max_retries:
                self._sleep(retry_after(resp))
                continue
            break
        if resp.status_code == 304:
            return dest, False
        resp.raise_for_status()
        body = resp.content
        sha = hashlib.sha256(body).hexdigest()
        if known and dest.exists() and known.sha256 == sha:
            return dest, False
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(body)
        tmp.replace(dest)
        self.manifest.add(
            ManifestEntry(
                url=url,
                path=rel_path,
                sha256=sha,
                fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
                etag=resp.headers.get("ETag"),
                license=license,
            )
        )
        return dest, True

    def fetch_json(self, url: str, subdir: str) -> object:
        """API の応答（JSON）を取り、raw_dir/subdir/ に URL のハッシュの名前で控える。"""
        name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        path, _ = self.fetch(url, f"{subdir}/{name}.json")
        data = json.loads(path.read_text("utf-8"))
        if isinstance(data, dict) and "error" in data:
            # MediaWiki は 200 でエラーを返すことがある。控えを消し、次の実行で取り直す
            path.unlink(missing_ok=True)
            raise ApiError(f"{url}：{data['error']}")
        return data


class ApiError(RuntimeError):
    """API がエラーの JSON を返した（控えは残さない）。"""


def retry_after(resp: httpx.Response, default: float = 5.0, cap: float = 120.0) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", default)), cap)
    except ValueError:
        return default
