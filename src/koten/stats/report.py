"""koten stats fetch の報告（点の数・抜けた年・変わった数）。ネットにも DB にも触らない。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from koten.stats.series import (
    SERIES_COLUMNS,
    Point,
    Series,
    parse_points,
    parse_series,
    segments,
    value_num,
)

_YEAR_LEN = 4


def _text(p: Point | None) -> str:
    if p is None:
        return "なし"
    return p.value.strip() or (f"（{p.mark}）" if p.mark.strip() else "（空）")


def latest_with_number(points: list[Point]) -> str:
    years = sorted(p.period for p in points if value_num(p) is not None)
    return years[-1] if years else ""


def missing_years(points: list[Point]) -> list[str]:
    """最初と最後の年のあいだで、点が1つも無い年。"""
    years = sorted(int(p.period) for p in points if len(p.period) == _YEAR_LEN)
    if len(years) < 2:
        return []
    have = set(years)
    return [str(y) for y in range(years[0], years[-1] + 1) if y not in have]


def mark_list(points: list[Point]) -> list[str]:
    return [
        f"{p.period}:{p.mark}" for p in sorted(points, key=lambda p: p.period) if p.mark.strip()
    ]


def changed_values(old: list[Point], new: list[Point]) -> list[str]:
    """前の台帳の点から値か mark が変わった年。「年: 前 → 今」。増えた年・消えた年は含めない。"""
    now = {p.period: p for p in new}
    return [
        f"{p.period}: {_text(p)} → {_text(now[p.period])}"
        for p in sorted(old, key=lambda p: p.period)
        if p.period in now and (now[p.period].value, now[p.period].mark) != (p.value, p.mark)
    ]


def added_removed_years(old: list[Point], new: list[Point]) -> tuple[list[str], list[str]]:
    a, b = {p.period for p in old}, {p.period for p in new}
    return sorted(b - a), sorted(a - b)


def units_text(points: list[Point], units: set[str]) -> str:
    return (
        "・".join(sorted({*units, *(p.src_unit for p in points if p.src_unit.strip())})) or "なし"
    )


def report_lines(
    s: Series,
    points: list[Point],
    *,
    units: set[str],
    diffs: list[str],
    changed: list[str],
    added: list[str] = (),
    removed: list[str] = (),
    lapsed: bool = False,
) -> list[str]:
    """系列1つ分の報告。1行目が要約、続きが気をつける点。"""
    seg = segments(s, points)
    head = [
        f"{len(points)} 点",
        f"最新の数のある年 {latest_with_number(points) or 'なし'}",
        f"抜けた年 {'・'.join(missing_years(points)) or 'なし'}",
        f"mark {'・'.join(mark_list(points)) or 'なし'}",
        f"区切り {len(set(seg.values()))} 区間",
        f"単位 {units_text(points, units)}",
    ]
    lines = [f"{s.series_id}：" + "、".join(head)]
    lines += [f"  台帳との違い：{d}" for d in diffs]
    if changed:
        lines.append("  前から変わった数：" + "、".join(changed))
    if added:
        lines.append("  増えた年：" + "・".join(added))
    if removed:
        lines.append("  消えた年：" + "・".join(removed))
    if lapsed:
        lines.append("  オーナーの確かめが外れる（数・名前・表の情報が前の確かめと違う）")
    return lines


def series_from_draft(item: Mapping[str, Any]) -> Series:
    """下書きの series の1件を、台帳の1行と同じ形にして Series にする。

    下書きでは themes が配列、selector・breaks が JSON の値のままでもよい。
    """
    row: dict[str, str] = {}
    for c in SERIES_COLUMNS:
        v = item.get(c, "")
        if c == "themes" and isinstance(v, list):
            v = ";".join(str(t) for t in v)
        elif isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        row[c] = "" if v is None else str(v)
    return parse_series([row])[0]


def stored_points(rows: list[dict[str, str]], series_id: str) -> list[Point]:
    return parse_points([r for r in rows if r.get("series_id", "").strip() == series_id])
