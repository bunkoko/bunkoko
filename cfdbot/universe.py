"""売買の対象を商品以外（FX・株価指数・国債・個別株）に広げたときの研究（./cfd universe）。

同じ戦略（今の構成のまま、銘柄ごとに調整しない）を多くの市場に当て、次を確かめる:
  - 前半（〜2013）に良かった市場は、後半（2014〜）も良いか（過去の成績で選ぶ意味があるか）
  - どの資産クラスで効くか
  - 今の 4 商品に別の資産クラスを足すと、まとめた成績が良くなるか（分散）

データは Yahoo の日足（先物・為替・指数・株価）。設定と判定の条件は docs/universe.md（結果を見る前に決めたもの）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .events import trading_day_keys
from .instruments import Instrument
from .metrics import daily_equity


@dataclass(frozen=True)
class Market:
    key: str
    ticker: str          # Yahoo のティッカー
    group: str           # 資産クラス（GROUPS のキー）
    label: str


@dataclass(frozen=True)
class Group:
    label: str
    cost: float          # 往復コスト（価格に対する割合。スプレッド・手数料・滑りの合計の目安）
    financing: float     # 保有コスト（年率、名目に対して。買い・売りとも支払いとして扱う）
    margin: float        # 証拠金率
    weekend: bool = False  # 土日も取引される（金曜〜週末の新規停止を使わない）


GROUPS = {
    "core": Group("今の 4 商品", 0.0008, 0.03, 0.05),
    "commodity": Group("他の商品", 0.0008, 0.03, 0.05),
    "fx": Group("為替", 0.00015, 0.01, 0.04),
    "index": Group("株価指数", 0.0003, 0.03, 0.10),
    "bond": Group("国債先物", 0.0003, 0.02, 0.05),
    "jp_stock": Group("日本株", 0.0015, 0.03, 0.20),
    "us_stock": Group("米国株", 0.0015, 0.03, 0.20),
    # 国内の暗号資産の証拠金取引・CFD の目安: 往復 0.2%、建玉に 1 日 0.04%（年 14.6%）、レバレッジ 2 倍まで
    "crypto": Group("暗号資産", 0.002, 0.146, 0.50, weekend=True),
}
# 暗号資産の参考: コストの小さい場合（海外の大きな取引所に近い。国内の個人には使えないことが多い）
CRYPTO_LOW_COST = Group("暗号資産（低コストの場合）", 0.0006, 0.05, 0.50, weekend=True)
# 暗号資産は Yahoo のデータが 2014 年からなので、前半・後半を自分の期間の中ほどで分ける
CRYPTO_SPLIT = "2020-07-01"

_M = Market
UNIVERSE: tuple[Market, ...] = (
    # 今の 4 商品（先物）
    _M("GOLD", "GC=F", "core", "金"), _M("SILVER", "SI=F", "core", "銀"),
    _M("WTI", "CL=F", "core", "WTI 原油"), _M("BRENT", "BZ=F", "core", "ブレント原油"),
    # 他の商品（先物）
    _M("COPPER", "HG=F", "commodity", "銅"), _M("PLATINUM", "PL=F", "commodity", "プラチナ"),
    _M("PALLADIUM", "PA=F", "commodity", "パラジウム"), _M("NATGAS", "NG=F", "commodity", "天然ガス"),
    _M("GASOLINE", "RB=F", "commodity", "ガソリン"), _M("CORN", "ZC=F", "commodity", "トウモロコシ"),
    _M("WHEAT", "ZW=F", "commodity", "小麦"), _M("SOYBEAN", "ZS=F", "commodity", "大豆"),
    _M("COFFEE", "KC=F", "commodity", "コーヒー"), _M("SUGAR", "SB=F", "commodity", "砂糖"),
    # 為替
    _M("USDJPY", "JPY=X", "fx", "ドル円"), _M("EURUSD", "EURUSD=X", "fx", "ユーロドル"),
    _M("GBPUSD", "GBPUSD=X", "fx", "ポンドドル"), _M("AUDUSD", "AUDUSD=X", "fx", "豪ドル米ドル"),
    _M("USDCAD", "CAD=X", "fx", "米ドルカナダドル"), _M("USDCHF", "CHF=X", "fx", "米ドルスイスフラン"),
    _M("NZDUSD", "NZDUSD=X", "fx", "NZドル米ドル"), _M("EURJPY", "EURJPY=X", "fx", "ユーロ円"),
    _M("GBPJPY", "GBPJPY=X", "fx", "ポンド円"), _M("AUDJPY", "AUDJPY=X", "fx", "豪ドル円"),
    # 株価指数
    _M("SP500", "^GSPC", "index", "S&P500"), _M("NASDAQ100", "^NDX", "index", "ナスダック100"),
    _M("DOW", "^DJI", "index", "ダウ"), _M("RUSSELL", "^RUT", "index", "ラッセル2000"),
    _M("NIKKEI", "^N225", "index", "日経平均"), _M("DAX", "^GDAXI", "index", "独DAX"),
    _M("FTSE", "^FTSE", "index", "英FTSE100"), _M("STOXX50", "^STOXX50E", "index", "ユーロストックス50"),
    _M("HANGSENG", "^HSI", "index", "香港ハンセン"), _M("ASX200", "^AXJO", "index", "豪ASX200"),
    # 国債先物
    _M("UST5Y", "ZF=F", "bond", "米5年国債先物"), _M("UST10Y", "ZN=F", "bond", "米10年国債先物"),
    _M("UST30Y", "ZB=F", "bond", "米30年国債先物"),
    # 日本株（2001 年ごろに時価総額が大きかった銘柄。今の勝ち組だけを選ばないため）
    _M("TOYOTA", "7203.T", "jp_stock", "トヨタ"), _M("SONY", "6758.T", "jp_stock", "ソニー"),
    _M("NTT", "9432.T", "jp_stock", "NTT"), _M("MUFG", "8306.T", "jp_stock", "三菱UFJ"),
    _M("SOFTBANK", "9984.T", "jp_stock", "ソフトバンクG"), _M("HITACHI", "6501.T", "jp_stock", "日立"),
    _M("HONDA", "7267.T", "jp_stock", "ホンダ"), _M("CANON", "7751.T", "jp_stock", "キヤノン"),
    _M("TAKEDA", "4502.T", "jp_stock", "武田薬品"), _M("NOMURA", "8604.T", "jp_stock", "野村HD"),
    # 米国株（同じく 2001 年ごろの大型株）
    _M("GE", "GE", "us_stock", "GE"), _M("MSFT", "MSFT", "us_stock", "マイクロソフト"),
    _M("XOM", "XOM", "us_stock", "エクソン"), _M("WMT", "WMT", "us_stock", "ウォルマート"),
    _M("CITI", "C", "us_stock", "シティ"), _M("PFE", "PFE", "us_stock", "ファイザー"),
    _M("INTC", "INTC", "us_stock", "インテル"), _M("IBM", "IBM", "us_stock", "IBM"),
    _M("JNJ", "JNJ", "us_stock", "J&J"), _M("AIG", "AIG", "us_stock", "AIG"),
    # 暗号資産（24 時間・土日も動く）
    _M("BTC", "BTC-USD", "crypto", "ビットコイン"), _M("ETH", "ETH-USD", "crypto", "イーサリアム"),
)
BY_KEY = {m.key: m for m in UNIVERSE}


def data_path(root: str | Path, m: Market) -> Path:
    return Path(root) / f"{m.key}.csv"


def market_instrument(m: Market, g: Group | None = None) -> Instrument:
    """研究用の銘柄仕様。数量はほぼ連続（最小単位の影響を避ける）、スプレッドはデータの列（価格に比例）で持つ。"""
    g = g or GROUPS[m.group]
    return Instrument(symbol=m.key, description=m.label, unit="unit", min_qty=1e-6, qty_step=1e-6, max_qty=1e15,
                      margin_rate=g.margin, spread=0.0, slippage=0.0, tick_size=1e-6,
                      financing_long=g.financing, financing_short=g.financing, cluster=m.key)


def with_costs(frame: pd.DataFrame, m: Market, g: Group | None = None) -> pd.DataFrame:
    """往復コストを、足ごとのスプレッド（価格 × 割合。tick 1e-6 単位）として持たせる。"""
    f = frame.copy()
    f["spread"] = (g or GROUPS[m.group]).cost * f["close"] / 1e-6
    return f


def hold_returns(frame: pd.DataFrame) -> pd.Series:
    """持っているだけ（買って持ち続ける）の日ごとの変化率（コストなし。比べる相手として）。"""
    c = frame["close"].astype(float)
    keys = trading_day_keys(c.index + pd.Timedelta(days=1))   # 足の終わりの時刻で、戦略の日ごとの資産と同じ日付にする
    r = pd.Series(c.to_numpy(), index=keys).groupby(level=0).last().pct_change().dropna()
    r.index = pd.DatetimeIndex(r.index)
    return r


def timeframe_costs(hourly: pd.DataFrame, costs: dict[str, float], atr_period: int = 20,
                    stop_atr: float = 2.5) -> pd.DataFrame:
    """時間足ごとの「往復コスト ÷ 損切り幅（stop_atr × ATR）」。check_data の表と同じ考え方。

    hourly: 1 時間足（UTC の index、open/high/low/close）。4 時間足・日足はここから作る。
    """
    rows = []
    for name, rule in (("1 時間足", "1h"), ("4 時間足", "4h"), ("日足", "1D")):
        f = hourly if rule == "1h" else hourly.resample(rule, label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        prev = f["close"].shift(1)
        tr = pd.concat([f["high"] - f["low"], (f["high"] - prev).abs(), (f["low"] - prev).abs()], axis=1).max(axis=1)
        rel = float((tr.rolling(atr_period).mean() / f["close"]).median())
        rows.append({"timeframe": name, "atr_pct": rel,
                     **{k: c / (stop_atr * rel) if rel > 0 else np.nan for k, c in costs.items()}})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- 集計
def day_returns(equity: pd.Series, start: pd.Timestamp | None = None) -> pd.Series:
    """取引日ごとの資産の変化率（日付の index）。"""
    if equity.empty:
        return pd.Series(dtype=float)
    d = daily_equity(equity)
    d.index = pd.DatetimeIndex(d.index).tz_localize(None) if getattr(d.index, "tz", None) else pd.DatetimeIndex(d.index)
    if start is not None:
        s = pd.Timestamp(start)
        d = d[d.index >= (s.tz_convert(None) if s.tzinfo else s)]
    return d.pct_change().dropna()


def sharpe(r: pd.Series) -> float:
    r = r.dropna()
    if len(r) < 60 or r.std() <= 0:
        return float("nan")
    return float(r.mean() / r.std() * np.sqrt(252))


def portfolio(returns: pd.DataFrame, members) -> pd.Series:
    """同じリスクで持った市場の平均（その日に取引の無い市場は除く）。"""
    cols = [c for c in members if c in returns]
    if not cols:
        return pd.Series(dtype=float)
    return returns[cols].mean(axis=1, skipna=True).dropna()


def group_portfolio(returns: pd.DataFrame, groups: dict[str, list[str]], names) -> pd.Series:
    """資産クラスごとの平均を、さらにクラスどうしで平均（クラスごとに同じリスク）。"""
    parts = {n: portfolio(returns, groups[n]) for n in names if groups.get(n)}
    if not parts:
        return pd.Series(dtype=float)
    return pd.DataFrame(parts).mean(axis=1, skipna=True).dropna()


def at_vol(r: pd.Series, target: float = 0.10) -> tuple[float, float]:
    """年率の変動を target にそろえたときの年率リターンと最大DD（分散の効果を比べるため）。"""
    r = r.dropna()
    if len(r) < 60 or r.std() <= 0:
        return float("nan"), float("nan")
    x = r * (target / (r.std() * np.sqrt(252)))
    eq = (1 + x).cumprod()
    years = len(x) / 252
    cagr = eq.iloc[-1] ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0
    mdd = float((1 - eq / eq.cummax()).max())
    return float(cagr), mdd


def rank_corr(a: np.ndarray, b: np.ndarray) -> float:
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 5:
        return float("nan")
    ra = pd.Series(a[ok]).rank().to_numpy()
    rb = pd.Series(b[ok]).rank().to_numpy()
    return float(np.corrcoef(ra, rb)[0, 1])


@dataclass
class Selection:
    n: int                   # 比べた市場の数
    k: int                   # 選んだ数
    rho: float               # 前半と後半のシャープの順位相関
    rho_p: float             # 順位を混ぜたときに rho 以上になる割合（小さいほど偶然ではない）
    picked: list[str]
    sharpe_picked: float     # 後半の、前半の上位 k 市場の平均のシャープ
    sharpe_all: float        # 後半の、全市場の平均のシャープ
    pct_vs_random: float     # 同じ数をでたらめに選んだ場合より良かった割合


def selection_test(sr1: pd.Series, returns_h2: pd.DataFrame, sr2: pd.Series, frac: float = 0.25,
                   draws: int = 2000, seed: int = 20261008) -> Selection | None:
    """前半のシャープで上位を選ぶと、後半に全部やでたらめに選んだ場合より良いか。"""
    keys = [k for k in sr1.index if np.isfinite(sr1[k]) and np.isfinite(sr2.get(k, np.nan))]
    n = len(keys)
    if n < 8:
        return None
    rng = np.random.default_rng(seed)
    a, b = sr1[keys].to_numpy(float), sr2[keys].to_numpy(float)
    rho = rank_corr(a, b)
    perm = np.array([rank_corr(a, rng.permutation(b)) for _ in range(draws)])
    k = max(3, int(round(frac * n)))
    picked = list(sr1[keys].sort_values(ascending=False).index[:k])
    s_pick = sharpe(portfolio(returns_h2, picked))
    rand = np.array([sharpe(portfolio(returns_h2, list(rng.choice(keys, k, replace=False)))) for _ in range(draws)])
    return Selection(n, k, rho, float(np.mean(perm >= rho)), picked, s_pick, sharpe(portfolio(returns_h2, keys)),
                     float(np.mean(rand < s_pick)))
