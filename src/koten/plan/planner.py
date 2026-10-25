"""回の企画（SPEC §8.5・§8.6・§3 量産判定対策）。

悩み（形式C はテーマ）から、形式A〜D の plan_json を作る。
- 再使用間隔：直近10回の作品・直近5回の著者を主役にしない（検索の段階で外す）
- 同じクラスタの悩みは30日以内に主役にしない。同じ形式を3回続けない
- 一節は MMR で作品・著者が重ならないように候補を絞り、最終判断は LLM が理由つきで行う。
  LLM の選択が条件を満たさなければ、検索の順位で選ぶ（理由にそう書く）
- 候補が足りないときは企画を作らない（PlanError）
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from koten.authors import author_label
from koten.embed.encoder import Encoder
from koten.index.search import Filter, HotIndex
from koten.llm import ChatLLM, fill, load_prompt, parse_json_object
from koten.retrieve.search import (
    Queries,
    Reranker,
    Scored,
    apply_rerank,
    hybrid_search,
    transform_query,
)

PLAN_SCHEMA_VERSION = "1.0"
EPISODE_TOTAL_SEC = 720  # 12分の回を基準にする（SPEC §2）
ACTIVE_STATES = "state NOT IN ('discarded')"


class PlanError(RuntimeError):
    """企画を作れない（候補が足りない・再使用間隔に当たる など）。その悩みは後回しにする。"""


@dataclass
class PlanConfig:
    query_prompt: str = "query_v1"
    select_prompt: str = "select_v1"
    rrf_k: int = 60
    fused_top: int = 50
    shortlist: int = 10
    work_episodes: int = 10
    author_episodes: int = 5
    worry_cluster_days: int = 30
    same_format_in_row_max: int = 2
    min_sec: int = 600
    max_sec: int = 900
    query_prefix: str = "query: "
    passage_prefix: str = "passage: "


@dataclass
class Planner:
    conn: sqlite3.Connection
    index: HotIndex
    encoder: Encoder
    llm: ChatLLM
    reranker: Reranker | None
    prompts_dir: Path
    config_dir: Path
    vocab: list[str]
    cfg: PlanConfig = field(default_factory=PlanConfig)
    log: Callable[[str], None] = lambda _: None

    def __post_init__(self) -> None:
        self.formats = yaml.safe_load((self.config_dir / "formats.yaml").read_text("utf-8"))[
            "formats"
        ]

    # --- 再使用間隔 ---------------------------------------------------------

    def _recent_episode_ids(self, n: int) -> list[str]:
        rows = self.conn.execute(
            f"SELECT episode_id FROM episodes WHERE {ACTIVE_STATES} "
            "ORDER BY created_at DESC, episode_id DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [r[0] for r in rows]

    def _recent(self, column: str, n: int) -> list[str]:
        ids = self._recent_episode_ids(n)
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT DISTINCT {column} FROM usage WHERE role = 'main' AND {column} IS NOT NULL "
            f"AND episode_id IN ({marks})",
            ids,
        ).fetchall()
        return [r[0] for r in rows]

    def base_filter(self, **kw: Any) -> Filter:
        return Filter(
            exclude_caution=True,
            exclude_work_ids=self._recent("work_id", self.cfg.work_episodes),
            exclude_author_ids=self._recent("author_id", self.cfg.author_episodes),
            **kw,
        )

    def check_worry_cluster(self, worry: dict[str, Any], now: datetime) -> None:
        since = (now - timedelta(days=self.cfg.worry_cluster_days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = self.conn.execute(
            f"SELECT episode_id, worry_ids FROM episodes WHERE {ACTIVE_STATES} AND created_at >= ?",
            (since,),
        ).fetchall()
        cluster = worry.get("cluster_id") or worry["worry_id"]
        for r in rows:
            for wid in json.loads(r["worry_ids"]):
                other = self.conn.execute(
                    "SELECT cluster_id FROM worries WHERE worry_id = ?", (wid,)
                ).fetchone()
                if wid == worry["worry_id"] or (other and other["cluster_id"] == cluster):
                    raise PlanError(
                        f"{worry['worry_id']} と同じクラスタの悩みを{self.cfg.worry_cluster_days}日"
                        f"以内の回（{r['episode_id']}）で使っています"
                    )

    def allowed_formats(self) -> list[str]:
        """同じ形式を same_format_in_row_max 回より多く続けない。使っていない順に並べる。"""
        rows = self.conn.execute(
            f"SELECT format FROM episodes WHERE {ACTIVE_STATES} ORDER BY created_at DESC, "
            "episode_id DESC"
        ).fetchall()
        recent = [r[0] for r in rows]
        n = self.cfg.same_format_in_row_max
        blocked = recent[0] if len(recent) >= n and len(set(recent[:n])) == 1 else None
        last_used = {f: (recent.index(f) if f in recent else len(recent) + 1) for f in "ABCD"}
        return sorted([f for f in "ABCD" if f != blocked], key=lambda f: -last_used[f])

    # --- 悩みの読み込みと検索 -----------------------------------------------

    def worry(self, worry_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM worries WHERE worry_id = ?", (worry_id,)).fetchone()
        if row is None:
            raise PlanError(f"{worry_id} という悩みはありません")
        w = dict(row)
        if w["status"] != "active":
            raise PlanError(
                f"{worry_id} は {w['status']} なので回を作れません（{w['blocked_reason']}）"
            )
        w["themes"] = json.loads(w["themes"] or "[]")
        return w

    def queries(self, worry: dict[str, Any]) -> Queries:
        return transform_query(
            self.conn,
            self.llm,
            self.prompts_dir,
            self.cfg.query_prompt,
            worry["worry_id"],
            worry["text_general"],
            self.vocab,
            known_themes=worry["themes"],
        )

    def search(self, q: Queries, flt: Filter, *, top: int | None = None) -> list[Scored]:
        fused = hybrid_search(
            self.index,
            self.encoder,
            q,
            flt,
            query_prefix=self.cfg.query_prefix,
            passage_prefix=self.cfg.passage_prefix,
            rrf_k=self.cfg.rrf_k,
            top=top or self.cfg.fused_top,
        )
        return apply_rerank(fused, self.reranker, q.text)

    # --- 選択 ---------------------------------------------------------------

    @staticmethod
    def diverse(items: list[Scored], n: int, *, key: str | None, per_key: int = 1) -> list[Scored]:
        """MMR の考え方で、上から順に、同じ作品（か著者）が per_key 件を超えないように拾う。"""
        out: list[Scored] = []
        counts: dict[str, int] = {}
        for s in items:
            k = None
            if key == "work":
                k = s.row["work_id"]
            elif key == "author":
                k = "・".join(s.row.get("author_ids") or [s.row["work_id"]])
            if k is not None and counts.get(k, 0) >= per_key:
                continue
            if k is not None:
                counts[k] = counts.get(k, 0) + 1
            out.append(s)
            if len(out) >= n:
                break
        return out

    def llm_select(
        self,
        fmt: str,
        worries: list[dict[str, Any]],
        shortlist: list[Scored],
        rules: str,
        check: Callable[[list[dict[str, Any]]], str | None],
        fallback: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str, bool]:
        """LLM に最終選択をさせる。戻り値は（選択, 問いかけ, LLM の選択を使ったか）。"""
        system_tpl, user_tpl = load_prompt(self.prompts_dir, self.cfg.select_prompt)
        system = fill(system_tpl, rules=rules)
        worry_lines = "\n".join(f"- {w['worry_id']}：{w['text_general']}" for w in worries)
        cand_lines = "\n".join(
            f"- {s.candidate_id}｜{s.row['title']}（{author_label(s.row)}）"
            f"{s.row['section'] or ''}｜「{s.row['quote_text']}」｜要旨：{s.row['summary_ja']}"
            for s in shortlist
        )
        user = fill(
            user_tpl,
            format_name=f"{fmt} {self.formats[fmt]['name']}",
            worries=worry_lines,
            candidates=cand_lines,
        )
        allowed = {s.candidate_id for s in shortlist}
        for _ in range(3):
            try:
                d = parse_json_object(self.llm.complete(system, user, None))
            except ValueError:
                continue
            picks = [
                {
                    "candidate_id": str(p.get("candidate_id")),
                    "reason": str(p.get("reason") or "").strip(),
                    "worry_id": str(p.get("worry_id") or "") or worries[0]["worry_id"],
                    "side": str(p.get("side") or "").strip(),
                }
                for p in d.get("selected") or []
                if isinstance(p, dict)
            ]
            if any(p["candidate_id"] not in allowed for p in picks):
                continue
            if len({p["candidate_id"] for p in picks}) != len(picks):
                continue
            if check(picks) is None:
                return picks, str(d.get("question") or "").strip(), True
        self.log("  LLM の選択が条件を満たさなかったので、検索の順位で選びます")
        return fallback, "", False

    # --- 形式ごとの企画 -----------------------------------------------------

    def plan(
        self,
        fmt: str,
        *,
        worry_id: str | None = None,
        theme: str | None = None,
        work_id: str | None = None,
        force: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        if fmt not in self.formats:
            raise PlanError(f"形式 {fmt} はありません（A〜D）")
        if fmt == "C":
            return self._plan_c(theme, worry_id, force, now)
        if worry_id is None:
            raise PlanError(f"形式 {fmt} には悩み（--worry）が必要です")
        worry = self.worry(worry_id)
        if not force:
            self.check_worry_cluster(worry, now)
        if fmt == "A":
            return self._plan_a(worry, now)
        if fmt == "B":
            return self._plan_b(worry, work_id, now)
        return self._plan_d(worry, now)

    def _passages_range(self, fmt: str) -> tuple[int, int]:
        p = self.formats[fmt]["passages"]
        return int(p["min"]), int(p["max"])

    def _plan_a(self, worry: dict[str, Any], now: datetime) -> dict[str, Any]:
        lo, hi = self._passages_range("A")
        q = self.queries(worry)
        ranked = self.search(q, self.base_filter())
        shortlist = self.diverse(ranked, self.cfg.shortlist, key="work")
        if len(shortlist) < lo:
            raise PlanError(f"別々の作品の候補が{len(shortlist)}つしかありません（{lo}つ必要）")

        def check(picks: list[dict[str, Any]]) -> str | None:
            works = [self._row(p)["work_id"] for p in picks]
            if not lo <= len(picks) <= hi:
                return "数"
            return "作品の重複" if len(set(works)) != len(works) else None

        fallback = [self._auto_pick(s, worry["worry_id"]) for s in shortlist[: min(hi, 5)]]
        rules = (
            f"- {lo}〜{hi}個を選ぶ。すべて別々の作品から選ぶ。\n"
            "- 悩みの理解が深まる順（共感 → 見方を変える → 前へ進む）に並べる。"
        )
        picks, _, by_llm = self.llm_select("A", [worry], shortlist, rules, check, fallback)
        self._index_rows(shortlist)
        chapters = self._chapters("A", [[p] for p in picks], [worry], themes=q.themes)
        return self._plan_json("A", [worry], q.themes, chapters, picks, by_llm, [q], now)

    def _plan_b(self, worry: dict[str, Any], work_id: str | None, now: datetime) -> dict[str, Any]:
        lo, hi = self._passages_range("B")
        q = self.queries(worry)
        flt = self.base_filter()
        if work_id is None:
            # 悩みに合う候補が多い作品から、一節が足りるものを選ぶ
            scores: dict[str, float] = {}
            for s in self.search(q, flt):
                scores[s.row["work_id"]] = scores.get(s.row["work_id"], 0.0) + s.rrf
            ordered = sorted(scores, key=lambda w: -scores[w])[:10]  # 上位10作品まで試す
        else:
            ordered = [work_id]
        for wid in ordered:
            ranked = self.search(q, self.base_filter(only_work_ids=[wid]), top=30)
            if len(ranked) >= lo:
                break
        else:
            raise PlanError(f"1作品で{lo}つ以上の一節がそろう作品がありません")
        shortlist = ranked[: max(self.cfg.shortlist, hi)]
        title = shortlist[0].row["title"]

        themes = {t for s in shortlist for t in s.row["themes"]}
        others = self._worries_with_themes(themes, exclude={worry["worry_id"]}, limit=4)
        worries = [worry, *others]
        if len(worries) < 3:
            raise PlanError(
                f"『{title}』を当てはめる悩みが{len(worries)}つしかありません（3つ必要）"
            )

        def check(picks: list[dict[str, Any]]) -> str | None:
            if not lo <= len(picks) <= hi:
                return "数"
            used = {p["worry_id"] for p in picks}
            if not used <= {w["worry_id"] for w in worries} or len(used) < 3:
                return "悩みの数"
            return None

        fallback = [
            self._auto_pick(s, worries[i % len(worries)]["worry_id"])
            for i, s in enumerate(shortlist[: min(hi, max(lo, len(worries)))])
        ]
        rules = (
            f"- 『{title}』の一節から{lo}〜{hi}個を選ぶ。\n"
            "- それぞれの一節を、悩み一覧のどれか1つに当てはめ、worry_id に書く。"
            "3つ以上の悩みを使う。\n"
            "- 作品の流れが分かる順（本の中の順）に並べる。"
        )
        picks, _, by_llm = self.llm_select("B", worries, shortlist, rules, check, fallback)
        self._index_rows(shortlist)
        groups = _group_by(picks, "worry_id")
        plan = self._chapters("B", groups, worries, themes=q.themes)
        out = self._plan_json("B", worries, q.themes, plan, picks, by_llm, [q], now)
        out["work_id"] = shortlist[0].row["work_id"]
        return out

    def _plan_c(
        self, theme: str | None, worry_id: str | None, force: bool, now: datetime
    ) -> dict[str, Any]:
        lo, hi = self._passages_range("C")
        if theme is None:
            if worry_id is None:
                raise PlanError("形式C にはテーマ（--theme）か悩み（--worry）が必要です")
            theme = (self.worry(worry_id)["themes"] or [None])[0]
            if theme is None:
                raise PlanError(f"{worry_id} にテーマがありません")
        if theme not in self.vocab:
            raise PlanError(f"テーマ「{theme}」は統制語彙にありません")
        worries = self._worries_with_themes({theme}, exclude=set(), limit=3, first=worry_id)
        if not force:
            for w in worries:
                self.check_worry_cluster(w, now)
        if len(worries) < 3:
            raise PlanError(f"テーマ「{theme}」の悩みが{len(worries)}つしかありません（3つ必要）")

        shortlist: list[Scored] = []
        used_works: set[str] = set()
        queries = []
        per_worry: dict[str, list[Scored]] = {}
        for w in worries:
            q = self.queries(w)
            queries.append(q)
            ranked = [
                s for s in self.search(q, self.base_filter()) if s.row["work_id"] not in used_works
            ]
            picks = self.diverse(ranked, 4, key="work")
            per_worry[w["worry_id"]] = picks
            for s in picks:
                if s.candidate_id not in {x.candidate_id for x in shortlist}:
                    shortlist.append(s)
            used_works.update(s.row["work_id"] for s in picks[:2])
        if any(not v for v in per_worry.values()):
            raise PlanError("一節が見つからない悩みがあります")

        def check(picks: list[dict[str, Any]]) -> str | None:
            works = [self._row(p)["work_id"] for p in picks]
            if not lo <= len(picks) <= hi or len(set(works)) != len(works):
                return "数か作品の重複"
            counts = _group_by(picks, "worry_id")
            if {p[0]["worry_id"] for p in counts} != {w["worry_id"] for w in worries}:
                return "悩みの割り当て"
            return None if all(1 <= len(g) <= 2 for g in counts) else "1つの悩みに3つ以上"

        # 検索の順位で選ぶとき：まず各悩みに1つずつ、次に5つになるまで2つ目を足す
        fallback: list[dict[str, Any]] = []
        taken: set[str] = set()
        goal = min(hi, max(lo, 5))
        for _ in range(2):
            for w in worries:
                if len(fallback) >= goal:
                    break
                s = next(
                    (s for s in per_worry[w["worry_id"]] if s.row["work_id"] not in taken), None
                )
                if s is not None:
                    fallback.append(self._auto_pick(s, w["worry_id"]))
                    taken.add(s.row["work_id"])
        rules = (
            f"- 3つの悩みそれぞれに1〜2個ずつ、合わせて{lo}〜{hi}個を選び、worry_id に"
            "当てはめる悩みを書く。\n- すべて別々の作品から選ぶ。"
        )
        picks, _, by_llm = self.llm_select("C", worries, shortlist, rules, check, fallback)
        self._index_rows(shortlist)
        groups = _group_by(picks, "worry_id")
        chapters = self._chapters("C", groups, worries, themes=[theme])
        return self._plan_json("C", worries, [theme], chapters, picks, by_llm, queries, now)

    def _plan_d(self, worry: dict[str, Any], now: datetime) -> dict[str, Any]:
        lo, hi = self._passages_range("D")
        q = self.queries(worry)
        ranked = self.search(q, self.base_filter())
        shortlist = self.diverse(ranked, 12, key="author", per_key=3)
        authors = {"・".join(s.row.get("author_ids") or []) for s in shortlist}
        if len(authors) < 2:
            raise PlanError("2人以上の著者の候補がそろいません")

        def check(picks: list[dict[str, Any]]) -> str | None:
            if not lo <= len(picks) <= hi:
                return "数"
            sides: dict[str, set[str]] = {}
            for p in picks:
                sides.setdefault(p["side"], set()).add(
                    "・".join(self._row(p).get("author_ids") or [])
                )
            if set(sides) != {"A", "B"}:
                return "A と B"
            if any(len(v) != 1 for v in sides.values()) or sides["A"] == sides["B"]:
                return "1つの側に1人"
            return None

        rules = (
            f"- 同じ悩みに逆の見方をしている2人の著者を選び、合わせて{lo}〜{hi}個の一節を選ぶ。\n"
            '- 片方の著者の一節は side を "A"、もう片方は "B" にする（1つの側は1人の著者だけ）。\n'
            "- 2人の考えが本当に逆向きになるように選ぶ。似た考えなら選ばない。"
        )
        # 対立かどうかは検索の順位では決められないので、LLM が選べなければ企画を作らない
        picks, question, by_llm = self.llm_select("D", [worry], shortlist, rules, check, [])
        if not by_llm:
            raise PlanError("逆の見方をしている2人の著者を選べませんでした")
        self._index_rows(shortlist)
        groups = [[p for p in picks if p["side"] == side] for side in ("A", "B")]
        chapters = self._chapters("D", groups, [worry], themes=q.themes)
        out = self._plan_json("D", [worry], q.themes, chapters, picks, by_llm, [q], now)
        out["question"] = question
        return out

    # --- 部品 -----------------------------------------------------------------

    def _index_rows(self, items: list[Scored]) -> None:
        self._rows = {s.candidate_id: s for s in items}

    def _row(self, pick: dict[str, Any]) -> dict[str, Any]:
        if not hasattr(self, "_pos"):
            self._pos = {cid: i for i, cid in enumerate(self.index.ids)}
        return self.index.rows[self._pos[pick["candidate_id"]]]

    @staticmethod
    def _auto_pick(s: Scored, worry_id: str) -> dict[str, Any]:
        return {
            "candidate_id": s.candidate_id,
            "reason": f"検索の順位で選んだ（{s.provenance()}）",
            "worry_id": worry_id,
            "side": "",
        }

    def _worries_with_themes(
        self, themes: set[str], *, exclude: set[str], limit: int, first: str | None = None
    ) -> list[dict[str, Any]]:
        from koten.worries.master import load_theme_weights, ranked_worries

        ranked = ranked_worries(self.conn, load_theme_weights(self.config_dir), 90)
        out: list[dict[str, Any]] = []
        if first:
            out.append(self.worry(first))
        for w in ranked:
            if w["worry_id"] in exclude or any(o["worry_id"] == w["worry_id"] for o in out):
                continue
            if themes & set(w["themes"]):
                out.append(w)
            if len(out) >= limit:
                break
        return out

    def _stats_for(self, themes: list[str]) -> list[str]:
        if not themes:
            return []
        marks = ",".join("?" for _ in themes)
        rows = self.conn.execute(
            f"SELECT stat_id FROM stats WHERE theme IN ({marks}) ORDER BY stat_id", themes
        ).fetchall()
        return [r[0] for r in rows][:2]

    def _chapters(
        self,
        fmt: str,
        groups: list[list[dict[str, Any]]],
        worries: list[dict[str, Any]],
        *,
        themes: list[str],
    ) -> list[dict[str, Any]]:
        """formats.yaml の章立てに一節を割り当て、尺の目安を12分前後にそろえる。"""
        chapters: list[dict[str, Any]] = []
        groups = list(groups)
        all_worry_ids = [w["worry_id"] for w in worries]
        for spec in self.formats[fmt]["chapters"]:
            role = spec["role"]
            lo, hi = spec["target_sec"]
            if spec.get("optional"):
                continue  # 研究の章（CC BY の論文）は M8 で論文が入ってから
            if "repeat" in spec:
                r_lo, r_hi = spec["repeat"]
                merged = _fit_groups(groups, r_lo, r_hi)
                for g in merged:
                    chapters.append(
                        {
                            "role": role,
                            "target_sec": [lo, hi],
                            "candidate_ids": [p["candidate_id"] for p in g],
                            "worry_ids": sorted({p["worry_id"] for p in g}),
                            "stat_ids": [],
                        }
                    )
                continue
            ch: dict[str, Any] = {
                "role": role,
                "target_sec": [lo, hi],
                "candidate_ids": [],
                "worry_ids": all_worry_ids,
                "stat_ids": self._stats_for(themes) if role == "context" else [],
            }
            if role in ("view_a", "view_b"):
                g = groups[0 if role == "view_a" else 1] if len(groups) > 1 else []
                ch["candidate_ids"] = [p["candidate_id"] for p in g]
            chapters.append(ch)
        _assign_seconds(chapters, EPISODE_TOTAL_SEC)
        human = next((c for c in chapters if c["role"] in ("summary", "compare")), chapters[-1])
        human["human_take"] = True  # 人の一言を入れる枠（SPEC §8.7）
        for i, c in enumerate(chapters):
            c["chapter_id"] = f"ch{i}"
        return chapters

    def _plan_json(
        self,
        fmt: str,
        worries: list[dict[str, Any]],
        themes: list[str],
        chapters: list[dict[str, Any]],
        picks: list[dict[str, Any]],
        by_llm: bool,
        queries: list[Queries],
        now: datetime,
    ) -> dict[str, Any]:
        selection = []
        for p in picks:
            row = self._row(p)
            s = self._rows.get(p["candidate_id"])
            selection.append(
                {
                    **p,
                    "work_id": row["work_id"],
                    "author_ids": row.get("author_ids") or [],
                    "title": row["title"],
                    "authors": row["author_names"],
                    "review_required": bool(row["review_required"]),
                    "rank_info": s.provenance() if s else "",
                    "rerank": s.rerank if s else None,
                }
            )
        total = sum(c["target_sec"] for c in chapters)
        return {
            "schema_version": PLAN_SCHEMA_VERSION,
            "format": fmt,
            "format_name": self.formats[fmt]["name"],
            "theme": themes,
            "worries": [{"worry_id": w["worry_id"], "text": w["text_general"]} for w in worries],
            "chapters": chapters,
            "selection": selection,
            "selected_by": "llm" if by_llm else "search_rank",
            "queries": [
                {"text": q.text, "paraphrase": q.paraphrase, "hyde": q.hyde, "fallback": q.fallback}
                for q in queries
            ],
            "target_total_sec": total,
            # 章の切れ目（オープニングの後から）を広告位置の候補にする。秒は音声のあとで決まる
            "ad_break_after_chapters": [c["chapter_id"] for c in chapters[1:-1]],
            "review_required": any(x["review_required"] for x in selection),
            "llm": self.llm.name,
            "reranker": self.reranker.name if self.reranker else None,
            "created_at": now.isoformat(timespec="seconds"),
        }

    # --- 保存 -----------------------------------------------------------------

    def save(self, plan: dict[str, Any], now: datetime | None = None) -> str:
        """回（episodes）を planned で作り、使う一節を usage に、悩みの使用日を記録する。"""
        now = now or datetime.now(UTC)
        day = now.strftime("%Y%m%d")
        n = self.conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE episode_id LIKE ?", (f"E{day}-%",)
        ).fetchone()[0]
        episode_id = f"E{day}-{n + 1:02d}"
        plan = {**plan, "episode_id": episode_id}
        worry_ids = [w["worry_id"] for w in plan["worries"]]
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        with self.conn:
            self.conn.execute(
                "INSERT INTO episodes(episode_id, format, theme, worry_ids, state, plan_json, "
                "review_required, created_at) VALUES (?, ?, ?, ?, 'planned', ?, ?, ?)",
                (
                    episode_id,
                    plan["format"],
                    json.dumps(plan["theme"], ensure_ascii=False),
                    json.dumps(worry_ids),
                    json.dumps(plan, ensure_ascii=False),
                    int(plan["review_required"]),
                    stamp,
                ),
            )
            for s in plan["selection"]:
                for author in s["author_ids"] or [None]:
                    self.conn.execute(
                        "INSERT INTO usage(episode_id, candidate_id, work_id, author_id, role) "
                        "VALUES (?, ?, ?, ?, 'main')",
                        (episode_id, s["candidate_id"], s["work_id"], author),
                    )
            self.conn.executemany(
                "UPDATE worries SET last_used_at = ? WHERE worry_id = ?",
                [(now.isoformat(timespec="seconds"), w) for w in worry_ids],
            )
        return episode_id


def _group_by(picks: list[dict[str, Any]], key: str) -> list[list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for p in picks:
        groups.setdefault(p[key], []).append(p)
    return list(groups.values())


def _fit_groups(groups: list[list[Any]], lo: int, hi: int) -> list[list[Any]]:
    """一節のまとまりを、章の数が lo〜hi に収まるようにつなげる（多すぎれば後ろからまとめる）。"""
    groups = [list(g) for g in groups if g]
    while len(groups) > hi:
        last = groups.pop()
        groups[-1].extend(last)
    return groups


def _assign_seconds(chapters: list[dict[str, Any]], total: int) -> None:
    """各章の目安の真ん中から始め、合計が total に近づくよう範囲の中で伸び縮みさせる。"""
    for c in chapters:
        lo, hi = c["target_sec"]
        c["_range"] = (lo, hi)
        c["target_sec"] = (lo + hi) / 2
    for _ in range(20):
        diff = total - sum(c["target_sec"] for c in chapters)
        movable = [
            c
            for c in chapters
            if (diff > 0 and c["target_sec"] < c["_range"][1])
            or (diff < 0 and c["target_sec"] > c["_range"][0])
        ]
        if abs(diff) < 1 or not movable:
            break
        step = diff / len(movable)
        for c in movable:
            lo, hi = c["_range"]
            c["target_sec"] = min(max(c["target_sec"] + step, lo), hi)
    for c in chapters:
        del c["_range"]
        c["target_sec"] = int(round(c["target_sec"]))
