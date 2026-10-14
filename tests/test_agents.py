import numpy as np
import pandas as pd

from cfdbot.agents import collector as C
from cfdbot.agents import decider as D
from cfdbot.agents import evaluator as E
from cfdbot.agents import ir_scorer as I
from cfdbot.agents import paths


def test_paths_and_evaluator_does_not_import_other_agents():
    p = paths("x")
    assert p["disclosures"].as_posix() == "x/ir/disclosures.csv" and p["ledger"].name == "ledger.csv"
    src = open(E.__file__, encoding="utf-8").read()
    for other in ("collector", "ir_scorer", "selector", "decider"):
        assert f"import {other}" not in src and f"from .{other}" not in src and f"from . import {other}" not in src


def test_collector_normalize_and_parsers():
    assert C.ticker_of("72030") == "7203.T" and C.ticker_of("417A0") == "417A.T"
    a = C.normalize_title("２０２６年３月期 第１四半期決算短信〔日本基準〕（連結）")
    b = C.normalize_title("2025年3月期 第2四半期決算短信〔日本基準〕(連結)")
    assert a == b == "0年0月期第0四半期決算短信〔日本基準〕(連結)"
    assert C.normalize_title("トヨタ自動車株式会社による自己株式の取得", "トヨタ自動車") == "株式会社による自己株式の取得"
    payload = {"items": [{"Tdnet": {"id": "1", "pubdate": "2026-10-05 15:30:00", "company_code": "72030", "company_name": "トヨタ ",
                                    "title": " 自己株式の取得状況に関するお知らせ",
                                    "document_url": "https://webapi.yanoshin.jp/rd.php?https://www.release.tdnet.info/inbs/140120261005540001.pdf"}}]}
    y = C.parse_yanoshin(payload)
    assert y.iloc[0].to_dict() == {"id": "140120261005540001", "time": "2026-10-05 15:30:00", "ticker": "7203.T", "company": "トヨタ",
                                   "title": "自己株式の取得状況に関するお知らせ",
                                   "url": "https://www.release.tdnet.info/inbs/140120261005540001.pdf", "xbrl": ""}
    payload["items"][0]["Tdnet"]["url_xbrl"] = "https://webapi.yanoshin.jp/rd.php?https://www.release.tdnet.info/inbs/081220261005540001.zip"
    o = C.parse_yanoshin(payload)
    assert o.iloc[0]["xbrl"] == "081220261005540001.zip" and o.iloc[0]["id"] == y.iloc[0]["id"]
    extra = pd.DataFrame([("x1", "2026-10-06 09:00:00", "9999.T", "他社", "お知らせ", "", "")], columns=C.COLUMNS)
    m = C.merge_disclosures(y, pd.concat([o, extra]), {"7203.T"})
    assert len(m) == 1 and m.iloc[0]["id"] == "140120261005540001" and m.iloc[0]["xbrl"] == ""   # 同じ開示は 1 回、対象外の会社は入れない
    assert C.merge_disclosures(y, o, {"7203.T"}, prefer_new=True).iloc[0]["xbrl"] == "081220261005540001.zip"   # 取り直しは新しい方を残す
    old = y.drop(columns=["xbrl"])                                                    # 前の形（xbrl の列が無い）も読める
    assert C.merge_disclosures(old, extra.iloc[:0])["xbrl"].tolist() == [""]
    assert C.doc_url("7203.T", "a.pdf") == "https://www2.jpx.co.jp/disc/72030/a.pdf" and C.doc_url("417A.T", "b.zip").endswith("/417A0/b.zip")


def test_collector_embeds_only_new_titles(tmp_path):
    class Fake:
        calls: list = []

        def encode(self, texts):
            Fake.calls.append(list(texts))
            return np.array([[len(t), 1.0] for t in texts], dtype=np.float32)

    path = tmp_path / "ir" / "titles.npz"
    keys = pd.Series(["b", "a", "b", ""])
    assert C.embed_new_titles(keys, path, Fake(), log=lambda s: None) == 2
    t, v = C.load_title_vectors(path)
    assert list(t) == ["a", "b"] and v.shape == (2, 2) and v.dtype == np.float16
    assert C.embed_new_titles(pd.Series(["a", "ccc"]), path, Fake(), log=lambda s: None) == 1 and Fake.calls[-1] == ["ccc"]
    assert list(C.load_title_vectors(path)[0]) == ["a", "b", "ccc"]


def _pdf(text: str) -> bytes:
    """文字が 1 行だけ入った最小の PDF。"""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs):
        offs.append(len(out))
        out += f"{i + 1} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()


def _xbrl_zip() -> bytes:
    import io
    import zipfile

    ix = ('<html><body><ix:nonFraction contextRef="CurrentYearDuration_ConsolidatedMember_LowerMember" name="tse-ed-t:NetSales" '
          'unitRef="JPY" xsi:nil="true"/><ix:nonNumeric name="tse-ed-t:Empty" contextRef="C"/>'      # 値の無い項目（/> で閉じる）
          '<ix:nonFraction contextRef="CurrentYearDuration_ConsolidatedMember_ResultMember" decimals="-6" scale="6" '
          'format="ixt:numdotdecimal" name="tse-ed-t:NetSales" unitRef="JPY">931,265</ix:nonFraction>'
          '△<ix:nonFraction name="tse-ed-t:ChangeInOperatingIncome" contextRef="NextYearDuration_ConsolidatedMember_ForecastMember" '
          'scale="-2" sign="-" format="ixt:numdotdecimal">12.5</ix:nonFraction>'
          '<ix:nonFraction name="tse-ed-t:DividendPerShare" contextRef="X" xsi:nil="true"></ix:nonFraction>'
          '<ix:nonNumeric name="tse-ed-t:FiscalYearEnd" contextRef="CurrentYearInstant" format="ixt:dateyearmonthdaycjk"> 2026年３月31日 </ix:nonNumeric>'
          '<ix:nonNumeric name="tse-ed-t:CorrectionOfForecast" contextRef="C" format="ixt:booleantrue"><span>有</span></ix:nonNumeric>'
          '<ix:nonNumeric name="tse-ed-t:LongNote" contextRef="C">' + "長い説明" * 30 + '</ix:nonNumeric></body></html>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("XBRLData/Summary/tse-acedjpsm-13320-20260520313320-ixbrl.htm", ix)
        z.writestr("XBRLData/Attachment/0500000-acbs01-tse-acedjpfr-13320-ixbrl.htm", ix.replace("NetSales", "Ignored"))
    return buf.getvalue()


def test_collector_reads_pdf_text_and_xbrl_numbers():
    assert C.pdf_text(_pdf("Upward revision of forecast")) == "Upward revision of forecast"
    assert C.pdf_text(b"not a pdf") == "" and len(C.pdf_text(_pdf("abc"), max_chars=2)) == 2
    f = C.xbrl_facts(_xbrl_zip())
    num = f[f["text"] == ""].drop(columns="text")
    assert num.to_dict("records") == [
        {"kind": "acedjpsm", "name": "NetSales", "context": "CurrentYearDuration_ConsolidatedMember_ResultMember", "value": 931265e6},
        {"kind": "acedjpsm", "name": "ChangeInOperatingIncome", "context": "NextYearDuration_ConsolidatedMember_ForecastMember", "value": -0.125}]
    notes = f[f["text"] != ""].set_index("name")["text"].to_dict()                    # 数字でない項目（長いものは入れない）
    assert notes == {"FiscalYearEnd": "2026年３月31日", "CorrectionOfForecast": "true"}
    assert C.xbrl_facts(b"broken").empty and C.xbrl_summary(b"broken") == b""         # サマリー以外（財務諸表の本体）と、値の無いものは入れない
    import io
    import zipfile

    kept = zipfile.ZipFile(io.BytesIO(C.xbrl_summary(_xbrl_zip())))
    assert kept.namelist() == ["tse-acedjpsm-13320-20260520313320-ixbrl.htm"] and C.xbrl_facts(C.xbrl_summary(_xbrl_zip())).equals(f)


def test_collector_collects_docs_slowly_and_resumes(tmp_path):
    disc = pd.DataFrame([("p1", "2026-10-08 15:30:00", "7203.T", "トヨタ", "決算短信", "", "x1.zip"),
                         ("p2", "2026-10-09 15:30:00", "1332.T", "ニッスイ", "お知らせ", "", ""),
                         ("p3", "2026-10-13 10:00:00", "1332.T", "ニッスイ", "今日の開示", "", "")], columns=C.COLUMNS)
    calls = []

    def get(url):
        calls.append(url)
        if "p3" in url:
            raise OSError("404")                                                      # 当日の分はまだ載っていない
        return _xbrl_zip() if url.endswith(".zip") else _pdf("hello")

    got = C.collect_docs(disc, tmp_path, interval=0, get=get, log=lambda s: None)
    assert got == {"text": 2, "xbrl": 1, "missing": 1, "left": 0}
    assert calls[0] == "https://www2.jpx.co.jp/disc/72030/p1.pdf" and calls[1].endswith("/72030/x1.zip")
    assert (tmp_path / "text" / "2026-10" / "p1.txt").read_text(encoding="utf-8") == "hello"
    assert pd.read_csv(tmp_path / "xbrl" / "2026-10" / "x1.csv")["name"].tolist()[:2] == ["NetSales", "ChangeInOperatingIncome"]
    assert (tmp_path / "xbrl" / "2026-10" / "x1.zip").exists()                        # サマリーの原本も残す
    (tmp_path / "xbrl" / "2026-10" / "x1.csv").write_text("kind,name,context,value,text\n")     # 表だけ壊れても、原本から作り直せる
    assert C.reparse_facts(tmp_path) == 1 and len(pd.read_csv(tmp_path / "xbrl" / "2026-10" / "x1.csv")) == 4
    calls.clear()
    again = C.collect_docs(disc, tmp_path, interval=0, get=get, log=lambda s: None)
    assert again["text"] == 0 and again["missing"] == 1 and calls == ["https://www2.jpx.co.jp/disc/13320/p3.pdf"]   # 取れた分は取り直さない
    import os

    (tmp_path / ".docs.lock").write_text(str(os.getpid()))                           # ほかの取得が動いている間は取りに行かない
    calls.clear()
    assert C.collect_docs(disc, tmp_path, interval=0, get=get, log=lambda s: None).get("busy") == 1 and calls == []
    (tmp_path / ".docs.lock").write_text("999999999")                                 # 止まった取得の印は無視して進む
    assert "busy" not in C.collect_docs(disc, tmp_path, interval=0, get=get, log=lambda s: None) and not (tmp_path / ".docs.lock").exists()
    only = C.collect_docs(disc, tmp_path / "b", pdf=False, max_docs=5, interval=0, get=get, log=lambda s: None)
    assert only == {"text": 0, "xbrl": 1, "missing": 0, "left": 0}
    assert C.collect_docs(disc, tmp_path / "c", max_docs=1, interval=0, get=get, log=lambda s: None)["left"] == 3
    import time

    assert C.INTERVAL == 1.0                                                          # 1 秒に 1 件（利用者が決めた速さ）
    t0 = time.monotonic()
    C.collect_docs(disc.iloc[:1], tmp_path / "d", xbrl=False, interval=0.2, get=get, log=lambda s: None)
    C.collect_docs(disc.iloc[2:], tmp_path / "d", xbrl=False, interval=0.2, get=get, log=lambda s: None)   # 取れなくても同じだけ待つ
    assert 0.35 < time.monotonic() - t0 < 1.0


def test_ir_decision_day_and_daily_features():
    idx = pd.DatetimeIndex(["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06"])       # 3・4 日は土日
    times = pd.Series(["2026-10-01 13:00:00", "2026-10-01 13:00:01", "2026-10-02 15:30:00", "2026-10-04 10:00:00",
                       "2026-10-06 15:30:00", "2026-09-30 20:00:00"])
    assert list(I.decision_pos(times, idx)) == [0, 1, 2, 2, 4, 0]                       # 13:00 まではその日。後は次の営業日
    assert list(E.decision_pos(times, idx)) == list(I.decision_pos(times, idx))         # 評価 AI も同じ決まり（別に持つ）
    disc = pd.DataFrame({"time": times, "ticker": ["A", "A", "A", "B", "A", "Z"], "title": list("abcdef")})
    keys = pd.Series(["k1", "k2", "k1", "k2", "k1", "k1"])
    vecs = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float16)
    table, mat = I.daily_ir(disc, keys, np.array(["k1", "k2"]), vecs, idx, ["A", "B"])
    assert table[["pos", "j", "n"]].to_numpy().tolist() == [[0, 0, 1], [1, 0, 1], [2, 0, 1], [2, 1, 1]]   # 先の日・対象外の株は入らない
    assert np.allclose(mat, [[1, 0], [0, 1], [1, 0], [0, 1]]) and table["titles"].tolist() == ["a", "b", "c", "d"]
    two = pd.DataFrame({"time": ["2026-10-02 09:00:00", "2026-10-02 11:00:00"], "ticker": ["A", "A"], "title": ["x", "y"]})
    t2, m2 = I.daily_ir(two, pd.Series(["k1", "k2"]), np.array(["k1", "k2"]), vecs, idx, ["A", "B"])
    assert t2["n"].tolist() == [2] and np.allclose(m2, [[0.5, 0.5]]) and t2["titles"].iloc[0] == "x / y"


def test_ir_walk_forward_learns_which_disclosures_precede_rises():
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2010-01-04", "2018-12-31")
    n_stock, rows = 60, 30_000
    pos = np.sort(rng.integers(0, len(idx) - 10, rows))
    j = rng.integers(0, n_stock, rows)
    keep = ~pd.Series(pos * n_stock + j).duplicated().to_numpy()
    pos, j = pos[keep], j[keep]
    good = rng.random(len(pos)) < 0.5
    mat = (np.where(good[:, None], 1.0, -1.0) * np.r_[1.0, np.zeros(39)] + rng.normal(0, 1.0, (len(pos), 40))).astype(np.float32)
    y = rng.normal(0, 0.25, (len(idx), n_stock))
    y[pos, j] += np.where(good, 0.15, -0.15)                                           # 「良い開示」の後は順位が上がる
    table = pd.DataFrame({"pos": pos, "j": j, "n": 1, "titles": ""})
    score = I.walk_forward(table, mat, y, idx, first_year=2016, log=lambda s: None)
    years = idx.year.to_numpy()[pos]
    assert np.isnan(score[years < 2016]).all() and np.isfinite(score[years >= 2016]).all()
    late = years >= 2016
    assert np.corrcoef(score[late], y[pos, j][late])[0, 1] > 0.2 and score[late & good].mean() > score[late & ~good].mean() + 0.05
    close = pd.DataFrame({"A": [1.0, 1, 1, 1, 1, 2, 2], "B": [1.0, 1, 1, 1, 1, 1, 1]}, index=idx[:7])
    r = I.target_rank(close, hold=5)
    assert r[0, 0] == 0.5 and r[0, 1] == 0.0 and np.isnan(r[2:]).all()                # 5 日後に上がっている株が上の順位


def _scores(n_days=30, n=100, seed=0):
    idx = pd.bdate_range("2020-01-01", periods=n_days)
    cols = [f"S{i}" for i in range(n)]
    sel = pd.DataFrame(np.tile(np.linspace(-0.05, 0.05, n), (n_days, 1)), index=idx, columns=cols)   # 後ろの株ほど点数が高い
    return idx, cols, sel


def test_decider_books_and_reasons():
    idx, cols, sel = _scores()
    ir_long = pd.DataFrame({"date": [idx[5]] * 6, "ticker": ["S0", "S1", "S2", "S3", "S95", "S50"],
                            "score": [0.2, 0.1, -0.1, -0.2, -0.5, 0.0], "titles": ["上方修正", "増配", "x", "y", "下方修正", "z"]})
    ir = D.ir_matrix(ir_long, idx, cols)
    assert ir.notna().sum().sum() == 6 and ir.loc[idx[5], "S0"] == 0.2
    bk = D.books(sel, ir)
    assert (bk["selector"].sum(axis=1) == 10).all() and bk["selector"][5, 90:].all()
    assert bk["combined"][4].tolist() == bk["selector"][4].tolist()                   # 開示の無い日は銘柄選定と同じ
    assert bk["combined"][5, 0] and bk["combined"][5, 1] and not bk["combined"][5, 95] and bk["combined"][5].sum() == 10
    assert bk["ir"][5].sum() == 1 and bk["ir"][5, 0] and not bk["ir"][4].any()        # 6 社の上位 20% = 1 社
    few = D.books(sel, D.ir_matrix(ir_long.iloc[:4], idx, cols))
    assert not few["ir"].any()                                                        # 開示が 5 社未満の日は IR だけでは買わない
    table = D.decisions_table(bk, sel, ir, str(idx[3].date()))
    assert set(table["book"]) == {"selector", "combined", "ir"} and table["date"].min() == idx[3]
    day = table[(table["date"] == idx[5]) & (table["book"] == "combined")]
    out = D.explain(day, ir_long, {"S0": "会社ゼロ"})
    r0 = out[out["ticker"] == "S0"].iloc[0]
    assert r0["name"] == "会社ゼロ" and "IR の点数 +0.200（上方修正）" in r0["reason"]
    assert "開示なし" in out[out["ticker"] == "S99"].iloc[0]["reason"]


def test_evaluator_audit_placebo_helpers_and_verdict():
    idx, cols, sel = _scores(n_days=400)
    rng = np.random.default_rng(1)
    ir_w = np.full(sel.shape, np.nan)
    for t in range(400):
        ir_w[t, rng.choice(100, 12, replace=False)] = rng.normal(0, 0.05, 12)
    ir_long = pd.DataFrame([(idx[t], cols[j], ir_w[t, j]) for t, j in zip(*np.nonzero(np.isfinite(ir_w)))], columns=["date", "ticker", "score"])
    bk = D.books(sel, D.ir_matrix(ir_long, idx, cols))
    dec = D.decisions_table(bk, sel, D.ir_matrix(ir_long, idx, cols), str(idx[0].date()))
    exp = E.expected_books(sel.to_numpy(), ir_w)
    for b in ("selector", "combined", "ir"):
        assert (E.to_matrix(dec, b, idx, cols) == exp[b]).all()                       # 判断 AI の記録は、評価 AI の計算と一致する
    sh = E.shuffle_rows(ir_w, rng)
    assert (np.isfinite(sh) == np.isfinite(ir_w)).all() and np.allclose(np.sort(sh[3][np.isfinite(sh[3])]), np.sort(ir_w[3][np.isfinite(ir_w[3])]))
    assert not np.allclose(np.nan_to_num(sh), np.nan_to_num(ir_w))
    pool = np.isfinite(ir_w)
    rnd = E.random_among(pool, np.full(400, 3), rng)
    assert (rnd.sum(axis=1) == 3).all() and not (rnd & ~pool).any()

    cc = pd.DataFrame(rng.normal(0, 0.01, sel.shape), index=idx, columns=cols)
    cc.iloc[:, 90:] += 0.002                                                          # 銘柄選定が選ぶ株は本当に上がる
    pnl = E.book_pnl(exp["selector"], cc)
    manual = (cc.iloc[:, 90:].mean(axis=1) - cc.mean(axis=1)).iloc[10:].mean()           # 選ばれた 10 銘柄の平均 − 全銘柄の平均
    assert abs(pnl["gross"].iloc[10:].mean() - manual) < 1e-12 and manual > 0.001 and (pnl["d"] <= pnl["gross"] + 1e-12).all()
    ex = E.excess_forward(cc)
    assert np.isnan(ex[-5:]).all() and abs(ex[0, 0] - (cc.iloc[1:6, 0] - cc.iloc[1:6].mean(axis=1)).sum()) < 1e-12

    m = pd.date_range("2016-01-31", periods=130, freq="ME")
    strong = pd.Series(0.01 + np.random.default_rng(2).normal(0, 0.01, 130), index=m)
    assert E.verdict(strong, 1.0)["verdict"] == "使えそう"
    fading = strong.copy()
    fading[fading.index >= E.STRICT_FROM] = -0.001                                    # 2025-07 以降がマイナスなら格下げ
    v = E.verdict(fading, 1.0)
    assert v["verdict"] == "まだ判断できない" and v["strict_mean"] < 0
    assert E.verdict(strong, 0.5)["verdict"] == "使えない"


def test_evaluator_keywords_and_ledger():
    assert E.keyword_of("業績予想の上方修正に関するお知らせ") == "上方修正"
    assert E.keyword_of("（訂正）業績予想の上方修正") == "訂正"
    assert E.keyword_of("自己株式取得に係る事項の決定に関するお知らせ") == "自己株式の取得（決定）"
    assert E.keyword_of("自己株式の取得状況に関するお知らせ") == "自己株式の取得状況・終了"
    assert E.keyword_of("業績予想の修正に関するお知らせ") == "業績予想の修正（向きは題名に無い）"
    assert E.keyword_of("代表取締役の異動に関するお知らせ") == "その他"

    idx = pd.bdate_range("2016-01-04", periods=40)
    cc = pd.DataFrame(0.0, index=idx, columns=["A.T", "B.T", "C.T", "D.T"])
    cc.loc[idx[11]:idx[15], "A.T"] = 0.01                                             # A は 10 日目の引けの後 5 日間 +1% ずつ
    disc = pd.DataFrame({"time": [f"{idx[10]:%Y-%m-%d} 09:00:00", f"{idx[10]:%Y-%m-%d} 10:00:00", f"{idx[20]:%Y-%m-%d} 16:00:00"],
                         "ticker": ["A.T", "A.T", "B.T"], "title": ["業績予想の上方修正", "上方修正（追加）", "特別損失の計上"]})
    kt = E.keyword_table(disc, cc).set_index("kind")
    assert kt.loc["上方修正", "n"] == 1 and abs(kt.loc["上方修正", "mean"] - 0.05 * 0.75) < 1e-12   # 同じ日の同じ種類は 1 回。平均との差
    assert kt.loc["特別損失・減損", "n"] == 1 and abs(kt.loc["特別損失・減損", "mean"]) < 1e-12

    day = f"{idx[10]:%Y-%m-%d}"
    ledger = pd.DataFrame({"decided_at": [f"{day} 13:05:00", f"{day} 13:05:00", f"{day} 18:00:00", f"{idx[38]:%Y-%m-%d} 13:00:00"],
                           "date": [day, day, day, f"{idx[38]:%Y-%m-%d}"], "ticker": ["A.T", "B.T", "A.T", "A.T"],
                           "book": ["combined", "combined", "selector", "combined"]})
    s = E.ledger_summary(ledger, cc).set_index("book")
    assert "selector" not in s.index                                                  # 引けの後に書いた判断は数えない
    assert s.loc["combined", "days"] == 2 and s.loc["combined", "scored_days"] == 1   # 5 日たっていない日はまだ採点しない
    assert abs(s.loc["combined", "mean_excess"] - (0.0375 + -0.0125) / 2) < 1e-12
    assert E.ledger_summary(pd.DataFrame(), cc).empty


def test_schedule_plist_runs_on_weekdays_after_13():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("agents_script", Path(__file__).resolve().parents[1] / "scripts" / "agents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pl = mod.make_plist(Path("/x"), Path("/x/data/agents/run.log"))
    assert pl["ProgramArguments"] == ["/x/cfd", "agents", "run", "--prices"] and pl["Label"] == "com.bunkoko.agents"
    assert [(d["Weekday"], d["Hour"], d["Minute"]) for d in pl["StartCalendarInterval"]] == [(k, 13, 5) for k in range(1, 6)]
    c = mod.make_plist(Path("/x"), Path("/x/data/agents/collect.log"), "collect")
    assert c["ProgramArguments"] == ["/x/cfd", "agents", "collect", "--docs"] and c["Label"] == "com.bunkoko.agents.collect"
    assert c["StartCalendarInterval"] == {"Hour": 8, "Minute": 0}


# --------------------------------------------------------------------------- 決算の数字（docs/agents.md 10 章）
def _facts(kind: str, rows: list[tuple[str, str, float]], notes: dict | None = None) -> pd.DataFrame:
    data = [(kind, n, c, v, np.nan) for n, c, v in rows] + [(kind, k, "X", np.nan, v) for k, v in (notes or {}).items()]
    return pd.DataFrame(data, columns=C.FACT_COLUMNS)


def test_earnings_summarize_reads_each_kind_of_disclosure():
    from cfdbot.agents import earnings as N

    q = N.summarize(_facts("qcedjpsm", [
        ("ChangeInProfitAttributableToOwnersOfParent", "CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember", 0.107),
        ("ChangeInOperatingIncome", "CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember", 2.5),          # +250% は +100% にそろえる
        ("ChangeInNetSales", "PriorAccumulatedQ2Duration_ConsolidatedMember_ResultMember", 0.9),                  # 前年の欄は使わない
        ("ProfitAttributableToOwnersOfParent", "CurrentYearDuration_ConsolidatedMember_ForecastMember", 27880e6),
        ("ProfitAttributableToOwnersOfParent", "CurrentYearDuration_NonConsolidatedMember_ForecastMember", 1.0),
        ("DividendPerShare", "CurrentYearDuration_AnnualMember_NonConsolidatedMember_ForecastMember", 120.0),
        ("DividendPerShare", "PriorYearDuration_AnnualMember_NonConsolidatedMember_ResultMember", 100.0)], {"FiscalYearEnd": "2017-02-28"}))
    assert q["kind"] == 2 and q["fy"] == 2017 * 12 + 2 and q["fc"] == 27880e6 and q["dps"] == 120.0
    assert q["yoy_profit"] == 0.107 and q["yoy_op"] == 1.0 and np.isnan(q["yoy_sales"])
    a = N.summarize(_facts("acedifsm", [
        ("ProfitAttributableToOwnersOfParentIFRS", "CurrentYearDuration_ConsolidatedMember_ResultMember", 48e9),
        ("ProfitAttributableToOwnersOfParentIFRS", "NextYearDuration_ConsolidatedMember_LowerMember", 90e9),      # 幅のある予想は真ん中
        ("ProfitAttributableToOwnersOfParentIFRS", "NextYearDuration_ConsolidatedMember_UpperMember", 110e9),
        ("ChangeInSalesIFRS", "CurrentYearDuration_ConsolidatedMember_ResultMember", 0.062),
        ("DividendPerShare", "CurrentYearDuration_AnnualMember_NonConsolidatedMember_ResultMember", 350.0),
        ("DividendPerShare", "NextYearDuration_AnnualMember_NonConsolidatedMember_ForecastMember", 385.0)], {"FiscalYearEnd": "2016年８月31日"}))
    assert a["kind"] == 4 and a["fy"] == 2016 * 12 + 8 and a["actual"] == 48e9 and a["fc"] == 100e9 and a["yoy_sales"] == 0.062
    assert a["dps"] == 385.0 and a["dps_actual"] == 350.0
    r = N.summarize(_facts("rvfc", [("NetIncome", "CurrentYearDuration_NonConsolidatedMember_PreviousMember_ForecastMember", 35e9),
                                    ("NetIncome", "CurrentYearDuration_NonConsolidatedMember_CurrentMember_ForecastMember", 50e9),
                                    ("NetIncome", "CurrentAccumulatedQ2Duration_NonConsolidatedMember_CurrentMember_ForecastMember", 1.0)]))
    assert r["kind"] == 5 and abs(r["fc_rev"] - (50 - 35) / 35) < 1e-12 and r["fc"] == 50e9
    half = N.summarize(_facts("rvfc", [("NetIncome", "CurrentAccumulatedQ2Duration_ConsolidatedMember_CurrentMember_ForecastMember", 7e9),
                                       ("NetIncome", "CurrentAccumulatedQ2Duration_ConsolidatedMember_PreviousMember_ForecastMember", 5e9)]))
    assert half["kind"] == 5 and np.isnan(half["fc_rev"])                              # 上期だけの修正は、通期の変化率にしない
    d = N.summarize(_facts("rvdf", [("DividendPerShare", "CurrentYearDuration_AnnualMember_NonConsolidatedMember_PreviousMember_ForecastMember", 100.0),
                                    ("DividendPerShare", "CurrentYearDuration_AnnualMember_NonConsolidatedMember_CurrentMember_ForecastMember", 120.0)]))
    assert d["kind"] == 6 and abs(d["dps_rev"] - 0.2) < 1e-12 and N.summarize(pd.DataFrame(columns=C.FACT_COLUMNS)) == {}


def test_earnings_company_features_compare_with_the_previous_forecast():
    from cfdbot.agents import earnings as N

    fy = 2026 * 12 + 3
    items = [("2025-05-10 15:00:00", {"kind": 4, "fy": fy - 12, "fc": 100.0, "actual": 80.0, "dps": 50.0, "dps_actual": 50.0, "yoy_profit": 0.1}),
             ("2025-08-05 15:00:00", {"kind": 1, "fy": fy, "fc": 100.0, "dps": 50.0}),
             ("2025-11-05 15:00:00", {"kind": 2, "fy": fy, "fc": 120.0, "dps": 60.0}),                 # 短信の中で引き上げ
             ("2025-12-20 15:00:00", {"kind": 5, "fy": None, "fc": 90.0, "fc_rev": -0.25}),            # 修正の開示（前後は書類の中）
             ("2026-02-05 15:00:00", {"kind": 3, "fy": fy, "fc": 90.0, "dps": 60.0}),                  # 修正の後は据え置き
             ("2026-02-20 15:00:00", {"kind": 6, "fy": None, "dps": 30.0, "dps_rev": -0.5}),
             ("2026-05-10 15:00:00", {"kind": 4, "fy": fy, "fc": 45.0, "actual": 90.0, "dps": 30.0, "dps_actual": 30.0}),
             ("2026-08-05 15:00:00", {"kind": 1, "fy": fy + 12, "fc": 45.0, "dps": 33.0})]
    f = N.company_features(items[::-1])                                               # 順番がばらばらでも古い順に見る
    assert f["kind"].tolist() == [4, 1, 2, 5, 3, 6, 4, 1]
    assert np.allclose(f["fc_rev"], [0.25, 0.0, 0.2, -0.25, 0.0, np.nan, -0.5, 0.0], equal_nan=True)
    assert np.allclose(f["dps_rev"], [0.0, 0.0, 0.2, np.nan, 0.0, -0.5, 0.0, 0.1], equal_nan=True)
    assert f["yoy_profit"].iloc[0] == 0.1 and f["yoy_profit"].iloc[1:].isna().all()
    first = N.company_features([("2026-02-05 15:00:00", {"kind": 3, "fy": fy, "fc": 90.0})])
    assert np.isnan(first["fc_rev"].iloc[0])                                          # 直前の予想を知らない最初の開示は欠け


def test_earnings_daily_merges_same_day_and_model_learns(tmp_path):
    from cfdbot.agents import earnings as N

    idx = pd.bdate_range("2026-10-01", periods=5)
    feat = pd.DataFrame([("A", f"{idx[1]:%Y-%m-%d} 15:00:00", np.nan, 0.3, 0.2, 0.1, np.nan, 2),     # 短信（15:00 → 翌営業日の判断）
                         ("A", f"{idx[1]:%Y-%m-%d} 15:00:00", 0.4, np.nan, np.nan, np.nan, np.nan, 5),  # 同じ時刻の修正の開示
                         ("B", f"{idx[2]:%Y-%m-%d} 11:00:00", -0.1, np.nan, np.nan, np.nan, np.nan, 5),
                         ("Z", f"{idx[2]:%Y-%m-%d} 11:00:00", 0.9, np.nan, np.nan, np.nan, np.nan, 5)],
                        columns=["ticker", "time"] + N.FEATURES)
    t = N.daily(feat, idx, ["A", "B"])
    assert t[["pos", "j"]].to_numpy().tolist() == [[2, 0], [2, 1]]                     # 対象外の株（Z）は入らない
    a = t.iloc[0]
    assert a["fc_rev"] == 0.4 and a["yoy_profit"] == 0.3 and a["kind"] == 2 and np.isnan(a["dps_rev"])   # 修正の変化率＋短信の前年比

    (tmp_path / "xbrl" / "2026-10").mkdir(parents=True)
    _facts("rvfc", [("NetIncome", "CurrentYearDuration_ConsolidatedMember_PreviousMember_ForecastMember", 10.0),
                    ("NetIncome", "CurrentYearDuration_ConsolidatedMember_CurrentMember_ForecastMember", 12.0)]).to_csv(tmp_path / "xbrl" / "2026-10" / "r1.csv", index=False)
    disc = pd.DataFrame([("p1", "2026-10-02 15:00:00", "A", "会社", "業績予想の修正", "", "r1.zip"), ("p2", "2026-10-02 15:00:00", "A", "会社", "お知らせ", "", ""),
                         ("p3", "2026-10-05 15:00:00", "B", "会社", "まだ取っていない", "", "r9.zip")], columns=C.COLUMNS)
    built = N.build(disc, tmp_path, log=lambda s: None)
    assert len(built) == 1 and built.iloc[0]["ticker"] == "A" and abs(built.iloc[0]["fc_rev"] - 0.2) < 1e-12 and built.iloc[0]["kind"] == 5

    rng = np.random.default_rng(0)
    days = pd.bdate_range("2016-10-03", "2021-12-31")
    n = 6000
    pos, j = rng.integers(0, len(days) - 10, n), rng.integers(0, 80, n)
    table = pd.DataFrame({"pos": pos, "j": j, "fc_rev": rng.normal(0, 0.2, n), "yoy_profit": rng.normal(0, 0.2, n), "yoy_op": np.nan,
                          "yoy_sales": np.nan, "dps_rev": np.nan, "kind": rng.integers(1, 6, n)})
    y = rng.normal(0, 0.25, (len(days), 80))
    y[pos, j] += np.where(table["fc_rev"] > 0.05, 0.15, -0.05)                         # 上方修正の後は順位が上がる
    score = N.walk_forward(table, y, days, params=dict(N.GBM_PARAMS, min_samples_leaf=50), log=lambda s: None)
    years = days.year.to_numpy()[pos]
    assert np.isnan(score[years < 2019]).all() and np.isfinite(score[years >= 2019]).all()
    late = years >= 2019
    up = (table["fc_rev"] > 0.05).to_numpy()
    assert score[late & up].mean() > score[late & ~up].mean() + 0.1


def test_earnings_command_runs_end_to_end(tmp_path, capsys):
    """作り物の小さな市場で、数字の読み取り → 点数 → 判断 → 採点 → 報告書 まで通す（上方修正の後に上がる市場）。"""
    import importlib.util
    from argparse import Namespace
    from pathlib import Path

    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2016-10-03", "2025-12-31")
    cols = [f"{1000 + i}.T" for i in range(80)]
    ret = rng.normal(0.0003, 0.01, (len(idx), 80))
    data, root, ir = tmp_path / "stocks", tmp_path / "agents", tmp_path / "agents" / "ir"
    data.mkdir()
    rows = []
    for j, t in enumerate(cols):
        fc = 100.0
        for k, i in enumerate(range(5 + j % 40, len(idx) - 10, 63)):                  # 63 営業日ごとに決算短信
            new = fc * (1 + rng.choice([-0.1, 0.0, 0.0, 0.1]))
            if new > fc * 1.05:
                ret[i + 1:i + 6, j] += 0.004                                          # 上方修正の後 5 日は上がる
            stem = f"x{j:02d}{k:03d}"
            month = f"{idx[i]:%Y-%m}"
            (ir / "xbrl" / month).mkdir(parents=True, exist_ok=True)
            fy = idx[i] + pd.offsets.YearEnd(0)
            _facts("qcedjpsm", [("ProfitAttributableToOwnersOfParent", "CurrentYearDuration_ConsolidatedMember_ForecastMember", new),
                                ("ChangeInProfitAttributableToOwnersOfParent", "CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember", float(rng.normal(0, 0.2))),
                                ("ChangeInNetSales", "CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember", float(rng.normal(0, 0.1)))],
                   {"FiscalYearEnd": f"{fy:%Y-%m-%d}"}).to_csv(ir / "xbrl" / month / f"{stem}.csv", index=False)
            rows.append((f"p{stem}", f"{idx[i]:%Y-%m-%d} 11:00:00", t, "会社", "決算短信", "", f"{stem}.zip"))
            fc = new if fy.year == (idx[min(i + 63, len(idx) - 1)] + pd.offsets.YearEnd(0)).year else 100.0   # 決算期が替わったら予想は元に戻る
    close = pd.DataFrame(100 * np.exp(np.cumsum(ret, axis=0)), index=idx, columns=cols)
    close.to_csv(data / "jp_close.csv")
    (close.shift(1).fillna(100.0) * (1 + rng.normal(0, 0.002, close.shape))).to_csv(data / "jp_open.csv")
    pd.DataFrame(rows, columns=C.COLUMNS).to_csv(ir / "disclosures.csv", index=False)
    pd.DataFrame(rng.normal(0, 0.02, close.shape), index=idx, columns=cols).to_csv(root / "selector_scores.csv")

    spec = importlib.util.spec_from_file_location("agents_script2", Path(__file__).resolve().parents[1] / "scripts" / "agents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.cmd_earnings(Namespace(root=str(root), data=str(data), out=str(tmp_path / "out"), placebo=20))
    capsys.readouterr()
    report = next((tmp_path / "out").glob("*/earnings.md")).read_text(encoding="utf-8")
    v = pd.read_csv(next((tmp_path / "out").glob("*/verdicts_earnings.csv"))).set_index("key")
    assert list(v.index) == ["E1", "E2", "E3"] and v.loc["E1", "mean"] > 0 and v.loc["E1", "t"] > 3 and v.loc["E1", "placebo"] == 1.0
    assert v.loc["E1", "verdict"] == "使えそう" and "selector 0 件" in report and "e1 0 件" in report   # 記録は決まりどおり
    num = pd.read_csv(root / "ir_numbers.csv")
    assert {"score", "fc_rev", "kind"} <= set(num.columns) and num["fc_rev"].notna().sum() > 1000
    dec = pd.read_csv(root / "decisions_numbers.csv")
    assert {"selector", "combined_num", "e1"} <= set(dec["book"])                      # この市場は 1 日の開示が 5 社未満なので ir_num は買わない


# --------------------------------------------------------------------------- まだ入れていなかった項目（docs/agents.md 11 章）
def test_extra_fetch_keeps_the_pace_and_resumes(tmp_path):
    import time

    from cfdbot.agents import extra as X

    calls = []

    def history(t):
        calls.append((t, time.monotonic()))
        if t == "C":
            return pd.DataFrame()                                                       # 空で返ってきた銘柄は残さない
        idx = pd.DatetimeIndex(["2026-10-01", "2026-10-02"], tz="Asia/Tokyo")
        return pd.DataFrame({"Close": [10.0, 11.0], "Volume": [100, 0], "Stock Splits": [0.0, 2.0]}, index=idx)

    assert X.fetch_raw(["A", "B", "C"], tmp_path, interval=0.05, history=history, log=lambda s: None) == (2, 1)
    assert all(b[1] - a[1] >= 0.045 for a, b in zip(calls, calls[1:]))                  # 取り始めから次の取り始めまで間をあける
    assert X.fetch_raw(["A", "B", "C"], tmp_path, interval=0.0, history=history, log=lambda s: None) == (0, 1)   # 取れた分は取り直さない
    idx = pd.DatetimeIndex(["2026-10-01", "2026-10-02", "2026-10-05"])
    close, volume, splits = X.load_raw(tmp_path, idx, ["A", "B", "C"])
    assert close["A"].tolist()[:2] == [10.0, 11.0] and np.isnan(close["A"].iloc[2]) and close["C"].isna().all()
    assert volume["A"].iloc[0] == 100 and np.isnan(volume["A"].iloc[1])                 # 出来高 0 は欠け
    assert splits[["ticker", "ratio"]].to_numpy().tolist() == [["A", 2.0], ["B", 2.0]]


def test_extra_volume_and_industry_features_use_only_the_past():
    from cfdbot.agents import extra as X

    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2024-01-01", periods=300)
    cols = ["A", "B", "C", "D"]
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.01, (300, 4)), axis=0)), index=idx, columns=cols)
    volume = pd.DataFrame(rng.integers(1000, 2000, (300, 4)).astype(float), index=idx, columns=cols)
    cc = close.pct_change(fill_method=None)
    f = X.volume_features(cc, close, volume)
    assert list(f) == X.VOLUME
    t = 280
    assert abs(f["vol_shock_1"].iloc[t, 0] - volume.iloc[t - 1, 0] / volume.iloc[t - 51:t - 1, 0].mean()) < 1e-12
    assert abs(f["turnover_60"].iloc[t, 1] - (close * volume).iloc[t - 60:t, 1].mean()) < 1e-6
    v2, c2 = volume.copy(), close.copy()
    v2.iloc[t:] *= 10                                                                   # 当日から先を変えても、当日の入力は変わらない
    c2.iloc[t:] *= 2
    g = X.volume_features(c2.pct_change(fill_method=None), c2, v2)
    assert all(np.allclose(f[k].iloc[:t + 1], g[k].iloc[:t + 1], equal_nan=True) for k in X.VOLUME)
    volume.iloc[t - 1, 2] = 0.0                                                         # 出来高 0 の日は欠けとして扱う
    assert np.isnan(X.volume_features(cc, close, volume)["vol_shock_1"].iloc[t, 2])
    surge = X.volume_surge(v2)
    assert not surge.iloc[t].any() and surge.iloc[t + 1].all()                          # 急増は翌日の判断から見える

    ind = X.industry_features(close, pd.Series({"A": "化学", "B": "化学", "C": "機械", "D": "機械"}))
    r5 = np.log(close.shift(1) / close.shift(6))
    assert abs(ind["rel_ind_5"].iloc[t, 0] - (r5.iloc[t, 0] - r5.iloc[t, :2].mean())) < 1e-12
    r20 = np.log(close.shift(1) / close.shift(21))
    assert abs(ind["ind_ret_20"].iloc[t, 3] - r20.iloc[t, 2:].mean()) < 1e-12 and ind["ind_ret_20"].iloc[t, 2] == ind["ind_ret_20"].iloc[t, 3]
    i2 = X.industry_features(c2, pd.Series({"A": "化学", "B": "化学", "C": "機械", "D": "機械"}))
    assert all(np.allclose(ind[k].iloc[:t + 1], i2[k].iloc[:t + 1], equal_nan=True) for k in X.INDUSTRY)


def test_extra_fundamentals_are_point_in_time_and_follow_splits():
    from cfdbot.agents import extra as X

    idx = pd.bdate_range("2025-01-01", periods=420)
    # 実際の株価は 20 → 10 日目に 1:2 の分割で 10。Yahoo の終値は、分割より前もさかのぼって 10 にしてある
    raw = pd.DataFrame({"A": np.full(420, 10.0), "B": np.full(420, 50.0)}, index=idx)
    splits = pd.DataFrame({"date": [idx[10]], "ticker": ["A"], "ratio": [2.0]})
    events = pd.DataFrame({"ticker": ["A", "A"], "time": [f"{idx[3]:%Y-%m-%d} 15:00:00", f"{idx[12]:%Y-%m-%d} 11:00:00"],
                           "fc": [50.0, 60.0], "dps": [2.0, 1.0], "shares": [100.0, 200.0], "equity": [400.0, 400.0], "eq_ratio": [0.4, 0.5]})
    pos = I.decision_pos(events["time"], idx)
    assert pos.tolist() == [4, 12]
    f = X.fundamental_panel(events, pos, raw, splits)
    assert list(f) == X.FUNDAMENTAL and f["ep_fc"]["B"].isna().all()
    assert f["ep_fc"]["A"].iloc[:4].isna().all()                                        # 開示を使える日より前は欠け
    cap = np.exp(f["size"]["A"])
    assert np.allclose(cap.iloc[4:420 - 1].dropna(), 2000.0)                            # 100 株 × 20 円 = 分割の後の 200 株 × 10 円
    assert abs(f["ep_fc"]["A"].iloc[4] - 50 / 2000) < 1e-12 and abs(f["bm"]["A"].iloc[4] - 0.2) < 1e-12
    assert abs(f["dy_fc"]["A"].iloc[4] - 0.10) < 1e-12 and abs(f["roe_fc"]["A"].iloc[4] - 0.125) < 1e-12   # 配当 2 円 ÷ 株価 20 円
    assert abs(f["ep_fc"]["A"].iloc[12] - 60 / 2000) < 1e-12 and abs(f["dy_fc"]["A"].iloc[12] - 0.10) < 1e-12 and f["equity_ratio"]["A"].iloc[12] == 0.5
    last = pd.Timestamp(idx[12]) + pd.Timedelta(days=X.STALE_DAYS)
    assert np.isfinite(f["ep_fc"]["A"][idx <= last]).iloc[12:].all() and f["ep_fc"]["A"][idx > last].isna().all()   # 古くなったら欠け
    # 分割の後に出た「修正の開示」は、分割の前の短信の株数（100 株）を持ったまま。配当だけ新しい数え方（1 株 1 円）で出し直した
    carried = pd.DataFrame({"ticker": ["A", "A"], "time": [f"{idx[3]:%Y-%m-%d} 15:00:00", f"{idx[12]:%Y-%m-%d} 11:00:00"], "fc": [50.0, 60.0],
                            "dps": [2.0, 1.0], "shares": [100.0, 100.0], "equity": [400.0, 400.0], "eq_ratio": [0.4, 0.4],
                            "shares_time": [f"{idx[3]:%Y-%m-%d} 15:00:00"] * 2, "dps_time": [f"{idx[3]:%Y-%m-%d} 15:00:00", f"{idx[12]:%Y-%m-%d} 11:00:00"]})
    g = X.fundamental_panel(carried, pos, raw, splits)
    assert np.allclose(np.exp(g["size"]["A"]).iloc[4:250], 2000.0) and abs(g["dy_fc"]["A"].iloc[12] - 0.10) < 1e-12 and abs(g["ep_fc"]["A"].iloc[12] - 0.03) < 1e-12


def test_earnings_extra_reads_progress_shares_and_forecasts():
    from cfdbot.agents import earnings as N

    q = _facts("qcedjpsm", [("OperatingIncome", "CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember", 60.0),
                            ("OperatingIncome", "PriorAccumulatedQ2Duration_ConsolidatedMember_ResultMember", 40.0),
                            ("OperatingIncome", "CurrentYearDuration_ConsolidatedMember_ForecastMember", 100.0),
                            ("NetSales", "CurrentYearDuration_ConsolidatedMember_ForecastMember", 1000.0),
                            ("ProfitAttributableToOwnersOfParent", "CurrentYearDuration_ConsolidatedMember_ForecastMember", 70.0),
                            ("OwnersEquity", "CurrentAccumulatedQ2Instant_ConsolidatedMember_ResultMember", 500.0),
                            ("OwnersEquity", "PriorYearInstant_ConsolidatedMember_ResultMember", 450.0),
                            ("CapitalAdequacyRatio", "CurrentAccumulatedQ2Instant_ConsolidatedMember_ResultMember", 0.4),
                            (N.SHARES, "CurrentAccumulatedQ2Instant_NonConsolidatedMember_ResultMember", 110.0),
                            (N.TREASURY, "CurrentAccumulatedQ2Instant_NonConsolidatedMember_ResultMember", 10.0)], {"FiscalYearEnd": "2027-03-31"})
    x = N.summarize_extra(q)
    assert (x["cum_op"], x["cum_op_fc"], x["prior_cum_op"], x["fc_op"], x["fc_sales"]) == (60.0, 100.0, 40.0, 100.0, 1000.0)
    assert (x["shares"], x["equity"], x["eq_ratio"], x["op_name"]) == (100.0, 500.0, 0.4, "OperatingIncome")
    a = N.summarize_extra(_facts("acedifsm", [("OperatingIncomeIFRS", "CurrentYearDuration_ConsolidatedMember_ResultMember", 90.0),
                                              ("OperatingIncomeIFRS", "NextYearDuration_ConsolidatedMember_ForecastMember", 99.0),
                                              ("EquityAttributableToOwnersOfParentIFRS", "CurrentYearInstant_ConsolidatedMember_ResultMember", 300.0),
                                              (N.SHARES, "CurrentYearInstant_NonConsolidatedMember_ResultMember", 50.0)]))
    assert (a["actual_op"], a["fc_op"], a["equity"], a["shares"], a["op_name"]) == (90.0, 99.0, 300.0, 50.0, "OperatingIncomeIFRS")   # 自己株式が無ければ 0
    r = N.summarize_extra(_facts("rvfc", [("OperatingIncome", "CurrentYearDuration_ConsolidatedMember_PreviousMember_ForecastMember", 100.0),
                                          ("OperatingIncome", "CurrentYearDuration_ConsolidatedMember_CurrentMember_ForecastMember", 130.0)]))
    assert abs(r["fc_op_rev"] - 0.3) < 1e-12 and np.isnan(r["fc_sales_rev"]) and N.summarize_extra(_facts("rvdf", [("DividendPerShare", "x", 1.0)])) == {}

    fy = 2027 * 12 + 3
    base = {"shares": 100.0, "equity": 500.0, "eq_ratio": 0.4, "op_name": "OperatingIncome"}
    items = [("2026-05-10 15:00:00", dict(base, kind=4, fy=fy - 12, fc=70.0, dps=10.0, fc_op=100.0, fc_sales=1000.0, actual_op=80.0, actual_sales=800.0)),
             ("2026-08-05 15:00:00", dict(base, kind=1, fy=fy, fc=70.0, dps=10.0, fc_op=100.0, fc_sales=1000.0, cum_op=30.0, cum_op_fc=100.0, prior_cum_op=16.0)),
             ("2026-09-01 15:00:00", {"kind": 5, "fy": None, "fc": 84.0, "fc_rev": 0.2, "fc_op": 120.0, "fc_op_rev": 0.2, "fc_sales_rev": np.nan}),
             ("2026-09-15 15:00:00", {"kind": 6, "fy": None, "dps": 12.0, "dps_rev": 0.2}),
             ("2026-11-05 15:00:00", dict(base, kind=2, fy=fy, fc=84.0, dps=12.0, fc_op=120.0, fc_sales=1100.0, cum_op=60.0, cum_op_fc=120.0, prior_cum_op=48.0,
                                          shares=np.nan, op_name="OrdinaryIncome"))]
    e = N.company_extra(items[::-1])
    assert e["kind"].tolist() == [4, 1, 5, 6, 2]
    assert np.allclose(e["prog_diff"], [np.nan, 30 / 100 - 16 / 80, np.nan, np.nan, np.nan], equal_nan=True)   # 名前の違う利益どうしは比べない
    assert np.allclose(e["fc_op_rev"], [0.25, 0.0, 0.2, np.nan, 0.0], equal_nan=True) and np.allclose(e["fc_sales_rev"], [0.25, 0.0, np.nan, np.nan, 0.1], equal_nan=True)
    assert e["fc"].tolist() == [70.0, 70.0, 84.0, 84.0, 84.0] and e["dps"].tolist() == [10.0, 10.0, 10.0, 12.0, 12.0]   # 修正はその項目だけ書き換える
    assert e["shares"].tolist()[:4] == [100.0] * 4 and np.isnan(e["shares"].iloc[4])     # 短信は、欠けていれば欠けに書き換える
    assert e["shares_time"].str[:10].tolist() == ["2026-05-10", "2026-08-05", "2026-08-05", "2026-08-05", "2026-11-05"]   # 株数は短信の日の数え方
    assert e["dps_time"].str[:10].tolist() == ["2026-05-10", "2026-08-05", "2026-08-05", "2026-09-15", "2026-11-05"]
    assert abs(N._progress(30.0, 100.0, 16.0, 80.0) - 0.1) < 1e-12 and np.isnan(N._progress(30.0, -5.0, 16.0, 80.0)) and np.isnan(N._progress(30.0, 100.0, 16.0, 0.0))


def test_extra_repairs_unadjusted_splits_and_keeps_share_counts_continuous(tmp_path):
    from cfdbot.agents import earnings as N
    from cfdbot.agents import extra as X

    idx = pd.bdate_range("2026-01-05", periods=12)
    # Yahoo が直していない例: 分割（1:3）の記録は 8 日目なのに、値段は 7 日目に 1/3 になり、その前はさかのぼって直されていない
    d = pd.DataFrame({"close": [300.0] * 7 + [100.0] * 5, "volume": [10.0] * 7 + [30.0] * 5, "split": [0.0] * 8 + [3.0] + [0.0] * 3}, index=idx)
    fixed, n = X.repair_unadjusted(d)
    assert n == 1 and np.allclose(fixed["close"], 100.0) and np.allclose(fixed["volume"], 30.0) and fixed["split"][fixed["split"] > 0].index.tolist() == [idx[7]]
    ok = pd.DataFrame({"close": [100.0, 101.0, 99.0, 100.0, 102.0], "volume": [1.0] * 5, "split": [0.0, 0.0, 2.0, 0.0, 0.0]}, index=idx[:5])
    assert X.repair_unadjusted(ok)[1] == 0 and X.repair_unadjusted(ok)[0].equals(ok)     # 直してある分割はそのまま

    # 株数: 分割（1:2、権利落ち 3/28）の後の短信に、期末の実際の株数（分割の前）を書く会社と、分割の後の数に直して書く会社がある
    splits = pd.DataFrame({"date": pd.to_datetime(["2024-03-28"] * 2), "ticker": ["A", "B"], "ratio": [2.0, 2.0]})
    times = pd.Series(["2024-02-01 15:00:00", "2024-04-30 15:00:00", "2024-04-30 15:00:00", "2024-08-01 15:00:00"])
    assert X.adjusted_shares(times, np.array([100.0, 100.0, 100.0, 201.0]), splits, "A").tolist() == [200.0, 200.0, 200.0, 201.0]   # 前のまま書いた会社
    assert X.adjusted_shares(times, np.array([100.0, 200.0, 200.0, 201.0]), splits, "B").tolist() == [200.0, 200.0, 200.0, 201.0]   # 直して書いた会社
    assert X.adjusted_shares(times[1:], np.array([100.0, 100.0, 201.0]), splits, "A").tolist() == [100.0, 100.0, 201.0]            # 直前が無ければ「入れてある」とみなす
    assert X.adjusted_shares(times, np.array([100.0, np.nan, 150.0, 150.0]), splits, "C").tolist()[::2] == [100.0, 150.0]          # 分割の無い会社の増資はそのまま

    (tmp_path / "xbrl" / "2026-10").mkdir(parents=True)
    _facts("rvfc", [("NetIncome", "CurrentYearDuration_ConsolidatedMember_PreviousMember_ForecastMember", 10.0),
                    ("NetIncome", "CurrentYearDuration_ConsolidatedMember_CurrentMember_ForecastMember", 12.0)]).to_csv(tmp_path / "xbrl" / "2026-10" / "r1.csv", index=False)
    disc = pd.DataFrame([("p1", "2026-10-02 15:00:00", "A", "会社", "業績予想の修正", "", "r1.zip"),
                         ("p2", "2026-10-05 15:00:00", "A", "会社", "（訂正・数値データ訂正）「業績予想の修正」の一部訂正について", "", "r1.zip")], columns=C.COLUMNS)
    assert len(N.build(disc, tmp_path, log=lambda s: None)) == 2 and len(N.build(disc, tmp_path, log=lambda s: None, skip_corrections=True)) == 1
    assert N.is_correction(disc["title"]).tolist() == [False, True]


def test_extra_spread_counts_non_overlapping_days():
    from cfdbot.agents import extra as X

    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2024-01-01", periods=100)
    x = pd.DataFrame(rng.normal(size=(100, 60)), index=idx)
    forward = 0.01 * x.to_numpy() + rng.normal(0, 0.001, (100, 60))
    mean, t, n = X.spread(x, forward, np.arange(100) >= 20, 5)
    assert n == 16 and mean > 0.02 and t > 10
    x.iloc[:, 20:] = np.nan                                                             # 値のある銘柄が少ない日は数えない
    assert X.spread(x, forward, np.ones(100, bool), 5)[2] == 0


def test_extra_command_runs_end_to_end(tmp_path, capsys, monkeypatch):
    """作り物の小さな市場で、入力 → 学び直し → 採点 → 報告書 まで通す（進捗率が高い株が、その後 60 日上がる市場）。"""
    import importlib.util
    from argparse import Namespace
    from pathlib import Path

    from cfdbot import stocks_short as H
    from cfdbot.agents import earnings as N

    monkeypatch.setattr(H, "GBM_PARAMS", dict(H.GBM_PARAMS, max_iter=10, min_samples_leaf=200))   # 試験を速くする
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2016-10-03", "2025-12-31")
    cols = [f"{1000 + i}.T" for i in range(80)]
    ret = rng.normal(0.0003, 0.01, (len(idx), 80))
    data, root, ir = tmp_path / "stocks", tmp_path / "agents", tmp_path / "agents" / "ir"
    (data / "jp_raw").mkdir(parents=True)
    rows = []
    for j, t in enumerate(cols):
        actual = 100.0                                                                  # 前の期の通期の営業利益
        for k, i in enumerate(range(5 + j % 40, len(idx) - 70, 63)):                    # 63 営業日ごとに、第 1〜3 四半期 → 本決算
            q, fy = k % 4 + 1, f"{2017 + k // 4}-12-31"
            stem, month = f"x{j:02d}{k:03d}", f"{idx[i]:%Y-%m}"
            (ir / "xbrl" / month).mkdir(parents=True, exist_ok=True)
            shares = [(N.SHARES, "{}_NonConsolidatedMember_ResultMember", 1e6), ("OwnersEquity", "{}_ConsolidatedMember_ResultMember", 5e7)]
            if q == 4:
                facts = [("OperatingIncome", "CurrentYearDuration_ConsolidatedMember_ResultMember", actual),
                         ("ProfitAttributableToOwnersOfParent", "CurrentYearDuration_ConsolidatedMember_ResultMember", 70.0),
                         ("OperatingIncome", "NextYearDuration_ConsolidatedMember_ForecastMember", actual),
                         ("ProfitAttributableToOwnersOfParent", "NextYearDuration_ConsolidatedMember_ForecastMember", 70.0)]
                facts += [(n, c.format("CurrentYearInstant"), v) for n, c, v in shares]
                kind = "acedjpsm"
            else:
                surprise = rng.choice([-0.3, 0.0, 0.0, 0.3])
                if surprise > 0 and k >= 4:                                             # 進捗率が高い（前の期の実績が分かっている期だけ）
                    ret[i + 1:i + 61, j] += 0.0015
                facts = [("OperatingIncome", f"CurrentAccumulatedQ{q}Duration_ConsolidatedMember_ResultMember", actual * q / 4 * (1 + surprise)),
                         ("OperatingIncome", f"PriorAccumulatedQ{q}Duration_ConsolidatedMember_ResultMember", actual * q / 4),
                         ("OperatingIncome", "CurrentYearDuration_ConsolidatedMember_ForecastMember", actual),
                         ("ProfitAttributableToOwnersOfParent", "CurrentYearDuration_ConsolidatedMember_ForecastMember", 70.0),
                         ("ChangeInNetSales", f"CurrentAccumulatedQ{q}Duration_ConsolidatedMember_ResultMember", float(rng.normal(0, 0.1)))]
                facts += [(n, c.format(f"CurrentAccumulatedQ{q}Instant"), v) for n, c, v in shares]
                kind = "qcedjpsm"
            _facts(kind, facts, {"FiscalYearEnd": fy}).to_csv(ir / "xbrl" / month / f"{stem}.csv", index=False)
            rows.append((f"p{stem}", f"{idx[i]:%Y-%m-%d} 11:00:00", t, "会社", "決算短信", "", f"{stem}.zip"))
    close = pd.DataFrame(100 * np.exp(np.cumsum(ret, axis=0)), index=idx, columns=cols)
    close.to_csv(data / "jp_close.csv")
    (close.shift(1).fillna(100.0) * (1 + rng.normal(0, 0.002, close.shape))).to_csv(data / "jp_open.csv")
    pd.DataFrame({"ticker": cols, "name": cols, "sector": [f"業種{j % 8}" for j in range(80)], "size": "x"}).to_csv(data / "universe_jp.csv", index=False)
    for j, t in enumerate(cols[:78]):                                                   # 2 銘柄は出来高が取れなかったことにする
        split = np.zeros(len(idx))
        split[500] = 2.0 if j == 0 else 0.0
        pd.DataFrame({"date": idx, "close": close[t].to_numpy(), "volume": rng.integers(1000, 5000, len(idx)), "split": split}).to_csv(data / "jp_raw" / f"{t}.csv", index=False)
    rows.append(("pcorr", f"{idx[1500]:%Y-%m-%d} 11:00:00", cols[3], "会社", "（訂正・数値データ訂正）「第１四半期決算短信」の一部訂正について", "", "x03000.zip"))
    pd.DataFrame(rows, columns=C.COLUMNS).to_csv(ir / "disclosures.csv", index=False)
    pd.DataFrame(rng.normal(0, 0.02, close.shape), index=idx, columns=cols).to_csv(root / "selector_scores.csv")

    spec = importlib.util.spec_from_file_location("agents_script3", Path(__file__).resolve().parents[1] / "scripts" / "agents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    args = Namespace(root=str(root), data=str(data), out=str(tmp_path / "out"), placebo=10, fetch=False, reuse=False)
    mod.cmd_earnings(args)
    mod.cmd_extra(args)
    capsys.readouterr()
    report = next((tmp_path / "out").glob("*/extra.md")).read_text(encoding="utf-8")
    v = pd.read_csv(next((tmp_path / "out").glob("*/verdicts_extra.csv"))).set_index("key")
    assert list(v.index) == ["F1", "F2", "F3", "F4"]
    assert v.loc["F3", "mean"] > 0 and v.loc["F3", "t"] > 3 and v.loc["F3", "placebo"] == 1.0 and v.loc["F3", "verdict"] == "使えそう"
    assert "訂正の開示 1 回を除いた" in report and "78 銘柄、分割 1 回" in report and "訂正の開示を除くと、10 章の E1・E2 はどう変わるか" in report
    num = pd.read_csv(root / "ir_numbers_extra.csv")
    assert set(N.FEATURES + N.EXTRA) | {"score", "score6"} <= set(num.columns) and num["prog_diff"].notna().sum() > 1000 and num["ep_fc"].notna().sum() > 1000
    assert len(num) == len(pd.read_csv(root / "ir_numbers.csv")) - 1                    # 訂正の開示の分だけ、10 章の記録より行が少ない
    for name in ("selector_volume.csv", "selector_fundamental.csv"):
        sc = pd.read_csv(root / name, index_col=0)
        assert sc.shape[1] == 80 and sc.notna().any(axis=1).all()
    assert (data / "jp_volume.csv").exists() and (data / "jp_splits.csv").exists()
    mod.cmd_extra(Namespace(**{**vars(args), "reuse": True}))                           # 点数が書いてあれば学び直さない
    assert "銘柄選定（出来高を足した版）" not in capsys.readouterr().out
