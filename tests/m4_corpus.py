"""M4 のテスト用：11作品・9人の著者の小さな候補表と索引、悩み、偽のLLM（本文は架空）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from koten.augment.extract import CANDIDATES_SCHEMA, DONE_SCHEMA
from koten.db import connect, load_stats
from koten.embed.encoder import HashEncoder
from koten.embed.run import run_embed
from koten.index.build import build_index
from koten.ingest.aozora import WORKS_SCHEMA
from koten.parquet import write_rows

MODEL = "hash-bigram"

# (作品ID, 作品名, 著者ID, 著者名, [(要旨, テーマ, 悩み)])
WORKS = [
    (
        "001",
        "比べる心",
        "p1",
        "甲",
        [
            ("他人と比べると心が乱れる", "嫉妬・比較", "同期の昇進を喜べない"),
            ("嫉妬は自分を低くする", "嫉妬・比較", "人と比べて落ち込む"),
            ("比べるのをやめると楽になる", "嫉妬・比較", "SNSで比べてしまう"),
            ("人の幸福を祝える人は幸福だ", "嫉妬・比較", "人の成功を祝えない"),
            ("比較は不幸の始まり", "嫉妬・比較", "人と比べて落ち込む"),
        ],
    ),
    (
        "002",
        "妬みの話",
        "p2",
        "乙",
        [
            ("妬みは相手を引き下げようとする", "嫉妬・比較", "同期の昇進を喜べない"),
            ("人の幸福を喜べるのは強さだ", "嫉妬・比較", "人の成功を祝えない"),
        ],
    ),
    (
        "003",
        "孤独の書",
        "p3",
        "丙",
        [
            ("孤独は街の中にある", "孤独", "休日が孤独だ"),
            ("ひとりの時間が人を育てる", "孤独", "一人暮らしで話さない"),
        ],
    ),
    (
        "004",
        "勤めの記",
        "p4",
        "丁",
        [
            ("仕事の量に追われると心が痩せる", "仕事の量", "仕事が終わらない"),
            ("働くことは自分を知ること", "働く意味", "働く意味がわからない"),
        ],
    ),
    (
        "005",
        "失敗について",
        "p5",
        "戊",
        [
            ("失敗は次への材料になる", "失敗・責任", "仕事のミスを引きずる"),
            ("比べず自分の歩幅で進む", "嫉妬・比較", "同期と比べてしまう"),
        ],
    ),
    (
        "006",
        "競う心",
        "p6",
        "己",
        [
            ("競い合うことで人は伸びる", "嫉妬・比較", "同期の昇進を喜べない"),
            ("負けた悔しさを力に変える", "嫉妬・比較", "人と比べて落ち込む"),
        ],
    ),
    (
        "007",
        "続・比べる心",
        "p1",
        "甲",
        [
            ("比べる相手は昨日の自分", "嫉妬・比較", "同期と比べてしまう"),
        ],
    ),
    (
        "009",
        "見栄の話",
        "p7",
        "庚",
        [("見栄を張ると比べる心が強まる", "嫉妬・比較", "SNSで比べてしまう")],
    ),
    (
        "010",
        "うらやむ心",
        "p8",
        "辛",
        [("うらやむ心は努力の芽", "嫉妬・比較", "同期の昇進を喜べない")],
    ),
    (
        "011",
        "自分の道",
        "p9",
        "壬",
        [("自分の道を行けば比べずにすむ", "嫉妬・比較", "同期と比べてしまう")],
    ),
    (
        "008",
        "友の書",
        "p3",
        "丙",
        [
            ("友は数より深さ", "孤独", "友達が少ない"),
            ("孤独を恐れず人と向き合う", "孤独", "休日が孤独だ"),
        ],
    ),
]

WORRIES = [
    ("W0001", "同期の昇進を素直に喜べない", ["嫉妬・比較"], 1000),
    ("W0003", "SNSで他人と比べて落ち込んでしまう", ["嫉妬・比較"], 2000),
    ("W0004", "人の成功を素直に祝えない", ["嫉妬・比較"], 500),
    ("W0014", "休日が孤独でつらい", ["孤独"], 900),
    ("W0015", "一人暮らしで誰とも話さない", ["孤独"], 500),
    ("W0016", "友達が少なくて寂しい", ["孤独"], 400),
]


def build(hdd: Path, ssd: Path, config_dir: Path) -> None:
    works, cands, done = [], [], []
    for wid, title, pid, author, items in WORKS:
        work_id = f"aozora:{wid}"
        works.append(
            {
                "work_id": work_id,
                "source": "aozora",
                "source_work_id": wid,
                "title": title,
                "subtitle": "",
                "author_ids": [f"aozora:person:{pid}"],
                "author_names": [author],
                "translator_ids": [],
                "ndc": "NDC 159",
                "kana_type": "新字新仮名",
                "lang": "ja",
                "base_book": "",
                "url": "",
                "text_url": "",
                "license": "PD_JP",
                "license_reason": "",
                "review_required": False,
                "raw_path": None,
            }
        )
        for i, (summary, theme, worry) in enumerate(items, start=1):
            chunk_id = f"{work_id}#{i}"
            quote = f"{summary}ということを、{title}はこう語る。"
            cands.append(
                {
                    "candidate_id": f"{chunk_id}/q1",
                    "chunk_id": chunk_id,
                    "work_id": work_id,
                    "section": f"第{i}章",
                    "quote_text": quote,
                    "quote_start": 0,
                    "quote_end": len(quote),
                    "context_text": quote,
                    "summary_ja": summary,
                    "themes": [theme],
                    "worries": [worry],
                    "quotability": 0.8,
                    "caution": False,
                    "caution_reason": "",
                    "llm": "fake",
                    "prompt_ver": "augment_v1",
                }
            )
            done.append(
                {
                    "chunk_id": chunk_id,
                    "status": "ok",
                    "n_candidates": 1,
                    "dropped": "{}",
                    "error": "",
                }
            )
    write_rows(hdd / "clean/aozora/works.parquet", works, WORKS_SCHEMA)
    adir = hdd / "augment/fake/augment_v1"
    write_rows(adir / "part-00001.parquet", cands, CANDIDATES_SCHEMA)
    write_rows(adir / "done-00001.parquet", done, DONE_SCHEMA)
    run_embed(hdd, adir, HashEncoder(name=MODEL), passage_prefix="passage: ")
    build_index(hdd, ssd, adir, MODEL, tokenizer="bigram")

    conn = connect(ssd)
    load_stats(conn, config_dir / "stats.csv")
    with conn:
        for wid, text, themes, demand in WORRIES:
            conn.execute(
                "INSERT INTO worries(worry_id, text_raw, text_general, themes, audience, "
                "source_type, demand_score, cluster_id, is_representative, status) "
                "VALUES (?, ?, ?, ?, '全般', 'seed', ?, ?, 1, 'active')",
                (wid, text, text, json.dumps(themes, ensure_ascii=False), demand, wid),
            )


class FakePlanLLM:
    """検索文づくりと一節の選択を、条件を守って決まった形で返す偽のLLM。"""

    name = "fake-plan-llm"

    def __init__(self, broken_select: bool = False):
        self.calls = {"query": 0, "select": 0}
        self.broken_select = broken_select

    def complete(self, system, user, schema=None):
        if "候補：" not in user:
            self.calls["query"] += 1
            text = user.split("悩み：", 1)[1].strip()
            return json.dumps(
                {
                    "paraphrase": text,
                    "hyde": f"{text}という気持ちを和らげる",
                    "themes": ["嫉妬・比較", "未知"],
                },
                ensure_ascii=False,
            )
        self.calls["select"] += 1
        if self.broken_select:
            return '{"selected": [{"candidate_id": "存在しない"}]}'
        fmt = re.search(r"形式：(\w)", user).group(1)
        worries = re.findall(r"^- (W\d{4})：", user, flags=re.M)
        cands = re.findall(r"^- (\S+?)｜(.+?)（(.+?)）", user, flags=re.M)
        picks, works, authors = [], set(), {}
        for cid, title, author in cands:
            if fmt in "AC" and title in works:
                continue
            if fmt == "D":
                if author in authors or len(authors) >= 2:
                    continue  # 1人1つずつ、2人まで
                authors[author] = "AB"[len(authors)]
            works.add(title)
            picks.append(
                {
                    "candidate_id": cid,
                    "reason": f"{title}が悩みに響く",
                    "worry_id": worries[len(picks) % len(worries)],
                    "side": authors.get(author, "") if fmt == "D" else "",
                }
            )
            limit = {"A": 4, "B": 5, "C": 5, "D": 2}[fmt]
            if len(picks) >= limit and (fmt != "D" or len(authors) == 2):
                break
        return json.dumps(
            {
                "selected": picks,
                "question": "あなたはどちらの考えに近いですか" if fmt == "D" else "",
            },
            ensure_ascii=False,
        )
