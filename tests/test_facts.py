"""背景の事実：和暦・事実の決まり・自動の事実・読み込み・選び方・企画への付け方。"""

# ruff: noqa: E501

from datetime import date

import pytest

from koten.config import DEFAULT_CONFIG_DIR
from koten.db import connect
from koten.facts.dates import era_year, years_since
from koten.facts.model import Fact
from koten.facts.select import select_facts
from koten.facts.store import FactsError, build_facts, derive_auto, load_facts, read_curated
from koten.worries.master import keyword_block, load_blocked_keywords
from tests.facts_fixture import FACTS, write_curated, write_persons
from tests.m4_corpus import build

TODAY = date(2026, 10, 26)


def test_era_year_rules():
    assert era_year(1906) == "明治39年"
    assert era_year(1873) == "明治6年"
    assert era_year(1872) is None  # 改暦の前
    assert era_year(1912) is None  # 年だけでは明治か大正か決まらない
    assert era_year(1912, 7, 29) == "明治45年" and era_year(1912, 7, 30) == "大正元年"
    assert era_year(1926, 12, 25) == "昭和元年" and era_year(1927) == "昭和2年"
    assert era_year(1989, 1, 7) == "昭和64年" and era_year(1989, 1, 8) == "平成元年"
    assert era_year(2020) == "令和2年"
    assert years_since(1913, TODAY) == 110 and years_since(2020, TODAY) is None
    assert years_since(1906, TODAY, 12) == 110  # 1906年12月から2026年10月は119年あまり
    assert years_since(1906, TODAY, 9) == 120


def test_fact_problems():
    good = Fact(**{k: v for k, v in FACTS[0].items()}, checked_by="人")
    assert good.problems() == []
    assert good.slot_text("year") == "1906年" and good.slot_text("title") == "『比べる心』"
    assert good.slot_text("year_wareki") == "明治39年"
    with pytest.raises(KeyError):
        good.slot_text("person")

    def bad(**kw):
        return Fact(**{**FACTS[0], "checked_by": "人", **kw}).problems()

    assert any("checked_by" in p for p in bad(checked_by=""))
    assert any(
        "数字「12」が項目に無い" in p
        for p in bad(text_ja="『比べる心』は、1906年に架空の町で12人と書かれました。")
    )
    assert any("text_ja に無い" in p for p in bad(slots={**FACTS[0]["slots"], "place": "別の町"}))
    assert any(
        "「」の引用" in p
        for p in bad(text_ja="『比べる心』は、1906年に架空の町で「書く」と言いました。")
    )
    assert any("kind death_cause は使えない" in p for p in bad(kind="death_cause"))
    died = "甲野一郎は、1906年に架空の町で自ら命を絶ちました。"
    assert any(
        "死因" in p
        for p in bad(
            text_ja=died, slots={"person": "甲野一郎", "year": "1906", "place": "架空の町"}
        )
    )
    # 年の項目の数字の一部でも、別の数（年齢など）は項目にする
    age = "『比べる心』は、1906年に架空の町で19歳のときに書かれました。"
    assert any("数字「19」が項目に無い" in p for p in bad(text_ja=age))
    assert any("実在する" in p for p in bad(date="1906-02-30"))
    assert any("interest" in p for p in bad(interest="多い"))
    assert any("合わない" in p for p in bad(date="1907"))
    assert any("数字だけ" in p for p in bad(slots={**FACTS[0]["slots"], "year": "千九百六年"}))


def test_derive_auto_facts():
    works = [
        {
            "work_id": "aozora:100",
            "title": "茶の書",
            "license": "PD_JP",
            "url": "https://www.aozora.gr.jp/cards/000010/card100.html",
            "translator_ids": ["aozora:person:000013"],
            "original_title": "THE BOOK",
            "first_pub": "「架空誌」1906（明治39）年5月",
        },
        {
            "work_id": "aozora:101",
            "title": "随想",
            "license": "PD_JP",
            "url": "u",
            "translator_ids": [],
            "first_pub": "「文學界」1938（昭和13）年6月号～1941（昭和16）年11月号",
        },
        {
            "work_id": "aozora:102",
            "title": "保護中",
            "license": "COPYRIGHTED",
            "first_pub": "「x」1990（平成2）年1月",
        },
    ]
    persons = [
        {"person_id": "aozora:person:000010", "canonical_id": "aozora:person:000011",
         "name": "岡倉覚三", "birth_date": "1863-02-14", "death_date": "1913-09-02"},
        {"person_id": "aozora:person:000011", "canonical_id": "aozora:person:000011",
         "name": "岡倉天心", "birth_date": "1863-02-14", "death_date": "1913-09-02"},
        {"person_id": "aozora:person:000013", "canonical_id": "aozora:person:000013",
         "name": "村岡博", "birth_date": "1895", "death_date": "1946"},
    ]  # fmt: skip
    facts, sources = derive_auto(works, persons, TODAY)
    by = {f.fact_id: f for f in facts}
    assert all(f.problems() == [] for f in facts), [(f.fact_id, f.problems()) for f in facts]
    assert "A-p000010-lifespan" not in by  # 別名は代表の人物にまとめる
    assert by["A-p000011-lifespan"].text_ja == "岡倉天心は1863年に生まれ、1913年に亡くなりました。"
    assert by["A-w100-translation"].text_ja == "ここで読む『茶の書』は、村岡博による日本語訳です。"
    assert by["A-w100-original_title"].scope == "original_work"
    assert "A-w100-first_pub" not in by  # 訳書の初出は、訳と原著のどちらか分からない
    assert by["A-w101-first_pub"].text_ja == "『随想』は、1938年6月から「文學界」に載りました。"
    assert by["A-w101-years_since"].slot_text("number") == "80年あまり"
    assert not any(f.subject_id == "aozora:102" for f in facts)  # 保護中の作品には作らない
    assert {s.kind for s in sources} == {"aozora_csv"} and all(s.credit for s in sources)


def test_read_curated_rejects_all_on_any_problem(tmp_path):
    known = {"aozora:001", "aozora:person:p1"}
    write_curated(tmp_path)
    facts, sources = read_curated(tmp_path, known)
    assert [f.fact_id for f in facts] == ["F-w001-001", "F-p1-001"] and len(sources) == 2

    # ウィキペディアだけでは確かめたことにならない
    write_curated(
        tmp_path,
        FACTS[:1],
        [{"fact_id": "F-w001-001", "seq": 1, "kind": "wikipedia", "role": "confirm",
          "title": "ウィキペディア", "url": "https://ja.wikipedia.org/wiki/x", "locator": "",
          "excerpt": "", "license": "CC BY-SA 4.0", "credit": "ウィキペディア"}],
    )  # fmt: skip
    with pytest.raises(FactsError) as e:
        read_curated(tmp_path, known)
    assert "確かめた出典に wikipedia は使えない" in str(e.value)
    assert "確かめた出典（role=confirm、ウィキペディア以外）が無い" in str(e.value)

    # 種類を web にしても、ウィキペディアの URL は確かめた出典にならない
    src = {"fact_id": "F-w001-001", "seq": 1, "kind": "web", "role": "confirm", "title": "記事",
           "url": "https://ja.wikipedia.org/w/index.php?oldid=1", "locator": "", "excerpt": "",
           "license": "CC BY-SA 4.0", "credit": "x"}  # fmt: skip
    write_curated(tmp_path, FACTS[:1], [src])
    with pytest.raises(FactsError, match="ウィキペディアは確かめた出典"):
        read_curated(tmp_path, known)
    # seq の重複・空
    book = {"fact_id": "F-w001-001", "seq": 1, "kind": "book", "role": "confirm", "title": "本",
            "url": "", "locator": "p.1", "excerpt": "", "license": "事実のみ", "credit": ""}  # fmt: skip
    write_curated(tmp_path, FACTS[:1], [book, {**book, "role": "lead"}])
    with pytest.raises(FactsError, match="重複"):
        read_curated(tmp_path, known)
    write_curated(tmp_path, FACTS[:1], [{**book, "seq": ""}])
    with pytest.raises(FactsError, match="seq が数字ではない"):
        read_curated(tmp_path, known)
    # Excel で保存した BOM 付きの CSV も読める
    write_curated(tmp_path, FACTS[:1], [book])
    for name in ("facts.csv", "fact_sources.csv"):
        path = tmp_path / name
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert [f.fact_id for f in read_curated(tmp_path, known)[0]] == ["F-w001-001"]

    write_curated(tmp_path, [{**FACTS[0], "checked_by": ""}])
    with pytest.raises(FactsError, match="checked_by"):
        read_curated(tmp_path, known)
    write_curated(tmp_path, [{**FACTS[0], "subject_id": "aozora:999"}])
    with pytest.raises(FactsError, match="書誌に無い"):
        read_curated(tmp_path, known)
    cats = load_blocked_keywords(DEFAULT_CONFIG_DIR)
    word = next(iter(cats.values()))[0]
    text = f"『比べる心』は、1906年に架空の町で{word}について書かれました。"
    write_curated(tmp_path, [{**FACTS[0], "text_ja": text}])
    with pytest.raises(FactsError, match="除外カテゴリ"):
        read_curated(tmp_path, known, lambda t: keyword_block(t, cats))


@pytest.fixture
def env(settings_file, tmp_path):
    from koten.config import load_settings

    s = load_settings(settings_file)
    build(s.require_hdd(), s.ssd_root, DEFAULT_CONFIG_DIR)
    write_persons(s.require_hdd())
    cfg = tmp_path / "facts_config"
    cfg.mkdir()
    write_curated(cfg)
    r = build_facts(connect(s.ssd_root), s.require_hdd(), cfg, today=TODAY)
    assert r.curated == 2 and r.auto >= 1
    return s


def test_build_and_select(env):
    conn = connect(env.ssd_root)
    facts = load_facts(conn, ["F-w001-001", "A-pp1-lifespan"])
    assert facts["A-pp1-lifespan"].slots == {"person": "甲野一郎", "year": "1880", "year2": "1940"}
    ids = select_facts(conn, work_ids=["aozora:001"], person_ids=["aozora:person:p1"], limit=2)
    assert ids == ["F-w001-001", "F-p1-001"]  # 人が確かめた事実を先に、作品の成り立ちを人より先に
    ids = select_facts(
        conn, work_ids=["aozora:001"], person_ids=["aozora:person:p1"], limit=5,
        exclude={"F-w001-001"},
    )  # fmt: skip
    assert "F-w001-001" not in ids and ids[-1] == "A-pp1-lifespan"
    assert select_facts(conn, work_ids=["aozora:003"], person_ids=[], limit=3) == []


def test_planner_attaches_facts(env):
    from tests.test_plan import make_planner

    p = make_planner(env)
    plan = p.plan("A", worry_id="W0001", now=__import__("datetime").datetime(2026, 11, 1))
    by_work = {s["candidate_id"]: s["work_id"] for s in plan["selection"]}
    with_facts = [c for c in plan["chapters"] if c.get("fact_ids")]
    assert with_facts, plan["chapters"]
    for c in with_facts:  # 事実のある作品（001）か作者（p1：001・007）の章だけに付く
        assert {by_work[cid] for cid in c["candidate_ids"]} & {"aozora:001", "aozora:007"}
    all_ids = [f for c in plan["chapters"] for f in c.get("fact_ids", [])]
    assert len(all_ids) == len(set(all_ids))  # 同じ事実を2つの章に付けない
    assert set(plan["facts"]) == set(all_ids) and all(len(v) == 16 for v in plan["facts"].values())
    assert abs(plan["target_total_sec"] - 720) <= 30


def test_cli_facts_commands(settings_file, tmp_path):
    from typer.testing import CliRunner

    from koten.cli import app
    from koten.config import load_settings
    from tests.test_plan import make_planner

    s = load_settings(settings_file)
    build(s.require_hdd(), s.ssd_root, DEFAULT_CONFIG_DIR)
    write_persons(s.require_hdd())
    st = ["--settings", str(settings_file)]
    run = CliRunner().invoke
    r = run(app, ["facts", "build", *st])  # config/facts.csv は見出しだけ
    assert r.exit_code == 0, r.output
    assert "人が確かめた事実 0" in r.output and "自動の事実 1" in r.output
    r = run(app, ["facts", "list", "--subject", "aozora:person:p1", *st])
    assert "A-pp1-lifespan［auto・lifespan］甲野一郎は1880年に生まれ" in r.output
    assert "confirm：青空文庫 作家別作品一覧" in r.output
    r = run(app, ["facts", "coverage", *st])
    assert "自動の事実 1 件（lifespan 1）" in r.output
    p = make_planner(s)
    eid = p.save(p.plan("A", worry_id="W0001"))
    r = run(app, ["facts", "todo", "--episode", eid, *st])
    assert r.exit_code == 0 and "甲野一郎（aozora:person:p1）：確かめた事実 0・自動 1" in r.output
    assert f"koten facts draft --episode {eid}" in r.output


def test_translation_fact_first_and_once_per_episode(env):
    from koten.facts.store import text_sha

    conn = connect(env.ssd_root)
    with conn:
        conn.execute(
            "INSERT INTO facts(fact_id, subject_type, subject_id, scope, kind, text_ja, slots, "
            "origin, status, interest, text_sha) VALUES ('A-w002-translation', 'work', "
            "'aozora:002', 'translation', 'translation', 'ここで読む『妬みの話』は、乙野の訳です。', "
            "'{}', 'auto', 'auto', 2, ?)",
            (text_sha("x"),),
        )
    kw = {"work_ids": ["aozora:002"], "person_ids": [], "limit": 3}
    assert select_facts(conn, **kw) == ["A-w002-translation"]
    # 最近の回で使っていても、訳の事実は付ける。この回でもう付けたなら付けない
    assert select_facts(conn, **kw, exclude={"A-w002-translation"}) == ["A-w002-translation"]
    assert select_facts(conn, **kw, taken={"A-w002-translation"}) == []


def test_attach_facts_to_existing_plan(settings_file, tmp_path):
    """企画を作ったあとで確かめた事実を足し、koten facts attach で付け直す。"""
    import json

    from typer.testing import CliRunner

    from koten.cli import app
    from koten.config import load_settings
    from tests.test_plan import make_planner

    s = load_settings(settings_file)
    build(s.require_hdd(), s.ssd_root, DEFAULT_CONFIG_DIR)
    write_persons(s.require_hdd())
    cfg = tmp_path / "facts_config"
    cfg.mkdir()
    write_curated(cfg, facts=[], sources=[])
    conn = connect(s.ssd_root)
    build_facts(conn, s.require_hdd(), cfg, today=TODAY)
    p = make_planner(s)
    eid = p.save(p.plan("A", worry_id="W0001"))
    before = json.loads(conn.execute("SELECT plan_json FROM episodes").fetchone()[0])
    assert "F-w001-001" not in [f for c in before["chapters"] for f in c.get("fact_ids", [])]

    write_curated(cfg)
    build_facts(conn, s.require_hdd(), cfg, today=TODAY)
    r = CliRunner().invoke(
        app, ["facts", "attach", "--episode", eid, "--settings", str(settings_file)]
    )
    assert r.exit_code == 0, r.output
    after = json.loads(conn.execute("SELECT plan_json FROM episodes").fetchone()[0])
    assert after["selection"] == before["selection"]  # 一節は変えない
    ids = [f for c in after["chapters"] for f in c["fact_ids"]]
    assert "F-w001-001" in ids and set(after["facts"]) == set(ids)
    assert abs(after["target_total_sec"] - 720) <= 30 and after["schema_version"] == "1.1"
    with conn:
        conn.execute("UPDATE episodes SET state = 'script_done'")
    r = CliRunner().invoke(
        app, ["facts", "attach", "--episode", eid, "--settings", str(settings_file)]
    )
    assert r.exit_code == 2 and "--force" in r.output


def test_translator_lifespan_is_not_attached(env):
    conn = connect(env.ssd_root)
    with conn:
        conn.execute(
            "INSERT INTO persons(person_id, canonical_id, name, birth_date, death_date) "
            "VALUES ('aozora:person:t1', 'aozora:person:t1', '訳野訳子', '1890', '1950')"
        )
        conn.execute(
            "INSERT INTO facts(fact_id, subject_type, subject_id, scope, kind, text_ja, slots, "
            "origin, status, interest, text_sha) VALUES ('A-pt1-lifespan', 'person', "
            "'aozora:person:t1', 'person', 'lifespan', '訳野訳子は1890年に生まれ、1950年に亡くなりました。', "
            "'{}', 'auto', 'auto', 1, 'x')"
        )
    kw = {"work_ids": ["aozora:002"], "person_ids": [], "limit": 3}
    assert "A-pt1-lifespan" not in select_facts(conn, **kw, translator_ids=["aozora:person:t1"])
    # 作者としてなら付く
    assert "A-pt1-lifespan" in select_facts(conn, **{**kw, "person_ids": ["aozora:person:t1"]})
