"""権利判定（SPEC §3 R1〜R5、§7 license の値）を1か所に集約する。

ここの判定を緩める変更はしない。境界は tests/test_license.py で固定している。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class License(StrEnum):
    PD_JP = "PD_JP"
    CC0 = "CC0"
    CC_BY = "CC_BY"
    BLOCKED_SA = "BLOCKED_SA"
    BLOCKED_ND = "BLOCKED_ND"
    BLOCKED_NC = "BLOCKED_NC"
    COPYRIGHTED = "COPYRIGHTED"
    UNKNOWN = "UNKNOWN"
    NEEDS_REVIEW = "NEEDS_REVIEW"


# R1：画面に原文を出せる
USABLE = frozenset({License.PD_JP, License.CC0, License.CC_BY})

# R3：1967年以前の没者は保護期間満了、1968年以降の没者は保護中
LAST_EXPIRED_DEATH_YEAR = 1967
# R4：連合国籍で1958年以降に亡くなった著者の戦前・戦中の作品は戦時加算で保護が続く場合がある
WARTIME_EXTENSION_FROM_DEATH_YEAR = 1958


def is_usable(lic: License | str) -> bool:
    """画面に原文を出してよいか（R1）。"""
    return License(lic) in USABLE


def may_store_text(lic: License | str) -> bool:
    """本文を保存・索引化してよいか（R2）。

    NEEDS_REVIEW は人が PD_JP と確認するまで本文を持たない（書誌情報のみ）。
    """
    return License(lic) in USABLE


@dataclass(frozen=True)
class Person:
    name: str
    death_year: int | None
    role: str = "author"  # author / translator / editor など
    # 連合国籍か。None は不明（R4 を否定できないので手動確認へ回す）
    allied: bool | None = False


def judge_public_domain(
    persons: list[Person], *, prewar_work: bool | None = None
) -> tuple[License, str]:
    """著者・訳者の没年から保護期間満了かを判定する（R3・R4・R5）。

    persons には原著者と訳者を全て渡す。1人でも保護中なら作品は保護中（R5：訳者の没年も判定）。
    prewar_work は作品が戦前・戦中（1952年4月28日の平和条約発効前）のものか。None は不明。
    """
    if not persons:
        return License.UNKNOWN, "著者情報なし"
    reasons: list[str] = []
    needs_review = False
    for p in persons:
        if p.death_year is None:
            return License.UNKNOWN, f"{p.name}（{p.role}）の没年が不明"
        if p.death_year > LAST_EXPIRED_DEATH_YEAR:
            return License.COPYRIGHTED, f"{p.name}（{p.role}）は{p.death_year}年没で保護中（R3）"
        if p.death_year >= WARTIME_EXTENSION_FROM_DEATH_YEAR and p.allied is not False:
            if prewar_work is not False:
                needs_review = True
                nationality = "連合国籍" if p.allied else "国籍不明"
                reasons.append(
                    f"{p.name}（{p.role}）は{nationality}で{p.death_year}年没。戦時加算の確認が必要（R4）"
                )
                continue
        reasons.append(f"{p.name}（{p.role}）は{p.death_year}年没で満了")
    if needs_review:
        return License.NEEDS_REVIEW, "；".join(reasons)
    return License.PD_JP, "；".join(reasons)


_CC_URL = re.compile(r"creativecommons\.org/(licenses|publicdomain)/([a-z\-]+)", re.I)


def from_cc_license(value: str | None) -> License:
    """論文などのライセンス表記（"CC BY 4.0"、"cc-by-nc"、CCのURLなど）を判定する（R2）。"""
    if not value:
        return License.UNKNOWN
    v = value.strip().lower()
    m = _CC_URL.search(v)
    if m:
        kind, code = m.group(1), m.group(2)
        if kind == "publicdomain":
            return License.CC0 if code == "zero" else License.UNKNOWN
        v = "cc-" + code
    v = re.sub(r"[\s_]+", "-", v)
    v = re.sub(r"-\d+(\.\d+)*$", "", v)  # 版番号を除く
    if v in {"cc0", "cc-zero", "cc-0"}:
        return License.CC0
    if not v.startswith("cc-by"):
        return License.UNKNOWN
    parts = set(v.split("-")[2:])
    if "nc" in parts:
        return License.BLOCKED_NC
    if "nd" in parts:
        return License.BLOCKED_ND
    if "sa" in parts:
        return License.BLOCKED_SA
    if parts:
        return License.UNKNOWN
    return License.CC_BY
