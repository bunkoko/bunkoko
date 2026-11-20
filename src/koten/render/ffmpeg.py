"""動画の組み立てと G7（尺）。docs/m6_design.md の koten.render・SPEC §8.8・§8.10。

流れ（途中のファイルは render/tmp/ に置き、終わったら消す）：

1. ショットごとに、画像から長さちょうどの動画を作る（libx264・yuv420p・同じ大きさと fps）。
   フレーム数は、章の頭からの時刻を round(t*fps) で区切った差にする
   （足すと round(章の長さ*fps)。ショットごとに丸めると、ずれがたまる）。
   写真（zoom）は zoompan でゆっくり寄る。カード（still）は動かさない
2. 章ごとにショットをつなぐ（concat demuxer。どのショットも同じ設定で作るので、
   つなぎ直さずに済む）
3. 章の映像に章の wav をのせて render/<chapter_id>.mp4（AAC 192k）
4. 章の映像をつなぎ（-c copy）、章の wav をつないだ音を AAC にして render/<episode_id>.mp4。
   できた長さを ffprobe で確かめる

4 で音を章の mp4 から -c copy でつながないのは、AAC が章の頭ごとに 1024 サンプルの前置き
（エンコーダーの遅れ）を持つため。そのままつなぐと時刻が逆戻りし、章ごとに音が
20 ミリ秒ほど遅れていく。映像は章の mp4 と同じもの（つなぎ直していない）を使い、
章の頭は音声の offset にそろえる。
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from collections.abc import Callable
from pathlib import Path

from koten.media.model import ChapterTiming, Shot, ShotList, VoiceManifest

ZOOM_END = 1.06  # 写真はショットのあいだに 1.00 → 1.06 倍へゆっくり寄る
# zoompan は切り抜きの位置を整数に丸めるので、2倍に広げてから寄る（揺れを減らす）
ZOOM_OVERSAMPLE = 2
# 音は AAC 192k。サンプリング周波数とチャンネルは YouTube の勧める 48kHz・ステレオにそろえる
AUDIO = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
TIME_TOL = 0.01  # ショットの区切りと章の長さの食い違いを許す幅（秒）
WAV_TOL = 0.05  # 章の wav の長さと manifest の食い違いを許す幅（秒）
DURATION_TOL = 0.1  # できた動画の長さと音声の長さの食い違いを許す幅（秒）


class RenderError(RuntimeError):
    """動画を作れないとき（ffmpeg が無い・ファイルが無い・画面の切り替えと音声が合わない など）。"""


def frame_counts(starts: list[float], end: float, fps: int) -> list[int]:
    """ショットごとのフレーム数。章の頭からの時刻を round(t*fps) で区切った差。

    starts はショットの始め（章の頭から。昇順）、end は章の終わり。足すと round(end*fps) になる。
    最初のショットは章の頭（0 フレーム目）から始める。区切りは章の終わりを越えない
    （許す幅の中のずれで、章の映像が音声より長くも短くもならないように）。
    """
    if not starts:
        return []
    last = round(end * fps)
    bounds = [0] + [min(max(round(t * fps), 0), last) for t in starts[1:]] + [last]
    return [b - a for a, b in zip(bounds, bounds[1:], strict=False)]


def render_episode(
    media: Path,
    manifest: VoiceManifest,
    shots: ShotList,
    *,
    bitrate: str = "8M",
    ffmpeg: str = "ffmpeg",
    log: Callable[[str], None] = print,
) -> Path:
    """ショットと章の音声から、章ごとの mp4 と完成した mp4（render/<episode_id>.mp4）を作る。"""
    ffmpeg_path = shutil.which(ffmpeg)
    if ffmpeg_path is None:
        raise RenderError(
            f"{ffmpeg} が見つかりません。Mac なら brew install ffmpeg で入れてください"
        )
    ffprobe = _ffprobe_next_to(ffmpeg_path)
    media = Path(media).resolve()
    plan = _plan(media, manifest, shots)

    out_dir = media / "render"
    tmp = out_dir / "tmp"
    if tmp.exists():
        shutil.rmtree(tmp)  # 前に途中で止まったときの残り
    tmp.mkdir(parents=True)
    try:
        videos: list[tuple[ChapterTiming, Path]] = []
        for ch, chapter_shots, counts in plan:
            parts = []
            for i, (shot, n) in enumerate(zip(chapter_shots, counts, strict=True)):
                if n <= 0:
                    continue  # 半フレームより短いショットは、前後のショットに吸われる
                part = tmp / f"{ch.chapter_id}-{i:03d}.mp4"
                _encode_shot(ffmpeg_path, media / shot.image, shot.motion, n, shots, bitrate, part)
                parts.append(part)
            video = tmp / f"{ch.chapter_id}-video.mp4"
            _concat(ffmpeg_path, parts, tmp / f"{ch.chapter_id}.txt", video)
            chapter_mp4 = tmp / f"{ch.chapter_id}.mp4"
            _run(
                [ffmpeg_path, "-i", str(video), "-i", str(media / ch.wav)]
                + ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *AUDIO, str(chapter_mp4)],
                f"章 {ch.chapter_id} の音声のせ",
            )
            chapter_mp4.replace(out_dir / f"{ch.chapter_id}.mp4")
            videos.append((ch, video))
            size = f"ショット {len(parts)} 本・{sum(counts)} フレーム"
            log(f"{ch.chapter_id}：{size}（{_mmss(ch.duration)}）")

        final_tmp = tmp / f"{manifest.episode_id}.mp4"
        video_list = tmp / "episode-video.txt"
        video_list.write_text(
            "".join(f"{_file_line(v)}duration {ch.duration:.6f}\n" for ch, v in videos), "utf-8"
        )
        wav_list = tmp / "episode-audio.txt"
        wav_list.write_text("".join(_file_line(media / ch.wav) for ch, _ in videos), "utf-8")
        _run(
            [ffmpeg_path, *_concat_input(video_list), *_concat_input(wav_list)]
            + ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *AUDIO]
            + ["-movflags", "+faststart", str(final_tmp)],
            "章をつなぐ",
        )
        actual = probe_duration(final_tmp, ffprobe=ffprobe)
        expected = manifest.total_sec
        if abs(actual - expected) > DURATION_TOL:
            raise RenderError(
                f"できた動画の長さ {actual:.2f} 秒が、音声の長さ {expected:.2f} 秒と合いません"
            )
        final = out_dir / f"{manifest.episode_id}.mp4"
        final_tmp.replace(final)
        log(f"完成：render/{final.name}（{_mmss(actual)}）")
        return final
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def probe_duration(path: Path, ffprobe: str = "ffprobe") -> float:
    """ファイルの長さ（秒）を ffprobe で読む。"""
    cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration"]
    cmd += ["-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    except FileNotFoundError as e:
        raise RenderError(
            f"{ffprobe} が見つかりません。Mac なら brew install ffmpeg で入れてください"
            "（ffprobe も一緒に入ります）"
        ) from e
    if r.returncode != 0:
        raise RenderError(f"{path} の長さを読めません（ffprobe：{_last_line(r.stderr)}）")
    try:
        return float(r.stdout.strip())
    except ValueError as e:
        raise RenderError(
            f"{path} の長さを読めません（ffprobe の答え：{r.stdout.strip()!r}）"
        ) from e


def g7_issues(
    manifest: VoiceManifest,
    chapter_ranges: dict[str, tuple[float, float]],
    total: tuple[float, float] = (600.0, 900.0),
) -> list[str]:
    """G7（尺）：全体の長さ・章の長さが範囲の外なら、その文を返す。空なら合格。

    chapter_ranges は章の ID → （下の端, 上の端）。範囲の無い章は見ない。
    音声に無い章が chapter_ranges にあれば、それも返す。
    """
    issues = []
    t = manifest.total_sec
    lo, hi = total
    if not lo <= t <= hi:
        issues.append(f"全体の長さが {_out_of_range(t, lo, hi)}")
    seen = set()
    for ch in manifest.chapters:
        seen.add(ch.chapter_id)
        if ch.chapter_id not in chapter_ranges:
            continue
        lo, hi = chapter_ranges[ch.chapter_id]
        d = ch.duration
        if not lo <= d <= hi:
            name = f"章「{ch.title}」（{ch.chapter_id}）" if ch.title else f"章 {ch.chapter_id}"
            issues.append(f"{name}の長さが {_out_of_range(d, lo, hi)}")
    for cid in chapter_ranges:
        if cid not in seen:
            issues.append(f"章 {cid} の音声がありません（koten voice をやり直してください）")
    return issues


# --- 中身 ---------------------------------------------------------------------


def _plan(
    media: Path, manifest: VoiceManifest, shots: ShotList
) -> list[tuple[ChapterTiming, list[Shot], list[int]]]:
    """作る前に全部を確かめ、章ごとの（章・ショット・フレーム数）を返す。

    ショットは章の [0, 長さ] をすき間なく重ならずに覆っていること（koten visuals の約束）。
    """
    if shots.episode_id != manifest.episode_id:
        raise RenderError(
            f"画面の切り替え（{shots.episode_id}）と音声（{manifest.episode_id}）の回が違います"
        )
    if shots.fps <= 0 or shots.width <= 0 or shots.height <= 0:
        raise RenderError(
            f"大きさか fps が正しくありません（{shots.width}×{shots.height}・{shots.fps}）"
        )
    if shots.width % 2 or shots.height % 2:
        raise RenderError(f"幅と高さは偶数にしてください（{shots.width}×{shots.height}）")
    if not manifest.chapters:
        raise RenderError("音声に章がありません（koten voice をやり直してください）")
    redo = "（koten visuals をやり直してください）"
    known = {ch.chapter_id for ch in manifest.chapters}
    stray = sorted({s.chapter_id for s in shots.shots} - known)
    if stray:
        raise RenderError(f"画面の切り替えに、音声に無い章があります：{'・'.join(stray)}{redo}")

    plan = []
    formats = set()
    for ch in manifest.chapters:
        # 章の wav は concat でそのままつなぐので、周波数・チャンネル数・ビット数がそろっていること
        formats.add(_check_wav(media / ch.wav, ch))
        if len(formats) > 1:
            raise RenderError(
                "章の wav の形式（サンプリング周波数・チャンネル数・ビット数）がそろっていません"
                "（koten voice をやり直してください）"
            )
        chapter_shots = sorted(
            (s for s in shots.shots if s.chapter_id == ch.chapter_id),
            key=lambda s: (s.start, s.duration),  # 長さ 0 のショットは同じ時刻のショットの前に
        )
        if not chapter_shots:
            raise RenderError(f"章 {ch.chapter_id} のショットがありません{redo}")
        pos = 0.0
        for s in chapter_shots:
            if abs(s.start - pos) > TIME_TOL:
                raise RenderError(
                    f"章 {ch.chapter_id} のショットが {pos:.2f} 秒のところで"
                    f"{'すき間' if s.start > pos else '重なり'}になっています{redo}"
                )
            if not s.duration >= 0:  # 負の長さ（と NaN）は、すき間と重なりを隠してしまう
                raise RenderError(
                    f"章 {ch.chapter_id} の {s.start:.2f} 秒からのショットの長さ"
                    f"（{s.duration}）が正しくありません{redo}"
                )
            if s.motion not in ("zoom", "still"):
                raise RenderError(f"ショットの動き {s.motion!r} がわかりません（zoom か still）")
            if not (media / s.image).is_file():
                raise RenderError(f"画像がありません：{s.image}{redo}")
            pos = s.start + s.duration
        if abs(pos - ch.duration) > TIME_TOL:
            raise RenderError(
                f"章 {ch.chapter_id} の画面の切り替え（{pos:.2f} 秒）が、音声の長さ"
                f"（{ch.duration:.2f} 秒）と合いません{redo}"
            )
        counts = frame_counts([s.start for s in chapter_shots], ch.duration, shots.fps)
        if sum(counts) <= 0:
            raise RenderError(
                f"章 {ch.chapter_id} が短すぎて動画になりません（{ch.duration:.3f} 秒）"
            )
        plan.append((ch, chapter_shots, counts))
    return plan


def _check_wav(path: Path, ch: ChapterTiming) -> tuple[int, int, int]:
    """章の wav があり、長さが manifest と合うことを確かめる。

    （サンプリング周波数, チャンネル数, 1サンプルのバイト数）を返す。
    """
    if not path.is_file():
        raise RenderError(f"章の音声がありません：{ch.wav}（koten voice が先）")
    try:
        with wave.open(str(path), "rb") as w:
            fmt = (w.getframerate(), w.getnchannels(), w.getsampwidth())
            sec = w.getnframes() / fmt[0]
    except (wave.Error, EOFError, ZeroDivisionError) as e:
        raise RenderError(f"章の音声 {ch.wav} を読めません（{e}）") from e
    if abs(sec - ch.duration) > WAV_TOL:
        raise RenderError(
            f"章の音声 {ch.wav} の長さ（{sec:.2f} 秒）が manifest（{ch.duration:.2f} 秒）と"
            "合いません（koten voice をやり直してください）"
        )
    return fmt


def _encode_shot(
    ffmpeg: str, image: Path, motion: str, n: int, shots: ShotList, bitrate: str, out: Path
) -> None:
    """画像1枚から n フレームの動画を作る。

    どのショットも同じ設定（大きさ・fps・libx264・yuv420p・ビットレート）で作るので、
    章の中でも章のあいだでも、つなぎ直さずに（-c copy で）つなげる。
    """
    w, h, fps = shots.width, shots.height, shots.fps
    if motion == "still":
        # カード：同じ絵を n フレーム
        src = ["-loop", "1", "-framerate", str(fps), "-i", str(image)]
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    else:
        # 写真：1枚から zoompan で n フレーム。真ん中に向かって 1.00 → ZOOM_END 倍
        src = ["-i", str(image)]
        bw, bh = w * ZOOM_OVERSAMPLE, h * ZOOM_OVERSAMPLE
        vf = (
            f"scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"zoompan=z='1+{ZOOM_END - 1:.4f}*on/{n}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2'"
            f":d={n}:s={w}x{h}:fps={fps}"
        )
    encode = ["-c:v", "libx264", "-b:v", bitrate, "-pix_fmt", "yuv420p", "-r", str(fps), "-an"]
    _run(
        [ffmpeg, *src, "-vf", f"{vf},setsar=1,format=yuv420p", *encode, "-frames:v", str(n)]
        + [str(out)],
        f"ショット（{image.name}）",
    )


def _concat(ffmpeg: str, parts: list[Path], list_path: Path, out: Path) -> None:
    list_path.write_text("".join(_file_line(p) for p in parts), "utf-8")
    _run([ffmpeg, *_concat_input(list_path), "-c", "copy", str(out)], f"{out.stem} をつなぐ")


def _concat_input(list_path: Path) -> list[str]:
    return ["-f", "concat", "-safe", "0", "-i", str(list_path)]


def _file_line(path: Path) -> str:
    """concat demuxer の一覧の1行。' はファイル名の中で '\\'' と書く。"""
    escaped = str(Path(path).resolve()).replace("'", "'\\''")
    return f"file '{escaped}'\n"


def _run(cmd: list[str], what: str) -> None:
    full = [cmd[0], "-hide_banner", "-nostdin", "-v", "error", "-y", *cmd[1:]]
    r = subprocess.run(full, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RenderError(f"{what}で ffmpeg が失敗しました：{_last_line(r.stderr)}")


def _ffprobe_next_to(ffmpeg_path: str) -> str:
    """ffmpeg と同じ場所の ffprobe（無ければ PATH の ffprobe）。"""
    found = shutil.which("ffprobe", path=str(Path(ffmpeg_path).parent)) or shutil.which("ffprobe")
    if found is None:
        raise RenderError("ffprobe が見つかりません。Mac なら brew install ffmpeg で入れてください")
    return found


def _last_line(stderr: str) -> str:
    lines = [x for x in stderr.strip().splitlines() if x.strip()]
    return lines[-1] if lines else "不明なエラー"


def _out_of_range(sec: float, lo: float, hi: float) -> str:
    """「9:30（570.0 秒）で、10:00〜15:00 に入っていません（短すぎます）」"""
    side = "短すぎます" if sec < lo else "長すぎます"
    return f"{_mmss(sec)}（{sec:.1f} 秒）で、{_mmss(lo)}〜{_mmss(hi)} に入っていません（{side}）"


def _mmss(sec: float) -> str:
    s = int(sec)
    return f"{s // 60}:{s % 60:02d}"
