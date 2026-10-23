"""テスト用のフィクスチャ。青空文庫の作品CSVとテキストzipを小さく作る（本文は架空）。"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import pytest

COLUMNS = [
    "作品ID",
    "作品名",
    "作品名読み",
    "ソート用読み",
    "副題",
    "副題読み",
    "原題",
    "初出",
    "分類番号",
    "文字遣い種別",
    "作品著作権フラグ",
    "公開日",
    "最終更新日",
    "図書カードURL",
    "人物ID",
    "姓",
    "名",
    "姓読み",
    "名読み",
    "姓読みソート用",
    "名読みソート用",
    "姓ローマ字",
    "名ローマ字",
    "役割フラグ",
    "生年月日",
    "没年月日",
    "人物著作権フラグ",
    "底本名1",
    "底本出版社名1",
    "底本初版発行年1",
    "テキストファイルURL",
]

SITE = "https://www.aozora.gr.jp/"


def row(
    work_id,
    title,
    ndc,
    kana,
    person_id,
    sei,
    mei,
    role,
    death,
    *,
    work_flag="なし",
    person_flag="なし",
    subtitle="",
):
    return {
        "作品ID": work_id,
        "作品名": title,
        "副題": subtitle,
        "分類番号": ndc,
        "文字遣い種別": kana,
        "作品著作権フラグ": work_flag,
        "図書カードURL": f"{SITE}cards/{person_id}/card{work_id}.html",
        "人物ID": person_id,
        "姓": sei,
        "名": mei,
        "役割フラグ": role,
        "没年月日": death,
        "人物著作権フラグ": person_flag,
        "底本名1": "架空の底本",
        "底本出版社名1": "架空書房",
        "底本初版発行年1": "1950（昭和25）年",
        "テキストファイルURL": f"{SITE}cards/{person_id}/files/{work_id}_ruby_1.zip",
    }


def list_zip(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in COLUMNS})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("list_person_all_extended_utf8.csv", ("﻿" + buf.getvalue()).encode())
    return out.getvalue()


def text_zip(text: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("work.txt", text.replace("\n", "\r\n").encode("cp932"))
    return out.getvalue()


SAMPLE_TEXT = """人生のノート
架空　太郎

-------------------------------------------------------
【テキスト中に現れる記号について】

《》：ルビ
（例）嫉妬《しっと》

-------------------------------------------------------

［＃３字下げ］嫉妬について［＃「嫉妬について」は中見出し］

　｜嫉妬《しっと》は人を《ひと》比べることから生まれる。他人の幸福を見て、自分の不足を数えるとき、心は落ち着きを失う。
　※［＃「てへん＋劣」、第3水準1-84-77］るものは何もないのに、比べる癖だけが残る。［＃「比べる癖」に傍点］
　昔の人はこう書いた／＼。

［＃改ページ］

［＃３字下げ］孤独について［＃「孤独について」は中見出し］

　孤独は山にあるのではなく、街にある。※［＃「さんずい＋（夕／匕）」、U+23D7B、12-3］
　人の間にいて、ひとりであることを知る。

底本：「架空の底本」架空書房
　　　1950（昭和25）年1月1日発行
入力：架空
校正：架空
"""


@pytest.fixture(autouse=True)
def _ignore_repo_dotenv(monkeypatch):
    """テストは手元の .env（APIキー・連絡先）に左右されないようにする。"""
    monkeypatch.setattr("koten.config.load_dotenv", lambda *a, **k: False)


@pytest.fixture
def settings_file(tmp_path: Path) -> Path:
    hdd = tmp_path / "hdd" / "koten"
    hdd.parent.mkdir()
    p = tmp_path / "settings.toml"
    p.write_text(
        f"""
[paths]
hdd_root = "{hdd}"
ssd_root = "{tmp_path / "ssd"}"

[voicevox]
endpoint = "http://127.0.0.1:9"

[llm.augment]
provider = "openai_compatible"
endpoint = "http://127.0.0.1:9/v1"
model = "local"

[llm.script]
provider = "anthropic"
model = "claude-sonnet-5-5"

[llm.judge]
provider = "anthropic"
model = "claude-haiku-5-5"

[embed]
model = "intfloat/multilingual-e5-large"

[rate_limit]
per_host_per_sec = 1000

[chunk]
min_chars = 20
max_chars = 80
""",
        encoding="utf-8",
    )
    return p


class FakeLLM:
    """本文の最初の「20字以上の文」を一節として返す偽のLLM。作り話の一節も1つ混ぜる。"""

    name = "fake-llm"

    def __init__(self, broken_first: int = 0, interrupt_at: int | None = None):
        self.calls = 0
        self.broken_first = broken_first
        self.interrupt_at = interrupt_at

    def complete(self, system, user, schema=None):
        import json
        import re

        self.calls += 1
        if self.interrupt_at is not None and self.calls == self.interrupt_at:
            raise KeyboardInterrupt
        if self.calls <= self.broken_first:
            return "すみません、JSONではありません"
        body = user.split("本文：", 1)[1]
        sentences = [s.strip() + "。" for s in re.split("。", body) if s.strip()]
        real = next((s for s in sentences if 20 <= len(s) <= 120), None)
        quotes = [
            {
                "quote_text": "人生とは比べないことである。だれもが知っている真理だ。",
                "summary_ja": "作り話",
                "themes": ["嫉妬・比較"],
                "worries": ["比べてしまう"],
                "quotability": 0.9,
                "caution": False,
                "caution_reason": "",
            }
        ]
        if real:
            quotes.insert(
                0,
                {
                    "quote_text": real,
                    "summary_ja": "他人と比べると心が乱れる",
                    "themes": ["嫉妬・比較", "存在しないテーマ"],
                    "worries": ["同期の昇進を喜べない", "SNSで人と比べる", "x" * 30],
                    "quotability": 1.7,
                    "caution": False,
                    "caution_reason": "",
                },
            )
        return "```json\n" + json.dumps({"quotes": quotes}, ensure_ascii=False) + "\n```"


@pytest.fixture
def hdd_with_chunks(settings_file):
    """フィクスチャの青空文庫2作品を ingest → normalize → chunk まで通した HDD。"""
    from koten.chunk.chunker import chunk_source
    from koten.config import load_settings
    from koten.ingest import aozora
    from koten.normalize.aozora import normalize
    from tests.test_ingest_aozora import CONF, FakeServer, make_fetcher

    s = load_settings(settings_file)
    hdd = s.require_hdd()
    aozora.ingest(hdd, CONF, make_fetcher(hdd, FakeServer()))
    normalize(hdd)
    chunk_source(hdd, "aozora", 20, 80)
    return s, hdd
