"""M6：台本のあとの段を通しで動かす（音声 → 画面 → 組み立て → 公開用キット → 確認画面 → 承認）。

VOICEVOX は偽物、写真は ffmpeg で作った色の画像。動画は小さく（320×180・10fps）作る。
ffmpeg と Playwright（Chromium）が無ければ飛ばす。
"""

from __future__ import annotations

import json
import shutil
import subprocess

import httpx
import pytest
from typer.testing import CliRunner

from koten.cli import app
from koten.config import DEFAULT_CONFIG_DIR, load_settings
from koten.db import connect
from koten.media import produce
from koten.media.model import MANIFEST, media_dir
from koten.media.prepare import used_ids
from koten.render.ffmpeg import probe_duration
from koten.voice.voicevox import VoicevoxClient
from tests.m4_corpus import build
from tests.test_script import planned, run
from tests.test_voice_synth import ENDPOINT, FakeEngine


def _playwright_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or not _playwright_ok(),
    reason="ffmpeg か Playwright（Chromium）が無い",
)


@pytest.fixture
def env(settings_file):
    """小さな検索の索引と、台本を書ける回の材料（tests/test_script.py の env と同じ）。"""
    s = load_settings(settings_file)
    build(s.require_hdd(), s.ssd_root, DEFAULT_CONFIG_DIR)
    return s


def _settings_for(env, tmp_path, *, credit: str = ""):
    photos = tmp_path / "photos"
    photos.mkdir(parents=True)
    for i, color in enumerate(("navy", "olive", "maroon")):
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color={color}:s=640x480",
                "-frames:v",
                "1",
                str(photos / f"p{i}.jpg"),
            ],
            check=True,
        )
    text = env.path.read_text("utf-8").replace(
        "[paths]\n", f'[paths]\nphotos_dir = "{photos}"\n', 1
    )
    text = text.replace(
        '[voicevox]\nendpoint = "http://127.0.0.1:9"\n',
        f'[voicevox]\nendpoint = "{ENDPOINT}"\nspeaker_id = 3\ncredit = "{credit}"\n',
    )
    text += '\n[video]\nwidth = 320\nheight = 180\nfps = 10\nbitrate = "200k"\n'
    text += "\n[episode]\nmin_sec = 1\nmax_sec = 10000\nchars_per_min = 300\n"
    path = tmp_path / "s.toml"
    path.write_text(text, "utf-8")
    return load_settings(path)


def test_produce_from_script_to_approval(env, tmp_path, monkeypatch):
    eid = planned(env, "A", worry_id="W0001")
    r, _ = run(env, eid)
    assert r.state == "script_done"
    s = _settings_for(env, tmp_path)
    conn = connect(s.ssd_root)
    engine = FakeEngine()
    client = VoicevoxClient(ENDPOINT, 3, client=httpx.Client(transport=httpx.MockTransport(engine)))
    logs: list[str] = []

    # 音声：同じ speaker が両方に渡る・時刻が残る・状態が voiced
    m = produce.step_voice(s, conn, eid, client=client, log=logs.append)
    media = media_dir(s.require_hdd(), eid)
    assert (media / MANIFEST).exists() and m.total_sec > 0
    assert {q.url.params["speaker"] for q in engine.paths("/audio_query")} == {"3"}
    assert {q.url.params["speaker"] for q in engine.paths("/synthesis")} == {"3"}
    state = conn.execute("SELECT state FROM episodes WHERE episode_id = ?", (eid,)).fetchone()[0]
    assert state == "voiced"
    # 章タイトルの記号は差し込まれている（G1 で {{author:}} などを使う）
    assert all("{{" not in c.title for c in m.chapters)

    # 画面：カードと写真で章をすき間なく覆う・使った写真を記録する
    shots = produce.step_visuals(s, conn, eid, log=logs.append)
    assert shots.photos_used and set(shots.photos_used) <= {"p0.jpg", "p1.jpg", "p2.jpg"}
    for ch in m.chapters:
        mine = [x for x in shots.shots if x.chapter_id == ch.chapter_id]
        assert abs(sum(x.duration for x in mine) - ch.duration) < 1e-6
    assert any(x.kind == "quote_card" for x in shots.shots)
    used = {
        r[0]
        for r in conn.execute(
            "SELECT path FROM media_usage WHERE episode_id = ? AND kind = 'photo'", (eid,)
        )
    }
    assert used == set(shots.photos_used)

    # 組み立て：長さが音声と合う。G7（章の長さ）は、この小さな台本では範囲の外になる
    out, issues = produce.step_render(s, conn, eid, log=logs.append)
    assert out.exists() and abs(probe_duration(out) - m.total_sec) < 0.3
    assert issues and all("章" in x or "合計" in x for x in issues)
    v = json.loads(
        conn.execute(
            "SELECT verification_json FROM episodes WHERE episode_id = ?", (eid,)
        ).fetchone()[0]
    )
    assert v["G7"]["ok"] is False

    # 公開用キット：説明欄・字幕・チャプター・サムネイル3枚
    kit = produce.step_kit(s, conn, eid, log=logs.append)
    assert len(kit["thumbnails"]) == 3
    desc = (media / "kit" / "description.txt").read_text("utf-8")
    assert "▼この動画で読んだ古典" in desc and "{{" not in desc
    assert (media / "kit" / "subtitles.srt").read_text("utf-8").startswith("1\n")

    # 確認画面：承認できない理由（G7・クレジット・人の一言）が出る
    page, blockers = produce.step_review(s, conn, eid, holds=[], log=logs.append)
    assert page.exists()
    html = page.read_text("utf-8")
    assert any(b.startswith("G7") for b in blockers)
    assert any("クレジット" in b for b in blockers) and any("人の一言" in b for b in blockers)
    assert "VOICEVOX のクレジットが空" in html

    # CLI：承認は止まる。人の一言を省き、クレジットを書き、G7 を通したら承認できる
    st = ["--settings", str(s.path)]
    res = CliRunner().invoke(app, ["episode", "approve", "--episode", eid, *st])
    assert res.exit_code == 2 and "G7" in res.output, res.output
    res = CliRunner().invoke(app, ["episode", "take", "--episode", eid, "--skip", *st])
    assert res.exit_code == 0, res.output
    s2 = _settings_for(env, tmp_path / "with_credit", credit="VOICEVOX:テスト")
    v["G7"] = {"ok": True, "issues": []}
    with conn:
        conn.execute(
            "UPDATE episodes SET verification_json = ? WHERE episode_id = ?",
            (json.dumps(v, ensure_ascii=False), eid),
        )
    # 人の一言を省いても、状態は rendered のまま（声を入れ直したときだけ戻る）
    assert (
        conn.execute("SELECT state FROM episodes WHERE episode_id = ?", (eid,)).fetchone()[0]
        == "rendered"
    )
    # この回で使った統計・研究を人が元の資料で確かめるまで、承認できない
    used = used_ids(
        json.loads(
            conn.execute(
                "SELECT script_json FROM episodes WHERE episode_id = ?", (eid,)
            ).fetchone()[0]
        )
    )
    res = CliRunner().invoke(
        app, ["episode", "approve", "--episode", eid, "--settings", str(s2.path)]
    )
    if used["stats"] or used["findings"]:
        assert res.exit_code == 2 and "人が元の資料で確かめていない" in res.output, res.output
    with conn:
        conn.execute("UPDATE stats SET checked_by = 'オーナー', checked_at = '2026-11-24'")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'findings'").fetchone():
            conn.execute("UPDATE findings SET status = 'verified'")
    # クレジットを書いたら、説明欄を作り直すまで承認できない
    res = CliRunner().invoke(
        app, ["episode", "approve", "--episode", eid, "--settings", str(s2.path)]
    )
    assert res.exit_code == 2 and "クレジットが無い" in res.output, res.output
    assert "人が元の資料で確かめていない" not in res.output
    produce.step_kit(s2, conn, eid, log=logs.append)
    assert "音声：VOICEVOX:テスト" in (media / "kit" / "description.txt").read_text("utf-8")
    res = CliRunner().invoke(
        app, ["episode", "approve", "--episode", eid, "--note", "見た", "--settings", str(s2.path)]
    )
    assert res.exit_code == 0, res.output
    row = conn.execute(
        "SELECT state, reviewer_note FROM episodes WHERE episode_id = ?", (eid,)
    ).fetchone()
    assert (row["state"], row["reviewer_note"]) == ("reviewed", "見た")

    # 承認のあとで画面を作り直したら、動画を作り直して確かめ直すまで承認できない
    produce.step_visuals(s2, conn, eid, log=logs.append)
    state_now = conn.execute("SELECT state FROM episodes WHERE episode_id = ?", (eid,)).fetchone()[
        0
    ]
    assert state_now == "voiced"
    # 差し戻した回は、音声から作り直すまで画面も動画も作らない（古い動画を rendered にしない）
    res = CliRunner().invoke(
        app,
        ["episode", "reject", "--episode", eid, "--to", "script_done", "--note", "直す", *st],
    )
    assert res.exit_code == 0, res.output
    with pytest.raises(produce.ProduceError, match="音声のあと"):
        produce.step_render(s2, conn, eid, log=logs.append)
    with pytest.raises(produce.ProduceError, match="音声のあと"):
        produce.step_visuals(s2, conn, eid, log=logs.append)


def test_recent_photos_are_not_reused(env, tmp_path):
    """直近5回で使った写真は、次の回で選ばない。"""
    s = _settings_for(env, tmp_path)
    conn = connect(s.ssd_root)
    with conn:
        for i in range(2):
            conn.execute(
                "INSERT INTO episodes(episode_id, format, worry_ids, state) "
                "VALUES (?, 'A', '[]', 'rendered')",
                (f"E0-{i}",),
            )
        conn.execute("INSERT INTO media_usage VALUES ('E0-0', 'photo', 'p0.jpg')")
        conn.execute("INSERT INTO media_usage VALUES ('E0-1', 'photo', 'p1.jpg')")
    assert produce.recent_photos(conn, "E9") == {"p0.jpg", "p1.jpg"}
    assert produce.recent_photos(conn, "E9", n=1) in ({"p0.jpg"}, {"p1.jpg"})
    with conn:
        conn.execute("UPDATE episodes SET state = 'discarded' WHERE episode_id = 'E0-0'")
    assert produce.recent_photos(conn, "E9") == {"p1.jpg"}


def test_voice_only_check_page_has_audio(env, tmp_path):
    """画面の前に、台本と音声だけで確かめる：確認画面に章ごとと通しの音声が出る。"""
    eid = planned(env, "A", worry_id="W0001")
    r, _ = run(env, eid)
    assert r.state == "script_done"
    s = _settings_for(env, tmp_path)
    conn = connect(s.ssd_root)
    client = VoicevoxClient(
        ENDPOINT, 3, client=httpx.Client(transport=httpx.MockTransport(FakeEngine()))
    )
    m = produce.step_voice(s, conn, eid, client=client, log=lambda _: None)
    page, blockers = produce.step_review(s, conn, eid, holds=[], log=lambda _: None)
    html = page.read_text("utf-8")
    media = media_dir(s.require_hdd(), eid)
    whole = media / "voice" / "all.wav"
    # 音声と台本を並べた通しの音声（1つ）と、章ごと・通しの表（章の数 + 1）
    assert whole.exists() and html.count("<audio") == len(m.chapters) + 2
    assert 'id="sync-lines"' in html and html.count("data-t0=") > len(m.chapters)
    import wave

    with wave.open(str(whole), "rb") as w:
        assert abs(w.getnframes() / w.getframerate() - m.total_sec) < 0.01
    assert any("状態が voiced" in b for b in blockers)  # 承認はまだ（画面と動画がこれから）


def test_produce_to_checks_step_names(env, tmp_path):
    s = _settings_for(env, tmp_path)
    st = ["--settings", str(s.path)]
    res = CliRunner().invoke(app, ["produce", "--episode", "E1", "--to", "audio", *st])
    assert res.exit_code == 2 and "--to は" in res.output
    res = CliRunner().invoke(
        app, ["produce", "--episode", "E1", "--from", "render", "--to", "voice", *st]
    )
    assert res.exit_code == 2 and "前の段" in res.output


def test_stale_segments_catch_text_changed_after_voice(env, tmp_path):
    """音声のあとで台帳の文（統計の数字・研究の一文など）が変わったら、承認の前に気づく。"""
    eid = planned(env, "A", worry_id="W0001")
    r, _ = run(env, eid)
    assert r.state == "script_done"
    s = _settings_for(env, tmp_path)
    conn = connect(s.ssd_root)
    client = VoicevoxClient(
        ENDPOINT, 3, client=httpx.Client(transport=httpx.MockTransport(FakeEngine()))
    )
    produce.step_voice(s, conn, eid, client=client, log=lambda _: None)
    media = media_dir(s.require_hdd(), eid)
    ep = produce.load_episode(conn, s.ssd_root, eid)
    assert produce.stale_segments(s, ep, media) == []
    # 音声に入った文と、今の台帳で差し込んだ文が違う段落を数える
    from koten.media.model import VoiceManifest

    m = VoiceManifest.load(media / MANIFEST)
    seg = m.chapters[0].segments[0]
    seg.display = seg.display + "（前の文）"
    m.save(media / MANIFEST)
    assert produce.stale_segments(s, ep, media) == [seg.seg_id]
