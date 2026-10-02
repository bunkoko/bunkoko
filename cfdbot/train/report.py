"""学習結果の保存（レポート・EA 用ファイル・CSV）。"""

from __future__ import annotations

import json
import math
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..backtest import BacktestConfig
from ..export import export_sleeve
from ..metrics import compute_metrics
from .config import TrainConfig, tf_minutes
from .evaluate import make_sleeve, pick_task
from .portfolio import WindowResult, max_drawdown


def _daily_stats(r: pd.Series) -> dict[str, float]:
    if r.empty:
        return {"cagr": 0.0, "max_drawdown": 0.0, "sharpe": 0.0}
    growth = float(np.prod(1 + r.to_numpy()))
    years = max(len(r) / 252, 1e-9)
    sd = r.std()
    return {
        "cagr": growth ** (1 / years) - 1 if growth > 0 else -1.0,
        "max_drawdown": max_drawdown(r.to_numpy()),
        "sharpe": float(r.mean() / sd * math.sqrt(252)) if sd > 0 else 0.0,
    }


def _chain(curves: list[pd.Series], initial: float) -> pd.Series:
    out, level = [], 1.0
    for eq in curves:
        if eq.empty:
            continue
        norm = eq / initial * level
        out.append(norm)
        level = float(norm.iloc[-1])
    return pd.concat(out) * initial if out else pd.Series(dtype=float)


def _fmt_pick(p) -> str:
    return f"{p.label}×{p.multiplier:.2f}"


def write_outputs(out: Path, cfg: TrainConfig, ds, tasks, compute_info: dict[str, Any],
                  results: list[WindowResult], final: WindowResult, jobs, validated,
                  bt_cfg: BacktestConfig, instruments, started: datetime, config_path: str | None,
                  log: Callable[[str], None], costs: pd.DataFrame | None = None) -> None:
    acc = cfg.account
    # ---- 検証期間をつないだ成績（近似: 日次リターンの合成 / 実際: 全ルールでの再現）
    approx = pd.concat([w.oos for w in results if w.oos is not None]) if results else pd.Series(dtype=float)
    approx_stats = _daily_stats(approx)
    real_eq = _chain([v["equity"] for v in validated], acc.equity)
    real_trades = pd.concat([v["trades"] for v in validated], ignore_index=True) if validated else pd.DataFrame()
    real_lev = pd.concat([v["leverage"] for v in validated]) if validated else pd.Series(dtype=float)
    real = compute_metrics(real_eq, real_trades, acc.equity, real_lev) if not real_eq.empty else {"trades": 0}

    approx.rename("return").to_csv(out / "oos_returns_approx.csv")
    real_eq.rename("equity").to_csv(out / "oos_equity.csv")
    if not real_trades.empty:
        real_trades.to_csv(out / "oos_trades.csv", index=False)

    # ---- 期間ごとの表
    real_by_window = {id(w): v for (w, _), v in zip(jobs, validated)}
    rows = []
    for w in results:
        v = real_by_window.get(id(w))
        real_ret = float(v["equity"].iloc[-1] / acc.equity - 1) if v is not None and not v["equity"].empty else 0.0
        rows.append({
            "train_start": w.train[0].date(), "train_end": w.train[1].date(),
            "test_start": w.test[0].date(), "test_end": w.test[1].date(),
            "picks": " ".join(_fmt_pick(p) for p in w.picks) or "（見送り）",
            "train_vol": round(w.train_vol, 4), "train_dd": round(w.train_dd, 4),
            "test_return_approx": round(float(np.prod(1 + w.oos.to_numpy()) - 1), 4) if w.oos is not None else 0.0,
            "test_return_real": round(real_ret, 4),
            "test_trades": int(len(v["trades"])) if v is not None else 0,
        })
    win_df = pd.DataFrame(rows)
    win_df.to_csv(out / "windows.csv", index=False)

    # ---- 本番用の構成と EA ファイル
    final_rows = []
    ea_dir = out / "ea"
    for p in final.picks:
        sleeve = make_sleeve(pick_task(p), ds, risk_weight=p.multiplier)
        paths = export_sleeve(sleeve, instruments[p.symbol], bt_cfg, ea_dir, tf_minutes(p.timeframe),
                              suffix=p.timeframe)
        final_rows.append({
            "symbol": p.symbol, "strategy": p.strategy, "timeframe": p.timeframe, "htf": p.htf,
            "fill_timeframe": ds.fine_timeframe(p.symbol, p.timeframe),
            "params": p.params, "exit": p.exit,
            "risk_per_trade": round(acc.base_risk * p.multiplier, 5), "multiplier": round(p.multiplier, 4),
            "train_score": round(p.score, 3), "train_trades": p.trades,
            "ea_files": [str(x.relative_to(out)) for x in paths],
        })
    (out / "final.json").write_text(json.dumps({
        "signal_timeframes": cfg.data.signal_timeframes,
        "train_period": [str(final.train[0].date()), str(final.train[1].date())],
        "train_vol": final.train_vol, "train_dd": final.train_dd,
        "sleeves": final_rows,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if config_path and Path(config_path).exists():
        shutil.copy(config_path, out / "train.toml")

    # ---- レポート
    sharpe = real.get("sharpe", 0.0)
    mdd = real.get("max_drawdown", 1.0)
    ok = sharpe >= 0.5 and mdd < acc.max_drawdown_halt and real.get("trades", 0) >= 30
    elapsed = (datetime.now() - started).total_seconds() / 60
    lines = [
        f"# 学習レポート（{started:%Y-%m-%d %H:%M}）",
        "",
        "## 判定",
        "",
        ("**検証期間の成績は基準を満たした。デモ口座での確認に進んでよい水準。**" if ok else
         "**検証期間の成績が基準（シャープレシオ 0.5 以上・最大DD が停止条件未満・取引 30 回以上）に届かない。"
         "本番には使わないこと。**"),
        "",
        "検証期間（学習に使っていない期間）だけをつないだ成績で判断している。学習期間の成績は良く見えて当然なので使わない。",
        "",
        "## 検証期間の成績",
        "",
        "| | 実際の資金・全ルールで再現 | 近似（日次リターンの合成） |",
        "|---|---|---|",
        f"| 年率リターン | {real.get('cagr', 0):.1%} | {approx_stats['cagr']:.1%} |",
        f"| 最大ドローダウン | {real.get('max_drawdown', 0):.1%} | {approx_stats['max_drawdown']:.1%} |",
        f"| シャープレシオ | {real.get('sharpe', 0):.2f} | {approx_stats['sharpe']:.2f} |",
        f"| 取引回数（年） | {real.get('trades_per_year', 0):.0f} | – |",
        f"| 勝率 / PF | {real.get('win_rate', 0):.0%} / {real.get('profit_factor', 0):.2f} | – |",
        f"| 平均 / 最大レバレッジ | {real.get('avg_leverage', 0):.2f} / {real.get('max_leverage', 0):.2f} 倍 | – |",
        "",
        f"- 資金 {acc.equity:,.0f} 円、基準リスク {acc.base_risk:.1%}（配分後 {acc.min_risk_per_trade:.2%}〜{acc.max_risk_per_trade:.1%}）",
        f"- 最大DDでの停止（{acc.max_drawdown_halt:.0%}）は評価のため外している。上の最大DD がこれを超えていたら本番では途中で停止する",
        "- 近似と実際の差が大きい場合は、最小単位・レバレッジ上限・同一銘柄 1 ポジションなどのルールが効いている",
        "",
        "## 本番用の構成（直近の学習期間で決定）",
        "",
        f"学習期間: {final.train[0]:%Y-%m-%d} 〜 {final.train[1]:%Y-%m-%d}、"
        f"想定ボラティリティ {final.train_vol:.1%}、学習期間の最大DD {final.train_dd:.1%}",
        "",
    ]
    if final_rows:
        lines += ["| 銘柄 | 戦略 | 時間足 | 上位足フィルタ | 1回の損失 | 学習期間の評価 | パラメータ |",
                  "|---|---|---|---|---|---|---|"]
        for r in final_rows:
            params = ", ".join(f"{k}={v}" for k, v in {**r["params"], **{f"exit.{k}": v for k, v in r["exit"].items()}}.items())
            htf = f"{r['htf']['timeframe']} EMA{r['htf']['ema']}" if r["htf"] else "–"
            lines.append(f"| {r['symbol']} | {r['strategy']} | {r['timeframe']} | {htf} | {r['risk_per_trade']:.2%} "
                         f"| {r['train_score']:.2f} | {params} |")
        lines += ["", "EA 用ファイル: `ea/`（ファイル名の時間足のチャートに貼る。違う時間足では EA が起動しない。"
                  "1 銘柄 1 チャート・ea_magic は別々に）"]
    else:
        lines.append("条件を満たす戦略が無かった（全銘柄見送り）。")
    lines += [
        "",
        "## 期間ごとの結果",
        "",
        _md_table(win_df),
        "",
        "## 時間足ごとのコスト",
        "",
        "往復コスト（スプレッド＋スリッページ×2）が損切り幅（2.5 ATR）の何 % か。"
        "5% 未満は良好、5〜10% は注意、10% 超は不向き（1 回の取引の期待値の多くがコストで消える）。",
        "",
        _md_table(_cost_table(costs)) if costs is not None and not costs.empty else "（計算なし）",
        "",
        "## 計算",
        "",
        f"- データ: {', '.join(f'{k}={p.name}' for k, p in ds.files.items())}",
        f"- 売買の足: {', '.join(cfg.data.signal_timeframes)} / 約定の再現: "
        + ", ".join(f"{s}@{t}←{ds.fine_timeframe(s, t) or 'なし'}" for s, t in ds.signal_pairs()),
        f"- パラメータの組み合わせ: {len(tasks)} 件（キャッシュ {compute_info.get('cached', 0)} 件、"
        f"間引き {compute_info.get('subsampled', 0)} 件、失敗 {compute_info.get('failed', 0)} 件）",
        f"- 計算した端末: {compute_info.get('completed_by', {})}",
        f"- 所要時間: {elapsed:.0f} 分",
        "",
        "## 注意",
        "",
        "- 候補（銘柄×戦略×パラメータ）が多いほど、偶然良く見えたものを選びやすい。判断は必ず検証期間の成績で行う",
        "- 学習のたびに結果が大きく変わる（期間ごとの採用戦略がばらばら）なら、その戦略群は安定していない",
        "- 次はこの構成を EA でデモ口座に載せ、1〜3 か月の実績と比べる（docs/roadmap.md フェーズ 7）",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cost_table(costs: pd.DataFrame) -> pd.DataFrame:
    t = costs.copy()
    t["timeframe"] = [tf if f else f"{tf}（作成）" for tf, f in zip(t["timeframe"], t["from_file"])]
    t["spread"] = t["spread"].map(lambda v: f"{v:.4g}")
    t["atr"] = t["atr"].map(lambda v: f"{v:.4g}")
    t["spread_atr"] = t["spread_atr"].map(lambda v: f"{v:.1%}")
    t["cost_per_r"] = t["cost_per_r"].map(lambda v: f"{v:.1%}")
    t = t.drop(columns=["from_file"])
    t.columns = ["銘柄", "時間足", "スプレッド", "ATR", "スプレッド/ATR", "往復コスト/損切り幅", "判定"]
    return t


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "（なし）"
    head = "| " + " | ".join(df.columns) + " |"
    sep = "|" + "---|" * len(df.columns)
    body = ["| " + " | ".join(str(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join([head, sep, *body])
