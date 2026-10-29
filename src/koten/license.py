"""権利判定（SPEC §3 R1〜R5、§7 license の値）を1か所に集約する。

ここの判定を緩める変更はしない。境界は tests/test_license.py で固定している。
資料の台帳（config/ref_works.csv）の判定も、ここの関数を組み合わせて決める（koten/ref/）。
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
# R4：連合国籍の人の戦前・戦中の作品は、戦時加算で保護が延びる。日数は国ごとに違い
# （米英など3,794日。ギリシャ4,180日、もっと長い国もあるとされる。要確認）、1955〜1957年没でも
# 保護が残る計算になりうる。国ごとの日数を確かめるまで、1955年以降に亡くなった連合国籍・
# 国籍不明の人は人の確認へ回す（D-019。前は1958年から）
WARTIME_REVIEW_FROM_DEATH_YEAR = 1955
# 著作権法53条：団体名義の著作物は公表から数える。1967年までに公表したものは満了の見込み
LAST_EXPIRED_CORPORATE_PUB_YEAR = 1967

# 厳しさの順。組み合わせるときは厳しいほうを採る（stricter）
_RANK = {
    License.PD_JP: 0,
    License.CC0: 0,
    License.CC_BY: 0,
    License.NEEDS_REVIEW: 1,
    License.UNKNOWN: 2,
    License.BLOCKED_SA: 3,
    License.BLOCKED_ND: 3,
    License.BLOCKED_NC: 3,
    License.COPYRIGHTED: 4,
}


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
    persons: list[Person],
    *,
    prewar_work: bool | None = None,
    rights_holder_allied: bool | None = False,
) -> tuple[License, str]:
    """著者・訳者の没年から保護期間満了かを判定する（R3・R4・R5）。

    persons には関わった人を全て渡す（原著者・訳者・編者・校訂者・注釈者・序文を書いた人など）。
    1人でも保護中なら作品は保護中（R5：訳者の没年も判定）。
    prewar_work は作品が戦前・戦中（1952年4月28日の平和条約発効前）のものか。None は不明。
    rights_holder_allied は、1941年12月8日に権利を持っていた人・団体（英米の出版社など）が
    連合国籍か。None は不明。連合国籍でない人の分も、戦時加算を確かめる側に倒す。
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
        allied = p.allied if p.allied is not False else rights_holder_allied
        if p.death_year >= WARTIME_REVIEW_FROM_DEATH_YEAR and allied is not False:
            if prewar_work is not False:
                needs_review = True
                if p.allied is False:
                    who = "権利者が連合国籍" if allied else "権利者の国籍不明"
                else:
                    who = "連合国籍" if allied else "国籍不明"
                reasons.append(
                    f"{p.name}（{p.role}）は{who}で{p.death_year}年没。戦時加算の確認が必要（R4）"
                )
                continue
        reasons.append(f"{p.name}（{p.role}）は{p.death_year}年没で満了")
    if needs_review:
        return License.NEEDS_REVIEW, "；".join(reasons)
    return License.PD_JP, "；".join(reasons)


def judge_corporate_work(credited_as: str, pub_year: int | None) -> tuple[License, str]:
    """団体名義だけで公表された著作物（著作権法53条：公表から数える）。

    1967年までの公表なら満了の見込みだが、D-018（案1）により1冊ずつ人が確かめるまで
    NEEDS_REVIEW にする。確かめること：団体の名義だけで公表されたか、あとで個人の名が
    著作者として出ていないか（53条2項。出ていればその人の没年で判定する）。
    """
    if pub_year is None:
        return License.UNKNOWN, f"団体名義（{credited_as}）の公表年が不明"
    if pub_year > LAST_EXPIRED_CORPORATE_PUB_YEAR:
        return License.COPYRIGHTED, f"団体名義（{credited_as}）で{pub_year}年公表。保護中（53条）"
    return (
        License.NEEDS_REVIEW,
        f"団体名義（{credited_as}）で{pub_year}年公表。53条では満了の見込み。"
        "団体名義だけで公表されたかを人が確かめる（D-018）",
    )


def judge_edition(pub_year: int | None) -> tuple[License, str]:
    """使う版そのものの刊行年。戦後の復刻・増補では、足された解説・注にも権利がある。

    1968年以降の版は、足された部分の書き手が全員 persons に入っているかを人が確かめるまで
    NEEDS_REVIEW にする（元の版を使うのが原則）。
    """
    if pub_year is None:
        return License.UNKNOWN, "使う版の刊行年が不明"
    if pub_year > LAST_EXPIRED_DEATH_YEAR:
        return (
            License.NEEDS_REVIEW,
            f"{pub_year}年の版。復刻・増補で足された解説・注の書き手を人が確かめる",
        )
    return License.PD_JP, f"{pub_year}年の版"


# NDL の公開範囲の表示（2つ目の関門。license.py の結果と比べて厳しいほうを採る）
NDL_ACCESS = {
    "internet_pd": "インターネット公開（保護期間満了）",
    "internet_saitei": "インターネット公開（裁定）",
    "internet_kyodaku": "インターネット公開（許諾）",
    "kojin": "個人向けデジタル化資料送信サービス",
    "kannai": "国立国会図書館内限定",
}


def judge_ndl_access(label: str | None) -> tuple[License, str]:
    """NDL が「保護期間満了」として公開している資料だけを使う。

    裁定・許諾での公開は権利が残っている。個人送信・館内限定は、NDL がまだ満了と
    確かめていない（個人送信は私的な利用に限られる）。どれも人の確認では使えるようにしない。
    """
    name = NDL_ACCESS.get((label or "").strip())
    if name is None:
        return License.UNKNOWN, "NDL の公開範囲が書かれていない"
    if label == "internet_pd":
        return License.PD_JP, f"NDL の表示は「{name}」"
    if label in ("internet_saitei", "internet_kyodaku"):
        return License.COPYRIGHTED, f"NDL の表示は「{name}」。権利が残っている"
    return License.UNKNOWN, f"NDL の表示は「{name}」。NDL はまだ保護期間満了としていない"


def stricter(*results: tuple[License, str]) -> tuple[License, str]:
    """いくつかの判定のうち、いちばん厳しい結果を採る。理由はその結果になったものを並べる。"""
    if not results:
        return License.UNKNOWN, "判定なし"
    worst = max((License(lic) for lic, _ in results), key=lambda lic: _RANK[lic])
    if _RANK[worst] == 0:
        return worst, "；".join(r for _, r in results)
    return worst, "；".join(r for lic, r in results if _RANK[License(lic)] == _RANK[worst])


class ReviewError(ValueError):
    pass


def apply_review(computed: License | str, reviewed: License | str | None) -> License:
    """人の確認の結果を当てる。使えるようにできるのは NEEDS_REVIEW だけ。

    - 人が PD_JP と確かめたら、NEEDS_REVIEW を PD_JP にできる
    - 厳しくする向き（COPYRIGHTED・UNKNOWN など）は、いつでも受ける
    - UNKNOWN・COPYRIGHTED などを人の確認で使えるようにはしない（データを直して判定し直す）
    """
    computed = License(computed)
    if not reviewed:
        return computed
    reviewed = License(reviewed)
    if _RANK[reviewed] == 0:
        if reviewed != License.PD_JP:
            raise ReviewError(f"人の確認で付けられるのは PD_JP だけ（{reviewed}）")
        if computed == License.NEEDS_REVIEW or computed == License.PD_JP:
            return License.PD_JP
        raise ReviewError(f"判定が {computed} のものは、人の確認で使えるようにはできない")
    return computed if _RANK[computed] >= _RANK[reviewed] else reviewed


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
