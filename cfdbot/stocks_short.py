"""株の短期の売買（一晩〜5 日）: ルール・機械学習・ニュース・強化学習。docs/stocks_short.md。

判断は営業日 t の 13:30 まで（使えるのは t−1 日の引けまでと t 日の寄り）。買いは t 日の引け。
夜だけ持つ形は t+1 日の寄りで売り、5 日持つ形は t+5 日の引けで売る（資金を 5 つに分けて毎日 1/5 ずつ入れ替える）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .stocks import CLIP

PRIOR_TRIALS, NEW_TRIALS = 58, 6
COST = 0.0010
TOP_SHARE, MIN_VALID = 0.10, 50
NIGHT_WINDOW, REV_DAYS, HOLD = 20, 5, 5
FIRST_TEST_YEAR, EMBARGO = 2010, 6
GBM_PARAMS = dict(max_iter=200, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=2000, l2_regularization=1.0,
                  early_stopping=False, random_state=0)
RL_GAMMA, RL_TRAIN_END, N_DECILES = 0.99, "2018-01-01", 10
NEWS_CUTOFF_UTC_HOUR, NEWS_MIN_HEADLINES, NEWS_PCS, NEWS_RIDGE, NEWS_TOP = 4, 20, 10, 10.0, 2 / 3
NEWS_JUDGE_FROM = "2025-07-01"
WINDOWS = (5, 20, 60, 250)


# --------------------------------------------------------------------------- リターン
def parts(open_: pd.DataFrame, close: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """night[t] = 前日の引け → t 日の寄り、day[t] = t 日の寄り → 引け、cc[t] = 前日の引け → t 日の引け（±30% にそろえる）。"""
    return {"night": (open_ / close.shift(1) - 1).clip(-CLIP, CLIP), "day": (close / open_ - 1).clip(-CLIP, CLIP),
            "cc": close.pct_change(fill_method=None).clip(-CLIP, CLIP)}


# --------------------------------------------------------------------------- 選ぶ
def top_share(score: pd.DataFrame, tradable: pd.DataFrame, share: float = TOP_SHARE, count: int | None = None) -> np.ndarray:
    """日ごとに score の上位 share（count を渡せばその数）を選ぶ。その日に売買できる（tradable）銘柄の中から。

    値のある銘柄が MIN_VALID 未満の日は選ばない。
    """
    s = score.where(tradable).to_numpy(dtype=float)
    ok = np.isfinite(s)
    n = ok.sum(axis=1)
    k = np.where(n >= MIN_VALID, np.maximum(np.round(n * share), 1) if count is None else count, 0).astype(int)
    order = np.argsort(np.where(ok, -s, np.inf), axis=1, kind="stable")
    rank = np.empty_like(order)
    np.put_along_axis(rank, order, np.arange(s.shape[1])[None, :], axis=1)
    return (rank < k[:, None]) & ok


def random_like(sel: np.ndarray, tradable: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """日ごとに、sel と同じ数を、売買できる銘柄からでたらめに選ぶ。"""
    k = sel.sum(axis=1)
    r = np.where(tradable, rng.random(sel.shape, dtype=np.float32), np.inf)
    order = np.argsort(r, axis=1)
    rank = np.empty_like(order)
    np.put_along_axis(rank, order, np.arange(sel.shape[1])[None, :], axis=1)
    return (rank < k[:, None]) & tradable


# --------------------------------------------------------------------------- 損益
def night_pnl(night: pd.DataFrame, sel: np.ndarray, cost: float = 0.0) -> pd.DataFrame:
    """t 日に選んだ株を引けで買い、t+1 日の寄りで売る。index は判断の日 t。選んだ株が無い日は 0（売買しない）。"""
    r = night.shift(-1).to_numpy()
    s = sel & np.isfinite(r)
    n = s.sum(axis=1)
    gross = np.where(n > 0, (np.nan_to_num(r) * s).sum(axis=1) / np.maximum(n, 1), 0.0)
    return pd.DataFrame({"gross": gross, "d": np.where(n > 0, gross - cost, 0.0), "names": n}, index=night.index)


def cohort_weights(sel: np.ndarray, hold: int = HOLD) -> np.ndarray:
    """t 日の引けの後に持っている重み: 直近 hold 日の選択を、それぞれ等分して 1/hold ずつ。"""
    k = sel.sum(axis=1, keepdims=True)
    c = np.where(k > 0, sel / np.maximum(k, 1), 0.0)
    cs = np.cumsum(np.vstack([np.zeros((1, c.shape[1])), c]), axis=0)
    lo = np.maximum(np.arange(len(c)) + 1 - hold, 0)
    return (cs[1:] - cs[lo]) / hold


def weights_pnl(cc: pd.DataFrame, weights: np.ndarray, cost: float = 0.0) -> pd.DataFrame:
    """weights[t]（t 日の引けの後の重み）で t+1 日の値動きを受ける。index は値動きの日。

    gross = 持っている株の損益 − 持っている割合 × 全銘柄の平均、d = gross − 入れ替えた割合 × cost。
    """
    r = cc.to_numpy()
    mkt = market_mean(cc).to_numpy()
    w = np.vstack([np.zeros((1, weights.shape[1])), weights[:-1]])            # 前日の引けの後の重み
    gross = (w * np.nan_to_num(r)).sum(axis=1) - w.sum(axis=1) * mkt
    turn = 0.5 * np.abs(np.diff(w, axis=0, prepend=np.zeros((1, w.shape[1])))).sum(axis=1)
    return pd.DataFrame({"gross": gross, "d": gross - cost * turn, "turnover": turn, "invested": w.sum(axis=1)}, index=cc.index)


def market_mean(ret: pd.DataFrame) -> pd.Series:
    """全銘柄の平均（値の無い日は 0）。"""
    return ret.mean(axis=1).fillna(0.0)


def perf(daily: pd.Series) -> dict:
    """日ごとの損益（資金に対する割合）の年率・値動きの大きさ・シャープ・最大の下げ（足し算で数える）。"""
    d = daily.dropna()
    eq = d.cumsum()
    sd = d.std() * np.sqrt(252)
    return {"ann": float(d.mean() * 252), "vol": float(sd), "sharpe": float(d.mean() * 252 / sd) if sd > 0 else np.nan,
            "mdd": float((eq - eq.cummax()).min()) if len(eq) else np.nan}


# --------------------------------------------------------------------------- ルール（K1・K2）
def night_mean_score(night: pd.DataFrame, window: int = NIGHT_WINDOW) -> pd.DataFrame:
    """直近 window 営業日の夜のリターンの平均（当日の寄りまで）。"""
    return night.rolling(window, min_periods=window).mean()


def reversal_score(open_: pd.DataFrame, days: int = REV_DAYS) -> pd.DataFrame:
    """直近 days 営業日のリターン（当日の寄りまで）の符号を逆にしたもの（下がった株ほど大きい）。"""
    return -(open_ / open_.shift(days) - 1).clip(-1.0, 3.0)


# --------------------------------------------------------------------------- 機械学習（M1・M2）
def rank_pct(df: pd.DataFrame) -> pd.DataFrame:
    """その日の銘柄の中での順位（−0.5〜+0.5）。"""
    return df.rank(axis=1, pct=True) - 0.5


def features(open_: pd.DataFrame, close: pd.DataFrame) -> tuple[list[str], np.ndarray]:
    """入力 24 個（日 × 株 × 入力）。どれも t 日の 13:30 までに分かる（docs/stocks_short.md 5 章）。"""
    p = parts(open_, close)
    night, day, cc = p["night"], p["day"], p["cc"]
    prev_close = close.shift(1)
    stock: dict[str, pd.DataFrame] = {}
    for k in WINDOWS:
        stock[f"night_{k}"] = night.rolling(k, min_periods=k).mean()
        stock[f"day_{k}"] = day.shift(1).rolling(k, min_periods=k).mean()
        stock[f"ret_{k}"] = np.log(prev_close / prev_close.shift(k))
    stock["gap"] = night
    stock["day_prev"] = day.shift(1)
    stock["vol_20"] = cc.shift(1).rolling(20, min_periods=20).std()
    stock["vol_60"] = cc.shift(1).rolling(60, min_periods=60).std()
    stock["night_day_vol"] = night.rolling(60, min_periods=60).std() / day.shift(1).rolling(60, min_periods=60).std()
    stock["high_dist"] = prev_close / prev_close.rolling(250, min_periods=250).max() - 1
    m_cc = cc.mean(axis=1).shift(1)
    nxt = pd.Series(close.index, index=close.index).shift(-1)
    market = {"mkt_gap": night.mean(axis=1), "mkt_ret_5": m_cc.rolling(5).sum(), "mkt_ret_20": m_cc.rolling(20).sum(),
              "mkt_vol_20": m_cc.rolling(20).std(), "weekday": pd.Series(close.index.dayofweek, index=close.index, dtype=float),
              "days_to_next": (nxt - close.index.to_series()).dt.days.fillna(1.0)}
    names = list(stock) + list(market)
    x = np.empty((len(close), close.shape[1], len(names)), dtype=np.float32)
    for i, k in enumerate(stock):
        x[:, :, i] = rank_pct(stock[k]).to_numpy(dtype=np.float32)
    for i, k in enumerate(market):
        x[:, :, len(stock) + i] = market[k].to_numpy(dtype=np.float32)[:, None]
    return names, x


def targets(open_: pd.DataFrame, close: pd.DataFrame, hold: int = HOLD) -> tuple[pd.DataFrame, pd.DataFrame]:
    """答え（その日の銘柄の中での順位）: 今夜のリターン、これから hold 日の終値のリターン。"""
    night = parts(open_, close)["night"]
    return rank_pct(night.shift(-1)), rank_pct(np.log(close.shift(-hold) / close))


def walk_forward(x: np.ndarray, y: pd.DataFrame, tradable: np.ndarray, first_year: int = FIRST_TEST_YEAR,
                 embargo: int = 0, params: dict | None = None, log=print) -> pd.DataFrame:
    """毎年、その年より前（embargo 営業日ぶん手前まで）だけで学び、その年を予測する。予測の無い所は NaN。"""
    from sklearn.ensemble import HistGradientBoostingRegressor

    idx = y.index
    yv = y.to_numpy(dtype=np.float32)
    pred = np.full(yv.shape, np.nan, dtype=np.float32)
    for year in range(first_year, idx[-1].year + 1):
        test = np.flatnonzero(idx.year == year)
        if len(test) == 0:
            continue
        end = max(test[0] - embargo, 0)
        tr = tradable[:end] & np.isfinite(yv[:end])
        if tr.sum() < 10_000:
            continue
        model = HistGradientBoostingRegressor(**(params or GBM_PARAMS)).fit(x[:end][tr], yv[:end][tr])
        te = tradable[test]
        block = np.full(te.shape, np.nan, dtype=np.float32)
        block[te] = model.predict(x[test][te])
        pred[test] = block
        log(f"  {year}: 学習 {int(tr.sum()):,} 件 → 予測 {int(te.sum()):,} 件")
    return pd.DataFrame(pred, index=idx, columns=y.columns)


def yearly_ic(pred: pd.DataFrame, y: pd.DataFrame) -> pd.Series:
    """予測と実際の順位の相関（日ごとに計算して年ごとに平均）。"""
    daily = pred.corrwith(y, axis=1)
    return daily.groupby(daily.index.year).mean()


# --------------------------------------------------------------------------- 強化学習（Q1）
def deciles(pred: pd.DataFrame, n: int = N_DECILES) -> np.ndarray:
    """その日の予測の順位を n 段階に（n−1 が一番上）。予測の無い所は −1。"""
    r = pred.rank(axis=1, pct=True).to_numpy()
    d = np.clip(np.ceil(np.nan_to_num(r) * n - 1e-9) - 1, 0, n - 1).astype(int)       # 上位 1/n がちょうど n−1 になる
    return np.where(np.isfinite(r), d, -1)


def fit_policy(dec: np.ndarray, excess_next: np.ndarray, cost: float = COST, gamma: float = RL_GAMMA,
               n: int = N_DECILES) -> dict:
    """段階ごとの翌日の報酬の平均と段階の移り方を数え、価値反復で「持ち続ける段階」を決める。

    dec[t] は t 日の段階、excess_next[t] は t+1 日の「株 − 全銘柄の平均」。状態 = 持っている株の段階。
    持ち続ける: mu[d] + γ Σ P[d,d'] V(d')。入れ替える: −cost + 一番上の段階の株を持ったときの値。
    """
    now, nxt, rew = dec[:-1].ravel(), dec[1:].ravel(), excess_next[:-1].ravel()
    ok = (now >= 0) & (nxt >= 0) & np.isfinite(rew)
    mu = np.array([rew[ok & (now == d)].mean() if (ok & (now == d)).any() else 0.0 for d in range(n)])
    trans = np.zeros((n, n))
    np.add.at(trans, (now[ok], nxt[ok]), 1.0)
    trans = trans / np.maximum(trans.sum(axis=1, keepdims=True), 1.0)
    v = np.zeros(n)
    for _ in range(5000):
        keep = mu + gamma * trans @ v
        switch = -cost + keep[n - 1]
        new = np.maximum(keep, switch)
        if np.max(np.abs(new - v)) < 1e-12:
            break
        v = new
    keep = mu + gamma * trans @ v
    return {"mu": mu, "trans": trans, "keep": keep >= -cost + keep[n - 1], "value": v}


def policy_weights(pred: pd.DataFrame, dec: np.ndarray, keep: np.ndarray, slots: int, start: int = 0) -> np.ndarray:
    """枠 slots で、段階が keep でなくなった株を売り、空いた枠を予測の高い株で埋める。t 日の引けの後の重み。"""
    p = pred.to_numpy(dtype=float)
    w = np.zeros(p.shape)
    held: list[int] = []
    for t in range(start, len(p)):
        held = [j for j in held if dec[t, j] >= 0 and keep[dec[t, j]]]
        free = slots - len(held)
        if free > 0 and np.isfinite(p[t]).any():
            cand = np.argsort(np.where(np.isfinite(p[t]), -p[t], np.inf), kind="stable")
            have = set(held)
            for j in cand:
                if free == 0 or not np.isfinite(p[t, j]):
                    break
                if j not in have:
                    held.append(int(j))
                    free -= 1
        w[t, held] = 1.0 / slots
    return w


# --------------------------------------------------------------------------- ニュース（N1）
def news_bucket(available: pd.Series, cutoff_hour: int = NEWS_CUTOFF_UTC_HOUR) -> pd.Series:
    """見出しを使える日（日本の日付）に分ける: D−1 日 13:00（日本時間）より後 〜 D 日 13:00 までのものが D 日。"""
    t = pd.to_datetime(available, utc=True) - pd.Timedelta(hours=cutoff_hour, seconds=1)
    return (t.dt.floor("D") + pd.Timedelta(days=1)).dt.tz_localize(None)


def news_daily(buckets: np.ndarray, vecs: np.ndarray) -> tuple[pd.DatetimeIndex, np.ndarray, np.ndarray]:
    """日ごとの見出しのベクトルの平均と件数。"""
    days, inv = np.unique(buckets, return_inverse=True)
    total = np.zeros((len(days), vecs.shape[1]), dtype=np.float64)
    np.add.at(total, inv, vecs.astype(np.float64))
    n = np.bincount(inv, minlength=len(days))
    return pd.DatetimeIndex(days), total / n[:, None], n


def news_model(mean_vec: np.ndarray, count: np.ndarray, y: np.ndarray, train: np.ndarray,
               pcs: int = NEWS_PCS, alpha: float = NEWS_RIDGE, top: float = NEWS_TOP) -> tuple[np.ndarray, float]:
    """学習の日だけで主成分とリッジ回帰を作り、全部の日の予測と「持つ夜」の境目（学習の予測の上位 1 − top）を返す。"""
    from sklearn.decomposition import PCA
    from sklearn.linear_model import Ridge

    tr = train & np.isfinite(y)
    mu, sd = mean_vec[tr].mean(axis=0), mean_vec[tr].std(axis=0) + 1e-12
    z = (mean_vec - mu) / sd
    pca = PCA(n_components=pcs, random_state=0).fit(z[tr])
    f = np.column_stack([pca.transform(z), np.log(np.maximum(count, 1))])
    fm, fs = f[tr].mean(axis=0), f[tr].std(axis=0) + 1e-12
    f = (f - fm) / fs
    model = Ridge(alpha=alpha).fit(f[tr], y[tr])
    pred = model.predict(f)
    return pred, float(np.quantile(pred[tr], top))


def news_pnl(night_next: pd.Series, hold: np.ndarray, cost: float = 0.0) -> pd.DataFrame:
    """持つ夜だけ 1306 を引けで買い、翌朝の寄りで売る。index は判断の日。"""
    r = night_next.to_numpy()
    on = hold & np.isfinite(r)
    return pd.DataFrame({"gross": np.where(on, np.nan_to_num(r), 0.0), "d": np.where(on, np.nan_to_num(r) - cost, 0.0), "on": on},
                        index=night_next.index)


def news_placebo(night_next: pd.Series, hold: np.ndarray, n: int = 1000, seed: int = 11) -> np.ndarray:
    """持つ夜を、同じ数だけでたらめな日に置いたときの gross の平均。"""
    r = np.nan_to_num(night_next.to_numpy())
    ok = np.isfinite(night_next.to_numpy())
    k = int((hold & ok).sum())
    rng = np.random.default_rng(seed)
    pool = np.flatnonzero(ok)
    return np.array([r[rng.choice(pool, size=k, replace=False)].sum() / len(r) for _ in range(n)])
