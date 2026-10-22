"""案 2-US 米国の 10-K のリスク要因（docs/us_text.md）。

点検 1: 時点ごとの S&P500（Wikipedia の「List of S&P 500 companies」の、月末ごとの古い版の一覧）と、Yahoo の値段のそろい方。
SEC EDGAR からの取得は、点検 1 を通った後に足す。取ったものは data/edgar/（コミットしない）。
"""

from __future__ import annotations

import pandas as pd

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_TITLE = "List of S&P 500 companies"
START = pd.Timestamp("2014-12-31")       # この月末から（保有月 2015-01〜。2〜5 章の版。点検 1 で止まった）
WINDOW_START = pd.Timestamp("2021-12-31")  # 9 章（判定の版）: この月末から
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


def describe(tables: list[pd.DataFrame]) -> str:
    """表の数・形・最初の列の名前（一覧が見つからないときに、直すための手がかり）。"""
    out = []
    for i, t in enumerate(tables[:12]):
        f = _flat(t)
        out.append(f"[{i}] {f.shape[0]}行×{f.shape[1]}列 列={[str(c)[:24] for c in f.columns[:6]]} 1行目={[str(v)[:16] for v in f.iloc[0, :6]] if len(f) else []}")
    return f"全部で {len(tables)} 個。" + " / ".join(out)


def parse_constituents(tables: list[pd.DataFrame], min_rows: int = 400) -> pd.DataFrame:
    """ページのその版の S&P500 の一覧（ticker / name / sector / cik）。昔の版の列の名前（Ticker symbol・Company など）にも対応する。"""
    for t in tables:
        f = _flat(t)
        if len(f) < min_rows:
            continue
        low = {c: str(c).lower() for c in f.columns}
        tick = next((c for c, l in low.items() if "symbol" in l or "ticker" in l), None)
        name = next((c for c, l in low.items() if "security" in l or "company" in l), None)
        if tick is None or name is None:
            continue
        sector = next((c for c, l in low.items() if "gics" in l and "sector" in l and "sub" not in l), None)
        cik = next((c for c, l in low.items() if l.strip() == "cik" or "cik" in l.split()), None)
        tk = f[tick].astype(str).str.replace(r"\[.*?\]", "", regex=True).str.strip()
        out = pd.DataFrame({"ticker": tk.map(yahoo_symbol), "name": f[name].astype(str).str.strip(),
                            "sector": f[sector].astype(str).str.strip() if sector is not None else UNKNOWN_SECTOR,
                            "cik": pd.to_numeric(f[cik], errors="coerce").astype("Int64") if cik is not None else pd.NA})
        out = out[out["ticker"].str.match(r"^[A-Z][A-Z0-9\-]{0,9}$", na=False)]
        return out.drop_duplicates("ticker").reset_index(drop=True)
    raise ValueError("このページの版に S&P500 の一覧が見つからない。表の形: " + describe(tables))


def membership_from_snapshots(snaps: dict[pd.Timestamp, pd.DataFrame]) -> tuple[dict[pd.Timestamp, frozenset], pd.DataFrame]:
    """月末ごとのページの版の一覧から、月末ごとの会社と、その時点の名前・業種・CIK。"""
    members = {m: frozenset(df["ticker"]) for m, df in sorted(snaps.items())}
    info = pd.concat([df.assign(month=m) for m, df in sorted(snaps.items())], ignore_index=True) if snaps else pd.DataFrame()
    return members, info


def link_symbols(info: pd.DataFrame) -> pd.DataFrame:
    """値段に使う記号（列 symbol）を足す（9 章）。同じ CIK の会社が後の版で 1 つの別の記号になっていれば、その記号でつなぐ（FB→META など）。

    その会社の最後の版にその記号が残っていればそのまま。最後の版の記号が 2 つ以上（株の種類が複数）なら、つながずにそのまま。CIK の無い行もそのまま。
    """
    out = info.copy()
    out["symbol"] = out["ticker"]
    ok = out["cik"].notna()
    if not ok.any():
        return out
    last = out[ok].groupby("cik")["month"].transform("max")
    final = out[ok][out[ok]["month"] == last].groupby("cik")["ticker"].agg(lambda s: sorted(set(s)))
    for i in out.index[ok]:
        names = final.get(out.at[i, "cik"], [])
        if out.at[i, "ticker"] not in names and len(names) == 1:
            out.at[i, "symbol"] = names[0]
    return out


def members_by_symbol(info: pd.DataFrame, start: pd.Timestamp | None = None) -> dict[pd.Timestamp, frozenset]:
    """月末ごとの会社を、値段に使う記号で（start より前の月は除く）。"""
    rows = info if start is None else info[info["month"] >= start]
    return {m: frozenset(g["symbol"]) for m, g in rows.groupby("month")}


def month_ends(start: pd.Timestamp, last: pd.Timestamp) -> pd.DatetimeIndex:
    return pd.date_range(pd.Timestamp(start), pd.Timestamp(last), freq="ME")


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
    df = pd.DataFrame(rows, columns=["month", "members", "with_price"]).set_index("month")
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
