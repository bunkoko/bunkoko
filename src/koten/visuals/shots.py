"""画面の切り替え（ショットリスト。docs/m6_design.md・SPEC §8.10）。

章ごとに、音声の [0, 章の長さ] を、すき間なく重ならずにショットで覆う。

- 段落の区切りは段落の start（最初の段落は 0 から）。章の終わりの間は最後のショットに含める
- カードのある段落：その段落の区間を、そのカード1枚（still）
- 写真の段落（photo。author_card・page_image も今は写真）：続く写真の段落をまとめた区間を、
  min_shot〜max_shot 秒（目安 7 秒）の同じ長さに区切り、写真を順に当てる（zoom）。
  同じ長さに割るので、短すぎる端は出ない（端の分は前のショットたちに足される）
"""

from __future__ import annotations

import math

from koten.media.model import ChapterTiming, Shot, ShotList, VoiceManifest

TARGET_SHOT = 7.0  # 写真を替える目安の秒数
CARD_KINDS = ("quote_card", "title_card", "chart")


def photo_pieces(length: float, min_shot: float, max_shot: float) -> int:
    """写真の区間をいくつに割るか。どれも min_shot〜max_shot に入り、目安の長さに近い数。

    区間が min_shot より短ければ1つ（それより短いショットは避けられない）。
    """
    if length <= min_shot:
        return 1
    lo = max(1, math.ceil(length / max_shot - 1e-9))
    hi = max(lo, math.floor(length / min_shot + 1e-9))
    return min(hi, max(lo, round(length / TARGET_SHOT)))


Span = tuple[str, float, float]  # 段落の ID・始め・終わり（章の頭から）
Run = tuple[float, float, list[Span], bool]  # 区間の始め・終わり・中の段落・カードか


def _runs(ch: ChapterTiming, cards: dict[str, str]) -> list[Run]:
    """章を、段落の区切りで区間に分ける。続く写真の段落は1つの区間にまとめる。

    段落の区間は、その段落の start から次の段落の start まで
    （最初の段落は 0 から、最後の段落は章の終わりまで）。
    長さが 0 の段落は飛ばす（カードでも映せない。前後の写真の区間は1つにつなぐ）。
    """
    if not ch.segments:
        return [(0.0, ch.duration, [], False)] if ch.duration > 0 else []
    bounds = [0.0]
    for seg in ch.segments[1:]:
        bounds.append(min(max(seg.start, bounds[-1]), ch.duration))
    bounds.append(ch.duration)
    runs: list[Run] = []
    for i, seg in enumerate(ch.segments):
        span = (seg.seg_id, bounds[i], bounds[i + 1])
        if span[2] <= span[1]:
            continue
        is_card = seg.seg_id in cards
        if runs and not is_card and not runs[-1][3]:
            prev = runs[-1]
            runs[-1] = (prev[0], span[2], [*prev[2], span], False)
        else:
            runs.append((span[1], span[2], [span], is_card))
    return runs


def build_shots(
    manifest: VoiceManifest,
    cards: dict[str, str],
    photos: list[str],
    *,
    width: int,
    height: int,
    fps: int,
    min_shot: float = 5.0,
    max_shot: float = 10.0,
) -> ShotList:
    """音声の時刻・カード（seg_id → 画像の相対パス）・写真（media からの相対パス）から ShotList。

    写真は回の中で順に当てる（足りなければ始めに戻る）。photos が空なら ValueError。
    """
    if not photos:
        raise ValueError("写真が0枚です（写真が無いときは、色だけの予備の画像を1枚渡してください）")
    if not 0 < min_shot <= max_shot:
        raise ValueError(f"min_shot（{min_shot}）と max_shot（{max_shot}）の大きさが合いません")
    kinds = {
        seg.seg_id: (seg.visual or {}).get("type") or ""
        for ch in manifest.chapters
        for seg in ch.segments
    }
    shots: list[Shot] = []
    used = 0  # 当てた写真の数（回の中で通しで数える）
    for ch in manifest.chapters:
        for start, end, spans, is_card in _runs(ch, cards):
            if is_card:
                sid = spans[0][0]
                kind = kinds.get(sid, "")
                shots.append(
                    Shot(
                        chapter_id=ch.chapter_id,
                        start=start,
                        duration=end - start,
                        image=cards[sid],
                        motion="still",
                        kind=kind if kind in CARD_KINDS else "title_card",
                        seg_ids=[sid],
                    )
                )
                continue
            k = photo_pieces(end - start, min_shot, max_shot)
            # 区切りの時刻を先に決め、ショットの長さは隣の区切りの差にする（足すと区間の長さになる）
            cuts = [start + (end - start) * j / k for j in range(k)] + [end]
            for j in range(k):
                a, b = cuts[j], cuts[j + 1]
                shots.append(
                    Shot(
                        chapter_id=ch.chapter_id,
                        start=a,
                        duration=b - a,
                        image=photos[used % len(photos)],
                        motion="zoom",
                        kind="photo",
                        seg_ids=[sid for sid, s0, s1 in spans if s0 < b and s1 > a],
                    )
                )
                used += 1
    return ShotList(
        episode_id=manifest.episode_id, width=width, height=height, fps=fps, shots=shots
    )
