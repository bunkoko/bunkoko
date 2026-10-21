"""青空文庫テキストの正規化（SPEC §6・§8.2）。

- ルビ《》と｜を除去、［＃…］の注記を除去
- 外字注記は代替文字に置換して記録（Unicode か JIS X 0213 の面区点があれば実字、無ければ〓）
- 末尾の「底本：」以降は出典情報として切り出す
- 見出し注記から章（section）を取り出す
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa

from koten.license import is_usable
from koten.parquet import read_rows, write_rows

SOURCE = "aozora"
GETA = "〓"

TEXTS_SCHEMA = pa.schema(
    [
        ("work_id", pa.string()),
        ("text_orig", pa.string()),
        ("sections", pa.string()),  # JSON: [{"title", "start", "end"}]
        ("colophon", pa.string()),
        ("gaiji", pa.string()),  # JSON: [{"note", "replacement"}]
    ]
)

_SEPARATOR = re.compile(r"^-{20,}\s*$")
_GAIJI = re.compile(r"※［＃([^］]*)］")
_UNICODE = re.compile(r"U\+([0-9A-Fa-f]{4,6})")
_JIS = re.compile(r"第[34]水準([12])-(\d{1,2})-(\d{1,2})")
_RUBY = re.compile(r"《[^》]*》")
_ANNOTATION = re.compile(r"［＃[^］]*］")
_LEVELS = {"大": 0, "中": 1, "小": 2}
_HEADING_QUOTED = re.compile(r"［＃「[^］]+」は(?:同行|窓)?([大中小])見出し］")
_HEADING_WRAPPED = re.compile(
    r"［＃(?:同行|窓)?([大中小])見出し］.*［＃(?:同行|窓)?\1見出し終わり］"
)


@dataclass
class AozoraText:
    text_orig: str
    sections: list[dict[str, Any]]
    colophon: str
    gaiji: list[dict[str, str]] = field(default_factory=list)


def decode(raw: bytes) -> str:
    for enc in ("cp932", "shift_jis_2004"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


def read_zip_text(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".txt"))
        return decode(zf.read(name))


def _gaiji_char(note: str) -> str | None:
    m = _UNICODE.search(note)
    if m:
        return chr(int(m.group(1), 16))
    m = _JIS.search(note)
    if m:
        plane, row, cell = (int(g) for g in m.groups())
        raw = bytes([0xA0 + row, 0xA0 + cell])
        if plane == 2:
            raw = b"\x8f" + raw
        try:
            return raw.decode("euc_jis_2004")
        except UnicodeDecodeError:
            return None
    return None


def clean_line(line: str, gaiji_log: list[dict[str, str]]) -> str:
    def replace_gaiji(m: re.Match[str]) -> str:
        ch = _gaiji_char(m.group(1)) or GETA
        gaiji_log.append({"note": m.group(1), "replacement": ch})
        return ch

    line = _GAIJI.sub(replace_gaiji, line)
    line = _ANNOTATION.sub("", line)
    line = _RUBY.sub("", line).replace("｜", "")
    # くの字点（横組みの代用表記）
    return line.replace("／″＼", "〴〵").replace("／＼", "〳〵")


def _heading_level(line: str) -> int | None:
    m = _HEADING_QUOTED.search(line) or _HEADING_WRAPPED.search(line)
    return _LEVELS[m.group(1)] if m else None


def split_header_footer(text: str) -> tuple[list[str], str]:
    lines = text.replace("\r\n", "\n").split("\n")
    seps = [i for i, line in enumerate(lines[:80]) if _SEPARATOR.match(line)]
    if len(seps) >= 2:
        start = seps[1] + 1
    else:
        start = next((i + 1 for i, line in enumerate(lines) if not line.strip()), 0)
    end = len(lines)
    for i in range(len(lines) - 1, start - 1, -1):
        if lines[i].startswith("底本："):
            end = i
            break
    colophon = "\n".join(lines[end:]).strip()
    return lines[start:end], colophon


def parse(text: str) -> AozoraText:
    body, colophon = split_header_footer(text)
    gaiji: list[dict[str, str]] = []
    out: list[str] = []
    pos = 0
    levels: list[str | None] = [None, None, None]
    sections: list[dict[str, Any]] = [{"title": "", "start": 0, "end": None}]
    for raw_line in body:
        level = _heading_level(raw_line)
        line = clean_line(raw_line, gaiji).rstrip()
        if not line.strip() and (not out or not out[-1].strip()):
            continue  # 空行は1行にまとめる
        if level is not None and line.strip():
            levels[level] = line.strip()
            for lower in range(level + 1, len(levels)):
                levels[lower] = None
            sections[-1]["end"] = pos
            sections.append(
                {
                    "title": " / ".join(t for t in levels if t),
                    "start": pos + len(line) + 1,
                    "end": None,
                }
            )
        out.append(line)
        pos += len(line) + 1
    while out and not out[-1].strip():
        pos -= len(out.pop()) + 1
    text_orig = "\n".join(out)
    sections[-1]["end"] = len(text_orig)
    for s in sections:
        s["start"] = min(s["start"], len(text_orig))
    sections = [s for s in sections if s["end"] > s["start"]]
    return AozoraText(text_orig=text_orig, sections=sections, colophon=colophon, gaiji=gaiji)


def normalize(hdd_root: Path) -> int:
    """clean/aozora/works.parquet の使用可の作品を正規化して texts.parquet に書く。"""
    clean_dir = hdd_root / "clean" / SOURCE
    raw_dir = hdd_root / "raw" / SOURCE
    rows = []
    for w in read_rows(clean_dir / "works.parquet"):
        if not w["raw_path"] or not is_usable(w["license"]):
            continue
        parsed = parse(read_zip_text(raw_dir / w["raw_path"]))
        rows.append(
            {
                "work_id": w["work_id"],
                "text_orig": parsed.text_orig,
                "sections": json.dumps(parsed.sections, ensure_ascii=False),
                "colophon": parsed.colophon,
                "gaiji": json.dumps(parsed.gaiji, ensure_ascii=False),
            }
        )
    write_rows(clean_dir / "texts.parquet", rows, TEXTS_SCHEMA)
    return len(rows)
