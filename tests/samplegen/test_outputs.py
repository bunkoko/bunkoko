"""出力ファイル：再現性、manifest、ground_truth の中身、書き込みの安全装置、衛生。"""

import ast
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.samplegen.conftest import read_csv
from tools.samplegen import prompts, spec
from tools.samplegen.codeframes import OTHER, QUESTIONS, validate

PKG = Path(spec.__file__).parent
REPO = PKG.parents[1]
MANIFEST = "work/spec/spec_manifest.json"
REGENERATE = "uv run python -m tools.samplegen.spec で作り直す"


# --- 再現性・manifest ---


def test_same_seed_gives_identical_output(built):
    again = spec.build_outputs()
    assert again.keys() == built.keys()
    for k in built:
        assert again[k] == built[k], k


def test_other_process_with_other_hash_seed_gives_identical_output(built, tmp_path):
    """別プロセス・別の PYTHONHASHSEED でも、出力はバイト単位で同じ。"""
    env = {**os.environ, "PYTHONHASHSEED": "123", "PYTHONPATH": str(REPO)}
    done = subprocess.run(
        [sys.executable, "-m", "tools.samplegen.spec", "--out", str(tmp_path)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "changed=True" in done.stdout
    for rel, data in built.items():
        assert (tmp_path / rel).read_bytes() == data, rel


def test_other_seed_is_refused():
    with pytest.raises(ValueError):
        spec.build_outputs(seed=1)


def test_manifest_records_seed_version_and_hashes(built):
    assert "manifest.json" not in built  # データ名直下の manifest.json は手順9（凍結）用に空けておく
    m = json.loads(built[MANIFEST])
    assert m["seed"] == 20261016
    assert m["stage"] == "spec" and m["dataset"] == "es_survey_v1"
    assert m["code_version"] == spec.CODE_VERSION
    assert m["code_sha256"] == spec._code_hash()
    assert m["batches"] == 98 and m["content_rows"] == 1950 and m["respondents"] == 1000
    assert set(m["files"]) == {k for k in built if k != MANIFEST}
    for k, digest in m["files"].items():
        assert digest == hashlib.sha256(built[k]).hexdigest(), k


def test_output_layout(built):
    assert {k.split("/")[0] for k in built} == {"ground_truth", "work"}
    for n in spec.GT_FILES:
        assert f"ground_truth/{n}" in built
    assert "work/spec/spec_rows.csv" in built and "work/spec/spec_respondents.csv" in built
    assert not any(k.startswith(("input/", "raw_llm/", "expected/")) for k in built)
    assert all(spec.BATCH_NAME_RE.fullmatch(Path(k).name) for k in built if k.startswith("work/prompts/"))


def test_csv_files_are_utf8_without_bom_and_lf(built):
    for k, v in built.items():
        if k.endswith(".csv"):
            assert not v.startswith(b"\xef\xbb\xbf"), k
            assert b"\r" not in v, k
            v.decode("utf-8")


def test_committed_outputs_match_a_fresh_build(built, tmp_path):
    """samples/fictional/es_survey_v1 の生成物は、いまのコードで新しく作ったものとバイト単位で一致する。"""
    spec.write_outputs(tmp_path, built)
    committed = spec.DEFAULT_OUT
    for rel in built:
        disk = committed / rel
        assert disk.is_file(), rel
        # 人手の見本・禁止フレーズの欄に差し込んだ中身は比べない
        assert spec.strip_sections(disk.read_bytes()) == spec.strip_sections((tmp_path / rel).read_bytes()), (
            f"{rel} が古い。{REGENERATE}"
        )
    on_disk = spec._managed(committed)
    assert set(on_disk) == set(built), f"余分・不足の生成物がある。{REGENERATE}"  # 古い生成物が残っていない
    m = json.loads((committed / MANIFEST).read_text(encoding="utf-8"))
    assert m["code_sha256"] == spec._code_hash(), f"コードのハッシュが古い。{REGENERATE}"
    assert m["files"] == {k: hashlib.sha256(v).hexdigest() for k, v in built.items() if k != MANIFEST}, REGENERATE


# --- ground_truth の中身 ---


def test_codeframe_files(built):
    validate()
    for qid, q in QUESTIONS.items():
        rows = read_csv(built[f"ground_truth/codeframe_{qid.lower()}.csv"])
        assert list(rows[0]) == ["コード", "ラベル", "定義"]
        assert [r["ラベル"] for r in rows] == [lb.name for lb in q.labels]
        assert [r["コード"] for r in rows] == [f"c{i:02d}" for i in range(1, len(q.labels) + 1)]
        assert rows[-1]["ラベル"] == OTHER
        assert all(r["定義"] for r in rows)
    assert len(read_csv(built["ground_truth/codeframe_q1.csv"])) == 15


def test_rows_gt_matches_spec_in_every_shared_column(gt_rows, spec_rows):
    assert len(gt_rows) == len(spec_rows) == 3000
    assert [r["row_id"] for r in gt_rows] == sorted(r["row_id"] for r in gt_rows)  # 並びも固定
    header = set(gt_rows[0])
    assert {
        "row_id", "respondent_id", "question", "kind", "label_codes", "label_names", "n_labels",
        "difficulty", "trap_type", "decoy_code", "emotion", "style", "length_band",
        "dept", "age", "tenure", "emp", "pos", "sat",
    } <= header  # fmt: skip
    assert not header & {"text", "text_fixed", "must_topics", "avoid_topics", "trap_keyword"}
    shared = header & set(spec_rows[0])
    assert {"difficulty", "label_names", "decoy_code", "emotion", "style", "trap_type"} <= shared
    for a, b in zip(gt_rows, spec_rows, strict=True):
        for col in shared:
            assert a[col] == b[col], (a["row_id"], col)
        assert a["length_band"] == b["length_band"]
        want = len(a["label_codes"].split("|")) if a["label_codes"] else 0
        assert a["n_labels"] == str(want)
        assert len(a["label_names"].split("|")) == want or (want == 0 and a["label_names"] == "")


def test_rows_gt_label_names_follow_codes(gt_rows):
    for r in gt_rows:
        if r["kind"] != "content":
            assert r["label_codes"] == "" and r["difficulty"] == ""
            continue
        q = QUESTIONS[r["question"]]
        names = [next(lb.name for lb in q.labels if lb.code == c) for c in r["label_codes"].split("|")]
        assert names == r["label_names"].split("|")
        assert r["difficulty"] in {"normal", "hard"}
        assert (r["trap_type"] != "") == (r["difficulty"] == "hard")


def test_labels_long_matches_rows_gt(built, gt_rows):
    long = read_csv(built["ground_truth/labels_long.csv"])
    by_row: dict[str, list[tuple[str, str]]] = {}
    for r in long:
        got = by_row.setdefault(r["row_id"], [])
        got.append((r["code"], r["label"]))
        assert r["rank"] == str(len(got))
        lb = QUESTIONS[r["question"]].by_name(r["label"])
        assert lb.code == r["code"]  # ラベル名とコードがコード表と合っている
    for r in gt_rows:
        want = (
            list(zip(r["label_codes"].split("|"), r["label_names"].split("|"), strict=True)) if r["label_codes"] else []
        )
        assert by_row.get(r["row_id"], []) == want
    assert len(by_row) == 1950


def test_no_personal_info_in_outputs(built):
    """個人情報は今回は入れない（手順6は別の作業）。"""
    bad = [
        re.compile(r"@"),
        re.compile(r"https?://"),
        re.compile(r"\d{2,4}-\d{2,4}-\d{3,4}"),
        re.compile(r"(?<!\d)0\d{9,10}(?!\d)"),
    ]
    for k, v in built.items():
        if k.endswith((".csv", ".md", ".yaml")):  # manifest はハッシュの数字が並ぶので除く
            text = v.decode("utf-8")
            for pat in bad:
                assert not pat.search(text), (k, pat.pattern)


# --- 書き込みの安全装置 ---


def _prompt_file(root, name="q1_001"):
    return root / "work" / "prompts" / f"{name}.md"


def test_write_outputs_is_idempotent(built, tmp_path):
    assert spec.write_outputs(tmp_path, built) is True
    assert spec.write_outputs(tmp_path, built) is False
    assert _prompt_file(tmp_path).read_bytes() == built["work/prompts/q1_001.md"]
    assert not list(tmp_path.rglob("*.tmp"))
    assert not (tmp_path / "manifest.json").exists()


def test_frozen_guard_only_blocks_batches_that_have_a_raw_json(built, tmp_path, caplog):
    spec.write_outputs(tmp_path, built)
    raw = tmp_path / "raw_llm"
    raw.mkdir()
    (raw / ".DS_Store").write_bytes(b"x")  # json 以外は凍結として数えない
    (raw / "notes.txt").write_text("memo", encoding="utf-8")

    other = dict(built)
    other["work/spec/spec_rows.csv"] = b"x\n"  # 仕様表だけが変わる → 通る
    other[MANIFEST] = json.dumps({"dataset": "es_survey_v1", "stage": "spec", "note": "manifest だけが違う"}).encode()
    assert spec.write_outputs(tmp_path, other) is True
    assert spec.write_outputs(tmp_path, built) is True  # 戻す

    changed = dict(built)
    changed["work/prompts/q1_001.md"] = built["work/prompts/q1_001.md"] + "\n- 変更\n".encode()
    assert spec.write_outputs(tmp_path, changed) is True  # raw_llm/q1_001.json がまだない
    assert spec.write_outputs(tmp_path, built) is True

    (raw / "q1_001.json").write_text('{"items": []}', encoding="utf-8")
    before = _prompt_file(tmp_path).read_bytes()
    with pytest.raises(SystemExit) as exc:
        spec.write_outputs(tmp_path, changed)
    assert "1件" in str(exc.value) and "q1_001" in str(exc.value)
    assert _prompt_file(tmp_path).read_bytes() == before  # 何も書き換わっていない

    # 別のバッチ（raw_llm に json がない）の変更は通る
    changed2 = dict(built)
    changed2["work/prompts/q1_002.md"] = built["work/prompts/q1_002.md"] + "\n- 変更\n".encode()
    assert spec.write_outputs(tmp_path, changed2) is True

    # --force：上書きし、影響するバッチの名前と件数だけをログに出す。raw_llm は残る
    with caplog.at_level(logging.WARNING, logger="samplegen.spec"):
        assert spec.write_outputs(tmp_path, changed, force=True) is True
    assert any("1件" in rec.getMessage() and "q1_001" in rec.getMessage() for rec in caplog.records)
    assert (raw / "q1_001.json").read_text(encoding="utf-8") == '{"items": []}'
    assert spec.write_outputs(tmp_path, built, force=True) is True


def test_unchanged_run_with_frozen_text_is_allowed(built, tmp_path):
    spec.write_outputs(tmp_path, built)
    (tmp_path / "raw_llm").mkdir()
    (tmp_path / "raw_llm" / "q1_001.json").write_text('{"items": []}', encoding="utf-8")
    assert spec.write_outputs(tmp_path, built) is False


def test_inserted_human_samples_and_banned_phrases_survive_a_rebuild(built, tmp_path):
    spec.write_outputs(tmp_path, built)
    p = _prompt_file(tmp_path)
    text = p.read_text(encoding="utf-8")
    text = text.replace(prompts.HUMAN_SAMPLES_EMPTY, "見本1：人手が足りない\n見本2：残業が多い")
    text = text.replace(prompts.BANNED_EMPTY, "・ご存じのとおり")
    p.write_text(text, encoding="utf-8")
    raw = tmp_path / "raw_llm"
    raw.mkdir()
    (raw / "q1_001.json").write_text('{"items": []}', encoding="utf-8")

    assert spec.write_outputs(tmp_path, built) is False  # 欄の中身は比べない
    assert p.read_text(encoding="utf-8") == text

    # 仕様表が変わって他のバッチが書き直されても、差し込んだ中身は消えない
    other = dict(built)
    other["work/prompts/q1_002.md"] = built["work/prompts/q1_002.md"] + "\n- 変更\n".encode()
    (tmp_path / "work/prompts/q1_002.md").write_text(
        (tmp_path / "work/prompts/q1_002.md").read_text(encoding="utf-8").replace(prompts.BANNED_EMPTY, "・要望"),
        encoding="utf-8",
    )
    assert spec.write_outputs(tmp_path, other) is True
    assert "・要望" in (tmp_path / "work/prompts/q1_002.md").read_text(encoding="utf-8")
    assert p.read_text(encoding="utf-8") == text


def test_frozen_batch_with_changed_body_is_refused_even_if_markers_differ(built, tmp_path):
    spec.write_outputs(tmp_path, built)
    p = _prompt_file(tmp_path)
    p.write_text(p.read_text(encoding="utf-8").replace(prompts.BANNED_EMPTY, "・禁止"), encoding="utf-8")
    (tmp_path / "raw_llm").mkdir()
    (tmp_path / "raw_llm" / "q1_001.json").write_text("{}", encoding="utf-8")
    changed = dict(built)
    changed["work/prompts/q1_001.md"] = built["work/prompts/q1_001.md"].replace("文字数".encode(), "字数".encode(), 1)
    with pytest.raises(SystemExit):
        spec.write_outputs(tmp_path, changed)
    assert "・禁止" in p.read_text(encoding="utf-8")


def test_write_outputs_deletes_only_its_own_names(built, tmp_path):
    spec.write_outputs(tmp_path, built)
    prompts_dir = tmp_path / "work" / "prompts"
    stale = prompts_dir / "q3_999.md"  # 自分の命名規則に合う古いバッチ → 消す
    stale.write_text("old", encoding="utf-8")
    keep = {
        prompts_dir / "README.md": "keep",
        prompts_dir / "q1_dup_001.md": "keep",
        prompts_dir / "q1_001.json": "keep",
        prompts_dir / "q4_001.md": "keep",
        tmp_path / "work" / "spec" / "dedup_retry.csv": "keep",
        tmp_path / "work" / "notes.csv": "keep",
        tmp_path / "ground_truth" / "pii_spans.csv": "keep",
        tmp_path / "ground_truth" / "noise_log.jsonl": "keep",
    }
    for p, body in keep.items():
        p.write_text(body, encoding="utf-8")
    assert spec.write_outputs(tmp_path, built) is True
    assert not stale.exists()
    for p in keep:
        assert p.read_text(encoding="utf-8") == "keep", p


def test_refuses_a_folder_of_another_dataset(built, tmp_path):
    spec.write_outputs(tmp_path, built)
    manifest = tmp_path / MANIFEST
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["dataset"] = "ec_reviews_v1"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    victim = _prompt_file(tmp_path, "q2_001")
    before = victim.read_bytes()
    with pytest.raises(SystemExit):
        spec.write_outputs(tmp_path, built)
    assert victim.read_bytes() == before
    manifest.write_text("not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        spec.write_outputs(tmp_path, built)


def test_does_not_follow_symlinked_folders(built, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "q1_001.md").write_text("precious", encoding="utf-8")
    out = tmp_path / "out"
    (out / "work").mkdir(parents=True)
    (out / "work" / "prompts").symlink_to(target, target_is_directory=True)
    with pytest.raises(SystemExit):
        spec.write_outputs(out, built)
    assert (target / "q1_001.md").read_text(encoding="utf-8") == "precious"
    assert [p.name for p in target.iterdir()] == ["q1_001.md"]


def test_does_not_overwrite_a_symlinked_file(built, tmp_path):
    out = tmp_path / "out"
    spec.write_outputs(out, built)
    secret = tmp_path / "secret.csv"
    secret.write_text("keep", encoding="utf-8")
    rows = out / "work" / "spec" / "spec_rows.csv"
    rows.unlink()
    rows.symlink_to(secret)
    with pytest.raises(SystemExit):
        spec.write_outputs(out, dict(built))  # リンクの先には書かない
    assert secret.read_text(encoding="utf-8") == "keep"
    assert rows.is_symlink()


# --- CLI ---


def test_cli_main_writes_files(tmp_path, capsys):
    assert spec.main(["--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "batches=98" in out and "content_rows=1950" in out
    assert (tmp_path / MANIFEST).exists() and not (tmp_path / "manifest.json").exists()
    assert len(list((tmp_path / "work/prompts").glob("*.md"))) == 98
    assert spec.main(["--out", str(tmp_path)]) == 0
    assert "changed=False" in capsys.readouterr().out


def test_cli_force_flag_is_wired_and_raw_llm_survives(tmp_path):
    assert spec.main(["--out", str(tmp_path)]) == 0
    raw = tmp_path / "raw_llm"
    raw.mkdir()
    (raw / "q1_001.json").write_text('{"items": []}', encoding="utf-8")
    p = _prompt_file(tmp_path)
    original = p.read_bytes()
    p.write_bytes(original + "\n- 手で足した行\n".encode())  # 凍結済みバッチの中身が、作り直しで変わる状況

    with pytest.raises(SystemExit):
        spec.main(["--out", str(tmp_path)])
    assert p.read_bytes().endswith("手で足した行\n".encode())  # 止まったので何も変わらない

    assert spec.main(["--out", str(tmp_path), "--force"]) == 0
    assert p.read_bytes() == original
    assert (raw / "q1_001.json").read_text(encoding="utf-8") == '{"items": []}'


def test_stdout_has_no_answer_text(tmp_path, capsys):
    spec.main(["--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1
    for q in QUESTIONS.values():
        for lb in q.labels:
            for t in lb.topics:
                assert t not in out


# --- 衛生 ---


def test_no_print_calls_in_samplegen_or_tests():
    for path in [*PKG.glob("*.py"), *Path(__file__).parent.glob("*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"print", "pprint"}, path.name


def test_no_network_or_external_ai_imports():
    banned = {
        "socket",
        "requests",
        "httpx",
        "urllib",
        "urllib3",
        "http",
        "aiohttp",
        "openai",
        "anthropic",
        "subprocess",
    }
    for path in PKG.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                mods = {(node.module or "").split(".")[0]}
            else:
                continue
            assert not mods & banned, (path.name, mods & banned)


# --- コードのハッシュ（仕様表の生成が読むモジュールだけ） ---


def test_code_hash_covers_only_the_modules_the_spec_reads(tmp_path):
    pkg = tmp_path / "samplegen"
    pkg.mkdir()
    for name in spec.SPEC_SOURCES:
        shutil.copy(PKG / name, pkg / name)
    base = spec._code_hash(pkg)
    assert base == spec._code_hash()  # 本物のフォルダと同じ中身なら同じハッシュ
    (pkg / "noise.py").write_text('"""あとから足すノイズの工程"""\n', encoding="utf-8")
    (pkg / "pii.py").write_text("X = 1\n", encoding="utf-8")
    (pkg / "notes.yaml").write_text("a: 1\n", encoding="utf-8")
    assert spec._code_hash(pkg) == base  # 関係のないファイルを足しても変わらない
    (pkg / "answers.py").write_text((PKG / "answers.py").read_text(encoding="utf-8") + "\n# 変更\n", encoding="utf-8")
    assert spec._code_hash(pkg) != base  # 生成が読むモジュールの変更では変わる


def test_spec_sources_list_has_no_gap():
    """spec を import したときに実際に読み込まれる tools.samplegen のモジュールは、すべてハッシュの対象に入っている。"""
    code = (
        "import json, sys\n"
        "import tools.samplegen.spec\n"
        "mods = sorted(m for m in sys.modules if m.startswith('tools.samplegen.'))\n"
        "sys.stdout.write(json.dumps(mods))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, check=True)
    loaded = json.loads(done.stdout)
    assert "tools.samplegen.spec" in loaded and len(loaded) >= 6
    for mod in loaded:
        assert f"{mod.split('.', 2)[2]}.py" in spec.SPEC_SOURCES, mod
    assert "planted_effects.yaml" in spec.SPEC_SOURCES
    for name in spec.SPEC_SOURCES:
        assert (PKG / name).is_file()


# --- 人手の見本・禁止フレーズの欄：空でも、目印の後ろに空白があっても、差し込んだ中身を失わない ---

_EDITS = {
    "emptied": lambda text: text.replace(
        prompts.BANNED_BEGIN + "\n" + prompts.BANNED_EMPTY + "\n" + prompts.BANNED_END,
        prompts.BANNED_BEGIN + prompts.BANNED_END,
    ),
    "space_after_begin": lambda text: text.replace(
        prompts.BANNED_BEGIN + "\n" + prompts.BANNED_EMPTY, prompts.BANNED_BEGIN + " \n・ご存じのとおり"
    ),
    "filled": lambda text: text.replace(prompts.BANNED_EMPTY, "・ご存じのとおり"),
}


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("edit", list(_EDITS))
def test_edited_sections_are_left_alone(built, tmp_path, edit, frozen):
    spec.write_outputs(tmp_path, built)
    p = _prompt_file(tmp_path)
    text = _EDITS[edit](p.read_text(encoding="utf-8"))
    assert text != built["work/prompts/q1_001.md"].decode("utf-8")
    p.write_text(text, encoding="utf-8")
    if frozen:
        (tmp_path / "raw_llm").mkdir()
        (tmp_path / "raw_llm" / "q1_001.json").write_text('{"items": []}', encoding="utf-8")
    assert spec.write_outputs(tmp_path, built) is False  # 凍結ありでも、拒否も書き換えもしない
    assert p.read_text(encoding="utf-8") == text


def test_section_regex_accepts_empty_and_padded_bodies():
    for body in ("", " ", "\n", " \n中身\n "):
        doc = f"a{prompts.BANNED_BEGIN}{body}{prompts.BANNED_END}b".encode()
        assert spec.strip_sections(doc) == f"a{prompts.BANNED_BEGIN}(欄){prompts.BANNED_END}b".encode()
        fresh = f"a{prompts.BANNED_BEGIN}\nNEW\n{prompts.BANNED_END}b".encode()
        assert spec._keep_sections(fresh, doc) == doc


# --- 凍結したバッチが古いファイルとして消されそうなとき ---


def test_frozen_batch_that_would_be_deleted_is_refused_and_force_keeps_raw_llm(built, tmp_path, caplog):
    spec.write_outputs(tmp_path, built)
    extra = tmp_path / "work" / "prompts" / "q3_024.md"  # いまの計画にないバッチ（古いファイル）
    extra.write_text("old batch", encoding="utf-8")
    raw = tmp_path / "raw_llm"
    raw.mkdir()
    (raw / "q3_024.json").write_text('{"items": []}', encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        spec.write_outputs(tmp_path, built)
    assert "q3_024" in str(exc.value) and "1件" in str(exc.value)
    assert extra.read_text(encoding="utf-8") == "old batch"  # 消されずに残る

    with caplog.at_level(logging.WARNING, logger="samplegen.spec"):
        assert spec.write_outputs(tmp_path, built, force=True) is True
    assert not extra.exists()
    assert (raw / "q3_024.json").read_text(encoding="utf-8") == '{"items": []}'
    assert any("q3_024" in rec.getMessage() and "1件" in rec.getMessage() for rec in caplog.records)


def test_frozen_guard_applies_to_q3_batches_too(built, tmp_path):
    spec.write_outputs(tmp_path, built)
    (tmp_path / "raw_llm").mkdir()
    (tmp_path / "raw_llm" / "q3_023.json").write_text("{}", encoding="utf-8")
    changed = dict(built)
    changed["work/prompts/q3_023.md"] = built["work/prompts/q3_023.md"] + "\n- 変更\n".encode()
    with pytest.raises(SystemExit):
        spec.write_outputs(tmp_path, changed)
    assert (tmp_path / "work/prompts/q3_023.md").read_bytes() == built["work/prompts/q3_023.md"]
