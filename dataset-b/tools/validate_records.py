#!/usr/bin/env python3
"""metadata.jsonl（record.v1）を検証する。

    python3 tools/validate_records.py FILE.jsonl [--images-root DIR] [--strict]

schema/record.v1.schema.json による検査のあと、スキーマでは表しにくい項目間の整合性と
キャプションの書式を確認する。--images-root を付けると画像ファイルそのものも確認する
（DIR は file_name の基準。通常は metadata.jsonl と同じフォルダ）。
エラーが1件でもあれば終了コード 1。--strict では警告もエラーとして扱う。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:
    sys.exit("jsonschema が見つかりません。python3 -m pip install -r requirements.txt を実行してください。")

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "record.v1.schema.json"

# JIS X 0401 都道府県コード → (日本語名, 英語名)
PREFECTURES = {
    "01": ("北海道", "Hokkaido"), "02": ("青森県", "Aomori"), "03": ("岩手県", "Iwate"),
    "04": ("宮城県", "Miyagi"), "05": ("秋田県", "Akita"), "06": ("山形県", "Yamagata"),
    "07": ("福島県", "Fukushima"), "08": ("茨城県", "Ibaraki"), "09": ("栃木県", "Tochigi"),
    "10": ("群馬県", "Gunma"), "11": ("埼玉県", "Saitama"), "12": ("千葉県", "Chiba"),
    "13": ("東京都", "Tokyo"), "14": ("神奈川県", "Kanagawa"), "15": ("新潟県", "Niigata"),
    "16": ("富山県", "Toyama"), "17": ("石川県", "Ishikawa"), "18": ("福井県", "Fukui"),
    "19": ("山梨県", "Yamanashi"), "20": ("長野県", "Nagano"), "21": ("岐阜県", "Gifu"),
    "22": ("静岡県", "Shizuoka"), "23": ("愛知県", "Aichi"), "24": ("三重県", "Mie"),
    "25": ("滋賀県", "Shiga"), "26": ("京都府", "Kyoto"), "27": ("大阪府", "Osaka"),
    "28": ("兵庫県", "Hyogo"), "29": ("奈良県", "Nara"), "30": ("和歌山県", "Wakayama"),
    "31": ("鳥取県", "Tottori"), "32": ("島根県", "Shimane"), "33": ("岡山県", "Okayama"),
    "34": ("広島県", "Hiroshima"), "35": ("山口県", "Yamaguchi"), "36": ("徳島県", "Tokushima"),
    "37": ("香川県", "Kagawa"), "38": ("愛媛県", "Ehime"), "39": ("高知県", "Kochi"),
    "40": ("福岡県", "Fukuoka"), "41": ("佐賀県", "Saga"), "42": ("長崎県", "Nagasaki"),
    "43": ("熊本県", "Kumamoto"), "44": ("大分県", "Oita"), "45": ("宮崎県", "Miyazaki"),
    "46": ("鹿児島県", "Kagoshima"), "47": ("沖縄県", "Okinawa"),
}
PREF_BY_NAME = {ja: (code, en) for code, (ja, en) in PREFECTURES.items()}
JMA_SEASON = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
              6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}
CAPTION_KEYS = ("ja_short", "ja_detailed", "en_short")
PEOPLE_JA = ("人々", "人物", "女性", "男性", "観光客", "参拝客", "乗客", "通行人", "歩行者",
             "子ども", "子供", "少年", "少女", "若者", "老人", "家族連れ", "カップル")
PEOPLE_JA_RE = re.compile(r"人影|人が|人の姿|人たち|歩く人|立つ人|座る人")
PEOPLE_EN = {"person", "persons", "people", "man", "men", "woman", "women", "tourist", "tourists",
             "child", "children", "boy", "boys", "girl", "girls", "kid", "kids", "crowd", "pedestrian",
             "pedestrians", "visitor", "visitors", "passenger", "passengers", "worshipper", "worshippers"}
# 出荷画像に残してよい JPEG の APP セグメント（JFIF、ICC プロファイル、Adobe 色変換）
ALLOWED_APP = {0xE0: (b"JFIF\x00", b"JFXX\x00"), 0xE2: (b"ICC_PROFILE\x00",), 0xEE: (b"Adobe",)}
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
CATEGORIES = tuple(json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["properties"]["category"]["enum"])


@dataclass
class Issue:
    line: int  # 0 = ファイル全体
    level: str  # "error" | "warning"
    message: str

    def __str__(self) -> str:
        where = f"{self.line}行目" if self.line else "全体"
        return f"{where}: [{'エラー' if self.level == 'error' else '警告'}] {self.message}"


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    rows: int = 0
    categories: Counter = field(default_factory=Counter)
    judgments: Counter = field(default_factory=Counter)

    def add(self, line: int, level: str, message: str) -> None:
        self.issues.append(Issue(line, level, message))

    def count(self, level: str) -> int:
        return sum(i.level == level for i in self.issues)


def show(value) -> str:
    s = json.dumps(value, ensure_ascii=False)
    return s if len(s) <= 40 else s[:37] + "..."


def json_path(path) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else p)
    return out or "レコード"


def schema_message(e) -> str:
    v, val, inst = e.validator, e.validator_value, e.instance
    if v == "required":
        msg = "必須キーがありません: " + ", ".join(k for k in val if k not in inst)
    elif v == "additionalProperties":
        extra = ", ".join(sorted(set(inst) - set(e.schema.get("properties", {}))))
        msg = f"定義にないキーがあります: {extra}（緯度経度・正確な日時などは入れられません）"
    elif v == "type":
        msg = f"型が違います（期待: {' または '.join(val) if isinstance(val, list) else val}、値: {show(inst)}）"
    elif v == "enum":
        choices = "、".join(show(x) for x in val) if len(val) <= 10 else "スキーマを参照"
        msg = f"許可されていない値です: {show(inst)}（候補: {choices}）"
    elif v == "const":
        msg = f"値は {show(val)} にしてください（値: {show(inst)}）"
    elif v == "pattern":
        msg = f"形式が違います: {show(inst)}（正規表現 {val}）"
    elif v == "minimum":
        msg = f"{val} 以上にしてください（値: {show(inst)}）"
    elif v == "minLength":
        msg = "空文字にはできません"
    elif v == "uniqueItems":
        msg = f"同じ値が重複しています: {show(inst)}"
    elif v == "minItems":
        msg = f"{val} 件以上必要です"
    elif v == "maxItems":
        msg = "空の配列にしてください" if val == 0 else f"{val} 件以下にしてください"
    else:
        msg = e.message
    return f"{json_path(e.absolute_path)}: {msg}"


def check_location(loc):
    if not isinstance(loc, dict):
        return
    pref, code, muni = loc.get("prefecture"), loc.get("municipality_code"), loc.get("municipality")
    code_ok = isinstance(code, str) and re.fullmatch(r"[0-9]{5}", code) is not None
    if isinstance(code, str) and not code_ok:
        yield f"location.municipality_code は5桁の数字にしてください（値: {show(code)}）"
    if pref in PREF_BY_NAME:
        pcode, pen = PREF_BY_NAME[pref]
        if loc.get("prefecture_en") != pen:
            yield f"location.prefecture_en が prefecture と合いません（{pref} は {pen}、値: {show(loc.get('prefecture_en'))}）"
        if code_ok:
            if code[:2] != pcode:
                yield f"location.municipality_code の先頭2桁 {code[:2]} が {pref} の都道府県コード {pcode} と合いません"
            elif code[2:] == "000":
                yield "location.municipality_code が都道府県のコードです。市区町村のコードを入れてください"
    if isinstance(muni, str) and muni and not muni.endswith(("市", "区", "町", "村")):
        yield f"location.municipality は市・区・町・村で終わる市区町村名にしてください（それより細かい地名は不可。値: {show(muni)}）"


def check_capture(loc, cap):
    if not isinstance(cap, dict):
        return
    ym, season = cap.get("year_month"), cap.get("season")
    if not (isinstance(ym, str) and re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", ym)):
        if isinstance(ym, str):
            yield f"capture.year_month は YYYY-MM にしてください（値: {show(ym)}）"
        return
    if ym > date.today().strftime("%Y-%m"):
        yield f"capture.year_month が未来の年月です（{ym}）。撮影日時の設定を確認してください"
    if loc is None and season is not None:
        yield f"location が null の行は capture.season も null にしてください（南北どちらの半球か不明なため。値: {show(season)}）"
    elif isinstance(loc, dict) and season != JMA_SEASON[int(ym[5:])]:
        yield f"capture.season が撮影月と合いません（{ym} は気象庁の区分で {JMA_SEASON[int(ym[5:])]}、値: {show(season)}）"


def check_verification(ver):
    if not isinstance(ver, dict):
        return
    verified, judgment, fields = ver.get("human_verified"), ver.get("judgment"), ver.get("error_fields")
    n = len(fields) if isinstance(fields, list) else 0
    if verified is False and (judgment is not None or n):
        yield "verification: human_verified が false の行は judgment を null、error_fields を空にしてください"
    if verified is True and judgment is None:
        yield "verification: human_verified が true の行は judgment（correct / minor_error / major_error）が必要です"
    if judgment == "correct" and n:
        yield "verification: judgment が correct の行は error_fields を空にしてください"
    if judgment in ("minor_error", "major_error") and not n:
        yield f"verification: judgment が {judgment} の行は error_fields に誤りのあった項目を入れてください"


def check_captions(caps):
    """(level, message) を返す。"""
    if not isinstance(caps, dict):
        return
    ja_s, ja_d, en = (caps.get(k) if isinstance(caps.get(k), str) else None for k in CAPTION_KEYS)
    for key in CAPTION_KEYS:
        text = caps.get(key)
        if isinstance(text, str) and (text != text.strip() or "\n" in text):
            yield "error", f"captions.{key}: 前後の空白や改行を取り除いてください"
    if ja_s is not None:
        if ja_s.count("。") != 1 or not ja_s.endswith("。"):
            yield "error", "captions.ja_short: 「。」で終わる1文にしてください（「。」は文末の1つだけ）"
        if not 15 <= len(ja_s) <= 60:
            yield "error", f"captions.ja_short: 15〜60文字にしてください（{len(ja_s)}文字）"
    if ja_d is not None:
        n = ja_d.count("。")
        if not ja_d.endswith("。") or not 3 <= n <= 5 or "。。" in ja_d:
            yield "error", f"captions.ja_detailed: 「。」で終わる文を3〜5文にしてください（「。」が{n}個）"
        if not 80 <= len(ja_d) <= 300:
            yield "error", f"captions.ja_detailed: 合計80〜300文字にしてください（{len(ja_d)}文字）"
    if en is not None:
        if not en.endswith(".") or re.search(r"[.!?]\s", en):
            yield "error", "captions.en_short: \".\" で終わる1文にしてください"
        if not 5 <= len(en.split()) <= 30:
            yield "error", f"captions.en_short: 5〜30語にしてください（{len(en.split())}語）"

    for key, text in (("ja_short", ja_s), ("ja_detailed", ja_d)):
        if text is None:
            continue
        if text.startswith(("この写真", "この画像")):
            yield "warning", f"captions.{key}: 「この写真」「この画像」で書き始めないでください"
        if re.search(r"写真|画像|撮影|写って", text):
            yield "warning", f"captions.{key}: 「写真」「画像」「撮影」「写っている」は使わないでください"
        if "日本" in text:
            yield "warning", f"captions.{key}: 「日本」は書かないでください（地名）"
        if hits := list(dict.fromkeys([w for w in PEOPLE_JA if w in text] + PEOPLE_JA_RE.findall(text))):
            yield "warning", f"captions.{key}: 人物を表す語があります（{'、'.join(hits)}）。人物が写っていれば行ごと除外してください"
        if letters := re.findall(r"[A-Za-zＡ-Ｚａ-ｚ]+", text):
            yield "warning", f"captions.{key}: 英字があります（{', '.join(letters)}）。固有名詞でないか確認してください"
        if re.search(r"(です|ます|でした|ました|ません|でしょう)。", text):
            yield "warning", f"captions.{key}: です・ます調の文があります。常体で書いてください"
    if en is not None:
        words = re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)*", en)
        if hits := sorted({w for w in words if w.lower() in PEOPLE_EN}):
            yield "warning", f"captions.en_short: 人物を表す語があります（{', '.join(hits)}）。人物が写っていれば行ごと除外してください"
        if re.search(r"\bJapan(ese)?\b", en):
            yield "warning", 'captions.en_short: "Japan" "Japanese" は書かないでください（地名）'
        if re.match(r"(this|the|a|an)\s+(photo|photograph|image|picture|shot)\b", en, re.I):
            yield "warning", "captions.en_short: \"This photo\" \"An image of\" のような書き出しにしないでください"
        if capitalized := [w for w in words[1:] if w[0].isupper() and w != "I"]:
            yield "warning", (f"captions.en_short: 文中に大文字で始まる語があります（{', '.join(capitalized)}）。"
                              "固有名詞でないか確認してください")
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", en):
            yield "warning", "captions.en_short: 日本語の文字が含まれています"
    for key, text in zip(CAPTION_KEYS, (ja_s, ja_d, en), strict=True):
        if text is not None and (big := [n for n in re.findall(r"\d+", text) if int(n) > 5]):
            yield "warning", f"captions.{key}: 6以上の数があります（{', '.join(big)}）。数は5以下の数えやすいものだけにしてください"


def check_caption_output(obj) -> list[tuple[str, str]]:
    """VLM の出力1件（caption_output.v1）の書式を検査し、(level, message) を返す。生成スクリプトが形のチェックの後に呼ぶ。"""
    if not isinstance(obj, dict):
        return [("error", "VLM の出力が JSON オブジェクトになっていません")]
    out = list(check_captions(obj))
    flags = obj.get("flags")
    if isinstance(flags, list) and any(flags.count(f) > 1 for f in flags):
        out.append(("error", "flags: 同じ値が重複しています"))
    if obj.get("category") not in CATEGORIES:
        out.append(("error", f"category: 許可されていない値です（値: {show(obj.get('category'))}）"))
    return out


def control_chars(value, path):
    """改行や制御文字を含む文字列の (パス, 値) を返す。"""
    if isinstance(value, str):
        if CONTROL_CHARS.search(value):
            yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from control_chars(v, (*path, k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from control_chars(v, (*path, i))


def semantic_checks(rec: dict):
    """スキーマ外の検査。(トップレベルのキー, level, message) を返す。"""
    for key, value in rec.items():
        if key != "captions":  # キャプションは check_captions で見る
            for path, v in control_chars(value, (key,)):
                yield key, "error", f"{json_path(path)}: 改行や制御文字を含めないでください（値: {show(v)}）"
    rid = rec.get("id")
    if isinstance(rid, str) and rec.get("file_name") != f"images/{rid}.jpg":
        yield "file_name", "error", f"file_name は \"images/{rid}.jpg\" にしてください（値: {show(rec.get('file_name'))}）"
    for msg in check_location(rec.get("location")):
        yield "location", "error", msg
    for msg in check_capture(rec.get("location"), rec.get("capture")):
        yield "capture", "error", msg
    for msg in check_verification(rec.get("verification")):
        yield "verification", "error", msg
    for level, msg in check_captions(rec.get("captions")):
        yield "captions", level, msg


def check_record(rec, validator) -> list[tuple[str, str]]:
    """1行分の (level, message) を返す。"""
    schema_errors = sorted(validator.iter_errors(rec), key=lambda e: [str(p) for p in e.absolute_path])
    if not isinstance(rec, dict):
        return [("error", "1行が1つの JSON オブジェクトになっていません")]
    found = list(semantic_checks(rec))
    flagged = {key for key, level, _ in found if level == "error"}
    out = []
    for e in schema_errors:
        top = str(e.absolute_path[0]) if e.absolute_path else ""
        # if/then の規則は semantic_checks でも検査し、そちらの方が分かりやすいので重複分は出さない
        if not ("then" in e.schema_path and top in flagged):
            out.append(("error", schema_message(e)))
    return out + [(level, msg) for _, level, msg in found]


def data_after_eoi(data: bytes, sos: int) -> bool:
    """最初の SOS のあとの最初の EOI（FF D9）より後ろに、空白以外のデータがあれば True。

    圧縮データの中の 0xFF は FF 00 に置き換えられ、リスタートマーカーは FF D0〜D7 なので、
    SOS のあとに最初に現れる FF D9 が主画像の終わり（EOI）になる。
    """
    eoi = data.find(b"\xff\xd9", sos + 2)
    return eoi != -1 and bool(data[eoi + 2:].strip(b" \t\r\n\x00"))


def jpeg_metadata(data: bytes) -> list[str]:
    """画像データの開始（SOS）までにある、残してはいけないメタデータの種類を返す。
    EOI のあとに続くデータ（MPF の副画像など）があれば、それも返す。"""
    found, i = [], 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0xD9:
            break
        if marker == 0xDA:
            if data_after_eoi(data, i):
                found.append("EOI以降のデータ（MPF の副画像など）")
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        size = int.from_bytes(data[i + 2:i + 4], "big")
        body = data[i + 4:i + 2 + size]
        if marker == 0xFE:
            found.append("COM（コメント）")
        elif 0xE0 <= marker <= 0xEF and not body.startswith(ALLOWED_APP.get(marker, ())):
            found.append("EXIF" if body.startswith(b"Exif\x00") else "XMP" if b"ns.adobe.com/xap" in body[:40]
                         else "IPTC" if marker == 0xED else "MPF" if body.startswith(b"MPF\x00") else f"APP{marker - 0xE0}")
        i += 2 + size
    return found


def check_image_file(rec: dict, root: Path, pil):
    fn, img = rec.get("file_name"), rec.get("image_info")
    if not isinstance(fn, str) or not isinstance(img, dict):
        return
    path = root / fn
    if not path.is_file():
        yield f"画像ファイルがありません: {path}"
        return
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != img.get("sha256"):
        yield "image_info.sha256 が画像ファイルと一致しません"
    if not data.startswith(b"\xff\xd8"):
        yield f"JPEG ファイルではありません: {path}"
        return
    found = jpeg_metadata(data)
    if pil is not None:
        try:
            with pil.open(path) as im:
                if (im.width, im.height) != (img.get("width"), img.get("height")):
                    yield f"image_info の幅・高さ {img.get('width')}x{img.get('height')} が画像ファイル {im.width}x{im.height} と一致しません"
                if "EXIF" in found and im.getexif().get_ifd(0x8825):
                    found.append("GPS")
        except OSError as exc:
            yield f"画像を開けません: {exc}"
    if found:
        yield f"画像にメタデータが残っています（{'、'.join(found)}）。出荷画像からは削除してください"


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if dup := sorted({k for k in keys if keys.count(k) > 1}):
        raise ValueError(f"同じキーが重複しています: {', '.join(dup)}")
    return dict(pairs)


def validate_file(path: Path, images_root: Path | None = None) -> Report:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    report = Report()
    pil = None
    if images_root is not None:
        try:
            from PIL import Image as pil
        except ImportError:
            report.add(0, "warning", "Pillow が見つからないため画像の幅・高さの照合を省略しました（python3 -m pip install Pillow）")
    seen_ids: dict[str, int] = {}
    seen_sha: dict[str, int] = {}
    for lineno, raw in enumerate(Path(path).read_bytes().splitlines(), 1):
        if not raw.strip():
            report.add(lineno, "warning", "空行があります")
            continue
        report.rows += 1
        try:
            rec = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
        except UnicodeDecodeError:
            report.add(lineno, "error", "UTF-8 として読めません")
            continue
        except json.JSONDecodeError as exc:
            report.add(lineno, "error", f"JSON として読めません: {exc.msg}（{exc.colno}文字目）")
            continue
        except ValueError as exc:
            report.add(lineno, "error", f"JSON として読めません: {exc}")
            continue
        for level, msg in check_record(rec, validator):
            report.add(lineno, level, msg)
        if not isinstance(rec, dict):
            continue
        rid, img = rec.get("id"), rec.get("image_info")
        if isinstance(rid, str):
            if rid in seen_ids:
                report.add(lineno, "error", f"id {rid} が重複しています（{seen_ids[rid]}行目と同じ）")
            seen_ids.setdefault(rid, lineno)
        sha = img.get("sha256") if isinstance(img, dict) else None
        if isinstance(sha, str):
            if sha in seen_sha:
                report.add(lineno, "warning", f"image_info.sha256 が {seen_sha[sha]}行目と同じです（同じ画像が重複している可能性）")
            seen_sha.setdefault(sha, lineno)
        if images_root is not None:
            for msg in check_image_file(rec, images_root, pil):
                report.add(lineno, "error", msg)
        if isinstance(rec.get("category"), str):
            report.categories[rec["category"]] += 1
        ver = rec.get("verification")
        if isinstance(ver, dict) and ver.get("human_verified") is True:
            report.judgments[str(ver.get("judgment"))] += 1
    if report.rows == 0:
        report.add(0, "error", "レコードが1件もありません")
    return report


def print_report(report: Report, path: Path) -> None:
    for issue in sorted(report.issues, key=lambda i: i.line):
        print(issue)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    order = schema["properties"]["category"]["enum"]
    cats = sorted(report.categories.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order))
    judged = " / ".join(f"{j} {report.judgments[j]}" for j in ("correct", "minor_error", "major_error"))
    print("---- 集計 ----")
    print(f"対象: {path}")
    print(f"行数: {report.rows}")
    print(f"エラー: {report.count('error')} 件 / 警告: {report.count('warning')} 件")
    print("カテゴリ別: " + (" / ".join(f"{c} {n}" for c, n in cats) or "なし"))
    print(f"人による確認: {sum(report.judgments.values())} 行（{judged}）")
    print("結果: " + ("NG" if report.count("error") else "OK"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="metadata.jsonl（record.v1）を検証します。")
    ap.add_argument("jsonl", type=Path, help="検証する JSONL ファイル")
    ap.add_argument("--images-root", type=Path,
                    help="file_name の基準フォルダ（通常は metadata.jsonl と同じ場所）。指定すると画像ファイルも確認します")
    ap.add_argument("--strict", action="store_true", help="警告もエラーとして扱います")
    args = ap.parse_args(argv)
    if not args.jsonl.is_file():
        print(f"ファイルがありません: {args.jsonl}")
        return 1
    if args.images_root is not None and not args.images_root.is_dir():
        print(f"--images-root のフォルダがありません: {args.images_root}")
        return 1
    report = validate_file(args.jsonl, args.images_root)
    if args.strict:
        for issue in report.issues:
            issue.level = "error"
    print_report(report, args.jsonl)
    return 1 if report.count("error") else 0


if __name__ == "__main__":
    sys.exit(main())
