"""guard_customer_data.py（PreToolUse フック）のテスト。

案件フォルダは一時ディレクトリの中に作った架空の HOME に置く。実在の顧客データは使わない。
`python3 -m unittest discover -s tests/guard` で実行できる（pytest でも動く）。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "guard_customer_data.py"
JOBS = "fukugyo-c-jobs"


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.repo = self.home / "dev" / "bunkoko"
        self.jobs = self.home / JOBS
        (self.jobs / "C2610-001" / "00_input").mkdir(parents=True)
        (self.jobs / "C2610-001" / "00_input" / "answers.csv").write_text("id,q1\n1,架空\n")
        self.repo.mkdir(parents=True)
        (self.repo / "link-to-jobs").symlink_to(self.jobs)

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, tool, tool_input, raw=None):
        env = dict(os.environ, HOME=str(self.home))
        env.pop("SC_EXTRA_JOB_ROOTS", None)
        data = raw if raw is not None else json.dumps(
            {"tool_name": tool, "tool_input": tool_input, "cwd": str(self.repo)}
        )
        return subprocess.run(
            [sys.executable, str(HOOK)], input=data, capture_output=True, text=True, env=env
        )

    def assert_denied(self, tool, tool_input):
        r = self.run_hook(tool, tool_input)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.strip(), f"許可されてしまった: {tool} {tool_input}")
        out = json.loads(r.stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def assert_allowed(self, tool, tool_input):
        r = self.run_hook(tool, tool_input)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "", f"拒否されてしまった: {tool} {tool_input} -> {r.stdout}")

    # --- 拒否されるべきもの ---
    def test_read_file_in_jobs(self):
        self.assert_denied("Read", {"file_path": str(self.jobs / "C2610-001" / "00_input" / "answers.csv")})

    def test_read_with_tilde_and_upper_case(self):
        self.assert_denied("Read", {"file_path": "~/Fukugyo-C-Jobs/C2610-001/00_input/answers.csv"})

    def test_bash_cat(self):
        self.assert_denied("Bash", {"command": "cat ~/fukugyo-c-jobs/C2610-001/00_input/answers.csv"})

    def test_bash_via_symlink_in_repo(self):
        self.assert_denied("Bash", {"command": "head -n 3 link-to-jobs/C2610-001/00_input/answers.csv"})

    def test_read_via_symlink_in_repo(self):
        self.assert_denied("Read", {"file_path": str(self.repo / "link-to-jobs" / "C2610-001" / "00_input" / "answers.csv")})

    def test_bash_glob_reaching_jobs(self):
        self.assert_denied("Bash", {"command": "cat ~/fuku*/C2610-001/00_input/*.csv"})

    def test_bash_recursive_search_from_home(self):
        self.assert_denied("Bash", {"command": "grep -rn 給与 ~"})

    def test_bash_find_from_home(self):
        self.assert_denied("Bash", {"command": "find $HOME -name '*.csv'"})

    def test_python_one_liner_with_path(self):
        self.assert_denied("Bash", {"command": "python3 -c \"print(open('/x/../" + str(self.jobs)[1:] + "/a').read())\""})

    def test_grep_tool_from_home(self):
        self.assert_denied("Grep", {"pattern": "給与", "path": str(self.home)})

    def test_glob_tool_pattern(self):
        self.assert_denied("Glob", {"pattern": "**/*.csv", "path": str(self.home)})

    def test_write_into_jobs(self):
        self.assert_denied("Write", {"file_path": str(self.jobs / "note.txt"), "content": "x"})

    def test_spotlight_blocked(self):
        self.assert_denied("Bash", {"command": "mdfind -onlyin ~ 給与"})

    def test_mcp_tool_with_path(self):
        self.assert_denied("mcp__fs__read_file", {"path": "~/fukugyo-c-jobs/C2610-001/00_input/answers.csv"})

    def test_extra_root_from_env(self):
        env_root = Path(self.tmp.name) / "Volumes" / "C2610-001"
        env_root.mkdir(parents=True)
        env = dict(os.environ, HOME=str(self.home), SC_EXTRA_JOB_ROOTS=str(env_root))
        data = json.dumps({"tool_name": "Read", "tool_input": {"file_path": str(env_root / "a.csv")}, "cwd": str(self.repo)})
        r = subprocess.run([sys.executable, str(HOOK)], input=data, capture_output=True, text=True, env=env)
        self.assertIn('"deny"', r.stdout)

    # --- 許可されるべきもの ---
    def test_read_repo_file(self):
        self.assert_allowed("Read", {"file_path": str(self.repo / "README.md")})

    def test_write_doc_mentioning_jobs_dir(self):
        self.assert_allowed("Write", {"file_path": str(self.repo / "CLAUDE.md"), "content": "案件フォルダ ~/fukugyo-c-jobs は読まない"})

    def test_edit_doc_mentioning_jobs_dir(self):
        self.assert_allowed("Edit", {"file_path": str(self.repo / "docs" / "design.md"), "old_string": "a", "new_string": "~/fukugyo-c-jobs"})

    def test_bash_in_repo(self):
        self.assert_allowed("Bash", {"command": "uv run pytest tests/ && git status"})

    def test_bash_glob_not_reaching_jobs(self):
        self.assert_allowed("Bash", {"command": "ls ~/*.txt ~/dev/*"})

    def test_recursive_search_inside_repo(self):
        self.assert_allowed("Bash", {"command": "grep -rn survey_coder src/ tests/"})

    def test_grep_tool_in_repo(self):
        self.assert_allowed("Grep", {"pattern": "def main", "path": str(self.repo)})

    def test_agent_prompt_mentioning_jobs(self):
        self.assert_allowed("Agent", {"prompt": "~/fukugyo-c-jobs には触れないこと"})

    # --- 判断できない入力は止める ---
    def test_malformed_input_fails_closed(self):
        r = self.run_hook("Bash", {}, raw="{not json")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
