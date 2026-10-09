import numpy as np
import pandas as pd
import pytest

from cfdbot.leadlag import (DRIVER_BY_KEY, HYPOTHESES, TARGET_BY_KEY, DriverSeries, build_panel, close_ns,
                            driver_features, hypothesis_returns, read_yahoo, shifted_driver, target_frame)
from cfdbot.ml import FEATURES


def _series(n=900, seed=0, start="2015-01-01", tz_freq="B"):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq=tz_freq)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))), index=idx)


def test_hypotheses_refer_to_known_series():
    assert len(HYPOTHESES) == 41
    for d, t, sign, _ in HYPOTHESES:
        assert d in DRIVER_BY_KEY and t in TARGET_BY_KEY and sign in (-1, 1)


def test_tokyo_close_uses_previous_us_day_and_ny_close_too():
    oil = DriverSeries.from_series("oil", "price", _series(), "America/New_York")
    day = pd.DatetimeIndex(["2016-03-08"])                       # 火曜
    for tz in ("Asia/Tokyo", "America/New_York"):
        k = np.searchsorted(oil.avail, close_ns(day, tz), side="right") - 1
        assert _series().index[k[0]] == pd.Timestamp("2016-03-07")   # 月曜の原油まで（火曜の分は使わない）


def test_driver_changes_and_shift():
    s = pd.Series([100.0, 110.0, 99.0, 99.0], index=pd.date_range("2020-01-06", periods=4, freq="B"))
    d = DriverSeries.from_series("x", "price", s, "America/New_York")
    decide = d.avail + 1
    assert d.raw(1, decide)[1] == pytest.approx(0.10)
    y = DriverSeries.from_series("y", "yield", s / 50, "America/New_York")
    assert y.raw(1, y.avail + 1)[2] == pytest.approx(99 / 50 - 110 / 50)
    z = shifted_driver(d, 0.5)
    assert sorted(z.values) == sorted(d.values) and (z.avail == d.avail).all()


def test_beta_weighted_feature_tracks_a_linked_stock():
    rng = np.random.default_rng(3)
    n = 1200
    idx = pd.bdate_range("2012-01-02", periods=n)
    oil_r = rng.normal(0, 0.02, n)
    oil = pd.Series(80 * np.exp(np.cumsum(oil_r)), index=idx)
    stock = pd.Series(1000 * np.exp(np.cumsum(-0.5 * oil_r + rng.normal(0, 0.005, n))), index=idx)
    d = {"oil": DriverSeries.from_series("oil", "price", oil, "America/New_York")}
    f = target_frame(stock, "America/New_York", d)
    late = f.iloc[600:]
    # 原油が上がった後は「下がるはず」の向き（傾きがマイナス）になる
    assert np.corrcoef(late["bw_oil_5"].fillna(0), late["drv_oil_5"].fillna(0))[0, 1] < -0.9
    cols = list(FEATURES) + driver_features(["oil"])
    panel = build_panel({"S": f}, {"S": "us_stock"}, cols)
    assert panel.x.shape[1] == len(FEATURES) + 6 and np.isfinite(panel.x).all()


def test_hypothesis_returns_profit_from_a_planted_lag():
    rng = np.random.default_rng(4)
    n = 1500
    idx = pd.bdate_range("2010-01-04", periods=n)
    oil_r = rng.normal(0, 0.02, n)
    oil = pd.Series(80 * np.exp(np.cumsum(oil_r)), index=idx)
    # 原油の動きを、2〜6 日遅れて少しずつ逆向きに織り込む株（ゆっくり伝わる）
    slow = pd.Series(oil_r).rolling(5).sum().shift(2).fillna(0).to_numpy() / 5
    stock = pd.Series(1000 * np.exp(np.cumsum(-0.5 * slow + rng.normal(0, 0.005, n))), index=idx)
    d = DriverSeries.from_series("oil", "price", oil, "America/New_York")
    f = target_frame(stock, "America/New_York", {"oil": d})
    good = hypothesis_returns(f, d, -1, 5, (0.0005, 0.0))
    bad = hypothesis_returns(f, d, +1, 5, (0.0005, 0.0))
    sr = lambda r: r.mean() / r.std() * np.sqrt(252)  # noqa: E731
    assert sr(good) > 0.5 and sr(bad) < -0.5


def test_read_yahoo_drops_bad_rows(tmp_path):
    p = tmp_path / "x.csv"
    pd.DataFrame({"date": ["2020-01-02", "2020-01-03", "2020-01-03"], "close": [10.0, 0.0, 11.0],
                  "tz": "Asia/Tokyo"}).to_csv(p, index=False)
    s, tz = read_yahoo(p)
    assert list(s) == [10.0, 11.0] and tz == "Asia/Tokyo"
