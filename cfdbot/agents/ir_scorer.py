"""IR 評価 AI: 開示の題名の埋め込みから「その後 5 日、市場の中でどの順位になるか」の点数を作る。docs/agents.md。"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

CUTOFF_HOUR, PCS, RIDGE = 13, 32, 10.0
FIRST_TEST_YEAR, TRAIN_FROM, EMBARGO, HOLD = 2016, "2010-01-01", 6, 5
MIN_TRAIN = 2000


def decision_pos(times: pd.Series, index: pd.DatetimeIndex) -> np.ndarray:
    """開示の時刻 → 使う営業日の位置。13:00 までならその日、後なら翌日。その日以降で最初の営業日（無ければ len(index)）。"""
    t = pd.to_datetime(times)
    late = (t.dt.hour * 3600 + t.dt.minute * 60 + t.dt.second) > CUTOFF_HOUR * 3600
    day = t.dt.normalize() + pd.to_timedelta(late.astype(int), unit="D")
    return index.searchsorted(day.to_numpy())


def daily_ir(disc: pd.DataFrame, keys: pd.Series, titles: np.ndarray, vecs: np.ndarray, index: pd.DatetimeIndex,
             tickers: list[str]) -> tuple[pd.DataFrame, np.ndarray]:
    """株 × 日ごとに、開示の題名の埋め込みの平均と件数。返す表は pos（日の位置）・j（株の位置）・n・titles。"""
    col = {t: k for k, t in enumerate(tickers)}
    row = {t: k for k, t in enumerate(titles)}
    d = pd.DataFrame({"pos": decision_pos(disc["time"], index), "j": disc["ticker"].map(col), "v": keys.map(row),
                      "title": disc["title"].to_numpy()})
    d = d[(d["pos"] < len(index)) & d["j"].notna() & d["v"].notna()]
    if d.empty:
        return pd.DataFrame(columns=["pos", "j", "n", "titles"]), np.zeros((0, vecs.shape[1] if vecs.ndim == 2 else 0), np.float32)
    d = d.astype({"j": int, "v": int}).sort_values(["pos", "j"])
    code = d["pos"].to_numpy() * len(tickers) + d["j"].to_numpy()
    uniq, inv = np.unique(code, return_inverse=True)
    total = np.zeros((len(uniq), vecs.shape[1]), dtype=np.float64)
    np.add.at(total, inv, vecs[d["v"].to_numpy()].astype(np.float64))
    n = np.bincount(inv, minlength=len(uniq))
    text = d.groupby(code)["title"].agg(lambda s: " / ".join(list(s)[:3])).reindex(uniq).to_numpy()
    table = pd.DataFrame({"pos": uniq // len(tickers), "j": uniq % len(tickers), "n": n, "titles": text})
    return table, (total / n[:, None]).astype(np.float32)


def target_rank(close: pd.DataFrame, hold: int = HOLD) -> np.ndarray:
    """答え: 引けから hold 営業日後の引けまでのリターンの、その日の全銘柄の中での順位（−0.5〜+0.5）。"""
    return (np.log(close.shift(-hold) / close).rank(axis=1, pct=True) - 0.5).to_numpy()


def fit_predict(mat: np.ndarray, n: np.ndarray, y: np.ndarray, train: np.ndarray, pcs: int = PCS, alpha: float = RIDGE) -> np.ndarray:
    """学習の行だけで主成分とリッジ回帰を作り、全部の行の点数を返す。"""
    from sklearn.decomposition import PCA
    from sklearn.linear_model import Ridge

    mu, sd = mat[train].mean(axis=0), mat[train].std(axis=0) + 1e-12
    z = (mat - mu) / sd
    pca = PCA(n_components=min(pcs, z.shape[1], int(train.sum()) - 1), random_state=0).fit(z[train])
    f = np.column_stack([pca.transform(z), np.log(np.maximum(n, 1))])
    fm, fs = f[train].mean(axis=0), f[train].std(axis=0) + 1e-12
    f = (f - fm) / fs
    return Ridge(alpha=alpha).fit(f[train], y[train]).predict(f)


def walk_forward(table: pd.DataFrame, mat: np.ndarray, y: np.ndarray, index: pd.DatetimeIndex,
                 first_year: int = FIRST_TEST_YEAR, embargo: int = EMBARGO, log: Callable[[str], None] = print) -> np.ndarray:
    """毎年、その年より前（embargo 営業日ぶん手前まで）の開示だけで学び、その年の開示に点数を付ける。無い所は NaN。"""
    pos, j = table["pos"].to_numpy(), table["j"].to_numpy()
    yrow = y[pos, j]
    years = index.year.to_numpy()[pos]
    start = int(index.searchsorted(pd.Timestamp(TRAIN_FROM)))
    out = np.full(len(table), np.nan)
    for year in range(first_year, int(index[-1].year) + 1):
        test = years == year
        if not test.any():
            continue
        first = int(np.flatnonzero(index.year == year)[0])
        train = (pos >= start) & (pos < first - embargo) & np.isfinite(yrow)
        if train.sum() < MIN_TRAIN:
            continue
        out[test] = fit_predict(mat, table["n"].to_numpy(), yrow, train)[test]
        log(f"  {year}: 学習 {int(train.sum()):,} 件 → 点数 {int(test.sum()):,} 件")
    return out
