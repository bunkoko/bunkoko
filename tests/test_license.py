"""権利判定の境界（SPEC §12 単体テスト）。ここを緩める変更はしない。"""

import pytest

from koten.license import (
    License,
    Person,
    from_cc_license,
    is_usable,
    judge_public_domain,
    may_store_text,
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


def test_allied_1957_is_public_domain():
    assert judge_public_domain([Person("B", 1957, allied=True)])[0] == License.PD_JP


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
