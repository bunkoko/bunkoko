"""読み上げの差し込み（M6）：語釈・解釈・古い日本語訳は、人が確かめた読み（かな）で読む。

画面に出す文（resolve）は今までどおり。読みが無ければ画面と同じ本文を読む。
解決できない記号は、読み上げでも同じ誤りで止める。
"""

import shutil

import pytest

from koten.config import DEFAULT_CONFIG_DIR, load_settings
from koten.db import connect
from koten.facts.model import Fact
from koten.ref.store import build_refs, load_refs, load_renderings
from koten.script.placeholders import Resolver, UnresolvedPlaceholder
from koten.shiyaku.store import Reading, Shiyaku, reading_for, save
from tests.ref_fixture import entry, person, rendering, work, write, write_texts

KUN = "学ビテ時ニ之ヲ習フ、亦説バシカラズヤ"
KUN_READ = "まなびてときにこれをならう、またよろこばしからずや"
DEF = "ココロノ マガリナキコト。"
DEF_SHOW = "心の曲がりなきこと。"
DEF_READ = "こころのまがりなきこと"
PD = "有子ガ言ツタ。"
PD_READ = "ゆうしがいった"
LEGGE = "The Master said, Is it not pleasant to learn with a constant perseverance?"
AI_JA = "先生は言った。学んで、折にふれて復習する。うれしいことではないか。"


# --- 記号だけで確かめる（DB を使わない） ---------------------------------------


def ref_row(rid, text, *, reading="", display_text=""):
    return {
        "rendering_id": rid,
        "text": text,
        "display_text": display_text or None,
        "reading": reading or None,
        "ref_title": "架空漢文大系",
        "ref_pub_year": 1909,
        "ref_source": "owned",
    }


def candidate():
    return {
        "quote_text": "子曰學而時習之",
        "title": "論語",
        "author_display": ["孔子"],
        "translator_names": [],
        "section": "学而",
    }


def pd_reading(speech=PD_READ):
    return Reading(
        "pd", PD, "『架空訳論語』（1954）", "I-pd-1.2", "sha-pd", False, "I-pd-1.2", speech=speech
    )


def resolver(**kw):
    base = {
        "candidates": {"C1": candidate()},
        "stats": {"ST1": {"label": "悩む人の割合", "value": 32.9, "unit": "%"}},
        "worries": {"W0001": "同期の昇進を喜べない"},
        "facts": {
            "F1": Fact(
                "F1",
                "person",
                "p1",
                "person",
                "lifespan",
                "架空の人は長く生きました。",
                slots={"person": "架空太郎"},
            )
        },
        "refs": {
            "I-kun-1": ref_row("I-kun-1", KUN, reading=KUN_READ),
            "J-dict-1": ref_row("J-dict-1", DEF, reading=DEF_READ, display_text=DEF_SHOW),
            "I-noread-1": ref_row("I-noread-1", KUN),
            "J-noread-1": ref_row("J-noread-1", DEF, display_text=DEF_SHOW),
        },
        "findings": {
            "FN-1": {
                "claim_ja": "比べると落ち込みやすい。",
                "source_label": "架空大学の調査（2019年）",
                "population_ja": "大学生300人",
                "limits_ja": "",
            }
        },
        "readings": {"C1": pd_reading()},
    }
    return Resolver(**{**base, **kw})


def test_speak_uses_reading_for_def_and_interp():
    r = resolver()
    text = "{{interp_src:I-kun-1}}は「{{interp:I-kun-1}}」、辞書では「{{def:J-dict-1}}」。"
    assert r.speak(text) == (
        f"1909年（明治42年）の『架空漢文大系』は「{KUN_READ}」、辞書では「{DEF_READ}」。"
    )
    # 画面に出す文は今までどおり（表記を改めたものがあればそれ）
    assert r.resolve(text) == (
        f"1909年（明治42年）の『架空漢文大系』は「{KUN}」、辞書では「{DEF_SHOW}」。"
    )
    assert r.value("interp", "I-kun-1") == KUN and r.value("def", "J-dict-1") == DEF_SHOW


def test_speak_falls_back_to_display_text_without_reading():
    r = resolver()
    assert r.speak("「{{interp:I-noread-1}}」") == f"「{KUN}」"
    assert r.speak("「{{def:J-noread-1}}」") == f"「{DEF_SHOW}」"


def test_speak_quote_ja_uses_pd_speech():
    r = resolver()
    assert r.speak("{{quote_ja:C1}}") == PD_READ
    assert r.resolve("{{quote_ja:C1}}") == PD
    assert r.value("quote_ja", "C1") == PD
    # 読みが無い訳（自前の訳など）は、画面と同じ文を読む
    ai = Reading("shiyaku", AI_JA, "試訳", "S-lunyu-1.1-1", "sha-ai", False, "I-legge-1.1")
    r = resolver(readings={"C1": ai})
    assert r.speak("{{quote_ja:C1}}") == AI_JA == r.resolve("{{quote_ja:C1}}")
    r = resolver(readings={"C1": pd_reading(speech="")})
    assert r.speak("{{quote_ja:C1}}") == PD


def test_speak_quote_ja_checks_the_expected_translation():
    exp = {"C1": {"ref": "I-pd-1.2", "sha": "sha-pd"}}
    assert resolver(expected=exp).speak("{{quote_ja:C1}}") == PD_READ
    for bad in ({"ref": "S-lunyu-1.2-1", "sha": "sha-pd"}, {"ref": "I-pd-1.2", "sha": "old"}):
        r = resolver(expected={"C1": bad})
        with pytest.raises(UnresolvedPlaceholder) as shown:
            r.resolve("{{quote_ja:C1}}")
        with pytest.raises(UnresolvedPlaceholder) as spoken:
            r.speak("{{quote_ja:C1}}")
        assert str(spoken.value) == str(shown.value) and "変わった" in str(spoken.value)


def test_other_kinds_speak_like_resolve():
    r = resolver()
    text = (
        "{{quote:C1}}／{{source:C1}}／{{author:C1}}／{{title:C1}}／{{stat:ST1}}／"
        "{{worry:W0001}}／{{fact:F1}}／{{fact:F1.person}}／{{def_src:J-dict-1}}／"
        "{{interp_src:I-kun-1}}／{{finding:FN-1}}／{{finding_src:FN-1}}／{{finding_pop:FN-1}}"
    )
    assert r.speak(text) == r.resolve(text)
    assert "子曰學而時習之" in r.speak(text) and "架空太郎" in r.speak(text)


@pytest.mark.parametrize(
    "text",
    [
        "{{quote:C9}}",
        "{{quote_ja:C9}}",
        "{{source:C9}}",
        "{{stat:ST9}}",
        "{{worry:W9999}}",
        "{{fact:F9}}",
        "{{fact:F1.place}}",
        "{{def:J-none-1}}",
        "{{interp:I-none-1}}",
        "{{def:I-kun-1}}",  # 語釈の記号に解釈の ID
        "{{interp:J-dict-1}}",
        "{{def_src:J-none-1}}",
        "{{finding:FN-9}}",
        "{{finding_limits:FN-1}}",  # 台帳の欄が空
        "{{quote_ja :C1}}",  # 使えない記号
        "{{memo:C1}}",
    ],
)
def test_speak_raises_like_resolve(text):
    r = resolver()
    with pytest.raises(UnresolvedPlaceholder) as shown:
        r.resolve(text)
    with pytest.raises(UnresolvedPlaceholder) as spoken:
        r.speak(text)
    assert str(spoken.value) == str(shown.value)


def test_speak_quote_ja_without_reading_raises():
    r = resolver(readings={})
    with pytest.raises(UnresolvedPlaceholder, match="日本語がまだありません"):
        r.speak("{{quote_ja:C1}}")


# --- DB から（renderings.reading・dict_entries.reading が読み上げに届く） -----------


def ledger(cfg):
    shutil.copy(DEFAULT_CONFIG_DIR / "canon.yaml", cfg / "canon.yaml")
    write(
        cfg,
        [
            work(
                "R-kun",
                kind="kundoku",
                title="架空漢文大系",
                pub_year="1909",
                lang="ja",
                credit="『架空漢文大系』（1909）",
            ),
            work("R-dict", title="架空辞書", pub_year="1891", credit="架空辞書（1891）"),
            work(
                "R-shimo",
                kind="translation",
                title="架空現代訳論語",
                pub_year="1954",
                lang="ja",
                credit="下村架空『架空現代訳論語』（1954）",
            ),
            work(
                "R-legge",
                kind="translation",
                title="Confucian Analects",
                pub_year="1861",
                lang="en",
                credit="レッグの英訳（1861）",
                holder_allied="yes",
            ),
        ],
        [
            person("R-kun", 1, "服部架空", "editor", 1939),
            person("R-dict", 1, "大槻架空", "author", 1928),
            person("R-shimo", 1, "下村架空", "translator", 1955),
            person("R-legge", 1, "James Legge", "translator", 1897, country="イギリス"),
        ],
    )
    write_texts(
        cfg,
        [
            rendering("I-kun-1.1", "lunyu:1.1", "R-kun", KUN, reading=KUN_READ),
            rendering(
                "I-legge-1.1",
                "lunyu:1.1",
                "R-legge",
                LEGGE,
                role="translation",
                lang="en",
                reading="",
                locator="Book I, ch. 1",
            ),
            rendering(
                "I-shimo-1.2",
                "lunyu:1.2",
                "R-shimo",
                PD,
                role="translation",
                reading=PD_READ,
                locator="3ページ",
            ),
        ],
        [
            entry(
                "J-dict-sunao-1",
                "R-dict",
                "素直",
                DEF,
                headword_kana="すなほ",
                reading=DEF_READ,
                display_text=DEF_SHOW,
            ),
        ],
    )


@pytest.fixture
def conn(settings_file, tmp_path):
    s = load_settings(settings_file)
    cfg = tmp_path / "ref_config"
    cfg.mkdir()
    ledger(cfg)
    c = connect(s.ssd_root)
    build_refs(c, cfg)
    return c


def test_pd_reading_carries_the_checked_reading(conn):
    r = reading_for(conn, "lunyu:1.2")
    assert (r.kind, r.ref, r.text, r.speech) == ("pd", "I-shimo-1.2", PD, PD_READ)


def test_ai_translation_has_no_speech(conn):
    src = load_renderings(conn, ["I-legge-1.1"])["I-legge-1.1"]
    save(
        conn,
        Shiyaku(
            shiyaku_id="S-lunyu-1.1-1",
            passage_key="lunyu:1.1",
            kind="shiyaku",
            text=AI_JA,
            source_id="I-legge-1.1",
            source_sha=src["text_sha"],
            basis_ids=[],
            label="試訳",
            status="approved",
            drafter="test",
            approved_by="owner",
        ),
    )
    r = reading_for(conn, "lunyu:1.1")
    assert (r.kind, r.text, r.speech) == ("shiyaku", AI_JA, "")


def test_speak_with_refs_from_db(conn):
    refs = load_refs(conn, ["I-kun-1.1", "J-dict-sunao-1"])
    r = Resolver(
        {"C1": candidate()},
        {},
        {},
        refs=refs,
        readings={"C1": reading_for(conn, "lunyu:1.2")},
    )
    text = "「{{interp:I-kun-1.1}}」「{{def:J-dict-sunao-1}}」{{quote_ja:C1}}"
    assert r.speak(text) == f"「{KUN_READ}」「{DEF_READ}」{PD_READ}"
    assert r.resolve(text) == f"「{KUN}」「{DEF_SHOW}」{PD}"
