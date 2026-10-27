"""仕様表の生成（design.md 6.2 手順1と、手順3の入力づくり）。

    uv run python -m tools.samplegen.spec [--out DIR] [--force]

乱数の種は 20261016 で固定。同じコードなら出力は1バイトも変わらない。
本文は作らない。ラベル・属性・回答の種類・文字数帯・文体・感情・難しさだけを決める。
データ名フォルダ直下の manifest.json は 6.2 手順9（凍結）用に空けてあり、ここでは書かない。
この工程の manifest は work/spec/spec_manifest.json。
"""

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import platform
import re
import sys
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import yaml

from tools.samplegen import answers as ans
from tools.samplegen import prompts
from tools.samplegen.attributes import Respondent, generate_respondents
from tools.samplegen.codeframes import QUESTIONS, validate
from tools.samplegen.sampling import SEED

log = logging.getLogger("samplegen.spec")

CODE_VERSION = "samplegen-spec-2"
DATASET = "es_survey_v1"
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "samples" / "fictional" / DATASET

ATTR_COLUMNS = ("dept", "age", "tenure", "emp", "pos", "sat")
MANIFEST = "work/spec/spec_manifest.json"
# このモジュールが書くファイルの名前。これ以外は読みも消しもしない
GT_FILES = (
    "rows_gt.csv",
    "labels_long.csv",
    "codeframe_q1.csv",
    "codeframe_q2.csv",
    "codeframe_q3.csv",
    "planted_effects.yaml",
)
SPEC_FILES = ("spec_rows.csv", "spec_respondents.csv", "spec_manifest.json")
BATCH_NAME_RE = re.compile(r"q[1-3]_[0-9]{3}\.md")
LIST_SEP = "|"

# バッチの「人手の見本」「禁止フレーズ」の欄。作り直しても差し込んだ中身を消さない
# 改行や空白に頼らない：目印と目印の間は、空でも空白つきでも、そのまま欄の中身として扱う
_SECTION_RE = re.compile(r"(<!-- (HUMAN_SAMPLES|BANNED_PHRASES):BEGIN -->)(.*?)(<!-- \2:END -->)", re.DOTALL)

EFFECTS_HEADER = """\
# 仕込んだ属性効果（design.md 6.1）と、この種で実現した値。
# when は 属性名: [値...]（属性どうしは AND、値どうしは OR）。属性名は dept=部署, age=年代, tenure=勤続年数,
# emp=雇用形態, pos=役職, sat=総合満足度。乗数は Q1 の主ラベルの割り付け（IPF）と、2つ目以降のラベルの重みに掛ける。
# リフト = （対象の内でそのラベルが付いた割合）÷（対象の外での割合）。内と外は Q1 で中身のある回答者で数える。
#   ipf_expected_lift      : IPF が目標にした期待リフト（主ラベル）
#   realized_lift_primary  : 実現したリフト（主ラベル）
#   realized_lift_any      : 実現したリフト（付いたラベル全部。2つ目・3つ目を含む）
"""


def _csv_bytes(header: Sequence[str], rows: Sequence[Sequence[object]]) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _code_of(qid: str, name: str) -> str:
    return QUESTIONS[qid].by_name(name).code


def _all_rows(rows_by_q: dict[str, list[ans.RowSpec]]) -> list[ans.RowSpec]:
    """回答者 → 設問の順に並べた全行。"""
    out = [r for rows in rows_by_q.values() for r in rows]
    return sorted(out, key=lambda r: (r.respondent_id, r.qid))


# manifest のコードのハッシュに入れるファイル：仕様表の生成が実際に読むモジュールと効果の定義だけ。
# あとから noise.py などを足しても変わらない（test_outputs.py が、ここに漏れがないことを確かめる）
SPEC_SOURCES = (
    "answers.py",
    "attributes.py",
    "codeframes.py",
    "planted_effects.yaml",
    "prompts.py",
    "sampling.py",
    "spec.py",
)


def _code_hash(root: Path | None = None) -> str:
    h = hashlib.sha256()
    here = Path(__file__).parent if root is None else Path(root)
    for name in SPEC_SOURCES:
        h.update(name.encode("utf-8"))
        h.update((here / name).read_bytes())
    return h.hexdigest()


def strip_sections(data: bytes) -> bytes:
    """人手の見本・禁止フレーズの欄の中身を固定の文字に置き換える（比較用）。"""
    text = data.decode("utf-8")
    return _SECTION_RE.sub(lambda m: m.group(1) + "(欄)" + m.group(4), text).encode("utf-8")


def _keep_sections(new: bytes, existing: bytes) -> bytes:
    """new の欄の中身を、existing に差し込んであった中身に置き換える。"""
    old = {m.group(2): m.group(3) for m in _SECTION_RE.finditer(existing.decode("utf-8"))}
    text = new.decode("utf-8")

    def sub(m: re.Match) -> str:
        return m.group(1) + old.get(m.group(2), m.group(3)) + m.group(4)

    return _SECTION_RE.sub(sub, text).encode("utf-8")


def _effects_yaml(result: ans.BuildResult) -> bytes:
    src = yaml.safe_load(ans.EFFECTS_PATH.read_text(encoding="utf-8"))
    stats = {s["id"]: s for s in result.effect_stats}
    q = QUESTIONS[src["question"]]
    effects = []
    for e in src["effects"]:
        item = {
            "id": e["id"],
            "label": e["label"],
            "code": q.by_name(e["label"]).code,
            "when": e["when"],
            "multiplier": e["multiplier"],
        }
        item.update({k: v for k, v in stats[e["id"]].items() if k not in ("id", "multiplier")})
        effects.append(item)
    body = yaml.safe_dump(
        {"version": src["version"], "question": src["question"], "effects": effects},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=None,
        width=1000,
    )
    return (EFFECTS_HEADER + body).encode("utf-8")


def build_outputs(seed: int = SEED) -> dict[str, bytes]:
    """出力ファイルを {データ名フォルダからの相対パス: 中身} で返す。ディスクには触れない。"""
    if seed != SEED:
        raise ValueError("乱数の種は 20261016 で固定です")
    validate()
    respondents = generate_respondents()
    by_id = {r.respondent_id: r for r in respondents}
    result = ans.build_all_rows(respondents)
    rows_by_q = result.rows_by_q
    rows = _all_rows(rows_by_q)
    files: dict[str, bytes] = {}

    # --- work/spec ---
    files["work/spec/spec_respondents.csv"] = _csv_bytes(
        ["respondent_id", *ATTR_COLUMNS, "traits"],
        [[r.respondent_id, *(getattr(r, a) for a in ATTR_COLUMNS), LIST_SEP.join(r.traits)] for r in respondents],
    )
    spec_header = [
        "row_id", "respondent_id", "question", "kind", "text_fixed",
        "label_codes", "label_names", "length_band", "len_min", "len_max",
        "style", "emotion", "difficulty", "trap_type", "decoy_code", "trap_keyword",
        "must_topics", "avoid_topics", *ATTR_COLUMNS, "traits",
    ]  # fmt: skip
    spec_rows = []
    for row in rows:
        r = by_id[row.respondent_id]
        spec_rows.append(
            [
                row.row_id, row.respondent_id, row.qid, row.kind, row.text_fixed,
                LIST_SEP.join(_code_of(row.qid, n) for n in row.labels),
                LIST_SEP.join(row.labels),
                row.band, row.len_min or "", row.len_max or "",
                row.style, row.emotion, row.difficulty, row.trap_type,
                _code_of(row.qid, row.decoy) if row.decoy else "", row.trap_keyword,
                LIST_SEP.join(row.must_topics), LIST_SEP.join(row.avoid_topics),
                *(getattr(r, a) for a in ATTR_COLUMNS), LIST_SEP.join(r.traits),
            ]
        )  # fmt: skip
    files["work/spec/spec_rows.csv"] = _csv_bytes(spec_header, spec_rows)

    # --- ground_truth ---
    gt_header = [
        "row_id", "respondent_id", "question", "kind", "label_codes", "label_names",
        "n_labels", "difficulty", "trap_type", "decoy_code", "emotion", "style",
        "length_band", *ATTR_COLUMNS,
    ]  # fmt: skip
    gt_rows = []
    long_rows = []
    for row in rows:
        r = by_id[row.respondent_id]
        gt_rows.append(
            [
                row.row_id, row.respondent_id, row.qid, row.kind,
                LIST_SEP.join(_code_of(row.qid, n) for n in row.labels),
                LIST_SEP.join(row.labels), len(row.labels),
                row.difficulty, row.trap_type,
                _code_of(row.qid, row.decoy) if row.decoy else "",
                row.emotion, row.style, row.band,
                *(getattr(r, a) for a in ATTR_COLUMNS),
            ]
        )  # fmt: skip
        for rank, name in enumerate(row.labels, start=1):
            long_rows.append([row.row_id, row.qid, rank, _code_of(row.qid, name), name])
    files["ground_truth/rows_gt.csv"] = _csv_bytes(gt_header, gt_rows)
    files["ground_truth/labels_long.csv"] = _csv_bytes(["row_id", "question", "rank", "code", "label"], long_rows)
    for qid, q in QUESTIONS.items():
        files[f"ground_truth/codeframe_{qid.lower()}.csv"] = _csv_bytes(
            ["コード", "ラベル", "定義"], [[lb.code, lb.name, lb.definition] for lb in q.labels]
        )
    files["ground_truth/planted_effects.yaml"] = _effects_yaml(result)

    # --- work/prompts ---
    plan = prompts.batch_plan(rows_by_q)
    for name, batch_rows in plan:
        text = prompts.render_batch(name, QUESTIONS[batch_rows[0].qid], batch_rows, by_id)
        files[f"work/prompts/{name}.md"] = text.encode("utf-8")

    files[MANIFEST] = _manifest(files, respondents, rows_by_q, plan)
    return files


def _manifest(files, respondents: list[Respondent], rows_by_q, plan) -> bytes:
    content_rows = sum(1 for rows in rows_by_q.values() for r in rows if r.kind == "content")
    data = {
        "dataset": DATASET,
        "stage": "spec",
        "seed": SEED,
        "code_version": CODE_VERSION,
        "code_sha256": _code_hash(),
        "python": ".".join(platform.python_version_tuple()[:2]),
        "numpy": np.__version__,
        "respondents": len(respondents),
        "content_rows": content_rows,
        "batches": len(plan),
        "files": {k: hashlib.sha256(v).hexdigest() for k, v in sorted(files.items())},
    }
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _regular_files(directory: Path, accept) -> dict[str, Path]:
    """directory 直下の通常ファイル（シンボリックリンクは除く）のうち、accept(名前) が真のもの。"""
    if not directory.is_dir():
        return {}
    return {p.name: p for p in sorted(directory.iterdir()) if p.is_file() and not p.is_symlink() and accept(p.name)}


def _managed(out: Path) -> dict[str, Path]:
    """この生成が書く名前のファイル（再生成のとき消してよいもの）。"""
    found: dict[str, Path] = {}
    for name, p in _regular_files(out / "work" / "prompts", lambda n: bool(BATCH_NAME_RE.fullmatch(n))).items():
        found[f"work/prompts/{name}"] = p
    for name, p in _regular_files(out / "work" / "spec", lambda n: n in SPEC_FILES).items():
        found[f"work/spec/{name}"] = p
    for name, p in _regular_files(out / "ground_truth", lambda n: n in GT_FILES).items():
        found[f"ground_truth/{name}"] = p
    return found


def _check_target(out: Path) -> None:
    for d in (out / "work", out / "work" / "prompts", out / "work" / "spec", out / "ground_truth"):
        if d.is_symlink():
            raise SystemExit("出力先のフォルダがシンボリックリンクです。たどらないので止めました。")
    manifest = out / MANIFEST
    if manifest.is_file():
        try:
            dataset = json.loads(manifest.read_text(encoding="utf-8")).get("dataset")
        except ValueError, UnicodeDecodeError:
            dataset = None
        if dataset != DATASET:
            raise SystemExit(f"出力先の manifest のデータ名が {DATASET} ではありません。止めました。")


def _frozen(out: Path, name: str) -> bool:
    """raw_llm/<名前>.json（凍結した本文）があるか。.DS_Store などは数えない。"""
    p = out / "raw_llm" / f"{name}.json"
    return p.is_file()


def write_outputs(out: Path, files: dict[str, bytes], *, force: bool = False) -> bool:
    """書き込む。何か書いたら True。

    raw_llm/<名前>.json があるバッチの中身が変わるときは、force なしでは止める。
    人手の見本・禁止フレーズの欄に差し込んだ中身は、作り直しても残す。
    """
    out = Path(out)
    _check_target(out)
    current = _managed(out)
    files = dict(files)
    for rel in files:
        if rel.startswith("work/prompts/") and rel in current:
            files[rel] = _keep_sections(files[rel], current[rel].read_bytes())

    changed = [k for k, v in files.items() if k not in current or current[k].read_bytes() != v]
    stale = [k for k in current if k not in files]
    batch_names = [Path(k).stem for k in (*changed, *stale) if k.startswith("work/prompts/")]
    hit = sorted(n for n in batch_names if _frozen(out, n))
    if hit:
        shown = ", ".join(hit[:5]) + (" ..." if len(hit) > 5 else "")
        if not force:
            raise SystemExit(
                f"raw_llm/ に凍結済みの本文があるバッチの仕様が変わります（{len(hit)}件: {shown}）。"
                "上書きするなら --force を付けてください。"
            )
        log.warning("force: 凍結済みの本文があるバッチの仕様を上書きします（%d件: %s）", len(hit), shown)

    for rel in stale:
        current[rel].unlink()
    for rel in changed:
        dest = out / rel
        if dest.is_symlink():
            raise SystemExit("出力先のファイルがシンボリックリンクです。止めました。")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(files[rel])
        os.replace(tmp, dest)
    return bool(changed or stale)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="架空アンケートの仕様表を作る")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--force", action="store_true", help="raw_llm/ に本文があるバッチの仕様も上書きする")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    files = build_outputs()
    changed = write_outputs(args.out, files, force=args.force)
    n_batches = sum(1 for k in files if k.startswith("work/prompts/"))
    manifest = json.loads(files[MANIFEST])
    sys.stdout.write(
        f"respondents={manifest['respondents']} content_rows={manifest['content_rows']} "
        f"batches={n_batches} files={len(files)} changed={changed}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
