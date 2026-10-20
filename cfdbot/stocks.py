"""上がりやすい株を探す（時期・時間帯・業種・会社）。docs/stocks.md。

どの仮説も「差の系列 d」（コストを引いた後。仮説が正しければ平均がプラス）にして、同じ条件で判定する。
偶然との比較（placebo）は、コストを引く前の d の平均どうしで比べる。
"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd

from .stats import expected_max_z

PRIOR_TRIALS, NEW_TRIALS = 41, 17
COST_JP, COST_JP_SECTOR_ETF, COST_US, COST_US_FULL = 0.0010, 0.0020, 0.0030, 0.0100
T_MIN = 1.65                       # 片側 5%
TOM_BEFORE, TOM_AFTER = 1, 3       # 月の最後の 1 営業日＋翌月の最初の 3 営業日
TOP_SECTORS, MOM_MONTHS, SEASON_YEARS, SEASON_MIN_YEARS = 5, 6, 20, 10
PEAD_HOLD, MIN_NAMES = 60, 10
CLIP = 0.30                        # 1 日の値動きの上限（データの誤りへの備え）


# --------------------------------------------------------------------------- 判定
def t_value(d) -> float:
    v = np.asarray(pd.Series(d).dropna(), dtype=float)
    if len(v) < 3 or v.std(ddof=1) <= 0:
        return float("nan")
    return float(v.mean() / (v.std(ddof=1) / np.sqrt(len(v))))


def judge(d: pd.Series, split: str, n_trials: int = PRIOR_TRIALS + NEW_TRIALS, placebo_pct: float | None = None) -> dict:
    """docs/stocks.md 5 章の条件。d は月ごと（か年ごと）の差の系列。"""
    d = d.dropna()
    a, b = d[d.index < pd.Timestamp(split)], d[d.index >= pd.Timestamp(split)]
    t_all, t2, thr = t_value(d), t_value(b), expected_max_z(n_trials)
    c1 = len(a) > 0 and len(b) > 0 and a.mean() > 0 and b.mean() > 0
    c2, c3 = t2 >= T_MIN, t_all >= thr
    c4 = placebo_pct is None or placebo_pct >= 0.95
    if c1 and c2 and c3 and c4:
        verdict = "使えそう"
    elif c1 and c4 and t_all >= T_MIN:
        verdict = "まだ判断できない"
    else:
        verdict = "使えない"
    return {"n": len(d), "mean": float(d.mean()), "mean1": float(a.mean()) if len(a) else np.nan,
            "mean2": float(b.mean()) if len(b) else np.nan, "t": t_all, "t1": t_value(a), "t2": t2,
            "threshold": thr, "placebo": placebo_pct, "verdict": verdict, "start": d.index[0], "end": d.index[-1]}


def month_sum(daily: pd.Series) -> pd.Series:
    """日ごとの系列を月ごとに足す。最初に 0 が続く部分（まだ持っていない期間）は落とす。"""
    s = daily.dropna()
    nz = np.flatnonzero(s.to_numpy() != 0)
    if len(nz) == 0:
        return pd.Series(dtype=float)
    return s.iloc[nz[0]:].resample("ME").sum()


# --------------------------------------------------------------------------- 時期: 月替わり（T1・T2）
def _tom_cycles(index: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """窓の最初の位置（月の最後の営業日）と、次の窓の最初までの日数。"""
    per = index.to_period("M")
    first = np.flatnonzero(per[1:] != per[:-1]) + 1            # 月の最初の営業日の位置
    starts = first - TOM_BEFORE
    return starts[:-1], np.diff(starts)


def tom_mask(index: pd.DatetimeIndex) -> np.ndarray:
    """月替わりの 4 日（月の最後の営業日＋翌月の最初の 3 営業日）かどうか。"""
    per = index.to_period("M")
    first = np.flatnonzero(per[1:] != per[:-1]) + 1
    mask = np.zeros(len(index), dtype=bool)
    for k in range(-TOM_BEFORE, TOM_AFTER):
        mask[np.clip(first + k, 0, len(index) - 1)] = True
    return mask


def _cumsum(close: pd.Series) -> np.ndarray:
    return np.r_[0.0, np.nancumsum(close.pct_change().to_numpy())]


def tom_diff(close: pd.Series, cost: float = 0.0) -> pd.Series:
    """月ごとに、月替わり 4 日の 1 日あたりの平均（−コスト÷4）− 残りの日の 1 日あたりの平均。"""
    w = TOM_BEFORE + TOM_AFTER
    s, n = _tom_cycles(close.index)
    cs = _cumsum(close)
    win = cs[s + w] - cs[s]
    rest = cs[s + n] - cs[s + w]
    d = win / w - cost / w - rest / (n - w)
    return pd.Series(d, index=close.index[s + TOM_BEFORE])


def tom_placebo(close: pd.Series, n: int = 1000, seed: int = 11) -> np.ndarray:
    """月ごとに 4 日の窓をでたらめな位置に置いたときの d の平均（コスト無し）。"""
    w = TOM_BEFORE + TOM_AFTER
    s, ln = _tom_cycles(close.index)
    cs = _cumsum(close)
    total = cs[s + ln] - cs[s]
    rng = np.random.default_rng(seed)
    out = np.empty(n)
    for i in range(n):
        k = (rng.random(len(s)) * (ln - w + 1)).astype(int)
        win = cs[s + k + w] - cs[s + k]
        out[i] = np.mean(win / w - (total - win) / (ln - w))
    return out


# --------------------------------------------------------------------------- 時期: 半年（T3〜T5）
def season_blocks(close: pd.Series, start_month: int) -> pd.DataFrame:
    """start_month から始まる 12 か月を 1 行にした月ごとの対数リターン（12 か月そろった行だけ）。"""
    m = np.log(close.resample("ME").last().dropna()).diff().dropna()
    per = m.index.to_period("M")
    k = per.year * 12 + per.month - 1 - (start_month - 1)
    t = pd.DataFrame({"block": k // 12, "pos": k % 12, "r": m.to_numpy()})
    b = t.pivot(index="block", columns="pos", values="r").dropna()
    b.index = pd.to_datetime([f"{int(y)}-{start_month:02d}-01" for y in b.index])
    return b


def season_diff(blocks: pd.DataFrame, good: tuple[int, ...], cost: float = 0.0) -> pd.Series:
    """年ごとに（良いとされる 6 か月）−（残りの 6 か月）− コスト 1 往復。"""
    g = list(good)
    return blocks[g].sum(axis=1) - blocks.drop(columns=g).sum(axis=1) - cost


def season_placebo(blocks: pd.DataFrame, n_good: int = 6, n: int = 1000, seed: int = 11) -> np.ndarray:
    """年ごとに 12 か月からでたらめに 6 か月を選んだときの d の平均。"""
    b = blocks.to_numpy()
    total = b.sum(axis=1)
    rng = np.random.default_rng(seed)
    out = np.empty(n)
    for i in range(n):
        pick = np.argsort(rng.random(b.shape), axis=1)[:, :n_good]
        chosen = np.take_along_axis(b, pick, axis=1).sum(axis=1)
        out[i] = np.mean(2 * chosen - total)
    return out


# --------------------------------------------------------------------------- 時間帯: 夜と昼（N1・N2）
# --------------------------------------------------------------------------- 先の記録のための値段の控え（docs/agents.md 12.7）
ARCHIVE = "archive"   # data/stocks/archive（コミットしない）


def archive_prices(root: str | Path, open_: pd.DataFrame, close: pd.DataFrame, prefix: str = "jp") -> Path:
    """取り直した値段を控え（root/archive/{prefix}_open.csv・_close.csv）に足す。新しく取った値を優先し、
    取れなくなった銘柄（上場廃止・TOB・一覧から外れた）の過去の値は消さずに残す。"""
    d = Path(root) / ARCHIVE
    d.mkdir(parents=True, exist_ok=True)
    for name, new in (("open", open_), ("close", close)):
        p = d / f"{prefix}_{name}.csv"
        merged = new.combine_first(pd.read_csv(p, index_col=0, parse_dates=True)) if p.exists() else new
        tmp = p.with_name(p.name + ".tmp")
        merged.sort_index().to_csv(tmp, float_format="%.6g")
        tmp.replace(p)
    return d


def record_prices(root: str | Path, prefix: str = "jp") -> tuple[pd.DataFrame, pd.DataFrame]:
    """先の記録に使う値段: 今の値段（優先）に、控えにしか無い銘柄・日を足したもの。控えが無ければ今の値段だけ。"""
    root = Path(root)
    out = []
    for name in ("open", "close"):
        cur = pd.read_csv(root / f"{prefix}_{name}.csv", index_col=0, parse_dates=True)
        p = root / ARCHIVE / f"{prefix}_{name}.csv"
        out.append(cur.combine_first(pd.read_csv(p, index_col=0, parse_dates=True)).sort_index() if p.exists() else cur)
    return drop_closed_days(out[0], out[1])


def drop_closed_days(open_: pd.DataFrame, close: pd.DataFrame, share: float = 0.8) -> tuple[pd.DataFrame, pd.DataFrame]:
    """休場日なのに前日の値のまま入っている日（share 以上の銘柄の終値が前日と同じ）を落とす。Yahoo の古い日本株にある。"""
    same = ((close == close.shift(1)) & close.notna()).sum(axis=1) / close.notna().sum(axis=1).clip(lower=1)
    keep = (same < share).to_numpy()
    return open_[keep], close[keep]


def night_day(open_: pd.DataFrame, close: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """夜 = 前日の終値 → 始値、昼 = 始値 → 終値。±30% にそろえる。"""
    night = (open_ / close.shift(1) - 1).clip(-CLIP, CLIP)
    day = (close / open_ - 1).clip(-CLIP, CLIP)
    return night, day


def night_day_diff(night: pd.Series, day: pd.Series, cost: float = 0.0) -> pd.Series:
    """日ごとに（夜 − コスト）− 昼。月ごとに足す。"""
    ok = night.notna() & day.notna()
    return month_sum((night - cost - day)[ok])


# --------------------------------------------------------------------------- 業種: 上位を選ぶ（S1〜S3）
def momentum_score(ret: pd.DataFrame, months: int = MOM_MONTHS) -> pd.DataFrame:
    """直近 months か月のリターン（その月を含む）。"""
    return np.log1p(ret).rolling(months, min_periods=months).sum()


def seasonal_score(ret: pd.DataFrame, years: int = SEASON_YEARS, min_years: int = SEASON_MIN_YEARS) -> pd.DataFrame:
    """翌月と同じ暦の月の、過去 years 年の平均リターン（月が欠けずに並んでいること）。"""
    parts = [ret.shift(12 * k - 1) for k in range(1, years + 1)]
    cnt = sum(p.notna().astype(int) for p in parts)
    tot = sum(p.fillna(0.0) for p in parts)
    return (tot / cnt).where(cnt >= min_years)


def top_n_active(ret: pd.DataFrame, score: pd.DataFrame, n: int = TOP_SECTORS, cost: float = 0.0) -> pd.DataFrame:
    """月末の score の上位 n を等分で翌月持つ。gross = 上位の平均 − 全体の平均、d = gross − 入れ替えた割合 × cost。"""
    r, s = ret.to_numpy(), score.to_numpy()
    rows, prev = [], None
    for t in range(len(ret) - 1):
        ok = np.isfinite(s[t]) & np.isfinite(r[t + 1])
        if ok.sum() < 2 * n:
            prev = None
            continue
        cand = np.flatnonzero(ok)
        top = set(cand[np.argsort(-s[t][cand], kind="stable")[:n]].tolist())
        gross = r[t + 1][sorted(top)].mean() - r[t + 1][cand].mean()
        turn = 1.0 if prev is None else 1.0 - len(top & prev) / n
        rows.append((ret.index[t + 1], gross, turn))
        prev = top
    out = pd.DataFrame(rows, columns=["time", "gross", "turnover"]).set_index("time")
    out["d"] = out["gross"] - out["turnover"] * cost
    return out


def top_n_now(score: pd.DataFrame, n: int = TOP_SECTORS) -> pd.Series:
    """最後の月末の score の上位 n（今あてはまるもの）。"""
    return score.iloc[-1].dropna().sort_values(ascending=False).head(n)


def top_n_placebo(ret: pd.DataFrame, score: pd.DataFrame, n_top: int = TOP_SECTORS, n: int = 1000, seed: int = 11) -> np.ndarray:
    """毎月でたらめに n_top 個選んだときの gross の平均（本物と同じ月だけ）。"""
    r, s = ret.to_numpy(), score.to_numpy()
    months = [(r[t + 1], np.flatnonzero(np.isfinite(s[t]) & np.isfinite(r[t + 1]))) for t in range(len(ret) - 1)]
    months = [(x[c], x[c].mean()) for x, c in months if len(c) >= 2 * n_top]
    rng = np.random.default_rng(seed)
    out = np.zeros(n)
    for x, mean in months:
        pick = np.argsort(rng.random((n, len(x))), axis=1)[:, :n_top]
        out += x[pick].mean(axis=1) - mean
    return out / max(len(months), 1)


def group_returns(ret: pd.DataFrame, groups: dict[str, str], min_members: int = 5) -> pd.DataFrame:
    """業種ごとのリターン = 所属する株の平均（min_members 社以上の業種。その月に 3 社以上の値がある時だけ）。"""
    g = pd.Series(groups).reindex(ret.columns)
    out = {}
    for name, cols in g.groupby(g).groups.items():
        if len(cols) >= min_members:
            sub = ret[list(cols)]
            out[name] = sub.mean(axis=1).where(sub.notna().sum(axis=1) >= 3)
    return pd.DataFrame(out)


def monthly_returns(close: pd.DataFrame) -> pd.DataFrame:
    """日ごとの変化率（±30% にそろえる）を月ごとに掛け合わせる。値が 1 日も無い月は欠け。最後の月（途中）も含む。

    月末の終値どうしで割ると、Yahoo の価格の誤り（1 日で数万倍など）がそのまま入るため。
    """
    r = np.log1p(close.pct_change(fill_method=None).clip(-CLIP, CLIP))
    m = r.groupby(r.index.to_period("M")).sum(min_count=1)
    m.index = m.index.to_timestamp(how="end").normalize()
    return np.expm1(m)


# --------------------------------------------------------------------------- 会社: 配当の月（C4・C5）
def ex_month_flags(dividends: pd.DataFrame, months: pd.DatetimeIndex, tickers) -> pd.DataFrame:
    """月 × 株で、その月に権利落ち日があったか。dividends は列 date / ticker。"""
    per = pd.to_datetime(dividends["date"]).dt.to_period("M").dt.to_timestamp(how="end").dt.normalize()
    flags = pd.crosstab(per, dividends["ticker"]) > 0
    return flags.reindex(index=months.normalize(), columns=list(tickers), fill_value=False)


def dividend_month_sets(ret: pd.DataFrame, ex: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """その月が「配当の月」と見込まれる株（12 か月前の同じ月に権利落ち）と、ほかの配当株（直近 12 か月に配当あり）。"""
    e = ex.to_numpy()
    pred = np.zeros_like(e)
    pred[12:] = e[:-12]
    payer = np.zeros_like(e)
    payer[1:] = (pd.DataFrame(e).rolling(12, min_periods=1).sum().to_numpy() > 0)[:-1]   # 前月までの 12 か月
    ok = np.isfinite(ret.to_numpy())
    return pred & ok, payer & ~pred & ok


def dividend_month_diff(ret: pd.DataFrame, ex: pd.DataFrame, cost: float = 0.0, min_names: int = MIN_NAMES) -> pd.DataFrame:
    treat, ctrl = dividend_month_sets(ret, ex)
    r = np.nan_to_num(ret.to_numpy())
    nt, nc = treat.sum(axis=1), ctrl.sum(axis=1)
    ok = (nt >= min_names) & (nc >= min_names)
    gross = (r * treat).sum(axis=1) / np.maximum(nt, 1) - (r * ctrl).sum(axis=1) / np.maximum(nc, 1)
    out = pd.DataFrame({"gross": gross, "n_treat": nt, "n_ctrl": nc}, index=ret.index)[ok]
    out["d"] = out["gross"] - cost
    return out


def dividend_month_placebo(ret: pd.DataFrame, ex: pd.DataFrame, n: int = 1000, seed: int = 11,
                           min_names: int = MIN_NAMES) -> np.ndarray:
    """同じ月に、配当株の中から同じ数をでたらめに選んだときの gross の平均。"""
    treat, ctrl = dividend_month_sets(ret, ex)
    r = ret.to_numpy()
    rng = np.random.default_rng(seed)
    out, months = np.zeros(n), 0
    for t in range(len(ret)):
        k, c = int(treat[t].sum()), int(ctrl[t].sum())
        if k < min_names or c < min_names:
            continue
        x = r[t][treat[t] | ctrl[t]]
        pick = np.argsort(rng.random((n, len(x))), axis=1)[:, :k]
        chosen = x[pick].sum(axis=1)
        out += chosen / k - (x.sum() - chosen) / c
        months += 1
    return out / max(months, 1)


# --------------------------------------------------------------------------- 会社: 決算の後（C6・C7）
def pead_events(ret: pd.DataFrame, mkt: pd.Series, events: pd.DataFrame, hold: int = PEAD_HOLD) -> pd.DataFrame:
    """決算ごとの 2 日間の反応（株 − 全銘柄の平均）と「良い決算」の印。events は列 ticker / date / surprise。

    i1 = 発表日以降で最初の営業日の次の営業日（その終値で買う）。持つのは i1+1 〜 end（次の決算の i1 まで）。
    """
    idx = ret.index
    cols = {t: k for k, t in enumerate(ret.columns)}
    ev = events[events["ticker"].isin(cols)].copy()
    ev["j"] = ev["ticker"].map(cols).astype(int)
    ev["i1"] = idx.searchsorted(pd.to_datetime(ev["date"]).to_numpy()) + 1
    ev = ev[ev["i1"] < len(idx)]
    a = ret.to_numpy() - mkt.to_numpy()[:, None]
    i1, j = ev["i1"].to_numpy(), ev["j"].to_numpy()
    ev["reaction"] = a[i1 - 1, j] + a[i1, j]
    ev = ev.dropna(subset=["reaction", "surprise"]).sort_values(["j", "i1"]).drop_duplicates(["j", "i1"], keep="last")
    ev["good"] = (ev["reaction"] > 0) & (ev["surprise"] > 0)
    when = idx[ev["i1"].to_numpy()]
    ev["quarter"] = (when.year * 4 + when.quarter).to_numpy()
    nxt = ev.groupby("j")["i1"].shift(-1).fillna(10 ** 9)
    ev["end"] = np.minimum(ev["i1"] + hold, nxt).astype(int)
    return ev.reset_index(drop=True)


def pead_holdings(ev: pd.DataFrame, good: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """日 × 株で、その日に持っているか（good の決算の i1+1 〜 end）。"""
    t = shape[0]
    diff = np.zeros((t + 1, shape[1]), dtype=np.int32)
    g = ev[np.asarray(good, dtype=bool)]
    start, end, j = g["i1"].to_numpy() + 1, g["end"].to_numpy() + 1, g["j"].to_numpy()
    keep = start < t
    np.add.at(diff, (start[keep], j[keep]), 1)
    np.add.at(diff, (np.minimum(end[keep], t), j[keep]), -1)
    return diff[:-1].cumsum(axis=0) > 0


def pead_active(ret: pd.DataFrame, mkt: pd.Series, hold: np.ndarray, cost: float = 0.0,
                min_names: int = MIN_NAMES) -> pd.DataFrame:
    """持っている株を毎日等分。gross = その平均 − 全銘柄の平均、d = gross − 入れ替えた割合 × cost（日ごと）。"""
    r = np.nan_to_num(ret.to_numpy())
    h = hold & ret.notna().to_numpy()
    n = h.sum(axis=1)
    on = n >= min_names
    w = np.where(on[:, None], h / np.maximum(n, 1)[:, None], 0.0)
    gross = np.where(on, (w * r).sum(axis=1) - mkt.to_numpy(), 0.0)
    turn = 0.5 * np.abs(np.diff(w, axis=0, prepend=np.zeros((1, w.shape[1])))).sum(axis=1)
    return pd.DataFrame({"gross": gross, "d": gross - cost * turn, "names": n, "turnover": turn}, index=ret.index)


def pead_placebo(ret: pd.DataFrame, mkt: pd.Series, ev: pd.DataFrame, n: int = 200, seed: int = 11) -> np.ndarray:
    """同じ四半期の決算の中で「良い決算」の印をでたらめに付け替えたときの gross の日ごとの平均。"""
    rng = np.random.default_rng(seed)
    groups = list(ev.groupby("quarter").indices.values())
    good = ev["good"].to_numpy()
    out = np.empty(n)
    for i in range(n):
        fake = good.copy()
        for g in groups:
            fake[g] = rng.permutation(good[g])
        out[i] = pead_active(ret, mkt, pead_holdings(ev, fake, ret.shape))["gross"].mean()
    return out


# --------------------------------------------------------------------------- データの読み取り
def parse_french(text: str, section: str | None = None) -> pd.DataFrame:
    """フレンチ教授のデータ集の CSV から月次の表を 1 つ取り出す（% を小数に。−99.99 などは欠け）。

    section を渡すと、その言葉を見出しに含む表。渡さなければ最初の月次の表。
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith(","):
            continue
        title = lines[i - 1].strip() if i > 0 else ""
        rows = []
        for row in lines[i + 1:]:
            head = row.split(",")[0].strip()
            if not (len(head) == 6 and head.isdigit()):
                break
            rows.append(row)
        if not rows or (section and section.lower() not in title.lower()):
            continue
        cols = [c.strip() for c in line.split(",")[1:]]
        df = pd.read_csv(io.StringIO("\n".join(rows)), header=None, names=["ym"] + cols, skipinitialspace=True)
        ym = pd.to_datetime(df.pop("ym").astype(str), format="%Y%m")
        df.index = pd.DatetimeIndex(ym.dt.to_period("M").dt.to_timestamp(how="end").dt.normalize(), name=None)
        df = df.apply(pd.to_numeric, errors="coerce")
        return df.where(df > -99).div(100.0)
    raise ValueError(f"月次の表が見つからない（{section}）")


def read_french_zip(blob: bytes, section: str | None = None) -> pd.DataFrame:
    z = zipfile.ZipFile(io.BytesIO(blob))
    return parse_french(z.read(z.namelist()[0]).decode("latin-1"), section)


def read_xlsx(blob: bytes) -> pd.DataFrame:
    """1 枚目の表を読む（1 行目が見出し。東証の上場銘柄一覧用。openpyxl が要らないように最小限）。"""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    z = zipfile.ZipFile(io.BytesIO(blob))
    strings = ["".join(t.text or "" for t in si.iter(ns + "t"))
               for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(ns + "si")]
    sheet = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]
    rows = []
    for r in ET.fromstring(z.read(sheet)).iter(ns + "row"):
        row = {}
        for c in r.findall(ns + "c"):
            v = c.find(ns + "v")
            if v is not None:
                row[re.match(r"[A-Z]+", c.get("r")).group(0)] = strings[int(v.text)] if c.get("t") == "s" else v.text
        rows.append(row)
    return pd.DataFrame(rows[1:]).rename(columns=rows[0])


# --------------------------------------------------------------------------- 参考: 1 時間ごと
def hourly_returns(bars: pd.DataFrame, dividends: pd.Series | None = None) -> pd.DataFrame:
    """1 銘柄の 1 時間足（index = その取引所の時刻、列 open / close）を、日 × 時間帯のリターンにする。

    列: 「夜」（前日の最後の足の終値 → 最初の足の始値。権利落ち日は配当を足し戻す）、各足の開始時刻（HH:MM）、
    「昼休み」（前の足が終わってから次の足が始まるまでに値が飛ぶ日本の 11:30 → 12:30）。
    """
    b = bars.dropna(subset=["open", "close"]).sort_index()
    day = pd.Series(b.index.date, index=b.index)
    first = day != day.shift(1)
    prev_close = b["close"].shift(1)
    gap = b["open"] / prev_close - 1
    label = pd.Series(b.index.strftime("%H:%M"), index=b.index)
    parts = [pd.DataFrame({"day": day, "label": label, "r": b["close"] / b["open"] - 1})]
    night = gap[first]
    if dividends is not None and len(dividends):
        div = pd.Series(day[first].map(dividends).to_numpy(), index=night.index).fillna(0.0)
        night = night + div / prev_close[first]
    parts.append(pd.DataFrame({"day": day[first], "label": "夜", "r": night}))
    lunch = (~first) & (label == "12:00")
    parts.append(pd.DataFrame({"day": day[lunch], "label": "昼休み", "r": gap[lunch]}))
    long = pd.concat(parts).dropna()
    long = long[long["r"].abs() < 0.20]                         # 分割などで飛んだ値は使わない
    return long.pivot_table(index="day", columns="label", values="r", aggfunc="first")


def profile_table(per_ticker: list[pd.DataFrame]) -> pd.DataFrame:
    """銘柄を平均した日ごとのリターンから、時間帯ごとの平均（1 日あたり）・t 値・日数。"""
    mean = pd.concat(per_ticker).groupby(level=0).mean()
    rows = {c: {"mean": mean[c].mean(), "t": t_value(mean[c]), "days": int(mean[c].notna().sum())} for c in mean}
    return pd.DataFrame(rows).T
