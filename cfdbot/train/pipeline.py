"""学習の全体の流れ（scripts/train.py から呼ぶ）。

    データ読み込み → タスク作成 → 計算時間の見積もり（予算超過なら間引き）
    → 分散実行（結果はキャッシュ、途中再開可）→ ウォークフォワードで戦略選択・配分
    → 実際の資金・ルールでの最終検証 → EA 用ファイルとレポートを出力
"""

from __future__ import annotations

import hashlib
import itertools
import json
import multiprocessing as mp
import os
import random
import secrets
import socket
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..backtest import BacktestConfig, CostModel, FilterConfig, run_backtest
from ..events import ET, load_events_csv
from ..exits import ExitConfig
from ..export import EA_STRATEGIES
from ..instruments import get_instruments, load_instruments
from ..risk import RiskConfig
from ..strategies import make_strategy
from .config import TIMEFRAMES, TrainConfig, scale_grid, tf_minutes
from .dataset import Dataset, load_dataset
from .distributed import Coordinator, ResultStore, _local_worker_entry
from .costs import timeframe_costs
from .evaluate import EvalSettings, Evaluator, Task, decode_array, make_sleeve, pick_task
from .portfolio import (
    SleeveCandidates,
    WindowResult,
    build_portfolio,
    make_windows,
    portfolio_returns,
)

Log = Callable[[str], None]


# --------------------------------------------------------------------------- タスク
def _split(values: dict[str, Any]) -> tuple[dict, dict, dict]:
    """grid / fixed の 1 組を (戦略パラメータ, 出口設定, 上位足フィルタ) に分ける。"""
    params, ex, htf = {}, {}, {}
    for k, v in values.items():
        if k.startswith("exit."):
            ex[k[5:]] = v
        elif k.startswith("htf."):
            htf[k[4:]] = v
        else:
            params[k] = v
    return params, ex, htf


def _normalize_htf(htf: dict, tf: str) -> dict | None:
    """上位足フィルタの指定を整える。使えない組み合わせは None。"""
    name = str(htf.get("timeframe", "none")).upper()
    if name in ("NONE", "", "0"):
        return {}
    if name not in TIMEFRAMES or tf_minutes(name) <= tf_minutes(tf):
        return None  # 売買の足より粗くないと意味がない
    return {"timeframe": name, "ema": int(htf.get("ema", 50))}


def build_tasks(cfg: TrainConfig, ds: Dataset) -> tuple[list[Task], dict[str, tuple[int, ...]]]:
    """戦略 × 銘柄 × 売買の足 × パラメータの組み合わせを作る。"""
    tasks: list[Task] = []
    dims: dict[str, tuple[int, ...]] = {}
    seen: set[str] = set()
    for spec in cfg.strategies:
        if spec.name not in EA_STRATEGIES:
            raise ValueError(f"学習できる戦略は {EA_STRATEGIES}（EA で動くもの）: {spec.name}")
        for tf in spec.timeframes or cfg.data.signal_timeframes:
            if tf in spec.grids:
                grid, fixed = spec.grids[tf], dict(spec.fixed)
            else:  # 基準の時間足で書いた本数を、同じ時間幅になるよう換算
                grid = scale_grid(spec.grid, cfg.data.grid_timeframe, tf)
                fixed = {k: v[0] for k, v in scale_grid({k: [v] for k, v in spec.fixed.items()},
                                                        cfg.data.grid_timeframe, tf).items()}
            keys = list(grid)
            values = [list(grid[k]) for k in keys]
            for sym in spec.symbols:
                if sym not in ds.symbols or ds.signal_frame(sym, tf) is None:
                    continue
                dims[f"{sym}:{spec.name}@{tf}"] = tuple(len(v) for v in values)
                for pos in itertools.product(*[range(len(v)) for v in values]):
                    params, ex, htf = _split({**fixed, **{k: values[n][j] for n, (k, j) in enumerate(zip(keys, pos))}})
                    htf = _normalize_htf(htf, tf)
                    if htf is None or (htf and ds.context_frame(sym, htf["timeframe"]) is None):
                        continue
                    try:
                        make_strategy(spec.name, **params)
                        ExitConfig().with_overrides(ex)
                    except ValueError:
                        continue  # 成り立たない組み合わせ（例: 手仕舞い期間 ≥ エントリー期間）
                    task = Task(sym, spec.name, params, ex, pos, tf, htf)
                    if task.id not in seen:  # 上位足フィルタなしの重複など
                        seen.add(task.id)
                        tasks.append(task)
    return tasks, dims


def _eval_settings(cfg: TrainConfig, ds: Dataset) -> EvalSettings:
    inst = get_instruments(cfg.data.broker)
    if cfg.data.instruments:
        inst.update(load_instruments(cfg.data.instruments))
    missing = [s for s in ds.symbols if s not in inst]
    if missing:
        raise ValueError(f"銘柄仕様が無い: {missing}（data.instruments の JSON に追加）")
    events = load_events_csv(cfg.data.events) if cfg.data.events else []
    return EvalSettings(
        instruments={k: inst[k].to_dict() for k in ds.symbols},
        events=[{"time": e.time.isoformat(), "name": e.name, "tag": e.tag} for e in events],
        fx_const=float(cfg.data.fx),
        base_risk=cfg.account.base_risk,
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # 実際には送信しない（経路から自分の IP を調べるだけ）
            return s.getsockname()[0]
    except OSError:
        return "<このMacのIPアドレス>"


# --------------------------------------------------------------------------- 計算
def _calibrate(ev: Evaluator, todo: list[Task], store: ResultStore, log: Log) -> float:
    """戦略ごとに 1 件ずつ実行して 1 件あたりの時間を測る（結果は保存して無駄にしない）。"""
    secs = []
    seen = set()
    for t in todo:
        if (t.strategy, t.timeframe) in seen:
            continue
        seen.add((t.strategy, t.timeframe))
        try:
            res = ev.run(t)
        except Exception as e:  # noqa: BLE001
            res = {"id": t.id, "ok": False, "error": repr(e)}
        store.put(t.id, res)
        if res.get("ok"):
            secs.append(res["secs"])
        else:
            log(f"{t.sleeve} の計算に失敗: {res['error']}")
    sec = float(np.mean(secs)) if secs else 1.0
    log(f"1 件あたり約 {sec:.2f} 秒（この Mac の 1 コア）")
    return sec


def _run_tasks(cfg: TrainConfig, tasks: list[Task], meta: dict, ds: Dataset, store: ResultStore,
               ev: Evaluator, log: Log) -> dict[str, Any]:
    done_ids = store.ids()
    todo = [t for t in tasks if t.id not in done_ids]
    info: dict[str, Any] = {"total": len(tasks), "cached": len(tasks) - len(todo), "subsampled": 0}
    if not todo:
        log(f"全 {len(tasks)} 件が計算済み（キャッシュを使用）")
        return info
    sec = _calibrate(ev, todo, store, log)
    done_ids = store.ids()
    todo = [t for t in todo if t.id not in done_ids]
    workers = cfg.compute.workers or max((os.cpu_count() or 2) - 1, 1)
    est_h = len(todo) * sec / workers / 3600
    budget = cfg.compute.time_budget_hours
    if est_h > budget:
        keep = max(int(len(todo) * budget / est_h), 1)
        rng = random.Random(cfg.compute.seed)
        todo = rng.sample(todo, keep)
        info["subsampled"] = info["total"] - info["cached"] - keep
        log(f"見積もり {est_h:.1f} 時間 > 予算 {budget} 時間 → {keep} 件に間引き"
            "（探索範囲を狭める方が過剰最適化の点でも望ましい）")
        est_h = budget
    log(f"計算: {len(todo)} 件 / ワーカー {workers}（この Mac）/ 見積もり 約 {est_h * 60:.0f} 分")

    token = secrets.token_urlsafe(9)
    coord = Coordinator([t.to_dict() for t in todo], meta, ds.files, store, token,
                        host=cfg.compute.listen, port=cfg.compute.port)
    coord.start()
    if cfg.compute.listen not in ("127.0.0.1", "localhost"):
        log("iPad など他の端末から参加するには、その端末で次を実行:")
        log(f"  python remote_worker.py http://{_lan_ip()}:{coord.address[1]} {token}")
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_local_worker_entry, args=(coord.url, token, f"mac-{i + 1}"), daemon=True)
             for i in range(workers)]
    for p in procs:
        p.start()
    t0 = time.time()

    def progress(st: dict[str, Any]) -> None:
        el = time.time() - t0
        rate = st["done"] / el if el > 0 else 0
        eta = f"残り約 {(st['total'] - st['done']) / rate / 60:.0f} 分" if rate > 0 else "計算中"
        remote = [w for w in st["workers"] if not w.startswith("mac-")]
        extra = f" / 外部 {len(remote)} 台" if remote else ""
        log(f"  {st['done']}/{st['total']} 件（{st['done'] / st['total']:.0%}） {eta}{extra}"
            + (f" / 失敗 {st['failed']}" if st["failed"] else ""))

    try:
        coord.wait(progress, interval=15.0)
    finally:
        for p in procs:
            p.join(timeout=10)
            if p.is_alive():
                p.terminate()
        coord.stop()
    st = coord.status()
    info.update({"failed": st["failed"], "completed_by": st["completed_by"],
                 "minutes": round((time.time() - t0) / 60, 1)})
    if st["failed"]:
        log(f"失敗 {st['failed']} 件。例: {st['errors'][0]}")
    return info


def _load_candidates(tasks: list[Task], dims: dict[str, tuple[int, ...]], store: ResultStore) -> list[SleeveCandidates]:
    results = store.get_many([t.id for t in tasks])
    by: dict[str, list[tuple[Task, dict]]] = {}
    for t in tasks:
        r = results.get(t.id)
        if r and r.get("ok"):
            by.setdefault(t.sleeve, []).append((t, r))
    out = []
    for key, items in sorted(by.items()):
        out.append(SleeveCandidates(
            symbol=items[0][0].symbol, strategy=items[0][0].strategy,
            params=[t.params for t, _ in items], exits=[t.exit for t, _ in items],
            pos=[t.pos for t, _ in items], dims=dims[key],
            returns=np.vstack([decode_array(r["ret"], "float32").astype(float) for _, r in items]),
            entries=[decode_array(r["entries"], "int32") for _, r in items],
            timeframe=items[0][0].timeframe, htfs=[t.htf for t, _ in items],
        ))
    return out


# --------------------------------------------------------------------------- 最終検証
def _account_config(cfg: TrainConfig, fx, events) -> BacktestConfig:
    a = cfg.account
    return BacktestConfig(
        initial_equity=a.equity,
        fx_rate=fx,
        risk=RiskConfig(
            risk_per_trade=a.base_risk, max_total_risk=a.max_total_risk,
            cluster_max_risk=dict(a.cluster_max_risk), daily_loss_limit=a.daily_loss_limit,
            max_drawdown_halt=a.max_drawdown_halt, max_leverage_symbol=a.max_leverage_symbol,
            max_leverage_total=a.max_leverage_total,
        ),
        costs=CostModel(),
        filters=FilterConfig(extra_events=tuple(events)),
    )


def _day_start_utc(day: pd.Timestamp) -> pd.Timestamp:
    """取引日 D の始まり（前日 17:00 ET）。"""
    return (day - pd.Timedelta(hours=7)).tz_localize(ET).tz_convert("UTC")


def _warm_time(task: Task) -> pd.Timedelta:
    """指標が落ち着くまでに必要な時間（上位足フィルタの EMA を含む）。"""
    t = make_strategy(task.strategy, **task.params).warmup_bars() * pd.Timedelta(TIMEFRAMES[task.timeframe])
    if task.htf:
        t = max(t, task.htf["ema"] * 3 * pd.Timedelta(TIMEFRAMES[task.htf["timeframe"]]))
    return t


def validate_window(ds: Dataset, instruments, picks, bt_cfg: BacktestConfig,
                    start: pd.Timestamp, end: pd.Timestamp) -> dict[str, Any]:
    """検証期間を、実際の資金・全ルール（細かい足での約定の再現を含む）でバックテストする。"""
    tasks = [pick_task(p) for p in picks]
    sleeves = [make_sleeve(t, ds, risk_weight=p.multiplier) for t, p in zip(tasks, picks)]
    data, fine = {}, {}
    for t in tasks:
        frame = ds.signal_frame(t.symbol, t.timeframe)
        warm = max(_warm_time(x) for x in tasks if x.symbol == t.symbol) * 1.5 + pd.Timedelta(days=7)
        data[t.symbol] = frame[(frame.index >= start - warm) & (frame.index < end)]
        f = ds.fine_frame(t.symbol, t.timeframe)
        if f is not None:
            fine[t.symbol] = f[(f.index >= start - warm) & (f.index < end)]
    # 評価のため最大DDでの停止は外す（停止に触れたかはレポートで確認）
    cfg = replace(bt_cfg, trade_start=start, trade_end=end, risk=replace(bt_cfg.risk, max_drawdown_halt=1.0))
    res = run_backtest(data, instruments, sleeves, cfg, fine_data=fine or None)
    return {"equity": res.equity, "trades": res.trades, "leverage": res.leverage}


# --------------------------------------------------------------------------- 本体
def run_training(cfg: TrainConfig, log: Log = print, config_path: str | None = None) -> Path:
    started = datetime.now()
    ds = load_dataset(cfg.data)
    for s in ds.skipped:
        log(f"読み飛ばし: {s}")
    for sym in ds.symbols:
        parts = [f"{tf} {df.index[0]:%Y-%m-%d}〜{df.index[-1]:%Y-%m-%d}（{len(df):,} 本）"
                 for tf, df in sorted(ds.frames[sym].items(), key=lambda x: tf_minutes(x[0]))]
        log(f"{sym}: " + " / ".join(parts))
        for tf in cfg.data.signal_timeframes:
            made = "" if tf in ds.frames[sym] else "（細かい足から作成）"
            fine = ds.fine_timeframe(sym, tf)
            if ds.signal_frame(sym, tf) is not None:
                log(f"  売買 {tf}{made} / 約定の再現 {fine or 'なし（' + tf + ' の高値・安値で推定）'}")
    log("円換算: " + ("USDJPY のデータ" if ds.fx is not None else f"固定 {cfg.data.fx} 円"))

    settings = _eval_settings(cfg, ds)
    costs = timeframe_costs(ds, settings.instrument_map())
    for _, row in costs[costs["timeframe"].isin(cfg.data.signal_timeframes)].iterrows():
        log(f"コスト {row['symbol']}@{row['timeframe']}: 往復コスト = 損切り幅の {row['cost_per_r']:.1%}（{row['verdict']}）")
    tasks, dims = build_tasks(cfg, ds)
    if not tasks:
        raise ValueError("学習するタスクが無い（strategies の symbols・timeframes とデータを確認）")
    log(f"候補: {len(dims)} 通り（銘柄×戦略×時間足）、パラメータの組み合わせ {len(tasks)} 件")

    settings_hash = hashlib.sha256(json.dumps(settings.to_dict(), sort_keys=True).encode()).hexdigest()
    out_root = Path(cfg.output_dir)
    store = ResultStore(out_root / "cache" / f"{ds.digest[:16]}-{settings_hash[:8]}.sqlite")
    meta = {
        "version": 1,
        "server_tz": cfg.data.server_tz,
        "signal_timeframes": cfg.data.signal_timeframes,
        "fill_timeframe": cfg.data.fill_timeframe,
        "files": {k: {"path": str(p.resolve()), "name": p.name, "sha256": _sha(p)} for k, p in ds.files.items()},
        "settings": settings.to_dict(),
    }
    ev = Evaluator(ds, settings)
    compute_info = _run_tasks(cfg, tasks, meta, ds, store, ev, log)

    cands = _load_candidates(tasks, dims, store)
    store.close()
    calendar = ev.calendar
    # 候補ごとに指標が落ち着く日を求め、学習期間がそれより前に始まる回ではその候補を使わない。
    # （日足の 200 本 EMA のように長いものに合わせると、H4 の候補まで検証の回数が減るため）
    for c in cands:
        idx = ds.signal_frame(c.symbol, c.timeframe).index
        warm = max(_warm_time(t) for t in tasks
                   if t.symbol == c.symbol and t.strategy == c.strategy and t.timeframe == c.timeframe)
        first = idx[min(int(np.ceil(warm / pd.Timedelta(TIMEFRAMES[c.timeframe]))), len(idx) - 1)]
        day = (first.tz_convert(ET) + pd.Timedelta(hours=7)).normalize().tz_localize(None)
        c.ready = int(calendar.searchsorted(day))
    start_day = calendar[min((c.ready for c in cands), default=0)]
    wf, pc, acc = cfg.walkforward, cfg.portfolio, cfg.account
    min_mult = acc.min_risk_per_trade / acc.base_risk
    max_mult = acc.max_risk_per_trade / acc.base_risk

    windows = make_windows(calendar, start_day, wf.train_months, wf.test_months)
    if not windows:
        raise ValueError("データが短すぎて学習期間と検証期間を取れない（train_months を短くするか、データを増やす）")
    log(f"ウォークフォワード: {len(windows)} 期間（学習 {wf.train_months} か月 / 検証 {wf.test_months} か月）")

    results: list[WindowResult] = []
    for a, b, c in windows:
        picks, vol, dd = build_portfolio(cands, a, b, wf, pc, min_mult, max_mult)
        oos = pd.Series(portfolio_returns(cands, picks, b, c), index=calendar[b:c])
        results.append(WindowResult((calendar[a], calendar[b - 1]), (calendar[b], calendar[c - 1]), picks, vol, dd, oos))

    # 本番用: 直近の学習期間で決める
    end_day = calendar[-1]
    a = int(calendar.searchsorted(end_day - pd.DateOffset(months=wf.train_months)))
    final_picks, final_vol, final_dd = build_portfolio(cands, a, len(calendar), wf, pc, min_mult, max_mult)
    final = WindowResult((calendar[a], end_day), None, final_picks, final_vol, final_dd)

    # 実際の資金・ルールで検証期間を再現
    events = settings.event_list()
    fx = ds.fx if ds.fx is not None else cfg.data.fx
    bt_cfg = _account_config(cfg, fx, events)
    instruments = settings.instrument_map()
    jobs = [(w, None) for w in results if w.picks]
    log(f"最終検証: {len(jobs)} 期間を実際の資金 {acc.equity:,.0f} 円・全ルールで再現")
    validated = [validate_window(ds, instruments, w.picks, bt_cfg, _day_start_utc(w.test[0]),
                                 _day_start_utc(w.test[1] + pd.Timedelta(days=1))) for w, _ in jobs]

    out = out_root / started.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    from .report import write_outputs

    write_outputs(out, cfg, ds, tasks, compute_info, results, final, jobs, validated, bt_cfg, instruments,
                  started, config_path, log, costs)
    log(f"完了: {out}")
    return out
