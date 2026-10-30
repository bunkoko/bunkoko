"""HDD の引っ越しと控え（D-008・D-031・D-033。SPEC §5 のバックアップ）。

- copy_tree：前の置き場所（SD カード）から、新しい HDD へ丸ごと写す。途中で止めても、同じ
  コマンドで続きから写す（大きさと更新日時が同じファイルは飛ばす）。写した先のほうが新しい
  ファイルは上書きしない。Mac が exFAT に作る付帯情報（._ で始まるファイル）と .DS_Store は
  ファイルとしては写さない（タグなどの付帯情報は、Mac では cp が中身といっしょに写す）
- verify_tree：写した先に、元のファイルが同じ大きさで全部あるかを確かめる
- skip_card：SD カードの、koten のフォルダ以外のもの（hdd-copy --whole-card）で写さないもの
- mount_root・volume_format・can_write：HDD がつながっているか・Mac で書ける形式か
- backup_db：SSD の SQLite を、書き込み中でも壊れない方法（SQLite の backup）で写す
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# 写している途中の印（hdd-copy が置き、写し終えて確かめたら消す）。印があるあいだは HDD を使わない
COPYING_MARK = ".koten-copying"
# 写し終えた印。HDD で動かし始めたあとに SD カードの古い中身で戻さないよう、koten のフォルダは
# もう写さない
COPIED_MARK = ".koten-copied"
# ボリュームの直下にある、Mac・Windows の管理用のもの（ゴミ箱・Spotlight の索引など）
SYSTEM_ENTRIES = frozenset(
    {
        ".Spotlight-V100",
        ".fseventsd",
        ".Trashes",
        ".TemporaryItems",
        ".DocumentRevisions-V100",
        ".apdisk",
        "System Volume Information",
        "$RECYCLE.BIN",
    }
)
# Mac の HDD に向かない形式（diskutil の FilesystemType → 見せる名前）。NTFS は Mac からは読むだけ
UNSUITED_FORMATS = {
    "ntfs": "NTFS（Windows 用。Mac からは書き込めません）",
    "exfat": "exFAT（記録の仕組み〔ジャーナル〕が無く、抜けたときに壊れやすい）",
    "msdos": "FAT32（1ファイル4GBまで）",
}
MAC = sys.platform == "darwin"
NEWER_SEC = 2  # 写した先の更新日時がこれより新しければ、写した先のほうが新しいとみなす

Skip = Callable[[Path], bool]


def _mac_meta(rel: Path) -> bool:
    return any(part.startswith("._") or part == ".DS_Store" for part in rel.parts)


def skip_koten(rel: Path) -> bool:
    """koten のフォルダで写さないもの：Mac の付帯情報・書きかけ（.part・.copying）・印。"""
    if rel.name.endswith((".part", ".copying")) or rel.name in (COPYING_MARK, COPIED_MARK):
        return True
    return _mac_meta(rel)


def _nfc(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", p) for p in parts)


def skip_card(exclude: Path) -> Skip:
    """SD カードのほかのもので写さないもの：直下の管理用のもの・Mac の付帯情報・exclude の下
    （koten のフォルダ。別に hdd_root へ写す）。利用者のファイルなので .part は写す。"""
    ex = _nfc(exclude.parts)

    def skip(rel: Path) -> bool:
        if rel.parts and rel.parts[0] in SYSTEM_ENTRIES:
            return True
        if _nfc(rel.parts[: len(ex)]) == ex:
            return True
        if rel.name.endswith(".copying") or rel.name in (COPYING_MARK, COPIED_MARK):
            return True
        return _mac_meta(rel)

    return skip


@dataclass
class Listing:
    files: list[tuple[Path, Path]] = field(default_factory=list)  # (元のファイル, 相対パス)
    dirs: list[Path] = field(default_factory=list)  # 相対パス（空のフォルダも写す）
    problems: list[str] = field(default_factory=list)  # 読めなかったフォルダ


def list_files(root: Path, skip: Skip = skip_koten) -> Listing:
    """root の下の、写すファイルとフォルダ。読めないフォルダは problems に書いて先へ進む。"""
    out = Listing()

    def onerror(e: OSError) -> None:
        where = Path(e.filename) if e.filename else root
        rel = where.relative_to(root) if where.is_relative_to(root) else where
        out.problems.append(f"読めない：{rel}（{e.strerror or e}）")

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        here = Path(dirpath)
        rel_dir = here.relative_to(root)
        keep = []
        for d in sorted(dirnames):
            rel = rel_dir / d
            if not skip(rel) and not (here / d).is_symlink():
                keep.append(d)
                out.dirs.append(rel)
        dirnames[:] = keep
        for name in sorted(filenames):
            p, rel = here / name, rel_dir / name
            if not skip(rel) and p.is_file() and not p.is_symlink():
                out.files.append((p, rel))
    return out


def _state(src: Path, dst: Path) -> str:
    """same（写し済み）・newer（写した先のほうが新しい。上書きしない）・copy（写す）。"""
    try:
        b = dst.stat()
    except FileNotFoundError:
        return "copy"
    try:
        a = src.stat()
    except OSError:
        return "copy"  # 読めない元は、写すときに理由を書く
    if a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime):
        return "same"
    return "newer" if b.st_mtime - a.st_mtime > NEWER_SEC else "copy"


def bytes_to_copy(files: list[tuple[Path, Path]], dst: Path) -> int:
    """これから写す量（写し済み・写した先のほうが新しいものを除く）。"""
    total = 0
    for p, rel in files:
        if _state(p, dst / rel) == "copy":
            with contextlib.suppress(OSError):
                total += p.stat().st_size
    return total


@dataclass
class CopyResult:
    copied: int = 0
    skipped: int = 0  # 写し済み（同じ大きさ・更新日時）
    bytes: int = 0
    kept: list[str] = field(default_factory=list)  # 写した先のほうが新しいので上書きしなかった
    meta_lost: int = 0  # 中身は写したが、タグなどの付帯情報は写せなかった（Mac）
    problems: list[str] = field(default_factory=list)  # 写せない・写した先に無い・大きさが違う


def _cp(src: Path, dst: Path) -> int:
    """Mac の cp（-p なし）：中身と付帯情報（拡張属性）を写す。ロックなどの属性は写さない。"""
    return subprocess.run(["/bin/cp", "--", str(src), str(dst)], capture_output=True).returncode


def _copy_data(src: Path, tmp: Path) -> bool:
    """中身を写す。付帯情報まで写せたか、写すものが無ければ True。"""
    if MAC:
        if _cp(src, tmp) == 0:
            return True
        # 付帯情報だけ写せなかったときは中身だけ写し直す。読めないファイルは下でも失敗する
        tmp.unlink(missing_ok=True)
        shutil.copyfile(src, tmp)
        return False
    shutil.copyfile(src, tmp)
    return True


def _copy_one(src: Path, target: Path, result: CopyResult) -> int:
    # 上のフォルダはたどって作らない（途中で HDD が外れたら、Mac 本体の /Volumes に書かずに
    # 失敗する）
    target.parent.mkdir(exist_ok=True)
    tmp = target.with_name(target.name + ".copying")
    tmp.unlink(missing_ok=True)  # 前に止めたときの書きかけ
    st = src.stat()
    try:
        if not _copy_data(src, tmp):
            result.meta_lost += 1
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        tmp.replace(target)  # 途中で止めても、半端なファイルを本物の名前で残さない
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
    return st.st_size


def copy_tree(
    src: Path,
    dst: Path,
    *,
    skip: Skip = skip_koten,
    listing: Listing | None = None,
    log: Callable[[str], None] = lambda _: None,
    every: int = 1000,
    log_bytes: int = 5 * 10**9,
) -> CopyResult:
    """src の中身を dst へ写す（src は変えない）。終わったら写した分を verify_tree で確かめる。"""
    # 始めた時点のファイルだけを写して確かめる（途中で増えても止めない）
    listing = listing if listing is not None else list_files(src, skip)
    result = CopyResult(problems=list(listing.problems))
    dst.mkdir(parents=True, exist_ok=True)  # 置き場所は呼ぶ側で確かめてある
    for rel in listing.dirs:  # 上のフォルダから順に並んでいる
        try:
            (dst / rel).mkdir(exist_ok=True)
        except OSError as e:
            result.problems.append(f"フォルダを作れない：{rel}（{e.strerror or e}）")
    checked: list[tuple[Path, Path]] = []
    logged = 0
    for n, (p, rel) in enumerate(listing.files, start=1):
        state = _state(p, dst / rel)
        if state == "same":
            result.skipped += 1
            checked.append((p, rel))
        elif state == "newer":
            result.kept.append(str(rel))
        else:
            try:
                result.bytes += _copy_one(p, dst / rel, result)
            except OSError as e:
                result.problems.append(f"写せない：{rel}（{e.strerror or e}）")
            else:
                result.copied += 1
                checked.append((p, rel))
        if n % every == 0 or result.bytes - logged >= log_bytes:
            logged = result.bytes
            log(
                f"  {n}/{len(listing.files)} ファイル（写した {result.copied}・"
                f"{result.bytes / 1e9:.1f}GB、写し済み {result.skipped}）"
            )
    result.problems += verify_tree(src, dst, checked)
    return result


def verify_tree(src: Path, dst: Path, files: list[tuple[Path, Path]] | None = None) -> list[str]:
    """写した先に、元のファイルが同じ大きさであるか（files を省くと、今の src の全部）。"""
    problems = []
    for p, rel in files if files is not None else list_files(src).files:
        target = dst / rel
        if not target.exists():
            problems.append(f"無い：{rel}")
        elif target.stat().st_size != p.stat().st_size:
            problems.append(f"大きさが違う：{rel}")
    return problems


def mount_root(path: Path) -> Path:
    """path があるドライブのいちばん上（/Volumes/HD-NRLD-A など。Mac 本体の上なら /）。"""
    p = Path(path).expanduser().resolve()
    while not os.path.ismount(p):
        p = p.parent
    return p


def volume_format(path: Path) -> str | None:
    """path があるドライブの形式（apfs・hfs・exfat・msdos・ntfs など）。Mac の diskutil で調べる。
    分からなければ None。"""
    if not MAC:
        return None
    try:
        r = subprocess.run(
            ["diskutil", "info", "-plist", str(mount_root(path))], capture_output=True, timeout=60
        )
        info = plistlib.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError):
        return None
    fmt = info.get("FilesystemType") if isinstance(info, dict) else None
    return str(fmt).lower() if fmt else None


def can_write(folder: Path) -> str | None:
    """folder に小さなファイルを作って消す。書けなければその理由。"""
    probe = folder / ".koten-write-test"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as e:
        return e.strerror or str(e)
    return None


def free_bytes(folder: Path) -> int:
    return shutil.disk_usage(folder).free


def backup_db(db: Path, dst: Path) -> None:
    """SQLite を、書き込み中でも整った形で写す（WAL の分も含む）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".copying")
    with sqlite3.connect(db) as src_conn, sqlite3.connect(tmp) as out:
        src_conn.backup(out)
    tmp.replace(dst)
