"""M6 の公開用キット（koten.publish_kit.kit）：チャプター・説明欄・字幕・広告位置の候補。"""

from __future__ import annotations

import re

import pytest

from koten.media.model import (
    ChapterTiming,
    ClassicSource,
    KitInput,
    SegmentTiming,
    Sentence,
    VoiceManifest,
)
from koten.publish_kit.kit import (
    DESCRIPTION_MAX,
    DescriptionError,
    ad_break_seconds,
    build_description,
    build_srt,
    chapter_lines,
    write_kit,
)

EP = "E20261120-01"


def _manifest(
    chapters: list[tuple[str, float]], sentences: dict[int, list[Sentence]] | None = None
):
    """章の（題・長さ）の並びから VoiceManifest を作る。offset は前の章の長さの合計。"""
    sentences = sentences or {}
    out, offset = [], 0.0
    for i, (title, duration) in enumerate(chapters):
        sents = sentences.get(i, [])
        segs = (
            [
                SegmentTiming(
                    seg_id=f"ch{i}-s0",
                    kind="narration",
                    start=sents[0].start,
                    end=sents[-1].end,
                    display="".join(s.display for s in sents),
                    reading="".join(s.reading for s in sents),
                    visual={"type": "photo"},
                    sentences=sents,
                )
            ]
            if sents
            else []
        )
        out.append(
            ChapterTiming(
                chapter_id=f"ch{i}",
                role="opening" if i == 0 else "classic",
                title=title,
                wav=f"voice/ch{i}.wav",
                offset=offset,
                duration=duration,
                segments=segs,
            )
        )
        offset += duration
    return VoiceManifest(
        episode_id=EP,
        speaker_id=13,
        speed_scale=1.0,
        engine_version="0.0.0",
        sample_rate=24000,
        chapters=out,
    )


def _kit(**kw) -> KitInput:
    base = dict(
        episode_id=EP,
        title="同期の昇進を素直に喜べないとき",
        hook="同期が先に昇進した。ひとりになると胸がざわつく。",
        promise="この動画では、三木清『人生論ノート』などの古典の一節を手がかりに考えます。",
        classics=[
            ClassicSource(
                title="人生論ノート",
                authors="三木清",
                section="嫉妬について",
                base_book="『人生論ノート』新潮文庫",
                url="https://www.aozora.gr.jp/cards/000218/card46845.html",
                license="PD_JP",
            )
        ],
        notices=["古典の読み方はひとつではありません。この動画の読み方は、そのひとつです。"],
        question="あなたが比べてしまうのは、どんなときですか。",
        hashtags=["嫉妬", "#社会人", "#古典"],
    )
    base.update(kw)
    return KitInput(**base)


CHAPTERS = [("はじめに", 40.0), ("嫉妬の正体", 200.0), ("三木清の見方", 250.0), ("最後に", 90.0)]


# --- チャプター -----------------------------------------------------------------


def test_chapter_lines_start_at_zero_and_ascend():
    lines = chapter_lines(_manifest(CHAPTERS))
    assert lines == ["00:00 はじめに", "00:40 嫉妬の正体", "04:00 三木清の見方", "08:10 最後に"]


def test_chapter_lines_floor_seconds():
    lines = chapter_lines(_manifest([("a", 40.9), ("b", 30.0), ("c", 30.0)]))
    assert lines == ["00:00 a", "00:40 b", "01:10 c"]


def test_short_chapter_is_merged_into_previous():
    m = _manifest([("a", 40.0), ("短い", 5.0), ("b", 60.0), ("c", 60.0), ("終わり", 8.0)])
    assert chapter_lines(m) == ["00:00 a", "00:45 b", "01:45 c"]


def test_short_first_chapter_takes_the_next():
    m = _manifest([("はじめに", 4.0), ("b", 60.0), ("c", 60.0), ("d", 60.0)])
    # 最初の章は 00:00 のまま。次の章（b）をそこにまとめる
    assert chapter_lines(m) == ["00:00 はじめに", "01:04 c", "02:04 d"]


def test_fewer_than_three_chapters_is_empty():
    assert chapter_lines(_manifest([("a", 60.0), ("b", 60.0)])) == []
    assert chapter_lines(_manifest([("a", 60.0), ("短い", 5.0), ("b", 60.0)])) == []
    assert chapter_lines(_manifest([])) == []


def test_chapter_lines_over_an_hour():
    m = _manifest([("a", 1800.0), ("b", 1830.0), ("c", 60.0)])
    assert chapter_lines(m) == ["00:00 a", "30:00 b", "1:00:30 c"]


def test_chapter_title_newline_is_one_line():
    m = _manifest([("a", 60.0), ("二行の\n題", 60.0), ("c", 60.0)])
    assert chapter_lines(m)[1] == "01:00 二行の 題"


# --- 説明欄 ---------------------------------------------------------------------


def test_description_order_and_first_two_lines():
    kit = _kit(
        data_sources=["厚生労働省「労働安全衛生調査」（2025年）　https://www.mhlw.go.jp/x.html"],
        background_sources=["『三木清全集』第1巻　https://dl.ndl.go.jp/pid/1"],
        books=[{"title": "嫉妬の本", "url": "https://www.amazon.co.jp/dp/0000000000"}],
        amazon_notice="Amazonのアソシエイトとして、古典チャンネルは適格販売により収入を得ています。",
        credits=["音声：VOICEVOX:青山龍星"],
    )
    text = build_description(kit, _manifest(CHAPTERS))
    lines = text.split("\n")
    assert lines[0] == kit.hook
    assert lines[1] == kit.promise
    order = [
        "▼目次",
        "▼この動画で読んだ古典",
        "▼この動画で使った調査・データ",
        "▼背景と昔の資料の出典",
        "（広告）",
        kit.amazon_notice,
        "▼参考にした本",
        "・嫉妬の本　https://www.amazon.co.jp/dp/0000000000",
        "音声：VOICEVOX:青山龍星",
        kit.notices[0],
        kit.question,
        "#嫉妬 #社会人 #古典",
    ]
    idx = [lines.index(x) for x in order]
    assert idx == sorted(idx)
    # 「（広告）」と文言は、本の一覧のすぐ上（見出しのすぐ上）
    i = lines.index("（広告）")
    assert lines[i : i + 4] == [
        "（広告）",
        kit.amazon_notice,
        "▼参考にした本",
        "・嫉妬の本　https://www.amazon.co.jp/dp/0000000000",
    ]
    assert "00:40 嫉妬の正体" in lines
    assert lines[-1] == "#嫉妬 #社会人 #古典"


def test_description_skips_empty_sections():
    text = build_description(_kit(hashtags=[]), _manifest([("a", 60.0), ("b", 60.0)]))
    for heading in ("▼目次", "▼この動画で使った調査・データ", "▼背景と昔の資料の出典"):
        assert heading not in text
    assert "（広告）" not in text and "▼参考にした本" not in text
    assert "#" not in text
    assert "▼この動画で読んだ古典" in text
    assert "\n\n\n" not in text


def test_description_uses_only_given_text():
    kit = _kit()
    text = build_description(kit, _manifest(CHAPTERS))
    fixed = {"▼目次", "▼この動画で読んだ古典"}
    given = {kit.hook, kit.promise, *kit.notices, kit.question, "#嫉妬 #社会人 #古典"}
    chapters = set(chapter_lines(_manifest(CHAPTERS)))
    for line in text.split("\n"):
        if not line or line in fixed | given | chapters:
            continue
        assert line.startswith("・三木清『人生論ノート』")


def test_classic_line_has_source_base_url_license():
    c = ClassicSource(
        title="自省録",
        authors="マルクス・アウレリウス",
        translator="神谷美恵子",
        section="第二巻",
        base_book="『自省録』岩波文庫",
        url="https://example.org/card",
        license="PD_JP",
        label="試訳",
    )
    text = build_description(_kit(classics=[c, c]), _manifest(CHAPTERS))
    line = (
        "・マルクス・アウレリウス『自省録』（神谷美恵子訳）「第二巻」（試訳）"
        "　底本：『自省録』岩波文庫　https://example.org/card　著作権の保護期間満了"
    )
    assert text.count(line) == 1  # 同じ行は1つだけ


def test_cc_by_shows_modification():
    plain = ClassicSource(title="t", authors="a", url="https://x.org", license="CC_BY")
    translated = ClassicSource(title="u", authors="b", license="CC_BY", label="試訳")
    text = build_description(_kit(classics=[plain, translated]), _manifest(CHAPTERS))
    assert "・a『t』　https://x.org　CC BY（改変なし）" in text
    assert "・b『u』（試訳）　CC BY（訳して読み上げ。改変あり）" in text


def test_hashtags_at_most_three_with_hash():
    text = build_description(
        _kit(hashtags=["嫉妬", "#嫉妬", "働く 人", "#古典", "#四つめ"]), _manifest(CHAPTERS)
    )
    assert text.split("\n")[-1] == "#嫉妬 #働く人 #古典"


def test_description_requires_classics():
    with pytest.raises(DescriptionError, match="古典"):
        build_description(_kit(classics=[]), _manifest(CHAPTERS))


@pytest.mark.parametrize("license", ["", "NEEDS_REVIEW", "CC_BY_SA", "UNKNOWN"])
def test_classic_license_must_be_usable(license):
    # 権利の判定（R1）を通らない一節や、ライセンスの無い出典（R7）は説明欄に出さない
    c = ClassicSource(title="t", authors="a", url="https://x.org", license=license)
    with pytest.raises(DescriptionError, match="ライセンス"):
        build_description(_kit(classics=[c]), _manifest(CHAPTERS))


def test_description_episode_must_match():
    with pytest.raises(DescriptionError, match="回が違います"):
        build_description(_kit(episode_id="E20261120-02"), _manifest(CHAPTERS))


def test_amazon_link_needs_notice():
    kit = _kit(books=[{"title": "本", "url": "https://amzn.to/abc"}], amazon_notice="")
    with pytest.raises(DescriptionError, match="アソシエイト"):
        build_description(kit, _manifest(CHAPTERS))


def test_no_survey_numbers_in_data_lines():
    kit = _kit(data_sources=["労働安全衛生調査（2025年）39.5%　https://x.go.jp/a%20b"])
    with pytest.raises(DescriptionError, match="数値"):
        build_description(kit, _manifest(CHAPTERS))
    # URL の中の % は数えない
    ok = _kit(data_sources=["労働安全衛生調査（2025年）　https://x.go.jp/a%E3%81%82"])
    assert "a%E3%81%82" in build_description(ok, _manifest(CHAPTERS))


@pytest.mark.parametrize(
    "field",
    [
        {"hook": "仕事の量を挙げた人の割合は39.5%（労働安全衛生調査、2025年）。"},
        {"hook": "相談件数は55614件（個別労働紛争解決制度の施行状況、2025年）。"},
        {"question": "あなたは7割の人と同じですか。"},
        {"background_sources": ["半数が答えた調査　https://x.go.jp/a"]},
    ],
)
def test_survey_numbers_anywhere_are_rejected(field):
    # 統計の記号が差し込まれたつかみ・問いかけ・出典の行にも、調査の数値を出さない（D-036）
    with pytest.raises(DescriptionError, match="数値"):
        build_description(_kit(**field), _manifest(CHAPTERS))


def test_survey_number_in_chapter_title_is_rejected():
    m = _manifest([("はじめに", 60.0), ("39.5%の人が悩む", 60.0), ("最後に", 60.0)])
    with pytest.raises(DescriptionError, match="目次"):
        build_description(_kit(), m)


def test_years_and_book_titles_with_numbers_are_kept():
    # 年・回・書誌の数字は止めない（本の題の「9割」は調査の数値ではない）
    kit = _kit(
        data_sources=["日本財団18歳意識調査 第78回（2026年）　https://x.org/1"],
        books=[{"title": "人は話し方が9割", "url": "https://www.amazon.co.jp/dp/1"}],
        amazon_notice="Amazonのアソシエイトとして、古典チャンネルは適格販売により収入を得ています。",
    )
    text = build_description(kit, _manifest(CHAPTERS))
    assert "・人は話し方が9割　https://www.amazon.co.jp/dp/1" in text
    assert "第78回（2026年）" in text


def test_unresolved_placeholder_is_rejected(tmp_path):
    # 章の題を差し込まずに渡すと、目次に記号のまま出てしまう
    m = _manifest([("はじめに", 60.0), ("{{author:C1}}の見方", 60.0), ("最後に", 60.0)])
    with pytest.raises(DescriptionError, match="差し込んでいない記号"):
        build_description(_kit(), m)
    with pytest.raises(DescriptionError):
        write_kit(_kit(), m, tmp_path)
    assert not (tmp_path / "kit").exists()


def test_angle_brackets_are_rejected():
    with pytest.raises(DescriptionError, match="使えない字"):
        build_description(_kit(question="<b>どう思いますか</b>"), _manifest(CHAPTERS))


def test_description_limit():
    base = build_description(_kit(notices=[]), _manifest(CHAPTERS))
    room = DESCRIPTION_MAX - len(base) - 2  # 注意書きの節の前の空行（\n\n）
    just = build_description(_kit(notices=["あ" * room]), _manifest(CHAPTERS))
    assert len(just) == DESCRIPTION_MAX
    with pytest.raises(DescriptionError, match="上限"):
        build_description(_kit(notices=["あ" * (room + 1)]), _manifest(CHAPTERS))


# --- 字幕 -----------------------------------------------------------------------


def _cues(srt: str) -> list[tuple[str, str, list[str]]]:
    out = []
    for block in srt.strip().split("\n\n"):
        num, times, *lines = block.split("\n")
        start, end = times.split(" --> ")
        out.append((start, end, lines))
    return out


def test_srt_adds_chapter_offset_and_numbers():
    sents = {
        0: [Sentence("はじめの文。", "はじめの文。", 0.5, 2.0)],
        1: [
            Sentence("次の章の文。", "次の章の文。", 1.25, 3.0),
            Sentence("もう一つ。", "もう一つ。", 3.25, 4.5),
        ],
    }
    srt = build_srt(_manifest([("a", 61.0), ("b", 30.0)], sents))
    assert srt.startswith("1\n00:00:00,500 --> 00:00:02,000\nはじめの文。\n\n2\n")
    cues = _cues(srt)
    assert [c[:2] for c in cues] == [
        ("00:00:00,500", "00:00:02,000"),
        ("00:01:02,250", "00:01:04,000"),
        ("00:01:04,250", "00:01:05,500"),
    ]
    assert re.findall(r"^\d+$", srt, re.M) == ["1", "2", "3"]
    assert srt.endswith("\n") and not srt.endswith("\n\n")


def _one_cue_lines(text: str) -> list[str]:
    cues = _cues(build_srt(_manifest([("a", 30.0)], {0: [Sentence(text, text, 1.0, 9.0)]})))
    assert len(cues) == 1
    lines = cues[0][2]
    assert "".join(lines) == text
    assert all(len(x) <= 28 for x in lines)
    return lines


def test_srt_long_sentence_two_lines_at_comma():
    text = "誰かの成功を素直に喜べないとき、私たちは物差しを借りているのかもしれません。"
    assert 28 < len(text) <= 56
    lines = _one_cue_lines(text)
    assert lines == [
        "誰かの成功を素直に喜べないとき、",
        "私たちは物差しを借りているのかもしれません。",
    ]


def test_srt_two_lines_without_breaking_a_word():
    # 読点で切ると後ろが28字を超えるので、ひらがなから漢字へ変わる所で切る
    text = "誰かの成功を喜べないとき、私たちは自分の物差しを他人から借りているのかもしれません。"
    lines = _one_cue_lines(text)
    assert lines == [
        "誰かの成功を喜べないとき、私たちは自分の",
        "物差しを他人から借りているのかもしれません。",
    ]


def test_srt_very_long_sentence_splits_time_by_chars():
    text = "あ" * 70 + "。"
    srt = build_srt(_manifest([("a", 30.0)], {0: [Sentence(text, text, 0.0, 7.1)]}))
    cues = _cues(srt)
    assert len(cues) == 2
    assert "".join(line for c in cues for line in c[2]) == text
    assert all(len(line) <= 28 for c in cues for line in c[2])
    assert all(len(c[2]) <= 2 for c in cues)
    # 時間は字数で割る。つながっていて、最後は文の終わり
    assert cues[0][0] == "00:00:00,000"
    assert cues[0][1] == cues[1][0]
    assert cues[1][1] == "00:00:07,100"
    first_chars = sum(len(x) for x in cues[0][2])
    ms = int(cues[0][1].split(",")[1]) + int(cues[0][1].split(":")[2].split(",")[0]) * 1000
    assert ms == round(7100 * first_chars / len(text))


def test_srt_no_line_starts_with_punctuation_or_small_kana():
    for text in ("あ" * 27 + "。" + "い" * 27, "あ" * 20 + "っ" + "あ" * 19, "あ" * 20 + "ー" * 20):
        srt = build_srt(_manifest([("a", 30.0)], {0: [Sentence(text, text, 0.0, 5.0)]}))
        lines = [line for _, _, c in _cues(srt) for line in c]
        assert "".join(lines) == text
        assert all(len(x) <= 28 for x in lines)
        assert not any(line[0] in "。っー" for line in lines[1:]), lines


def test_srt_line_does_not_end_with_opening_bracket():
    # 句読点もひらがなの切れ目も無いので真ん中で切る。そこが「のすぐあとなら、「の前で切る
    text = "ア" * 22 + "「" + "イ" * 21 + "」"
    lines = _one_cue_lines(text)
    assert lines == ["ア" * 22, "「" + "イ" * 21 + "」"]


def test_srt_empty_manifest():
    assert build_srt(_manifest([("a", 10.0)])) == ""


# --- 広告位置の候補 ---------------------------------------------------------------


def test_ad_breaks_skip_the_first_boundary():
    assert ad_break_seconds(_manifest(CHAPTERS)) == [240.0, 490.0]
    assert ad_break_seconds(_manifest([("a", 30.0), ("b", 30.0)])) == []


# --- 書き出し -------------------------------------------------------------------


def test_write_kit_writes_three_files(tmp_path):
    sents = {0: [Sentence("はじめの文。", "はじめの文。", 0.5, 2.0)]}
    manifest = _manifest(CHAPTERS, sents)
    paths = write_kit(_kit(), manifest, tmp_path)
    assert paths == {
        "description": "kit/description.txt",
        "subtitles": "kit/subtitles.srt",
        "chapters": "kit/chapters.txt",
    }
    assert (tmp_path / paths["description"]).read_text("utf-8") == build_description(
        _kit(), manifest
    )
    assert (tmp_path / paths["subtitles"]).read_text("utf-8") == build_srt(manifest)
    assert (tmp_path / paths["chapters"]).read_text("utf-8").splitlines() == chapter_lines(manifest)
    assert not list((tmp_path / "kit").glob("*.part"))


def test_write_kit_writes_nothing_when_description_fails(tmp_path):
    with pytest.raises(DescriptionError):
        write_kit(_kit(classics=[]), _manifest(CHAPTERS), tmp_path)
    assert not (tmp_path / "kit").exists()
