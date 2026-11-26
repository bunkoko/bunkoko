"""台本と DB から、M6 の部品に渡す形を作る（統合の側。docs/m6_design.md）。

- prepare_lines：読み上げる段落（Line）。画面の文は差し込んだ原文、読みは読みの直しを通した文
- card_specs：カードの中身（引用カード・章タイトル・統計）
- kit_input：説明欄の材料（どれも検査を通った台本の文か、DB・設定の値。AI に新しく書かせない）
- chapter_ranges：G7 の章の長さの範囲（formats.yaml から）

ここで読むのは SSD の DB と台本だけ（HDD は読まない）。
"""

from __future__ import annotations

from typing import Any

from koten.authors import author_label, translator_label
from koten.findings import FINDING_SEC
from koten.media.model import CardSpec, ClassicSource, KitInput, Line, split_sentences
from koten.script.placeholders import Resolver, format_source, short_section

# 画面に出すライセンスの短い表示（R7。詳しくは説明欄）
LICENSE_NOTE = {
    "PD_JP": "著作権の保護期間満了",
    "CC0": "CC0",
    "CC_BY": "CC BY（改変なし）",
}

# 決まった注意書き（AI に書かせない。docs/research/2026-11-05-description-and-evidence.md §4）
NOTICE_ALWAYS = "古典の読み方はひとつではありません。この動画の読み方は、そのひとつです。"
NOTICE_DATA = (
    "調査や研究は、ある時期・ある人たちを調べた結果で、すべての人に当てはまるものではありません。"
)
NOTICE_CHART = "グラフは、上の資料の数値をもとに当チャンネルが作りました。"
NOTICE_REFS = "昔の辞書や注釈は、その時代の説明として紹介しています。"


def _segments(script: dict[str, Any]):
    for ch in script["chapters"]:
        for seg in ch["segments"]:
            yield ch, seg


def prepare_lines(
    script: dict[str, Any],
    resolver: Resolver,
    overrides: dict[str, str] | None = None,
) -> list[Line]:
    """読み上げる段落の並び。

    - 一節（quote）：画面は原文。read_as が modern（旧字・旧仮名・外国語）なら、言葉は変えずに
      仮名づかいと字体を今に直して読む。古典の一節の日本語（{{quote_ja:}}。M8b）はその日本語を読む
    - 人が確かめた読み（昔の辞書・時代の解釈・古い日本語訳の reading）があれば、それで読む
      （Resolver.speak）。くの字点・踊り字は、どの段落でも開いて読む（speech_text）
    - 読みの直し（config/readings.csv）：key は「回の ID:段落の ID」（E20261107-01:ch2-s3。その回
      だけ）か候補の ID（どの回でも）。回の段落が先。sha の列があれば、画面の文がその sha のときだけ
    - 人の一言（human_take）は、その章の最後に足す（文字なら読み上げ、録った声ならその wav）
    """
    from koten.voice.reading import modernize, override_for, speech_text

    overrides = overrides or {}
    lines: list[Line] = []
    take = script.get("human_take") or {}
    for ch in script["chapters"]:
        for seg in ch["segments"]:
            display = resolver.resolve(seg["text"])
            ref = (seg.get("visual") or {}).get("ref")
            reading = override_for(
                overrides, f"{script['episode_id']}:{seg['seg_id']}", display
            ) or (override_for(overrides, ref, display) if ref else None)
            if reading is None:
                spoken = resolver.speak(seg["text"])
                if (
                    seg["kind"] == "quote"
                    and not seg.get("translation")
                    and seg.get("read_as") == "modern"
                ):
                    spoken = modernize(spoken)
                reading = speech_text(spoken)
            lines.append(
                Line(
                    chapter_id=ch["chapter_id"],
                    seg_id=seg["seg_id"],
                    kind=seg["kind"],
                    display=display,
                    reading=reading,
                    visual=dict(seg.get("visual") or {}),
                    chapter_title=resolver.resolve(ch.get("title", "")),
                    role=ch.get("role", ""),
                )
            )
        if take.get("chapter_id") == ch["chapter_id"] and (
            take.get("text") or take.get("audio_path")
        ):
            text = (take.get("text") or "").strip()
            lines.append(
                Line(
                    chapter_id=ch["chapter_id"],
                    seg_id=f"{ch['chapter_id']}-take",
                    kind="human_take",
                    display=text,
                    reading=speech_text(text) if text else "",
                    visual={"type": "photo", "ref": None},
                    chapter_title=resolver.resolve(ch.get("title", "")),
                    role=ch.get("role", ""),
                    audio_path=take.get("audio_path") or None,
                )
            )
    return lines


def card_specs(script: dict[str, Any], resolver: Resolver) -> list[CardSpec]:
    """カードの中身。写真の段落には作らない。"""
    out: list[CardSpec] = []
    for _, seg in _segments(script):
        visual = seg.get("visual") or {}
        kind, ref = visual.get("type"), visual.get("ref")
        if kind == "quote_card" and ref in resolver.candidates:
            c = resolver.candidates[ref]
            tr = seg.get("translation") or {}
            out.append(
                CardSpec(
                    seg_id=seg["seg_id"],
                    type="quote_card",
                    text=c["quote_text"],  # 画面はいつも原文（訳の段落でも）
                    source=format_source(c),
                    label=tr.get("label") or "",
                    license_note=LICENSE_NOTE.get(c.get("license") or "", ""),
                )
            )
        elif kind == "title_card":
            out.append(
                CardSpec(
                    seg_id=seg["seg_id"], type="title_card", title=resolver.resolve(seg["text"])
                )
            )
        elif kind == "chart" and ref in resolver.stats:
            s = resolver.stats[ref]
            stat = {k: s.get(k) for k in ("label", "value", "unit", "survey", "year")}
            out.append(CardSpec(seg_id=seg["seg_id"], type="chart", stat=stat))
    return out


def used_ids(script: dict[str, Any]) -> dict[str, list[str]]:
    """台本で使った一節・統計・事実・語釈と解釈・研究の ID（出てきた順。重ねない）。"""
    out: dict[str, list[str]] = {k: [] for k in ("quotes", "stats", "facts", "refs", "findings")}

    def add(key: str, ids: list[str]) -> None:
        for i in ids:
            if i and i not in out[key]:
                out[key].append(i)

    for _, seg in _segments(script):
        visual = seg.get("visual") or {}
        if seg["kind"] == "quote" and visual.get("ref"):
            add("quotes", [visual["ref"]])
        if seg["kind"] == "stat" and visual.get("ref"):
            add("stats", [visual["ref"]])
        add("facts", list(seg.get("facts") or []))
        add("refs", list(seg.get("refs") or []))
        add("findings", list(seg.get("findings") or []))
    return out


NUMBER_PLACEHOLDERS = ("{{stat:", "{{finding")


def _hook(script: dict[str, Any], resolver: Resolver) -> str:
    """①つかみ：冒頭の章の最初の語りの、最初の1〜2文（検査を通った文）。

    統計・研究の記号が入った語りは使わない（説明欄に調査の数値を書かない。D-036）。
    """
    for ch in script["chapters"]:
        for seg in ch["segments"]:
            if seg["kind"] == "narration" and not any(
                p in seg["text"] for p in NUMBER_PLACEHOLDERS
            ):
                return "".join(split_sentences(resolver.resolve(seg["text"]))[:2])
        break
    return ""


def kit_input(
    script: dict[str, Any],
    resolver: Resolver,
    *,
    fact_sources: dict[str, list[dict[str, Any]]],
    credits: list[str],
    hashtags: list[str],
    channel_name: str = "",
    books: list[dict[str, str]] | None = None,
    source_renderings: dict[str, dict[str, Any]] | None = None,
) -> KitInput:
    """説明欄の材料。

    source_renderings：古典の一節（canon。M8b）の画面の原文がどの版のものか（候補の source_id →
    renderings の行。ref_credit・ref_url・ref_pid・locator）。底本と URL の代わりに出す（R7）。
    """
    source_renderings = source_renderings or {}
    ids = used_ids(script)
    cands = [resolver.candidates[c] for c in ids["quotes"] if c in resolver.candidates]
    # 主役の作品：最初の一節の作品（出てきた順）
    main = cands[0] if cands else None
    worry = next(iter(resolver.worries.values()), "")
    data_note = "悩みの背景として、調査のデータも紹介します。" if ids["stats"] else ""
    promise = ""
    if main is not None:
        promise = (
            f"この動画では、{author_label(main)}『{main['title']}』などの古典の一節を手がかりに、"
            f"「{worry}」という悩みを考えます。{data_note}"
        )

    classics: list[ClassicSource] = []
    seen_works: set[str] = set()
    labels = {}
    for _, seg in _segments(script):
        ref = (seg.get("visual") or {}).get("ref")
        if ref and (seg.get("translation") or {}).get("label"):
            labels[ref] = seg["translation"]["label"]
    for cid in ids["quotes"]:
        c = resolver.candidates.get(cid)
        if c is None:
            continue
        key = f"{c.get('work_id')}|{c.get('section')}"
        if key in seen_works:
            continue
        seen_works.add(key)
        base_book, url = c.get("base_book") or "", c.get("url") or ""
        r = source_renderings.get(c.get("source_id") or "")
        if r is not None:
            # 古典の一節：画面の原文の版（「國譯漢文大成」など）とその場所・URL
            parts = (r.get("ref_credit") or "", r.get("locator") or "")
            base_book = "　".join(x for x in parts if x)
            url = r.get("ref_url") or (
                f"https://dl.ndl.go.jp/pid/{r['ref_pid']}" if r.get("ref_pid") else ""
            )
        classics.append(
            ClassicSource(
                title=c["title"],
                authors=author_label(c),
                translator=translator_label(c),
                section=short_section(c.get("section") or ""),
                base_book=base_book,
                url=url,
                license=c.get("license") or "",
                label=labels.get(cid, ""),
            )
        )

    data_sources: list[str] = []
    for sid in ids["stats"]:
        s = resolver.stats.get(sid)
        if s is None:
            continue
        y = str(s.get("year") or "").strip().removesuffix("年")
        year = f"（{y}年）" if y else ""
        line = f"{s.get('survey') or s.get('label')}{year}　{s.get('url') or ''}".strip()
        if line not in data_sources:
            data_sources.append(line)
    for fid in ids["findings"]:
        f = resolver.findings.get(fid) or {}
        # 説明欄には書誌（著者 (年). 題. 雑誌, 巻(号). DOI）。語りの言い方（〇〇大学の研究）ではない
        label = f.get("citation") or f.get("source_label") or ""
        if label and label not in data_sources:
            data_sources.append(label)

    background: list[str] = []
    for fid in ids["facts"]:
        for src in fact_sources.get(fid, []):
            if src.get("role") != "confirm" or src.get("kind") == "wikipedia":
                continue  # ウィキペディアは手がかりだけ（R2）
            # クレジット（CC BY の資料は決まった書き方）とライセンス（R7）
            head = src.get("credit") or src.get("title") or ""
            lic = src.get("license") or ""
            if lic in ("PD_JP", "PD"):
                lic = ""  # 保護期間満了の資料は書き方の決まりが無い
            parts = [head, src.get("locator") or "", src.get("url") or ""]
            if lic and lic not in head:
                parts.append(lic)
            line = "　".join(p for p in parts if p)
            if line and line not in background:
                background.append(line)
    from koten.ref.store import ref_source_label

    for rid in ids["refs"]:
        r = resolver.refs.get(rid)
        if r is None:
            continue
        url = r.get("ref_url") or (
            f"https://dl.ndl.go.jp/pid/{r['ref_pid']}" if r.get("ref_pid") else ""
        )
        parts = [r.get("ref_credit") or ref_source_label(r), r.get("locator") or "", url]
        line = "　".join(p for p in parts if p)
        if line not in background:
            background.append(line)

    notices = [NOTICE_ALWAYS]
    if ids["stats"] or ids["findings"]:
        notices.append(NOTICE_DATA)
    if any((seg.get("visual") or {}).get("type") == "chart" for _, seg in _segments(script)):
        notices.append(NOTICE_CHART)
    if ids["refs"]:
        notices.append(NOTICE_REFS)

    books = books or []
    amazon = (
        f"Amazonのアソシエイトとして、{channel_name}は適格販売により収入を得ています。"
        if books and channel_name
        else ""
    )
    return KitInput(
        episode_id=script["episode_id"],
        title=resolver.resolve(script.get("title") or ""),
        hook=_hook(script, resolver),
        promise=promise,
        classics=classics,
        data_sources=data_sources,
        background_sources=background,
        books=books,
        amazon_notice=amazon,
        credits=[c for c in credits if c],
        notices=notices,
        # 問いかけ：検査を通った文。{{author:}}・{{title:}}・{{worry:}} は使ってよい（script_v5）
        question=resolver.resolve((script.get("comment_prompt") or "").strip()),
        hashtags=hashtags[:3],
    )


def chapter_ranges(
    script: dict[str, Any], formats: dict[str, Any]
) -> dict[str, tuple[float, float]]:
    """G7 の章の長さの範囲。formats.yaml の役ごとの target_sec。背景の事実・語釈・研究が付いた章は、
    上の端に fact_sec・def_sec・finding_sec（研究1件ごと）の上の端を足す（長くなってよい。
    短くなるのは止めない）。
    役が formats.yaml に無い章は入れない。"""
    fmt = formats.get(script.get("format") or "", {})
    by_role = {c["role"]: c for c in fmt.get("chapters", [])}
    out: dict[str, tuple[float, float]] = {}
    for ch in script["chapters"]:
        conf = by_role.get(ch.get("role"))
        if conf is None:
            continue
        lo, hi = (float(x) for x in conf["target_sec"])
        has_facts = any(seg.get("facts") for seg in ch["segments"])
        has_refs = any(seg.get("refs") for seg in ch["segments"])
        if has_facts and conf.get("fact_sec"):
            hi += float(conf["fact_sec"][1])
        if has_refs:
            hi += float((conf.get("def_sec") or [20, 30])[1])  # 企画（planner.ref_sec）と同じ既定
        # 研究の一文と限界は台帳の決まった文で、書き手が短くできないので、使った研究の数だけ足す。
        # 研究の章（形式A）は、もともと研究を読む章の長さなので足さない
        findings = {f for seg in ch["segments"] for f in seg.get("findings") or []}
        if findings and ch.get("role") != "research":
            hi += len(findings) * float((conf.get("finding_sec") or FINDING_SEC)[1])
        out[ch["chapter_id"]] = (lo, hi)
    return out
