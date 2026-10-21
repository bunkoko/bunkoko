"""guard_customer_data.py（PreToolUse フック）のテスト。

案件フォルダは一時ディレクトリの中に作った架空の HOME に置く。実在の顧客データは使わない。
`python3 -m unittest discover -s tests/guard` で実行できる（pytest でも動く）。
フックを置き換える前に新版を試すときは、環境変数 GUARD_HOOK に新版のパスを入れて実行する。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(
    os.environ.get("GUARD_HOOK")
    or Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "guard_customer_data.py"
).resolve()
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

    def run_hook(self, tool, tool_input, raw=None, cwd=None):
        env = dict(os.environ, HOME=str(self.home))
        env.pop("SC_EXTRA_JOB_ROOTS", None)
        data = raw if raw is not None else json.dumps(
            {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd or self.repo)}
        )
        return subprocess.run(
            [sys.executable, str(HOOK)], input=data, capture_output=True, text=True, env=env
        )

    def assert_denied(self, tool, tool_input, cwd=None):
        r = self.run_hook(tool, tool_input, cwd=cwd)
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

    def test_bash_description_is_checked_too(self):
        # コマンド系は全文を調べる
        self.assert_denied("Bash", {"command": "ls", "description": "~/fukugyo-c-jobs を確認"})

    def test_glob_tool_tilde_pattern(self):
        self.assert_denied("Glob", {"pattern": "~/fuku*/**/*.csv"})

    def test_grep_tool_without_path_from_home(self):
        self.assert_denied("Grep", {"pattern": "給与"}, cwd=self.home)

    def test_mcp_camel_case_path_key(self):
        self.assert_denied("mcp__fs__read_file", {"filePath": str(self.jobs / "C2610-001" / "a.csv")})

    def test_mcp_path_key_in_nested_list(self):
        self.assert_denied("mcp__fs__read_many", {"options": {"paths": ["README.md", "~/fukugyo-c-jobs/C2610-001"]}})

    def test_mcp_file_uri(self):
        self.assert_denied("mcp__ide__open", {"uri": "file://" + str(self.jobs / "C2610-001" / "a.csv")})

    def test_mcp_shell_command_key(self):
        self.assert_denied("mcp__shell__run", {"command": "cat ~/fukugyo-c-jobs/C2610-001/00_input/answers.csv"})

    def test_mcp_shell_command_key_recursive(self):
        self.assert_denied("mcp__shell__run", {"cmd": "grep -rn 給与 ~"})

    def test_other_tool_single_path_value(self):
        self.assert_denied("StructuredOutput", {"result": "~/fukugyo-c-jobs/C2610-001/00_input/answers.csv"})

    def test_other_tool_single_path_with_space(self):
        self.assert_denied("mcp__x__open", {"target": "~/fukugyo-c-jobs/C2610-001/受領 データ.csv"})

    def test_other_tool_single_relative_token(self):
        self.assert_denied("mcp__x__open", {"target": "../../fukugyo-c-jobs/C2610-001"})

    def test_other_tool_single_path_via_symlink(self):
        self.assert_denied("mcp__x__open", {"target": "link-to-jobs/C2610-001/00_input/answers.csv"})

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

    def test_mcp_doc_update_mentioning_jobs(self):
        # 文章の中にフォルダ名があるだけでは止めない
        content = "## ディレクトリ構成\n\n案件フォルダは ~/fukugyo-c-jobs/ に置きます。\n\n~/fukugyo-c-jobs/C2610-001/"
        self.assert_allowed(
            "mcp__Claude_Docs__update",
            {"ref": {"object": "node", "id": "af7ba491"}, "payload": {"ops": [{"op": "insert", "source": {"from": {"content": content}}}]}},
        )

    def test_mcp_one_line_prose_mentioning_jobs(self):
        self.assert_allowed("mcp__x__note", {"text": "案件フォルダ ~/fukugyo-c-jobs/ は読まない"})

    def test_mcp_prose_in_non_path_key(self):
        self.assert_allowed("mcp__x__set", {"profile": "fukugyo-c-jobs の運用ルール"})

    def test_structured_output_prose(self):
        self.assert_allowed("StructuredOutput", {"summary": "~/fukugyo-c-jobs は読んでいません", "ok": True})

    def test_mcp_path_key_in_repo(self):
        self.assert_allowed("mcp__fs__read_file", {"path": str(self.repo / "docs" / "design.md")})

    def test_glob_tool_in_repo(self):
        self.assert_allowed("Glob", {"pattern": "**/*.py", "path": str(self.repo)})

    # --- 判断できない入力は止める ---
    def test_malformed_input_fails_closed(self):
        r = self.run_hook("Bash", {}, raw="{not json")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
