"""M2 を手で注文する形（docs/agents.md 13 章）の試験。作ったデータだけを使う。"""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path

import numpy as np
import pandas as pd

from cfdbot import stocks_short as H
from cfdbot.agents import manual as M

ROOT = Path(__file__).resolve().parents[1]


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "agents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_week_starts_follow_holidays():
    idx = pd.bdate_range("2026-10-01", "2026-10-31").drop(pd.Timestamp("2026-10-12"))   # 10-12（月）は休み
    starts = idx[M.week_starts(idx)]
    assert list(starts.strftime("%m-%d")) == ["10-01", "10-05", "10-13", "10-19", "10-26"]
    assert M.is_week_start(idx, pd.Timestamp("2026-10-13")) and not M.is_week_start(idx, pd.Timestamp("2026-10-14"))
    assert not M.is_week_start(idx, pd.Timestamp("2026-10-12"))                          # 営業日でない日は書かない


def test_delay_and_weekly_weights_line_up_with_the_buy_day():
    sel = np.zeros((12, 3), dtype=bool)
    sel[0, 0] = sel[5, 1] = True
    tradable = np.ones_like(sel)
    d = M.delayed(sel, tradable)
    assert d[1, 0] and d[6, 1] and d.sum() == 2
    tradable[6, 1] = False                                             # 買う日に値段が無ければ買えない
    assert not M.delayed(sel, tradable)[6, 1]
    w = M.weekly_weights(sel, np.array([0, 5]))
    assert (w[0] == 0).all()                                           # 決めた日はまだ持っていない
    assert (w[1:6, 0] == 1).all() and (w[1:6, 1] == 0).all()           # 1 日遅れで買い、次の買いの日（6）まで持つ
    assert (w[6:, 1] == 1).all() and (w[6:, 0] == 0).all()


def _market(seed=0, start="2009-06-01", end="2019-12-31", n=60, signal=True):
    idx = pd.bdate_range(start, end)
    rng = np.random.default_rng(seed)
    cc = rng.normal(0.0002, 0.015, (len(idx), n))
    close = pd.DataFrame(100 * np.exp(np.cumsum(cc, axis=0)), index=idx, columns=[f"{1000 + i}.T" for i in range(n)])
    open_ = close.shift(1).fillna(100.0)
    r = close.pct_change(fill_method=None)
    if signal:   # 1 日遅れで買って 5 日持つと受ける値動き（t+2〜t+6）を、少しだけ当てる点数
        fut = sum(r.shift(-k) for k in range(2, 7))
        score = 0.3 * fut + rng.normal(0, 0.03, close.shape)
    else:
        score = pd.DataFrame(rng.normal(0, 1, close.shape), index=idx, columns=close.columns)
    return open_, close, score[score.index >= "2010-01-01"]


def test_evaluate_finds_a_real_signal_and_rejects_noise():
    open_, close, score = _market()
    r = M.evaluate(score, open_, close, placebo=20)
    assert r["M2a"]["verdict"] == "使えそう" and r["M2a"]["placebo"] == 1.0 and r["M2a"]["threshold"] > 2.47
    assert r["M2b"]["mean"] > 0 and r["M2b"]["turnover"] < r["M2a"]["turnover"] * 1.5
    assert r["period"][0] >= pd.Timestamp("2010-01-01")
    refs = r["ref"]
    assert refs["5 日保有・遅れ 1 日（M2a）"]["gross_ann"] > refs["5 日保有・遅れ 5 日"]["gross_ann"]   # 当たる日から離れるほど弱い
    noise = M.evaluate(_market(seed=3, signal=False)[2], *_market(seed=3, signal=False)[:2], placebo=20)
    assert noise["M2a"]["verdict"] == "使えない" and noise["M2b"]["verdict"] == "使えない"


def test_manual_rows_only_on_the_first_trading_day_of_the_week():
    idx = pd.bdate_range("2026-10-01", "2026-10-31")
    row = pd.Series(np.linspace(-1, 1, 60), index=[f"{1000 + i}.T" for i in range(60)])
    names = {t: f"会社{t[:4]}" for t in row.index}
    mon = M.manual_rows(pd.Timestamp("2026-10-19"), row, idx, names)
    assert len(mon) == 10 and (mon["book"] == M.BOOK).all() and mon["ticker"].iloc[0] == "1059.T"
    assert list(mon.columns) == ["date", "ticker", "book", "selector", "ir", "combined", "name", "reason"]
    assert "1/10 位" in mon["reason"].iloc[0]
    assert M.manual_rows(pd.Timestamp("2026-10-20"), row, idx, names).empty            # 火曜は書かない
    assert M.manual_rows(pd.Timestamp("2026-10-19"), row.iloc[:30], idx, names).empty  # 点数のある株が 50 未満


def test_this_week_lists_sells_and_buys():
    def rows(day, tickers):
        return pd.DataFrame({"decided_at": f"{day} 13:06:00", "date": day, "ticker": tickers, "book": M.BOOK,
                             "selector": np.linspace(0.1, 0.01, len(tickers)), "name": [f"会社{t}" for t in tickers]})
    ex = pd.concat([rows("2026-10-19", ["A.T", "B.T", "C.T"]), rows("2026-10-26", ["B.T", "C.T", "D.T"]),
                    pd.DataFrame({"decided_at": "2026-10-26 13:06:00", "date": "2026-10-26", "ticker": ["Z.T"], "book": "random",
                                  "selector": [0.0], "name": ["でたらめ"]})])
    w = M.this_week(ex)
    assert w["date"] == pd.Timestamp("2026-10-26") and w["buy"] == ["D.T"] and w["sell"] == ["A.T"] and w["keep"] == ["B.T", "C.T"]
    assert M.this_week(pd.DataFrame())["date"] is None


def _decide_setup(tmp_path, last="2026-10-19"):
    idx = pd.bdate_range("2026-09-01", last)
    cols = [f"{1000 + i}.T" for i in range(100)]
    root, data = tmp_path / "agents", tmp_path / "stocks"
    root.mkdir(parents=True)
    data.mkdir(parents=True)
    pd.DataFrame(np.tile(np.linspace(-0.05, 0.05, 100), (len(idx), 1)), index=idx, columns=cols).to_csv(root / "selector_scores.csv")
    pd.DataFrame({"date": [idx[-1]], "ticker": [cols[0]], "score": [0.1], "titles": ["上方修正"]}).to_csv(root / "ir_scores.csv", index=False)
    pd.DataFrame({"ticker": cols, "name": [f"会社{i}" for i in range(100)], "sector": "x"}).to_csv(data / "universe_jp.csv", index=False)
    return idx, cols, root, data


def test_decide_writes_the_weekly_book_on_mondays_and_manual_shows_it(tmp_path, capsys):
    idx, cols, root, data = _decide_setup(tmp_path)
    mod = _script("agents_manual")
    mod.cmd_decide(Namespace(root=str(root), data=str(data)))
    lg = pd.read_csv(root / "ledger.csv")
    assert set(lg["book"]) == {"combined", "selector"}                                    # ledger.csv は今までどおり
    ex = pd.read_csv(root / "ledger_extra.csv", dtype={"ticker": str})
    m = ex[ex["book"] == M.BOOK]
    assert len(m) == 10 and m["ticker"].iloc[0] == cols[-1] and (m["date"] == "2026-10-19").all()
    assert (ex["decided_at"] == lg["decided_at"].iloc[0]).all()
    capsys.readouterr()
    mod.cmd_manual(Namespace(root=str(root), test=False, now="2026-10-19 13:30"))
    out = capsys.readouterr().out
    assert "今週の手で買う候補" in out and "会社99" in out and "| 1 | 会社99 | 1099 |" in out and "次の営業日" in out
    mod.cmd_manual(Namespace(root=str(root), test=False, now="2026-10-27 13:30"))
    assert "今週の分がまだ無い" in capsys.readouterr().out
    # 火曜の判断では手で買う候補の帳を書かない
    idx2, cols2, root2, data2 = _decide_setup(tmp_path / "tue", last="2026-10-20")
    mod.cmd_decide(Namespace(root=str(root2), data=str(data2)))
    assert M.BOOK not in set(pd.read_csv(root2 / "ledger_extra.csv")["book"])


def test_manual_test_command_writes_a_report(tmp_path, capsys):
    open_, close, score = _market(end="2013-12-31", n=60)
    root, data = tmp_path / "agents", tmp_path / "stocks"
    root.mkdir()
    data.mkdir()
    score.to_csv(root / "selector_scores.csv")
    close.to_csv(data / "jp_close.csv")
    open_.to_csv(data / "jp_open.csv")
    mod = _script("agents_manual_test")
    mod.cmd_manual(Namespace(root=str(root), data=str(data), out=str(tmp_path / "out"), test=True, placebo=10, now=None))
    text = next((tmp_path / "out").glob("*/manual.md")).read_text(encoding="utf-8")
    assert "## 1. 判定" in text and "| M2a |" in text and "| M2b |" in text and "遅れ 5 日" in text
    assert H.COST == M.COST
