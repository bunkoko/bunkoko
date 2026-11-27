"""e-Stat のファイル（CSV・xlsx・xls）から年ごとの数を読む（src/koten/stats/files.py）。

e-Stat の統計表は新しい年が Excel・CSV でしか出ないことがある。ファイルの種類は
拡張子でなく中身の先頭で決める（net.py が拡張子なしで置くため）。
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from pathlib import Path

import openpyxl
import xlrd

from koten.stats.series import (
    Point,
    Series,
    SeriesError,
    parse_period_name,
    part_problems,
    strip_period_notes,
    to_decimal,
)

FILE_URL = "https://www.e-stat.go.jp/stat-search/file-download?statInfId={sid}&fileKind={kind}"

_XLSX_MAGIC = b"PK\x03\x04"
_XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # OLE2（古い Excel）
_REF_RE = re.compile(r"([A-Za-z]+)([0-9]+)")
_BARE_YEAR_RE = re.compile(r"[0-9]{4}")
# number_format の字句：引用の文字・\x・_x（空白）・*x（埋め）・[..]（色・地域・条件）・1字
_FMT_TOK_RE = re.compile(r'"[^"]*"?|\\.|_.|\*.|\[[^\]]*\]?|.', re.S)
_SIGNIFICANT = 15  # 表計算ソフトが数として持つ桁（それより下は float の誤差）


class _BoolCell(str):
    """xlsx の真偽値のセル（値の欄に来たら断る）。"""


class _DateCell(str):
    """xlsx の日付のセル（値の欄・年の欄に来たら断る）。"""


class _ErrorCell(str):
    """.xls の Excel のエラーのセル（#N/A など。値の欄・年の欄に来たら断る）。"""


def _noise_free(v: int | float) -> Decimal:
    """セルの数を Decimal に。float は repr から読み、15 桁より下（float の誤差）を丸める。

    0.1+0.2 は 0.3、39.449999999999996 は 39.45（表計算ソフトが見せる数と同じ）。"""
    if not isinstance(v, float):
        return Decimal(v)
    d = Decimal(repr(v))
    if d and len(d.as_tuple().digits) > _SIGNIFICANT:
        d = d.quantize(Decimal(1).scaleb(d.adjusted() - _SIGNIFICANT + 1), rounding=ROUND_HALF_EVEN)
    return d


def _general(d: Decimal) -> str:
    """General の見せ方：整数なら整数、そうでなければ必要な桁だけ（桁区切りなし）。"""
    if d == d.to_integral_value():
        return str(int(d))
    return format(d.normalize(), "f")


def _sections(fmt: str) -> list[list[str]]:
    """number_format を ; で区画に分ける（正;負;0;文字）。区画は字句の並び。"""
    secs: list[list[str]] = [[]]
    for tok in _FMT_TOK_RE.findall(fmt):
        if tok == ";":
            secs.append([])
        else:
            secs[-1].append(tok)
    return secs


def _section_kind(toks: list[str]) -> str:
    """number（数の桁 0・# がある）・text（文字だけ）・general（General・@・決まりの外）。"""
    plain = "".join(t for t in toks if len(t) == 1)  # 引用・\x・_x・*x・[..] を除いた字
    if re.search(r"(?i)general", plain) or "@" in plain:
        return "general"
    if re.search(r"[Ee][+-]", plain) or "/" in plain or re.search(r"[A-Za-z]", plain):
        return "general"  # 指数・分数・日付などは決まりの外
    return "number" if any(t in ("0", "#") for t in toks) else "text"


def _literal(toks: list[str]) -> str:
    """文字だけの区画が見せる文字（引用を外し、\\x は x、_x・*x・[..]・? は外す）。"""
    out = ""
    for t in toks:
        if t.startswith('"'):
            out += t[1:].removesuffix('"')
        elif t.startswith("\\"):
            out += t[1:]
        elif t[0] not in "_*[?":
            out += t
    return out.strip()


def _number_text(d: Decimal, toks: list[str], sign: str) -> str:
    """数の区画で、正の数 d を見せる文字（桁区切りは付けない。符号は sign）。"""
    pct = toks.count("%")
    digits = [i for i, t in enumerate(toks) if t in ("0", "#", "?")]
    point = next((i for i, t in enumerate(toks) if t == "."), None)
    after = [toks[i] for i in digits if point is not None and i > point]
    decimals = len(after)  # 0・#・? の桁。丸めはこの桁まで
    required = after.count("0")  # 0 の桁だけは 0 でも出す。# と ? の桁は末尾の 0 を落とす（R43）
    commas = 0  # 数の部分の終わりのカンマ：1つごとに 1000 で割る
    for t in toks[digits[-1] + 1 :]:
        if t != ",":
            break
        commas += 1
    value = d * Decimal(100) ** pct / Decimal(1000) ** commas
    shown = value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    if shown == 0:
        if "0" not in toks:
            return ""  # 「#」だけの書式の 0 は、Excel は何も出さない
        return _trim_optional(f"{abs(shown):f}", decimals, required)
    return sign + _trim_optional(f"{shown:f}", decimals, required)


def _trim_optional(text: str, decimals: int, required: int) -> str:
    """# と ? の小数の桁の末尾の 0（と、小数が残らなければ点）を落とす。"""
    if decimals <= required:
        return text
    head, _, frac = text.partition(".")
    frac = frac.rstrip("0").ljust(required, "0")
    return f"{head}.{frac}" if frac else head


def _render_number(v: int | float, fmt: str) -> str:
    """数のセルを、Excel がその number_format で見せるとおりの文字にする（R31）。

    正;負;0 の区画から当てはまる区画を選ぶ。0 の区画が文字だけ（"-" など）ならその文字
    （数にしない。印になる）。小数の桁はその区画から、丸めは 0 から遠いほうへ、数の部分の
    終わりのカンマ1つごとに 1000 で割り、% は 100 倍。引用の文字・\\x・[..] は数に入れない。
    負の数は、負の区画の文字（△ など）でなく - を付ける（数として読めるように）。
    条件つき・指数・分数・日付などの書式は General の見せ方にする。
    区画が 4 つ未満で最後の区画に @ があれば、それは文字の区画で、数には使わない（R43。
    「0.0;@」は負の数にも最初の区画）。小数の # と ? の桁は、末尾の 0 を出さない（「0.0#」の
    1.5 は 1.5）。"""
    d = _noise_free(v)
    fmt = fmt or "General"
    if any(t.startswith("[") and t[1:2] in ("<", ">", "=") for t in _FMT_TOK_RE.findall(fmt)):
        return _general(d)
    secs = _sections(fmt)
    if 2 <= len(secs) <= 3 and "@" in "".join(t for t in secs[-1] if len(t) == 1):
        secs = secs[:-1]  # 区画が 4 つ未満で、最後が @ の区画：文字の区画（数には使わない。R43）
    if d == 0 and len(secs) >= 3:
        toks, sign = secs[2], ""
    elif d < 0 and len(secs) >= 2:
        toks, sign = secs[1], "-"
    else:
        toks, sign = secs[0], ("-" if d < 0 else "")
    kind = _section_kind(toks)
    if kind == "text":
        return _literal(toks)
    if kind == "general":
        return _general(d)
    try:
        with localcontext() as ctx:
            ctx.prec = 60  # 大きな数を丸めても桁があふれない
            return _number_text(abs(d), toks, sign)
    except InvalidOperation:
        return _general(d)


def _render_cell(cell) -> str:
    v = cell.value
    if v is None:
        return ""
    if isinstance(v, bool):
        return _BoolCell(str(v))
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return _DateCell(str(v))
    if isinstance(v, (int, float)):
        return _render_number(v, cell.number_format or "General")
    return str(v).strip()


def _pad(rows: list[list[str]]) -> list[list[str]]:
    width = max((len(r) for r in rows), default=0)
    return [r + [""] * (width - len(r)) for r in rows]


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("cp932")
        except UnicodeDecodeError as e:
            raise SeriesError("CSV を utf-8 でも cp932 でも読めない") from e


_HTML_MARKS = (b"<!doctype", b"<html", b"<?xml", b"<head", b"<body", b"<meta", b"<title")


def file_problem(data: bytes) -> str | None:
    """e-Stat のファイル（CSV・xlsx・xls）として読めない中身なら、その理由。読めそうなら None。

    エラーやお知らせの HTML・PDF・中身の分からない（NUL を含む）もの・空のものを断る。
    .xls（OLE2 の先頭）は、xlrd がブックとして開けたときだけ通す
    （net.py の Fetcher.fetch の validate に渡し、前の良いファイルを上書きしない。F7）。"""
    if data.startswith(_XLSX_MAGIC):
        return None
    if data.startswith(_XLS_MAGIC):
        try:
            xlrd.open_workbook(
                file_contents=data, on_demand=True, logfile=io.StringIO(), verbosity=0
            ).release_resources()
        except Exception as e:  # noqa: BLE001 - xlrd は壊れ方ごとに別の例外を出す
            return f"Excel（.xls）として開けない（{type(e).__name__}：{e}）"
        return None
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if not head:
        return "中身が空のファイルが返った（CSV・xlsx・xls でない）"
    if head.startswith(b"%pdf"):
        return "PDF が返った（CSV・xlsx・xls でない。CSV か Excel の版を選ぶ）"
    if head.startswith(_HTML_MARKS) or b"<html" in head[:1024]:
        return "HTML（エラーやお知らせのページ）が返った（CSV・xlsx・xls でない）"
    if b"\x00" in data[:4096]:
        return "中身の分からないファイルが返った（CSV・xlsx・xls でない）"
    return None


def _render_xls_cell(book, cell) -> str:
    """.xls のセルを、xlsx の _render_cell と同じ文字にする。"""
    t = cell.ctype
    if t in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return ""
    if t == xlrd.XL_CELL_TEXT:
        return str(cell.value).strip()
    if t == xlrd.XL_CELL_BOOLEAN:
        return _BoolCell(str(bool(cell.value)))
    if t == xlrd.XL_CELL_DATE:
        try:
            return _DateCell(str(xlrd.xldate_as_datetime(cell.value, book.datemode)))
        except (xlrd.xldate.XLDateError, ValueError, OverflowError):
            return _DateCell(str(cell.value))
    if t == xlrd.XL_CELL_ERROR:
        return _ErrorCell(xlrd.error_text_from_code.get(cell.value, f"#ERR{cell.value}"))
    fmt = book.format_map[book.xf_list[cell.xf_index].format_key].format_str
    return _render_number(cell.value, fmt or "General")


def _xls_grid(data: bytes, sheet: str) -> list[list[str]]:
    if sheet == "":
        raise SeriesError(".xls は sheet（シートの名前）が要る")
    try:
        book = xlrd.open_workbook(
            file_contents=data, formatting_info=True, logfile=io.StringIO(), verbosity=0
        )
    except Exception as e:  # noqa: BLE001 - xlrd は壊れ方ごとに別の例外を出す
        raise SeriesError(f"Excel（.xls）として開けない（{type(e).__name__}：{e}）") from e
    try:
        if sheet not in book.sheet_names():
            raise SeriesError(
                f"シート {sheet!r} が無い。あるシート：{'・'.join(book.sheet_names())}"
            )
        ws = book.sheet_by_name(sheet)
        rows = [[_render_xls_cell(book, c) for c in ws.row(r)] for r in range(ws.nrows)]
    finally:
        book.release_resources()
    return _pad(rows)


def read_grid(path: Path, sheet: str) -> list[list[str]]:
    """ファイルを文字の表にする。xlsx・xls は sheet が要り、CSV は "" でなければならない。"""
    data = Path(path).read_bytes()
    return _grid_from_bytes(data, sheet)


def _grid_from_bytes(data: bytes, sheet: str) -> list[list[str]]:
    if (problem := file_problem(data)) is not None:
        raise SeriesError(problem)
    if data.startswith(_XLSX_MAGIC):
        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as e:  # noqa: BLE001 - openpyxl は壊れ方ごとに別の例外を出す
            raise SeriesError(f"xlsx を開けない：{e}") from e
        try:
            if sheet not in wb.sheetnames:
                raise SeriesError(
                    f"シート {sheet!r} が無い。あるシート：{'・'.join(wb.sheetnames)}"
                )
            rows = [[_render_cell(c) for c in row] for row in wb[sheet].iter_rows()]
        finally:
            wb.close()
        return _pad(rows)
    if data.startswith(_XLS_MAGIC):
        return _xls_grid(data, sheet)
    if sheet != "":
        raise SeriesError(f'CSV に sheet は指定できない（"" にする）：{sheet!r}')
    try:
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(_decode(data), newline=""))]
    except csv.Error as e:
        raise SeriesError(f"CSV を読めない：{e}") from e
    return _pad(rows)


def _col(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _ref(ref: str) -> tuple[int, int]:
    m = _REF_RE.fullmatch(ref.strip())
    if not m or int(m[2]) < 1:
        raise SeriesError(f"セルの指定を読めない：{ref!r}")
    return int(m[2]) - 1, _col(m[1])


def cell_pos(ref: str) -> tuple[int, int]:
    """ "D12" → (行, 列)（0 始まり）。範囲は渡さない。"""
    return _ref(ref)


def cells(grid: list[list[str]], ref: str) -> list[str]:
    """ "A3" か 1 方向の範囲 "D12:R12"・"D5:D20" のセルの文字。表の外は ""。"""
    a, _, b = ref.partition(":")
    r1, c1 = _ref(a)
    r2, c2 = _ref(b) if b else (r1, c1)
    if r1 != r2 and c1 != c2:
        raise SeriesError(f"2 次元の範囲は読まない（行か列の一方向だけ）：{ref!r}")
    if r1 > r2 or c1 > c2:
        raise SeriesError(f"範囲の向きが逆：{ref!r}")
    out = []
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            row = grid[r] if r < len(grid) else []
            out.append(row[c] if c < len(row) else "")
    return out


def _period(text: str, kind: str) -> str:
    if isinstance(text, _DateCell):
        raise SeriesError(f"年の欄が日付のセルになっている（{text}）。年の文字か数のセルを指す")
    if isinstance(text, _BoolCell):
        raise SeriesError(f"年の欄が真偽値のセルになっている（{text}）")
    if isinstance(text, _ErrorCell):
        raise SeriesError(f"年の欄が Excel のエラー（{text}）")
    t = strip_period_notes(text)  # 終わりの注の印（1)・※ など）と改行は外す（F10）
    if _BARE_YEAR_RE.fullmatch(t):
        return t
    year, k = parse_period_name(t)
    if k != kind:
        raise SeriesError(f"時間 {text!r} は{k}だが、系列は{kind}")
    return year


def _require(spec: dict, idx: int) -> None:
    """部分の欄の形を、取る前に確かめる（series_problems と同じ決まり。R30）。"""
    n = idx + 1
    if not isinstance(spec, dict):
        raise SeriesError(f"{n} 番目の part がオブジェクトでない")
    if problems := part_problems(spec):
        raise SeriesError("、".join(f"{n} 番目の part {m}" for m in problems))


def _part_points(s: Series, idx: int, spec: dict, fetch, grids: dict | None) -> list[Point]:
    n = idx + 1
    _require(spec, idx)
    sid, kind = str(spec["statInfId"]).strip(), str(spec["fileKind"]).strip()
    path, fetched_at = fetch(FILE_URL.format(sid=sid, kind=kind), f"files/{sid}_{kind}")
    data = Path(path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    sheet = str(spec.get("sheet", ""))
    try:
        if grids is None:
            grid = _grid_from_bytes(data, sheet)
        else:
            # 同じ（中身・シート）は 1 回だけ読む（R40）
            if (sha, sheet) not in grids:
                grids[(sha, sheet)] = _grid_from_bytes(data, sheet)
            grid = grids[(sha, sheet)]
        return _points_from_grid(s, idx, spec, grid, sha, fetched_at)
    except SeriesError as e:
        if str(e).startswith(f"{n} 番目"):
            raise
        raise SeriesError(f"{n} 番目の part（{sid}_{kind}）：{e}") from e


def _points_from_grid(s, idx, spec, grid, sha, fetched_at) -> list[Point]:
    n = idx + 1
    vals = cells(grid, spec["values"])
    for v in vals:
        if isinstance(v, _BoolCell):
            raise SeriesError(f"値のセルが真偽値（{v}）。数のセルを指す")
        if isinstance(v, _DateCell):
            raise SeriesError(f"値のセルが日付のセル（{v}）。数のセルを指す")
        if isinstance(v, _ErrorCell):
            raise SeriesError(f"値のセルが Excel のエラー（{v}）。数のセルを指す")
    if spec.get("periods_cells"):
        periods = [_period(t, s.period_kind) for t in cells(grid, spec["periods_cells"])]
    else:
        periods = [str(p) for p in spec.get("periods", [])]
        for p in periods:
            if not _BARE_YEAR_RE.fullmatch(p):
                raise SeriesError(f"periods は半角 4 桁の年：{p!r}")
    if len(periods) != len(vals):
        raise SeriesError(
            f"{n} 番目の part の時間の数（{len(periods)}）と値の数（{len(vals)}）が違う"
        )
    # 見出しは、セルの中の空白の並び（改行も）を 1 つの空白にする（R41）
    label = "／".join(
        t
        for t in (
            re.sub(r"\s+", " ", x).strip()
            for ref in spec.get("label_cells") or []
            for x in cells(grid, ref)
        )
        if t
    )
    unit = cells(grid, spec["unit_cell"])[0] if spec.get("unit_cell") else ""
    out = []
    for period, text in zip(periods, vals, strict=True):
        text = text.strip()
        if not text:
            continue
        num = to_decimal(text) is not None
        out.append(
            Point(
                s.series_id,
                period,
                text if num else "",
                "" if num else text,
                str(idx),
                label,
                unit,
                fetched_at,
                sha,
            )
        )
    return out


def file_points(
    s: Series,
    fetch: Callable[[str, str], tuple[Path, str]],
    grids: dict | None = None,
) -> list[Point]:
    """estat_file の系列の点。fetch(url, rel_path) は (ファイル, 取得日時) を返す。

    grids を渡すと、同じ（中身・シート）の表を 1 回だけ読んで使い回す（R40）。"""
    seen: dict[str, int] = {}
    out: list[Point] = []
    parts = s.selector.get("parts") if isinstance(s.selector, dict) else None
    if not isinstance(parts, list):
        raise SeriesError(f"selector の parts が並びでない（{parts!r}）")
    for idx, spec in enumerate(parts):
        for p in _part_points(s, idx, spec, fetch, grids):
            if p.period in seen:
                a, b = seen[p.period] + 1, idx + 1
                raise SeriesError(f"{p.period} 年が {a} 番目と {b} 番目の part で重なっている")
            seen[p.period] = idx
            out.append(p)
    return sorted(out, key=lambda p: p.period)
