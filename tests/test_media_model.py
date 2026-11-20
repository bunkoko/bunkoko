"""M6 の受け渡しの形（koten.media.model）。"""

from koten.media.model import (
    ChapterTiming,
    SegmentTiming,
    Sentence,
    Shot,
    ShotList,
    VoiceManifest,
    pair_sentences,
    split_sentences,
)


def test_split_sentences_keeps_quotes_whole():
    text = "彼はこう書きます。「至当かも知れない。が、不快なんだ。」と。\n次の段。本当に？"
    assert split_sentences(text) == [
        "彼はこう書きます。",
        "「至当かも知れない。が、不快なんだ。」と。",
        "次の段。",
        "本当に？",
    ]
    assert split_sentences("") == [] and split_sentences("句点なし") == ["句点なし"]


def test_pair_sentences_falls_back_to_whole_paragraph():
    assert pair_sentences("さうして。氣が。", "そうして。気が。") == [
        ("さうして。", "そうして。"),
        ("氣が。", "気が。"),
    ]
    # 数がそろわなければ段落ごと
    assert pair_sentences("一。二。", "一二。") == [("一。二。", "一二。")]
    assert pair_sentences("画面だけ", "") == []


def test_manifest_and_shots_roundtrip(tmp_path):
    m = VoiceManifest(
        "E1",
        3,
        1.0,
        "0.24.1",
        24000,
        [
            ChapterTiming(
                "ch0",
                "opening",
                "はじめに",
                "voice/ch0.wav",
                0.0,
                12.5,
                [
                    SegmentTiming(
                        "ch0-s1",
                        "narration",
                        0.1,
                        12.0,
                        "文。",
                        "文。",
                        {"type": "photo"},
                        [Sentence("文。", "文。", 0.1, 12.0)],
                    )
                ],
            )
        ],
    )
    m.save(tmp_path / "voice" / "manifest.json")
    back = VoiceManifest.load(tmp_path / "voice" / "manifest.json")
    assert back == m and back.total_sec == 12.5
    s = ShotList(
        "E1", 1920, 1080, 30, [Shot("ch0", 0.0, 12.5, "photos/p1.jpg", "zoom", "photo", ["ch0-s1"])]
    )
    s.save(tmp_path / "visuals" / "shots.json")
    assert ShotList.load(tmp_path / "visuals" / "shots.json") == s
