"""e-Stat のファイル（CSV・xlsx）から点を読む（src/koten/stats/files.py）。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import openpyxl
import pytest

from koten.stats.files import cells, file_points, read_grid
from koten.stats.series import Series, SeriesError, segments

FETCHED = "2026-11-27T00:00:00+09:00"


def make_series(parts: list[dict], period_kind: str = "年") -> Series:
    return Series(
        series_id="t_file",
        themes=("孤独",),
        label="テスト",
        unit="％",
        survey="調査",
        publisher="省",
        source_kind="estat_file",
        period_kind=period_kind,
        selector={"parts": parts},
        table_title="",
        url="",
        latest_known="",
        breaks=(),
        trend="",
        trend_from="",
        trend_to="",
        trend_note="",
        status="draft",
        checked_by="",
        checked_at="",
        checked_sha="",
        where="",
        note="",
    )


def part(**kw) -> dict:
    base = {"statInfId": "000001", "fileKind": "0", "updated": "2026-01-01", "sheet": ""}
    base.update(kw)
    return base


def fetch_local(*paths: Path):
    """呼ばれた順に、決めたファイルを返す fetch。"""
    it = iter(paths)
    calls: list[tuple[str, str]] = []

    def fetch(url: str, rel: str):
        calls.append((url, rel))
        return next(it), FETCHED

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def write_xlsx(path: Path, rows: list[list], sheet: str = "表1", fmts: dict | None = None) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for r in rows:
        ws.append(r)
    for ref, f in (fmts or {}).items():
        ws[ref].number_format = f
    wb.save(path)
    return path


CSV_ROWS = [
    ["表題", "", "", ""],
    ["", "2021年", "2022年", "2023年"],
    ["孤独感が　ある人", "39.5", "40.0", "40.1"],
]


def write_csv(path: Path, rows: list[list[str]], enc: str) -> Path:
    path.write_bytes("\n".join(",".join(r) for r in rows).encode(enc))
    return path


def test_csv_cp932_and_utf8sig(tmp_path):
    s = make_series([part(values="B3:D3", periods_cells="B2:D2", label_cells=["A3"])])
    a = write_csv(tmp_path / "a.bin", CSV_ROWS, "cp932")
    b = write_csv(tmp_path / "b.bin", CSV_ROWS, "utf-8-sig")
    pa = file_points(s, fetch_local(a))
    pb = file_points(s, fetch_local(b))
    key = lambda ps: [(p.period, p.value, p.mark, p.src_label) for p in ps]  # noqa: E731
    assert (
        key(pa)
        == key(pb)
        == [
            ("2021", "39.5", "", "孤独感が　ある人"),
            ("2022", "40.0", "", "孤独感が　ある人"),
            ("2023", "40.1", "", "孤独感が　ある人"),
        ]
    )
    assert pa[0].fetched_at == FETCHED
    assert pa[0].src_sha == hashlib.sha256(a.read_bytes()).hexdigest()
    assert pa[0].part == "0" and pa[0].series_id == "t_file"


def test_fetch_gets_url_and_rel_path(tmp_path):
    s = make_series([part(statInfId="0003", fileKind="1", values="B3:B3", periods=["2021"])])
    f = fetch_local(write_csv(tmp_path / "a", CSV_ROWS, "utf-8-sig"))
    file_points(s, f)
    assert f.calls == [
        (
            "https://www.e-stat.go.jp/stat-search/file-download?statInfId=0003&fileKind=1",
            "files/0003_1",
        )
    ]


def test_xlsx_row_range_with_marks(tmp_path):
    x = write_xlsx(
        tmp_path / "a.bin",
        [[], [], ["", 2021, 2022, 2023], [], ["x", 39.5, "-", 40.1]],
    )
    s = make_series([part(sheet="表1", values="B5:D5", periods_cells="B3:D3")])
    pts = file_points(s, fetch_local(x))
    assert [(p.period, p.value, p.mark) for p in pts] == [
        ("2021", "39.5", ""),
        ("2022", "", "-"),
        ("2023", "40.1", ""),
    ]


def test_xlsx_number_format_decimals_and_integers(tmp_path):
    x = write_xlsx(
        tmp_path / "a.bin",
        [["", 2021, 2022, 2023], ["x", 40, 40, 1234]],
        fmts={"B2": "0.0", "C2": "#,##0.0", "D2": "General"},
    )
    s = make_series([part(sheet="表1", values="B2:D2", periods_cells="B1:D1")])
    assert [p.value for p in file_points(s, fetch_local(x))] == ["40.0", "40.0", "1234"]


def test_empty_cell_skipped(tmp_path):
    x = write_xlsx(tmp_path / "a.bin", [["", 2021, 2022], ["x", 1.5, None]])
    s = make_series([part(sheet="表1", values="B2:C2", periods_cells="B1:C1")])
    assert [p.period for p in file_points(s, fetch_local(x))] == ["2021"]


def test_column_range_and_multi_letter_columns(tmp_path):
    x = write_xlsx(tmp_path / "a.bin", [[2021, 1.5], [2022, 2.5]])
    s = make_series([part(sheet="表1", values="B1:B2", periods_cells="A1:A2")])
    assert [(p.period, p.value) for p in file_points(s, fetch_local(x))] == [
        ("2021", "1.5"),
        ("2022", "2.5"),
    ]
    wide = [[""] * 26 + ["AA", "AB"], [""] * 26 + ["7", "8"]]
    g = read_grid(write_xlsx(tmp_path / "w.bin", wide), "表1")
    assert cells(g, "AA1:AB1") == ["AA", "AB"]
    assert cells(g, "AA2") == ["7"]


def test_xls_signature_refused(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 20)
    with pytest.raises(SeriesError, match=r"\.xls"):
        read_grid(p, "")


def test_csv_with_sheet_refused(tmp_path):
    p = write_csv(tmp_path / "a", CSV_ROWS, "utf-8-sig")
    with pytest.raises(SeriesError, match="sheet"):
        read_grid(p, "表1")


def test_unknown_sheet_lists_available(tmp_path):
    x = write_xlsx(tmp_path / "a.bin", [[1]], sheet="表1")
    with pytest.raises(SeriesError, match="表1"):
        read_grid(x, "無い")


def test_two_dimensional_range_refused():
    with pytest.raises(SeriesError):
        cells([["1", "2"], ["3", "4"]], "A1:B2")


def test_cells_outside_grid_are_empty():
    assert cells([["1"]], "A1:C1") == ["1", "", ""]
    assert cells([["1"]], "Z9") == [""]


def test_grid_rows_are_padded(tmp_path):
    g = read_grid(write_csv(tmp_path / "a", [["a"], ["b", "c", "d"]], "utf-8-sig"), "")
    assert [len(r) for r in g] == [3, 3]


def test_period_kind_mismatch(tmp_path):
    p = write_csv(tmp_path / "a", [["", "2021年度"], ["x", "1"]], "utf-8-sig")
    s = make_series([part(values="B2:B2", periods_cells="B1:B1")], period_kind="年")
    with pytest.raises(SeriesError):
        file_points(s, fetch_local(p))


def test_count_mismatch(tmp_path):
    p = write_csv(tmp_path / "a", CSV_ROWS, "utf-8-sig")
    s = make_series([part(values="B3:D3", periods=["2021", "2022"])])
    with pytest.raises(SeriesError, match="数"):
        file_points(s, fetch_local(p))


def test_periods_cells_that_do_not_line_up_are_refused_before_any_fetch():
    s = make_series([part(values="B3:D3", periods_cells="B2:C2")])
    with pytest.raises(SeriesError, match="1 番目の part の periods_cells.*並んでいない"):
        file_points(s, lambda url, rel: pytest.fail("取りに行かない"))


def test_wareki_period_cells_and_average(tmp_path):
    p = write_csv(tmp_path / "a", [["", "令和3年", "令和4年平均"], ["x", "1", "2"]], "utf-8-sig")
    s = make_series([part(values="B2:C2", periods_cells="B1:C1")])
    assert [p.period for p in file_points(s, fetch_local(p))] == ["2021", "2022"]


def test_periods_list_must_be_four_digits(tmp_path):
    p = write_csv(tmp_path / "a", CSV_ROWS, "utf-8-sig")
    s = make_series([part(values="B3:C3", periods=["2021", "22"])])
    with pytest.raises(SeriesError):
        file_points(s, fetch_local(p))


def two_parts(tmp_path, label2="孤独感がある人", year2="2022"):
    f1 = write_csv(tmp_path / "f1", [["", "2021"], ["孤独感が　ある人", "39.5"]], "utf-8-sig")
    f2 = write_csv(tmp_path / "f2", [["", year2], [label2, "40.1"]], "utf-8-sig")
    s = make_series(
        [
            part(statInfId="1", values="B2:B2", periods_cells="B1:B1", label_cells=["A2"]),
            part(statInfId="2", values="B2:B2", periods_cells="B1:B1", label_cells=["A2"]),
        ]
    )
    return s, fetch_local(f1, f2)


def test_two_parts_with_different_label_text_make_a_break(tmp_path):
    s, f = two_parts(tmp_path)
    pts = file_points(s, f)
    assert [p.part for p in pts] == ["0", "1"]
    assert segments(s, pts) == {"2021": 0, "2022": 1}


def test_overlapping_years_between_parts_is_refused(tmp_path):
    s, f = two_parts(tmp_path, year2="2021")
    with pytest.raises(SeriesError, match="2021"):
        file_points(s, f)


def test_label_cells_unit_and_sorted(tmp_path):
    rows = [["", "2023", "2021"], ["ラベル", "1", "2"], ["％", "", ""]]
    p = write_csv(tmp_path / "a", rows, "utf-8-sig")
    s = make_series(
        [
            part(
                values="B2:C2",
                periods_cells="B1:C1",
                label_cells=["A2", "A9", "B3"],
                unit_cell="A3",
            )
        ]
    )
    pts = file_points(s, fetch_local(p))
    assert [x.period for x in pts] == ["2021", "2023"]
    assert pts[0].src_label == "ラベル" and pts[0].src_unit == "％"


# --- 修正 1 回目 ---------------------------------------------------------------


def _one(tmp_path, rows, fmts=None, values="B2:B2", periods_cells="B1:B1", **kw):
    x = write_xlsx(tmp_path / "a.bin", rows, fmts=fmts)
    s = make_series([part(sheet="表1", values=values, periods_cells=periods_cells, **kw)])
    return file_points(s, fetch_local(x))


def test_percent_format_renders_as_displayed(tmp_path):
    rows = [["", 2021, 2022, 2023], ["x", 0.395, 0.4, 0.1 + 0.2]]
    pts = _one(
        tmp_path,
        rows,
        values="B2:D2",
        periods_cells="B1:D1",
        fmts={"B2": "0.0%", "C2": "0%", "D2": "0.0%"},
    )
    assert [p.value for p in pts] == ["39.5", "40", "30.0"]


def test_general_non_integral_number(tmp_path):
    pts = _one(tmp_path, [["", 2021], ["x", 39.5]])
    assert pts[0].value == "39.5"


def test_bool_and_date_value_cells_refused(tmp_path):
    import datetime

    with pytest.raises(SeriesError, match="真偽値"):
        _one(tmp_path, [["", 2021], ["x", True]])
    with pytest.raises(SeriesError, match="日付"):
        _one(tmp_path, [["", 2021], ["x", datetime.datetime(2021, 1, 1)]])


def test_date_period_cell_refused(tmp_path):
    import datetime

    with pytest.raises(SeriesError, match="年の欄が日付"):
        rows = [[datetime.datetime(2021, 1, 1)], [1.5]]
        _one(tmp_path, rows, values="A2:A2", periods_cells="A1:A1")


def test_csv_error_and_undecodable_name_the_part(tmp_path):
    s = make_series([part(values="B2:B2", periods_cells="B1:B1")])
    bad = tmp_path / "bad"
    bad.write_bytes(b"\x81\x20\xff\xfe\x80\n")  # utf-8 でも cp932 でも読めない
    with pytest.raises(SeriesError, match="1 番目の part"):
        file_points(s, fetch_local(bad))
    big = tmp_path / "big"
    big.write_text('a,"' + "x" * 200000 + "\n", encoding="utf-8")  # 閉じない引用＋巨大な欄
    with pytest.raises(SeriesError):
        file_points(s, fetch_local(big))


@pytest.mark.parametrize("key", ["statInfId", "fileKind", "values", "periods_cells"])
def test_missing_required_key_is_series_error(tmp_path, key):
    spec = part(values="B2:B2", periods_cells="B1:B1")
    spec.pop(key, None)
    s = make_series([spec])
    p = write_csv(tmp_path / "a", CSV_ROWS, "utf-8-sig")
    with pytest.raises(SeriesError, match=f"1 番目の part.*{key}|1 番目の part.*periods"):
        file_points(s, fetch_local(p))


def test_human_messages_count_parts_from_one(tmp_path):
    s, f = two_parts(tmp_path, year2="2021")
    with pytest.raises(SeriesError, match="1 番目と 2 番目の part"):
        file_points(s, f)


def test_bom_with_ragged_rows(tmp_path):
    p = tmp_path / "a"
    p.write_bytes("﻿,2021,2022\nx,1.5\n".encode())
    s = make_series([part(values="B2:C2", periods_cells="B1:C1")])
    assert [(x.period, x.value) for x in file_points(s, fetch_local(p))] == [("2021", "1.5")]


def test_part_ids_must_be_digits_before_any_fetch():
    s = make_series([part(statInfId="../etc", fileKind="0", values="B3:B3", periods=["2021"])])
    calls = []
    with pytest.raises(SeriesError, match="半角の数字だけ"):
        file_points(s, lambda url, rel: calls.append(rel))
    assert calls == []


# --- 最後の見直しの直し：部分の欄の型・ファイルでない応答（R30・F7） -------------------------


@pytest.mark.parametrize(
    "key,bad",
    [
        ("label_cells", None), ("label_cells", [3]), ("label_cells", "A3"), ("values", None),
        ("values", ["B2:C2"]), ("values", 5), ("periods_cells", ["B1:C1"]), ("periods_cells", 5),
        ("unit_cell", 5), ("unit_cell", None), ("sheet", None), ("statInfId", 1),
    ],
)  # fmt: skip
def test_wrong_types_are_series_errors_before_any_fetch(key, bad):
    spec = part(values="B2:C2", periods_cells="B1:C1", label_cells=["A2"]) | {key: bad}
    with pytest.raises(SeriesError, match=f"1 番目の part.*{key}"):
        file_points(make_series([spec]), lambda url, rel: pytest.fail("取りに行かない"))


@pytest.mark.parametrize("bad", [2021, [2021], None])
def test_explicit_periods_of_the_wrong_type_are_series_errors(bad):
    spec = part(values="B2:B2", periods=bad)
    with pytest.raises(SeriesError, match="1 番目の part.*periods"):
        file_points(make_series([spec]), lambda url, rel: pytest.fail("取りに行かない"))


@pytest.mark.parametrize("parts", [None, "x", 5])
def test_parts_that_are_not_a_list_are_a_series_error(parts):
    s = make_series([])
    s = Series(**{**s.__dict__, "selector": {"parts": parts}})
    with pytest.raises(SeriesError, match="parts"):
        file_points(s, lambda url, rel: pytest.fail("取りに行かない"))


@pytest.mark.parametrize(
    "body,why",
    [
        (b"<!DOCTYPE html><html><body>maintenance</body></html>", "HTML"),
        (b"\xef\xbb\xbf  <html lang='ja'>x</html>", "HTML"),
        (b"%PDF-1.7 ...", "PDF"),
        (b"\x00\x01\x02binary", "中身の分からない"),
        (b"", "空"),
    ],
)  # fmt: skip
def test_bodies_that_are_not_csv_or_xlsx_are_refused(tmp_path, body, why):
    from koten.stats.files import file_problem

    assert why in (file_problem(body) or "")
    p = tmp_path / "a"
    p.write_bytes(body)
    with pytest.raises(SeriesError, match=why):
        read_grid(p, "")


def test_csv_and_xlsx_bodies_pass_the_check(tmp_path):
    from koten.stats.files import file_problem

    csv_p = write_csv(tmp_path / "a", CSV_ROWS, "cp932")
    x = write_xlsx(tmp_path / "b.bin", [[1]])
    assert file_problem(csv_p.read_bytes()) is None and file_problem(x.read_bytes()) is None
    assert file_problem("<参考>,2021\nx,1\n".encode()) is None  # 「<」で始まる見出しの CSV


# --- xlsx の数を、Excel が見せるとおりに（R31） ------------------------------------------------


def _render(value, fmt):
    from koten.stats.files import _render_number

    return _render_number(value, fmt)


ACCOUNTING = '_ * #,##0_ ;_ * \\-#,##0_ ;_ * "-"_ ;_ @_ '
ACCOUNTING_2 = '_ * #,##0.00_ ;_ * \\-#,##0.00_ ;_ * "-"??_ ;_ @_ '


@pytest.mark.parametrize(
    "value,fmt,want",
    [
        # 0 の区画が文字だけ：その文字（印になる。0 にしない）
        (0, '#,##0;"△"#,##0;"-"', "-"),
        (0, '0.0%;"△"0.0%;"-"', "-"),
        (0, '#,##0;"▲ "#,##0;"―"', "―"),
        (0, '#,##0;-#,##0;"－"', "－"),
        (0, '#,##0.0;-#,##0.0;"…"', "…"),
        (0.0, ACCOUNTING, "-"),
        (0, ACCOUNTING_2, "-"),
        (0, "#,##0;-#,##0;", ""),  # 0 の区画が空：Excel は何も出さない（点にしない）
        # 0 の区画に数の桁があれば、数
        (0, "0.0;-0.0;0.0", "0.0"),
        (0, "0.0", "0.0"),
        (0, "General", "0"),
        (0.0001, '#,##0;-#,##0;"-"', "0"),  # 0 ではない
        # 正・負の区画の小数の桁は、その区画から
        (5, '#,##0;"△"#,##0;"-"', "5"),
        (-1.5, '0.0;"△"0.0;"-"', "-1.5"),
        (-1.25, '0.0;"△"0.00', "-1.25"),
        (1.25, '0.0;"△"0.00', "1.3"),
        (-2.5, "0", "-3"),
        # 四捨五入は 0 から遠いほうへ（Excel）
        (0.25, "0.0", "0.3"),
        (-0.25, "0.0", "-0.3"),
        (8.85, "0.0", "8.9"),
        (39.5, "0", "40"),
        (2.5, "0", "3"),
        (1234.56, "#,##0", "1235"),
        (1.005, "0.00", "1.01"),
        (-0.04, "0.0", "0.0"),
        # 数の部分の終わりのカンマ1つごとに 1000 で割る
        (1234567, "#,##0.0,", "1234.6"),
        (1234567890, "#,##0,,", "1235"),
        (1500, "0,", "2"),
        (1234567, '#,##0,"千人"', "1235"),
        # パーセントは 100 倍
        (0.395, "0.0%", "39.5"),
        (0.4, "0%", "40"),
        (0.1 + 0.2, "0.0%", "30.0"),
        # 引用の文字・\x・[..] は数に入れない
        (39.45, '0.0"人"', "39.5"),
        (39.45, "[Red]0.0", "39.5"),
        (39.45, "0.0\\人", "39.5"),
        (1234.5, "[$-411]#,##0", "1235"),
        (78.85, "#,##0.0_ ", "78.9"),
        (-78.85, '#,##0.0_ ;[Red]\\-#,##0.0\\ ', "-78.9"),
        # 文字だけの区画は、正・負でもその文字
        (12, '"x";"x";"x"', "x"),
        # 「#」だけの書式の 0 は空（Excel は何も出さない）
        (0, "#,###", ""),
        # 決まりの外の書式は General の見せ方
        (12345, "0.00E+00", "12345"),
        (1.5, "# ?/?", "1.5"),
        (39.45, "[>=100]0;0.0", "39.45"),
        (39.5, "@", "39.5"),
        (39.5, "General", "39.5"),
        (40, "General", "40"),
        (0.1 + 0.2, "General", "0.3"),  # float の誤差を出さない
        (39.449999999999996, "0.0", "39.5"),
        (0.39449999999999996, "0.0%", "39.5"),
        (1.5e40, "#,##0", "15000000000000000000000000000000000000000"),  # 桁があふれない
    ],
)  # fmt: skip
def test_numbers_render_as_excel_shows_them(value, fmt, want):
    assert _render(value, fmt) == want


def _patch_values(path: Path, real: dict[str, str]) -> Path:
    """保存した xlsx の <v> の文字を差し替える（openpyxl は短い形で書くため）。"""
    import zipfile

    src = zipfile.ZipFile(path)
    items = {n: src.read(n) for n in src.namelist()}
    src.close()
    xml = items["xl/worksheets/sheet1.xml"].decode()
    for short, long in real.items():
        xml = xml.replace(f"<v>{short}</v>", f"<v>{long}</v>")
    items["xl/worksheets/sheet1.xml"] = xml.encode()
    with zipfile.ZipFile(path, "w") as z:
        for n, b in items.items():
            z.writestr(n, b)
    return path


def test_float_noise_in_the_file_is_removed(tmp_path):
    x = write_xlsx(
        tmp_path / "a.bin",
        [["", 2021, 2022, 2023], ["x", 0.3, 39.45, 0.3945]],
        fmts={"B2": "General", "C2": "0.0", "D2": "0.0%"},
    )
    _patch_values(
        x, {"0.3": "0.30000000000000004", "39.45": "39.449999999999996",
            "0.3945": "0.39449999999999996"},
    )  # fmt: skip
    s = make_series([part(sheet="表1", values="B2:D2", periods_cells="B1:D1")])
    assert [p.value for p in file_points(s, fetch_local(x))] == ["0.3", "39.5", "39.5"]


def test_a_zero_shown_as_a_dash_is_a_mark_in_the_grid_points_and_trend(tmp_path):
    from dataclasses import replace

    from koten.stats.series import verify_trend

    dash = '#,##0;"△"#,##0;"-"'
    x = write_xlsx(
        tmp_path / "a.bin",
        [["", 2019, 2020, 2021, 2022], ["x", 0, 120, 340, 560]],
        fmts={f"{c}2": dash for c in "BCDE"},
    )
    assert read_grid(x, "表1")[1] == ["x", "-", "120", "340", "560"]
    s = make_series([part(sheet="表1", values="B2:E2", periods_cells="B1:E1")])
    pts = file_points(s, fetch_local(x))
    assert [(p.period, p.value, p.mark) for p in pts][0] == ("2019", "", "-")
    trend = replace(s, trend="増加", trend_from="2019", trend_to="2022", trend_note="n")
    ok, why = verify_trend(trend, pts)
    assert not ok and "2019" in why
