"""W2 FOMC の日（米国の金利の会合の声明の日）。docs/wide.md 4 章 W2・6 章（結果を見る前に決めたもの）。

予定されていた会合の声明の日（config/fomc_days.csv）に、S&P500（^GSPC）が前の営業日の引け → 声明の日の引けで、
ふだんの日（声明の前の日までの直近 250 営業日の 1 日の平均）より上がるかを見る。事象ごとに往復 0.30% を引き、月ごとに足す。

日付の表は指令役が記憶から書いたので、連邦準備制度の公式のページと見比べるまで使わない:
fetch で公式のページを保存 → verify_dates で日付（20060131 の形）がページにあるかを見る → 自動で合わなければ、
人が見比べて mark_checked で印を付ける（docs/wide.md 6 章の 4）。
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from . import base

KEY, TICKER = "W2", "^GSPC"
START = "2000-01-01"                  # d はこの月から（docs/wide.md 4 章 W2）
WINDOW = 250                          # ふだんの日 = 声明の前の日までの直近 250 営業日の 1 日の平均リターン
COST = base.COST_US                   # 米国 往復 0.30%
FED = "https://www.federalreserve.gov/monetarypolicy/"
YEARS = range(2000, 2021)             # 年ごとの過去のページ（fomchistorical<年>.htm）。2021 年からは日程のページで見る
CALENDAR = "fomccalendars.htm"
INTERVAL = 1.0                        # 1 秒に 1 件・同時に 1 本（取り始めてから次を取り始めるまでの秒数）
UA = "Mozilla/5.0 (Macintosh)"


def _days_file(data: Path) -> Path:
    return Path(data).parent / "config" / "fomc_days.csv"      # data はリポジトリの data フォルダ。表は隣の config


def _pages(data: Path) -> Path:
    return Path(data) / "wide" / "fomc"


def _mark_file(data: Path) -> Path:
    return Path(data) / "wide" / "fomc_checked.json"


def _page_of(year: int) -> str:
    return f"fomchistorical{year}.htm" if year <= YEARS[-1] else CALENDAR


def _list(xs, k: int = 10) -> str:
    xs = list(xs)
    return "、".join(xs[:k]) + (f" ほか {len(xs) - k}" if len(xs) > k else "")


# --------------------------------------------------------------------------- 読み取りと形の点検
def read_days(data: Path) -> pd.DatetimeIndex:
    """日付の表（# で始まる行は説明、date の列）。無い・読めない・同じ日が 2 回あるなら NotReady。"""
    p = _days_file(data)
    if not p.exists():
        raise base.NotReady(f"FOMC の日付の表 {p} が無い")
    try:
        t = pd.read_csv(p, comment="#", dtype=str)
    except Exception as e:  # 空・壊れた表
        raise base.NotReady(f"FOMC の日付の表 {p} を読めない（{type(e).__name__}）") from e
    if "date" not in t.columns:
        raise base.NotReady(f"FOMC の日付の表に date の列が無い（列: {list(t.columns)}）")
    s = t["date"].fillna("").str.strip()
    days = pd.to_datetime(s, format="%Y-%m-%d", errors="coerce")
    if days.isna().any():
        raise base.NotReady(f"FOMC の日付の表に、日付（2006-01-31 の形）として読めない行がある（{_list(s[days.isna()].map(repr), 5)}）")
    if days.empty:
        raise base.NotReady("FOMC の日付の表が空")
    if days.duplicated().any():
        raise base.NotReady(f"FOMC の日付の表に同じ日が 2 回ある（{_list(f'{d:%Y-%m-%d}' for d in days[days.duplicated()])}）")
    return pd.DatetimeIndex(days).sort_values()


def read_prices(data: Path) -> pd.Series:
    """S&P500（^GSPC）の終値。値のある日を営業日とみなす。無い・形が違うなら NotReady。"""
    p = Path(data) / "stocks" / "misc_close.csv"
    if not p.exists():
        raise base.NotReady(f"S&P500 の値段 {p} が無い（./cfd stocks --fetch）")
    try:
        t = pd.read_csv(p, index_col=0)
    except Exception as e:
        raise base.NotReady(f"S&P500 の値段 {p} を読めない（{type(e).__name__}）") from e
    if TICKER not in t.columns:
        raise base.NotReady(f"{p} に {TICKER} の列が無い（./cfd stocks --fetch）")
    # 時刻や時差（冬 -05:00・夏 -04:00 が混ざる）が付いていても、その市場の日付（先頭の 10 文字）で見る
    idx = pd.to_datetime(pd.Index(t.index.astype(str)).str[:10], format="%Y-%m-%d", errors="coerce")
    if idx.isna().any() or not pd.api.types.is_numeric_dtype(t[TICKER]):
        raise base.NotReady(f"{p} の日付か {TICKER} の値に、読めないものがある")
    c = pd.Series(t[TICKER].to_numpy(dtype=float), index=idx.normalize()).dropna().sort_index()
    if c.empty or not c.index.is_unique or (c <= 0).any():
        raise base.NotReady(f"{p} の {TICKER} が空・同じ日が 2 回ある・0 以下の値がある、のどれか")
    if (c.index < pd.Timestamp(START)).sum() < WINDOW + 1:
        raise base.NotReady(f"{TICKER} の値段が {START[:7]} より前に {WINDOW + 1} 営業日以上ない"
                            f"（{c.index[0]:%Y-%m-%d} から。最初の月のふだんの日が測れない）")
    if c.index[-1] < pd.Timestamp(START) + pd.offsets.MonthBegin(1):
        raise base.NotReady(f"{TICKER} の値段が {START[:7]} の後の終わった月まで無い（最後 {c.index[-1]:%Y-%m-%d}）")
    return c


def _states(days: pd.DatetimeIndex, close: pd.Series) -> pd.Series:
    """表の日ごとの扱い: before（2000-01 より前）・future（値段の最後の日より後）・holiday（値段の表で営業日でない）・
    unfinished（終わっていない月）・used（使う）。"""
    last = close.index[-1]
    cut = last.to_period("M").to_timestamp()                     # base.finished_months と同じ区切り
    conds = [days < pd.Timestamp(START), days > last, ~days.isin(close.index), days >= cut]
    return pd.Series(np.select(conds, ["before", "future", "holiday", "unfinished"], "used"), index=days)


def _last_month(close: pd.Series) -> pd.Timestamp:
    return base.month_end(close.index[-1].to_period("M").to_timestamp() - pd.Timedelta(days=1))


# --------------------------------------------------------------------------- 公式のページとの見比べ
def _get(url: str, tries: int = 3) -> bytes:
    for k in range(tries):
        try:
            with urlopen(Request(url, headers={"User-Agent": UA}), timeout=60) as r:
                return r.read()
        except HTTPError as e:
            if 400 <= e.code < 500 or k == tries - 1:              # 404 などは取り直しても同じ
                raise
        except Exception:  # 回線の不調
            if k == tries - 1:
                raise
        time.sleep(5 * (k + 1))
    raise RuntimeError(url)


def fetch(data: Path, log=print) -> None:
    """公式のページ（年ごとの過去のページ 2000〜2020 年と、日程のページ）を data/wide/fomc/ にそのまま保存する。

    1 秒に 1 件・同時に 1 本。取れないページ（404 など）があっても止めずに fetch.json に書く。
    年ごとのページは変わらないので、保存済みなら取り直さない（日程のページは毎回取り直す）。
    """
    folder = _pages(data)
    folder.mkdir(parents=True, exist_ok=True)
    rec: dict = {"date": f"{date.today():%Y-%m-%d}", "saved": [], "failed": {}}
    done = None                         # 前のページを取り終えた時（_get の中の取り直しの後でも、次まで 1 秒あける）
    for name in [_page_of(y) for y in YEARS] + [CALENDAR]:
        path = folder / name
        if name != CALENDAR and path.exists() and path.stat().st_size > 0:
            rec["saved"].append(name)
            continue
        if done is not None and (wait := INTERVAL - (time.monotonic() - done)) > 0:
            time.sleep(wait)
        try:
            path.write_bytes(_get(FED + name))
            rec["saved"].append(name)
        except Exception as e:  # noqa: BLE001  404・回線の不調。記録して次へ
            rec["failed"][name] = f"HTTP {e.code}" if isinstance(e, HTTPError) else type(e).__name__
            log(f"  FOMC の公式のページ {name} が取れない（{rec['failed'][name]}）")
        done = time.monotonic()
    (folder / "fetch.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"FOMC の公式のページ: 保存 {len(rec['saved'])}、取れない {len(rec['failed'])}")
    try:
        v = verify_dates(data)
    except base.NotReady as e:
        log(f"  日付の見比べはできない（{e}）")
        return
    log(f"  日付の見比べ: 表の {len(v['found'])} 日のうち、公式のページにあった {sum(v['found'].values())} 日"
        + (f"。まだ声明の無い日 {len(v['later'])}" if v["later"] else "")
        + (f"。見つからない日: {_list(v['missing'])}" if v["missing"] else ""))


def verify_dates(data: Path) -> dict:
    """表の日ごとに、YYYYMMDD（例 20060131）が保存した公式のページにあるかを見る。

    2020 年までは年ごとの過去のページ、2021 年からは日程のページ。そのページが無いか見つからないときは、
    もう一方（日程のページ・その年のページ）が保存してあれば、そちらも見る。
    日程のページを保存した日の前の日から後の会合は、まだ声明が出ていない（ページに日付の入ったリンクが無い）ので、
    見つからなくても missing でなく later に入れる（計算に入る日なら、check・compute が止める）。
    返す dict: found（日付 → あったか）・missing（無かった日）・later（まだ声明の無い日）・
    pages_missing（要るのに保存していないページ）・ok（missing が無い）。
    """
    days = read_days(data)
    folder, texts = _pages(data), {}

    def text(name: str) -> str | None:
        if name not in texts:
            p = folder / name
            texts[name] = p.read_bytes().decode("utf-8", "replace") if p.exists() else None
        return texts[name]

    found, need = {}, set()
    for d in days:
        main = _page_of(d.year)
        other = CALENDAR if main != CALENDAR else f"fomchistorical{d.year}.htm"
        if text(main) is None:
            need.add(main)
        found[f"{d:%Y-%m-%d}"] = any(t is not None and f"{d:%Y%m%d}" in t for t in (text(main), text(other)))
    cal = folder / CALENDAR
    cut = pd.Timestamp(date.fromtimestamp(cal.stat().st_mtime)) - pd.Timedelta(days=1) if cal.exists() else None
    later = [k for k, ok in found.items() if not ok and cut is not None and pd.Timestamp(k) >= cut]
    missing = [k for k, ok in found.items() if not ok and k not in later]
    return {"found": found, "missing": missing, "later": later, "pages_missing": sorted(need), "ok": bool(found) and not missing}


def mark_checked(data: Path, note: str) -> Path:
    """人が公式のページと見比べた印（data/wide/fomc_checked.json に {date, note}）。note は何と見比べたか。"""
    note = str(note).strip()
    if not note:
        raise ValueError("何と見比べたかを書く（例: federalreserve.gov の FOMC の日程と年ごとの過去のページ）")
    p = _mark_file(data)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": f"{date.today():%Y-%m-%d}", "note": note}, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def _late(v: dict, used: pd.DatetimeIndex) -> list[str]:
    """まだ声明の無かった日（later）のうち、計算に入る日。日程のページが古い。"""
    return [k for k in v["later"] if pd.Timestamp(k) in used]


def _checked(data: Path, v: dict, used: pd.DatetimeIndex) -> str | None:
    """日付を確かめた方法（文）。自動で全部あった、または手で見比べた印がある。どちらでもなければ None。"""
    if v["ok"] and not _late(v, used):
        rest = f"。日程のページを取った日より後の {len(v['later'])} 日は、まだ声明が無いので見ていない" if v["later"] else ""
        return f"日付は公式のページと自動で見比べた（{len(v['found']) - len(v['later'])} 日すべてあった{rest}）"
    p = _mark_file(data)
    if not p.exists():
        return None
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
        how = f"{m.get('date', '?')}: {m.get('note', '?')}"
    except Exception:  # noqa: BLE001  壊れた印も、印があるとみなす
        how = "印のファイルが読めない"
    return f"日付は手で公式のページと見比べた印がある（{how}）。自動では {len(v['missing'])} 日見つからなかった"


def _not_checked(data: Path, v: dict, used: pd.DatetimeIndex) -> str:
    saved = [p for p in [_page_of(y) for y in YEARS] + [CALENDAR] if (_pages(data) / p).exists()]
    if not saved:
        return "FOMC の公式のページがまだ保存されていない。./cfd wide --fetch で取ってから、もう一度 ./cfd wide --check"
    late = _late(v, used)
    return ("FOMC の日付の表がまだ公式のページで確かめられていない。"
            + (f"保存したページに見つからない日 {len(v['missing'])}: {_list(v['missing'])}。" if v["missing"] else "")
            + (f"日程のページを取った日より後で、計算に入る日 {len(late)}: {_list(late)}（./cfd wide --fetch で取り直す）。" if late else "")
            + (f"保存していないページ: {_list(v['pages_missing'])}。" if v["pages_missing"] else "")
            + "federalreserve.gov の FOMC の日程と年ごとの過去のページを開いて config/fomc_days.csv と見比べ（違えば公式に合わせて直し、"
            "docs/wide.md 10 章に書く）、合っていれば ./cfd wide --fomc-checked \"<何と見比べたか>\"")


# --------------------------------------------------------------------------- 点検と計算
def check(data: Path) -> base.Check:
    """手元のファイルだけを見る（通信しない）。期間・件数・日付の確かめだけで、リターンの数字は出さない。"""
    c, why = base.Check(KEY, False), []
    try:
        days = read_days(data)
    except base.NotReady as e:
        c.why = str(e)
        return c
    c.lines.append(f"FOMC の声明の日の表: {len(days)} 日（{days[0]:%Y-%m-%d}〜{days[-1]:%Y-%m-%d}）")
    used = pd.DatetimeIndex([])
    try:
        close = read_prices(data)
    except base.NotReady as e:
        why.append(str(e))
    else:
        st = _states(days, close)
        used = st.index[st == "used"]
        trading, future = int(days.isin(close.index).sum()), int((st == "future").sum())
        off = [f"{d:%Y-%m-%d}" for d in days[~days.isin(close.index) & (days <= close.index[-1])]]
        c.lines += [
            f"S&P500（{TICKER}）の終値: {close.index[0]:%Y-%m-%d}〜{close.index[-1]:%Y-%m-%d}（{len(close)} 日）",
            f"表の日のうち、値段の表で営業日 {trading}・営業日でない {len(off)}・値段の最後の日より後 {future}"
            + (f"（営業日でない日: {_list(off)}。使わない）" if off else ""),
            (f"使う事象 {int((st == 'used').sum())}（d は {START[:7]}〜{_last_month(close):%Y-%m}。"
             f"終わっていない月の日 {int((st == 'unfinished').sum())}・{START[:7]} より前の日 {int((st == 'before').sum())} は入れない）"),
        ]
    v = verify_dates(data)
    how = _checked(data, v, used)
    c.lines.append(f"公式のページとの見比べ: {how}" if how else
                   f"公式のページとの見比べ: まだ（見つからない日 {len(v['missing'])}・計算に入るのにまだ声明の無かった日 {len(_late(v, used))}）")
    if how is None:
        why.insert(0, _not_checked(data, v, used))
    c.ok, c.why = not why, " ／ ".join(why)
    return c


def compute(data: Path) -> base.Stream:
    """月の系列 d = その月の事象の値の合計（会合の無い月は 0）。事象の値 = 声明の日のリターン − ふだんの日 − 0.30%。"""
    days, close = read_days(data), read_prices(data)
    st = _states(days, close)
    used = st.index[st == "used"]
    v = verify_dates(data)
    how = _checked(data, v, used)
    if how is None:
        raise base.NotReady(_not_checked(data, v, used))
    r = close.pct_change()
    usual = r.rolling(WINDOW).mean().shift(1)                     # 声明の前の日までの 250 営業日（その日のリターンは入れない）
    gross = (r - usual).reindex(used)
    if gross.isna().any():
        raise base.NotReady("W2 の事象の値が計算できない日がある（値段の形が違う）")
    end = _last_month(close)

    def monthly(x: pd.Series) -> pd.Series:
        return base.fill_months(base.finished_months(x.groupby(x.index.map(base.month_end)).sum(), close.index[-1]), START, end)

    d = monthly((gross - COST).astype(float))
    off = st.index[st == "holiday"]
    notes = [
        (f"S&P500（{TICKER}）の終値 {close.index[0]:%Y-%m-%d}〜{close.index[-1]:%Y-%m-%d}。"
         f"FOMC の声明の日の表 {len(days)} 日（{days[0]:%Y-%m-%d}〜{days[-1]:%Y-%m-%d}）。{how}"),
        f"d は {START[:7]}〜{end:%Y-%m}（{len(d)} か月）。使った事象 {len(used)}。事象ごとに往復 {COST:.2%} を引いた",
    ]
    if len(off):
        notes.append(f"値段の表で営業日でない日 {len(off)} は使わない（{_list(f'{x:%Y-%m-%d}' for x in off)}）")
    for state, what in (("unfinished", "終わっていない月の日"), ("future", "値段の最後の日より後の日（まだ来ていない）"),
                        ("before", f"{START[:7]} より前の日")):
        if (k := int((st == state).sum())):
            notes.append(f"{what} {k} は入れない")
    return base.Stream(KEY, d, events=len(used), gross=float(monthly(gross.astype(float)).mean()), notes=notes)
