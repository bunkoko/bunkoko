"""検索の評価の grader（2値・Claude Code の Haiku）と、人の点との一致。"""

import json
import re
import subprocess

import pytest
from typer.testing import CliRunner

from koten.cli import app
from koten.config import REPO_ROOT
from koten.eval.grader import agreement, cohen_kappa, grade_file
from koten.eval.retrieval import parse_grade, read_eval, read_rows, write_eval
from koten.llm import ClaudeCLILLM, LLMError, make_llm

PROMPTS = REPO_ROOT / "prompts"


def fake_run(stdout="", returncode=0, record=None):
    def run(argv, **kw):
        if record is not None:
            record.append((argv, kw))
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="失敗の理由")

    return run


def test_claude_cli_arguments_and_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    calls = []
    out = json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": '{"ok": true}',
            "total_cost_usd": 0.0012,
            "duration_ms": 900,
        }
    )
    monkeypatch.setattr("koten.llm.subprocess.run", fake_run(out, record=calls))
    llm = make_llm({"provider": "claude_cli", "model": "haiku"})
    assert isinstance(llm, ClaudeCLILLM) and llm.name == "claude-cli:haiku"
    assert llm.complete("前提", "質問", {"type": "object"}) == '{"ok": true}'
    argv, kw = calls[0]
    assert argv[:2] == ["claude", "-p"] and argv[-1] == "質問"
    for flag, value in [
        ("--model", "haiku"),
        ("--output-format", "json"),
        ("--system-prompt", "前提"),
        ("--tools", ""),
    ]:
        assert argv[argv.index(flag) + 1] == value
    assert "--no-session-persistence" in argv and "--safe-mode" in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}
    assert "--bare" not in argv  # --bare は API キーでしか認証しない
    # サブスクリプションで動かすため、API キーは子プロセスに渡さない
    assert "ANTHROPIC_API_KEY" not in kw["env"]
    assert kw["cwd"] != str(REPO_ROOT)
    assert llm.stats["calls"] == 1 and llm.stats["cost_micro_usd"] == 1200


def test_claude_cli_structured_output(monkeypatch):
    out = json.dumps({"is_error": False, "result": "", "structured_output": {"grades": []}})
    monkeypatch.setattr("koten.llm.subprocess.run", fake_run(out))
    assert json.loads(ClaudeCLILLM().complete("s", "u", {})) == {"grades": []}


@pytest.mark.parametrize(
    "run,match",
    [
        (fake_run("", returncode=1), "終了コード 1"),
        (fake_run("JSONではない"), "JSON ではありません"),
        (fake_run(json.dumps({"is_error": True, "result": "上限"})), "上限"),
    ],
)
def test_claude_cli_errors(monkeypatch, run, match):
    monkeypatch.setattr("koten.llm.subprocess.run", run)
    with pytest.raises(LLMError, match=match):
        ClaudeCLILLM().complete("s", "u")


def test_claude_cli_missing_and_timeout(monkeypatch):
    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr("koten.llm.subprocess.run", missing)
    with pytest.raises(LLMError, match="見つかりません"):
        ClaudeCLILLM().complete("s", "u")

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr("koten.llm.subprocess.run", slow)
    with pytest.raises(LLMError, match="終わりません"):
        ClaudeCLILLM(timeout_sec=1).complete("s", "u")


def test_parse_grade_binary():
    assert [parse_grade(v) for v in ("1", "0", "2", "", "x", " 1 ")] == [1, 0, 1, None, None, 1]


def _eval_csv(path, rows):
    cols = {
        "worry_text": "悩み",
        "title": "作品",
        "authors": "甲",
        "section": "",
        "quote_text": "一節",
        "summary_ja": "要旨",
        "grade": "",
        "llm_grade": "",
        "llm_reason": "",
        "llm_grader": "",
    }
    write_eval(path, [{**cols, **r} for r in rows])


class FakeGrader:
    """候補IDが「y」で始まれば合う。drop に入れた候補は1回目に答えない。"""

    name = "fake-grader"

    def __init__(self, drop=()):
        self.calls = 0
        self.drop = set(drop)

    def complete(self, system, user, schema=None):
        self.calls += 1
        ids = re.findall(r"candidate_id: (\S+)", user)
        grades = [
            {"candidate_id": c, "relevant": c.startswith("y"), "reason": "理由"}
            for c in ids
            if not (self.calls == 1 and c in self.drop)
        ]
        return json.dumps({"grades": grades}, ensure_ascii=False)


def test_grade_file_batches_per_worry_and_resumes(tmp_path):
    path = tmp_path / "eval.csv"
    _eval_csv(
        path,
        [
            {"worry_id": "W1", "candidate_id": "y1"},
            {"worry_id": "W1", "candidate_id": "n1"},
            {"worry_id": "W2", "candidate_id": "y2"},
            {"worry_id": "W2", "candidate_id": "n2", "llm_grade": "1"},  # 判定済みは飛ばす
        ],
    )
    llm = FakeGrader(drop={"n1"})
    r = grade_file(path, llm, PROMPTS)
    assert llm.calls == 3  # W1 は答えの抜けで1回やり直し、W2 は1回
    assert (r.graded, r.skipped, r.missing) == (3, 1, [])
    rows = {x["candidate_id"]: x for x in read_rows(path)}
    assert [rows[c]["llm_grade"] for c in ("y1", "n1", "y2", "n2")] == ["1", "0", "1", "1"]
    assert rows["y1"]["llm_grader"] == "fake-grader/grader_v1" and rows["y1"]["llm_reason"]
    assert read_eval(path, "llm_grade") == {"W1": {"y1": 1, "n1": 0}, "W2": {"y2": 1, "n2": 1}}
    again = FakeGrader()
    assert grade_file(path, again, PROMPTS).graded == 0 and again.calls == 0
    assert grade_file(path, again, PROMPTS, redo=True, worry_ids={"W2"}).graded == 2
    assert rows["n2"]["llm_grade"] == "1" and read_eval(path, "llm_grade")["W2"]["n2"] == 0


def test_cohen_kappa():
    assert cohen_kappa([(1, 1), (0, 0), (1, 1), (0, 0)]) == pytest.approx(1.0)
    assert cohen_kappa([(1, 0), (0, 1)]) == pytest.approx(-1.0)
    # 一致率 0.8、どちらも半分が「合う」→ 偶然の一致 0.5 → κ = (0.8-0.5)/(1-0.5) = 0.6
    pairs = [(1, 1)] * 4 + [(0, 0)] * 4 + [(1, 0), (0, 1)]
    assert cohen_kappa(pairs) == pytest.approx(0.6)
    assert cohen_kappa([]) is None


def test_agreement_and_cli(tmp_path, settings_file, monkeypatch):
    path = tmp_path / "eval.csv"
    _eval_csv(
        path,
        [
            {"worry_id": "W1", "candidate_id": "a", "grade": "1", "llm_grade": "1"},
            {"worry_id": "W1", "candidate_id": "b", "grade": "0", "llm_grade": "0"},
            {
                "worry_id": "W1",
                "candidate_id": "c",
                "grade": "1",
                "llm_grade": "0",
                "llm_reason": "関係が薄い",
            },
            {"worry_id": "W1", "candidate_id": "d", "grade": "", "llm_grade": "1"},
        ],
    )
    a = agreement(path)
    assert (a.n, a.both_yes, a.both_no, a.human_only, a.llm_only) == (3, 1, 1, 1, 0)
    assert a.accuracy == pytest.approx(2 / 3) and a.llm_precision == 1.0 and a.llm_recall == 0.5
    st = ["--settings", str(settings_file)]
    r = CliRunner().invoke(app, ["eval", "agreement", "--file", str(path), *st])
    assert r.exit_code == 0, r.output
    assert "一致率 67%" in r.output and "[人だけ「合う」]" in r.output and "関係が薄い" in r.output

    monkeypatch.setattr("koten.llm.make_llm", lambda conf, model=None: FakeGrader())
    r = CliRunner().invoke(app, ["eval", "grade", "--file", str(path), *st])
    assert r.exit_code == 0, r.output
    assert "判定 0 件" in r.output and "判定済みで飛ばした 4 件" in r.output


def test_doctor_checks_claude_cli(settings_file, monkeypatch):
    from koten import doctor
    from koten.config import load_settings

    with settings_file.open("a", encoding="utf-8") as f:
        f.write('\n[llm.grader]\nprovider = "claude_cli"\nmodel = "haiku"\n')
    s = load_settings(settings_file)
    monkeypatch.setattr("koten.doctor.shutil.which", lambda c: None)
    assert doctor.check_llm(s, "grader", None).status == doctor.NG
    monkeypatch.setattr("koten.doctor.shutil.which", lambda c: "/usr/local/bin/claude")
    c = doctor.check_llm(s, "grader", None)
    assert c.status == doctor.OK and "haiku" in c.detail
    assert "エフォート xhigh" in doctor.check_llm(load_settings(), "judge", None).detail


def test_claude_cli_effort(monkeypatch):
    calls = []
    out = json.dumps({"is_error": False, "result": "{}"})
    monkeypatch.setattr("koten.llm.subprocess.run", fake_run(out, record=calls))
    llm = make_llm({"provider": "claude_cli", "model": "opus", "effort": "xhigh"})
    assert llm.name == "claude-cli:opus@xhigh"
    llm.complete("s", "u")
    argv = calls[0][0]
    assert argv[argv.index("--effort") + 1] == "xhigh"
    assert "--effort" not in ClaudeCLILLM().args("s", "u", None)  # 指定しなければ付けない
    with pytest.raises(ValueError, match="effort"):
        ClaudeCLILLM(effort="超高")


def test_settings_use_opus_xhigh_for_script_and_judge():
    from koten.config import load_settings

    s = load_settings()
    for role in ("script", "judge"):
        conf = s.section(f"llm.{role}")
        assert (conf["provider"], conf["model"], conf["effort"]) == ("claude_cli", "opus", "xhigh")
