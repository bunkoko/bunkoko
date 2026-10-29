"""検証ゲート G1〜G6（SPEC §8.8）。G7（尺）は音声合成のあと（M6）。

各ゲートは {"ok": bool, "issues": [...]} を返す。結果は verification_json に保存し、確認画面に出す。
- G1 差し込み：全プレースホルダが解決でき、「」の中は差し込んだ引用か悩み文だけ。
  著者名・作品名・統計の数字を直接書いていない（R6）
- G2 ライセンス：使う一節が全て使用可（PD_JP・CC0・CC_BY）。外国語は自前訳が要る（R5）
- G3 忠実さ：一節の解説が一節と前後の文脈から言える範囲（別の LLM が判定）
- G4 再使用間隔：主役の作品・著者が直近の回と重ならない
- G5 安全：悩みが blocked でなく除外キーワードに当たらず、一節に caution がない
- G6 要確認ソース：要確認の一節・自前訳を含めば review_required。企画のあとで事実が変わった・
  使えなくなったときも人の確認へ

背景の事実（SPEC R6・D-012）：G1 は、事実の記号が章に渡した事実を指しているか、語りに年・元号・
世紀・「百年前」や、事実の項目（人名・地名・作品名など）・書誌の人名を直接書いていないかを見る。
G3 は、背景の事実を使った章を、1文ずつ「一節・前後の文脈・使った事実」と照らし合わせる（判定役は
文ごとの種類と根拠の事実IDを返し、合否はコードが決める）。

昔の辞書の語釈・時代の解釈（D-015・M8a）：G1 は、記号が章に渡した語釈・解釈を指し、本文の記号が
「」の中にあり、同じ段落に出典の記号があるか、書名や本文を直接書いていないかを見る。「辞書では」
「本来の意味」「語源」は語釈・解釈の記号と同じ文のときだけ、「本当の意味」「通説」「多くの学者」と
今の辞書の名前（広辞苑など）は使わせない。G3 は、語釈・解釈を使った文を、その本文と照らし合わせる。

ゲートとは別に、視聴の確かめ（冒頭30秒・タイトル・尺の見積もり）をする。不合格でも回は止めず、
章を書き直させ、直らなければ確認画面に出す（docs/decisions.md D-011）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from koten.license import is_usable
from koten.llm import ChatLLM, fill, load_prompt, parse_json_object
from koten.ref.store import ref_source_label, ref_text
from koten.script.data import EpisodeData
from koten.script.placeholders import PLACEHOLDER, Resolver
from koten.textnorm import normalize_for_match

QUOTED = re.compile(r"「([^「」]*)」")
FACT_REF = re.compile(r"\{\{fact:\s*([^{}.\s]+)\s*(?:\.\s*([^{}\s]+)\s*)?\}\}")
NUM = "0-9０-９"
KANJI_DIGIT = "〇一二三四五六七八九"
KANJI_NUM = KANJI_DIGIT + "十百千"
NOT_ERA = r"(?!\s*(?:代|時代))"  # 「2020年代」「人生100年時代」は年の主張ではない
# 背景の主張に当たる年の言い方（語りに直接書かず、事実の項目の記号で書く）
HISTORICAL = [
    (re.compile(rf"(?<![{NUM}])[{NUM}]{{3,4}}\s*(?:（[^）]{{1,8}}）\s*)?年{NOT_ERA}"), "年"),
    (re.compile(rf"(?<![{KANJI_NUM}])[一二][{KANJI_DIGIT}]{{3}}\s*年{NOT_ERA}"), "漢数字の年"),
    (re.compile(rf"千[一二三四五六七八九]?百[{KANJI_DIGIT}十]*\s*年{NOT_ERA}"), "漢数字の年"),
    (
        re.compile(
            rf"(明治|大正|昭和|平成|令和|慶応|元治|文久|万延|安政|嘉永|弘化|天保)"
            rf"\s*[{NUM}{KANJI_NUM}元]+\s*年"
        ),
        "元号の年",
    ),
    (re.compile(rf"[{NUM}{KANJI_NUM}]+\s*世紀"), "世紀"),
    (
        re.compile(
            rf"(?:[{NUM}]{{3,}}|[一二三四五六七八九]?[百千][{KANJI_NUM}]*)"
            rf"\s*年(前|あまり|余り|以上|近く)"
        ),
        "「○百年前」のような年数",
    ),
]
# 背景の段落（事実の記号を使った段落）では、年齢・月・日・人数などの数も直接書かない
BG_NUMBER = re.compile(
    rf"(?:[{NUM}]+|[{KANJI_NUM}]{{2,}})\s*(?:歳|才|か月|ヶ月|カ月|月|日|人|回|冊|年)"
)
# 記号は「1906年」「『作品名』」「「雑誌名」」の形で入るので、単位や括弧を重ねない
DOUBLED = re.compile(
    r"\{\{fact:[^{}]+\.(?:year|month|age)\d*(?:_wareki)?\s*\}\}\s*[年月歳才]"
    r"|『\s*\{\{(?:fact:[^{}]+\.title\d*|title:[^{}]+)\s*\}\}\s*』"
    r"|「\s*\{\{fact:[^{}]+\.periodical\d*\s*\}\}\s*」"
)
BG = "背景："  # 背景の事実にかかわる問題の頭（背景を外して書き直すかを決めるのに使う）
REF = "資料："  # 語釈・解釈にかかわる問題の頭（語釈・解釈を外して書き直すかを決めるのに使う）
REF_REF = re.compile(r"\{\{(def|def_src|interp|interp_src):\s*([^{}\s]+)\s*\}\}")
REF_BODY = re.compile(r"\{\{(def|interp):\s*([^{}\s]+)\s*\}\}")
REF_BODY_QUOTED = re.compile(r"「\s*\{\{(def|interp):\s*([^{}\s]+)\s*\}\}\s*」")
# 辞書や昔の本が言ったことにする言い方。同じ文に語釈・解釈の記号があるときだけ使える
REF_CLAIM = re.compile(
    r"辞書(?:では|によると|によれば|を引くと|を引けば|には|の説明)|本来の意味|もともとの意味|"
    r"元々の意味|語源"
)
# 根拠を DB に持てない言い方（いつも使わない）
NEVER_CLAIM = re.compile(r"本当の意味|通説|多くの学者|学者の間で|学説")
# 今の辞書・今の訳注の名前（語りでは出さない。D-027。照合に使う本は説明欄の参考文献だけ）
MODERN_REFS = (
    "広辞苑",
    "大辞林",
    "大辞泉",
    "日本国語大辞典",
    "新明解",
    "明鏡国語",
    "三省堂国語辞典",
    "岩波国語辞典",
    "新選国語辞典",
    "大漢和",
    "新漢語林",
    "漢辞海",
    "新釈漢文大系",
    "全釈漢文大系",
)
# 時代の人全体の考えにする言い方（資料や事実の記号で言う）
ERA_PEOPLE = re.compile(
    r"(?:江戸|明治|大正|昭和|平安|鎌倉|室町|戦国|当時)(?:時代)?の(?:人々|人|日本人|学者|読者)(?:は|が|も)"
)
SHORT_QUOTE_MAX = 10  # 「」で語を強調するだけなら許す長さ
JUDGED_ROLES = {"work_intro", "summary", "compare"}  # 一節を持たないが、一節に触れる章
# 一節を持たない章のうち、1文ずつの判定（judge_v2）にかける章（背景の作り話が紛れやすい）
SENTENCE_ROLES = {"opening", "context", "ending", "research"}
SENTENCE_TYPES = {
    "passage",
    "background",
    "connective",
    "interpretation",
    "application",
    "question",
    "reference",
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "faithful": {"type": "boolean"},
        "problems": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["faithful", "problems"],
    "additionalProperties": False,
}


def resolver_for(data: EpisodeData) -> Resolver:
    worries = {wid: w["text_general"] for wid, w in data.worries.items()}
    return Resolver(data.candidates, data.stats, worries, data.facts, data.refs)


def _gate(issues: list[str], **extra: Any) -> dict[str, Any]:
    return {"ok": not issues, "issues": issues, **extra}


# --- G1 ---------------------------------------------------------------------


def _names(data: EpisodeData) -> set[str]:
    """記号で書かせる著者名・訳者名と『作品名』（1字の名前は普通の語と区別できないので除く）。"""
    names: set[str] = set()
    for c in data.candidates.values():
        people = (c.get("author_names") or []) + (c.get("translator_names") or [])
        names.update(n for n in people if len(n) >= 2)
        if len(c["title"]) >= 2:
            names.add(f"『{c['title']}』")
    return names


def _passage_text(chapter: dict[str, Any], data: EpisodeData) -> str:
    """その章の一節と前後の文脈（ここに出てくる語は、語りで使っても直書きとみなさない）。"""
    ids = chapter.get("sources") or []
    return "\n".join(
        f"{data.candidates[c]['quote_text']}\n{data.candidates[c].get('context_text') or ''}"
        for c in ids
        if c in data.candidates
    )


def fact_leaks(text: str, data: EpisodeData, passage: str, facts: set[str]) -> list[str]:
    """記号を取り除いた語りに、背景の年・数・事実の項目・書誌の人名が直接書かれていないか。

    事実の項目は facts（この章に渡した事実と、この段落が指す事実）のものだけを見る。作品名は
    『』つき、雑誌・新聞名は「」つきで探す（『こころ』のような題が、普通の言葉と重なるため）。
    その章の一節と前後の文脈（passage）に出てくる語は除く。
    """
    bare = PLACEHOLDER.sub("", text)
    out = []
    for pattern, label in HISTORICAL:
        for m in pattern.finditer(bare):
            if m.group(0) not in passage:
                out.append(f"{BG}{label}「{m.group(0)}」を直接書いている（年は事実の記号で書く）")
    if FACT_REF.search(text):
        for m in BG_NUMBER.finditer(bare):
            if m.group(0) not in passage:
                out.append(
                    f"{BG}背景の段落に数「{m.group(0)}」を直接書いている"
                    "（年齢・月などは事実の記号で書く。事実に無い数は書かない）"
                )
    for fid in sorted(facts & set(data.facts)):
        for name, value in data.facts[fid].slots.items():
            v = str(value)
            if name.startswith(("year", "month", "age", "number")) or len(v) < 2:
                continue
            if name.startswith("title"):
                v = f"『{v}』"
            elif name.startswith("periodical"):
                v = f"「{v}」"
            if v in bare and v not in passage:
                out.append(f"{BG}「{v}」を直接書いている（{{{{fact:{fid}.{name}}}}} で書く）")
    for name in data.person_names:
        if name in bare and name not in passage:
            out.append(f"{BG}人名「{name}」を直接書いている（記号で書く）")
    return list(dict.fromkeys(out))


def ref_issues(text: str, data: EpisodeData, offered: set[str], kind: str) -> list[str]:
    """語釈・解釈の記号の使い方と、辞書や昔の本が言ったことにする言い方（段落1つ分）。"""
    out = []
    bodies = [m.group(2) for m in REF_BODY.finditer(text)]
    quoted = [m.group(2) for m in REF_BODY_QUOTED.finditer(text)]
    for rid in sorted(set(bodies)):
        if bodies.count(rid) != quoted.count(rid):
            out.append(f"{REF}語釈・解釈 {rid} は「」の中に記号だけを書く（「{{{{def:ID}}}}」）")
        if not re.search(rf"\{{\{{(?:def|interp)_src:\s*{re.escape(rid)}\s*\}}\}}", text):
            out.append(
                f"{REF}語釈・解釈 {rid} の出典の記号（{{{{def_src:…}}}} など）が同じ段落に無い"
            )
    bare = PLACEHOLDER.sub("", text)
    for rid in sorted(offered & set(data.refs)):
        r = data.refs[rid]
        if f"『{r['ref_title']}』" in bare:
            out.append(f"{REF}書名『{r['ref_title']}』を直接書いている（出典の記号で書く）")
        body = ref_text(r)
        if len(body) >= 8 and body in bare:
            out.append(f"{REF}語釈・解釈 {rid} の本文を記号を使わずに書いている")
    if kind != "narration":
        return out
    for sent in split_sentences(text):
        claim = REF_CLAIM.search(PLACEHOLDER.sub("", sent))
        if claim and not REF_REF.search(sent):
            out.append(
                f"「{claim.group(0)}」は、渡した語釈・解釈の記号と同じ文のときだけ言える"
                "（今の辞書の説明は、自分たちの言葉だと分かる言い方にする）"
            )
    if never := NEVER_CLAIM.search(bare):
        out.append(f"「{never.group(0)}」は根拠を示せないので言わない")
    out += [f"今の辞書・訳注の名前「{n}」を出さない" for n in MODERN_REFS if n in bare]
    if era := ERA_PEOPLE.search(bare):
        out.append(
            f"{BG}「{era.group(0)}」のように時代の人全体の考えにしない（資料や事実の記号で言う）"
        )
    return out


def g1_chapter(
    chapter: dict[str, Any],
    data: EpisodeData,
    shape_issues: list[str],
    allowed_facts: set[str] | None = None,
    offered_facts: set[str] | None = None,
    allowed_refs: set[str] | None = None,
    offered_refs: set[str] | None = None,
) -> list[str]:
    """allowed_facts：この章で記号で指してよい事実（章に渡した事実と、前の章で使った事実）。
    offered_facts：この章に渡した事実（その項目の直書きを見る）。
    allowed_refs・offered_refs：語釈・解釈について同じ。"""
    resolver = resolver_for(data)
    offered_all = set(chapter.get("fact_ids") or []) if offered_facts is None else offered_facts
    allowed = offered_all if allowed_facts is None else allowed_facts
    offered_r = set(chapter.get("ref_ids") or []) if offered_refs is None else offered_refs
    allowed_r = offered_r if allowed_refs is None else allowed_refs
    passage = _passage_text(chapter, data)
    issues = [f"{chapter['chapter_id']}：{x}" for x in shape_issues]
    quotes = [normalize_for_match(c["quote_text"]) for c in data.candidates.values()]
    worries = [normalize_for_match(w["text_general"]) for w in data.worries.values()]
    names = _names(data)
    stat_values = [
        str(s["value"]) for s in data.stats.values() if re.search(r"\d", str(s["value"]))
    ]
    for seg in chapter["segments"]:
        sid, text = seg["seg_id"], seg["text"]
        for p in resolver.problems(text):
            issues.append(f"{sid}：解決できない記号 {p}")
        refs = {fid for fid, _ in FACT_REF.findall(text)}
        for fid in sorted(refs):
            if fid in data.facts and fid not in allowed:
                issues.append(f"{sid}：{BG}この章に渡していない事実 {fid} を使っている")
        if seg["kind"] == "transition" and refs:
            issues.append(f"{sid}：{BG}章タイトルに事実の記号を使わない")
        rrefs = {rid for _, rid in REF_REF.findall(text)}
        for rid in sorted(rrefs):
            if rid in data.refs and rid not in allowed_r:
                issues.append(f"{sid}：{REF}この章に渡していない語釈・解釈 {rid} を使っている")
        if seg["kind"] == "transition" and rrefs:
            issues.append(f"{sid}：{REF}章タイトルに語釈・解釈の記号を使わない")
        if seg["kind"] in ("narration", "transition"):
            issues += [
                f"{sid}：{x}" for x in ref_issues(text, data, offered_r | rrefs, seg["kind"])
            ]
        if DOUBLED.search(text):
            issues.append(
                f"{sid}：記号のあとに単位や括弧を重ねている（記号は「1906年」「『作品名』」の形で入る）"
            )
        if seg["kind"] in ("narration", "transition"):
            offered = offered_all | refs
            issues += [f"{sid}：{x}" for x in fact_leaks(text, data, passage, offered)]
        if seg["kind"] == "transition":
            bare_title = PLACEHOLDER.sub("", text)
            issues += [
                f"{sid}：章タイトルに著者名・作品名「{n}」を直接書いている"
                for n in names
                if n in bare_title
            ]
        if seg["kind"] != "narration":
            continue
        for m in QUOTED.finditer(text):
            inner = m.group(1).strip()
            pm = PLACEHOLDER.fullmatch(inner)
            if pm and pm.group(1) in ("quote", "worry", "def", "interp"):
                continue
            norm = normalize_for_match(inner)
            if len(norm) <= SHORT_QUOTE_MAX:
                continue
            if any(norm in q for q in quotes) or any(norm in w for w in worries):
                continue  # 一節や悩み文の一部をそのまま引いている
            issues.append(f"{sid}：「」の中が一節でも悩み文でもない（{inner[:30]}）")
        bare = PLACEHOLDER.sub("", text)
        for name in names:
            if name in bare:
                issues.append(f"{sid}：著者名・作品名「{name}」を直接書いている（記号を使う）")
        for v in stat_values:
            if re.search(rf"(?<![\d.]){re.escape(v)}(?![\d.])", bare):
                issues.append(f"{sid}：統計の数字「{v}」を直接書いている（{{{{stat:…}}}} を使う）")
    return issues


# --- G2・G4・G5・G6 ---------------------------------------------------------


def g2_license(data: EpisodeData) -> dict[str, Any]:
    issues = []
    for s in data.plan["selection"]:
        c = data.candidates.get(s["candidate_id"])
        if c is None:
            issues.append(f"{s['candidate_id']}：候補表に無い")
            continue
        if not is_usable(c["license"]):
            issues.append(f"{s['candidate_id']}：ライセンス {c['license']} は使えない（R1）")
        if (c.get("lang") or "ja") != "ja":
            issues.append(f"{s['candidate_id']}：外国語の一節には自前訳が要る（R5。M8 で対応）")
    return _gate(issues)


def g4_recency(
    conn: sqlite3.Connection, data: EpisodeData, work_episodes: int, author_episodes: int
) -> dict[str, Any]:
    created = conn.execute(
        "SELECT created_at FROM episodes WHERE episode_id = ?", (data.episode_id,)
    ).fetchone()[0]

    def recent(column: str, n: int) -> set[str]:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT episode_id FROM episodes WHERE state != 'discarded' AND episode_id != ? "
                "AND created_at <= ? ORDER BY created_at DESC, episode_id DESC LIMIT ?",
                (data.episode_id, created, n),
            )
        ]
        if not ids:
            return set()
        marks = ",".join("?" for _ in ids)
        return {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT {column} FROM usage WHERE role = 'main' AND {column} IS NOT NULL "
                f"AND episode_id IN ({marks})",
                ids,
            )
        }

    works, authors = recent("work_id", work_episodes), recent("author_id", author_episodes)
    issues = []
    for s in data.plan["selection"]:
        if s["work_id"] in works:
            issues.append(f"{s['title']}：直近{work_episodes}回で主役にした作品")
        for a in s.get("author_ids") or []:
            if a in authors:
                issues.append(f"{s['title']}：直近{author_episodes}回で主役にした著者")
    return _gate(issues)


def g5_safety(data: EpisodeData, categories: dict[str, list[str]]) -> dict[str, Any]:
    from koten.worries.master import keyword_block

    issues = []
    for wid, w in data.worries.items():
        if w["status"] != "active":
            issues.append(f"{wid}：悩みが {w['status']}（{w.get('blocked_reason') or ''}）")
        hit = keyword_block(w["text_general"] or w["text_raw"], categories)
        if hit:
            issues.append(f"{wid}：除外カテゴリ「{hit[0]}」のキーワード（{hit[1]}）")
    for cid, c in data.candidates.items():
        if c.get("caution"):
            issues.append(f"{cid}：注意フラグ（{c.get('caution_reason') or ''}）")
    return _gate(issues)


def g6_review(data: EpisodeData, script: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    for cid, c in data.candidates.items():
        if c.get("review_required"):
            reasons.append(f"{cid}：要確認のソース（NDL など）")
    for ch in script["chapters"]:
        for seg in ch["segments"]:
            if seg["text"].startswith("{{quote_ja:"):
                reasons.append(f"{seg['seg_id']}：自前訳（試訳）を含む")
    reasons += [f"背景の事実 {x}" for x in data.fact_issues]
    reasons += [f"語釈・解釈 {x}" for x in data.ref_issues]
    used = {r for ch in script["chapters"] for r in ch.get("refs", [])}
    for rid in sorted(used):
        r = data.refs.get(rid)
        if r is not None and r.get("ref_source") == "ndl":
            reasons.append(f"{rid}：NDL の資料の本文（画面のページ画像と照らし合わせる）")
    # G6 は不合格ではなく「人の確認待ち」にする
    return {"ok": True, "issues": [], "review_required": bool(reasons), "reasons": reasons}


# --- 視聴の確かめ（冒頭30秒・タイトル） ---------------------------------------

HOOK_SEC = 30  # 冒頭のこの秒数のうちに悩みを言う（YouTube Studio の「イントロ」も30秒時点の割合）
# 冒頭を挨拶やチャンネルの話に使わない
OPENING_NG = ("こんにちは", "こんばんは", "ようこそ", "チャンネル", "高評価")
TITLE_MAX = 40
TITLE_KINDS = {"author", "title", "worry"}  # タイトルで使える記号
# 中身で約束できない言い方（クリックは増えても、見て満足されなければおすすめに載らない）
TITLE_NG = ("絶対", "必ず", "衝撃", "激変", "人生が変わる", "最強", "知らないと損")
# 語釈・解釈を約束する言い方（タイトルでは記号を使えず、根拠を示せない）
TITLE_REF_NG = ("本当の意味", "語源", "辞書では", "辞書によると")


def _length(resolver: Resolver, text: str) -> int:
    try:
        return len(resolver.resolve(text))
    except KeyError:
        return len(text)


def opening_issues(chapter: dict[str, Any], resolver: Resolver, chars_per_min: int) -> list[str]:
    """オープニングが、冒頭30秒のうちに悩みを言い、挨拶やチャンネルの話をしていないか。"""
    hook_chars = HOOK_SEC * chars_per_min // 60
    issues = []
    pos, worry_at = 0, None
    for seg in chapter["segments"]:
        if seg["kind"] == "transition":
            continue
        i = seg["text"].find("{{worry:")
        if i >= 0 and worry_at is None:
            worry_at = pos + _length(resolver, seg["text"][:i])
        pos += _length(resolver, seg["text"])
    if worry_at is None or worry_at >= hook_chars:
        where = "言っていない" if worry_at is None else f"{worry_at}字目まで出てこない"
        issues.append(
            f"冒頭の約{hook_chars}字（{HOOK_SEC}秒）のうちに、今日の悩みを {{{{worry:悩みID}}}} で"
            f"言う（今は{where}）"
        )
    narration = [seg["text"] for seg in chapter["segments"] if seg["kind"] == "narration"]
    bare = PLACEHOLDER.sub("", "".join(narration))
    if found := [w for w in OPENING_NG if w in bare]:
        issues.append(
            f"オープニングで挨拶やチャンネルの話（「{'」「'.join(found)}」）をしない。"
            "すぐ悩みから入る（登録のお願いはエンディングで）"
        )
    return issues


def title_issues(title: str, data: EpisodeData) -> list[str]:
    """動画のタイトル案が、記号の決まりと長さを守り、中身で約束できない言い方をしていないか。"""
    if not title:
        return ["動画のタイトル案（episode_title）が無い"]
    resolver = resolver_for(data)
    issues = [f"タイトルの記号を解決できない：{p}" for p in resolver.problems(title)]
    for m in PLACEHOLDER.finditer(title):
        if m.group(1) not in TITLE_KINDS:
            issues.append(f"タイトルで使える記号は author・title・worry だけ（{m.group(0)}）")
    bare = PLACEHOLDER.sub("", title)
    issues += [
        f"タイトルに著者名・作品名「{n}」を直接書いている（記号を使う）"
        for n in sorted(_names(data))
        if n in bare
    ]
    issues += [x.removeprefix(BG) for x in fact_leaks(title, data, "", set())]
    if not issues and (n := _length(resolver, title)) > TITLE_MAX:
        issues.append(f"タイトルが長い（{n}字。差し込んだあとで{TITLE_MAX}字以内）")
    issues += [
        f"タイトルに「{w}」のような約束できない言い方を使わない" for w in TITLE_NG if w in bare
    ]
    issues += [f"タイトルに「{w}」を使わない" for w in TITLE_REF_NG if w in bare]
    issues += [f"タイトルに今の辞書・訳注の名前「{n}」を出さない" for n in MODERN_REFS if n in bare]
    return issues


# --- G3 ---------------------------------------------------------------------


JUDGE_SENTENCES_SCHEMA = {
    "type": "object",
    "properties": {
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "type": {
                        "type": "string",
                        "enum": [
                            "passage",
                            "background",
                            "connective",
                            "interpretation",
                            "application",
                            "question",
                            "reference",
                        ],
                    },
                    "supported_by": {"type": "array", "items": {"type": "string"}},
                    "ok": {"type": "boolean"},
                    "problem": {"type": "string"},
                },
                "required": ["n", "type", "supported_by", "ok", "problem"],
                "additionalProperties": False,
            },
        },
        "passage_problems": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sentences", "passage_problems"],
    "additionalProperties": False,
}
SENTENCE_END = "。！？!?"
OPEN_Q, CLOSE_Q = "「『（", "」』）"


def split_sentences(text: str) -> list[str]:
    """文に分ける。「」『』（）の中の「。」では切らない（引用した語釈や一節が文の途中で切れないように）。"""
    out, buf, depth = [], [], 0
    for ch in text:
        buf.append(ch)
        if ch in OPEN_Q:
            depth += 1
        elif ch in CLOSE_Q and depth:
            depth -= 1
        elif ch in SENTENCE_END and depth == 0:
            out.append("".join(buf))
            buf = []
    if "".join(buf).strip():
        out.append("".join(buf))
    return [x.strip() for x in out if x.strip()]


MARKER = re.compile(r"〔([^〔〕]+)〕")


class FaithfulnessJudge:
    def __init__(
        self, llm: ChatLLM, prompts_dir, prompt: str = "judge_v1", facts_prompt: str = "judge_v3"
    ):
        self.llm = llm
        self.system, self.user_tpl = load_prompt(prompts_dir, prompt)
        self.f_system, self.f_user_tpl = load_prompt(prompts_dir, facts_prompt)

    def needs_judge(self, chapter: dict[str, Any]) -> bool:
        """語りのある章はすべて判定する（オープニング・悩みの場面・エンディングも）。"""
        return any(seg["kind"] == "narration" for seg in chapter["segments"])

    def _passages(self, chapter: dict[str, Any], data: EpisodeData) -> str:
        from koten.script.placeholders import format_source

        ids = chapter.get("sources") or [s["candidate_id"] for s in data.plan["selection"]]
        return "\n".join(
            f"- 出典：{format_source(data.candidates[c])}\n"
            f"  一節：{data.candidates[c]['quote_text']}\n"
            f"  前後の文脈：{data.candidates[c]['context_text']}"
            for c in ids
            if c in data.candidates
        )

    def judge(
        self,
        chapter: dict[str, Any],
        data: EpisodeData,
        prior_facts: list[str] | None = None,
        title: str = "",
        prior_refs: list[str] | None = None,
    ) -> list[str]:
        """問題の一覧（空なら合格）。判定できなかったときも問題として返す。

        この章か前の章で背景の事実・語釈・解釈を使ったとき、また一節を持たない章（オープニング・
        悩みの場面・エンディング）は、1文ずつの判定（judge_v3）にする。title はオープニングの
        動画タイトル案（あれば一緒に判定する）。
        """
        shown = [
            f
            for f in dict.fromkeys((prior_facts or []) + (chapter.get("facts") or []))
            if f in data.facts
        ]
        shown_refs = [
            r
            for r in dict.fromkeys((prior_refs or []) + (chapter.get("refs") or []))
            if r in data.refs
        ]
        if shown or shown_refs or chapter["role"] in SENTENCE_ROLES or title:
            return self._judge_sentences(chapter, data, shown, title, shown_refs)
        resolver = resolver_for(data)
        lines = []
        for seg in chapter["segments"]:
            if seg["kind"] == "stat":
                continue
            try:
                text = resolver.resolve(seg["text"])
            except KeyError:
                text = seg["text"]
            lines.append(f"（一節）「{text}」" if seg["kind"] == "quote" else text)
        user = fill(
            self.user_tpl,
            passages=self._passages(chapter, data),
            chapter_title=chapter["title"],
            narration="\n".join(lines),
        )
        for _ in range(3):
            try:
                d = parse_json_object(self.llm.complete(self.system, user, JUDGE_SCHEMA))
            except ValueError:
                continue
            if isinstance(d.get("faithful"), bool):
                problems = [str(p) for p in d.get("problems") or [] if str(p).strip()]
                if d["faithful"]:
                    return []
                return problems or ["忠実さに問題がある（理由なし）"]
        return ["忠実さを判定できなかった"]

    def _judge_sentences(
        self,
        chapter: dict[str, Any],
        data: EpisodeData,
        shown: list[str],
        title: str = "",
        shown_refs: list[str] | None = None,
    ) -> list[str]:
        resolver = resolver_for(data)
        shown_refs = shown_refs or []

        def ref_mark(m: re.Match) -> str:
            # 語釈・解釈と、その出典から差し込んだ値にも〔ID〕の印を付ける
            kind, rid = m.group(1), m.group(2)
            r = data.refs.get(rid)
            if r is None:
                return m.group(0)
            value = ref_source_label(r) if kind.endswith("_src") else ref_text(r)
            return f"{value}〔{rid}〕"

        def marked(text: str) -> str:
            # 事実から差し込んだ値に〔事実ID〕の印を付けて見せる（文の事実は「。」の前に付ける）
            def one(m: re.Match) -> str:
                fid, slot = m.group(1), m.group(2)
                f = data.facts.get(fid)
                if f is None:
                    return m.group(0)
                try:
                    value = f.slot_text(slot) if slot else f.text_ja
                except (KeyError, ValueError):
                    return m.group(0)
                if not slot and value.endswith("。"):
                    return f"{value[:-1]}〔{fid}〕。"
                return f"{value}〔{fid}〕"

            text = REF_REF.sub(ref_mark, FACT_REF.sub(one, text))
            try:
                return resolver.resolve(text)
            except KeyError:
                return text

        sentences: list[tuple[str, str, set[str]]] = []
        lines = []
        if title:
            sentences.append(("title", marked(title), set(MARKER.findall(marked(title)))))
            lines.append(f"S1（動画のタイトル案）：{marked(title)}")
        for seg in chapter["segments"]:
            if seg["kind"] == "stat":
                try:
                    lines.append(f"（統計・DBの値）{resolver.resolve(seg['text'])}")
                except KeyError:
                    pass
                continue
            text = marked(seg["text"])
            if seg["kind"] == "quote":
                lines.append(f"（一節）「{text}」")
                continue
            for sent in split_sentences(text):
                sentences.append((seg["seg_id"], sent, set(MARKER.findall(sent))))
                lines.append(f"S{len(sentences)}（{seg['seg_id']}）：{sent}")
        worries = "\n".join(f"- {w['text_general']}" for w in data.worries.values())
        facts = (
            "\n".join(
                f"- {fid}"
                + {"original_work": "（原著について）", "translation": "（この訳について）"}.get(
                    data.facts[fid].scope, ""
                )
                + f"：{data.facts[fid].text_ja}"
                for fid in shown
            )
            or "（なし）"
        )
        roles = {"original": "原文", "kundoku": "訓読", "translation": "訳", "commentary": "注釈"}
        refs = (
            "\n".join(
                f"- {rid}（"
                + (
                    f"辞書の語釈：見出し「{data.refs[rid]['headword']}」"
                    if rid.startswith("J-")
                    else f"{roles.get(data.refs[rid]['role'], '')}：一節 "
                    f"{data.refs[rid]['passage_key']}"
                )
                + f"、{ref_source_label(data.refs[rid])}）：{ref_text(data.refs[rid])}"
                for rid in shown_refs
            )
            or "（なし）"
        )
        user = fill(
            self.f_user_tpl,
            passages=self._passages(chapter, data) or "（なし）",
            worries=worries or "（なし）",
            facts=facts,
            refs=refs,
            chapter_title=chapter["title"],
            sentences="\n".join(lines),
        )
        for _ in range(3):
            try:
                d = parse_json_object(
                    self.llm.complete(self.f_system, user, JUDGE_SENTENCES_SCHEMA)
                )
            except ValueError:
                continue
            got = {
                x["n"]: x
                for x in d.get("sentences") or []
                if isinstance(x, dict)
                and isinstance(x.get("n"), int)
                and isinstance(x.get("ok"), bool)
                and x.get("type") in SENTENCE_TYPES
            }
            if set(got) != set(range(1, len(sentences) + 1)):
                continue  # 文の番号・種類・可否がそろわない判定は使わない
            return sentence_verdict(
                sentences, got, set(shown) | set(shown_refs), d.get("passage_problems") or []
            )
        return ["忠実さを判定できなかった（1文ずつの判定がそろわない）"]


def sentence_verdict(
    sentences: list[tuple[str, str, set[str]]],
    got: dict[int, dict[str, Any]],
    shown: set[str],
    passage_problems: list[Any],
) -> list[str]:
    """判定役の文ごとの答えから、合否をコードで決める（問題の一覧。空なら合格）。

    背景の事実にかかわる問題（背景の主張・事実の印のある文）には BG の頭を、語釈・解釈に
    かかわる問題（紹介の文・語釈や解釈の印のある文）には REF の頭を付ける。
    """
    problems = [str(p) for p in passage_problems if str(p).strip()]
    for n, (sid, sent, markers) in enumerate(sentences, start=1):
        x = got[n]
        sup = {str(f) for f in x.get("supported_by") or []}
        ref_marks = {m for m in markers if m.startswith(("J-", "I-"))}
        prefix = ""
        if x.get("type") == "reference" or ref_marks:
            prefix = REF
        elif x.get("type") == "background" or markers:
            prefix = BG
        head = f"{prefix}S{n}（{sid}）「{sent[:30]}」"
        if not x.get("ok"):
            problems.append(f"{head}：{x.get('problem') or '根拠が無い'}")
            continue
        if x.get("type") == "background" and not sup:
            problems.append(f"{head}：背景の主張なのに、根拠の事実が無い")
        if x.get("type") == "reference" and not sup:
            problems.append(f"{head}：辞書・昔の本の紹介なのに、根拠の語釈・解釈が無い")
        if markers and not markers <= sup:
            problems.append(f"{head}：差し込んだ事実 {sorted(markers - sup)} が根拠に無い")
        if sup - shown:
            problems.append(f"{head}：見せていない事実 {sorted(sup - shown)} を根拠にしている")
    return problems


def summarize(verification: dict[str, Any]) -> str:
    parts = []
    for g in ("G1", "G2", "G3", "G4", "G5", "G6"):
        v = verification.get(g)
        if v is None:
            continue
        mark = "合格" if v["ok"] else "不合格"
        if g == "G6" and v.get("review_required"):
            mark = "要確認"
        if g == "G3" and v.get("needs_human"):
            mark = "人の確認へ"
        parts.append(f"{g} {mark}")
    viewing = verification.get("viewing")
    if viewing and viewing["issues"]:
        parts.append(f"視聴の注意{len(viewing['issues'])}件")
    return "、".join(parts)


def dumps(verification: dict[str, Any]) -> str:
    return json.dumps(verification, ensure_ascii=False)
