"""背景の事実の下書き（Wikidata・ウィキペディアは偽のサーバ）と、確かめた下書きの取り込み。"""

# ruff: noqa: E501

import csv
import json
from datetime import date

import httpx
import pytest

from koten.config import DEFAULT_CONFIG_DIR, load_settings
from koten.db import connect
from koten.facts import wikimedia as wm
from koten.facts.draft import (
    DraftConfig,
    accept_review,
    draft_subject,
    person_subject,
    write_review,
)
from koten.facts.store import FactsError, build_facts, read_curated
from koten.net import Fetcher
from tests.facts_fixture import write_curated, write_persons
from tests.m4_corpus import build

PROMPTS = DEFAULT_CONFIG_DIR.parent / "prompts"

ENTITY = {
    "entities": {
        "Q100": {
            "id": "Q100",
            "sitelinks": {"jawiki": {"title": "甲野一郎"}},
            "claims": {
                "P569": [
                    {
                        "mainsnak": {
                            "datavalue": {
                                "type": "time",
                                "value": {"time": "+1880-02-03T00:00:00Z", "precision": 11},
                            }
                        },
                        "references": [
                            {"snaks": {"P248": [{"datavalue": {"type": "wikibase-entityid", "value": {"id": "Q9"}}}]}}
                        ],
                    }
                ],
                "P19": [
                    {
                        "mainsnak": {"datavalue": {"type": "wikibase-entityid", "value": {"id": "Q5"}}},
                        "references": [{"snaks": {"P143": [{}]}}],
                    }
                ],
                "P509": [{"mainsnak": {"datavalue": {"type": "wikibase-entityid", "value": {"id": "Q7"}}}}],
            },
        }
    }
}  # fmt: skip
WIKITEXT = "'''甲野一郎'''は架空の作家。1905年に架空の国の作家を訪ねた<ref>架空の伝記</ref>。"


class FakeWikimedia:
    def __init__(self, busy: int = 0):
        self.calls: list[str] = []
        self.busy = busy

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        assert "contact" in request.headers["User-Agent"]
        if self.busy > 0:
            self.busy -= 1
            return httpx.Response(429, headers={"Retry-After": "7"})
        if "list=search" in url:
            return httpx.Response(200, json={"query": {"search": [{"title": "Q100"}]}})
        if "Special:EntityData/Q100.json" in url:
            return httpx.Response(200, json=ENTITY)
        if "wbgetentities" in url:
            return httpx.Response(
                200, json={"entities": {"Q5": {"labels": {"ja": {"value": "架空の町"}}}}}
            )
        if "ja.wikipedia.org" in url:
            page = {
                "title": "甲野一郎",
                "revisions": [{"revid": 42, "slots": {"main": {"content": WIKITEXT}}}],
            }
            return httpx.Response(200, json={"query": {"pages": [page]}})
        return httpx.Response(404)  # fmt: skip


def fetcher(tmp_path, server, name, sleeps=None):
    client = httpx.Client(
        transport=httpx.MockTransport(server), headers={"User-Agent": "koten-test (contact: t@x)"}
    )
    return Fetcher(
        tmp_path / "raw" / name,
        user_agent="koten-test (contact: t@x)",
        per_host_per_sec=1000,
        client=client,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )


class DraftLLM:
    name = "fake-draft"

    def __init__(self):
        self.users: list[str] = []

    def complete(self, system, user, schema=None):
        self.users.append(user)
        facts = [
            {
                "text_ja": "甲野一郎は、1905年に架空の国の作家を訪ねました。",
                "kind": "life_event",
                "scope": "person",
                "slots": {"person": "甲野一郎", "year": "1905", "person2": "架空の国の作家"},
                "date": "1905",
                "sensitive": "none",
                "interest": 3,
                "wikipedia_ref": "架空の伝記",
                "confirm_hint": "架空の伝記（架空書房）",
            },
            {
                "text_ja": "甲野一郎は、1880年に生まれ、12人きょうだいでした。",
                "kind": "life_event",
                "scope": "person",
                "slots": {"person": "甲野一郎", "year": "1880"},
                "date": "1880",
                "sensitive": "none",
                "interest": 1,
                "wikipedia_ref": "",
                "confirm_hint": "",
            },
        ]
        return json.dumps({"facts": facts}, ensure_ascii=False)


@pytest.fixture
def env(settings_file, tmp_path):
    s = load_settings(settings_file)
    build(s.require_hdd(), s.ssd_root, DEFAULT_CONFIG_DIR)
    write_persons(s.require_hdd())
    cfg = tmp_path / "facts_config"
    cfg.mkdir()
    write_curated(cfg, facts=[], sources=[])
    build_facts(connect(s.ssd_root), s.require_hdd(), cfg, today=date(2026, 10, 26))
    return s, cfg


def test_ids_for_aozora():
    assert wm.aozora_author_value("aozora:person:000879") == "879"
    assert (
        wm.aozora_work_value("https://www.aozora.gr.jp/cards/000879/card127.html")
        == "000879/card127"
    )
    assert wm.aozora_work_value("https://example.org/x") is None


def test_fetch_waits_on_429(tmp_path):
    server, sleeps = FakeWikimedia(busy=2), []
    f = fetcher(tmp_path, server, "wikidata", sleeps)
    assert wm.find_qid(f, "P7311", "879") == "Q100"
    assert sleeps == [7.0, 7.0] and len(server.calls) == 3
    wm.find_qid(f, "P7311", "879")  # 2回目はキャッシュ（取りに行かない）
    assert len(server.calls) == 3


def test_claims_skip_death_cause_and_mark_references():
    found = wm.claims(ENTITY["entities"]["Q100"], wm.PERSON_PROPS)
    by = {c.prop: c for c in found}
    assert (
        by["P569"].value == "1880-02-03"
        and by["P569"].referenced
        and by["P569"].refs == ["P248:Q9"]
    )
    assert not by["P19"].referenced  # 取り込み元（P143）だけの出典は弱い
    assert "P509" not in by  # 死因は読まない


def test_draft_and_accept(env, tmp_path):
    s, cfg = env
    conn = connect(s.ssd_root)
    server = FakeWikimedia()
    llm = DraftLLM()
    subj = person_subject(conn, "aozora:person:p1")
    rows = draft_subject(
        subj,
        wikidata=fetcher(tmp_path, server, "wikidata"),
        wikipedia=fetcher(tmp_path, server, "wikipedia"),
        llm=llm,
        prompts_dir=PROMPTS,
        cfg=DraftConfig(),
        taken_ids={"F-pp1-001"},
    )
    assert [r["fact_id"] for r in rows] == ["F-pp1-002", "F-pp1-003"]
    good, bad = rows
    assert (
        good["problems"] == ""
        and good["lead_url"] == "https://ja.wikipedia.org/w/index.php?oldid=42"
    )
    assert "数字「12」が項目に無い" in bad["problems"]
    assert bad["confirm_kind"] == "wikidata_ref"  # 1880 が出典つきの生年月日と合う
    assert (
        "死因" not in llm.users[0] and "架空の町" in llm.users[0] and "（出典あり）" in llm.users[0]
    )
    assert "<ref>架空の伝記</ref>" in llm.users[0]

    review = tmp_path / "review.csv"
    good.update(
        {
            "accept": "1",
            "confirm_kind": "book",
            "confirm_title": "架空の伝記",
            "confirm_locator": "架空書房、12ページ",
            "confirm_license": "事実のみ（文は自前）",
            "checked_by": "テスト",
        }
    )
    write_review(review, [good, bad])
    known = {"aozora:person:p1"}
    assert accept_review(review, cfg, known, today=date(2026, 10, 26)) == 1
    facts, sources = read_curated(cfg, known)
    assert [f.fact_id for f in facts] == ["F-pp1-002"]
    assert {(x.role, x.kind) for x in sources} == {("confirm", "book"), ("lead", "wikipedia")}

    # おかしい行を accept にすると、何も書かない
    bad.update({"accept": "1", "checked_by": "テスト", "confirm_kind": "book", "confirm_locator": "x",
                "confirm_license": "x"})  # fmt: skip
    write_review(review, [bad])
    before = (cfg / "facts.csv").read_text("utf-8")
    with pytest.raises(FactsError, match="12"):
        accept_review(review, cfg, known)
    assert (cfg / "facts.csv").read_text("utf-8") == before
    with (cfg / "fact_sources.csv").open(encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 2
