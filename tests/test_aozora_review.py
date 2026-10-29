"""青空文庫で没年・公表の年が空のところを、人が確かめた台帳と作品CSVの年で埋める（D-029）。"""

# ruff: noqa: E501

import pytest

from koten.ingest import aozora
from koten.ingest.review import REVIEW_COLUMNS, AozoraReview, ReviewError, read_review
from koten.license import License
from tests.conftest import list_zip, row

HEAD = ",".join(REVIEW_COLUMNS)


def r(*args, base="", parent="", first="", **kw):
    """作品CSVの1行。底本・親本・初出の年を指定する（既定は空）。"""
    out = row(*args, **kw)
    out["底本初版発行年1"] = base
    out["底本の親本初版発行年1"] = parent
    out["初出"] = first
    return out


ROWS = [
    # 晶子訳『源氏物語』：原著者の没年・生年が空、訳者は1942年没、底本は戦後
    r("1", "源氏物語", "NDC 913", "新字新仮名", "000052", "紫", "式部", "著者", "", base="1965"),
    r(
        "1",
        "源氏物語",
        "NDC 913",
        "新字新仮名",
        "000885",
        "与謝野",
        "晶子",
        "翻訳者",
        "1942-05-29",
        base="1965",
    ),
    # 作者不詳の作品の訳：訳者は1964年没。親本1960（訳が公表されれば元の作品も公表済み）
    r(
        "2",
        "現代語訳　平家物語",
        "NDC 913",
        "新字新仮名",
        "001080",
        "作者不詳",
        "",
        "著者",
        "",
        base="2015",
        parent="1960",
    ),
    r(
        "2",
        "現代語訳　平家物語",
        "NDC 913",
        "新字新仮名",
        "000300",
        "尾崎",
        "士郎",
        "翻訳者",
        "1964-02-19",
        base="2015",
        parent="1960",
    ),
    # 作者不詳の原文。底本は江戸時代の版
    r(
        "3",
        "実語教",
        "NDC 159",
        "新字新仮名",
        "001080",
        "作者不詳",
        "",
        "著者",
        "",
        base="文政新版、1820（文政3）年",
    ),
    # 作品CSVに没年がある人（台帳で上書きしない）
    r("4", "随筆", "NDC 914", "新字新仮名", "000004", "架空", "四郎", "著者", "1980-01-01"),
    # 訳者の名前で選ぶ（NDC は問わない）
    r(
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
    r("5", "和泉式部日記", "NDC 915", "新字新仮名", "000060", "和泉", "式部", "著者", ""),
    # 生年だけある古い人（菅原孝標女 1008年生まれ）
    r(
        "6",
        "更級日記",
        "NDC 915",
        "新字新仮名",
        "000061",
        "菅原孝標女",
        "",
        "著者",
        "",
        birth="1008",
    ),
    # 生年が1846年より後で没年が空の人は、生年だけでは決めない
    r("7", "日記", "NDC 915", "新字新仮名", "000062", "架空", "七郎", "著者", "", birth="1890"),
    # 1900年より前に初出がある人（名前が出ている）
    r(
        "8",
        "雑記",
        "NDC 914",
        "新字新仮名",
        "000063",
        "架空",
        "八郎",
        "著者",
        "",
        first="「架空雑誌」1899（明治32）年5月",
    ),
    # 作者不詳で、公表の証拠が1968年より後だけ
    r("9", "うた", "NDC 911", "新字新仮名", "001080", "作者不詳", "", "著者", "", base="1990"),
]


def works():
    return aozora.read_work_list(list_zip(ROWS))


def write(tmp_path, *lines):
    p = tmp_path / "aozora_review.csv"
    p.write_text("\n".join([HEAD, *lines]) + "\n", encoding="utf-8")
    return p


def test_published_by_is_the_earliest_year():
    w = works()
    assert w["2"].published_by == 1960 and w["3"].published_by == 1820
    assert w["8"].published_by == 1899 and w["5"].published_by is None


def test_without_review():
    w = works()
    # 名前はあるが、生年も1900年より前の公表も無い → 判定不能のまま
    assert aozora.judge_license(w["1"])[0] == License.UNKNOWN
    assert aozora.unknown_people(w["1"]) == ["紫式部"]
    assert aozora.judge_license(w["7"])[0] == License.UNKNOWN
    # 作者不詳で、訳か版が1967年までに公表済み → 満了（52条・4条4項）
    lic, reason = aozora.judge_license(w["2"])
    assert lic == License.PD_JP and "52条" in reason and "1960" in reason
    assert aozora.judge_license(w["3"])[0] == License.PD_JP
    # 作者不詳で、公表の証拠が1968年より後だけ → 判定不能
    assert aozora.judge_license(w["9"])[0] == License.UNKNOWN
    # 1845年までに生まれた人は、1967年より後まで生きていない
    lic, reason = aozora.judge_license(w["6"])
    assert lic == License.PD_JP and "1008年生まれ" in reason
    # 1900年より前に名前が出ている人（利用者の決まり）
    lic, reason = aozora.judge_license(w["8"])
    assert lic == License.PD_JP and "1900年より前" in reason


def test_reviewed_death_and_early_rows(tmp_path):
    w = works()
    review = read_review(
        write(tmp_path, "early,紫式部,,1008,紫式部日記（1008年の記事）,オーナー,2026-11-01,")
    )
    lic, reason = aozora.judge_license(w["1"], review)
    assert lic == License.PD_JP and "1900年より前" in reason and "与謝野晶子" in reason
    assert aozora.unknown_people(w["1"], review) == []
    review = read_review(write(tmp_path, "death,紫式部,,1031,国史大辞典,オーナー,2026-11-01,"))
    assert aozora.judge_license(w["1"], review)[0] == License.PD_JP
    # 作品CSVに年がある人は、台帳に書いても上書きしない
    review = read_review(write(tmp_path, "death,架空四郎,,1900,間違い,オーナー,2026-11-01,"))
    assert aozora.judge_license(w["4"], review)[0] == License.COPYRIGHTED
    review = read_review(write(tmp_path, "early,架空四郎,,1890,間違い,オーナー,2026-11-01,"))
    assert aozora.judge_license(w["4"], review)[0] == License.COPYRIGHTED


def test_anonymous_rows(tmp_path):
    w = works()
    review = read_review(write(tmp_path, "anonymous,作者不詳,うた,1300,根拠,オーナー,2026-11-01,"))
    assert aozora.judge_license(w["9"], review)[0] == License.PD_JP
    late = read_review(write(tmp_path, "anonymous,作者不詳,実語教,1970,根拠,オーナー,2026-11-01,"))
    # 台帳の行を先に見る（1968年からの公表なら保護中）
    assert aozora.judge_license(w["3"], late)[0] == License.COPYRIGHTED
    # 作品名が違えば効かない
    assert aozora.judge_license(w["9"], late)[0] == License.UNKNOWN


@pytest.mark.parametrize(
    "line, message",
    [
        ("death,紫式部,,1031,,オーナー,2026-11-01,", "根拠"),
        ("death,紫式部,,千年,根拠,オーナー,2026-11-01,", "year"),
        ("death,紫式部,,1031,根拠,オーナー,11月1日,", "checked_at"),
        ("death,紫式部,源氏物語,1031,根拠,オーナー,2026-11-01,", "title"),
        ("death,作者不詳,,1300,根拠,オーナー,2026-11-01,", "anonymous"),
        ("early,紫式部,,1905,根拠,オーナー,2026-11-01,", "1900"),
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
    with pytest.raises(ReviewError, match="重なって"):
        read_review(write(tmp_path, line, "early,紫式部,,1008,根拠,オーナー,2026-11-01,"))
    p = tmp_path / "aozora_review.csv"
    p.write_text("kind,name,year\n", encoding="utf-8")
    with pytest.raises(ReviewError, match="列"):
        read_review(p)
    assert read_review(tmp_path / "missing.csv") == AozoraReview()


def test_include_translators_selects_classic_translations():
    conf = {"ndc_prefixes": ["1"], "include_translators": ["与謝野晶子"]}
    picked = {w.work_id for w in aozora.select_works(works(), conf)}
    assert {"1", "5"} <= picked and "2" not in picked


def test_shipped_review_file_reads():
    from koten.config import DEFAULT_CONFIG_DIR
    from koten.ingest.review import REVIEW_FILE

    review = read_review(DEFAULT_CONFIG_DIR / REVIEW_FILE)
    assert "紫式部" in review.early
