"""キャリー（原油の期間構造）。docs/carry.md。EIA の NYMEX WTI 先物の限月 1〜4 の日次の清算値を使う。

- 限月 1 を持ち、切り替わる 5 営業日前に限月 2 へ乗り換えたときの、同じ限月どうしの損益（ロールの飛びを混ぜない）
- キャリー = ln(限月 1 ÷ 限月 2)。プラスがバックワーデーション
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .universe import GROUPS

EIA_CODES = ("RCLC1", "RCLC2", "RCLC3", "RCLC4")
CURVES = {"wti": EIA_CODES, "natgas": ("RNGC1", "RNGC2", "RNGC3", "RNGC4")}   # EIA の限月 1〜4 の系列（2024-04 まで）
ROLL_LEAD = 5                      # 切り替わる何営業日前に限月 2 へ乗り換えるか
SWITCH_WINDOW = (17, 26)           # 限月 1 が切り替わる日を探す暦日の範囲
VOL_WINDOW, VOL_TARGET, MAX_LEV = 60, 0.10, 2.0
COST = GROUPS["core"].cost         # 往復 0.08%
FINANCING = GROUPS["core"].financing   # 年 3%（CFD）
TREND_DAYS = 252


# --------------------------------------------------------------------------- データ
def fetch_eia_daily(code: str) -> pd.DataFrame:
    """EIA の日次の系列（キー不要の履歴 Excel）。date / value の表。"""
    import io

    from .context import _get, eia_table

    try:
        import xlrd  # type: ignore  # noqa: F401
    except ImportError:
        raise RuntimeError("Excel（.xls）を読む xlrd が無い。./cfd update で入る") from None
    area = "ng" if code.startswith("RNG") else "pet"   # 天然ガスは ng/、原油は pet/ の下にある
    raw = _get(f"https://www.eia.gov/dnav/{area}/hist_xls/{code}d.xls", timeout=60, tries=2)
    book = pd.read_excel(io.BytesIO(raw), sheet_name=None, header=None, engine="xlrd")
    name = "Data 1" if "Data 1" in book else next((k for k in book if k.lower().startswith("data")), None)
    if name is None:
        raise RuntimeError(f"EIA の Excel に Data のシートが無い（{list(book)}）")
    return eia_table(book[name])


def fetch_curve(fetch: Callable[[str], pd.DataFrame] = fetch_eia_daily, codes=EIA_CODES) -> pd.DataFrame:
    """限月 1〜4 を 1 つの表に（index: 日付、列: c1..c4）。"""
    cols = {}
    for i, code in enumerate(codes, 1):
        df = fetch(code)
        s = pd.Series(df["value"].to_numpy(float), index=pd.DatetimeIndex(pd.to_datetime(df["date"])))
        cols[f"c{i}"] = s[~s.index.duplicated(keep="last")].sort_index()
    out = pd.DataFrame(cols).dropna(subset=["c1", "c2"])
    out.index.name = "date"
    return out


def curve_path(root: str | Path, name: str = "wti") -> Path:
    return Path(root) / f"{name}_curve.csv"


def load_curve(root: str | Path, name: str = "wti") -> pd.DataFrame:
    p = curve_path(root, name)
    df = pd.read_csv(p, index_col="date", parse_dates=["date"])
    return df.sort_index()


def carry_gate(index: pd.DatetimeIndex, b: pd.Series) -> pd.DataFrame:
    """足ごとの絞り込み（列 long / short）: 前日の EIA のキャリーがプラスなら買いだけ、マイナスなら売りだけ。無い日は止めない。

    index はバックテストの足の時刻（UTC）。足の取引日（17:00 ET 区切り）の前の日までの最新の値を使う。
    """
    from .events import trading_day_keys

    days = pd.DatetimeIndex(trading_day_keys(index))
    bb = b.dropna().sort_index()
    if bb.empty:
        return pd.DataFrame({"long": True, "short": True}, index=index)
    bb.index = pd.DatetimeIndex(bb.index)
    if bb.index.tz is not None:
        bb.index = bb.index.tz_localize(None)
    pos = np.searchsorted(bb.index.to_numpy(), (days - pd.Timedelta(days=1)).to_numpy(), side="right") - 1
    v = np.where(pos >= 0, bb.to_numpy()[np.clip(pos, 0, None)], np.nan)
    return pd.DataFrame({"long": ~(v < 0), "short": ~(v > 0)}, index=index)


# --------------------------------------------------------------------------- 限月の切り替えと損益
def rule_expiry(year: int, month: int) -> pd.Timestamp:
    """NYMEX の規則: 受け渡し月の前月の 25 日の 3 営業日前（25 日が営業日でなければ、その前の営業日の 3 営業日前）。

    year/month は受け渡し月。米国の祝日は連邦の祝日で近似。
    """
    from pandas.tseries.holiday import USFederalHolidayCalendar
    from pandas.tseries.offsets import CustomBusinessDay

    bd = CustomBusinessDay(calendar=USFederalHolidayCalendar())
    prev = pd.Timestamp(year=year, month=month, day=1) - pd.offsets.MonthBegin(1)
    d25 = pd.Timestamp(year=prev.year, month=prev.month, day=25)
    if not bd.is_on_offset(d25):
        d25 = d25 - bd
    return d25 - 3 * bd


def switch_days(curve: pd.DataFrame) -> pd.Series:
    """限月 1 が次の限月に切り替わった日（True）。データから見つけ、見つからない月は規則で決める。

    戻り値の attrs["by_data"] に、データで見つけた月の割合を入れる。
    """
    c1, c2 = curve["c1"], curve["c2"]
    idx = curve.index
    out = pd.Series(False, index=idx)
    by_data = total = 0
    for (y, m), days in idx.to_series().groupby([idx.year, idx.month]):
        days = days.index
        win = days[(days.day >= SWITCH_WINDOW[0]) & (days.day <= SWITCH_WINDOW[1])]
        best, best_score = None, 0.0
        for t in win:
            i = idx.get_loc(t)
            if i == 0:
                continue
            score = abs(c1.iloc[i] - c1.iloc[i - 1]) - abs(c1.iloc[i] - c2.iloc[i - 1])   # 前日の限月 2 の方に近いほど大
            if score > best_score:
                best, best_score = t, score
        total += 1
        if best is not None and best_score > 0.01:
            out[best] = True
            by_data += 1
        else:
            nxt = pd.Timestamp(year=y, month=m, day=1) + pd.offsets.MonthBegin(1)            # 翌月が受け渡し月
            exp = rule_expiry(nxt.year, nxt.month)
            after = idx[idx > exp]
            if len(after) and after[0].month == m:
                out[after[0]] = True
    out.attrs["by_data"] = by_data / total if total else float("nan")
    return out


def front_returns(curve: pd.DataFrame, roll_lead: int = ROLL_LEAD) -> pd.DataFrame:
    """限月 1 を持ち、切り替わる roll_lead 営業日前に限月 2 へ乗り換えたときの日々の損益（1 単位の値段に対する割合）。

    列: ret（損益 ÷ 前日の持っている限月の値段の絶対値）、held（持っている限月: 1 か 2）、switch（切り替わった日）。
    """
    sw = switch_days(curve)
    swa = sw.to_numpy()
    n = len(curve)
    held = np.ones(n, int)
    sw_idx = np.flatnonzero(swa)
    for i in sw_idx:
        held[max(0, i - roll_lead):i] = 2                     # 切り替わる前の roll_lead 日は限月 2 を持つ
    c1, c2 = curve["c1"].to_numpy(float), curve["c2"].to_numpy(float)
    ret = np.full(n, np.nan)
    for i in range(1, n):
        if swa[i]:
            prev = c2[i - 1]                                   # 前日の限月 2 ＝ 当日の限月 1
            cur = c1[i] if held[i] == 1 else c2[i]
        elif held[i] == 2 and held[i - 1] == 1:
            prev, cur = c1[i - 1], c1[i]                       # 乗り換える日の終値で売るので、その日の損益は限月 1
        else:
            prev = c1[i - 1] if held[i - 1] == 1 else c2[i - 1]
            cur = c1[i] if held[i - 1] == 1 else c2[i]
        if np.isfinite(prev) and np.isfinite(cur) and prev != 0:
            ret[i] = (cur - prev) / abs(prev)
    out = pd.DataFrame({"ret": ret, "held": held, "switch": swa}, index=curve.index)
    out.attrs["by_data"] = sw.attrs.get("by_data", float("nan"))
    return out


def basis(curve: pd.DataFrame) -> pd.Series:
    """キャリー = ln(限月 1 ÷ 限月 2)。どちらかが 0 以下の日は NaN。"""
    c1, c2 = curve["c1"], curve["c2"]
    ok = (c1 > 0) & (c2 > 0)
    return pd.Series(np.where(ok, np.log(c1.where(ok) / c2.where(ok)), np.nan), index=curve.index, name="basis")


# --------------------------------------------------------------------------- 向きと成績
def month_ends(index: pd.DatetimeIndex) -> np.ndarray:
    m = index.to_period("M")
    return np.r_[m[1:] != m[:-1], True]


def signals(curve: pd.DataFrame, fr: pd.DataFrame) -> pd.DataFrame:
    """月末の終値で決める向き（翌月に効く）。carry・trend・combo。"""
    b = basis(curve)
    adj = (1 + fr["ret"].fillna(0.0)).cumprod()                       # 飛びの無いつなぎ値
    tr = np.sign(adj / adj.shift(TREND_DAYS) - 1)
    ca = np.sign(b)
    combo = tr.where(np.sign(tr) == np.sign(ca), 0.0)
    out = pd.DataFrame({"carry": ca, "trend": tr, "combo": combo}, index=curve.index)
    me = np.broadcast_to(month_ends(curve.index)[:, None], out.shape)
    return out.where(me).ffill().shift(1).fillna(0.0)                 # 月末の値を翌月に。決めた翌日から


def evaluate(fr: pd.DataFrame, pos: pd.Series, financing: float = FINANCING, cost: float = COST,
             vol_target: float = VOL_TARGET, max_lev: float = MAX_LEV) -> pd.DataFrame:
    """資金 1 に対する日ごとの損益。量は月末に決めて翌月は固定（年率 vol_target、名目 ≤ max_lev）。"""
    r = fr["ret"]
    sd = r.rolling(VOL_WINDOW, min_periods=VOL_WINDOW // 2 + 10).std() * np.sqrt(252)
    me = month_ends(fr.index)
    w = (vol_target / sd).clip(upper=max_lev).where(pd.Series(me, index=fr.index)).ffill().shift(1)
    units = (pos * w).fillna(0.0)
    units = units.where(units.abs() > 0, 0.0)
    pnl = units.shift(1).fillna(0.0) * r.fillna(0.0)
    fin = units.shift(1).abs().fillna(0.0) * financing / 252
    change = (units.shift(1).fillna(0.0) - units.shift(2).fillna(0.0)).abs()      # 前日の終値で決めた分を当日の始値で約定
    trade = change * cost / 2
    return pd.DataFrame({"ret": pnl - fin - trade, "lev": units.abs()}, index=fr.index)


def sharpe(r) -> float:
    r = pd.Series(r).dropna()
    if len(r) < 60 or r.std() <= 0:
        return float("nan")
    return float(r.mean() / r.std() * np.sqrt(252))


def placebo_sharpes(fr: pd.DataFrame, pos: pd.Series, mask: np.ndarray, n: int = 200, seed: int = 13,
                    **kw) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        k = int(rng.integers(1, len(pos)))
        p = pd.Series(np.roll(pos.to_numpy(), k), index=pos.index)
        out.append(sharpe(evaluate(fr, p, **kw)["ret"].to_numpy()[mask]))
    return np.array(out)
