"""チャンク化（SPEC §8.2）。

段落単位で min_chars〜max_chars 字にまとめ、文の途中では切らない。章をまたがない。
各チャンクは作品の text_orig 上の位置（char_start・char_end）を持ち、
text_orig[char_start:char_end] がそのチャンクの本文になる。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa

from koten import ids
from koten.parquet import read_rows, write_rows
from koten.textnorm import normalize_for_match

CHUNKS_SCHEMA = pa.schema(
    [
        ("chunk_id", pa.string()),
        ("work_id", pa.string()),
        ("section", pa.string()),
        ("seq", pa.int32()),
        ("text_orig", pa.string()),
        ("text_norm", pa.string()),
        ("char_start", pa.int32()),
        ("char_end", pa.int32()),
    ]
)

# 文末：。！？（全角・半角）と、その後に続く閉じ括弧
_SENTENCE_END = re.compile(r"[。！？!?]+[」』）)〕】]*")


@dataclass
class Span:
    start: int
    end: int

    def __len__(self) -> int:
        return self.end - self.start


def paragraphs(text: str, start: int, end: int) -> list[Span]:
    """範囲内の行を、前後の空白を除いた段落の位置にする。"""
    spans = []
    pos = start
    for line in text[start:end].split("\n"):
        stripped = line.strip()
        if stripped:
            lead = len(line) - len(line.lstrip())
            s = pos + lead
            spans.append(Span(s, s + len(stripped)))
        pos += len(line) + 1
    return spans


def sentences(text: str, span: Span) -> list[Span]:
    out = []
    cur = span.start
    for m in _SENTENCE_END.finditer(text, span.start, span.end):
        if m.end() > cur:
            out.append(Span(cur, m.end()))
            cur = m.end()
    if cur < span.end:
        out.append(Span(cur, span.end))
    return out


def _units(text: str, paras: list[Span], max_chars: int) -> list[Span]:
    units: list[Span] = []
    for p in paras:
        if len(p) <= max_chars:
            units.append(p)
            continue
        for s in sentences(text, p):
            # 1文が max_chars を超えるときだけ、やむを得ず文の途中で切る
            while len(s) > max_chars:
                units.append(Span(s.start, s.start + max_chars))
                s = Span(s.start + max_chars, s.end)
            units.append(s)
    return units


def chunk_section(text: str, start: int, end: int, min_chars: int, max_chars: int) -> list[Span]:
    units = _units(text, paragraphs(text, start, end), max_chars)
    chunks: list[Span] = []
    cur: Span | None = None
    for u in units:
        if cur is None:
            cur = Span(u.start, u.end)
        elif u.end - cur.start <= max_chars:
            cur.end = u.end
        else:
            chunks.append(cur)
            cur = Span(u.start, u.end)
    if cur is not None:
        chunks.append(cur)
    return _fix_short(text, chunks, min_chars, max_chars)


def _sentence_units(text: str, start: int, end: int, max_chars: int) -> list[Span]:
    units: list[Span] = []
    for p in paragraphs(text, start, end):
        for s in sentences(text, p):
            while len(s) > max_chars:
                units.append(Span(s.start, s.start + max_chars))
                s = Span(s.start + max_chars, s.end)
            units.append(s)
    return units


def _rebalance(text: str, a: Span, b: Span, max_chars: int) -> tuple[Span, Span] | None:
    """隣り合う2チャンクを文の切れ目で分け直し、短い方をできるだけ長くする。"""
    units = _sentence_units(text, a.start, b.end, max_chars)
    best, best_score = None, min(len(a), len(b))
    for k in range(1, len(units)):
        left = Span(units[0].start, units[k - 1].end)
        right = Span(units[k].start, units[-1].end)
        if len(left) > max_chars or len(right) > max_chars:
            continue
        score = min(len(left), len(right))
        if score > best_score:
            best, best_score = (left, right), score
    return best


def _fix_short(text: str, chunks: list[Span], min_chars: int, max_chars: int) -> list[Span]:
    """min_chars 未満のチャンクを、前か後ろとつなぐ。

    つなぐと max_chars を超えるときは、隣のチャンクと文の切れ目で分け直す。
    章が丸ごと短いときは、章をまたがないのでそのまま残す。
    """
    i = 0
    while i < len(chunks):
        c = chunks[i]
        if len(c) >= min_chars:
            i += 1
            continue
        prev = chunks[i - 1] if i > 0 else None
        nxt = chunks[i + 1] if i + 1 < len(chunks) else None
        if prev is not None and c.end - prev.start <= max_chars:
            prev.end = c.end
            del chunks[i]
            i -= 1
            continue
        if nxt is not None and nxt.end - c.start <= max_chars:
            c.end = nxt.end
            del chunks[i + 1]
            continue
        options = []
        if nxt is not None and (r := _rebalance(text, c, nxt, max_chars)):
            options.append((min(len(r[0]), len(r[1])), i, r))
        if prev is not None and (r := _rebalance(text, prev, c, max_chars)):
            options.append((min(len(r[0]), len(r[1])), i - 1, r))
        if options:
            _, j, (left, right) = max(options, key=lambda o: o[0])
            chunks[j], chunks[j + 1] = left, right
        i += 1
    return chunks


def chunk_work(
    work_id: str, text: str, sections: list[dict[str, Any]], min_chars: int, max_chars: int
) -> list[dict[str, Any]]:
    rows = []
    seq = 0
    for sec in sections or [{"title": "", "start": 0, "end": len(text)}]:
        for span in chunk_section(text, sec["start"], sec["end"], min_chars, max_chars):
            seq += 1
            body = text[span.start : span.end]
            rows.append(
                {
                    "chunk_id": ids.chunk_id(work_id, seq),
                    "work_id": work_id,
                    "section": sec["title"],
                    "seq": seq,
                    "text_orig": body,
                    "text_norm": normalize_for_match(body),
                    "char_start": span.start,
                    "char_end": span.end,
                }
            )
    return rows


def chunk_source(hdd_root: Path, source: str, min_chars: int, max_chars: int) -> int:
    """clean/<source>/texts.parquet を chunks/<source>.parquet にする。"""
    rows: list[dict[str, Any]] = []
    for t in read_rows(hdd_root / "clean" / source / "texts.parquet"):
        rows.extend(
            chunk_work(
                t["work_id"], t["text_orig"], json.loads(t["sections"]), min_chars, max_chars
            )
        )
    write_rows(hdd_root / "chunks" / f"{source}.parquet", rows, CHUNKS_SCHEMA)
    return len(rows)
