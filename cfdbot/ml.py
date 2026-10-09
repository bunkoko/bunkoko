"""多くの市場をまとめて学習する、ポジションを直接決めるモデル（研究用。./cfd ml）。

考え方（Lim, Zohren, Roberts「Enhancing Time-Series Momentum Strategies Using Deep Neural Networks」2019 と同じ形）:
  - 入力は市場ごとに標準化した値動き（いくつかの期間のリターン、MACD、タートルの状態、値動きの大きさ）
  - 出力は −1〜+1 のポジション。値動きの大きさで割って、どの市場も同じリスクにそろえる
  - 学習の目的は「コストを引いた後のシャープレシオ」を直接大きくすること（勝ち負けの当てっこではない）
  - 59 市場をまとめて 1 つのモデルを学ぶので、4 銘柄だけより数十倍のデータで学べる

ポジションが市場に影響しない前提では、これは「1 期先の報酬で方策を学ぶ強化学習」と同じ形になる。
手数料を考えて「今のポジションから動かすか」まで学ばせるのが完全な強化学習（docs/ml.md の段階 2）。

設定は docs/ml.md に、結果を見る前に決めたもの。numpy だけで動く（追加のインストール不要）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

HORIZONS = (1, 5, 21, 63, 126, 252)          # 標準化したリターンの期間（日）
MACD_PAIRS = ((8, 24), (16, 48), (32, 96))   # MACD の短期・長期（Baz ほか 2015 と同じ）
VOL_SPAN = 60                                # 値動きの大きさ（指数平滑の標準偏差）
TARGET_VOL = 0.15 / np.sqrt(252)             # 1 市場あたりの目標の値動き（年率 15%）
MAX_LEVERAGE = 5.0                           # 値動きがごく小さい時期の倍率の上限
FEATURES = tuple([f"ret{h}" for h in HORIZONS] + [f"macd{s}_{l}" for s, l in MACD_PAIRS]
                 + ["turtle", "vol_ratio"])


def turtle_state(close: pd.Series, entry: int = 55, exit_: int = 20) -> np.ndarray:
    """タートル 55/20 の向き（終値だけで判定。損切りは入れない）: 55 日の高値を超えたら +1、20 日の安値を割るまで続ける。"""
    c = close.to_numpy(float)
    hi = close.rolling(entry).max().shift(1).to_numpy()
    lo = close.rolling(entry).min().shift(1).to_numpy()
    ex_lo = close.rolling(exit_).min().shift(1).to_numpy()
    ex_hi = close.rolling(exit_).max().shift(1).to_numpy()
    out = np.zeros(len(c))
    pos = 0.0
    for i in range(len(c)):
        if pos > 0 and c[i] < ex_lo[i]:
            pos = 0.0
        elif pos < 0 and c[i] > ex_hi[i]:
            pos = 0.0
        if pos == 0:
            if c[i] > hi[i]:
                pos = 1.0
            elif c[i] < lo[i]:
                pos = -1.0
        out[i] = pos
    return out


def market_frame(close: pd.Series) -> pd.DataFrame:
    """1 市場の、日ごとの入力・翌日のリターン・値動きの大きさ（判断はその日の終値、損益は翌日の終値まで）。"""
    close = close.astype(float)
    r = close.pct_change()
    sig = r.ewm(span=VOL_SPAN, min_periods=VOL_SPAN).std()
    out = {}
    for h in HORIZONS:
        out[f"ret{h}"] = ((close / close.shift(h) - 1) / (sig * np.sqrt(h))).clip(-5, 5)
    for s, l_ in MACD_PAIRS:
        m = close.ewm(span=s, adjust=False).mean() - close.ewm(span=l_, adjust=False).mean()
        q = m / close.rolling(63).std()
        out[f"macd{s}_{l_}"] = (q / q.rolling(252).std()).clip(-5, 5)
    out["turtle"] = pd.Series(turtle_state(close), index=close.index)
    out["vol_ratio"] = np.log(r.rolling(20).std() / r.rolling(252).std()).clip(-3, 3)
    f = pd.DataFrame(out, index=close.index).replace([np.inf, -np.inf], np.nan)
    f["sigma"] = sig
    f["next_ret"] = r.shift(-1)
    return f


@dataclass
class Panel:
    """全市場を縦に並べたデータ（市場ごとに日付順）。"""

    x: np.ndarray            # 入力（欠けは 0）
    next_ret: np.ndarray
    lev: np.ndarray          # 目標の値動き ÷ その市場の値動き（上限つき）
    cost: np.ndarray         # 動かした名目 1 あたりのコスト（往復コストの半分）
    fin: np.ndarray          # 持っている名目 1 あたりの 1 日の保有コスト
    market: np.ndarray       # 市場の番号
    dates: np.ndarray        # 日付（datetime64[ns]）
    keys: list[str]
    first: np.ndarray        # 市場の最初の行か（前日のポジションを 0 とする）

    def take(self, mask: np.ndarray) -> "Panel":
        idx = np.flatnonzero(mask)
        first = np.ones(len(idx), bool)
        if len(idx) > 1:
            first[1:] = (self.market[idx[1:]] != self.market[idx[:-1]]) | (idx[1:] != idx[:-1] + 1)
        return Panel(self.x[idx], self.next_ret[idx], self.lev[idx], self.cost[idx], self.fin[idx],
                     self.market[idx], self.dates[idx], self.keys, first)


def build_panel(closes: dict[str, pd.Series], costs: dict[str, tuple[float, float]]) -> Panel:
    """closes: 市場 → 日足の終値（日付の index）。costs: 市場 → (往復コスト, 保有コスト 年率)。"""
    parts = []
    keys = list(closes)
    for mi, k in enumerate(keys):
        f = market_frame(closes[k])
        f = f[f["sigma"].notna() & f["next_ret"].notna()]
        if f.empty:
            continue
        rt, fin = costs[k]
        f = f.assign(_m=mi, _cost=rt / 2, _fin=fin / 252)
        parts.append(f)
    df = pd.concat(parts)
    x = df[list(FEATURES)].to_numpy(np.float32)
    x = np.where(np.isfinite(x), x, 0.0).astype(np.float32)
    lev = np.minimum(TARGET_VOL / df["sigma"].to_numpy(float), MAX_LEVERAGE)
    m = df["_m"].to_numpy(int)
    first = np.r_[True, m[1:] != m[:-1]]
    return Panel(x, df["next_ret"].to_numpy(float), lev, df["_cost"].to_numpy(float), df["_fin"].to_numpy(float),
                 m, df.index.to_numpy(dtype="datetime64[ns]"), keys, first)


# --------------------------------------------------------------------------- 成績（コスト込み）
def strategy_returns(p: np.ndarray, panel: Panel) -> np.ndarray:
    """ポジション p（−1〜+1）の、行ごとの翌日の損益（目標の値動きにそろえた名目で、売買と保有のコストを引く）。"""
    u = p * panel.lev
    prev = np.r_[0.0, u[:-1]]
    prev[panel.first] = 0.0
    return u * panel.next_ret - panel.cost * np.abs(u - prev) - panel.fin * np.abs(u)


def daily_portfolio(rets: np.ndarray, panel: Panel) -> pd.Series:
    """日ごとに、その日に取引のあった市場の平均（市場ごとに同じリスク）。"""
    return pd.Series(rets).groupby(panel.dates).mean().sort_index()


def ann_sharpe(r: pd.Series | np.ndarray) -> float:
    r = np.asarray(r, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 60 or r.std() <= 0:
        return float("nan")
    return float(r.mean() / r.std() * np.sqrt(252))


# --------------------------------------------------------------------------- 決まったルール（比べる相手）
def rule_positions(panel: Panel, name: str) -> np.ndarray:
    x = {f: panel.x[:, i] for i, f in enumerate(FEATURES)}
    if name == "long":
        return np.ones(len(panel.x))
    if name == "tsmom":                       # 12 か月のリターンの向き（Moskowitz ほか 2012）
        return np.sign(x["ret252"])
    if name == "macd":                        # MACD の組み合わせ（Baz ほか 2015）
        phi = [y * np.exp(-y * y / 4) / 0.89 for y in (x[f"macd{s}_{l_}"] for s, l_ in MACD_PAIRS)]
        return np.clip(np.mean(phi, axis=0), -1, 1)
    if name == "turtle":
        return x["turtle"]
    raise ValueError(name)


RULES = {"long": "持っているだけ（買いのみ）", "tsmom": "12 か月の向き", "macd": "MACD の組み合わせ",
         "turtle": "タートル 55/20 の向き（今の戦略に近い）"}


# --------------------------------------------------------------------------- 学習
@dataclass
class PolicyModel:
    """ポジション = tanh(出力)。hidden = 0 なら線形。"""

    hidden: int
    params: dict[str, np.ndarray]
    history: list[float] = field(default_factory=list)

    def forward(self, x: np.ndarray):
        if self.hidden:
            a = np.tanh(x @ self.params["W1"] + self.params["b1"])
            z = a @ self.params["w2"] + self.params["b2"][0]
            return np.tanh(z), a
        return np.tanh(x @ self.params["w"] + self.params["b"][0]), None

    def positions(self, x: np.ndarray) -> np.ndarray:
        return self.forward(x)[0].astype(float)


def _init(n_in: int, hidden: int, rng) -> dict[str, np.ndarray]:
    if hidden:
        return {"W1": (rng.normal(size=(n_in, hidden)) / np.sqrt(n_in)).astype(np.float32),
                "b1": np.zeros(hidden, np.float32), "w2": (rng.normal(size=hidden) * 0.1).astype(np.float32),
                "b2": np.zeros(1, np.float32)}
    # 全部 0 から始めるとポジションが 0 のまま（シャープの勾配が出ない）ので、小さな乱数から始める
    return {"w": (rng.normal(size=n_in) * 0.1 / np.sqrt(n_in)).astype(np.float32), "b": np.zeros(1, np.float32)}


def sharpe_and_grad(model: PolicyModel, panel: Panel, l2: float):
    """コスト込みのシャープ（年率）と、各パラメータについての勾配（シャープを大きくする向き）。"""
    p, a = model.forward(panel.x)
    p = p.astype(float)
    u = p * panel.lev
    prev = np.r_[0.0, u[:-1]]
    prev[panel.first] = 0.0
    du = u - prev
    R = u * panel.next_ret - panel.cost * np.abs(du) - panel.fin * np.abs(u)
    n = len(R)
    mu, sd = R.mean(), R.std()
    if sd <= 0:
        return 0.0, {k: np.zeros_like(v) for k, v in model.params.items()}
    S = mu / sd * np.sqrt(252)
    dS_dR = (1.0 / sd - mu * (R - mu) / sd ** 3) / n * np.sqrt(252)
    sgn = np.sign(du)
    dR_du = panel.next_ret - panel.cost * sgn - panel.fin * np.sign(u)
    nxt = np.r_[sgn[1:], 0.0] * np.r_[panel.cost[1:], 0.0]       # 翌日の売買コストは今日の u にも依る
    nxt[np.r_[panel.first[1:], True]] = 0.0
    nxt_dS = np.r_[dS_dR[1:], 0.0]
    g_u = dS_dR * dR_du + nxt_dS * nxt
    g_z = (g_u * panel.lev * (1 - p * p)).astype(np.float32)
    grads = {}
    if model.hidden:
        grads["w2"] = a.T @ g_z
        grads["b2"] = np.array([g_z.sum()], np.float32)
        g_a = np.outer(g_z, model.params["w2"]) * (1 - a * a)
        grads["W1"] = panel.x.T @ g_a
        grads["b1"] = g_a.sum(axis=0)
        for k in ("W1", "w2"):
            grads[k] = grads[k] - 2 * l2 * model.params[k]
    else:
        grads["w"] = panel.x.T @ g_z - 2 * l2 * model.params["w"]
        grads["b"] = np.array([g_z.sum()], np.float32)
    return float(S), grads


def fit_policy(train: Panel, valid: Panel | None, hidden: int = 0, iters: int = 300, lr: float = 0.01,
               l2: float = 1e-3, seed: int = 0) -> PolicyModel:
    """Adam でシャープを大きくする。valid があれば、valid のシャープが一番良かった時点のパラメータを使う（早めに止める）。"""
    rng = np.random.default_rng(seed)
    model = PolicyModel(hidden, _init(train.x.shape[1], hidden, rng))
    m = {k: np.zeros_like(v) for k, v in model.params.items()}
    v = {k: np.zeros_like(v) for k, v in model.params.items()}
    best, best_params = -np.inf, {k: x.copy() for k, x in model.params.items()}
    for t in range(1, iters + 1):
        s, g = sharpe_and_grad(model, train, l2)
        for k in model.params:
            m[k] = 0.9 * m[k] + 0.1 * g[k]
            v[k] = 0.999 * v[k] + 0.001 * g[k] * g[k]
            step = lr * (m[k] / (1 - 0.9 ** t)) / (np.sqrt(v[k] / (1 - 0.999 ** t)) + 1e-8)
            model.params[k] = (model.params[k] + step).astype(np.float32)     # シャープを大きくする向きに進む
        if valid is not None and (t % 10 == 0 or t == iters):
            vs = ann_sharpe(strategy_returns(model.positions(valid.x), valid))
            model.history.append(vs)
            if np.isfinite(vs) and vs > best:
                best, best_params = vs, {k: x.copy() for k, x in model.params.items()}
    if valid is not None and np.isfinite(best):
        model.params = best_params
    return model


def walk_forward(panel: Panel, first_test_year: int, last_year: int, step: int = 2, hidden: int = 0,
                 iters: int = 300, valid_years: int = 3, embargo_days: int = 5, seed: int = 0,
                 train_mask: np.ndarray | None = None, test_mask: np.ndarray | None = None,
                 log=None) -> np.ndarray:
    """step 年ごとに、それより前のデータだけで学び直し、次の step 年のポジションを出す（学習に使っていない期間だけ）。

    train_mask / test_mask: 学習・検証に使う行（市場を分けて確かめるとき）。検証期間でない行は NaN。
    """
    years = panel.dates.astype("datetime64[Y]").astype(int) + 1970
    out = np.full(len(panel.x), np.nan)
    tr_all = np.ones(len(panel.x), bool) if train_mask is None else train_mask
    te_all = np.ones(len(panel.x), bool) if test_mask is None else test_mask
    for y0 in range(first_test_year, last_year + 1, step):
        cut = np.datetime64(f"{y0}-01-01") - np.timedelta64(embargo_days, "D")
        vcut = np.datetime64(f"{y0 - valid_years}-01-01")
        train = panel.take(tr_all & (panel.dates < vcut))
        valid = panel.take(tr_all & (panel.dates >= vcut) & (panel.dates < cut))
        if len(train.x) < 1000:
            continue
        model = fit_policy(train, valid if len(valid.x) > 250 else None, hidden=hidden, iters=iters, seed=seed)
        test = te_all & (years >= y0) & (years < y0 + step)
        out[test] = model.positions(panel.x[test])
        if log:
            log(f"  {y0}〜{min(y0 + step - 1, last_year)}: {len(train.x):,} 行で学習")
    return out


def shift_targets(panel: Panel, rng) -> Panel:
    """偶然との比較用: 市場ごとに、翌日のリターンの並びを時間方向にずらす（入力との関係だけを壊す）。"""
    nr = panel.next_ret.copy()
    for mi in np.unique(panel.market):
        idx = np.flatnonzero(panel.market == mi)
        if len(idx) > 10:
            nr[idx] = np.roll(nr[idx], int(rng.uniform(0.2, 0.8) * len(idx)))
    return Panel(panel.x, nr, panel.lev, panel.cost, panel.fin, panel.market, panel.dates, panel.keys, panel.first)


def split_markets(keys: list[str], groups: dict[str, str], seed: int = 20261009) -> tuple[set[str], set[str]]:
    """資産クラスごとに市場を半分ずつに分ける（学習に使っていない市場で確かめるため）。"""
    rng = np.random.default_rng(seed)
    a, b = set(), set()
    for g in dict.fromkeys(groups[k] for k in keys):
        members = [k for k in keys if groups[k] == g]
        rng.shuffle(members)
        a.update(members[0::2])
        b.update(members[1::2])
    return a, b
