"""束ねる検証の計算の部分（cfdbot/bundle.py）。docs/bundle.md 3・4 章。作ったデータだけ。期待する数字は手で出したもの。"""

import numpy as np
import pandas as pd
import pytest

from cfdbot import bundle as B


def months(n, start="2010-01-31"):
    return pd.date_range(start, periods=n, freq="ME")


def swing(n, size, start="2010-01-31"):
    """+size・−size を交互に並べた月の系列（標準偏差が size に比例するので、重さを手で出せる）。"""
    return pd.Series([size if k % 2 == 0 else -size for k in range(n)], index=months(n, start))


# --------------------------------------------------------------------------- 月ごとにそろえる（3 章の 1）
def test_to_monthly_sums_days_and_drops_the_unfinished_month():
    daily = pd.Series([0.01, 0.02, 0.03, 0.04, 0.05],
                      index=pd.to_datetime(["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-29", "2024-03-01"]))
    m = B.to_monthly(daily)
    assert list(m.index) == [pd.Timestamp("2024-01-31"), pd.Timestamp("2024-02-29")]     # 3 月は終わっていないので使わない
    assert m.tolist() == pytest.approx([0.03, 0.07])


def test_to_monthly_counts_a_month_without_trades_as_zero():
    daily = pd.Series([0.01, 0.02, 0.5], index=pd.to_datetime(["2024-01-10", "2024-03-10", "2024-05-02"]))
    m = B.to_monthly(daily)
    assert [f"{x:%Y-%m}" for x in m.index] == ["2024-01", "2024-02", "2024-03", "2024-04"]
    assert m.tolist() == pytest.approx([0.01, 0.0, 0.02, 0.0])


# --------------------------------------------------------------------------- 重さ（3 章の 2・3）
def test_weights_equalise_the_size_of_moves():
    s = {"x": swing(30, 0.01), "y": swing(30, 0.02)}            # y の値動きは x の 2 倍 → 重さは半分
    w = B.weights(s, {"X": ("x",), "Y": ("y",)})
    assert w.loc[months(30)[12], "x"] == pytest.approx(2 / 3) and w.loc[months(30)[12], "y"] == pytest.approx(1 / 3)
    assert w.loc[months(30)[29]].sum() == pytest.approx(1.0)
    assert w.iloc[:12].isna().all().all()                        # 前の月の値が 12 か月たまるまでは、重さが無い


def test_a_family_gets_one_share_however_many_members_it_has():
    s = {"a1": swing(30, 0.01), "a2": swing(30, 0.01), "b": swing(30, 0.01)}
    w = B.weights(s, {"A": ("a1", "a2"), "B": ("b",)})
    row = w.loc[months(30)[20]]
    assert row["a1"] == pytest.approx(0.25) and row["a2"] == pytest.approx(0.25) and row["b"] == pytest.approx(0.5)


def test_weights_are_shared_among_the_series_available_that_month():
    s = {"x": swing(50, 0.01), "y": swing(44, 0.02, start="2010-07-31")}      # y は 6 か月遅れて始まる
    w = B.weights(s, {"X": ("x",), "Y": ("y",)})
    idx = months(50)
    assert w.loc[idx[14], "x"] == pytest.approx(1.0) and np.isnan(w.loc[idx[14], "y"])   # y はまだ 12 か月たまっていない
    # 番号 45 の月: どちらも前の 36 か月（＋と−が 18 ずつ）で測るので、標準偏差の比はちょうど 1:2
    assert w.loc[idx[45], "x"] == pytest.approx(2 / 3) and w.loc[idx[45], "y"] == pytest.approx(1 / 3)


# --------------------------------------------------------------------------- 束（3 章の 4）
def test_bundle_needs_half_of_the_families_and_adds_weight_times_value():
    s = {"x": swing(60, 0.01), "y": swing(54, 0.02, start="2010-07-31"), "z": swing(48, 0.01, start="2011-01-31")}
    b = B.bundle(s, {"X": ("x",), "Y": ("y",), "Z": ("z",)})
    idx = months(60)
    # 3 家族の半分以上 = 2 家族。x は番号 12 から、y は 6 か月遅れ ＋ 12 か月 = 番号 18 から使える → 束は番号 18 の月から
    assert b.index[0] == idx[18]
    # 番号 18: x は前の 18 か月（＋9・−9）、y は前の 12 か月（＋6・−6）で測る。値は x +0.01（偶数番目）、y +0.02（y の 13 番目）
    sx, sy = 0.01 * (18 / 17) ** 0.5, 0.02 * (12 / 11) ** 0.5
    wx = (1 / sx) / (1 / sx + 1 / sy)
    assert b.loc[idx[18]] == pytest.approx(wx * 0.01 + (1 - wx) * 0.02)
    # 番号 50・51: 3 本とも前の 36 か月で測る → 重さは 0.4・0.2・0.4。番号 50 は 3 本ともプラスの番、51 はマイナスの番
    assert b.loc[idx[50]] == pytest.approx(0.4 * 0.01 + 0.2 * 0.02 + 0.4 * 0.01)
    assert b.loc[idx[51]] == pytest.approx(-(0.4 * 0.01 + 0.2 * 0.02 + 0.4 * 0.01))


def test_bundle_does_not_use_later_months():
    s = {"x": swing(30, 0.01), "y": swing(30, 0.02)}
    fam = {"X": ("x",), "Y": ("y",)}
    b1 = B.bundle(s, fam)
    s2 = {"x": s["x"].copy(), "y": s["y"].copy()}
    s2["x"].iloc[-1] = 9.0                                       # 最後の月だけ変える
    b2 = B.bundle(s2, fam)
    assert b1.iloc[:-1].equals(b2.iloc[:-1]) and b1.iloc[-1] != b2.iloc[-1]


# --------------------------------------------------------------------------- 束の数字（4.1）
def test_stats_by_hand():
    d = pd.Series([0.01, -0.01, 0.00, 0.03, 0.01, 0.02], index=months(6))
    st = B.stats(d)
    assert st["n"] == 6 and st["mean"] == pytest.approx(0.01)
    assert st["mean1"] == pytest.approx(0.0) and st["mean2"] == pytest.approx(0.02)
    assert st["t"] == pytest.approx(1.7320508) and st["t2"] == pytest.approx(3.4641016)
    assert st["vol"] == pytest.approx(0.0489898) and st["max_dd"] == pytest.approx(0.01)


# --------------------------------------------------------------------------- 物差し（4.3）: 効き目の無い世界で、良く見えたものだけを束ねる
def _slow_world(rng, n_months, n_tries):
    """物差しの 1 つの世界を、1 本ずつ素直に数える（道具とは別の書き方）。返すのは束の全期間の t 値。"""
    picked = []
    for _ in range(n_tries):
        x = rng.standard_normal(n_months)
        a, b = x[: n_months // 2], x[n_months // 2:]
        t = x.mean() / (x.std(ddof=1) / np.sqrt(n_months))
        if a.mean() > 0 and b.mean() > 0 and t >= 1.65:
            picked.append(x)
    if not picked:
        return 0.0
    port = np.mean(picked, axis=0)
    return float(port.mean() / (port.std(ddof=1) / np.sqrt(n_months)))


def test_yardstick_agrees_with_a_plain_count_of_the_same_worlds():
    y = B.yardstick(n_months=120, n_tries=40, n_worlds=3000, seed=5)
    rng = np.random.default_rng(99)                              # 別の乱数。分布の形が合うかを見る
    slow = np.array([_slow_world(rng, 120, 40) for _ in range(3000)])
    assert y["q50"] == pytest.approx(np.quantile(slow, 0.50), abs=0.15)
    assert y["q95"] == pytest.approx(np.quantile(slow, 0.95), abs=0.25)


def test_yardstick_of_a_single_try_is_mostly_zero():
    y = B.yardstick(n_months=120, n_tries=1, n_worlds=2000, seed=1)
    assert y["q50"] == 0.0 and y["q90"] == 0.0                   # 1 本だけなら、選ばれるのは 20 回に 1 回ほど


def test_yardstick_rises_with_the_number_of_tries():
    q = [B.yardstick(n_months=120, n_tries=n, n_worlds=2000, seed=3)["q95"] for n in (1, 20, 100)]
    assert q[0] < q[1] < q[2]


def test_yardstick_is_the_same_for_the_same_seed():
    assert B.yardstick(60, 30, 500, seed=7) == B.yardstick(60, 30, 500, seed=7)


# --------------------------------------------------------------------------- 判定（4.2）
@pytest.mark.parametrize("st, want", [
    ({"n": 120, "mean1": 0.01, "mean2": 0.01, "t": 7.0, "t2": 3.0}, "偶然を超えた"),
    ({"n": 120, "mean1": 0.01, "mean2": 0.01, "t": 6.0, "t2": 3.0}, "良く見えるが、選んだ分と区別できない"),     # 物差しに届かない
    ({"n": 120, "mean1": 0.01, "mean2": 0.01, "t": 7.0, "t2": 1.0}, "良く見えるが、選んだ分と区別できない"),     # 後半だけでは言えない
    ({"n": 120, "mean1": -0.01, "mean2": 0.02, "t": 7.0, "t2": 3.0}, "使えない"),                              # 前半がマイナス
    ({"n": 120, "mean1": 0.01, "mean2": 0.01, "t": 1.6, "t2": 1.0}, "使えない"),
    ({"n": 59, "mean1": 0.01, "mean2": 0.01, "t": 9.0, "t2": 5.0}, "数が足りない（判定しない）"),
])
def test_verdict(st, want):
    assert B.verdict(st, y95=6.3) == want


def test_verdict_never_passes_below_the_first_trial_line_even_if_the_yardstick_is_low():
    st = {"n": 120, "mean1": 0.01, "mean2": 0.01, "t": 1.5, "t2": 1.7}
    assert B.verdict(st, y95=1.0) == "使えない"


# --------------------------------------------------------------------------- 点検（4.1）
def test_correlations_between_families():
    x = swing(60, 0.01)
    s = {"x": x, "y": 2 * x, "z": -x}
    c = B.correlations(s, {"X": ("x",), "Y": ("y",), "Z": ("z",)})
    assert c.loc["X", "Y"] == pytest.approx(1.0) and c.loc["X", "Z"] == pytest.approx(-1.0) and c.loc["Y", "Y"] == pytest.approx(1.0)


def test_leave_one_out_gives_the_bundle_without_that_family():
    s = {"x": swing(60, 0.01) + 0.002, "y": swing(60, 0.02) + 0.004, "z": swing(60, 0.01) + 0.05}
    out = B.leave_one_out(s, {"X": ("x",), "Y": ("y",), "Z": ("z",)})
    # Z を抜くと x と y の束: 重さ 2/3・1/3、月は番号 12〜59 の 48 か月（＋と−が 24 ずつ）→ 平均は 2/3 × 0.002 + 1/3 × 0.004
    assert out.loc["Z", "n"] == 48 and out.loc["Z", "mean"] == pytest.approx(2 / 3 * 0.002 + 1 / 3 * 0.004)
    assert out.loc["X", "mean"] > out.loc["Z", "mean"]           # 儲けの大きい Z が残る束のほうが、平均は高い


def test_to_monthly_starts_at_the_first_trade():
    """まだ何も持っていない最初の期間（0 が続く日）は、系列に入れない（元の検証の数え方と同じ）。"""
    daily = pd.Series([0.0, 0.0, 0.0, 0.02, 0.0, 0.01, 0.5],
                      index=pd.to_datetime(["2024-01-10", "2024-01-20", "2024-02-05", "2024-03-07", "2024-04-11", "2024-05-13", "2024-06-03"]))
    m = B.to_monthly(daily)
    assert [f"{x:%Y-%m}" for x in m.index] == ["2024-03", "2024-04", "2024-05"]     # 1・2 月は始まる前。4 月は持っていて 0。6 月は終わっていない
    assert m.tolist() == pytest.approx([0.02, 0.0, 0.01])


def test_to_monthly_of_a_series_without_any_trade_is_empty():
    daily = pd.Series([0.0, 0.0], index=pd.to_datetime(["2024-01-10", "2024-02-10"]))
    assert B.to_monthly(daily).empty
