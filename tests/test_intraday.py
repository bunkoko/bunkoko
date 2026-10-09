import lzma
import struct
from datetime import date

import numpy as np
import pandas as pd
import pytest

from cfdbot.intraday import (breakeven_bps, donchian, evaluate, fetch_days, intraday_momentum, opening_range,
                             parse_candles, positions, reversion, rules, to_m5)


def _bi5(rows):
    raw = b"".join(struct.pack(">IIIIIf", *r) for r in rows)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def test_parse_candles_and_m5_mid_with_spread():
    day = date(2024, 1, 2)
    bid = parse_candles(_bi5([(0, 2000000, 2001000, 1999000, 2002000, 1.0), (60, 2001000, 2002000, 2000000, 2003000, 2.0),
                              (120, 2002000, 2002000, 2002000, 2002000, 0.0)]), day, 1000)
    assert len(bid) == 2                                           # 出来高 0 の分は捨てる
    assert bid.index[1] == pd.Timestamp("2024-01-02 00:01", tz="UTC")
    assert bid["close"].iloc[0] == 2001.0 and bid["high"].iloc[1] == 2003.0
    ask = bid + 0.3
    m5 = to_m5(bid, ask)
    assert len(m5) == 1
    assert m5["close"].iloc[0] == pytest.approx(2002.15) and m5["spread"].iloc[0] == pytest.approx(0.3)
    assert parse_candles(b"", day, 1000).empty


def test_fetch_days_skip_saturday_unless_weekend_market():
    d = fetch_days(date(2024, 1, 5), date(2024, 1, 8), weekend=False)
    assert [x.weekday() for x in d] == [4, 6, 0]
    assert len(fetch_days(date(2024, 1, 5), date(2024, 1, 8), weekend=True)) == 4


def _bars(closes, start="2024-01-02 13:00", freq="5min", tz="UTC"):
    idx = pd.date_range(start, periods=len(closes), freq=freq, tz=tz)
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "spread": 0.0}, index=idx)


def test_donchian_and_reversion_positions():
    up = list(np.full(10, 100.0)) + [101, 102, 103] + [99.0, 98.0]
    pos = donchian(_bars(up), 5, 2)
    assert pos[9] == 0 and pos[10] == 1 and pos[12] == 1
    assert pos[13] == -1                                            # 2 本の安値を割って手仕舞い、5 本の安値も割ったので売り
    z = list(np.full(30, 100.0) + np.tile([0.1, -0.1], 15)) + [95.0, 99.0, 100.2]
    pr = reversion(_bars(z))
    assert pr[30] == 1 and pr[32] == 0                              # 下に大きく離れたら買い、平均に戻ったら手仕舞い


def test_opening_range_breakout_in_new_york_time():
    # 9:30 ET（冬は 14:30 UTC）から 30 分の値幅、その後に上抜け
    closes = [100.0] * 6 + [100.5, 101.0, 101.0]
    df = _bars(closes, start="2024-01-02 14:30")
    pos = opening_range(df, "America/New_York", (9, 30), (16, 0), 30)
    assert (pos[:6] == 0).all() and pos[6] == 1 and pos[8] == 1


def test_intraday_momentum_holds_last_hour_only():
    idx = pd.date_range("2024-01-02 14:30", "2024-01-02 21:00", freq="5min", tz="UTC")   # 9:30〜16:00 ET
    c = np.linspace(100, 101, len(idx))
    df = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "spread": 0.0}, index=idx)
    pos = intraday_momentum(df)
    et = idx.tz_convert("America/New_York")
    held = pos != 0
    assert held.any() and et[held].min().strftime("%H:%M") == "14:55" and et[held].max().strftime("%H:%M") == "15:50"
    assert (pos[held] == 1).all()


def test_evaluate_costs_and_breakeven():
    n = 288 * 40
    idx = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
    rng = np.random.default_rng(0)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    df = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "spread": 0.02}, index=idx)
    pos = np.sign(np.r_[c[1:] / c[:-1] - 1, 0.0])                     # 先を知っている（必ず勝つ）向き
    free = evaluate(df, pos, np.zeros(n))
    paid = evaluate(df, pos, np.full(n, 0.02))
    assert free.daily_net.sum() == pytest.approx(free.daily_gross.sum())
    assert paid.daily_net.sum() < free.daily_net.sum()
    be = breakeven_bps(free)
    assert be > 0
    # 損益分岐のコストを払うと、損益がほぼ 0
    half = be / 2 / 1e4 * df["close"].to_numpy()
    at_be = evaluate(df, pos, 2 * half)
    assert at_be.daily_net.sum() == pytest.approx(0.0, abs=abs(free.gross_total) * 0.05)


def test_rules_are_fourteen_and_runnable():
    rl = rules()
    assert len(rl) == 14 and len({r.key for r in rl}) == 14
    df = _bars(100 + np.cumsum(np.random.default_rng(1).normal(0, 0.1, 288 * 12)), start="2024-01-01")
    for r in rl:
        if r.timeframe == "5min":
            p = positions(r, df)
            assert len(p) == len(df) and set(np.unique(p)) <= {-1.0, 0.0, 1.0}


def test_bid_only_days_get_spread_from_sampled_hours(tmp_path):
    from cfdbot.intraday import load_m5, m5_path

    day = date(2024, 1, 3)
    bid = parse_candles(_bi5([(0, 2000000, 2001000, 1999000, 2002000, 1.0)]), day, 1000)
    with_ask = to_m5(bid, bid + 0.4)
    no_ask = to_m5(bid.set_axis(bid.index + pd.Timedelta(days=1)))
    assert np.isnan(no_ask["spread"].iloc[0]) and no_ask["close"].iloc[0] == 2001.0
    path = m5_path(tmp_path, "GOLD", "2024-01")
    path.parent.mkdir(parents=True)
    pd.concat([with_ask, no_ask]).to_csv(path, compression="gzip")
    df = load_m5(tmp_path, "GOLD")
    assert df["spread"].to_list() == pytest.approx([0.4, 0.4])     # 同じ時刻の取れた日の値で埋める
