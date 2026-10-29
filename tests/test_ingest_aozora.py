"""青空文庫の取得〜チャンク化を、フィクスチャと偽のHTTPで通す（M1）。"""

# ruff: noqa: E501

import json

import httpx

from koten.chunk.chunker import chunk_source
from koten.config import load_settings
from koten.ingest import aozora
from koten.license import License
from koten.net import Fetcher
from koten.normalize.aozora import normalize
from koten.parquet import read_rows
from tests.conftest import SAMPLE_TEXT, list_zip, row, text_zip

ROWS = [
    # NDC 159・新字新仮名（使用可）
    row(
        "100",
        "人生のノート",
        "NDC 159",
        "新字新仮名",
        "000001",
        "架空",
        "太郎",
        "著者",
        "1945-09-26",
    ),
    # 同じ作品の旧字旧仮名（重複として除かれる）
    row(
        "101",
        "人生のノート",
        "NDC 159",
        "旧字旧仮名",
        "000001",
        "架空",
        "太郎",
        "著者",
        "1945-09-26",
    ),
    # 随筆 914。著者が1968年没（フラグが「なし」でも R3 で保護中）
    row("200", "随筆", "NDC 914", "新字新仮名", "000002", "架空", "次郎", "著者", "1968-01-01"),
    # 著作権フラグ「あり」
    row(
        "300",
        "日記",
        "NDC 915",
        "新字新仮名",
        "000003",
        "架空",
        "三郎",
        "著者",
        "1990-01-01",
        person_flag="あり",
    ),
    # 対象外の NDC
    row("400", "小説", "NDC 913", "新字新仮名", "000004", "架空", "四郎", "著者", "1930-01-01"),
    # 対象外の NDC だが include_works で指定
    row(
        "500", "人生論ノート", "NDC 913", "新字新仮名", "000005", "三木", "清", "著者", "1945-09-26"
    ),
    # 翻訳：原著者は国籍不明で1960年没 → R4 で手動確認
    row("600", "省察", "NDC 131", "新字新仮名", "000006", "外国", "著者", "著者", "1960-01-01"),
    row("600", "省察", "NDC 131", "新字新仮名", "000007", "日本", "訳者", "翻訳者", "1940-01-01"),
]

CONF = {
    "list_url": "https://www.aozora.gr.jp/index_pages/list_person_all_extended_utf8.zip",
    "text_base_url": "https://mirror.example/aozora/",
    "ndc_prefixes": ["1", "914", "915"],
    "include_works": [{"title": "人生論ノート", "author": "三木清"}],
    "exclude_work_ids": [],
}


class FakeServer:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        assert request.headers["User-Agent"] == "test-agent"
        if url.endswith("list_person_all_extended_utf8.zip"):
            return httpx.Response(200, content=list_zip(ROWS), headers={"ETag": '"v1"'})
        if url.startswith("https://mirror.example/aozora/cards/"):
            return httpx.Response(200, content=text_zip(SAMPLE_TEXT))
        return httpx.Response(404)


def make_fetcher(hdd, server):
    client = httpx.Client(
        transport=httpx.MockTransport(server), headers={"User-Agent": "test-agent"}
    )
    return Fetcher(
        hdd / "raw" / "aozora", user_agent="test-agent", per_host_per_sec=1000, client=client
    )


def test_select_and_judge():
    works = aozora.select_works(aozora.read_work_list(list_zip(ROWS)), CONF)
    picked = {w.work_id: w for w in works}
    assert set(picked) == {"100", "200", "300", "500", "600"}
    assert aozora.judge_license(picked["100"])[0] == License.PD_JP
    assert aozora.judge_license(picked["200"])[0] == License.COPYRIGHTED
    assert aozora.judge_license(picked["300"])[0] == License.COPYRIGHTED
    assert aozora.judge_license(picked["500"])[0] == License.PD_JP
    assert aozora.judge_license(picked["600"])[0] == License.NEEDS_REVIEW


def test_include_works_come_first():
    works = aozora.select_works(aozora.read_work_list(list_zip(ROWS)), CONF)
    assert works[0].title == "人生論ノート"
    assert [w.work_id for w in works[1:]] == ["100", "200", "300", "600"]


def test_ndc_parsing():
    assert aozora.ndc_codes("NDC 159 914") == ["159", "914"]
    assert aozora.ndc_codes("NDC K913") == ["913"]
    assert aozora.matches_ndc("NDC 121", ["1"])
    assert not aozora.matches_ndc("NDC 913", ["1", "914", "915"])


def test_ingest_normalize_chunk_end_to_end(settings_file):
    s = load_settings(settings_file)
    hdd = s.require_hdd()
    server = FakeServer()

    logs: list[str] = []
    r = aozora.ingest(hdd, CONF, make_fetcher(hdd, server), limit=1, log=logs.append)
    assert logs[0].startswith("作品CSVを確認中")
    assert any(line.startswith("[1/1] 人生論ノート（三木清）") for line in logs)
    assert (r.selected, r.usable, r.fetched) == (5, 2, 1)
    assert r.blocked == {"COPYRIGHTED": 2, "NEEDS_REVIEW": 1}
    # 中断しても続きから：2回目は残りの1件だけ取り、作品CSVは再取得しない
    server2 = FakeServer()
    r2 = aozora.ingest(hdd, CONF, make_fetcher(hdd, server2))
    assert (r2.fetched, r2.skipped) == (1, 1)
    assert len(server2.calls) == 1 and "mirror.example" in server2.calls[0]

    works = {w["source_work_id"]: w for w in read_rows(hdd / "clean/aozora/works.parquet")}
    # 本文を保存しない作品は書誌情報だけ持ち、原本も取らない（R2）
    assert works["200"]["raw_path"] is None and works["200"]["license"] == "COPYRIGHTED"
    assert works["600"]["review_required"] is True and works["600"]["raw_path"] is None
    assert works["100"]["work_id"] == "aozora:100"
    assert works["100"]["raw_path"] == "cards/000001/files/100_ruby_1.zip"
    manifest = [json.loads(x) for x in (hdd / "raw/aozora/manifest.jsonl").read_text().splitlines()]
    assert {m["license"] for m in manifest if "cards/" in m["path"]} == {"PD_JP"}
    assert all(len(m["sha256"]) == 64 and m["fetched_at"] for m in manifest)

    assert normalize(hdd) == 2
    n = chunk_source(hdd, "aozora", 20, 80)
    chunks = read_rows(hdd / "chunks/aozora.parquet")
    assert n == len(chunks) > 0
    assert {c["work_id"] for c in chunks} == {"aozora:100", "aozora:500"}
    assert {c["section"] for c in chunks} == {"嫉妬について", "孤独について"}
    assert not any(m in c["text_orig"] for c in chunks for m in ("《", "｜", "［＃"))


def test_fetch_error_does_not_stop_batch(settings_file):
    hdd = load_settings(settings_file).require_hdd()

    def flaky(request: httpx.Request) -> httpx.Response:
        if "/cards/000001/" in str(request.url):
            return httpx.Response(404)
        return FakeServer()(request)

    r = aozora.ingest(hdd, CONF, make_fetcher(hdd, flaky))
    assert r.fetched == 1 and len(r.errors) == 1
    assert r.errors[0].startswith("100 人生のノート: Client error '404 Not Found'")
    assert "\n" not in r.errors[0]
    works = {w["source_work_id"]: w for w in read_rows(hdd / "clean/aozora/works.parquet")}
    assert works["100"]["raw_path"] is None
    assert works["500"]["raw_path"] is not None


def test_person_aliases_merge_same_person():
    """青空文庫で別名が別の人物として並ぶとき、1人の著者にまとめて表示する。"""
    rows = [
        # 同じ人（姓・生年月日・没年月日が同じ）が2つの人物IDで1つの作品に並ぶ
        row(
            "700",
            "茶の書",
            "NDC 791",
            "新字新仮名",
            "000010",
            "岡倉",
            "覚三",
            "著者",
            "1913-09-02",
            birth="1863-02-14",
        ),
        row(
            "700",
            "茶の書",
            "NDC 791",
            "新字新仮名",
            "000011",
            "岡倉",
            "天心",
            "著者",
            "1913-09-02",
            birth="1863-02-14",
        ),
        row(
            "700", "茶の書", "NDC 791", "新字新仮名", "000013", "村岡", "博", "翻訳者", "1946-01-01"
        ),
        # 天心の名でだけ載る作品が多い → 天心を主な名前にする
        row(
            "701",
            "東の理想",
            "NDC 702",
            "新字新仮名",
            "000011",
            "岡倉",
            "天心",
            "著者",
            "1913-09-02",
            birth="1863-02-14",
        ),
        row(
            "702",
            "日本の目覚め",
            "NDC 702",
            "新字新仮名",
            "000011",
            "岡倉",
            "天心",
            "著者",
            "1913-09-02",
            birth="1863-02-14",
        ),
        # 生年月日の無い人物はまとめない
        row("703", "随想", "NDC 914", "新字新仮名", "000012", "岡倉", "一", "著者", "1913-09-02"),
    ]
    works = aozora.read_work_list(list_zip(rows))
    aliases = aozora.person_aliases(works)
    assert aliases == {"000010": "000011", "000011": "000011"}
    r = aozora.work_row(works["700"], License.PD_JP, "", None, aliases)
    assert r["author_ids"] == ["aozora:person:000011"]
    assert r["author_display"] == ["岡倉天心（岡倉覚三）"]
    assert r["author_names"] == ["岡倉覚三", "岡倉天心"]  # G1 の直書きの検出には両方を使う
    assert r["translator_names"] == ["村岡博"]
    only = aozora.work_row(works["703"], License.PD_JP, "", None, aliases)
    assert only["author_display"] == ["岡倉一"] and only["author_ids"] == ["aozora:person:000012"]


def test_alias_head_outside_selection_gets_a_person_row():
    """代表の人物（作品の多い別名）が対象外の作品にしか載っていなくても、persons に入る。"""
    rows = [
        row("800", "随想集", "NDC 914", "新字新仮名", "000020", "徳冨", "健次郎", "著者", "1927-09-18", birth="1868-12-08"),
        row("801", "小説一", "NDC 913", "新字新仮名", "000021", "徳冨", "蘆花", "著者", "1927-09-18", birth="1868-12-08"),
        row("802", "小説二", "NDC 913", "新字新仮名", "000021", "徳冨", "蘆花", "著者", "1927-09-18", birth="1868-12-08"),
    ]  # fmt: skip
    listed = aozora.read_work_list(list_zip(rows))
    aliases = aozora.person_aliases(listed)
    selected = aozora.select_works(listed, CONF)
    assert [w.work_id for w in selected] == ["800"]
    persons = {p["person_id"]: p for p in aozora.person_rows(selected, aliases, listed)}
    assert set(persons) == {"aozora:person:000020", "aozora:person:000021"}
    assert persons["aozora:person:000020"]["canonical_id"] == "aozora:person:000021"


def test_ingest_removes_text_that_is_no_longer_storable(settings_file):
    """前の判定で保存した本文も、今の判定で保存できなければ消す（R2。R4 を厳しくしたときなど）。"""
    hdd = load_settings(settings_file).require_hdd()
    rows = ROWS + [
        # 翻訳：原著者は国籍不明で1956年没 → 今は R4 の確認へ（前は満了としていた）
        row(
            "700", "随想", "NDC 131", "新字新仮名", "000008", "外国", "随想家", "著者", "1956-01-01"
        ),
        row(
            "700", "随想", "NDC 131", "新字新仮名", "000009", "日本", "訳者", "翻訳者", "1940-01-01"
        ),
    ]

    def server(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("list_person_all_extended_utf8.zip"):
            return httpx.Response(200, content=list_zip(rows))
        return FakeServer()(request)

    work = {w.work_id: w for w in aozora.select_works(aozora.read_work_list(list_zip(rows)), CONF)}
    assert aozora.judge_license(work["700"])[0] == License.NEEDS_REVIEW
    stale = hdd / "raw" / "aozora" / aozora.text_rel_path(work["700"].text_url)
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(text_zip(SAMPLE_TEXT))

    logs: list[str] = []
    r = aozora.ingest(hdd, CONF, make_fetcher(hdd, server), log=logs.append)
    assert not stale.exists()
    assert r.removed == ["aozora:700 随想（NEEDS_REVIEW）"]
    assert any("本文を消しました" in line for line in logs)
    works = {w["source_work_id"]: w for w in read_rows(hdd / "clean/aozora/works.parquet")}
    assert works["700"]["raw_path"] is None
