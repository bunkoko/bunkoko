"""案 2-US 米国の 10-K のリスク要因（docs/us_text.md）。

点検 1: 時点ごとの S&P500（Wikipedia の今の一覧と変更の表をさかのぼって作る）と、Yahoo の値段のそろい方。
SEC EDGAR からの取得は、点検 1 を通った後に足す。取ったものは data/edgar/（コミットしない）。
"""

from __future__ import annotations

import pandas as pd

WIKI_SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
START = pd.Timestamp("2014-12-31")       # この月末から（保有月 2015-01〜）
COUNT_RANGE = (490, 515)                 # 点検 1 (a): 毎月末の会社の数
MAX_BAD_MONTHS = 6                       # 点検 1 (a): 数が外れてよい月の数
MAX_MISSING = 0.10                       # 点検 1 (b): 会社 × 月のうち、値段の無い割合の上限
UNKNOWN_SECTOR = "不明"


def yahoo_symbol(ticker: str) -> str:
    """BRK.B → BRK-B（Yahoo の記号）。"""
    return str(ticker).strip().upper().replace(".", "-")


def _flat(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [" ".join(dict.fromkeys(str(c) for c in col if str(c) and not str(c).startswith("Unnamed"))) for col in df.columns]
    return df


def parse_wiki(tables: list[pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Wikipedia の表から、今の一覧（ticker / name / sector / cik）と変更の表（date / added / added_name / removed / removed_name）。"""
    cur = next(_flat(t) for t in tables if {"Symbol", "Security"} <= set(_flat(t).columns))
    current = pd.DataFrame({"ticker": cur["Symbol"].map(yahoo_symbol), "name": cur["Security"].astype(str),
                            "sector": cur.get("GICS Sector", pd.Series(UNKNOWN_SECTOR, index=cur.index)).astype(str),
                            "cik": pd.to_numeric(cur.get("CIK"), errors="coerce").astype("Int64") if "CIK" in cur else pd.NA})
    ch = None
    for t in tables:
        f = _flat(t)
        cols = " ".join(f.columns)
        if "Added" in cols and "Removed" in cols:
            ch = f
            break
    if ch is None:
        raise ValueError("Wikipedia のページに変更の表が無い")

    def col(*words: str) -> pd.Series:
        for c in ch.columns:
            if all(w in c for w in words):
                return ch[c]
        return pd.Series([None] * len(ch), index=ch.index)

    date = pd.to_datetime(col("Date").astype(str).str.replace(r"\[.*?\]", "", regex=True), errors="coerce", format="mixed")

    def tick(s: pd.Series) -> pd.Series:
        s = s.astype(str).str.strip()
        return s.where(s.ne("") & s.ne("nan") & s.ne("None") & s.ne("—") & s.ne("-")).map(lambda x: yahoo_symbol(x) if isinstance(x, str) else None)

    changes = pd.DataFrame({"date": date, "added": tick(col("Added", "Ticker")), "added_name": col("Added", "Security"),
                            "removed": tick(col("Removed", "Ticker")), "removed_name": col("Removed", "Security")})
    changes = changes.dropna(subset=["date"])
    return current.reset_index(drop=True), changes.sort_values("date", ascending=False).reset_index(drop=True)


def month_ends(start: pd.Timestamp, last: pd.Timestamp) -> pd.DatetimeIndex:
    return pd.date_range(pd.Timestamp(start), pd.Timestamp(last), freq="ME")


def membership(current: pd.DataFrame, changes: pd.DataFrame, ends: pd.DatetimeIndex) -> dict[pd.Timestamp, frozenset]:
    """月末ごとの会社。今の一覧から、その月末より後の変更を新しい順に取り消していく（足された会社を除き、外された会社を戻す）。"""
    members = set(current["ticker"])
    ch = changes.sort_values("date", ascending=False).reset_index(drop=True)
    out: dict[pd.Timestamp, frozenset] = {}
    k = 0
    for m in sorted(ends, reverse=True):
        while k < len(ch) and ch.at[k, "date"] > m:
            added, removed = ch.at[k, "added"], ch.at[k, "removed"]
            if isinstance(added, str) and added:
                members.discard(added)
            if isinstance(removed, str) and removed:
                members.add(removed)
            k += 1
        out[m] = frozenset(members)
    return dict(sorted(out.items()))


def monthly_last(close: pd.DataFrame) -> pd.DataFrame:
    """月ごとの最後の終値（行は暦の月末の日付。月の途中で値段が無くなった株は、その月の最後の値段）。"""
    g = close.groupby(close.index.to_period("M")).last()
    g.index = g.index.to_timestamp(how="end").normalize()
    return g


def coverage(members: dict[pd.Timestamp, frozenset], mclose: pd.DataFrame) -> dict:
    """点検 1。毎月末の数・値段の無い割合・通るか。"""
    rows = []
    for m, tick in members.items():
        have = 0
        if m in mclose.index:
            row = mclose.loc[m]
            have = int(sum(1 for t in tick if t in row.index and pd.notna(row[t])))
        rows.append({"month": m, "members": len(tick), "with_price": have})
    df = pd.DataFrame(rows).set_index("month")
    bad = df[(df["members"] < COUNT_RANGE[0]) | (df["members"] > COUNT_RANGE[1])]
    total = int(df["members"].sum())
    missing = 1 - df["with_price"].sum() / total if total else 1.0
    ok_a, ok_b = len(bad) <= MAX_BAD_MONTHS, missing <= MAX_MISSING
    return {"table": df, "bad_months": bad, "missing": float(missing), "ok_count": ok_a, "ok_price": ok_b, "ok": ok_a and ok_b}


def missing_names(members: dict[pd.Timestamp, frozenset], mclose: pd.DataFrame) -> pd.Series:
    """値段の無い会社 × 月の数（会社ごと）。"""
    counts: dict[str, int] = {}
    for m, tick in members.items():
        row = mclose.loc[m] if m in mclose.index else pd.Series(dtype=float)
        for t in tick:
            if t not in row.index or pd.isna(row.get(t)):
                counts[t] = counts.get(t, 0) + 1
    return pd.Series(counts, dtype=int).sort_values(ascending=False)
