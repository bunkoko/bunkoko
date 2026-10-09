"""ニュースの収集・保存と、日ごとの特徴・仮説の検証（./cfd news。設定と判定の条件は docs/news.md）。

取得先:
- GDELT（世界のニュースを 15 分ごとに集めて無料で公開している）の DOC API。「5 秒に 1 回まで」なので 6 秒ずつあける
  - 記事の一覧（見出し・URL・GDELT が見つけた時刻）。さかのぼれるのは直近 3 か月ほど
  - キーワードに当てはまる記事の量（全記事に対する割合）と論調の推移
- Yahoo Finance の銘柄ごとのニュース（直近 1 日ほどの 50 件。さかのぼれないので、集め始めた日から）

先読みの防止: 見出しは「使えるようになった時刻」（GDELT は見つけた時刻 + 30 分、Yahoo は初めて取れた時刻）で
UTC の日に分ける。UTC の D 日の分は D 日が終わってから（翌日 0:00 UTC 以降の最初の判断から）使う。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import pandas as pd

GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_WAIT = 6.0                                # 1 件ごとにあける秒数（GDELT の決まりは 5 秒に 1 回まで）
GDELT_BACKOFF = (30.0, 60.0, 120.0, 240.0)      # 断られたときに待ってやり直す秒数
GDELT_DELAY = pd.Timedelta(minutes=30)          # GDELT が見つけてから一覧に載るまでの余裕
MAX_RECORDS = 250                               # 記事の一覧の 1 回の上限
BLOCK = pd.Timedelta(hours=6)                   # さかのぼって取るときの 1 回の幅（6 時間ごとに最新の 250 件）
YAHOO_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
UA = "Mozilla/5.0"     # Yahoo は長いブラウザの名乗りだと「アクセス過多」で断ることがある
TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Query:
    key: str
    text: str     # GDELT の検索式（OR はかっこの中に書く）
    label: str


# キーワードで数える量と論調（docs/news.md 2 章。AI は使わない）
TIMELINE_QUERIES: tuple[Query, ...] = (
    Query("oil_macro", "(oil OR crude OR opec) (demand OR supply OR inventories OR economy)", "原油の需給・景気"),
    Query("geo", '(war OR missile OR sanctions OR attack) (oil OR "middle east" OR russia)', "地政学"),
    Query("gold", 'gold ("safe haven" OR bullion OR inflation)', "金"),
)
TIMELINE_MODES = ("timelinevolraw", "timelinetone")
# 見出しを集める検索（埋め込みで話題を分けるので広めに取る。docs/news.md 6 章）
ARTICLE_QUERIES: tuple[Query, ...] = (
    Query("oil", "(oil OR crude OR opec OR brent)", "原油"),
    Query("gold", "(gold OR silver OR bullion)", "金・銀"),
    Query("geo", "(war OR missile OR sanctions OR attack OR military OR conflict)", "地政学"),
    Query("macro", '("federal reserve" OR inflation OR "interest rates" OR recession)', "金融政策・景気"),
)
YAHOO_TICKERS = ("CL=F", "BZ=F", "GC=F", "SI=F")
HEADLINE_COLUMNS = ["id", "available", "published", "source", "query", "title", "url", "domain", "language",
                    "country"]


# --------------------------------------------------------------------------- 取得
class Throttled(RuntimeError):
    """GDELT に「アクセスが多すぎる」と断られ続けた。"""


class QueryError(RuntimeError):
    """GDELT が検索式や期間を受け付けなかった（文章で理由が返る）。"""


def http_get(url: str, timeout: float = 60.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def gdelt_url(query: str, mode: str, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None,
              timespan: str | None = None) -> str:
    params = {"query": f"{query} sourcelang:english", "mode": mode, "format": "json"}
    if start is not None and end is not None:
        params["startdatetime"] = start.strftime("%Y%m%d%H%M%S")
        params["enddatetime"] = end.strftime("%Y%m%d%H%M%S")
    elif timespan:
        params["timespan"] = timespan
    if mode == "artlist":
        params |= {"maxrecords": str(MAX_RECORDS), "sort": "DateDesc"}
    else:
        params["timelinesmooth"] = "0"
    return GDELT_API + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


class GdeltClient:
    """GDELT に 1 件ずつ、間をあけて問い合わせる。断られたら待ってやり直す。"""

    def __init__(self, wait: float = GDELT_WAIT, backoff: Iterable[float] = GDELT_BACKOFF,
                 get: Callable[[str], tuple[int, bytes]] = http_get, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print):
        self.wait, self.backoff = wait, tuple(backoff)
        self._get, self._sleep, self._clock, self._log = get, sleep, clock, log
        self._last = -1e18
        self.requests = 0

    def _pace(self, extra: float) -> None:
        left = self._last + self.wait + extra - self._clock()
        if left > 0:
            self._sleep(left)
        self._last = self._clock()

    def fetch(self, url: str) -> str:
        last = ""
        for extra in (0.0,) + self.backoff:
            if extra:
                self._log(f"  GDELT に断られた（{last}）。{extra:.0f} 秒待ってやり直す")
            self._pace(extra)
            self.requests += 1
            try:
                status, raw = self._get(url)
            except OSError as e:        # 通信の失敗（タイムアウトなど）
                last = f"通信の失敗: {e}"[:80]
                continue
            text = raw.decode("utf-8", errors="replace").strip()
            if status == 429 or "limit requests" in text.lower():
                last = "アクセス過多"
                continue
            if status >= 500:
                last = f"HTTP {status}"
                continue
            if status != 200 or (text and not text.startswith("{")):
                raise QueryError(f"HTTP {status}: {text[:200]}")
            return text
        raise Throttled(last)


def _loads(text: str) -> dict:
    if not text:
        return {}
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:   # GDELT の JSON には壊れた \ が混ざることがある
        return json.loads(re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text), strict=False)


def gdelt_time(s: str) -> pd.Timestamp:
    return pd.Timestamp(pd.to_datetime(s, format="%Y%m%dT%H%M%SZ", utc=True))


def clean_title(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def headline_id(url: str) -> str:
    """URL から見出しの ID（同じ記事を 2 回数えないため）。utm_ などの追跡用の引数は除く。"""
    p = urllib.parse.urlsplit(url.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    norm = urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"),
                                    urllib.parse.urlencode(q), ""))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def title_key(title: str) -> str:
    """見出しの比べ方（大文字小文字・記号の違いは同じとみなす。転載の重複を消す）。"""
    return re.sub(r"[^0-9a-z぀-ヿ一-鿿]+", "", title.lower())


def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=HEADLINE_COLUMNS)
    for c in ("available", "published"):
        df[c] = pd.to_datetime(df[c], utc=True)
    return df


def parse_artlist(text: str, query_key: str) -> pd.DataFrame:
    rows = []
    for a in _loads(text).get("articles") or []:
        url, title = (a.get("url") or "").strip(), clean_title(a.get("title"))
        if not url or not title or not a.get("seendate"):
            continue
        seen = gdelt_time(a["seendate"])
        rows.append({"id": headline_id(url), "available": seen + GDELT_DELAY, "published": seen, "source": "gdelt",
                     "query": query_key, "title": title, "url": url, "domain": a.get("domain") or "",
                     "language": a.get("language") or "", "country": a.get("sourcecountry") or ""})
    return _frame(rows)


def parse_timeline(text: str) -> pd.DataFrame:
    """推移（列 value、量なら全記事数の norm も）。index は UTC の時刻。"""
    tl = _loads(text).get("timeline") or []
    data = tl[0].get("data") if tl else None
    if not data:
        return pd.DataFrame(columns=["value"], index=pd.DatetimeIndex([], tz="UTC", name="time"))
    df = pd.DataFrame(data)
    out = pd.DataFrame({"value": pd.to_numeric(df["value"], errors="coerce").to_numpy()},
                       index=pd.DatetimeIndex(pd.to_datetime(df["date"], format="%Y%m%dT%H%M%SZ", utc=True), name="time"))
    if "norm" in df:
        out["norm"] = pd.to_numeric(df["norm"], errors="coerce").to_numpy()
    return out.dropna(subset=["value"])


def yahoo_url(ticker: str, count: int = 50) -> str:
    return YAHOO_SEARCH + "?" + urllib.parse.urlencode({"q": ticker, "newsCount": count, "quotesCount": 0})


def parse_yahoo_news(raw: bytes, ticker: str, now: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for x in json.loads(raw.decode("utf-8", errors="replace") or "{}").get("news") or []:
        url, title = (x.get("link") or "").strip(), clean_title(x.get("title"))
        ts = x.get("providerPublishTime")
        if not url or not title or ts is None:
            continue
        rows.append({"id": headline_id(url), "available": now, "published": pd.Timestamp(int(ts), unit="s", tz="UTC"),
                     "source": "yahoo", "query": ticker, "title": title, "url": url,
                     "domain": x.get("publisher") or "", "language": "English", "country": ""})
    return _frame(rows)


# --------------------------------------------------------------------------- 保存
class NewsStore:
    """data/news の下に保存する。見出しは UTC の日ごとの CSV（使えるようになった時刻で分ける）。"""

    def __init__(self, root: str | Path = "data/news"):
        self.root = Path(root)
        self._keys: dict[pd.Timestamp, tuple[set[str], set[str]]] = {}

    # ---- 見出し
    def headline_path(self, day: pd.Timestamp) -> Path:
        return self.root / "headlines" / f"{day:%Y-%m}" / f"{day:%Y-%m-%d}.csv"

    def _day_keys(self, day: pd.Timestamp) -> tuple[set[str], set[str]]:
        if day not in self._keys:
            p = self.headline_path(day)
            if p.exists():
                df = pd.read_csv(p, usecols=["id", "title"], dtype=str, keep_default_na=False)
                self._keys[day] = (set(df["id"]), {title_key(t) for t in df["title"]})
            else:
                self._keys[day] = (set(), set())
        return self._keys[day]

    def add(self, df: pd.DataFrame) -> int:
        """新しい見出しだけを足す（同じ URL・同じ見出しは、その日と前の 2 日に既にあれば足さない）。"""
        if df.empty:
            return 0
        df = df.sort_values("available")
        added = 0
        for day, g in df.groupby(df["available"].dt.floor("D")):
            day = pd.Timestamp(day)
            ids, keys = self._day_keys(day)
            older = [self._day_keys(day - pd.Timedelta(days=k)) for k in (1, 2)]
            keep = []
            for i, row in enumerate(g.itertuples(index=False)):
                tk = title_key(row.title)
                if not tk or row.id in ids or tk in keys or any(row.id in o[0] or tk in o[1] for o in older):
                    continue
                ids.add(row.id)
                keys.add(tk)
                keep.append(i)
            if not keep:
                continue
            out = g.iloc[keep].copy()
            for c in ("available", "published"):
                out[c] = out[c].dt.strftime(TIME_FMT)
            p = self.headline_path(day)
            p.parent.mkdir(parents=True, exist_ok=True)
            out.to_csv(p, mode="a", header=not p.exists(), index=False)
            added += len(out)
        return added

    def days(self) -> list[pd.Timestamp]:
        d = self.root / "headlines"
        return sorted(pd.Timestamp(p.stem, tz="UTC") for p in d.glob("*/*.csv")) if d.exists() else []

    def load_day(self, day: pd.Timestamp) -> pd.DataFrame:
        p = self.headline_path(day)
        if not p.exists():
            return _frame([])
        df = pd.read_csv(p, dtype=str, keep_default_na=False)
        for c in ("available", "published"):
            df[c] = pd.to_datetime(df[c], utc=True)
        return df

    def load(self, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> pd.DataFrame:
        days = [d for d in self.days() if (start is None or d >= start) and (end is None or d < end)]
        frames = [self.load_day(d) for d in days]
        return pd.concat(frames, ignore_index=True) if frames else _frame([])

    # ---- 推移（キーワードの量・論調）
    def timeline_path(self, key: str, mode: str) -> Path:
        return self.root / "timeline" / f"{key}_{mode}.csv"

    def save_timeline(self, key: str, mode: str, df: pd.DataFrame) -> None:
        old = self.load_timeline(key, mode)
        new = pd.concat([old, df]) if len(old) else df
        new = new[~new.index.duplicated(keep="last")].sort_index()
        p = self.timeline_path(key, mode)
        p.parent.mkdir(parents=True, exist_ok=True)
        out = new.copy()
        out.index = out.index.strftime(TIME_FMT)
        out.index.name = "time"
        out.to_csv(p)

    def load_timeline(self, key: str, mode: str) -> pd.DataFrame:
        p = self.timeline_path(key, mode)
        if not p.exists():
            return pd.DataFrame(columns=["value"], index=pd.DatetimeIndex([], tz="UTC", name="time"))
        df = pd.read_csv(p)
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("time"), utc=True), name="time")
        return df

    # ---- どこまで取ったか
    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}

    def save_state(self, **kw) -> None:
        s = self.state() | kw
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------- 集める
def windows(start: pd.Timestamp, end: pd.Timestamp, width: pd.Timedelta) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    out, s = [], start
    while s < end:
        e = min(s + width, end)
        out.append((s, e))
        s = e
    return out


def collect_articles(client: GdeltClient, store: NewsStore, start: pd.Timestamp, end: pd.Timestamp,
                     width: pd.Timedelta, state_key: str, queries=ARTICLE_QUERIES,
                     log: Callable[[str], None] = print) -> tuple[int, int]:
    """start〜end の記事の一覧を、幅 width ごとに古い方から取る。終わった所までを state_key に記録する。

    戻り値: (足した見出しの数, 上限の 250 件に達した回数)。
    """
    added = capped = 0
    ws = windows(start, end, width)
    for i, (s, e) in enumerate(ws):
        for q in queries:
            try:
                df = parse_artlist(client.fetch(gdelt_url(q.text, "artlist", s, e)), q.key)
            except QueryError as err:
                log(f"  {q.key} {s:%m-%d %H:%M}: 受け付けられなかった（{err}）")
                continue
            capped += len(df) >= MAX_RECORDS
            added += store.add(df)
        store.save_state(**{state_key: e.strftime(TIME_FMT)})
        if len(ws) > 4 and (i + 1) % 20 == 0:
            log(f"  {e:%Y-%m-%d %H:%M} まで（{i + 1}/{len(ws)}、見出し +{added}）")
    return added, capped


def update_timelines(client: GdeltClient, store: NewsStore, start: pd.Timestamp, end: pd.Timestamp,
                     queries=TIMELINE_QUERIES, modes=TIMELINE_MODES, log: Callable[[str], None] = print) -> int:
    """キーワードの量・論調の推移を start〜end について 3 か月ずつ取って保存する。取れた点の数を返す。"""
    got = 0
    for q in queries:
        for mode in modes:
            for s, e in windows(start, end, pd.Timedelta(days=91)):
                try:
                    df = parse_timeline(client.fetch(gdelt_url(q.text, mode, s, e)))
                except QueryError as err:
                    log(f"  {q.key} {mode} {s:%Y-%m}: 受け付けられなかった（{err}）")
                    continue
                if len(df):
                    store.save_timeline(q.key, mode, df)
                    got += len(df)
    return got


def collect_yahoo(store: NewsStore, now: pd.Timestamp, get: Callable[[str], tuple[int, bytes]] = http_get,
                  sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print) -> int:
    added = 0
    for t in YAHOO_TICKERS:
        try:
            status, raw = get(yahoo_url(t))
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            added += store.add(parse_yahoo_news(raw, t, now))
        except Exception as e:  # noqa: BLE001  1 銘柄の失敗で全体を止めない
            log(f"  Yahoo {t}: {e}"[:160])
        sleep(1.0)
    return added


# --------------------------------------------------------------------------- 日ごとの特徴
def daily_timeline(vol: pd.DataFrame | None, tone: pd.DataFrame | None) -> pd.DataFrame:
    """推移を UTC の日ごとに: share（全記事に対する割合）と tone（平均の論調）。毎日の行がある（無い日は NaN）。"""
    cols = {}
    if vol is not None and len(vol):
        g = vol.groupby(vol.index.floor("D"))
        cols["share"] = g["value"].sum() / g["norm"].sum() if "norm" in vol else g["value"].mean()
    if tone is not None and len(tone):
        cols["tone"] = tone.groupby(tone.index.floor("D"))["value"].mean()
    if not cols:
        return pd.DataFrame()
    df = pd.DataFrame(cols)
    full = pd.date_range(df.index.min(), df.index.max(), freq="D", tz="UTC")
    return df.reindex(full)


def spikes(x: pd.Series, window: int = 60, z: float = 2.0, min_periods: int = 20) -> pd.Series:
    """直近 window 日（その日を含まない）の平均から z 標準偏差を超えて増えた日。"""
    m = x.rolling(window, min_periods=min_periods).mean().shift(1)
    s = x.rolling(window, min_periods=min_periods).std().shift(1)
    return ((x - m) / s > z).fillna(False)


def sample_like_backfill(df: pd.DataFrame) -> pd.DataFrame:
    """記事の一覧を、さかのぼって取ったときと同じ取り方にそろえる（検索ごと・6 時間ごとに最新の 250 件）。

    さかのぼった期間は 6 時間ごとに最新の 250 件しか取れず、毎日集めた期間はもっと多い。そのままだと
    つなぎ目で数え方が変わるので、特徴を作る前に同じ取り方にそろえる（docs/news.md 6 章）。
    """
    g = df[df["source"] == "gdelt"]
    if g.empty:
        return g
    block = g["published"].dt.floor(BLOCK)
    g = g.assign(_b=block).sort_values("published", ascending=False)
    return g.groupby(["query", "_b"], sort=False).head(MAX_RECORDS).drop(columns="_b").sort_values("available")


# --------------------------------------------------------------------------- 価格との突き合わせ
def avail_ns(days: pd.DatetimeIndex) -> np.ndarray:
    """UTC の日 D の分が使えるようになる時刻（D+1 日 0:00 UTC、ns）。"""
    return ((days.tz_convert("UTC") if days.tz is not None else days.tz_localize("UTC"))
            + pd.Timedelta(days=1)).as_unit("ns").asi8.astype(np.int64)


def flags_at(days: pd.DatetimeIndex, flags: np.ndarray, decide: np.ndarray) -> np.ndarray:
    """判断ごとに、前の判断のあとに使えるようになった日のどれかに印があるか（土日の分は月曜の判断に入る）。"""
    out = np.zeros(len(decide), bool)
    f = np.asarray(flags, bool)
    if not len(decide) or not f.any():
        return out
    k = np.searchsorted(decide, avail_ns(days)[f], side="left")
    k = k[k < len(decide)]
    out[k] = True
    return out


def values_at(days: pd.DatetimeIndex, values: np.ndarray, decide: np.ndarray) -> np.ndarray:
    """判断ごとに、それまでに使えるようになった最新の日の値（無ければ NaN）。"""
    v = np.asarray(values, float)
    k = np.searchsorted(avail_ns(days), decide, side="right") - 1
    out = np.full(len(decide), np.nan)
    ok = k >= 0
    out[ok] = v[k[ok]]
    return out


@dataclass
class MarketDaily:
    """1 市場の日足。判断はその日の引け、next_ret は次の日の値動き（対数）。"""

    key: str
    dates: pd.DatetimeIndex
    decide: np.ndarray
    next_ret: np.ndarray
    norm_abs: np.ndarray      # ふだんの値動き: 直近 60 日の |値動き| の平均（その日まで）
    sigma: np.ndarray         # 1 日の値動きの標準偏差（指数平滑、その日まで）

    @classmethod
    def from_close(cls, key: str, close: pd.Series, tz: str) -> "MarketDaily":
        from .leadlag import close_ns

        r = np.log(close).diff()
        return cls(key, pd.DatetimeIndex(close.index), close_ns(pd.DatetimeIndex(close.index), tz),
                   r.shift(-1).to_numpy(float), r.abs().rolling(60, min_periods=40).mean().to_numpy(float),
                   r.ewm(span=60, min_periods=40).std().to_numpy(float))


def vol_ratios(m: MarketDaily, flags: np.ndarray) -> np.ndarray:
    """印のある判断の次の日の |値動き| ÷ ふだんの値動き。"""
    x = np.abs(m.next_ret) / m.norm_abs
    ok = np.asarray(flags, bool) & np.isfinite(x)
    return x[ok]


def hold_returns(m: MarketDaily, signal: np.ndarray, h: int, cost: tuple[float, float]) -> pd.Series:
    """判断ごとの向き（+1 / −1 / 0）を h 日持つ（毎日 1/h ずつ入れ替える）。年率 15% にそろえた日ごとの損益。"""
    from .ml import MAX_LEVERAGE, TARGET_VOL

    s = np.where(np.isfinite(signal), signal, 0.0)
    pos = pd.Series(s).rolling(h, min_periods=1).mean().to_numpy()
    lev = np.minimum(TARGET_VOL / m.sigma, MAX_LEVERAGE)
    u = np.where(np.isfinite(lev), pos * lev, 0.0)
    prev = np.r_[0.0, u[:-1]]
    rt, fin = cost
    pnl = u * np.nan_to_num(m.next_ret) - rt / 2 * np.abs(u - prev) - fin / 252 * np.abs(u)
    ok = np.isfinite(m.next_ret) & np.isfinite(m.sigma)
    return pd.Series(pnl[ok], index=m.dates[ok])


def ann_sharpe(r: pd.Series) -> float:
    r = r.dropna()
    sd = r.std()
    return float(r.mean() / sd * np.sqrt(252)) if len(r) > 20 and sd > 0 else float("nan")


def shift_days(values: np.ndarray, k: int) -> np.ndarray:
    """日ごとの並びを k 日ずらす（偶然との比較。印の数と続き方はそのまま、時期だけが合わなくなる）。"""
    return np.roll(np.asarray(values), k)
