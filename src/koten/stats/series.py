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
_STATS_DATA_ID_RE = re.compile(r"[0-9]+")
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


def load_meta(meta_dir: Path, stats_data_id: str) -> dict | None:
    """docs/estat/<statsDataId>.json（選んだ分類だけの表の情報）。無ければ None。"""
    path = Path(meta_dir) / f"{stats_data_id}.json"
    if not path.exists():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SeriesError(f"{path} を読めない（JSON が壊れている）：{type(e).__name__}") from e
    if not isinstance(meta, dict):
        raise SeriesError(f"{path} は JSON のオブジェクトでない")
    return meta


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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=POINTS_COLUMNS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in POINTS_COLUMNS})
    os.replace(tmp, path)


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


def _selector_problems(kind: str, sel: Any) -> list[str]:
    if isinstance(sel, dict) and not sel:
        return ["selector が空（{}）"]
    if not isinstance(sel, dict):
        return ["selector が JSON のオブジェクトになっていない"]
    out: list[str] = []
    if kind == "estat_api":
        sdid = sel.get("statsDataId")
        if not isinstance(sdid, str) or not _STATS_DATA_ID_RE.fullmatch(sdid):
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
            for k in ("statInfId", "fileKind", "values"):
                if not str(part.get(k, "")).strip():
                    out.append(f"selector の parts[{i}] に {k} が無い")
            if "sheet" not in part:
                out.append(f'selector の parts[{i}] に sheet が無い（CSV は ""）')
            if not (part.get("periods_cells") or part.get("periods")):
                out.append(f"selector の parts[{i}] に periods_cells か periods が無い")
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

    metas は statsDataId → 表の情報（load_meta が1表ごとに返すもの）。estat_api の系列は
    表の情報が無ければ問題、あれば survey=stat_name・publisher=gov_org・
    table_title=title が（前後の空白を除いて）一致すること。estat_file の系列は照らさない。
    """
    out: list[str] = []
    seen: dict[str, int] = {}
    ids: set[str] = set()
    for n, r in enumerate(series_rows, 2):
        p = f"{SERIES_FILE} {n}行目："
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
        if r.get("period_kind", "").strip() not in ("年", "年度"):
            out.append(f"{p}period_kind は 年 か 年度（{r.get('period_kind', '')!r}）")
        if r.get("selector", "").strip():
            sel = _json_or_none(r["selector"])
            for m in _selector_problems(kind, sel):
                out.append(f"{p}{m}")
            sdid = sel.get("statsDataId") if isinstance(sel, dict) else None
            if kind == "estat_api" and isinstance(sdid, str) and sdid.strip():
                meta = metas.get(sdid)
                if meta is None:
                    out.append(
                        f"{p}{sid} の表の情報（{META_DIR}/{sdid}.json）が無い。"
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
                        elif mine != theirs.strip():
                            out.append(
                                f"{p}{col} {mine!r} が表の情報の {key} {theirs.strip()!r} と違う"
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
    for n, r in enumerate(point_rows, 2):
        p = f"{POINTS_FILE} {n}行目："
        sid, period = r.get("series_id", "").strip(), r.get("period", "").strip()
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
    """人が確かめた内容の指紋。数・名前・区切りのどれかが変われば変わる。"""
    meta_names = [meta.get("title"), meta.get("chosen")] if meta is not None else None
    body = json.dumps(
        [
            s.label,
            s.unit,
            s.selector,
            meta_names,
            sorted((p.period, p.value, p.mark, p.src_label) for p in points),
            [(b.before, b.note) for b in s.breaks],
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def owner_ok(s: Series, sha: str) -> bool:
    return s.checked_by == HUMAN_CHECKER and s.checked_sha == sha
