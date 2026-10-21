"""文章の信号（docs/text_signals.md）。どちらも月 1 回の入れ替え（5 日保有の枠の外の別枠）。

- 1C 文章で近い会社のモメンタム: 毎月末に、有報の「事業の内容」の埋め込みで近い 10 社の、直近 12 か月（t−12〜t−1）の
  等加重のリターンの平均を信号にし、上位 5 分の 1 を等金額で 1 か月持つ（Hoberg & Phillips 2018 の型）
- 案 2 有報の文面の前年との差（Lazy Prices）: 「事業等のリスク」の節の、前年の有報との近さ（文字 3 つ組の出現のコサイン。
  言語モデルを使わない）の上位 5 分の 1（変えていない会社）を等金額で 1 か月持つ
比べる相手は全銘柄（今の TOPIX500 の 490 社）の等加重の平均。コストは入れ替えた割合 × 往復 0.10%。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Callable

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- 決まり（docs/text_signals.md 3・4 章。結果を見る前に決めた）
K_PEERS = 10                 # 1C: 近い会社の数（先に固定）
LOOKBACK = 12                # 1C: 近い会社の直近 12 か月（t−12〜t−1）のリターン
QUANTILE = 0.2               # 上位 5 分の 1
MIN_FIRMS = 300              # 信号のある会社がこれ以上そろった最初の月末から判定の期間
DOC_VALID_DAYS = 455         # 有報は提出日の翌日から 455 日まで使う（次の有報が遅れても 15 か月）
PAIR_DAYS = (300, 430)       # 案 2: 前年の有報と組にする期末の間（決算期の変更は組にしない）
YEAR_DAYS = (350, 380)       # 案 2: 決算期の長さ（期首〜期末）がこの間でない有報は、決算期を変えた年として組にしない
PAIR_VALID_DAYS = 365        # 案 2: 近さは提出日の翌日から 1 年使う
MIN_CHARS = 200              # これより短い節は使わない
NGRAM = 3
CHUNK_CHARS = 1_000
COST = 0.001                 # 往復 0.10%
TRIALS = 83                  # 80 ＋ 1C ＋ 案 3 ＋ 案 2（docs/text_signals.md 2 章）
SINCE_REF = "2025-01-01"     # 参考: 2025 年以降（埋め込みのモデルの学習の締め切りより後の見込み）だけの値


# --------------------------------------------------------------------------- 文章
def clean_text(text: str, company: str | list[str] | tuple[str, ...] = "") -> str:
    """全角・半角をそろえ、その会社の名前を消し、数字を 0 にする（どの会社か・いつかを分かりにくくする。前年と同じ手順）。

    company には名前を 1 つか、いくつか（東証の銘柄名と、EDINET の提出者名など）渡せる。長い名前から消す。
    """
    t = unicodedata.normalize("NFKC", str(text or ""))
    names = set()
    for c in ([company] if isinstance(company, str) else list(company)):
        name = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(c or "")))
        names |= {name, re.sub(r"(株式会社|\(株\))", "", name)}
    for n in sorted(names, key=len, reverse=True):
        if len(n) >= 2:
            t = t.replace(n, "")
    t = re.sub(r"\d+", "0", t)
    return re.sub(r"\s+", " ", t).strip()


def ngram_counts(text: str, n: int = NGRAM) -> Counter:
    t = re.sub(r"\s+", "", text)
    return Counter(t[i:i + n] for i in range(max(len(t) - n + 1, 0)))


def ngram_cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return float("nan")
    small, big = (a, b) if len(a) < len(b) else (b, a)
    dot = sum(v * big.get(k, 0) for k, v in small.items())
    na, nb = np.sqrt(sum(v * v for v in a.values())), np.sqrt(sum(v * v for v in b.values()))
    return float(dot / (na * nb)) if na > 0 and nb > 0 else float("nan")


def doc_vector(text: str, encode: Callable[[list[str]], np.ndarray]) -> np.ndarray:
    """1,000 字ずつに切って埋め込み、平均して長さを 1 にそろえる。"""
    parts = [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)] or [""]
    v = np.asarray(encode(parts), dtype=np.float32).mean(axis=0)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


# --------------------------------------------------------------------------- 月末の値段とリターン
def month_ends(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(index, index=index)
    return pd.DatetimeIndex(s.groupby(index.to_period("M")).max().to_numpy())


def monthly_close(close: pd.DataFrame, today: pd.Timestamp | None = None) -> pd.DataFrame:
    """各月の最後の日の終値。today を渡せば、その月（まだ終わっていない月。場中の値段のこともある）の行は落とす。"""
    m = close.loc[month_ends(close.index)]
    if today is not None and len(m) and m.index[-1].to_period("M") >= pd.Timestamp(today).to_period("M"):
        m = m.iloc[:-1]
    return m


def first_docs(meta: pd.DataFrame) -> pd.DataFrame:
    """同じ会社・同じ決算期の有報が複数あれば、最初に出したもの（後の訂正の本文で検証しない）。列: ticker / docID / submit / periodEnd。"""
    m = meta.assign(submit=pd.to_datetime(meta["submitDateTime"]), periodEnd=pd.to_datetime(meta["periodEnd"], errors="coerce"),
                    periodStart=pd.to_datetime(meta["periodStart"], errors="coerce") if "periodStart" in meta else pd.NaT)
    m = m.sort_values("submit").drop_duplicates(["ticker", "periodEnd"], keep="first")
    return m[["ticker", "docID", "submit", "periodStart", "periodEnd"]].reset_index(drop=True)


def active_docs(meta: pd.DataFrame, when: pd.Timestamp, valid_days: int = DOC_VALID_DAYS) -> pd.DataFrame:
    """その月末に使える有報: 提出日がその日より前で、valid_days 日より新しいものの中で、会社ごとに一番新しいもの。"""
    w = pd.Timestamp(when)
    m = meta[(meta["submit"].dt.normalize() < w) & (meta["submit"] >= w - pd.Timedelta(days=valid_days))]
    return m.sort_values("submit").drop_duplicates("ticker", keep="last")


# --------------------------------------------------------------------------- 1C: 近い会社のモメンタム
def peer_means(tickers: list[str], vecs: np.ndarray, values: pd.Series, k: int = K_PEERS) -> pd.Series:
    """会社ごとに、埋め込みで近い k 社（自分は除く。値のある会社だけ）の値の平均。"""
    if not len(tickers):
        return pd.Series(dtype=float)
    v = np.asarray(vecs, dtype=np.float32)
    sim = v @ v.T
    np.fill_diagonal(sim, -np.inf)
    ok = values.reindex(tickers).notna().to_numpy()
    sim[:, ~ok] = -np.inf
    vals = values.reindex(tickers).to_numpy(dtype=float)
    out = np.full(len(tickers), np.nan)
    kk = min(k, int(ok.sum()) - 1)
    if kk <= 0:
        return pd.Series(out, index=tickers)
    top = np.argpartition(-sim, kk - 1, axis=1)[:, :kk]
    for i in range(len(tickers)):
        cols = [j for j in top[i] if np.isfinite(sim[i, j])]
        if cols:
            out[i] = float(np.mean(vals[cols]))
    return pd.Series(out, index=tickers)


def sector_means(tickers: list[str], sectors: pd.Series, values: pd.Series) -> pd.Series:
    """会社ごとに、同じ 33 業種のほかの会社（値のある会社）の値の平均（S2 型の参考と、業種を引いた参考に使う）。"""
    sec = sectors.reindex(tickers).fillna("")
    val = values.reindex(tickers)
    df = pd.DataFrame({"sec": sec, "v": val})
    s, n = df.groupby("sec")["v"].transform("sum"), df.groupby("sec")["v"].transform("count")
    own = val.fillna(0.0)
    cnt = n - val.notna().astype(int)
    out = (s - own) / cnt.where(cnt > 0)
    return out.where(sec != "")


def momentum_signals(meta: pd.DataFrame, vec_of: dict[str, np.ndarray], mclose: pd.DataFrame, k: int = K_PEERS,
                     lookback: int = LOOKBACK, sectors: pd.Series | None = None) -> dict[str, dict[pd.Timestamp, pd.Series]]:
    """月末ごとの信号。main = 近い k 社の直近 lookback か月のリターンの平均。参考: 1 か月・業種を引いた版・S2 型（同じ業種の仲間）。"""
    out: dict[str, dict] = {"main": {}, "one_month": {}, "minus_sector": {}, "s2": {}}
    r_long = mclose / mclose.shift(lookback) - 1
    r_one = mclose / mclose.shift(1) - 1
    for when in mclose.index:
        act = active_docs(meta, when)
        act = act[act["docID"].isin(vec_of) & act["ticker"].isin(mclose.columns)]
        if len(act) < k + 1:
            continue
        tick = act["ticker"].tolist()
        vecs = np.vstack([vec_of[d] for d in act["docID"]])
        main = peer_means(tick, vecs, r_long.loc[when])
        out["main"][when] = main
        out["one_month"][when] = peer_means(tick, vecs, r_one.loc[when])
        if sectors is not None:
            sec_long = sector_means(tick, sectors, r_long.loc[when])
            out["minus_sector"][when] = main - sec_long
            out["s2"][when] = sec_long
    return out


# --------------------------------------------------------------------------- 案 2: 有報の文面の前年との差
def full_year(start, end, year_days: tuple[int, int] = YEAR_DAYS) -> bool:
    """決算期が 1 年の長さか（期首の分からない有報は、期末の間だけで見る）。"""
    if pd.isna(start) or pd.isna(end):
        return True
    return year_days[0] <= (pd.Timestamp(end) - pd.Timestamp(start)).days <= year_days[1]


def lazy_pairs(meta: pd.DataFrame, text_of: Callable[[str], str], pair_days: tuple[int, int] = PAIR_DAYS) -> pd.DataFrame:
    """会社ごとに、決算期が 1 年ちがう前年の有報と組にし、節の近さ（文字 3 つ組のコサイン）。列: ticker / date（提出日）/ sim。

    決算期を変えた年（どちらかの有報の期首〜期末が 1 年の長さでない）は組にしない（docs/text_signals.md 4.2）。
    """
    rows = []
    for t, g in meta.sort_values("periodEnd").groupby("ticker"):
        g = g.dropna(subset=["periodEnd"]).reset_index(drop=True)
        for a, b in zip(g.index[:-1], g.index[1:]):
            gap = (g.at[b, "periodEnd"] - g.at[a, "periodEnd"]).days
            if not pair_days[0] <= gap <= pair_days[1]:
                continue
            if "periodStart" in g and not (full_year(g.at[a, "periodStart"], g.at[a, "periodEnd"])
                                           and full_year(g.at[b, "periodStart"], g.at[b, "periodEnd"])):
                continue
            x, y = text_of(g.at[a, "docID"]), text_of(g.at[b, "docID"])
            if len(x) < MIN_CHARS or len(y) < MIN_CHARS:
                continue
            rows.append((t, g.at[b, "submit"], ngram_cosine(ngram_counts(x), ngram_counts(y))))
    return pd.DataFrame(rows, columns=["ticker", "date", "sim"])


def lazy_signals(pairs: pd.DataFrame, ends: pd.DatetimeIndex, valid_days: int = PAIR_VALID_DAYS) -> dict[pd.Timestamp, pd.Series]:
    out: dict[pd.Timestamp, pd.Series] = {}
    if pairs is None or pairs.empty:
        return out
    pairs = pairs.assign(date=pd.to_datetime(pairs["date"]))
    for when in ends:
        p = pairs[(pairs["date"].dt.normalize() < when) & (pairs["date"] >= when - pd.Timedelta(days=valid_days))]
        p = p.sort_values("date").drop_duplicates("ticker", keep="last")
        if len(p):
            out[when] = p.set_index("ticker")["sim"]
    return out


# --------------------------------------------------------------------------- 月ごとの損益と判定
def select_top(signal: pd.Series, q: float = QUANTILE, highest: bool = True) -> list[str]:
    s = signal.dropna()
    n = int(np.floor(len(s) * q))
    if n <= 0:
        return []
    return s.sort_values(ascending=not highest).index[:n].tolist()


def turnover_of(prev: set, cur: set) -> float:
    """等金額の持ち方で、入れ替えた割合（片道）。最初の月は 1（全部を買う）。"""
    if not cur:
        return 0.0
    if not prev:
        return 1.0
    wn, wo = 1 / len(cur), 1 / len(prev)
    return 0.5 * sum(abs((wn if t in cur else 0.0) - (wo if t in prev else 0.0)) for t in cur | prev)


def monthly_book(signals: dict[pd.Timestamp, pd.Series], mclose: pd.DataFrame, start: pd.Timestamp | None = None,
                 q: float = QUANTILE, highest: bool = True, min_firms: int = 1,
                 picker: Callable[[pd.Series], list[str]] | None = None) -> pd.DataFrame:
    """月末に選んで次の月末まで持つ。行は持った月（次の月末の日付）。列: port / mkt / gross / turnover / d / n / firms。

    判定の期間は「300 社そろった最初の月末（first_month）から、データの最後まで」なので、始まりの後の月は社数が減っても数える
    （選ぶ株が 1 つも無い月だけ飛ばす）。"""
    fwd = mclose.shift(-1) / mclose - 1
    rows, prev = [], set()
    for when, sig in sorted(signals.items()):
        if start is not None and when < start:
            continue
        sig = sig[sig.index.isin(mclose.columns)]
        sig = sig[mclose.loc[when].reindex(sig.index).notna()]
        if sig.notna().sum() < min_firms:
            continue
        nxt = mclose.index[mclose.index > when]
        if not len(nxt):
            continue
        pick = picker(sig) if picker else select_top(sig, q, highest)
        if not pick:
            continue
        r = fwd.loc[when]
        port = r.reindex(pick).mean()
        mkt = r.mean()
        cur = set(pick)
        turnover = turnover_of(prev, cur)
        prev = cur
        rows.append((nxt[0], port, mkt, port - mkt, turnover, port - mkt - turnover * COST, len(pick), int(sig.notna().sum())))
    return pd.DataFrame(rows, columns=["month", "port", "mkt", "gross", "turnover", "d", "n", "firms"]).set_index("month")


def placebo(signals: dict[pd.Timestamp, pd.Series], mclose: pd.DataFrame, start: pd.Timestamp | None, n: int,
            rng: np.random.Generator, q: float = QUANTILE) -> np.ndarray:
    """偶然: 毎月、信号のある会社から同じ数をでたらめに選ぶ（コストを引く前の平均）。"""
    out = []
    for _ in range(n):
        def pick(sig: pd.Series) -> list[str]:
            s = sig.dropna()
            k = int(np.floor(len(s) * q))
            return rng.choice(s.index.to_numpy(), size=k, replace=False).tolist() if k > 0 else []
        b = monthly_book(signals, mclose, start, q, picker=pick)
        out.append(b["gross"].mean() if len(b) else np.nan)
    return np.asarray(out, dtype=float)


def first_month(signals: dict[pd.Timestamp, pd.Series], mclose: pd.DataFrame, min_firms: int = MIN_FIRMS) -> pd.Timestamp | None:
    for when, sig in sorted(signals.items()):
        sig = sig[sig.index.isin(mclose.columns)]
        if sig[mclose.loc[when].reindex(sig.index).notna()].notna().sum() >= min_firms:
            return when
    return None


def midpoint(months: pd.DatetimeIndex) -> str:
    return f"{months[len(months) // 2]:%Y-%m-%d}" if len(months) else "2100-01-01"


def judge(book: pd.DataFrame, null: np.ndarray, n_trials: int = TRIALS) -> dict:
    """docs/stocks.md 5 章の 4 つの条件（月ごとの d。前半・後半は判定の期間の月の数で半分）。"""
    from . import stocks as S

    d = book["d"].dropna()
    real = float(book["gross"].mean()) if len(book) else float("nan")
    ok = null[np.isfinite(null)]
    pct = float(np.mean(ok < real)) if len(ok) and np.isfinite(real) else None
    v = S.judge(d, midpoint(d.index), n_trials, pct)
    v.update(placebo_k=int(np.sum(ok < real)) if pct is not None else None, placebo_n=len(ok))
    since = d[d.index >= pd.Timestamp(SINCE_REF)]
    v.update(placebo=pct, months=len(d), split=midpoint(d.index), since_mean=float(since.mean()) if len(since) else float("nan"),
             since_months=len(since), gross_ann=real * 12 if np.isfinite(real) else float("nan"),
             turnover=float(book["turnover"].mean()) if len(book) else float("nan"))
    return v
