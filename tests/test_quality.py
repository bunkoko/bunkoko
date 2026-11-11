"""質の検証 Q1（docs/quality.md）。作ったデータだけで確かめる（本物のデータは取らない）。"""

import importlib.util
import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdbot import quality as Q
from cfdbot.stats import expected_max_z

_spec = importlib.util.spec_from_file_location("quality_study", Path(__file__).resolve().parents[1] / "scripts" / "quality_study.py")
study = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(study)

COLS = ["SMALL LoOP", "ME1 OP2", "SMALL HiOP", "BIG LoOP", "ME2 OP2", "BIG HiOP"]


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", text)
    return buf.getvalue()


def _table(title: str, cols: list[str], idx: pd.DatetimeIndex, values: np.ndarray) -> str:
    rows = [f"{d:%Y%m}," + ",".join(f"{v * 100:8.2f}" for v in row) for d, row in zip(idx, values)]
    return f"  {title}\n," + ",".join(cols) + "\n" + "\n".join(rows) + "\n\n"


def make(edge: float = 0.0, seed: int = 0, start: str = "1990-07-31", months: int = 432, cols=COLS, gap: int | None = None,
         scale: float = 1.0, own_market: bool = False, edge_late: float | None = None) -> tuple[bytes, bytes]:
    """フレンチ教授のデータ集と同じ形の 2 つの zip。BIG HiOP = 市場 ＋ edge（月）＋ 雑音。"""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=months, freq="ME")
    mkt = rng.normal(0.004, 0.05, months)
    e = np.full(months, edge)
    if edge_late is not None:
        e[idx >= pd.Timestamp(Q.SPLIT)] = edge_late
    vals = np.column_stack([mkt + rng.normal(0, 0.012, months) for _ in cols])
    if "BIG HiOP" in cols:
        base = rng.normal(0.004, 0.05, months) if own_market else mkt
        vals[:, cols.index("BIG HiOP")] = base + e + rng.normal(0, 0.012, months)
    vals = vals * scale
    if gap is not None:
        idx, vals, mkt_i = idx.delete(gap), np.delete(vals, gap, axis=0), idx
    else:
        mkt_i = idx
    port = ("This file was created by test\n\n" + _table("Average Value Weighted Returns -- Monthly", list(cols), idx, vals)
            + _table("Average Equal Weighted Returns -- Monthly", list(cols), idx, vals * 0 + 0.5))   # 等分の表は使わないはず
    fac = np.column_stack([mkt - 0.001, rng.normal(0, 0.03, months), rng.normal(0, 0.03, months), np.full(months, 0.001)])
    return _zip(port), _zip("test\n\n" + _table("", ["Mkt-RF", "SMB", "HML", "RF"], mkt_i, fac).lstrip())


def test_edge_is_found_and_no_edge_is_not():
    port, mkt, info = Q.load(*make(edge=0.004))
    assert info["months"] == 432 and info["start"] == "1990-07" and info["columns"] == COLS and info["corr"] > 0.9
    j = Q.q1(port, mkt, 93)
    assert j["verdict"] == "使えそう" and j["t"] > j["threshold"] and j["t2"] >= 1.65
    assert abs(j["threshold"] - expected_max_z(93)) < 1e-12 and 2.4 < j["threshold"] < 2.6
    assert abs(j["gross"] - j["mean"] - Q.TURN * Q.COST) < 1e-12 and Q.TURN * Q.COST == 0.05 * 0.0010   # C3 と同じコスト
    assert j["n"] == 432 and j["start"] == pd.Timestamp("1990-07-31")
    assert Q.q1(port, mkt, 95)["threshold"] > j["threshold"]                     # 後の番号ほど基準が上がる

    for seed in range(5):                                                         # 効きが無ければ見つからない
        p0, m0, _ = Q.load(*make(edge=0.0, seed=seed))
        assert Q.q1(p0, m0, 93)["verdict"] != "使えそう"
    pf, mf, _ = Q.load(*make(edge=0.006, edge_late=-0.002))                       # 前半だけ効いて後半に消えた
    assert Q.q1(pf, mf, 93)["verdict"] == "使えない"
    with pytest.raises(Q.QualityError):
        Q.q1(port, mkt, 92)                                                       # 登録の時点の数より前の番号は使えない


def test_wrong_shape_stops_without_computing():
    bad = {
        "列の名前が違う": make(cols=["SW", "SN", "SR", "BW", "BN", "BR"]),
        "列の数が違う（25 組のファイル）": make(cols=COLS + ["EXTRA"]),
        "始まりが遅い": make(start="1995-07-31", months=372),
        "短い": make(months=200),
        "月が抜けている": make(gap=100),
        "単位が違う": make(scale=5.0),
        "市場と合わない（別の地域）": make(own_market=True),
    }
    for why, (p, m) in bad.items():
        with pytest.raises(Q.QualityError):
            Q.load(p, m)
            pytest.fail(why)
    good_p, good_m = make()
    with pytest.raises(Q.QualityError):
        Q.load(b"<html>404 Not Found</html>", good_m)                             # zip でない
    with pytest.raises(Q.QualityError):
        Q.load(good_p, good_p)                                                    # 市場のファイルに Mkt-RF が無い
    short_p, short_m = make(months=372)                                           # 市場のファイルだけ古い
    with pytest.raises(Q.QualityError):
        Q.load(good_p, short_m)


def _getter(files: dict, calls: list):
    def get(url: str) -> bytes:
        calls.append(url)
        name = url.rsplit("/", 1)[1].replace("_CSV.zip", "")
        if name not in files:
            raise OSError("HTTP Error 404: Not Found")
        return files[name]
    return get


def test_runs_only_once_whatever_the_output_folder(tmp_path, capsys, monkeypatch):
    p, m = make(edge=0.004)
    files, calls = {Q.FILE_PORT: p, Q.FILE_MKT: m}, []
    get = _getter(files, calls)
    marker = tmp_path / Q.MARKER

    assert study.main(["--check"], getter=get, root=tmp_path) is None            # 形だけ。印は付かない・差は出ない
    out = capsys.readouterr().out
    assert "形の点検は通った" in out and "使えそう" not in out and "年 " not in out and not marker.exists()
    assert calls[0].endswith("Japan_6_Portfolios_ME_OP_CSV.zip") and calls[1].endswith("Japan_3_Factors_CSV.zip")

    for argv in ([], ["--trials", "92"], ["--trials", "920"]):                                         # 数を渡さない・小さい → 取りに行く前に止まる
        n = len(calls)
        with pytest.raises(SystemExit):
            study.main(argv, getter=get, root=tmp_path)
        assert len(calls) == n and not marker.exists()

    path = study.main(["--trials", "93", "--out", str(tmp_path / "a")], getter=get, root=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "「使えそう」" in text and "93 個" in text and path.parent.parent == tmp_path / "a"
    rec = json.loads(marker.read_text(encoding="utf-8"))
    assert rec["verdict"] == "使えそう" and rec["trials"] == 93 and rec["report"] == text.splitlines()
    capsys.readouterr()

    n = len(calls)
    for argv in (["--trials", "93"], ["--trials", "94", "--out", str(tmp_path / "b")]):   # 保存先を変えても動かない
        with pytest.raises(SystemExit) as e:
            study.main(argv, getter=get, root=tmp_path)
        assert "動かし直さない" in str(e.value) and "使えそう" in str(e.value)
    assert len(calls) == n and not (tmp_path / "b").exists()                      # 取りにも行かない

    marker.write_text("こわれた", encoding="utf-8")                                # 壊れた印でも動かさない
    with pytest.raises(SystemExit):
        study.main(["--trials", "93"], getter=get, root=tmp_path)
    marker.unlink()
    monkeypatch.setattr(Q, "RESULT", {"date": "2026-11-12", "verdict": "使えない", "detail": "x"})   # プログラムに書いた結果でも止まる
    with pytest.raises(SystemExit) as e:
        study.main(["--trials", "93"], getter=get, root=tmp_path)
    assert "2026-11-12" in str(e.value) and len(calls) == n


def test_missing_file_or_wrong_shape_leaves_no_mark(tmp_path, capsys):
    p, m = make(edge=0.004)
    marker = tmp_path / Q.MARKER
    for files in ({Q.FILE_MKT: m},                                                # 組のファイルが無い（404）
                  {Q.FILE_PORT: make(cols=["SW", "SN", "SR", "BW", "BN", "BR"])[0], Q.FILE_MKT: m},
                  {Q.FILE_PORT: b"<html>", Q.FILE_MKT: m}):
        with pytest.raises(SystemExit) as e:
            study.main(["--trials", "93"], getter=_getter(files, []), root=tmp_path)
        assert "何も計算していない" in str(e.value)
        assert not marker.exists() and not (tmp_path / "output").exists()
    assert "使え" not in capsys.readouterr().out
    assert study.main(["--trials", "93"], getter=_getter({Q.FILE_PORT: p, Q.FILE_MKT: m}, []), root=tmp_path).exists()   # 止まった後でも 1 回は計算できる
