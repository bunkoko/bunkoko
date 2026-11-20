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


def test_yardstick_matches_a_plain_count_on_the_same_random_numbers():
    """登録の文（4.3 の物差し 1）のとおりに 1 本ずつ数え直して、同じ乱数の並びでぴったり合うか。"""
    for n_months, n_tries, n_worlds, seed in ((61, 30, 120, 3), (37, 7, 200, 9)):
        rng = np.random.default_rng(seed)
        ts, picked, passed = [], 0, 0
        for _ in range(n_worlds):
            x = rng.standard_normal((n_months, n_tries))
            chosen = []
            for j in range(n_tries):
                s = pd.Series(x[:, j])
                a, b = s.iloc[: n_months // 2], s.iloc[n_months // 2:]           # 奇数なら後半が 1 つ多い
                if a.mean() > 0 and b.mean() > 0 and s.mean() / (s.std(ddof=1) / np.sqrt(n_months)) >= 1.65:
                    chosen.append(x[:, j])
            picked += len(chosen)
            if not chosen:
                ts.append(0.0)
                continue
            port = pd.Series(np.mean(chosen, axis=0))
            t = port.mean() / (port.std(ddof=1) / np.sqrt(n_months))
            ts.append(t)
            a, b = port.iloc[: n_months // 2], port.iloc[n_months // 2:]
            if a.mean() > 0 and b.mean() > 0 and t >= 1.65 and b.mean() / (b.std(ddof=1) / np.sqrt(len(b))) >= 1.65:
                passed += 1
        y = B.yardstick(n_months, n_tries, n_worlds, seed)
        assert y["q50"] == pytest.approx(np.quantile(ts, 0.50), abs=1e-9) and y["q95"] == pytest.approx(np.quantile(ts, 0.95), abs=1e-9)
        assert y["q90"] == pytest.approx(np.quantile(ts, 0.90), abs=1e-9)
        assert y["pass_share"] == pytest.approx(passed / n_worlds) and y["picked_mean"] == pytest.approx(picked / n_worlds)


def test_the_registered_numbers_have_not_moved():
    """結果を見る前に決めた数（docs/bundle.md 4.3・2.2）。変えるときは、文書を先に直す。"""
    assert (B.N_TRIES, B.N_WORLDS, B.SEED) == (100, 20_000, 20261120)
    assert B.N_WORLDS_FAM == 5_000 and B.MIN_FAMILIES == 8
    assert B.LAST_MONTH == pd.Timestamp("2026-11-30")
    assert B.FAMILIES == {"A": ("EA",), "B": ("C2",), "C": ("carry", "combo"), "D": ("M2", "M2a", "M2b"), "E": ("E1", "E2"), "F": ("J7",),
                          "G": ("C3",), "H": ("C1", "S1"), "I": ("W7",), "J": ("1C",), "K": ("M1",)}
    assert B.SUBS == {"株の柱だけ": {f: B.FAMILIES[f] for f in "DEFGJK"}, "株以外の柱だけ": {f: B.FAMILIES[f] for f in "ABCI"},
                      "今の口座で持てる柱だけ": {"A": ("EA",), "B": ("C2",), "D": ("M2b",), "E": ("E1",), "F": ("J7",)}}
    assert (B.PRIOR_TRIALS, B.TRIAL) == (100, 101)


# --------------------------------------------------------------------------- 物差し 2（4.3）: 柱の数と期間を、本物の束にそろえた世界
FAM3 = {"X": ("x1", "x2"), "Y": ("y",), "Z": ("z",)}


def shaped(seed=0):
    """形だけ本物らしい系列: x1 は長い、x2 は遅れて始まる、y は持たない月（0）がある、z は短い。"""
    rng = np.random.default_rng(seed)
    x1 = pd.Series(0.02 * rng.standard_normal(150), index=months(150, "2005-01-31"))
    x2 = pd.Series(0.02 * rng.standard_normal(100), index=months(100, "2009-03-31"))
    y = pd.Series(0.03 * rng.standard_normal(120), index=months(120, "2007-07-31"))
    y.iloc[[5, 6, 7, 30, 31, 80]] = 0.0
    z = pd.Series(0.01 * rng.standard_normal(90), index=months(90, "2010-01-31"))
    return {"x1": x1, "x2": x2, "y": y, "z": z}


def composite(world, members):
    """家族の中の系列を、値のある月ごとに平均したもの。"""
    return pd.DataFrame({m: world[m] for m in members}).mean(axis=1)


def passes_first_line(d):
    a, b = d.iloc[: len(d) // 2], d.iloc[len(d) // 2:]
    return bool(a.mean() > 0 and b.mean() > 0 and d.mean() / (d.std(ddof=1) / np.sqrt(len(d))) >= 1.65)


def test_a_null_world_keeps_the_months_and_the_idle_months_of_the_real_series():
    real = shaped()
    pools = B.null_pools(real, FAM3, n_worlds=4, seed=1)
    for w in range(4):
        world = B.null_world(real, FAM3, pools, w)
        assert set(world) == set(real)
        for k, s in real.items():
            assert world[k].index.equals(s.index)
            assert ((world[k] == 0) == (s == 0)).all()            # 持たなかった月は 0 のまま、ほかの月は乱数
        both = world["x1"].index.intersection(world["x2"].index)
        assert world["x1"][both].equals(world["x2"][both])        # 家族の中の系列は同じ乱数
        assert not np.allclose(world["x1"][both].to_numpy(), real["x1"][both].to_numpy())      # 本物の値は使っていない


def test_every_family_of_a_null_world_passed_the_first_trial_line_on_its_own_period():
    real = shaped()
    pools = B.null_pools(real, FAM3, n_worlds=30, seed=2)
    for w in range(30):
        world = B.null_world(real, FAM3, pools, w)
        assert all(passes_first_line(composite(world, ms)) for ms in FAM3.values())


def test_null_worlds_differ_between_worlds_and_families_but_not_between_runs():
    real = shaped()
    a, b = B.null_pools(real, FAM3, 3, seed=5), B.null_pools(real, FAM3, 3, seed=5)
    w0, w0b, w1 = B.null_world(real, FAM3, a, 0), B.null_world(real, FAM3, b, 0), B.null_world(real, FAM3, a, 1)
    assert all(w0[k].equals(w0b[k]) for k in real)                # 同じ種なら同じ
    assert not w0["z"].equals(w1["z"])                            # 世界ごとに別の乱数
    twins = {"p": real["z"], "q": real["z"].copy()}               # 形のまったく同じ 2 家族でも、乱数は別
    w = B.null_world(twins, {"P": ("p",), "Q": ("q",)}, B.null_pools(twins, {"P": ("p",), "Q": ("q",)}, 1, seed=5), 0)
    assert abs(np.corrcoef(w["p"], w["q"])[0, 1]) < 0.5


def test_a_family_with_no_trade_at_all_cannot_make_a_null_world():
    real = shaped()
    real["z"] = real["z"] * 0.0
    with pytest.raises(B.base.NotReady, match="0"):
        B.null_pools(real, FAM3, 2, seed=1)


def _slow_families(series, families, n_worlds, seed, ref):
    """物差し 2 を、登録の文（4.3）のとおりに 1 本ずつ数える（道具とは別の書き方。同じ乱数の並び）。"""
    order = list(families)
    present = {f: [m for m in ms if m in series] for f, ms in families.items() if any(m in series for m in ms)}
    real = B.bundle(series, families)
    worlds = [{} for _ in range(n_worlds)]
    for f, ms in present.items():
        rng = np.random.default_rng([seed, order.index(f)])       # 家族ごとに別の並び
        idx = sorted(set().union(*[set(series[m].index) for m in ms]))
        got = 0
        while got < n_worlds:
            noise = pd.Series(rng.standard_normal(len(idx)), index=idx)
            members = {m: noise.reindex(series[m].index) * (series[m] != 0) for m in ms}
            if passes_first_line(composite(members, ms)):
                worlds[got].update(members)
                got += 1
    ts, first, by_ref = [], 0, 0
    for world in worlds:
        b = B.bundle(world, families).reindex(real.index).dropna()
        st = B.stats(b)
        ts.append(st["t"])
        both = st["mean1"] > 0 and st["mean2"] > 0 and st["t2"] >= 1.65
        first += bool(both and st["t"] >= 1.65)
        by_ref += bool(both and st["t"] >= ref)
    return {"q50": np.quantile(ts, 0.5), "q90": np.quantile(ts, 0.9), "q95": np.quantile(ts, 0.95),
            "share_first": first / n_worlds, "share_ref": by_ref / n_worlds}


def test_yardstick_of_families_matches_a_plain_count_on_the_same_random_numbers():
    real = shaped()
    got = B.yardstick_families(real, FAM3, n_worlds=60, seed=11, ref=2.5)
    want = _slow_families(real, FAM3, 60, 11, 2.5)
    for k, v in want.items():
        assert got[k] == pytest.approx(v, abs=1e-9), k
    assert got["n_worlds"] == 60


def test_yardstick_of_families_uses_only_the_months_of_the_real_bundle():
    """本物の束に無い月（本物の系列が動かず、値動きの大きさを測れない月）は、乱数の世界でも数えない。"""
    rng = np.random.default_rng(3)
    x = pd.Series(0.02 * rng.standard_normal(120), index=months(120))
    y = pd.Series(0.02 * rng.standard_normal(120), index=months(120))
    y.iloc[:48] = 0.01                                            # 最初の 4 年は同じ値が続く → 本物では、その間 y は使えない
    z = pd.Series(0.02 * rng.standard_normal(60), index=months(60, "2015-01-31"))
    s, fam = {"x": x, "y": y, "z": z}, {"X": ("x",), "Y": ("y",), "Z": ("z",)}
    assert B.bundle(s, fam).index[0] == months(120)[49]           # 3 家族の半分 = 2 家族がそろうのは、y が動き出した後
    got = B.yardstick_families(s, fam, n_worlds=40, seed=2, ref=2.5)
    want = _slow_families(s, fam, 40, 2, 2.5)
    assert got["q50"] == pytest.approx(want["q50"], abs=1e-9) and got["q95"] == pytest.approx(want["q95"], abs=1e-9)


def test_yardstick_of_families_only_counts_the_families_that_are_there():
    real = shaped()
    del real["z"]
    got = B.yardstick_families(real, FAM3, n_worlds=40, seed=11, ref=2.5)
    want = _slow_families(real, FAM3, 40, 11, 2.5)
    assert got["q95"] == pytest.approx(want["q95"], abs=1e-9)


def test_yardstick_of_one_family_is_the_line_it_had_to_pass():
    """1 家族だけなら、束 ＝ その系列。線（1.65）を通ったものだけなので、t 値の真ん中は 2 のあたり。"""
    s = {"x": pd.Series(0.01 * np.random.default_rng(0).standard_normal(240), index=months(240))}
    y = B.yardstick_families(s, {"X": ("x",)}, n_worlds=300, seed=4)
    assert 1.6 < y["q50"] < 2.2 and y["q95"] > y["q50"]


def test_yardstick_of_families_rises_with_the_number_of_families():
    rng = np.random.default_rng(1)
    s = {f"s{k}": pd.Series(0.01 * rng.standard_normal(150), index=months(150)) for k in range(6)}
    two = B.yardstick_families({k: s[k] for k in ("s0", "s1")}, {"A": ("s0",), "B": ("s1",)}, n_worlds=150, seed=4)
    six = B.yardstick_families(s, {chr(65 + k): (f"s{k}",) for k in range(6)}, n_worlds=150, seed=4)
    assert six["q50"] > two["q50"] + 1.0                          # 柱が多いほど、効き目が無くても t 値は高く出る


def test_yardstick_of_families_is_the_same_for_the_same_seed():
    real = shaped()
    assert B.yardstick_families(real, FAM3, 30, seed=7) == B.yardstick_families(real, FAM3, 30, seed=7)
    assert B.yardstick_families(real, FAM3, 30, seed=7) != B.yardstick_families(real, FAM3, 30, seed=8)


# --------------------------------------------------------------------------- 点検は、束と同じ月で数える（4.1）
def test_bundle_can_be_asked_for_fewer_families_than_half():
    s = {"x": swing(60, 0.01), "y": swing(54, 0.02, start="2010-07-31"), "z": swing(48, 0.01, start="2011-01-31")}
    fam = {"X": ("x",), "Y": ("y",), "Z": ("z",)}
    assert B.bundle(s, fam).index[0] == months(60)[18]            # ふつうは半分（2 家族）以上の月だけ
    one = B.bundle(s, fam, need=1)
    assert one.index[0] == months(60)[12] and one.loc[months(60)[12]] == pytest.approx(0.01)     # x しか使えない月は、x の値そのまま


def test_leave_one_out_is_counted_on_the_months_of_the_whole_bundle():
    """5 家族: 長い a・b と、4 年遅れて始まる c・d・e。束は 3 家族そろう月から。

    c を抜いた 4 家族をそのまま束ねると「半分」が 2 家族になり、a・b だけの古い月まで入ってしまう（抜いた効果と期間の違いが混ざる）。
    """
    s = {"a": swing(120, 0.01) + 0.001, "b": swing(120, 0.02) + 0.002}
    s.update({k: swing(72, 0.01, start="2014-01-31") + 0.001 for k in ("c", "d", "e")})
    fam = {k.upper(): (k,) for k in s}
    whole = B.bundle(s, fam)
    out = B.leave_one_out(s, fam)
    assert whole.index[0] == months(120)[60] and len(whole) == 60
    assert (out["n"] == 60).all()


def test_leave_one_out_keeps_every_month_of_the_bundle_even_when_half_would_not_be_reached():
    """10 家族で、ちょうど半分（5 家族）しか使えない月。1 つ抜くと 4／9 で「半分」に届かないが、もとの束の月なので数える。"""
    s = {f"l{k}": swing(100, 0.01) + 0.001 for k in range(5)}
    s.update({f"s{k}": swing(40, 0.01, start="2015-01-31") + 0.001 for k in range(5)})
    fam = {k.upper(): (k,) for k in s}
    whole = B.bundle(s, fam)
    assert whole.index[0] == months(100)[12]                      # 長い 5 家族だけで半分
    assert (B.leave_one_out(s, fam)["n"] == len(whole)).all()


# --------------------------------------------------------------------------- B2 のための控え（5 章）
def test_sigma_for_b2_is_measured_on_the_last_36_months_of_each_series():
    long = pd.concat([swing(20, 0.05), swing(36, 0.01, start="2011-09-30")])      # 古い 20 か月は荒い。最後の 36 か月は ±1%
    gap = swing(40, 0.02)
    gap = gap.drop(gap.index[[30, 31]])                           # 途中に無い月が 2 つ → 窓は暦の 36 か月（値は 34 個）
    short = swing(8, 0.01)
    sg = B.sigma_b2({"long": long, "gap": gap, "short": short})
    assert sg["long"] == pytest.approx(0.01 * (36 / 35) ** 0.5)
    assert sg["gap"] == pytest.approx(0.02 * (34 / 33) ** 0.5)
    assert np.isnan(sg["short"])                                  # 値のある月が 12 に届かない
