"""EA の「行き過ぎの回避」（自分の値動き）を、使っていない市場で 1 回だけ確かめ直す（scripts/extcheck_study.py）。docs/cross_asset.md 8 章。

基準 = 行き過ぎの回避の無いタートル 55/20 日足（turtle55_d1.json）。足した版 = 基準＋「自分が直近 20 日に、取引の向きに
ふだんの 1.5 倍を超えて動いていたら新規に入らない」（turtle55_d1_ext.json との違いはここだけ）。相方の行き過ぎの確かめ
（7 章、cfdbot/partner.py）と同じ強さにそろえる: 使っていない市場・つなぎ足の飛びを除く作り・偶然 200 回。
偶然は 2 つ: (1) 同じ数の取引を、同じ長さだけ、でたらめに選んで止める（主）、(2) 絞り込みの時期をずらす（7 章と同じ）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import numpy as np
import pandas as pd

from . import indicators as ind
from . import partner as P
from .backtest import Sleeve, run_backtest
from .context import fetch_yahoo, from_bars, yahoo_csv_frame
from .study import shifted
from .universe import (
    GROUPS,
    data_path,
    day_returns,
    market_instrument,
    sharpe,
    with_costs,
)

N_DAYS, THRESHOLD = 20, 1.5          # EA（turtle55_d1_ext.json）と同じ。動かさない
PLACEBO = 200                        # 偶然の回数（2 種類とも）
PASS_PCT = 0.95                      # 偶然より良い割合の基準（2 種類とも）
MIN_MARKETS = 6                      # 判定に残る市場がこれより少なければ判定しない
PRIOR_TRIALS = 91                    # docs/stocktake_20261110.md（2026-11-10 の時点）
SEED = 20261111
DAY = pd.Timedelta("1D")
# 判定に使う市場: 相方の確かめ（7 章）の 10 市場から、行き過ぎの回避の採用（6 章の期間 C）で使ったプラチナを除いた 9 市場
SEEN = ("PLATINUM",)                 # 参考に並べるだけ（判定しない）
MARKETS: tuple[str, ...] = tuple(k for pair in P.PAIRS for k in pair if k not in SEEN)
RESULT: dict = {"date": "2026-11-11", "verdict": "通らなかった",
                 "detail": "9 市場で差 -0.05（0.07 → 0.03）、でたらめに止めた偶然より良い 28%・時期をずらした偶然より良い 22%、R が良くなった市場 44%"}
                                     # 1 回計算したら、ここに日付と判定を書く（書いてあれば道具は止まる。8.7）


def own_gates(frames: dict[str, pd.DataFrame], tf: pd.Timedelta = DAY, n: int = N_DAYS,
              threshold: float = THRESHOLD, clean: bool = True) -> dict[str, pd.DataFrame]:
    """自分の直近 n 本の変化が、ふだんの threshold 倍を超えた向きの新規を止める絞り込み（列 long / short）。

    clean=True（主の判定）: 1 日の変化がそれまでの 252 日の標準偏差の 4 倍を超えた日（つなぎ足の乗り換えの飛びとみなす）を 0 にして
    から計算する（partner.clean_zchange。7 章と同じ）。clean=False（参考）: EA・ドンチャン戦略の max_extension と同じ式（indicators.extension_z）。
    """
    out = {}
    for s, f in frames.items():
        z = P.clean_zchange(from_bars(s, f, tf), n)[0] if clean else ind.extension_z(f["close"], n).to_numpy(float)
        out[s] = pd.DataFrame({"long": ~(z > threshold), "short": ~(z < -threshold)}, index=f.index)   # 計算できない（NaN）ときは止めない
    return out


def load_group(keys, root, bt, start: pd.Timestamp, since: str, fetch: bool = True, label: str = "判定",
               log: Callable[[str], None] = print) -> tuple[P.Group, list[str]]:
    """Yahoo の日足（data/universe/<名前>.csv。無ければ取る）で市場ごとに作る。戻り値は (まとまり, 取れなかった市場の名前)。

    「取れなかった」（ファイルが無い・Yahoo に断られた・読めない）と「取れたが 3 年に満たない」（Group.dropped）を分ける
    （2026-11-11 点検の指摘で直した）。前者が 1 つでもあれば、呼ぶ側は何も計算せずに止まる（8.2）。外して判定しない。
    """
    frames, insts, dropped, missing = {}, {}, [], []
    for k in keys:
        m = P.MARKETS[k]
        p = data_path(root, m)
        if not p.exists() and fetch:
            root.mkdir(parents=True, exist_ok=True)
            try:
                fetch_yahoo(m.ticker, since).to_csv(p, index=False)
            except Exception as e:  # noqa: BLE001
                log(f"  ✗ {m.label}（{m.ticker}）: {str(e)[:100]}")
        f = None
        if p.exists():
            try:
                f = yahoo_csv_frame(p, repair=True)
            except Exception as e:  # noqa: BLE001
                log(f"  ✗ {m.label}: ファイルが読めない（{str(e)[:100]}）")
        if f is None or f.empty:
            missing.append(m.label)
            continue
        if len(f) < P.MIN_YEARS * 252:
            dropped.append(m.label)
            continue
        g = GROUPS[m.group]
        frames[k], insts[k] = with_costs(f, m, g), market_instrument(m, g)
    return P.Group("E", label, frames, insts, replace(bt, trade_start=start), {}, start, dropped), missing


def run(group: P.Group, pk0, gates: dict[str, pd.DataFrame] | None = None) -> dict:
    """pk0 の戦略を全市場に同じリスクで当てる（partner.run と同じ。取引の表も返す）。"""
    sleeves = [Sleeve(s, P._strategy(pk0), dict(pk0.exit), entry_gate=(gates or {}).get(s)) for s in group.frames]
    res = run_backtest(group.frames, group.instruments, sleeves, group.bt)
    r = day_returns(res.equity, group.start)
    t = res.trades
    by = {s: (int((t["symbol"] == s).sum()), float(t.loc[t["symbol"] == s, "r_multiple"].mean()) if (t["symbol"] == s).any() else np.nan)
          for s in group.frames} if not t.empty else {s: (0, np.nan) for s in group.frames}
    return {"returns": r, "sharpe": sharpe(r), "trades": len(t), "by_symbol": by, "table": t}


def trade_spans(trades: pd.DataFrame, frames: dict[str, pd.DataFrame]) -> dict[tuple[str, str], list[tuple[int, int]]]:
    """基準の取引ごとの (合図の足, 手仕舞いの足) の位置。市場と向き（long / short）ごと。

    合図の足 = 入った足の 1 つ前（合図は終値で出て、次の足の始値で入る）。止める範囲は [合図の足, 手仕舞いの足) の合図。
    """
    out: dict[tuple[str, str], list[tuple[int, int]]] = {}
    if trades is None or trades.empty:
        return out
    for sym, side, t0, t1 in zip(trades["symbol"], trades["side"], trades["entry_time"], trades["exit_time"]):
        idx = frames[sym].index
        p = int(idx.searchsorted(pd.Timestamp(t0), side="left")) - 1
        e = int(idx.searchsorted(pd.Timestamp(t1), side="right")) - 1
        if p < 0:
            continue
        out.setdefault((sym, "long" if side > 0 else "short"), []).append((p, max(e, p + 1)))
    return out


def blocked(spans: dict, gates: dict[str, pd.DataFrame]) -> dict[tuple[str, str], list[int]]:
    """本物の絞り込みが止めた基準の取引ごとの、止めた長さ（足の数）。その取引の終わりまで止めた（丸ごと見送り）なら −1。"""
    out: dict[tuple[str, str], list[int]] = {}
    for (sym, col), items in spans.items():
        ok = gates[sym][col].to_numpy(bool)
        hits = []
        for p, e in items:
            if ok[p]:
                continue
            d = 0
            while p + d < e and not ok[p + d]:
                d += 1
            hits.append(-1 if p + d >= e else d)
        out[(sym, col)] = hits
    return out


def random_block_gates(frames: dict[str, pd.DataFrame], spans: dict, hits: dict, rng: np.random.Generator) -> dict[str, pd.DataFrame]:
    """同じ市場・同じ向きで、同じ数の基準の取引をでたらめに選び、本物と同じ長さだけ新規を止める絞り込み。

    本物の絞り込みは「どの取引を止めるか」を行き過ぎで選ぶ。こちらは数と長さが同じで、選び方だけがでたらめ。
    負けている市場では新規を止めるだけで成績が良く見えるので、その分を差し引いて比べるため（docs/cross_asset.md 8.2）。
    """
    cols = {s: {"long": np.ones(len(f), bool), "short": np.ones(len(f), bool)} for s, f in frames.items()}
    for (sym, col), items in spans.items():
        ds = hits.get((sym, col), [])
        if not ds:
            continue
        pick = rng.choice(len(items), size=len(ds), replace=False)
        for j, d in zip(pick, rng.permutation(ds)):
            p, e = items[j]
            cols[sym][col][p:(e if d < 0 else min(p + d, e))] = False
    return {s: pd.DataFrame(c, index=frames[s].index) for s, c in cols.items()}


def placebo_diffs(group: P.Group, pk0, gates: dict[str, pd.DataFrame], base: dict, n: int, seed: int = SEED,
                  log: Callable[[str], None] = print) -> tuple[np.ndarray, np.ndarray, dict]:
    """偶然 2 種類の、基準とのシャープの差。戻り値は (でたらめに止める, 時期をずらす, 偶然の取引の数の平均)。

    取引の数は読むためだけ（判定に使わない）: でたらめに止める偶然は、本物より実際に減る取引が少なくなりやすい（8.2）。
    """
    rng = np.random.default_rng(seed)
    spans = trade_spans(base["table"], group.frames)
    hits = blocked(spans, gates)
    fracs = rng.uniform(0.15, 0.85, n)
    rnd, shf = np.empty(n), np.empty(n)
    n_rnd, n_shf = np.empty(n), np.empty(n)
    for i in range(n):
        a = run(group, pk0, random_block_gates(group.frames, spans, hits, rng))
        b = run(group, pk0, shifted(gates, fracs[i]))
        rnd[i], shf[i] = a["sharpe"] - base["sharpe"], b["sharpe"] - base["sharpe"]
        n_rnd[i], n_shf[i] = a["trades"], b["trades"]
        if (i + 1) % 20 == 0:
            log(f"  偶然 {i + 1}/{n}")
    return rnd, shf, {"random_trades": float(n_rnd.mean()) if n else float("nan"), "shift_trades": float(n_shf.mean()) if n else float("nan")}


def config_matches(by_gate: dict, by_config: dict) -> bool:
    """作りの確かめ（8.6）: 飛びを除かない絞り込みで足した版と、構成ファイルの「有り」をそのまま当てた版が同じか。"""
    a, b = by_gate["sharpe"], by_config["sharpe"]
    same = (not np.isfinite(a) and not np.isfinite(b)) or (np.isfinite(a) and np.isfinite(b) and abs(a - b) < 1e-9)
    return by_gate["trades"] == by_config["trades"] and same


def better_share(null: np.ndarray, diff: float) -> float | None:
    return float(np.mean(null < diff)) if len(null) else None


def verdict(diff: float, pct_random: float | None, pct_shift: float | None, share: float | None, n_markets: int | None = None) -> str:
    """docs/cross_asset.md 8.3 の判定。条件 4 に当てはまる市場が 1 つも無い（share が None）ときは「通らなかった」。"""
    if n_markets is not None and n_markets < MIN_MARKETS:
        return f"判定できない（判定に残った市場が {n_markets}。{MIN_MARKETS} 未満）"
    if not np.isfinite(diff) or pct_random is None or pct_shift is None:
        return "判定できない（データ不足）"
    ok = diff > 0 and pct_random >= PASS_PCT and pct_shift >= PASS_PCT and (share or 0.0) >= 0.5
    return "通った（使っていない市場でも確かめられた）" if ok else "通らなかった（8.5 の通りに利用者が決める）"
