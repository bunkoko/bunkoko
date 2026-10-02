"""複数銘柄・複数戦略のイベント駆動バックテスト。

約束事（EA の挙動と揃えている）:
- 価格データは Bid。Ask = Bid + スプレッド。買いは Ask で建て Bid で決済、売りはその逆
- シグナルは足の終値で判断し、次の足の始値で成行執行（スリッページ込み）
- 損切り/利確は足の高値・安値で判定。始値で逆指値を飛び越えた（窓開け）場合は始値で約定
- 同じ足で損切りと利確の両方に届いた場合は損切りを優先（保守的）
- 細かい足（fine_data。例: H1 で売買し M5）を渡すと、足の中の値動きを小足ごとにたどって
  損切り・利確の順番と約定価格、時間帯ごとのスプレッドを再現する（足の確定時の判断は変わらない）
- 逆指値の書き換え（建値・トレーリング）は足の確定時のみ
- 金利調整額/キャリングコストは保有時間に比例して日割りで差し引く
- 損益は口座通貨（円）。建値通貨(USD)からの換算に fx_rate を使う
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from . import indicators as ind
from .events import (
    ET,
    TRADING_DAY_ROLL_ET,
    Event,
    EventIndex,
    friday_cutoff_passed,
    recurring_oil_events,
)
from .exits import ExitConfig, ExitState, on_bar_close
from .instruments import Instrument
from .risk import RiskConfig, leverage_room, notional, open_risk, position_size, required_margin
from .strategies.base import Strategy

DAY_NS = 86_400 * 10**9


@dataclass(frozen=True)
class CostModel:
    spread_mult: float = 1.0          # ストレステスト用（2.0 = スプレッド2倍）
    slippage_mult: float = 1.0
    use_data_spread: bool = True      # CSV の spread 列があれば max(仕様値, 実績値) を使う
    commission_per_unit: float = 0.0  # 片道・1単位あたり（建値通貨）
    financing: bool = True


@dataclass(frozen=True)
class FilterConfig:
    oil_events: bool = True                 # API(火)/EIA(水) を自動でイベント登録
    extra_events: tuple[Event, ...] = ()    # FOMC/CPI/雇用統計、祝日でずれた EIA など
    event_block_before: pd.Timedelta = pd.Timedelta(hours=4)  # 発表の何時間前から新規停止
    event_block_after: pd.Timedelta = pd.Timedelta(hours=1)   # 発表の何時間後まで新規停止
    event_flatten_lead: pd.Timedelta | None = None  # flatten_before_events の先読み幅（None=1本分）
    no_entry_after_fri_et: float | None = 12.0      # 金曜この時刻(ET)以降は新規停止（None=無効）
    weekend_flatten_fri_et: float = 16.0            # flatten_before_weekend の締め時刻(ET)
    max_spread_mult: float = 3.0            # 実績スプレッドが仕様値のこの倍を超えたら新規見送り
    max_fill_delay: pd.Timedelta | None = None      # 判断から約定までの許容遅延（None=2本分）


@dataclass
class BacktestConfig:
    initial_equity: float = 1_000_000.0
    fx_rate: float | pd.Series = 150.0      # USD/JPY（定数 or 時系列）
    exit: ExitConfig = field(default_factory=ExitConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostModel = field(default_factory=CostModel)
    filters: FilterConfig = field(default_factory=FilterConfig)
    trade_start: pd.Timestamp | None = None  # これ以前のデータは指標計算（ウォームアップ）のみに使う
    trade_end: pd.Timestamp | None = None
    timeframe: pd.Timedelta | None = None    # None ならデータから推定


@dataclass
class Sleeve:
    """1銘柄 × 1戦略の運用単位。"""

    symbol: str
    strategy: Strategy
    exit_overrides: dict[str, Any] = field(default_factory=dict)
    name: str = ""
    risk_weight: float = 1.0   # 1回の損失 = risk_per_trade × これ（ポートフォリオ学習で配分）
    # 上位足フィルタ: 上位足の終値が EMA より上なら買いだけ、下なら売りだけ（htf_ema=0 で無効）
    htf_frame: pd.DataFrame | None = field(default=None, repr=False)
    htf_ema: int = 0
    htf_timeframe: str = ""    # 表示・EA 書き出し用の名前（"H4" など）

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"{self.symbol}:{self.strategy.name}"

    def exit_config(self, base: ExitConfig, tag: str | None = None) -> ExitConfig:
        return base.with_overrides(self.strategy.exit_overrides(tag)).with_overrides(
            self.exit_overrides
        )


@dataclass
class _Position:
    sleeve: str
    symbol: str
    side: int
    qty: float
    initial_qty: float
    entry_time: pd.Timestamp
    entry: float
    stop: float
    tp: float | None
    cfg: ExitConfig
    tag: str | None
    state: ExitState
    last_accrual: int
    pnl: float = 0.0           # 円（手数料・金利・一部利確を含む）
    price_pnl: float = 0.0     # 建値通貨ベースの値幅損益 × 数量（R 計算用）
    mfe: float = 0.0           # 最大含み益（価格幅）
    mae: float = 0.0           # 最大含み損（価格幅）
    pending_exit: str | None = None


@dataclass
class _Pending:
    sleeve: str
    symbol: str
    side: int
    qty: float
    stop_dist: float
    atr: float
    decided: int               # 判断した足の終了時刻 (ns)
    tag: str | None
    cfg: ExitConfig


@dataclass
class _SymbolData:
    inst: Instrument
    times: np.ndarray          # 足の開始時刻 (ns, UTC)
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    spread: np.ndarray         # 足ごとのスプレッド（価格単位、倍率適用後）
    data_spread: np.ndarray | None
    slip: float
    tf: pd.Timedelta = pd.Timedelta(hours=1)   # この銘柄の時間足
    tf_ns: int = 0
    # 約定の再現に使う細かい足（無ければ None）。fj0[i]:fj1[i] が足 i の中の小足
    ft: np.ndarray | None = None
    fo: np.ndarray | None = None
    fh: np.ndarray | None = None
    fl: np.ndarray | None = None
    fsp: np.ndarray | None = None
    fj0: np.ndarray | None = None
    fj1: np.ndarray | None = None


@dataclass
class _SleeveData:
    sleeve: Sleeve
    sd: _SymbolData
    signals: pd.DataFrame
    entry: np.ndarray
    stop_dist: np.ndarray
    tags: np.ndarray | None
    exit_cols: dict[str, tuple[np.ndarray, np.ndarray]]
    atr: np.ndarray
    warmup: int
    cfg_cache: dict[str | None, ExitConfig]


@dataclass
class _Exposure:
    heat: float                       # 全ポジションの損切り時損失（円）
    cluster_heat: dict[str, float]
    margin: float                     # 必要証拠金（円）
    notional: dict[str, float]        # 銘柄ごとの名目建玉（円）
    notional_total: float


class BacktestResult:
    def __init__(self, trades, equity, rejections, signals, config, timeframe, leverage=None):
        self.trades: pd.DataFrame = trades
        self.equity: pd.Series = equity
        #: 足ごとの実効レバレッジ（名目建玉の合計 ÷ 資産）
        self.leverage: pd.Series = leverage if leverage is not None else pd.Series(dtype=float)
        self.rejections: pd.DataFrame = rejections
        self.signals: dict[str, pd.DataFrame] = signals
        self.config: BacktestConfig = config
        self.timeframe: pd.Timedelta = timeframe

    def metrics(self) -> dict[str, float]:
        from .metrics import compute_metrics

        return compute_metrics(self.equity, self.trades, self.config.initial_equity, self.leverage)

    def summary_by(self, key: str = "sleeve") -> pd.DataFrame:
        from .metrics import trade_stats

        if self.trades.empty:
            return pd.DataFrame()
        return pd.DataFrame(
            {k: trade_stats(g) for k, g in self.trades.groupby(key)}
        ).T


def infer_timeframe(index: pd.DatetimeIndex) -> pd.Timedelta:
    diffs = pd.Series(index[1:] - index[:-1])
    return diffs.mode().iloc[0]


class Backtester:
    def __init__(
        self,
        data: dict[str, pd.DataFrame],
        instruments: dict[str, Instrument],
        sleeves: list[Sleeve],
        config: BacktestConfig | None = None,
        fine_data: dict[str, pd.DataFrame] | None = None,
    ):
        self.cfg = config or BacktestConfig()
        self.fine_data = fine_data or {}
        missing = {s.symbol for s in sleeves} - set(data)
        if missing:
            raise ValueError(f"no data for {sorted(missing)}")
        names = [s.name for s in sleeves]
        if len(set(names)) != len(names):
            raise ValueError("sleeve names must be unique")
        self.data = data
        self.instruments = instruments
        self.sleeves = sleeves
        first = data[sleeves[0].symbol]
        self.tf = self.cfg.timeframe or infer_timeframe(first.index)
        self.tf_ns = self.tf.value

    # ------------------------------------------------------------------ 準備
    def _prepare(self) -> None:
        cfg = self.cfg
        self.sym: dict[str, _SymbolData] = {}
        for symbol in {s.symbol for s in self.sleeves}:
            df = self.data[symbol]
            if df.index.tz is None:
                raise ValueError(f"{symbol}: index must be tz-aware (UTC)")
            inst = self.instruments[symbol]
            base_spread = inst.spread * cfg.costs.spread_mult
            spread = np.full(len(df), base_spread)
            data_spread = None
            if cfg.costs.use_data_spread and "spread" in df:
                data_spread = df["spread"].to_numpy(dtype=float) * inst.point_size
                spread = np.maximum(spread, data_spread * cfg.costs.spread_mult)
            self.sym[symbol] = _SymbolData(
                inst=inst,
                times=df.index.tz_convert("UTC").as_unit("ns").asi8,
                o=df["open"].to_numpy(float),
                h=df["high"].to_numpy(float),
                l=df["low"].to_numpy(float),
                c=df["close"].to_numpy(float),
                spread=spread,
                data_spread=data_spread,
                slip=inst.slippage * cfg.costs.slippage_mult,
            )
            tf = cfg.timeframe or infer_timeframe(df.index)
            self.sym[symbol].tf, self.sym[symbol].tf_ns = tf, tf.value
            fine = self.fine_data.get(symbol)
            if fine is not None and len(fine):
                self._attach_fine(self.sym[symbol], fine, inst)

        atr_cache: dict[tuple[str, int], pd.Series] = {}
        self.sl: dict[str, _SleeveData] = {}
        for s in self.sleeves:
            df = self.data[s.symbol]
            base = s.exit_config(cfg.exit)
            key = (s.symbol, base.atr_period)
            if key not in atr_cache:
                atr_cache[key] = ind.atr(df, base.atr_period)
            atr = atr_cache[key]
            sig = s.strategy.generate(df, atr)
            exit_cols = {}
            for col in sig.columns:
                if col.startswith("exit_long:"):
                    tag = col.split(":", 1)[1]
                    exit_cols[tag] = (
                        sig[col].to_numpy(bool),
                        sig[f"exit_short:{tag}"].to_numpy(bool),
                    )
            exit_cols[None] = (sig["exit_long"].to_numpy(bool), sig["exit_short"].to_numpy(bool))
            warmup = s.strategy.warmup_bars()
            if s.htf_frame is not None and s.htf_ema > 0:
                sig, htf_warm = self._apply_htf_filter(s, sig)
                warmup = max(warmup, htf_warm)
            self.sl[s.name] = _SleeveData(
                sleeve=s,
                sd=self.sym[s.symbol],
                signals=sig,
                entry=sig["entry"].to_numpy(np.int8),
                stop_dist=sig["stop_dist"].to_numpy(float),
                tags=sig["tag"].to_numpy(object) if "tag" in sig else None,
                exit_cols=exit_cols,
                atr=atr.to_numpy(float),
                warmup=warmup,
                cfg_cache={},
            )

        start = min(d.times[0] for d in self.sym.values())
        end = max(d.times[-1] + d.tf_ns for d in self.sym.values())
        events: list[Event] = list(cfg.filters.extra_events)
        if cfg.filters.oil_events:
            events += recurring_oil_events(pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"))
        self.events = EventIndex(events)



    def _fx_series(self, times: np.ndarray) -> np.ndarray:
        """各時刻の USD/JPY（定数 or 時系列の直前の値）。"""
        fx = self.cfg.fx_rate
        if not isinstance(fx, pd.Series):
            return np.full(len(times), float(fx))
        s = fx[~fx.index.duplicated(keep="last")].sort_index()
        if s.index.tz is None:
            s.index = s.index.tz_localize("UTC")
        idx = pd.DatetimeIndex(pd.to_datetime(times, utc=True))
        return s.reindex(idx, method="ffill").bfill().to_numpy(float)

    def _attach_fine(self, d: _SymbolData, fine: pd.DataFrame, inst: Instrument) -> None:
        cfg = self.cfg
        if infer_timeframe(fine.index) >= d.tf:
            raise ValueError(f"{inst.symbol}: 約定の再現に使う足は売買の足より細かくする")
        ft = fine.index.tz_convert("UTC").as_unit("ns").asi8
        sp = np.full(len(fine), inst.spread * cfg.costs.spread_mult)
        if cfg.costs.use_data_spread and "spread" in fine:
            sp = np.maximum(sp, fine["spread"].to_numpy(float) * inst.point_size * cfg.costs.spread_mult)
        d.ft, d.fsp = ft, sp
        d.fo, d.fh, d.fl = (fine[c].to_numpy(float) for c in ("open", "high", "low"))
        d.fj0 = np.searchsorted(ft, d.times, side="left")
        d.fj1 = np.searchsorted(ft, d.times + d.tf_ns, side="left")

    def _apply_htf_filter(self, s: Sleeve, sig: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        """上位足の向きと逆のエントリーを消す。使うのは判断時点で確定済みの上位足だけ。"""
        hf = s.htf_frame
        htf_td = infer_timeframe(hf.index)
        tf = self.sym[s.symbol].tf
        if htf_td <= tf:
            raise ValueError(f"{s.name}: 上位足フィルタの足は売買の足より粗くする")
        close = hf["close"].to_numpy(float)
        trend = np.sign(close - ind.ema(hf["close"], s.htf_ema).to_numpy(float))
        htf_close_time = hf.index.tz_convert("UTC").as_unit("ns").asi8 + htf_td.value
        decide = self.data[s.symbol].index.tz_convert("UTC").as_unit("ns").asi8 + tf.value
        k = np.searchsorted(htf_close_time, decide, side="right") - 1
        tr = np.where(k >= 0, trend[np.clip(k, 0, None)], 0.0)
        entry = sig["entry"].to_numpy(np.int8)
        blocked = ((entry > 0) & (tr <= 0)) | ((entry < 0) & (tr >= 0))
        sig = sig.copy()
        sig["entry"] = np.where(blocked, 0, entry).astype(np.int8)
        sig["htf_trend"] = tr
        # 上位足の EMA が落ち着くまで（3倍の本数）は売買しない
        warm = int(np.ceil(s.htf_ema * 3 * htf_td / tf))
        first = np.searchsorted(decide, htf_close_time[0])
        return sig, warm + int(first)

    def _exit_cfg(self, sd: _SleeveData, tag: str | None) -> ExitConfig:
        if tag not in sd.cfg_cache:
            sd.cfg_cache[tag] = sd.sleeve.exit_config(self.cfg.exit, tag)
        return sd.cfg_cache[tag]

    # ------------------------------------------------------------------ 実行
    def run(self) -> BacktestResult:
        self._prepare()
        cfg = self.cfg
        rc = cfg.risk
        fc = cfg.filters
        start_ns = pd.Timestamp(cfg.trade_start).value if cfg.trade_start is not None else None
        end_ns = pd.Timestamp(cfg.trade_end).value if cfg.trade_end is not None else None

        self.cash = cfg.initial_equity
        self.positions: dict[str, _Position] = {}
        self.pending: dict[str, _Pending] = {}
        self.trades: list[dict] = []
        self.rejections: list[dict] = []
        self.last_close: dict[str, float] = {}
        self.last_spread: dict[str, float] = {}
        equity_t: list[int] = []
        equity_v: list[float] = []
        lev_v: list[float] = []
        sleeves_by_sym: dict[str, list[_SleeveData]] = {}
        for sd in self.sl.values():
            sleeves_by_sym.setdefault(sd.sleeve.symbol, []).append(sd)

        peak = cfg.initial_equity
        halted = False
        day_key = None
        day_start_eq = prev_equity = cfg.initial_equity
        last_i: dict[str, int] = {}
        syms = list(self.sym)

        # 出来事（足の終了 / 足の開始）を時刻順に処理する。同じ時刻では終了を先に処理し、
        # その判断を直後の開始で執行する。時間足が銘柄ごとに違っても先読みにならない
        ev_t, ev_kind, ev_sym, ev_i = [], [], [], []
        for si, sym in enumerate(syms):
            d = self.sym[sym]
            n = len(d.times) if end_ns is None else int(np.searchsorted(d.times, end_ns))
            idx = np.arange(n)
            for kind, times in ((1, d.times[:n]), (0, d.times[:n] + d.tf_ns)):
                ev_t.append(times)
                ev_kind.append(np.full(n, kind))
                ev_sym.append(np.full(n, si))
                ev_i.append(idx)
        ev_t, ev_kind, ev_sym, ev_i = (np.concatenate(x) if x else np.zeros(0, int) for x in (ev_t, ev_kind, ev_sym, ev_i))
        order = np.lexsort((ev_sym, ev_kind, ev_t))
        ev_t, ev_kind, ev_sym, ev_i = ev_t[order], ev_kind[order], ev_sym[order], ev_i[order]
        bounds = np.flatnonzero(np.diff(ev_t)) + 1
        groups = np.split(np.arange(len(ev_t)), bounds) if len(ev_t) else []
        uniq_t = ev_t[np.r_[0, bounds]] if len(ev_t) else ev_t
        fx_at = self._fx_series(uniq_t)
        close_et = pd.DatetimeIndex(pd.to_datetime(uniq_t, utc=True)).tz_convert(ET)
        day_keys = (close_et + pd.Timedelta(hours=24 - TRADING_DAY_ROLL_ET)).normalize().asi8

        fx = float(fx_at[0]) if len(fx_at) else 1.0
        for g, members in enumerate(groups):
            t = int(uniq_t[g])
            fx = fx_at[g]
            closing = [(syms[ev_sym[e]], self.sym[syms[ev_sym[e]]], int(ev_i[e])) for e in members if ev_kind[e] == 0]
            opening = [(syms[ev_sym[e]], self.sym[syms[ev_sym[e]]], int(ev_i[e])) for e in members if ev_kind[e] == 1]

            if closing:
                tc = t  # 足の終了時刻
                # 1) 足の中の損切り/利確（細かい足があれば小足ごとに）
                for sym, d, i in closing:
                    last_i[sym] = i
                    self._intrabar(sym, d, i, d.times[i], fx)
                for sym, d, i in closing:
                    self.last_close[sym] = d.c[i]
                    self.last_spread[sym] = d.spread[i]
                    if cfg.costs.financing:
                        self._accrue_financing(sym, d, i, tc, fx)

                # 2) 終値: ポジション管理（逆指値更新・強制決済・手仕舞いシグナル）
                for sym, d, i in closing:
                    self._on_close_manage(sym, d, i, tc, fx, fc.event_flatten_lead or d.tf)

                equity = self._equity(fx)
                dk = day_keys[g]
                if dk != day_key:  # 取引日の開始時点（＝前の足の終了時点）の資産を基準にする
                    day_key, day_start_eq = dk, prev_equity
                prev_equity = equity
                peak = max(peak, equity)
                if not halted and equity <= peak * (1 - rc.max_drawdown_halt):
                    halted = True
                    self.rejections.append({"time": pd.Timestamp(tc, tz="UTC"), "sleeve": "*",
                                            "side": 0, "reason": "halt_triggered"})
                daily_blocked = equity <= day_start_eq * (1 - rc.daily_loss_limit)

                # 3) 終値: 新規シグナル → フィルタ → リスク判定 → 次の始値で執行を予約
                in_range = (start_ns is None or tc >= start_ns) and (end_ns is None or tc < end_ns)
                if in_range:
                    for sym, d, i in closing:
                        for sd in sleeves_by_sym[sym]:
                            self._consider_entry(sd, d, i, tc, fx, equity, halted, daily_blocked)
                if start_ns is None or tc >= start_ns:
                    equity_t.append(tc)
                    equity_v.append(equity)
                    gross = sum(notional(p.qty, self.last_close[p.symbol], fx) for p in self.positions.values())
                    lev_v.append(gross / equity if equity > 0 else 0.0)

            # 4) 始値: 予約済みの決済 → 予約済みの新規
            for sym, d, i in opening:
                self._on_open(sym, d, i, d.times[i], fx, (fc.max_fill_delay or 2 * d.tf).value)

        # 最後に残ったポジションは最終足の終値で決済
        for name in list(self.positions):
            pos = self.positions[name]
            d = self.sym[pos.symbol]
            i = last_i[pos.symbol]
            price = self._exit_price_close(pos.side, d, i)
            self._close(name, price, pd.Timestamp(d.times[i] + d.tf_ns, tz="UTC"), "end", fx)
        if equity_v:
            equity_v[-1] = self.cash

        index = pd.to_datetime(np.array(equity_t), utc=True)
        equity = pd.Series(equity_v, index=index, name="equity")
        leverage = pd.Series(lev_v, index=index, name="leverage")
        trades = pd.DataFrame(self.trades)
        rejections = pd.DataFrame(self.rejections, columns=["time", "sleeve", "side", "reason"])
        signals = {n: sd.signals for n, sd in self.sl.items()}
        return BacktestResult(trades, equity, rejections, signals, cfg, self.tf, leverage)

    # ------------------------------------------------------------------ 各処理
    def _on_open(self, sym, d: _SymbolData, i, ts, fx, max_delay) -> None:
        o, sp, slip = d.o[i], d.spread[i], d.slip
        if d.fj0 is not None and d.fj1[i] > d.fj0[i]:
            sp = d.fsp[d.fj0[i]]  # 足の最初の小足のスプレッド（再開直後の広がりなど）
        for name, pos in list(self.positions.items()):
            if pos.symbol == sym and pos.pending_exit:
                price = o - slip if pos.side > 0 else o + sp + slip
                self._close(name, price, pd.Timestamp(ts, tz="UTC"), pos.pending_exit, fx)
        for name, pe in list(self.pending.items()):
            if pe.symbol != sym:
                continue
            del self.pending[name]
            when = pd.Timestamp(ts, tz="UTC")
            if ts - pe.decided > max_delay:
                self._reject(when, name, pe.side, "stale")
                continue
            if name in self.positions:
                self._reject(when, name, pe.side, "position_exists")
                continue
            n_sym = sum(1 for p in self.positions.values() if p.symbol == sym)
            if n_sym >= self.cfg.risk.max_positions_per_symbol:
                self._reject(when, name, pe.side, "symbol_limit")
                continue
            if (
                d.data_spread is not None
                and d.data_spread[i] > self.cfg.filters.max_spread_mult * d.inst.spread
            ):
                self._reject(when, name, pe.side, "spread")
                continue
            fill = o + sp + slip if pe.side > 0 else o - slip
            stop = fill - pe.side * pe.stop_dist
            tp = None
            if pe.cfg.partial_tp_r > 0:
                tp = fill + pe.side * pe.cfg.partial_tp_r * pe.stop_dist
            commission = self.cfg.costs.commission_per_unit * pe.qty * fx
            self.cash -= commission
            self.positions[name] = _Position(
                sleeve=name, symbol=sym, side=pe.side, qty=pe.qty, initial_qty=pe.qty,
                entry_time=when, entry=fill, stop=stop, tp=tp, cfg=pe.cfg, tag=pe.tag,
                state=ExitState(side=pe.side, entry=fill, stop=stop, r_dist=pe.stop_dist,
                                atr_entry=pe.atr, extreme=o),
                last_accrual=ts, pnl=-commission,
            )

    def _intrabar(self, sym, d: _SymbolData, i, ts, fx) -> None:
        if d.fj0 is not None and d.fj1[i] > d.fj0[i]:
            if any(p.symbol == sym for p in self.positions.values()):
                self._intrabar_fine(sym, d, int(d.fj0[i]), int(d.fj1[i]), fx)
            return
        o, h, l, sp, slip = d.o[i], d.h[i], d.l[i], d.spread[i], d.slip
        when = pd.Timestamp(ts, tz="UTC")
        for name, pos in list(self.positions.items()):
            if pos.symbol != sym:
                continue
            side = pos.side
            # MFE / MAE（Bid/Ask を考慮）
            if side > 0:
                pos.mfe = max(pos.mfe, h - pos.entry)
                pos.mae = max(pos.mae, pos.entry - l)
            else:
                pos.mfe = max(pos.mfe, pos.entry - (l + sp))
                pos.mae = max(pos.mae, (h + sp) - pos.entry)
            # 窓開けで利確価格を超えて始まった場合は先に一部利確（始値で約定）
            if pos.tp is not None and self._tp_gap(pos, o, sp):
                self._partial(name, pos, o if side > 0 else o + sp, when, fx)
            # 損切り
            if side > 0:
                if o <= pos.stop:
                    self._close(name, o - slip, when, "stop_gap", fx)
                    continue
                if l <= pos.stop:
                    self._close(name, pos.stop - slip, when, "stop", fx)
                    continue
            else:
                if o + sp >= pos.stop:
                    self._close(name, o + sp + slip, when, "stop_gap", fx)
                    continue
                if h + sp >= pos.stop:
                    self._close(name, pos.stop + slip, when, "stop", fx)
                    continue
            # 足中の一部利確（指値なのでスリッページなし）
            if pos.tp is not None:
                if (side > 0 and h >= pos.tp) or (side < 0 and l + sp <= pos.tp):
                    self._partial(name, pos, pos.tp, when, fx)

    def _intrabar_fine(self, sym, d: _SymbolData, j0: int, j1: int, fx) -> None:
        """小足を順にたどって損切り・利確を判定する（同じ小足で両方なら損切りを優先）。"""
        so, sh, sl, ssp, st = d.fo[j0:j1], d.fh[j0:j1], d.fl[j0:j1], d.fsp[j0:j1], d.ft[j0:j1]
        n = j1 - j0
        slip = d.slip
        for name, pos in list(self.positions.items()):
            if pos.symbol != sym:
                continue
            side = pos.side
            k, end, exit_at = 0, n, None
            while k < n:
                if side > 0:
                    s_hit = np.flatnonzero(sl[k:] <= pos.stop)
                    t_hit = np.flatnonzero(sh[k:] >= pos.tp) if pos.tp is not None else s_hit[:0]
                else:
                    s_hit = np.flatnonzero(sh[k:] + ssp[k:] >= pos.stop)
                    t_hit = np.flatnonzero(sl[k:] + ssp[k:] <= pos.tp) if pos.tp is not None else s_hit[:0]
                s = k + int(s_hit[0]) if len(s_hit) else n
                t = k + int(t_hit[0]) if len(t_hit) else n
                if t < s:  # 利確の方が先の小足（指値なのでスリッページなし。窓なら始値）
                    o_t = so[t] if side > 0 else so[t] + ssp[t]
                    gapped = o_t >= pos.tp if side > 0 else o_t <= pos.tp
                    self._partial(name, pos, o_t if gapped else pos.tp, pd.Timestamp(st[t], tz="UTC"), fx)
                    k = t  # 同じ小足の中で損切りに届く場合も確認する
                    continue
                if s < n:
                    o_s = so[s] if side > 0 else so[s] + ssp[s]
                    if (side > 0 and o_s <= pos.stop) or (side < 0 and o_s >= pos.stop):
                        price, reason = (o_s - slip if side > 0 else o_s + slip), "stop_gap"
                    else:
                        price, reason = (pos.stop - slip if side > 0 else pos.stop + slip), "stop"
                    end, exit_at = s + 1, (price, reason, pd.Timestamp(st[s], tz="UTC"))
                break
            if side > 0:
                pos.mfe = max(pos.mfe, float(sh[:end].max()) - pos.entry)
                pos.mae = max(pos.mae, pos.entry - float(sl[:end].min()))
            else:
                pos.mfe = max(pos.mfe, pos.entry - float((sl[:end] + ssp[:end]).min()))
                pos.mae = max(pos.mae, float((sh[:end] + ssp[:end]).max()) - pos.entry)
            if exit_at is not None:
                self._close(name, exit_at[0], exit_at[2], exit_at[1], fx)

    @staticmethod
    def _tp_gap(pos: _Position, o: float, sp: float) -> bool:
        return (pos.side > 0 and o >= pos.tp) or (pos.side < 0 and o + sp <= pos.tp)

    def _partial(self, name, pos: _Position, price, when, fx) -> None:
        inst = self.instruments[pos.symbol]
        part = inst.round_qty_down(pos.qty * pos.cfg.partial_fraction)
        pos.tp = None
        if part < inst.min_qty or pos.qty - part < inst.min_qty:
            return  # 最小単位の都合で分割できない
        self._realize(pos, part, price, fx)
        pos.qty = round(pos.qty - part, 10)

    def _accrue_financing(self, sym, d: _SymbolData, i, tc, fx) -> None:
        inst = d.inst
        for pos in self.positions.values():
            if pos.symbol != sym:
                continue
            rate = inst.financing_long if pos.side > 0 else inst.financing_short
            dt_days = (tc - pos.last_accrual) / DAY_NS
            cost = pos.qty * d.c[i] * fx * rate * dt_days / 365.0
            self.cash -= cost
            pos.pnl -= cost
            pos.last_accrual = tc

    def _on_close_manage(self, sym, d: _SymbolData, i, tc, fx, flatten_lead) -> None:
        fc = self.cfg.filters
        when = pd.Timestamp(tc, tz="UTC")
        for name, pos in list(self.positions.items()):
            if pos.symbol != sym or pos.pending_exit:
                continue
            cfg = pos.cfg
            if cfg.flatten_before_weekend and (
                friday_cutoff_passed(when, fc.weekend_flatten_fri_et)
                or friday_cutoff_passed(when + d.tf, fc.weekend_flatten_fri_et)
            ):
                self._close(name, self._exit_price_close(pos.side, d, i), when, "weekend", fx)
                continue
            if cfg.flatten_before_events and self.events.upcoming(
                when, d.inst.event_tags, flatten_lead
            ):
                self._close(name, self._exit_price_close(pos.side, d, i), when, "event", fx)
                continue
            sd = self.sl[name]
            upd = on_bar_close(
                pos.state, cfg, d.h[i], d.l[i], d.c[i], sd.atr[i], d.spread[i], d.inst.stop_level
            )
            if upd.new_stop is not None:
                pos.stop = pos.state.stop = upd.new_stop
            if upd.exit_now:
                pos.pending_exit = upd.exit_now
                continue
            ex_long, ex_short = sd.exit_cols.get(pos.tag, sd.exit_cols[None])
            if (pos.side > 0 and ex_long[i]) or (pos.side < 0 and ex_short[i]):
                pos.pending_exit = "signal"

    def _consider_entry(self, sd: _SleeveData, d: _SymbolData, i, tc, fx, equity, halted, daily_blocked):
        side = int(sd.entry[i])
        if side == 0 or i < sd.warmup:
            return
        name = sd.sleeve.name
        if name in self.positions or name in self.pending:
            return
        when = pd.Timestamp(tc, tz="UTC")
        fc = self.cfg.filters
        rc = self.cfg.risk
        inst = d.inst
        if halted:
            return self._reject(when, name, side, "halt")
        if daily_blocked:
            return self._reject(when, name, side, "daily_loss")
        if fc.no_entry_after_fri_et is not None and friday_cutoff_passed(when, fc.no_entry_after_fri_et):
            return self._reject(when, name, side, "weekend")
        if self.events.in_window(when, inst.event_tags, fc.event_block_before, fc.event_block_after):
            return self._reject(when, name, side, "event")
        n_sym = sum(1 for p in self.positions.values() if p.symbol == inst.symbol) + sum(
            1 for p in self.pending.values() if p.symbol == inst.symbol
        )
        if n_sym >= rc.max_positions_per_symbol:
            return self._reject(when, name, side, "symbol_limit")

        tag = sd.tags[i] if sd.tags is not None else None
        cfg = self._exit_cfg(sd, tag)
        atr = sd.atr[i]
        stop_dist = cfg.clip_stop(sd.stop_dist[i], atr)

        exp = self._exposure(fx)
        budget = rc.max_total_risk * equity - exp.heat
        reason = "heat"
        cap = rc.cluster_max_risk.get(inst.cluster)
        if cap is not None:
            c_budget = cap * equity - exp.cluster_heat.get(inst.cluster, 0.0)
            if c_budget < budget:
                budget, reason = c_budget, "cluster"
        if budget <= 0:
            return self._reject(when, name, side, reason)
        size = position_size(equity, stop_dist, inst, fx, rc, budget, sd.sleeve.risk_weight)
        if size.qty <= 0:
            return self._reject(when, name, side, size.reason or reason)
        qty = size.qty
        # レバレッジ上限（超える分は数量を減らす）
        room = leverage_room(equity, rc, exp.notional.get(inst.symbol, 0.0), exp.notional_total)
        if notional(qty, d.c[i], fx) > room:
            qty = inst.round_qty_down(room / notional(1.0, d.c[i], fx)) if room > 0 else 0
            if qty < inst.min_qty:
                return self._reject(when, name, side, "leverage")
        avail = rc.max_margin_utilization * equity - exp.margin
        need = required_margin(qty, d.c[i], inst, fx)
        if need > avail:
            qty = inst.round_qty_down(avail / required_margin(1.0, d.c[i], inst, fx)) if avail > 0 else 0
            if qty < inst.min_qty:
                return self._reject(when, name, side, "margin")
        self.pending[name] = _Pending(
            sleeve=name, symbol=inst.symbol, side=side, qty=qty, stop_dist=stop_dist,
            atr=atr, decided=tc, tag=tag, cfg=cfg,
        )

    # ------------------------------------------------------------------ 補助
    def _exposure(self, fx) -> _Exposure:
        """保有中 + 約定待ちのポジションのリスク・証拠金・名目建玉を集計する。"""
        exp = _Exposure(0.0, {}, 0.0, {}, 0.0)
        items = [(p.symbol, open_risk(p.side, p.qty, p.entry, p.stop, fx), p.qty, self.last_close.get(p.symbol, p.entry))
                 for p in self.positions.values()]
        items += [(p.symbol, p.stop_dist * p.qty * fx, p.qty, self.last_close.get(p.symbol, 0.0))
                  for p in self.pending.values()]
        for symbol, risk, qty, price in items:
            inst = self.instruments[symbol]
            exp.heat += risk
            exp.cluster_heat[inst.cluster] = exp.cluster_heat.get(inst.cluster, 0.0) + risk
            exp.margin += required_margin(qty, price, inst, fx)
            n = notional(qty, price, fx)
            exp.notional[symbol] = exp.notional.get(symbol, 0.0) + n
            exp.notional_total += n
        return exp

    def _equity(self, fx) -> float:
        eq = self.cash
        for p in self.positions.values():
            c = self.last_close[p.symbol]
            mark = c if p.side > 0 else c + self.last_spread[p.symbol]
            eq += p.side * (mark - p.entry) * p.qty * fx
        return eq

    def _exit_price_close(self, side, d: _SymbolData, i) -> float:
        return d.c[i] - d.slip if side > 0 else d.c[i] + d.spread[i] + d.slip

    def _realize(self, pos: _Position, qty, price, fx) -> None:
        gross = pos.side * (price - pos.entry) * qty
        commission = self.cfg.costs.commission_per_unit * qty * fx
        pnl = gross * fx - commission
        self.cash += pnl
        pos.pnl += pnl
        pos.price_pnl += gross

    def _close(self, name, price, when, reason, fx) -> None:
        pos = self.positions.pop(name)
        self._realize(pos, pos.qty, price, fx)
        risk_unit = pos.state.r_dist * pos.initial_qty
        self.trades.append({
            "sleeve": pos.sleeve,
            "symbol": pos.symbol,
            "tag": pos.tag,
            "side": pos.side,
            "qty": pos.initial_qty,
            "entry_time": pos.entry_time,
            "entry_price": pos.entry,
            "exit_time": when,
            "exit_price": price,
            "reason": reason,
            "pnl": pos.pnl,
            "r_multiple": pos.price_pnl / risk_unit if risk_unit > 0 else np.nan,
            "bars_held": pos.state.bars_held,
            "mfe_r": pos.mfe / pos.state.r_dist,
            "mae_r": pos.mae / pos.state.r_dist,
        })

    def _reject(self, when, name, side, reason) -> None:
        self.rejections.append({"time": when, "sleeve": name, "side": side, "reason": reason})


def run_backtest(
    data: dict[str, pd.DataFrame],
    instruments: dict[str, Instrument],
    sleeves: list[Sleeve],
    config: BacktestConfig | None = None,
    fine_data: dict[str, pd.DataFrame] | None = None,
) -> BacktestResult:
    return Backtester(data, instruments, sleeves, config, fine_data).run()
