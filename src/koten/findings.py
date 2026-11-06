"""研究の台帳（D-034）：人が論文を読んで、自分の言葉で書いた研究の一文。

config/findings.csv（人が書く）→ koten findings build → SSD の findings 表。
台本では {{finding_src:ID}}（例「ハーバード大学などの調査（2023年）」）と {{finding:ID}}（一文）で
差し込む。「〇〇大学の研究では」と言えるのは、この記号と同じ文だけ（G1）。

- 有料の論文（license が COPYRIGHTED）は、本文も要旨も保存せず、クラウドの AI に渡さない。
  台帳に持つのは書誌と、人が本文の結果の節を読んで書いた一文・限界・効果の大きさだけ
- 台本で使えるのは status が verified で、本文（full_text）で確かめ、取り下げ・訂正を確かめ、
  追試で再現しなかった（failed）ものでない行だけ。要旨や発表文だけで確かめた行は draft のまま
- 追試がまちまち・まだの研究は、限界（limits_ja）を必ず書く。判定役が、語りがそれを越えて
  いないか（言いすぎ・相関を原因と言う・調べた人より広く言う）を見る
"""

from __future__ import annotations

import csv
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

FINDINGS_FILE = "findings.csv"
COLUMNS = [
    "finding_id",
    "themes",  # ; で区切る（config/themes.yaml の言葉。「嫉妬・比較」のように・を含むので）
    "claim_ja",  # 研究の一文（人が自分の言葉で書く。台本にはこのまま入る）
    "limits_ja",  # 限界（「大学生を調べたもので、働く人にも当てはまるかは分かっていません」など）
    "effect_ja",  # 効果の大きさ：わずか・小さい・中くらい・大きい（分からなければ空）
    "design",  # theory・experiment・field_experiment・survey・longitudinal・meta_analysis・review
    "population_ja",  # 調べた人（「アメリカの会社員245人」など）
    "country",
    "affiliation_ja",  # 語りで言う機関（「ハーバード大学など」）。authors_ja とどちらかが要る
    "authors_ja",  # 語りで言う著者（「ボリーノら」）
    "authors",  # 書誌の著者（Bolino, M. C.; Turnley, W. H.）
    "year",
    "title",
    "journal",
    "volume",
    "issue",
    "pages",
    "doi",
    "url",
    "license",  # CC0・CC_BY・COPYRIGHTED
    "replication",  # replicated・mixed・not_tested・failed（大きな追試・メタ分析で確かめた結果）
    "replication_note",  # どこで確かめたか
    "retraction_checked_at",  # 取り下げ・訂正を確かめた日（Crossref など）
    "verified_from",  # full_text・abstract・press_release（台本で使えるのは full_text だけ）
    "checked_by",
    "checked_at",
    "status",  # verified・draft
    "note",
]
DESIGNS = {
    "theory": "考え方",
    "experiment": "実験",
    "field_experiment": "実験",
    "survey": "調査",
    "longitudinal": "長く追いかけた調査",
    "meta_analysis": "多くの研究をまとめた分析",
    "review": "研究のまとめ",
}
REPLICATION = ("replicated", "mixed", "not_tested", "failed")
EFFECTS = ("", "わずか", "小さい", "中くらい", "大きい")
VERIFIED_FROM = ("full_text", "abstract", "press_release")
STATUSES = ("verified", "draft")
LICENSES = ("CC0", "CC_BY", "COPYRIGHTED")
# 研究の一文を渡す章（古典の章には渡さない。企画と G1 の両方で使う）
FINDING_ROLES = {"context", "research", "summary", "worry_block", "compare"}
CLAIM_MAX = 150
LIMITS_MAX = 150
POPULATION_MAX = 60
NOTE_MAX = 200  # メモ・追試のメモ（論文の文を貼らない。自分の言葉で短く）
ID = re.compile(r"FN-[0-9A-Za-z][0-9A-Za-z-]*$")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}$")  # 全角の数字は通さない（語りに全角で出る）
DOI = re.compile(r"10\.[0-9]{4,9}/\S+$")
DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
# 一文にも限界にも書かない言い方（研究で言えることを越える）。限界の「必ずしも」は打ち消しなので除く
OVERCLAIM = re.compile(r"証明|必ず|絶対|間違いなく|誰でも|すべての人|全ての人")
# 一文にだけ書かない言い方（調べた人より広く言う・確かさを言いすぎる）
CLAIM_OVERCLAIM = re.compile(
    r"誰もが|例外なく|確実に|[0-9０-９]+\s*[%％]の人|科学的|医学的|明らかにな|判明"
)
# 原因と結果の言い方。人を割り振る実験（experiment・field_experiment）と、それをまとめた
# メタ分析でない研究（調査・追いかけた調査・考え方・まとめ）では、関係を原因とは書かない
CAUSAL = re.compile(r"原因|せいで|引き起こ|もたら")
CAUSAL_DESIGNS = {"experiment", "field_experiment", "meta_analysis"}
ASCII_WORDS = re.compile(r"[A-Za-z]")


class FindingsError(ValueError):
    pass


@dataclass(frozen=True)
class Finding:
    finding_id: str
    themes: tuple[str, ...]
    claim_ja: str
    limits_ja: str
    effect_ja: str
    design: str
    population_ja: str
    country: str
    affiliation_ja: str
    authors_ja: str
    authors: str
    year: str
    title: str
    journal: str
    volume: str
    issue: str
    pages: str
    doi: str
    url: str
    license: str
    replication: str
    replication_note: str
    retraction_checked_at: str
    verified_from: str
    checked_by: str
    checked_at: str
    status: str
    note: str

    @property
    def usable(self) -> bool:
        """台本で使えるか（verified・本文で確かめた・取り下げを確かめた・再現しなかったものでない）。"""
        return (
            self.status == "verified"
            and self.verified_from == "full_text"
            and bool(self.retraction_checked_at)
            and self.replication != "failed"
        )

    def source_label(self) -> str:
        """語りで言う出典（「ハーバード大学などの調査（2023年）」「ボリーノらが示した考え方（2015年）」）。

        考え方・まとめ・メタ分析を「〇〇大学の」でつなぐと、大学の公式の考えや、まとめた研究が
        全部その大学のもののように聞こえるので、「が示した」「による」でつなぐ。
        """
        who = self.affiliation_ja or self.authors_ja
        word = DESIGNS.get(self.design, "研究")
        if self.design == "theory":
            return f"{who}が示した{word}（{self.year}年）"
        if self.design == "meta_analysis":
            return f"{who}による、{word}（{self.year}年）"
        if self.design == "review":
            return f"{who}による{word}（{self.year}年）"
        return f"{who}の{word}（{self.year}年）"

    def citation(self) -> str:
        """説明欄の書誌（著者 (年). 題. 雑誌, 巻(号), ページ. DOI）。"""
        vol = self.volume + (f"({self.issue})" if self.issue else "")
        parts = [f"{self.authors} ({self.year}). {self.title}."]
        if self.journal:
            parts.append(
                f"{self.journal}"
                + (f", {vol}" if vol else "")
                + (f", {self.pages}" if self.pages else "")
                + "."
            )
        parts.append(f"https://doi.org/{self.doi}" if self.doi else self.url)
        return " ".join(p for p in parts if p)

    def text_sha(self) -> str:
        """台本と判定役に渡る文（一文・限界・出典・調べた人・効果）の指紋。

        企画のあとで変われば人の確認を立てる。
        """
        body = "\n".join(
            [self.claim_ja, self.limits_ja, self.source_label(), self.population_ja, self.effect_ja]
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]

    def row(self) -> dict[str, Any]:
        out = {c: getattr(self, c) for c in COLUMNS}
        out["themes"] = ";".join(self.themes)
        return out


def _themes(config_dir: Path) -> set[str]:
    path = config_dir / "themes.yaml"
    if not path.exists():
        return set()
    return set(yaml.safe_load(path.read_text("utf-8")).get("themes") or [])


def _day(text: str, today: date) -> date | None:
    """2026-11-05 の形の、ありうる日付（先の日付でない）。"""
    if not DATE.match(text):
        return None
    try:
        d = date.fromisoformat(text)
    except ValueError:
        return None
    return d if d <= today else None


def read_findings(config_dir: Path) -> list[Finding]:
    """config/findings.csv を読む。決まりに合わない行が1つでもあれば、全部を止める。"""
    path = config_dir / FINDINGS_FILE
    if not path.exists():
        return []
    known = _themes(config_dir)
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = list(reader.fieldnames or [])
        if cols != COLUMNS:
            raise FindingsError(f"{path.name} の列は {','.join(COLUMNS)} にしてください")
        rows = list(reader)
    out, problems, seen = [], [], set()
    today = date.today()
    for line, r in enumerate(rows, start=2):
        v = {c: (r.get(c) or "").strip() for c in COLUMNS}
        if not any(v.values()):
            continue
        where = f"{line}行目"
        # 台本では「{{finding:ID}}。」と後ろに「。」が付くので、一文の終わりの「。」は外す
        v["claim_ja"] = v["claim_ja"].rstrip("。．.").rstrip()
        # DOI は https://doi.org/ や doi: を付けて写しがちなので、10. で始まる形にそろえる
        v["doi"] = DOI_PREFIX.sub("", v["doi"]).strip()
        fid = v["finding_id"]
        if not ID.match(fid):
            problems.append(f"{where}：finding_id は FN- で始める（{fid}）")
        if fid in seen:
            problems.append(f"{where}：{fid} が重なっている")
        seen.add(fid)
        themes = tuple(t.strip() for t in re.split(r"[;；]", v["themes"]) if t.strip())
        if not themes:
            problems.append(f"{where}：themes が空")
        if known and (bad := [t for t in themes if t not in known]):
            problems.append(f"{where}：themes.yaml に無いテーマ（{'・'.join(bad)}）")
        claim, limits = v["claim_ja"], v["limits_ja"]
        if not claim:
            problems.append(f"{where}：claim_ja（研究の一文）が空")
        elif len(claim) > CLAIM_MAX:
            problems.append(f"{where}：claim_ja が長い（{len(claim)}字。{CLAIM_MAX}字まで）")
        elif re.search(r"[。．！？!?]", claim):
            problems.append(f"{where}：claim_ja は1文で書く（途中に「。」を入れない）")
        if m := OVERCLAIM.search(claim) or CLAIM_OVERCLAIM.search(claim):
            problems.append(f"{where}：claim_ja に「{m.group(0)}」のような言いすぎを書かない")
        elif v["design"] not in CAUSAL_DESIGNS and (m := CAUSAL.search(claim)):
            problems.append(
                f"{where}：claim_ja の「{m.group(0)}」：実験でない研究は、関係を原因とは書かない"
                "（「〜な人ほど、〜な傾向がありました」のように書く）"
            )
        if m := OVERCLAIM.search(limits.replace("必ずしも", "")):
            problems.append(
                f"{where}：limits_ja に「{m.group(0)}」を書かない"
                "（「〜とまでは言えません」「〜かは分かっていません」のように書く）"
            )
        for col, text in (("claim_ja", claim), ("limits_ja", limits)):
            if len(ASCII_WORDS.findall(text)) > len(text) * 0.3:
                problems.append(
                    f"{where}：{col} は自分の日本語で書く（論文の文を写さない。D-034）"
                )
        for col, most in (
            ("limits_ja", LIMITS_MAX),
            ("population_ja", POPULATION_MAX),
            ("replication_note", NOTE_MAX),
            ("note", NOTE_MAX),
        ):
            if len(v[col]) > most:
                problems.append(
                    f"{where}：{col} が長い（{len(v[col])}字。{most}字まで。"
                    "論文の文を貼らず、自分の言葉で短く書く）"
                )
        for col, allowed in (
            ("design", tuple(DESIGNS)),
            ("replication", REPLICATION),
            ("effect_ja", EFFECTS),
            ("verified_from", VERIFIED_FROM),
            ("status", STATUSES),
            ("license", LICENSES),
        ):
            if v[col] not in allowed:
                problems.append(
                    f"{where}：{col} は {'・'.join(a or '空' for a in allowed)} のどれか"
                )
        if not re.fullmatch(r"[0-9]{4}", v["year"]) or not 1800 <= int(v["year"]) <= today.year:
            problems.append(f"{where}：year は西暦4桁（半角の数字）")
        for col in ("authors", "title"):
            if not v[col]:
                problems.append(f"{where}：{col} が空")
        if not (v["doi"] or v["url"]):
            problems.append(f"{where}：doi か url が要る")
        if v["doi"] and not DOI.match(v["doi"]):
            problems.append(f"{where}：doi は 10. で始まる形（10.1002/job.635 など）")
        if v["url"] and not re.match(r"https?://\S+$", v["url"]):
            problems.append(f"{where}：url は https:// で始まる形")
        if not (v["affiliation_ja"] or v["authors_ja"]):
            problems.append(f"{where}：affiliation_ja か authors_ja（語りで言う名前）が要る")
        if v["replication"] in ("mixed", "not_tested") and not limits:
            problems.append(f"{where}：追試がまちまち・まだの研究は limits_ja（限界）を書く")
        if v["status"] == "verified":
            if v["verified_from"] != "full_text":
                problems.append(
                    f"{where}：verified にできるのは本文（結果の節）で確かめた行だけ"
                    "（要旨・発表文だけなら draft のまま）"
                )
            if v["replication"] == "failed":
                problems.append(f"{where}：追試で再現しなかった研究は使わない（draft にする）")
            if v["replication"] in ("replicated", "mixed") and not v["replication_note"]:
                problems.append(
                    f"{where}：replication_note（追試・メタ分析をどこで確かめたか）を書く"
                )
            if not v["population_ja"]:
                problems.append(
                    f"{where}：population_ja（調べた人。考え方の論文なら「人を調べた研究ではない」）"
                    "が要る"
                )
            checked = _day(v["checked_at"], today)
            if not v["checked_by"] or checked is None:
                problems.append(
                    f"{where}：checked_by と checked_at（2026-11-05 の形。先の日付は不可）が要る"
                )
            retracted = _day(v["retraction_checked_at"], today)
            if retracted is None:
                problems.append(
                    f"{where}：retraction_checked_at（取り下げを確かめた日。2026-11-05 の形。"
                    "先の日付は不可）が要る"
                )
            elif v["year"].isdigit() and retracted.year < int(v["year"]):
                problems.append(
                    f"{where}：retraction_checked_at が論文の年より前（論文が出たあとに確かめる）"
                )
        out.append(Finding(**{**v, "themes": themes}))
    if problems:
        raise FindingsError(f"{path.name} に問題があります：" + "、".join(problems))
    return out


SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY,
    themes TEXT NOT NULL,
    usable INTEGER NOT NULL,
    text_sha TEXT NOT NULL,
    source_label TEXT NOT NULL,
    citation TEXT NOT NULL,
"""
    + ",\n".join(f"    {c} TEXT" for c in COLUMNS if c not in ("finding_id", "themes"))
    + "\n);\n"
)


def build_findings(conn: sqlite3.Connection, findings: list[Finding]) -> int:
    """SSD の findings 表を作り直す（台帳を正とする。消した行は表からも消える）。"""
    cols = ["finding_id", "themes", "usable", "text_sha", "source_label", "citation"] + [
        c for c in COLUMNS if c not in ("finding_id", "themes")
    ]
    with conn:
        conn.execute("DROP TABLE IF EXISTS findings")
        conn.executescript(SCHEMA)
        conn.executemany(
            f"INSERT INTO findings({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [
                [
                    f.finding_id,
                    ";".join(f.themes),
                    int(f.usable),
                    f.text_sha(),
                    f.source_label(),
                    f.citation(),
                ]
                + [getattr(f, c) for c in COLUMNS if c not in ("finding_id", "themes")]
                for f in findings
            ],
        )
    return len(findings)


def _has_table(conn: sqlite3.Connection) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'findings'"
        ).fetchone()
    )


def load_findings(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids or not _has_table(conn):
        return {}
    marks = ",".join("?" for _ in ids)
    return {
        r["finding_id"]: dict(r)
        for r in conn.execute(f"SELECT * FROM findings WHERE finding_id IN ({marks})", ids)
    }


def findings_for(conn: sqlite3.Connection, themes: list[str]) -> list[str]:
    """テーマが重なる、台本で使える研究の一文（ID 順）。"""
    if not themes or not _has_table(conn):
        return []
    want = set(themes)
    rows = conn.execute(
        "SELECT finding_id, themes FROM findings WHERE usable = 1 ORDER BY finding_id"
    ).fetchall()
    return [r[0] for r in rows if want & set(r[1].split(";"))]


def finding_shas(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    return {fid: r["text_sha"] for fid, r in load_findings(conn, list(dict.fromkeys(ids))).items()}
