"""元の資料の該当箇所を探す（koten.sources.locate）。PDF を使わない部分を確かめる。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from koten.sources.locate import (
    Word,
    check_passages,
    find_page,
    fragment_link,
    highlight,
    norm,
    original_span,
)


def test_norm_unifies_width_and_drops_spaces():
    assert norm("「仕事の量」　３９．５％ ( 51.6)") == "「仕事の量」39.5%(51.6)"


def test_find_page_whole_line_then_parts():
    pages = ["表紙", "仕事の量 39.5％ [同43.2％]", "ほかのページ 39.5"]
    assert find_page(pages, "仕事の量 39.5％", "39.5") == (2, "whole")
    # 行のままは無いが（調べ役が前後を足して写したなど）、断片と数字が多く当たるページ
    pages = ["あいう", "「仕事の量」39.5％[同43.2％]が最も多く"]
    assert find_page(pages, "まとめ「仕事の量」39.5%[同43.2%]が最も多く", "39.5")[0] == 2
    # 数字が1つ当たるだけのページは選ばない
    assert find_page(["あいう", "仕事の量は 39.5 で"], "仕事の量 39.5％ [同43.2％]", "39.5") is None


def _w(text, x, y, line):
    return Word(text, x, y, x + 10 * len(text), y + 10, line)


def test_highlight_picks_the_visual_row_with_the_most_numbers():
    # pdftotext の行（line）は段組みごとに分かれるが、高さで1行にまとめ直す
    words = [
        _w("20～29歳", 0, 100, 1),
        _w("72.0", 100, 100, 2),
        _w("(", 150, 100, 3),
        _w("51.6)", 160, 100, 3),
        _w("30～39歳", 0, 120, 1),
        _w("69.0", 100, 120, 2),
        _w("(", 150, 120, 3),
        _w("43.0)", 160, 120, 3),
    ]
    fills, marks = highlight(words, "20～29歳 100.0 72.0 (100.0) ( 51.6)", "51.6")
    assert len(fills) == 1 and fills[0][1] < 105 and fills[0][3] < 115  # 20代の行だけ
    assert len(marks) == 1 and marks[0][0] >= 158  # 数字を赤く囲む
    # 写した行がそのままあれば、その行
    fills, _ = highlight(words, "30～39歳69.0", "69.0")
    assert len(fills) == 1 and fills[0][1] > 115
    # 数字も行も当たらなければ印を付けない
    assert highlight(words, "まったく別の文です", "99.9") == ([], [])


def test_original_span_and_fragment_link_use_the_page_characters():
    text = "読書量が減っている理由は「情報機器で時間が取られる」が４３．６％"
    span = original_span(text, "情報機器で時間が取られる」が43.6%")
    assert span.startswith("情報機器で時間が取られる")
    link = fragment_link(
        "https://example.org/a#x", "This is the main result of the study here and more"
    )
    assert link.startswith("https://example.org/a#:~:text=")
    assert unquote(link.split("text=")[1]) == "This is the main result of the study"


def test_check_passages_drops_sentences_not_in_the_article(tmp_path):
    page = tmp_path / "a.html"
    page.write_text(
        "<html><body><p>Workers with   high demands detached less (r = −0.28).</p></body></html>",
        "utf-8",
    )
    d = {
        "landing_url": "https://example.org/article",
        "passages": [
            {
                "role": "claim",
                "text": "Workers with high demands detached less (r = −0.28).",
                "mark": [],
            },
            {"role": "limits", "text": "This sentence was invented.", "mark": []},
        ],
    }
    dropped = check_passages(d, lambda url, rel: Path(page))
    assert [p["role"] for p in d["passages"]] == ["claim"] and dropped
    assert d["jump"].startswith("https://example.org/article#:~:text=Workers")
