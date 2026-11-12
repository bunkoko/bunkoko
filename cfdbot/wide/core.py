"""W1〜W7 の 1 つずつの判定と、組み合わせ（WX）のまとめての判定。docs/wide.md 3・5・7 章。

ここでは判定・組み合わせ・偶然との比べ・1 回だけの印・報告の文を作る（ルールごとの d は w1.py〜w7.py、取得と実行は scripts/wide_study.py）。
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from ..stats import expected_max_z
from .base import MIN_EVENTS, MIN_MONTHS, Stream

RULES = ("W1", "W2", "W3", "W4", "W5", "W6", "W7")
NAMES = {"W1": "資産のトレンド（上がっている資産だけ持つ）", "W2": "FOMC の声明の日", "W3": "1 年の高値に近い株",
         "W4": "配当の権利落ちの日", "W5": "公募増資の値決めの後", "W6": "物言う株主の新しい取得", "W7": "金利の高い通貨（為替のキャリー）"}
PRIOR_TRIALS = 93                  # 登録した日（2026-11-11）までに試した数
TRIALS = 101                       # 8 つの判定がどれも使う数（docs/wide.md 7 章。計算する順で分けない・下げない）
THRESHOLD = expected_max_z(TRIALS)   # 2.5341
T_MIN = 1.65                       # 後半だけの t 値の線
P_GOOD, P_MAYBE = 0.0056, 0.05     # WX の p の線（5 章 3・4）
SIGMA_WINDOW, SIGMA_MIN = 36, 12   # 組の重さ: 前の月までの直近 36 か月（12 か月以上あれば）
N_FLIPS, SEED = 10_000, 20261111   # 偶然との比べ（符号の入れ替え）
SHORT = "数が足りない（判定しない）"
MARKER = Path("data") / "wide" / "W_DONE.json"   # 1 回だけの印（リポジトリの根から。報告の保存先とは別）
RESULT: dict = {}                  # 1 回計算して docs/wide.md 10 章に書いたら、ここにも書く（{"date":…, "verdicts": {...}, "trials": …}）。
                                   # 書いてあれば、印のファイルが無い別の機械でも動かさない


# --------------------------------------------------------------------------- 1 つずつの判定（3 章）
def t_value(x) -> float:
    v = np.asarray(pd.Series(x).dropna(), dtype=float)
    if len(v) < 3 or v.std(ddof=1) <= 0:
        return float("nan")
    return float(v.mean() / (v.std(ddof=1) / np.sqrt(len(v))))


def halves(d: pd.Series) -> tuple[pd.Series, pd.Series]:
    """月の数で半分に分ける（奇数なら後半が 1 つ多い）。"""
    d = d.dropna()
    return d.iloc[: len(d) // 2], d.iloc[len(d) // 2:]


def judge(s: Stream) -> dict:
    d = s.d.dropna()
    out = {"key": s.key, "n": len(d), "events": s.events, "gross": s.gross,
           "start": d.index[0] if len(d) else None, "end": d.index[-1] if len(d) else None}
    if len(d) < MIN_MONTHS or (s.events is not None and s.events < MIN_EVENTS):
        return {**out, "verdict": SHORT, "mean": float(d.mean()) if len(d) else float("nan"),
                "mean1": float("nan"), "mean2": float("nan"), "t": float("nan"), "t2": float("nan")}
    a, b = halves(d)
    t_all, t2 = t_value(d), t_value(b)
    c1 = a.mean() > 0 and b.mean() > 0
    if c1 and t2 >= T_MIN and t_all >= THRESHOLD:
        verdict = "使えそう"
    elif c1 and t_all >= T_MIN:
        verdict = "まだ判断できない"
    else:
        verdict = "使えない"
    return {**out, "verdict": verdict, "mean": float(d.mean()), "mean1": float(a.mean()), "mean2": float(b.mean()),
            "t": t_all, "t2": t2, "split": b.index[0]}


# --------------------------------------------------------------------------- 組み合わせ（5 章）
def sigmas(d: pd.Series) -> pd.Series:
    """前の月までの直近 36 か月（暦の月。そのうち値のある月が 12 以上）の標準偏差。

    途中に作らなかった月（W7 など）があっても窓が 36 か月より長くならないよう、月を続けて並べ直してから測る。
    """
    d = d.dropna()
    if d.empty:
        return d
    idx = pd.period_range(d.index[0], d.index[-1], freq="M").to_timestamp(how="end").normalize()
    full = d.reindex(idx)
    return full.shift(1).rolling(SIGMA_WINDOW, min_periods=SIGMA_MIN).std(ddof=1).reindex(d.index)


def combo_series(streams: dict[str, pd.Series], keys: tuple[str, ...]) -> pd.Series:
    """値動きの大きさが同じになる重さで一緒に持った月の系列。入るルールすべてに d と正の σ がある月だけ。"""
    d = pd.concat({k: streams[k] for k in keys}, axis=1, sort=True)
    sg = pd.concat({k: sigmas(streams[k].dropna()) for k in keys}, axis=1, sort=True).reindex(d.index)
    ok = d.notna().all(axis=1) & sg.notna().all(axis=1) & (sg > 0).all(axis=1)
    inv = (1.0 / sg[ok])
    w = inv.div(inv.sum(axis=1), axis=0)
    return (w * d[ok]).sum(axis=1)


def combos(streams: dict[str, pd.Series]) -> tuple[pd.DataFrame, dict[tuple[str, ...], pd.Series], int]:
    """2 つ以上の組をすべて作る。返すのは表（組ごとの数字）・組の系列・月が足りずに使わなかった組の数。"""
    keys = sorted(streams)
    rows, series, dropped = [], {}, 0
    for r in range(2, len(keys) + 1):
        for ks in itertools.combinations(keys, r):
            c = combo_series(streams, ks)
            if len(c) < MIN_MONTHS:
                dropped += 1
                continue
            a, b = halves(c)
            series[ks] = c
            rows.append({"combo": "+".join(ks), "size": len(ks), "n": len(c), "start": c.index[0], "end": c.index[-1],
                         "mean": float(c.mean()), "mean1": float(a.mean()), "mean2": float(b.mean()),
                         "t": t_value(c), "t2": t_value(b)})
    return pd.DataFrame(rows), series, dropped


def flip_pvalue(series: dict[tuple[str, ...], pd.Series], best_t: float, n: int = N_FLIPS, seed: int = SEED) -> float:
    """月ごとに、すべての組の値の符号を同じ向きにそろえてでたらめに入れ替え、「全部の組の中で一番高い t 値」が本物以上になる割合。

    符号を入れ替えても 2 乗の和は変わらないので、和だけを入れ替えて t 値を出す（速い）。
    """
    m = pd.concat(series, axis=1, sort=True)           # 月 × 組（無い月は NaN）
    x = m.to_numpy(dtype=float)
    mask = np.isfinite(x)
    x0 = np.where(mask, x, 0.0)
    cnt = mask.sum(axis=0).astype(float)
    s2 = (x0 ** 2).sum(axis=0)
    rng = np.random.default_rng(seed)
    hits, chunk = 0, 1000
    for start in range(0, n, chunk):
        k = min(chunk, n - start)
        signs = rng.choice((-1.0, 1.0), size=(k, x.shape[0]))
        s1 = signs @ x0                                  # 引き × 組
        mean = s1 / cnt
        var = (s2 - cnt * mean ** 2) / (cnt - 1)
        with np.errstate(divide="ignore", invalid="ignore"):
            t = np.where(var > 0, mean / np.sqrt(var / cnt), np.nan)
        hits += int((np.nanmax(t, axis=1) >= best_t - 1e-12).sum())
    return (hits + 1) / (n + 1)


def judge_combos(single: dict[str, dict], streams: dict[str, Stream], n_flips: int = N_FLIPS) -> dict:
    """WX の判定。単独の判定が出たルール（数が足りないものは除く）が 2 つ以上なければ「作れない」。"""
    use = {k: streams[k].d.dropna() for k, j in single.items() if j["verdict"] != SHORT}
    if len(use) < 2:
        return {"verdict": "作れない", "why": f"判定の出たルールが {len(use)} つ（2 つ以上要る）", "table": pd.DataFrame(), "dropped": 0}
    table, series, dropped = combos(use)
    if table.empty:
        return {"verdict": "作れない", "why": f"月が {MIN_MONTHS} に届く組が無い", "table": table, "dropped": dropped}
    best = table.sort_values(["t", "size", "combo"], ascending=[False, True, True], na_position="last").iloc[0]
    key = tuple(best["combo"].split("+"))
    p = flip_pvalue(series, float(best["t"]), n=n_flips)
    pos = best["mean1"] > 0 and best["mean2"] > 0
    if pos and best["t2"] >= T_MIN and p <= P_GOOD:
        verdict = "使えそう"
    elif pos and p <= P_MAYBE:
        verdict = "まだ判断できない"
    else:
        verdict = "使えない"
    member_best = max(single[k]["t"] for k in key)
    # p = 1（本物の一番がどの入れ替えの一番にも届かない。どの組も大きく負けたとき）でも止まらない（inv_cdf(0) は落ちる）
    z = NormalDist().inv_cdf(1 - p) if p < 1 else float("-inf")
    return {"verdict": verdict, "best": best.to_dict(), "p": p, "z": z,
            "no_lift": bool(best["t"] < member_best), "member_best": member_best,
            "table": table, "dropped": dropped, "n_combos": len(table), "rules": sorted(use)}


# --------------------------------------------------------------------------- 1 回だけの印
def done(marker: Path = MARKER) -> dict | None:
    """もう計算してあれば、その記録（無ければ None）。プログラムに書いた RESULT が先。"""
    if RESULT:
        return dict(RESULT)
    if marker.exists():
        try:
            rec = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001  壊れた印も「計算した」とみなす
            return {"date": "?", "verdicts": {}, "detail": f"{marker} が読めない"}
        if not (isinstance(rec, dict) and rec.get("verdicts")):
            return {"date": "?", "verdicts": {}, "detail": f"{marker} の中身が変"}
        return rec
    return None


def trials_used(single: dict[str, dict], wx: dict) -> int:
    """判定を出した分だけ数える（数が足りない・外した・作れないは入れない）。基準は TRIALS のまま。"""
    n = sum(1 for j in single.values() if j["verdict"] != SHORT)
    return PRIOR_TRIALS + n + (1 if wx["verdict"] != "作れない" else 0)


def mark(marker: Path, single: dict[str, dict], wx: dict, lines: list[str], made: str) -> dict:
    """判定を出したら、まず印を書く（報告の中身も入れておく）。"""
    marker.parent.mkdir(parents=True, exist_ok=True)
    verdicts = {k: j["verdict"] for k, j in single.items()}
    verdicts["WX"] = wx["verdict"]
    rec = {"date": made, "verdicts": verdicts, "trials": trials_used(single, wx), "report": lines}
    marker.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return rec


# --------------------------------------------------------------------------- 報告
def _pct(x, digits: int = 2) -> str:
    return f"{x:.{digits}%}" if x is not None and np.isfinite(x) else "–"


def _f2(x) -> str:
    return f"{x:.2f}" if x is not None and np.isfinite(x) else "–"


def _ym(ts) -> str:
    return f"{pd.Timestamp(ts):%Y-%m}" if ts is not None and not pd.isna(ts) else "–"


def report_lines(single: dict[str, dict], wx: dict, streams: dict[str, Stream], skipped: dict[str, str], made: str) -> list[str]:
    L = ["# 広げた候補 7 つと、その組み合わせ（W1〜W7・WX）", "",
         f"作成 {made}。仮説と判定の条件は docs/wide.md（結果を見る前に決めたもの）。この計算は 1 回だけで、動かし直さない。", "",
         "## 1. 判定", "",
         "| 記号 | 中身 | 月の平均（コストの後） | 前半 → 後半 | t 値 全期間／後半 | 月・事象の数 | 判定 |",
         "|---|---|---|---|---|---|---|"]
    for k in RULES:
        if k in skipped:
            L.append(f"| {k} | {NAMES[k]} | – | – | – | – | 外した（{skipped[k]}） |")
            continue
        j = single[k]
        ev = f"{j['n']} か月" + (f"・{j['events']} 件" if j["events"] is not None else "")
        L.append(f"| {k} | {NAMES[k]} | {_pct(j['mean'])} | {_pct(j['mean1'])} → {_pct(j['mean2'])} | "
                 f"{_f2(j['t'])}／{_f2(j['t2'])} | {ev} | **{j['verdict']}** |")
    if wx["verdict"] == "作れない":
        L.append(f"| WX | 組み合わせ | – | – | – | – | 作れない（{wx['why']}） |")
    else:
        b = wx["best"]
        L.append(f"| WX | 組み合わせ（代表 {b['combo']}） | {_pct(b['mean'])} | {_pct(b['mean1'])} → {_pct(b['mean2'])} | "
                 f"{_f2(b['t'])}／{_f2(b['t2'])} | {int(b['n'])} か月 | **{wx['verdict']}** |")
    used = trials_used(single, wx)
    L += ["",
          f"- 基準: 全期間の t 値 {THRESHOLD:.3f}（{TRIALS} 個試した場合。どの判定も同じ）。後半だけの t 値 {T_MIN}。前半・後半は月の数で半分",
          f"- 試した数: {PRIOR_TRIALS} → **{used}**（判定を出した分だけ。基準は {TRIALS} のまま下げない）"]
    if wx["verdict"] != "作れない":
        L.append(f"- WX: {len(wx['rules'])} つのルール（{'・'.join(wx['rules'])}）から {wx['n_combos']} 通りの組を作った"
                 f"（月が {MIN_MONTHS} に届かず使わなかった組 {wx['dropped']}）。代表 = 一番 t 値の高い組。"
                 f"偶然との比べ（月ごとに全部の組の符号を同じ向きに入れ替え {N_FLIPS:,} 回）の p = {wx['p']:.4f}（正規分布の値で {_f2(wx['z'])}。"
                 f"「使えそう」は p ≤ {P_GOOD}、「まだ判断できない」は p ≤ {P_MAYBE}）")
        if wx["no_lift"]:
            L.append(f"- 読み方（5 章の決め）: 代表の t 値 {_f2(wx['best']['t'])} は、入っている単独のルールの一番高い t 値 {_f2(wx['member_best'])} より低い"
                     " → **組み合わせの上乗せは無い**")
    L += ["- 「使えそう」でもお金は入れない。次の段は記録だけの売買を新しく登録すること（利用者が決める）。W3〜W6 は今残っている 490 社だけの検証",
          "", "## 2. 組み合わせの表（見るだけ。代表のほかは判定しない。ここから良さそうな組を選び直さない）", ""]
    if wx["verdict"] == "作れない" or wx["table"].empty:
        L.append("- 組は作れなかった")
    else:
        L += ["| 組 | 月の数 | 月の平均 | 前半 → 後半 | t 値 全期間／後半 |", "|---|---|---|---|---|"]
        for r in wx["table"].sort_values(["size", "combo"]).itertuples(index=False):
            L.append(f"| {r.combo} | {r.n} | {_pct(r.mean)} | {_pct(r.mean1)} → {_pct(r.mean2)} | {_f2(r.t)}／{_f2(r.t2)} |")
    L += ["", "## 3. データと注意", ""]
    for k in RULES:
        if k in skipped or k not in streams:
            continue
        s, j = streams[k], single[k]
        L.append(f"- {k}: {_ym(j['start'])}〜{_ym(j['end'])}。コストを引く前の月の平均 {_pct(s.gross)}")
        L += [f"  - {x}" for x in s.notes]
    L += ["- 個別株の 1 日の値動きは ±30% にそろえた。税金は入れていない",
          "- W5・W6 は買った月に数える（持つ期間は次の月にかかる）。組の重さに使う「前の月まで」の値の一部は、その月の中で決まる（影響はごく小さい。条件どおりに計算した）",
          "- 結果を見てルールの決め方・期間・コスト・名前の一覧を替えて試し直さない（docs/wide.md）"]
    return L
