"""元の資料の該当箇所を探して、確かめのページで「そのページに飛ぶ・印が付いて見える」ようにする。

統計：元の資料の PDF を取り（net.py）、調べ役が写した数字の行（evidence）が載っているページを探す。
そのページを画像にして、該当の行に黄色の印を付け、前後を切り出す（img/<stat_id>.jpg）。
リンクは PDF の該当ページ（#page=）にする。PDF でない資料は、文の断片のリンク（#:~:text=）にする。

研究（CC BY の論文）：調べ役が本文から写した結果の文（passages）が、論文の本文にそのままあるかを
確かめる（net.py で本文を取り直して探す）。無い文は出さない。リンクは文の断片のリンクにする。

- 使う道具：pdftotext・pdftoppm（poppler）と ffmpeg。ページを作るのは Claude の環境だけ
- 取った資料は data/sources-cache に、画像は data/sources-img に置く（Git に入れない）
- 国の資料は政府標準利用規約などで、出典を書けば複製できる。画像は確かめのページ（本人だけが開ける）
  にだけ載せる
"""

from __future__ import annotations

import hashlib
import html
import re
import subprocess
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

DPI = 110
MARGIN_ABOVE = 700  # 印の上に残す高さ（画像の点。表の題と列の見出しが入るように）
MARGIN_BELOW = 260  # 印の下に残す高さ
MIN_CHUNK = 6


class LocateError(RuntimeError):
    pass


def norm(text: str) -> str:
    """探すための形：全角・半角をそろえ、空白を全部取る。"""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("−", "-").replace("–", "-").replace("―", "-")
    return re.sub(r"\s+", "", text)


def original_span(text: str, target: str, most: int = 20) -> str:
    """text の中で target（探すための形）に当たる元の文字の並びを、頭から most 字まで返す。

    文の断片のリンクは、ページに書いてあるとおりの文字でないと飛ばないので、元の文字に戻す。
    """
    normed, where = "", []
    for i, ch in enumerate(text):
        n = norm(ch)
        normed += n
        where += [i] * len(n)
    t = norm(target)
    at = normed.find(t) if t else -1
    if at < 0:
        return ""
    end = where[min(at + len(t), len(where)) - 1] + 1
    return re.sub(r"\s+", " ", text[where[at] : end]).strip()[:most]


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    line: int


def pdf_pages(path: Path) -> list[str]:
    """PDF の各ページの文字（pdftotext。ページは \\f で分かれる）。"""
    out = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"], capture_output=True, check=True
    ).stdout.decode("utf-8", "replace")
    return out.split("\f")


def _chunks(text: str, size: int = 10) -> list[str]:
    """文を探すための断片（数字を含む断片を先に）。"""
    t = norm(text)
    if len(t) <= size:
        return [t] if len(t) >= MIN_CHUNK else []
    parts = [t[i : i + size] for i in range(0, len(t) - size + 1, size // 2)]
    return sorted(dict.fromkeys(parts), key=lambda p: not re.search(r"\d", p))


def clean_evidence(evidence: str) -> str:
    """調べ役が写した行から、調べ役のことわり書き（「（PDFの…写した）」など）を外す。"""
    return re.sub(
        r"[（(][^（）()]*(?:写した|PDF|ＰＤＦ|注：)[^（）()]*[）)]\s*$", "", evidence or ""
    )


def find_page(pages: list[str], evidence: str, value: str) -> tuple[int, str] | None:
    """evidence が載っているページ（1 から）と、見つけ方（whole：行のまま・parts：断片）。"""
    evidence = clean_evidence(evidence)
    target = norm(evidence)
    normed = [norm(p) for p in pages]
    if len(target) >= MIN_CHUNK:
        for i, p in enumerate(normed):
            if target in p:
                return i + 1, "whole"
    chunks = _chunks(evidence)
    if not chunks:
        return None
    v = norm(value).replace(",", "")
    best, best_score = None, 0.0
    for i, p in enumerate(normed):
        hit = sum(1 for c in chunks if c in p)
        score = hit / len(chunks) + (0.25 if v and v in p.replace(",", "") else 0)
        if score > best_score:
            best, best_score = i + 1, score
    if best is None or best_score < 0.5:
        return None
    return best, "parts"


def page_words(path: Path, page: int) -> tuple[list[Word], float, float]:
    """ページの語と位置（pdftotext -bbox-layout。左上が原点、単位はポイント）。"""
    out = subprocess.run(
        ["pdftotext", "-bbox-layout", "-f", str(page), "-l", str(page), str(path), "-"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", "replace")
    m = re.search(r'<page width="([\d.]+)" height="([\d.]+)"', out)
    if not m:
        raise LocateError(f"{path.name} の {page} ページを読めない")
    width, height = float(m.group(1)), float(m.group(2))
    words: list[Word] = []
    for n, line in enumerate(re.findall(r"<line[^>]*>(.*?)</line>", out, re.S)):
        for w in re.finditer(
            r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>',
            line,
        ):
            words.append(
                Word(
                    html.unescape(w.group(5)),
                    float(w.group(1)),
                    float(w.group(2)),
                    float(w.group(3)),
                    float(w.group(4)),
                    n,
                )
            )
    return words, width, height


NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _lines(words: list[Word]) -> dict[int, list[int]]:
    """見た目の行（高さがそろう語の並び）ごとの語の番号。

    pdftotext の行は段組みのかたまりごとに分かれ、表の1行が別々になるので、語の上下の位置で
    まとめ直す。左から右の順。
    """
    order = sorted(range(len(words)), key=lambda i: (words[i].y0 + words[i].y1) / 2)
    rows: list[list[int]] = []
    for i in order:
        w = words[i]
        center, height = (w.y0 + w.y1) / 2, max(1.0, w.y1 - w.y0)
        if rows:
            last = [words[j] for j in rows[-1]]
            c = sum((x.y0 + x.y1) / 2 for x in last) / len(last)
            if abs(center - c) <= max(2.0, 0.45 * height):
                rows[-1].append(i)
                continue
        rows.append([i])
    return {n: sorted(r, key=lambda i: words[i].x0) for n, r in enumerate(rows)}


Box = tuple[float, float, float, float]


def _width(ch: str) -> float:
    """字の幅の見積もり（半角は全角の半分）。語の中のどこに字があるかを割り出すのに使う。"""
    return 0.5 if ord(ch) < 0x2E80 or 0xFF61 <= ord(ch) <= 0xFF9F else 1.0


def _char_box(w: Word, first: int, last: int) -> Box:
    """語の中の first〜last 文字目（元の字の番号）の四角。字の幅で割り振って見積もる。"""
    widths = [_width(c) for c in w.text] or [1.0]
    total = sum(widths)
    left = w.x0 + (w.x1 - w.x0) * sum(widths[:first]) / total
    right = w.x0 + (w.x1 - w.x0) * sum(widths[: last + 1]) / total
    return (left - 1, w.y0 - 2, right + 1, w.y1 + 2)


def _seq(words: list[Word], order: list[int]) -> tuple[str, list[tuple[int, int]]]:
    """語を order の順につないだ探すための文字と、1字ごとの（語の番号, 語の中の元の字の番号）。"""
    text, where = "", []
    for i in order:
        for j, ch in enumerate(words[i].text):
            n = norm(ch)
            text += n
            where += [(i, j)] * len(n)
    return text, where


def _boxes(words: list[Word], chars: list[tuple[int, int]]) -> list[Box]:
    """当たった字の四角を、見た目の行ごとに1つにまとめる。"""
    by_word: dict[int, list[int]] = {}
    for i, j in chars:
        by_word.setdefault(i, []).append(j)
    out = []
    for idx in _lines(words).values():
        parts = [_char_box(words[i], min(by_word[i]), max(by_word[i])) for i in idx if i in by_word]
        if parts:
            out.append(
                (
                    min(x[0] for x in parts),
                    min(x[1] for x in parts),
                    max(x[2] for x in parts),
                    max(x[3] for x in parts),
                )
            )
    return out


def _pick_value(text: str, value: str, label: str) -> tuple[int, int] | None:
    """text の中の数字の場所。同じ数字が2つ以上あれば、ラベルの「」の言葉のすぐあとのものを選ぶ。"""
    v = norm(value)
    hits = [m.start() for m in re.finditer(re.escape(v), text)] if v else []
    if not hits:
        return None
    terms = [norm(t) for t in re.findall(r"「([^」]+)」", label or "")]
    best = None
    for t in terms:
        for m in re.finditer(re.escape(t), text) if t else []:
            after = [h for h in hits if h >= m.end()]
            if after and (best is None or after[0] - m.end() < best[1]):
                best = (after[0], after[0] - m.end())
    start = best[0] if best else hits[0]
    return start, start + len(v)


def highlight(
    words: list[Word], evidence: str, value: str, label: str = ""
) -> tuple[list[Box], list[Box]]:
    """印を付ける四角：（黄色で塗る所, 赤で囲む数字）。

    1. 写した行がそのままあれば、その字だけを塗る（語の途中で始まり・終わる行も字の幅で割り出す）
    2. 無ければ（表の行など）、写した行の数字をいちばん多く含み、統計の数字も含む見た目の行を選び、
       写した数字を含む語だけを塗る（同じ行の別の表を塗らない）
    数字（value）は、塗った所の中で赤く囲む。同じ数字が2つあれば、ラベルの「」の言葉のあとのもの。
    """
    flow, flow_at = _seq(words, list(range(len(words))))
    target = norm(clean_evidence(evidence))
    span: list[tuple[int, int]] = []
    span_text = ""
    if len(target) >= MIN_CHUNK and (at := flow.find(target)) >= 0:
        span, span_text = flow_at[at : at + len(target)], target
    else:
        # 数字は空白を残したまま取り出す（空白を取ると「8.2 33.4」が「8.233.4」になる）
        nums = [
            n.replace(",", "")
            for n in NUMBER.findall(unicodedata.normalize("NFKC", clean_evidence(evidence)))
        ]
        v = norm(value).replace(",", "")
        rows = _lines(words)
        # 統計の数字がページに同じ形で無いとき（「1時間54分」が表では「1.54」）は数字の多さで選ぶ
        need_value = bool(v) and v in flow.replace(",", "")
        best, best_score = None, 0
        for line, idx in rows.items():
            line_text = "".join(norm(words[i].text) for i in idx).replace(",", "")
            if need_value and v not in line_text:
                continue
            score = sum(1 for n in set(nums) if n in line_text)
            if score > best_score:
                best, best_score = line, score
        least = min(2, len(set(nums))) if need_value else min(3, len(set(nums)))
        if best is not None and best_score >= max(1, least):
            keep = [
                i
                for i in rows[best]
                if any(n in norm(words[i].text).replace(",", "") for n in set(nums))
            ]
            # 数字の語のあいだにある語（同じ表の列）も含め、行の左右の別の表は含めない
            xs = sorted(words[i].x0 for i in keep)
            inside = [i for i in rows[best] if xs and xs[0] <= words[i].x0 <= xs[-1]]
            label_words = [
                i
                for i in rows[best]
                if i not in inside and xs and words[i].x1 <= xs[0] and words[i].x1 >= xs[0] - 200
            ]
            chosen = sorted(set(inside) | set(label_words[-1:]), key=lambda i: words[i].x0)
            span_text, span = _seq(words, chosen)
    if not span:
        return [], []
    fills = _boxes(words, span)
    hit = _pick_value(span_text, str(value), label)
    marks = _boxes(words, span[hit[0] : hit[1]]) if hit else []
    return fills, marks


def render(
    pdf: Path,
    page: int,
    rects: list[tuple[float, float, float, float]],
    size: tuple[float, float],
    dest: Path,
    marks: list[tuple[float, float, float, float]] | None = None,
) -> None:
    """ページを画像にし、四角に印を付けて、前後を切り出す（JPEG）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    png = dest.with_suffix(".page")
    subprocess.run(
        [
            "pdftoppm",
            "-f",
            str(page),
            "-l",
            str(page),
            "-r",
            str(DPI),
            "-png",
            "-singlefile",
            str(pdf),
            str(png),
        ],
        check=True,
        capture_output=True,
    )
    src = Path(f"{png}.png")  # pdftoppm -singlefile は名前の後ろに .png を付ける
    scale = DPI / 72
    height = int(size[1] * scale)
    filters = []
    for x0, y0, x1, y1 in rects:
        x, y = int(x0 * scale), int(y0 * scale)
        w, h = max(4, int((x1 - x0) * scale)), max(4, int((y1 - y0) * scale))
        filters.append(f"drawbox=x={x}:y={y}:w={w}:h={h}:color=yellow@0.45:t=fill")
    for x0, y0, x1, y1 in marks or rects:
        x, y = int(x0 * scale), int(y0 * scale)
        w, h = max(4, int((x1 - x0) * scale)), max(4, int((y1 - y0) * scale))
        filters.append(f"drawbox=x={x - 3}:y={y - 3}:w={w + 6}:h={h + 6}:color=red@0.85:t=3")
    if rects:
        # 上は広く残す（表の題と列の見出しが上にあるため）。下は少し
        top = max(0, int(min(r[1] for r in rects) * scale) - MARGIN_ABOVE)
        bottom = min(height, int(max(r[3] for r in rects) * scale) + MARGIN_BELOW)
        filters.append(f"crop=iw:{bottom - top}:0:{top}")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(src),
            "-vf",
            ",".join(filters) or "null",
            "-q:v",
            "4",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    src.unlink(missing_ok=True)


def fragment_link(url: str, text: str) -> str:
    """文の断片のリンク（開くとその文に飛び、印が付く。Chrome・Safari など）。"""
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t:
        return url
    words = t.split(" ")
    if len(words) > 1:
        t = " ".join(words[:8])
    else:
        t = t[:20]
    return f"{url.split('#')[0]}#:~:text={quote(t, safe='')}"


def _cache_name(url: str, suffix: str) -> str:
    return f"sources/{hashlib.sha256(url.encode()).hexdigest()[:24]}{suffix}"


def locate_stat(
    d: dict[str, Any],
    fetch: Callable[[str, str], Path],
    img_dir: Path,
) -> str:
    """統計の1行の該当ページを探して、d に page・img・jump・located を書く。戻り値は見つけ方。"""
    url = d.get("doc_url") or d["url"]
    d.pop("img", None)
    d.pop("page", None)
    path = fetch(url, _cache_name(url, ".bin"))
    head = path.read_bytes()[:5]
    if head != b"%PDF-":
        text = article_text(path.read_text("utf-8", "replace"))
        span = original_span(text, d.get("evidence", ""))
        if span:
            d["jump"] = f"{url.split('#')[0]}#:~:text={quote(span, safe='')}"
            d["located"] = "html"
            return "html"
        d["jump"] = url
        d["located"] = "none"
        return "none"
    pages = pdf_pages(path)
    hit = find_page(pages, d.get("evidence", ""), str(d.get("value", "")))
    if hit is None and str(d.get("page_hint") or "").isdigit():
        # 文字を取り出せない PDF（画像の字など）は、調べ役が見たページを、印なしで出す
        hit = (int(d["page_hint"]), "hint")
    if hit is None:
        d["jump"] = url
        d["located"] = "none"
        return "none"
    page, how = hit
    words, width, height = page_words(path, page)
    rects, marks = highlight(
        words, d.get("evidence", ""), str(d.get("value", "")), d.get("label", "")
    )
    name = f"{d['stat_id']}.jpg"
    render(path, page, rects, (width, height), img_dir / name, marks)
    d["page"] = page
    d["img"] = f"img/{name}"
    d["jump"] = f"{url.split('#')[0]}#page={page}"
    d["located"] = how if rects else "page"
    return d["located"]


def article_text(raw: str) -> str:
    """論文のページ（HTML か JATS の XML）の本文の文字（タグを外し、空白を1つに）。"""
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))


def check_passages(d: dict[str, Any], fetch: Callable[[str, str], Path]) -> list[str]:
    """研究の結果の文が、論文の本文にそのままあるかを確かめる。無い文は外して、その文を返す。"""
    src = d.get("fulltext_url") or d.get("landing_url")
    if not src or not d.get("passages"):
        return []
    path = fetch(src, _cache_name(src, ".txt"))
    body = norm(article_text(path.read_text("utf-8", "replace")))
    kept, dropped = [], []
    for p in d["passages"]:
        (kept if norm(p.get("text", "")) in body else dropped).append(p)
    d["passages"] = kept
    if d.get("landing_url"):
        frag = d.get("fragment") or (kept[0]["text"] if kept else "")
        d["jump"] = fragment_link(d["landing_url"], frag) if kept else d["landing_url"]
    return [p.get("text", "")[:60] for p in dropped]
