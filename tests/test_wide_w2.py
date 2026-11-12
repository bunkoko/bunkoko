"""W2（FOMC の声明の日）の点検・計算・公式のページとの見比べ。作ったデータだけで、通信しない。"""

from __future__ import annotations

import itertools
import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import numpy as np
import pandas as pd
import pytest

from cfdbot.wide import base
from cfdbot.wide import w2 as W

ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, days, close: pd.Series | None, other: dict | None = None) -> Path:
    """config/fomc_days.csv と data/stocks/misc_close.csv を作り、data フォルダを返す。"""
    data = tmp_path / "data"
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "fomc_days.csv").write_text(
        "# 説明の行\n# もう 1 行\ndate\n" + "".join(f"{d}\n" for d in days), encoding="utf-8")
    if close is not None:
        t = pd.DataFrame({"^GSPC": close, "SPY": close * 0.1})
        for k, v in (other or {}).items():                     # ^GSPC の無い日（日本だけの営業日など）
            t.loc[pd.Timestamp(k), "SPY"] = v
        (data / "stocks").mkdir(parents=True, exist_ok=True)
        t.sort_index().rename_axis("Date").to_csv(data / "stocks" / "misc_close.csv")
    return data


def _hand_prices() -> pd.Series:
    idx = pd.bdate_range("1998-10-01", "2000-05-15")
    c = pd.Series(100.0, index=idx)
    c[idx >= "2000-02-02"] = 101.0                          # 2000-02-02 に +1%
    c[idx >= "2000-03-21"] = 101.0 * 0.98                   # 2000-03-21 に −2%
    return c


HAND_DAYS = ["1999-12-15", "2000-02-02", "2000-03-21", "2000-04-15", "2000-05-10", "2000-06-28"]


# --------------------------------------------------------------------------- 手で計算できる小さな例
def test_hand_computed(tmp_path):
    data = _write(tmp_path, HAND_DAYS, _hand_prices(), other={"2000-04-15": 9.0})
    W.mark_checked(data, "テスト")
    s = W.compute(data)
    e1 = 0.01 - 0.0 - 0.003                                 # ふだんの日は 0（それまで値動きなし）
    e2 = -0.02 - 0.01 / 250 - 0.003                         # 250 営業日の中に 2000-02-02 の +1% が入る
    want = pd.Series([0.0, e1, e2, 0.0], index=pd.to_datetime(["2000-01-31", "2000-02-29", "2000-03-31", "2000-04-30"]))
    assert list(s.d.index) == list(want.index)
    np.testing.assert_allclose(s.d.to_numpy(), want.to_numpy(), atol=1e-12)
    assert s.key == "W2" and s.events == 2
    assert s.gross == pytest.approx((0.01 + (-0.02 - 0.01 / 250)) / 4, abs=1e-12)
    text = "\n".join(s.notes)
    assert "営業日でない日 1" in text and "2000-04-15" in text           # 土曜（値段の表には SPY だけある日）
    assert "終わっていない月の日 1" in text and "まだ来ていない" in text and "2000-01 より前の日 1" in text
    assert "手で公式のページと見比べた" in text


def test_two_events_in_a_month_are_summed(tmp_path):
    c = _hand_prices()
    c[c.index >= "2000-02-10"] *= 1.005                       # 2000-02-10 に +0.5%
    data = _write(tmp_path, ["2000-02-02", "2000-02-10"], c)
    W.mark_checked(data, "テスト")
    s = W.compute(data)
    e1 = 0.01 - 0.003
    e2 = 0.005 - 0.01 / 250 - 0.003
    assert s.d.loc["2000-02-29"] == pytest.approx(e1 + e2, abs=1e-12)
    assert s.events == 2 and (s.d.drop(pd.Timestamp("2000-02-29")) == 0).all()


def test_check_counts_without_returns(tmp_path):
    data = _write(tmp_path, HAND_DAYS, _hand_prices())
    W.mark_checked(data, "公式の日程")
    c = W.check(data)
    assert c.ok and c.key == "W2" and c.why == ""
    text = "\n".join(c.lines)
    assert "6 日（1999-12-15〜2000-06-28）" in text
    assert "営業日 4・営業日でない 1・値段の最後の日より後 1" in text and "2000-04-15" in text
    assert "使う事象 2" in text and "2000-01〜2000-04" in text
    assert "公式の日程" in text
    assert "%" not in text and "0.007" not in text and "0.01" not in text


# --------------------------------------------------------------------------- 先読みをしない
def _walk(seed: int = 3) -> pd.Series:
    idx = pd.bdate_range("1998-06-01", "2001-06-29")
    r = np.random.default_rng(seed).normal(0.0003, 0.012, len(idx))
    return pd.Series(1000 * np.exp(np.cumsum(r)), index=idx)


def _monthly_days(close: pd.Series) -> list[str]:
    s = close[close.index >= "2000-01-01"]
    return [f"{g.index[min(10, len(g) - 1)]:%Y-%m-%d}" for _, g in s.groupby(s.index.to_period("M"))]


def test_no_look_ahead(tmp_path):
    close = _walk()
    days = _monthly_days(close)
    x = pd.Timestamp(days[8])                                 # 2000-09 の声明の日
    base_d = W.compute(_mk(tmp_path / "a", days, close)).d
    later = close.copy()
    later[later.index > x] *= np.random.default_rng(9).uniform(0.5, 1.5, int((later.index > x).sum()))
    d2 = W.compute(_mk(tmp_path / "b", days, later)).d
    d3 = W.compute(_mk(tmp_path / "c", days, close[close.index <= x + pd.Timedelta(days=40)])).d
    keep = base_d.index <= base.month_end(x)
    pd.testing.assert_series_equal(base_d[keep], d2[keep])
    pd.testing.assert_series_equal(base_d[keep], d3[d3.index <= base.month_end(x)])
    assert not np.allclose(base_d[~keep], d2[~keep])          # 後ろは変わる（入れ替えが効いている）


def _mk(folder: Path, days, close) -> Path:
    data = _write(folder, days, close)
    W.mark_checked(data, "テスト")
    return data


# --------------------------------------------------------------------------- データが無い・形が違う
@pytest.mark.parametrize("case", ["no_days", "no_prices", "no_date_col", "bad_date", "dup", "no_ticker",
                                  "late_start", "zero", "text_price", "empty"])
def test_not_ready(tmp_path, case):
    c = _hand_prices()
    days = ["2000-02-02", "2000-03-21"]
    data = _write(tmp_path, days, None if case == "no_prices" else c)
    cfg = tmp_path / "config" / "fomc_days.csv"
    px = data / "stocks" / "misc_close.csv"
    if case == "no_days":
        cfg.unlink()
    elif case == "no_date_col":
        cfg.write_text("day\n2000-02-02\n", encoding="utf-8")
    elif case == "bad_date":
        cfg.write_text("date\n2000-02-02\n2000-13-01\n", encoding="utf-8")
    elif case == "dup":
        cfg.write_text("date\n2000-02-02\n2000-02-02\n", encoding="utf-8")
    elif case == "empty":
        cfg.write_text("# 説明だけ\ndate\n", encoding="utf-8")
    elif case == "no_ticker":
        pd.DataFrame({"SPY": c}).rename_axis("Date").to_csv(px)
    elif case == "late_start":
        _write(tmp_path, days, c[c.index >= "1999-03-01"])
    elif case == "zero":
        z = c.copy()
        z.iloc[5] = 0.0
        _write(tmp_path, days, z)
    elif case == "text_price":
        t = pd.DataFrame({"^GSPC": c.astype(object)})
        t.iloc[5, 0] = "abc"
        t.rename_axis("Date").to_csv(px)
    W.mark_checked(data, "テスト")
    with pytest.raises(base.NotReady):
        W.compute(data)
    chk = W.check(data)
    assert not chk.ok and chk.why


def test_not_verified_blocks(tmp_path):
    c = _walk()
    days = _monthly_days(c)[:15]
    data = _write(tmp_path, days, c)
    chk = W.check(data)                                      # 公式のページがまだ無い
    assert not chk.ok and "--fetch" in chk.why
    pages = data / "wide" / "fomc"
    pages.mkdir(parents=True)
    (pages / "fomchistorical2000.htm").write_text("<html>nothing</html>", encoding="utf-8")
    chk = W.check(data)
    assert not chk.ok and "見つからない日 15" in chk.why and "ほか 5" in chk.why
    assert days[0] in chk.why and days[9] in chk.why and days[10] not in chk.why
    assert "fomchistorical2001.htm" in chk.why                 # 2001 年のページは保存していない
    assert any("営業日" in x for x in chk.lines)               # 値段の数は出す
    with pytest.raises(base.NotReady):
        W.compute(data)
    W.mark_checked(data, "公式の日程と年ごとのページ")
    assert W.check(data).ok
    assert W.compute(data).events == 15


def test_mark_checked_needs_note(tmp_path):
    with pytest.raises(ValueError):
        W.mark_checked(tmp_path / "data", "  ")
    p = W.mark_checked(tmp_path / "data", "公式のページ")
    rec = json.loads(p.read_text(encoding="utf-8"))
    assert p == tmp_path / "data" / "wide" / "fomc_checked.json" and set(rec) == {"date", "note"} and rec["note"] == "公式のページ"


# --------------------------------------------------------------------------- 公式のページとの見比べ
def test_verify_dates(tmp_path):
    data = _write(tmp_path, ["2006-01-31", "2006-03-28", "2020-11-05", "2021-01-27", "2024-11-07"], None)
    pages = data / "wide" / "fomc"
    pages.mkdir(parents=True)
    (pages / "fomchistorical2006.htm").write_text('<a href="/newsevents/press/monetary20060131a.htm">', encoding="utf-8")
    (pages / "fomccalendars.htm").write_text("fomcminutes20210127.htm monetary20201105a.htm", encoding="utf-8")
    v = W.verify_dates(data)
    assert v["found"] == {"2006-01-31": True, "2006-03-28": False, "2020-11-05": True, "2021-01-27": True, "2024-11-07": False}
    assert v["missing"] == ["2006-03-28", "2024-11-07"] and not v["ok"]
    assert v["pages_missing"] == ["fomchistorical2020.htm"]    # 2020 年は日程のページで見つかったが、年のページは無い
    (pages / "fomchistorical2006.htm").write_text("20060131 20060328", encoding="utf-8")
    (pages / "fomccalendars.htm").write_text("20201105 20210127 20241107", encoding="utf-8")
    v = W.verify_dates(data)
    assert v["ok"] and v["missing"] == []
    _write(tmp_path, ["2006-01-31", "2006-03-28", "2020-11-05", "2021-01-27", "2024-11-07"], _hand_prices())
    c = W.check(data)                                         # 自動で全部あれば、印が無くても通る
    assert "すべてあった" in "\n".join(c.lines) and "見比べ" in "\n".join(c.lines)


def _stamp(path: Path, day: str) -> None:
    ts = datetime.fromisoformat(day + "T12:00").timestamp()       # その日の昼（この機械の時刻）に保存した、とする
    os.utime(path, (ts, ts))


def test_meetings_after_the_calendar_page(tmp_path):
    idx = pd.bdate_range("1998-10-01", "2026-11-10")
    close = pd.Series(np.linspace(100, 300, len(idx)), index=idx)
    data = _write(tmp_path, ["2006-01-31", "2026-10-28", "2026-12-09"], close)
    pages = data / "wide" / "fomc"
    pages.mkdir(parents=True)
    (pages / "fomchistorical2006.htm").write_text("monetary20060131a.htm", encoding="utf-8")
    (pages / "fomccalendars.htm").write_text("monetary20261028a.htm December 8-9", encoding="utf-8")
    _stamp(pages / "fomccalendars.htm", "2026-11-12")
    v = W.verify_dates(data)                                  # 12-09 はまだ声明が無い → 見つからない日に数えない
    assert v["ok"] and v["missing"] == [] and v["later"] == ["2026-12-09"] and v["found"]["2026-12-09"] is False
    c = W.check(data)
    assert c.ok and "2 日すべてあった" in "\n".join(c.lines)
    assert W.compute(data).events == 2
    (pages / "fomccalendars.htm").write_text("old page", encoding="utf-8")
    _stamp(pages / "fomccalendars.htm", "2026-10-01")             # 10-28 の会合より前に取った古い日程のページ
    v = W.verify_dates(data)
    assert v["ok"] and v["later"] == ["2026-10-28", "2026-12-09"]
    c = W.check(data)                                         # 10-28 は計算に入るのに、公式で確かめていない
    assert not c.ok and "計算に入る日 1: 2026-10-28" in c.why and "--fetch" in c.why
    with pytest.raises(base.NotReady):
        W.compute(data)
    W.mark_checked(data, "公式の日程")
    assert W.check(data).ok and W.compute(data).events == 2


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s


def test_fetch_saves_pages_politely(tmp_path, monkeypatch):
    data = _write(tmp_path, ["2006-01-31", "2024-11-07"], None)
    clock, calls = _Clock(), []

    def fake_get(url):
        calls.append((clock.now, url))
        clock.now += 0.2
        if url.endswith("fomchistorical2020.htm"):
            raise HTTPError(url, 404, "Not Found", None, None)
        if url.endswith("fomchistorical2003.htm"):
            raise URLError("down")
        return f"<html>{url} 20060131 20241107</html>".encode()

    monkeypatch.setattr(W, "time", clock)
    monkeypatch.setattr(W, "_get", fake_get)
    logs: list[str] = []
    W.fetch(data, log=logs.append)
    pages = data / "wide" / "fomc"
    assert len(calls) == 22 and calls[0][1] == W.FED + "fomchistorical2000.htm" and calls[-1][1] == W.FED + "fomccalendars.htm"
    starts = [t for t, _ in calls]
    assert all(b - a >= W.INTERVAL - 1e-9 for a, b in itertools.pairwise(starts))   # 1 秒に 1 件
    rec = json.loads((pages / "fetch.json").read_text(encoding="utf-8"))
    assert rec["failed"] == {"fomchistorical2020.htm": "HTTP 404", "fomchistorical2003.htm": "URLError"}
    assert len(rec["saved"]) == 20 and not (pages / "fomchistorical2020.htm").exists()
    assert (pages / "fomchistorical2006.htm").exists() and (pages / "fomccalendars.htm").exists()
    assert any("fomchistorical2020.htm" in x and "404" in x for x in logs) and any("あった 2" in x for x in logs)
    calls.clear()
    W.fetch(data, log=logs.append)                              # 保存済みの年のページは取り直さない
    assert [u for _, u in calls] == [W.FED + "fomchistorical2003.htm", W.FED + "fomchistorical2020.htm", W.FED + "fomccalendars.htm"]
    assert W.verify_dates(data)["ok"]


def test_fetch_waits_a_second_after_a_retry(tmp_path, monkeypatch):
    """_get の中で取り直した後も、次のページまで 1 秒あける（取り直しの直後にすぐ次を取らない）。"""
    data = _write(tmp_path, ["2006-01-31"], None)
    clock, starts, failed = _Clock(), [], set()

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"20060131"

    def fake_urlopen(req, timeout):
        starts.append((clock.now, req.full_url))
        clock.now += 0.05                                      # すぐ返ってくる
        if req.full_url.endswith("fomchistorical2005.htm") and "2005" not in failed:
            failed.add("2005")
            raise HTTPError(req.full_url, 503, "Busy", None, None)   # 1 回目だけ 503 → 5 秒待って取り直す
        return _Resp()

    monkeypatch.setattr(W, "time", clock)
    monkeypatch.setattr(W, "urlopen", fake_urlopen)
    W.fetch(data, log=lambda *_: None)
    assert len(starts) == 23 and sum(u.endswith("fomchistorical2005.htm") for _, u in starts) == 2
    gaps = [b - a for (a, _), (b, _) in itertools.pairwise(starts)]
    assert min(gaps) >= W.INTERVAL - 1e-9                      # 前は取り直しの 0.05 秒後に次を取っていた
    assert (data / "wide" / "fomc" / "fomchistorical2005.htm").exists()


def test_tz_aware_price_dates(tmp_path):
    """値段の日付に時差（冬 -05:00・夏 -04:00）が付いていても落ちず、時差の無い表と同じ d になる。"""
    c = _hand_prices()
    plain = _mk(tmp_path / "a", HAND_DAYS, c)
    data = _write(tmp_path / "b", HAND_DAYS, None)
    (data / "stocks").mkdir(parents=True)
    t = pd.DataFrame({"^GSPC": c.to_numpy()}, index=c.index.tz_localize("America/New_York"))
    t.rename_axis("Date").to_csv(data / "stocks" / "misc_close.csv")
    assert "-04:00" in (data / "stocks" / "misc_close.csv").read_text() and "-05:00" in (data / "stocks" / "misc_close.csv").read_text()
    W.mark_checked(data, "テスト")
    pd.testing.assert_series_equal(W.compute(data).d, W.compute(plain).d)
    assert W.check(data).ok


def test_get_retries_only_network_errors(monkeypatch):
    seen, sleeps = [], []
    monkeypatch.setattr(W, "time", SimpleNamespace(sleep=sleeps.append))

    def not_found(req, timeout):
        seen.append(req.full_url)
        raise HTTPError(req.full_url, 404, "Not Found", None, None)

    monkeypatch.setattr(W, "urlopen", not_found)
    with pytest.raises(HTTPError):
        W._get("https://example.invalid/x")
    assert len(seen) == 1 and sleeps == []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"ok"

    tries = iter([URLError("a"), URLError("b"), None])

    def flaky(req, timeout):
        e = next(tries)
        if e:
            raise e
        return _Resp()

    monkeypatch.setattr(W, "urlopen", flaky)
    assert W._get("https://example.invalid/y") == b"ok" and sleeps == [5, 10]


def test_real_table_reads():
    days = W.read_days(ROOT / "data")                         # リポジトリの config/fomc_days.csv
    assert len(days) >= 200 and days.is_unique and days.is_monotonic_increasing
    assert days[0] >= pd.Timestamp("2000-01-01") and (days.day_of_week < 5).all()
