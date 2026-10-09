"""先行指標（商品・為替・金利・米国株 → 日米のセクター株・個別株）の研究（./cfd leadlag）。

例: 原油が上がると、原料が高くなる化学株や燃料が高くなる航空株が、少し遅れて下がるか。
  - 部 A: 結果を見る前に決めた「先行指標 → 株、向き」の仮説ごとに、遅れて効くかを確かめる
  - 部 B: 機械学習（cfdbot/ml.py と同じ形）に、先行指標の入力を足すと良くなるかを確かめる

先読みを防ぐため、株の判断はその取引所の引け（東京 15:00・ニューヨーク 16:00）とし、
先行指標はその時刻までに日付が終わったもの（その取引所の時刻で翌日 0:00 以降）だけを使う。
例えば東京の D+1 日の判断には、ニューヨークの D 日の原油・S&P500 まで使える（D+1 日の分は使わない）。
設定と判定の条件は docs/leadlag.md（結果を見る前に決めたもの）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .ml import MAX_LEVERAGE, TARGET_VOL, Panel, market_frame


@dataclass(frozen=True)
class Driver:
    key: str
    ticker: str
    kind: str        # "price"（変化は比率）/ "yield"（変化は差）
    label: str


@dataclass(frozen=True)
class Target:
    key: str
    ticker: str
    group: str       # "us_sector" / "jp_sector" / "jp_stock" / "us_stock"
    label: str


DRIVERS: tuple[Driver, ...] = (
    Driver("oil", "CL=F", "price", "WTI 原油"),
    Driver("natgas", "NG=F", "price", "天然ガス"),
    Driver("copper", "HG=F", "price", "銅"),
    Driver("gold", "GC=F", "price", "金"),
    Driver("usdjpy", "JPY=X", "price", "ドル円"),
    Driver("us10y", "^TNX", "yield", "米 10 年金利"),
    Driver("spx", "^GSPC", "price", "S&P500"),
)

_T = Target
TARGETS: tuple[Target, ...] = (
    # 米国のセクター ETF
    _T("XLE", "XLE", "us_sector", "米エネルギー"), _T("XLB", "XLB", "us_sector", "米素材（化学を含む）"),
    _T("XLI", "XLI", "us_sector", "米資本財"), _T("XLY", "XLY", "us_sector", "米一般消費財"),
    _T("XLP", "XLP", "us_sector", "米生活必需品"), _T("XLU", "XLU", "us_sector", "米公益"),
    _T("XLF", "XLF", "us_sector", "米金融"), _T("XLK", "XLK", "us_sector", "米情報技術"),
    _T("XLV", "XLV", "us_sector", "米ヘルスケア"), _T("XLRE", "XLRE", "us_sector", "米不動産"),
    _T("XLC", "XLC", "us_sector", "米通信"),
    # 日本の TOPIX-17 業種 ETF（NEXT FUNDS）
    _T("JP_FOOD", "1617.T", "jp_sector", "食品"), _T("JP_ENERGY", "1618.T", "jp_sector", "エネルギー資源"),
    _T("JP_CONSTR", "1619.T", "jp_sector", "建設・資材"), _T("JP_CHEM", "1620.T", "jp_sector", "素材・化学"),
    _T("JP_PHARMA", "1621.T", "jp_sector", "医薬品"), _T("JP_AUTO", "1622.T", "jp_sector", "自動車・輸送機"),
    _T("JP_STEEL", "1623.T", "jp_sector", "鉄鋼・非鉄"), _T("JP_MACH", "1624.T", "jp_sector", "機械"),
    _T("JP_ELEC", "1625.T", "jp_sector", "電機・精密"), _T("JP_IT", "1626.T", "jp_sector", "情報通信・サービス"),
    _T("JP_POWER", "1627.T", "jp_sector", "電力・ガス"), _T("JP_TRANS", "1628.T", "jp_sector", "運輸・物流"),
    _T("JP_TRADE", "1629.T", "jp_sector", "商社・卸売"), _T("JP_RETAIL", "1630.T", "jp_sector", "小売"),
    _T("JP_BANK", "1631.T", "jp_sector", "銀行"), _T("JP_FIN", "1632.T", "jp_sector", "金融（除く銀行）"),
    _T("JP_REIT", "1633.T", "jp_sector", "不動産"),
    # 日本の個別株（原油・為替・金利の影響を受けやすい業種）
    _T("SHINETSU", "4063.T", "jp_stock", "信越化学"), _T("SUMICHEM", "4005.T", "jp_stock", "住友化学"),
    _T("MCHEM", "4188.T", "jp_stock", "三菱ケミカルG"), _T("ASAHIKASEI", "3407.T", "jp_stock", "旭化成"),
    _T("MITSUICHEM", "4183.T", "jp_stock", "三井化学"), _T("ANA", "9202.T", "jp_stock", "ANA"),
    _T("JAL", "9201.T", "jp_stock", "JAL"), _T("ENEOS", "5020.T", "jp_stock", "ENEOS"),
    _T("IDEMITSU", "5019.T", "jp_stock", "出光興産"), _T("INPEX", "1605.T", "jp_stock", "INPEX"),
    _T("MITSUBISHI", "8058.T", "jp_stock", "三菱商事"), _T("MITSUI", "8031.T", "jp_stock", "三井物産"),
    _T("NSTEEL", "5401.T", "jp_stock", "日本製鉄"), _T("NYK", "9101.T", "jp_stock", "日本郵船"),
    _T("TOYOTA", "7203.T", "jp_stock", "トヨタ"), _T("MUFG", "8306.T", "jp_stock", "三菱UFJ"),
    _T("TEPCO", "9501.T", "jp_stock", "東京電力HD"),
    # 米国の個別株
    _T("DOW_CHEM", "DOW", "us_stock", "ダウ（化学）"), _T("LYB", "LYB", "us_stock", "ライオンデルバセル（化学）"),
    _T("EMN", "EMN", "us_stock", "イーストマン（化学）"), _T("DAL", "DAL", "us_stock", "デルタ航空"),
    _T("UAL", "UAL", "us_stock", "ユナイテッド航空"), _T("LUV", "LUV", "us_stock", "サウスウエスト航空"),
    _T("VLO", "VLO", "us_stock", "バレロ（石油精製）"), _T("XOM", "XOM", "us_stock", "エクソン"),
    _T("CVX", "CVX", "us_stock", "シェブロン"), _T("FCX", "FCX", "us_stock", "フリーポート（銅）"),
    _T("NEM", "NEM", "us_stock", "ニューモント（金）"), _T("JPM", "JPM", "us_stock", "JPモルガン"),
)
TARGET_BY_KEY = {t.key: t for t in TARGETS}
DRIVER_BY_KEY = {d.key: d for d in DRIVERS}
GROUP_LABELS = {"us_sector": "米セクター", "jp_sector": "日本の業種", "jp_stock": "日本の個別株", "us_stock": "米国の個別株"}
GROUP_COST = {"us_sector": (0.0005, 0.03), "jp_sector": (0.002, 0.03), "jp_stock": (0.0015, 0.03),
              "us_stock": (0.0015, 0.03)}      # (往復コスト, 保有コスト 年率)
CLOSE_HOUR = {"America/New_York": 16.0, "Asia/Tokyo": 15.0}

# 部 A: 結果を見る前に決めた仮説（先行指標, 株, 向き）。向き +1 は「先行指標が上がると、後で株も上がる」
HYPOTHESES: tuple[tuple[str, str, int, str], ...] = (
    ("oil", "JP_CHEM", -1, "原油高 → 原料高で化学が下がる"),
    ("oil", "SHINETSU", -1, "同上（信越化学）"), ("oil", "SUMICHEM", -1, "同上（住友化学）"),
    ("oil", "MCHEM", -1, "同上（三菱ケミカル）"), ("oil", "ASAHIKASEI", -1, "同上（旭化成）"),
    ("oil", "MITSUICHEM", -1, "同上（三井化学）"), ("oil", "DOW_CHEM", -1, "同上（ダウ）"),
    ("oil", "LYB", -1, "同上（ライオンデルバセル）"), ("oil", "EMN", -1, "同上（イーストマン）"),
    ("oil", "ANA", -1, "原油高 → 燃料高で航空が下がる"), ("oil", "JAL", -1, "同上（JAL）"),
    ("oil", "DAL", -1, "同上（デルタ）"), ("oil", "UAL", -1, "同上（ユナイテッド）"), ("oil", "LUV", -1, "同上（サウスウエスト）"),
    ("oil", "JP_TRANS", -1, "原油高 → 運輸・物流が下がる"), ("oil", "JP_AUTO", -1, "原油高 → 自動車が下がる"),
    ("oil", "XLY", -1, "原油高 → 米一般消費財が下がる"),
    ("oil", "XLE", +1, "原油高 → エネルギー株が上がる"), ("oil", "JP_ENERGY", +1, "同上（日本）"),
    ("oil", "INPEX", +1, "同上（INPEX）"), ("oil", "ENEOS", +1, "原油高 → 在庫の評価益で石油元売りが上がる"),
    ("oil", "IDEMITSU", +1, "同上（出光）"), ("oil", "JP_TRADE", +1, "原油高 → 資源の商社が上がる"),
    ("oil", "XOM", +1, "原油高 → 石油大手が上がる"), ("oil", "CVX", +1, "同上（シェブロン）"),
    ("copper", "JP_STEEL", +1, "銅高 → 鉄鋼・非鉄が上がる"), ("copper", "FCX", +1, "銅高 → 銅鉱山が上がる"),
    ("copper", "XLB", +1, "銅高 → 米素材が上がる"),
    ("gold", "NEM", +1, "金高 → 金鉱山が上がる"),
    ("usdjpy", "JP_AUTO", +1, "円安 → 輸出の自動車が上がる"), ("usdjpy", "JP_ELEC", +1, "円安 → 電機・精密が上がる"),
    ("usdjpy", "JP_MACH", +1, "円安 → 機械が上がる"), ("usdjpy", "JP_RETAIL", -1, "円安 → 輸入コスト高で小売が下がる"),
    ("us10y", "JP_BANK", +1, "金利上昇 → 銀行が上がる"), ("us10y", "XLF", +1, "同上（米金融）"),
    ("us10y", "JPM", +1, "同上（JPモルガン）"), ("us10y", "XLU", -1, "金利上昇 → 公益が下がる"),
    ("us10y", "XLRE", -1, "金利上昇 → 不動産が下がる"), ("us10y", "JP_REIT", -1, "同上（日本の不動産）"),
    ("natgas", "TEPCO", -1, "天然ガス高 → 燃料費で電力が下がる"), ("natgas", "JP_POWER", -1, "同上（電力・ガス）"),
)
HORIZONS = (5, 21)    # 先行指標の直近 h 日の動きで、株の次の h 日を見る（1 週・1 か月）


def read_yahoo(path: str | Path) -> tuple[pd.Series, str]:
    """Yahoo の日足 CSV → 終値（日付の index）とタイムゾーン。"""
    df = pd.read_csv(path).dropna(subset=["close"])
    df = df[df["close"] > 0]
    s = pd.Series(df["close"].to_numpy(float), index=pd.DatetimeIndex(pd.to_datetime(df["date"])))
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s, str(df["tz"].iloc[-1]) if "tz" in df and len(df) else "America/New_York"


def end_of_day_ns(dates: pd.DatetimeIndex, tz: str) -> np.ndarray:
    """その日がその取引所の時刻で終わった時（翌日 0:00）。先行指標が使えるようになる時刻（控えめ）。"""
    t = (dates + pd.Timedelta(days=1)).tz_localize(tz, nonexistent="shift_forward", ambiguous=False)
    return t.tz_convert("UTC").as_unit("ns").asi8.astype(np.int64)


def close_ns(dates: pd.DatetimeIndex, tz: str) -> np.ndarray:
    """株の判断の時刻（その取引所の引け）。"""
    h = CLOSE_HOUR.get(tz, 16.0)
    t = (dates + pd.Timedelta(hours=h)).tz_localize(tz, nonexistent="shift_forward", ambiguous=False)
    return t.tz_convert("UTC").as_unit("ns").asi8.astype(np.int64)


def change(values: np.ndarray, k: int, kind: str) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) > k:
        out[k:] = values[k:] / values[:-k] - 1 if kind == "price" else values[k:] - values[:-k]
    return out


@dataclass
class DriverSeries:
    key: str
    kind: str
    avail: np.ndarray        # 使えるようになる時刻（ns、昇順）
    values: np.ndarray
    sigma: np.ndarray        # 1 日の変化の大きさ（指数平滑の標準偏差）

    @classmethod
    def from_series(cls, key: str, kind: str, s: pd.Series, tz: str) -> "DriverSeries":
        v = s.to_numpy(float)
        sig = pd.Series(change(v, 1, kind)).ewm(span=60, min_periods=60).std().to_numpy()
        return cls(key, kind, end_of_day_ns(s.index, tz), v, sig)

    def asof(self, arr: np.ndarray, decide: np.ndarray) -> np.ndarray:
        k = np.searchsorted(self.avail, decide, side="right") - 1
        out = np.full(len(decide), np.nan)
        ok = k >= 0
        out[ok] = arr[k[ok]]
        return out

    def raw(self, k: int, decide: np.ndarray) -> np.ndarray:
        """判断の時刻までに使える、直近 k 日の変化（比率か差）。"""
        return self.asof(change(self.values, k, self.kind), decide)

    def normalized(self, k: int, decide: np.ndarray) -> np.ndarray:
        with np.errstate(invalid="ignore", divide="ignore"):
            return self.asof(change(self.values, k, self.kind) / (self.sigma * np.sqrt(k)), decide)


def shifted_driver(d: DriverSeries, frac: float) -> DriverSeries:
    """偶然との比較用: 先行指標の値の並びを時期だけずらす（使える時刻はそのまま。株との時間の関係だけ壊す）。"""
    k = int(len(d.values) * frac)
    return DriverSeries(d.key, d.kind, d.avail, np.roll(d.values, k), np.roll(d.sigma, k))


def target_frame(close: pd.Series, tz: str, drivers: dict[str, DriverSeries], lags=(1, 5, 21),
                 beta_window: int = 252) -> pd.DataFrame:
    """株 1 銘柄の、日ごとの入力（自分の値動き＋先行指標）・翌日のリターン・値動きの大きさ。

    先行指標の入力は 2 種類:
      drv_<d>_<k>: 先行指標の直近 k 日の変化 ÷（その大きさ × √k）
      bw_<d>_<k>:  上を、その株が先行指標にどれだけ連れて動いてきたか（直近 1 年の 5 日リターンの回帰の傾き）で
                   掛けたもの ÷（株の値動き × √k）。「株がまだ追いついていない分」の目安
    """
    f = market_frame(close)
    decide = close_ns(close.index, tz)
    r = close.pct_change()
    sig = f["sigma"].to_numpy()
    r5 = (close / close.shift(5) - 1).to_numpy()
    for key, d in drivers.items():
        d5 = d.raw(5, decide)
        beta = (pd.Series(r5).rolling(beta_window, min_periods=beta_window // 2).cov(pd.Series(d5))
                / pd.Series(d5).rolling(beta_window, min_periods=beta_window // 2).var()).to_numpy()
        for k in lags:
            f[f"drv_{key}_{k}"] = np.clip(d.normalized(k, decide), -5, 5)
            with np.errstate(invalid="ignore", divide="ignore"):
                f[f"bw_{key}_{k}"] = np.clip(beta * d.raw(k, decide) / (sig * np.sqrt(k)), -5, 5)
    f["decide_ns"] = decide
    f["r1"] = r
    return f


def driver_features(drivers, lags=(1, 5, 21)) -> list[str]:
    return [f"{p}_{d}_{k}" for d in drivers for k in lags for p in ("drv", "bw")]


def build_panel(frames: dict[str, pd.DataFrame], groups: dict[str, str], features: list[str],
                start: str | None = None) -> Panel:
    """target_frame の結果を ml.Panel にする（cfdbot/ml.py の学習・評価がそのまま使える）。"""
    parts, keys = [], list(frames)
    for mi, k in enumerate(keys):
        f = frames[k]
        f = f[f["sigma"].notna() & f["next_ret"].notna()]
        if start is not None:
            f = f[f.index >= pd.Timestamp(start)]
        if f.empty:
            continue
        rt, fin = GROUP_COST[groups[k]]
        parts.append(f.assign(_m=mi, _cost=rt / 2, _fin=fin / 252))
    df = pd.concat(parts)
    x = df.reindex(columns=features).to_numpy(np.float32)
    x = np.where(np.isfinite(x), x, 0.0).astype(np.float32)
    lev = np.minimum(TARGET_VOL / df["sigma"].to_numpy(float), MAX_LEVERAGE)
    m = df["_m"].to_numpy(int)
    return Panel(x, df["next_ret"].to_numpy(float), lev, df["_cost"].to_numpy(float), df["_fin"].to_numpy(float),
                 m, df.index.to_numpy(dtype="datetime64[ns]"), keys, np.r_[True, m[1:] != m[:-1]])


# --------------------------------------------------------------------------- 部 A
def hypothesis_returns(frame: pd.DataFrame, d: DriverSeries, sign: int, h: int, cost: tuple[float, float]) -> pd.Series:
    """仮説の売買: 先行指標の直近 h 日の向き × 仮説の向きで、株を h 日持つ（毎日 1/h ずつ入れ替える）。

    値動きの大きさでそろえた（年率 15%）日ごとの損益（コスト込み）。index は株の日付。
    """
    decide = frame["decide_ns"].to_numpy()
    sig_d = np.sign(d.raw(h, decide))
    sig_d = np.where(np.isfinite(sig_d), sig_d, 0.0) * sign
    pos = pd.Series(sig_d).rolling(h, min_periods=1).mean().to_numpy()       # h 日分を重ねて持つ
    lev = np.minimum(TARGET_VOL / frame["sigma"].to_numpy(float), MAX_LEVERAGE)
    u = np.where(np.isfinite(lev), pos * lev, 0.0)
    nr = frame["next_ret"].to_numpy(float)
    prev = np.r_[0.0, u[:-1]]
    rt, fin = cost
    pnl = u * np.nan_to_num(nr) - rt / 2 * np.abs(u - prev) - fin / 252 * np.abs(u)
    ok = np.isfinite(nr) & np.isfinite(frame["sigma"].to_numpy(float))
    return pd.Series(pnl[ok], index=frame.index[ok])
