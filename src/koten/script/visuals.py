"""台本の画面の内訳と、写真が何枚要るかの見積もり（M6 の前の確かめ）。

写真の段落（visual が photo）ごとに1枚、またはその段落を every 秒ごとに替える場合の枚数を数える。
同じ写真は直近 photo_episodes 回で使わない（SPEC §8.10）ので、要る枚数は1回分 × その回数。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from koten.script.placeholders import Resolver

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp"}


@dataclass
class EpisodeVisuals:
    episode_id: str
    seconds: dict[str, float] = field(default_factory=dict)  # 画面の種類ごとの秒数
    counts: dict[str, int] = field(default_factory=dict)  # 画面の種類ごとの段落の数
    photo_segments: list[float] = field(default_factory=list)  # 写真の段落ごとの秒数

    def photos(self, every: float | None) -> int:
        """写真の枚数：段落ごとに1枚（every が None）か、every 秒ごとに替える。"""
        if not every:
            return len(self.photo_segments)
        return sum(max(1, math.ceil(sec / every)) for sec in self.photo_segments)


def episode_visuals(
    episode_id: str, script: dict[str, Any], resolver: Resolver, chars_per_min: int
) -> EpisodeVisuals:
    out = EpisodeVisuals(episode_id)
    for ch in script["chapters"]:
        for seg in ch["segments"]:
            kind = (seg.get("visual") or {}).get("type") or "none"
            try:
                chars = len(resolver.resolve(seg["text"]))
            except KeyError:
                chars = len(seg["text"])
            sec = chars * 60 / chars_per_min
            out.seconds[kind] = out.seconds.get(kind, 0.0) + sec
            out.counts[kind] = out.counts.get(kind, 0) + 1
            if kind == "photo":
                out.photo_segments.append(sec)
    return out


def count_photos(photos_dir: Path) -> int | None:
    if not photos_dir.exists():
        return None
    return sum(
        1
        for p in photos_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in PHOTO_EXT and not p.name.startswith(".")
    )
