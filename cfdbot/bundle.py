"""束ねる検証（docs/bundle.md。結果を見る前に決めたもの）。柱ごとの月の系列を、値動きの大きさをそろえて一緒に持つ。

値動きの大きさ（sigmas）・t 値・前半と後半の分け方は、広げた候補（docs/wide.md。cfdbot/wide/core.py）と同じものを使う。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .stats import expected_max_z
from .wide import base
from .wide import core as W

N_TRIES, N_WORLDS, SEED = 100, 20_000, 20261120      # 物差し（4.3）: 試した数・世界の数・乱数の種
BEYOND, LOOKS_GOOD, USELESS = "偶然を超えた", "良く見えるが、選んだ分と区別できない", "使えない"


# --------------------------------------------------------------------------- 月ごとにそろえる（3 章の 1）
def to_monthly(daily: pd.Series) -> pd.Series:
    """日ごとの成績を暦の月ごとに足す（複利にしない）。最後の日の属する月は終わっていないので落とす。間の、取引の無い月は 0。"""
    s = pd.Series(daily).dropna().sort_index()
    m = s.groupby(pd.DatetimeIndex(s.index).map(base.month_end)).sum()
    m = base.finished_months(m, s.index[-1])
    if m.empty:
        return m
    last = base.month_end(pd.Timestamp(s.index[-1]).to_period("M").to_timestamp() - pd.Timedelta(days=1))   # 最後の日の月の、前の月
    return base.fill_months(m, m.index[0], last)


# --------------------------------------------------------------------------- 重さと束（3 章の 2〜4）
def _present(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> dict[str, list[str]]:
    """系列のある家族だけ（家族 → その系列の名前）。"""
    return {f: [m for m in ms if m in series] for f, ms in families.items() if any(m in series for m in ms)}


def weights(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> pd.DataFrame:
    """月ごとの重さ。∝（1 ÷ 前の月までの値動きの大きさ）÷（同じ家族でその月に使える系列の数）。足して 1。使えない系列は欠け。"""
    fam = _present(series, families)
    cols = [m for ms in fam.values() for m in ms]
    d = pd.concat({k: series[k] for k in cols}, axis=1, sort=True)
    sg = pd.concat({k: W.sigmas(series[k].dropna()) for k in cols}, axis=1, sort=True).reindex(d.index)
    ok = d.notna() & sg.notna() & (sg > 0)
    raw = (1.0 / sg).where(ok)
    for ms in fam.values():
        n = ok[ms].sum(axis=1)
        raw[ms] = raw[ms].div(n.where(n > 0), axis=0)
    return raw.div(raw.sum(axis=1, min_count=1), axis=0)


def bundle(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> pd.Series:
    """束の月の系列。使える家族が半分以上ある月だけ（足りない月は入れない）。"""
    fam = _present(series, families)
    w = weights(series, families)
    d = pd.concat({k: series[k] for k in w.columns}, axis=1, sort=True).reindex(w.index)
    usable = pd.DataFrame({f: w[ms].notna().any(axis=1) for f, ms in fam.items()})
    keep = usable.sum(axis=1) >= math.ceil(len(fam) / 2)
    return (w * d).sum(axis=1, min_count=1)[keep]


# --------------------------------------------------------------------------- 束の数字（4.1）
def stats(d: pd.Series) -> dict:
    """月の数・平均・前半と後半の平均・t 値（全期間・後半）・年率の値動き・最大の下げ（足し上げた線の、山からの落ち込み）。"""
    d = d.dropna()
    a, b = W.halves(d)
    line = d.cumsum()
    return {"n": len(d), "mean": float(d.mean()), "mean1": float(a.mean()), "mean2": float(b.mean()),
            "t": W.t_value(d), "t2": W.t_value(b), "vol": float(d.std(ddof=1) * math.sqrt(12)),
            "max_dd": float((line.cummax().clip(lower=0.0) - line).max())}


# --------------------------------------------------------------------------- 物差しと判定（4.2・4.3）
def yardstick(n_months: int, n_tries: int = N_TRIES, n_worlds: int = N_WORLDS, seed: int = SEED) -> dict:
    """何も効いていない世界で、良く見えたものだけを同じ重さで束ねたときの、束の全期間の t 値の分布（50・90・95% の点）。

    1 つの世界: 平均 0・互いに無関係・月の数 n_months の系列を n_tries 本。「前半・後半とも平均がプラス、全期間の t 値 1.65 以上」
    のものだけを選ぶ。選ばれたのが 0 本なら t 値は 0。pass_share は、束が「1 回目として使えそう」に見える世界の割合。
    """
    rng = np.random.default_rng(seed)
    h, root = n_months // 2, math.sqrt(n_months)
    step = max(1, min(n_worlds, 4_000_000 // max(1, n_months * n_tries)))    # 一度に作る世界の数（メモリを使いすぎない）
    ts, passed, picked = [], 0, 0
    for start in range(0, n_worlds, step):
        x = rng.standard_normal((min(step, n_worlds - start), n_months, n_tries))
        sel = ((x[:, :h].mean(1) > 0) & (x[:, h:].mean(1) > 0) & (x.mean(1) / (x.std(1, ddof=1) / root) >= W.T_MIN))
        k = sel.sum(1)
        port = (x * sel[:, None, :]).sum(2) / np.maximum(k, 1)[:, None]
        some = k > 0
        t = np.zeros(len(k))
        t[some] = port[some].mean(1) / (port[some].std(1, ddof=1) / root)
        b = port[some][:, h:]
        t2 = b.mean(1) / (b.std(1, ddof=1) / math.sqrt(n_months - h))
        passed += int(((port[some][:, :h].mean(1) > 0) & (b.mean(1) > 0) & (t[some] >= W.T_MIN) & (t2 >= W.T_MIN)).sum())
        picked += int(k.sum())
        ts.append(t)
    t = np.concatenate(ts)
    return {"q50": float(np.quantile(t, 0.50)), "q90": float(np.quantile(t, 0.90)), "q95": float(np.quantile(t, 0.95)),
            "pass_share": passed / n_worlds, "picked_mean": picked / n_worlds}


def verdict(st: dict, y95: float) -> str:
    """4.2 の 3 つの言葉。物差しの 95% の点（y95）に届いても、1 回目としての線（1.65）は必ず要る。"""
    if st["n"] < base.MIN_MONTHS:
        return W.SHORT
    if st["mean1"] > 0 and st["mean2"] > 0 and st["t"] >= W.T_MIN:
        return BEYOND if st["t2"] >= W.T_MIN and st["t"] >= y95 else LOOKS_GOOD
    return USELESS


# --------------------------------------------------------------------------- 点検（4.1。判定しない）
def family_series(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> dict[str, pd.Series]:
    """家族ごとの月の系列（3 章の重さを、家族の中だけで使ったもの）。"""
    return {f: bundle({m: series[m] for m in ms}, {f: tuple(ms)}) for f, ms in _present(series, families).items()}


def correlations(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> pd.DataFrame:
    """家族どうしの相関の表（どちらにも値のある月で）。"""
    return pd.DataFrame(family_series(series, families)).corr()


def leave_one_out(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> pd.DataFrame:
    """家族を 1 つずつ抜いた束の数字（行 = 抜いた家族）。"""
    fam = _present(series, families)
    rows = {}
    for f in fam:
        rest = {g: tuple(ms) for g, ms in fam.items() if g != f}
        rows[f] = stats(bundle({m: series[m] for ms in rest.values() for m in ms}, rest))
    return pd.DataFrame(rows).T


# --------------------------------------------------------------------------- 登録した柱（docs/bundle.md 2.2。計算の後で足したり外したりしない）
FAMILIES: dict[str, tuple[str, ...]] = {
    "A": ("EA",), "B": ("C2",), "C": ("carry", "combo"), "D": ("M2", "M2a", "M2b"), "E": ("E1", "E2"), "F": ("J7",),
    "G": ("C3",), "H": ("C1", "S1"), "I": ("W7",), "J": ("1C",), "K": ("M1",),
}
FAMILY_NAMES = {"A": "商品のトレンド（今の EA）", "B": "暗号資産のトレンド", "C": "原油のキャリー", "D": "株の値動きの学習（M2）",
                "E": "決算の数字", "F": "決算をまたぐ", "G": "日本の割安株", "H": "米国のモメンタム", "I": "為替のキャリー",
                "J": "文章で近い会社", "K": "夜だけの学習"}
SERIES_NAMES = {"EA": "金・銀・WTI・ブレントのタートル＋行き過ぎの回避", "C2": "BTC・ETH に同じ戦略（GMO のコスト）",
                "carry": "原油のキャリー", "combo": "トレンドとキャリーが揃った月だけ",
                "M2": "M2（上位 10% を 5 日）", "M2a": "M2a（1 日遅れ）", "M2b": "M2b（上位 10 銘柄を週 1 回）",
                "E1": "E1（会社予想を 5% 以上引き上げ）", "E2": "E2（数字で学んだ AI）", "J7": "J7（決算発表をまたぐ）",
                "C3": "日本の割安株（C3）", "C1": "米国のモメンタム（C1）", "S1": "米国の業種のモメンタム（S1）",
                "W7": "為替のキャリー（W7）", "1C": "文章で近い会社のモメンタム（1C）", "M1": "夜だけの学習（M1）"}
SUBS: dict[str, dict[str, tuple[str, ...]]] = {             # 4.1 の ③。判定しない
    "株の柱だけ": {f: FAMILIES[f] for f in "DEFGJK"},
    "株以外の柱だけ": {f: FAMILIES[f] for f in "ABCI"},
    "今の口座で持てる柱だけ": {"A": ("EA",), "B": ("C2",), "D": ("M2b",), "E": ("E1",), "F": ("J7",)},
}
MIN_FAMILIES = 8                                            # 家族がこれに届かなければ、B1 は計算しない（2.2）
DIR = Path("data") / "bundle"
SERIES_DIR, MARKER = DIR / "series", DIR / "B1_DONE.json"   # 柱ごとの月の系列（<名前>.csv: month・d）と、1 回だけの印
PRIOR_TRIALS, TRIAL = 100, 101                              # 登録した日までに試した数と、B1 の通し番号（6 章）
THRESHOLD_REF = expected_max_z(TRIAL)                       # 参考の列だけが使う線（今までの数え方）
RESULT: dict = {}                                           # 1 回計算して docs/bundle.md 8 章に書いたら、ここにも書く（{"date":…, "verdict":…, "trial": 101}）


def load_series(folder) -> tuple[dict[str, pd.Series], dict[str, str]]:
    """登録した名前のファイルだけを読む。返すのは、月末の日付で並んだ系列と、無かった系列の理由。"""
    series, missing = {}, {}
    for k in SERIES_NAMES:
        p = Path(folder) / f"{k}.csv"
        if not p.exists():
            missing[k] = "ファイルが無い"
            continue
        try:
            df = pd.read_csv(p)
        except Exception as e:
            raise base.NotReady(f"{p} を読めない（{type(e).__name__}）") from e
        if not {"month", "d"} <= set(df.columns):
            raise base.NotReady(f"{p} に列 month・d が無い")
        idx = pd.DatetimeIndex(pd.to_datetime(df["month"], errors="coerce"))
        if idx.isna().any() or idx.has_duplicates or not idx.is_monotonic_increasing or (idx != idx.map(base.month_end)).any():
            raise base.NotReady(f"{p} の month が、月末の日付で古い順に並んでいない")
        d = pd.Series(pd.to_numeric(df["d"], errors="coerce").to_numpy(), index=idx).dropna()      # 値の無い月は「その月は無い」
        if d.empty:
            missing[k] = "値が 1 つも無い"
            continue
        series[k] = d
    return series, missing


def check(folder) -> tuple[bool, list[str]]:
    """系列ごとの期間・月の数・欠けだけを見る（成績の数字は出さない）。家族が足りなければ通さない。"""
    try:
        series, missing = load_series(folder)
    except base.NotReady as e:
        return False, [str(e)]
    fam = _present(series, FAMILIES)
    lines = [f"系列 {len(series)}／{len(SERIES_NAMES)} 本、{len(fam)} 家族（登録は {len(FAMILIES)} 家族）"]
    for f, ms in FAMILIES.items():
        for m in ms:
            if m not in series:
                lines.append(f"{f} {FAMILY_NAMES[f]}・{m}: 無い（{missing[m]}）")
                continue
            d = series[m]
            gap = len(pd.period_range(d.index[0], d.index[-1], freq="M")) - len(d)
            lines.append(f"{f} {FAMILY_NAMES[f]}・{m}: {d.index[0]:%Y-%m}〜{d.index[-1]:%Y-%m}（{len(d)} か月" + (f"。間に無い月 {gap}" if gap else "") + "）")
    if len(fam) < MIN_FAMILIES:
        return False, [*lines, f"家族が {MIN_FAMILIES} に届かない（{len(fam)} 家族）ので、B1 は計算しない（docs/bundle.md 2.2）"]
    b = bundle(series, FAMILIES)
    if b.empty:
        return False, [*lines, "束を作れる月が 1 つも無い（使える家族が半分以上そろう月が無い）"]
    lines.append(f"束を作れる月 {b.index[0]:%Y-%m}〜{b.index[-1]:%Y-%m}（{len(b)} か月）"
                 + (f"。{base.MIN_MONTHS} に届かないので、判定は「{W.SHORT}」になる" if len(b) < base.MIN_MONTHS else ""))
    return True, lines


def run(series: dict[str, pd.Series], n_worlds: int | None = None) -> dict:
    """B1 の計算（4 章）。判定するのは 11 家族すべての束 1 本だけ。ほかは点検。"""
    n_worlds = N_WORLDS if n_worlds is None else n_worlds
    fam = _present(series, FAMILIES)
    if len(fam) < MIN_FAMILIES:
        raise base.NotReady(f"家族が {MIN_FAMILIES} に届かない（{len(fam)} 家族）。B1 は計算しない（docs/bundle.md 2.2）")
    b = bundle(series, FAMILIES)
    st = stats(b)
    yard = {"n_months": st["n"], **yardstick(st["n"], N_TRIES, n_worlds, SEED),
            "q95_50": yardstick(st["n"], 50, n_worlds, SEED)["q95"], "q95_200": yardstick(st["n"], 200, n_worlds, SEED)["q95"]}
    c1 = bool(st["mean1"] > 0 and st["mean2"] > 0)
    short = st["n"] < base.MIN_MONTHS
    fs = family_series(series, FAMILIES)
    rows = {f: stats(s.reindex(b.index).dropna()) for f, s in fs.items()}                   # 家族 1 本ずつ（束と同じ月）
    best = max(rows, key=lambda f: rows[f]["t"] if math.isfinite(rows[f]["t"]) else -math.inf)
    subs = {}
    for name, sub in SUBS.items():
        have = {m: series[m] for ms in sub.values() for m in ms if m in series}
        subs[name] = stats(bundle(have, sub)) if have else None
    return {"stats": st, "start": b.index[0], "end": b.index[-1], "n_families": len(fam), "n_series": sum(len(ms) for ms in fam.values()),
            "missing": [m for m in SERIES_NAMES if m not in series], "yard": yard, "verdict": verdict(st, yard["q95"]),
            "first": W.SHORT if short else W.label(c1, st["t"], st["t2"], W.T_MIN),
            "ref": W.SHORT if short else W.label(c1, st["t"], st["t2"], THRESHOLD_REF),
            "families": rows, "best_family": best, "lift": bool(st["t"] > rows[best]["t"]),
            "corr": correlations(series, FAMILIES).reindex(index=list(FAMILIES), columns=list(FAMILIES)),
            "loo": leave_one_out(series, FAMILIES), "subs": subs, "trial": TRIAL}


# --------------------------------------------------------------------------- 印と報告
def done(marker: Path | None = None) -> dict | None:
    """もう計算してあれば、その記録（無ければ None）。プログラムに書いた RESULT が先。壊れた印も「計算した」とみなす。"""
    if RESULT:
        return dict(RESULT)
    marker = MARKER if marker is None else Path(marker)
    if not marker.exists():
        return None
    try:
        rec = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"date": "?", "verdict": "?", "detail": f"{marker} が読めない"}
    return rec if isinstance(rec, dict) and rec.get("verdict") else {"date": "?", "verdict": "?", "detail": f"{marker} の中身が変"}


def mark(marker, res: dict, lines: list[str], made: str) -> dict:
    """判定を出したら、まず印を書く（報告の中身も入れておく）。"""
    marker = Path(marker)
    marker.parent.mkdir(parents=True, exist_ok=True)
    rec = {"date": made, "verdict": res["verdict"], "first": res["first"], "ref": res["ref"], "trial": res["trial"], "report": lines}
    marker.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return rec


def _pct(x, digits: int = 2) -> str:
    return "–" if x is None or not math.isfinite(x) else f"{x * 100:.{digits}f}%".replace("-", "−")


def _f2(x) -> str:
    return "–" if x is None or not math.isfinite(x) else f"{x:.2f}".replace("-", "−")


def _row(name: str, st: dict | None) -> str:
    if st is None or not st["n"]:
        return f"| {name} | – | – | – | – | – | – |"
    ratio = st["max_dd"] / st["vol"] if st["vol"] and math.isfinite(st["vol"]) and st["vol"] > 0 else float("nan")
    return (f"| {name} | {int(st['n'])} | {_pct(st['mean'])} | {_pct(st['mean1'])} → {_pct(st['mean2'])} | {_f2(st['t'])}／{_f2(st['t2'])} | "
            f"{_pct(st['vol'], 1)} | {_pct(st['max_dd'], 1)}（{_f2(ratio)} 倍） |")


def report_lines(res: dict, made: str) -> list[str]:
    st, y = res["stats"], res["yard"]
    head = "| | 月の数 | 月の平均 | 前半 → 後半 | t 値 全期間／後半 | 年率の値動き | 最大の下げ（年率の値動きの何倍か） |"
    L = [f"# 束ねる検証 B1（過去の数字での束。目安）— 作成 {made}", "",
         "決め方は docs/bundle.md（結果を見る前に決めたもの）。1 回だけ計算した。**束の中身は、良く見えたものだけを選んだもの**なので、数字はそのまま信じない。", "",
         "## 1. 判定", "",
         f"- 束: {res['n_families']} 家族・{res['n_series']} 本。{res['start']:%Y-%m}〜{res['end']:%Y-%m} の {st['n']} か月"
         + (f"（用意できなかった系列: {'・'.join(res['missing'])}）" if res["missing"] else ""),
         "", head, "|---|---|---|---|---|---|---|", _row("束（全部の家族）", st), "",
         f"- **判定: {res['verdict']}**",
         (f"- 物差し（効き目の無い {N_TRIES} 個から、同じ決まりで良く見えたものだけを束ねた世界）: 束の t 値は、半分の世界で {_f2(y['q50'])} 以上、"
         f"10 回に 1 回は {_f2(y['q90'])} 以上、20 回に 1 回は **{_f2(y['q95'])}** 以上。本物の束は {_f2(st['t'])}"),
         (f"- 「1 回目として」の判定（全期間・後半とも t 値 {W.T_MIN}）: {res['first']}。参考（通し番号 {TRIAL} の線 {THRESHOLD_REF:.3f}）: {res['ref']}。"
         f"**ただしこの 2 つは、でたらめでも {y['pass_share']:.0%} が「使えそう」になる**（選んだ後の数字なので）"),
         f"- 参考（判定に使わない）: 試した数を 50 個とした物差しの 20 回に 1 回の点は {_f2(y['q95_50'])}、200 個なら {_f2(y['q95_200'])}",
         (f"- 上乗せ: 一番 t 値の高い家族は {res['best_family']} {FAMILY_NAMES[res['best_family']]}（束と同じ月で t 値 {_f2(res['families'][res['best_family']]['t'])}）。"
         f"束はそれより{'高い' if res['lift'] else '高くない'}"),
         "", "## 2. 点検（判定しない。ここから良いものを選ばない）", "", "### 2.1 家族 1 本ずつ（束と同じ月）", "", head, "|---|---|---|---|---|---|---|"]
    L += [_row(f"{f} {FAMILY_NAMES[f]}", res["families"].get(f)) for f in FAMILIES]
    L += ["", "### 2.2 家族どうしの相関（どちらにも値のある月）", "", "| | " + " | ".join(FAMILIES) + " |", "|---|" + "---|" * len(FAMILIES)]
    for f in FAMILIES:
        L.append(f"| {f} {FAMILY_NAMES[f]} | " + " | ".join(_f2(res["corr"].loc[f, g]) if f != g else "–" for g in FAMILIES) + " |")
    L += ["", "### 2.3 家族を 1 つ抜いた束", "", head.replace("| |", "| 抜いた家族 |", 1), "|---|---|---|---|---|---|---|"]
    L += [_row(f"{f} {FAMILY_NAMES[f]}", res["loo"].loc[f].to_dict() if f in res["loo"].index else None) for f in FAMILIES]
    L += ["", "### 2.4 決めてあった小さな束", "", head, "|---|---|---|---|---|---|---|"]
    L += [_row(name, res["subs"][name]) for name in SUBS]
    L += ["", "## 3. データと注意", "",
          "- 系列は、今の値段のデータから作り直したもの（株は今の構成銘柄）。単独の判定はやり直していない",
          "- 株の柱（D・E・F・J・K）は同じ銘柄の上にあり、今残っている会社だけを見ている偏りが残る。G・H は紙の上の組",
          "- 束の中身は年によって変わる（古い年は少ない柱で作られる）",
          "- 本番の判定は B2（2026-12 からの月だけで見る。docs/bundle.md 5 章）"]
    return L
