from koten.textnorm import normalize_for_match


def test_absorbs_space_newline_and_width():
    assert normalize_for_match("　嫉妬は\n ＡＢＣ１２３！") == "嫉妬はABC123!"


def test_halfwidth_kana_to_fullwidth():
    assert normalize_for_match("ｶﾞｯｺｳ") == "ガッコウ"


def test_kanji_unchanged():
    # 互換漢字（U+F900台）も変えない
    assert normalize_for_match("欄髙﨑") == "欄髙﨑"
