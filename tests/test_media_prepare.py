"""M6：台本と DB から説明欄の材料を作る（koten.media.prepare.kit_input・chapter_ranges）。"""

from __future__ import annotations

from koten.media.prepare import chapter_ranges, kit_input
from koten.script.placeholders import Resolver

CAND = {
    "candidate_id": "aozora:001#1/q1",
    "work_id": "aozora:001",
    "title": "比べる心",
    "author_names": ["甲"],
    "section": "一",
    "quote_text": "一節の本文。",
    "license": "PD_JP",
    "base_book": "底本の本",
    "url": "https://www.aozora.gr.jp/cards/001/card1.html",
}
CANON = {
    "candidate_id": "canon:lunyu:1.1",
    "work_id": "canon:lunyu",
    "title": "論語",
    "author_names": ["孔子"],
    "section": "学而 1",
    "quote_text": "學而時習之",
    "license": "PD_JP",
    "base_book": "",
    "url": "",
    "source_id": "J-lunyu-1",
}


def _script(**kw):
    return {
        "episode_id": "E1",
        "format": "A",
        "title": "{{title:aozora:001#1/q1}}で考える",
        "comment_prompt": kw.get(
            "question", "{{author:aozora:001#1/q1}}の言葉を、どう読みますか。"
        ),
        "chapters": [
            {
                "chapter_id": "ch0",
                "role": "opening",
                "title": "はじめに",
                "segments": [
                    {
                        "seg_id": "ch0-s1",
                        "kind": "narration",
                        "text": "語りの文。二つ目。三つ目。",
                        "visual": {"type": "photo", "ref": None},
                    },
                ],
            },
            {
                "chapter_id": "ch2",
                "role": "classic",
                "title": "一節",
                "segments": [
                    {
                        "seg_id": "ch2-s1",
                        "kind": "quote",
                        "text": "{{quote:aozora:001#1/q1}}",
                        "visual": {"type": "quote_card", "ref": "aozora:001#1/q1"},
                    },
                    {
                        "seg_id": "ch2-s2",
                        "kind": "quote",
                        "text": "{{quote:canon:lunyu:1.1}}",
                        "visual": {"type": "quote_card", "ref": "canon:lunyu:1.1"},
                        "translation": {"ref": "S-lunyu-1.1-1", "kind": "shiyaku", "label": "試訳"},
                    },
                    {
                        "seg_id": "ch2-s3",
                        "kind": "narration",
                        "text": "背景の語り。",
                        "visual": {"type": "photo", "ref": None},
                        "facts": ["F1"],
                        "refs": ["J-genkai-1"],
                        "findings": ["R1"],
                    },
                ],
            },
        ],
    }


def _resolver():
    return Resolver(
        {CAND["candidate_id"]: CAND, CANON["candidate_id"]: CANON},
        {},
        {"W0001": "同期の昇進を素直に喜べない"},
        refs={
            "J-genkai-1": {
                "ref_id": "R-genkai",
                "ref_title": "言海",
                "ref_pub_year": "1891",
                "ref_credit": "国立国会図書館デジタルコレクション『言海』",
                "ref_pid": "123",
                "locator": "第3巻 45コマ",
                "text": "語釈",
            }
        },
        findings={
            "R1": {
                "source_label": "ミシガン大学の調査（2019年）",
                "citation": "Smith, J. (2019). Title. Journal, 12(3), 45-67. https://doi.org/10.1/x",
            }
        },
    )


def test_kit_sources_carry_credits_licenses_and_citations():
    kit = kit_input(
        _script(),
        _resolver(),
        fact_sources={
            "F1": [
                {
                    "role": "confirm",
                    "kind": "aozora_csv",
                    "title": "作家別作品一覧",
                    "url": "https://www.aozora.gr.jp/index_pages/person1.html",
                    "locator": "",
                    "license": "CC BY 4.0",
                    "credit": "青空文庫 作家別作品一覧",
                },
                {
                    "role": "lead",
                    "kind": "wikipedia",
                    "title": "ウィキペディア",
                    "url": "https://ja.wikipedia.org/x",
                    "license": "CC BY-SA 4.0",
                    "credit": "",
                },
            ]
        },
        credits=["音声：VOICEVOX:テスト"],
        hashtags=["#古典"],
        source_renderings={
            "J-lunyu-1": {
                "ref_credit": "國譯漢文大成",
                "locator": "1巻 3頁",
                "ref_url": "",
                "ref_pid": "999",
            }
        },
    )
    # 問いかけ・タイトルの記号は差し込む
    assert kit.question == "甲の言葉を、どう読みますか。" and kit.title == "『比べる心』で考える"
    by_title = {c.title: c for c in kit.classics}
    assert by_title["比べる心"].base_book == "底本の本"
    # 古典の一節：画面の原文の版と URL（国会図書館の pid）
    assert by_title["論語"].base_book == "國譯漢文大成　1巻 3頁"
    assert (
        by_title["論語"].url == "https://dl.ndl.go.jp/pid/999" and by_title["論語"].label == "試訳"
    )
    # 研究は書誌、事実の出典は CC BY のクレジットとライセンス（ウィキペディアは出さない）
    assert kit.data_sources == [
        "Smith, J. (2019). Title. Journal, 12(3), 45-67. https://doi.org/10.1/x"
    ]
    assert any("青空文庫 作家別作品一覧" in x and "CC BY 4.0" in x for x in kit.background_sources)
    assert not any("wikipedia" in x for x in kit.background_sources)
    # 語釈：クレジットと国会図書館の URL
    assert any(
        x.startswith("国立国会図書館デジタルコレクション『言海』") and "dl.ndl.go.jp/pid/123" in x
        for x in kit.background_sources
    )


def test_chapter_ranges_add_ref_seconds_like_the_planner():
    script = _script()
    formats = {
        "A": {
            "chapters": [
                {"role": "opening", "target_sec": [30, 60]},
                {"role": "classic", "target_sec": [90, 160], "fact_sec": [20, 30]},
            ]
        }
    }
    r = chapter_ranges(script, formats)
    # 事実（fact_sec）と語釈（def_sec が無ければ 20〜30 秒）の分だけ上の端を広げる。下の端はそのまま
    assert r == {"ch0": (30.0, 60.0), "ch2": (90.0, 220.0)}
