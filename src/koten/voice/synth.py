"""1回分の読み上げを VOICEVOX で合成する（docs/m6_design.md・SPEC §8.9）。

段落（Line）を文に分け、文ごとに audio_query → synthesis で wav を作り、無音を挟んで章ごとの wav に
つなぐ。文と段落の時刻は、つないだ音声の実際のフレーム数から記録する（目安の長さは使わない）。

    voice/<chapter_id>.wav   章ごとの音声（16bit PCM・モノラル）
    voice/manifest.json      VoiceManifest
    voice/cache/<sha1>.wav   文ごとの音声（同じ読みを合成し直さないため）

読みの直し（旧仮名・くの字点など）はここではしない。Line.reading は準備済みのものを受け取る。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import wave
from collections.abc import Callable
from pathlib import Path

from koten.media.model import (
    MANIFEST,
    ChapterTiming,
    Line,
    SegmentTiming,
    Sentence,
    VoiceManifest,
    pair_sentences,
)
from koten.voice.voicevox import VoiceError, VoicevoxClient

SAMPLE_WIDTH = 2  # 16bit
_CHAPTER_ID = re.compile(r"[\w-]+")


def synthesize(
    lines: list[Line],
    out_dir: Path,
    client: VoicevoxClient,
    *,
    episode_id: str,
    speed_scale: float = 1.0,
    sample_rate: int = 24000,
    pause_sentence: float = 0.25,
    pause_segment: float = 0.6,
    pause_chapter: float = 1.2,
    log: Callable[[str], object] = print,
) -> VoiceManifest:
    """Line の並びから章ごとの wav と voice/manifest.json を書き、VoiceManifest を返す。

    out_dir は media_dir（<hdd_root>/media/<episode_id>）。章は Line に最初に出てきた順。
    文のあいだに pause_sentence、段落のあいだに pause_segment、章の終わりに pause_chapter の無音。
    """
    if not lines:
        raise ValueError("読み上げる段落がありません")
    by_chapter: dict[str, list[Line]] = {}
    for line in lines:
        by_chapter.setdefault(line.chapter_id, []).append(line)
    # 合成を始める前に確かめる（途中の章で止まって、合成の時間をむだにしないように）
    for chapter_id in by_chapter:
        if not _CHAPTER_ID.fullmatch(chapter_id):
            raise ValueError(f"章の ID（{chapter_id!r}）はファイル名に使える字だけにしてください")

    voice = out_dir / "voice"
    engine_version = client.version()
    cache = _Cache(voice / "cache", client, speed_scale, sample_rate, engine_version)

    chapters: list[ChapterTiming] = []
    offset_frames = 0
    for chapter_id, group in by_chapter.items():
        track = _Track(sample_rate)
        segments: list[SegmentTiming] = []
        for line in group:
            pairs = [] if line.audio_path else pair_sentences(line.display, line.reading)
            if not line.audio_path and not pairs:
                log(f"  {line.seg_id}：読みが空なので飛ばしました")
                continue
            if segments:
                track.pause(pause_segment)
            if line.audio_path:
                segments.append(_human_take(line, out_dir, track))
            else:
                segments.append(_speak(line, pairs, track, cache, pause_sentence))
        if not segments:
            log(f"{chapter_id}：読み上げる段落が無いので、章ごと飛ばしました")
            continue
        track.pause(pause_chapter)
        wav_rel = f"voice/{chapter_id}.wav"
        _write_wav(out_dir / wav_rel, bytes(track.pcm), sample_rate)
        chapter = ChapterTiming(
            chapter_id=chapter_id,
            role=group[0].role,
            title=group[0].chapter_title,
            wav=wav_rel,
            offset=offset_frames / sample_rate,
            duration=track.now,
            segments=segments,
        )
        chapters.append(chapter)
        offset_frames += track.frames
        log(f"{chapter_id}「{chapter.title}」：{len(segments)}段落・{chapter.duration:.1f}秒")

    manifest = VoiceManifest(
        episode_id=episode_id,
        speaker_id=client.speaker_id,
        speed_scale=speed_scale,
        engine_version=engine_version,
        sample_rate=sample_rate,
        chapters=chapters,
    )
    manifest.save(out_dir / MANIFEST)
    log(
        f"音声：{len(chapters)}章・{manifest.total_sec:.1f}秒"
        f"（合成 {cache.made} 文・作り置き {cache.reused} 文）→ {out_dir / MANIFEST}"
    )
    return manifest


# --- 段落 ---------------------------------------------------------------------


def _speak(
    line: Line,
    pairs: list[tuple[str, str]],
    track: _Track,
    cache: _Cache,
    pause_sentence: float,
) -> SegmentTiming:
    sentences: list[Sentence] = []
    for i, (display, reading) in enumerate(pairs):
        if i:
            track.pause(pause_sentence)
        start = track.now
        track.add(cache.sentence(reading))
        sentences.append(Sentence(display, reading, start, track.now))
    return SegmentTiming(
        seg_id=line.seg_id,
        kind=line.kind,
        start=sentences[0].start,
        end=sentences[-1].end,
        display=line.display,
        reading=line.reading,
        visual=dict(line.visual),
        sentences=sentences,
    )


def _human_take(line: Line, out_dir: Path, track: _Track) -> SegmentTiming:
    """人が自分の声で録った wav を、合成せずにそのまま入れる。"""
    path = out_dir / str(line.audio_path)
    if not path.is_file():
        raise VoiceError(
            f"人の一言の音声 {path} がありません。"
            "録った wav を置き直すか、人の一言の設定を見直してください"
        )
    try:
        frames, rate, channels, width = _read_pcm(path)
    except (wave.Error, EOFError) as e:
        raise VoiceError(_convert_hint(path, track.rate, "読めない形です")) from e
    if (rate, channels, width) != (track.rate, 1, SAMPLE_WIDTH):
        found = f"{rate}Hz・{channels}ch・{width * 8}bit です"
        raise VoiceError(_convert_hint(path, track.rate, found))
    start = track.now
    track.add(frames)
    display = re.sub(r"\s*\n\s*", "", line.display).strip()
    reading = re.sub(r"\s*\n\s*", "", line.reading).strip() or display
    sentences = [Sentence(display, reading, start, track.now)] if display else []
    return SegmentTiming(
        seg_id=line.seg_id,
        kind=line.kind,
        start=start,
        end=track.now,
        display=line.display,
        reading=line.reading,
        visual=dict(line.visual),
        sentences=sentences,
    )


def _convert_hint(path: Path, rate: int, found: str) -> str:
    return (
        f"人の一言の音声 {path.name} は{found}。"
        f"ffmpeg -i {path.name} -ac 1 -ar {rate} -sample_fmt s16 変換後.wav で"
        f"モノラル・{rate}Hz・16bit にしてから使ってください"
    )


# --- 文ごとの音声と作り置き ---------------------------------------------------------


class _Cache:
    """文ごとの wav を voice/cache/ に残し、同じ条件の読みは合成し直さない。

    鍵は speaker・speed・サンプリング周波数・エンジンの版・ユーザー辞書の中身・読み。
    辞書に読みを足したあとは、同じ読みでも発音が変わりうるので作り直す。
    """

    def __init__(
        self,
        cache_dir: Path,
        client: VoicevoxClient,
        speed_scale: float,
        sample_rate: int,
        engine_version: str,
    ):
        self.dir = cache_dir
        self.client = client
        self.speed_scale = speed_scale
        self.sample_rate = sample_rate
        words = sorted(
            (w.get("surface", ""), w.get("pronunciation", ""), w.get("accent_type", 0))
            for w in client.user_dict().values()
        )
        self.base = "\n".join(
            [
                str(client.speaker_id),
                repr(float(speed_scale)),
                str(sample_rate),
                engine_version,
                json.dumps(words, ensure_ascii=False),
            ]
        )
        self.made = 0
        self.reused = 0

    def path(self, reading: str) -> Path:
        key = hashlib.sha1(f"{self.base}\n{reading}".encode()).hexdigest()
        return self.dir / f"{key}.wav"

    def sentence(self, reading: str) -> bytes:
        """1文の音声（16bit・モノラルの PCM のフレーム）。"""
        path = self.path(reading)
        if path.is_file():
            try:
                frames, rate, channels, width = _read_pcm(path)
                if (rate, channels, width) == (self.sample_rate, 1, SAMPLE_WIDTH):
                    self.reused += 1
                    return frames
            except (wave.Error, EOFError):
                pass  # 壊れた作り置きは作り直す
        query = self.client.audio_query(reading)
        query.update(
            speedScale=self.speed_scale, outputSamplingRate=self.sample_rate, outputStereo=False
        )
        data = self.client.synthesis(query)
        try:
            frames, rate, channels, width = _read_pcm(data)
        except (wave.Error, EOFError) as e:
            raise VoiceError(
                f"VOICEVOX から読めない音声が返りました（「{reading[:20]}」）。"
                "VOICEVOX を起動し直してから、もう一度動かしてください"
            ) from e
        if (rate, channels, width) != (self.sample_rate, 1, SAMPLE_WIDTH):
            raise VoiceError(
                f"VOICEVOX の音声が {rate}Hz・{channels}ch・{width * 8}bit でした"
                f"（{self.sample_rate}Hz・モノラル・16bit を頼みました）。"
                "VOICEVOX を新しい版にしてから、もう一度動かしてください"
            )
        _write_wav(path, frames, self.sample_rate)
        self.made += 1
        return frames


# --- wav ----------------------------------------------------------------------


class _Track:
    """章の音声をためる（16bit・モノラル）。時刻はたまったフレーム数から出す。"""

    def __init__(self, rate: int):
        self.rate = rate
        self.pcm = bytearray()

    @property
    def frames(self) -> int:
        return len(self.pcm) // SAMPLE_WIDTH

    @property
    def now(self) -> float:
        return self.frames / self.rate

    def add(self, frames: bytes) -> None:
        self.pcm += frames

    def pause(self, sec: float) -> None:
        self.pcm += bytes(SAMPLE_WIDTH * max(0, round(sec * self.rate)))


def _read_pcm(src: bytes | Path) -> tuple[bytes, int, int, int]:
    """wav のフレーム・サンプリング周波数・チャンネル数・1サンプルのバイト数。"""
    f = io.BytesIO(src) if isinstance(src, bytes) else str(src)
    with wave.open(f, "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels(), w.getsampwidth()


def _write_wav(path: Path, frames: bytes, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(rate)
        w.writeframes(frames)
    tmp.replace(path)
