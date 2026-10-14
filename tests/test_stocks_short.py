import numpy as np
import pandas as pd

from cfdbot import stocks_short as H


def _market(n_days=700, n=80, seed=0, night_edge=0.002):
    """前半の 20 銘柄だけ夜に上がりやすい市場。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=n_days)
    night = rng.normal(0, 0.005, (n_days, n))
    night[:, :20] += night_edge
    day = rng.normal(0, 0.008, (n_days, n))
    close = 100 * np.exp(np.cumsum(night + day, axis=0))
    open_ = np.vstack([np.full((1, n), 100.0), close[:-1]]) * np.exp(night)
    cols = [f"S{i}" for i in range(n)]
    return pd.DataFrame(open_, index=idx, columns=cols), pd.DataFrame(close, index=idx, columns=cols)


def test_parts_and_top_share():
    o, c = _market()
    p = H.parts(o, c)
    assert abs(p["night"].iloc[5, 3] - (o.iloc[5, 3] / c.iloc[4, 3] - 1)) < 1e-12
    assert abs((1 + p["night"].iloc[5, 3]) * (1 + p["day"].iloc[5, 3]) - (1 + p["cc"].iloc[5, 3])) < 1e-12
    tradable = c.notna() & p["night"].notna()
    score = H.night_mean_score(p["night"])
    sel = H.top_share(score, tradable)
    assert not sel[:20].any() and (sel[25:].sum(axis=1) == 8).all()                   # 20 日たつまでは選ばない。80 の 10%
    assert sel[100:, :20].sum() / sel[100:].sum() > 0.6                               # 夜に強い 20 銘柄に寄る
    assert (H.top_share(score, tradable, count=3)[25:].sum(axis=1) == 3).all()
    few = tradable.copy()
    few.iloc[:, 40:] = False                                                          # 売買できる銘柄が 50 未満
    assert not H.top_share(score, few).any()
    rnd = H.random_like(sel, tradable.to_numpy(), np.random.default_rng(1))
    assert (rnd.sum(axis=1) == sel.sum(axis=1)).all() and not rnd[0].any() and (rnd != sel).any()


def test_night_pnl_uses_next_night_and_charges_cost():
    o, c = _market()
    p = H.parts(o, c)
    sel = np.zeros(c.shape, dtype=bool)
    sel[10, [1, 2]] = True
    pnl = H.night_pnl(p["night"], sel, cost=0.001)
    want = p["night"].iloc[11, [1, 2]].mean()
    assert abs(pnl["gross"].iloc[10] - want) < 1e-12 and abs(pnl["d"].iloc[10] - (want - 0.001)) < 1e-12
    assert pnl["d"].iloc[9] == 0 and pnl["names"].iloc[10] == 2 and pnl["d"].iloc[-1] == 0   # 選ばない日はコストもかからない
    tradable = c.notna() & p["night"].notna()
    full = H.night_pnl(p["night"], H.top_share(H.night_mean_score(p["night"]), tradable))
    assert full["gross"].iloc[30:].mean() > p["night"].mean().mean() + 0.0005        # 平均より高い夜を選べている


def test_cohort_weights_and_weights_pnl():
    sel = np.zeros((8, 4), dtype=bool)
    sel[0, 0] = sel[1, 1] = True
    w = H.cohort_weights(sel, hold=2)
    assert np.allclose(w[0], [0.5, 0, 0, 0]) and np.allclose(w[1], [0.5, 0.5, 0, 0]) and np.allclose(w[2], [0, 0.5, 0, 0])
    assert np.allclose(w[3:], 0)
    idx = pd.bdate_range("2020-01-01", periods=8)
    cc = pd.DataFrame(0.0, index=idx, columns=list("ABCD"))
    cc.iloc[2] = [0.04, 0.02, 0.0, 0.0]                                               # 3 日目: 平均 1.5%
    pnl = H.weights_pnl(cc, w, cost=0.01)
    assert abs(pnl["gross"].iloc[2] - (0.5 * 0.04 + 0.5 * 0.02 - 1.0 * 0.015)) < 1e-12   # 前日の引けの後の重みで受ける
    assert pnl["gross"].iloc[1] == 0 and abs(pnl["invested"].iloc[2] - 1.0) < 1e-12
    assert np.allclose(pnl["turnover"].iloc[1:5], [0.25, 0.25, 0.25, 0.25])           # 買い 0.5 → 片道ずつ
    assert abs(pnl["d"].iloc[2] - (pnl["gross"].iloc[2] - 0.0025)) < 1e-12
    perf = H.perf(pd.Series([0.01, -0.02, 0.01]))
    assert abs(perf["mdd"] + 0.02) < 1e-12 and abs(perf["ann"]) < 1e-9


def test_reversal_score_and_rank():
    o, _ = _market(n_days=30, n=60)
    s = H.reversal_score(o)
    assert s.iloc[:5].isna().all().all() and abs(s.iloc[10, 0] + (o.iloc[10, 0] / o.iloc[5, 0] - 1)) < 1e-12
    r = H.rank_pct(pd.DataFrame([[3.0, 1.0, 2.0, np.nan]]))
    assert np.allclose(r.iloc[0, :3], [0.5, -1 / 6, 1 / 6]) and np.isnan(r.iloc[0, 3])


def test_features_use_only_past_and_walk_forward_learns():
    o, c = _market(n_days=900, n=80, night_edge=0.004)
    names, x = H.features(o, c)
    assert len(names) == 24 and x.shape == (900, 80, 24) and names.index("gap") < 18
    o2, c2 = o.copy(), c.copy()
    c2.iloc[600:] *= 1.5                                                              # 600 日目の引けより後を変えても、
    o2.iloc[601:] *= 1.5
    _, x2 = H.features(o2, c2)
    assert np.allclose(x[:601], x2[:601], equal_nan=True)                             # 600 日目までの入力は変わらない
    y1, y5 = H.targets(o, c)
    p = H.parts(o, c)
    assert abs(y1.iloc[10].max() - 0.5) < 1e-12 and y1.iloc[-1].isna().all() and y5.iloc[-5:].isna().all().all()
    assert y1.iloc[10].idxmax() == p["night"].iloc[11].idxmax()
    tradable = (c.notna() & p["night"].notna()).to_numpy()
    pred = H.walk_forward(x, y1, tradable, first_year=2017, params=dict(H.GBM_PARAMS, max_iter=30, min_samples_leaf=200), log=lambda s: None)
    assert pred[: "2016"].isna().all().all() and pred["2017":].notna().iloc[5:].all().all()
    ic = H.yearly_ic(pred, y1).dropna()
    assert ic.index.min() == 2017 and ic.mean() > 0.05                                # 夜に強い銘柄を学べている
    sel = H.top_share(pred, pd.DataFrame(tradable, index=c.index, columns=c.columns))
    assert sel[-50:, :20].sum() / sel[-50:].sum() > 0.8


def test_deciles_policy_and_weights():
    idx = pd.bdate_range("2020-01-01", periods=400)
    rng = np.random.default_rng(3)
    n = 100
    base = np.tile(np.arange(n, dtype=float), (400, 1))                               # 予測の順位は株ごとにほぼ変わらない
    pred = pd.DataFrame(base + rng.normal(0, 3.0, (400, n)), index=idx)
    dec = H.deciles(pred)
    assert dec.min() == 0 and dec.max() == 9 and (np.bincount(dec[0]) == 10).all()
    pred_nan = pred.copy()
    pred_nan.iloc[0, 0] = np.nan
    assert H.deciles(pred_nan)[0, 0] == -1
    excess = np.repeat(((np.arange(n) // 10) - 4.5)[None, :] * 0.0002, 400, axis=0)   # 上の段階ほど翌日が良い
    pol = H.fit_policy(dec, excess, cost=0.001)
    assert pol["keep"][9] and not pol["keep"][0] and pol["mu"][9] > pol["mu"][0]
    assert list(pol["keep"]) == sorted(pol["keep"])                                   # ある段階から上だけ持ち続ける
    costly = H.fit_policy(dec, excess, cost=0.5)
    assert costly["keep"].all()                                                       # コストが大きければ入れ替えない
    w = H.policy_weights(pred, dec, pol["keep"], slots=10, start=5)
    assert not w[:5].any() and np.allclose(w[5:].sum(axis=1), 1.0) and (w[5] > 0).sum() == 10
    always = H.policy_weights(pred, dec, np.arange(10) == 9, slots=10, start=5)       # 一番上以外は必ず入れ替える
    turn = lambda a: np.abs(np.diff(a, axis=0)).sum()                                  # noqa: E731
    assert turn(w) < turn(always)


def test_news_bucket_model_and_pnl():
    t = pd.Series(["2025-01-06T04:00:00Z", "2025-01-06T04:00:01Z", "2025-01-05T05:30:00Z", "2025-01-06T23:30:00Z"])
    b = H.news_bucket(t)
    assert list(b) == [pd.Timestamp("2025-01-06"), pd.Timestamp("2025-01-07"), pd.Timestamp("2025-01-06"), pd.Timestamp("2025-01-07")]
    vecs = np.array([[1.0, 0.0], [3.0, 0.0], [0.0, 2.0], [5.0, 5.0]])
    days, mean, cnt = H.news_daily(b.to_numpy(), vecs)
    assert list(days) == [pd.Timestamp("2025-01-06"), pd.Timestamp("2025-01-07")] and list(cnt) == [2, 2]
    assert np.allclose(mean, [[0.5, 1.0], [4.0, 2.5]])

    rng = np.random.default_rng(5)
    n = 900
    v = rng.normal(0, 1, (n, 30))
    y = 0.004 * v[:, 0] + rng.normal(0, 0.004, n)                                     # 1 つ目の向きが翌朝のリターンを決める
    train = np.arange(n) < 600
    pred, thr = H.news_model(v, np.full(n, 100), y, train)
    assert np.corrcoef(pred[~train], y[~train])[0, 1] > 0.4 and 0.25 < np.mean(pred[train] > thr) < 0.40
    idx = pd.bdate_range("2022-01-03", periods=n)
    hold = (pred > thr) & ~train
    pnl = H.news_pnl(pd.Series(y, index=idx)[~train], hold[~train], cost=0.001)
    on = pnl["on"].to_numpy()
    assert pnl["gross"][on].mean() > 0.002 and (pnl["d"][~on] == 0).all()
    assert abs((pnl["gross"] - pnl["d"])[on].mean() - 0.001) < 1e-12
    null = H.news_placebo(pd.Series(y, index=idx)[~train], hold[~train], 300)
    assert np.mean(null < pnl["gross"].mean()) > 0.99 and abs(null.mean()) < 0.0005
