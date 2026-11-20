"""M6 の組み立て（koten.render.ffmpeg）。小さな画像と短い無音の wav で動画を作る。

ffmpeg・ffprobe が無ければ、それを使うテストは skip する。
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from koten.media.model import ChapterTiming, Shot, ShotList, VoiceManifest
from koten.render.ffmpeg import (
    RenderError,
    _plan,
    frame_counts,
    g7_issues,
    probe_duration,
    render_episode,
)

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg がありません")

EP = "E20261120-01"
W, H, FPS = 160, 90, 30
RATE = 24000


def _chapter(cid: str, duration: float, offset: float = 0.0, title: str = "") -> ChapterTiming:
    return ChapterTiming(
        chapter_id=cid,
        role="opening",
        title=title or cid,
        wav=f"voice/{cid}.wav",
        offset=offset,
        duration=duration,
    )


def _manifest(*chapters: ChapterTiming) -> VoiceManifest:
    return VoiceManifest(
        episode_id=EP,
        speaker_id=13,
        speed_scale=1.0,
        engine_version="0.24.1",
        sample_rate=RATE,
        chapters=list(chapters),
    )


# --- フレーム数と G7（ffmpeg は要らない） ------------------------------------


def test_frame_counts_do_not_drift():
    # 0.35 秒のショットが 100 本。1本ずつ丸めると 10.5 → 10 で 1000 フレーム（50 足りない）
    starts = [i * 0.35 for i in range(100)]
    counts = frame_counts(starts, 35.0, 30)
    assert sum(counts) == round(35.0 * 30) == 1050
    assert set(counts) <= {10, 11}


def test_frame_counts_follow_the_chapter_clock():
    counts = frame_counts([0.0, 0.62, 1.0], 1.37, 30)
    assert counts == [round(0.62 * 30), round(1.0 * 30) - round(0.62 * 30), 41 - 30]
    assert sum(counts) == round(1.37 * 30)


def test_frame_counts_start_at_zero_and_stop_at_the_chapter_end():
    # 最初のショットが許す幅の中で 0 からずれていても、章の頭（0 フレーム目）から数える
    assert frame_counts([0.009, 0.5], 1.0, 60) == [30, 30]
    # 区切りが章の終わりを越えても、章のフレーム数は round(end*fps) のまま
    assert frame_counts([0.0, 1.009], 1.0, 60) == [60, 0]
    assert frame_counts([], 1.0, 30) == []


def _plan_media(tmp_path: Path, chapters: dict[str, tuple[float, int]]) -> tuple[Path, list]:
    """_plan を試すための media（画像は中身の無いファイル、wav は無音）。

    chapters は章の ID → （長さ, チャンネル数）。
    """
    media = tmp_path / "media"
    (media / "cards").mkdir(parents=True)
    (media / "cards" / "a.png").write_bytes(b"x")
    out, offset = [], 0.0
    for cid, (sec, channels) in chapters.items():
        path = media / "voice" / f"{cid}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        n = round(sec * RATE)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(bytes(2 * channels * n))
        out.append(_chapter(cid, n / RATE, offset=offset))
        offset += n / RATE
    return media, out


def test_plan_rejects_negative_shot_durations(tmp_path):
    media, chapters = _plan_media(tmp_path, {"ch1": (1.0, 1)})
    # 終わりの位置だけを見ると章の長さに合ってしまう並び（1.2 + (-0.2) = 1.0）
    shots = ShotList(
        EP,
        W,
        H,
        FPS,
        [
            Shot("ch1", 0.0, 1.2, "cards/a.png", "still", "title_card"),
            Shot("ch1", 1.2, -0.2, "cards/a.png", "still", "title_card"),
        ],
    )
    with pytest.raises(RenderError, match="長さ（-0.2）が正しくありません"):
        _plan(media, _manifest(*chapters), shots)


def test_plan_accepts_zero_length_shot_in_any_order(tmp_path):
    media, chapters = _plan_media(tmp_path, {"ch1": (1.0, 1)})
    shots = ShotList(
        EP,
        W,
        H,
        FPS,
        [
            Shot("ch1", 0.0, 0.5, "cards/a.png", "still", "title_card"),
            Shot("ch1", 0.5, 0.5, "cards/a.png", "still", "title_card"),
            Shot("ch1", 0.5, 0.0, "cards/a.png", "still", "chart"),
        ],
    )
    [(_, got, counts)] = _plan(media, _manifest(*chapters), shots)
    assert [s.duration for s in got] == [0.5, 0.0, 0.5]
    assert counts == [15, 0, 15]


def test_plan_rejects_chapter_wavs_of_different_formats(tmp_path):
    # 章の wav は concat でそのままつなぐので、チャンネル数が違うと音が壊れる
    media, chapters = _plan_media(tmp_path, {"ch1": (1.0, 1), "ch2": (1.0, 2)})
    shots = ShotList(
        EP,
        W,
        H,
        FPS,
        [Shot(c.chapter_id, 0.0, c.duration, "cards/a.png", "still", "chart") for c in chapters],
    )
    with pytest.raises(RenderError, match="形式"):
        _plan(media, _manifest(*chapters), shots)


def test_g7_passes_inside_the_ranges():
    m = _manifest(_chapter("ch1", 300.0), _chapter("ch2", 400.0, offset=300.0))
    assert g7_issues(m, {"ch1": (240.0, 360.0), "ch2": (300.0, 480.0)}) == []


def test_g7_reports_total_and_chapters():
    m = _manifest(
        _chapter("ch1", 100.0, title="はじめに"),
        _chapter("ch2", 470.0, offset=100.0, title="論語の一節"),
        _chapter("ch3", 10.0, offset=570.0),
    )
    issues = g7_issues(m, {"ch1": (60.0, 90.0), "ch2": (240.0, 480.0), "ch9": (30.0, 60.0)})
    assert len(issues) == 3
    assert (
        issues[0] == "全体の長さが 9:40（580.0 秒）で、10:00〜15:00 に入っていません（短すぎます）"
    )
    assert "章「はじめに」（ch1）の長さが 1:40（100.0 秒）" in issues[1]
    assert issues[1].endswith("（長すぎます）")
    assert "ch9" in issues[2] and "音声がありません" in issues[2]
    # ch2 は範囲の中、ch3 は範囲が無いので見ない
    assert not any("ch2" in x or "ch3" in x for x in issues)


def test_g7_too_long_total():
    m = _manifest(_chapter("ch1", 901.0))
    issues = g7_issues(m, {}, total=(600.0, 900.0))
    assert issues == [
        "全体の長さが 15:01（901.0 秒）で、10:00〜15:00 に入っていません（長すぎます）"
    ]


def test_render_needs_ffmpeg(tmp_path):
    m = _manifest(_chapter("ch1", 1.0))
    shots = ShotList(EP, W, H, FPS, [Shot("ch1", 0.0, 1.0, "cards/a.png", "still", "title_card")])
    with pytest.raises(RenderError, match="brew install ffmpeg"):
        render_episode(tmp_path, m, shots, ffmpeg="koten-no-such-ffmpeg", log=lambda _: None)


# --- 動画を作る（ffmpeg が要る） ---------------------------------------------


def _image(path: Path, src: str) -> str:
    """ffmpeg の lavfi（色・試験の絵）で小さな画像を書き、media からの相対パスを返す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src]
    subprocess.run([*cmd, "-frames:v", "1", "-update", "1", str(path)], check=True)
    return path.parent.name + "/" + path.name


def _silent_wav(path: Path, sec: float) -> float:
    """無音の wav を書き、実際の長さ（サンプル数 / 周波数）を返す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = round(sec * RATE)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(2 * n))
    return n / RATE


def _media(tmp_path: Path, lengths: dict[str, float]) -> tuple[Path, VoiceManifest, dict]:
    # HDD の名前に空白・日本語・' があっても、concat の一覧が読めること
    media = tmp_path / "SD カード's 古典×悩み" / "media" / EP
    images = {
        "card": _image(media / "cards" / "ch1-s1.png", f"smptebars=s={W}x{H}"),
        "photo": _image(media / "photos" / "p1.jpg", f"testsrc2=s={W}x{H}"),
    }
    chapters, offset = [], 0.0
    for cid, sec in lengths.items():
        d = _silent_wav(media / "voice" / f"{cid}.wav", sec)
        chapters.append(_chapter(cid, d, offset=offset))
        offset += d
    return media, _manifest(*chapters), images


def _streams(path: Path) -> list[dict[str, str]]:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,nb_frames"]
    out = subprocess.run([*cmd, "-of", "compact", str(path)], capture_output=True, text=True)
    rows = []
    for line in out.stdout.splitlines():
        rows.append(dict(kv.split("=", 1) for kv in line.split("|")[1:]))
    return rows


def _gray_frames(path: Path) -> list[bytes]:
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-f", "rawvideo"]
    raw = subprocess.run([*cmd, "-pix_fmt", "gray", "-"], capture_output=True).stdout
    return [raw[i : i + W * H] for i in range(0, len(raw), W * H)]


def _diff(a: bytes, b: bytes) -> float:
    """2つのフレームの、画素ごとの明るさの差の平均（0〜255）。"""
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


def _decode_errors(path: Path) -> str:
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"]
    return subprocess.run(cmd, capture_output=True, text=True).stderr.strip()


@needs_ffmpeg
def test_render_one_chapter_two_shots(tmp_path):
    media, m, img = _media(tmp_path, {"ch1": 1.37})
    d = m.chapters[0].duration
    shots = ShotList(
        EP,
        W,
        H,
        FPS,
        [
            Shot("ch1", 0.0, 0.62, img["card"], "still", "title_card", ["ch1-s1"]),
            Shot("ch1", 0.62, d - 0.62, img["photo"], "zoom", "photo", ["ch1-s2"]),
        ],
    )
    logs: list[str] = []
    out = render_episode(media, m, shots, bitrate="1M", log=logs.append)

    assert out == media.resolve() / "render" / f"{EP}.mp4"
    assert (media / "render" / "ch1.mp4").is_file()
    assert not (media / "render" / "tmp").exists()  # 途中のファイルは消える
    assert probe_duration(out) == pytest.approx(d, abs=0.05)
    assert probe_duration(media / "render" / "ch1.mp4") == pytest.approx(d, abs=0.05)
    video, audio = sorted(_streams(out), key=lambda s: s["codec_type"], reverse=True)
    assert (video["codec_name"], int(video["nb_frames"])) == ("h264", round(d * FPS))
    assert audio["codec_name"] == "aac"
    assert any("完成" in x for x in logs)
    assert _decode_errors(out) == ""
    # カード（still）は動かず、写真（zoom）はゆっくり寄る
    frames = _gray_frames(out)
    card = round(0.62 * FPS)
    assert _diff(frames[0], frames[card - 1]) < 1.0
    assert _diff(frames[card], frames[-1]) > 3.0


@needs_ffmpeg
def test_render_two_chapters_keeps_chapter_starts(tmp_path):
    media, m, img = _media(tmp_path, {"ch1": 1.37, "ch2": 0.93})
    d1, d2 = (c.duration for c in m.chapters)
    shots = ShotList(
        EP,
        W,
        H,
        FPS,
        [
            Shot("ch1", 0.0, 0.4, img["photo"], "zoom", "photo"),
            Shot("ch1", 0.4, d1 - 0.4, img["card"], "still", "quote_card"),
            Shot("ch2", 0.0, d2, img["photo"], "zoom", "photo"),
        ],
    )
    out = render_episode(media, m, shots, bitrate="1M", log=lambda _: None)

    assert probe_duration(out) == pytest.approx(d1 + d2, abs=0.05)
    video = next(s for s in _streams(out) if s["codec_type"] == "video")
    assert int(video["nb_frames"]) == round(d1 * FPS) + round(d2 * FPS)
    # 2章目の最初のフレームは、音声の章の頭（offset）にある
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "packet=pts_time"]
    pts = subprocess.run([*cmd, "-of", "csv=p=0", str(out)], capture_output=True, text=True)
    times = sorted(float(x) for x in pts.stdout.split())
    assert times[round(d1 * FPS)] == pytest.approx(m.chapters[1].offset, abs=0.001)
    assert _decode_errors(out) == ""


@needs_ffmpeg
def test_render_rejects_shots_that_do_not_cover_the_audio(tmp_path):
    media, m, img = _media(tmp_path, {"ch1": 1.37})
    short = ShotList(EP, W, H, FPS, [Shot("ch1", 0.0, 1.0, img["card"], "still", "title_card")])
    with pytest.raises(RenderError, match="合いません"):
        render_episode(media, m, short, log=lambda _: None)
    gap = ShotList(
        EP,
        W,
        H,
        FPS,
        [
            Shot("ch1", 0.0, 0.5, img["card"], "still", "title_card"),
            Shot("ch1", 0.7, 0.67, img["photo"], "zoom", "photo"),
        ],
    )
    with pytest.raises(RenderError, match="すき間"):
        render_episode(media, m, gap, log=lambda _: None)
    assert not (media / "render").exists()  # 確かめてから作るので、何も書かない


@needs_ffmpeg
def test_render_reports_missing_files(tmp_path):
    media, m, img = _media(tmp_path, {"ch1": 1.0})
    d = m.chapters[0].duration
    missing = ShotList(EP, W, H, FPS, [Shot("ch1", 0.0, d, "cards/none.png", "still", "chart")])
    with pytest.raises(RenderError, match="画像がありません：cards/none.png"):
        render_episode(media, m, missing, log=lambda _: None)
    (media / "voice" / "ch1.wav").unlink()
    ok = ShotList(EP, W, H, FPS, [Shot("ch1", 0.0, d, img["card"], "still", "chart")])
    with pytest.raises(RenderError, match="章の音声がありません"):
        render_episode(media, m, ok, log=lambda _: None)


@needs_ffmpeg
def test_probe_duration_of_a_missing_file(tmp_path):
    with pytest.raises(RenderError, match="長さを読めません"):
        probe_duration(tmp_path / "none.mp4")
