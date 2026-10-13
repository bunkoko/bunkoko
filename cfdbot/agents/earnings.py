"""IR 評価 AI（数字版）: 決算短信・予想の修正の XBRL の数字から入力を作り、点数を付ける。docs/agents.md 10 章。

入力（開示 1 回につき 6 個。無いものは欠け）
  fc_rev      通期の純利益の会社予想の変化率（本決算では、来期の予想 ÷ 今期の実績 − 1）
  yoy_profit  純利益の前年同期比        yoy_op  営業利益の前年同期比        yoy_sales  売上の前年同期比
  dps_rev     年間配当の予想の変化率
  kind        開示の種類（1〜3 = 第 1〜3 四半期、4 = 本決算、5 = 業績予想の修正、6 = 配当予想の修正）
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .ir_scorer import EMBARGO, decision_pos

FEATURES = ["fc_rev", "yoy_profit", "yoy_op", "yoy_sales", "dps_rev", "kind"]
UP_REVISION = 0.05                       # E1: 通期の純利益の予想を 5% 以上引き上げた
FIRST_TEST_YEAR, MIN_TRAIN = 2019, 2000
GBM_PARAMS = dict(max_iter=100, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200, early_stopping=False, random_state=0)
# 名前の優先順（上から順に、最初に見つかったものを使う。IFRS・米国基準は名前の後ろに IFRS・US が付く）
PROFIT = ("ProfitAttributableToOwnersOfParent", "ProfitAttributableToOwnersOfParentIFRS", "NetIncomeAttributableToOwnersOfParentUS",
          "NetIncome", "ProfitIFRS", "NetIncomeUS", "NetIncomeIFRS")
OPERATING = ("OperatingIncome", "OperatingIncomeIFRS", "OperatingIncomeUS", "OrdinaryIncome", "ProfitBeforeTaxIFRS",
             "IncomeBeforeIncomeTaxesUS", "OrdinaryIncomeBK", "OrdinaryIncomeIN")
SALES = ("NetSales", "SalesIFRS", "RevenueIFRS", "NetSalesIFRS", "NetSalesUS", "OperatingRevenues", "OperatingRevenuesIFRS",
         "OperatingRevenuesSpecific", "OrdinaryRevenuesBK", "OrdinaryRevenuesIN", "OperatingRevenuesSE", "NetOperatingRevenuesSE",
         "GrossOperatingRevenues", "TotalOperatingRevenues", "RevenuesUS", "OperatingRevenuesUS")


def _value(num: pd.DataFrame, names: tuple[str, ...], need: set[str]) -> float:
    """names の優先順で、区分（context を _ で切った語）が need をすべて含む最初の値。予想に幅があれば上下の真ん中。"""
    for name in names:
        rows = num[num["name"] == name]
        if rows.empty:
            continue
        tok = rows["tokens"]
        hit = rows[[need <= t for t in tok]]
        if len(hit):
            return float(hit["value"].iloc[0])
        if "ForecastMember" in need:
            base = need - {"ForecastMember"}
            lo = rows[[(base | {"LowerMember"}) <= t for t in tok]]
            hi = rows[[(base | {"UpperMember"}) <= t for t in tok]]
            if len(lo) and len(hi):
                return float((lo["value"].iloc[0] + hi["value"].iloc[0]) / 2)
    return float("nan")


def _ratio(new: float, old: float) -> float:
    if not (np.isfinite(new) and np.isfinite(old)) or old == 0:
        return float("nan")
    return float(np.clip((new - old) / abs(old), -1.0, 1.0))


def _fy_key(text: str) -> int | None:
    """決算期の末日（2027-03-31 など）を 年 × 12 ＋ 月 に。読めなければ None。"""
    m = re.search(r"(\d{4})\D+(\d{1,2})", str(text))
    return int(m.group(1)) * 12 + int(m.group(2)) if m else None


def summarize(facts: pd.DataFrame) -> dict:
    """1 つの開示（サマリーの表）から、種類・決算期・予想・前年同期比などを取り出す。"""
    if facts.empty:
        return {}
    kind = str(facts["kind"].iloc[0])
    is_num = facts["text"].isna() | (facts["text"] == "")
    num = facts[is_num].copy()
    num["tokens"] = [set(str(c).split("_")) for c in num["context"]]
    notes = facts[~is_num].drop_duplicates("name").set_index("name")["text"].to_dict()
    cons = "ConsolidatedMember" if any("ConsolidatedMember" in t for t in num["tokens"]) else "NonConsolidatedMember"
    out = {"fy": _fy_key(notes.get("FiscalYearEnd", ""))}
    if kind == "rvfc":
        cur, prev = {"CurrentYearDuration", cons, "CurrentMember", "ForecastMember"}, {"CurrentYearDuration", cons, "PreviousMember", "ForecastMember"}
        new, old = _value(num, PROFIT, cur), _value(num, PROFIT, prev)
        return dict(out, kind=5, fc=new, fc_rev=_ratio(new, old))
    if kind == "rvdf":
        cur = {"CurrentYearDuration", "AnnualMember", "CurrentMember", "ForecastMember"}
        new, old = _value(num, ("DividendPerShare",), cur), _value(num, ("DividendPerShare",), (cur - {"CurrentMember"}) | {"PreviousMember"})
        return dict(out, kind=6, dps=new, dps_rev=_ratio(new, old))
    quarters = sorted({int(m.group(1)) for t in num["tokens"] for x in t if (m := re.fullmatch(r"CurrentAccumulatedQ(\d)Duration", x))})
    annual = not quarters
    period = "CurrentYearDuration" if annual else f"CurrentAccumulatedQ{quarters[0]}Duration"
    result = {period, cons, "ResultMember"}
    out.update(kind=4 if annual else quarters[0],
               yoy_profit=_clip(_value(num, tuple("ChangeIn" + n for n in PROFIT), result)),
               yoy_op=_clip(_value(num, tuple("ChangeIn" + n for n in OPERATING), result)),
               yoy_sales=_clip(_value(num, tuple("ChangeIn" + n for n in SALES), result)))
    year = "NextYearDuration" if annual else "CurrentYearDuration"
    out["fc"] = _value(num, PROFIT, {year, cons, "ForecastMember"})
    out["dps"] = _value(num, ("DividendPerShare",), {year, "AnnualMember", "ForecastMember"})
    if annual:
        out["actual"] = _value(num, PROFIT, result)
        out["dps_actual"] = _value(num, ("DividendPerShare",), {"CurrentYearDuration", "AnnualMember", "ResultMember"})
    return out


def _clip(x: float) -> float:
    return float(np.clip(x, -1.0, 1.0)) if np.isfinite(x) else float("nan")


def company_features(items: list[tuple[str, dict]]) -> pd.DataFrame:
    """1 社の開示（時刻, summarize の結果）を古い順に見て、直前の予想と比べた変化率を付ける。"""
    fc: dict[int, float] = {}
    dps: dict[int, float] = {}
    cur: int | None = None
    rows = []
    for time, s in sorted(items, key=lambda x: x[0]):
        if not s:
            continue
        k = s["kind"]
        row = {"time": time, "kind": k, "fc_rev": np.nan, "dps_rev": np.nan,
               "yoy_profit": s.get("yoy_profit", np.nan), "yoy_op": s.get("yoy_op", np.nan), "yoy_sales": s.get("yoy_sales", np.nan)}
        if k == 5:                                              # 業績予想の修正: 前と後は書類の中にある
            row["fc_rev"] = s.get("fc_rev", np.nan)
            if cur is not None and np.isfinite(s.get("fc", np.nan)):
                fc[cur] = s["fc"]
        elif k == 6:                                            # 配当予想の修正
            row["dps_rev"] = s.get("dps_rev", np.nan)
            if cur is not None and np.isfinite(s.get("dps", np.nan)):
                dps[cur] = s["dps"]
        elif k == 4:                                            # 本決算: 来期の予想を今期の実績と比べる
            row["fc_rev"] = _ratio(s.get("fc", np.nan), s.get("actual", np.nan))
            row["dps_rev"] = _ratio(s.get("dps", np.nan), s.get("dps_actual", np.nan))
            cur = (s["fy"] + 12) if s.get("fy") else (cur + 12 if cur is not None else None)
            if cur is not None:
                fc[cur], dps[cur] = s.get("fc", np.nan), s.get("dps", np.nan)
        else:                                                   # 四半期: 同じ決算期の直前の予想と比べる
            cur = s.get("fy") or cur
            if cur is not None:
                row["fc_rev"] = _ratio(s.get("fc", np.nan), fc.get(cur, np.nan))
                row["dps_rev"] = _ratio(s.get("dps", np.nan), dps.get(cur, np.nan))
                if np.isfinite(s.get("fc", np.nan)):
                    fc[cur] = s["fc"]
                if np.isfinite(s.get("dps", np.nan)):
                    dps[cur] = s["dps"]
        rows.append(row)
    return pd.DataFrame(rows, columns=["time"] + FEATURES)


def build(disc: pd.DataFrame, root: Path, log: Callable[[str], None] = print) -> pd.DataFrame:
    """保存してある数字の表（root/xbrl/<年-月>/<名前>.csv）から、開示ごとの入力を作る。列: ticker / time / FEATURES。"""
    have = {p.stem: p for p in (root / "xbrl").glob("*/*.csv")}
    d = disc[disc["xbrl"] != ""].copy()
    d["stem"] = d["xbrl"].str.replace(".zip", "", regex=False)
    d = d[d["stem"].isin(have)]
    parts = []
    for n, (ticker, g) in enumerate(d.groupby("ticker")):
        items = []
        for r in g.itertuples():
            try:
                items.append((r.time, summarize(pd.read_csv(have[r.stem]))))
            except Exception:  # noqa: BLE001
                continue
        f = company_features(items)
        if len(f):
            parts.append(f.assign(ticker=ticker))
        if (n + 1) % 100 == 0:
            log(f"  数字の入力 {n + 1} 社")
    cols = ["ticker", "time"] + FEATURES
    return pd.concat(parts, ignore_index=True)[cols] if parts else pd.DataFrame(columns=cols)


def daily(feat: pd.DataFrame, index: pd.DatetimeIndex, tickers: list[str]) -> pd.DataFrame:
    """株 × 判断の日ごとに 1 行にまとめる（同じ日に短信と修正が出たら、修正の変化率を優先し、ほかは最初にある値）。列: pos / j / FEATURES。"""
    col = {t: k for k, t in enumerate(tickers)}
    d = feat.assign(pos=decision_pos(feat["time"], index), j=feat["ticker"].map(col)).dropna(subset=["j"])
    d = d[d["pos"] < len(index)].astype({"j": int})
    d = d.sort_values(["pos", "j", "kind"], ascending=[True, True, False])       # 修正（5・6）を先に
    agg = {c: "first" for c in FEATURES if c != "kind"}                          # first は欠けを飛ばす
    out = d.groupby(["pos", "j"], as_index=False).agg({**agg, "kind": "min"})    # 種類は短信の方（小さい番号）
    return out[["pos", "j"] + FEATURES]


def walk_forward(table: pd.DataFrame, y: np.ndarray, index: pd.DatetimeIndex, first_year: int = FIRST_TEST_YEAR,
                 embargo: int = EMBARGO, params: dict | None = None, log: Callable[[str], None] = print) -> np.ndarray:
    """毎年、その年より前（embargo 営業日ぶん手前まで）の開示だけで学び、その年の開示に点数を付ける。無い所は NaN。"""
    from sklearn.ensemble import HistGradientBoostingRegressor

    pos, j = table["pos"].to_numpy(), table["j"].to_numpy()
    x = table[FEATURES].to_numpy(dtype=float)
    yrow = y[pos, j]
    years = index.year.to_numpy()[pos]
    out = np.full(len(table), np.nan)
    for year in range(first_year, int(index[-1].year) + 1):
        test = years == year
        if not test.any():
            continue
        first = int(np.flatnonzero(index.year == year)[0])
        train = (pos < first - embargo) & np.isfinite(yrow)
        if train.sum() < MIN_TRAIN:
            continue
        use = np.isfinite(x[train]).any(axis=0)                                  # 学習の期間に 1 つも値の無い入力は外す（学習の部品が受け付けない）
        model = HistGradientBoostingRegressor(**(params or GBM_PARAMS)).fit(x[train][:, use], yrow[train])
        out[test] = model.predict(x[test][:, use])
        log(f"  {year}: 学習 {int(train.sum()):,} 件 → 点数 {int(test.sum()):,} 件")
    return out
