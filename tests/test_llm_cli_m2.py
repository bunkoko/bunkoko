import json

import httpx
import pytest
from typer.testing import CliRunner

from koten.cli import app
from koten.llm import LLMError, OpenAICompatLLM


def test_openai_compat_request_and_response():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"quotes": []}'}}]})

    llm = OpenAICompatLLM(
        "http://127.0.0.1:1234/v1/",
        "local-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert llm.complete("sys", "user", {"type": "object"}) == '{"quotes": []}'
    assert seen["url"] == "http://127.0.0.1:1234/v1/chat/completions"
    body = seen["body"]
    assert body["model"] == "local-model"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["response_format"]["type"] == "json_schema"


def test_openai_compat_connection_error():
    def handler(req):
        raise httpx.ConnectError("refused")

    llm = OpenAICompatLLM(
        "http://x/v1", "m", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(LLMError):
        llm.complete("s", "u")


def test_augment_requires_model_name(hdd_with_chunks, settings_file):
    text = settings_file.read_text().replace('model = "local"', 'model = "要確認"')
    settings_file.write_text(text)
    r = CliRunner().invoke(app, ["augment", "--settings", str(settings_file)])
    assert r.exit_code == 2 and "モデル名" in r.output


def test_embed_before_augment_says_what_to_run(hdd_with_chunks, settings_file):
    r = CliRunner().invoke(app, ["embed", "--settings", str(settings_file)])
    assert r.exit_code == 2 and "koten augment" in r.output


def test_sudachi_index_if_installed(hdd_with_chunks):
    pytest.importorskip("sudachipy")
    from koten.augment.extract import AugmentConfig, load_themes, run_augment
    from koten.config import DEFAULT_CONFIG_DIR, REPO_ROOT
    from koten.embed.encoder import HashEncoder
    from koten.embed.run import run_embed
    from koten.index.build import build_index
    from koten.index.search import HotIndex
    from tests.conftest import FakeLLM

    s, hdd = hdd_with_chunks
    run_augment(
        hdd, REPO_ROOT / "prompts", load_themes(DEFAULT_CONFIG_DIR), FakeLLM(), AugmentConfig()
    )
    adir = hdd / "augment/fake-llm/augment_v1"
    run_embed(hdd, adir, HashEncoder(name="hash-bigram"))
    build_index(hdd, s.ssd_root, adir, "hash-bigram", tokenizer="sudachi")
    hits = HotIndex(s.ssd_root, "hash-bigram").search_bm25("比べてしまう", 3)
    assert hits and "比べ" in hits[0].row["summary_ja"] + "".join(hits[0].row["worries"])


def test_augment_model_option_and_candidates(hdd_with_chunks, settings_file, monkeypatch):
    from tests.conftest import FakeLLM

    fake = FakeLLM()
    monkeypatch.setattr(
        "koten.llm.OpenAICompatLLM.complete",
        lambda self, system, user, schema=None: fake.complete(system, user),
    )
    st = ["--settings", str(settings_file)]
    r = CliRunner().invoke(app, ["augment", "--limit", "2", "--model", "other-model", *st])
    assert r.exit_code == 0, r.output
    assert "1チャンクあたり" in r.output and "other-model" in r.output
    _, hdd = hdd_with_chunks
    assert (hdd / "augment/other-model/augment_v1").exists()
    r = CliRunner().invoke(app, ["candidates", "--model", "other-model", "--n", "1", *st])
    assert r.exit_code == 0, r.output
    assert "other-model：候補" in r.output and "要旨：" in r.output
