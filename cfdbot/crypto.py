"""暗号資産の「記録だけの売買」（docs/crypto.md 6 章）。GMOコインの公開 API（口座不要）を使う。

毎朝（日本時間 6:30）に日足と売り買いの値段を取り、今の戦略が最後の足で決めた「持つ向き」を追記する。
後から `paper_report` で、記録した向き × 翌日の値動きの成績を出す。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .backtest import BacktestConfig, Sleeve, run_backtest
from .instruments import Instrument
from .news import http_get
from .risk import RiskConfig
from .strategies import make_strategy
from .universe import CRYPTO_VENUES

GMO_PUBLIC = "https://api.coin.z.com/public/v1"
SYMBOLS = {"BTC_JPY": ("BTC", "ビットコイン", 0.01), "ETH_JPY": ("ETH", "イーサリアム", 0.1)}   # 最小の数量は GMO の案内
FIRST_YEAR = 2018
BAR = pd.Timedelta(days=1)
VENUE = CRYPTO_VENUES["gmo"]           # 往復 0.1%・建玉に 0.04%/日・2 倍まで（docs/crypto.md 3 章）
FINANCING_DAY = VENUE.financing / 365
LOG_COLUMNS = ["logged_at", "symbol", "bar_time", "close", "position", "qty", "bid", "ask", "spread_pct"]
STRATEGY = ("donchian", {"entry_period": 55, "exit_period": 20, "trend_ema": 0, "buffer_atr": 0.0,
                         "max_extension": 1.5, "extension_period": 20})     # turtle55_d1_ext.json と同じ
MIN_DAYS, MAX_GAP_DAYS, MAX_SPREAD = 250, 30, 0.002


# --------------------------------------------------------------------------- GMO の公開 API
def klines_url(symbol: str, year: int) -> str:
    return f"{GMO_PUBLIC}/klines?symbol={symbol}&interval=1day&date={year}"


def ticker_url(symbol: str) -> str:
    return f"{GMO_PUBLIC}/ticker?symbol={symbol}"


def _data(raw: bytes) -> list | dict:
    d = json.loads(raw.decode("utf-8"))
    if d.get("status") != 0:
        raise RuntimeError(f"GMOコインの API が断った: {str(d.get('messages', d))[:200]}")
    return d["data"]


def parse_klines(raw: bytes) -> pd.DataFrame:
    """日足（openTime はミリ秒の UTC。GMO の 1 日は日本時間の朝 6 時区切り）。"""
    rows = _data(raw)
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["openTime"].astype("int64"), unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["time", "open", "high", "low", "close", "volume"]].dropna(subset=["close"]).sort_values("time")


def parse_ticker(raw: bytes) -> dict:
    rows = _data(raw)
    t = rows[0] if isinstance(rows, list) else rows
    bid, ask = float(t["bid"]), float(t["ask"])
    return {"bid": bid, "ask": ask, "last": float(t.get("last", np.nan)),
            "spread_pct": (ask - bid) / ((ask + bid) / 2) if ask > 0 and bid > 0 else np.nan}


# --------------------------------------------------------------------------- 置き場
class PaperStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def klines_path(self, symbol: str) -> Path:
        return self.root / "klines" / f"{symbol}.csv"

    @property
    def log_path(self) -> Path:
        return self.root / "paper" / "log.csv"

    def load_klines(self, symbol: str) -> pd.DataFrame:
        p = self.klines_path(symbol)
        if not p.exists():
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        df = pd.read_csv(p)
        df["time"] = pd.to_datetime(df["time"], utc=True)
        return df

    def save_klines(self, symbol: str, new: pd.DataFrame) -> int:
        """足して保存（同じ時刻は新しい方）。増えた本数を返す。"""
        old = self.load_klines(symbol)
        both = pd.concat([old, new], ignore_index=True) if len(old) else new
        both = both.drop_duplicates("time", keep="last").sort_values("time")
        p = self.klines_path(symbol)
        p.parent.mkdir(parents=True, exist_ok=True)
        out = both.copy()
        out["time"] = out["time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        out.to_csv(p, index=False)
        return len(both) - len(old)

    def load_log(self) -> pd.DataFrame:
        p = self.log_path
        if not p.exists():
            return pd.DataFrame(columns=LOG_COLUMNS)
        df = pd.read_csv(p)
        for c in ("logged_at", "bar_time"):
            df[c] = pd.to_datetime(df[c], utc=True)
        return df

    def append_log(self, rows: list[dict]) -> None:
        """追記だけ（書き換えない）。"""
        if not rows:
            return
        p = self.log_path
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        out = pd.DataFrame(rows, columns=LOG_COLUMNS)
        for c in ("logged_at", "bar_time"):
            out[c] = pd.to_datetime(out[c], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(p, "a", encoding="utf-8", newline="") as f:
            out.to_csv(f, header=new, index=False)


# --------------------------------------------------------------------------- 戦略を当てる
def instrument(symbol: str) -> Instrument:
    key, label, min_qty = SYMBOLS[symbol]
    return Instrument(symbol=key, description=f"{label}（GMOコイン {symbol}）", unit=key, min_qty=min_qty,
                      qty_step=min_qty, max_qty=1e6, margin_rate=VENUE.margin, spread=0.0, slippage=0.0, tick_size=1.0,
                      financing_long=VENUE.financing, financing_short=VENUE.financing, cluster="crypto", quote_ccy="JPY")


def frame(klines: pd.DataFrame, until: pd.Timestamp) -> pd.DataFrame:
    """確定した足だけ（until までに終わった足）を、バックテストの形に。コストは往復の割合を足ごとのスプレッドで持つ。"""
    k = klines[klines["time"] + BAR <= until]
    f = pd.DataFrame({c: k[c].to_numpy(float) for c in ("open", "high", "low", "close")},
                     index=pd.DatetimeIndex(k["time"]))
    f["spread"] = VENUE.cost * f["close"]        # tick 1 円なので、価格 × 割合がそのままスプレッド（円）
    return f


@dataclass
class Decision:
    symbol: str
    bar_time: pd.Timestamp
    close: float
    position: int       # 次の足で持つ向き（+1 買い・−1 売り・0 なし）
    qty: float


def decide(frames: dict[str, pd.DataFrame], equity: float) -> dict[str, Decision]:
    """最後の足の終値で決めた向き。最後に「値の動かない足」を 1 本足して、そこで約定したことにして読む。"""
    data, insts = {}, {}
    for sym, f in frames.items():
        if len(f) < 130:          # 55 日の高値安値＋余裕（ウォームアップ）
            continue
        last = f.index[-1]
        extra = pd.DataFrame({"open": f["close"].iloc[-1], "high": f["close"].iloc[-1], "low": f["close"].iloc[-1],
                              "close": f["close"].iloc[-1], "spread": f["spread"].iloc[-1]}, index=[last + BAR])
        data[sym] = pd.concat([f, extra])
        insts[sym] = instrument(sym)
    if not data:
        return {}
    cfg = BacktestConfig(initial_equity=equity, fx_rate=1.0, timeframe=BAR,
                         risk=replace(RiskConfig(), max_drawdown_halt=1.0, cluster_max_risk={"crypto": 0.02}))
    cfg = replace(cfg, filters=replace(cfg.filters, no_entry_after_fri_et=None, max_spread_mult=float("inf"),
                                       oil_events=False))
    sleeves = [Sleeve(insts[s].symbol, make_strategy(STRATEGY[0], **STRATEGY[1])) for s in data]
    res = run_backtest({insts[s].symbol: d for s, d in data.items()}, {insts[s].symbol: insts[s] for s in data},
                       sleeves, cfg)
    open_at_end = res.trades[res.trades["reason"] == "end"] if len(res.trades) else res.trades
    out = {}
    for sym, f in frames.items():
        if sym not in data:
            continue
        key = insts[sym].symbol
        t = open_at_end[open_at_end["symbol"] == key] if len(open_at_end) else open_at_end
        side, qty = (int(t["side"].iloc[-1]), float(t["qty"].iloc[-1])) if len(t) else (0, 0.0)
        out[sym] = Decision(sym, f.index[-1], float(f["close"].iloc[-1]), side, qty)
    return out


# --------------------------------------------------------------------------- 毎朝の記録
def paper_run(store: PaperStore, get: Callable[[str], tuple[int, bytes]] = http_get,
              now: pd.Timestamp | None = None, equity: float = 1_000_000.0,
              log: Callable[[str], None] = print) -> list[Decision]:
    now = now or pd.Timestamp.now(tz="UTC")
    done = store.load_log()
    frames, tickers = {}, {}
    for sym in SYMBOLS:
        have = store.load_klines(sym)
        first = int(have["time"].iloc[-1].year) if len(have) else FIRST_YEAR
        for year in range(first, now.year + 1):
            status, raw = get(klines_url(sym, year))
            if status == 404:              # その年の分が無い（取り扱いの始まる前の年）
                continue
            if status != 200:
                raise RuntimeError(f"{sym} の日足を取れなかった（{year} 年、HTTP {status}）")
            got = parse_klines(raw)
            if len(got):
                store.save_klines(sym, got)
        if store.load_klines(sym).empty:
            raise RuntimeError(f"{sym} の日足が 1 本も取れなかった（{FIRST_YEAR}〜{now.year} 年）")
        status, raw = get(ticker_url(sym))
        if status != 200:
            raise RuntimeError(f"{sym} の値段を取れなかった（HTTP {status}）")
        tickers[sym] = parse_ticker(raw)
        frames[sym] = frame(store.load_klines(sym), now)
    decisions = decide(frames, equity)
    rows = []
    for sym, d in decisions.items():
        if ((done["symbol"] == sym) & (done["bar_time"] == d.bar_time)).any():
            log(f"{sym}: {d.bar_time:%Y-%m-%d} の分は記録済み")
            continue
        t = tickers[sym]
        rows.append({"logged_at": now, "symbol": sym, "bar_time": d.bar_time, "close": d.close, "position": d.position,
                     "qty": d.qty, "bid": t["bid"], "ask": t["ask"], "spread_pct": t["spread_pct"]})
        word = {1: "買い", -1: "売り", 0: "なし"}[d.position]
        log(f"{sym}: 足 {d.bar_time:%Y-%m-%d}（終値 {d.close:,.0f} 円）→ 次の足は {word}"
            f"（{d.qty} {SYMBOLS[sym][0]}）。スプレッド {t['spread_pct']:.3%}")
    store.append_log(rows)
    return list(decisions.values())


# --------------------------------------------------------------------------- 成績
def symbol_returns(log: pd.DataFrame, klines: pd.DataFrame) -> tuple[pd.Series, int]:
    """記録した向き × 翌日の終値の変化（コスト込み）。戻り値の 2 つ目は記録の無かった日の数。"""
    k = klines.set_index("time")["close"].astype(float)
    g = log.sort_values("bar_time").drop_duplicates("bar_time", keep="first").set_index("bar_time")
    if g.empty:
        return pd.Series(dtype=float), 0
    days = k.index[(k.index >= g.index[0]) & (k.index <= k.index[-1])]
    pos = g["position"].reindex(days).ffill().fillna(0.0)          # 記録の無い日は前の向きのまま
    spread = g["spread_pct"].reindex(days).ffill().fillna(VENUE.cost)
    gaps = int(sum(1 for d in days[:-1] if d not in g.index))
    ret = k.reindex(days).pct_change().fillna(0.0)
    prev = pos.shift(1).fillna(0.0)
    # 前の日に決めた向き × その日の値動き − 持っている分の保有コスト − 向きを変えた分の片道のスプレッド（決めた日に引く）
    r = prev * ret - prev.abs() * FINANCING_DAY - (pos - prev).abs() * spread / 2
    return r, gaps


def paper_equity(store: PaperStore, start: float = 1_000_000.0) -> pd.Series:
    """記録上の資産（円）の推移。記録した向き × 数量 × 終値の変化から、保有コストと向きを変えた分のスプレッドを引く。
    数量は記録した時の資金（paper_run の equity）で決めたものなので、start にはその資金を渡す。"""
    pnl = []
    log = store.load_log()
    for sym in SYMBOLS:
        g = log[log["symbol"] == sym]
        if g.empty:
            continue
        k = store.load_klines(sym).set_index("time")["close"].astype(float)
        g = g.sort_values("bar_time").drop_duplicates("bar_time", keep="first").set_index("bar_time")
        days = k.index[(k.index >= g.index[0]) & (k.index <= k.index[-1])]
        if len(days) == 0:
            continue
        q = (g["position"] * g["qty"]).astype(float).reindex(days).ffill().fillna(0.0)   # 記録の無い日は前のまま
        spread = g["spread_pct"].reindex(days).ffill().fillna(VENUE.cost)
        c = k.reindex(days)
        prev = q.shift(1).fillna(0.0)
        r = prev * c.diff().fillna(0.0) - prev.abs() * c.shift(1).fillna(c) * FINANCING_DAY \
            - (q - prev).abs() * c * spread / 2
        pnl.append(r.rename(sym))
    if not pnl:
        return pd.Series(dtype=float)
    return start + pd.concat(pnl, axis=1).fillna(0.0).sum(axis=1).cumsum()


def drawdown(equity: pd.Series) -> float:
    """最高値からの今の下落（0〜1）。"""
    if equity.empty:
        return 0.0
    peak = float(equity.cummax().iloc[-1])
    return max(0.0, 1.0 - float(equity.iloc[-1]) / peak) if peak > 0 else 1.0


def paper_report(store: PaperStore, now: pd.Timestamp | None = None) -> tuple[list[str], dict]:
    now = now or pd.Timestamp.now(tz="UTC")
    log = store.load_log()
    lines = ["# 記録だけの売買（暗号資産）", f"作成: {now:%Y-%m-%d %H:%M}　判定の条件: docs/crypto.md 6 章"]
    if log.empty:
        lines.append("- まだ記録が無い（./cfd crypto paper）")
        return lines, {}
    rets, info = {}, {}
    lines.append("\n| 銘柄 | 記録した日 | 向きを変えた回数 | 買い／売り／なしの日 | スプレッド 中央値 | シャープ | 記録の無い日 |")
    lines.append("|---|---|---|---|---|---|---|")
    for sym in SYMBOLS:
        g = log[log["symbol"] == sym]
        if g.empty:
            continue
        r, gaps = symbol_returns(g, store.load_klines(sym))
        rets[sym] = r
        pos = g.sort_values("bar_time")["position"]
        changes = int((pos.diff().fillna(pos.iloc[0]) != 0).sum())
        sp = float(g["spread_pct"].median())
        s = float(r.mean() / r.std() * np.sqrt(365)) if len(r) > 30 and r.std() > 0 else np.nan
        info[sym] = {"days": len(g), "sharpe": s, "spread": sp, "gaps": gaps}
        lines.append(f"| {SYMBOLS[sym][1]} | {len(g)} | {changes} | {int((pos > 0).sum())}／{int((pos < 0).sum())}／"
                     f"{int((pos == 0).sum())} | {sp:.3%} | {s:.2f} | {gaps} |" if np.isfinite(s) else
                     f"| {SYMBOLS[sym][1]} | {len(g)} | {changes} | {int((pos > 0).sum())}／{int((pos < 0).sum())}／"
                     f"{int((pos == 0).sum())} | {sp:.3%} | – | {gaps} |")
    if not rets:
        return lines, info
    df = pd.DataFrame(rets)
    vol = df.rolling(60, min_periods=20).std().shift(1)
    both = (df / vol).mean(axis=1, skipna=True).dropna()
    s_both = float(both.mean() / both.std() * np.sqrt(365)) if len(both) > 30 and both.std() > 0 else np.nan
    n_days = int(max(v["days"] for v in info.values()))
    gaps = int(max(v["gaps"] for v in info.values()))
    spread = float(np.median([v["spread"] for v in info.values()]))
    info["both"] = {"days": n_days, "sharpe": s_both, "spread": spread, "gaps": gaps}
    lines.append(f"\n- まとめ（同じリスク）: シャープ {s_both:.2f}、記録 {n_days} 日、スプレッドの中央値 {spread:.3%}"
                 if np.isfinite(s_both) else f"\n- まとめ: 記録 {n_days} 日（30 日たまると成績を出す）")
    if n_days < MIN_DAYS:
        verdict = f"途中経過（判定は {MIN_DAYS} 日たまってから。あと {MIN_DAYS - n_days} 日）"
    elif gaps > MAX_GAP_DAYS:
        verdict = f"無効（記録の無い日が {gaps} 日。{MAX_GAP_DAYS} 日を超えたので翌年にやり直す）"
    elif np.isfinite(s_both) and s_both >= 0 and spread <= MAX_SPREAD:
        verdict = "**2028-01 に少額で始めてよい**（シャープ 0 以上、スプレッド 0.2% 以下）"
    else:
        verdict = "**始めない。もう 1 年記録する**（シャープ 0 未満、またはスプレッド 0.2% 超）"
    info["verdict"] = verdict
    lines.append(f"- 判定: {verdict}")
    return lines, info
