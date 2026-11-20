"""束ねる検証（docs/bundle.md。結果を見る前に決めたもの）。柱ごとの月の系列を、値動きの大きさをそろえて一緒に持つ。

値動きの大きさ（sigmas）・t 値・前半と後半の分け方は、広げた候補（docs/wide.md。cfdbot/wide/core.py）と同じものを使う。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .stats import expected_max_z
from .wide import base
from .wide import core as W

N_TRIES, N_WORLDS, SEED = 100, 20_000, 20261120      # 物差し 1（4.3）: 試した数・世界の数・乱数の種
N_WORLDS_FAM = 5_000                                 # 物差し 2（4.3）: 世界の数（乱数の種は同じ。家族ごとに別の並び）
LAST_MONTH = pd.Timestamp("2026-11-30")              # B1 が使う最後の月（3 章の 1。B2 は 2026-12 から）
_BATCH = 4_000                                       # 物差し 2: 線を通る乱数を探すとき、一度に引く本数（結果には関わらない）
BEYOND, LOOKS_GOOD, USELESS = "偶然を超えた", "良く見えるが、選んだ分と区別できない", "使えない"


# --------------------------------------------------------------------------- 月ごとにそろえる（3 章の 1）
def to_monthly(daily: pd.Series) -> pd.Series:
    """日ごとの成績を暦の月ごとに足す（複利にしない）。最後の日の属する月は終わっていないので落とす。間の、取引の無い月は 0。

    始まりは、最初に値が 0 でなくなった日（まだ何も持っていない期間は入れない。元の検証の cfdbot.stocks.month_sum と同じ数え方）。
    """
    s = pd.Series(daily).dropna().sort_index()
    moved = np.flatnonzero(s.to_numpy() != 0)
    if len(moved) == 0:
        return pd.Series(dtype=float)
    last_day = s.index[-1]
    s = s.iloc[moved[0]:]
    m = s.groupby(pd.DatetimeIndex(s.index).map(base.month_end)).sum()
    m = base.finished_months(m, last_day)
    if m.empty:
        return m
    last = base.month_end(pd.Timestamp(last_day).to_period("M").to_timestamp() - pd.Timedelta(days=1))      # 最後の日の月の、前の月
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


def usable_families(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> pd.Series:
    """月ごとの、使える家族の数（その月に値と値動きの大きさのある系列が 1 本でもある家族）。"""
    w = weights(series, families)
    return pd.DataFrame({f: w[ms].notna().any(axis=1) for f, ms in _present(series, families).items()}).sum(axis=1)


def bundle(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]], need: int | None = None) -> pd.Series:
    """束の月の系列。使える家族が半分以上ある月だけ（足りない月は入れない）。need を渡すと、その数以上の月。"""
    fam = _present(series, families)
    w = weights(series, families)
    d = pd.concat({k: series[k] for k in w.columns}, axis=1, sort=True).reindex(w.index)
    usable = pd.DataFrame({f: w[ms].notna().any(axis=1) for f, ms in fam.items()})
    keep = usable.sum(axis=1) >= (math.ceil(len(fam) / 2) if need is None else need)
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


# --------------------------------------------------------------------------- 物差し 2（4.3）: 柱の数と期間を、本物の束にそろえた世界
def null_pools(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]], n_worlds: int,
               seed: int = SEED) -> dict[str, tuple[pd.DatetimeIndex, np.ndarray]]:
    """家族ごとに、自分の期間で「1 回目の線」（前半・後半とも平均がプラス、全期間の t 値 1.65 以上）を通った乱数（世界 × 月）。

    線を通ったかは、家族の中の系列を値のある月ごとに平均したもので見る（系列 ＝ 乱数。本物が 0 の月は 0）。乱数の並びは家族ごとに別。
    """
    order, pools = list(families), {}
    for f, ms in _present(series, families).items():
        idx = series[ms[0]].index
        for m in ms[1:]:
            idx = idx.union(series[m].index)
        held = pd.DataFrame({m: (series[m] != 0).astype(float) for m in ms}).reindex(idx)       # 値の無い月は欠け
        share = held.mean(axis=1).to_numpy()                 # 家族の平均が、その月に乱数の何倍か（誰も持たない月は 0）
        if not (share > 0).any():
            raise base.NotReady(f"家族 {f} の系列は、どの月も 0（物差し 2 を作れない）")
        n, h = len(idx), len(idx) // 2
        rng = np.random.default_rng([seed, order.index(f)])
        kept, got = [], 0
        while got < n_worlds:
            x = rng.standard_normal((_BATCH, n))
            c = x * share
            ok = (c[:, :h].mean(1) > 0) & (c[:, h:].mean(1) > 0) & (c.mean(1) / (c.std(1, ddof=1) / math.sqrt(n)) >= W.T_MIN)
            kept.append(x[ok])
            got += int(ok.sum())
        pools[f] = (idx, np.concatenate(kept)[:n_worlds])
    return pools


def null_world(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]], pools: dict, w: int, since=None) -> dict[str, pd.Series]:
    """w 番目の世界の系列。本物と同じ月に乱数を置く（家族の中は同じ乱数。本物が 0 の月 ＝ 持たなかった月は 0 のまま）。"""
    out = {}
    for f, ms in _present(series, families).items():
        idx, pool = pools[f]
        noise = pd.Series(pool[w], index=idx)
        for m in ms:
            s = noise.reindex(series[m].index).where(series[m] != 0, 0.0)
            out[m] = s if since is None else s[s.index >= since]
    return out


def yardstick_families(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]], n_worlds: int = N_WORLDS_FAM,
                       seed: int = SEED, ref: float | None = None) -> dict:
    """効き目の無い家族が、どれも自分の期間で 1 回目の線を通っていた世界を、本物と同じ作り方で束ねたときの t 値の分布。

    share_first・share_ref は、その世界の束が「1 回目として」・参考の線（ref）で「使えそう」に見える割合。
    """
    ref = THRESHOLD_REF if ref is None else ref
    real = bundle(series, families)
    if real.empty:
        raise base.NotReady("束を作れる月が 1 つも無い（物差し 2 を作れない）")
    pools = null_pools(series, families, n_worlds, seed)
    since = base.month_end(real.index[0] - pd.DateOffset(months=W.SIGMA_WINDOW + 1))      # これより前の月は、束の月の重さに関わらない
    ts, first, by_ref = np.empty(n_worlds), 0, 0
    for w in range(n_worlds):
        st = stats(bundle(null_world(series, families, pools, w, since), families).reindex(real.index).dropna())
        ts[w] = st["t"]
        both = bool(st["mean1"] > 0 and st["mean2"] > 0)
        first += W.label(both, st["t"], st["t2"], W.T_MIN) == "使えそう"
        by_ref += W.label(both, st["t"], st["t2"], ref) == "使えそう"
    return {"q50": float(np.quantile(ts, 0.50)), "q90": float(np.quantile(ts, 0.90)), "q95": float(np.quantile(ts, 0.95)),
            "share_first": first / n_worlds, "share_ref": by_ref / n_worlds, "n_worlds": n_worlds}


# --------------------------------------------------------------------------- 点検（4.1。判定しない）
def family_series(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> dict[str, pd.Series]:
    """家族ごとの月の系列（3 章の重さを、家族の中だけで使ったもの）。"""
    return {f: bundle({m: series[m] for m in ms}, {f: tuple(ms)}) for f, ms in _present(series, families).items()}


def correlations(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> pd.DataFrame:
    """家族どうしの相関の表（どちらにも値のある月で）。"""
    return pd.DataFrame(family_series(series, families)).corr()


def leave_one_out(series: dict[str, pd.Series], families: dict[str, tuple[str, ...]]) -> pd.DataFrame:
    """家族を 1 つずつ抜いた束の数字（行 = 抜いた家族）。もとの束と同じ月で数える（残った家族で重さを足して 1 にする）。

    抜いた束をそのまま作ると、家族の数で「半分」の線が変わって別の期間の数字になり、抜いた効果と期間の違いが混ざる。
    """
    fam = _present(series, families)
    whole = bundle(series, families)
    rows = {}
    for f in fam:
        rest = {g: tuple(ms) for g, ms in fam.items() if g != f}
        rows[f] = stats(bundle({m: series[m] for ms in rest.values() for m in ms}, rest, need=1).reindex(whole.index).dropna())
    return pd.DataFrame(rows).T


def sigma_b2(series: dict[str, pd.Series]) -> dict[str, float]:
    """B2 で固定する値動きの大きさ（5 章）: 系列ごとに、その系列の最後の月までの直近 36 暦月（値のある月が 12 以上）の標準偏差。"""
    out = {}
    for k, d in series.items():
        d = d.dropna()
        nxt = base.month_end(d.index[-1] + pd.Timedelta(days=1))                  # 最後の月の、次の月（その月の重さに使う値を取り出す）
        out[k] = float(W.sigmas(pd.concat([d, pd.Series([0.0], index=[nxt])])).iloc[-1])
    return out


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
SEAL = "EXPORT_DONE.json"                                   # 書き出しが最後まで終わった印（SERIES_DIR の中。系列ごとの様子とファイルの指紋）
PRIOR_TRIALS, TRIAL = 100, 101                              # 登録した日までに試した数と、B1 の通し番号（6 章）
THRESHOLD_REF = expected_max_z(TRIAL)                       # 参考の列だけが使う線（今までの数え方）
RESULT: dict = {}                                           # 1 回計算して docs/bundle.md 8 章に書いたら、ここにも書く（{"date":…, "verdict":…, "trial": 101}）
# 登録の表（2.2）の「記録の期間」: 月の系列にしたときの始まりと終わり（終わりが None のものは、書き出した月の前の月まで）
EXPECTED: dict[str, tuple[str, str | None]] = {
    "EA": ("2001-01", None), "C2": ("2014-09", None), "carry": ("1986-03", "2024-03"), "combo": ("1987-01", "2024-03"),
    "M2": ("2010-01", None), "M2a": ("2010-01", None), "M2b": ("2010-01", None), "E1": ("2016-10", None), "E2": ("2019-01", None),
    "J7": ("2009-10", None), "C3": ("1990-07", None), "C1": ("1927-01", None), "S1": ("1927-01", None), "W7": ("1999-08", None),
    "1C": ("2017-07", None), "M1": ("2010-01", None),
}
START_SLACK, END_SLACK = 12, 3                              # 始まりは 12 か月、終わりは 3 か月までのずれは見逃す（それより大きければ ⚠）


def load_series(folder) -> tuple[dict[str, pd.Series], dict[str, str]]:
    """登録した名前のファイルだけを読む。返すのは、月末の日付で並んだ系列（2026-11 まで）と、無かった系列の理由。"""
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
        d = d[d.index <= LAST_MONTH]                                # B1 は 2026-11 までの月だけ（B2 の月と重ねない）
        if d.empty:
            missing[k] = "値が 1 つも無い"
            continue
        series[k] = d
    return series, missing


# --------------------------------------------------------------------------- 書き出しの終わった印
def fingerprint(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def seal(folder, status: dict[str, str] | None = None, made: str = "") -> dict:
    """書き出しが最後まで終わったら書く印。16 本それぞれの様子と、書いたファイルの指紋を残す（途中で止まった書き出しには印が無い）。"""
    folder = Path(folder)
    files = {k: fingerprint(folder / f"{k}.csv") for k in SERIES_NAMES if (folder / f"{k}.csv").exists()}
    rec = {"date": made, "series": {k: (status or {}).get(k, "書いた" if k in files else "作れなかった") for k in SERIES_NAMES}, "sha256": files}
    (folder / SEAL).write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return rec


def read_seal(folder) -> tuple[dict | None, str | None]:
    """印と、印が使えない理由（使えるなら None）。書き出しの後でファイルが変わっていないかも見る。"""
    folder = Path(folder)
    path = folder / SEAL
    if not path.exists():
        return None, "書き出しが終わった印が無い（--export をまだ動かしていないか、途中で止まった）。--export を動かし直す"
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        files = dict(rec["sha256"])
    except Exception:  # noqa: BLE001
        return None, f"{path} が読めない。--export を動かし直す"
    for k in SERIES_NAMES:
        p = folder / f"{k}.csv"
        if p.exists() != (k in files):
            return None, f"{k}.csv が、書き出しの後で{'置かれている' if p.exists() else '消えている'}。--export を動かし直す"
        if p.exists() and fingerprint(p) != files[k]:
            return None, f"{k}.csv が、書き出しの後で変わっている。--export を動かし直す"
    return rec, None


def _months_between(a: pd.Timestamp, b: pd.Timestamp) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def _shape_warning(k: str, d: pd.Series, newest: pd.Timestamp) -> str | None:
    """登録の表（2.2）の期間と見比べる（成績は見ない）。大きくずれていたら、その理由の文。"""
    start, end = EXPECTED[k]
    off = _months_between(pd.Timestamp(start), d.index[0])
    if abs(off) > START_SLACK:
        return f"⚠ {k}: 始まりが {d.index[0]:%Y-%m}。登録の表（{start}）と {abs(off)} か月ずれている（違う系列を拾っていないか）"
    if end is not None and abs(_months_between(pd.Timestamp(end), d.index[-1])) > END_SLACK:
        return f"⚠ {k}: 終わりが {d.index[-1]:%Y-%m}。登録の表（{end}）とずれている"
    if end is None and _months_between(d.index[-1], newest) > END_SLACK:
        return f"⚠ {k}: 終わりが {d.index[-1]:%Y-%m}。ほかの系列（{newest:%Y-%m} まで）より {_months_between(d.index[-1], newest)} か月古い"
    return None


def check(folder, without: tuple[str, ...] = ()) -> tuple[bool, list[str]]:
    """系列ごとの期間・月の数・欠け・0 の月の数だけを見る（成績の数字は出さない）。⚠ で始まる行は、登録の表と形が違う印。

    通らないのは: 書き出しの印が無い／無い系列を名指し（without）していない／家族が足りない／束の月が 60 に届かない。
    """
    rec, problem = read_seal(folder)
    if problem:
        return False, [problem]
    try:
        series, missing = load_series(folder)
    except base.NotReady as e:
        return False, [str(e)]
    fam = _present(series, FAMILIES)
    lines = [f"系列 {len(series)}／{len(SERIES_NAMES)} 本、{len(fam)} 家族（登録は {len(FAMILIES)} 家族）。書き出したのは {rec.get('date') or '?'}"]
    ends = [d.index[-1] for k, d in series.items() if EXPECTED[k][1] is None]
    newest = max(ends) if ends else None
    warns = []
    for f, ms in FAMILIES.items():
        for m in ms:
            if m not in series:
                lines.append(f"{f} {FAMILY_NAMES[f]}・{m}: 無い（{rec['series'].get(m) or missing[m]}）")
                continue
            d = series[m]
            gap = len(pd.period_range(d.index[0], d.index[-1], freq="M")) - len(d)
            idle = int((d == 0).sum())
            late = int((d[d.index > d.index[-1] - pd.DateOffset(months=W.SIGMA_WINDOW)] == 0).sum())
            lines.append(f"{f} {FAMILY_NAMES[f]}・{m}: {d.index[0]:%Y-%m}〜{d.index[-1]:%Y-%m}（{len(d)} か月" + (f"。間に無い月 {gap}" if gap else "")
                         + (f"。0 の月 {idle}、うち最後の {W.SIGMA_WINDOW} か月に {late}" if idle else "") + "）")
            warn = _shape_warning(m, d, newest if newest is not None else d.index[-1])
            if warn:
                warns.append(warn)
    lines += warns
    gone = [m for m in SERIES_NAMES if m not in series]
    unknown = [m for m in without if m not in SERIES_NAMES]
    if unknown:
        return False, [*lines, f"--without に、登録に無い名前がある: {'・'.join(unknown)}"]
    if set(without) != set(gone):
        if not gone:
            return False, [*lines, f"--without で名指しした系列（{'・'.join(without)}）は、ある。ある系列は外せない"]
        how = (f"無い系列: {'・'.join(gone)}。作り直せるなら --export をやり直す。外して進めるなら、理由を docs/bundle.md 8 章に書いてから "
               f"--without {','.join(gone)}（実際に無い系列とぴったり同じ名指しのときだけ進む）")
        return False, [*lines, how]
    if len(fam) < MIN_FAMILIES:
        return False, [*lines, f"家族が {MIN_FAMILIES} に届かない（{len(fam)} 家族）ので、B1 は計算しない（docs/bundle.md 2.2）"]
    b = bundle(series, FAMILIES)
    if b.empty:
        return False, [*lines, "束を作れる月が 1 つも無い（使える家族が半分以上そろう月が無い）"]
    lines.append(f"束を作れる月 {b.index[0]:%Y-%m}〜{b.index[-1]:%Y-%m}（{len(b)} か月）")
    if len(b) < base.MIN_MONTHS:
        return False, [*lines, f"束の月が {base.MIN_MONTHS} に届かないので、判定しない（計算もしない。docs/bundle.md 4.2）"]
    return True, lines


def run(series: dict[str, pd.Series], n_worlds: int | None = None, n_worlds_fam: int | None = None) -> dict:
    """B1 の計算（4 章）。判定するのは 11 家族すべての束 1 本だけ。ほかは点検（束と同じ月で数える）。"""
    n_worlds = N_WORLDS if n_worlds is None else n_worlds
    n_worlds_fam = N_WORLDS_FAM if n_worlds_fam is None else n_worlds_fam
    fam = _present(series, FAMILIES)
    if len(fam) < MIN_FAMILIES:
        raise base.NotReady(f"家族が {MIN_FAMILIES} に届かない（{len(fam)} 家族）。B1 は計算しない（docs/bundle.md 2.2）")
    b = bundle(series, FAMILIES)
    if len(b) < base.MIN_MONTHS:
        raise base.NotReady(f"束の月が {base.MIN_MONTHS} に届かない（{len(b)} か月）。判定しない（docs/bundle.md 4.2）")
    st = stats(b)
    yard = {"n_months": st["n"], **yardstick(st["n"], N_TRIES, n_worlds, SEED),
            "q95_50": yardstick(st["n"], 50, n_worlds, SEED)["q95"], "q95_200": yardstick(st["n"], 200, n_worlds, SEED)["q95"]}
    yard2 = yardstick_families(series, FAMILIES, n_worlds_fam, SEED, THRESHOLD_REF)
    line = max(yard["q95"], yard2["q95"])                           # 4.2 の線は、2 本の物差しの高いほう
    c1 = bool(st["mean1"] > 0 and st["mean2"] > 0)
    frame = pd.DataFrame(family_series(series, FAMILIES)).reindex(index=b.index, columns=list(FAMILIES))
    rows = {f: stats(frame[f].dropna()) for f in fam}               # 家族 1 本ずつ（束の月のうち、その家族に値のある月）
    best = max(rows, key=lambda f: rows[f]["t"] if math.isfinite(rows[f]["t"]) else -math.inf)
    lift_t = stats(b.reindex(frame[best].dropna().index))["t"]      # 上乗せ: 束も、その家族に値のある月だけで測る（同じ月で比べる）
    subs = {}
    for name, sub in SUBS.items():
        have = {m: series[m] for ms in sub.values() for m in ms if m in series}
        subs[name] = stats(bundle(have, sub).reindex(b.index).dropna()) if have else None        # 束の月の中で、自分の家族が半分以上そろう月
    w = weights(series, FAMILIES).reindex(b.index)
    return {"stats": st, "start": b.index[0], "end": b.index[-1], "n_families": len(fam), "n_series": sum(len(ms) for ms in fam.values()),
            "missing": [m for m in SERIES_NAMES if m not in series], "yard": yard, "yard2": yard2, "line": line, "verdict": verdict(st, line),
            "first": W.label(c1, st["t"], st["t2"], W.T_MIN), "ref": W.label(c1, st["t"], st["t2"], THRESHOLD_REF),
            "families": rows, "best_family": best, "lift_t": lift_t, "lift": bool(lift_t > rows[best]["t"]),
            "corr": correlations(series, FAMILIES).reindex(index=list(FAMILIES), columns=list(FAMILIES)),
            "loo": leave_one_out(series, FAMILIES), "subs": subs, "trial": TRIAL,
            "bundle": b, "usable": usable_families(series, FAMILIES).reindex(b.index), "weights": w, "family_frame": frame,
            "sigma_b2": sigma_b2(series), "last_months": {k: d.index[-1] for k, d in series.items()}}


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


def mark(marker, res: dict, lines: list[str], made: str, **extra) -> dict:
    """判定を出したら、まず印を書く（報告の中身・B2 で使う値動きの大きさ・入力の指紋なども入れておく）。"""
    marker = Path(marker)
    marker.parent.mkdir(parents=True, exist_ok=True)
    rec = {"date": made, "verdict": res["verdict"], "first": res["first"], "ref": res["ref"], "trial": res["trial"],
           "sigma_b2": {k: (v if math.isfinite(v) else None) for k, v in res["sigma_b2"].items()}, **extra, "report": lines}
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
    st, y, y2 = res["stats"], res["yard"], res["yard2"]
    best = res["best_family"]
    head = "| | 月の数 | 月の平均 | 前半 → 後半 | t 値 全期間／後半 | 年率の値動き | 最大の下げ（年率の値動きの何倍か） |"
    L = [f"# 束ねる検証 B1（過去の数字での束。目安）— 作成 {made}", "",
         "決め方は docs/bundle.md（結果を見る前に決めたもの）。1 回だけ計算した。**束の中身は、良く見えたものだけを選んだもの**なので、数字はそのまま信じない。", "",
         "## 1. 判定", "",
         f"- 束: {res['n_families']} 家族・{res['n_series']} 本。{res['start']:%Y-%m}〜{res['end']:%Y-%m} の {st['n']} か月"
         + (f"（用意できなかった系列: {'・'.join(res['missing'])}）" if res["missing"] else ""),
         "", head, "|---|---|---|---|---|---|---|", _row("束（全部の家族）", st), "",
         f"- **判定: {res['verdict']}**",
         (f"- 物差し 1（効き目の無い {N_TRIES} 個から、同じ決まりで良く見えたものだけを束ねた世界。選ばれるのは平均 {y['picked_mean']:.1f} 個）: "
          f"束の t 値は、半分の世界で {_f2(y['q50'])} 以上、10 回に 1 回は {_f2(y['q90'])} 以上、20 回に 1 回は **{_f2(y['q95'])}** 以上"),
         (f"- 物差し 2（効き目の無い {res['n_families']} 家族が、どれも自分の期間で 1 回目の線を通っていた世界を、本物と同じ作り方で束ねた）: "
          f"半分の世界で {_f2(y2['q50'])} 以上、10 回に 1 回は {_f2(y2['q90'])} 以上、20 回に 1 回は **{_f2(y2['q95'])}** 以上"),
         f"- 判定の線は、2 本の高いほうの **{_f2(res['line'])}**。本物の束は {_f2(st['t'])}",
         (f"- 「1 回目として」の判定（全期間・後半とも t 値 {W.T_MIN}）: {res['first']}。参考（通し番号 {TRIAL} の線 {THRESHOLD_REF:.3f}）: {res['ref']}。"
          f"**ただしこの 2 つは、でたらめ（物差し 2 の世界）でも {y2['share_first']:.0%}・{y2['share_ref']:.0%} が「使えそう」になる**（選んだ後の数字なので）"),
         f"- 参考（判定に使わない）: 物差し 1 で、試した数を 50 個としたときの 20 回に 1 回の点は {_f2(y['q95_50'])}、200 個なら {_f2(y['q95_200'])}",
         (f"- 上乗せ: 一番 t 値の高い家族は {best} {FAMILY_NAMES[best]}（その家族に値のある {int(res['families'][best]['n'])} か月で t 値 {_f2(res['families'][best]['t'])}）。"
          f"同じ月だけで測った束の t 値は {_f2(res['lift_t'])} で、それより{'高い' if res['lift'] else '高くない'}"),
         "", "## 2. 点検（判定しない。ここから良いものを選ばない）", "",
         f"どの表も、束と同じ月（{res['start']:%Y-%m}〜{res['end']:%Y-%m}）の中で数えた。", "",
         "### 2.1 家族 1 本ずつ（束の月のうち、その家族に値のある月）", "", head, "|---|---|---|---|---|---|---|"]
    L += [_row(f"{f} {FAMILY_NAMES[f]}", res["families"].get(f)) for f in FAMILIES]
    L += ["", "### 2.2 家族どうしの相関（どちらにも値のある月。束の月より前も含む）", "", "| | " + " | ".join(FAMILIES) + " |", "|---|" + "---|" * len(FAMILIES)]
    for f in FAMILIES:
        L.append(f"| {f} {FAMILY_NAMES[f]} | " + " | ".join(_f2(res["corr"].loc[f, g]) if f != g else "–" for g in FAMILIES) + " |")
    L += ["", "### 2.3 家族を 1 つ抜いた束（残った家族で重さを足して 1 にする）", "", head.replace("| |", "| 抜いた家族 |", 1), "|---|---|---|---|---|---|---|"]
    L += [_row(f"{f} {FAMILY_NAMES[f]}", res["loo"].loc[f].to_dict() if f in res["loo"].index else None) for f in FAMILIES]
    L += ["", "### 2.4 決めてあった小さな束（束の月のうち、自分の家族が半分以上そろう月）", "", head, "|---|---|---|---|---|---|---|"]
    L += [_row(name, res["subs"][name]) for name in SUBS]
    L += ["", "## 3. B2 で使う値動きの大きさ（docs/bundle.md 5 章。8 章に書き写す）", "",
          f"系列ごとに、その系列の最後の月までの直近 {W.SIGMA_WINDOW} 暦月（値のある月が {W.SIGMA_MIN} 以上）の、月の値の標準偏差。B2 の間は変えない。", "",
          "| 系列 | 最後の月 | 月の値動きの大きさ |", "|---|---|---|"]
    for k in SERIES_NAMES:
        if k in res["sigma_b2"]:
            sg = res["sigma_b2"][k]
            L.append(f"| {k} | {res['last_months'][k]:%Y-%m} | {_pct(sg, 3) if math.isfinite(sg) else '–（値のある月が足りない）'} |")
    L += ["", "## 4. データと注意", "",
          "- 系列は、今の値段のデータから作り直したもの（株は今の構成銘柄）。単独の判定はやり直していない",
          "- 株の柱（D・E・F・J・K）は同じ銘柄の上にあり、今残っている会社だけを見ている偏りが残る。G・H は紙の上の組",
          "- 束の中身は年によって変わる（古い年は少ない柱で作られる）",
          "- 持たない月（0 の月）が多い系列は、重さが大きく出ることがある（docs/bundle.md 7 章）",
          "- 本番の判定は B2（2026-12 からの月だけで見る。docs/bundle.md 5 章）"]
    return L
