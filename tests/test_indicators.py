import numpy as np
import pandas as pd
import pytest

from cfdbot import indicators as ind


def _df(n=300, seed=0):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.random(n)
    l = np.minimum(o, c) - rng.random(n)
    idx = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx)


def test_atr_constant_range():
    idx = pd.date_range("2026-01-01", periods=50, freq="4h", tz="UTC")
    df = pd.DataFrame({"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0}, index=idx)
    assert ind.atr(df, 14).iloc[-1] == pytest.approx(2.0)


def test_rsi_extremes():
    up = pd.Series(np.arange(1.0, 50.0))
    dn = pd.Series(np.arange(50.0, 1.0, -1))
    assert ind.rsi(up, 14).iloc[-1] == pytest.approx(100.0)
    assert ind.rsi(dn, 14).iloc[-1] == pytest.approx(0.0)
    assert ind.rsi(pd.Series([5.0] * 20), 14).iloc[-1] == pytest.approx(50.0)


def test_adx_range():
    a = ind.adx(_df(), 14)
    assert a.between(0, 100).all()


def test_donchian_excludes_current_bar():
    df = _df(50)
    upper, lower = ind.donchian(df, 10)
    assert upper.iloc[20] == df["high"].iloc[10:20].max()
    assert lower.iloc[20] == df["low"].iloc[10:20].min()


@pytest.mark.parametrize(
    "fn",
    [
        lambda d: ind.atr(d, 14),
        lambda d: ind.adx(d, 14),
        lambda d: ind.rsi(d["close"], 2),
        lambda d: ind.ema(d["close"], 50),
        lambda d: ind.efficiency_ratio(d["close"], 20),
        lambda d: ind.stdev(d["close"], 20),
    ],
)
def test_no_lookahead(fn):
    df = _df(300)
    full = fn(df)
    for k in (60, 150, 299):
        part = fn(df.iloc[:k])
        pd.testing.assert_series_equal(full.iloc[:k], part, check_names=False)
