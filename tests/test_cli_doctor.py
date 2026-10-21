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
