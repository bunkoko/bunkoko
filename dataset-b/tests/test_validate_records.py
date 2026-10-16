"""tools/validate_records.py のテスト。dataset-b/ で python3 -m unittest discover -s tests を実行する。"""
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import validate_records as vr  # noqa: E402

from jsonschema import Draft202012Validator  # noqa: E402

SAMPLE = ROOT / "examples" / "metadata.sample.jsonl"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_ROWS = [json.loads(line) for line in SAMPLE.read_text(encoding="utf-8").splitlines()]

try:
    from PIL import Image
except ImportError:
    Image = None


def issues(path, level, **kw):
    return [i for i in vr.validate_file(path, **kw).issues if i.level == level]


class TempJsonl:
    """レコード（dict）または生の行（str）から一時 JSONL を作る。"""

    def __init__(self, *rows):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.path = self.root / "metadata.jsonl"
        lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in rows]
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.dir.cleanup()


def row(index=0, **changes):
    """サンプルの行をコピーし、captions__ja_short のような __ 区切りのキーで値を差し替える。"""
    rec = copy.deepcopy(SAMPLE_ROWS[index])
    for dotted, value in changes.items():
        *parents, last = dotted.split("__")
        target = rec
        for p in parents:
            target = target[p]
        target[last] = value
    return rec


class SampleTest(unittest.TestCase):
    def test_sample_has_no_errors_or_warnings(self):
        report = vr.validate_file(SAMPLE)
        self.assertEqual([str(i) for i in report.issues], [])
        self.assertEqual(report.rows, 4)
        self.assertEqual(sum(report.judgments.values()), 2)

    def test_cli_exit_codes_and_summary(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(vr.main([str(SAMPLE), "--strict"]), 0)
            self.assertEqual(vr.main([str(FIXTURES / "duplicate_id.jsonl")]), 1)
        self.assertIn("結果: OK", out.getvalue())
        self.assertIn("2行目: [エラー] id jts-000001 が重複しています", out.getvalue())

    def test_sample_covers_required_cases(self):
        verified = [r["verification"]["judgment"] for r in SAMPLE_ROWS if r["verification"]["human_verified"]]
        self.assertIn("correct", verified)
        self.assertIn("minor_error", verified)
        self.assertTrue(any(r["location"] is None and r["capture"]["season"] is None for r in SAMPLE_ROWS))
        self.assertTrue(any(not r["verification"]["human_verified"] and r["location"] for r in SAMPLE_ROWS))


class FixtureTest(unittest.TestCase):
    # ファイル名: (エラーの出る行, エラー件数, メッセージの一部)
    CASES = {
        "season_mismatch.jsonl": ([1], 1, "capture.season が撮影月と合いません"),
        "season_without_location.jsonl": ([1], 1, "capture.season も null"),
        "ja_short_missing_period.jsonl": ([1], 1, "captions.ja_short: 「。」で終わる1文"),
        "error_without_error_fields.jsonl": ([1], 1, "error_fields に誤りのあった項目"),
        "unverified_with_judgment.jsonl": ([1], 1, "human_verified が false の行は judgment を null"),
        "extra_gps_key.jsonl": ([1, 2], 2, "定義にないキーがあります"),
        "duplicate_id.jsonl": ([2], 1, "id jts-000001 が重複しています"),
        "wrong_prefecture_code.jsonl": ([1], 1, "先頭2桁 13 が 長野県 の都道府県コード 20"),
        "file_name_mismatch.jsonl": ([1], 1, 'file_name は "images/jts-000001.jpg"'),
        "trailing_newline_code.jsonl": ([1], 2, "location.municipality_code: 改行や制御文字を含めないでください"),
    }

    def test_every_fixture_has_a_case(self):
        self.assertEqual(sorted(p.name for p in FIXTURES.glob("*.jsonl")), sorted(self.CASES))

    def test_each_fixture_fails_with_expected_error(self):
        for name, (lines, count, fragment) in self.CASES.items():
            with self.subTest(name):
                errs = issues(FIXTURES / name, "error")
                self.assertEqual(sorted({e.line for e in errs}), lines, [str(e) for e in errs])
                self.assertEqual(len(errs), count, [str(e) for e in errs])
                self.assertTrue(any(fragment in e.message for e in errs), [str(e) for e in errs])

    def test_schema_alone_rejects_conditional_rule_fixtures(self):
        validator = Draft202012Validator(json.loads(vr.SCHEMA_PATH.read_text(encoding="utf-8")))
        for name in ("season_mismatch.jsonl", "season_without_location.jsonl", "error_without_error_fields.jsonl",
                     "unverified_with_judgment.jsonl", "extra_gps_key.jsonl"):
            with self.subTest(name):
                for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
                    self.assertFalse(validator.is_valid(json.loads(line)))


class RuleTest(unittest.TestCase):
    def errors_for(self, *rows):
        with TempJsonl(*rows) as t:
            return [i.message for i in issues(t.path, "error")]

    def warnings_for(self, *rows):
        with TempJsonl(*rows) as t:
            return [i.message for i in issues(t.path, "warning")]

    def assertErrorContains(self, rec, fragment):
        errs = self.errors_for(rec)
        self.assertTrue(any(fragment in e for e in errs), errs)

    def test_json_syntax_error_reports_line(self):
        with TempJsonl(SAMPLE_ROWS[0], '{"id": "jts-000002",') as t:
            errs = issues(t.path, "error")
        self.assertEqual([e.line for e in errs], [2])
        self.assertIn("JSON として読めません", errs[0].message)

    def test_duplicate_json_key_is_error(self):
        line = json.dumps(SAMPLE_ROWS[0], ensure_ascii=False)[:-1] + ', "id": "jts-000009"}'
        self.assertTrue(any("同じキーが重複" in e for e in self.errors_for(line)))

    def test_missing_key_message_is_japanese(self):
        rec = row()
        del rec["provenance"]
        self.assertErrorContains(rec, "必須キーがありません: provenance")

    def test_exact_date_field_is_rejected(self):
        self.assertErrorContains(row(capture__day=14), "capture: 定義にないキーがあります: day")

    def test_verified_without_judgment(self):
        self.assertErrorContains(row(verification__human_verified=True), "judgment（correct / minor_error / major_error）が必要")

    def test_correct_with_error_fields(self):
        self.assertErrorContains(row(1, verification__error_fields=["category"]), "correct の行は error_fields を空に")

    def test_prefecture_en_mismatch(self):
        self.assertErrorContains(row(location__prefecture_en="Nagasaki"), "prefecture_en が prefecture と合いません")

    def test_prefecture_level_code_is_rejected(self):
        self.assertErrorContains(row(location__municipality_code="20000"), "都道府県のコードです")

    def test_finer_than_municipality_is_rejected(self):
        self.assertErrorContains(row(location__municipality="松本市中央一丁目"), "市・区・町・村で終わる")

    def test_future_year_month(self):
        self.assertErrorContains(row(capture__year_month="2999-10"), "未来の年月")

    def test_location_without_season(self):
        self.assertErrorContains(row(capture__season=None), "capture.season が撮影月と合いません")

    def test_ja_short_length(self):
        self.assertErrorContains(row(captions__ja_short="赤い列車。"), "15〜60文字")

    def test_ja_short_two_sentences(self):
        self.assertErrorContains(row(captions__ja_short="赤い列車が走る。背景は紅葉した山だ。"), "「。」で終わる1文")

    def test_ja_detailed_sentence_count(self):
        two = ("谷を横切る鉄橋の上を、二両編成の赤い列車が渡っている。"
               "手前から奥の山の斜面まで、赤や橙、黄色に色づいた木々が覆っていて、空は薄い雲に覆われ、光は柔らかい。")
        self.assertErrorContains(row(captions__ja_detailed=two), "3〜5文")

    def test_ja_detailed_must_end_with_period(self):
        text = SAMPLE_ROWS[0]["captions"]["ja_detailed"].rstrip("。")
        self.assertErrorContains(row(captions__ja_detailed=text), "3〜5文")

    def test_en_short_rules(self):
        self.assertErrorContains(row(captions__en_short="A red train."), "5〜30語")
        self.assertErrorContains(row(captions__en_short="A red train crosses a bridge. Autumn leaves surround it."), "1文")
        self.assertErrorContains(row(captions__en_short="A red train crossing an iron bridge"), "1文")

    def test_trailing_newline_in_caption(self):
        self.assertErrorContains(row(captions__ja_short="紅葉した山あいの鉄橋を渡る赤い列車。\n"), "空白や改行")

    def test_style_warnings_are_not_errors(self):
        rec = row(captions__ja_short="この写真は紅葉の山で観光客が赤いJR列車を見ている様子です。",
                  captions__en_short="This photo shows tourists watching a red train near Matsumoto with 12 cars.")
        self.assertEqual(self.errors_for(rec), [])
        warnings = " ".join(self.warnings_for(rec))
        for fragment in ("「この写真」", "人物を表す語があります（観光客）", "英字があります（JR）", "です・ます調",
                         "tourists", "\"This photo\"", "大文字で始まる語があります（Matsumoto）", "6以上の数があります（12）"):
            self.assertIn(fragment, warnings)

    def test_strict_turns_warnings_into_errors(self):
        rec = row(captions__en_short="A red train crossing an iron bridge while people watch.")
        with TempJsonl(rec) as t, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(vr.main([str(t.path)]), 0)
            self.assertEqual(vr.main([str(t.path), "--strict"]), 1)
        self.assertIn("[エラー] captions.en_short: 人物を表す語があります（people）", out.getvalue())

    def test_trailing_newline_outside_captions_is_error(self):
        sha = SAMPLE_ROWS[0]["image_info"]["sha256"]
        for key, value in (("capture__year_month", "2099-10\n"), ("provenance__prompt_version", "caption-v1\n"),
                           ("image_info__sha256", sha + "\n"), ("location__municipality_code", "13101\n")):
            with self.subTest(key):
                self.assertErrorContains(row(**{key: value}), key.replace("__", ".") + ": 改行や制御文字を含めないでください")
        self.assertErrorContains(row(capture__year_month="2099-10\n"), "capture.year_month は YYYY-MM にしてください")
        self.assertErrorContains(row(location__municipality_code="13101\n"), "municipality_code は5桁の数字にしてください")

    def test_empty_file_is_error(self):
        for content in ("", "\n\n"):
            with self.subTest(repr(content)), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "metadata.jsonl"
                path.write_text(content, encoding="utf-8")
                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(vr.main([str(path)]), 1)
                self.assertIn("[エラー] レコードが1件もありません", out.getvalue())

    def test_banned_words_are_warnings(self):
        detailed = SAMPLE_ROWS[0]["captions"]["ja_detailed"] + "鉄橋の手前を歩く人が見える。"
        cases = (
            (row(captions__ja_short="鉄橋を渡る赤い列車が写っている山あいの風景。"), "「写真」「画像」「撮影」「写っている」"),
            (row(captions__ja_short="日本の山あいの鉄橋を渡る赤い列車。"), "「日本」は書かないでください"),
            (row(captions__en_short="Japanese noodles in a white bowl on a wooden table."), '"Japan" "Japanese"'),
            (row(captions__ja_detailed=detailed), "captions.ja_detailed: 人物を表す語があります（歩く人）"),
        )
        for rec, fragment in cases:
            with self.subTest(fragment):
                self.assertEqual(self.errors_for(rec), [])
                self.assertTrue(any(fragment in w for w in self.warnings_for(rec)), self.warnings_for(rec))

    def test_duplicate_sha256_is_warning(self):
        second = row(1, image_info__sha256=SAMPLE_ROWS[0]["image_info"]["sha256"])
        self.assertEqual(self.errors_for(SAMPLE_ROWS[0], second), [])
        self.assertTrue(any("sha256 が 1行目と同じ" in w for w in self.warnings_for(SAMPLE_ROWS[0], second)))


class CaptionOutputTest(unittest.TestCase):
    """生成スクリプトが VLM の出力ごとに呼ぶ check_caption_output のテスト。"""

    def output(self, **changes):
        out = dict(SAMPLE_ROWS[0]["captions"], category="railway", flags=[])
        out.update(changes)
        return out

    def errors(self, obj):
        return [msg for level, msg in vr.check_caption_output(obj) if level == "error"]

    def test_valid_output_has_no_errors(self):
        self.assertEqual(vr.check_caption_output(self.output()), [])

    def test_prompt_format_example_passes(self):
        text = (ROOT / "prompts" / "caption_v1_system.txt").read_text(encoding="utf-8")
        example = json.loads(next(line for line in text.splitlines() if line.startswith('{"ja_short"')))
        self.assertEqual(vr.check_caption_output(example), [])

    def test_long_ja_short_is_error(self):
        self.assertTrue(any("15〜60文字" in e for e in self.errors(self.output(ja_short="山" * 60 + "。"))))

    def test_duplicate_flags_is_error(self):
        self.assertIn("flags: 同じ値が重複しています", self.errors(self.output(flags=["person", "person"])))

    def test_unknown_category_is_error(self):
        self.assertTrue(any(e.startswith("category: 許可されていない値") for e in self.errors(self.output(category="night"))))


@unittest.skipIf(Image is None, "Pillow が無いため画像の検査はテストしない")
class ImageFileTest(unittest.TestCase):
    def make(self, t, rec, size=(40, 30), exif=None):
        path = t.root / rec["file_name"]
        path.parent.mkdir(exist_ok=True)
        kwargs = {"exif": exif} if exif is not None else {}
        Image.new("RGB", size, (200, 60, 40)).save(path, "JPEG", **kwargs)
        rec["image_info"].update(width=size[0], height=size[1], sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    def run_check(self, rec, **make_kw):
        with TempJsonl() as t:
            self.make(t, rec, **make_kw)
            t.path.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
            return [i.message for i in issues(t.path, "error", images_root=t.root)]

    def test_clean_image_passes(self):
        self.assertEqual(self.run_check(row()), [])

    def test_exif_with_gps_is_error(self):
        exif = Image.Exif()
        exif[0x0110] = "iPhone"
        exif.get_ifd(0x8825)[1] = "N"
        errs = self.run_check(row(), exif=exif)
        self.assertTrue(any("メタデータが残っています" in e and "EXIF" in e and "GPS" in e for e in errs), errs)

    def test_size_and_hash_mismatch(self):
        with TempJsonl() as t:
            rec = row()
            self.make(t, rec)
            rec["image_info"].update(width=41, sha256="0" * 64)
            t.path.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
            errs = " ".join(i.message for i in issues(t.path, "error", images_root=t.root))
        self.assertIn("幅・高さ 41x30 が画像ファイル 40x30 と一致しません", errs)
        self.assertIn("sha256 が画像ファイルと一致しません", errs)

    def test_data_after_eoi_is_error(self):
        with TempJsonl() as t:
            rec = row()
            self.make(t, rec)
            path = t.root / rec["file_name"]
            data = path.read_bytes() + b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00GPSDATA!\xff\xd9"
            path.write_bytes(data)
            rec["image_info"]["sha256"] = hashlib.sha256(data).hexdigest()
            t.path.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
            errs = [i.message for i in issues(t.path, "error", images_root=t.root)]
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("EOI以降のデータ", errs[0])

    def test_missing_file(self):
        with TempJsonl(row()) as t:
            errs = [i.message for i in issues(t.path, "error", images_root=t.root)]
        self.assertTrue(any("画像ファイルがありません" in e for e in errs), errs)


if __name__ == "__main__":
    unittest.main()
