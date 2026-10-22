from koten.textnorm import locate, normalize_for_match


def test_absorbs_space_newline_and_width():
    assert normalize_for_match("　嫉妬は\n ＡＢＣ１２３！") == "嫉妬はABC123!"


def test_halfwidth_kana_to_fullwidth():
    assert normalize_for_match("ｶﾞｯｺｳ") == "ガッコウ"


def test_kanji_unchanged():
    # 互換漢字（U+F900台）も変えない
    assert normalize_for_match("欄髙﨑") == "欄髙﨑"


def test_locate_exact():
    text = "　人は比べる。だから苦しい。"
    assert locate("だから苦しい。", text) == (7, 14)


def test_locate_ignores_space_and_width_but_returns_original():
    text = "　死について考える。\n　ＡＩは考えない。"
    start, end = locate("考える。AIは考えない", text)
    assert text[start:end] == "考える。\n　ＡＩは考えない"


def test_locate_rejects_altered_words():
    assert locate("死について悩む。", "　死について考える。") is None
    assert locate("", "本文") is None
