"""W5 公募増資の値決めの後。docs/wide.md 4 章 W5（結果を見る前に決めたもの）。

適時開示の題名から「発行価格・売出価格・処分価格の決定」の知らせを拾い、開示の日付の次の営業日の引けで買って
10 営業日後の引けで売る。事象ごとの値 = 株のリターン − 同じ期間の全銘柄の平均 − 0.10%。買った日の月ごとに平均する。

値段を読む・事象を買って持つ・月の系列にする部品（load_close・hold_events・span・event_stream など）は W6 も使う。
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd

from .. import stocks as S
from ..agents import paths as agent_paths
from ..agents.collector import ticker_of
from . import base

KEY = "W5"
HOLD = 10                                         # 買いの日から売りの日までの営業日の数
GAP = 10                                          # 同じ会社で、残した開示からこの営業日数のうちに出た 2 回目以降は使わない
PRICES = Path("stocks") / "jp_close.csv"          # data の下。日付 × 銘柄（7203.T の形）の調整後終値（配当込み）
WORDS = ("発行価格", "売出価格", "処分価格")        # どれかと「決定」が題名に入る
MUST = "決定"
NOT = ("訂正", "新株予約権", "社債", "投資口", "第三者割当", "ストック")   # どれか 1 つでも入れば使わない
DROPS = {"no_ticker": "値段の表に無い銘柄", "before": "開示が値段の表の始まりより前",
         "pending": "売りの日がまだ来ていない（値段の表の外）", "no_price": "買いか売りの日に値段が無い",
         "outside": "買った月が期間の外（終わっていない月など）"}
TOKYO = "Asia/Tokyo"


def jst(raw) -> pd.DatetimeIndex:
    """日時の文字列を、時差の無い日本の時刻にする（「2024-01-15 15:00:00」「2024-01-15T15:00」「2024-01-15」のどれでも）。
    読めない値は NaT。時差の付いた値は日本の時刻に直す（時差の違う値が混ざっていれば 1 つずつ）。
    pandas に書き方を当てさせると、最初の行と違う書き方の行が黙って NaT になるので、ISO8601 として読む。"""
    s = ["" if pd.isna(x) else str(x).strip() for x in raw]
    try:
        t = pd.DatetimeIndex(pd.to_datetime(s, format="ISO8601", errors="coerce"))
        return t.tz_convert(TOKYO).tz_localize(None) if t.tz is not None else t
    except (ValueError, TypeError):               # 時差のある値と無い値・違う時差が混ざっている
        one = [pd.to_datetime(x, format="ISO8601", errors="coerce") for x in s]
        return pd.DatetimeIndex([pd.NaT if pd.isna(x) else x.tz_convert(TOKYO).tz_localize(None) if x.tzinfo else x for x in one])


# --------------------------------------------------------------------------- 値段と、事象を買って持つ部品（W6 も使う）
def load_close(data: Path) -> pd.DataFrame:
    """490 銘柄の調整後終値。値の 1 つも無い日と、休場日なのに前日の値のまま入っている日（cfdbot.stocks.drop_closed_days）は落とす。
    数でない値（「.」など）・0 以下・無限の値段は、値段の無い日として扱う。時差の付いた日付は日本の日付にする。"""
    path = Path(data) / PRICES
    if not path.exists():
        raise base.NotReady(f"{path} が無い。先に ./cfd stocks --fetch")
    try:
        c = pd.read_csv(path, index_col=0)
    except Exception as e:                        # 空・壊れたファイル
        raise base.NotReady(f"{path} を読めない（{type(e).__name__}）。./cfd stocks --fetch で取り直す") from e
    idx = jst(c.index)
    if idx.isna().any():
        raise base.NotReady(f"{path} に日付として読めない行がある。./cfd stocks --fetch で取り直す")
    c.index = idx.normalize()
    c = c.apply(pd.to_numeric, errors="coerce")
    c = c.where(np.isfinite(c) & (c > 0)).sort_index().dropna(how="all")
    if c.empty or c.shape[1] == 0:
        raise base.NotReady(f"{path} に値段が 1 つも無い")
    if not c.index.is_unique:
        raise base.NotReady(f"{path} に同じ日付が 2 回ある。./cfd stocks --fetch で取り直す")
    return S.drop_closed_days(c, c)[1]


def hold_events(close: pd.DataFrame, ev: pd.DataFrame, hold: int, cost: float = base.COST_JP) -> tuple[pd.DataFrame, dict]:
    """事象（列 ticker・day。day は知らせの日付）ごとに、day より後の最初の営業日の引けで買い、hold 営業日後の引けで売る。

    株 = (買い, 売り] の日ごとのリターン（±30% にそろえる）を掛け合わせたもの。値段の無い日は 0%（次に値段のある日に、
    前の値段からの動きが入る）。全銘柄の平均 = 日ごとに、その日にリターンのある株の平均を、同じ日々で掛け合わせたもの。
    値 = 株 − 平均 − cost。返すのは使った事象（元の列 ＋ buy・sell・stock・market・gross・value）と、使わなかった数（理由ごと）。
    """
    idx, n = close.index, len(close.index)
    r = close.pct_change(fill_method=None).clip(-S.CLIP, S.CLIP)              # 両方の日に値段のある株だけ
    cum_m = np.log1p(r.mean(axis=1).fillna(0.0).to_numpy()).cumsum()           # 全銘柄の平均（その日にリターンのある株）
    own = close.ffill().pct_change(fill_method=None).clip(-S.CLIP, S.CLIP)    # 株のほう: 値段の無い日は 0%
    cum_s = np.log1p(own.fillna(0.0).to_numpy()).cumsum(axis=0)
    ev = ev.reset_index(drop=True)
    j = close.columns.get_indexer(ev["ticker"])
    day = pd.DatetimeIndex(pd.to_datetime(ev["day"])).normalize()
    b = idx.searchsorted(day, side="right")                                   # 知らせの日付より後の最初の営業日
    s = b + hold
    no_ticker = j < 0
    before = ~no_ticker & (day < idx[0])
    pending = ~no_ticker & ~before & (s > n - 1)
    rest = ~(no_ticker | before | pending)
    px = close.to_numpy(dtype=float)
    have = np.zeros(len(ev), dtype=bool)
    have[rest] = np.isfinite(px[b[rest], j[rest]]) & np.isfinite(px[s[rest], j[rest]])
    no_price = rest & ~have
    bb, ss, jj = b[have], s[have], j[have]
    stock = np.expm1(cum_s[ss, jj] - cum_s[bb, jj])
    market = np.expm1(cum_m[ss] - cum_m[bb])
    used = ev[have].assign(buy=idx[bb], sell=idx[ss], stock=stock, market=market, gross=stock - market,
                           value=stock - market - cost)
    drop = {"no_ticker": int(no_ticker.sum()), "before": int(before.sum()), "pending": int(pending.sum()),
            "no_price": int(no_price.sum())}
    return used.reset_index(drop=True), drop


def span_end(index: pd.DatetimeIndex, hold: int, unknown_day) -> pd.Timestamp:
    """最後の月（月末の日付）: 「まだ分からない最初の日」の月より前の月。まだ分からない最初の日は、
    データ（開示・一覧）の分からない最初の日と、売りの日が値段の表の外になる最初の買いの日の、早いほう。"""
    first_out = index[len(index) - hold] if len(index) > hold else index[0]
    first = min(pd.Timestamp(unknown_day), first_out)
    return first.to_period("M").to_timestamp() - pd.Timedelta(days=1)


def span(index: pd.DatetimeIndex, hold: int, first_day, unknown_day) -> tuple[pd.Timestamp, pd.Timestamp]:
    """d の最初と最後の月（月末の日付）。最初は first_day の月（値段の表の始まりの月より前なら、そちら）、最後は span_end。"""
    start = max(base.month_end(first_day), base.month_end(index[0]))
    end = span_end(index, hold, unknown_day)
    if end < start:
        raise base.NotReady(f"d を作れる月が無い（{start:%Y-%m} から {end:%Y-%m} まで。データの期間が短い）")
    return start, end


def event_stream(close: pd.DataFrame, ev: pd.DataFrame, hold: int, first_day, unknown_day) -> tuple[pd.Series, pd.Series, pd.DataFrame, dict]:
    """月の系列 d（コストの後）と gross（前）を、span の月だけ並べる（事象の無い月は 0）。返すのは d・gross・使った事象・使わなかった数。"""
    start, end = span(close.index, hold, first_day, unknown_day)
    used, drop = hold_events(close, ev, hold)
    m = used["buy"].map(base.month_end) if len(used) else pd.Series(dtype="datetime64[ns]")
    out = ((m < start) | (m > end)).to_numpy(dtype=bool)
    drop["outside"] = int(out.sum())
    used, m = used[~out], m[~out]
    d = base.fill_months(used.groupby(m)["value"].mean(), start, end)
    g = base.fill_months(used.groupby(m)["gross"].mean(), start, end)
    return d, g, used, drop


def drop_line(drop: dict) -> str:
    return "使わなかった事象: " + "、".join(f"{DROPS[k]} {v} 件" for k, v in drop.items())


# --------------------------------------------------------------------------- W5 の事象
def is_offering(title) -> bool:
    """題名に「発行価格」「売出価格」「処分価格」のどれかと「決定」が入り、除く言葉がどれも入らない（全角と半角をそろえて見る）。"""
    t = unicodedata.normalize("NFKC", str(title))
    return any(w in t for w in WORDS) and MUST in t and not any(w in t for w in NOT)


def first_only(ev: pd.DataFrame, index: pd.DatetimeIndex, gap: int = GAP) -> pd.DataFrame:
    """同じ会社で、残した開示から gap 営業日のうちに出た 2 回目以降を落とす（最初の 1 回だけ）。営業日は値段の表で数える。"""
    ev = ev.sort_values(["ticker", "when"], kind="stable")
    pos = index.searchsorted(pd.DatetimeIndex(ev["day"]), side="right")
    keep, last = [], {}
    for t, p in zip(ev["ticker"], pos):
        ok = t not in last or p - last[t] > gap
        keep.append(ok)
        if ok:
            last[t] = p
    return ev[np.array(keep, dtype=bool)]


def _ticker(code) -> str:
    """一覧の銘柄（7203.T の形。TDnet の 5 桁の会社コードなら頭 4 文字 ＋ .T）。"""
    s = str(code).strip()
    return s if s.endswith(".T") else ticker_of(s)


def _disclosures(data: Path) -> tuple[pd.DataFrame, Path, int]:
    """適時開示の一覧（data/agents/ir/disclosures.csv。無ければ data/agents/disclosures.csv）。列 time・ticker・title が要る。
    返すのは、日時の読める開示・ファイルの場所・日時の読めない行の数（使わない）。"""
    cands = (agent_paths(Path(data) / "agents")["disclosures"], Path(data) / "agents" / "disclosures.csv")
    path = next((p for p in cands if p.exists()), None)
    if path is None:
        raise base.NotReady(f"適時開示の一覧 {cands[0]} が無い。先に ./cfd agents collect")
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception as e:
        raise base.NotReady(f"{path} を読めない（{type(e).__name__}）") from e
    miss = [c for c in ("time", "ticker", "title") if c not in df.columns]
    if miss:
        raise base.NotReady(f"{path} に列が無い: {'、'.join(miss)}（id・time・ticker・company・title・url・xbrl のはず）")
    when = pd.Series(jst(df["time"]), index=df.index)            # 時差つきで書かれていたら、日本の時刻にそろえる
    df = df.assign(when=when, day=when.dt.normalize(), ticker=df["ticker"].map(_ticker))[when.notna()]
    if df.empty:
        raise base.NotReady(f"{path} に日付の読める開示が 1 つも無い")
    return df, path, int(when.isna().sum())


def _prepare(data: Path) -> dict:
    close = load_close(data)
    disc, path, bad = _disclosures(data)
    hit = disc[disc["title"].map(is_offering).astype(bool)]
    if hit.empty:
        raise base.NotReady(f"{path} に、題名が条件に合う開示が 1 つも無い（形が違うかもしれない）")
    mine = hit[hit["ticker"].isin(close.columns)]
    return {"close": close, "disc": disc, "path": path, "bad": bad, "hit": hit, "mine": mine, "ev": first_only(mine, close.index)}


def _bad_line(p: dict) -> str:
    return f"。日時の読めない行 {p['bad']} 件は使わない" if p["bad"] else ""


def compute(data: Path) -> base.Stream:
    p = _prepare(data)
    close, disc = p["close"], p["disc"]
    d, g, used, drop = event_stream(close, p["ev"], HOLD, disc["when"].min(), disc["when"].max())
    notes = [
        (f"期間 {d.index[0]:%Y-%m}〜{d.index[-1]:%Y-%m}（{len(d)} か月）。開示の一覧 {disc['when'].min():%Y-%m-%d}〜"
         f"{disc['when'].max():%Y-%m-%d}、値段の最後の日 {close.index[-1]:%Y-%m-%d}{_bad_line(p)}"),
        (f"題名が条件に合う開示 {len(p['hit'])} 件 → 490 銘柄のもの {len(p['mine'])} 件 → 同じ会社の {GAP} 営業日のうちの 2 回目以降を"
         f"除いて {len(p['ev'])} 件 → 使った {len(used)} 件"),
        drop_line(drop),
        f"買い = 開示の日付の次の営業日の引け、売り = {HOLD} 営業日後の引け。値 = 株 − 同じ期間の全銘柄の平均 − {base.COST_JP:.2%}",
    ]
    return base.Stream(KEY, d, events=len(used), gross=float(g.mean()), notes=notes)


def span_line(used: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> str:
    """点検用: d を作れる月と、その中で買う事象の数（リターンは出さない）。"""
    n = sum(start <= base.month_end(b) <= end for b in used["buy"])
    return f"d を作れる月 {start:%Y-%m}〜{end:%Y-%m}（{len(pd.period_range(start, end, freq='M'))} か月）。その中で買う事象 {n} 件"


def check(data: Path) -> base.Check:
    try:
        p = _prepare(data)
        start, end = span(p["close"].index, HOLD, p["disc"]["when"].min(), p["disc"]["when"].max())
    except base.NotReady as e:
        return base.Check(KEY, False, [f"値段 {Path(data) / PRICES}・適時開示の一覧 {Path(data) / 'agents' / 'ir' / 'disclosures.csv'}"],
                          why=str(e))
    close, disc, hit = p["close"], p["disc"], p["hit"]
    used, drop = hold_events(close, p["ev"], HOLD)
    lines = [
        f"開示の一覧 {p['path']}（{len(disc)} 件、{disc['when'].min():%Y-%m-%d}〜{disc['when'].max():%Y-%m-%d}）{_bad_line(p)}",
        f"値段 {Path(data) / PRICES}（{close.shape[1]} 銘柄、{close.index[0]:%Y-%m-%d}〜{close.index[-1]:%Y-%m-%d}）",
        f"題名が条件に合う開示 {len(hit)} 件（{hit['when'].min():%Y-%m-%d}〜{hit['when'].max():%Y-%m-%d}）",
        f"うち 490 銘柄のもの {len(p['mine'])} 件 → 同じ会社の {GAP} 営業日のうちの 2 回目以降を除いて {len(p['ev'])} 件",
        f"買いと売りの日に値段のある事象 {len(used)} 件。{drop_line(drop)}",
        span_line(used, start, end),
    ]
    return base.Check(KEY, True, lines)
