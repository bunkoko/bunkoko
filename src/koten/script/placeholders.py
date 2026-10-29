"""プレースホルダの差し込み（SPEC §8.7・R6）。

台本を書く LLM には、引用・出典・著者名・作品名・統計の数字を書かせず、次の記号で指定させる。
音声化・画面づくりの前に、DB の値で機械的に置き換える。これで引用文は必ず原文と一致する。

- {{quote:<candidate_id>}}   一節の原文
- {{source:<candidate_id>}}  著者『作品名』（訳者訳）章（画面・語りで出典を言うとき）
- {{author:<candidate_id>}}  著者名
- {{title:<candidate_id>}}   『作品名』
- {{stat:<stat_id>}}         統計の内容と値（例：……は32.9%（労働安全衛生調査、2025年））
- {{worry:<worry_id>}}       一般化した悩み文
- {{fact:<fact_id>}}         背景の事実の確認済みの1文
- {{fact:<fact_id>.<項目>}}  その事実の項目（人名・地名・作品名・年・年齢・数。year_wareki で和暦）
- {{def:<J-…>}}               昔の辞書の語釈（資料のまま。「」の中に置く）
- {{def_src:<J-…>}}           その辞書（例：1891年（明治24年）の『言海』）
- {{interp:<I-…>}}            時代の解釈（訓読・古い訳・注釈。資料のまま。「」の中に置く）
- {{interp_src:<I-…>}}        その資料
"""

from __future__ import annotations

import re
from typing import Any

from koten.authors import author_label, translator_label
from koten.ref.store import ref_source_label, ref_text

PLACEHOLDER = re.compile(
    r"\{\{(quote|quote_ja|source|author|title|stat|worry|fact|def|def_src|interp|interp_src)"
    r":([^{}]+?)\}\}"
)
REF_KINDS = {"def": "J-", "def_src": "J-", "interp": "I-", "interp_src": "I-"}
ANY_BRACES = re.compile(r"\{\{[^{}]*\}\}")


class UnresolvedPlaceholder(KeyError):
    pass


def format_stat(s: dict[str, Any]) -> str:
    unit = s.get("unit") or ""
    value = f"{s['value']}{unit}"
    survey = s.get("survey") or ""
    year = f"、{s['year']}年" if s.get("year") else ""
    where = f"（{survey}{year}）" if survey else ""
    return f"{s['label']}は{value}{where}"


SECTION_MAX = 30  # 出典に入れる章の名前の上限（本文が混ざった長い章名を語らせない）


def short_section(section: str) -> str:
    """章の名前（「大見出し / 中見出し / 小見出し」）が長ければ深い見出しから外し、
    1つにしてもまだ長ければ切る。"""
    parts = [p.strip() for p in (section or "").split(" / ") if p.strip()]
    while len(parts) > 1 and len(" / ".join(parts)) > SECTION_MAX:
        parts.pop()
    out = " / ".join(parts)
    if len(out) > SECTION_MAX:
        out = out[:SECTION_MAX].rstrip() + "…"
    return out


def format_source(c: dict[str, Any]) -> str:
    author = author_label(c)
    translator = translator_label(c)
    translator = f"（{translator}訳）" if translator else ""
    section = short_section(c.get("section") or "")
    section = f"「{section}」" if section else ""
    return f"{author}『{c['title']}』{translator}{section}"


class Resolver:
    def __init__(
        self,
        candidates: dict[str, dict[str, Any]],
        stats: dict[str, dict[str, Any]],
        worries: dict[str, str],
        facts: dict[str, Any] | None = None,
        refs: dict[str, dict[str, Any]] | None = None,
    ):
        self.candidates = candidates
        self.stats = stats
        self.worries = worries
        self.facts = facts or {}  # この回の企画に付いた、使える事実だけ（koten.facts.model.Fact）
        self.refs = refs or {}  # この回の企画に付いた、確かめ済みの語釈・解釈だけ

    def value(self, kind: str, key: str) -> str:
        key = key.strip()
        if kind in ("quote", "quote_ja", "source", "author", "title"):
            c = self.candidates.get(key)
            if c is None:
                raise UnresolvedPlaceholder(f"{{{{{kind}:{key}}}}}：この回の一節にない候補")
            if kind == "quote":
                return c["quote_text"]
            if kind == "quote_ja":
                # 外国語の一節の自前訳（M8 で候補表に持たせる）。まだ無いので解決できない
                raise UnresolvedPlaceholder(f"{{{{quote_ja:{key}}}}}：自前訳がまだありません")
            if kind == "source":
                return format_source(c)
            if kind == "author":
                return author_label(c)
            return f"『{c['title']}』"
        if kind == "stat":
            s = self.stats.get(key)
            if s is None:
                raise UnresolvedPlaceholder(f"{{{{stat:{key}}}}}：この回の統計にない")
            return format_stat(s)
        if kind == "worry":
            w = self.worries.get(key)
            if w is None:
                raise UnresolvedPlaceholder(f"{{{{worry:{key}}}}}：この回の悩みにない")
            return w
        if kind == "fact":
            fid, _, slot = key.partition(".")
            fid, slot = fid.strip(), slot.strip()
            f = self.facts.get(fid)
            if f is None:
                raise UnresolvedPlaceholder(f"{{{{fact:{key}}}}}：この回の企画に無い事実")
            if not slot:
                return f.text_ja
            try:
                return f.slot_text(slot)
            except (KeyError, ValueError) as e:
                raise UnresolvedPlaceholder(f"{{{{fact:{key}}}}}：{e}") from e
        if kind in REF_KINDS:
            r = self.refs.get(key)
            if r is None or not key.startswith(REF_KINDS[kind]):
                raise UnresolvedPlaceholder(f"{{{{{kind}:{key}}}}}：この回の企画に無い語釈・解釈")
            return ref_source_label(r) if kind.endswith("_src") else ref_text(r)
        raise UnresolvedPlaceholder(f"{{{{{kind}:{key}}}}}")

    def resolve(self, text: str) -> str:
        out = PLACEHOLDER.sub(lambda m: self.value(m.group(1), m.group(2)), text)
        stray = ANY_BRACES.search(out)
        if stray:
            raise UnresolvedPlaceholder(f"{stray.group(0)}：使えない記号")
        return out

    def problems(self, text: str) -> list[str]:
        """解決できないプレースホルダの一覧（空なら全部解決できる）。"""
        found = []
        for m in PLACEHOLDER.finditer(text):
            try:
                self.value(m.group(1), m.group(2))
            except UnresolvedPlaceholder as e:
                found.append(str(e))
        rest = PLACEHOLDER.sub("", text)
        found += [f"{m.group(0)}：使えない記号" for m in ANY_BRACES.finditer(rest)]
        rest = ANY_BRACES.sub("", rest)
        if "{{" in rest or "}}" in rest:
            found.append("閉じていない記号（{{ か }} だけが残っている）")
        return found
