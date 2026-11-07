"""スマホで点を付けるページ（koten eval page・merge）。"""

import json

from typer.testing import CliRunner

from koten.cli import app
from koten.eval.phone import build_page, merge_grades, page_items, row_key
from koten.eval.retrieval import read_rows, write_eval


def _rows():
    base = {
        "title": "坊っちゃん",
        "authors": "夏目漱石",
        "section": "八",
        "summary_ja": "要旨",
        "llm_reason": "LLMの理由",
        "llm_grader": "haiku/grader_v2",
    }
    return [
        {
            **base,
            "worry_id": "W0001",
            "worry_text": "同期の昇進を素直に喜べない",
            "candidate_id": "aozora:000148#3/q1",
            "quote_text": "一節</script><b>",
            "context_text": "前。一節</script><b>後。",
            "grade": "",
            "llm_grade": "1",
        },
        {
            **base,
            "worry_id": "W0001",
            "worry_text": "同期の昇進を素直に喜べない",
            "candidate_id": "aozora:000148#9/q2",
            "quote_text": "二つ目",
            "context_text": "",
            "grade": "0",
            "llm_grade": "0",
        },
    ]


def test_row_key_is_safe_and_stable():
    k = row_key("W0001", "aozora:000148#3/q1")
    assert k.startswith("W0001-") and all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in k)
    assert k == row_key("W0001", "aozora:000148#3/q1") != row_key("W0002", "aozora:000148#3/q1")


def test_page_has_no_llm_grades_and_cannot_break_out_of_the_script():
    html = build_page(_rows())
    assert "LLMの理由" not in html and "haiku/grader_v2" not in html and '"llm' not in html
    assert (
        "一節</script>" not in html and "\\u003c/script\\u003e" in html
    )  # 本文で script を閉じない
    assert "/*__ITEMS__*/" not in html and "const SAMPLE = false" in html
    items = page_items(_rows())
    assert [i["g"] for i in items] == ["", "0"]  # 人が付けた点だけ入れる
    assert items[0]["c"].startswith("前。") and set(items[0]) == set("k w wt t a s q c m g".split())
    assert "const SAMPLE = true" in build_page(_rows(), sample=True)


def test_merge_grades(tmp_path):
    path = tmp_path / "eval.csv"
    write_eval(path, _rows())
    a, b = (row_key(r["worry_id"], r["candidate_id"]) for r in _rows())
    changed, matched, unknown = merge_grades(path, {a: "1", b: "0", "W9-x": "1", "W0001-y": "?"})
    assert (changed, matched, unknown) == (1, 2, ["W9-x"])
    rows = read_rows(path)
    assert [r["grade"] for r in rows] == ["1", "0"]
    assert [r["llm_grade"] for r in rows] == ["1", "0"]  # ほかの列はそのまま
    assert merge_grades(path, {a: "0"})[0] == 1 and read_rows(path)[0]["grade"] == "0"  # 新しい点


def test_page_and_merge_commands(tmp_path):
    path = tmp_path / "eval.csv"
    write_eval(path, _rows())
    out = tmp_path / "page.html"
    r = CliRunner().invoke(app, ["eval", "page", "--file", str(path), "--out", str(out)])
    assert r.exit_code == 0 and "悩み 1 件・2 行" in r.output, r.output
    assert out.read_text("utf-8").startswith("<!DOCTYPE html>")
    grades = tmp_path / "grades.json"
    grades.write_text(json.dumps({row_key("W0001", "aozora:000148#3/q1"): "1"}), "utf-8")
    r = CliRunner().invoke(app, ["eval", "merge", "--from", str(grades), "--file", str(path)])
    assert r.exit_code == 0 and "1 行の grade" in r.output, r.output
    assert read_rows(path)[0]["grade"] == "1"
    missing = CliRunner().invoke(app, ["eval", "page", "--file", str(tmp_path / "none.csv")])
    assert missing.exit_code == 2 and "eval export" in missing.output
