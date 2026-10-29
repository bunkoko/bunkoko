"""権利判定の境界（SPEC §12 単体テスト）。ここを緩める変更はしない。"""

import pytest

from koten.license import (
    License,
    Person,
    ReviewError,
    apply_review,
    from_cc_license,
    is_usable,
    judge_corporate_work,
    judge_edition,
    judge_ndl_access,
    judge_public_domain,
    may_store_text,
    stricter,
)


def jp(name, year, role="author"):
    return Person(name, year, role, allied=False)


def test_1967_death_is_public_domain():
    assert judge_public_domain([jp("A", 1967)])[0] == License.PD_JP


def test_1968_death_is_protected():
    assert judge_public_domain([jp("A", 1968)])[0] == License.COPYRIGHTED


def test_unknown_death_year():
    assert judge_public_domain([jp("A", None)])[0] == License.UNKNOWN
    assert judge_public_domain([])[0] == License.UNKNOWN


def test_allied_1954_is_public_domain():
    assert judge_public_domain([Person("B", 1954, allied=True)])[0] == License.PD_JP
    assert judge_public_domain([Person("B", 1954, allied=None)])[0] == License.PD_JP


@pytest.mark.parametrize("year", [1955, 1956, 1957])
def test_allied_1955_to_1957_needs_review(year):
    # 国ごとの戦時加算の日数を確かめるまで、1955〜1957年没も人の確認へ（D-019）
    lic, reason = judge_public_domain([Person("B", year, allied=True)])
    assert lic == License.NEEDS_REVIEW and "R4" in reason
    assert judge_public_domain([Person("B", year, allied=None)])[0] == License.NEEDS_REVIEW
    assert judge_public_domain([jp("B", year)])[0] == License.PD_JP


def test_rights_holder_allied_needs_review():
    # 1941-12-08 に英米の出版社が権利を持っていた訳書など
    lic, reason = judge_public_domain([jp("訳者", 1960, "translator")], rights_holder_allied=True)
    assert lic == License.NEEDS_REVIEW and "権利者が連合国籍" in reason
    assert (
        judge_public_domain([jp("訳者", 1960)], rights_holder_allied=None)[0]
        == License.NEEDS_REVIEW
    )
    assert judge_public_domain([jp("訳者", 1960)], rights_holder_allied=False)[0] == License.PD_JP
    assert judge_public_domain([jp("訳者", 1950)], rights_holder_allied=True)[0] == License.PD_JP
    lic, _ = judge_public_domain([jp("訳者", 1960)], prewar_work=False, rights_holder_allied=True)
    assert lic == License.PD_JP


def test_allied_1958_needs_review():
    lic, reason = judge_public_domain([Person("B", 1958, allied=True)])
    assert lic == License.NEEDS_REVIEW
    assert "R4" in reason


def test_unknown_nationality_1958_needs_review():
    assert judge_public_domain([Person("B", 1960, allied=None)])[0] == License.NEEDS_REVIEW


def test_allied_postwar_work_is_public_domain():
    lic, _ = judge_public_domain([Person("B", 1960, allied=True)], prewar_work=False)
    assert lic == License.PD_JP


def test_only_translator_protected():
    persons = [jp("原著者", 1900), jp("訳者", 1990, role="translator")]
    lic, reason = judge_public_domain(persons)
    assert lic == License.COPYRIGHTED
    assert "訳者" in reason


def test_translator_needs_review_propagates():
    persons = [jp("原著者", 1900), Person("訳者", 1965, "translator", allied=True)]
    assert judge_public_domain(persons)[0] == License.NEEDS_REVIEW


@pytest.mark.parametrize(
    "value,expected",
    [
        ("CC BY 4.0", License.CC_BY),
        ("cc-by", License.CC_BY),
        ("https://creativecommons.org/licenses/by/4.0/", License.CC_BY),
        ("CC0", License.CC0),
        ("cc0-1.0", License.CC0),
        ("https://creativecommons.org/publicdomain/zero/1.0/", License.CC0),
        ("CC BY-SA 4.0", License.BLOCKED_SA),
        ("cc-by-nd", License.BLOCKED_ND),
        ("CC BY-NC-SA 4.0", License.BLOCKED_NC),
        ("https://creativecommons.org/licenses/by-nc/4.0/", License.BLOCKED_NC),
        ("All rights reserved", License.UNKNOWN),
        ("", License.UNKNOWN),
        (None, License.UNKNOWN),
    ],
)
def test_from_cc_license(value, expected):
    assert from_cc_license(value) == expected


def test_usable_and_storable():
    for lic in (License.PD_JP, License.CC0, License.CC_BY):
        assert is_usable(lic) and may_store_text(lic)
    for lic in (
        License.BLOCKED_SA,
        License.BLOCKED_ND,
        License.BLOCKED_NC,
        License.COPYRIGHTED,
        License.UNKNOWN,
        License.NEEDS_REVIEW,
    ):
        assert not is_usable(lic) and not may_store_text(lic)


def test_corporate_work():
    assert (
        judge_corporate_work("三省堂編輯所", 1967)[0] == License.NEEDS_REVIEW
    )  # 案1：人が確かめる
    assert judge_corporate_work("三省堂編輯所", 1968)[0] == License.COPYRIGHTED
    assert judge_corporate_work("三省堂編輯所", None)[0] == License.UNKNOWN


def test_edition_year():
    assert judge_edition(1891)[0] == License.PD_JP
    assert judge_edition(1967)[0] == License.PD_JP
    assert judge_edition(1968)[0] == License.NEEDS_REVIEW
    assert judge_edition(None)[0] == License.UNKNOWN


@pytest.mark.parametrize(
    "label,expected",
    [
        ("internet_pd", License.PD_JP),
        ("internet_saitei", License.COPYRIGHTED),
        ("internet_kyodaku", License.COPYRIGHTED),
        ("kojin", License.UNKNOWN),
        ("kannai", License.UNKNOWN),
        ("", License.UNKNOWN),
        (None, License.UNKNOWN),
        ("インターネット公開", License.UNKNOWN),
    ],
)
def test_ndl_access(label, expected):
    assert judge_ndl_access(label)[0] == expected


def test_stricter_takes_worst():
    ok = (License.PD_JP, "a")
    assert stricter(ok, (License.PD_JP, "b")) == (License.PD_JP, "a；b")
    assert stricter(ok, (License.NEEDS_REVIEW, "r"))[0] == License.NEEDS_REVIEW
    assert stricter((License.NEEDS_REVIEW, "r"), (License.UNKNOWN, "u")) == (License.UNKNOWN, "u")
    assert stricter((License.UNKNOWN, "u"), (License.COPYRIGHTED, "c"))[0] == License.COPYRIGHTED
    assert stricter()[0] == License.UNKNOWN


def test_review_only_resolves_needs_review():
    assert apply_review(License.NEEDS_REVIEW, None) == License.NEEDS_REVIEW
    assert apply_review(License.NEEDS_REVIEW, "PD_JP") == License.PD_JP
    assert apply_review(License.NEEDS_REVIEW, "COPYRIGHTED") == License.COPYRIGHTED
    assert apply_review(License.PD_JP, "COPYRIGHTED") == License.COPYRIGHTED
    assert apply_review(License.PD_JP, "UNKNOWN") == License.UNKNOWN
    assert apply_review(License.COPYRIGHTED, "UNKNOWN") == License.COPYRIGHTED
    for computed in (License.UNKNOWN, License.COPYRIGHTED, License.BLOCKED_SA):
        with pytest.raises(ReviewError):
            apply_review(computed, "PD_JP")
    with pytest.raises(ReviewError):
        apply_review(License.NEEDS_REVIEW, "CC0")
