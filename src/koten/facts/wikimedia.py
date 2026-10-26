"""Wikidata と日本語版ウィキペディアから、背景の事実の手がかりを取る（net.py を通す）。

- Wikidata（CC0）：青空文庫の作家ID（P7311）・作品ID（P7312）で項目を見つけ、生没年・出身地・
  刊行日などを読む。出典（P248・P854 など）が付いた記述だけを「確かめに使える」とみなす
  （P143「ウィキメディア・プロジェクトから取り込み」だけの出典は、ウィキペディアと同じ扱い）
- ウィキペディア（CC BY-SA 4.0）：記事の wikitext を HDD の raw/wikipedia/ に置き、AI の下書きの
  手がかりにだけ使う。索引にせず、台本を書く LLM と判定役には渡さない（SPEC R2・D-013）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from koten.net import Fetcher

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
ENTITY_URL = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
WIKIPEDIA_API = "https://ja.wikipedia.org/w/api.php"
AOZORA_AUTHOR = "P7311"  # 青空文庫著作家識別子（例：879。0埋めなし）
AOZORA_WORK = "P7312"  # 青空文庫作品識別子（例：000879/card127）
WEAK_REF = {"P143", "P4656", "P813"}  # 取り込み元・取り込みURL・参照日だけの出典は弱い

# 読む項目（死因 P509・死に方 P1196・肖像 P18 は取らない）
PERSON_PROPS = {
    "P569": "生年月日",
    "P570": "没年月日",
    "P19": "出生地",
    "P20": "死没地",
    "P69": "出身校",
    "P106": "職業",
    "P135": "流派・運動",
    "P737": "影響を受けた人",
    "P1066": "師",
    "P3373": "きょうだい",
    "P463": "所属",
    "P800": "主な作品",
}
WORK_PROPS = {
    "P577": "刊行日",
    "P50": "著者",
    "P655": "訳者",
    "P407": "言語",
    "P291": "刊行地",
    "P123": "出版社",
    "P629": "原著（版の元の作品）",
    "P1476": "題名",
}


@dataclass
class Claim:
    prop: str
    label: str
    value: str  # 日付は YYYY-MM-DD（細かさまで）、項目は QID、文字列はそのまま
    referenced: bool  # 取り込み元だけでない出典があるか
    refs: list[str] = field(default_factory=list)


def aozora_author_value(person_id: str) -> str:
    """aozora:person:000879 → 879（P7311 の値は0埋めしない）。"""
    num = person_id.rsplit(":", 1)[-1]
    return str(int(num)) if num.isdigit() else num


def aozora_work_value(card_url: str) -> str | None:
    """図書カードURL https://www.aozora.gr.jp/cards/000879/card127.html → 000879/card127。"""
    marker = "/cards/"
    if marker not in card_url or not card_url.endswith(".html"):
        return None
    return card_url.split(marker, 1)[1][: -len(".html")]


def find_qid(fetcher: Fetcher, prop: str, value: str) -> str | None:
    """haswbstatement で項目を探す。ちょうど1件のときだけ返す（取り違えを避ける）。"""
    params = {
        "action": "query",
        "list": "search",
        "srsearch": f'haswbstatement:"{prop}={value}"',
        "srlimit": "5",
        "format": "json",
    }
    data = fetcher.fetch_json(f"{WIKIDATA_API}?{urlencode(params)}", "search")
    hits = [h["title"] for h in (data.get("query") or {}).get("search", [])]  # type: ignore[union-attr]
    return hits[0] if len(hits) == 1 else None


def entity(fetcher: Fetcher, qid: str) -> dict[str, Any]:
    data = fetcher.fetch_json(ENTITY_URL.format(qid=qid), "entity")
    entities = data["entities"]  # type: ignore[index]
    return entities.get(qid) or next(iter(entities.values()))


def labels(fetcher: Fetcher, qids: list[str]) -> dict[str, str]:
    """QID の日本語の名前（無ければ英語）。50件ずつ。"""
    out: dict[str, str] = {}
    qids = sorted(set(qids))
    for i in range(0, len(qids), 50):
        params = {
            "action": "wbgetentities",
            "ids": "|".join(qids[i : i + 50]),
            "props": "labels",
            "languages": "ja|en",
            "format": "json",
        }
        data = fetcher.fetch_json(f"{WIKIDATA_API}?{urlencode(params)}", "labels")
        for qid, e in (data.get("entities") or {}).items():  # type: ignore[union-attr]
            lab = e.get("labels") or {}
            name = (lab.get("ja") or lab.get("en") or {}).get("value")
            if name:
                out[qid] = name
    return out


def claims(e: dict[str, Any], props: dict[str, str]) -> list[Claim]:
    out = []
    for prop, label in props.items():
        for st in (e.get("claims") or {}).get(prop, []):
            if st.get("rank") == "deprecated":
                continue
            dv = (st.get("mainsnak") or {}).get("datavalue") or {}
            value = _value(dv)
            if value is None:
                continue
            refs = []
            strong = False
            for ref in st.get("references") or []:
                keys = set((ref.get("snaks") or {}).keys())
                if keys - WEAK_REF:
                    strong = True
                for p in ("P248", "P854"):
                    for snak in (ref.get("snaks") or {}).get(p, []):
                        v = _value((snak or {}).get("datavalue") or {})
                        if v:
                            refs.append(f"{p}:{v}")
            out.append(Claim(prop, label, value, strong, refs))
    return out


def _value(dv: dict[str, Any]) -> str | None:
    t, v = dv.get("type"), dv.get("value")
    if t == "time":
        time, precision = v["time"], int(v.get("precision", 9))
        ymd = time.lstrip("+").split("T")[0]
        y, m, d = (ymd.split("-") + ["00", "00"])[:3]
        if precision >= 11 and m != "00" and d != "00":
            return f"{y}-{m}-{d}"
        if precision == 10 and m != "00":
            return f"{y}-{m}"
        return y
    if t == "wikibase-entityid":
        return v.get("id")
    if t == "monolingualtext":
        return v.get("text")
    if t == "string":
        return v
    return None


def ja_title(e: dict[str, Any]) -> str | None:
    return ((e.get("sitelinks") or {}).get("jawiki") or {}).get("title")


def wikipedia_wikitext(fetcher: Fetcher, title: str) -> tuple[int, str] | None:
    """記事の最新版の (版番号, wikitext)。手がかりとしてだけ使う（R2）。"""
    params = {
        "action": "query",
        "prop": "revisions",
        "rvprop": "content|ids",
        "rvslots": "main",
        "titles": title,
        "format": "json",
        "formatversion": "2",
    }
    data = fetcher.fetch_json(f"{WIKIPEDIA_API}?{urlencode(params)}", "wikitext")
    pages = (data.get("query") or {}).get("pages") or []  # type: ignore[union-attr]
    if not pages or pages[0].get("missing"):
        return None
    rev = pages[0]["revisions"][0]
    return int(rev["revid"]), rev["slots"]["main"]["content"]


def wikipedia_url(title: str, revid: int | None = None) -> str:
    base = f"https://ja.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
    return f"https://ja.wikipedia.org/w/index.php?oldid={revid}" if revid else base
