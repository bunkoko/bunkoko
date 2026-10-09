import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdbot.news import (MAX_RECORDS, GdeltClient, MarketDaily, NewsStore, QueryError, Throttled, collect_articles,
                         collect_gkg, daily_timeline, flags_at, gdelt_url, gkg_slots, gkg_url, headline_id,
                         hold_returns, parse_artlist, parse_gkg, parse_gkg_file, parse_timeline, parse_yahoo_news,
                         sample_like_backfill, spikes, values_at, vol_ratios)
from cfdbot.news_embed import EmbeddingCache, HashEmbedder, daily_features, load_day_vectors, topic_scores

ROOT = Path(__file__).resolve().parents[1]


def _art(url, title, seen, lang="English"):
    return {"url": url, "url_mobile": "", "title": title, "seendate": seen, "socialimage": "",
            "domain": url.split("/")[2], "language": lang, "sourcecountry": "United States"}


def _artlist(arts):
    return json.dumps({"articles": arts})


def test_parse_artlist_times_ids_and_broken_escapes():
    text = _artlist([_art("https://a.com/x?utm_source=t", "Oil &amp; gas  rally", "20261009T101500Z"),
                     _art("https://b.com/y", "", "20261009T101500Z")])
    text = text.replace("rally", "rally \\q")        # GDELT の壊れた \ を含む JSON
    df = parse_artlist(text, "oil")
    assert len(df) == 1
    row = df.iloc[0]
    assert row["title"] == "Oil & gas rally \\q"
    assert row["published"] == pd.Timestamp("2026-10-09 10:15", tz="UTC")
    assert row["available"] == pd.Timestamp("2026-10-09 10:45", tz="UTC")     # 見つけてから 30 分の余裕
    assert row["id"] == headline_id("https://a.com/x")                        # 追跡用の引数は ID に入れない
    assert parse_artlist("", "oil").empty and parse_artlist("{}", "oil").empty


def test_gdelt_url_has_query_window_and_language():
    u = gdelt_url('(oil OR crude) "middle east"', "artlist", pd.Timestamp("2026-07-01", tz="UTC"),
                  pd.Timestamp("2026-07-01 06:00", tz="UTC"))
    assert "sourcelang%3Aenglish" in u and "startdatetime=20260701000000" in u and "enddatetime=20260701060000" in u
    assert "maxrecords=250" in u and "sort=DateDesc" in u and "%22middle%20east%22" in u
    assert "timelinesmooth=0" in gdelt_url("gold", "timelinetone", timespan="1d")


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def clock(self):
        return self.t


def test_gdelt_client_paces_and_backs_off_when_throttled():
    replies = [(429, b"Please limit requests to one every 5 seconds"), (200, b'{"articles": []}'),
               (200, b'{"articles": []}')]
    fc = FakeClock()
    c = GdeltClient(get=lambda url: replies.pop(0), sleep=fc.sleep, clock=fc.clock, log=lambda s: None)
    assert c.fetch("u1") == '{"articles": []}'
    assert c.fetch("u2") == '{"articles": []}'
    assert fc.sleeps[0] == pytest.approx(36.0)        # 断られたら 30 秒＋いつもの 6 秒
    assert fc.sleeps[1] == pytest.approx(6.0)         # 次の問い合わせまで 6 秒あける
    bad = GdeltClient(get=lambda url: (200, b"The specified phrase is too short."), sleep=fc.sleep, clock=fc.clock)
    with pytest.raises(QueryError):
        bad.fetch("u")
    never = GdeltClient(get=lambda url: (429, b"limit requests"), backoff=(1, 1), sleep=fc.sleep, clock=fc.clock,
                        log=lambda s: None)
    with pytest.raises(Throttled):
        never.fetch("u")
    assert never.requests == 3


def test_timeline_daily_share_uses_counts():
    data = {"timeline": [{"series": "Article Count", "data": [
        {"date": "20260101T000000Z", "value": 10, "norm": 1000}, {"date": "20260101T120000Z", "value": 30, "norm": 1000},
        {"date": "20260103T000000Z", "value": 5, "norm": 500}]}]}
    vol = parse_timeline(json.dumps(data))
    tone = parse_timeline(json.dumps({"timeline": [{"series": "Average Tone", "data": [
        {"date": "20260101T000000Z", "value": -2.0}, {"date": "20260101T120000Z", "value": -4.0}]}]}))
    d = daily_timeline(vol, tone)
    assert list(d.index.strftime("%m-%d")) == ["01-01", "01-02", "01-03"]       # 無い日も行を作る（NaN）
    assert d["share"].iloc[0] == pytest.approx(0.02) and np.isnan(d["share"].iloc[1])
    assert d["tone"].iloc[0] == pytest.approx(-3.0)
    assert parse_timeline("{}").empty


def test_store_dedups_by_url_and_title_across_recent_days(tmp_path):
    st = NewsStore(tmp_path)
    a = parse_artlist(_artlist([_art("https://a.com/1", 'Oil, "crude" jump\nagain', "20261009T100000Z"),
                                _art("https://b.com/1", "OIL crude JUMP again!", "20261009T110000Z"),
                                _art("https://c.com/2", "Gold steady", "20261009T233500Z")]), "oil")
    assert st.add(a) == 2                         # 同じ見出しの転載は 1 件。23:35 の分は翌日に入る
    assert [d.strftime("%m-%d") for d in st.days()] == ["10-09", "10-10"]
    assert st.headline_path(pd.Timestamp("2026-10-09", tz="UTC"), "gdelt").exists()
    assert st.counts(pd.Timestamp("2026-10-09", tz="UTC")) == {"gdelt": 1}
    # 別の取得先なら同じ見出しでも別に数える（取得先ごとに数え方をそろえるため）
    other = a.iloc[:1].assign(source="gkg")
    assert st.add(other) == 1 and st.days("gkg") == [pd.Timestamp("2026-10-09", tz="UTC")]
    assert len(st.load_day(pd.Timestamp("2026-10-09", tz="UTC"))) == 2
    assert len(st.load_day(pd.Timestamp("2026-10-09", tz="UTC"), "gdelt")) == 1
    assert st.load_day(pd.Timestamp("2026-10-09", tz="UTC"))["title"].iloc[0] == 'Oil, "crude" jump again'
    st2 = NewsStore(tmp_path)                     # 読み込み直しても重複しない
    again = parse_artlist(_artlist([_art("https://a.com/1", "Oil jumps (updated)", "20261010T050000Z"),
                                    _art("https://d.com/3", "New story", "20261010T050000Z")]), "oil")
    assert st2.add(again) == 1
    assert len(st2.load()) == 4


def test_parse_yahoo_news_uses_first_seen_time():
    raw = json.dumps({"news": [{"uuid": "x", "title": "Oil slips", "publisher": "Reuters",
                                "link": "https://finance.yahoo.com/a.html", "providerPublishTime": 1791538282}]})
    now = pd.Timestamp("2026-10-09 12:00", tz="UTC")
    df = parse_yahoo_news(raw.encode(), "CL=F", now)
    assert len(df) == 1 and df["available"].iloc[0] == now and df["published"].iloc[0] < now
    assert df["source"].iloc[0] == "yahoo" and df["query"].iloc[0] == "CL=F"


class FakeGdelt:
    def __init__(self, per_call):
        self.urls, self.per_call, self.requests = [], per_call, 0

    def fetch(self, url):
        self.urls.append(url)
        i = len(self.urls)
        start = url.split("startdatetime=")[1][:14]
        seen = f"{start[:8]}T{start[8:]}Z"
        return _artlist([_art(f"https://x.com/{i}-{k}", f"story {i} {k}", seen) for k in range(self.per_call)])


def test_collect_articles_walks_windows_and_records_progress(tmp_path):
    st = NewsStore(tmp_path)
    fake = FakeGdelt(per_call=3)
    s, e = pd.Timestamp("2026-10-01", tz="UTC"), pd.Timestamp("2026-10-01 12:00", tz="UTC")
    added, capped = collect_articles(fake, st, s, e, pd.Timedelta(hours=6), "until", log=lambda m: None)
    assert len(fake.urls) == 2 * 4 and added == 24 and capped == 0
    assert st.state()["until"] == "2026-10-01T12:00:00Z"
    full = FakeGdelt(per_call=MAX_RECORDS)
    assert collect_articles(full, st, s, s + pd.Timedelta(hours=6), pd.Timedelta(hours=6), "u",
                            log=lambda m: None)[1] == 4


def test_sample_like_backfill_keeps_latest_per_block():
    t0 = pd.Timestamp("2026-10-01", tz="UTC")
    n = MAX_RECORDS + 50
    pub = [t0 + pd.Timedelta(seconds=60 * i) for i in range(n)]     # 全部同じ 6 時間の中
    df = pd.DataFrame({"id": [str(i) for i in range(n)], "available": pub, "published": pub,
                       "source": "gdelt", "query": "oil", "title": "t"})
    df = pd.concat([df, df.assign(query="gold", id=df["id"] + "g"), df.head(5).assign(source="yahoo")])
    out = sample_like_backfill(df)
    assert len(out) == 2 * MAX_RECORDS and (out["source"] == "gdelt").all()
    assert out[out["query"] == "oil"]["published"].min() == pub[50]


def test_flags_and_values_map_news_days_to_next_decision():
    days = pd.date_range("2026-10-01", "2026-10-07", freq="D", tz="UTC")      # 木〜水
    # 判断: 米東部 16 時（夏時間は UTC 20 時）。木・金・月・火・水
    dates = pd.DatetimeIndex(["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"])
    m = MarketDaily.from_close("X", pd.Series(np.linspace(100, 104, 5), index=dates), "America/New_York")
    fl = np.zeros(len(days), bool)
    fl[2] = True                                    # 土曜の分 → 月曜の判断
    fl[4] = True                                    # 月曜の分 → 火曜の判断
    assert list(flags_at(days, fl, m.decide)) == [False, False, True, True, False]
    fl2 = np.zeros(len(days), bool)
    fl2[0] = True                                   # 木曜の分は木曜の引けには使えない → 金曜
    assert list(flags_at(days, fl2, m.decide)) == [False, True, False, False, False]
    v = values_at(days, np.arange(len(days), dtype=float), m.decide)
    assert np.isnan(v[0]) and list(v[1:]) == [0.0, 3.0, 4.0, 5.0]


def test_spikes_and_volatility_after_news():
    rng = np.random.default_rng(0)
    n = 400
    x = pd.Series(rng.normal(1.0, 0.1, n))
    x.iloc[200] = 3.0
    sp = spikes(x)
    assert sp.iloc[200] and sp.iloc[:20].sum() == 0 and sp.sum() < 15
    # 急増の翌日に大きく動く市場を作る → 比が 1 を大きく超える
    days = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    dates = pd.bdate_range("2024-01-01", periods=n)
    r = rng.normal(0, 0.01, n)
    fl = np.zeros(n, bool)
    fl[100:380:20] = True
    close = pd.Series(100 * np.exp(np.cumsum(r)), index=dates)
    m0 = MarketDaily.from_close("X", close, "America/New_York")
    hit = flags_at(days, fl, m0.decide)
    big = np.where(hit)[0] + 1                      # 判断の次の日の値動き
    r2 = r.copy()
    r2[big[big < n]] = 0.05
    m = MarketDaily.from_close("X", pd.Series(100 * np.exp(np.cumsum(r2)), index=dates), "America/New_York")
    assert vol_ratios(m, flags_at(days, fl, m.decide)).mean() > 3
    assert 0.6 < vol_ratios(m0, hit).mean() < 1.6
    pnl = hold_returns(m, np.ones(n), 5, (0.0, 0.0))
    assert len(pnl) > 300 and np.isfinite(pnl).all()


def test_hash_embedder_is_deterministic_and_topical():
    e = HashEmbedder()
    v = e.encode(["OPEC agrees to cut oil production", "OPEC agrees oil production cut deal", "Fed raises rates"])
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5)
    assert v[0] @ v[1] > v[0] @ v[2]
    assert np.allclose(e.encode(["OPEC agrees to cut oil production"]), v[:1])
    anchors = {"oil": e.encode(["OPEC cut oil production"]), "macro": e.encode(["Fed raises interest rates"])}
    sc = topic_scores(v, anchors)
    assert sc["oil"][0] > sc["macro"][0] and sc["macro"][2] > sc["oil"][2]


def _vec_days(n_days, surge_day, novel_day, dim=16, per_day=60, seed=0):
    """話題の方向 geo と原油の方向 oil を決め、ある日に geo の見出しを増やし、別の日に原油の見出しの中身を変える。"""
    rng = np.random.default_rng(seed)
    basis = np.linalg.qr(rng.normal(size=(dim, dim)))[0]
    geo, oil, oil2 = basis[0], basis[1], basis[2]
    days = []
    for d in range(n_days):
        k_geo = 30 if d == surge_day else 3
        rows = [rng.normal(0, 0.3, dim) + geo for _ in range(k_geo)]
        oil_dir = 0.85 * oil + 0.53 * oil2 if d == novel_day else oil     # 原油の話題のまま、中身が変わる
        rows += [rng.normal(0, 0.3, dim) + oil_dir for _ in range(15)]
        rows += [rng.normal(0, 1.0, dim) for _ in range(per_day - len(rows))]
        v = np.array(rows, dtype=np.float32)
        days.append((pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(days=d), v / np.linalg.norm(v, axis=1,
                                                                                                  keepdims=True)))
    anchors = {"geo": geo[None, :].astype(np.float32), "oil": oil[None, :].astype(np.float32),
               "gold": basis[3][None, :].astype(np.float32)}
    return days, anchors


def test_daily_features_detect_topic_surge_and_novelty():
    days, anchors = _vec_days(90, surge_day=70, novel_day=80)
    f = daily_features(days, anchors)
    assert len(f) == 90 and f["share_geo"].iloc[:20].isna().all()       # 履歴が 20 日に満たない間は出さない
    assert f["share_geo"].iloc[70] > 3 * f["share_geo"].iloc[40:70].mean()
    assert spikes(f["share_geo"]).iloc[70]
    nov = f["novelty_oil"]
    assert nov.iloc[80] > 3 * nov.iloc[50:80].mean()
    # 先読みしない: 後ろの日を変えても、前の日の特徴は変わらない
    f2 = daily_features(days[:75], anchors)
    pd.testing.assert_series_equal(f["share_geo"].iloc[:75], f2["share_geo"], check_freq=False)


def test_embedding_cache_adds_only_new_headlines(tmp_path):
    st = NewsStore(tmp_path)
    st.add(parse_artlist(_artlist([_art("https://a.com/1", "Oil jumps on OPEC cut", "20261009T100000Z")]), "oil"))
    day = pd.Timestamp("2026-10-09", tz="UTC")

    class Counting(HashEmbedder):
        calls = 0

        def encode(self, texts):
            Counting.calls += len(texts)
            return super().encode(texts)

    e = Counting()
    cache = EmbeddingCache(st, "hash")
    assert cache.update_day(day, e) == 1 and cache.update_day(day, e) == 0
    st.add(parse_artlist(_artlist([_art("https://b.com/2", "Gold steady", "20261009T120000Z")]), "gold"))
    assert cache.update_day(day, e) == 1 and Counting.calls == 2
    ids, vecs = cache.load(day)
    assert len(ids) == 2 and vecs.shape == (2, 512)
    got = load_day_vectors(st, "hash", pd.Timestamp("2026-10-10", tz="UTC"), source="gdelt")
    assert len(got) == 1 and got[0][1].shape == (2, 512)
    assert load_day_vectors(st, "hash", pd.Timestamp("2026-10-10", tz="UTC")) == []     # 特徴は生データの見出しだけ


def test_launchd_job_runs_collect_every_15_minutes():
    spec = importlib.util.spec_from_file_location("news_study", ROOT / "scripts" / "news_study.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pl = mod.make_plist(Path("/Users/me/bunkoko"), Path("/Users/me/bunkoko/data/news/collect.log"))
    assert pl["ProgramArguments"] == ["/Users/me/bunkoko/cfd", "news", "collect"]
    assert pl["StartInterval"] == 900 and pl["WorkingDirectory"] == "/Users/me/bunkoko"


def test_gate_test_blocks_entries_after_news_and_compares_with_shifted(monkeypatch):
    spec = importlib.util.spec_from_file_location("news_study_gate", ROOT / "scripts" / "news_study.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import cfdbot.study as study

    idx = pd.date_range("2023-01-02 22:00", periods=700, freq="D", tz="UTC")
    rng = np.random.default_rng(3)
    base_r = rng.normal(0.0003, 0.01, len(idx))
    news_days = pd.date_range("2023-01-01", periods=702, freq="D", tz="UTC")
    fl = np.zeros(len(news_days), bool)
    fl[30::25] = True
    loss = flags_at(news_days, fl, idx.as_unit("ns").asi8 + pd.Timedelta(days=1).value)   # 足の終わりが判断
    assert loss.sum() > 20
    base_r[loss] = -0.03                            # ニュースの翌日に入ると大きく負ける

    class FakePeriod:
        frames = {"GOLD": pd.DataFrame({"close": 1.0}, index=idx)}
        tfs = {"GOLD": pd.Timedelta(days=1)}
        start = idx[0]

        def run(self, picks, gates=None):
            r = base_r.copy()
            if gates:
                r[~gates["GOLD"]["long"].to_numpy()] = 0.0003
            eq = pd.Series(1e7 * np.cumprod(1 + r), index=idx)
            return {"equity": eq, "trades": pd.DataFrame(columns=["symbol", "r_multiple"]), "signals": {}}

    class FakeSetup:
        period_b = FakePeriod()
        picks = []

    monkeypatch.setattr(study, "build_setup", lambda *a, **k: FakeSetup())
    args = type("A", (), {"final": "", "config": "", "context": "", "gate_placebo": 10})()
    r = mod.gate_test(args, {"GOLD": (news_days, fl)}, lambda s: None)
    assert r["blocked"] == int(loss.sum()) and r["test"][0] > r["base"][0] + 0.5
    assert r["pct"] == 1.0 and mod.gate_verdict(r) == "有望"


def _gkg_zip(rows, extra=None):
    """GKG の 1 行 = 27 列（タブ区切り）。2 種類・3 サイト・4 URL・7 テーマ・9 場所・15 論調・26 見出しの入った XML。"""
    import io
    import zipfile

    lines = []
    for i, (kind, url, title) in enumerate(rows):
        cols = [""] * 27
        cols[2], cols[3], cols[4] = kind, url.split("/")[2], url
        if extra and i in extra:
            cols[7], cols[9], cols[15] = extra[i]
        cols[26] = f"<PAGE_LINKS>x</PAGE_LINKS><PAGE_TITLE>{title}</PAGE_TITLE>" if title is not None else ""
        lines.append("\t".join(cols))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("20261009120000.gkg.csv", "\n".join(lines))
    return buf.getvalue()


def test_parse_gkg_keeps_titles_with_topic_words():
    t = pd.Timestamp("2026-10-09 12:00", tz="UTC")
    raw = _gkg_zip([("1", "https://a.com/1", "Oil prices climb as OPEC+ trims output"),
                    ("1", "https://b.com/2", "Gold &amp; silver rally"),
                    ("1", "https://c.com/3", "Missiles strike near the border"),
                    ("1", "https://d.com/4", "Inflation cools in September"),
                    ("1", "https://e.com/5", "Local team wins the cup"),          # 対象の言葉が無い
                    ("1", "https://f.com/6", "Goldman upgrades bank stocks"),     # gold を含む別の単語は数えない
                    ("2", "https://g.com/7", "Oil spill reported"),               # ウェブの記事以外
                    ("1", "https://h.com/8", None)])                              # 見出しが無い
    df = parse_gkg(raw, t)
    assert list(df["query"]) == ["oil", "gold", "geo", "macro"]
    assert df["title"].iloc[1] == "Gold & silver rally"
    assert (df["published"] == t).all() and (df["available"] == t + pd.Timedelta(minutes=30)).all()
    assert (df["source"] == "gkg").all() and df["domain"].iloc[0] == "a.com"
    assert gkg_url(t).endswith("/gdeltv2/20261009120000.gkg.csv.zip")


def test_gkg_slots_every_three_hours():
    s = pd.Timestamp("2026-10-09 01:10", tz="UTC")
    sl = gkg_slots(s, pd.Timestamp("2026-10-09 09:00", tz="UTC"))
    assert [x.strftime("%H:%M") for x in sl] == ["03:00", "06:00", "09:00"]
    assert gkg_slots(pd.Timestamp("2026-10-09 09:01", tz="UTC"), pd.Timestamp("2026-10-09 10:00", tz="UTC")) == []


def test_collect_gkg_skips_missing_files_and_records_progress(tmp_path):
    st = NewsStore(tmp_path)
    calls = []

    def get(url):
        calls.append(url)
        if "20261009030000" in url:
            return 404, b""                                  # GDELT 側の欠け
        stamp = url.rsplit("/", 1)[1][:14]
        return 200, _gkg_zip([("1", f"https://a.com/{stamp}", f"Oil story {stamp}")])

    added, done, missing = collect_gkg(st, pd.Timestamp("2026-10-09", tz="UTC"),
                                       pd.Timestamp("2026-10-09 09:00", tz="UTC"), "gkg_until", get=get,
                                       sleep=lambda s: None, log=lambda s: None)
    assert (added, done, missing) == (3, 3, 1) and len(calls) == 4
    assert st.state()["gkg_until"] == "2026-10-09T09:00:00Z"
    assert st.counts(pd.Timestamp("2026-10-09", tz="UTC")) == {"gkg": 3}
    with pytest.raises(RuntimeError):
        collect_gkg(st, pd.Timestamp("2026-10-10", tz="UTC"), pd.Timestamp("2026-10-10 01:00", tz="UTC"), "k",
                    get=lambda u: (503, b""), sleep=lambda s: None, log=lambda s: None)


def test_gkg_tone_themes_and_file_counts_are_kept(tmp_path):
    t = pd.Timestamp("2026-10-09 12:00", tz="UTC")
    extra = {0: ("ECON_OILPRICE;ENV_OIL;ECON_OILPRICE;", "1#Iran#IR#IR#32#53#IR;4#Tehran, Iran#IR#IR07#35.7#51.4#-1",
                 "-3.5,1.2,4.7,5.9,20.1,0.8,412"),
             1: ("ARMEDCONFLICT;", "1#Israel#IS#IS#31.5#34.75#IS", "-6.0,0.5,6.5,7.0,25.0,1.0,250"),
             2: ("ECON_OILPRICE;SPORTS;", "", "2.0,3.0,1.0,4.0,10.0,0.0,300")}
    raw = _gkg_zip([("1", "https://a.com/1", "Oil jumps as Iran tensions rise"),
                    ("1", "https://b.com/2", "Missile attack reported"),
                    ("1", "https://c.com/3", "Local team wins the cup")], extra)
    g = parse_gkg_file(raw, t)
    assert len(g.headlines) == 2 and list(g.articles["id"]) == list(g.headlines["id"])
    a = g.articles.iloc[0]
    assert a["tone"] == -3.5 and a["words"] == 412 and a["themes"] == "ECON_OILPRICE;ENV_OIL" and a["countries"] == "IR"
    # ファイルの集計は話題を問わない全記事（見出しを取らなかった 3 件目も入る）
    assert g.stats["n_all"] == 3 and g.stats["tone_mean"] == pytest.approx((-3.5 - 6.0 + 2.0) / 3)
    assert g.stats["themes"].startswith("ECON_OILPRICE:2;") and "SPORTS:1" in g.stats["themes"]
    assert g.stats["countries"] == "IR:1;IS:1"

    st = NewsStore(tmp_path)
    assert st.add_gkg(g) == 2
    assert st.add_gkg(g) == 0                       # 同じファイルを 2 回読んでも記事は増えない
    arts = st.load_gkg_articles(pd.Timestamp("2026-10-09", tz="UTC"))
    assert len(arts) == 2 and arts["polarity"].iloc[1] == 7.0
    stats = st.load_gkg_stats()
    assert len(stats) == 1 and stats.index[0] == t and stats["n_all"].iloc[0] == 3
