"""この回に使う自分の写真（docs/m6_design.md・SPEC §8.10）。

- list_photos：写真のフォルダ（settings の photos_dir。HDD）にある写真の一覧
- pick_photos：直近の回で使った写真を除いて、回の ID で決まる順に選ぶ
- prepare_photo：画面の大きさ（1920×1080）に切り抜いた jpg を書く（ffmpeg。HEIC は Mac の sips で
  jpg にしてから）

写真のフォルダは動画の書き出しのときだけ読む（検索と API は読まない）。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}


class PhotoError(RuntimeError):
    """写真を切り抜けないとき（ffmpeg・sips が無い、読めない写真）。"""


def list_photos(photos_dir: Path) -> list[Path]:
    """写真の一覧（下のフォルダも見る）。隠しファイル・隠しフォルダの中は除く。並びは名前順。"""
    out = []
    for p in photos_dir.rglob("*"):
        rel = p.relative_to(photos_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if p.is_file() and p.suffix.lower() in PHOTO_EXT:
            out.append(p)
    return sorted(out, key=lambda p: p.relative_to(photos_dir).as_posix())


def pick_photos(
    photos: list[Path], exclude: set[str], n: int, seed: str, photos_dir: Path
) -> list[Path]:
    """直近の回の写真（exclude。photos_dir からの相対パス）を除いて、seed で決まる順に n 枚。

    足りなければ、選んだ写真をこの回の中で使い回す（直近の回の写真は使わない）。0枚なら空。
    """
    usable = []
    for p in photos:
        rel = p.relative_to(photos_dir)
        if rel.as_posix() not in exclude and str(rel) not in exclude:
            usable.append(p)
    if not usable or n <= 0:
        return []

    def order(p: Path) -> str:
        key = f"{seed}\n{p.relative_to(photos_dir).as_posix()}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()

    usable.sort(key=order)
    return [usable[i % len(usable)] for i in range(n)]


def prepare_photo(src: Path, dst: Path, width: int, height: int) -> None:
    """src を width×height に切り抜いた jpg を dst に書く。

    画面を覆うように拡大・縮小し、はみ出した分を真ん中を残して切る。

    向き（写真の Exif の回転）は ffmpeg が直す。
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise PhotoError("写真の切り抜きに ffmpeg が要ります（Mac なら brew install ffmpeg）")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="koten-photo-") as tmp:
        source = src
        if src.suffix.lower() == ".heic":
            source = _heic_to_jpg(src, Path(tmp) / "src.jpg")
        part = dst.with_name(dst.stem + ".part.jpg")
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={width}:{height},setsar=1"
        )
        cmd = [ffmpeg, "-v", "error", "-y", "-i", str(source), "-vf", vf]
        cmd += ["-frames:v", "1", "-q:v", "2", "-update", "1", str(part)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not part.exists():
            part.unlink(missing_ok=True)
            msg = (r.stderr.strip().splitlines() or ["不明なエラー"])[-1]
            raise PhotoError(f"写真 {src} を切り抜けませんでした（ffmpeg：{msg}）")
        part.replace(dst)


def _heic_to_jpg(src: Path, out: Path) -> Path:
    """HEIC を Mac の sips で jpg にする。"""
    sips = shutil.which("sips")
    if sips is None:
        raise PhotoError(
            f"写真 {src.name} は HEIC です。HEIC は Mac の sips で jpg にしてから使います。"
            "この機械には sips が無いので、jpg に書き出してから写真のフォルダに置いてください"
        )
    r = subprocess.run(
        [sips, "-s", "format", "jpeg", str(src), "--out", str(out)], capture_output=True, text=True
    )
    if r.returncode != 0 or not out.exists():
        msg = (r.stderr.strip().splitlines() or ["不明なエラー"])[-1]
        raise PhotoError(f"写真 {src} を jpg にできませんでした（sips：{msg}）")
    return out
