"""文章の信号（cfdbot/text_signals.py。1C・案 2）と、geo_vol を使っていない市場で（scripts/news_study.py の volcheck）の試験。作ったデータだけ。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

from cfdbot import edinet as ED
from cfdbot import text_signals as TS
from cfdbot.news_embed import HashEmbedder

ROOT = Path(__file__).resolve().parents[1]
N_FIRMS, N_CLUSTERS = 330, 33


def _script(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_clean_text_and_ngram_cosine():
    t = TS.clean_text("トヨタ自動車株式会社は 2025 年に 1,000 台を販売", "トヨタ自動車株式会社")
    assert "トヨタ" not in t and "2025" not in t and "0 年に 0,0 台" in t
    a, b = TS.ngram_counts("為替の変動と原材料の価格"), TS.ngram_counts("為替の変動と原材料の価格")
    assert abs(TS.ngram_cosine(a, b) - 1) < 1e-9
    assert TS.ngram_cosine(a, TS.ngram_counts("新しい工場の建設と人手の不足")) < 0.2
    assert np.isnan(TS.ngram_cosine(a, TS.ngram_counts("")))


def test_peer_and_sector_means_exclude_self_and_use_only_past_documents():
    tick = ["A", "B", "C", "D"]
    vecs = np.array([[1, 0], [0.9, 0.1], [0, 1], [0.1, 0.9]], dtype=np.float32)
    vals = pd.Series({"A": 0.10, "B": 0.20, "C": -0.10, "D": np.nan})
    pm = TS.peer_means(tick, vecs, vals, k=1)
    assert pm["A"] == 0.20 and pm["B"] == 0.10 and pm["D"] == -0.10                  # 自分は除く・値の無い株は仲間にしない
    sm = TS.sector_means(tick, pd.Series({"A": "x", "B": "x", "C": "y", "D": "y"}), vals)
    assert np.isclose(sm["A"], 0.20) and np.isnan(sm["C"]) and np.isclose(sm["D"], -0.10)
    meta = pd.DataFrame({"ticker": ["A", "A"], "docID": ["a1", "a2"], "submit": pd.to_datetime(["2020-06-25", "2021-06-25"]),
                         "periodEnd": pd.to_datetime(["2020-03-31", "2021-03-31"])})
    assert TS.active_docs(meta, pd.Timestamp("2021-06-25"))["docID"].tolist() == ["a1"]   # 提出の日の当日は、まだ使わない
    assert TS.active_docs(meta, pd.Timestamp("2021-06-30"))["docID"].tolist() == ["a2"]
    assert TS.active_docs(meta, pd.Timestamp("2023-06-30")).empty                        # 古すぎる有報は使わない
    assert TS.turnover_of(set(), {"A", "B"}) == 1.0 and TS.turnover_of({"A", "B"}, {"A", "B"}) == 0.0
    assert TS.turnover_of({"A", "B"}, {"A", "C"}) == 0.5


def _world(tmp_path, seed=0, lazy=False):
    """330 社・33 の「似た会社の集まり」（業種とは別）。集まりごとに続く値動きの癖がある。毎年 6 月 25 日に有報。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-05", "2024-12-30")
    tick = [f"{1000 + i}.T" for i in range(N_FIRMS)]
    cluster = np.arange(N_FIRMS) % N_CLUSTERS
    drift = rng.normal(0, 0.0012, N_CLUSTERS)[cluster]
    ret = drift + rng.normal(0, 0.012, (len(idx), N_FIRMS))
    changed = {}
    if lazy:
        ret = rng.normal(0, 0.012, (len(idx), N_FIRMS))
        for i in range(N_FIRMS):
            for y in range(2017, 2025):
                if rng.random() < 0.3:
                    changed[(i, y)] = True
                    d0 = idx.searchsorted(pd.Timestamp(f"{y}-06-26"))
                    ret[d0:d0 + 240, i] -= 0.0015                                            # 文面を大きく変えた会社は、その後に下がる
    close = pd.DataFrame(1000 * np.exp(np.cumsum(ret, axis=0)), index=idx, columns=tick)
    data = tmp_path / "stocks"
    data.mkdir(parents=True, exist_ok=True)
    close.to_csv(data / "jp_close.csv")
    close.shift(1).bfill().to_csv(data / "jp_open.csv")
    pd.DataFrame({"ticker": tick, "name": [f"会社{i}" for i in range(N_FIRMS)],
                  "sector": [f"業種{i % 11}" for i in range(N_FIRMS)]}).to_csv(data / "universe_jp.csv", index=False)
    store = ED.Store(tmp_path / "edinet")
    vocab = [chr(0x4E00 + 200 + k) for k in range(300)]
    rows = []
    for i, t in enumerate(tick):
        base = "".join(rng.choice(vocab, 600))
        for y in range(2016, 2025):
            mark = (chr(0x4E00 + cluster[i]) + chr(0x4E40 + cluster[i])) * 60
            risks = "".join(rng.choice(vocab, 600)) if changed.get((i, y)) else base
            doc_id = f"D{t[:4]}{y}"
            p = store.doc_path(t, doc_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"business": mark + "".join(rng.choice(vocab, 200)), "risks": risks}, ensure_ascii=False), encoding="utf-8")
            if changed.get((i, y)) is None:
                base = risks
            rows.append({"docID": doc_id, "secCode": t[:4] + "0", "ticker": t, "filerName": f"会社{i}", "submitDateTime": f"{y}-06-25 15:00",
                         "periodStart": f"{y - 1}-04-01", "periodEnd": f"{y}-03-31", "docTypeCode": "120", "source": "csv",
                         "chars_business": 320, "chars_risks": 600, "chars_mdna": 0, "fetched_at": "x"})
    store._append(store.index_path, rows, ED.INDEX_COLUMNS)
    return idx, tick, close, data, store


def test_momentum_finds_planted_peer_momentum_and_the_command_writes_a_report(tmp_path):
    idx, tick, close, data, store = _world(tmp_path)
    meta = TS.first_docs(store.index())
    enc = HashEmbedder(256)
    vec = {r.docID: TS.doc_vector(store.load_doc(r.ticker, r.docID)["business"], enc.encode) for r in meta.itertuples()}
    mclose = TS.monthly_close(close)
    sig = TS.momentum_signals(meta, vec, mclose, sectors=pd.Series([f"業種{i % 11}" for i in range(N_FIRMS)], index=tick))
    start = TS.first_month(sig["main"], mclose)
    assert start == pd.Timestamp("2016-06-30")                                      # 2016-06-25 の有報が使える最初の月末
    book = TS.monthly_book(sig["main"], mclose, start)
    null = TS.placebo(sig["main"], mclose, start, 30, np.random.default_rng(1))
    v = TS.judge(book, null)
    assert v["mean"] > 0 and v["placebo"] == 1.0 and v["t"] > 3                       # 似た会社の集まりの続く癖を拾う
    s2 = TS.monthly_book(sig["s2"], mclose, start)
    assert book["gross"].mean() > s2["gross"].mean()                                 # 業種（集まりと別）では拾いにくい
    mod = _script("edinet_m", "edinet.py")
    path = mod.main(["momentum", "--root", str(tmp_path / "edinet"), "--data", str(data), "--out", str(tmp_path / "out"),
                     "--placebo", "10"], encoder=enc)
    text = path.read_text(encoding="utf-8")
    assert "## 1. 判定" in text and "| 1C |" in text and "S2 型" in text and "hash" in text and "2025 年から" in text
    assert (tmp_path / "edinet" / "vecs_business_qwen3-0.6b.npz").exists()            # 埋め込みは取っておき、次は使い回す


def test_lazy_prices_finds_planted_changers_and_the_command_writes_a_report(tmp_path):
    idx, tick, close, data, store = _world(tmp_path, seed=3, lazy=True)
    meta = TS.first_docs(store.index())
    text = {r.docID: store.load_doc(r.ticker, r.docID)["risks"] for r in meta.itertuples()}
    pairs = TS.lazy_pairs(meta, lambda d: text[d])
    assert len(pairs) == N_FIRMS * 8 and pairs["sim"].between(0, 1.0001).all()
    mclose = TS.monthly_close(close)
    sig = TS.lazy_signals(pairs, mclose.index)
    start = TS.first_month(sig, mclose)
    top = TS.monthly_book(sig, mclose, start)
    low = TS.monthly_book(sig, mclose, start, highest=False)
    assert top["gross"].mean() > 0 > low["gross"].mean()                              # 変えていない会社 > 平均 > 変えた会社
    assert top["turnover"].iloc[1:].mean() < 0.5                                     # 月 1 回でも入れ替えは少ない
    mod = _script("edinet_l", "edinet.py")
    path = mod.main(["lazy", "--root", str(tmp_path / "edinet"), "--data", str(data), "--out", str(tmp_path / "out"), "--placebo", "10"])
    out = path.read_text(encoding="utf-8")
    assert "| 案 2 |" in out and "変えた会社" in out


def test_volcheck_pieces_har_has_no_lookahead_and_btc_closes_at_midnight():
    news = _script("news_vol", "news_study.py")
    rng = np.random.default_rng(5)
    r = rng.normal(0, 0.01, 600)
    f1 = news.har_forecast(r)
    r2 = r.copy()
    r2[400:] *= 5                                                                    # 先の値を変えても、それより前の予測は変わらない
    f2 = news.har_forecast(r2)
    assert np.allclose(f1[:400], f2[:400], equal_nan=True) and np.isfinite(f1[300:]).all()
    days = pd.bdate_range("2024-01-01", periods=5)
    m = news.vol_market("BTC", pd.Series(np.linspace(100, 104, 5), index=days), "UTC")
    assert pd.Timestamp(int(m.decide[0]), tz="UTC") == pd.Timestamp("2024-01-02", tz="UTC")   # UTC の日足はその日の終わりが引け
    t = news.vol_market("TOPIX", pd.Series([100, 100, 200, 201, 202], index=days, dtype=float), "Asia/Tokyo")
    assert np.isnan(t.next_ret[1])                                                   # 1 日で 2 倍（分割の誤り）は除く
    assert news.volcheck_verdict({"SP500": "有望", "TOPIX": "有望", "BTC": "偶然の範囲"}).startswith("使えそう")
    assert news.volcheck_verdict({"SP500": "有望", "TOPIX": "偶然の範囲", "BTC": "偶然の範囲"}).startswith("使えない")
    assert news.volcheck_verdict({"SP500": "有望", "TOPIX": "途中経過（x）", "BTC": "有望"}).startswith("まだ判定しない")


def test_preflight_rules_match_the_preregistration(tmp_path):
    # 社名は東証の銘柄名と提出者名の両方を消す
    t = TS.clean_text("日本たばこ産業株式会社とＪＴの子会社", ["ＪＴ", "日本たばこ産業株式会社"])
    assert "日本たばこ産業" not in t and "JT" not in t
    # まだ終わっていない今月は落とす
    idx = pd.bdate_range("2026-08-03", "2026-10-21")
    mc = TS.monthly_close(pd.DataFrame({"A": np.arange(len(idx), dtype=float)}, index=idx), today=pd.Timestamp("2026-10-21 13:05"))
    assert mc.index[-1] == pd.Timestamp("2026-09-30")
    assert TS.monthly_close(pd.DataFrame({"A": np.arange(len(idx), dtype=float)}, index=idx), today=pd.Timestamp("2026-11-02")).index[-1] == idx[-1]
    # 決算期を変えた年（9 か月・13 か月）は組にしない
    meta = pd.DataFrame({"ticker": ["A"] * 4, "docID": ["a1", "a2", "a3", "a4"],
                         "submit": pd.to_datetime(["2018-06-25", "2019-06-25", "2020-03-25", "2021-03-25"]),
                         "periodStart": pd.to_datetime(["2017-04-01", "2018-04-01", "2019-04-01", "2020-01-01"]),
                         "periodEnd": pd.to_datetime(["2018-03-31", "2019-03-31", "2019-12-31", "2020-12-31"])})
    pairs = TS.lazy_pairs(meta, lambda d: "為替の変動と原材料の価格" * 30)
    assert pairs["date"].tolist() == [pd.Timestamp("2019-06-25")]                     # 2019-12 期（9 か月）とその次は組にしない
    # 始まりの後は 300 社に届かない月も数える（選ぶ株が無い月だけ飛ばす）
    ends = pd.DatetimeIndex(["2020-01-31", "2020-02-28", "2020-03-31", "2020-04-30"])
    mclose = pd.DataFrame(100.0 + np.arange(4)[:, None] * np.arange(1, 401)[None, :] * 0.01, index=ends,
                          columns=[f"{i}.T" for i in range(400)])
    sig = {ends[0]: pd.Series(np.arange(400.0), index=mclose.columns),
           ends[1]: pd.Series(np.arange(50.0), index=mclose.columns[:50]),
           ends[2]: pd.Series(np.arange(3.0), index=mclose.columns[:3])}
    assert TS.first_month(sig, mclose) == ends[0]
    book = TS.monthly_book(sig, mclose, ends[0])
    assert len(book) == 2 and book["firms"].tolist() == [400, 50]                    # 3 社の月は上位 5 分の 1 が 0 社なので飛ばす
    v = TS.judge(pd.concat([book] * 3).set_axis(pd.date_range("2020-02-29", periods=6, freq="ME")), np.array([-1.0, -1.0, 1.0, np.nan]))
    assert v["placebo_k"] == 2 and v["placebo_n"] == 3


def test_commands_refuse_an_unfinished_fetch_and_reports_are_reproducible(tmp_path):
    import os

    import pytest

    idx, tick, close, data, store = _world(tmp_path)
    mod = _script("edinet_pf", "edinet.py")
    enc = HashEmbedder(256)
    base = ["--root", str(tmp_path / "edinet"), "--data", str(data), "--placebo", "10"]
    first = mod.main(["momentum", *base, "--out", str(tmp_path / "o1")], encoder=enc).read_text(encoding="utf-8")
    again = mod.main(["momentum", *base, "--out", str(tmp_path / "o2")], encoder=enc).read_text(encoding="utf-8")
    pick = lambda s: [x for x in s.splitlines() if x.startswith("| 1C |") or x.startswith("- 条件")]  # noqa: E731
    assert pick(first) == pick(again) and "④ 偶然より良い割合" in first and "残り 0 件" in first   # 取っておきを使う 2 回目も同じ数字
    # 取得が途中（見つけた有報のうち本文の無いものがある）なら止める
    store._append(store.cand_path, [{"docID": "NOTYET", "secCode": "10000", "ticker": "1000.T", "filerName": "x", "submitDateTime": "2024-06-25 15:00",
                                     "periodStart": "2023-04-01", "periodEnd": "2024-03-31", "docTypeCode": "120", "csvFlag": "1", "xbrlFlag": "1",
                                     "withdrawalStatus": "0", "listed_on": "2024-06-25"}], ED.CAND_COLUMNS)
    with pytest.raises(SystemExit, match="まだ終わっていない"):
        mod.main(["lazy", *base, "--out", str(tmp_path / "o3")])
    # 取得が動いている間も止める
    (tmp_path / "edinet" / ".lock").write_text(str(os.getpid()))
    with pytest.raises(SystemExit, match="まだ動いている"):
        mod.main(["momentum", *base, "--out", str(tmp_path / "o4")], encoder=enc)
