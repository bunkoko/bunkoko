"""統計の「系列」（複数年の数の並び）と「点」（1年ぶんの数）の形と決まり（段1）。

DB もネットワークも使わない、形と決まりだけの層。
- config/stat_series.csv：系列の台帳（何を・どの表から・どう確かめたか）
- config/stat_points.csv：取ってきた点（年ごとの数。数は元の文字のまま）

数は元の文字のまま持ち（`1,234` のまま）、数として読むのは value_num だけ。
特殊文字（`-`・`…`・`x` など）は mark に残し、数にしない・0 にしない。
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from koten.db import AI_CHECKER, HUMAN_CHECKER, TRENDS
from koten.worries.master import keyword_block

SERIES_COLUMNS = [
    "series_id", "themes", "label", "unit", "survey", "publisher", "source_kind",
    "period_kind", "selector", "table_title", "url", "latest_known", "breaks",
    "trend", "trend_from", "trend_to", "trend_note", "status", "checked_by",
    "checked_at", "checked_sha", "where", "note",
]  # fmt: skip
POINTS_COLUMNS = [
    "series_id", "period", "value", "mark", "part", "src_label", "src_unit",
    "fetched_at", "src_sha",
]  # fmt: skip
SERIES_FILE = "stat_series.csv"
POINTS_FILE = "stat_points.csv"
META_DIR = "docs/estat"  # REPO_ROOT から。config_dir があるときは meta_dir_for を使う

_ID_RE = re.compile(r"[a-z0-9][a-z0-9_]*")
_YEAR_RE = re.compile(r"[0-9]{4}")  # \d は全角などの数字にも当たるので使わない
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# 半角の数字だけ（statInfId・fileKind・statsDataId・点の part の番号）
DIGITS_RE = re.compile(r"[0-9]+")
# label に年を入れない（年度も「年」に当たる）。NFKC で全角数字を半角にしてから当てる。
# 4桁の数だけ（従業員2000人以上）は許す。漢数字の年（二〇二〇年・平成二十五年）も断る。
_KANJI_NUM = "〇一二三四五六七八九"
_LABEL_YEAR_RE = re.compile(
    r"(?<![0-9])(?:19|20)[0-9]{2}\s*年"
    rf"|[{_KANJI_NUM}]{{4}}\s*年"
    rf"|(?:明治|大正|昭和|平成|令和)\s*[0-9元{_KANJI_NUM}十百]+\s*年"
)
_ERA_BASE = {"明治": 1868, "大正": 1912, "昭和": 1926, "平成": 1989, "令和": 2019}
_ERA_LAST = {"明治": 45, "大正": 15, "昭和": 64, "平成": 31}  # 令和は終わりが無い
_ERA_YEAR_RE = re.compile(r"(?P<era>明治|大正|昭和|平成|令和)\s*(?P<en>[0-9]+|元)\s*年(?P<fy>度?)")
_AD_YEAR_RE = re.compile(r"(?<![0-9])(?P<ad>[0-9]{4})\s*年(?P<fy>度?)")


class SeriesError(ValueError):
    """系列・点の読み書き・年の読み取りの失敗。"""


def norm_unit(text: str) -> str:
    """単位の比べ用：NFKC・％→%・前後の空白を取る（e-Stat と台帳で同じ比べ方）。"""
    return unicodedata.normalize("NFKC", text or "").replace("％", "%").strip()


def norm_name(text: str) -> str:
    """表の名前（調査名・作成機関・表の題）の比べ用：NFKC のあと、空白（全角の空白・改行も）を
    1つの空白にまとめ、前後を取る。書くときの形は変えない（比べるときだけ）。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip()


# e-Stat の API の URL（appId=…）や JSON の appId の鍵。台帳のどの欄にも入れない（値は出さない）
_APPID_RE = re.compile(r'(?i)appid\s*=|"appid"\s*:')


@dataclass(frozen=True)
class Break:
    """時系列の区切り。before の年から前とは比べられない。"""

    before: str
    note: str


@dataclass(frozen=True)
class Series:
    series_id: str
    themes: tuple[str, ...]
    label: str
    unit: str
    survey: str
    publisher: str
    source_kind: str
    period_kind: str
    selector: dict
    table_title: str
    url: str
    latest_known: str
    breaks: tuple[Break, ...]
    trend: str
    trend_from: str
    trend_to: str
    trend_note: str
    status: str
    checked_by: str
    checked_at: str
    checked_sha: str
    where: str
    note: str


@dataclass(frozen=True)
class Point:
    series_id: str
    period: str
    value: str
    mark: str
    part: str
    src_label: str
    src_unit: str
    fetched_at: str
    src_sha: str


# --- ファイルの場所・読み書き -------------------------------------------------


def meta_dir_for(config_dir: Path) -> Path:
    """config_dir から表の情報（docs/estat）の場所を決める。"""
    return Path(config_dir).parent / "docs" / "estat"


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [{k: (v or "") for k, v in r.items() if k is not None} for r in csv.DictReader(f)]


def read_series_rows(config_dir: Path) -> list[dict[str, str]]:
    return _read_rows(Path(config_dir) / SERIES_FILE)


def read_point_rows(config_dir: Path) -> list[dict[str, str]]:
    return _read_rows(Path(config_dir) / POINTS_FILE)


def valid_series_id(text: str) -> bool:
    """series_id として使える形か（docs/estat/<series_id>.json の名前にもなる）。"""
    return bool(_ID_RE.fullmatch(text))


def load_meta(meta_dir: Path, series_id: str) -> dict | None:
    """docs/estat/<series_id>.json（その系列が選んだ分類だけの表の情報）。無ければ None。"""
    if not valid_series_id(series_id):
        raise SeriesError(f"series_id {series_id!r} は表の情報のファイル名にできない")
    path = Path(meta_dir) / f"{series_id}.json"
    if not path.exists():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SeriesError(f"{path} を読めない（JSON が壊れている）：{type(e).__name__}") from e
    if not isinstance(meta, dict):
        raise SeriesError(f"{path} は JSON のオブジェクトでない")
    return meta


def csv_text(columns: list[str], rows: list[dict[str, str]]) -> str:
    """CSV の文（改行は \\n。列に無い鍵は書かない）。"""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") or "" for c in columns})
    return buf.getvalue()


def write_text_atomic(path: Path, text: str) -> None:
    """同じ場所の一時ファイルに書いてから置き換える（途中で止まっても前のファイルが残る）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_series_rows(config_dir: Path, rows: list[dict[str, str]]) -> None:
    """stat_series.csv を書き直す（列は SERIES_COLUMNS。一時ファイルから置き換える）。"""
    write_text_atomic(Path(config_dir) / SERIES_FILE, csv_text(SERIES_COLUMNS, rows))


def write_points(config_dir: Path, series_ids: set[str], new: list[Point]) -> None:
    """series_ids の系列の行だけを new で入れ替えて書く。ほかの系列の行はそのまま。

    並びは (series_id, period)。ファイルが無ければ作る。
    """
    path = Path(config_dir) / POINTS_FILE
    stray = sorted({p.series_id for p in new} - set(series_ids))
    if stray:
        raise SeriesError(f"入れ替える系列に入っていない series_id の点がある：{'・'.join(stray)}")
    kept = [r for r in read_point_rows(config_dir) if r.get("series_id", "") not in series_ids]
    rows = kept + [{c: getattr(p, c) for c in POINTS_COLUMNS} for p in new]
    rows.sort(key=lambda r: (r["series_id"], r["period"]))
    write_text_atomic(path, csv_text(POINTS_COLUMNS, rows))


# --- 決まりの確かめ -----------------------------------------------------------


def _is_date(text: str) -> bool:
    if not _DATE_RE.fullmatch(text):
        return False
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


_CELL_RE = re.compile(r"([A-Za-z]+)([1-9][0-9]*)")


def _cell_of(text: str) -> tuple[int, int] | None:
    m = _CELL_RE.fullmatch(text.strip())
    if not m:
        return None
    col = 0
    for ch in m[1].upper():
        col = col * 26 + ord(ch) - 64
    return int(m[2]) - 1, col - 1


def cell_range(ref: Any) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """ "D12" か 1 方向の範囲 "D12:R12"・"D5:D20" → ((行, 列), (行, 列))（0 始まり）。

    文字でない・セルの形でない・2 次元・向きが逆の範囲は None。"""
    if not isinstance(ref, str):
        return None
    a, sep, b = ref.partition(":")
    start, end = _cell_of(a), _cell_of(b) if sep else _cell_of(a)
    if start is None or end is None:
        return None
    if start[0] != end[0] and start[1] != end[1]:
        return None
    if start[0] > end[0] or start[1] > end[1]:
        return None
    return start, end


def _lined_up(values: tuple, periods: tuple) -> bool:
    """年のセルが値のセルと並んでいるか（行の範囲は同じ列、列の範囲は同じ行。F8）。"""
    (r1, c1), (r2, c2) = values
    (p1, q1), (p2, q2) = periods
    if (r1, c1) == (r2, c2):  # 値が1つ：年のセルも1つで、同じ列か同じ行
        return (p1, q1) == (p2, q2) and (q1 == c1 or p1 == r1)
    if r1 == r2:  # 行の範囲
        return p1 == p2 and (q1, q2) == (c1, c2)
    return q1 == q2 and (p1, p2) == (r1, r2)  # 列の範囲


def part_problems(part: dict) -> list[str]:
    """estat_file の部分1つの欄の形（JSON の型・セルの形・年のセルの並び）の問題（R30・F8）。

    文は「に … が無い」「の … は …」の形（前に部分の名前を付けて使う）。書いていない欄と
    空の文字は無いのと同じ。書いた欄が null や違う型なら問題。"""
    out: list[str] = []

    def given(k: str) -> bool:
        return k in part and part[k] != ""

    for k in ("statInfId", "fileKind"):
        v = part.get(k)
        if not given(k):
            out.append(f"に {k} が無い")
        elif not isinstance(v, str):
            out.append(f"の {k} は半角の数字だけの文字列（{v!r}）")
        elif not DIGITS_RE.fullmatch(v.strip()):
            out.append(f"の {k} は半角の数字だけ（{v!r}）")
    if "sheet" not in part:
        out.append('に sheet が無い（CSV は ""）')
    elif not isinstance(part["sheet"], str):
        out.append(f'の sheet は文字（CSV は ""）（{part["sheet"]!r}）')
    values = cell_range(part.get("values"))
    if not given("values"):
        out.append("に values が無い")
    elif values is None:
        out.append(f'の values はセルの文字（例 "D12:R12"。行か列の一方向）（{part["values"]!r}）')
    periods = None
    if given("periods_cells"):
        periods = cell_range(part["periods_cells"])
        if periods is None:
            out.append(f'の periods_cells はセルの文字（例 "D5:R5"）（{part["periods_cells"]!r}）')
        elif values is not None and not _lined_up(values, periods):
            out.append(
                f"の periods_cells（{part['periods_cells']!r}）が values（{part['values']!r}）と"
                "並んでいない（行の範囲なら同じ列、列の範囲なら同じ行。ずれるなら periods で書く）"
            )
    if "periods" in part:
        ps = part["periods"]
        if not (
            isinstance(ps, list) and all(isinstance(x, str) and _YEAR_RE.fullmatch(x) for x in ps)
        ):
            out.append(f'の periods は半角 4 桁の年の文字の並び（例 ["2021"]）（{ps!r}）')
    if not (given("periods_cells") or (isinstance(part.get("periods"), list) and part["periods"])):
        out.append("に periods_cells か periods が無い")
    if "label_cells" in part:
        lc = part["label_cells"]
        if not (isinstance(lc, list) and all(cell_range(x) is not None for x in lc)):
            out.append(f'の label_cells は見出しのセルの並び（例 ["A12", "D6"]）（{lc!r}）')
    for k in ("unit_cell", "updated"):
        if k in part and not isinstance(part[k], str):
            out.append(f"の {k} は文字（{part[k]!r}）")
    if isinstance(part.get("unit_cell"), str) and part["unit_cell"].strip():
        if cell_range(part["unit_cell"]) is None:
            out.append(f'の unit_cell はセルの文字（例 "A3"）（{part["unit_cell"]!r}）')
    return out


def _selector_problems(kind: str, sel: Any) -> list[str]:
    if isinstance(sel, dict) and not sel:
        return ["selector が空（{}）"]
    if not isinstance(sel, dict):
        return ["selector が JSON のオブジェクトになっていない"]
    out: list[str] = []
    if kind == "estat_api":
        sdid = sel.get("statsDataId")
        if not isinstance(sdid, str) or not DIGITS_RE.fullmatch(sdid):
            out.append("selector の statsDataId は半角数字の文字列（先頭の 0 を落とさない）にする")
        cds = [k for k in sel if k.startswith("cd")]
        if not cds:
            out.append("selector に cd で始まる鍵が1つも無い")
        for k in cds:
            v = sel[k]
            if not isinstance(v, str) or not v.strip() or "," in v:
                out.append(f"selector の {k} は符号1つ（, を含まない）にする")
    elif kind == "estat_file":
        parts = sel.get("parts")
        if not isinstance(parts, list) or not parts:
            return ["selector の parts が1つも無い"]
        for i, part in enumerate(parts, 1):
            if not isinstance(part, dict):
                out.append(f"selector の parts[{i}] がオブジェクトでない")
                continue
            out += [f"selector の parts[{i}] {m}" for m in part_problems(part)]
    return out


def series_problems(
    series_rows: list[dict[str, str]],
    point_rows: list[dict[str, str]],
    *,
    themes: set[str],
    legacy_ids: set[str],
    blocked: dict[str, list[str]],
    metas: dict[str, dict],
) -> list[str]:
    """「何行目：何が」の文の一覧。空なら全部読める（読み込みは全部か無しか）。

    metas は series_id → 表の情報（docs/estat/<series_id>.json を load_meta が返したもの。
    表ごとでなく系列ごと：同じ表の別の選び方が上書きし合わない）。estat_api の系列は
    表の情報が無ければ問題、あれば survey=stat_name・publisher=gov_org・
    table_title=title が一致すること（NFKC のあと空白をまとめて比べる：norm_name）。
    estat_api の系列の unit は、点の src_unit（空でないもの）と同じ単位であること（norm_unit）。
    estat_file の系列は照らさない。どの欄にも appId=（API の URL）を入れない。
    """
    out: list[str] = []
    seen: dict[str, int] = {}
    ids: set[str] = set()
    api_unit: dict[str, str] = {}
    for n, r in enumerate(series_rows, 2):
        p = f"{SERIES_FILE} {n}行目："
        leak = [c for c, v in r.items() if c and _APPID_RE.search(str(v or ""))]
        if leak:
            # 値を文に出さないよう、この行のほかの確かめはしない
            out += [
                f"{p}{c} に appId が入っている（e-Stat の API の URL は貼らない。"
                "dbview?sid= の URL にする）"
                for c in leak
            ]
            continue
        sid = r.get("series_id", "").strip()
        if not _ID_RE.fullmatch(sid):
            out.append(f"{p}series_id は英小文字・数字・_ にする（{sid!r}）")
        elif sid in seen:
            out.append(f"{p}series_id {sid} が {seen[sid]}行目と重なっている")
        elif sid in legacy_ids:
            out.append(f"{p}series_id {sid} は stats.csv の stat_id と同じ名前")
        if sid:
            seen.setdefault(sid, n)
            ids.add(sid)
        th = [t.strip() for t in r.get("themes", "").split(";") if t.strip()]
        if not 1 <= len(th) <= 3:
            out.append(f"{p}themes は ; 区切りで1〜3個")
        if len(set(th)) != len(th):
            out.append(f"{p}themes に同じ語が2回ある")
        for t in th:
            if t not in themes:
                out.append(f"{p}themes の {t!r} は themes.yaml に無い")
        for col in ("label", "unit", "survey", "publisher", "url", "selector"):
            if not r.get(col, "").strip():
                out.append(f"{p}{col} が空")
        if r.get("url", "").strip() and not re.match(r"https?://", r["url"].strip()):
            out.append(f"{p}url は http(s) で始める")
        if _LABEL_YEAR_RE.search(unicodedata.normalize("NFKC", r.get("label", ""))):
            out.append(f"{p}label に年を入れない（年は点の側で持つ）")
        for col in ("label", "table_title", "survey"):
            hit = keyword_block(r.get(col, ""), blocked)
            if hit:
                out.append(f"{p}{col} が除外カテゴリ（{hit[0]}：{hit[1]}）に当たる")
        lk = r.get("latest_known", "").strip()
        if lk and not _YEAR_RE.fullmatch(lk):
            out.append(f"{p}latest_known は空か YYYY（半角）（{lk!r}）")
        kind = r.get("source_kind", "").strip()
        if kind not in ("estat_api", "estat_file"):
            out.append(f"{p}source_kind は estat_api か estat_file（{kind!r}）")
        if kind == "estat_api" and sid:
            api_unit.setdefault(sid, r.get("unit", ""))
        if r.get("period_kind", "").strip() not in ("年", "年度"):
            out.append(f"{p}period_kind は 年 か 年度（{r.get('period_kind', '')!r}）")
        if r.get("selector", "").strip():
            sel = _json_or_none(r["selector"])
            for m in _selector_problems(kind, sel):
                out.append(f"{p}{m}")
            sdid = sel.get("statsDataId") if isinstance(sel, dict) else None
            if kind == "estat_api" and isinstance(sdid, str) and sdid.strip():
                meta = metas.get(sid)
                if meta is None:
                    out.append(
                        f"{p}{sid} の表の情報（{META_DIR}/{sid}.json）が無い。"
                        "Mac で uv run koten stats meta を実行する"
                    )
                else:
                    for col, key in (
                        ("survey", "stat_name"),
                        ("publisher", "gov_org"),
                        ("table_title", "title"),
                    ):
                        mine = r.get(col, "").strip()
                        theirs = meta.get(key)
                        if not isinstance(theirs, str):
                            out.append(f"{p}表の情報の {key} が文字列でない（{theirs!r}）")
                        elif norm_name(mine) != norm_name(theirs):
                            out.append(
                                f"{p}{col} {mine!r} が表の情報の {key} {theirs.strip()!r} と違う"
                            )
                    if meta.get("stats_data_id") != sdid:
                        out.append(
                            f"{p}表の情報（{META_DIR}/{sid}.json）は別の表のもの"
                            f"（{meta.get('stats_data_id')!r}・台帳は {sdid!r}）。"
                            "uv run koten stats meta か stats fetch をやり直す"
                        )
                    chosen = meta.get("chosen")
                    have = (
                        sorted(str(c.get("code")) for c in chosen if isinstance(c, dict))
                        if isinstance(chosen, list)
                        else None
                    )
                    want = sorted(str(v) for k, v in sel.items() if k.startswith("cd"))
                    if have != want:
                        out.append(
                            f"{p}表の情報の選んだ項目の符号 {have} が"
                            f" selector の符号 {want} と違う。"
                            "uv run koten stats meta か stats fetch をやり直す"
                        )
                    for c in meta.get("chosen") or []:
                        if not isinstance(c, dict):
                            continue
                        for key in ("class", "item"):
                            hit = keyword_block(str(c.get(key) or ""), blocked)
                            if hit:
                                out.append(
                                    f"{p}選んだ分類の {key}（{c.get(key)}）が"
                                    f"除外カテゴリ（{hit[0]}：{hit[1]}）に当たる"
                                )
        if r.get("breaks", "").strip():
            br = _json_or_none(r["breaks"])
            if not isinstance(br, list):
                out.append(f'{p}breaks は [{{"before":"YYYY","note":"…"}}] の JSON')
            else:
                for b in br:
                    ok = isinstance(b, dict) and _YEAR_RE.fullmatch(str(b.get("before", "")))
                    if not ok or not str(b.get("note", "")).strip():
                        out.append(f"{p}breaks の各項目は before（YYYY）と空でない note が要る")
                        break
        trend = r.get("trend", "").strip()
        if trend:
            if trend not in TRENDS:
                out.append(f"{p}trend は {'・'.join(TRENDS)} か空")
            for col in ("trend_from", "trend_to"):
                if not _YEAR_RE.fullmatch(r.get(col, "").strip()):
                    out.append(f"{p}trend を書いたら {col}（YYYY）も要る")
            tf, tt = r.get("trend_from", "").strip(), r.get("trend_to", "").strip()
            if _YEAR_RE.fullmatch(tf) and _YEAR_RE.fullmatch(tt) and tf > tt:
                out.append(f"{p}trend_from が trend_to より後")
            if not r.get("trend_note", "").strip():
                out.append(f"{p}trend を書いたら trend_note も要る")
        if r.get("status", "").strip() not in ("", "保留"):
            out.append(f"{p}status は 保留 か空")
        by = r.get("checked_by", "").strip()
        if by not in ("", AI_CHECKER, HUMAN_CHECKER):
            out.append(f"{p}checked_by は空・{AI_CHECKER}・{HUMAN_CHECKER}")
        if by and not _is_date(r.get("checked_at", "").strip()):
            out.append(f"{p}checked_by を書いたら checked_at（YYYY-MM-DD、実在する日）も要る")

    pseen: set[tuple[str, str]] = set()
    bad_units: dict[str, list[str]] = {}
    for n, r in enumerate(point_rows, 2):
        p = f"{POINTS_FILE} {n}行目："
        sid, period = r.get("series_id", "").strip(), r.get("period", "").strip()
        unit = r.get("src_unit", "")
        if sid in api_unit and norm_unit(unit) and norm_unit(unit) != norm_unit(api_unit[sid]):
            got = bad_units.setdefault(sid, [])
            if unit.strip() not in got:
                got.append(unit.strip())
        if sid not in ids:
            out.append(f"{p}series_id {sid!r} が {SERIES_FILE} に無い")
        if not _YEAR_RE.fullmatch(period):
            out.append(f"{p}period は YYYY（{period!r}）")
        if (sid, period) in pseen:
            out.append(f"{p}({sid}, {period}) が重なっている")
        pseen.add((sid, period))
        value, mark = r.get("value", "").strip(), r.get("mark", "").strip()
        if bool(value) == bool(mark):
            out.append(f"{p}value と mark はどちらか一方だけ")
        elif value and to_decimal(value) is None:
            out.append(f"{p}value {value!r} を数として読めない")
    for sid, units in bad_units.items():
        got = "・".join(repr(u) for u in units)
        out.append(
            f"{sid}：e-Stat の単位 {got} と unit {api_unit[sid].strip()!r} が違う"
            "（unit は e-Stat の単位にする。uv run koten stats fetch が写す）"
        )
    return out


# --- 読み込み（寛容。決まりの確かめは series_problems だけ） --------------------


def parse_series(rows: list[dict[str, str]]) -> list[Series]:
    out = []
    for r in rows:
        sel = _json_or_none(r.get("selector", ""))
        br = _json_or_none(r.get("breaks", ""))
        breaks: list[Break] = []
        if isinstance(br, list):
            for b in br:
                if isinstance(b, dict):
                    breaks.append(Break(str(b.get("before", "")), str(b.get("note", ""))))
        vals: dict[str, Any] = {c: r.get(c, "").strip() for c in SERIES_COLUMNS}
        vals["themes"] = tuple(t.strip() for t in r.get("themes", "").split(";") if t.strip())
        vals["selector"] = sel if isinstance(sel, dict) else {}
        vals["breaks"] = tuple(breaks)
        out.append(Series(**vals))
    return out


def parse_points(rows: list[dict[str, str]]) -> list[Point]:
    """キー（series_id・period）の前後の空白は取る。value は元の文字のまま（R6）。"""
    out = []
    for r in rows:
        vals = {c: r.get(c, "") for c in POINTS_COLUMNS}
        vals["series_id"] = vals["series_id"].strip()
        vals["period"] = vals["period"].strip()
        out.append(Point(**vals))
    return out


# --- 数・年 -------------------------------------------------------------------


def to_decimal(text: str) -> Decimal | None:
    if not text.isascii():
        return None
    try:
        d = Decimal(text.replace(",", "").strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def value_num(p: Point) -> Decimal | None:
    """数として読めるときだけ Decimal。mark（特殊文字）・空は None（0 にしない）。"""
    if p.mark.strip() or not p.value.strip():
        return None
    return to_decimal(p.value)


def period_label(period: str, period_kind: str) -> str:
    return f"{period}{period_kind}"


def parse_year(text: str) -> tuple[str, str]:
    """「2013年」「平成25年度」「令和元年」「2013年（平成25年）」→ ("2013", "年"|"年度")。

    月・四半期は断る。西暦と和暦が両方あるときは同じ年でなければ断る（括弧の和暦は
    西暦を優先して読む）。元号の最後の年（平成31・昭和64 など）を超える年も断る。
    """
    t = unicodedata.normalize("NFKC", text or "").strip()
    if any(w in t for w in ("月", "期")):
        raise SeriesError(f"月・四半期の時間軸は扱わない（年・年度だけ）：{text!r}")
    ad = _AD_YEAR_RE.search(t)
    era = _ERA_YEAR_RE.search(t)
    rest = t
    for m in (ad, era):
        if m:
            rest = rest.replace(m.group(0), "", 1)
    if (not ad and not era) or rest.strip(" \t()") != "":
        raise SeriesError(f"年として読めない：{text!r}")
    era_year = None
    if era:
        n = 1 if era["en"] == "元" else int(era["en"])
        if n < 1 or n > _ERA_LAST.get(era["era"], 10**4):
            raise SeriesError(f"元号の年が範囲外：{text!r}")
        era_year = _ERA_BASE[era["era"]] + n - 1
    ad_year = int(ad["ad"]) if ad else None
    if ad and era and (ad_year != era_year or ad["fy"] != era["fy"]):
        raise SeriesError(f"西暦と和暦が食い違っている：{text!r}")
    year = ad_year if ad else era_year
    fy = (ad or era)["fy"]
    return f"{year:04d}", "年度" if fy else "年"


_AVG_RE = re.compile(r"(?P<base>.*?年度?)\s*(?:年)?平均")


def parse_period_name(text: str) -> tuple[str, str]:
    """parse_year に、年のあとの「平均」「年平均」を外す規則を足したもの。

    「2013年度平均」は年度の平均として実在する。
    """
    t = unicodedata.normalize("NFKC", text or "").strip()
    m = _AVG_RE.fullmatch(t)
    return parse_year(m["base"] if m else t)


# --- 選び方（selector）の変更 --------------------------------------------------

_PART_PLACE = ("statInfId", "fileKind", "sheet", "values")  # 部分が読むセル
_PART_IDENTITY = (*_PART_PLACE, "periods_cells", "periods")


def _norm(v: Any) -> Any:
    """比べるための値：文字は前後の空白を取り、数は文字にする。空（None・""・[]）は None。"""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        v = str(v)
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, list):
        return [_norm(x) for x in v] or None
    if isinstance(v, dict):
        return {k: n for k, x in v.items() if (n := _norm(x)) is not None} or None
    return v


def _norm_part(part: Any) -> Any:
    """部分の比べる形。空の値は書いていないのと同じ。ただし sheet の "" は CSV の印なので残す。"""
    if not isinstance(part, dict):
        return part
    out = {k: n for k, v in part.items() if k != "sheet" and (n := _norm(v)) is not None}
    if "sheet" in part:
        sheet = part["sheet"]
        out["sheet"] = sheet.strip() if isinstance(sheet, str) else _norm(sheet)
    return out


def _norm_selector(sel: dict) -> dict:
    out = {k: n for k, v in sel.items() if k != "parts" and (n := _norm(v)) is not None}
    parts = sel.get("parts")
    out["parts"] = [_norm_part(p) for p in parts] if isinstance(parts, list) else parts
    return out


def _keys_of(part: dict, keys: tuple[str, ...]) -> tuple:
    n = _norm_part(part)
    return tuple(n.get(k) for k in keys)


def fetched_parts(points: list[Point], n_parts: int) -> set[int]:
    """数を取った部分の番号（点の part。0 から）。この部分は選び方を変えられない（R23）。

    part が台帳の部分（n_parts 個）の番号として読めない点があれば、どの部分の数か分からない
    ので、台帳の部分を全部とする。
    """
    out: set[int] = set()
    for p in points:
        t = p.part.strip()
        if not (DIGITS_RE.fullmatch(t) and int(t) < n_parts):
            return set(range(n_parts))
        out.add(int(t))
    return out


def selection_change(kind: str, draft: dict, ledger: dict, *, points: list[Point]) -> str:
    """台帳の行の選び方と下書きの選び方の比べ方：same・growth・stale・different。

    points は台帳のその系列の点（stat_points.csv）。estat_api は、少しでも違えば different。
    estat_file で変えられないのは、数を取った部分（fetched_parts の番号）だけ：その番号の部分の
    statInfId・fileKind・sheet・values・periods_cells・periods が台帳と同じなら growth
    （数の無い部分を直す・外す・足すのも、数を取った部分の updated・label_cells・unit_cell を
    直すのも growth。数が1つも無い系列は、どう変えても growth）。数を取った部分を変える・
    動かすのは different。台帳の部分の並びの頭だけで、数を取った部分が足りない下書きは stale
    （下書きが台帳より古い）。値は前後の空白を取り、periods・periods_cells の空（None・[]）は
    書いていないのと同じにして比べる（same もこの比べ方）。
    """
    if kind != "estat_file":
        return "same" if draft == ledger else "different"
    nd, nl = _norm_selector(draft), _norm_selector(ledger)
    if nd == nl:
        return "same"
    dp, lp = draft.get("parts"), ledger.get("parts")
    if not (isinstance(dp, list) and isinstance(lp, list)):
        return "different"
    if {k: v for k, v in nd.items() if k != "parts"} != {
        k: v for k, v in nl.items() if k != "parts"
    }:
        return "different"

    def same_part(new: Any, old: Any) -> bool:
        return (
            isinstance(new, dict)
            and isinstance(old, dict)
            and _keys_of(new, _PART_IDENTITY) == _keys_of(old, _PART_IDENTITY)
        )

    for i in sorted(fetched_parts(points, len(lp))):  # 小さい番号から：下書きにある部分を先に見る
        if i >= len(dp):
            prefix = all(same_part(a, b) for a, b in zip(dp, lp, strict=False))
            return "stale" if prefix else "different"
        if not same_part(dp[i], lp[i]):
            return "different"
    return "growth"


def _explicit_periods(part: dict) -> set[str]:
    """部分の periods の年（periods_cells があればファイルの年を使うので、空）。"""
    if _norm(part.get("periods_cells")) is not None:
        return set()
    periods = part.get("periods")
    if not isinstance(periods, list):
        return set()
    return {str(p).strip() for p in periods if str(p).strip()}


def part_overlaps(draft: dict, frozen: set[int], points: list[Point]) -> list[str]:
    """数を取っていない部分（frozen に無い番号）が、ほかの部分と重なる理由。重ならなければ空。

    重なるのは：ほかの部分と同じセル（statInfId・fileKind・sheet・values）を読む、periods の
    年にもう数がある（points）、periods の年がほかの部分の periods にもある。部分は 1 から数える。
    periods_cells の年はファイルを読まないと分からない（Mac の koten stats fetch --drafts）。
    """
    parts = draft.get("parts")
    if not isinstance(parts, list):
        return []
    have = {p.period for p in points}
    out: list[str] = []
    for i, part in enumerate(parts):
        if i in frozen or not isinstance(part, dict):
            continue
        # 組は1回だけ言う：前の部分か、数を取った部分と比べる
        others = [
            (j, o)
            for j, o in enumerate(parts)
            if j != i and isinstance(o, dict) and (j < i or j in frozen)
        ]
        place = _keys_of(part, _PART_PLACE)
        same = [j for j, o in others if _keys_of(o, _PART_PLACE) == place]
        if same:
            out.append(f"{i + 1} 番目の部分は {same[0] + 1} 番目と同じセルを読む")
        years = _explicit_periods(part)
        if hit := sorted(years & have):
            out.append(f"{i + 1} 番目の部分の {'・'.join(hit)} 年はもう数がある")
        for j, o in others:
            if both := sorted((years & _explicit_periods(o)) - have):
                y = "・".join(both)
                out.append(f"{i + 1} 番目の部分の {y} 年は {j + 1} 番目の部分にもある")
    return out


# --- 区切り・比べられるか・傾向 ------------------------------------------------


def _by_period(points: list[Point]) -> list[Point]:
    return sorted(points, key=lambda p: p.period)


def segments(s: Series, points: list[Point]) -> dict[str, int]:
    """period → 区切りの番号（0 から）。

    breaks の before をまたぐ（前の点 < before <= この点）、または src_label が
    空でなく、それまでに見た最後の空でない src_label と違うところで番号を1つ進める
    （間に src_label が空の点があっても食い違いは隠れない）。
    """
    seg: dict[str, int] = {}
    cur = 0
    prev: Point | None = None
    last_label = ""
    for p in _by_period(points):
        if prev is not None:
            crossed = any(prev.period < b.before <= p.period for b in s.breaks)
            drift = bool(p.src_label and last_label and p.src_label != last_label)
            if crossed or drift:
                cur += 1
        if p.src_label:
            last_label = p.src_label
        seg[p.period] = cur
        prev = p
    return seg


def comparable(s: Series, points: list[Point], a: str, b: str) -> tuple[bool, str]:
    """a 年と b 年を同じ区切りで比べてよいか。だめなら理由。"""
    seg = segments(s, points)
    by = {p.period: p for p in points}
    for y in (a, b):
        if y not in seg:
            return False, f"{y} の点が無い"
    for y in (a, b):
        if value_num(by[y]) is None:
            return False, f"{y} は数が無い（{by[y].mark or '値なし'}）"
    if seg[a] == seg[b]:
        return True, ""
    lo, hi = sorted((a, b))
    notes = [bk.note for bk in s.breaks if lo < bk.before <= hi]
    why = "調べ方が変わっている" + (
        f"（{'／'.join(notes)}）" if notes else "（元の資料の項目名が変わった）"
    )
    return False, why


def verify_trend(s: Series, points: list[Point]) -> tuple[bool, str]:
    """trend（増加・減少）が数で言えるか。言えるときだけ (True, 抜けた年の文)。"""
    if not s.trend:
        return False, "trend なし"
    try:
        lo, hi = int(s.trend_from), int(s.trend_to)
    except ValueError:
        return False, "trend_from・trend_to が年として読めない"
    nums = [(p, value_num(p)) for p in _by_period(points) if lo <= int(p.period) <= hi]
    nums = [(p, v) for p, v in nums if v is not None]
    if len(nums) < 2:
        return False, "trend の期間に数のある点が2つ未満"
    have = {p.period for p, _ in nums}
    for y in (lo, hi):
        if f"{y:04d}" not in have:
            return False, f"trend の端の年 {y} に数が無い"
    if any(f"{lo:04d}" < b.before <= f"{hi:04d}" for b in s.breaks):
        return False, "trend の期間の中に調べ方の区切りがある"
    seg = segments(s, points)
    if len({seg[p.period] for p, _ in nums}) != 1:
        return False, "trend の期間が調べ方の区切りをまたいでいる"
    vals = [v for _, v in nums]
    pairs = list(zip(vals, vals[1:], strict=False))
    ok = all(b > a for a, b in pairs) if s.trend == "増加" else all(b < a for a, b in pairs)
    if not ok:
        word = "増え続けて" if s.trend == "増加" else "減り続けて"
        return False, f"数が{word}いない"
    missing = [str(y) for y in range(lo, hi + 1) if f"{y:04d}" not in have]
    return True, ("数が無い年：" + "・".join(missing)) if missing else ""


def is_stale(s: Series, points: list[Point]) -> bool:
    """元の資料に新しい年があるはずなのに、最新の数のある年がそれより古い。

    latest_known があって数のある点が1つも無いときも True（今の数として使えない）。
    """
    if not s.latest_known.strip():
        return False
    years = [int(p.period) for p in points if value_num(p) is not None]
    if not years:
        return True
    return max(years) < int(s.latest_known)


# --- 人の確認の鍵 -------------------------------------------------------------


def check_sha(s: Series, points: list[Point], meta: dict | None) -> str:
    """人が確かめた内容の指紋。数・名前・区切り・単位のどれかが変われば変わる。

    カードに出る年か年度か（period_kind）・調査名・作成機関・点の単位（src_unit）も入れる。
    """
    meta_names = [meta.get("title"), meta.get("chosen")] if meta is not None else None
    body = json.dumps(
        [
            s.label,
            s.unit,
            s.selector,
            meta_names,
            sorted((p.period, p.value, p.mark, p.src_label, p.src_unit) for p in points),
            [(b.before, b.note) for b in s.breaks],
            s.period_kind,
            s.survey,
            s.publisher,
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def owner_ok(s: Series, sha: str) -> bool:
    return s.checked_by == HUMAN_CHECKER and s.checked_sha == sha
