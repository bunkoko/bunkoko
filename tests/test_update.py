"""koten update（留守のあいだの取り直し）と、augment が2つ同時に動かないための仕組み。"""

from types import SimpleNamespace

from typer.testing import CliRunner

from koten import runlock
from koten.cli import app
from koten.config import load_settings
from koten.llm import LLMError

PS = """\
    1     0 /sbin/launchd
  100     1 -zsh
  200   100 caffeinate -i uv run koten update
  201   200 uv run koten update
  202   201 /Users/x/.venv/bin/python /Users/x/.venv/bin/koten update
  300     1 caffeinate -i uv run koten augment
  301   300 uv run koten augment
  302   301 /Users/x/.venv/bin/python /Users/x/.venv/bin/koten augment
  400   100 tee -a /Users/x/koten-update.log
  500   100 less /Users/x/koten-augment.log
"""


def test_other_processes_skips_self_and_parents():
    found = runlock.other_processes(runlock.AUGMENT_COMMAND, PS, me=202)
    assert [pid for pid, _ in found] == [300, 301, 302]
    # 自分が augment なら、自分と親（uv・caffeinate）は数えない
    assert runlock.other_processes(runlock.AUGMENT_COMMAND, PS, me=302) == []


def test_lock_is_exclusive(tmp_path):
    path = runlock.lock_path(tmp_path, "augment")
    assert not runlock.is_busy(path)
    with runlock.hold(path):
        assert runlock.is_busy(path)
        try:
            with runlock.hold(path):
                raise AssertionError("2つ目のロックが取れてしまった")
        except runlock.BusyError:
            pass
    assert not runlock.is_busy(path)


def test_augment_refuses_while_an_old_augment_runs(settings_file, monkeypatch):
    s = load_settings(settings_file)
    (s.hdd_root / "chunks").mkdir(parents=True)
    (s.hdd_root / "chunks" / "aozora.parquet").write_bytes(b"x")
    monkeypatch.setattr(runlock, "other_processes", lambda *a, **k: [(302, "koten augment")])
    r = CliRunner().invoke(app, ["augment", "--settings", str(settings_file)])
    assert r.exit_code == 2 and "ほかの augment" in r.output


def _fake_steps(monkeypatch, calls, *, augment_errors=0, others=1):
    monkeypatch.setattr("koten.cli._make_fetcher", lambda s, raw: None)
    removed = ["aozora:700 題（UNKNOWN）"]
    monkeypatch.setattr(
        "koten.cli._ingest_aozora",
        lambda s, hdd, f, **k: calls.append("ingest") or SimpleNamespace(removed=removed),
    )
    monkeypatch.setattr("koten.cli.normalize", lambda src, settings: calls.append("normalize"))
    monkeypatch.setattr("koten.cli.chunk", lambda src, settings: calls.append("chunk"))
    seen = {"ps": 0, "augment": 0}

    def ps(*a, **k):
        seen["ps"] += 1
        return [(302, "koten augment")] if seen["ps"] <= others else []

    monkeypatch.setattr(runlock, "other_processes", ps)
    monkeypatch.setattr("koten.cli.time.sleep", lambda sec: calls.append(f"sleep{sec}"))
    monkeypatch.setattr("koten.augment.forget.episodes_using", lambda conn, ids: [])
    monkeypatch.setattr(
        "koten.augment.forget.forget_works",
        lambda hdd, adir, ids, apply: (
            calls.append(f"forget{sorted(ids)}") or SimpleNamespace(candidates=1, done_chunks=1)
        ),
    )

    def augment(s, hdd, **k):
        seen["augment"] += 1
        calls.append("augment")
        if seen["augment"] <= augment_errors:
            raise LLMError("止まった")

    monkeypatch.setattr("koten.cli._augment_run", augment)


def test_update_waits_for_the_running_augment(settings_file, monkeypatch):
    calls: list[str] = []
    _fake_steps(monkeypatch, calls)
    r = CliRunner().invoke(app, ["update", "--settings", str(settings_file)])
    assert r.exit_code == 0, r.output
    # 取り直し・正規化・チャンク化はすぐ。付加情報を除くのと augment は、動いていた augment のあと
    assert calls == [
        "ingest",
        "normalize",
        "chunk",
        "sleep600",
        "forget['aozora:700']",
        "augment",
    ]
    assert "終わるのを待っています" in r.output and "終わりました" in r.output


def test_update_retries_when_the_llm_stops(settings_file, monkeypatch):
    calls: list[str] = []
    _fake_steps(monkeypatch, calls, augment_errors=2, others=0)
    r = CliRunner().invoke(app, ["update", "--settings", str(settings_file)])
    assert r.exit_code == 0, r.output
    assert calls[-5:] == ["augment", "sleep600", "augment", "sleep600", "augment"]

    calls.clear()
    _fake_steps(monkeypatch, calls, augment_errors=9, others=0)
    r = CliRunner().invoke(app, ["update", "--retries", "1", "--settings", str(settings_file)])
    assert r.exit_code == 1 and "Ollama を確かめて" in r.output


def test_only_one_update_at_a_time(settings_file, monkeypatch):
    calls: list[str] = []
    _fake_steps(monkeypatch, calls, others=0)
    s = load_settings(settings_file)
    with runlock.hold(runlock.lock_path(s.ssd_root, "update")):
        r = CliRunner().invoke(app, ["update", "--settings", str(settings_file)])
    assert r.exit_code == 2 and "ほかの koten update" in r.output and calls == []
