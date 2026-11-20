"""M6 の部品のあいだの受け渡しの形（docs/m6_design.md）。

部品は次の順につながる。どれもこのファイルの形だけを受け取り、ほかの部品の中身を知らない。

1. 読み上げの準備（統合の側。koten.media.prepare）：台本と DB から Line の並びを作る
2. 音声（koten.voice）：Line → 章ごとの wav と VoiceManifest（段落・文ごとの時刻）
3. 画面（koten.visuals）：VoiceManifest と CardSpec → カードの画像と ShotList
4. 組み立て（koten.render）：ShotList と章の wav → 章ごとの mp4 → 完成した mp4。G7（尺）
5. 公開用キット（koten.publish_kit）：KitInput と VoiceManifest → 説明欄・チャプター・字幕
6. 確認画面（koten.review）：ReviewBundle → exports/<回>/review.html。承認・差し戻し

置き場所（HDD。検索と API は読まない）：

    <hdd_root>/media/<episode_id>/
        voice/<chapter_id>.wav     章ごとの音声
        voice/manifest.json        VoiceManifest
        cards/<名前>.png           カードの画像（1920×1080）
        photos/<名前>.jpg          この回に使う写真（1920×1080 に切り抜いたもの）
        visuals/shots.json         ShotList
        render/<chapter_id>.mp4    章ごとの動画
        render/<episode_id>.mp4    完成した動画
        kit/description.txt        説明欄
        kit/subtitles.srt          字幕
        kit/thumb_<n>.png          サムネイル（1280×720）
    <hdd_root>/exports/<episode_id>/review.html   確認画面

時刻はどれも秒（float）。段落と文の時刻は「章の頭から」、章の offset は「動画の頭から」。
パスは、ことわりが無ければ media_dir（<hdd_root>/media/<episode_id>）からの相対パス。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MANIFEST = "voice/manifest.json"
SHOTS = "visuals/shots.json"


def media_dir(hdd_root: Path, episode_id: str) -> Path:
    return hdd_root / "media" / episode_id


def exports_dir(hdd_root: Path, episode_id: str) -> Path:
    return hdd_root / "exports" / episode_id


# --- 1. 読み上げる段落 ---------------------------------------------------------


@dataclass
class Line:
    """読み上げる1段落。display は画面・字幕の文、reading は VOICEVOX に渡す文。"""

    chapter_id: str
    seg_id: str
    kind: str  # narration・quote・stat・transition・human_take
    display: str  # 差し込み済みの文（原文のまま。旧仮名もそのまま）
    reading: str  # 読みを直した文（旧仮名を今の仮名に、くの字点を開く など）
    visual: dict[str, Any] = field(default_factory=dict)  # 台本の visual（type・ref）
    chapter_title: str = ""
    role: str = ""  # 章の役（opening・classic・summary …）
    audio_path: str | None = None  # 人の一言を自分の声で録った wav（あれば合成しない）


# 文の終わり。「」の中の句点では切らない（引用の途中で字幕が割れないように）
_SENT_END = "。！？!?"


def split_sentences(text: str) -> list[str]:
    """文に分ける。句点・感嘆符・疑問符の後ろで切り、かぎかっこの中では切らない。

    空の文は返さない。区切りの記号は前の文に残す。改行も区切りにする。
    """
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch in "「『（(":
            depth += 1
        elif ch in "」』）)" and depth > 0:
            depth -= 1
        if ch == "\n":
            if "".join(buf).strip():
                out.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
        if ch in _SENT_END and depth == 0:
            out.append("".join(buf).strip())
            buf = []
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return [s for s in out if s]


def pair_sentences(display: str, reading: str) -> list[tuple[str, str]]:
    """画面の文と読みの文を、文ごとに組にする。

    数がそろわなければ（読みの直しで句点が増減したなど）、段落全体を1つの組にする。
    """
    d, r = split_sentences(display), split_sentences(reading)
    if d and len(d) == len(r):
        return list(zip(d, r, strict=True))
    whole_d = re.sub(r"\s*\n\s*", "", display).strip()
    whole_r = re.sub(r"\s*\n\s*", "", reading).strip()
    return [(whole_d, whole_r)] if whole_r else []


# --- 2. 音声の時刻 -------------------------------------------------------------


@dataclass
class Sentence:
    display: str
    reading: str
    start: float  # 章の頭から
    end: float


@dataclass
class SegmentTiming:
    seg_id: str
    kind: str
    start: float  # 章の頭から（この段落の最初の音）
    end: float  # この段落の最後の音（あとの間は含まない）
    display: str
    reading: str
    visual: dict[str, Any]
    sentences: list[Sentence] = field(default_factory=list)


@dataclass
class ChapterTiming:
    chapter_id: str
    role: str
    title: str
    wav: str  # media_dir からの相対パス（voice/<chapter_id>.wav）
    offset: float  # 動画の頭から（前の章の duration の合計）
    duration: float  # wav の長さ（章の終わりの間を含む）
    segments: list[SegmentTiming] = field(default_factory=list)


@dataclass
class VoiceManifest:
    episode_id: str
    speaker_id: int
    speed_scale: float
    engine_version: str
    sample_rate: int
    chapters: list[ChapterTiming] = field(default_factory=list)

    @property
    def total_sec(self) -> float:
        return sum(c.duration for c in self.chapters)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VoiceManifest:
        chapters = []
        for c in d.get("chapters", []):
            segs = []
            for s in c.get("segments", []):
                sents = [Sentence(**x) for x in s.get("sentences", [])]
                segs.append(SegmentTiming(**{**s, "sentences": sents}))
            chapters.append(ChapterTiming(**{**c, "segments": segs}))
        return cls(**{**d, "chapters": chapters})

    def save(self, path: Path) -> None:
        _write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: Path) -> VoiceManifest:
        return cls.from_dict(json.loads(path.read_text("utf-8")))


# --- 3. 画面 ------------------------------------------------------------------


@dataclass
class CardSpec:
    """段落ごとの画面の中身。統合の側が台本と DB から作る（本文はどれも差し込み済み）。

    type が photo の段落には CardSpec を作らない（写真を当てる）。
    """

    seg_id: str
    type: str  # quote_card・title_card・chart
    title: str = ""  # title_card：章のタイトル
    text: str = ""  # quote_card：一節の原文（旧仮名・くの字点もそのまま。縦書き）
    source: str = ""  # quote_card：出典の1行（著者『作品』（訳者訳）「章」。R7）
    label: str = ""  # quote_card：訳の表示（「試訳」「ロングの英訳（1862）」）。無ければ空
    license_note: str = ""  # quote_card：ライセンスの表示（CC BY なら「CC BY 4.0・改変なし」など）
    stat: dict[str, Any] | None = None  # chart：label・value・unit・survey・year


@dataclass
class Shot:
    chapter_id: str
    start: float  # 章の頭から
    duration: float
    image: str  # media_dir からの相対パス
    motion: str  # zoom（写真：ゆっくり寄る）か still
    kind: str  # photo・quote_card・title_card・chart
    seg_ids: list[str] = field(default_factory=list)


@dataclass
class ShotList:
    episode_id: str
    width: int
    height: int
    fps: int
    shots: list[Shot] = field(default_factory=list)
    photos_used: list[str] = field(default_factory=list)  # photos_dir からの相対パス（元の写真）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ShotList:
        return cls(**{**d, "shots": [Shot(**s) for s in d.get("shots", [])]})

    def save(self, path: Path) -> None:
        _write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: Path) -> ShotList:
        return cls.from_dict(json.loads(path.read_text("utf-8")))


@dataclass
class ThumbSpec:
    words: str  # 悩みの言葉を短く（タイトルと同じ約束）
    authors: list[str]  # 著者名（主な名前だけ）
    photo: str | None = None  # 背景の写真（絶対パス）。無ければ色の背景
    template: str = "a"  # a・b・c（回ごとに順に使う）


# --- 5. 公開用キット ------------------------------------------------------------


@dataclass
class ClassicSource:
    """説明欄の「この動画で読んだ古典」の1件（R7）。"""

    title: str
    authors: str
    translator: str = ""
    section: str = ""
    base_book: str = ""  # 底本
    url: str = ""  # 青空文庫の図書カードなど
    license: str = ""  # PD_JP・CC0・CC_BY …
    label: str = ""  # 訳の表示（「試訳」「ロングの英訳（1862）」）


@dataclass
class KitInput:
    """説明欄の材料。どの文も検査を通ったものか DB・設定から来たもの（AI に新しく書かせない）。"""

    episode_id: str
    title: str
    hook: str  # ①つかみ：冒頭のナレーションの最初の1〜2文
    promise: str  # ②約束の1文（プログラムが決める）
    classics: list[ClassicSource] = field(default_factory=list)  # ④
    data_sources: list[str] = field(
        default_factory=list
    )  # ⑤調査・統計・研究の出典の行（数値は書かない）
    background_sources: list[str] = field(default_factory=list)  # ⑥背景の事実・語釈・解釈の出典の行
    books: list[dict[str, str]] = field(
        default_factory=list
    )  # ⑦参考にした本：title・url（空なら節ごと出さない）
    amazon_notice: str = ""  # ⑦の上に出す Amazon アソシエイトの決まった文言
    credits: list[str] = field(default_factory=list)  # ⑧VOICEVOX のクレジットなど
    notices: list[str] = field(default_factory=list)  # ⑨決まった注意書き
    question: str = ""  # ⑩コメントへの問いかけ（G1 を通った文）
    hashtags: list[str] = field(default_factory=list)  # ⑪人が書いた対応表から（3つまで）


# --- 6. 確認画面 ---------------------------------------------------------------


@dataclass
class ReviewBundle:
    episode_id: str
    title: str
    state: str
    script_lines: list[str]  # 差し込んだ台本（koten.script.run.render の行）
    quotes: list[
        dict[str, str]
    ]  # 一節：seg_id・display（原文）・reading（読み上げ）・source・license
    sources: list[dict[str, str]]  # 出典とライセンス：kind・text・license
    gates: dict[str, Any]  # verification_json（G1〜G7）
    video: str | None  # 完成した動画の絶対パス
    thumbnails: list[str]  # 絶対パス
    description: str
    chapters: list[str]
    ad_breaks: list[float]
    warnings: list[str] = field(default_factory=list)  # 承認は止めない注意
    blockers: list[str] = field(default_factory=list)  # 承認できない理由（照合の保留・G7 など）
    human_take: dict[str, Any] = field(default_factory=dict)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(path)
