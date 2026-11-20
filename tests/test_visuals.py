"""M6 の画面（koten.visuals：カード・サムネイル・写真・ショットリスト）。

ffmpeg・Playwright（Chromium）が無ければ、それを使うテストは skip する。
"""

from __future__ import annotations

import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from koten.media.model import (
    CardSpec,
    ChapterTiming,
    SegmentTiming,
    ThumbSpec,
    VoiceManifest,
)
from koten.visuals import cards as cards_mod
from koten.visuals.cards import (
    CardError,
    card_page,
    fill,
    is_yoko,
    render_cards,
    render_thumbnails,
    stat_source,
    thumb_page,
)
from koten.visuals.photos import PhotoError, list_photos, pick_photos, prepare_photo
from koten.visuals.shots import build_shots, photo_pieces

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
STAT = {
    "label": "強い不安がある人の割合",
    "value": "39.5",
    "unit": "%",
    "survey": "労働安全衛生調査（令和7年）",
    "year": "2025",
}


# --- ショットリスト -------------------------------------------------------------


def _manifest(chapters: list[tuple[str, float, list[tuple[str, float, float, str]]]]):
    """(章の ID, 章の長さ, [(段落の ID, start, end, visual の type)]) から VoiceManifest。"""
    out = VoiceManifest("E1", 3, 1.0, "0.24.1", 24000)
    offset = 0.0
    for cid, duration, segs in chapters:
        out.chapters.append(
            ChapterTiming(
                chapter_id=cid,
                role="",
                title="",
                wav=f"voice/{cid}.wav",
                offset=offset,
                duration=duration,
                segments=[
                    SegmentTiming(sid, "narration", s, e, "", "", {"type": t, "ref": None})
                    for sid, s, e, t in segs
                ],
            )
        )
        offset += duration
    return out


MANIFEST = _manifest(
    [
        (
            "ch1",
            61.2,
            [
                ("s1", 0.3, 3.0, "title_card"),
                ("s2", 3.6, 20.0, "photo"),
                ("s3", 20.6, 35.0, "author_card"),
                ("s4", 35.6, 50.0, "quote_card"),
                ("s5", 50.6, 60.0, "photo"),
            ],
        ),
        ("ch2", 12.0, [("t1", 0.5, 10.8, "photo")]),
        ("ch3", 9.0, [("c1", 0.0, 3.0, "chart"), ("c2", 3.5, 7.8, "photo")]),
    ]
)
CARDS = {"s1": "cards/s1.png", "s4": "cards/s4.png", "c1": "cards/c1.png"}
PHOTOS = ["photos/p001.jpg", "photos/p002.jpg", "photos/p003.jpg"]


def _by_chapter(shots):
    out: dict[str, list] = {}
    for s in shots.shots:
        out.setdefault(s.chapter_id, []).append(s)
    return out


def test_shots_cover_each_chapter_without_gaps_or_overlaps():
    shots = build_shots(MANIFEST, CARDS, PHOTOS, width=1920, height=1080, fps=30)
    assert (shots.episode_id, shots.width, shots.height, shots.fps) == ("E1", 1920, 1080, 30)
    by_ch = _by_chapter(shots)
    assert list(by_ch) == ["ch1", "ch2", "ch3"]
    for ch in MANIFEST.chapters:
        got = by_ch[ch.chapter_id]
        assert got[0].start == 0.0
        for a, b in zip(got, got[1:], strict=False):
            assert a.start + a.duration == pytest.approx(b.start, abs=1e-9)
        assert got[-1].start + got[-1].duration == pytest.approx(ch.duration, abs=1e-9)
        assert sum(s.duration for s in got) == pytest.approx(ch.duration, abs=1e-9)
        assert all(s.duration > 0 for s in got)


def test_card_segments_get_one_still_shot_each():
    shots = build_shots(MANIFEST, CARDS, PHOTOS, width=1920, height=1080, fps=30)
    cards = {s.seg_ids[0]: s for s in shots.shots if s.motion == "still"}
    assert set(cards) == {"s1", "s4", "c1"}
    # 最初の段落は 0 から、次の段落の start まで
    assert (cards["s1"].start, cards["s1"].duration) == (0.0, pytest.approx(3.6))
    assert (cards["s1"].image, cards["s1"].kind) == ("cards/s1.png", "title_card")
    assert cards["s4"].start == 35.6
    assert cards["s4"].duration == pytest.approx(50.6 - 35.6)
    assert cards["s4"].kind == "quote_card"
    assert (cards["c1"].kind, cards["c1"].duration) == ("chart", pytest.approx(3.5))


def test_photo_runs_switch_every_5_to_10_seconds_in_order():
    shots = build_shots(MANIFEST, CARDS, PHOTOS, width=1920, height=1080, fps=30)
    ch1 = [s for s in _by_chapter(shots)["ch1"] if s.kind == "photo"]
    # s2・s3（author_card も今は写真）は1つの区間 3.6〜35.6 = 32 秒 → 6.4 秒ずつ5枚
    run = [s for s in ch1 if s.start < 35.6]
    assert len(run) == 5
    assert all(s.duration == pytest.approx(6.4) for s in run)
    assert all(s.motion == "zoom" for s in run)
    # 章の終わりの間は最後のショットに入る：50.6〜61.2 = 10.6 秒 → 5.3 秒ずつ2枚
    tail = [s for s in ch1 if s.start >= 50.6]
    assert [round(s.duration, 6) for s in tail] == [5.3, 5.3]
    # 写真は回の中で通しに、順に当てる（足りなければ始めに戻る）
    photos = [s.image for s in shots.shots if s.kind == "photo"]
    assert photos == [PHOTOS[i % 3] for i in range(len(photos))]
    # ショットの段落は、そのショットと重なる段落だけ
    assert run[0].seg_ids == ["s2"]
    assert run[2].seg_ids == ["s2", "s3"]
    assert run[-1].seg_ids == ["s3"]


def test_short_photo_run_and_chapter_without_segments():
    m = _manifest([("ch1", 3.2, [("a", 0.0, 2.9, "photo")]), ("ch2", 15.0, [])])
    shots = build_shots(m, {}, ["photos/p001.jpg"], width=1920, height=1080, fps=30)
    by_ch = _by_chapter(shots)
    assert [(s.start, s.duration) for s in by_ch["ch1"]] == [(0.0, 3.2)]
    assert [round(s.duration, 6) for s in by_ch["ch2"]] == [7.5, 7.5]
    assert by_ch["ch2"][0].seg_ids == []


def test_zero_length_card_does_not_split_a_photo_run():
    # b は長さ 0（次の段落と同じ時刻に始まる）。カードは映せないので、a と c の写真を1つの区間に
    # まとめる（分けると a の 3 秒だけの短いショットができる）
    m = _manifest(
        [
            (
                "ch1",
                14.0,
                [
                    ("a", 0.0, 2.5, "photo"),
                    ("b", 3.0, 3.0, "quote_card"),
                    ("c", 3.0, 13.5, "photo"),
                ],
            )
        ]
    )
    shots = build_shots(m, {"b": "cards/b.png"}, PHOTOS, width=1920, height=1080, fps=30)
    assert [(s.kind, s.start, s.duration) for s in shots.shots] == [
        ("photo", 0.0, 7.0),
        ("photo", 7.0, 7.0),
    ]
    assert [s.seg_ids for s in shots.shots] == [["a", "c"], ["c"]]


def test_photo_pieces_stay_within_bounds():
    assert photo_pieces(3.0, 5.0, 10.0) == 1
    assert photo_pieces(10.0, 5.0, 10.0) == 1
    assert photo_pieces(10.5, 5.0, 10.0) == 2
    length = 5.0
    while length < 300:
        k = photo_pieces(length, 5.0, 10.0)
        assert 5.0 - 1e-9 <= length / k <= 10.0 + 1e-9, length
        length += 0.37


def test_build_shots_needs_photos():
    with pytest.raises(ValueError, match="写真が0枚"):
        build_shots(MANIFEST, CARDS, [], width=1920, height=1080, fps=30)


# --- 写真 ----------------------------------------------------------------------


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def test_list_photos_skips_hidden_and_other_files(tmp_path):
    for name in [
        "b.JPG",
        "a.jpg",
        "sub/c.heic",
        "e.webp",
        "f.png",
        ".hidden.jpg",
        ".dir/d.jpg",
        "notes.txt",
    ]:
        _touch(tmp_path / name)
    got = [p.relative_to(tmp_path).as_posix() for p in list_photos(tmp_path)]
    assert got == ["a.jpg", "b.JPG", "e.webp", "f.png", "sub/c.heic"]


def test_pick_photos_excludes_recent_and_is_stable(tmp_path):
    photos = [_touch(tmp_path / f"p{i:02d}.jpg") for i in range(20)]
    exclude = {"p00.jpg", "p01.jpg"}
    a = pick_photos(photos, exclude, 5, "E20261120-01", tmp_path)
    assert a == pick_photos(photos, exclude, 5, "E20261120-01", tmp_path)
    assert len(a) == 5 and len(set(a)) == 5
    assert not {p.name for p in a} & exclude
    assert a != pick_photos(photos, exclude, 5, "E20261120-02", tmp_path)


def test_pick_photos_reuses_within_episode_when_short(tmp_path):
    photos = [_touch(tmp_path / f"p{i}.jpg") for i in range(3)]
    got = pick_photos(photos, {"p0.jpg"}, 5, "E1", tmp_path)
    assert len(got) == 5
    assert {p.name for p in got} == {"p1.jpg", "p2.jpg"}
    assert got[0] != got[1]
    assert pick_photos(photos, {"p0.jpg", "p1.jpg", "p2.jpg"}, 5, "E1", tmp_path) == []
    assert pick_photos(photos, set(), 0, "E1", tmp_path) == []


def _ffmpeg_image(path: Path, size: str) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}"]
        + ["-frames:v", "1", "-update", "1", str(path)],
        check=True,
    )
    return path


def _probe_size(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0"]
        + [str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    w, h = out.strip().split(",")
    return int(w), int(h)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg がありません")
@pytest.mark.parametrize("size", ["400x300", "300x400", "320x90"])
def test_prepare_photo_crops_to_cover(tmp_path, size):
    src = _ffmpeg_image(tmp_path / "src.png", size)
    dst = tmp_path / "out" / "p001.jpg"
    prepare_photo(src, dst, 192, 108)
    assert _probe_size(dst) == (192, 108)
    assert not list(dst.parent.glob("*.part*"))


def test_prepare_photo_needs_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr("koten.visuals.photos.shutil.which", lambda name: None)
    with pytest.raises(PhotoError, match="ffmpeg"):
        prepare_photo(tmp_path / "a.jpg", tmp_path / "b.jpg", 192, 108)


def test_prepare_photo_heic_needs_sips(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "koten.visuals.photos.shutil.which",
        lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
    )
    with pytest.raises(PhotoError, match="sips"):
        prepare_photo(_touch(tmp_path / "a.HEIC"), tmp_path / "b.jpg", 192, 108)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg がありません")
def test_prepare_photo_reports_broken_file(tmp_path):
    with pytest.raises(PhotoError, match="切り抜けません"):
        prepare_photo(_touch(tmp_path / "broken.jpg"), tmp_path / "b.jpg", 192, 108)


# --- カード（HTML を作るところ。ブラウザは要らない） -------------------------------


def test_fill_escapes_every_value():
    out = fill('<p>$a</p><img src="${b}">', {"a": "<script>&", "b": '"x'})
    assert out == '<p>&lt;script&gt;&amp;</p><img src="&quot;x">'
    with pytest.raises(CardError):
        fill("$missing", {})


def test_quote_card_page_is_vertical_and_escaped():
    spec = CardSpec(
        "ch2-s1",
        "quote_card",
        text="  人の<心>は、\n移ろふものなり。  ",
        source="兼好法師『徒然草』「第七段」",
        label="試訳",
        license_note="著作権の保護期間満了",
    )
    page, sizes = card_page(spec, (TEMPLATES / "quote_card.html").read_text("utf-8"))
    assert '<body class="tate">' in page
    assert "人の&lt;心&gt;は、\n移ろふものなり。</div>" in page
    assert "兼好法師『徒然草』「第七段」" in page and "試訳" in page
    assert "著作権の保護期間満了" in page
    assert list(sizes) == sorted(sizes, reverse=True)
    assert "writing-mode: vertical-rl" in page


def test_quote_card_latin_text_is_horizontal():
    assert is_yoko("Begin the morning by saying to thyself.")
    assert not is_yoko("吾輩は猫である。Name は無い。")
    spec = CardSpec("s", "quote_card", text="Begin the morning.", source="x")
    page, _ = card_page(spec, (TEMPLATES / "quote_card.html").read_text("utf-8"))
    assert '<body class="yoko">' in page


def test_chart_card_uses_stat_value_as_is():
    spec = CardSpec("s", "chart", stat={**STAT, "value": "55614", "unit": "件"})
    page, _ = card_page(spec, (TEMPLATES / "chart_card.html").read_text("utf-8"))
    assert '<span class="value">55614</span><span class="unit">件</span>' in page
    assert "出典：労働安全衛生調査（令和7年）（2025年）" in page
    assert stat_source({"survey": "国民生活基礎調査", "year": "2022年"}) == (
        "出典：国民生活基礎調査（2022年）"
    )
    assert stat_source({"survey": "国民生活基礎調査"}) == "出典：国民生活基礎調査"
    for bad in ({**STAT, "survey": ""}, {**STAT, "value": None}):
        with pytest.raises(CardError, match="数字か出典"):
            card_page(CardSpec("s", "chart", stat=bad), "")


def test_card_page_rejects_unknown_type_and_empty_quote():
    with pytest.raises(CardError, match="photo"):
        card_page(CardSpec("s", "photo"), "")
    with pytest.raises(CardError, match="一節が空"):
        card_page(CardSpec("s", "quote_card", text="  "), "")


def test_thumb_page_embeds_photo_as_data_uri(tmp_path):
    photo = tmp_path / "p.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xd9")
    tpl = (TEMPLATES / "thumb_a.html").read_text("utf-8")
    page, ok = thumb_page(ThumbSpec("同期の<昇進>", ["夏目漱石", "兼好法師"], str(photo)), tpl)
    assert ok and 'src="data:image/jpeg;base64,/9j/2Q=="' in page
    assert "同期の&lt;昇進&gt;" in page and "夏目漱石・兼好法師" in page
    assert '<body class="has-photo">' in page
    page, ok = thumb_page(ThumbSpec("言葉", [], str(tmp_path / "none.jpg")), tpl)
    assert not ok and '<body class="no-photo">' in page and 'src=""' in page
    page, ok = thumb_page(ThumbSpec("言葉", []), tpl)
    assert ok and '<body class="no-photo">' in page


def test_templates_load_nothing_from_outside():
    names = {*cards_mod.CARD_TEMPLATES.values(), *cards_mod.THUMB_TEMPLATES.values()}
    assert {p.name for p in TEMPLATES.glob("*.html")} == names
    for name in names:
        text = (TEMPLATES / name).read_text("utf-8")
        assert not re.search(r"https?:|//[a-z]|@import|@font-face|<script|<link|url\(", text), name
        # 画像は写真の差し込み口だけ（data: で渡す）
        assert re.findall(r"src=\"([^\"]*)\"", text) in ([], ["${photo}"]), name
        # どのフォントの並びにも、総称（serif など）の前に日本語のフォントを置く
        # （明朝の無い機械で serif が中国語のフォントになると、縦書きで字が重なる）
        for stack in re.findall(r"font-family:([^;]+);", text):
            fonts = [f.strip().strip('"') for f in stack.split(",")]
            assert fonts[-1] in ("serif", "sans-serif"), (name, stack)
            assert fonts[-2] in ("IPAGothic", "IPAMincho"), (name, stack)


# --- カード（Playwright で撮る） --------------------------------------------------


@pytest.fixture(scope="module")
def chromium():
    try:
        with cards_mod._browser():
            pass
    except CardError as e:
        pytest.skip(f"Playwright（Chromium）が使えません：{e}")


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_render_cards_writes_pngs_and_warns_when_too_long(tmp_path, chromium):
    passage = "吾輩は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。" * 6
    specs = [
        CardSpec("ch1-s1", "title_card", title="第一章　嫉妬という感情"),
        CardSpec("ch2-s3", "quote_card", text=passage, source="夏目漱石『吾輩は猫である』"),
        CardSpec("ch2-s4", "chart", stat=STAT),
        CardSpec("ch3-s1", "quote_card", text="長い一節。" * 800, source="出典"),
    ]
    warnings: list[str] = []
    logs: list[str] = []
    got = render_cards(specs, tmp_path, templates_dir=TEMPLATES, warnings=warnings, log=logs.append)
    assert got == {s.seg_id: f"cards/{s.seg_id}.png" for s in specs}
    for rel in got.values():
        assert _png_size(tmp_path / rel) == (1920, 1080)
    # 収まらない一節だけが警告になる（切らずに撮る）
    assert len(warnings) == 1 and warnings[0].startswith("ch3-s1：") and logs == warnings


def test_render_cards_other_size_and_thumbnails(tmp_path, chromium):
    got = render_cards(
        [CardSpec("t", "title_card", title="")],
        tmp_path,
        width=960,
        height=540,
        templates_dir=TEMPLATES,
        log=lambda m: None,
    )
    assert _png_size(tmp_path / got["t"]) == (960, 540)
    photo = tmp_path / "photo.png"
    photo.write_bytes((tmp_path / got["t"]).read_bytes())
    specs = [ThumbSpec("同期の昇進を喜べない", ["夏目漱石"], str(photo), t) for t in "abc"]
    thumbs = render_thumbnails(specs, tmp_path, templates_dir=TEMPLATES, log=lambda m: None)
    assert thumbs == ["kit/thumb_1.png", "kit/thumb_2.png", "kit/thumb_3.png"]
    for rel in thumbs:
        assert _png_size(tmp_path / rel) == (1280, 720)


def test_render_without_playwright_explains_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(CardError, match="playwright install chromium"):
        render_cards([CardSpec("t", "title_card", title="x")], tmp_path, templates_dir=TEMPLATES)
    with pytest.raises(CardError, match="テンプレート「z」"):
        render_thumbnails([ThumbSpec("x", [], template="z")], tmp_path, templates_dir=TEMPLATES)
    assert render_cards([], tmp_path, templates_dir=TEMPLATES) == {}
