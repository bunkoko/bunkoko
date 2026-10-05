"""外部データによるエントリーの絞り込み（仮説）と、全部の入力をまとめて使う予測（研究用）。

仮説は「経済的な理由がある向き」だけを事前に決めておく（データを見てから向きを選ばない）。
どれも「判断の時点で公表済みの値の、直近 n 本の変化の向き」で、買い・売りの片方だけを許す。

全部の入力を使う方は、過去の取引の結果（R）を外部データで説明するリッジ回帰を 1 年ごとに作り直し、
翌年は「予測が 0 以下の向きのエントリー」を見送る（その年より前の取引だけで学習する）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from .context import SPECS, CSeries, at_times, change, cot_percentile, level_z, zchange

ALL = ("GOLD", "SILVER", "WTI", "BRENT")
PARTNER = {"GOLD": "SILVER", "SILVER": "GOLD", "WTI": "BRENT", "BRENT": "WTI"}
RISKY = ("SILVER", "WTI", "BRENT")   # 景気・リスク選好で動きやすい（金は逆に買われることがある）
METALS = ("GOLD", "SILVER")
OIL = ("WTI", "BRENT")


def _each(symbols, key: str) -> dict[str, str]:
    return {s: key for s in symbols}


@dataclass(frozen=True)
class Hypothesis:
    name: str
    label: str
    inputs: dict[str, str]        # 銘柄 → 系列キー（"partner" は相方の銘柄の価格。"a|b" は a が無ければ b）
    sign: int = 1                 # +1: 系列が上がっていれば買いだけ / -1: 下がっていれば買いだけ
    kind: str = "trend"           # "trend" / "cot"
    n: int = 20                   # 何本前からの変化を見るか（日次なら約 1 か月）


HYPOTHESES: tuple[Hypothesis, ...] = (
    Hypothesis("usd", "ドル安なら買いだけ・ドル高なら売りだけ（ドルインデックス）", _each(ALL, "dxy|usd_broad"), -1),
    Hypothesis("real_rate", "実質金利が下がっていれば買いだけ（金・銀）", _each(METALS, "real10y"), -1),
    Hypothesis("bonds", "米国債の価格が上がっていれば買いだけ（金・銀）", _each(METALS, "tbond"), +1),
    Hypothesis("breakeven", "期待インフレ率が上がっていれば買いだけ", _each(ALL, "breakeven10y"), +1),
    Hypothesis("equities", "米国株が上がっていれば買いだけ（銀・原油）", _each(RISKY, "spx"), +1),
    Hypothesis("copper", "銅が上がっていれば買いだけ（銀・原油）", _each(RISKY, "copper"), +1),
    Hypothesis("miners", "関連株（金鉱株・銀鉱株・エネルギー株）と同じ向きだけ",
               {"GOLD": "gold_miners", "SILVER": "silver_miners", "WTI": "energy_equity", "BRENT": "energy_equity"}),
    Hypothesis("partner", "相方（金↔銀・WTI↔ブレント）と同じ向きだけ", _each(ALL, "partner")),
    Hypothesis("commodity_fx", "資源国通貨（豪ドル）が上がっていれば買いだけ", _each(ALL, "audusd"), +1),
    Hypothesis("credit", "社債スプレッドが広がっていれば売りだけ（銀・原油）", _each(RISKY, "hy_spread"), -1),
    Hypothesis("vix", "VIX が上がっていれば売りだけ（銀・原油）", _each(RISKY, "vix"), -1),
    Hypothesis("inventory", "米原油在庫が増えていれば売りだけ（4 週の変化）", _each(OIL, "crude_stocks"), -1, n=4),
    Hypothesis("cot", "投機筋の偏りが 3 年の上位・下位 10% なら、偏っている向きには入らない",
               {"GOLD": "cot_gold", "SILVER": "cot_silver", "WTI": "cot_wti", "BRENT": "cot_wti"}, kind="cot"),
)
HYPOTHESIS_BY_NAME = {h.name: h for h in HYPOTHESES}

# 全部の入力を使う予測で、向きを持つ特徴（変化の向き × 売買の向き）として使う系列
DIRECTIONAL_KEYS = (
    "ust10y", "ust2y", "real10y", "breakeven10y", "curve10y2y", "usd_broad", "hy_spread", "vix", "ovx", "gvz",
    "crude_stocks", "copper", "platinum", "natgas", "spx", "ndx", "nikkei", "em_equity", "gold_miners",
    "silver_miners", "energy_equity", "dxy", "usdjpy", "eurusd", "audusd", "usdcad", "tbond",
)
# 向きを持たない特徴（水準。荒れた相場かどうか）
LEVEL_KEYS = ("vix", "ovx", "gvz", "hy_spread")
COT_FOR = {"GOLD": "cot_gold", "SILVER": "cot_silver", "WTI": "cot_wti", "BRENT": "cot_wti"}


Resolver = Callable[[str, str], "CSeries | None"]   # (系列キー, 銘柄) → 系列


def decide_ns(index: pd.DatetimeIndex, tf: pd.Timedelta) -> np.ndarray:
    """各足の判断の時刻（足の終了時刻, UTC ns）。"""
    return (index.tz_convert("UTC").as_unit("ns").asi8 + tf.value).astype(np.int64)


def hypothesis_gate(h: Hypothesis, symbol: str, index: pd.DatetimeIndex, tf: pd.Timedelta,
                    resolve: Resolver) -> pd.DataFrame | None:
    """仮説 h による銘柄 symbol の絞り込み（列 long / short）。対象外・データが無ければ None。"""
    keys = h.inputs.get(symbol)
    if keys is None:
        return None
    cs = next((c for c in (resolve(k, symbol) for k in keys.split("|")) if c is not None), None)
    if cs is None:
        return None
    t = decide_ns(index, tf)
    if h.kind == "cot":
        pct = at_times(cs, cot_percentile(cs), t)
        allow_long, allow_short = ~(pct > 0.9), ~(pct < 0.1)
    else:
        d = h.sign * np.sign(at_times(cs, change(cs, h.n), t))
        allow_long, allow_short = ~(d < 0), ~(d > 0)   # データが無い日（NaN）は両方許す
    return pd.DataFrame({"long": allow_long, "short": allow_short}, index=index)


# --------------------------------------------------------------------------- 全部の入力を使う予測
@dataclass
class FeatureFrame:
    """銘柄ごと・足ごとの特徴。directional は買いなら +、売りなら − を掛けて使う。"""

    index: pd.DatetimeIndex
    decide: np.ndarray
    directional: pd.DataFrame
    neutral: pd.DataFrame

    def rows(self, side: int, at: np.ndarray | None = None) -> pd.DataFrame:
        d = self.directional if at is None else self.directional.iloc[at]
        n = self.neutral if at is None else self.neutral.iloc[at]
        side = np.broadcast_to(np.asarray(side, dtype=float), (len(d),))
        x = d.mul(side, axis=0)
        x = pd.concat([x.reset_index(drop=True), n.reset_index(drop=True)], axis=1)
        x["side"] = side
        return x


def feature_frame(symbol: str, frame: pd.DataFrame, tf: pd.Timedelta, resolve: Resolver) -> FeatureFrame:
    t = decide_ns(frame.index, tf)
    own = CSeries("own", frame[["close"]], t, "price")
    cols: dict[str, np.ndarray] = {
        "own_z20": at_times(own, zchange(own, 20), t),
        "own_z60": at_times(own, zchange(own, 60), t),
        "own_z120": at_times(own, zchange(own, 120), t),
    }
    partner = resolve("partner", symbol)
    if partner is not None:
        cols["partner_z20"] = at_times(partner, zchange(partner, 20), t)
    for key in DIRECTIONAL_KEYS:
        cs = resolve(key, symbol)
        if cs is not None:
            n = 4 if key == "crude_stocks" else 20
            cols[f"{key}_z{n}"] = at_times(cs, zchange(cs, n, window=52 if n == 4 else 252), t)
    cot = resolve(COT_FOR[symbol], symbol) if symbol in COT_FOR else None
    if cot is not None:
        cols["cot_pct"] = at_times(cot, cot_percentile(cot), t) - 0.5
    neutral = {}
    for key in LEVEL_KEYS:
        cs = resolve(key, symbol)
        if cs is not None:
            neutral[f"{key}_level"] = at_times(cs, level_z(cs), t)
    rows = pd.RangeIndex(len(t))
    return FeatureFrame(frame.index, t, pd.DataFrame(cols, index=rows), pd.DataFrame(neutral, index=rows))


@dataclass
class RidgeModel:
    columns: list[str]
    mean: np.ndarray
    scale: np.ndarray
    coef: np.ndarray
    intercept: float
    n: int = 0
    importance: dict[str, float] = field(default_factory=dict)

    def _z(self, x: pd.DataFrame) -> np.ndarray:
        a = x.reindex(columns=self.columns).to_numpy(float)
        z = (a - self.mean) / self.scale
        return np.where(np.isfinite(z), z, 0.0)   # 欠けている特徴は平均（0）として扱う

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return self._z(x) @ self.coef + self.intercept


def fit_ridge(x: pd.DataFrame, y: np.ndarray, alpha_per_row: float = 0.25) -> RidgeModel:
    cols = [c for c in x.columns if x[c].notna().mean() > 0.3]   # ほとんど欠けている特徴は使わない
    a = x[cols].to_numpy(float)
    mean = np.nanmean(a, axis=0)
    scale = np.nanstd(a, axis=0)
    scale = np.where(np.isfinite(scale) & (scale > 0), scale, 1.0)
    z = np.where(np.isfinite((a - mean) / scale), (a - mean) / scale, 0.0)
    y = np.asarray(y, dtype=float)
    b = float(y.mean())
    alpha = alpha_per_row * len(y)
    coef = np.linalg.solve(z.T @ z + alpha * np.eye(z.shape[1]), z.T @ (y - b))
    imp = dict(sorted(zip(cols, coef.round(4)), key=lambda kv: -abs(kv[1])))
    return RidgeModel(cols, mean, scale, coef, b, len(y), imp)


def trade_rows(trades: pd.DataFrame, feats: dict[str, FeatureFrame]) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """取引ごとの特徴（判断した足の値）・結果（R）・決済時刻。"""
    xs, ys, exits = [], [], []
    for sym, g in trades.groupby("symbol"):
        ff = feats.get(sym)
        if ff is None:
            continue
        entry_ns = pd.DatetimeIndex(g["entry_time"]).tz_convert("UTC").as_unit("ns").asi8
        k = np.searchsorted(ff.decide, entry_ns, side="right") - 1
        ok = k >= 0
        g = g[ok]
        for side in (1, -1):
            m = (g["side"] == side).to_numpy()
            if m.any():
                xs.append(ff.rows(side, k[ok][m]))
                ys.append(g["r_multiple"].to_numpy(float)[m])
                exits.append(pd.DatetimeIndex(g["exit_time"]).tz_convert("UTC").as_unit("ns").asi8[m])
    if not xs:
        return pd.DataFrame(), np.zeros(0), np.zeros(0, dtype=np.int64)
    return pd.concat(xs, ignore_index=True), np.concatenate(ys), np.concatenate(exits)


def ml_gates(feats: dict[str, FeatureFrame], pool_x: pd.DataFrame, pool_y: np.ndarray, pool_exit: np.ndarray,
             start: pd.Timestamp, end: pd.Timestamp, min_trades: int = 200, clip=(-3.0, 5.0),
             alpha_per_row: float = 0.25) -> tuple[dict[str, pd.DataFrame], list[tuple[int, RidgeModel | None]]]:
    """1 年ごとに、その年の始まりより前に決済した取引だけで学習し、その年のエントリーを絞り込む。"""
    gates = {s: pd.DataFrame({"long": np.ones(len(f.index), bool), "short": np.ones(len(f.index), bool)},
                             index=f.index) for s, f in feats.items()}
    models: list[tuple[int, RidgeModel | None]] = []
    y_clipped = np.clip(pool_y, *clip)
    for year in range(start.year, end.year + 1):
        lo = max(pd.Timestamp(f"{year}-01-01", tz="UTC"), start)
        hi = min(pd.Timestamp(f"{year + 1}-01-01", tz="UTC"), end)
        train = pool_exit < lo.value
        if train.sum() < min_trades:
            models.append((year, None))
            continue
        model = fit_ridge(pool_x[train].reset_index(drop=True), y_clipped[train], alpha_per_row)
        models.append((year, model))
        for s, ff in feats.items():
            m = (ff.decide > lo.value) & (ff.decide <= hi.value)
            if not m.any():
                continue
            at = np.flatnonzero(m)
            gates[s].iloc[at, 0] = model.predict(ff.rows(1, at)) > 0
            gates[s].iloc[at, 1] = model.predict(ff.rows(-1, at)) > 0
    return gates, models


def describe_inputs(resolve: Resolver, symbols=ALL) -> list[str]:
    """使える系列の一覧（表示用）。"""
    out = []
    for key, spec in SPECS.items():
        cs = next((c for c in (resolve(key, s) for s in symbols) if c is not None), None)
        if cs is not None and len(cs.values):
            out.append(f"{spec.label}（{key}）{cs.values.index[0]:%Y-%m}〜{cs.values.index[-1]:%Y-%m}")
    return out
