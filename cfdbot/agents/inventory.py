"""取ってある開示の本文の棚卸し: 何が・いつから・どれだけ読めているかを数える（docs/agents.md 10.6）。

見るだけ。値段は使わない・判定しない（試した数に入れない）。本文の使い道（人が読むための道具・過去で 1 回確かめる検証）を
選ぶ前の材料。読むのは、開示の一覧・本文のファイル・原本の PDF の名前・「サイトに無い」の控えだけ。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

from .collector import TEXT_MAX, TEXT_PAGES
from .evaluator import keyword_of

# evaluator から使うのは、題名の言葉で種類に分ける keyword_of だけ（値段・成績の計算は使わない）

SHORT = 200                # これより短い本文は「ほとんど読めていない」と数える（画像だけの PDF・読み取れない作りの PDF）
DIGIT_HEAVY = 0.30         # 空白を除いた文字の 3 割より多くが数字なら「ほぼ数字の表」
JP_LOW = 0.30              # かな・漢字が 3 割に満たなければ「日本語が少ない」（英文の開示・文字の崩れ・数字ばかり）
ENOUGH = 20                # 1 社にこれだけ本文があれば、その会社の「ふだん」を作れる見当
FULL = 0.90                # その月の開示（サイトに無いもの・直近を除く）の 9 割に本文があれば「そろっている」
STATS = ["id", "chars", "cut", "jp", "digit"]
CUT = f"{TEXT_MAX:,} 字で切れている"
_SPACE, _JP, _DIGIT = re.compile(r"\s+"), re.compile(r"[぀-ヿ一-鿿]+"), re.compile(r"\d+")


def _count(pattern: re.Pattern, text: str) -> int:
    return sum(len(m) for m in pattern.findall(text))


def text_stats(text: str) -> dict:
    """1 つの本文の様子: 残してある文字数 chars・TEXT_MAX に届いて切れているか cut・かな漢字の割合 jp・数字の割合 digit。

    割合は、空白を除いた文字のうち。
    """
    n = len(text) - _count(_SPACE, text)
    return {"chars": len(text), "cut": len(text) >= TEXT_MAX,
            "jp": _count(_JP, text) / n if n else 0.0, "digit": _count(_DIGIT, text) / n if n else 0.0}


def _months(folder: Path) -> list:
    try:
        return sorted((m for m in os.scandir(folder) if m.is_dir()), key=lambda m: m.name)
    except OSError:                                             # 置き場所が無い・外付けのディスクがつながっていない
        return []


def _files(month, suffix: str) -> list:
    try:
        return sorted((e for e in os.scandir(month.path) if e.name.endswith(suffix) and not e.name.startswith("._")), key=lambda e: e.name)
    except OSError:
        return []


def scan_texts(folder: Path, log: Callable[[str], None] | None = None, every: int = 5000) -> pd.DataFrame:
    """folder/<年-月>/<id>.txt を全部読んで、1 件ごとの様子（text_stats）の表にする。何も書かない。"""
    rows = []
    for month in _months(folder):
        for e in _files(month, ".txt"):
            try:
                text = Path(e.path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rows.append({"id": e.name[:-4], **text_stats(text)})
            if log and len(rows) % every == 0:
                log(f"  本文を読んでいる: {len(rows):,} 件（{month.name} まで）")
    return pd.DataFrame(rows, columns=STATS)


def pdf_ids(folder: Path) -> set[str]:
    """原本の PDF が残っている書類の id（名前を見るだけ。中は開かない）。"""
    return {e.name[:-4] for month in _months(folder) for e in _files(month, ".pdf")}


def build(disc: pd.DataFrame, texts: pd.DataFrame, pdfs: set[str], missing: set[str], since: str, recent_from: str) -> pd.DataFrame:
    """開示の一覧（since から後）に、本文の様子を付けた 1 件 1 行の表（古い順）。

    state: ある・サイトに無い（控えにある）・まだ（直近）（recent_from から後。毎日の収集の持ち分）・まだ。
    一覧に無い本文（一覧から外れた会社の分など）は入らない。
    """
    d = disc[disc["time"] >= since][["id", "time", "ticker", "company", "title"]]
    d = d.sort_values(["time", "id"], kind="stable").drop_duplicates("id").merge(texts.drop_duplicates("id"), on="id", how="left")
    d["kind"] = [keyword_of(t) for t in d["title"]]
    d["state"] = np.select([d["chars"].notna(), d["id"].isin(missing), d["time"] >= recent_from], ["ある", "サイトに無い", "まだ（直近）"], default="まだ")
    d["pdf"] = d["id"].isin(pdfs)
    d["cut"] = d["cut"].eq(True)
    return d.reset_index(drop=True)


def _counts(g: pd.DataFrame) -> dict:
    have = g[g["state"] == "ある"]
    read = have["chars"] >= SHORT
    return {"一覧": len(g), "本文あり": len(have), "サイトに無い": int((g["state"] == "サイトに無い").sum()),
            "まだ": int(g["state"].str.startswith("まだ").sum()), "原本あり": int(have["pdf"].sum()),
            "長さのまん中": int(have["chars"].median()) if len(have) else 0,
            "ほとんど読めていない": int((~read).sum()), CUT: int(have["cut"].sum()),
            "ほぼ数字の表": int((read & (have["digit"] > DIGIT_HEAVY)).sum()), "日本語が少ない": int((read & (have["jp"] < JP_LOW)).sum())}


def by_year(docs: pd.DataFrame) -> pd.DataFrame:
    """年ごとの件数（一覧・本文あり・サイトに無い・まだ・原本あり）と、本文の読め方。"""
    return pd.DataFrame([{"年": y, **_counts(g)} for y, g in docs.groupby(docs["time"].str[:4])])


def by_kind(docs: pd.DataFrame) -> pd.DataFrame:
    """種類（題名の言葉で分けたもの）ごとの件数と、本文の読め方。多い種類から。"""
    out = pd.DataFrame([{"種類": k, **_counts(g)} for k, g in docs.groupby("kind")])
    return out.sort_values(["一覧", "種類"], ascending=[False, True], kind="stable").reset_index(drop=True)


def full_since(docs: pd.DataFrame) -> str | None:
    """続けてそろっている最初の月（その月から後のどの月も、FULL 以上に本文がある）。無ければ None。

    サイトに無いもの・直近（毎日の収集の持ち分）は、抜けに数えない。
    """
    d = docs[docs["state"].isin(["ある", "まだ"])]
    if d.empty:
        return None
    share = (d["state"] == "ある").groupby(d["time"].str[:7]).mean().sort_index()
    first = None
    for month in reversed(list(share.index)):
        if share[month] < FULL:
            break
        first = month
    return first


def company_summary(docs: pd.DataFrame) -> dict:
    """会社ごとの本文の数: 一覧にある会社 listed・本文が 1 件以上ある会社 with_text・1 社あたりのまん中 median・少ない会社 least・多い会社 most・ENOUGH 件以上ある会社 enough。"""
    n = docs[docs["state"] == "ある"].groupby("ticker").size()
    some = len(n) > 0
    return {"listed": int(docs["ticker"].nunique()), "with_text": len(n), "median": float(n.median()) if some else 0.0,
            "least": int(n.min()) if some else 0, "most": int(n.max()) if some else 0, "enough": int((n >= ENOUGH).sum())}


def _pct(a: float, b: float) -> str:
    return f"{a / b:.0%}" if b else "–"


def _of(a: int, b: int, unit: str = "") -> str:
    """数と、b に占める割合（b が 0 なら、割合を言えないので「–」）。"""
    return f"{a:,}{unit}（{_pct(a, b)}）" if b else "–"


def summary_lines(docs: pd.DataFrame, orphans: int = 0) -> list[str]:
    """全体のまとめ（報告の 1 章。画面にもそのまま出す）。"""
    have = docs[docs["state"] == "ある"]
    state = docs["state"].value_counts()
    lines = [f"- 開示の一覧（{docs['time'].min()[:7]}〜）: {len(docs):,} 件・{docs['ticker'].nunique():,} 社（{docs['time'].min()[:10]}〜{docs['time'].max()[:10]}）",
             f"- 本文がある: {len(have):,} 件（{_pct(len(have), len(docs))}）・サイトに無い: {state.get('サイトに無い', 0):,} 件・まだ取っていない: {state.get('まだ', 0):,} 件"
             + f"（ほかに、直近で毎日の収集の持ち分: {state.get('まだ（直近）', 0):,} 件）"]
    first = full_since(docs)
    if first is None:
        lines.append(f"- 続けてそろっている月は、まだ無い（その月の {FULL:.0%} 以上に本文がある月が、最後まで続く所が無い）")
    else:
        before, last = docs[docs["time"].str[:7] < first], have["time"].max()[:7]
        span = (int(last[:4]) - int(first[:4])) * 12 + int(last[5:]) - int(first[5:]) + 1
        lines.append(f"- 続けてそろっているのは {first} から {last} までの {span} か月（その月の {FULL:.0%} 以上に本文がある月が、最後まで続く）。"
                     f"それより前: 一覧 {len(before):,} 件のうち、本文 {int((before['state'] == 'ある').sum()):,} 件")
    lines.append(f"- 原本の PDF がある: {int(have['pdf'].sum()):,} 件（本文のある分の {_pct(have['pdf'].sum(), len(have))}）。"
                 + "無い分は、2026-11-26 までの毎日の収集で取った本文（その後は、毎日の収集でも原本を残す）")
    if len(have):
        short = int((have["chars"] < SHORT).sum())
        lines.append(f"- 本文の長さ（残してある文字数。PDF の最初の {TEXT_PAGES} ページ・{TEXT_MAX:,} 字まで）: まん中 {int(have['chars'].median()):,} 字・"
                     f"{CUT} {_of(int(have['cut'].sum()), len(have), ' 件')}・ほとんど読めていない（{SHORT} 字未満）{_of(short, len(have), ' 件')}・"
                     f"うち空 {int((have['chars'] == 0).sum()):,} 件")
    lines.append(f"- 一覧に無い本文: {orphans:,} 件（一覧から外れた会社の分など。上の数には入れていない）")
    return lines


def _table(df: pd.DataFrame, first: str, share: bool) -> list[str]:
    head = [first, "一覧", "本文あり"] + (["割合", "サイトに無い", "まだ", "原本あり"] if share else []) + ["長さのまん中", "ほとんど読めていない", CUT]
    head += [] if share else ["ほぼ数字の表", "日本語が少ない"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in df.to_dict("records"):
        n = r["本文あり"]
        cells = [str(r[first]), f"{r['一覧']:,}", f"{n:,}"]
        if share:
            cells += [_pct(n, r["一覧"]), f"{r['サイトに無い']:,}", f"{r['まだ']:,}", f"{r['原本あり']:,}"]
        cells += [f"{r['長さのまん中']:,}" if n else "–", _of(r["ほとんど読めていない"], n), _of(r[CUT], n)]
        if not share:
            cells += [_of(r["ほぼ数字の表"], n), _of(r["日本語が少ない"], n)]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def report(docs: pd.DataFrame, when: str, place: str, orphans: int = 0) -> str:
    """棚卸しの報告（markdown）。数と割合だけ。本文の中身・値段・成績は出さない。"""
    c = company_summary(docs)
    out = [f"# IR の本文の棚卸し（{when}）", "",
           "**見るだけ。値段は使っていない・判定はしていない（試した数に入れない）。** 取ってある開示の本文について、何が・いつから・どれだけ読めているかを数えた。",
           f"本文の置き場所: {place}", "", "## 1. 全体", "", *summary_lines(docs, orphans), "",
           "## 2. 年ごと", "", "「割合」は、一覧のうち本文がある割合。右の 3 つは、本文のある分について。", "", *_table(by_year(docs), "年", share=True), "",
           "## 3. 種類ごと（題名の言葉で分けた）", "", "かっこの中は、その種類の本文のある分に占める割合。", "", *_table(by_kind(docs), "種類", share=False), "",
           "## 4. 会社ごと", "",
           f"- 一覧にある会社: {c['listed']:,} 社。本文が 1 件以上ある会社: {c['with_text']:,} 社",
           f"- 1 社あたりの本文: まん中 {c['median']:,.0f} 件（少ない会社 {c['least']:,} 件〜多い会社 {c['most']:,} 件）",
           f"- {ENOUGH} 件以上ある会社（その会社の「ふだん」を作れる見当）: {c['enough']:,} 社", "",
           "## 5. 読み方", "",
           f"- 本文は、PDF の最初の {TEXT_PAGES} ページを文字にして、{TEXT_MAX:,} 字までを残したもの。「切れている」は {TEXT_MAX:,} 字に届いたもの（続きがある）",
           f"- 「ほとんど読めていない」は {SHORT} 字未満（画像だけの PDF・読み取れない作りの PDF）",
           f"- 「ほぼ数字の表」は、空白を除いた文字の {DIGIT_HEAVY:.0%} より多くが数字のもの。「日本語が少ない」は、かな・漢字が {JP_LOW:.0%} に満たないもの"
           + "（英文の開示・文字の崩れ・数字ばかり）。どちらも、ほとんど読めていないものは除いて数えた",
           "- 種類は、題名の言葉で分けたもの（上から順に、最初に当てはまった言葉。どれにも当てはまらなければ「その他」）",
           "- 原本の PDF があるものは、読むページを変えて読み直せる（サイトに取りに行かない）",
           "- これは数えただけで、どの使い方が良いかは言っていない。使い方は、この表を見てから利用者が選ぶ（docs/todo.md の 41）", ""]
    return "\n".join(out)
