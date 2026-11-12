"""W7 金利の高い通貨（為替のキャリー）。docs/wide.md 4 章。

主要な 9 通貨（対ドル）を、月末 t に「その通貨の t 月の 3 か月金利 − 米国の t 月の 3 か月金利」の高い順に並べ、
上位 3 つを買い・下位 3 つを売って翌月持つ（等分。月 1 回入れ替え）。データは FRED（セントルイス連銀）の CSV。
"""

from __future__ import annotations

import time
import urllib.request
from itertools import pairwise
from pathlib import Path

import pandas as pd

from . import base

KEY = "W7"
# 通貨 → (為替の記号, 逆数にするか)。「1 ドル = いくら」の形（CAD・CHF・JPY・NOK・SEK）は逆数にして「1 単位 = 何ドル」にそろえる
FX = {"AUD": ("DEXUSAL", False), "CAD": ("DEXCAUS", True), "CHF": ("DEXSZUS", True), "EUR": ("DEXUSEU", False),
      "GBP": ("DEXUSUK", False), "JPY": ("DEXJPUS", True), "NOK": ("DEXNOUS", True), "NZD": ("DEXUSNZ", False),
      "SEK": ("DEXSDUS", True)}
RATE = {c: f"IR3TIB01{cc}M156N" for c, cc in zip(FX, ("AU", "CA", "CH", "EZ", "GB", "JP", "NO", "NZ", "SE"))}   # 3 か月金利（年率 %）
US = "IR3TIB01USM156N"
SERIES = [sid for sid, _ in FX.values()] + list(RATE.values()) + [US]
URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
DIR = Path("wide") / "fred"           # data の下。記号ごとに 1 つの CSV（取ったまま）
N_SIDE, MIN_CCY = 3, 8                # 買い・売りの数。為替と金利の両方がある通貨がこれに届かない月は作らない
COST = 0.0005                         # 入れ替えた通貨の割合に掛ける
START = "1999-01-31"                  # d の最初の月（ユーロの始まり）
LAST_BY = "2024-12-31"                # d の最後の月がこれより前なら、データが古いので計算しない
INTERVAL = 1.0                        # FRED への問い合わせの間（秒）。1 秒に 1 件まで・同時に 1 本


# --------------------------------------------------------------------------- 取得（Mac で）
def _get(url: str, timeout: float = 60.0) -> bytes:
    # ブラウザを名乗ると門前払いになることがある（cfdbot/context.py の fetch_fred と同じく名乗らない）
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "text/csv,*/*"}), timeout=timeout) as r:
        return r.read()


_sleep = time.sleep


def fetch(data: Path, log=print) -> None:
    """19 の系列を 1 つずつ取り、data/wide/fred/<記号>.csv に取ったまま書く。取れなかった系列があれば最後に RuntimeError。"""
    out = data / DIR
    out.mkdir(parents=True, exist_ok=True)
    failed, first = [], True
    for sid in SERIES:
        raw, err = None, ""
        for k in range(3):
            if not first:
                _sleep(INTERVAL if k == 0 else 10.0 * k)     # 問い合わせの間をあける（やり直しはもっと長く）
            first = False
            try:
                raw = _get(URL.format(sid))
                head = raw[:200].decode("utf-8", errors="replace").splitlines()[0] if raw else ""
                if sid not in head:
                    raise ValueError("CSV の見出しに記号が無い（エラーのページかもしれない）")
                break
            except Exception as e:  # noqa: BLE001  回線・相手の不調は種類を問わずやり直す
                raw, err = None, f"{type(e).__name__}: {str(e)[:80]}"
        if raw is None:
            failed.append(sid)
            log(f"  W7: {sid} が取れない（{err}）")
            continue
        tmp = out / f"{sid}.tmp"
        tmp.write_bytes(raw)
        tmp.replace(out / f"{sid}.csv")
        log(f"  W7: {sid}（{len(raw.splitlines()) - 1} 行）")
    if failed:
        raise RuntimeError(f"FRED から取れなかった系列: {'、'.join(failed)}。時間をおいて ./cfd wide --fetch")


# --------------------------------------------------------------------------- 読み取りと形の点検
def _read(data: Path, sid: str) -> pd.Series:
    """FRED の CSV（列 DATE か observation_date と記号。欠けは「.」）を、日付で並んだ値にする。"""
    path = data / DIR / f"{sid}.csv"
    if not path.exists():
        raise base.NotReady(f"{sid} が無い（{path}）。先に ./cfd wide --fetch")
    try:
        df = pd.read_csv(path, dtype=str)
    except Exception as e:
        raise base.NotReady(f"{path} を読めない（{type(e).__name__}）") from e
    date = next((c for c in df.columns if str(c).strip().lower() in ("date", "observation_date")), None)
    if date is None or sid not in df.columns:
        raise base.NotReady(f"{sid} の形が違う（列 {list(df.columns)[:4]}。DATE か observation_date と {sid} のはず）")
    raw = df[sid].fillna(".").astype(str).str.strip()
    val = pd.to_numeric(raw.where(raw != "."), errors="coerce")
    bad = int((val.isna() & (raw != ".")).sum())
    when = pd.to_datetime(df[date], errors="coerce")
    if bad or when.isna().any():
        raise base.NotReady(f"{sid} に数でない値 {bad} 行・日付でない行 {int(when.isna().sum())} 行がある（取り直す）")
    s = pd.Series(val.to_numpy(dtype=float), index=pd.DatetimeIndex(when), name=sid).dropna().sort_index()
    if s.empty:
        raise base.NotReady(f"{sid} に値が 1 つも無い")
    if not s.index.is_unique:
        raise base.NotReady(f"{sid} に同じ日付が 2 回ある")
    return s


def _fx_month(s: pd.Series, invert: bool) -> pd.Series:
    """日ごとの為替から月末の最後の値（その通貨 1 単位 = 何ドル）。最後の日の月は、途中かもしれないので使わない。"""
    if (s <= 0).any():
        raise base.NotReady(f"{s.name} に 0 以下の値がある")
    v = 1.0 / s if invert else s
    m = v.groupby(v.index.to_period("M")).last()
    m = m[m.index < v.index[-1].to_period("M")]
    m.index = m.index.to_timestamp(how="end").normalize()
    return m


def _rate_month(s: pd.Series) -> pd.Series:
    """月ごとの金利（年率 %）を、その月の月末の日付に並べる。"""
    per = s.index.to_period("M")
    if not per.is_unique:
        raise base.NotReady(f"{s.name} が月ごとの系列でない（同じ月が 2 回ある）")
    if (s.abs() >= 100).any():
        raise base.NotReady(f"{s.name} に年率 % に見えない値がある")
    return pd.Series(s.to_numpy(), index=per.to_timestamp(how="end").normalize(), name=s.name)


def _tables(data: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp, dict]:
    """月末ごとの為替（1 単位 = 何ドル）と金利の差（その通貨 − 米国、年率 %）。月は続けて並べる。"""
    missing = [sid for sid in SERIES if not (data / DIR / f"{sid}.csv").exists()]
    if missing:
        raise base.NotReady(f"無い系列 {len(missing)} 個: {'、'.join(missing)}。先に ./cfd wide --fetch")
    raw = {sid: _read(data, sid) for sid in SERIES}
    fx = pd.DataFrame({c: _fx_month(raw[sid], inv) for c, (sid, inv) in FX.items()})
    rate = pd.DataFrame({c: _rate_month(raw[RATE[c]]) for c in FX})
    us = _rate_month(raw[US])
    lo = min(fx.index.min(), rate.index.min(), us.index.min())
    hi = max(fx.index.max(), rate.index.max(), us.index.max())
    idx = pd.period_range(lo, hi, freq="M").to_timestamp(how="end").normalize()
    fx, rate, us = fx.reindex(idx), rate.reindex(idx), us.reindex(idx)
    last_day = max(raw[sid].index[-1] for sid, _ in FX.values())
    span = {sid: (s.index[0], s.index[-1]) for sid, s in raw.items()}
    return fx, rate.sub(us, axis=0), last_day, span


# --------------------------------------------------------------------------- 計算
def _plans(fx: pd.DataFrame, diff: pd.DataFrame) -> dict:
    """月末 t ごとに（買う 3 つ, 売る 3 つ, 通貨の数）。為替と金利の両方がある通貨が 8 つに届かない月は作らない。同じ値は記号の順。"""
    plans = {}
    for t in fx.index:
        ok = fx.loc[t].notna() & diff.loc[t].notna()
        if int(ok.sum()) < MIN_CCY:
            continue
        order = sorted(ok.index[ok], key=lambda c: (-float(diff.at[t, c]), c))
        plans[t] = (order[:N_SIDE], order[-N_SIDE:], len(order))
    return plans


def _build(fx: pd.DataFrame, diff: pd.DataFrame, start: str = START) -> tuple[pd.DataFrame, dict]:
    """得た月（t+1）ごとのコスト前の値 gross と入れ替えた割合 turn。fx・diff は続いた月末で並んでいること。"""
    plans = _plans(fx, diff)
    rows, prev, no_fx = [], None, 0
    months = list(fx.index)
    for t, nxt in pairwise(months):
        if nxt < pd.Timestamp(start):
            continue
        plan = plans.get(t)
        if plan is None:                                       # 作らない月（前の月の持ち物は無い扱い）
            prev = None
            continue
        long, short, n_ccy = plan
        x = fx.loc[nxt] / fx.loc[t] - 1 + diff.loc[t] / 100 / 12   # その通貨を持ったときの翌月のリターン（ドルから見て）
        if x[long + short].isna().any():                       # 持った通貨の翌月の為替が無い
            no_fx += 1
            prev = plan
            continue
        gross = (x[long].mean() - x[short].mean()) / 2          # 買い 3 つの平均 − 売り 3 つの平均（売りはその逆を得る）を半分ずつ
        turn = 1.0 if prev is None else (len(set(long) - set(prev[0])) + len(set(short) - set(prev[1]))) / (2 * N_SIDE)
        rows.append((nxt, float(gross), turn, n_ccy))
        prev = plan
    out = pd.DataFrame(rows, columns=["month", "gross", "turn", "n_ccy"]).set_index("month")
    out.index = pd.DatetimeIndex(out.index)
    return out, {"no_fx": no_fx}


def _stop(d_last) -> str:
    return (f"d の最後の月が {d_last:%Y-%m} で、2024-12 より前に終わる（データが古いので計算しない。docs/wide.md 4 章 W7）"
            if d_last is not None else "d を作れる月が 1 つも無い（データが古いので計算しない。docs/wide.md 4 章 W7）")


def compute(data: Path) -> base.Stream:
    fx, diff, last_day, _ = _tables(data)
    out, info = _build(fx, diff)
    out = out.loc[base.finished_months(out["gross"], last_day).index]
    if out.empty or out.index[-1] < pd.Timestamp(LAST_BY):
        raise base.NotReady(_stop(out.index[-1] if len(out) else None))
    d = out["gross"] - out["turn"] * COST
    hole = len(pd.period_range(d.index[0], d.index[-1], freq="M")) - len(d)
    notes = [
        f"期間 {d.index[0]:%Y-%m}〜{d.index[-1]:%Y-%m}（{len(d)} か月）。データの最後の日 {last_day:%Y-%m-%d}",
        (f"途中で抜けた月 {hole} か月（為替と金利の両方がある通貨が 8 つに届かない月は作らない。0 にもしない）。"
         f"持った通貨の翌月の為替が無くて使わなかった月 {info['no_fx']} か月。通貨が 8 つだけの月 {int((out['n_ccy'] == MIN_CCY).sum())} か月"),
        f"買い・売り各 {N_SIDE} 通貨（等分）。d =（買いの平均 − 売りの平均）÷ 2 − 入れ替えた割合 × {COST:.2%}",
    ]
    return base.Stream(KEY, d, events=None, gross=float(out["gross"].mean()), notes=notes)


def check(data: Path) -> base.Check:
    try:
        fx, diff, last_day, span = _tables(data)
    except base.NotReady as e:
        return base.Check(KEY, False, [f"フォルダ {data / DIR}（{len(SERIES)} 系列のはず）"], why=str(e))
    lines = [f"フォルダ {data / DIR}: {len(SERIES)} 系列そろっている"]
    lines += [f"{sid}: {a:%Y-%m-%d}〜{b:%Y-%m-%d}" for sid, (a, b) in span.items()]
    both = (fx.notna() & diff.notna())
    both = both[both.index >= pd.Timestamp(START) - pd.offsets.MonthEnd(1)]
    n_ok = both.sum(axis=1) >= MIN_CCY
    lines.append(f"為替と金利の両方がある通貨が 8 つ以上の月末: {int(n_ok.sum())} か月"
                 + (f"（{n_ok.index[n_ok][0]:%Y-%m}〜{n_ok.index[n_ok][-1]:%Y-%m}）" if n_ok.any() else ""))
    last_both = {c: both.index[both[c]][-1] for c in both.columns if both[c].any()}
    lines.append("通貨ごとの両方がある最後の月: " + "、".join(f"{c} {last_both[c]:%Y-%m}" if c in last_both else f"{c} なし" for c in FX))
    out, _ = _build(fx, diff)
    out = out.loc[base.finished_months(out["gross"], last_day).index]
    d_last = out.index[-1] if len(out) else None
    if d_last is None or d_last < pd.Timestamp(LAST_BY):
        return base.Check(KEY, False, lines, why=_stop(d_last))
    lines.append(f"d を作れる月: {out.index[0]:%Y-%m}〜{d_last:%Y-%m}（{len(out)} か月。最後の月が 2024-12 以降なので計算できる）")
    return base.Check(KEY, True, lines)
