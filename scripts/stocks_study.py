"""上がりやすい株を探す（./cfd stocks）。時期・時間帯・業種・会社の 17 の仮説を、同じ条件で判定する。

    python scripts/stocks_study.py --fetch     # 初回。銘柄の一覧・日足・配当・決算日・1 時間足を取る（20〜40 分）
    python scripts/stocks_study.py             # data/stocks のデータで計算する（数分）

仮説と判定の条件は docs/stocks.md（結果を見る前に決めたもの）。
"""

from __future__ import annotations

import argparse
import io
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot import stocks as S  # noqa: E402

FRENCH = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
FRENCH_FILES = ("49_Industry_Portfolios", "10_Portfolios_Prior_12_2", "F-F_Research_Data_Factors", "Japan_3_Factors",
                "Japan_6_Portfolios_ME_BE-ME", "Japan_6_Portfolios_ME_Prior_12_2")
JPX_LIST = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
SP500_LIST = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
JP_SIZES = ("TOPIX Core30", "TOPIX Large70", "TOPIX Mid400")
INDEX, ETF = {"us": "^GSPC", "jp": "^N225"}, {"us": "SPY", "jp": "1306.T"}
SECTOR_ETFS = {"us": ("XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY", "XLRE", "XLC"),
               "jp": tuple(f"{c}.T" for c in range(1617, 1634))}
SECTOR_ETF_NAMES = {"XLB": "素材", "XLE": "エネルギー", "XLF": "金融", "XLI": "資本財", "XLK": "情報技術", "XLP": "生活必需品",
                    "XLU": "公益", "XLV": "ヘルスケア", "XLY": "一般消費財", "XLRE": "不動産", "XLC": "通信サービス",
                    "1617.T": "食品", "1618.T": "エネルギー資源", "1619.T": "建設・資材", "1620.T": "素材・化学", "1621.T": "医薬品",
                    "1622.T": "自動車・輸送機", "1623.T": "鉄鋼・非鉄", "1624.T": "機械", "1625.T": "電機・精密",
                    "1626.T": "情報通信・サービスその他", "1627.T": "電力・ガス", "1628.T": "運輸・物流", "1629.T": "商社・卸売",
                    "1630.T": "小売", "1631.T": "銀行", "1632.T": "金融（除く銀行）", "1633.T": "不動産"}
US_HOURLY = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK-B", "LLY", "AVGO", "JPM", "TSLA", "V", "UNH", "XOM", "MA",
             "JNJ", "PG", "HD", "COST", "WMT", "ABBV", "NFLX", "CRM", "BAC", "KO", "CVX", "MRK", "ORCL", "PEP", "AMD")
TZ = {"us": "America/New_York", "jp": "Asia/Tokyo"}
MARKET = {"us": "米国", "jp": "日本"}
COST = {"us": S.COST_US, "jp": S.COST_JP}
FRENCH_NAMES = {"Agric": "農業", "Food": "食品", "Soda": "清涼飲料", "Beer": "酒類", "Smoke": "たばこ", "Toys": "娯楽用品",
                "Fun": "娯楽", "Books": "出版", "Hshld": "家庭用品", "Clths": "衣料", "Hlth": "医療サービス", "MedEq": "医療機器",
                "Drugs": "医薬品", "Chems": "化学", "Rubbr": "ゴム・プラスチック", "Txtls": "繊維", "BldMt": "建材", "Cnstr": "建設",
                "Steel": "鉄鋼", "FabPr": "金属加工", "Mach": "機械", "ElcEq": "電気設備", "Autos": "自動車", "Aero": "航空機",
                "Ships": "造船・鉄道車両", "Guns": "防衛", "Gold": "金鉱", "Mines": "鉱業", "Coal": "石炭", "Oil": "石油・ガス",
                "Util": "公益", "Telcm": "通信", "PerSv": "個人向けサービス", "BusSv": "企業向けサービス", "Hardw": "コンピュータ",
                "Softw": "ソフトウェア", "Chips": "半導体・電子部品", "LabEq": "計測機器", "Paper": "紙製品", "Boxes": "容器",
                "Trans": "運輸", "Whlsl": "卸売", "Rtail": "小売", "Meals": "外食・宿泊", "Banks": "銀行", "Insur": "保険",
                "RlEst": "不動産", "Fin": "証券・その他金融", "Other": "その他"}


# --------------------------------------------------------------------------- 取得
def http_get(url: str, tries: int = 3) -> bytes:
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh)"})
            return urllib.request.urlopen(req, timeout=60).read()
        except Exception:  # noqa: BLE001
            if k == tries - 1:
                raise
            time.sleep(5 * (k + 1))
    raise RuntimeError(url)


def fetch_universe(root: Path) -> None:
    j = S.read_xlsx(http_get(JPX_LIST))
    j = j[j["規模区分"].isin(JP_SIZES)]
    uni = pd.DataFrame({"ticker": j["コード"].astype(str) + ".T", "name": j["銘柄名"], "sector": j["33業種区分"], "size": j["規模区分"]})
    uni.to_csv(root / "universe_jp.csv", index=False)
    (root / S.ARCHIVE).mkdir(parents=True, exist_ok=True)              # その時の一覧の控え（docs/agents.md 12.7）
    uni.to_csv(root / S.ARCHIVE / f"universe_jp_{datetime.now():%Y%m%d}.csv", index=False)
    u = pd.read_html(io.StringIO(http_get(SP500_LIST).decode("utf-8")))[0]
    pd.DataFrame({"ticker": u["Symbol"].str.replace(".", "-", regex=False), "name": u["Security"],
                  "sector": u["GICS Sector"], "size": "S&P500"}).to_csv(root / "universe_us.csv", index=False)


def fetch_daily(tickers: list[str], start: str, chunk: int = 100) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """始値・終値（配当・分割を調整済み）の表と、配当（date / ticker / amount）。"""
    import yfinance as yf  # type: ignore

    opens, closes, divs = [], [], []
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        for k in range(3):
            d = yf.download(part, start=start, auto_adjust=True, actions=True, progress=False, threads=True, group_by="column")
            if not d.empty and d["Close"].notna().any().mean() > 0.5:
                break
            time.sleep(30 * (k + 1))
        if d.empty or "Dividends" not in d:                       # 3 回とも空（回線・Yahoo・空き容量の不調）。読めない理由で落ちないように
            raise RuntimeError(f"Yahoo から値段が取れない（{part[0]} からの {len(part)} 銘柄。3 回試した）")
        if d.index.tz is not None:
            d.index = d.index.tz_localize(None)
        opens.append(d["Open"])
        closes.append(d["Close"])
        dv = d["Dividends"].stack()
        dv = dv[dv > 0].rename("amount").reset_index()
        dv.columns = ["date", "ticker", "amount"]
        divs.append(dv)
        print(f"  日足 {min(i + chunk, len(tickers))}/{len(tickers)}", flush=True)
    o, c = pd.concat(opens, axis=1).sort_index(), pd.concat(closes, axis=1).sort_index()
    keep = [t for t in tickers if t in c and c[t].notna().sum() > 250]
    return o[keep].where(o[keep] > 0), c[keep].where(c[keep] > 0), pd.concat(divs, ignore_index=True)


def fetch_earnings(tickers: list[str], tz: str, path: Path, workers: int = 4) -> None:
    """決算発表日（その取引所の日付）と、予想との差（%）。途中で止まっても、取れた分から続ける。"""
    import yfinance as yf  # type: ignore

    none_path = path.with_suffix(".none.txt")
    have = pd.read_csv(path) if path.exists() else pd.DataFrame(columns=["ticker", "date", "estimate", "actual", "surprise"])
    skip = set(have["ticker"]) | (set(none_path.read_text().split()) if none_path.exists() else set())
    todo = [t for t in tickers if t not in skip]

    def one(t: str):
        for k in range(3):
            try:
                e = yf.Ticker(t).get_earnings_dates(limit=100)
                if e is None or e.empty:
                    return t, None
                return t, pd.DataFrame({"ticker": t, "date": e.index.tz_convert(tz).strftime("%Y-%m-%d"),
                                        "estimate": e["EPS Estimate"].to_numpy(), "actual": e["Reported EPS"].to_numpy(),
                                        "surprise": e["Surprise(%)"].to_numpy()})
            except Exception as ex:  # noqa: BLE001
                if "No earnings dates" in str(ex) or k == 2:
                    return t, None
                time.sleep(20 * (k + 1))
        return t, None

    for i in range(0, len(todo), 50):
        with ThreadPoolExecutor(workers) as pool:
            got = list(pool.map(one, todo[i:i + 50]))
        frames = [f for _, f in got if f is not None]
        if frames:
            have = pd.concat([have] + frames, ignore_index=True)
            have.to_csv(path, index=False)
        with none_path.open("a") as f:
            f.write("".join(f"{t}\n" for t, fr in got if fr is None))
        print(f"  決算日 {min(i + 50, len(todo))}/{len(todo)}（取れた銘柄 {have['ticker'].nunique()}）", flush=True)


def fetch_value(tickers: list[str], path: Path, workers: int = 4) -> None:
    """今の株価 ÷ 純資産（PBR）と時価総額（「今あてはまるもの」の一覧用。判定には使わない）。"""
    import yfinance as yf  # type: ignore

    def one(t: str):
        for k in range(2):
            try:
                i = yf.Ticker(t).info
                return {"ticker": t, "pb": i.get("priceToBook"), "market_cap": i.get("marketCap")}
            except Exception:  # noqa: BLE001
                time.sleep(10 * (k + 1))
        return {"ticker": t, "pb": None, "market_cap": None}

    with ThreadPoolExecutor(workers) as pool:
        rows = list(pool.map(one, tickers))
    pd.DataFrame(rows).to_csv(path, index=False)


def fetch_hourly(tickers: list[str], tz: str, folder: Path) -> None:
    import yfinance as yf  # type: ignore

    folder.mkdir(parents=True, exist_ok=True)
    d = yf.download(list(tickers), period="725d", interval="60m", auto_adjust=False, progress=False, threads=True, group_by="ticker")
    for t in tickers:
        if t not in d.columns.get_level_values(0):
            continue
        x = d[t].dropna(subset=["Close"])
        if x.empty:
            continue
        idx = x.index.tz_convert(tz).tz_localize(None)
        pd.DataFrame({"time": idx, "open": x["Open"].to_numpy(), "close": x["Close"].to_numpy()}).to_csv(folder / f"{t}.csv", index=False)


def fetch_all(root: Path, since: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    print("銘柄の一覧（東証・Wikipedia）…", flush=True)
    fetch_universe(root)
    (root / "french").mkdir(exist_ok=True)
    for name in FRENCH_FILES:
        (root / "french" / f"{name}.zip").write_bytes(http_get(f"{FRENCH}{name}_CSV.zip"))
    print("指数・ETF…", flush=True)
    misc = list(INDEX.values()) + list(ETF.values()) + ["QQQ", "1321.T"] + [t for v in SECTOR_ETFS.values() for t in v]
    o, c, dv = fetch_daily(misc, "1949-01-01")
    o.to_csv(root / "misc_open.csv", float_format="%.6g")
    c.to_csv(root / "misc_close.csv", float_format="%.6g")
    dv.to_csv(root / "misc_div.csv", index=False)
    for mkt in ("jp", "us"):
        uni = pd.read_csv(root / f"universe_{mkt}.csv")
        print(f"{MARKET[mkt]}の個別株 {len(uni)} 銘柄の日足…", flush=True)
        o, c, dv = fetch_daily(uni["ticker"].tolist(), since)
        o.to_csv(root / f"{mkt}_open.csv", float_format="%.6g")
        c.to_csv(root / f"{mkt}_close.csv", float_format="%.6g")
        dv.to_csv(root / f"{mkt}_div.csv", index=False)
        if mkt == "jp":
            try:
                S.archive_prices(root, o, c)
            except Exception as e:  # noqa: BLE001  控え（docs/agents.md 12.7）が書けなくても、取り直しは続ける
                print(f"値段の控え（{root / S.ARCHIVE}）を書けなかった（{type(e).__name__}: {str(e)[:120]}）", flush=True)
        print(f"{MARKET[mkt]}の決算日…", flush=True)
        fetch_earnings(list(c.columns), TZ[mkt], root / f"{mkt}_earnings.csv")
        if mkt == "jp":
            print("日本の割安度（PBR）…", flush=True)
            fetch_value(list(c.columns), root / "jp_value.csv")
        hourly = list(US_HOURLY) + ["SPY", "QQQ"] if mkt == "us" else uni[uni["size"] == JP_SIZES[0]]["ticker"].tolist() + ["1306.T", "1321.T"]
        print(f"{MARKET[mkt]}の 1 時間足 {len(hourly)} 銘柄…", flush=True)
        fetch_hourly(hourly, TZ[mkt], root / "hourly" / mkt)


# --------------------------------------------------------------------------- 読み込み
def wide(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, index_col=0, parse_dates=True)


def french(root: Path, name: str, section: str | None = None) -> pd.DataFrame:
    return S.read_french_zip((root / "french" / f"{name}.zip").read_bytes(), section)


def complete_months(daily: pd.DataFrame | pd.Series):
    """最後の（途中の）月を落とす。"""
    last = daily.index[-1]
    return daily[daily.index < pd.Timestamp(last.year, last.month, 1)]


def pct(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}%}" if x is not None and np.isfinite(x) else "–"


def f2(x: float) -> str:
    return f"{x:.2f}" if x is not None and np.isfinite(x) else "–"


def placebo_pct(null: np.ndarray, real: float) -> float:
    return float(np.mean(null < real))


def intraday_momentum(bars: pd.DataFrame) -> dict:
    """朝の最初の足まで（前日の引けから）の向きが、その日の最後の足に続くか。"""
    b = bars.sort_index()
    day = pd.Series(b.index.date, index=b.index)
    g = b.groupby(day.to_numpy())
    first_close, last_open, last_close = g["close"].first(), g["open"].last(), g["close"].last()
    first = first_close / last_close.shift(1) - 1
    last = last_close / last_open - 1
    ok = first.notna() & last.notna() & (g.size() >= 4)
    x = (np.sign(first) * last)[ok]
    return {"mean": float(x.mean()), "t": S.t_value(x), "days": int(ok.sum()),
            "corr": float(np.corrcoef(first[ok], last[ok])[0, 1])}


# --------------------------------------------------------------------------- 本体
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fetch", action="store_true", help="データを取り直す")
    p.add_argument("--data", default="data/stocks")
    p.add_argument("--since", default="2000-01-01")
    p.add_argument("--placebo", type=int, default=1000)
    p.add_argument("--out", default="output/stocks")
    args = p.parse_args()

    root = Path(args.data)
    if args.fetch or not (root / "us_close.csv").exists():
        fetch_all(root, args.since)

    if not (root / "jp_value.csv").exists():
        print("日本の割安度（PBR）…", flush=True)
        fetch_value(pd.read_csv(root / "jp_close.csv", nrows=1, index_col=0).columns.tolist(), root / "jp_value.csv")

    n_trials = S.PRIOR_TRIALS + S.NEW_TRIALS
    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    misc_o, misc_c = wide(root / "misc_open.csv"), wide(root / "misc_close.csv")
    misc_div = pd.read_csv(root / "misc_div.csv", parse_dates=["date"])
    uni = {m: pd.read_csv(root / f"universe_{m}.csv").set_index("ticker") for m in ("jp", "us")}
    opens, closes, dropped = {}, {}, {}
    for m in ("jp", "us"):
        o, c = wide(root / f"{m}_open.csv"), wide(root / f"{m}_close.csv")
        opens[m], closes[m] = S.drop_closed_days(o, c)
        dropped[m] = len(c) - len(closes[m])
    divs = {m: pd.read_csv(root / f"{m}_div.csv", parse_dates=["date"]) for m in ("jp", "us")}
    earn = {m: pd.read_csv(root / f"{m}_earnings.csv", parse_dates=["date"]) for m in ("jp", "us")}
    last_day = {m: closes[m].index[-1] for m in closes}

    results: list[dict] = []
    notes: dict[str, list[str]] = {}
    now: dict[str, list[str]] = {}

    def full_cost(key: str, d: pd.Series, scale: float, unit: str) -> None:
        """参考: 米国のコストを往復 1.0% にした場合。"""
        notes.setdefault("US 1.0%", []).append(f"{key}: 差 {unit} {pct(d.mean() * scale, 3 if scale == 1.0 and unit == '1 日あたり' else 1)}、"
                                               f"t 値 {f2(S.t_value(d))}")

    def add(key: str, label: str, d: pd.Series, split: str, unit: str, scale: float, null: np.ndarray | None = None,
            real: float | None = None) -> dict:
        pl = placebo_pct(null, real) if null is not None and len(null) else None
        j = S.judge(d, split, n_trials, pl)
        j.update(key=key, label=label, unit=unit, scale=scale, split=split)
        results.append(j)
        d.to_csv(out / f"d_{key}.csv", header=["d"])
        print(f"  {key} {j['verdict']}", flush=True)
        return j

    # ---- 時期
    print("時期…", flush=True)
    idx_close = {"us": misc_c["^GSPC"].dropna()["1950-01-01":], "jp": misc_c["^N225"].dropna()["1965-01-01":]}
    for key, mkt in (("T1", "us"), ("T2", "jp")):
        c = idx_close[mkt]
        add(key, f"{MARKET[mkt]}: 月替わり（月末 1 日＋月初 3 日）はほかの日より上がる", S.tom_diff(c, COST[mkt]), "2009-01-01",
            "1 日あたり", 1.0, S.tom_placebo(c, args.placebo), float(S.tom_diff(c).mean()))
        g = S.tom_diff(c)
        if mkt == "us":
            full_cost(key, S.tom_diff(c, S.COST_US_FULL), 1.0, "1 日あたり")
        r, tom = c.pct_change(), S.tom_mask(c.index)
        late = np.asarray(c.index >= "2009-01-01")
        notes.setdefault(key, []).append(
            f"コストを引く前の差は 1 日あたり {pct(g.mean(), 3)}（2009 年から {pct(g['2009':].mean(), 3)}）。"
            f"月替わりの 4 日（年 48 日）の合計は年 {pct(r[tom].mean() * 48)}、残りの日（年約 200 日）の合計は年 {pct(r[~tom].mean() * 200)}。"
            f"2009 年からは {pct(r[tom & late].mean() * 48)} と {pct(r[~tom & late].mean() * 200)}")
    winter, first_half = tuple(range(6, 12)), tuple(range(0, 6))
    for key, mkt, start, good, split, label in (
            ("T3", "us", 5, winter, "2003-01-01", "11〜4 月は 5〜10 月より上がる"),
            ("T4", "jp", 5, winter, "2003-01-01", "11〜4 月は 5〜10 月より上がる"),
            ("T5", "jp", 1, first_half, "2014-01-01", "1〜6 月は 7〜12 月より上がる")):
        b = S.season_blocks(complete_months(idx_close[mkt]), start)
        add(key, f"{MARKET[mkt]}: {label}", S.season_diff(b, good, COST[mkt]), split, "1 年あたり", 1.0,
            S.season_placebo(b, 6, args.placebo), float(S.season_diff(b, good).mean()))
        g, rest = b[list(good)].sum(axis=1), b.drop(columns=list(good)).sum(axis=1)
        if mkt == "us":
            full_cost(key, S.season_diff(b, good, S.COST_US_FULL), 1.0, "1 年あたり")
        notes.setdefault(key, []).append(
            f"良いとされる半年の平均 {pct(g.mean())}・残りの半年 {pct(rest.mean())}（対数リターン）。"
            f"後半（{split[:4]}〜）は {pct(g[split:].mean())}・{pct(rest[split:].mean())}。良い半年が勝った年の割合 {pct((g > rest).mean(), 0)}")

    # ---- 時間帯
    print("時間帯…", flush=True)
    spy_n, spy_d = S.night_day(misc_o[["SPY"]].dropna(), misc_c[["SPY"]].dropna())
    nd = {"us": (spy_n["SPY"], spy_d["SPY"])}
    jn, jd = S.night_day(opens["jp"], closes["jp"])
    nd["jp"] = (jn.mean(axis=1)["2001-01-01":], jd.mean(axis=1)["2001-01-01":])
    un, ud = S.night_day(opens["us"], closes["us"])
    e_n, e_d = S.night_day(misc_o[["1306.T"]].dropna(), misc_c[["1306.T"]].dropna())
    extra = {"米国の個別株の平均（2001〜）": (un.mean(axis=1)["2001-01-01":], ud.mean(axis=1)["2001-01-01":], S.COST_US),
             "1306（TOPIX 連動 ETF、2009〜）": (e_n["1306.T"], e_d["1306.T"], S.COST_JP)}
    for key, mkt, what in (("N1", "us", "SPY 1993〜"), ("N2", "jp", "TOPIX500 の今の銘柄の平均 2001〜")):
        n, d = nd[mkt]
        add(key, f"{MARKET[mkt]}: 夜だけ持つほうが昼だけ持つより良い（毎日 1 往復のコスト込み。{what}）",
            S.night_day_diff(n, d, COST[mkt]), "2010-01-01", "年率", 12.0)
        g = S.night_day_diff(n, d)
        if mkt == "us":
            full_cost(key, S.night_day_diff(n, d, S.COST_US_FULL), 12.0, "年率")
        notes.setdefault(key, []).append(
            f"コストを引く前: 夜 年 {pct(n.mean() * 252)}・昼 年 {pct(d.mean() * 252)}（2010 年から 夜 {pct(n['2010':].mean() * 252)}・"
            f"昼 {pct(d['2010':].mean() * 252)}）。差の t 値 {f2(S.t_value(g))}（2010 年から {f2(S.t_value(g['2010':]))}）。"
            f"毎日 1 往復しても夜が勝つのは、往復コストが {pct(max((n.mean() - d.mean()), 0), 3)} より小さいときだけ")
    for name, (n, d, c) in extra.items():
        g = S.night_day_diff(n, d)
        notes.setdefault("N 参考", []).append(
            f"{name}: 夜 年 {pct(n.mean() * 252)}・昼 年 {pct(d.mean() * 252)}、2010 年から 夜 {pct(n['2010':].mean() * 252)}・"
            f"昼 {pct(d['2010':].mean() * 252)}。コスト前の差の t 値 {f2(S.t_value(g))}")

    # ---- 業種
    print("業種…", flush=True)
    ind = french(root, "49_Industry_Portfolios", "Value Weight")
    mret = {m: complete_months(S.monthly_returns(closes[m])) for m in closes}
    jp_sec = S.group_returns(mret["jp"], uni["jp"]["sector"].to_dict())
    for key, label, ret, score, cost, split in (
            ("S1", "米国: 直近 6 か月に上がった業種は翌月も上がる（49 業種の上位 5）", ind, S.momentum_score(ind), S.COST_US, "2000-01-01"),
            ("S2", f"日本: 同じ（33 業種のうち 5 社以上の {jp_sec.shape[1]} 業種の上位 5）", jp_sec, S.momentum_score(jp_sec), S.COST_JP, "2013-01-01"),
            ("S3", "米国: 毎年その月に上がってきた業種は今年も上がる（49 業種の上位 5）", ind, S.seasonal_score(ind), S.COST_US, "2009-01-01")):
        res = S.top_n_active(ret, score, S.TOP_SECTORS, cost)
        add(key, label, res["d"], split, "年率", 12.0, S.top_n_placebo(ret, score, S.TOP_SECTORS, args.placebo), float(res["gross"].mean()))
        notes.setdefault(key, []).append(f"コストを引く前の差 年 {pct(res['gross'].mean() * 12)}、月に入れ替える割合 {pct(res['turnover'].mean(), 0)}")
        if key != "S2":
            full_cost(key, res["gross"] - res["turnover"] * S.COST_US_FULL, 12.0, "年率")
    names = lambda s: "、".join(f"{FRENCH_NAMES.get(k, k)}（{pct(v, 0)}）" for k, v in s.items())  # noqa: E731
    now["S1"] = [f"フレンチ 49 業種（{ind.index[-1]:%Y-%m} 末まで）の直近 6 か月の上位 5: {names(np.expm1(S.top_n_now(S.momentum_score(ind))))}"]
    now["S2"] = [f"東証 33 業種（{jp_sec.index[-1]:%Y-%m} 末まで）の直近 6 か月の上位 5: "
                 + "、".join(f"{k}（{pct(v, 0)}）" for k, v in np.expm1(S.top_n_now(S.momentum_score(jp_sec))).items())]
    for m in (11, 12, 1):
        s = ind[ind.index.month == m].tail(S.SEASON_YEARS).mean().sort_values(ascending=False).head(S.TOP_SECTORS)
        now.setdefault("S3", []).append(f"{m} 月に過去 20 年の平均が高い 5 業種: {names(s)}（月の平均）")
    for mkt, cost in (("us", S.COST_US), ("jp", S.COST_JP_SECTOR_ETF)):
        tick = [t for t in SECTOR_ETFS[mkt] if t in misc_c]
        r = complete_months(S.monthly_returns(misc_c[tick]))
        sc = S.momentum_score(r)
        res = S.top_n_active(r, sc, 3, cost)
        top = "、".join(f"{SECTOR_ETF_NAMES[k]}（{k.replace('.T', '')}、{pct(v, 0)}）" for k, v in np.expm1(S.top_n_now(sc, 3)).items())
        notes.setdefault("S 参考", []).append(
            f"{MARKET[mkt]}の業種 ETF {len(tick)} 本の上位 3（{res.index[0]:%Y-%m}〜）: コスト込みの差 年 {pct(res['d'].mean() * 12)}、"
            f"t 値 {f2(S.t_value(res['d']))}（2013 年から 年 {pct(res['d']['2013':].mean() * 12)}、t 値 {f2(S.t_value(res['d']['2013':]))}）。"
            f"今の上位 3: {top}")

    # ---- 会社（フレンチ教授の組）
    print("会社…", flush=True)
    ff, ffj = french(root, "F-F_Research_Data_Factors"), french(root, "Japan_3_Factors")
    mkt_us, mkt_jp = ff["Mkt-RF"] + ff["RF"], ffj["Mkt-RF"] + ffj["RF"]
    dec = french(root, "10_Portfolios_Prior_12_2", "Value Weight")
    jmom = french(root, "Japan_6_Portfolios_ME_Prior_12_2", "Value Weight")
    jval = french(root, "Japan_6_Portfolios_ME_BE-ME", "Value Weight")
    for key, label, port, mkt_r, turn, cost, split in (
            ("C1", "米国: 過去 1 年に上がった株は続けて上がる（上位 10% − 市場）", dec["Hi PRIOR"], mkt_us, 0.35, S.COST_US, "1994-01-01"),
            ("C2", "日本: 同じ（大型の上位 30% − 市場）", jmom["BIG HiPRIOR"], mkt_jp, 0.25, S.COST_JP, "2008-01-01"),
            ("C3", "日本: 割安株は上がる（大型の割安上位 30% − 市場）", jval["BIG HiBM"], mkt_jp, 0.05, S.COST_JP, "2008-01-01")):
        g = (port - mkt_r).dropna()
        add(key, label, g - turn * cost, split, "年率", 12.0)
        if key == "C1":
            full_cost(key, g - turn * S.COST_US_FULL, 12.0, "年率")
        notes.setdefault(key, []).append(f"コストを引く前の差 年 {pct(g.mean() * 12)}（{g.index[0]:%Y-%m}〜{g.index[-1]:%Y-%m}）。"
                                         f"直近 10 年は 年 {pct(g.tail(120).mean() * 12)}、直近 3 年は 年 {pct(g.tail(36).mean() * 12)}")

    # ---- 会社（配当の月）
    for key, mkt in (("C4", "us"), ("C5", "jp")):
        r = mret[mkt]
        ex = S.ex_month_flags(divs[mkt], r.index, r.columns)
        res = S.dividend_month_diff(r, ex, COST[mkt])
        add(key, f"{MARKET[mkt]}: 配当の月は、ほかの配当株より上がる", res["d"], "2014-01-01", "年率", 12.0,
            S.dividend_month_placebo(r, ex, args.placebo), float(res["gross"].mean()))
        if mkt == "us":
            full_cost(key, res["gross"] - S.COST_US_FULL, 12.0, "年率")
        notes.setdefault(key, []).append(f"コストを引く前の差 月 {pct(res['gross'].mean(), 2)}（年 {pct(res['gross'].mean() * 12)}）。"
                                         f"比べられた月 {len(res)}、配当の月の株は平均 {res['n_treat'].mean():.0f} 社")
        nxt = (last_day[mkt] + pd.offsets.MonthEnd(2)).normalize()            # 来月の月末（今月の末に選ぶ分）
        prev = nxt - pd.offsets.MonthEnd(12)
        full = S.ex_month_flags(divs[mkt], pd.DatetimeIndex([prev]), r.columns).iloc[0]
        picks = [t for t in full.index[full.to_numpy()]]
        show = "、".join(f"{uni[mkt]['name'].get(t, t)}（{t.replace('.T', '')}）" for t in picks[:25])
        now[key] = [f"{nxt:%Y-%m} が配当の月と見込まれる株 {len(picks)} 社" + (f": {show}" + (" ほか" if len(picks) > 25 else "") if picks else "")]

    # ---- 会社（決算の後）
    for key, mkt in (("C6", "us"), ("C7", "jp")):
        r = closes[mkt].pct_change(fill_method=None).clip(-S.CLIP, S.CLIP)
        mk = r.mean(axis=1)
        ev = S.pead_events(r, mk, earn[mkt])
        hold = S.pead_holdings(ev, ev["good"].to_numpy(), r.shape)
        res = S.pead_active(r, mk, hold, COST[mkt])
        null = S.pead_placebo(r, mk, ev, max(args.placebo // 5, 1)) if args.placebo else None
        add(key, f"{MARKET[mkt]}: 良い決算（予想を上回り、株価も上がった）の後 60 営業日は、ほかの株より上がる",
            S.month_sum(res["d"]), "2016-01-01", "年率", 12.0, null, float(res["gross"].mean()))
        on = res["names"] >= S.MIN_NAMES
        if mkt == "us":
            full_cost(key, S.month_sum(res["gross"] - S.COST_US_FULL * res["turnover"]), 12.0, "年率")
        bad = S.pead_active(r, mk, S.pead_holdings(ev, ((ev["reaction"] < 0) & (ev["surprise"] < 0)).to_numpy(), r.shape))
        notes.setdefault(key, []).append(
            f"決算 {len(ev)} 回（{ev['ticker'].nunique()} 銘柄、{r.index[ev['i1'].min()]:%Y-%m}〜）のうち良い決算 {pct(ev['good'].mean(), 0)}。"
            f"持っている株は平均 {res['names'][on].mean():.0f} 社。コストを引く前の差 年 {pct(res['gross'][on].mean() * 252)}、"
            f"1 日に入れ替える割合 {pct(res['turnover'][on].mean(), 1)}。逆に悪い決算（予想を下回り、株価も下がった）の後は 年 "
            f"{pct(bad['gross'][bad['names'] >= S.MIN_NAMES].mean() * 252)}")
        t_last = len(r) - 1
        cur = ev[ev["good"] & (ev["i1"] <= t_last) & (ev["end"] > t_last)].sort_values("i1", ascending=False)
        rows = [f"{uni[mkt]['name'].get(x.ticker, x.ticker)}（{x.ticker.replace('.T', '')}）{r.index[x.i1 - 1]:%m/%d} 発表・"
                f"反応 {x.reaction:+.1%}・残り {x.i1 + S.PEAD_HOLD - t_last} 日" for x in cur.head(30).itertuples()]
        now[key] = [f"{last_day[mkt]:%Y-%m-%d} の時点で条件に当てはまる株 {len(cur)} 社（新しい順に 30 社まで）"] + [f"  - {x}" for x in rows]

    # ---- 参考: 1 時間ごと
    print("1 時間足…", flush=True)
    hourly_text: list[str] = []
    for mkt in ("jp", "us"):
        folder = root / "hourly" / mkt
        dv_all = pd.concat([divs[mkt], misc_div])
        frames, etf_lines = [], []
        for path in sorted(folder.glob("*.csv")):
            t = path.stem
            b = pd.read_csv(path, parse_dates=["time"]).set_index("time")
            dv = dv_all[dv_all["ticker"] == t]
            dser = pd.Series(dv["amount"].to_numpy(), index=dv["date"].dt.date) if len(dv) else None
            if t in uni[mkt].index:
                frames.append(S.hourly_returns(b, dser))
            else:
                m = intraday_momentum(b)
                etf_lines.append(f"{t.replace('.T', '')}: 朝の向きに合わせて最後の足を持つと 1 日あたり {pct(m['mean'], 3)}（t 値 {f2(m['t'])}、"
                                 f"相関 {f2(m['corr'])}、{m['days']} 日）")
        if not frames:
            continue
        prof = S.profile_table(frames)
        order = ["夜"] + sorted(c for c in prof.index if c not in ("夜", "昼休み"))
        if "昼休み" in prof.index:
            order.insert(order.index("12:00"), "昼休み")
        hourly_text.append(f"\n**{MARKET[mkt]}（大型株 {len(frames)} 銘柄の平均、{frames[0].index[0]}〜{frames[0].index[-1]}、その取引所の時刻）**\n")
        hourly_text.append("| 時間帯（足の始まり） | 1 日あたりの平均 | 年に直すと | t 値 | 日数 |")
        hourly_text.append("|---|---|---|---|---|")
        for c in order:
            x = prof.loc[c]
            hourly_text.append(f"| {c} | {pct(x['mean'], 3)} | {pct(x['mean'] * 252)} | {f2(x['t'])} | {int(x['days'])} |")
        hourly_text.append("")
        hourly_text += [f"- {x}" for x in etf_lines]

    # ---- 報告
    say("# 上がりやすい株を探す（時期・時間帯・業種・会社）")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　仮説と判定の条件: docs/stocks.md（結果を見る前に決めたもの）")
    say(f"データ: 個別株は日本 {closes['jp'].shape[1]} 銘柄（〜{last_day['jp']:%Y-%m-%d}）・米国 {closes['us'].shape[1]} 銘柄（〜{last_day['us']:%Y-%m-%d}）、"
        f"フレンチ教授のデータ集は 〜{ind.index[-1]:%Y-%m}。休場日なのに前日の値のまま入っていた日（日本 {dropped['jp']} 日・米国 {dropped['us']} 日）は除いた")
    say(f"コスト（往復）: 日本株 {pct(S.COST_JP, 2)}・米国株 {pct(S.COST_US, 2)}。試した数: {S.PRIOR_TRIALS} ＋ {S.NEW_TRIALS} ＝ {n_trials}"
        f"（全期間の t 値の基準 {f2(S.expected_max_z(n_trials))}）")
    say("\n## 1. 判定の一覧\n")
    say("差 = コストを引いた後の「条件に合うもの − 比べる相手」。プラスなら仮説どおり。t 値は 1.65 以上で「偶然とは言いにくい」。\n")
    say("| 記号 | 仮説 | 期間 | 差（全期間） | 前半 | 後半 | t 値（全期間） | t 値（後半） | 偶然より良い割合 | 判定 |")
    say("|---|---|---|---|---|---|---|---|---|---|")
    for j in results:
        digits = 3 if j["unit"] == "1 日あたり" else 1
        sc = j["scale"]
        say(f"| {j['key']} | {j['label']} | {j['start']:%Y}〜{j['end']:%Y}（後半 {j['split'][:4]}〜） | "
            f"{j['unit']} {pct(j['mean'] * sc, digits)} | {pct(j['mean1'] * sc, digits)} | {pct(j['mean2'] * sc, digits)} | "
            f"{f2(j['t'])} | {f2(j['t2'])} | {pct(j['placebo'], 0) if j['placebo'] is not None else '（比べる相手なし）'} | **{j['verdict']}** |")
    by = {v: [j["key"] for j in results if j["verdict"] == v] for v in ("使えそう", "まだ判断できない", "使えない")}
    say("")
    for v, keys in by.items():
        say(f"- {v}: {'、'.join(keys) if keys else 'なし'}")

    say("\n## 2. 仮説ごとの細かい数字\n")
    for j in results:
        for x in notes.get(j["key"], []):
            say(f"- {j['key']}: {x}")
    say("\n参考（判定しない）\n")
    for k in ("N 参考", "S 参考"):
        for x in notes.get(k, []):
            say(f"- {x}")
    say("\n参考: 米国のコストを往復 1.0%（特定口座のふつうの手数料）にした場合\n")
    for x in notes.get("US 1.0%", []):
        say(f"- {x}")
    say("\n## 3. 参考: 1 時間ごとの平均（直近 2 年あまり。短いので判定には使わない）")
    for x in hourly_text:
        say(x)

    say("\n## 4. 今あてはまるもの\n")
    say("「使えそう」と判定された規則だけを上に、「まだ判断できない」は参考として下に並べる。上がることの約束ではない（docs/stocks.md 7 章）。\n")
    cal = {"T1": "米国", "T2": "日本"}
    nb = pd.bdate_range(last_day["jp"] + pd.offsets.MonthBegin(1), periods=3)
    le = pd.bdate_range(end=last_day["jp"] + pd.offsets.MonthEnd(0), periods=1)[0]
    for key, m in cal.items():
        now[key] = [f"{m}: 次の月替わりは {le:%m/%d}（月の最後の営業日）と {nb[0]:%m/%d}〜{nb[-1]:%m/%d}（祝日があれば 1 日ずつ後ろにずれる）"]
    now["T3"] = now["T4"] = ["11 月〜翌 4 月が「良いとされる半年」。今年は 10 月末の引けで買い、4 月末の引けで売る形"]
    now["T5"] = ["1〜6 月が「良いとされる半年」。12 月末の引けで買い、6 月末の引けで売る形"]
    now["N1"] = now["N2"] = ["買うなら引け、売るなら翌朝の寄り、が目安"]
    for mkt, key in (("us", "C1"), ("jp", "C2")):
        c = mret[mkt]
        mom = np.expm1(np.log1p(c.iloc[-12:-1]).sum(min_count=11)).dropna().sort_values(ascending=False)
        top = mom.head(max(len(mom) // 10, 1))
        now[key] = [f"{c.index[-1]:%Y-%m} 末の時点で、過去 12〜2 か月のリターンが上位 10% の {len(top)} 社（上から 25 社）: "
                    + "、".join(f"{uni[mkt]['name'].get(t, t)}（{t.replace('.T', '')}、{pct(v, 0)}）" for t, v in top.head(25).items())]
    val = pd.read_csv(root / "jp_value.csv").dropna(subset=["pb"])
    val = val[val["pb"] >= 0.1].sort_values("pb")                          # 0.1 倍未満は Yahoo の値の誤りとみなす
    cheap = val.head(int(len(val) * 0.3))
    now["C3"] = [f"TOPIX500 のうち株価 ÷ 純資産（PBR）が低い側 30% の {len(cheap)} 社（PBR {cheap['pb'].max():.2f} 倍以下。低い順に 25 社）: "
                 + "、".join(f"{uni['jp']['name'].get(x.ticker, x.ticker)}（{x.ticker.replace('.T', '')}、{x.pb:.2f} 倍）" for x in cheap.head(25).itertuples()),
                 "業種ごとの社数: " + "、".join(f"{k} {v}" for k, v in uni["jp"]["sector"].reindex(cheap["ticker"]).value_counts().head(8).items()),
                 "PBR は Yahoo の値で、株式分割の直後などは誤りがある。買う前に証券会社の画面で確かめる"]
    for title, verdict in (("### 使えそう", "使えそう"), ("### まだ判断できない（参考）", "まだ判断できない")):
        say(title + "\n")
        keys = by[verdict]
        if not keys:
            say("- なし\n")
        for k in keys:
            label = next(j["label"] for j in results if j["key"] == k)
            say(f"**{k} {label}**\n")
            for x in now.get(k, ["（一覧なし）"]):
                say(x if x.startswith("  - ") else f"- {x}")
            say("")

    say("## 5. 読み方・限界\n")
    say("- 判定は 4 つの条件すべて（前半・後半ともプラス、後半だけで t 値 1.65 以上、全期間の t 値が試した数で割り引いた基準以上、偶然より良い割合 95% 以上）")
    say("- 個別株は今の構成銘柄（つぶれた会社・外れた会社が入らない）。同じ銘柄群の中での比べ合いにしているが、少し良く出る側に偏りうる")
    say("- 指数は配当を含まない。フレンチ教授の日本の組はドル建て。税金は入れていない")
    say("- 「今あてはまるもの」は条件に当てはまるというだけ。1 銘柄に集中せず、同じ条件の銘柄を広く持つ形でしか統計は当てはまらない")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    pd.DataFrame(results).drop(columns=["start", "end"]).to_csv(out / "verdicts.csv", index=False)
    print(f"\n保存先: {out}/report.md")


if __name__ == "__main__":
    main()
