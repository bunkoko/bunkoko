"""読む一覧: その会社の「ふだん」と違う開示を、短い一覧にする（docs/ir_text.md 2 章）。

人が読む量を減らす道具。珍しさの順に並べるだけで、大事さ・株価への効き目は言わない。値段は使わない・判定しない。
「ふだん」は、その開示より前の YEARS 年の、同じ会社の開示から作る（先の開示は見ない）。
決め方（数え方・DIFF_LINE・YEARS）は、値段を見ずに決めた。後の検証（docs/ir_text.md 4 章）に使うので、値段を見てから変えない。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

import numpy as np
import pandas as pd

from .collector import normalize_title
from .evaluator import keyword_of
from .inventory import JP_LOW, SHORT, text_stats

YEARS = 3                  # 「ふだん」を作る過去の長さ（年）
RARE_DAYS = 365            # 題名の珍しさを数える過去の長さ（日。全部の会社で）
DIFF_LINE = 0.5            # 同じ題名の過去の本文と、これ以上違えば「中身が大きく違う」
TOP, HEAD, NEW_SHOWN = 5, 300, 5      # 一覧 1 つに、くわしく出す数・本文の頭として見せる字数・初めて出る言葉を見せる数
REST_SHOWN = 30                       # くわしく出さなかった分のうち、題名だけ並べる数
FIRST, CHANGED = "初めての題名", "いつもの題名だが"
NO_TEXT, UNREAD, FOREIGN = "本文がまだ無い", "本文が読めていない", "日本語が少ない"      # 比べられなかった理由（note の列）
COLUMNS = ["id", "time", "ticker", "company", "title", "kind", "key", "chars", "same", "rare", "diff", "ratio", "new", "new_share", "why", "note"]
_NUMBER = re.compile(r"\d[\d,.]*")
_WORD = re.compile(r"[一-鿿々]{2,}|[ァ-ヺー]{3,}")


def shingles(text: str) -> frozenset[str]:
    """文字 2 つ組の集まり。空白を除き、数字は 0 にそろえる（数字だけが変わった書類を、同じに見るため）。"""
    t = _NUMBER.sub("0", re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)))
    return frozenset(t[i:i + 2] for i in range(len(t) - 1))


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """2 つの集まりの重なり（0〜1。同じなら 1）。"""
    return len(a & b) / len(a | b) if a or b else 1.0


def words(text: str) -> set[str]:
    """言葉の集まり: 漢字 2 字以上の続きと、カタカナ 3 字以上の続き（言葉の切れ目を調べる部品は使わない）。"""
    return set(_WORD.findall(unicodedata.normalize("NFKC", text)))


def _back(time: str, **offset) -> str:
    return (pd.Timestamp(time) - pd.DateOffset(**offset)).strftime("%Y-%m-%d %H:%M:%S")


def describe(disc: pd.DataFrame, read: Callable[[str], str | None], day: str) -> pd.DataFrame:
    """day（YYYY-MM-DD）の開示 1 件ごとに、その会社の「ふだん」（その開示より前の YEARS 年）との違いを数える。

    disc は開示の一覧（id / time / ticker / company / title）。read(id) は、その開示の本文（無ければ None）。返す列:
    same（その会社の、同じ題名の過去の件数）・rare（過去 RARE_DAYS 日に、全部の会社で同じ題名が出た数）・
    diff（同じ題名の過去の本文との違い 0〜1。比べる相手が無ければ NaN）・ratio（その長さとの比）・
    new（その会社の過去の本文に無い言葉。長い順に NEW_SHOWN 個まで）・new_share（言葉のうち、初めての割合）・
    why（一覧に出す理由。出さないなら空）・note（数えられなかった理由。数えられたら空）。
    """
    d = disc[disc["time"].str[:10] <= day][["id", "time", "ticker", "company", "title"]].sort_values(["time", "id"], kind="stable")
    d = d.assign(key=[normalize_title(t, c) for t, c in zip(d["title"], d["company"])])
    cache: dict[str, str | None] = {}

    def text_of(doc: str) -> str | None:
        if doc not in cache:
            cache[doc] = read(doc)
        return cache[doc]

    rows = []
    for r in d[d["time"].str[:10] == day].itertuples(index=False):
        before = d["time"] < r.time
        past = d[before & (d["ticker"] == r.ticker) & (d["time"] >= _back(r.time, years=YEARS))]
        twins = past[past["key"] == r.key]
        rare = int((before & (d["key"] == r.key) & (d["time"] >= _back(r.time, days=RARE_DAYS))).sum())
        text = text_of(r.id)
        stats = text_stats(text) if text is not None else {"chars": 0, "jp": 0.0}
        note = NO_TEXT if text is None else UNREAD if stats["chars"] < SHORT else FOREIGN if stats["jp"] < JP_LOW else ""
        diff = ratio = share = np.nan
        new = ""
        if not note:
            olds = [o for o in map(text_of, twins["id"]) if o]
            if olds:
                mine = shingles(text)
                diff = 1.0 - max(similarity(mine, shingles(o)) for o in olds)
                ratio = len(text) / float(np.median([len(o) for o in olds]))
            seen = set().union(*(words(p) for p in map(text_of, past["id"]) if p))
            mine_words = words(text)
            fresh = mine_words - seen
            new = "・".join(sorted(fresh, key=lambda w: (-len(w), w))[:NEW_SHOWN])
            share = len(fresh) / len(mine_words) if mine_words else 0.0
        why = ""
        if not note and len(twins) == 0:
            why = f"{FIRST}（この会社では、{YEARS} 年のうちに無い）"
        elif not note and diff >= DIFF_LINE:
            why = f"{CHANGED}、本文が大きく違う（違い {diff:.2f}）"
        rows.append((r.id, r.time, r.ticker, r.company, r.title, keyword_of(r.title), r.key, stats["chars"], len(twins), rare, diff, ratio, new, share, why, note))
    return pd.DataFrame(rows, columns=COLUMNS)


def _item(n: str, r, read: Callable[[str], str | None]) -> list[str]:
    head = re.sub(r"\s+", " ", (read(r.id) or ""))[:HEAD]
    more = f"・同じ題名の過去の開示 {r.same} 件・長さはその {r.ratio:.1f} 倍" if r.same and np.isfinite(r.ratio) else ""
    return [f"### {n}. {r.company}（{str(r.ticker)[:4]}）{str(r.time)[11:16]} 「{r.title}」",
            f"- 種類: {r.kind}・全部の会社で過去 1 年に同じ題名 {r.rare} 件・長さ {r.chars:,} 字{more}",
            f"- 初めて出る言葉: {r.new or '（無い）'}",
            f"- 本文の頭: {head}", ""]


def _titles(part: pd.DataFrame, tail: Callable = lambda r: "") -> list[str]:
    """題名だけの並び（REST_SHOWN 件まで）。"""
    out = [f"- {r.company}（{str(r.ticker)[:4]}）{str(r.time)[11:16]} 「{r.title}」{tail(r)}" for r in part.head(REST_SHOWN).itertuples(index=False)]
    return out + ([f"（さらに {len(part) - REST_SHOWN} 件）"] if len(part) > REST_SHOWN else []) + [""]


def render(table: pd.DataFrame, day: str, read: Callable[[str], str | None], top: int = TOP, note: str = "") -> str:
    """読む一覧（markdown）。上に 2 つの短い一覧、次に、本文が無くて比べられなかった開示の題名、下に、くわしく出した開示の本文（生成 AI に渡すときに使う）。

    note は、頭に付ける注意の行（その日の開示がまだそろっていない、など）。
    """
    first = table[table["why"].str.startswith(FIRST)].sort_values(["rare", "new_share"], ascending=[True, False], kind="stable")
    changed = table[table["why"].str.startswith(CHANGED)].sort_values("diff", ascending=False, kind="stable")
    usual = int(((table["why"] == "") & (table["note"] == "")).sum())
    skipped = "".join(f"・{why} {n} 件" for why, n in table.loc[table["note"] != "", "note"].value_counts().items())
    out = [f"# 読む一覧（{day} の開示）", "",
           "**その会社の「ふだん」と違う開示を、珍しさの順に並べただけ。大事さや株価への効き目を言うものではない。値段は使っていない。**", ""]
    out += [note, ""] if note else []
    out += [f"この日の開示 {len(table)} 件: {FIRST} {len(first)} 件・{CHANGED}中身が大きく違う {len(changed)} 件・いつもどおり {usual} 件{skipped}", ""]
    shown = []
    for k, (title, part) in enumerate(((f"{FIRST}（その会社で {YEARS} 年のうちに無かった題名。全部の会社を通して珍しい順）", first),
                                       (f"{CHANGED}、中身が大きく違う（同じ題名の過去の本文との違いが {DIFF_LINE} 以上。違いの大きい順）", changed)), start=1):
        out += [f"## {k}. {title}", ""]
        if part.empty:
            out += ["（無い）", ""]
        for j, r in enumerate(part.head(top).itertuples(index=False), start=1):
            out += _item(f"{k}-{j}", r, read)
            shown.append(r)
        if len(part) > top:
            out += [f"ほかに {len(part) - top} 件（題名だけ。くわしく見るなら --each で増やす）:"] + _titles(part.iloc[top:])
    unread = table[table["note"].isin([NO_TEXT, UNREAD])].sort_values(["same", "rare"], kind="stable")       # その会社で初めての題名を先に
    out += ["## 3. 本文が無くて、比べられなかった開示（題名だけ）", ""]
    out += _titles(unread, lambda r: f" — {r.note}" + ("・この会社では初めての題名" if r.same == 0 else "")) if len(unread) else ["（無い）", ""]
    out += ["## 4. 一覧に出た開示の本文（残してある最初の 3 ページ）", ""]
    for r in shown:
        out += [f"### {str(r.ticker)[:4]} {r.company}「{r.title}」（{r.id}・{str(r.time)[:16]}）", "", read(r.id) or "", ""]
    return "\n".join(out)
