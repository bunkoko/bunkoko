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
# 他の通貨（docs/crypto.md 9 章）。GMO の取引所（レバレッジ）で扱う通貨のうち BTC・ETH 以外の 9 つ（組は固定）。
# 新しい 6 通貨の記号の綴りは推測を含むので、毎回 /public/v1/symbols の実物の一覧と照らし、無い記号は飛ばして知らせる
EXTRA = {   # 記号: (通貨, 名前, 数え始め（扱いの始まりの翌日）, Yahoo)
    "BCH_JPY": ("BCH", "ビットコインキャッシュ", "2019-02-01", "BCH-USD"),
    "LTC_JPY": ("LTC", "ライトコイン", "2019-02-01", "LTC-USD"),
    "XRP_JPY": ("XRP", "リップル", "2019-02-01", "XRP-USD"),
    "DOT_JPY": ("DOT", "ポルカドット", "2024-04-14", "DOT-USD"),
    "ATOM_JPY": ("ATOM", "コスモス", "2024-04-14", "ATOM-USD"),
    "ADA_JPY": ("ADA", "カルダノ", "2024-04-14", "ADA-USD"),
    "LINK_JPY": ("LINK", "チェーンリンク", "2024-04-14", "LINK-USD"),
    "DOGE_JPY": ("DOGE", "ドージコイン", "2024-04-14", "DOGE-USD"),
    "SOL_JPY": ("SOL", "ソラナ", "2024-04-14", "SOL-USD"),
}
EXTRA_COST = 0.002          # 他の通貨の往復コストの見込み（板が薄いので BTC・ETH の 2 倍）
EXTRA_MAX_SPREAD = 0.004    # 記録したスプレッドの中央値がこれを超える通貨は、先の判定の組から外す
EXTRA_PAUSE = 0.5           # 他の通貨の問い合わせの間（秒）
EXTRA_FROM, EXTRA_SPLIT = "2019-02-01", "2023-01-01"     # 9.3 の過去の検証: 数え始めと前半・後半の境目
EXTRA_TRIALS = 84           # 9.3: 81 ＋ 1C・案 2（事前登録済み）＋ この検証


# --------------------------------------------------------------------------- GMO の公開 API
def klines_url(symbol: str, year: int) -> str:
    return f"{GMO_PUBLIC}/klines?symbol={symbol}&interval=1day&date={year}"


def ticker_url(symbol: str) -> str:
    return f"{GMO_PUBLIC}/ticker?symbol={symbol}"


def symbols_url() -> str:
    return f"{GMO_PUBLIC}/symbols"


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


def parse_symbols(raw: bytes) -> dict[str, dict]:
    """取引ルール（/public/v1/symbols）。記号ごとの最小の数量・刻み。"""
    out = {}
    for r in _data(raw) or []:
        if "symbol" not in r:
            continue
        out[str(r["symbol"])] = {k: pd.to_numeric(r.get(k), errors="coerce") for k in ("minOrderSize", "sizeStep")}
    return out


# --------------------------------------------------------------------------- 置き場
class PaperStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    @property
    def extra_log_path(self) -> Path:
        return self.root / "paper" / "log_extra.csv"

    @property
    def extra_symbols_path(self) -> Path:
        return self.root / "paper" / "extra_symbols.json"

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

    def load_log(self, extra: bool = False) -> pd.DataFrame:
        p = self.extra_log_path if extra else self.log_path
        if not p.exists():
            return pd.DataFrame(columns=LOG_COLUMNS)
        df = pd.read_csv(p)
        for c in ("logged_at", "bar_time"):
            df[c] = pd.to_datetime(df[c], utc=True)
        return df

    def append_log(self, rows: list[dict], extra: bool = False) -> None:
        """追記だけ（書き換えない）。extra は他の通貨の記録（log_extra.csv。9 章）。"""
        if not rows:
            return
        p = self.extra_log_path if extra else self.log_path
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        out = pd.DataFrame(rows, columns=LOG_COLUMNS)
        for c in ("logged_at", "bar_time"):
            out[c] = pd.to_datetime(out[c], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(p, "a", encoding="utf-8", newline="") as f:
            out.to_csv(f, header=new, index=False)


# --------------------------------------------------------------------------- 戦略を当てる
def instrument(symbol: str) -> Instrument:
    if symbol in SYMBOLS:
        key, label, min_qty = SYMBOLS[symbol]
    else:                     # 他の通貨: 向きだけを見るので、数量の最小単位は気にしない（9 章）
        key, label, min_qty = EXTRA[symbol][0], EXTRA[symbol][1], 1e-8
    return Instrument(symbol=key, description=f"{label}（GMOコイン {symbol}）", unit=key, min_qty=min_qty,
                      qty_step=min_qty, max_qty=1e6, margin_rate=VENUE.margin, spread=0.0, slippage=0.0, tick_size=1.0,
                      financing_long=VENUE.financing, financing_short=VENUE.financing, cluster="crypto", quote_ccy="JPY")


def frame(klines: pd.DataFrame, until: pd.Timestamp, cost: float = VENUE.cost) -> pd.DataFrame:
    """確定した足だけ（until までに終わった足）を、バックテストの形に。コストは往復の割合を足ごとのスプレッドで持つ。"""
    k = klines[klines["time"] + BAR <= until]
    f = pd.DataFrame({c: k[c].to_numpy(float) for c in ("open", "high", "low", "close")},
                     index=pd.DatetimeIndex(k["time"]))
    f["spread"] = cost * f["close"]              # tick 1 円なので、価格 × 割合がそのままスプレッド（円）
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


# --------------------------------------------------------------------------- 他の通貨の毎朝の記録（9 章）
def extra_available(get: Callable[[str], tuple[int, bytes]] = http_get) -> dict:
    """GMO の実物の通貨の一覧と照らす。ok: 記録する、missing: 一覧に無い（飛ばす）、new: 9 章の表に無い新しい記号（記録しない）。"""
    status, raw = get(symbols_url())
    if status != 200:
        raise RuntimeError(f"通貨の一覧を取れなかった（HTTP {status}）")
    have = parse_symbols(raw)
    return {"ok": [s for s in EXTRA if s in have], "missing": [s for s in EXTRA if s not in have],
            "new": sorted(s for s in have if s.endswith("_JPY") and s not in EXTRA and s not in SYMBOLS)}


def fetch_klines(store: PaperStore, sym: str, first_year: int, now: pd.Timestamp,
                 get: Callable[[str], tuple[int, bytes]], pause: Callable[[], None] = lambda: None) -> None:
    have = store.load_klines(sym)
    first = int(have["time"].iloc[-1].year) if len(have) else first_year
    for year in range(first, now.year + 1):
        pause()
        status, raw = get(klines_url(sym, year))
        if status == 404:
            continue
        if status != 200:
            raise RuntimeError(f"{sym} の日足を取れなかった（{year} 年、HTTP {status}）")
        got = parse_klines(raw)
        if len(got):
            store.save_klines(sym, got)
    if store.load_klines(sym).empty:
        raise RuntimeError(f"{sym} の日足が 1 本も取れなかった（{first_year}〜{now.year} 年）")


def paper_extra_run(store: PaperStore, get: Callable[[str], tuple[int, bytes]] = http_get,
                    now: pd.Timestamp | None = None, equity: float = 1_000_000.0,
                    log: Callable[[str], None] = print, sleep: Callable[[float], None] | None = None) -> dict:
    """他の通貨の記録（9 章）。通貨ごとに別々に向きを決め、log_extra.csv に追記する。1 つの通貨の失敗は他に広げない。"""
    import time

    now = now or pd.Timestamp.now(tz="UTC")
    wait = sleep if sleep is not None else time.sleep

    def pause() -> None:
        wait(EXTRA_PAUSE)

    avail = extra_available(get)
    if avail["missing"]:
        log(f"他の通貨: GMO の一覧に無いので飛ばす → {'・'.join(avail['missing'])}")
    if avail["new"]:
        log(f"他の通貨: 9 章の表に無い新しい記号（記録しない。足すなら docs に書いてから）→ {'・'.join(avail['new'])}")
    done = store.load_log(extra=True)
    rows, problems, decisions = [], [], {}
    for sym in avail["ok"]:
        try:
            fetch_klines(store, sym, int(EXTRA[sym][2][:4]), now, get, pause)
            pause()
            status, raw = get(ticker_url(sym))
            if status != 200:
                raise RuntimeError(f"値段を取れなかった（HTTP {status}）")
            t = parse_ticker(raw)
            d = decide({sym: frame(store.load_klines(sym), now, EXTRA_COST)}, equity).get(sym)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{sym}: {str(e)[:160]}")
            log(f"⚠ 他の通貨 {sym}: {str(e)[:160]}")
            continue
        if d is None:
            log(f"{sym}: 足がまだ足りない（{len(store.load_klines(sym))} 本。130 本から）")
            continue
        decisions[sym] = d
        if ((done["symbol"] == sym) & (done["bar_time"] == d.bar_time)).any():
            log(f"{sym}: {d.bar_time:%Y-%m-%d} の分は記録済み")
            continue
        rows.append({"logged_at": now, "symbol": sym, "bar_time": d.bar_time, "close": d.close, "position": d.position,
                     "qty": d.qty, "bid": t["bid"], "ask": t["ask"], "spread_pct": t["spread_pct"]})
        word = {1: "買い", -1: "売り", 0: "なし"}[d.position]
        log(f"{sym}: 足 {d.bar_time:%Y-%m-%d}（終値 {d.close:,.4g} 円）→ 次の足は {word}。スプレッド {t['spread_pct']:.3%}")
    store.append_log(rows, extra=True)
    info = {"checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), **avail, "problems": problems, "logged": len(rows)}
    store.extra_symbols_path.parent.mkdir(parents=True, exist_ok=True)
    store.extra_symbols_path.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return info


# --------------------------------------------------------------------------- 成績
def symbol_returns(log: pd.DataFrame, klines: pd.DataFrame, cost: float = VENUE.cost) -> tuple[pd.Series, int]:
    """記録した向き × 翌日の終値の変化（コスト込み）。戻り値の 2 つ目は記録の無かった日の数。"""
    k = klines.set_index("time")["close"].astype(float)
    g = log.sort_values("bar_time").drop_duplicates("bar_time", keep="first").set_index("bar_time")
    if g.empty:
        return pd.Series(dtype=float), 0
    days = k.index[(k.index >= g.index[0]) & (k.index <= k.index[-1])]
    pos = g["position"].reindex(days).ffill().fillna(0.0)          # 記録の無い日は前の向きのまま
    spread = g["spread_pct"].reindex(days).ffill().fillna(cost)
    gaps = int(sum(1 for d in days[:-1] if d not in g.index))
    ret = k.reindex(days).pct_change().fillna(0.0)
    prev = pos.shift(1).fillna(0.0)
    # 前の日に決めた向き × その日の値動き − 持っている分の保有コスト − 向きを変えた分の片道のスプレッド（決めた日に引く）
    r = prev * ret - prev.abs() * FINANCING_DAY - (pos - prev).abs() * spread / 2
    return r, gaps


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


def same_risk(rets: dict[str, pd.Series]) -> pd.Series:
    """通貨ごとの成績を、直近 60 日の変動でそろえて平均（paper_report と同じまとめ方）。"""
    if not rets:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rets)
    vol = df.rolling(60, min_periods=20).std().shift(1)
    return (df / vol).mean(axis=1, skipna=True).dropna()


def ann_sharpe(r: pd.Series) -> float:
    r = r.dropna()
    return float(r.mean() / r.std() * np.sqrt(365)) if len(r) > 30 and r.std() > 0 else float("nan")


def paper_extra_report(store: PaperStore, now: pd.Timestamp | None = None) -> tuple[list[str], dict]:
    """他の通貨の記録の成績と、先の記録の判定（9.2）。BTC・ETH の判定（6 章）とは別。"""
    now = now or pd.Timestamp.now(tz="UTC")
    lines = ["", "## 他の通貨（docs/crypto.md 9 章。BTC・ETH の判定とは別）"]
    if store.extra_symbols_path.exists():
        a = json.loads(store.extra_symbols_path.read_text(encoding="utf-8"))
        lines.append(f"- 最後に照らした GMO の通貨の一覧（{a.get('checked_at', '?')}）: 記録 {len(a.get('ok', []))} 通貨"
                     + (f"、一覧に無いので飛ばした {'・'.join(a['missing'])}" if a.get("missing") else "")
                     + (f"、表に無い新しい記号 {'・'.join(a['new'])}（記録しない）" if a.get("new") else "")
                     + (f"、取れなかった {len(a['problems'])} 件" if a.get("problems") else ""))
    log = store.load_log(extra=True)
    if log.empty:
        lines.append("- まだ記録が無い（./cfd crypto paper で、BTC・ETH の後に記録する）")
        return lines, {}
    start = log["bar_time"].min()
    rets, info, wide = {}, {}, []
    lines.append("\n| 通貨 | 記録した日 | 向きを変えた回数 | 買い／売り／なしの日 | スプレッド 中央値 | シャープ | 記録の無い日 |")
    lines.append("|---|---|---|---|---|---|---|")
    for sym in EXTRA:
        g = log[log["symbol"] == sym]
        if g.empty:
            continue
        r, gaps = symbol_returns(g, store.load_klines(sym), EXTRA_COST)
        pos = g.sort_values("bar_time")["position"]
        changes = int((pos.diff().fillna(pos.iloc[0]) != 0).sum())
        sp = float(g["spread_pct"].median())
        s = ann_sharpe(r)
        info[sym] = {"days": len(g), "sharpe": s, "spread": sp, "gaps": gaps}
        if np.isfinite(sp) and sp > EXTRA_MAX_SPREAD:
            wide.append(sym)
        else:
            rets[sym] = r
        lines.append(f"| {EXTRA[sym][1]} | {len(g)} | {changes} | {int((pos > 0).sum())}／{int((pos < 0).sum())}／"
                     f"{int((pos == 0).sum())} | {sp:.3%} | {'–' if not np.isfinite(s) else f'{s:.2f}'} | {gaps} |")
    main = store.load_log()
    base = {}
    for sym in SYMBOLS:
        g = main[main["symbol"] == sym] if len(main) else main
        if len(g):
            r, _ = symbol_returns(g, store.load_klines(sym))
            base[sym] = r[r.index >= start]
    a, x = same_risk(base), same_risk(rets)
    b = pd.DataFrame({"a": a, "x": x}).mean(axis=1, skipna=True).dropna() if len(a) and len(x) else pd.Series(dtype=float)
    sa, sx, sb = ann_sharpe(a), ann_sharpe(x), ann_sharpe(b)
    n_days = int(log["bar_time"].nunique())
    gaps = int(max(v["gaps"] for v in info.values())) if info else 0
    info["groups"] = {"btc_eth": sa, "extra": sx, "both": sb, "days": n_days, "gaps": gaps, "wide": wide}

    def f(v: float) -> str:
        return f"{v:.2f}" if np.isfinite(v) else "–"

    lines.append(f"\n- 同じ期間（{start:%Y-%m-%d}〜）のシャープ: BTC・ETH の組 {f(sa)}、他の通貨の組 {f(sx)}、"
                 f"2 つの組を同じリスクで足すと {f(sb)}。記録 {n_days} 日")
    if wide:
        lines.append(f"- スプレッドの中央値が往復 {EXTRA_MAX_SPREAD:.1%} を超えたので組から外した: {'・'.join(wide)}")
    if n_days < MIN_DAYS:
        verdict = f"途中経過（判定は {MIN_DAYS} 日たまってから。あと {MIN_DAYS - n_days} 日）"
    elif gaps > MAX_GAP_DAYS:
        verdict = f"無効（記録の無い日が {gaps} 日。{MAX_GAP_DAYS} 日を超えたので翌年にやり直す）"
    elif np.isfinite(sx) and sx >= 0 and np.isfinite(sb) and np.isfinite(sa) and sb >= sa:
        verdict = "**先の記録でも条件を満たす**（他の通貨の組のシャープ 0 以上、足すと BTC・ETH の組だけ以上）"
    else:
        verdict = "**先の記録では条件を満たさない**（少額の本番には足さない）"
    info["verdict"] = verdict
    lines.append(f"- 先の記録の判定: {verdict}")
    lines.append("- 少額の本番に足すのは、9.3 の過去の検証が「足す候補」で、かつここが「満たす」ときだけ（docs/crypto.md 9.2）")
    return lines, info


# --------------------------------------------------------------------------- 9.3 の過去の検証の判定
def extra_judge(base: dict[str, pd.Series], extra: dict[str, pd.Series], split: str = EXTRA_SPLIT,
                n_trials: int = EXTRA_TRIALS) -> dict:
    """base: BTC・ETH の日ごとの成績、extra: 他の通貨の日ごとの成績（どちらも数え始めから）。docs/crypto.md 9.3 の 3 条件。"""
    from .stats import expected_max_z, moments, psr
    from .universe import group_portfolio, portfolio, sharpe

    returns = pd.DataFrame({**base, **extra}).sort_index()
    a = portfolio(returns, list(base))
    x = portfolio(returns, list(extra))
    b = group_portfolio(returns, {"btc_eth": list(base), "extra": list(extra)}, ["btc_eth", "extra"])
    cut = pd.Timestamp(split)

    def halves(r: pd.Series) -> dict:
        r = r.dropna()
        m = moments(r.to_numpy())
        t = m[0] * np.sqrt(m[1]) if np.isfinite(m[0]) else float("nan")
        return {"first": sharpe(r[r.index < cut]), "second": sharpe(r[r.index >= cut]), "full": sharpe(r),
                "psr": psr(*m) if np.isfinite(m[0]) else float("nan"), "t": float(t), "days": len(r)}

    ha, hx, hb = halves(a), halves(x), halves(b)
    c1 = bool(np.isfinite(hx["first"]) and np.isfinite(hx["second"]) and hx["first"] > 0.3 and hx["second"] > 0.3
              and np.isfinite(hx["psr"]) and hx["psr"] >= 0.95)
    c2 = bool(np.isfinite(hb["first"]) and np.isfinite(ha["first"]) and hb["first"] > ha["first"]
              and np.isfinite(hb["second"]) and np.isfinite(ha["second"]) and hb["second"] > ha["second"])
    bar = expected_max_z(n_trials)
    c3 = bool(np.isfinite(hx["t"]) and hx["t"] >= bar)
    verdict = "足す候補" if c1 and c2 and c3 else ("まだ判断できない" if c1 and c2 else "足さない")
    return {"btc_eth": ha, "extra": hx, "both": hb, "c1": c1, "c2": c2, "c3": c3, "bar": bar, "verdict": verdict,
            "series": {"btc_eth": a, "extra": x, "both": b}}
