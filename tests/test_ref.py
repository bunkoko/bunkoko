"""資料の台帳（config/ref_works.csv・ref_people.csv）の読み込みと権利の判定（M8a）。"""

# ruff: noqa: E501

import shutil

import pytest
from typer.testing import CliRunner

from koten.cli import app
from koten.config import DEFAULT_CONFIG_DIR
from koten.license import License
from koten.ref.registry import RefError, read_registry
from tests.ref_fixture import person, work, write


def judged(tmp_path, works, people):
    write(tmp_path, works, people)
    return {w.ref_id: w for w in read_registry(tmp_path)}


def test_registry_judges_every_contributor(tmp_path):
    got = judged(
        tmp_path,
        [
            work("R-a"),
            work("R-b", kind="kundoku"),
            work(
                "R-long", kind="translation", lang="en", source="standardebooks", holder_allied=""
            ),
        ],
        [
            person("R-a", 1, "甲野", "author", 1928),
            # 校訂者が保護中なら、著者が満了していても使えない
            person("R-b", 1, "乙山", "author", 1900),
            person("R-b", 2, "丙川", "reviser", 1980),
            person("R-long", 1, "古代の著者", "author", 180, country="", allied=""),
            person("R-long", 2, "英国の訳者", "translator", 1879, country="英国", allied="yes"),
        ],
    )
    assert got["R-a"].license == License.PD_JP and got["R-a"].usable
    assert got["R-b"].license == License.COPYRIGHTED
    assert "丙川（校訂）" in got["R-b"].computed_reason
    assert got["R-long"].license == License.PD_JP
    assert [p.name for p in got["R-long"].people] == ["古代の著者", "英国の訳者"]


def test_wartime_and_holder(tmp_path):
    got = judged(
        tmp_path,
        [work("R-a"), work("R-b", holder_allied=""), work("R-c")],
        [
            # 連合国籍・国籍不明で1955〜1957年没は人の確認へ
            person("R-a", 1, "英国の訳者", "translator", 1956, country="英国", allied="yes"),
            # 日本人でも、1941-12-08 の権利者が分からなければ人の確認へ
            person("R-b", 1, "丁原", "translator", 1960),
            person("R-c", 1, "戊田", "translator", 1960),
        ],
    )
    assert got["R-a"].license == License.NEEDS_REVIEW
    assert got["R-b"].license == License.NEEDS_REVIEW
    assert got["R-c"].license == License.PD_JP


def test_ndl_label_is_a_second_gate(tmp_path):
    ndl = {"source": "ndl", "pid": "000000", "ndl_checked_at": "2026-10-29"}
    got = judged(
        tmp_path,
        [
            work("R-pd", ndl_access="internet_pd", **ndl),
            work("R-kojin", ndl_access="kojin", **ndl),
            work("R-saitei", ndl_access="internet_saitei", **ndl),
            work("R-blank", ndl_access="", **ndl),
        ],
        [person(r, 1, "甲野", "author", 1928) for r in ("R-pd", "R-kojin", "R-saitei", "R-blank")],
    )
    assert got["R-pd"].license == License.PD_JP
    assert got["R-kojin"].license == License.UNKNOWN
    assert got["R-saitei"].license == License.COPYRIGHTED
    assert got["R-blank"].license == License.UNKNOWN


def test_corporate_work_needs_human_review(tmp_path):
    reviewed = {
        "reviewed_license": "PD_JP",
        "reviewed_by": "owner",
        "reviewed_at": "2026-10-29",
        "review_note": "奥付に団体名だけ。個人の著作者名なし",
    }
    got = judged(
        tmp_path,
        [
            work("R-corp", corporate="yes", credited_as="架空書院編輯所", pub_year="1903"),
            work(
                "R-corp-ok",
                corporate="yes",
                credited_as="架空書院編輯所",
                pub_year="1903",
                **reviewed,
            ),
            work(
                "R-corp-new",
                corporate="yes",
                credited_as="架空書院編輯所",
                pub_year="1970",
                first_pub_year="1970",
            ),
            # 1968年以降の復刻は、足された解説の書き手を人が確かめる
            work("R-reprint", pub_year="1975", first_pub_year="1891"),
        ],
        [person("R-reprint", 1, "甲野", "author", 1928)],
    )
    assert got["R-corp"].license == License.NEEDS_REVIEW
    assert "D-018" in got["R-corp"].computed_reason
    assert got["R-corp-ok"].license == License.PD_JP
    assert got["R-corp-ok"].computed == License.NEEDS_REVIEW
    assert got["R-corp-new"].license == License.COPYRIGHTED
    assert got["R-reprint"].license == License.NEEDS_REVIEW


def test_review_cannot_unlock_unknown_or_copyrighted(tmp_path):
    reviewed = {
        "reviewed_license": "PD_JP",
        "reviewed_by": "owner",
        "reviewed_at": "2026-10-29",
        "review_note": "x",
    }
    write(
        tmp_path,
        [work("R-a", **reviewed)],
        [person("R-a", 1, "甲野", "author", None)],  # 没年が分からない → UNKNOWN
    )
    with pytest.raises(RefError, match="使えるようにはできない"):
        read_registry(tmp_path)
    # 厳しくする向きの人の確認は、いつでも受ける
    got = judged(
        tmp_path,
        [
            work(
                "R-a",
                reviewed_license="COPYRIGHTED",
                reviewed_by="owner",
                reviewed_at="2026-10-29",
                review_note="増補あり",
            )
        ],
        [person("R-a", 1, "甲野", "author", 1928)],
    )
    assert got["R-a"].license == License.COPYRIGHTED


@pytest.mark.parametrize(
    "works,people,message",
    [
        ([work("R-a", source_class="S4")], [], "今の本（S4）は台帳に載せない"),
        ([work("bad")], [], "R- で始め"),
        ([work("R-a"), work("R-a")], [], "重複"),
        ([work("R-a", pub_year="")], [], "pub_year"),
        ([work("R-a", source="ndl")], [], "pid"),
        ([work("R-a", pid="000001")], [], "source は ndl"),
        (
            [work("R-a", reviewed_license="PD_JP")],
            [person("R-a", 1, "甲", "author", 1900)],
            "reviewed_by",
        ),
        (
            [work("R-a", based_on="R-zzz")],
            [person("R-a", 1, "甲", "author", 1900)],
            "R-zzz が台帳に無い",
        ),
        ([work("R-a")], [person("R-a", 1, "甲", "author", 1900, death_source="")], "death_source"),
        ([work("R-a")], [person("R-a", 1, "甲", "writer", 1900)], "role は"),
        ([work("R-a")], [person("R-x", 1, "甲", "author", 1900)], "ref_works.csv に無い資料"),
        (
            [work("R-a")],
            [person("R-a", 1, "甲", "author", 1900), person("R-a", 1, "乙", "editor", 1900)],
            "組が重複",
        ),
    ],
)
def test_registry_problems_stop_everything(tmp_path, works, people, message):
    write(tmp_path, works, people)
    with pytest.raises(RefError, match=message):
        read_registry(tmp_path)


def test_no_people_is_unknown(tmp_path):
    got = judged(tmp_path, [work("R-a")], [])
    assert got["R-a"].license == License.UNKNOWN


def test_bc_death_year(tmp_path):
    got = judged(
        tmp_path,
        [work("R-a", kind="original", source_class="S1", lang="zh")],
        [person("R-a", 1, "古代の思想家", "author", -479, country="")],
    )
    assert got["R-a"].license == License.PD_JP


def test_ref_check_command(tmp_path):
    shutil.copy(DEFAULT_CONFIG_DIR / "sources.yaml", tmp_path / "sources.yaml")
    (tmp_path / "settings.toml").write_text(
        f'[paths]\nhdd_root = "{tmp_path / "hdd"}"\nssd_root = "{tmp_path / "ssd"}"\n', "utf-8"
    )
    write(
        tmp_path,
        [
            work("R-a"),
            work("R-corp", corporate="yes", credited_as="架空書院編輯所", pub_year="1903"),
        ],
        [person("R-a", 1, "甲野", "author", 1928)],
    )
    st = ["--settings", str(tmp_path / "settings.toml")]
    r = CliRunner().invoke(app, ["ref", "check", *st])
    assert r.exit_code == 0, r.output
    assert "R-a  『架空辞書』（1891）  PD_JP 使える" in r.output
    assert "R-corp" in r.output and "NEEDS_REVIEW 使えない" in r.output
    assert "reviewed_license に PD_JP か COPYRIGHTED" in r.output
    write(tmp_path, [work("R-a", source_class="S4")], [])
    r = CliRunner().invoke(app, ["ref", "check", *st])
    assert r.exit_code == 2 and "台帳に問題があります" in r.output
