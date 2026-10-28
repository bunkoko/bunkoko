"""台帳の点検・でたらめの帳・E1 の帳・止める決まり（docs/agents.md 12 章）の試験。作ったデータだけを使う。"""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path

import numpy as np
import pandas as pd

from cfdbot.agents import collector as C
from cfdbot.agents import decider as D
from cfdbot.agents import earnings as N
from cfdbot.agents import evaluator as E

ROOT = Path(__file__).resolve().parents[1]


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "agents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_random_pick_is_reproducible_and_only_from_eligible():
    names = [f"S{i}" for i in range(100)]
    a = D.random_pick(names, 10, pd.Timestamp("2026-10-15"))
    assert a == D.random_pick(list(reversed(names)), 10, pd.Timestamp("2026-10-15"))   # 並びに関係なく、同じ日なら同じ
    assert len(a) == 10 and set(a) <= set(names) and len(set(a)) == 10
    assert a != D.random_pick(names, 10, pd.Timestamp("2026-10-16"))                    # 日が替われば替わる
    assert D.random_pick(names[:3], 10, pd.Timestamp("2026-10-15")) == sorted(names[:3])  # 足りなければある分だけ
    assert D.random_pick([], 5, pd.Timestamp("2026-10-15")) == []
    assert D.E1_UP == N.UP_REVISION                                                     # E1 の帳は 10.2 と同じ 5%


def test_e1_pick_and_extra_rows():
    day = pd.Timestamp("2026-10-15")
    rev = pd.DataFrame({"date": [day, day, day, day - pd.Timedelta(days=1)], "ticker": ["A.T", "B.T", "Z.T", "C.T"],
                        "fc_rev": [0.05, 0.049, 0.3, 0.5], "kind": [5, 2, 5, 5]})
    got = D.e1_pick(rev, day, ["A.T", "B.T", "C.T"])
    assert got["ticker"].tolist() == ["A.T"]                                            # 5% ちょうどは入る。4.9%・点数の無い株・別の日は入らない
    assert D.e1_pick(None, day, ["A.T"]).empty
    sel_row = pd.Series({"A.T": 0.01, "B.T": -0.02, "C.T": np.nan, "D.T": 0.0})
    rows = D.extra_rows(day, sel_row, 2, rev, {"A.T": "会社A"})
    assert list(rows.columns) == ["date", "ticker", "book", "selector", "ir", "combined", "name", "reason"]
    r = rows[rows["book"] == "random"]
    assert len(r) == 2 and set(r["ticker"]) <= {"A.T", "B.T", "D.T"}                     # 点数の無い C は選ばない
    e = rows[rows["book"] == "e1"].iloc[0]
    assert e["ticker"] == "A.T" and e["name"] == "会社A" and "+5%" in e["reason"]
    assert "【本業: 不明" in e["reason"]                                                  # 営業利益の予想の変化が無い表なら「不明」
    rows2 = D.extra_rows(day, sel_row, 2, rev.assign(fc_op_rev=[0.08, 0.0, 0.0, 0.0]), {})
    assert rows2[rows2["book"] == "e1"].iloc[0]["reason"].endswith("【本業: 営業利益の予想も +8%】")   # 本業の印（docs/text_signals.md 6 章）
    assert rows2["ticker"].tolist() == rows["ticker"].tolist()                          # 印を付けても、帳の中身は変わらない


def test_recent_revisions_reads_only_recent_and_skips_corrections(tmp_path):
    from tests.test_agents import _facts

    (tmp_path / "xbrl" / "2026-10").mkdir(parents=True)
    rv = [("NetIncome", "CurrentYearDuration_ConsolidatedMember_PreviousMember_ForecastMember", 10.0),
          ("NetIncome", "CurrentYearDuration_ConsolidatedMember_CurrentMember_ForecastMember", 12.0)]
    for stem in ("r1", "r2", "r3"):
        _facts("rvfc", rv).to_csv(tmp_path / "xbrl" / "2026-10" / f"{stem}.csv", index=False)
    disc = pd.DataFrame([("p1", "2026-10-13 15:30:00", "A.T", "会社", "業績予想の修正", "", "r1.zip"),      # 引け後 → 翌営業日の判断
                         ("p2", "2026-10-14 12:00:00", "B.T", "会社", "（訂正）業績予想の修正", "", "r2.zip"),  # 訂正は使わない
                         ("p3", "2024-01-05 12:00:00", "C.T", "会社", "業績予想の修正", "", "r3.zip")],      # 450 日より前は読まない
                        columns=C.COLUMNS)
    idx = pd.bdate_range("2026-09-01", "2026-10-15")
    out = N.recent_revisions(disc, tmp_path, idx, ["A.T", "B.T", "C.T"], log=lambda s: None)
    assert out["ticker"].tolist() == ["A.T"] and out.iloc[0]["date"] == pd.Timestamp("2026-10-14")
    assert abs(out.iloc[0]["fc_rev"] - 0.2) < 1e-12
    assert N.recent_revisions(disc.iloc[:0], tmp_path, idx, ["A.T"]).empty


def _ledger(days, books=("selector", "combined"), names=("A.T",), hour="13:05:00") -> pd.DataFrame:
    rows = [(f"{d:%Y-%m-%d} {hour}", f"{d:%Y-%m-%d}", t, b) for d in days for b in books for t in names]
    return pd.DataFrame(rows, columns=["decided_at", "date", "ticker", "book"])


def test_ledger_health_counts_missing_late_and_unpriced_days():
    cal = pd.bdate_range("2026-10-01", "2026-10-09")                    # 値段のある営業日（10-01〜10-09 の 7 日）
    lg = pd.concat([_ledger(cal[[1, 2, 4]]), _ledger(cal[[5]], hour="18:00:00")])   # 10-02・05・07 は数える、10-08 は引け後に書いた
    h = E.ledger_health(lg, cal, pd.Timestamp("2026-10-14 16:00"))
    assert h["first"] == cal[1] and h["last"] == cal[4] and h["counted_days"] == 3
    assert [d.strftime("%m-%d") for d in h["missing"]] == ["10-06", "10-08", "10-09"]   # 始まりより前（10-01）は数えない
    assert [d.strftime("%m-%d") for d in h["late"]] == ["10-08"]
    assert [d.strftime("%m-%d") for d in h["missing_weekdays"]] == ["10-12", "10-13", "10-14"]   # 値段の無い平日（今日は 15:25 の後）
    early = E.ledger_health(lg, cal, pd.Timestamp("2026-10-14 13:00"))
    assert [d.strftime("%m-%d") for d in early["missing_weekdays"]] == ["10-12", "10-13"]        # 今日はまだ書く時刻の前
    assert h["books"]["selector"]["days"] == 3 and h["books"]["selector"]["names_per_day"] == 1.0
    assert E.ledger_health(pd.DataFrame(), cal, pd.Timestamp("2026-10-14"))["rows"] == 0


def _market(n_days=800, n=60, seed=0):
    idx = pd.bdate_range("2026-10-01", periods=n_days)
    rng = np.random.default_rng(seed)
    cc = pd.DataFrame(rng.normal(0, 0.01, (n_days, n)), index=idx, columns=[f"S{i}.T" for i in range(n)])
    return idx, cc


def test_forward_book_judges_only_at_the_look_days_and_keeps_a_stop():
    idx, cc = _market()
    good = ["S0.T", "S1.T"]
    cc[good] += 0.004                                                  # 選んだ株は毎日上がる（本物の効き）
    bad = ["S2.T", "S3.T"]
    cc[bad] -= 0.004
    lg = pd.concat([_ledger(idx[:300], ("selector",), good), _ledger(idx[:300], ("random",), bad), _ledger(idx[:300], ("e1",), bad)])
    sel = E.forward_book(lg, "selector", cc)
    assert sel["days"] == 300 and sel["gross_ann"] > 0.5 and set(sel["looks"]) == {250}
    assert E.forward_verdict(sel, "selector").startswith("続ける（250 日")
    e1 = E.forward_book(lg, "e1", cc)
    assert e1["start"] == idx[0] and E.forward_verdict(e1, "e1").startswith("閉じる（250 日")
    assert E.forward_verdict(E.forward_book(lg, "random", cc), "random").startswith("点検")   # でたらめの帳が大きく外れたら点検
    early = E.forward_book(lg[pd.to_datetime(lg["date"]) <= idx[99]], "selector", cc)
    assert early["looks"] == {} and "あと 150 日" in E.forward_verdict(early, "selector")
    # 250 日の時点で閉じたら、その後に数字が戻っても閉じたまま
    later = cc.copy()
    later.loc[idx[260]:, bad] += 0.02
    assert E.forward_verdict(E.forward_book(lg, "e1", later), "e1").startswith("閉じる（250 日")
    # e1 の時計は random の始まりから（e1 は買う株の無い日が多い）
    sparse = pd.concat([_ledger(idx[:300], ("random",), ["S9.T"]), _ledger(idx[200:201], ("e1",), good)])
    assert E.book_start(sparse, "e1") == idx[0] and E.forward_book(sparse, "e1", cc)["days"] == 300
    assert E.forward_verdict(None, "selector") == "まだ記録が無い"


def _decide_setup(tmp_path):
    idx = pd.bdate_range("2026-09-01", "2026-10-15")
    cols = [f"{1000 + i}.T" for i in range(100)]
    root, data = tmp_path / "agents", tmp_path / "stocks"
    root.mkdir()
    data.mkdir()
    sel = pd.DataFrame(np.tile(np.linspace(-0.05, 0.05, 100), (len(idx), 1)), index=idx, columns=cols)
    sel.to_csv(root / "selector_scores.csv")
    pd.DataFrame({"date": [idx[-1]], "ticker": [cols[0]], "score": [0.1], "titles": ["上方修正"]}).to_csv(root / "ir_scores.csv", index=False)
    pd.DataFrame({"ticker": cols, "name": [f"会社{i}" for i in range(100)], "sector": "x"}).to_csv(data / "universe_jp.csv", index=False)
    pd.DataFrame({"date": [idx[-1], idx[-1]], "ticker": [cols[5], cols[6]], "fc_rev": [0.12, 0.01], "kind": [5, 2]}).to_csv(
        root / "revisions_recent.csv", index=False)
    return idx, cols, root, data


def test_decide_writes_new_books_to_a_separate_file_and_check_reads_both(tmp_path, capsys):
    idx, cols, root, data = _decide_setup(tmp_path)
    mod = _script("agents_forward")
    mod.cmd_decide(Namespace(root=str(root), data=str(data)))
    lg = pd.read_csv(root / "ledger.csv")
    assert lg["book"].value_counts().to_dict() == {"combined": 10, "selector": 10}       # ledger.csv は今までどおり（2 つの帳だけ）
    assert list(lg.columns) == ["decided_at", "date", "ticker", "book", "selector", "ir", "combined", "name", "reason"]
    ex = pd.read_csv(root / "ledger_extra.csv")
    assert ex["book"].value_counts().to_dict() == {"random": 10, "e1": 1} and ex[ex["book"] == "e1"].iloc[0]["ticker"] == cols[5]
    assert (ex["decided_at"] == lg["decided_at"].iloc[0]).all() and (ex["date"] == f"{idx[-1]:%Y-%m-%d}").all()   # 同じ時刻・同じ日
    mod.cmd_decide(Namespace(root=str(root), data=str(data)))                       # 同じ日は二度書かない（どちらのファイルも）
    assert len(pd.read_csv(root / "ledger.csv")) == len(lg) and len(pd.read_csv(root / "ledger_extra.csv")) == len(ex)

    rng = np.random.default_rng(1)
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.01, (len(idx), 100)), axis=0)), index=idx, columns=cols)
    close.to_csv(data / "jp_close.csv")
    close.shift(1).fillna(100.0).to_csv(data / "jp_open.csv")
    for name, df in (("ledger.csv", lg), ("ledger_extra.csv", ex)):                   # 時刻を今日の 13:06 に置き直す（試験の時計に合わせる）
        df.assign(decided_at=f"{idx[-1]:%Y-%m-%d} 13:06:00").to_csv(root / name, index=False)
    assert set(mod.read_ledgers(mod.paths(str(root)))["book"]) == {"selector", "combined", "random", "e1"}
    capsys.readouterr()
    mod.cmd_check(Namespace(root=str(root), data=str(data), out=str(tmp_path / "out"), now=f"{idx[-1]:%Y-%m-%d} 16:00"))
    text = next((tmp_path / "out" / "check").glob("*.md")).read_text(encoding="utf-8")
    assert "## 2. 台帳" in text and "先の判断として数える日" in text and "抜けた営業日: 無し" in text
    assert "| selector |" in text and "| random |" in text and "| e1 |" in text and "まだ判定しない" in text
    assert "最後の自動実行は" not in text                                                  # run.log が無いときは何も言わない
    (root / "run.log").write_text("[2026-10-15 13:05:05] 自動実行: 始め\n[2026-10-15 13:06:10] 自動実行: 値段\n"
                                  "[2026-10-15 13:06:27] 自動実行: E1 の一覧と注文の一覧\n埋め込みの部品が無い。先に ./cfd news setup を実行する\n",
                                  encoding="utf-8")
    mod.cmd_check(Namespace(root=str(root), data=str(data), out=str(tmp_path / "out"), now=f"{idx[-1]:%Y-%m-%d} 16:01"))
    text = (tmp_path / "out" / "check" / f"{idx[-1]:%Y%m%d}-1601.md").read_text(encoding="utf-8")
    assert "**⚠ 最後の自動実行は「E1 の一覧と注文の一覧」の後で止まった（「終わり」が無い）" in text and "埋め込みの部品が無い" in text


def test_decide_still_writes_ledger_when_the_new_books_fail(tmp_path, capsys, monkeypatch):
    idx, cols, root, data = _decide_setup(tmp_path)
    mod = _script("agents_forward_fail")

    def boom(*a, **k):
        raise ValueError("作り物の失敗")

    monkeypatch.setattr(D, "extra_rows", boom)
    mod.cmd_decide(Namespace(root=str(root), data=str(data)))
    out = capsys.readouterr().out
    lg = pd.read_csv(root / "ledger.csv")
    assert lg["book"].value_counts().to_dict() == {"combined": 10, "selector": 10}       # M2・combined は必ず書かれる
    assert not (root / "ledger_extra.csv").exists()
    assert "でたらめ・E1 の帳は書けなかった（ValueError: 作り物の失敗）。ledger.csv は書いてある" in out


def test_cusum_threshold_reproduces_the_registered_value_and_paths():
    s, alarm = E.cusum_path(np.array([0.0, -1.0, -1.0, 2.0]), k=0.5, h=2.5)
    assert np.allclose(s, [0.5, 2.0, 3.5, 2.0]) and alarm == 3                        # 3 週目に 2.5 を超える
    assert E.cusum_path(np.array([1.0, 1.0]), k=0.5)[1] is None
    h = E.cusum_threshold(n_paths=1500, iters=14)
    assert abs(h - E.CUSUM_H) < 1.0                                                   # 12.3（docs/agents.md 12.3）を作ったデータで再現する
    k, d = E.cusum_k(), E.CUSUM_SR_GOOD / np.sqrt(E.BLOCKS_PER_YEAR)
    good = E.cusum_arl(E.CUSUM_H, d, k, n_paths=1500, max_len=4000, seed=5)
    null = E.cusum_arl(E.CUSUM_H, 0.0, k, n_paths=1500, max_len=4000, seed=6)
    assert 4.0 < good / E.BLOCKS_PER_YEAR < 6.5 and 2.0 < null / E.BLOCKS_PER_YEAR < 3.5   # 誤った警報は 5 年に 1 回ほど、効きが無ければ 2〜3 年で警報


def test_weekly_watch_alarms_on_a_losing_book_and_closes():
    idx, cc = _market(n_days=900, seed=3)
    w = E.weekly_blocks(pd.Series(np.arange(12.0), index=idx[:12]))
    assert w.tolist() == [10.0, 35.0] and list(w.index) == [idx[4], idx[9]]           # 5 日ずつ。欠けた最後の週は使わない
    bad = ["S2.T", "S3.T"]
    cc[bad] -= 0.003
    lg = _ledger(idx[:400], ("selector",), bad)
    r = E.forward_book(lg, "selector", cc)
    sigma = E.history_sigma(np.zeros(cc.shape, dtype=bool) | (np.arange(cc.shape[1]) < 6)[None, :], cc, str(idx[0].date()), str(idx[-1].date()))
    cs = E.forward_cusum(r, sigma)
    assert cs["alarm"] is not None and cs["alarm"] < 80                               # 負け続ける帳は 250 日を待たずに警報
    assert E.forward_verdict(r, "selector", cs).startswith("閉じる（毎週の見張り")
    assert E.forward_cusum(r, float("nan")) is None
