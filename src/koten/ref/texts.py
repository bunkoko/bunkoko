"""時代の解釈（config/renderings.csv）と昔の辞書の語釈（config/dict_entries.csv）を読む（M8a）。

どちらも、人がページの画像と照らし合わせて書き起こした、保護期間の切れた資料の本文。
- 書くのは、台帳（ref_works.csv）で使える（PD_JP など）と判定された資料の本文だけ（R2）
- text は資料のまま。旧字を新字に直すなど表記を改めて画面に出すときは display_text に書く
- 日本語の本文には、読み上げ用の読み（reading。かなだけ）を人が確かめて書く
- 台本で使えるのは status が verified（画像と照合した）のものだけ
1行でもおかしければ、1件も入れない（facts.csv と同じ扱い）。
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from koten.ref.registry import RefWork

RENDERING_COLUMNS = [
    "rendering_id",
    "passage_key",
    "passage_to",
    "ref_id",
    "role",
    "lang",
    "text",
    "display_text",
    "reading",
    "locator",
    "status",
    "checked_by",
    "checked_at",
    "note",
]
ENTRY_COLUMNS = [
    "entry_id",
    "ref_id",
    "headword",
    "headword_kana",
    "sense",
    "text",
    "display_text",
    "reading",
    "locator",
    "status",
    "checked_by",
    "checked_at",
    "note",
]
ROLES = {"original": "原文", "kundoku": "訓読", "translation": "訳", "commentary": "注釈"}
# 台帳の資料の種類（kind）ごとに、書ける役割
ROLES_BY_KIND = {
    "original": ("original",),
    "kundoku": ("kundoku", "original", "commentary"),  # 訓読本の白文・頭注も書ける
    "translation": ("translation", "commentary"),  # 訳者の注
    "commentary": ("commentary", "kundoku", "translation", "original"),
}
STATUSES = ("transcribed", "verified")
USABLE_STATUS = "verified"
IMPORTED = "imported"  # 電子版から機械で分けた本文（HDD の clean/ref/。引用には使わない）
RENDERING_ID = re.compile(r"^I-[A-Za-z0-9][A-Za-z0-9_.-]*$")
ENTRY_ID = re.compile(r"^J-[A-Za-z0-9][A-Za-z0-9_.-]*$")
PASSAGE_KEY = re.compile(r"^([a-z_]+):(\S+)$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 読み上げの読み：かなと、区切りの記号だけ
READING = re.compile(r"^[ぁ-ゖァ-ヺー・、。，．！？「」『』（）\s]+$")
MAX_RENDERING = 800
MAX_ENTRY = 300


class TextsError(ValueError):
    pass


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Rendering:
    rendering_id: str
    passage_key: str
    ref_id: str
    role: str
    lang: str
    text: str
    locator: str
    status: str
    passage_to: str = ""
    display_text: str = ""
    reading: str = ""
    checked_by: str = ""
    checked_at: str = ""
    note: str = ""

    def row(self) -> dict[str, Any]:
        return {c: getattr(self, c) for c in RENDERING_COLUMNS} | {"text_sha": text_sha(self.text)}


@dataclass
class DictEntry:
    entry_id: str
    ref_id: str
    headword: str
    headword_kana: str
    sense: str
    text: str
    locator: str
    status: str
    display_text: str = ""
    reading: str = ""
    checked_by: str = ""
    checked_at: str = ""
    note: str = ""

    def row(self) -> dict[str, Any]:
        return {c: getattr(self, c) for c in ENTRY_COLUMNS} | {"text_sha": text_sha(self.text)}


def load_canon(config_dir: Path) -> dict[str, dict[str, Any]]:
    path = config_dir / "canon.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text("utf-8")).get("works") or {}


def passage_problem(key: str, canon: dict[str, dict[str, Any]]) -> str | None:
    """passage_key が canon.yaml の作品と番号の付け方に合うか。"""
    m = PASSAGE_KEY.match(key)
    if not m:
        return f"passage_key {key} は <作品記号>:<番号> の形にする（例 lunyu:1.1）"
    work = canon.get(m.group(1))
    if work is None:
        return f"作品記号 {m.group(1)} が config/canon.yaml に無い"
    if not re.match(work.get("pattern") or r"^\S+$", m.group(2)):
        return f"{key} の番号が {work['title']} の付け方（{work.get('numbering', '')}）に合わない"
    return None


def _common(
    g: dict[str, str],
    works: dict[str, RefWork],
    *,
    max_len: int,
) -> list[str]:
    """本文の行に共通の確かめ。"""
    bad: list[str] = []
    work = works.get(g["ref_id"])
    if work is None:
        bad.append(f"ref_id {g['ref_id']} が台帳（ref_works.csv）に無い")
    elif not work.usable:
        # R2：使えない資料の本文は DB にも CSV にも置かない
        bad.append(
            f"資料 {g['ref_id']} は {work.license}（{work.computed_reason}）。本文を書けない"
        )
    if not g["text"]:
        bad.append("text が空")
    elif len(g["text"]) > max_len:
        bad.append(f"text が長すぎる（{len(g['text'])}字。{max_len}字まで。使う所だけにする）")
    if g["display_text"] and g["display_text"] == g["text"]:
        bad.append("display_text は、表記を改めたときだけ書く（text と同じなら空にする）")
    if g["status"] not in STATUSES:
        bad.append(f"status は {'・'.join(STATUSES)}")
    if g["status"] == USABLE_STATUS and not (g["checked_by"] and DATE.match(g["checked_at"])):
        bad.append("verified には checked_by と checked_at（YYYY-MM-DD）が要る")
    if not g["locator"]:
        bad.append("locator（コマ・ページ・章など、資料の中の場所）が要る")
    elif work is not None and work.source == "ndl" and not re.search(r"コマ\s*\d+", g["locator"]):
        bad.append("NDL の資料の locator には「コマ12」のようにコマ番号を書く")
    lang = g.get("lang") or (work.lang if work else "ja")
    if lang == "ja":
        if not g["reading"]:
            bad.append("日本語の本文には reading（読み上げの読み。かな）が要る")
        elif not READING.match(g["reading"]):
            bad.append("reading はかなと区切りの記号だけで書く")
    return bad


def read_renderings(
    config_dir: Path, works: dict[str, RefWork], canon: dict[str, dict[str, Any]]
) -> list[Rendering]:
    rows = _read_csv(config_dir / "renderings.csv")
    problems: list[str] = []
    out: list[Rendering] = []
    seen: set[str] = set()
    for i, r in enumerate(rows, start=2):
        g = {c: (r.get(c) or "").strip() for c in RENDERING_COLUMNS}
        where = f"renderings.csv {i}行目（{g['rendering_id']}）"
        work = works.get(g["ref_id"])
        g["lang"] = g["lang"] or (work.lang if work else "ja")
        bad = _common(g, works, max_len=MAX_RENDERING)
        if not RENDERING_ID.match(g["rendering_id"]):
            bad.append("rendering_id は I- で始め、英数字と - _ . だけにする")
        elif g["rendering_id"] in seen:
            bad.append("rendering_id が重複")
        seen.add(g["rendering_id"])
        for key in filter(None, (g["passage_key"], g["passage_to"])):
            if p := passage_problem(key, canon):
                bad.append(p)
        if not g["passage_key"]:
            bad.append("passage_key が空")
        elif g["passage_to"] and g["passage_to"].split(":")[0] != g["passage_key"].split(":")[0]:
            bad.append("passage_to は passage_key と同じ作品にする")
        if g["role"] not in ROLES:
            bad.append(f"role は {'・'.join(ROLES)} のどれか")
        elif work is not None and g["role"] not in ROLES_BY_KIND.get(work.kind, ()):
            bad.append(f"資料の種類 {work.kind} の本からは role {g['role']} を書けない")
        if bad:
            problems += [f"{where}：{b}" for b in bad]
            continue
        out.append(Rendering(**{c: g[c] for c in RENDERING_COLUMNS}))
    if problems:
        raise TextsError("時代の解釈（renderings.csv）に問題があります：\n" + _lines(problems))
    return out


def read_dict_entries(config_dir: Path, works: dict[str, RefWork]) -> list[DictEntry]:
    rows = _read_csv(config_dir / "dict_entries.csv")
    problems: list[str] = []
    out: list[DictEntry] = []
    seen: set[str] = set()
    for i, r in enumerate(rows, start=2):
        g = {c: (r.get(c) or "").strip() for c in ENTRY_COLUMNS}
        where = f"dict_entries.csv {i}行目（{g['entry_id']}）"
        work = works.get(g["ref_id"])
        g["lang"] = work.lang if work else "ja"
        bad = _common(g, works, max_len=MAX_ENTRY)
        if not ENTRY_ID.match(g["entry_id"]):
            bad.append("entry_id は J- で始め、英数字と - _ . だけにする")
        elif g["entry_id"] in seen:
            bad.append("entry_id が重複")
        seen.add(g["entry_id"])
        if work is not None and work.kind != "dictionary":
            bad.append(f"資料 {g['ref_id']} は辞書ではない（kind={work.kind}）")
        if not g["headword"]:
            bad.append("headword（今の表記の見出し語。例：普通）が空")
        if not g["headword_kana"]:
            bad.append("headword_kana（辞書の見出しのかな。例：ふつう）が空")
        if g["sense"] and not g["sense"].isdigit():
            bad.append("sense（語義番号）は数字（1つしかなければ空）")
        if bad:
            problems += [f"{where}：{b}" for b in bad]
            continue
        out.append(DictEntry(**{c: g[c] for c in ENTRY_COLUMNS}))
    if problems:
        raise TextsError(
            "昔の辞書の語釈（dict_entries.csv）に問題があります：\n" + _lines(problems)
        )
    return out


def _lines(problems: list[str]) -> str:
    return "\n".join(f"- {p}" for p in problems)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))
