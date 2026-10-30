"""HDD の引っ越しと控え（D-008・D-031・D-033。SPEC §5 のバックアップ）。

- copy_tree：前の置き場所（SD カード）から、新しい HDD へ丸ごと写す。途中で止めても、同じ
  コマンドで続きから写す（大きさと更新日時が同じファイルは飛ばす）。写した先のほうが新しい
  ファイルは上書きしない。リンク（シンボリックリンク）はリンクのまま写す。Mac が exFAT に作る
  付帯情報（._ で始まるファイル）と .DS_Store はファイルとしては写さない（ファイルとフォルダの
  タグなどの付帯情報は、Mac では中身といっしょに写す）
- verify_tree：写した先に、元のファイルが同じ大きさで全部あるかを確かめる
- skip_card：SD カードの、koten のフォルダ以外のもの（hdd-copy --whole-card）で写さないもの
- mount_root・on_drive・volume_format・can_write：HDD がつながっているか・Mac で書ける形式か
- backup_db：SSD の SQLite を、書き込み中でも壊れない方法（SQLite の backup）で写す
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import hashlib
import os
import plistlib
import shutil
import sqlite3
import stat
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
# SD カードのほかのものを写し終えた印（HDD のいちばん上）。それからは写さずに、HDD にあるかだけを
# 確かめる（HDD で消したり直したりしたものを、SD カードの古いもので戻さない）
CARD_COPIED_MARK = ".koten-card-copied"
# koten backup の控えのフォルダの印。hdd-copy --whole-card は、この印のあるフォルダを写さない
BACKUP_MARK = ".koten-backup"
# koten のデータのフォルダにあるはずのもの（--from の取り違えを見つける）
DATA_DIRS = ("raw", "clean", "chunks", "augment")
# 書きかけのファイルの名前（.koten-<元の名前から作った12文字>.copying）。長い名前でも255字を超えない
TMP_PREFIX = ".koten-"
TMP_SUFFIX = ".copying"
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
# Mac の HDD に使う形式（diskutil の FilesystemType）。ほかの形式は写す前に止める
SUITED_FORMATS = frozenset({"apfs", "hfs"})
FORMAT_NAMES = {
    "ntfs": "NTFS（Windows 用。Mac からは書き込めません）",
    "exfat": "exFAT（記録の仕組み〔ジャーナル〕が無く、抜けたときに壊れやすい）",
    "msdos": "FAT32（1ファイル4GBまで）",
}
VOLUMES = Path("/Volumes")  # Mac の外付けのドライブがつながる場所
MAC = sys.platform == "darwin"
NEWER_SEC = 2  # 写した先の更新日時がこれより新しければ、写した先のほうが新しいとみなす
COPYFILE_XATTR = 1 << 2  # Mac の copyfile.h
ATTR_BIT_MAP_COUNT, ATTR_CMN_CRTIME, FSOPT_NOFOLLOW = 5, 0x200, 0x1  # Mac の sys/attr.h

Skip = Callable[[Path], bool]


def unsuited_format(fmt: str | None) -> str | None:
    """Mac の HDD に向かない形式なら、見せる名前。APFS・Mac OS 拡張か、分からなければ None。"""
    if not fmt or fmt in SUITED_FORMATS:
        return None
    if "ntfs" in fmt and fmt != "ntfs":  # 追加のソフト（ufsd_ntfs・tuxera_ntfs など）で書ける NTFS
        return f"NTFS（{fmt}。追加のソフトで書けても、試用が切れるなどで書けなくなります）"
    return FORMAT_NAMES.get(fmt, fmt)


def _mac_meta(rel: Path) -> bool:
    return any(part.startswith("._") or part == ".DS_Store" for part in rel.parts)


def _is_tmp(name: str) -> bool:
    return name.startswith(TMP_PREFIX) and name.endswith(TMP_SUFFIX)


def _tmp_for(target: Path) -> Path:
    digest = hashlib.sha1(target.name.encode("utf-8", "surrogateescape")).hexdigest()[:12]
    return target.with_name(f"{TMP_PREFIX}{digest}{TMP_SUFFIX}")


def skip_koten(rel: Path) -> bool:
    """koten のフォルダで写さないもの：Mac の付帯情報・書きかけ（.part・.copying）・印。"""
    if rel.name.endswith((".part", TMP_SUFFIX)) or rel.name in (COPYING_MARK, COPIED_MARK):
        return True
    return _mac_meta(rel)


def _nfc(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", p) for p in parts)


def skip_card(*excludes: Path) -> Skip:
    """SD カードのほかのもので写さないもの：直下の管理用のもの・Mac の付帯情報・hdd-copy の
    書きかけと印・excludes の下（koten のフォルダは別に hdd_root へ写す。控えは写さない）。
    利用者のファイルなので、.part や .copying で終わるものも写す。"""
    exs = [_nfc(e.parts) for e in excludes]

    def skip(rel: Path) -> bool:
        if rel.parts and rel.parts[0] in SYSTEM_ENTRIES:
            return True
        if any(_nfc(rel.parts[: len(ex)]) == ex for ex in exs):
            return True
        if _is_tmp(rel.name) or rel.name in (COPYING_MARK, COPIED_MARK, CARD_COPIED_MARK):
            return True
        return _mac_meta(rel)

    return skip


def backup_dirs(card: Path) -> list[Path]:
    """card のいちばん上にある、koten backup の控えのフォルダ（相対パス）。"""
    found = []
    with contextlib.suppress(OSError):
        for p in card.iterdir():
            with contextlib.suppress(OSError):
                if p.is_dir() and not p.is_symlink() and (p / BACKUP_MARK).exists():
                    found.append(Path(p.name))
    return sorted(found)


@dataclass
class Listing:
    files: list[tuple[Path, Path]] = field(default_factory=list)  # (元のファイル, 相対パス)
    dirs: list[Path] = field(default_factory=list)  # 相対パス（空のフォルダも写す）
    links: list[tuple[Path, Path]] = field(default_factory=list)  # リンク（リンクのまま写す）
    problems: list[str] = field(default_factory=list)  # 読めなかったもの


def list_files(root: Path, skip: Skip = skip_koten) -> Listing:
    """root の下の、写すファイル・フォルダ・リンク。読めないものは problems に書いて先へ進む。"""
    out = Listing()

    def onerror(e: OSError) -> None:
        where = Path(e.filename) if e.filename else root
        rel = where.relative_to(root) if where.is_relative_to(root) else where
        out.problems.append(f"読めない：{rel}（{e.strerror or e}）")

    def mode(p: Path, rel: Path) -> int | None:
        try:
            return p.lstat().st_mode
        except OSError as e:  # 傷んだ SD カードの読めない項目など。止めずに書いておく
            out.problems.append(f"読めない：{rel}（{e.strerror or e}）")
            return None

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        here = Path(dirpath)
        rel_dir = here.relative_to(root)
        keep = []
        for d in sorted(dirnames):
            rel = rel_dir / d
            if skip(rel) or (m := mode(here / d, rel)) is None:
                continue
            if stat.S_ISLNK(m):
                out.links.append((here / d, rel))
            else:
                keep.append(d)
                out.dirs.append(rel)
        dirnames[:] = keep
        for name in sorted(filenames):
            p, rel = here / name, rel_dir / name
            if skip(rel) or (m := mode(p, rel)) is None:
                continue
            if stat.S_ISREG(m):
                out.files.append((p, rel))
            elif stat.S_ISLNK(m):
                out.links.append((p, rel))
            else:
                out.problems.append(f"写せない：{rel}（ふつうのファイルではありません）")
    return out


def _state(src: Path, dst: Path) -> str:
    """same（写し済み）・newer（写した先のほうが新しい。上書きしない）・copy（写す）。"""
    try:
        b = dst.stat()
        a = src.stat()
    except OSError:
        return "copy"  # 写す先に無い・読めない元は、写すときに理由を書く
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
    copied: int = 0  # 写したファイルとリンク
    skipped: int = 0  # 写し済み（同じ大きさ・更新日時。同じ行き先のリンク）
    bytes: int = 0
    kept: list[str] = field(default_factory=list)  # 写した先のほうが新しいので上書きしなかった
    meta_lost: int = 0  # 中身は写したが、タグなどの付帯情報を写せなかったファイル・フォルダ（Mac）
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


class _AttrList(ctypes.Structure):  # Mac の struct attrlist
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


@functools.cache
def _libsystem() -> ctypes.CDLL:
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    lib.fcopyfile.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    lib.fcopyfile.restype = ctypes.c_int
    lib.setattrlist.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(_AttrList),
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_ulong,
    ]
    lib.setattrlist.restype = ctypes.c_int
    return lib


def _keep_times(st: os.stat_result, target: Path) -> None:
    """更新日時を元のとおりにする。Mac では作成日も（utime だけだと、作成日が更新日時に寄る）。"""
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
    born = getattr(st, "st_birthtime", 0)
    if MAC and born > 0:
        sec = int(born)
        ts = _Timespec(sec, int((born - sec) * 1e9))
        attrs = _AttrList(ATTR_BIT_MAP_COUNT, 0, ATTR_CMN_CRTIME, 0, 0, 0, 0)
        with contextlib.suppress(OSError, AttributeError):
            _libsystem().setattrlist(
                os.fsencode(target),
                ctypes.byref(attrs),
                ctypes.byref(ts),
                ctypes.sizeof(ts),
                FSOPT_NOFOLLOW,
            )


def _copy_dir_meta(src: Path, dst: Path) -> bool:
    """Mac：フォルダのタグ・コメントなどの付帯情報（拡張属性）を写す。"""
    try:
        a = os.open(src, os.O_RDONLY)
        try:
            b = os.open(dst, os.O_RDONLY)
            try:
                return _libsystem().fcopyfile(a, b, None, COPYFILE_XATTR) == 0
            finally:
                os.close(b)
        finally:
            os.close(a)
    except OSError:
        return False


def _keep_hidden(st: os.stat_result, target: Path) -> None:
    """Mac：隠す印（Finder で見えない）だけを写す。ロック（uchg）は写さない
    （写すと、書きかけの名前から本物の名前に変えられなくなる）。"""
    if MAC and getattr(st, "st_flags", 0) & stat.UF_HIDDEN:
        with contextlib.suppress(OSError, NotImplementedError):
            flags = os.lstat(target).st_flags  # 写した先のほかの印は変えない
            os.chflags(target, flags | stat.UF_HIDDEN, follow_symlinks=False)


def _copy_one(src: Path, target: Path, result: CopyResult) -> int:
    # 上のフォルダはここでは作らない（copy_tree が順に作ってある。途中で HDD が外れたら、
    # Mac 本体の /Volumes に書かずに失敗する）
    tmp = _tmp_for(target)
    tmp.unlink(missing_ok=True)  # 前に止めたときの書きかけ
    st = src.stat()
    try:
        if not _copy_data(src, tmp):
            result.meta_lost += 1
        _keep_times(st, tmp)
        tmp.replace(target)  # 途中で止めても、半端なファイルを本物の名前で残さない
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
    _keep_hidden(st, target)
    return st.st_size


def _copy_link(src: Path, target: Path, result: CopyResult) -> bool:
    """リンクをリンクのまま写す（行き先の文字列は変えない。cp -R と同じ）。写したら True。"""
    want = os.readlink(src)
    if target.is_symlink():
        if os.readlink(target) == want:
            result.skipped += 1
            return False
    elif target.exists():
        raise FileExistsError(17, "HDD に同じ名前のリンクでないものがあります")
    tmp = _tmp_for(target)
    tmp.unlink(missing_ok=True)
    os.symlink(want, tmp)
    os.replace(tmp, target)
    result.copied += 1
    return True


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
    """src の中身を dst へ写す（src は変えない）。終わったら写した分を verify_tree で確かめる。

    dst は呼ぶ側が作っておく。無ければ HDD が外れているとみなし、何も作らずに返す。
    """
    # 始めた時点のファイルだけを写して確かめる（途中で増えても止めない）
    listing = listing if listing is not None else list_files(src, skip)
    result = CopyResult(problems=list(listing.problems))

    def gone() -> bool:
        if dst.is_dir():
            return False
        result.problems.append(f"写す先（{dst}）がありません。HDD が外れていないか確かめてください")
        return True

    if gone():
        return result
    touched: set[Path] = set()  # この回に作った・中に写したフォルダ（あとで日付を元に戻す）
    for rel in listing.dirs:  # 上のフォルダから順に並んでいる
        target = dst / rel
        try:
            target.mkdir()
        except FileExistsError:
            if not target.is_dir():
                result.problems.append(f"フォルダを作れない：{rel}（同じ名前のファイルがある）")
            continue  # 前からあるフォルダは、HDD で付けたタグなどを SD カードの古いもので戻さない
        except OSError as e:
            if gone():
                return result
            result.problems.append(f"フォルダを作れない：{rel}（{e.strerror or e}）")
            continue
        touched.update((rel, rel.parent))
        if MAC:
            if not _copy_dir_meta(src / rel, target):
                result.meta_lost += 1
            with contextlib.suppress(OSError):
                _keep_hidden((src / rel).lstat(), target)
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
                if gone():
                    return result
                result.problems.append(f"写せない：{rel}（{e.strerror or e}）")
            else:
                result.copied += 1
                checked.append((p, rel))
                touched.add(rel.parent)
        if n % every == 0 or result.bytes - logged >= log_bytes:
            logged = result.bytes
            log(
                f"  {n}/{len(listing.files)} ファイル（写した {result.copied}・"
                f"{result.bytes / 1e9:.1f}GB、写し済み {result.skipped}）"
            )
    for p, rel in listing.links:
        try:
            if _copy_link(p, dst / rel, result):
                touched.add(rel.parent)
        except OSError as e:
            if gone():
                return result
            result.problems.append(f"写せない：{rel}（リンク。{e.strerror or e}）")
    # 中に写すとフォルダの更新日時が変わるので、深いほうから元の日付に戻す（いちばん上は変えない）
    for rel in sorted(touched - {Path(".")}, key=lambda r: len(r.parts), reverse=True):
        with contextlib.suppress(OSError):
            _keep_times((src / rel).stat(), dst / rel)
    result.problems += verify_tree(src, dst, checked)
    return result


def check_tree(listing: Listing, dst: Path, *, same: bool) -> list[str]:
    """写さずに、listing のものが dst にあるかだけを確かめる（写し終えたあとに打ったとき）。

    same=True なら、大きさか更新日時が違うもの（HDD のほうが新しいものは除く）も挙げる。
    koten のフォルダは HDD で作り直すので、あるかだけを見る（same=False）。
    """
    problems = list(listing.problems)
    for p, rel in listing.files + listing.links:
        target = dst / rel
        try:
            if not os.path.lexists(target):
                problems.append(f"HDD に無い：{rel}")
            elif same and (p, rel) in listing.files and _state(p, target) == "copy":
                problems.append(f"HDD と違う：{rel}")
        except OSError as e:
            problems.append(f"確かめられない：{rel}（{e.strerror or e}）")
    return problems


def verify_tree(src: Path, dst: Path, files: list[tuple[Path, Path]] | None = None) -> list[str]:
    """写した先に、元のファイルが同じ大きさであるか（files を省くと、今の src の全部）。"""
    problems = []
    for p, rel in files if files is not None else list_files(src).files:
        target = dst / rel
        try:
            if not target.exists():
                problems.append(f"無い：{rel}")
            elif target.stat().st_size != p.stat().st_size:
                problems.append(f"大きさが違う：{rel}")
        except OSError as e:
            problems.append(f"確かめられない：{rel}（{e.strerror or e}）")
    return problems


def mount_root(path: Path) -> Path:
    """path があるドライブのいちばん上（/Volumes/HD-NRLD-A など）。Mac 本体の上なら / など。"""
    p = Path(path).expanduser().resolve()
    while not os.path.ismount(p):
        p = p.parent
    return p


def on_drive(root: Path) -> bool:
    """mount_root の結果が、外付けのドライブ（/Volumes/名前）か。Mac 本体の / などは違う。"""
    return root.parent == VOLUMES


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
    """SQLite を、書き込み中でも整った形で写す（WAL の分も含む）。失敗したら書きかけを消す。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + TMP_SUFFIX)
    try:
        with (
            contextlib.closing(sqlite3.connect(db)) as src_conn,
            contextlib.closing(sqlite3.connect(tmp)) as out,
        ):
            src_conn.backup(out)
        tmp.replace(dst)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
