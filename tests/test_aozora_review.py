"""青空文庫で没年・公表の年が空のところを、人が確かめた台帳で埋める（D-029）。"""

# ruff: noqa: E501

import pytest

from koten.ingest import aozora
from koten.ingest.review import REVIEW_COLUMNS, AozoraReview, ReviewError, read_review
from koten.license import License
from tests.conftest import list_zip, row

HEAD = ",".join(REVIEW_COLUMNS)

ROWS = [
    # 晶子訳『源氏物語』：原著者の没年が空、訳者は1942年没
    row("1", "源氏物語", "NDC 913", "新字新仮名", "000052", "紫", "式部", "著者", ""),
    row(
        "1", "源氏物語", "NDC 913", "新字新仮名", "000885", "与謝野", "晶子", "翻訳者", "1942-05-29"
    ),
    # 作者不詳の作品の訳：訳者は1964年没
    row("2", "現代語訳　平家物語", "NDC 913", "新字新仮名", "001080", "作者不詳", "", "著者", ""),
    row(
        "2",
        "現代語訳　平家物語",
        "NDC 913",
        "新字新仮名",
        "000300",
        "尾崎",
        "士郎",
        "翻訳者",
        "1964-02-19",
    ),
    # 作者不詳の原文
    row("3", "実語教", "NDC 159", "新字新仮名", "001080", "作者不詳", "", "著者", ""),
    # 作品CSVに没年がある人（台帳で上書きしない）
    row("4", "随筆", "NDC 914", "新字新仮名", "000004", "架空", "四郎", "著者", "1980-01-01"),
    # 訳者の名前で選ぶ（NDC は問わない）
    row(
        "5",
        "和泉式部日記",
        "NDC 915",
        "新字新仮名",
        "000885",
        "与謝野",
        "晶子",
        "翻訳者",
        "1942-05-29",
    ),
    row("5", "和泉式部日記", "NDC 915", "新字新仮名", "000060", "和泉", "式部", "著者", ""),
]


def works():
    return aozora.read_work_list(list_zip(ROWS))


def write(tmp_path, *lines):
    p = tmp_path / "aozora_review.csv"
    p.write_text("\n".join([HEAD, *lines]) + "\n", encoding="utf-8")
    return p


def test_blank_death_years_stay_unknown_without_review():
    w = works()
    for wid in ("1", "2", "3"):
        assert aozora.judge_license(w[wid])[0] == License.UNKNOWN
        assert aozora.judge_license(w[wid], AozoraReview())[0] == License.UNKNOWN
    assert aozora.unknown_people(w["1"]) == ["紫式部"]


def test_reviewed_death_year_fills_the_blank(tmp_path):
    review = read_review(
        write(
            tmp_path,
            "death,紫式部,,1031,国史大辞典「紫式部」（没年は諸説。遅いほう）,オーナー,2026-11-01,",
        )
    )
    w = works()
    lic, reason = aozora.judge_license(w["1"], review)
    assert lic == License.PD_JP and "人が確かめた" in reason and "与謝野晶子" in reason
    assert aozora.unknown_people(w["1"], review) == []
    # 作品CSVに年がある人は、台帳に書いても上書きしない
    review = read_review(write(tmp_path, "death,架空四郎,,1900,間違い,オーナー,2026-11-01,"))
    assert aozora.judge_license(w["4"], review)[0] == License.COPYRIGHTED


def test_anonymous_work_by_publication_year(tmp_path):
    review = read_review(
        write(
            tmp_path,
            "anonymous,作者不詳,現代語訳　平家物語,1300,日本古典文学大辞典「平家物語」,オーナー,2026-11-01,",
            "anonymous,作者不詳,実語教,1600,往来物の研究,オーナー,2026-11-01,",
        )
    )
    w = works()
    lic, reason = aozora.judge_license(w["2"], review)
    assert lic == License.PD_JP and "52条" in reason and "尾崎士郎" in reason
    assert aozora.judge_license(w["3"], review)[0] == License.PD_JP
    # 1968年からの公表は保護中
    late = read_review(write(tmp_path, "anonymous,作者不詳,実語教,1970,根拠,オーナー,2026-11-01,"))
    assert aozora.judge_license(w["3"], late)[0] == License.COPYRIGHTED
    # 作品名が違えば効かない
    assert aozora.judge_license(w["2"], late)[0] == License.UNKNOWN


@pytest.mark.parametrize(
    "line, message",
    [
        ("death,紫式部,,1031,,オーナー,2026-11-01,", "根拠"),
        ("death,紫式部,,千年,根拠,オーナー,2026-11-01,", "year"),
        ("death,紫式部,,1031,根拠,オーナー,11月1日,", "checked_at"),
        ("death,紫式部,源氏物語,1031,根拠,オーナー,2026-11-01,", "title"),
        ("death,作者不詳,,1300,根拠,オーナー,2026-11-01,", "anonymous"),
        ("anonymous,紫式部,源氏物語,1031,根拠,オーナー,2026-11-01,", "作者不詳の作品だけ"),
        ("anonymous,作者不詳,,1300,根拠,オーナー,2026-11-01,", "title"),
        ("pd,紫式部,,1031,根拠,オーナー,2026-11-01,", "kind"),
    ],
)
def test_review_rows_are_checked(tmp_path, line, message):
    with pytest.raises(ReviewError, match=message):
        read_review(write(tmp_path, line))


def test_review_rejects_duplicates_and_wrong_columns(tmp_path):
    line = "death,紫式部,,1031,根拠,オーナー,2026-11-01,"
    with pytest.raises(ReviewError, match="重なって"):
        read_review(write(tmp_path, line, line))
    p = tmp_path / "aozora_review.csv"
    p.write_text("kind,name,year\n", encoding="utf-8")
    with pytest.raises(ReviewError, match="列"):
        read_review(p)
    assert read_review(tmp_path / "missing.csv") == AozoraReview()


def test_include_translators_selects_classic_translations():
    conf = {"ndc_prefixes": ["1"], "include_translators": ["与謝野晶子"]}
    picked = {w.work_id for w in aozora.select_works(works(), conf)}
    assert {"1", "5"} <= picked and "2" not in picked
