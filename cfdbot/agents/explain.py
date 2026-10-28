"""学習の部品の中身を見る（SHAP 値）。docs/agents.md 14 章。

M2（銘柄選定。値動きの 24 個）と、決算の数字で学んだ IR 評価 AI（E2。6 個）について、年ごとの部品（その年より前だけで学んだもの。
台帳に書く点数と同じ切り方）が、その年に点数を付けた行で何を使っているかを見る。見るためだけで、入力や学び方の選び直しには使わない。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .. import stocks_short as H
from . import earnings as N

SAMPLE, YEARS, PAIR_MIN = 3000, 5, 0.7     # 1 年あたりの行・何年分・「よく似た入力」の順位相関の線（14 章）

M2_LABELS = {
    "night_5": "夜（前日の引け→寄り）の平均・5 日", "night_20": "夜の平均・20 日", "night_60": "夜の平均・60 日", "night_250": "夜の平均・250 日",
    "day_5": "昼（寄り→引け）の平均・5 日", "day_20": "昼の平均・20 日", "day_60": "昼の平均・60 日", "day_250": "昼の平均・250 日",
    "ret_5": "値動き・5 日", "ret_20": "値動き・20 日", "ret_60": "値動き・60 日", "ret_250": "値動き・250 日",
    "gap": "今朝の夜の値動き", "day_prev": "前の日の昼の値動き", "vol_20": "荒さ・20 日", "vol_60": "荒さ・60 日",
    "night_day_vol": "夜の荒さ ÷ 昼の荒さ", "high_dist": "1 年の高値からの離れ",
    "mkt_gap": "市場全体の今朝の夜", "mkt_ret_5": "市場全体の値動き・5 日", "mkt_ret_20": "市場全体の値動き・20 日",
    "mkt_vol_20": "市場全体の荒さ・20 日", "weekday": "曜日（0 = 月曜）", "days_to_next": "次の営業日までの日数",
}
NUMBER_LABELS = {
    "fc_rev": "純利益の会社予想の変化率", "yoy_profit": "純利益の前年同期比", "yoy_op": "営業利益の前年同期比",
    "yoy_sales": "売上の前年同期比", "dps_rev": "配当予想の変化率", "kind": "開示の種類（1〜4 決算・5 業績予想・6 配当予想）",
}


# --------------------------------------------------------------------------- 年ごとの部品（台帳の点数と同じ切り方）
def m2_year(x: np.ndarray, y: pd.DataFrame, tradable: np.ndarray, year: int, embargo: int = H.EMBARGO, params: dict | None = None):
    """H.walk_forward と同じ切り方で year の部品を作る。学べなければ None。

    返り値: model・use（使う入力の印）・rows（点数を付ける行の (日, 株) の位置）・n_train（学んだ行の数）・train_end（学んだ最後の日）。
    """
    from sklearn.ensemble import HistGradientBoostingRegressor

    yv = y.to_numpy(dtype=np.float32)
    test = np.flatnonzero(y.index.year == year)
    if len(test) == 0:
        return None
    end = max(test[0] - embargo, 0)
    tr = tradable[:end] & np.isfinite(yv[:end])
    if tr.sum() < 10_000:
        return None
    xt = x[:end][tr]
    use = np.isfinite(xt).any(axis=0)
    model = HistGradientBoostingRegressor(**(params or H.GBM_PARAMS)).fit(xt[:, use], yv[:end][tr])
    days, stocks = np.nonzero(tradable[test])
    return {"model": model, "use": use, "rows": (test[days], stocks), "n_train": int(tr.sum()), "train_end": y.index[end - 1]}


def numbers_year(table: pd.DataFrame, y: np.ndarray, index: pd.DatetimeIndex, year: int, embargo: int = N.EMBARGO,
                 params: dict | None = None, features: list[str] | None = None):
    """earnings.walk_forward と同じ切り方。学べなければ None。返り値: model・use・rows（その年の行の位置）・n_train・train_end。"""
    from sklearn.ensemble import HistGradientBoostingRegressor

    pos, j = table["pos"].to_numpy(), table["j"].to_numpy()
    x = table[features or N.FEATURES].to_numpy(dtype=float)
    yrow = y[pos, j]
    test = index.year.to_numpy()[pos] == year
    if not test.any() or not (index.year == year).any():
        return None
    first = int(np.flatnonzero(index.year == year)[0])
    train = (pos < first - embargo) & np.isfinite(yrow)
    if train.sum() < N.MIN_TRAIN:
        return None
    use = np.isfinite(x[train]).any(axis=0)
    model = HistGradientBoostingRegressor(**(params or N.GBM_PARAMS)).fit(x[train][:, use], yrow[train])
    return {"model": model, "use": use, "rows": np.flatnonzero(test), "n_train": int(train.sum()),
            "train_end": index[int(pos[train].max())]}


# --------------------------------------------------------------------------- SHAP 値と読み方
def shap_of(model, X: np.ndarray) -> tuple[np.ndarray, float]:
    """SHAP 値（行 × 入力）と基準の値。木の部品の正確な値で、足すと点数に戻る。"""
    import shap

    ex = shap.TreeExplainer(model)
    sv = np.asarray(ex.shap_values(X), dtype=float)
    return sv, float(np.ravel(ex.expected_value)[0])


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 10:
        return float("nan")
    ra, rb = pd.Series(a[ok]).rank().to_numpy(), pd.Series(b[ok]).rank().to_numpy()
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def summary(sv: np.ndarray, X: np.ndarray, names: list[str]) -> pd.DataFrame:
    """入力ごとの影響の大きさ（SHAP 値の絶対値の平均）・割合・向き（入力の値と SHAP 値の順位相関）・欠けの割合。大きい順。"""
    mean_abs = np.abs(sv).mean(axis=0)
    tot = float(mean_abs.sum())
    out = pd.DataFrame({"name": names, "mean_abs": mean_abs, "share": mean_abs / tot if tot > 0 else np.nan,
                        "direction": [_spearman(X[:, k], sv[:, k]) for k in range(len(names))],
                        "missing": [float(np.mean(~np.isfinite(X[:, k]))) for k in range(len(names))]})
    return out.sort_values("mean_abs", ascending=False, kind="mergesort").reset_index(drop=True)


def similar_pairs(X: np.ndarray, names: list[str], threshold: float = PAIR_MIN) -> list[tuple[str, str, float]]:
    """入力どうしの順位相関の絶対値が threshold 以上の組（大きい順）。"""
    c = pd.DataFrame(X, columns=names).rank().corr()
    pairs = [(a, b, float(c.at[a, b])) for i, a in enumerate(names) for b in names[i + 1:]
             if np.isfinite(c.at[a, b]) and abs(c.at[a, b]) >= threshold]
    return sorted(pairs, key=lambda t: -abs(t[2]))


def beeswarm(sv: np.ndarray, X: np.ndarray, names: list[str], path: Path, title: str) -> Path:
    """bee swarm の図（点 1 つが 1 行。横が SHAP 値、色が入力の値。上ほど影響が大きい）。図の中の文字は英数字だけ（日本語の字体が無くても読める）。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import shap

    shap.summary_plot(sv, X, feature_names=names, show=False, max_display=len(names), plot_size=(9.0, 0.36 * len(names) + 1.6))
    plt.title(title, fontsize=11)
    plt.tight_layout()
    plt.savefig(path, dpi=130)
    plt.close("all")
    return path


def pick(n: int, sample: int, seed: int) -> np.ndarray:
    """n 行からくじで sample 行（多ければ全部）。同じ seed なら同じ行。"""
    return np.sort(np.random.default_rng(seed).choice(n, size=min(sample, n), replace=False))


def explain(model, X: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """SHAP 値・点数・足して点数に戻るかの最大のずれ。"""
    sv, base = shap_of(model, X)
    pred = model.predict(X)
    return sv, pred, float(np.max(np.abs(sv.sum(axis=1) + base - pred))) if len(X) else float("nan")


def stored_diff(pred: np.ndarray, stored: np.ndarray) -> tuple[float, int]:
    """13:05 の自動実行が書いた点数との差の最大と、比べた行の数（同じ部品なら丸めの分だけ）。"""
    ok = np.isfinite(stored) & np.isfinite(pred)
    return (float(np.max(np.abs(pred[ok] - stored[ok]))) if ok.any() else float("nan")), int(ok.sum())


def year_table(per_year: dict[int, pd.DataFrame]) -> pd.DataFrame:
    """年ごとの影響の大きさの順位（行 = 入力、列 = 年。1 が一番大きい）。最新の年の順に並べる。"""
    if not per_year:
        return pd.DataFrame()
    ranks = {y: s.set_index("name")["mean_abs"].rank(ascending=False, method="min").astype(int) for y, s in per_year.items()}
    t = pd.DataFrame(ranks).sort_index(axis=1)
    return t.sort_values(t.columns[-1], kind="mergesort")
