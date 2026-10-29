"""HDD の引っ越しと控え（D-008・D-031。SPEC §5 のバックアップ）。

- copy_tree：前の置き場所（SD カード）から、新しい HDD へ丸ごと写す。途中で止めても、同じ
  コマンドで続きから写す（大きさと更新日時が同じファイルは飛ばす）。Mac が exFAT に作る
  付帯情報（._ で始まるファイル）と .DS_Store は写さない
- verify_tree：写した先に、元のファイルが同じ大きさで全部あるかを確かめる
- backup_db：SSD の SQLite を、書き込み中でも壊れない方法（SQLite の backup）で写す
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path


def _skipped(rel: Path) -> bool:
    """写さないもの：Mac の付帯情報・書きかけのファイル（.part・.copying）。"""
    if rel.name.endswith((".part", ".copying")):
        return True
    return any(part.startswith("._") or part == ".DS_Store" for part in rel.parts)


def _files(root: Path) -> Iterator[tuple[Path, Path]]:
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and not p.is_symlink() and not _skipped(rel):
            yield p, rel


@dataclass
class CopyResult:
    copied: int = 0
    skipped: int = 0  # 写し済み（同じ大きさ・更新日時）
    bytes: int = 0
    problems: list[str] = field(default_factory=list)  # 写した先に無い・大きさが違う


def _same(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return False
    a, b = src.stat(), dst.stat()
    return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def copy_tree(
    src: Path, dst: Path, *, log: Callable[[str], None] = lambda _: None, every: int = 1000
) -> CopyResult:
    """src の中身を dst へ写す（src は変えない）。終わったら verify_tree で確かめる。"""
    result = CopyResult()
    # 始めた時点のファイルだけを写して確かめる（augment が動いていて、途中で増えても止めない）
    files = list(_files(src))
    for n, (p, rel) in enumerate(files, start=1):
        target = dst / rel
        if _same(p, target):
            result.skipped += 1
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".copying")
            shutil.copy2(p, tmp)
            tmp.replace(target)  # 途中で止めても、半端なファイルを本物の名前で残さない
            result.copied += 1
            result.bytes += p.stat().st_size
        if n % every == 0:
            log(f"  {n} ファイル（写した {result.copied}、写し済み {result.skipped}）")
    result.problems = verify_tree(src, dst, files)
    return result


def verify_tree(src: Path, dst: Path, files: list[tuple[Path, Path]] | None = None) -> list[str]:
    """写した先に、元のファイルが同じ大きさであるか（files を省くと、今の src の全部）。"""
    problems = []
    for p, rel in files if files is not None else _files(src):
        target = dst / rel
        if not target.exists():
            problems.append(f"無い：{rel}")
        elif target.stat().st_size != p.stat().st_size:
            problems.append(f"大きさが違う：{rel}")
    return problems


def backup_db(db: Path, dst: Path) -> None:
    """SQLite を、書き込み中でも整った形で写す（WAL の分も含む）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".copying")
    with sqlite3.connect(db) as src_conn, sqlite3.connect(tmp) as out:
        src_conn.backup(out)
    tmp.replace(dst)
