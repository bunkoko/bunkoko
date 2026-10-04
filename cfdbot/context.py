"""外部データ（金利・ドル・株価・他の商品・投機筋の建玉など）の取得と、判断時点での参照。

研究用。売買の判断に使うのは「その時点で公表済みだった値」だけにする（先読みしない）。
各系列に「観測日から何営業日後の何時（米東部時間）に公表されるか」を持たせ、
判断の時刻より前に公表された最新の値を使う。

    取得元         キー                   公表のタイミング（保守的に置いた値）
    FRED（米連銀）  金利・期待インフレ等     観測日の翌営業日 17:00 ET（ドル指数は週 1 回なので 5 営業日後）
    Yahoo          先物・株価指数・為替     観測日が取引所の時刻で終わったとき（翌日 0:00）
    CFTC           投機筋の建玉（週次）      火曜の観測 → 金曜 15:30 ET（3 営業日後 16:00 で扱う）

データは data/context/<取得元>/<キー>.csv に保存する（scripts/fetch_context.py が作る）。
"""

from __future__ import annotations

import io
import json
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.tseries.offsets import BDay

from .events import ET

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"


@dataclass(frozen=True)
class SeriesSpec:
    key: str
    source: str        # "fred" / "yahoo" / "cftc"
    code: str          # FRED の ID / Yahoo のティッカー / CFTC の市場コード
    label: str
    kind: str          # "price"（比率で変化を見る）/ "level"（差で見る: 金利・スプレッド・VIX など）
    lag_days: int = 0  # 観測日の何営業日後に公表されるか（FRED・CFTC）
    release_et: float = 17.0


CATALOG: tuple[SeriesSpec, ...] = (
    # ---- 米国の金利・物価・信用・恐怖指数（FRED。無料・キー不要）
    SeriesSpec("ust10y", "fred", "DGS10", "米10年国債利回り", "level", 1),
    SeriesSpec("ust2y", "fred", "DGS2", "米2年国債利回り", "level", 1),
    SeriesSpec("real10y", "fred", "DFII10", "米10年実質金利（物価連動債）", "level", 1),
    SeriesSpec("breakeven10y", "fred", "T10YIE", "期待インフレ率（10年）", "level", 1),
    SeriesSpec("curve10y2y", "fred", "T10Y2Y", "長短金利差（10年−2年）", "level", 1),
    SeriesSpec("usd_broad", "fred", "DTWEXBGS", "ドル指数（FRB・貿易加重）", "price", 5),
    SeriesSpec("hy_spread", "fred", "BAMLH0A0HYM2", "ハイイールド債スプレッド", "level", 1),
    SeriesSpec("vix", "fred", "VIXCLS", "VIX（米株の予想変動率）", "level", 1),
    SeriesSpec("ovx", "fred", "OVXCLS", "原油の予想変動率（OVX）", "level", 1),
    SeriesSpec("gvz", "fred", "GVZCLS", "金の予想変動率（GVZ）", "level", 1),
    SeriesSpec("crude_stocks", "fred", "WCESTUS1", "米原油在庫（週次・戦略備蓄を除く）", "price", 3),
    # ---- 先物（長期の検証に使う銘柄と、関連する商品）
    SeriesSpec("fut_gold", "yahoo", "GC=F", "金先物", "price"),
    SeriesSpec("fut_silver", "yahoo", "SI=F", "銀先物", "price"),
    SeriesSpec("fut_wti", "yahoo", "CL=F", "WTI原油先物", "price"),
    SeriesSpec("fut_brent", "yahoo", "BZ=F", "ブレント原油先物", "price"),
    SeriesSpec("copper", "yahoo", "HG=F", "銅先物", "price"),
    SeriesSpec("platinum", "yahoo", "PL=F", "プラチナ先物", "price"),
    SeriesSpec("natgas", "yahoo", "NG=F", "天然ガス先物", "price"),
    # ---- 株価（指数・関連業種）
    SeriesSpec("spx", "yahoo", "^GSPC", "S&P500", "price"),
    SeriesSpec("ndx", "yahoo", "^NDX", "ナスダック100", "price"),
    SeriesSpec("nikkei", "yahoo", "^N225", "日経平均", "price"),
    SeriesSpec("em_equity", "yahoo", "EEM", "新興国株（ETF）", "price"),
    SeriesSpec("gold_miners", "yahoo", "GDX", "金鉱株（ETF）", "price"),
    SeriesSpec("silver_miners", "yahoo", "SIL", "銀鉱株（ETF）", "price"),
    SeriesSpec("energy_equity", "yahoo", "XLE", "エネルギー株（ETF）", "price"),
    # ---- 為替・国債価格
    SeriesSpec("dxy", "yahoo", "DX-Y.NYB", "ドルインデックス", "price"),
    SeriesSpec("usdjpy", "yahoo", "JPY=X", "ドル円", "price"),
    SeriesSpec("eurusd", "yahoo", "EURUSD=X", "ユーロドル", "price"),
    SeriesSpec("audusd", "yahoo", "AUDUSD=X", "豪ドル（資源国通貨）", "price"),
    SeriesSpec("usdcad", "yahoo", "CAD=X", "ドル/カナダドル（産油国通貨）", "price"),
    SeriesSpec("tbond", "yahoo", "TLT", "米長期国債（ETF・価格）", "price"),
    # ---- 投機筋（大口の運用会社）の建玉（CFTC。週次）
    SeriesSpec("cot_gold", "cftc", "088691", "金の投機筋の買い越し", "level", 3, 16.0),
    SeriesSpec("cot_silver", "cftc", "084691", "銀の投機筋の買い越し", "level", 3, 16.0),
    SeriesSpec("cot_wti", "cftc", "067651", "WTIの投機筋の買い越し", "level", 3, 16.0),
    SeriesSpec("cot_copper", "cftc", "085692", "銅の投機筋の買い越し", "level", 3, 16.0),
)

SPECS = {s.key: s for s in CATALOG}

# 長期の検証で売買する先物（銘柄キー → 系列キー）
FUTURES_FOR = {"GOLD": "fut_gold", "SILVER": "fut_silver", "WTI": "fut_wti", "BRENT": "fut_brent"}


# --------------------------------------------------------------------------- 判断時点での参照
@dataclass
class CSeries:
    """1 つの系列。values は観測順、avail は各観測が公表された時刻（UTC, ns, 単調増加）。"""

    key: str
    values: pd.DataFrame      # 列: close（価格系）または value（水準系）。COT は net など
    avail: np.ndarray
    kind: str

    @property
    def main(self) -> pd.Series:
        col = "close" if "close" in self.values else "value" if "value" in self.values else self.values.columns[0]
        return self.values[col].astype(float)


def at_times(cs: CSeries, arr, decide_ns: np.ndarray) -> np.ndarray:
    """判断の時刻ごとに、それまでに公表済みの最新の値（arr は cs の観測と同じ並び）。無ければ NaN。"""
    arr = np.asarray(arr, dtype=float)
    k = np.searchsorted(cs.avail, decide_ns, side="right") - 1
    out = np.full(len(decide_ns), np.nan)
    ok = k >= 0
    out[ok] = arr[k[ok]]
    return out


def change(cs: CSeries, n: int) -> np.ndarray:
    """観測 n 本前からの変化（価格系は対数比、水準系は差）。"""
    x = cs.main.to_numpy(float)
    out = np.full(len(x), np.nan)
    if len(x) > n:
        if cs.kind == "price":
            with np.errstate(divide="ignore", invalid="ignore"):
                out[n:] = np.log(x[n:] / x[:-n])
        else:
            out[n:] = x[n:] - x[:-n]
    out[~np.isfinite(out)] = np.nan
    return out


def zchange(cs: CSeries, n: int, window: int = 252) -> np.ndarray:
    """n 本の変化を、1 本あたりの変化の大きさ（直近 window 本の標準偏差 × √n）で割ったもの。"""
    one = pd.Series(change(cs, 1))
    scale = one.rolling(window, min_periods=window // 2).std().to_numpy() * np.sqrt(n)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = change(cs, n) / scale
    z[~np.isfinite(z)] = np.nan
    return z


def level_z(cs: CSeries, window: int = 252) -> np.ndarray:
    x = cs.main
    m = x.rolling(window, min_periods=window // 2).mean()
    s = x.rolling(window, min_periods=window // 2).std()
    z = np.array((x - m) / s, dtype=float)
    z[~np.isfinite(z)] = np.nan
    return z


def cot_percentile(cs: CSeries, window: int = 156) -> np.ndarray:
    """投機筋の買い越し（建玉に対する比率）が、直近 window 週の中でどの位置か（0〜1）。"""
    net = cs.values["net"].astype(float)
    pct = net.rolling(window, min_periods=window // 2).apply(lambda w: (w[:-1] < w[-1]).mean() if len(w) > 1 else np.nan,
                                                              raw=True)
    return np.array(pct, dtype=float)


def from_bars(key: str, df: pd.DataFrame, tf: pd.Timedelta) -> CSeries:
    """売買に使っている足（UTC の足の開始時刻）を、足の終了時刻に公表される系列として扱う。"""
    avail = (df.index.tz_convert("UTC").as_unit("ns").asi8 + tf.value).astype(np.int64)
    return CSeries(key, df[["close"]].copy(), avail, "price")


def _avail_fred(dates: pd.DatetimeIndex, lag_days: int, release_et: float) -> np.ndarray:
    d = dates + BDay(lag_days) if lag_days else dates
    t = (d + pd.Timedelta(hours=release_et)).tz_localize(ET, nonexistent="shift_forward", ambiguous=False)
    return t.tz_convert("UTC").as_unit("ns").asi8.astype(np.int64)


def _avail_exchange(dates: pd.DatetimeIndex, tz: str) -> np.ndarray:
    """観測日がその取引所の時刻で終わった時（翌日 0:00）。"""
    t = (dates + pd.Timedelta(days=1)).tz_localize(tz, nonexistent="shift_forward", ambiguous=False)
    return t.tz_convert("UTC").as_unit("ns").asi8.astype(np.int64)


# --------------------------------------------------------------------------- 保存と読み込み
class ContextStore:
    """data/context の下に保存した系列を読む。"""

    def __init__(self, root: str | Path = "data/context"):
        self.root = Path(root)
        self._cache: dict[str, CSeries | None] = {}

    def path(self, spec: SeriesSpec) -> Path:
        return self.root / spec.source / f"{spec.key}.csv"

    def available(self) -> list[str]:
        return [s.key for s in CATALOG if self.path(s).exists()]

    def get(self, key: str) -> CSeries | None:
        if key not in self._cache:
            spec = SPECS[key]
            p = self.path(spec)
            self._cache[key] = load_series(p, spec) if p.exists() else None
        return self._cache[key]


def load_series(path: Path, spec: SeriesSpec) -> CSeries | None:
    df = pd.read_csv(path)
    if df.empty:
        return None
    dates = pd.DatetimeIndex(pd.to_datetime(df["date"]))
    df = df.drop(columns=["date"]).set_index(dates).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if spec.source == "yahoo":
        tz = str(df["tz"].iloc[-1]) if "tz" in df else "America/New_York"
        avail = _avail_exchange(df.index, tz)
        df = df.drop(columns=[c for c in ("tz",) if c in df])
    else:
        avail = _avail_fred(df.index, spec.lag_days, spec.release_et)
    order = np.argsort(avail, kind="stable")
    return CSeries(spec.key, df.iloc[order], avail[order], spec.kind)


def yahoo_frame(store: ContextStore, key: str) -> pd.DataFrame | None:
    """Yahoo の日足を、バックテストに使える形（UTC の足の開始時刻・24 時間足）にする。

    取引日 D の足は前日 18:00 ET に始まり D の 18:00 ET に終わるものとして並べる（CME の取引時間に近い）。
    価格が 0 以下の日（2020-04 の WTI など）以降は使わない。
    """
    p = store.path(SPECS[key])
    if not p.exists():
        return None
    df = pd.read_csv(p).dropna(subset=["close"])
    bad = df[(df[["open", "high", "low", "close"]] <= 0).any(axis=1)]
    if not bad.empty:
        df = df[pd.to_datetime(df["date"]) < pd.to_datetime(bad["date"]).min()]
    df = df.assign(**{c: df[c].fillna(df["close"]) for c in ("open", "high", "low")})
    dates = pd.DatetimeIndex(pd.to_datetime(df["date"]))
    opens = (dates - pd.Timedelta(hours=6)).tz_localize(ET, nonexistent="shift_forward", ambiguous=False)
    out = pd.DataFrame({c: df[c].to_numpy(float) for c in ("open", "high", "low", "close")},
                       index=opens.tz_convert("UTC"))
    # 高値・安値の欠け（始値・終値の外側に無い）を直す
    out["high"] = out[["open", "high", "low", "close"]].max(axis=1)
    out["low"] = out[["open", "high", "low", "close"]].min(axis=1)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out.index.name = "time"
    return out


# --------------------------------------------------------------------------- 取得
def _get(url: str, timeout: float = 60.0, tries: int = 4) -> bytes:
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001  通信の失敗は種類を問わず再試行する
            last = e
            time.sleep(2 ** i)
    raise RuntimeError(f"{url}: {last}")


def parse_fred_csv(raw: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(raw))
    df.columns = ["date", "value"] + list(df.columns[2:])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df[["date", "value"]].dropna()


def fetch_fred(code: str) -> pd.DataFrame:
    return parse_fred_csv(_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={code}"))


def parse_yahoo_chart(raw: bytes) -> pd.DataFrame:
    js = json.loads(raw)
    res = (js.get("chart") or {}).get("result") or []
    if not res:
        err = (js.get("chart") or {}).get("error")
        raise RuntimeError(f"Yahoo: {err}")
    r = res[0]
    tz = (r.get("meta") or {}).get("exchangeTimezoneName") or "America/New_York"
    ts = r.get("timestamp") or []
    q = (r.get("indicators") or {}).get("quote", [{}])[0]
    if not ts:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "tz"])
    dates = pd.to_datetime(np.asarray(ts, dtype=np.int64), unit="s", utc=True).tz_convert(tz)
    df = pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": pd.to_numeric(pd.Series(q.get("open")), errors="coerce"),
        "high": pd.to_numeric(pd.Series(q.get("high")), errors="coerce"),
        "low": pd.to_numeric(pd.Series(q.get("low")), errors="coerce"),
        "close": pd.to_numeric(pd.Series(q.get("close")), errors="coerce"),
        "volume": pd.to_numeric(pd.Series(q.get("volume")), errors="coerce"),
    })
    df["tz"] = tz
    df = df.dropna(subset=["close"]).drop_duplicates("date", keep="last")
    return df


def fetch_yahoo(ticker: str, since: str = "2000-01-01") -> pd.DataFrame:
    p1 = int(pd.Timestamp(since, tz="UTC").timestamp())
    p2 = int(pd.Timestamp.now(tz="UTC").timestamp()) + 86400
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker, safe='')}"
           f"?period1={p1}&period2={p2}&interval=1d&events=history")
    try:
        return parse_yahoo_chart(_get(url))
    except Exception as first:  # noqa: BLE001
        try:  # yfinance が入っていればそちらで再試行
            import yfinance as yf  # type: ignore
        except ImportError:
            raise first from None
        h = yf.Ticker(ticker).history(start=since, interval="1d", auto_adjust=False)
        if h.empty:
            raise first from None
        tz = str(h.index.tz) if h.index.tz is not None else "America/New_York"
        return pd.DataFrame({"date": h.index.strftime("%Y-%m-%d"), "open": h["Open"].to_numpy(),
                             "high": h["High"].to_numpy(), "low": h["Low"].to_numpy(),
                             "close": h["Close"].to_numpy(), "volume": h["Volume"].to_numpy(), "tz": tz})


_COT_COLS = {
    "code": "cftc_contract_market_code",
    "date": "report_date_as_yyyy-mm-dd",
    "oi": "open_interest_all",
    "mm_long": "m_money_positions_long_all",
    "mm_short": "m_money_positions_short_all",
}


def parse_cot(raw_zip: bytes) -> pd.DataFrame:
    """CFTC の建玉明細（Disaggregated・先物のみ）の zip を、市場コード・日付・投機筋の買い/売り・建玉の表にする。"""
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith((".txt", ".csv")))
        df = pd.read_csv(z.open(name), low_memory=False, dtype=str)
    cols = {c.strip().lower(): c for c in df.columns}
    pick = {}
    for k, want in _COT_COLS.items():
        hit = cols.get(want) or next((orig for low, orig in cols.items() if low.startswith(want[:20])), None)
        if hit is None:
            raise ValueError(f"CFTC の列が見つからない: {want}")
        pick[k] = hit
    out = pd.DataFrame({k: df[v] for k, v in pick.items()})
    out["code"] = out["code"].str.strip()
    out["date"] = pd.to_datetime(out["date"].str.strip()).dt.strftime("%Y-%m-%d")
    for c in ("oi", "mm_long", "mm_short"):
        out[c] = pd.to_numeric(out[c].str.replace(",", "").str.strip(), errors="coerce")
    return out.dropna()


def cot_urls(first_year: int, last_year: int) -> list[str]:
    base = "https://www.cftc.gov/files/dea/history/"
    urls = []
    if first_year <= 2016:
        urls.append(base + "fut_disagg_txt_hist_2006_2016.zip")
    urls += [base + f"fut_disagg_txt_{y}.zip" for y in range(max(first_year, 2017), last_year + 1)]
    return urls


def cot_series(table: pd.DataFrame, code: str) -> pd.DataFrame:
    t = table[table["code"] == code].sort_values("date").drop_duplicates("date", keep="last")
    t = t[t["oi"] > 0]
    return pd.DataFrame({"date": t["date"], "net": (t["mm_long"] - t["mm_short"]) / t["oi"],
                         "mm_long": t["mm_long"], "mm_short": t["mm_short"], "oi": t["oi"]})


# --------------------------------------------------------------------------- MT5 のサーバーにある関連銘柄
def load_mt5_catalog(path: str | Path) -> pd.DataFrame | None:
    """CfdExportBars が書いた symbols_all.txt（サーバーの全銘柄）。無ければ None。"""
    path = Path(path)
    if not path.exists():
        return None
    df = pd.read_csv(path, sep="\t", dtype=str, encoding_errors="replace").fillna("")
    df["group"] = df["path"].str.replace("/", "\\").str.split("\\").str[0]
    return df


def mt5_context_files(root: str | Path) -> dict[str, Path]:
    """data/context/mt5/<銘柄>_D1.csv（CfdExportBars の関連銘柄の日足）。銘柄名 → ファイル。"""
    d = Path(root) / "mt5"
    return {p.name.rsplit("_", 1)[0]: p for p in sorted(d.glob("*_D1.csv"))} if d.is_dir() else {}

