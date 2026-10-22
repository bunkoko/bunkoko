"""キャリーの記録だけの売買（docs/carry.md 8 章）の試験。作ったデータだけを使う。"""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path

import numpy as np
import pandas as pd

from cfdbot import carry_paper as P

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location("carry_study_paper", ROOT / "scripts" / "carry_study.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tickers_and_the_expiry_rule():
    assert P.ticker(2026, 12) == "CLZ26.NYM" and P.ticker(2027, 1) == "CLF27.NYM" and P.ticker(2026, 6) == "CLM26.NYM"
    # CLX26（2026 年 11 月限）の最終取引日は 2026-10-20（10-25 は日曜 → 10-23 の 3 営業日前）
    assert P.front_pair(pd.Timestamp("2026-10-20")) == ((2026, 11), (2026, 12))
    assert P.front_pair(pd.Timestamp("2026-10-21")) == ((2026, 12), (2027, 1))
    assert P.needed(pd.Timestamp("2026-10-01"), pd.Timestamp("2026-10-31")) == ["CLX26.NYM", "CLZ26.NYM", "CLF27.NYM"]
    assert P.month_complete(pd.Timestamp("2026-10-30"), pd.Timestamp("2026-10-31 06:30"))
    assert not P.month_complete(pd.Timestamp("2026-10-30"), pd.Timestamp("2026-10-30 20:00"))


def _contracts(start="2026-05-01", end="2028-12-31", backwardation=True, drift=0.0008, seed=0):
    """限月ごとの終値。どの限月も同じ値動きで、期先ほど 1 か月あたり 1% 安い（または高い）。"""
    days = pd.bdate_range(start, end)
    rng = np.random.default_rng(seed)
    base = 70 * np.exp(np.cumsum(rng.normal(drift, 0.01, len(days))))
    tickers = P.needed(days[0], days[-1] + pd.Timedelta(days=40))
    out = {}
    for i, t in enumerate(tickers):
        f = (0.99 if backwardation else 1.01) ** i
        out[t] = pd.DataFrame({"date": days.strftime("%Y-%m-%d"), "close": base * f})
    front = pd.DataFrame({"date": days.strftime("%Y-%m-%d"), "close": base})
    return out, front


def test_run_follows_the_sign_of_the_carry_and_judges_at_12_months():
    raw, front = _contracts()
    closes = {t: pd.Series(d["close"].to_numpy(), index=pd.to_datetime(d["date"])) for t, d in raw.items()}
    fr = pd.Series(front["close"].to_numpy(), index=pd.to_datetime(front["date"]))
    now = pd.Timestamp("2027-11-02 06:30")
    r = P.run(closes, fr, now)
    dec = r["decisions"]
    assert dec["month_end"].iloc[0] == pd.Timestamp("2026-10-30") and (dec["position"] == 1).all()   # 期近が高い → 買い
    assert dec["c1"].iloc[0] == "CLZ26.NYM" and dec["c2"].iloc[0] == "CLF27.NYM"
    assert len(r["monthly"]["cfd"]) == 12 and r["monthly"]["cfd"].index[0].month == 11
    assert r["monthly"]["futures"].sum() > r["monthly"]["cfd"].sum()                          # 先物は保有コストが無い
    # 限月の乗り換えの飛びは損益に混ぜない（どの限月も同じ値動きなので、損益は限月 1 の値動きとほぼ同じ）
    held = r["fr"]["ret"][r["fr"].index > pd.Timestamp("2026-11-01")]
    assert held.abs().max() < 0.06
    v = P.verdict(r["monthly"]["cfd"])
    assert v.startswith("続いている（12 か月") or v.startswith("まだ分からない（12 か月") or v.startswith("続いていない（12 か月")
    assert P.verdict(r["monthly"]["cfd"].iloc[:5]).startswith("途中経過（5 か月") and "あと 7 か月" in P.verdict(r["monthly"]["cfd"].iloc[:5])
    up = pd.Series([0.02, 0.01, 0.03] * 4, index=pd.date_range("2026-11-30", periods=12, freq="ME"))
    assert P.verdict(up).startswith("続いている") and P.verdict(-up).startswith("続いていない")
    # コンタンゴなら売り
    raw2, front2 = _contracts(backwardation=False)
    r2 = P.run({t: pd.Series(d["close"].to_numpy(), index=pd.to_datetime(d["date"])) for t, d in raw2.items()}, None, now)
    assert (r2["decisions"]["position"] == -1).all()


def test_paper_command_logs_once_and_keeps_going_when_a_contract_fails(tmp_path, capsys):
    raw, front = _contracts(end="2027-01-15")
    calls = []

    def getter(t, since):
        calls.append(t)
        if t == "CLN26.NYM":
            raise RuntimeError("期限の過ぎた限月")
        return front if t == P.FRONT else raw[t]

    mod = _script()
    args = Namespace(root=str(tmp_path / "carry"), out=str(tmp_path / "out"), no_fetch=False)
    waits = []
    path = mod.paper_stage(args, getter=getter, now=pd.Timestamp("2027-01-05 06:30"), sleep=waits.append)
    text = path.read_text(encoding="utf-8")
    log = pd.read_csv(tmp_path / "carry" / "paper" / "log.csv")
    assert list(log["month_end"]) == ["2026-10-30", "2026-11-30", "2026-12-31"] and (log["position"] == 1).all()
    assert (log["late"].fillna("") == ["遅れて記録", "遅れて記録", ""]).all()                # 月末から 7 日を過ぎた月に印
    assert "## 1. 途中経過と判定" in text and "途中経過（2 か月" in text and "CLZ26.NYM" in text
    assert "取れなかった" in capsys.readouterr().out and P.FRONT in calls and waits and set(waits) == {1.0}
    mod.paper_stage(Namespace(root=str(tmp_path / "carry"), out=str(tmp_path / "out"), no_fetch=True),
                    now=pd.Timestamp("2027-01-06 06:30"))
    assert len(pd.read_csv(tmp_path / "carry" / "paper" / "log.csv")) == 3                    # 同じ月末は二度書かない
