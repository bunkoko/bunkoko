"""案 2-US 米国の 10-K の「リスク要因」の前年との差（Lazy Prices）の計算と判定。docs/us_text.md 9 章（2022 年からの版）。

作り方は 3 章、判定は 5 章、参考は 6 章。9 章が 2〜5 章より優先する（判定の期間は 2021-12 末より後から）。
- 近さ: 社名と数字を消した節の、単語（英字の並び）の出現の数のコサイン（言語モデルは使わない）
- 持ち方: 毎月末に、その月末の S&P500 の会社で近さのある会社を業種ごとに並べ、各業種の上位 5 分の 1（切り捨て）を等金額で 1 か月持つ
- 差 d: 選んだ株の翌月のリターンの平均 − その月末の S&P500 の会社の平均 − 入れ替えた割合 × 往復 0.30%
ここでは計算だけをする（取得と報告の書き出しは scripts/edgar.py）。1 回だけ計算し、動かし直さない。
"""

from __future__ import annotations

import html
import re
from collections import Counter
from typing import Callable

import numpy as np
import pandas as pd

from . import stocks as S
from . import text_signals as TS

# --------------------------------------------------------------------------- 決まり（docs/us_text.md 3・5・9 章。結果を見る前に決めた）
TRIALS = 88                  # 試した数（基準 2.485）
COST = 0.003                 # 往復 0.30%（docs/stocks.md の COST_US）
COST_JP_BROKER = 0.010       # 決まり 1: 往復 1.0%（日本の大手の証券会社の手数料と為替）
MIN_CHARS = 1000             # どちらの節もこれ以上（社名と数字を消した後の文字数）
PAIR_GAP = (350, 380)        # 前年の 10-K と組にする期末の間（52・53 週の年を含む。決算期を変えた年は組にしない）
VALID_DAYS = 365             # 新しい方の受付日の翌日から 365 日使う
QUANTILE = 0.2               # 業種ごとの上位 5 分の 1
MIN_FIRMS = 300              # 近さのある会社がこれ以上そろった最初の月末から
START = pd.Timestamp("2021-12-31")
RULE_WINDOW = (pd.Timestamp("2020-11-09"), pd.Timestamp("2021-10-31"))   # SEC の規則の改正とコロナの書き直しの年
PLACEBO = 300
COVERAGE_MIN = 0.80          # 点検 3: 会社 × 月のうち近さのある割合
NW_LAGS = 12
FORMS = ("10-K", "10-K405")
SUFFIXES = {"inc", "corp", "corporation", "co", "company", "ltd", "holdings", "plc"}


# --------------------------------------------------------------------------- 文章
def _name_forms(name: str) -> set[str]:
    """名前そのものと、Inc・Corp などの後ろの言葉と先頭の the を外した形。"""
    n = re.sub(r"\s+", " ", html.unescape(str(name or "")).lower()).strip()
    out = {n}
    words = n.split()
    while words and words[-1].strip(",.") in SUFFIXES:
        words.pop()
        out.add(" ".join(words).rstrip(" ,."))
    if words and words[0] == "the":
        words = words[1:]
    out.add(" ".join(words).rstrip(" ,."))
    return {f for f in out if len(f) >= 2}


def clean_text(text: str, names=()) -> str:
    """小文字にし、会社の名前（後ろの Inc などを外した形も。長い名前から）を消し、数字を 0 にする。"""
    t = html.unescape(str(text or "")).lower()
    forms = set()
    for n in ([names] if isinstance(names, str) else list(names or [])):
        forms |= _name_forms(n)
    for f in sorted(forms, key=len, reverse=True):
        t = re.sub(r"(?<![a-z0-9])" + re.escape(f) + r"(?![a-z0-9])", " ", t)
    t = re.sub(r"\d+", "0", t)
    return re.sub(r"\s+", " ", t).strip()


def word_counts(text: str) -> Counter:
    return Counter(re.findall(r"[a-z]+", str(text or "").lower()))


def cosine(a: Counter, b: Counter) -> float:
    return TS.ngram_cosine(a, b)        # 数え方が違うだけで、同じコサイン（どちらかが空なら nan）


# --------------------------------------------------------------------------- 組と使える時点
def first_filings(filings: pd.DataFrame) -> pd.DataFrame:
    """10-K・10-K405 だけ。同じ会社・同じ期末に複数あれば、最初に受け付けたもの（訂正の後の本文で検証しない）。"""
    f = filings[filings["form"].astype(str).str.upper().isin(FORMS)].copy()
    f["acceptance"] = pd.to_datetime(f["acceptance"])
    f["report_date"] = pd.to_datetime(f["report_date"], errors="coerce")
    f = f.dropna(subset=["cik", "report_date", "acceptance"]).sort_values(["acceptance", "accession"])
    f = f.drop_duplicates(["cik", "report_date"], keep="first")
    return f.sort_values(["cik", "report_date"]).reset_index(drop=True)


def in_rule_window(day) -> bool:
    d = pd.Timestamp(day).normalize()
    return bool(RULE_WINDOW[0] <= d <= RULE_WINDOW[1])


def pairs(filings: pd.DataFrame, text_of: Callable[[str, str], str], names_of: Callable[[int], list[str]],
          kind: str = "risks") -> pd.DataFrame:
    """同じ会社の、期末の間が 350〜380 日の続いた 2 つの 10-K の近さ。列: cik / accession（新しい方）/ accepted（受付の日付）/ sim / in_window。"""
    f = first_filings(filings)
    cleaned: dict[str, tuple[int, Counter]] = {}     # 書類ごとに（社名と数字を消した後の文字数・単語の数）
    rows = []
    for cik, g in f.groupby("cik", sort=True):
        names = list(names_of(int(cik)) or [])

        def counts(acc: str) -> tuple[int, Counter]:
            if acc not in cleaned:
                try:
                    t = clean_text(text_of(acc, kind) or "", names)
                except Exception:   # 取れていない書類は組にしない
                    t = ""
                cleaned[acc] = (len(t), word_counts(t))
            return cleaned[acc]

        g = g.reset_index(drop=True)
        for a, b in zip(g.index[:-1], g.index[1:]):
            gap = (g.at[b, "report_date"] - g.at[a, "report_date"]).days
            if not PAIR_GAP[0] <= gap <= PAIR_GAP[1]:
                continue
            (nx, x), (ny, y) = counts(g.at[a, "accession"]), counts(g.at[b, "accession"])
            if nx < MIN_CHARS or ny < MIN_CHARS:
                continue
            sim = cosine(x, y)
            if not np.isfinite(sim):
                continue
            day = g.at[b, "acceptance"].normalize()
            rows.append((int(cik), g.at[b, "accession"], day, sim, in_rule_window(day)))
    return pd.DataFrame(rows, columns=["cik", "accession", "accepted", "sim", "in_window"])


def signals(pairs: pd.DataFrame, ends, trade_days: dict | None = None) -> dict[pd.Timestamp, pd.DataFrame]:
    """月末ごとに、受付日の翌日から 365 日の間にある組のうち、会社ごとに一番新しいもの。値: cik を行にした sim / in_window。

    比べる日は、その月末の値段の日（その月の最後の取引日。trade_days で渡す。無ければ月末より前の平日）。
    月末が土日・休みのとき、値段の日より後に受け付けた 10-K を使わないため（先読みを避ける）。
    """
    out: dict[pd.Timestamp, pd.DataFrame] = {}
    if pairs is None or pairs.empty:
        return out
    p = pairs.assign(accepted=pd.to_datetime(pairs["accepted"]).dt.normalize()).sort_values(["accepted", "accession"])
    for when in pd.DatetimeIndex(ends):
        w = pd.Timestamp(trade_days[when]).normalize() if trade_days and when in trade_days else \
            pd.offsets.BDay().rollback(pd.Timestamp(when).normalize())
        q = p[(p["accepted"] < w) & (p["accepted"] >= w - pd.Timedelta(days=VALID_DAYS))]
        q = q.drop_duplicates("cik", keep="last")
        if len(q):
            out[pd.Timestamp(when)] = q.set_index("cik")[["sim", "in_window"]]
    return out


# --------------------------------------------------------------------------- 月ごとの持ち方
def _key(cik, ticker) -> object:
    return int(cik) if pd.notna(cik) else f"T:{ticker}"


def _months(members: pd.DataFrame, mclose: pd.DataFrame, start: pd.Timestamp):
    """start 以後で、一覧と値段の両方にある月末と、その月の一覧。"""
    m = members.assign(month=pd.to_datetime(members["month"]))
    for when, g in m.groupby("month", sort=True):
        if when >= start and when in mclose.index:
            yield when, g


def _universe(g: pd.DataFrame, mclose: pd.DataFrame, when, nxt=None) -> pd.DataFrame:
    """その月末の会社（値段のあるもの）。同じ CIK の記号が 2 つ（GOOGL と GOOG など）でも 1 社に数える。列: key / sector / r。"""
    sym = g["symbol"].astype(str)
    p0 = mclose.loc[when].reindex(sym).to_numpy(dtype=float)
    u = g.assign(key=[_key(c, t) for c, t in zip(g["cik"], g["ticker"])], p0=p0)
    if nxt is not None:
        r = mclose.loc[nxt].reindex(sym).to_numpy(dtype=float) / p0 - 1
        u["gone"] = ~np.isfinite(r)                          # 翌月末の値段が無い = 最後の値段で手仕舞い、その後は 0（3 章）
        u["r"] = np.where(np.isfinite(r), r, 0.0)
    u = u[np.isfinite(p0)].sort_values("ticker").drop_duplicates("key", keep="first")
    return u.set_index("key")


def _signal_firms(u: pd.DataFrame, sig_m: pd.DataFrame | None) -> pd.DataFrame:
    if sig_m is None or sig_m.empty:
        return u.iloc[0:0].assign(sim=[], in_window=[])
    s = sig_m[sig_m["sim"].notna()]
    f = u[u.index.isin(s.index)].copy()
    f["sim"] = s["sim"].reindex(f.index).to_numpy(dtype=float)
    f["in_window"] = s["in_window"].reindex(f.index).astype(bool).to_numpy()
    return f


def _prepare(members, sig, mclose, start):
    """月末ごとの（月末・翌月末・近さのある会社・S&P500 の平均）。"""
    out = []
    for when, g in _months(members, mclose, start):
        nxt = mclose.index[mclose.index > when]
        if not len(nxt):
            continue
        u = _universe(g, mclose, when, nxt[0])
        mkt = float(np.nanmean(u["r"])) if u["r"].notna().any() else np.nan
        f = _signal_firms(u, sig.get(when))
        out.append((when, nxt[0], f[["sector", "sim", "in_window", "r", "gone"]], mkt))
    return out


def _top(f: pd.DataFrame, highest: bool = True, by_sector: bool = True) -> list:
    """業種ごと（by_sector）に近さの上位 5 分の 1（切り捨て。0 社の業種からは選ばない）。同じ近さは番号の順。"""
    f = f.sort_index().sort_values("sim", ascending=not highest, kind="mergesort")
    if not by_sector:
        return f.index[: int(np.floor(len(f) * QUANTILE))].tolist()
    out = []
    for _, g in f.groupby(f["sector"].fillna("").astype(str), sort=True):
        out += g.index[: int(np.floor(len(g) * QUANTILE))].tolist()
    return out


def _drift_turnover(prev: dict, cur: set) -> float:
    """月 1 回の等金額への合わせ直しも数えた入れ替え（片道）。前月の株は値動きで重さが変わっている。"""
    if not cur:
        return 0.0
    old = {k: 1 + r for k, r in prev.items() if np.isfinite(r)}
    tot = sum(old.values())
    if tot <= 0:
        return 1.0
    wn = 1 / len(cur)
    return 0.5 * sum(abs((wn if k in cur else 0.0) - old.get(k, 0.0) / tot) for k in cur | set(old))


def _run(prep, pick: Callable[[pd.DataFrame], list], cost: float = COST) -> pd.DataFrame:
    rows, prev, prev_r = [], set(), {}
    for when, nxt, f, mkt in prep:
        if f.empty:
            continue
        keys = pick(f)
        if not keys:
            continue
        r = f["r"].reindex(keys)
        port = float(r.mean()) if r.notna().any() else np.nan
        cur = set(keys)
        to, to2 = TS.turnover_of(prev, cur), _drift_turnover(prev_r, cur)
        prev, prev_r = cur, dict(zip(keys, r.to_numpy(dtype=float)))
        gross = port - mkt
        win = float(f["in_window"].reindex(keys).astype(bool).mean())
        rows.append((nxt, port, mkt, gross, to, gross - to * cost, len(keys), len(f), win, int(f["gone"].reindex(keys).sum()), to2))
    cols = ["month", "port", "mkt", "gross", "turnover", "d", "n", "firms", "window_share", "missing", "turnover_rebal"]
    return pd.DataFrame(rows, columns=cols).set_index("month")


def book(members: pd.DataFrame, sig: dict, mclose: pd.DataFrame, start: pd.Timestamp = START, highest: bool = True,
         by_sector: bool = True, cost: float = COST, picker: Callable[[pd.DataFrame], list] | None = None) -> pd.DataFrame:
    """月末に選んで翌月末まで持つ。行は持った月（翌月末）。列: port / mkt / gross / turnover / d / n / firms / window_share
    / missing（翌月末の値段が無く、最後の値段で手仕舞いした株の数）/ turnover_rebal。始まりの後は 300 社に届かない月も数える。"""
    pick = picker or (lambda f: _top(f, highest, by_sector))
    return _run(_prepare(members, sig, mclose, pd.Timestamp(start)), pick, cost)


def with_cost(b: pd.DataFrame, cost: float, rebalance: bool = False) -> pd.DataFrame:
    """同じ持ち方で、コストだけを変えた版。"""
    return b.assign(d=b["gross"] - b["turnover_rebal" if rebalance else "turnover"] * cost)


def first_month(sig: dict, members: pd.DataFrame, mclose: pd.DataFrame, start: pd.Timestamp = START) -> pd.Timestamp | None:
    """start 以後で、値段のある一覧の会社のうち近さのある会社が 300 社以上そろった最初の月末。"""
    for when, g in _months(members, mclose, pd.Timestamp(start)):
        if len(_signal_firms(_universe(g, mclose, when), sig.get(when))) >= MIN_FIRMS:
            return when
    return None


def placebo(members: pd.DataFrame, sig: dict, mclose: pd.DataFrame, start: pd.Timestamp, n: int,
            rng: np.random.Generator, by_sector: bool = True) -> np.ndarray:
    """偶然: 毎月、業種ごとに同じ数を、その業種の近さのある会社からでたらめに選ぶ（コストを引く前の差の平均）。"""
    plan = []
    for _, _, f, mkt in _prepare(members, sig, mclose, pd.Timestamp(start)):
        r = f["r"].to_numpy(dtype=float)
        grp = f["sector"].fillna("").astype(str).to_numpy() if by_sector else np.zeros(len(f), dtype=str)
        parts = [(np.flatnonzero(grp == s), int(np.floor(np.sum(grp == s) * QUANTILE))) for s in sorted(set(grp))]
        parts = [(i, k) for i, k in parts if k > 0]
        if parts:
            plan.append((r, parts, mkt))
    out = np.full(n, np.nan)
    for j in range(n):
        g = []
        for r, parts, mkt in plan:
            x = r[np.concatenate([rng.choice(i, size=k, replace=False) for i, k in parts])]
            g.append(np.nanmean(x) - mkt if np.isfinite(x).any() else np.nan)
        g = np.asarray(g, dtype=float)
        if np.isfinite(g).any():
            out[j] = np.nanmean(g)
    return out


# --------------------------------------------------------------------------- 判定
def newey_west_t(d, lags: int = NW_LAGS) -> float:
    """ニューイー・ウェストの t 値（バートレットの重み）。"""
    x = np.asarray(pd.Series(d).dropna(), dtype=float)
    n = len(x)
    if n < 3:
        return float("nan")
    e = x - x.mean()
    var = e @ e / n
    for lag in range(1, min(lags, n - 1) + 1):
        var += 2 * (1 - lag / (lags + 1)) * (e[lag:] @ e[:-lag]) / n
    return float(x.mean() / np.sqrt(var / n)) if var > 0 else float("nan")


def yearly(b: pd.DataFrame) -> pd.Series:
    """保有月の暦年ごとの d の合計。"""
    return b["d"].dropna().groupby(b["d"].dropna().index.year).sum() if len(b) else pd.Series(dtype=float)


def caps(b: pd.DataFrame) -> dict:
    """「使えそう」でも「まだ判断できない」より上にしない 3 つの決まり（5 章）。True = 当たらない（通る）。"""
    full = float(b["d"].mean())
    c1 = float(with_cost(b, COST_JP_BROKER)["d"].mean())
    ex = b[b["window_share"] == 0]["d"].dropna()
    c2 = float(ex.mean()) if len(ex) else float("nan")
    ys = yearly(b)
    pos, tot = int((ys > 0).sum()), int(len(ys))
    return {"cost_1pct": bool(np.isfinite(c1) and c1 > 0), "cost_1pct_mean": c1,
            "window_excluded": bool(np.isfinite(c2) and np.isfinite(full) and np.sign(c2) == np.sign(full)),
            "window_excluded_mean": c2, "window_excluded_months": int(len(ex)), "full_mean": full,
            "years": bool(tot > 0 and 2 * pos >= tot), "years_positive": pos, "years_total": tot}


def final_verdict(verdict: str, cp: dict) -> str:
    """「使えそう」でも、3 つの決まりのどれかに当たれば「まだ判断できない」にする。"""
    if verdict == "使えそう" and not (cp["cost_1pct"] and cp["window_excluded"] and cp["years"]):
        return "まだ判断できない"
    return verdict


def coverage(members: pd.DataFrame, sig: dict, since: pd.Timestamp = START) -> float:
    """点検 3: since 以後の月末の会社 × 月のうち、近さのある割合（値段は問わない）。"""
    m = members.assign(month=pd.to_datetime(members["month"]))
    m = m[m["month"] >= since]
    if m.empty:
        return float("nan")
    keys = [_key(c, t) for c, t in zip(m["cik"], m["ticker"])]
    m = m.assign(key=keys).drop_duplicates(["month", "key"])
    has = [w in sig and k in sig[w].index for w, k in zip(m["month"], m["key"])]
    return float(np.mean(has))


def evaluate(members: pd.DataFrame, sig_risks: dict, mclose: pd.DataFrame, rng: np.random.Generator,
             sig_mdna: dict | None = None, placebo_n: int = PLACEBO, today: pd.Timestamp | None = None) -> dict:
    """判定（5 章の 4 つの条件と 3 つの決まり）と参考（6 章）。1 回だけ計算する。

    today を渡せば、まだ終わっていない今月の値段の行は使わない（終わった最後の月まで）。点検 3 を通らなければ判定しない。
    """
    if today is not None:
        mclose = mclose[mclose.index.to_period("M") < pd.Timestamp(today).to_period("M")]
    m = members.assign(month=pd.to_datetime(members["month"]))
    per = m[m["month"] >= START].groupby("month")["ticker"].nunique()
    counts = {"members_min": int(per.min()) if len(per) else 0, "members_max": int(per.max()) if len(per) else 0,
              "months": int(len(per)), "coverage": coverage(m, sig_risks)}
    if not (np.isfinite(counts["coverage"]) and counts["coverage"] >= COVERAGE_MIN):
        return {"start": None, "counts": counts, "verdict": "判定しない", "reason": f"点検 3 を通らない（近さのある割合が {COVERAGE_MIN:.0%} に届かない）"}
    start = first_month(sig_risks, m, mclose)
    res = {"start": start, "counts": counts, "verdict": "判定しない"}
    if start is None:
        return res
    b = book(m, sig_risks, mclose, start)
    null = placebo(m, sig_risks, mclose, start, placebo_n, rng)
    ok = null[np.isfinite(null)]
    real = float(b["gross"].mean())
    pct = float(np.mean(ok < real)) if len(ok) and np.isfinite(real) else None
    d = b["d"].dropna()
    v = S.judge(d, TS.midpoint(d.index), TRIALS, pct if pct is not None else 0.0)   # 偶然と比べられなければ ④ は通らない
    v["placebo"] = pct
    v.update(placebo_k=int(np.sum(ok < real)) if pct is not None else None, placebo_n=int(len(ok)),
             months=len(d), split=TS.midpoint(d.index), gross_ann=real * 12, turnover=float(b["turnover"].mean()))
    cp = caps(b)
    final = final_verdict(v["verdict"], cp)
    last = m["month"].max()
    keys_last = {_key(c, t) for c, t in zip(m.loc[m["month"] == last, "cik"], m.loc[m["month"] == last, "ticker"])}
    now = m[[_key(c, t) in keys_last for c, t in zip(m["cik"], m["ticker"])]]
    refs = {"changed": book(m, sig_risks, mclose, start, highest=False),
            "not_sector": book(m, sig_risks, mclose, start, by_sector=False),
            "cost_1pct": with_cost(b, COST_JP_BROKER),
            "rebalance_1pct": with_cost(b, COST_JP_BROKER, rebalance=True),
            "window_excluded": b[b["window_share"] == 0],
            "yearly": yearly(b), "newey_west_t": newey_west_t(b["d"]),
            "mdna": book(m, sig_mdna, mclose, start) if sig_mdna else None,
            "current_only": book(now, sig_risks, mclose, start)}
    counts["low_months"] = int((b["firms"] < MIN_FIRMS).sum())
    counts["missing_picks"] = int(b["missing"].sum())
    res.update(book=b, judge=v, caps=cp, verdict=final, refs=refs, null=null)
    return res


# --------------------------------------------------------------------------- 報告
def _f(x, d: int = 2) -> str:
    return "–" if x is None or not np.isfinite(x) else f"{x:.{d}f}"


def _p(x, d: int = 1) -> str:
    return "–" if x is None or not np.isfinite(x) else f"{x:.{d}%}"


def _ok(b: bool) -> str:
    return "○" if b else "×"


def conditions_line(v: dict) -> str:
    """4 つの条件の成否を、丸めない値で。"""
    c1 = bool(np.isfinite(v["mean1"]) and np.isfinite(v["mean2"]) and v["mean1"] > 0 and v["mean2"] > 0)
    plac = f"{v['placebo_k']}/{v['placebo_n']} = {v['placebo']!r}" if v.get("placebo") is not None else "–"
    return (f"- 条件: ① 前半 {v['mean1']!r}・後半 {v['mean2']!r}（月の平均）が両方プラス {_ok(c1)}　"
            f"② 後半の t 値 {v['t2']!r} ≥ 1.65 {_ok(np.isfinite(v['t2']) and v['t2'] >= 1.65)}　"
            f"③ 全期間の t 値 {v['t']!r} ≥ {v['threshold']!r} {_ok(np.isfinite(v['t']) and v['t'] >= v['threshold'])}　"
            f"④ 偶然より良い割合 {plac} ≥ 0.95 {_ok(v.get('placebo') is not None and v['placebo'] >= 0.95)}")


def _ref_row(label: str, b: pd.DataFrame | None, turnover: str = "turnover") -> str:
    if b is None or b.empty:
        return f"| {label} | – | – | – | – |"
    return f"| {label} | {len(b)} か月 | {_p(b['d'].mean() * 12)} | {_f(S.t_value(b['d']))} | {_p(b[turnover].mean(), 0)} |"


def report_lines(result: dict, meta: dict) -> list[str]:
    """報告（Markdown）の行。meta: made・filings・firms・pairs・pairs_mdna・sim_median・no_cik・notes（どれも無くてよい）。"""
    from .stats import expected_max_z

    c = result.get("counts", {})
    L = ["# 案 2-US 米国の 10-K の「リスク要因」の前年との差（2022 年からの版）", "",
         f"作成: {meta.get('made', '–')}　決まり: docs/us_text.md 9 章（2〜5 章より優先。作り方 3 章・判定 5 章・参考 6 章。結果を見る前に決めたもの）。"
         f"試した数 {TRIALS}（全期間の t 値の基準 {_f(expected_max_z(TRIALS), 3)}）。1 回だけ計算し、動かし直さない", ""]
    L.append(f"- S&P500 の一覧: {START:%Y-%m} 末から {c.get('months', 0)} か月、毎月末 {c.get('members_min', 0)}〜{c.get('members_max', 0)} 社")
    if "filings" in meta:
        L.append(f"- 10-K: {meta['filings']:,} 件" + (f"・{meta['firms']:,} 社" if "firms" in meta else "")
                 + (f"。CIK の分からない会社 {meta['no_cik']:,} 社" if "no_cik" in meta else ""))
    if "pairs" in meta:
        L.append(f"- 前年と組にできた「リスク要因」{meta['pairs']:,} 組"
                 + (f"（近さの中央値 {_f(meta['sim_median'], 3)}）" if "sim_median" in meta else "")
                 + (f"・「経営者による分析」{meta['pairs_mdna']:,} 組" if "pairs_mdna" in meta else ""))
    L.append(f"- 会社 × 月のうち、近さのある割合: {_p(c.get('coverage'))}（点検 3 の線は 80%）")
    L += [f"- {x}" for x in meta.get("notes", [])]
    if result.get("start") is None:
        return L + ["", f"- **判定しない**: {result.get('reason') or f'近さのある会社が {MIN_FIRMS} 社そろった月末がまだ無い'}（試した数は足さない）"]
    b, v, cp, refs = result["book"], result["judge"], result["caps"], result["refs"]
    L.append(f"- 判定の期間: {result['start']:%Y-%m} 末に選んだ月から、持った月 {b.index[0]:%Y-%m}〜{b.index[-1]:%Y-%m}（{len(b)} か月。まだ終わっていない今月は数えない）。"
             f"始まりの後で {MIN_FIRMS} 社に届かなかった月 {c.get('low_months', 0)} か月（決まりどおり数えた）。"
             f"持っている間に値段が無くなり、最後の値段で手仕舞いした株 のべ {c.get('missing_picks', 0)}")
    L.append(f"- 偶然 = 毎月、業種ごとに同じ数を近さのある会社からでたらめに選ぶ {v['placebo_n']} 回（コストの前どうし）")
    L += ["", "## 1. 判定", "",
          "| 仮説 | 期間 | 差（年率・コストの後） | 前半 | 後半 | t 値（全期間） | t 値（後半） | 基準 | 偶然より良い割合 | 判定 |",
          "|---|---|---|---|---|---|---|---|---|---|",
          f"| リスク要因を前年から変えていない会社（業種ごとの上位 5 分の 1）を 1 か月持つ | {v['months']} か月（後半 {v['split'][:7]}〜） | "
          f"{_p(v['mean'] * 12)} | {_p(v['mean1'] * 12)} | {_p(v['mean2'] * 12)} | {_f(v['t'])} | {_f(v['t2'])} | {_f(v['threshold'], 3)} | "
          f"{_p(v['placebo'], 0)} | **{result['verdict']}** |", "", conditions_line(v), "",
          "「使えそう」でも、次のどれかに当たれば「まだ判断できない」より上にしない（5 章）:", "",
          f"- 決まり 1 往復 1.0% にしても d の平均がプラス: {cp['cost_1pct_mean']!r} {_ok(cp['cost_1pct'])}",
          f"- 決まり 2 受付が 2020-11-09〜2021-10-31 の組を使う月を除いた {cp['window_excluded_months']} か月の d の平均 "
          f"{cp['window_excluded_mean']!r} が、全期間の {cp['full_mean']!r} と同じ符号: {_ok(cp['window_excluded'])}",
          f"- 決まり 3 暦年の d の合計がプラスの年が半分以上: {cp['years_positive']}/{cp['years_total']} {_ok(cp['years'])}",
          f"- 4 つの条件だけの判定: {v['verdict']} → 決まりを当てた後: **{result['verdict']}**", "",
          "## 2. 参考（判定しない。試した数に入れない）", "",
          "| 参考 | 期間 | 差（年率・コストの後） | t 値 | 1 か月の入れ替え |", "|---|---|---|---|---|",
          _ref_row("判定の版（上）", b),
          _ref_row("変えた会社（業種ごとの近さの下位 5 分の 1）", refs["changed"]),
          _ref_row("業種をそろえない版（全体の上位 5 分の 1）", refs["not_sector"]),
          _ref_row("往復 1.0% のコスト", refs["cost_1pct"]),
          _ref_row("往復 1.0%・月 1 回の等金額への合わせ直しも数えた入れ替え", refs["rebalance_1pct"], "turnover_rebal"),
          _ref_row("2020-11-09〜2021-10-31 の組を使う月を除いた版", refs["window_excluded"]),
          _ref_row("「経営者による分析」（Item 7）の節", refs["mdna"]),
          _ref_row("今の S&P500 だけを昔に当てた版（偏りの大きさの目安）", refs["current_only"]), "",
          "- 暦年ごとの d の合計: " + ("・".join(f"{y} 年 {s:+.2%}" for y, s in refs["yearly"].items()) or "–"),
          f"- ニューイー・ウェストの t 値（12 か月）: {_f(refs['newey_west_t'])}", "",
          "## 3. 読み方・限界", "",
          "- 持った月は 5 年ほどしか無い。全期間の t 値 2.485 に届くには、コストの後で年 +3〜5% の効きが要る。論文の発表の後の見込みは年 0〜1.5% なので、"
          "**「使えない」は「効かない」ではなく「この長さでは見えなかった」と読む**（9.2）",
          "- 「使えそう」でもお金は入れない。有料のデータでの確かめ直しと 1 年以上の記録だけの帳を、別に事前登録してから（5 章）",
          "- 会社の一覧は Wikipedia の月末ごとの古い版から作った。持っている間に値段が無くなった株は、最後の値段で手仕舞いしたものとし、その後は 0 とした（3 章）",
          "- 近さは単語の出現の数のコサイン（言語モデルを使わないので先読みが無い）。社名と数字は消してから比べた",
          "- 米国株を日本から売り買いするコスト・税は 0.30% より重い（決まり 1 で往復 1.0% を見た）"]
    return L
