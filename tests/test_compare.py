from typer.testing import CliRunner

from koten.augment.compare import compare_models
from koten.augment.extract import AugmentConfig, load_themes, run_augment
from koten.cli import app
from koten.config import DEFAULT_CONFIG_DIR, REPO_ROOT
from tests.conftest import FakeLLM


def _run(hdd, name, limit, **kw):
    llm = FakeLLM(**kw)
    llm.name = name
    run_augment(
        hdd,
        REPO_ROOT / "prompts",
        load_themes(DEFAULT_CONFIG_DIR),
        llm,
        AugmentConfig(),
        limit=limit,
    )


def test_compare_uses_only_common_chunks(hdd_with_chunks):
    _, hdd = hdd_with_chunks
    _run(hdd, "model-a", 4)
    _run(hdd, "model-b", 2)
    summaries, md, chunk_ids = compare_models(hdd, "augment_v1", ["model-a", "model-b"], n=10)
    assert len(chunk_ids) == 2  # model-b が処理した2チャンクだけが共通
    assert all(cid in md for cid in chunk_ids)
    a, b = summaries
    assert a.chunks == b.chunks == 2
    assert a.candidates == b.candidates  # 同じ偽LLMなので、同じチャンクなら同じ結果
    assert a.dropped == b.dropped and a.dropped["原文に無い"] == 2
    assert md.count("### model-a") == 2 and "要旨：" in md


def test_compare_cli_writes_report(hdd_with_chunks, settings_file):
    _, hdd = hdd_with_chunks
    _run(hdd, "model-a", 3)
    _run(hdd, "model-b", 3)
    st = ["--settings", str(settings_file)]
    r = CliRunner().invoke(app, ["compare", "--models", "model-a,model-b", *st])
    assert r.exit_code == 0, r.output
    assert "| model-a |" in r.output and "| model-b |" in r.output
    assert "aozora:" in r.output  # どのチャンクで比べたかを出す
    reports = list((hdd / "exports").glob("augment-compare-*.md"))
    assert len(reports) == 1 and "## " in reports[0].read_text("utf-8")
    r = CliRunner().invoke(app, ["compare", "--models", "model-a,nothing", *st])
    assert r.exit_code == 2 and "nothing" in r.output
