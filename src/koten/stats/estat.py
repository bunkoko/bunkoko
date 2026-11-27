"""e-Stat の API（3.0）の応答を読む（段1）。

- 応答は XML→JSON の決まりの形：属性は "@名前"、値は "$"。同じ名前の要素が1つだけのときは
  並びでなく1つの dict で来る（as_list で並びにそろえる）。
- 名前（分類・項目）から符号を決める。曖昧なら黙って選ばず、候補を並べて止まる。
- appId は Fetcher の secret_params にだけ渡す。URL・控え・マニフェスト・例外には出ない。
- テストは本物の API を呼ばない（仕様書の形をまねた dict と httpx.MockTransport）。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

from koten.net import Fetcher
from koten.stats.series import (
    Point,
    Series,
    SeriesError,
    norm_name,
    norm_unit,
    parse_period_name,
    to_decimal,
)

API_BASE = "https://api.e-stat.go.jp/rest/3.0/app/json"
APP_ID_ENV = "KOTEN_ESTAT_APPID"
ESTAT_API_CREDIT = (
    "このサービスは、政府統計総合窓口(e-Stat)のAPI機能を使用していますが、"
    "サービスの内容は国によって保証されたものではありません。"
)
SUBDIR = "estat"
LIMIT = 100000
MAX_PAGES = 50
MAX_CANDIDATES = 30
_TIME = "time"


class EstatError(RuntimeError):
    """e-Stat の応答を読めない・問いが1つに決まらない。"""


class EstatNoData(EstatError):
    """STATUS 1（該当データなし）。その系列の失敗として扱う。"""


@dataclass
class ClassItem:
    code: str
    name: str
    level: str
    unit: str


@dataclass
class ClassObj:
    id: str
    name: str
    items: list[ClassItem]


@dataclass
class TableMeta:
    stats_data_id: str
    stat_code: str
    stat_name: str
    gov_org_code: str
    gov_org: str
    statistics_name: str
    title: str
    title_no: str
    cycle: str
    survey_date: str
    open_date: str
    updated_date: str
    classes: list[ClassObj]
    fetched_at: str = ""  # この表の情報を取った日時（マニフェストの記録。無ければ空）


# --- 小さな道具 ---------------------------------------------------------------


def as_list(x: object) -> list:
    """None → []、dict（1件だけ）→ [dict]、list → list。"""
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def _text(x: object) -> str:
    """文字・数・{"$": …} のどれでも文字にする（None は ""）。"""
    if isinstance(x, dict):
        x = x.get("$")
    return "" if x is None else str(x)


def _attr(d: object, name: str) -> str:
    return _text(d.get(name)) if isinstance(d, dict) else ""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _norm_name(text: str) -> str:
    """名前の比べ用：NFKC・波ダッシュ類を ~ にそろえる・空白を除く。"""
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("〜", "~").replace("～", "~")
    return re.sub(r"\s+", "", t)


def _norm_unit(text: str) -> str:
    return norm_unit(text)


def _root(data: object) -> tuple[str, dict]:
    if isinstance(data, dict):
        for key in ("GET_META_INFO", "GET_STATS_DATA"):
            if isinstance(data.get(key), dict):
                return key, data[key]
    raise EstatError("応答に GET_META_INFO も GET_STATS_DATA も無い")


def _status_of(data: object) -> int | None:
    """STATUS の数。RESULT が無い・数でないときは None。"""
    try:
        _, root = _root(data)
        res = root.get("RESULT")
        return int(str(res["STATUS"]).strip()) if isinstance(res, dict) else None
    except (EstatError, KeyError, ValueError):
        return None


# --- STATUS -------------------------------------------------------------------


def status_error(data: object) -> str | None:
    """STATUS が 100 以上（エラー）なら文。0〜2 は正常なので None。

    1（該当データなし）は呼び手が見分ける（EstatNoData）。fetch_json の error_of に渡す。
    """
    try:
        _, root = _root(data)
    except EstatError as e:
        return str(e)
    res = root.get("RESULT")
    if not isinstance(res, dict):
        return "応答に RESULT が無い"
    try:
        n = int(str(res.get("STATUS")).strip())
    except ValueError:
        return f"STATUS を読めない（{res.get('STATUS')!r}）"
    if n >= 100:
        return f"STATUS {n}：{_text(res.get('ERROR_MSG'))}"
    return None


# --- 表の情報 -----------------------------------------------------------------


def parse_meta(data: dict) -> TableMeta:
    key, root = _root(data)
    body = root.get("METADATA_INF" if key == "GET_META_INFO" else "STATISTICAL_DATA")
    if not isinstance(body, dict):
        raise EstatError("応答に表の情報（METADATA_INF／STATISTICAL_DATA）が無い")
    t = body.get("TABLE_INF")
    if not isinstance(t, dict):
        raise EstatError("応答に TABLE_INF が無い")
    classes = []
    for co in as_list((body.get("CLASS_INF") or {}).get("CLASS_OBJ")):
        items = [
            ClassItem(
                code=_attr(c, "@code"),
                name=_attr(c, "@name"),
                level=_attr(c, "@level"),
                unit=_attr(c, "@unit"),
            )
            for c in as_list(co.get("CLASS"))
        ]
        classes.append(ClassObj(id=_attr(co, "@id"), name=_attr(co, "@name"), items=items))
    return TableMeta(
        stats_data_id=_attr(t, "@id"),
        stat_code=_attr(t.get("STAT_NAME"), "@code"),
        stat_name=_text(t.get("STAT_NAME")),
        gov_org_code=_attr(t.get("GOV_ORG"), "@code"),
        gov_org=_text(t.get("GOV_ORG")),
        statistics_name=_text(t.get("STATISTICS_NAME")),
        title=_text(t.get("TITLE")),
        title_no=_attr(t.get("TITLE"), "@no"),
        cycle=_text(t.get("CYCLE")),
        survey_date=_text(t.get("SURVEY_DATE")),
        open_date=_text(t.get("OPEN_DATE")),
        updated_date=_text(t.get("UPDATED_DATE")),
        classes=classes,
    )


def _cd_key(class_id: str) -> str:
    return "cd" + class_id[:1].upper() + class_id[1:]


def _fixed_classes(meta: TableMeta) -> list[ClassObj]:
    return [c for c in meta.classes if c.id != _TIME]


def _candidates(c: ClassObj) -> str:
    names = [it.name for it in c.items[:MAX_CANDIDATES]]
    more = f"…ほか {len(c.items) - MAX_CANDIDATES} 個" if len(c.items) > MAX_CANDIDATES else ""
    return "／".join(names) + more


def resolve_selector(meta: TableMeta, names: dict[str, str]) -> dict[str, str]:
    """分類の名前→項目の名前から、時間以外の分類ごとに符号を1つ決める。"""
    fixed = _fixed_classes(meta)
    by_norm: dict[str, list[ClassObj]] = {}
    for c in fixed:
        by_norm.setdefault(_norm_name(c.name), []).append(c)
    for group in by_norm.values():
        if len(group) > 1:
            both = "／".join(f"{c.name}（{c.id}）" for c in group)
            raise EstatError(f"分類の名前が重なっていて名前では選べない：{both}")
    want: dict[str, tuple[str, str]] = {}
    for k, val in names.items():
        nk = _norm_name(k)
        if not isinstance(val, str):
            raise EstatError(f"分類 {k!r} の項目の名前は文字列にする（{val!r}）")
        if nk not in by_norm:
            have = "／".join(c.name for c in fixed)
            raise EstatError(f"分類の名前 {k!r} が表に無い（ある分類：{have}）")
        if nk in want:
            raise EstatError(f"分類の名前 {want[nk][0]!r} と {k!r} が同じ分類を指している")
        want[nk] = (k, val)
    sel = {"statsDataId": meta.stats_data_id}
    for c in _fixed_classes(meta):
        given = want.get(_norm_name(c.name))
        if given is None:
            if len(c.items) == 1:
                sel[_cd_key(c.id)] = c.items[0].code
                continue
            raise EstatError(f"分類「{c.name}」の項目の名前が要る。候補：{_candidates(c)}")
        if given[1].strip().startswith("#"):
            code = given[1].strip()[1:].strip()
            hits = [it for it in c.items if it.code == code]
            if not hits:
                raise EstatError(
                    f"分類「{c.name}」に符号 {code!r} の項目が無い。候補：{_candidates(c)}"
                )
            sel[_cd_key(c.id)] = hits[0].code
            continue
        target = _norm_name(given[1])
        hits = [it for it in c.items if _norm_name(it.name) == target]
        if not hits:
            raise EstatError(f"分類「{c.name}」に項目 {given[1]!r} が無い。候補：{_candidates(c)}")
        if len(hits) > 1:
            both = "／".join(f"{it.name}（{it.code}）" for it in hits)
            raise EstatError(
                f"分類「{c.name}」の項目 {given[1]!r} が複数に当たる：{both}。"
                '符号で選ぶときは "#001" のように書く'
            )
        sel[_cd_key(c.id)] = hits[0].code
    return sel


def _code_candidates(c: ClassObj) -> str:
    items = c.items[:MAX_CANDIDATES]
    more = f"…ほか {len(c.items) - MAX_CANDIDATES} 個" if len(c.items) > MAX_CANDIDATES else ""
    return "／".join(f"{it.name}（{it.code}）" for it in items) + more


def selector_from_codes(meta: TableMeta, sel: dict) -> dict[str, str]:
    """下書きの selector の符号（selector_names が無いとき）を表の情報で確かめる（R37）。

    時間以外の分類ごとに、表にある符号がちょうど1つ書いてあること。表に無い分類の鍵・時間の
    鍵（cdTime）は断る。足りないもの・表に無いものを全部並べて EstatError。"""
    fixed = _fixed_classes(meta)
    want = {_cd_key(c.id): c for c in fixed}
    problems = []
    extra = sorted(k for k in sel if k.startswith("cd") and k not in want)
    if extra:
        problems.append(f"表に無い分類の鍵がある（{'・'.join(extra)}。時間の cdTime は書かない）")
    sdid = sel.get("statsDataId")
    if sdid not in (None, "") and str(sdid).strip() != meta.stats_data_id:
        problems.append(f"selector の statsDataId {sdid!r} が表の番号 {meta.stats_data_id} と違う")
    out = {"statsDataId": meta.stats_data_id}
    for key, c in want.items():
        code = sel.get(key)
        if not isinstance(code, str) or not code.strip():
            problems.append(f"分類「{c.name}」の符号（{key}）が無い。候補：{_code_candidates(c)}")
            continue
        hits = [it for it in c.items if it.code == code.strip()]
        if not hits:
            problems.append(
                f"分類「{c.name}」（{key}）に符号 {code!r} の項目が無い。"
                f"候補：{_code_candidates(c)}"
            )
            continue
        out[key] = hits[0].code
    if problems:
        raise EstatError("、".join(problems))
    return out


def _chosen(meta: TableMeta, selector: dict) -> list[tuple[ClassObj, ClassItem]]:
    out = []
    for c in _fixed_classes(meta):
        code = selector.get(_cd_key(c.id))
        it = next((i for i in c.items if i.code == code), None)
        if it is None:
            raise EstatError(f"分類「{c.name}」に符号 {code!r} の項目が無い")
        out.append((c, it))
    return out


def meta_record(meta: TableMeta, selector: dict) -> dict:
    """docs/estat/<id>.json に書く形。選んだ項目だけ（分類の全項目は入れない）。"""
    time_obj = next((c for c in meta.classes if c.id == _TIME), None)
    return {
        "stats_data_id": meta.stats_data_id,
        "stat_code": meta.stat_code,
        "stat_name": meta.stat_name,
        "gov_org": meta.gov_org,
        "statistics_name": meta.statistics_name,
        "title": meta.title,
        "title_no": meta.title_no,
        "cycle": meta.cycle,
        "survey_date": meta.survey_date,
        "open_date": meta.open_date,
        "updated_date": meta.updated_date,
        "chosen": [
            {"class": c.name, "item": it.name, "code": it.code, "unit": it.unit}
            for c, it in _chosen(meta, selector)
        ],
        "times": [{"code": i.code, "name": i.name} for i in (time_obj.items if time_obj else [])],
        "fetched_at": meta.fetched_at or _now(),
    }


# --- 年 -----------------------------------------------------------------------


def period_of(code: str, name: str) -> tuple[str, str]:
    """時間の符号と名前から (YYYY, 年|年度)。月・四半期は断る。"""
    try:
        year, kind = parse_period_name(name)
    except SeriesError as e:
        raise EstatError(f"時間 {code} の名前 {name!r} を年として読めない：{e}") from e
    if code[:4] != year:
        raise EstatError(f"時間の符号 {code} の年と名前 {name!r}（{year}）が合わない")
    return year, kind


# --- 値 -----------------------------------------------------------------------


def _stat_body(data: dict) -> dict:
    _, root = _root(data)
    body = root.get("STATISTICAL_DATA")
    if not isinstance(body, dict):
        raise EstatError("応答に STATISTICAL_DATA が無い")
    return body


def _value_rows(data: dict) -> list[dict]:
    di = _stat_body(data).get("DATA_INF")
    rows = as_list(di.get("VALUE")) if isinstance(di, dict) else []
    if not all(isinstance(r, dict) for r in rows):
        raise EstatError("VALUE の行が dict でない")
    return rows


def parse_values(data: dict, selector: dict) -> tuple[list[tuple[str, str, str, str]], str | None]:
    """((period, value, mark, unit) の並び, NEXT_KEY)。value は元の文字のまま。"""
    out, nxt, _ = _parse_values(data, selector)
    return out, nxt


def _parse_values(
    data: dict, selector: dict
) -> tuple[list[tuple[str, str, str, str]], str | None, set[str]]:
    kinds: set[str] = set()
    body = _stat_body(data)
    nxt = _text((body.get("RESULT_INF") or {}).get("NEXT_KEY")) or None
    time_names: dict[str, str] = {}
    for co in as_list((body.get("CLASS_INF") or {}).get("CLASS_OBJ")):
        if _attr(co, "@id") == _TIME:
            for c in as_list(co.get("CLASS")):
                time_names[_attr(c, "@code")] = _attr(c, "@name")
    fixed = {k: v for k, v in selector.items() if k.startswith("cd")}
    out: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for r in _value_rows(data):
        for k, want in fixed.items():
            attr = "@" + k[2:3].lower() + k[3:]
            got = _attr(r, attr)
            if got != str(want):
                raise EstatError(
                    f"問いに決まっていない行がある：{attr} が {got!r}（選んだのは {want!r}）"
                )
        code = _attr(r, "@time")
        if code not in time_names:
            raise EstatError(f"時間の符号 {code!r} の名前が応答の CLASS_INF に無い")
        period, kind = period_of(code, time_names[code])
        kinds.add(kind)
        if period in seen:
            raise EstatError(f"同じ年 {period} が2つある")
        seen.add(period)
        raw = _text(r.get("$"))
        if not raw.strip():
            raise EstatError(f"{period} の値が空（応答の形が想定と違う）")
        if to_decimal(raw) is not None:
            out.append((period, raw, "", _attr(r, "@unit")))
        else:
            out.append((period, "", raw, _attr(r, "@unit")))
    return out, nxt, kinds


# --- 元の資料との照らし合わせ --------------------------------------------------


def cross_check(s: Series, meta: TableMeta, units: set[str]) -> list[str]:
    """系列の台帳と e-Stat の表の食い違い。空なら全部一致。"""
    out = []
    mine = _norm_unit(s.unit)
    for u in sorted(u for u in units if u.strip()):
        if _norm_unit(u) != mine:
            out.append(f"単位が違う：台帳 {s.unit!r}・e-Stat {u!r}")
    for label, ours, theirs in (
        ("調査名 survey", s.survey, meta.stat_name),
        ("公表元 publisher", s.publisher, meta.gov_org),
        ("表の題 table_title", s.table_title, meta.title),
    ):
        if norm_name(ours) != norm_name(theirs):  # series_problems と同じ比べ方（R33）
            out.append(f"{label} が違う：台帳 {ours!r}・e-Stat {theirs!r}")
    return out


OFFICIAL_LABELS = {
    "survey": "調査名",
    "publisher": "作成機関",
    "table_title": "表の題",
    "unit": "単位",
}


def official_fields(meta: TableMeta, points: list[Point]) -> dict[str, str]:
    """台帳に写す e-Stat の公の言葉（R25）：調査名・作成機関・表の題と、数の単位。

    単位は点の src_unit（norm_unit）。点ごとに違えば EstatError（その系列は取らない）。
    e-Stat が出さない言葉（空）は入れない（台帳のまま）。
    """
    units = sorted({norm_unit(p.src_unit) for p in points})
    if len(units) > 1:
        shown = "・".join(repr(u) if u else "（なし）" for u in units)
        raise EstatError(
            f"数の単位が年ごとに違う（{shown}）。"
            "表章項目か分類の選び方を確かめる（Claude に伝える）"
        )
    out = {
        "survey": meta.stat_name.strip(),
        "publisher": meta.gov_org.strip(),
        "table_title": meta.title.strip(),
    }
    out = {k: v for k, v in out.items() if v}
    if units and units[0]:
        out["unit"] = units[0]
    return out


def units_of(points: list[Point], meta: TableMeta, selector: dict) -> set[str]:
    """cross_check に渡す単位：点の src_unit（空でないもの）と選んだ表章項目の単位。"""
    out = {p.src_unit for p in points if p.src_unit.strip()}
    out |= {it.unit for c, it in _chosen(meta, selector) if c.id == "tab" and it.unit.strip()}
    return out


# --- クライアント -------------------------------------------------------------


class EstatClient:
    def __init__(self, fetcher: Fetcher, app_id: str):
        if not app_id or not app_id.strip():
            raise EstatError(f"e-Stat の appId が空。環境変数 {APP_ID_ENV} を設定する")
        self.fetcher = fetcher
        self._secret = {"appId": app_id.strip()}

    def _get(self, endpoint: str, params: dict[str, str], refresh: bool) -> tuple[dict, str]:
        """(応答, 取った日時)。日時はマニフェストの記録（取り直さなければ前のまま）。"""
        url = f"{API_BASE}/{endpoint}?" + urlencode(sorted(params.items()))
        data = self.fetcher.fetch_json(
            url, SUBDIR, secret_params=self._secret, error_of=status_error, refresh=refresh
        )
        entry = self.fetcher.manifest.get(url)
        if _status_of(data) == 1:
            # 控えを消す（e-Stat が数を出したあと、--refresh なしの次の実行でも取り直す）
            if entry is not None:
                (self.fetcher.raw_dir / entry.path).unlink(missing_ok=True)
            raise EstatNoData(
                f"{endpoint}：該当データなし（STATUS 1）。selector の符号（cd…）と"
                "表の番号（statsDataId）が表のとおりか確かめる"
            )
        return data, (entry.fetched_at if entry else "")  # type: ignore[return-value]

    def meta(self, stats_data_id: str, *, refresh: bool = False) -> TableMeta:
        params = {"lang": "J", "statsDataId": stats_data_id, "explanationGetFlg": "N"}
        data, fetched = self._get("getMetaInfo", params, refresh)
        meta = parse_meta(data)
        meta.fetched_at = fetched
        return meta

    def points(self, s: Series, *, refresh: bool = False) -> tuple[list[Point], TableMeta]:
        sel = s.selector
        sdid = str(sel.get("statsDataId", "")).strip()
        if not sdid:
            raise EstatError(f"{s.series_id}：selector に statsDataId が無い")
        want = {_cd_key(c.id): c for c in _fixed_classes(self.meta(sdid, refresh=refresh))}
        for key, c in want.items():
            if not str(sel.get(key, "")).strip():
                raise EstatError(f"{s.series_id}：分類「{c.name}」が決まっていない（{key}）")
        extra = sorted(k for k in sel if k.startswith("cd") and k not in want)
        if extra:
            raise EstatError(f"{s.series_id}：表に無い分類の指定がある（{'・'.join(extra)}）")
        base = {
            "lang": "J",
            "statsDataId": sdid,
            "metaGetFlg": "Y",
            "cntGetFlg": "N",
            "explanationGetFlg": "N",
            "annotationGetFlg": "Y",
            "replaceSpChar": "0",
            "limit": str(LIMIT),
            **{k: str(sel[k]) for k in want},
        }
        meta: TableMeta | None = None
        rows: list[dict] = []
        values: list[tuple[str, str, str, str]] = []
        seen: set[str] = set()
        stamps: list[str] = []
        start: str | None = None
        for _ in range(MAX_PAGES):
            params = dict(base)
            if start is not None:
                params["startPosition"] = start
            data, got = self._get("getStatsData", params, refresh)
            stamps.append(got)
            if meta is None:
                meta = parse_meta(data)
            page, start, kinds = _parse_values(data, sel)
            if kinds - {s.period_kind}:
                raise EstatError(
                    f"{s.series_id}：台帳の period_kind は {s.period_kind}、"
                    f"応答の時間は {'・'.join(sorted(kinds))}"
                )
            for row in page:
                if row[0] in seen:
                    raise EstatError(f"{s.series_id}：同じ年 {row[0]} がページをまたいで重なる")
                seen.add(row[0])
            values += page
            rows += _value_rows(data)
            if start is None:
                break
        else:
            raise EstatError(f"{s.series_id}：{MAX_PAGES} ページを超えても続きがある")
        assert meta is not None
        if not values:
            raise EstatNoData(f"{s.series_id}：応答に値が1つも無い")
        tab_unit = next((it.unit for c, it in _chosen(meta, sel) if c.id == "tab"), "")
        sha = hashlib.sha256(
            json.dumps(rows, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        fetched = min((t for t in stamps if t), default=_now())
        meta.fetched_at = fetched
        points = []
        for period, value, mark, unit in sorted(values):
            points.append(
                Point(s.series_id, period, value, mark, "", "", unit or tab_unit, fetched, sha)
            )
        return points, meta
