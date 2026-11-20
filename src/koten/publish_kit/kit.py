"""公開用キット：説明欄・チャプター・字幕（SRT）・広告位置の候補。

docs/m6_design.md の koten.publish_kit・SPEC §8.11・D-007・D-027・D-036。

- 説明欄の文は、どれも KitInput（検査を通った台本の文と、DB・設定の値）と VoiceManifest（章の題と
  時刻）から入れる。ここで新しい文は作らない。プログラムが持つのは、見出しと「（広告）」と
  ライセンスの決まった表示だけ
- 調査の数値は説明欄に書かない（動画の中だけで出す。D-036）
- 時刻は VoiceManifest の実測から。章の offset は動画の頭から、段落と文の時刻は章の頭から
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from koten.license import is_usable
from koten.media.model import ClassicSource, KitInput, VoiceManifest
from koten.script.gates import NUMBER_CLAIM

# YouTube の説明欄の上限（字）。D-036 で確かめる
DESCRIPTION_MAX = 5000
# YouTube のチャプターの条件（D-007）：00:00 から・昇順・どれも10秒以上・3つ以上
CHAPTER_MIN_SEC = 10
CHAPTER_MIN_COUNT = 3
HASHTAG_MAX = 3

# 説明欄に出すライセンスの表示（R7。CC BY は改変の有無も書く。画面のカードと同じ言い方）
LICENSE_TEXT = {
    "PD_JP": "著作権の保護期間満了",
    "CC0": "CC0",
    "CC_BY": "CC BY（改変なし）",
}
# CC BY の作品を訳して読んだ（訳の表示がある）ときは、改変ありと書く（R7）
LICENSE_TEXT_TRANSLATED = {"CC_BY": "CC BY（訳して読み上げ。改変あり）"}

# 書き出すファイル（media_dir からの相対パス）
KIT_FILES = {
    "description": "kit/description.txt",
    "subtitles": "kit/subtitles.srt",
    "chapters": "kit/chapters.txt",
}

# 説明欄に書かない調査の数値（D-036）。台本の検査（G1）と同じ割合・人数の形に、件数を足す
SURVEY_NUMBER = re.compile(rf"{NUMBER_CLAIM.pattern}|[%％]|[0-9０-９][0-9０-９,，]{{2,}}\s*件")
_URL = re.compile(r"https?://\S+")
# 差し込んでいない記号（{{author:…}} など）
_PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")

# 字幕の行の頭に置かない字（句読点・閉じかっこ・小さい仮名など）
_NO_START = "、。，．,.！？!?」』）)】〕…ー々ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮ"
# 字幕の行の終わりに置かない字（開きかっこ）
_NO_END = "「『（(【〔"
# 字幕の行を切るなら、この字のあと
_BREAK_AFTER = "、。！？!?」』）) 　"


class DescriptionError(RuntimeError):
    """説明欄を作れないとき（長すぎる・古典の出典が無い・YouTube で使えない字がある など）。"""


# --- チャプター -----------------------------------------------------------------


def chapter_lines(manifest: VoiceManifest) -> list[str]:
    """YouTube のチャプター（「00:00 タイトル」の行）。

    - 00:00 から、昇順。時刻は秒を切り捨てる（章の音が始まる少し前を指す）
    - 10秒に満たない章は前の章にまとめる（最初の章が短ければ、次の章をそこにまとめる）。
      長さは、切り捨てた時刻どうしで測る（YouTube が見るのは書いた時刻なので）
    - 3つに満たなければ空（チャプターにならない）
    - 1時間を超えた時刻は h:mm:ss
    """
    chapters = manifest.chapters
    if not chapters:
        return []
    starts = [0] + [int(c.offset) for c in chapters[1:]]
    end = int(manifest.total_sec)
    kept: list[tuple[int, str]] = []
    for i, ch in enumerate(chapters):
        start = starts[i]
        nxt = starts[i + 1] if i + 1 < len(chapters) else end
        title = _one_line(ch.title)
        if not kept:
            kept.append((0, title))
        elif nxt - start < CHAPTER_MIN_SEC:
            continue  # この章が短い：前の章にまとめる
        elif start - kept[-1][0] < CHAPTER_MIN_SEC:
            continue  # 前の章（最初の章）が短い：この章をそこにまとめる
        else:
            kept.append((start, title))
    if len(kept) < CHAPTER_MIN_COUNT:
        return []
    return [f"{_clock(t)} {title}" for t, title in kept]


def _clock(sec: int) -> str:
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# --- 説明欄 ---------------------------------------------------------------------


def build_description(kit: KitInput, manifest: VoiceManifest) -> str:
    """説明欄（SPEC §8.11 の①〜⑪の順）。空の節は見出しごと出さない。

    ①つかみ ②約束（この2つで最初の2行） ③▼目次 ④▼この動画で読んだ古典
    ⑤▼この動画で使った調査・データ ⑥▼背景と昔の資料の出典
    ⑦（広告）・Amazon アソシエイトの文言・▼参考にした本 ⑧クレジット ⑨注意書き
    ⑩問いかけ ⑪ハッシュタグ（3つまで）
    """
    if kit.episode_id != manifest.episode_id:
        raise DescriptionError(
            f"説明欄の材料（{kit.episode_id}）と音声の記録（{manifest.episode_id}）の回が違います"
        )
    if not kit.classics:
        raise DescriptionError(
            "この動画で読んだ古典の出典がありません（R7）。台本の一節と候補を確かめてください"
        )
    for c in kit.classics:
        if not _usable(c.license):
            raise DescriptionError(
                f"『{_one_line(c.title)}』のライセンス「{c.license or '（空）'}」は、使えるもの"
                "（PD_JP・CC0・CC_BY）ではありません（R1・R7）。G2 の結果を確かめてください"
            )
    toc = chapter_lines(manifest)
    blocks = [
        _lines([kit.hook, kit.promise]),
        _section("▼目次", toc),
        _section("▼この動画で読んだ古典", _lines([_classic_line(c) for c in kit.classics])),
        _section("▼この動画で使った調査・データ", _bullets(kit.data_sources)),
        _section("▼背景と昔の資料の出典", _bullets(kit.background_sources)),
        _books(kit),
        _lines(kit.credits),
        _lines(kit.notices),
        _lines([kit.question]),
        _lines([" ".join(_hashtags(kit.hashtags))]),
    ]
    text = "\n\n".join("\n".join(b) for b in blocks if b)
    _check(text, kit, toc)
    return text


def _one_line(text: str) -> str:
    """1行にする（説明欄の行の中で改行させない）。"""
    return re.sub(r"\s*[\r\n]+\s*", " ", text or "").strip()


def _lines(items: list[str]) -> list[str]:
    """空の行と、同じ行の繰り返しを除く。"""
    out: list[str] = []
    for item in items:
        line = _one_line(item)
        if line and line not in out:
            out.append(line)
    return out


def _bullets(items: list[str]) -> list[str]:
    return [x if x.startswith("・") else f"・{x}" for x in _lines(items)]


def _section(heading: str, lines: list[str]) -> list[str]:
    return [heading, *lines] if lines else []


def _classic_line(c: ClassicSource) -> str:
    """読んだ古典の1行：著者『作品』（訳者訳）「章」（訳の表示）　底本：…　URL　ライセンス。"""
    translator, section, label = _one_line(c.translator), _one_line(c.section), _one_line(c.label)
    head = f"{_one_line(c.authors)}『{_one_line(c.title)}』"
    if translator:
        head += f"（{translator}訳）"
    if section:
        head += f"「{section}」"
    if label:
        head += label if label.startswith("（") else f"（{label}）"
    base = _one_line(c.base_book)
    parts = [head, f"底本：{base}" if base else "", _one_line(c.url), _license_text(c)]
    return "・" + "　".join(p for p in parts if p)


def _usable(code: str) -> bool:
    try:
        return is_usable(_one_line(code))
    except ValueError:  # 知らないライセンスの名前
        return False


def _license_text(c: ClassicSource) -> str:
    code = _one_line(c.license)
    if _one_line(c.label) and code in LICENSE_TEXT_TRANSLATED:
        return LICENSE_TEXT_TRANSLATED[code]
    return LICENSE_TEXT.get(code, code)  # 表の無い表示は、そのまま出す（隠さない）


def _books(kit: KitInput) -> list[str]:
    """⑦参考にした本。一覧の上に「（広告）」と Amazon アソシエイトの決まった文言（D-027）。"""
    items: list[str] = []
    for book in kit.books:
        parts = [_one_line(book.get("title") or ""), _one_line(book.get("url") or "")]
        line = "　".join(p for p in parts if p)
        if line and f"・{line}" not in items:
            items.append(f"・{line}")
    if not items:
        return []
    notice = _one_line(kit.amazon_notice)
    if not notice and any(_is_amazon(book.get("url") or "") for book in kit.books):
        raise DescriptionError(
            "参考にした本に Amazon のリンクがあるのに、Amazon アソシエイトの決まった文言が空です"
            "（settings の kit.channel_name を書いてください。D-027）"
        )
    return ["（広告）", *([notice] if notice else []), "▼参考にした本", *items]


def _is_amazon(url: str) -> bool:
    return bool(re.search(r"(^|[/.])(amazon\.|amzn\.)", url))


def _hashtags(tags: list[str]) -> list[str]:
    """# を付け、中の空白を詰め、同じものを除いて3つまで。"""
    out: list[str] = []
    for tag in tags:
        word = re.sub(r"\s+", "", tag or "").lstrip("#＃")
        if word and f"#{word}" not in out:
            out.append(f"#{word}")
    return out[:HASHTAG_MAX]


def _check(text: str, kit: KitInput, toc: list[str]) -> None:
    # 調査の数値は説明欄に書かない（D-036）。つかみや章の題に統計の記号が差し込まれていても
    # 止める。URL の中は数えない。古典と本の書誌（『9割の…』のような題）は見ない
    parts = {
        "つかみ": [kit.hook],
        "約束": [kit.promise],
        "目次": [line.split(" ", 1)[-1] for line in toc],
        "調査・データ": kit.data_sources,
        "背景と昔の資料の出典": kit.background_sources,
        "問いかけ": [kit.question],
    }
    for where, lines in parts.items():
        for line in lines:
            if m := SURVEY_NUMBER.search(_URL.sub("", line or "")):
                raise DescriptionError(
                    f"説明欄の{where}の行に調査の数値「{m.group(0)}」があります：{_one_line(line)}。"
                    "説明欄には調査の数値を書かず、動画の中だけで出します（D-036）"
                )
    # 差し込み忘れの記号（章の題の {{author:…}} など）をそのまま出さない
    if m := _PLACEHOLDER.search(text):
        raise DescriptionError(
            f"説明欄に差し込んでいない記号 {m.group(0)} があります。"
            "章の題や台本の文を、DB の値で差し込んでから作ってください"
        )
    # YouTube は説明欄に < と > を入れられない
    bad = sorted({ch for ch in text if ch in "<>"})
    if bad:
        raise DescriptionError(
            f"説明欄に YouTube で使えない字（{' '.join(bad)}）があります。元の行を直してください"
        )
    if len(text) > DESCRIPTION_MAX:
        raise DescriptionError(
            f"説明欄が {len(text)} 字で、上限の {DESCRIPTION_MAX} 字を超えています。"
            "出典の行は削らずに、参考にした本を減らしてください"
        )


# --- 字幕 -----------------------------------------------------------------------


def build_srt(manifest: VoiceManifest, *, max_chars: int = 28) -> str:
    """文ごとの字幕（SRT）。時刻は章の offset に文の時刻を足したもの（動画の頭から）。

    長い文は max_chars 字までの行に分けて（句読点・閉じかっこのあとを優先。行の長さはそろえる）
    1つの字幕に2行まで。それより長ければ、文の時間を字数で割って次の字幕に続ける。
    """
    if max_chars < 1:
        raise ValueError("max_chars は1以上にしてください")
    cues: list[tuple[float, float, list[str]]] = []
    for ch in manifest.chapters:
        for seg in ch.segments:
            for sent in seg.sentences:
                text = _one_line(sent.display)
                if not text or sent.end <= sent.start:
                    continue
                cues += _cues(text, ch.offset + sent.start, ch.offset + sent.end, max_chars)
    blocks = [
        f"{i}\n{_srt_time(start)} --> {_srt_time(end)}\n" + "\n".join(lines)
        for i, (start, end, lines) in enumerate(cues, 1)
    ]
    return "\n\n".join(blocks) + "\n" if blocks else ""


def _cues(
    text: str, start: float, end: float, max_chars: int
) -> list[tuple[float, float, list[str]]]:
    """1文を、2行までの字幕に分ける。時間は字数で割る（最後の字幕は文の終わりにそろえる）。"""
    lines = _wrap(text, max_chars)
    groups = [lines[i : i + 2] for i in range(0, len(lines), 2)]
    total = sum(len(x) for x in lines)
    out: list[tuple[float, float, list[str]]] = []
    done = 0
    t0 = start
    for i, group in enumerate(groups):
        done += sum(len(x) for x in group)
        t1 = end if i == len(groups) - 1 else start + (end - start) * done / total
        out.append((t0, t1, group))
        t0 = t1
    return out


def _wrap(text: str, width: int) -> list[str]:
    """width 字までの行に分ける。行の数はいちばん少なくし、長さはなるべくそろえる。"""
    lines: list[str] = []
    rest = text
    while len(rest) > width:
        n = math.ceil(len(rest) / width)  # 残りに要る行の数
        lo = len(rest) - (n - 1) * width  # これより前で切ると、残りが n-1 行に入らない
        cut = _cut_at(rest, lo, width, target=math.ceil(len(rest) / n))
        lines.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    lines.append(rest)
    return [x for x in lines if x]


def _cut_at(text: str, lo: int, hi: int, target: int) -> int:
    """行を切る位置（lo〜hi 字のところ。target に近いほどよい）。

    句読点・閉じかっこのあとを先に探し、無ければ、ひらがなから漢字・カタカナなどへ変わる所
    （ことばの切れ目のことが多い）。どちらも無ければ target で切る。そのとき行の頭が句読点や
    小さい仮名に、行の終わりが開きかっこになるなら、lo までの間で前へずらす（ずらせなければ
    target のまま）。
    """

    def ok(i: int) -> bool:
        return text[i] not in _NO_START and text[i - 1] not in _NO_END

    punct = [i for i in range(lo, hi + 1) if ok(i) and text[i - 1] in _BREAK_AFTER]
    kana = [i for i in range(lo, hi + 1) if ok(i) and _hira(text[i - 1]) and not _hira(text[i])]
    for cands in (punct, kana):
        if cands:
            return min(cands, key=lambda i: abs(i - target))
    for cut in range(target, lo - 1, -1):
        if ok(cut):
            return cut
    return target


def _hira(ch: str) -> bool:
    return "ぁ" <= ch <= "ゖ"


def _srt_time(sec: float) -> str:
    ms = max(0, round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# --- 広告位置の候補 ---------------------------------------------------------------


def ad_break_seconds(manifest: VoiceManifest) -> list[float]:
    """広告位置の候補：章の切れ目の時刻（動画の頭から・秒）。

    最初の切れ目（冒頭の章のすぐあと）は除く。企画の ad_break_after_chapters と同じく、
    つかみの直後に広告を入れない。設定は人が YouTube Studio で行う（SPEC §8.11）。
    """
    return [c.offset for c in manifest.chapters[2:]]


# --- 書き出し -------------------------------------------------------------------


def write_kit(kit: KitInput, manifest: VoiceManifest, media: Path) -> dict[str, str]:
    """kit/description.txt・kit/subtitles.srt・kit/chapters.txt を書く。

    返すのは media_dir からの相対パス。
    説明欄を作れなければ（DescriptionError）、どのファイルも書かない。
    """
    lines = chapter_lines(manifest)
    texts = {
        "description": build_description(kit, manifest),
        "subtitles": build_srt(manifest),
        "chapters": "\n".join(lines) + "\n" if lines else "",
    }
    for key, text in texts.items():
        _write_text(Path(media) / KIT_FILES[key], text)
    return dict(KIT_FILES)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(text, "utf-8")
    tmp.replace(path)
