"""1回分の動画づくりの段（koten voice・visuals・render・kit・review・produce の中身）。

段ごとに HDD の media/<回>/ に結果を残し、次の段はそれを読む（途中からやり直せる）。
回の状態：script_done → voiced（音声）→ rendered（動画と G7）→ reviewed（人の承認）。
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from koten.config import REPO_ROOT, Settings
from koten.media.model import (
    MANIFEST,
    SHOTS,
    ReviewBundle,
    ShotList,
    ThumbSpec,
    VoiceManifest,
    exports_dir,
    media_dir,
)
from koten.media.prepare import card_specs, chapter_ranges, kit_input, prepare_lines, used_ids

TEMPLATES = REPO_ROOT / "templates"
PHOTO_EPISODES = 5  # 同じ写真は直近5回で使わない（SPEC §8.10）
PHOTO_EVERY = 7.0  # 写真を替える目安の秒数（visuals.build_shots の目安と同じ）
VOICE_STATES = {"script_done", "voiced", "rendered", "reviewed"}


class ProduceError(RuntimeError):
    pass


@dataclass
class Episode:
    episode_id: str
    state: str
    script: dict[str, Any]
    verification: dict[str, Any]
    data: Any  # koten.script.data.EpisodeData
    resolver: Any  # koten.script.placeholders.Resolver


def load_episode(conn: sqlite3.Connection, ssd_root: Path, episode_id: str) -> Episode:
    from koten.script import gates
    from koten.script.data import EpisodeData

    row = conn.execute(
        "SELECT state, script_json, verification_json FROM episodes WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    if row is None:
        raise ProduceError(f"{episode_id} という回はありません")
    if not row["script_json"]:
        raise ProduceError(f"{episode_id} の台本がありません（koten script が先）")
    script = json.loads(row["script_json"])
    data = EpisodeData.load(conn, ssd_root, episode_id)
    return Episode(
        episode_id,
        row["state"],
        script,
        json.loads(row["verification_json"] or "{}"),
        data,
        gates.resolver_for(data, script),
    )


def _set_state(
    conn: sqlite3.Connection, episode_id: str, state: str, msg: str, **cols: Any
) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    with conn:
        conn.execute(
            f"UPDATE episodes SET state = ?, state_msg = ?{', ' + sets if sets else ''} "
            "WHERE episode_id = ?",
            (state, msg, *cols.values(), episode_id),
        )


def dictionary_words(conn: sqlite3.Connection, ep: Episode) -> list[tuple[str, str]]:
    """読み上げの辞書に入れる語：この回の一節の作品名と著者・訳者の名前（書誌の読みがあるものだけ）。"""
    ids = used_ids(ep.script)
    words: dict[str, str] = {}
    for cid in ids["quotes"]:
        c = ep.resolver.candidates.get(cid) or {}
        w = conn.execute(
            "SELECT title, title_yomi FROM works_meta WHERE work_id = ?", (c.get("work_id"),)
        ).fetchone()
        if w and w["title"] and w["title_yomi"]:
            words.setdefault(w["title"], w["title_yomi"])
        for pid in [*(c.get("author_ids") or []), *(c.get("translator_ids") or [])]:
            p = conn.execute(
                "SELECT name, name_yomi FROM persons WHERE person_id = ?", (pid,)
            ).fetchone()
            if p and p["name"] and p["name_yomi"]:
                words.setdefault(p["name"], p["name_yomi"])
    return sorted(words.items())


# --- 音声 ---------------------------------------------------------------------


def step_voice(
    s: Settings,
    conn: sqlite3.Connection,
    episode_id: str,
    *,
    client: Any = None,
    log: Callable[[str], None] = print,
) -> VoiceManifest:
    from koten.voice.reading import load_overrides
    from koten.voice.synth import synthesize
    from koten.voice.voicevox import VoicevoxClient

    ep = load_episode(conn, s.ssd_root, episode_id)
    if ep.state not in VOICE_STATES:
        raise ProduceError(f"{episode_id} は {ep.state}。台本（script_done）のあとで作ります")
    take = ep.script.get("human_take") or {}
    conf = s.section("voicevox")
    overrides = load_overrides(s.config_dir / "readings.csv")
    lines = prepare_lines(ep.script, ep.resolver, overrides)
    if client is None:
        client = VoicevoxClient(
            conf.get("endpoint", "http://127.0.0.1:50021"), int(conf["speaker_id"])
        )
    added = client.register_words(dictionary_words(conn, ep))
    if added:
        log(f"読み上げの辞書に {added} 語を足しました（作品名・人名の読み）")
    media = media_dir(s.require_hdd(), episode_id)
    manifest = synthesize(
        lines,
        media,
        client,
        episode_id=episode_id,
        speed_scale=float(conf.get("speed_scale", 1.0)),
        pause_sentence=float(conf.get("pause_sentence", 0.25)),
        pause_segment=float(conf.get("pause_segment", 0.6)),
        pause_chapter=float(conf.get("pause_chapter", 1.2)),
        log=log,
    )
    note = (
        ""
        if (take.get("text") or take.get("audio_path") or take.get("skip"))
        else "（人の一言はまだ）"
    )
    _set_state(
        conn,
        episode_id,
        "voiced",
        f"音声 {_mmss(manifest.total_sec)}{note}",
        duration_sec=manifest.total_sec,
    )
    return manifest


# --- 画面 ---------------------------------------------------------------------


def recent_photos(conn: sqlite3.Connection, episode_id: str, n: int = PHOTO_EPISODES) -> set[str]:
    """直近 n 回（この回を除く、取り消していない回）で使った写真。"""
    rows = conn.execute(
        "SELECT episode_id FROM episodes WHERE episode_id != ? AND state NOT IN ('discarded') "
        "AND episode_id IN (SELECT DISTINCT episode_id FROM media_usage WHERE kind = 'photo') "
        "ORDER BY created_at DESC, episode_id DESC LIMIT ?",
        (episode_id, n),
    ).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    return {
        r[0]
        for r in conn.execute(
            f"SELECT path FROM media_usage WHERE kind = 'photo' AND episode_id IN ({marks})", ids
        )
    }


def photo_seconds(manifest: VoiceManifest, cards: dict[str, str]) -> float:
    return sum(
        seg.end - seg.start
        for ch in manifest.chapters
        for seg in ch.segments
        if seg.seg_id not in cards
    )


def step_visuals(
    s: Settings, conn: sqlite3.Connection, episode_id: str, *, log: Callable[[str], None] = print
) -> ShotList:
    from koten.visuals.cards import render_cards
    from koten.visuals.photos import list_photos, pick_photos, prepare_photo
    from koten.visuals.shots import build_shots

    ep = load_episode(conn, s.ssd_root, episode_id)
    media = media_dir(s.require_hdd(), episode_id)
    if not (media / MANIFEST).exists():
        raise ProduceError(f"{episode_id} の音声がありません（koten voice が先）")
    manifest = VoiceManifest.load(media / MANIFEST)
    video = s.section("video")
    width, height, fps = int(video["width"]), int(video["height"]), int(video["fps"])
    cards = render_cards(card_specs(ep.script, ep.resolver), media, templates_dir=TEMPLATES)
    log(f"カード {len(cards)} 枚")
    photos_dir = Path(s.raw["paths"]["photos_dir"]).expanduser()
    all_photos = list_photos(photos_dir) if photos_dir.exists() else []
    need = max(1, math.ceil(photo_seconds(manifest, cards) / PHOTO_EVERY) + 2)
    picked = pick_photos(all_photos, recent_photos(conn, episode_id), need, episode_id, photos_dir)
    if not picked:
        raise ProduceError(
            f"使える写真がありません（{photos_dir}。直近{PHOTO_EPISODES}回で使った写真は使わない）"
        )
    rels = []
    for i, src in enumerate(picked, start=1):
        rel = f"photos/p{i:03d}.jpg"
        if not (media / rel).exists():
            prepare_photo(src, media / rel, width, height)
        rels.append(rel)
    shots = build_shots(manifest, cards, rels, width=width, height=height, fps=fps)
    shots.photos_used = sorted({str(p.relative_to(photos_dir)) for p in picked})
    shots.save(media / SHOTS)
    with conn:
        conn.execute(
            "DELETE FROM media_usage WHERE episode_id = ? AND kind = 'photo'", (episode_id,)
        )
        conn.executemany(
            "INSERT OR IGNORE INTO media_usage(episode_id, kind, path) VALUES (?, 'photo', ?)",
            [(episode_id, p) for p in shots.photos_used],
        )
    log(f"写真 {len(picked)} 枚・画面の切り替え {len(shots.shots)} 回")
    return shots


# --- 組み立てと G7 --------------------------------------------------------------


def load_formats(s: Settings) -> dict[str, Any]:
    return yaml.safe_load((s.config_dir / "formats.yaml").read_text("utf-8"))["formats"]


def step_render(
    s: Settings, conn: sqlite3.Connection, episode_id: str, *, log: Callable[[str], None] = print
) -> tuple[Path, list[str]]:
    from koten.render.ffmpeg import g7_issues, render_episode

    ep = load_episode(conn, s.ssd_root, episode_id)
    media = media_dir(s.require_hdd(), episode_id)
    if not (media / SHOTS).exists():
        raise ProduceError(f"{episode_id} の画面の切り替えがありません（koten visuals が先）")
    manifest = VoiceManifest.load(media / MANIFEST)
    shots = ShotList.load(media / SHOTS)
    video = s.section("video")
    out = render_episode(media, manifest, shots, bitrate=str(video.get("bitrate", "8M")), log=log)
    ep_conf = s.section("episode")
    issues = g7_issues(
        manifest,
        chapter_ranges(ep.script, load_formats(s)),
        total=(float(ep_conf.get("min_sec", 600)), float(ep_conf.get("max_sec", 900))),
    )
    verification = dict(ep.verification)
    verification["G7"] = {"ok": not issues, "issues": issues, "total_sec": manifest.total_sec}
    _set_state(
        conn,
        episode_id,
        "rendered",
        f"動画 {_mmss(manifest.total_sec)}（G7 {'合格' if not issues else '不合格'}）",
        verification_json=json.dumps(verification, ensure_ascii=False),
        duration_sec=manifest.total_sec,
    )
    return out, issues


# --- 公開用キット ---------------------------------------------------------------


def read_hashtags(config_dir: Path, themes: list[str], fixed: list[str]) -> list[str]:
    """ハッシュタグ：テーマごとの対応表（config/hashtags.csv。人が書く）＋いつも付けるもの。3つまで。"""
    import csv

    path = config_dir / "hashtags.csv"
    tags: list[str] = []
    if path.exists():
        with path.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if (r.get("theme") or "").strip() in themes and (r.get("hashtag") or "").strip():
                    tag = r["hashtag"].strip()
                    tags.append(tag if tag.startswith("#") else f"#{tag}")
    out: list[str] = []
    for t in [*tags, *fixed]:
        if t not in out:
            out.append(t)
    return out[:3]


def step_kit(
    s: Settings, conn: sqlite3.Connection, episode_id: str, *, log: Callable[[str], None] = print
) -> dict[str, Any]:
    from koten.authors import author_label
    from koten.publish_kit.kit import write_kit
    from koten.visuals.cards import render_thumbnails

    ep = load_episode(conn, s.ssd_root, episode_id)
    media = media_dir(s.require_hdd(), episode_id)
    manifest = VoiceManifest.load(media / MANIFEST)
    conf_kit = s.section("kit")
    credit = (s.section("voicevox").get("credit") or "").strip()
    kit = kit_input(
        ep.script,
        ep.resolver,
        fact_sources=ep.data.fact_sources,
        credits=[f"音声：{credit}"] if credit else [],
        hashtags=read_hashtags(
            s.config_dir,
            list(ep.script.get("theme") or []),
            list(conf_kit.get("fixed_hashtags") or []),
        ),
        channel_name=str(conf_kit.get("channel_name") or ""),
    )
    paths = write_kit(kit, manifest, media)
    # サムネイル3枚（別々のテンプレート）：悩みの言葉・著者名・この回の写真
    shots = ShotList.load(media / SHOTS) if (media / SHOTS).exists() else None
    photos = sorted({s_.image for s_ in shots.shots if s_.kind == "photo"}) if shots else []
    ids = used_ids(ep.script)
    authors: list[str] = []
    for cid in ids["quotes"]:
        c = ep.resolver.candidates.get(cid)
        name = author_label(c) if c else ""
        if name and name not in authors:
            authors.append(name)
    words = next(iter(ep.resolver.worries.values()), "")
    specs = [
        ThumbSpec(
            words=words,
            authors=authors[:2],
            photo=str(media / photos[i % len(photos)]) if photos else None,
            template=t,
        )
        for i, t in enumerate(("a", "b", "c"))
    ]
    thumbs = render_thumbnails(specs, media, templates_dir=TEMPLATES)
    log(f"説明欄・字幕・チャプター・サムネイル {len(thumbs)} 枚")
    return {**paths, "thumbnails": thumbs, "kit": kit}


# --- 確認画面 -------------------------------------------------------------------


def blockers_for(
    s: Settings, ep: Episode, *, holds: list[Any], media: Path
) -> tuple[list[str], list[str]]:
    """承認できない理由と、注意。"""
    blockers: list[str] = []
    warnings: list[str] = []
    if ep.state not in ("rendered", "reviewed"):
        blockers.append(f"状態が {ep.state}（動画を作ってから：koten render）")
    if not (media / "render" / f"{ep.episode_id}.mp4").exists():
        blockers.append("完成した動画がありません")
    for gate, result in sorted(ep.verification.items()):
        if isinstance(result, dict) and result.get("ok") is False:
            issues = "・".join(str(x) for x in (result.get("issues") or [])[:3])
            blockers.append(f"{gate} 不合格{('：' + issues) if issues else ''}")
    if holds:
        blockers.append(f"今の本との照合で保留が {len(holds)} 件（koten check status）")
    if not (s.section("voicevox").get("credit") or "").strip():
        blockers.append(
            "VOICEVOX のクレジットが空"
            "（キャラクターの規約を確かめて settings の voicevox.credit に書く）"
        )
    take = ep.script.get("human_take") or {}
    if not (take.get("text") or take.get("audio_path") or take.get("skip")):
        blockers.append(
            "人の一言が決まっていません（koten episode take で文字か声を入れるか、--skip で省く）"
        )
    if ep.script.get("review_required") or ep.verification.get("G6", {}).get("review_required"):
        warnings.append(
            "G6：人の確認が要る回です（NDL・自前の訳・あとで変わった資料など）。中身をよく読んでください"
        )
    return blockers, warnings


def step_review(
    s: Settings,
    conn: sqlite3.Connection,
    episode_id: str,
    *,
    holds: list[Any],
    log: Callable[[str], None] = print,
) -> Path:
    from koten.review.page import build_review_html
    from koten.script.run import render

    ep = load_episode(conn, s.ssd_root, episode_id)
    hdd = s.require_hdd()
    media = media_dir(hdd, episode_id)
    manifest = VoiceManifest.load(media / MANIFEST) if (media / MANIFEST).exists() else None
    from koten.publish_kit.kit import ad_break_seconds, chapter_lines

    kit_dir = media / "kit"
    description = (
        (kit_dir / "description.txt").read_text("utf-8")
        if (kit_dir / "description.txt").exists()
        else ""
    )
    quotes = []
    if manifest is not None:
        for ch in manifest.chapters:
            for seg in ch.segments:
                if seg.kind == "quote":
                    c = ep.resolver.candidates.get((seg.visual or {}).get("ref")) or {}
                    from koten.script.placeholders import format_source

                    quotes.append(
                        {
                            "seg_id": seg.seg_id,
                            "display": seg.display,
                            "reading": seg.reading,
                            "source": format_source(c) if c else "",
                            "license": c.get("license") or "",
                        }
                    )
    kit = kit_input(
        ep.script,
        ep.resolver,
        fact_sources=ep.data.fact_sources,
        credits=[],
        hashtags=[],
    )
    sources = [
        {
            "kind": "古典",
            "text": f"{c.authors}『{c.title}』{c.section}　{c.base_book}　{c.url}",
            "license": c.license,
        }
        for c in kit.classics
    ]
    sources += [{"kind": "調査・データ", "text": t, "license": ""} for t in kit.data_sources]
    sources += [
        {"kind": "背景・昔の資料", "text": t, "license": ""} for t in kit.background_sources
    ]
    blockers, warnings = blockers_for(s, ep, holds=holds, media=media)
    thumbs = sorted(str(p) for p in kit_dir.glob("thumb_*.png"))
    video = media / "render" / f"{episode_id}.mp4"
    bundle = ReviewBundle(
        episode_id=episode_id,
        title=ep.resolver.resolve(ep.script.get("title") or ""),
        state=ep.state,
        script_lines=render(ep.script, ep.resolver),
        quotes=quotes,
        sources=sources,
        gates=ep.verification,
        video=str(video) if video.exists() else None,
        thumbnails=thumbs,
        description=description,
        chapters=chapter_lines(manifest) if manifest else [],
        ad_breaks=ad_break_seconds(manifest) if manifest else [],
        warnings=warnings,
        blockers=blockers,
        human_take=ep.script.get("human_take") or {},
    )
    out = exports_dir(hdd, episode_id) / "review.html"
    build_review_html(bundle, out)
    log(f"確認画面：{out}")
    return out


def _mmss(sec: float) -> str:
    m, s_ = divmod(int(round(sec)), 60)
    return f"{m}分{s_:02d}秒"
