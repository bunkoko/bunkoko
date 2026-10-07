"""メタラベリング（AFML 3・4・10・12 章）: 1 次モデル（今の戦略）の取引ごとに「勝ちやすいか」を判定する 2 次モデル。

- 入力は自分と相方の銘柄の値動きだけ（MT5 の中で計算できる）。向きのある入力は売りなら符号を反転
- ラベルは実際の取引の結果（R > 0 なら 1）。出口は戦略自身の損切り・手仕舞い（トリプルバリアの上下に当たる）
- 重みは |R|（頭打ちあり）× 平均の独自性（同時に持っている取引が多いほど小さい）
- 2 次モデルは L2 のロジスティック回帰（EA でも同じ計算ができる）
- 検証は、パージングとエンバーゴ付きの組み合わせ交差検証（CPCV）

設定は docs/meta_labeling.md に、結果を見る前に決めたもの。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

import numpy as np
import pandas as pd

from .context import CSeries, at_times, zchange
from .features import FeatureFrame, decide_ns
from .indicators import extension_z

META_DIRECTIONAL = ("ext20", "ext60", "ma200", "partner_ext20")
META_NEUTRAL = ("er55", "vol_ratio", "width55")
META_LABELS = {
    "ext20": "直近 20 本の動き（ふだんの何倍か、取引の向き）",
    "ext60": "直近 60 本の動き（同上）",
    "ma200": "200 本の平均からの離れ（取引の向き）",
    "partner_ext20": "相方の直近 20 本の動き（取引の向き）",
    "er55": "55 本の値動きの効率（直線的か）",
    "vol_ratio": "最近の値動きの大きさ（ふだんとの比）",
    "width55": "55 本の高値と安値の幅（ふだんとの比）",
}
DAY_NS = 86_400 * 10**9


def meta_feature_frame(symbol: str, frame: pd.DataFrame, tf: pd.Timedelta,
                       partner: CSeries | None = None) -> FeatureFrame:
    """足ごとの 2 次モデルの入力（その足の終値で判断する時点の値）。"""
    t = decide_ns(frame.index, tf)
    close = frame["close"].astype(float)
    lc = np.log(close)
    r1 = lc.diff()
    sd = r1.rolling(252, min_periods=126).std()          # ふだんの 1 日の動き（ext20 と同じ）
    hi55 = np.log(frame["high"].astype(float)).rolling(55).max()
    lo55 = np.log(frame["low"].astype(float)).rolling(55).min()
    with np.errstate(divide="ignore", invalid="ignore"):
        directional = {
            "ext20": extension_z(close, 20, 252).to_numpy(float),
            "ext60": extension_z(close, 60, 252).to_numpy(float),
            "ma200": (np.log(close / close.rolling(200).mean()) / sd).to_numpy(float),
            "partner_ext20": (at_times(partner, zchange(partner, 20), t) if partner is not None
                              else np.full(len(t), np.nan)),
        }
        neutral = {
            "er55": ((lc - lc.shift(55)).abs() / r1.abs().rolling(55).sum()).to_numpy(float),
            "vol_ratio": np.log(r1.rolling(20).std() / sd).to_numpy(float),
            "width55": ((hi55 - lo55) / (sd * np.sqrt(55))).to_numpy(float),
        }
    rows = pd.RangeIndex(len(t))
    d = pd.DataFrame(directional, index=rows).replace([np.inf, -np.inf], np.nan)
    n = pd.DataFrame(neutral, index=rows).replace([np.inf, -np.inf], np.nan)
    return FeatureFrame(frame.index, t, d, n)


def meta_rows(trades: pd.DataFrame, feats: dict[str, FeatureFrame]) -> pd.DataFrame:
    """取引ごとの入力（判断した足の値）と、判断・決済の時刻、結果（R）。取引の並び順のまま。"""
    cols = list(META_DIRECTIONAL) + list(META_NEUTRAL)
    out = []
    if trades.empty:
        return pd.DataFrame(columns=cols + ["symbol", "side", "decide_ns", "exit_ns", "r"])
    for sym, g in trades.groupby("symbol", sort=False):
        ff = feats.get(sym)
        if ff is None:
            continue
        entry_ns = pd.DatetimeIndex(g["entry_time"]).tz_convert("UTC").as_unit("ns").asi8
        k = np.searchsorted(ff.decide, entry_ns, side="right") - 1
        ok = k >= 0
        g, k = g[ok], k[ok]
        side = g["side"].to_numpy(float)
        x = ff.rows(side, k).drop(columns=["side"]).reindex(columns=cols)
        x["symbol"] = sym
        x["side"] = side
        x["decide_ns"] = ff.decide[k]
        x["exit_ns"] = pd.DatetimeIndex(g["exit_time"]).tz_convert("UTC").as_unit("ns").asi8
        x["r"] = g["r_multiple"].to_numpy(float)
        x.index = g.index
        out.append(x)
    if not out:
        return pd.DataFrame(columns=cols + ["symbol", "side", "decide_ns", "exit_ns", "r"])
    return pd.concat(out).sort_values("decide_ns", kind="stable").reset_index(drop=True)


def feature_columns() -> list[str]:
    return list(META_DIRECTIONAL) + list(META_NEUTRAL)


# --------------------------------------------------------------------------- 重み（AFML 4 章）
def uniqueness(start_ns: np.ndarray, end_ns: np.ndarray) -> np.ndarray:
    """各取引の平均の独自性: 持っていた日ごとの 1 / (その日に持っていた取引の数) の平均。"""
    if len(start_ns) == 0:
        return np.zeros(0)
    d0 = np.asarray(start_ns, dtype=np.int64) // DAY_NS
    d1 = np.maximum(np.asarray(end_ns, dtype=np.int64) // DAY_NS, d0)
    base = int(d0.min())
    span = int(d1.max()) - base + 2
    count = np.zeros(span)
    np.add.at(count, d0 - base, 1)
    np.add.at(count, d1 - base + 1, -1)
    conc = np.cumsum(count)[:-1]
    inv = np.where(conc > 0, 1.0 / np.maximum(conc, 1), 0.0)
    csum = np.concatenate([[0.0], np.cumsum(inv)])
    return (csum[d1 - base + 1] - csum[d0 - base]) / (d1 - d0 + 1)


def meta_weights(r: np.ndarray, start_ns: np.ndarray, end_ns: np.ndarray, clip: float = 5.0) -> np.ndarray:
    """|R|（clip で頭打ち）× 平均の独自性。平均 1 にそろえる。"""
    w = np.minimum(np.abs(np.asarray(r, dtype=float)), clip) * uniqueness(start_ns, end_ns)
    w = np.where(np.isfinite(w), w, 0.0)
    m = w.mean() if len(w) else 0.0
    return w / m if m > 0 else np.ones(len(w))


# --------------------------------------------------------------------------- 2 次モデル
@dataclass
class LogisticModel:
    columns: list[str]
    mean: np.ndarray
    scale: np.ndarray
    coef: np.ndarray
    intercept: float
    n: int = 0
    base_rate: float = float("nan")        # 重み付きの勝ちの割合（学習データ）
    importance: dict[str, float] = field(default_factory=dict)

    def _z(self, x: pd.DataFrame) -> np.ndarray:
        a = x.reindex(columns=self.columns).to_numpy(float)
        z = (a - self.mean) / self.scale
        return np.where(np.isfinite(z), z, 0.0)   # 欠けている入力は学習データの平均として扱う

    def prob(self, x: pd.DataFrame) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-(self._z(x) @ self.coef + self.intercept)))

    def to_dict(self) -> dict:
        return {"columns": self.columns, "mean": [float(v) for v in self.mean],
                "scale": [float(v) for v in self.scale], "coef": [float(v) for v in self.coef],
                "intercept": float(self.intercept), "n": int(self.n), "base_rate": float(self.base_rate)}


def fit_logistic(x: pd.DataFrame, y: np.ndarray, w: np.ndarray | None = None, lam_per_row: float = 0.1,
                 max_iter: int = 100) -> LogisticModel:
    """重み付き・L2 のロジスティック回帰（ニュートン法）。入力は学習データで標準化、切片は罰則なし。"""
    cols = [c for c in x.columns if x[c].notna().mean() > 0.3]   # ほとんど欠けている入力は使わない
    a = x[cols].to_numpy(float)
    y = np.asarray(y, dtype=float)
    w = np.ones(len(y)) if w is None else np.asarray(w, dtype=float)
    with np.errstate(invalid="ignore"):
        mean = np.nanmean(a, axis=0) if len(a) else np.zeros(len(cols))
        scale = np.nanstd(a, axis=0) if len(a) else np.ones(len(cols))
    mean = np.where(np.isfinite(mean), mean, 0.0)
    scale = np.where(np.isfinite(scale) & (scale > 0), scale, 1.0)
    z = np.where(np.isfinite((a - mean) / scale), (a - mean) / scale, 0.0)
    xm = np.column_stack([np.ones(len(y)), z])
    base = float(np.sum(w * y) / np.sum(w)) if np.sum(w) > 0 else 0.5
    base = min(max(base, 1e-4), 1 - 1e-4)
    beta = np.zeros(xm.shape[1])
    beta[0] = np.log(base / (1 - base))
    pen = np.full(xm.shape[1], lam_per_row * len(y))
    pen[0] = 0.0
    for _ in range(max_iter):
        p = 1.0 / (1.0 + np.exp(-(xm @ beta)))
        grad = xm.T @ (w * (p - y)) + pen * beta
        hess = xm.T @ (xm * (w * p * (1 - p))[:, None]) + np.diag(pen) + 1e-9 * np.eye(len(beta))
        step = np.linalg.solve(hess, grad)
        beta -= step
        if np.max(np.abs(step)) < 1e-10:
            break
    coef = beta[1:]
    imp = dict(sorted(zip(cols, coef.round(4)), key=lambda kv: -abs(kv[1])))
    return LogisticModel(cols, mean, scale, coef, float(beta[0]), len(y), base, imp)


# --------------------------------------------------------------------------- CPCV（AFML 12 章）
@dataclass
class CPCV:
    groups: np.ndarray                 # 取引ごとのグループ番号
    bounds: list[tuple[int, int]]      # グループごとの判断時刻の範囲 [lo, hi)（ns）
    combos: list[tuple[int, ...]]      # 検証に使うグループの組み合わせ
    train: list[np.ndarray]            # 組み合わせごとの学習に使う取引（パージング・エンバーゴ後）

    def paths(self) -> list[dict[int, int]]:
        """バックテストの経路: 経路ごとに「グループ → そのグループを検証した組み合わせの番号」。"""
        n_groups = len(self.bounds)
        per_group = {g: [i for i, c in enumerate(self.combos) if g in c] for g in range(n_groups)}
        n_paths = min(len(v) for v in per_group.values()) if per_group else 0
        return [{g: per_group[g][j] for g in range(n_groups)} for j in range(n_paths)]


def cpcv(decide: np.ndarray, exit_: np.ndarray, n_groups: int = 6, n_test: int = 2,
         embargo_ns: int = 0) -> CPCV:
    """取引を判断時刻の順に n_groups に分け、n_test グループずつ検証に使う全部の組み合わせを作る。

    パージング: 学習側から、判断〜決済の期間が検証グループの期間と重なる取引を除く。
    エンバーゴ: 検証グループの期間の直後 embargo_ns の間に判断した取引も除く。
    """
    decide = np.asarray(decide, dtype=np.int64)
    exit_ = np.asarray(exit_, dtype=np.int64)
    order = np.argsort(decide, kind="stable")
    groups = np.empty(len(decide), dtype=int)
    chunks = np.array_split(order, n_groups)
    starts = []
    for g, idx in enumerate(chunks):
        groups[idx] = g
        starts.append(int(decide[idx].min()) if len(idx) else None)
    lo_hi = []
    lo_min, hi_max = np.iinfo(np.int64).min, np.iinfo(np.int64).max
    for g in range(n_groups):
        lo = lo_min if g == 0 else starts[g]
        hi = hi_max if g == n_groups - 1 else starts[g + 1]
        lo_hi.append((lo, hi))
    combos = list(combinations(range(n_groups), n_test))
    train = []
    for c in combos:
        keep = ~np.isin(groups, c)
        for g in c:
            lo, hi = lo_hi[g]
            keep &= ~((decide < hi) & (exit_ >= lo))                       # パージング
            if hi != hi_max:
                keep &= ~((decide >= hi) & (decide < hi + embargo_ns))     # エンバーゴ
        train.append(np.flatnonzero(keep))
    return CPCV(groups, lo_hi, combos, train)


# --------------------------------------------------------------------------- 使い方（見送り・半分）
MODES = {
    "skip": "見送り（勝ちやすさ 0.5 以下は入らない）",
    "half": "半分（勝ちやすさ 0.5 以下は 1 回の損失を半分）",
}


def side_probs(ff: FeatureFrame, segments: list[tuple[int, int, LogisticModel | None]]) -> tuple[np.ndarray, np.ndarray]:
    """足ごとの、買い・売りそれぞれの勝ちやすさ。segments は (判断時刻の始め, 終わり, モデル)。無い所は NaN。"""
    p_long = np.full(len(ff.decide), np.nan)
    p_short = np.full(len(ff.decide), np.nan)
    for lo, hi, model in segments:
        if model is None:
            continue
        at = np.flatnonzero((ff.decide >= lo) & (ff.decide < hi))
        if len(at):
            p_long[at] = model.prob(ff.rows(1, at))
            p_short[at] = model.prob(ff.rows(-1, at))
    return p_long, p_short


def meta_gates(feats: dict[str, FeatureFrame], segments: list[tuple[int, int, LogisticModel | None]],
               mode: str, threshold: float = 0.5) -> dict[str, pd.DataFrame]:
    """2 次モデルによるエントリーの絞り込み（backtest の entry_gate）。判定できない足は今まで通り入る。"""
    if mode not in MODES:
        raise ValueError(f"mode は {list(MODES)} のどれか")
    out = {}
    for sym, ff in feats.items():
        pl, ps = side_probs(ff, segments)
        low_l, low_s = pl <= threshold, ps <= threshold          # NaN は False（今まで通り）
        if mode == "skip":
            out[sym] = pd.DataFrame({"long": ~low_l, "short": ~low_s}, index=ff.index)
        else:
            n = len(ff.index)
            out[sym] = pd.DataFrame({"long": np.ones(n, bool), "short": np.ones(n, bool),
                                     "long_size": np.where(low_l, 0.5, 1.0),
                                     "short_size": np.where(low_s, 0.5, 1.0)}, index=ff.index)
    return out


# --------------------------------------------------------------------------- 精度（AFML 3 章）
def auc(p: np.ndarray, y: np.ndarray) -> float:
    ok = np.isfinite(p)
    p, y = p[ok], np.asarray(y)[ok].astype(bool)
    n1, n0 = int(y.sum()), int((~y).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    ranks = pd.Series(p).rank().to_numpy()
    return float((ranks[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def classification(p: np.ndarray, r: np.ndarray, threshold: float = 0.5) -> dict:
    """勝ち（R > 0）の当て方。accepted = 勝ちやすさが threshold を超えた取引（見送りで残る取引）。"""
    p = np.asarray(p, dtype=float)
    r = np.asarray(r, dtype=float)
    ok = np.isfinite(p) & np.isfinite(r)
    p, r = p[ok], r[ok]
    y = r > 0
    acc = p > threshold
    return {
        "n": int(len(r)),
        "win_rate": float(y.mean()) if len(y) else float("nan"),
        "accepted": float(acc.mean()) if len(acc) else float("nan"),
        "precision": float(y[acc].mean()) if acc.any() else float("nan"),
        "recall": float((y & acc).sum() / y.sum()) if y.any() else float("nan"),
        "r_accepted": float(r[acc].mean()) if acc.any() else float("nan"),
        "r_rejected": float(r[~acc].mean()) if (~acc).any() else float("nan"),
        "auc": auc(p, y),
    }
