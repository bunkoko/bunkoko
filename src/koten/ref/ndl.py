"""国立国会図書館デジタルコレクションの資料を、一度だけまとめて取る（M8a・D-003）。

方針（2026-10-29 利用者）：NDL の資料は継続的には取らず、使う資料を一度にまとめて取り、
あとは HDD の写しだけを使う。

- 取る前に、資料の台帳で使える（関わった人全員の没年・NDL の公開範囲 internet_pd など）と
  判定されていること（R2）。設定の [ndl] fetch_enabled が true であること（一度だけまとめて
  取るのは継続的な利用に当たらないと利用者が判断した。D-003）
- まず IIIF のマニフェストを1回だけ取り、公開範囲（metadata の Access Restrictions）を見る。
  「PDM」か「インターネット公開（保護期間満了）」でなければ、画像は取らない
- 画像は、指定したコマだけを1件ずつ取る（net.py。国会図書館は3秒あける）。403 が出たら止める
  （続けて取ると止められた記録がある。しばらく待って、同じコマンドで続きから取る）
- 取ったものと日時は raw/ndl/<PID>/snapshot.json に残す。最後の確認は、人が画面で使うコマの
  公開範囲を見る（NDL は「転載したい画像を表示して確認」と案内している）
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from koten.net import Fetcher
from koten.ref.registry import RefWork

MANIFEST_URL = "https://dl.ndl.go.jp/api/iiif/{pid}/manifest.json"
IMAGE_URL = "https://dl.ndl.go.jp/api/iiif/{pid}/R{koma:07d}/full/{size}/0/default.jpg"
# マニフェストの公開範囲で、保護期間満了とみなす値（今の値は PDM、前の値は日本語の表示とされる）
PD_VALUES = {"pdm", "インターネット公開(保護期間満了)"}
ACCESS_LABELS = ("access restrictions", "公開範囲")


class NdlError(RuntimeError):
    pass


@dataclass
class Snapshot:
    ref_id: str
    pid: str
    access_value: str
    access: str  # internet_pd / unknown
    access_checked_at: str  # マニフェストを取った日時（続きから取るときも、最初に取った日時のまま）
    koma_total: int
    first_run_at: str = ""
    last_run_at: str = ""
    size: str = ""  # 画像の大きさ（続きから取るときに変えると、混ざるので止める）
    images: list[int] = field(default_factory=list)
    stopped: str = ""  # 途中で止めた理由（403 など）


def _text(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return _text(v.get("@value") or v.get("value") or v.get("none") or v.get("ja") or "")
    if isinstance(v, list):
        return " ".join(_text(x) for x in v)
    return str(v or "")


def access_value(manifest: dict[str, Any]) -> str:
    """マニフェストの metadata から公開範囲の値を取る（無ければ空）。"""
    for m in manifest.get("metadata") or []:
        if _text(m.get("label")).strip().lower() in ACCESS_LABELS:
            return _text(m.get("value")).strip()
    return ""


def access_kind(value: str) -> str:
    v = re.sub(r"\s+", "", value).replace("（", "(").replace("）", ")").lower()
    return "internet_pd" if v in PD_VALUES else "unknown"


def koma_total(manifest: dict[str, Any]) -> int:
    seqs = manifest.get("sequences") or []
    if seqs:
        return len(seqs[0].get("canvases") or [])
    return len(manifest.get("items") or [])  # IIIF v3


def parse_koma(spec: str, total: int) -> list[int]:
    """「1-20,35」→ [1…20, 35]。all は全部のコマ。"""
    if spec.strip() == "all":
        return list(range(1, total + 1))
    out: list[int] = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise NdlError(f"コマの指定 {part} が読めない（例：1-20,35 か all）")
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if not 1 <= lo <= hi <= total:
            raise NdlError(f"コマ {part} が範囲（1〜{total}）の外")
        out += range(lo, hi + 1)
    return sorted(set(out))


def snapshot_path(hdd_root: Path, pid: str) -> Path:
    return hdd_root / "raw" / "ndl" / pid / "snapshot.json"


def take_snapshot(
    hdd_root: Path,
    work: RefWork,
    fetcher: Fetcher,
    *,
    koma: str = "",
    size: str = "full",
    log: Callable[[str], None] = lambda _: None,
    now: datetime | None = None,
) -> Snapshot:
    if work.source != "ndl" or not work.pid:
        raise NdlError(f"{work.ref_id} は NDL の資料ではない（source=ndl と pid が要る）")
    if not work.usable:
        raise NdlError(f"{work.ref_id} は {work.license}（{work.computed_reason}）。取らない")
    pid = work.pid
    lic = str(work.license)
    out = snapshot_path(hdd_root, pid)
    previous = json.loads(out.read_text("utf-8")) if out.exists() else {}
    if previous.get("images") and previous.get("size") and previous["size"] != size:
        raise NdlError(
            f"前に取った画像は大きさ {previous['size']} です。"
            f"続きも同じ大きさで取る（--size {previous['size']}）"
        )
    url = MANIFEST_URL.format(pid=pid)
    try:
        path, _ = fetcher.fetch(url, f"{pid}/manifest.json", license=lic)
    except httpx.HTTPError as e:
        raise NdlError(f"{pid} のマニフェストを取れない（{_short(e)}）") from e
    manifest = json.loads(path.read_text("utf-8"))
    value = access_value(manifest)
    kind = access_kind(value)
    total = koma_total(manifest)
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    known = fetcher.manifest.get(url)
    snap = Snapshot(
        ref_id=work.ref_id,
        pid=pid,
        access_value=value,
        access=kind,
        access_checked_at=known.fetched_at if known else stamp,
        koma_total=total,
        first_run_at=previous.get("first_run_at") or stamp,
        last_run_at=stamp,
        size=size,
    )
    snap.images = sorted(set(previous.get("images") or []))
    if kind != "internet_pd":
        shown = value or "（無し）"
        snap.stopped = (
            f"マニフェストの公開範囲が「{shown}」。保護期間満了ではないので画像は取らない"
        )
        _write(out, snap)
        return snap
    wanted = parse_koma(koma, total) if koma else []
    for k in wanted:
        if k in snap.images:
            continue
        url = IMAGE_URL.format(pid=pid, koma=k, size=size)
        try:
            _, got = fetcher.fetch(url, f"{pid}/images/{k:04d}.jpg", license=lic)
        except httpx.HTTPStatusError as e:
            snap.stopped = (
                f"コマ{k}で {e.response.status_code}。しばらく待ってから、同じコマンドで続きを取る"
            )
            break
        except httpx.HTTPError as e:
            snap.stopped = f"コマ{k}で接続の失敗（{_short(e)}）。同じコマンドで続きを取る"
            break
        snap.images.append(k)
        if got:
            log(f"  コマ{k}")
    snap.images = sorted(set(snap.images))
    _write(out, snap)
    return snap


def _write(path: Path, snap: Snapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(snap), ensure_ascii=False, indent=1), "utf-8")


def _short(e: Exception) -> str:
    text = str(e).splitlines()[0] if str(e) else type(e).__name__
    return text[:120]


def record_check(conn: Any, snap: Snapshot, now: datetime | None = None) -> None:
    """マニフェストで見た公開範囲を SSD に写す（HDD が無くても ref build が見られるように）。"""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO ndl_checks(ref_id, pid, access, access_value, "
            "access_checked_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                snap.ref_id,
                snap.pid,
                snap.access,
                snap.access_value,
                snap.access_checked_at,
                (now or datetime.now(UTC)).isoformat(timespec="seconds"),
            ),
        )


def sync_snapshots(conn: Any, hdd_root: Path) -> None:
    """HDD の snapshot.json を SSD に写す（SSD を作り直したときのため）。"""
    for path in sorted((hdd_root / "raw" / "ndl").glob("*/snapshot.json")):
        if any(part.startswith(".") for part in path.parts[-2:]):
            continue
        d = json.loads(path.read_text("utf-8"))
        if not d.get("ref_id") or not d.get("access"):
            continue
        known = conn.execute(
            "SELECT access_checked_at FROM ndl_checks WHERE ref_id = ?", (d["ref_id"],)
        ).fetchone()
        if known is None or (known[0] or "") < (d.get("access_checked_at") or ""):
            record_check(
                conn,
                Snapshot(
                    ref_id=d["ref_id"],
                    pid=d.get("pid", ""),
                    access_value=d.get("access_value", ""),
                    access=d["access"],
                    access_checked_at=d.get("access_checked_at", ""),
                    koma_total=d.get("koma_total", 0),
                ),
            )


def restrictions(conn: Any, works: list[RefWork]) -> dict[str, str]:
    """マニフェストで保護期間満了ではなかった NDL の資料（ref_id → 使えない理由）。"""
    out = {}
    pids = {w.ref_id: w.pid for w in works if w.source == "ndl"}
    for r in conn.execute("SELECT * FROM ndl_checks"):
        if r["ref_id"] in pids and r["pid"] == pids[r["ref_id"]] and r["access"] != "internet_pd":
            out[r["ref_id"]] = (
                f"国会図書館のマニフェストの公開範囲は「{r['access_value'] or '（無し）'}」"
                f"（{r['access_checked_at']}）。保護期間満了ではない"
            )
    return out
