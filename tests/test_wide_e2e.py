"""広げた候補の検証を、作ったデータで最初から最後まで通す（点検 → 判定 → 2 回目は止まる）。本物のデータは使わない。"""

import importlib.util
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdbot import edinet as ED
from cfdbot.wide import core as C
from cfdbot.wide import w1, w6, w7

ROOT = Path(__file__).resolve().parents[1]


def build(root: Path) -> None:
    data = root / "data"
    rng = np.random.default_rng(0)
    END = "2026-11-10"
    # W1
    idx = pd.bdate_range("2004-11-18", END)
    df = pd.DataFrame(100*np.exp(np.cumsum(rng.normal(3e-4, .01, (len(idx), 5)), 0)), index=idx, columns=list(w1.ASSETS))
    df[w1.CASH] = 2.0
    p = data / w1.FILE; p.parent.mkdir(parents=True); df.rename_axis("date").to_csv(p)
    # W2
    (root/"config").mkdir(); shutil.copy(ROOT / "config" / "fomc_days.csv", root/"config"/"fomc_days.csv")
    idx = pd.bdate_range("1999-01-04", END)
    g = pd.DataFrame({"^GSPC": 1000*np.exp(np.cumsum(rng.normal(2e-4, .01, len(idx))))}, index=idx)
    (data/"stocks").mkdir(parents=True); g.rename_axis("Date").to_csv(data/"stocks"/"misc_close.csv")
    # W3・W4・W5・W6 の値段（40 銘柄）
    idx = pd.bdate_range("2006-01-04", END); tick = [f"{1001+i}.T" for i in range(40)]
    close = pd.DataFrame(100*np.exp(np.cumsum(rng.normal(3e-4, .02, (len(idx), 40)), 0)), index=idx, columns=tick)
    close.rename_axis("Date").to_csv(data/"stocks"/"jp_close.csv")
    div = [(d, t, 10.0) for d in idx[::63] for t in rng.choice(tick, 3, replace=False)]
    pd.DataFrame(div, columns=["date","ticker","amount"]).assign(date=lambda x: x.date.dt.strftime("%Y-%m-%d")).to_csv(data/"stocks"/"jp_div.csv", index=False)
    # W5
    rows = []
    for k, d in enumerate(pd.bdate_range("2009-10-01", END)[::40]):
        rows.append((f"x{k}", f"{d:%Y-%m-%d} 15:00", f"{tick[k % 40][:4]}0", "会社", "新株式発行及び株式売出しに係る発行価格及び売出価格等の決定に関するお知らせ", "", ""))
    p = data/"agents"/"ir"/"disclosures.csv"; p.parent.mkdir(parents=True)
    pd.DataFrame(rows, columns=["id","time","ticker","company","title","url","xbrl"]).to_csv(p, index=False)
    # W6
    days = ED.fetch_days(pd.Timestamp(END))
    (data/"wide").mkdir(exist_ok=True)
    pd.DataFrame({"date": [f"{d:%Y-%m-%d}" for d in days], "n": 1, "n350": 1, "checked_at": "x"}).to_csv(data/w6.FLISTED, index=False)
    codes = pd.DataFrame({"edinetCode": [f"E{1001+i}" for i in range(40)], "secCode": [f"{1001+i}0" for i in range(40)],
                          "filerName": "会社", "seen_on": "2020-01-06"})
    codes.to_csv(data/w6.FCODES, index=False)
    filers = ["エフィッシモ・キャピタル・マネージメント・ピーティーイー・エルティーディー", "Oasis Management Company Ltd.", "株式会社ストラテジックキャピタル"]
    r350 = []
    for k, d in enumerate(days[30::30]):
        r350.append((f"S{k}", f"{d:%Y-%m-%d} 09:30", filers[k % 3], f"F{k%3}", f"E{1001 + (k*7) % 40}", "大量保有報告書", "0", f"{d:%Y-%m-%d}"))
    pd.DataFrame(r350, columns=w6.COLS_350).to_csv(data/w6.F350, index=False)
    # W7
    months = pd.date_range("1998-01-31", "2026-10-31", freq="ME"); dd = pd.bdate_range("1998-01-01", "2026-10-30")
    for sid, inv in w7.FX.values():
        v = np.exp(np.cumsum(rng.normal(0, .005, len(dd))))
        p = data/w7.DIR/f"{sid}.csv"; p.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"observation_date": dd.strftime("%Y-%m-%d"), sid: np.round(v, 5)}).to_csv(p, index=False)
    for sid in list(w7.RATE.values()) + [w7.US]:
        pd.DataFrame({"observation_date": (months - pd.offsets.MonthBegin(1)).strftime("%Y-%m-%d"), sid: np.round(rng.uniform(0, 5, len(months)), 3)}).to_csv(data/w7.DIR/f"{sid}.csv", index=False)


def load_script():
    spec = importlib.util.spec_from_file_location("wide_study_e2e", ROOT / "scripts" / "wide_study.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_end_to_end(tmp_path, monkeypatch, capsys):
    build(tmp_path)
    monkeypatch.setattr(C, "N_FLIPS", 500)
    m = load_script()
    m.main(["--check"], root=tmp_path)
    out = capsys.readouterr().out
    assert "W2 FOMC の声明の日: 通らない" in out and out.count(": 通った") == 6
    assert "%" not in out.split("W7")[0].replace("100.0%", "")       # 点検にリターンの数字は出ない（W6 の 100.0% は日数の割合）
    with pytest.raises(SystemExit):
        m.main([], root=tmp_path)                                    # W2 が通らないまま判定はしない
    assert not (tmp_path / C.MARKER).exists()
    m.main(["--fomc-checked", "作ったデータの試し"], root=tmp_path)
    path = m.main(["--out", str(tmp_path / "out")], root=tmp_path)
    text = path.read_text(encoding="utf-8")
    for k in (*C.RULES, "WX"):
        assert f"| {k} |" in text
    assert "120 通りの組" in text
    rec = json.loads((tmp_path / C.MARKER).read_text(encoding="utf-8"))
    assert rec["trials"] == 101 and set(rec["verdicts"]) == {*C.RULES, "WX"}
    with pytest.raises(SystemExit):
        m.main([], root=tmp_path)
