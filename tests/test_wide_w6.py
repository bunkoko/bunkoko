"""W6 物言う株主の新しい取得（docs/wide.md 4 章）。作ったデータだけで確かめる（EDINET には問い合わせない）。"""

import functools
import json
import os

import numpy as np
import pandas as pd
import pytest

from cfdbot import edinet as ED
from cfdbot.wide import base
from cfdbot.wide import w6 as W

EFF = "エフィッシモ キャピタル マネージメント ピーティーイー エルティーディー"


# --------------------------------------------------------------------------- 名前・書類の見分け
def test_names():
    yes = [EFF, "Effissimo Capital Management Pte Ltd", "ひびき・パース・アドバイザーズ・リミテッド", "ＥＬＬＩＯＴＴ　ＩＮＶＥＳＴＭＥＮＴ",
           "ｴﾌｨｯｼﾓ･ｷｬﾋﾟﾀﾙ", "株式会社レノ", "株式会社 レノ", "株式会社ストラテジックキャピタル", "Strategic Capital, Inc.",
           "3D Investment Partners Pte. Ltd.", "スリーディー・インベストメント・パートナーズ",
           "株式会社シティインデックスイレブンス", "いちごアセットマネジメント株式会社", "Farallon Capital Management, L.L.C."]
    no = ["株式会社レノックス", "レノ", "野村證券株式会社", "エフ・アイ・エル", "ブラックロック・ジャパン株式会社"]
    assert all(W.is_activist(n) for n in yes), [n for n in yes if not W.is_activist(n)]
    assert not any(W.is_activist(n) for n in no), [n for n in no if W.is_activist(n)]
    assert W.norm("Ａ・Ｂ　c ｄ") == "abcd"
    assert len(W.NAMES) == 36


def test_new_report():
    assert W.is_new_report("大量保有報告書") and W.is_new_report("大量保有報告書（特例対象株券等）")
    for t in ("変更報告書", "変更報告書（短期大量譲渡）", "訂正報告書（大量保有報告書・変更報告書）", "大量保有報告書の訂正", ""):
        assert not W.is_new_report(t), t


def test_issuer_tickers():
    codes = pd.DataFrame([("E1", "10010", "a", "2016-01-04"), ("E2", "20020", "b", "2016-01-04"),
                          ("E4", "40040", "d", "2016-01-04"), ("E4", "50050", "d", "2016-06-01"), ("E5", "417A0", "e", "2016-01-04"),
                          ("", "60060", "x", "2016-01-04")], columns=W.COLS_CODES)
    got = W.issuer_tickers(codes, ["1001.T", "4004.T", "5005.T", "417A.T", "6006.T"])
    assert got == {"E1": "1001.T", "E4": "5005.T", "E5": "417A.T"}   # 値段の表に無い 2002.T は入れない。E4 は最後に見たほう


# --------------------------------------------------------------------------- 取得（作った応答で）
def _doc(doc_id, typ="120", ec="", sec="", filer="会社", issuer="", desc="有価証券報告書", ws="0", when="2024-01-08 10:00"):
    return {"docID": doc_id, "docTypeCode": typ, "edinetCode": ec, "secCode": sec, "filerName": filer, "issuerEdinetCode": issuer,
            "docDescription": desc, "withdrawalStatus": ws, "submitDateTime": when}


def _ok(results, meta="200"):
    return 200, json.dumps({"metadata": {"status": meta, "message": "OK"}, "results": results}).encode()


class Fake:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, path, params):
        assert path == "documents.json" and params["type"] == 2
        self.calls.append(params["date"])
        return self.answers.get(params["date"], _ok([]))


def _client(fake):
    return ED.Client(fake, sleep=lambda s: None, log=lambda s: None)


DAYS = pd.bdate_range("2024-01-08", "2024-01-11")


def test_fetch_resumes_and_keeps_350_and_codes(tmp_path):
    day1 = [_doc("S1", typ="350", ec="E9001", sec="99990", filer=EFF, issuer="E0001", desc="大量保有報告書"),
            _doc("Y1", ec="E0001", sec="10010", filer="一の会社"), _doc("Y2", ec="E0003"),           # 証券の番号の無い会社は書かない
            _doc("Y3", ec="E0001", sec="10010", filer="一の会社")]                                   # 同じ組は 1 回だけ
    fake = Fake({"2024-01-08": _ok(day1), "2024-01-10": _ok([], meta="400")})
    logs = []
    with pytest.raises(RuntimeError, match="1 日"):
        W.fetch(tmp_path, log=logs.append, client=_client(fake), today="2024-01-11", days=DAYS)
    assert fake.calls == ["2024-01-08", "2024-01-09", "2024-01-10"]                                  # 今日（1/11）はまだ見ない
    listed = pd.read_csv(tmp_path / W.FLISTED, dtype=str)
    assert list(listed["date"]) == ["2024-01-08", "2024-01-09"] and list(listed["n350"]) == ["1", "0"]
    s350 = pd.read_csv(tmp_path / W.F350, dtype=str, keep_default_na=False)
    assert list(s350.columns) == W.COLS_350
    assert s350.iloc[0].to_dict() == {"docID": "S1", "submitDateTime": "2024-01-08 10:00", "filerName": EFF, "edinetCode": "E9001",
                                      "issuerEdinetCode": "E0001", "docDescription": "大量保有報告書", "withdrawalStatus": "0",
                                      "listed_on": "2024-01-08"}
    codes = pd.read_csv(tmp_path / W.FCODES, dtype=str)
    assert codes[["edinetCode", "secCode"]].values.tolist() == [["E0001", "10010"]]                 # 350 の証券の番号は使わない
    assert not (tmp_path / W.FLOCK).exists()                                                        # 止まっても印は残さない
    # 2 回目: 取れなかった 1/10 だけ見る。同じ組は書き足さない
    fake2 = Fake({"2024-01-10": _ok([_doc("Y4", ec="E0001", sec="10010"), _doc("Y5", ec="E0002", sec="20020", filer="二の会社")])})
    W.fetch(tmp_path, log=logs.append, client=_client(fake2), today="2024-01-11", days=DAYS)
    assert fake2.calls == ["2024-01-10"]
    codes = pd.read_csv(tmp_path / W.FCODES, dtype=str)
    assert codes[["edinetCode", "secCode", "seen_on"]].values.tolist() == [["E0001", "10010", "2024-01-08"], ["E0002", "20020", "2024-01-10"]]
    assert len(pd.read_csv(tmp_path / W.FLISTED)) == 3
    # 3 回目: もう見る日が無い
    fake3 = Fake({})
    W.fetch(tmp_path, log=logs.append, client=_client(fake3), today="2024-01-11", days=DAYS)
    assert fake3.calls == []


def test_fetch_without_key_stops_before_any_call(tmp_path, monkeypatch):
    monkeypatch.setattr(W.ED, "load_key", lambda *a, **k: None)
    monkeypatch.setattr(W.ED, "http_getter", lambda *a, **k: pytest.fail("通信しようとした"))
    with pytest.raises(base.NotReady, match="./cfd edinet key"):
        W.fetch(tmp_path)
    assert not (tmp_path / "wide").exists()


def test_fetch_with_key_never_logs_it(tmp_path, monkeypatch):
    secret = "SECRET-KEY-123"
    fake = Fake({"2024-01-08": _ok([_doc("Y1", ec="E0001", sec="10010")])})
    seen = []
    monkeypatch.setattr(W.ED, "load_key", lambda *a, **k: secret)
    monkeypatch.setattr(W.ED, "http_getter", lambda key, *a, **k: seen.append(key) or fake)
    monkeypatch.setattr(W.ED, "Client", functools.partial(ED.Client, sleep=lambda s: None))
    logs = []
    W.fetch(tmp_path, log=logs.append, today="2024-01-11", days=DAYS)
    assert seen == [secret] and fake.calls == ["2024-01-08", "2024-01-09", "2024-01-10"]
    assert logs and not any(secret in x for x in logs)


def test_fetch_lock(tmp_path):
    lock = tmp_path / W.FLOCK
    lock.parent.mkdir(parents=True)
    lock.write_text(str(os.getpid()))                                                               # 動いている別の取得
    fake = Fake({})
    with pytest.raises(RuntimeError, match="別の取得"):
        W.fetch(tmp_path, log=lambda s: None, client=_client(fake), today="2024-01-11", days=DAYS)
    assert fake.calls == [] and lock.exists()                                                       # 人の印は消さない
    for stale in ("", "abc"):                                                                       # 残っただけの印は飛ばして進む
        lock.write_text(stale)
        W.fetch(tmp_path, log=lambda s: None, client=_client(fake), today="2024-01-11", days=DAYS)
        assert not lock.exists()


# --------------------------------------------------------------------------- 計算（手で数える）
def _close(days, tickers, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, (len(days), len(tickers))), axis=0)), index=days, columns=tickers)


def _expected(close, ticker, day, hold=20):
    """1 日ずつ掛け合わせて、事象の値を手で数える（部品とは別の書き方）。"""
    idx = close.index
    b = idx.get_loc(idx[idx > pd.Timestamp(day)][0])
    r = close.pct_change(fill_method=None).clip(-0.3, 0.3)
    stock = market = 1.0
    for k in range(b + 1, b + hold + 1):
        stock *= 1 + r[ticker].iloc[k]
        market *= 1 + r.iloc[k].mean()
    return idx[b], (stock - 1) - (market - 1) - 0.001


ROWS = [  # docID, 提出日時, 出した人, その番号, 相手の会社, 説明, 取り下げ
    ("a", "2016-03-10 10:00", EFF, "E9001", "E1001", "大量保有報告書", "0"),                    # 使う
    ("b", "2016-08-01 10:00", EFF, "E9001", "E1001", "大量保有報告書", "0"),                    # 同じ出した人 × 同じ会社の 2 回目
    ("c", "2016-04-01 10:00", EFF, "E9001", "E1001", "変更報告書", "0"),
    ("d", "2016-05-06 15:00", "ひびき・パース・アドバイザーズ・リミテッド", "E9002", "E1002", "大量保有報告書", ""),   # 金曜 → 月曜に買う
    ("e", "2016-09-15 09:30", "株式会社レノ", "E9003", "E1003", "大量保有報告書（特例対象株券等）", "0"),
    ("f", "2016-09-16 09:30", "株式会社レノックス", "E9004", "E1003", "大量保有報告書", "0"),
    ("g", "2016-10-03 09:30", "野村證券株式会社", "E9010", "E1002", "大量保有報告書", "0"),
    ("h", "2016-10-04 09:30", "Elliott Investment Management L.P.", "E9005", "E1002", "大量保有報告書", "1"),   # 取り下げ
    ("i", "2016-10-05 09:30", "Elliott Investment Management L.P.", "E9005", "E1009", "大量保有報告書", "0"),   # 値段の表に無い
    ("j", "2016-11-04 09:30", "ＥＬＬＩＯＴＴ　ＩＮＶＥＳＴＭＥＮＴ", "E9006", "E1004", "大量保有報告書", "0"),         # E1004 → 1005.T
    ("k", "2016-11-04 11:00", EFF, "E9001", "E1002", "大量保有報告書", "0"),                    # 別の会社なら使う
    ("l", "2016-11-07 11:00", EFF, "E9001", "E1002", "訂正報告書（大量保有報告書・変更報告書）", "0"),
    ("m", "2016-12-01 11:00", "Dalton Investments, Inc.", "E9007", "E1006", "大量保有報告書", "0"),   # 証券の番号が分からない
    ("n", "2017-06-01 11:00", "Elliott Investment Management L.P.", "E9005", "E1001", "大量保有報告書", "0"),   # 6 月は終わっていない
    ("a", "2016-03-10 10:00", EFF, "E9001", "E1001", "大量保有報告書", "0"),                    # 書き足しが 2 回になった行
]
CODES = [("E1001", "10010", "一", "2016-01-04"), ("E1002", "10020", "二", "2016-01-04"), ("E1003", "10030", "三", "2016-01-04"),
         ("E1009", "99990", "九", "2016-01-04"), ("E1004", "10040", "四", "2016-01-04"), ("E1004", "10050", "四", "2016-06-01")]


def _write(data, close, rows=ROWS, listed=None, codes=CODES):
    (data / "stocks").mkdir(parents=True, exist_ok=True)
    close.to_csv(data / "stocks" / "jp_close.csv")
    (data / "wide").mkdir(parents=True, exist_ok=True)
    listed = pd.bdate_range("2016-01-04", "2017-06-30") if listed is None else listed
    pd.DataFrame({"date": [f"{d:%Y-%m-%d}" for d in listed], "n": 5, "n350": 1, "checked_at": "2026-11-12 10:00"}).to_csv(data / W.FLISTED, index=False)
    pd.DataFrame([(*r, r[1][:10]) for r in rows], columns=W.COLS_350).to_csv(data / W.F350, index=False)
    pd.DataFrame(codes, columns=W.COLS_CODES).to_csv(data / W.FCODES, index=False)
    return data


def _data(tmp_path, seed=3):
    close = _close(pd.bdate_range("2016-01-01", "2017-06-30"), [f"{1001 + k}.T" for k in range(6)], seed)
    return _write(tmp_path, close), close


def test_compute_by_hand(tmp_path):
    data, close = _data(tmp_path)
    s = W.compute(data)
    d = s.d
    assert d.index[0] == pd.Timestamp("2016-01-31") and d.index[-1] == pd.Timestamp("2017-05-31") and len(d) == 17
    want = {}
    for t, day in [("1001.T", "2016-03-10"), ("1002.T", "2016-05-06"), ("1003.T", "2016-09-15"), ("1005.T", "2016-11-04"),
                   ("1002.T", "2016-11-04")]:
        buy, v = _expected(close, t, day)
        want.setdefault(base.month_end(buy), []).append(v)
    assert sorted(want) == [pd.Timestamp(x) for x in ("2016-03-31", "2016-05-31", "2016-09-30", "2016-11-30")]
    for mth, vs in want.items():
        assert d[mth] == pytest.approx(np.mean(vs), abs=1e-12)
    assert (d.drop(list(want)) == 0).all()
    assert s.events == 5
    assert s.gross == pytest.approx(sum(np.mean(vs) + 0.001 for vs in want.values()) / 17, abs=1e-12)
    text = "\n".join(s.notes)
    assert "350 の書類 14 件" in text and "取り下げのないもの 11 件" in text and "名前の一覧に合うもの 9 件" in text
    assert "結びつくもの 7 件" in text and "最初の 1 回 6 件 → 使った 5 件" in text
    assert "期間の外（終わっていない月など） 1 件" in text and "見ていない平日 0 日" in text


def test_no_lookahead(tmp_path):
    data, close = _data(tmp_path / "a")
    full = W.compute(data).d
    cut = pd.Timestamp("2016-12-15")
    # 12/15 より後の値段を入れ替え、後の書類を足しても、10 月までの d は変わらない（10 月の買いは 12/15 までに売る）
    noisy = close.copy()
    later = noisy.index > cut
    noisy.loc[later] = noisy.loc[later] * np.random.default_rng(5).uniform(0.7, 1.3, noisy.loc[later].shape)
    extra = ROWS + [("z", "2016-12-16 10:00", EFF, "E9001", "E1003", "大量保有報告書", "0")]
    d2 = W.compute(_write(tmp_path / "b", noisy, rows=extra)).d
    upto = pd.Timestamp("2016-10-31")
    assert np.allclose(full[full.index <= upto], d2[d2.index <= upto])
    # 12/15 で切ったデータ（値段・一覧・書類）で計算した月は、全部のデータの同じ月と同じ
    short = [r for r in ROWS if pd.Timestamp(r[1]) <= cut]
    d3 = W.compute(_write(tmp_path / "c", close[close.index <= cut], rows=short,
                          listed=pd.bdate_range("2016-01-04", cut))).d
    assert d3.index[-1] == upto
    assert np.allclose(d3, full.reindex(d3.index))


def test_check_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(W.ED, "fetch_days", lambda today, *a, **k: list(pd.bdate_range("2016-01-04", today)))
    data, _ = _data(tmp_path)
    c = W.check(data, today="2017-07-03")
    assert c.ok and c.key == "W6"
    text = "\n".join(c.lines)
    assert "一覧を見た日 390 日（2016-01-04〜2017-06-30）。見るはずの平日 390 日" in text and "350 の書類 14 件" in text
    assert "d を作れる月 2016-01〜2017-05（17 か月）" in text
    assert "%" not in text.replace("（100.0%）", "")                                            # 割合は見た日の数だけ。リターンは出さない
    some = pd.bdate_range("2016-01-04", "2017-06-30")
    _write(data, pd.read_csv(data / "stocks" / "jp_close.csv", index_col=0, parse_dates=True), listed=some[::10].append(some[1::10]).sort_values())
    c = W.check(data, today="2017-07-03")
    assert not c.ok and "95%" in c.why


def test_no_activist_gives_zero_months(tmp_path):
    close = _close(pd.bdate_range("2016-01-01", "2017-06-30"), [f"{1001 + k}.T" for k in range(6)])
    rows = [r for r in ROWS if r[0] in ("f", "g")]                                              # 名前の一覧に合わないものだけ
    s = W.compute(_write(tmp_path, close, rows=rows))
    assert s.events == 0 and len(s.d) == 17 and (s.d == 0).all() and s.gross == 0


# --------------------------------------------------------------------------- データが無い・形が違う
def test_not_ready(tmp_path):
    close = _close(pd.bdate_range("2016-01-01", "2017-06-30"), ["1001.T", "1002.T"])

    def drop_col(p, f, col):
        df = pd.read_csv(p / f, dtype=str)
        df.drop(columns=col).to_csv(p / f, index=False)

    cases = {
        "no_listed": lambda p: (p / W.FLISTED).unlink(),
        "no_350": lambda p: (p / W.F350).unlink(),
        "no_codes": lambda p: (p / W.FCODES).unlink(),
        "no_prices": lambda p: (p / "stocks" / "jp_close.csv").unlink(),
        "no_issuer_column": lambda p: drop_col(p, W.F350, "issuerEdinetCode"),
        "bad_listed_date": lambda p: (p / W.FLISTED).write_text("date,n,n350,checked_at\nきのう,1,0,x\n"),
        "empty_codes": lambda p: (p / W.FCODES).write_text(""),
        "too_short": lambda p: _write(p, close, listed=pd.bdate_range("2017-06-20", "2017-06-30")),
    }
    for name, make in cases.items():
        path = tmp_path / name
        _write(path, close)
        make(path)
        with pytest.raises(base.NotReady):
            W.compute(path)
        c = W.check(path)
        assert not c.ok and c.why, name


# --------------------------------------------------------------------------- 実際のファイルの癖（点検役が足した）
def test_header_only_350_gives_zero_months(tmp_path):
    """350 の書類が 1 行も無い（見出しだけ）でも落ちない。事象 0 の月はすべて 0。"""
    close = _close(pd.bdate_range("2016-01-01", "2017-06-30"), [f"{1001 + k}.T" for k in range(6)])
    data = _write(tmp_path, close, rows=[])
    s = W.compute(data)
    assert s.events == 0 and len(s.d) == 17 and (s.d == 0).all()
    assert "350 の書類 0 件" in "\n".join(W.check(data, today="2017-07-03").lines)


def test_mixed_submit_time_formats_are_not_dropped(tmp_path):
    """提出の日時の書き方が行ごとに違っても（T つき・秒つき）、黙って落とさない。"""
    data, _ = _data(tmp_path / "a")
    want = W.compute(data)
    rows = [(*r[:1], r[1].replace(" ", "T") + ":00", *r[2:]) if r[0] in ("d", "k") else r for r in ROWS]
    got = W.compute(_write(tmp_path / "b", pd.read_csv(data / "stocks" / "jp_close.csv", index_col=0), rows=rows))
    assert got.events == want.events == 5 and np.allclose(got.d, want.d)


def test_withdrawn_on_a_later_list_is_not_used(tmp_path):
    """同じ書類が後の日の一覧（取下げの日）に「取り下げ」で出たら、最初の日の一覧で 0 でも使わない。"""
    data, _ = _data(tmp_path / "a")
    want = W.compute(data)
    later = ("a", "2016-03-10 10:00", EFF, "E9001", "E1001", "大量保有報告書", "2")
    got = W.compute(_write(tmp_path / "b", pd.read_csv(data / "stocks" / "jp_close.csv", index_col=0), rows=ROWS + [later]))
    # a を使わないので、同じ出した人 × 同じ会社の次の大量保有報告書 b（8/1）が最初の 1 回になる
    assert want.events == got.events == 5
    assert got.d[pd.Timestamp("2016-03-31")] == 0 and want.d[pd.Timestamp("2016-03-31")] != 0
    assert want.d[pd.Timestamp("2016-08-31")] == 0 and got.d[pd.Timestamp("2016-08-31")] != 0


def test_tz_aware_price_dates(tmp_path):
    data, close = _data(tmp_path)
    want = W.compute(data).d
    tz = close.copy()
    tz.index = tz.index.tz_localize("Asia/Tokyo")
    tz.to_csv(data / "stocks" / "jp_close.csv")
    assert np.allclose(W.compute(data).d, want)


def test_append_writes_header_to_empty_file(tmp_path):
    """止まって空のまま残ったファイルに書き足すときも、見出しを書く（次の読み取りで列が無いにならない）。"""
    path = tmp_path / "x.csv"
    path.write_text("")
    W._append(path, [{"date": "2024-01-08", "n": 3, "n350": 0, "checked_at": "t"}], W.COLS_LISTED)
    assert list(pd.read_csv(path, dtype=str).columns) == W.COLS_LISTED
