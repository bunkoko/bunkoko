"""IR 評価 AI（数字版）: 決算短信・予想の修正の XBRL の数字から入力を作り、点数を付ける。docs/agents.md 10 章。

入力（開示 1 回につき 6 個。無いものは欠け）
  fc_rev      通期の純利益の会社予想の変化率（本決算では、来期の予想 ÷ 今期の実績 − 1）
  yoy_profit  純利益の前年同期比        yoy_op  営業利益の前年同期比        yoy_sales  売上の前年同期比
  dps_rev     年間配当の予想の変化率
  kind        開示の種類（1〜3 = 第 1〜3 四半期、4 = 本決算、5 = 業績予想の修正、6 = 配当予想の修正）
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .ir_scorer import EMBARGO, decision_pos

FEATURES = ["fc_rev", "yoy_profit", "yoy_op", "yoy_sales", "dps_rev", "kind"]
# 11 章で足す入力。前の 3 個は開示から、後の 3 個（予想益回り・時価総額・朝の値飛び）は判断の日の値を呼び出し側が付ける
EXTRA = ["prog_diff", "fc_op_rev", "fc_sales_rev", "ep_fc", "size", "gap_ex"]
STATE = ["fc", "dps", "shares", "equity", "eq_ratio"]     # その開示の後に分かっている値（割安さ・大きさの元）
STATE_TIMES = ["shares_time", "dps_time"]                  # 株数・1 株の配当が、いつの開示の数え方か（株式分割の調整に使う）
PROGRESS_UP = 0.05                       # F3: 進捗率が前年より 5 ポイント以上高い
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
SHARES, TREASURY = "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock", "NumberOfTreasuryStockAtTheEndOfFiscalYear"
EQUITY = ("OwnersEquity", "EquityAttributableToOwnersOfParentIFRS", "ShareholdersEquityUS", "NetAssets", "TotalEquityIFRS", "NetAssetsUS")
EQUITY_RATIO = ("CapitalAdequacyRatio", "EquityAttributableToOwnersOfParentToTotalAssetsRatioIFRS", "ShareholdersEquityRatioUS")


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


def _pick(num: pd.DataFrame, names: tuple[str, ...], need: set[str]) -> tuple[str | None, float]:
    """names の優先順で、区分が need をすべて含む最初の（名前, 値）。無ければ (None, NaN)。"""
    for name in names:
        v = _value(num, (name,), need)
        if np.isfinite(v):
            return name, v
    return None, float("nan")


def summarize_extra(facts: pd.DataFrame) -> dict:
    """summarize に足す分（11 章）: 営業利益・売上の予想、累計の営業利益、株数、自己資本、自己資本比率。"""
    if facts.empty:
        return {}
    kind = str(facts["kind"].iloc[0])
    is_num = facts["text"].isna() | (facts["text"] == "")
    num = facts[is_num].copy()
    num["tokens"] = [set(str(c).split("_")) for c in num["context"]]
    cons = "ConsolidatedMember" if any("ConsolidatedMember" in t for t in num["tokens"]) else "NonConsolidatedMember"
    if kind == "rvfc":
        cur, prev = {"CurrentYearDuration", cons, "CurrentMember", "ForecastMember"}, {"CurrentYearDuration", cons, "PreviousMember", "ForecastMember"}
        op, sales = _value(num, OPERATING, cur), _value(num, SALES, cur)
        return {"fc_op": op, "fc_sales": sales, "fc_op_rev": _ratio(op, _value(num, OPERATING, prev)), "fc_sales_rev": _ratio(sales, _value(num, SALES, prev))}
    if kind == "rvdf":
        return {}
    quarters = sorted({int(m.group(1)) for t in num["tokens"] for x in t if (m := re.fullmatch(r"CurrentAccumulatedQ(\d)Duration", x))})
    annual = not quarters
    period = "CurrentYear" if annual else f"CurrentAccumulatedQ{quarters[0]}"
    year = "NextYearDuration" if annual else "CurrentYearDuration"
    result, instant = {period + "Duration", cons, "ResultMember"}, {period + "Instant", cons, "ResultMember"}
    shares = _value(num, (SHARES,), {period + "Instant", "NonConsolidatedMember", "ResultMember"})
    treasury = _value(num, (TREASURY,), {period + "Instant", "NonConsolidatedMember", "ResultMember"})
    out = {"fc_op": _value(num, OPERATING, {year, cons, "ForecastMember"}), "fc_sales": _value(num, SALES, {year, cons, "ForecastMember"}),
           "shares": shares - (treasury if np.isfinite(treasury) else 0.0), "equity": _value(num, EQUITY, instant),
           "eq_ratio": _value(num, EQUITY_RATIO, instant)}
    name, value = _pick(num, OPERATING, result)                  # 進捗率は、同じ名前の利益どうしで比べる
    out["op_name"] = name
    if annual:
        out["actual_op"], out["actual_sales"] = value, _value(num, SALES, result)
    elif name is not None:
        out["cum_op"] = value
        out["cum_op_fc"] = _value(num, (name,), {year, cons, "ForecastMember"})
        out["prior_cum_op"] = _value(num, (name,), {f"PriorAccumulatedQ{quarters[0]}Duration", cons, "ResultMember"})
    return out


def _progress(cum: float, fc: float, prior_cum: float, prior_actual: float) -> float:
    """進捗率の差 = 今期の累計 ÷ 通期の予想 − 前年同期の累計 ÷ 前年の通期の実績（予想と前年の実績がプラスのときだけ）。"""
    if not all(np.isfinite(v) for v in (cum, fc, prior_cum, prior_actual)) or fc <= 0 or prior_actual <= 0:
        return float("nan")
    return float(np.clip(cum / fc - prior_cum / prior_actual, -1.0, 1.0))


def company_extra(items: list[tuple[str, dict]]) -> pd.DataFrame:
    """company_features と同じ順に 1 行ずつ: 進捗率の差・営業利益と売上の予想の変化率と、その開示の後に分かっている値（STATE）。

    items の辞書は summarize と summarize_extra を合わせたもの。短信では STATE を書き換える（欠けていれば欠けに）。修正の開示は
    その項目だけを書き換える。
    """
    nan = float("nan")
    fc_op: dict[int, float] = {}
    fc_sales: dict[int, float] = {}
    actual_op: dict[int, tuple[str | None, float]] = {}
    cur: int | None = None
    state: dict = {**dict.fromkeys(STATE, nan), **dict.fromkeys(STATE_TIMES, "")}
    rows = []
    for time, s in sorted(items, key=lambda x: x[0]):
        if not s:
            continue
        k = s["kind"]
        row = {"time": time, "kind": k, "prog_diff": nan, "fc_op_rev": nan, "fc_sales_rev": nan}
        if k == 5:
            row["fc_op_rev"], row["fc_sales_rev"] = s.get("fc_op_rev", nan), s.get("fc_sales_rev", nan)
            if cur is not None:
                if np.isfinite(s.get("fc_op", nan)):
                    fc_op[cur] = s["fc_op"]
                if np.isfinite(s.get("fc_sales", nan)):
                    fc_sales[cur] = s["fc_sales"]
            if np.isfinite(s.get("fc", nan)):
                state = dict(state, fc=s["fc"])
        elif k == 6:
            if np.isfinite(s.get("dps", nan)):
                state = dict(state, dps=s["dps"], dps_time=time)
        else:
            if k == 4:                                          # 本決算: 来期の予想を今期の実績と比べる
                row["fc_op_rev"] = _ratio(s.get("fc_op", nan), s.get("actual_op", nan))
                row["fc_sales_rev"] = _ratio(s.get("fc_sales", nan), s.get("actual_sales", nan))
                if s.get("fy"):
                    actual_op[s["fy"]] = (s.get("op_name"), s.get("actual_op", nan))
                cur = (s["fy"] + 12) if s.get("fy") else (cur + 12 if cur is not None else None)
                if cur is not None:
                    fc_op[cur], fc_sales[cur] = s.get("fc_op", nan), s.get("fc_sales", nan)
            else:                                               # 四半期: 同じ決算期の直前の予想と比べる。進捗率は前年の通期の実績と
                cur = s.get("fy") or cur
                if cur is not None:
                    row["fc_op_rev"] = _ratio(s.get("fc_op", nan), fc_op.get(cur, nan))
                    row["fc_sales_rev"] = _ratio(s.get("fc_sales", nan), fc_sales.get(cur, nan))
                    if np.isfinite(s.get("fc_op", nan)):
                        fc_op[cur] = s["fc_op"]
                    if np.isfinite(s.get("fc_sales", nan)):
                        fc_sales[cur] = s["fc_sales"]
                    name, prior = actual_op.get(cur - 12, (None, nan))
                    if name is not None and name == s.get("op_name"):
                        row["prog_diff"] = _progress(s.get("cum_op", nan), s.get("cum_op_fc", nan), s.get("prior_cum_op", nan), prior)
            state = {"fc": s.get("fc", nan), "dps": s.get("dps", nan), "shares": s.get("shares", nan), "equity": s.get("equity", nan),
                     "eq_ratio": s.get("eq_ratio", nan), "shares_time": time, "dps_time": time}
        rows.append({**row, **state})
    return pd.DataFrame(rows, columns=["time", "kind", "prog_diff", "fc_op_rev", "fc_sales_rev"] + STATE + STATE_TIMES)


def is_correction(titles: pd.Series) -> pd.Series:
    """題名に「訂正」のある開示（過去の決算短信・修正の出し直し。中身は古い期の数字）。"""
    return titles.map(lambda t: "訂正" in unicodedata.normalize("NFKC", str(t)))


def build(disc: pd.DataFrame, root: Path, log: Callable[[str], None] = print, extra: bool = False,
          skip_corrections: bool = False) -> pd.DataFrame:
    """保存してある数字の表（root/xbrl/<年-月>/<名前>.csv）から、開示ごとの入力を作る。列: ticker / time / FEATURES。

    extra=True なら、11 章の分（進捗率の差・営業利益と売上の予想の変化率・STATE）の列も付ける。
    skip_corrections=True なら、訂正の開示を使わない（10 章の E1〜E3 は使っていた。11 章からは使わない）。
    """
    have = {p.stem: p for p in (root / "xbrl").glob("*/*.csv")}
    d = disc[disc["xbrl"] != ""].copy()
    if skip_corrections:
        d = d[~is_correction(d["title"])]
    d["stem"] = d["xbrl"].str.replace(".zip", "", regex=False)
    d = d[d["stem"].isin(have)]
    parts = []
    for n, (ticker, g) in enumerate(d.groupby("ticker")):
        items = []
        for r in g.itertuples():
            try:
                facts = pd.read_csv(have[r.stem])
                s = summarize(facts)
                items.append((r.time, {**summarize_extra(facts), **s} if extra and s else s))
            except Exception:  # noqa: BLE001
                continue
        f = company_features(items)
        if extra and len(f):
            f = pd.concat([f, company_extra(items).drop(columns=["time", "kind"])], axis=1)
        if len(f):
            parts.append(f.assign(ticker=ticker))
        if (n + 1) % 100 == 0:
            log(f"  数字の入力 {n + 1} 社")
    cols = ["ticker", "time"] + FEATURES + (["prog_diff", "fc_op_rev", "fc_sales_rev"] + STATE + STATE_TIMES if extra else [])
    return pd.concat(parts, ignore_index=True)[cols] if parts else pd.DataFrame(columns=cols)


RECENT_DAYS = 450        # 毎日の台帳（docs/agents.md 12 章）: 予想の変化率を作るのに、さかのぼる日数（直前の予想が入る長さ）


def recent_revisions(disc: pd.DataFrame, root: Path, index: pd.DatetimeIndex, tickers: list[str],
                     days: int = RECENT_DAYS, log: Callable[[str], None] = print) -> pd.DataFrame:
    """毎日の台帳の E1 の帳に使う表（docs/agents.md 12 章）。直近 days 日の開示だけを読み、判断の日ごとに 1 行。

    列: date（判断の日）/ ticker / fc_rev / kind。訂正の開示は使わない（11 章からの決まり）。保存してある数字だけを使うので、
    まだ東証のサイトに載っていない開示（多くは前日の夕方の分）は入らない（その日の判断に間に合わなかった、という先のデータの事実のまま）。
    """
    cols = ["date", "ticker", "fc_rev", "kind"]
    if disc.empty or not len(index):
        return pd.DataFrame(columns=cols)
    since = (index[-1] - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    feat = build(disc[disc["time"] >= since], root, log=log, skip_corrections=True)
    if feat.empty:
        return pd.DataFrame(columns=cols)
    t = daily(feat, index, tickers)
    out = pd.DataFrame({"date": index[t["pos"].to_numpy()], "ticker": [tickers[k] for k in t["j"].to_numpy()],
                        "fc_rev": t["fc_rev"].to_numpy(), "kind": t["kind"].to_numpy()})
    return out[cols]


def daily(feat: pd.DataFrame, index: pd.DatetimeIndex, tickers: list[str], features: list[str] | None = None) -> pd.DataFrame:
    """株 × 判断の日ごとに 1 行にまとめる（同じ日に短信と修正が出たら、修正の変化率を優先し、ほかは最初にある値）。列: pos / j / FEATURES。"""
    features = features or FEATURES
    col = {t: k for k, t in enumerate(tickers)}
    d = feat.assign(pos=decision_pos(feat["time"], index), j=feat["ticker"].map(col)).dropna(subset=["j"])
    d = d[d["pos"] < len(index)].astype({"j": int})
    d = d.sort_values(["pos", "j", "kind"], ascending=[True, True, False])       # 修正（5・6）を先に
    agg = {c: "first" for c in features if c != "kind"}                          # first は欠けを飛ばす
    out = d.groupby(["pos", "j"], as_index=False).agg({**agg, "kind": "min"})    # 種類は短信の方（小さい番号）
    return out[["pos", "j"] + features]


def walk_forward(table: pd.DataFrame, y: np.ndarray, index: pd.DatetimeIndex, first_year: int = FIRST_TEST_YEAR,
                 embargo: int = EMBARGO, params: dict | None = None, log: Callable[[str], None] = print,
                 features: list[str] | None = None) -> np.ndarray:
    """毎年、その年より前（embargo 営業日ぶん手前まで）の開示だけで学び、その年の開示に点数を付ける。無い所は NaN。"""
    from sklearn.ensemble import HistGradientBoostingRegressor

    pos, j = table["pos"].to_numpy(), table["j"].to_numpy()
    x = table[features or FEATURES].to_numpy(dtype=float)
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
