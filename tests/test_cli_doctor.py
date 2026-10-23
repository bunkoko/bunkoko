import httpx
from typer.testing import CliRunner

from koten import doctor
from koten.cli import app
from koten.config import load_settings


def test_doctor_reports_all_targets(settings_file, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["doctor", "--settings", str(settings_file)])
    assert result.exit_code == 0, result.output
    for name in ("HDD", "SSD", "VOICEVOX", "LLM（augment）", "LLM（script）", "埋め込みモデル"):
        assert name in result.output
    assert "空き" in result.output


def test_doctor_with_services_up(settings_file, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/version":
            return httpx.Response(200, text='"0.21.1"')
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "local"}]})
        return httpx.Response(404)

    checks = {
        c.name: c
        for c in doctor.run_checks(
            load_settings(settings_file), httpx.Client(transport=httpx.MockTransport(handler))
        )
    }
    assert checks["VOICEVOX"].status == doctor.OK and "0.21.1" in checks["VOICEVOX"].detail
    assert checks["LLM（augment）"].status == doctor.OK
    assert checks["LLM（script）"].status == doctor.OK
    assert checks["HDD"].status == doctor.OK


def test_hdd_missing_stops_ingest(tmp_path, settings_file):
    text = settings_file.read_text().replace(str(tmp_path / "hdd"), str(tmp_path / "nohdd"))
    settings_file.write_text(text)
    result = CliRunner().invoke(app, ["ingest", "aozora", "--settings", str(settings_file)])
    assert result.exit_code == 2
    assert "HDDが未接続" in result.output


def test_unsupported_source(settings_file):
    result = CliRunner().invoke(app, ["ingest", "ndl", "--settings", str(settings_file)])
    assert result.exit_code == 2


def test_normalize_before_ingest_says_what_to_run(settings_file):
    result = CliRunner().invoke(app, ["normalize", "aozora", "--settings", str(settings_file)])
    assert result.exit_code == 2
    assert "works.parquet がありません" in result.output
    assert "koten ingest aozora" in result.output


def test_chunk_before_normalize_says_what_to_run(settings_file):
    result = CliRunner().invoke(app, ["chunk", "aozora", "--settings", str(settings_file)])
    assert result.exit_code == 2
    assert "koten normalize aozora" in result.output


def test_python_arch_check():
    assert doctor.check_python("Darwin", "x86_64").status == doctor.NG
    assert "Apple シリコン" in doctor.check_python("Darwin", "x86_64").detail
    assert doctor.check_python("Darwin", "arm64").status == doctor.OK
    assert doctor.check_python("Linux", "x86_64").status == doctor.OK


def test_ingest_refuses_placeholder_contact(settings_file):
    with settings_file.open("a", encoding="utf-8") as f:
        f.write(
            '\n[http]\nuser_agent = "koten-videos/0.1 (+contact: set-your-email@example.com)"\n'
        )
    result = CliRunner().invoke(app, ["ingest", "aozora", "--settings", str(settings_file)])
    assert result.exit_code == 2
    assert "連絡先が仮の値" in result.output
