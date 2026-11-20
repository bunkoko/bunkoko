"""リスクとお金の置き方の点検（docs/risk_plan.md 8 章。結果を見る前に決めたもの）。判定はしない・試した数に入れない。

8.1 持つ年数ごとの元本割れ: 指数を N 年持った（まとめて買う・毎月積み立てる）とき、元本を割って終わった割合。
8.2 戻し方 3 通りの比べ: 株と「元本が動かない置き場所」を決めた割合で 20 年持つとき、放置・年 1 回戻す・幅を超えたら戻す、の下げと割合のぶれ。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .wide import base

YEARS = (1, 3, 5, 10, 15, 20)                  # 持つ年数（8.1）
INDEXES = {"^GSPC": ("S&P500（米国。ドル建て・配当なし）", "1950-01-01"), "^N225": ("日経平均（日本。円建て・配当なし）", "1965-01-01")}
SHARES = (0.3, 0.5, 0.7)                       # 株の割合（8.2）
WAYS = {"drift": "放置（1 度も戻さない）", "yearly": "年 1 回、12 月末に必ず戻す", "band": "年 1 回見て、10 ポイントより外れていたら戻す"}
WINDOW_YEARS, BAND, STOCK = 20, 0.10, "^GSPC"
FILE = Path("stocks") / "misc_close.csv"       # data の下（./cfd stocks --fetch が取る。日付 × 記号）


# --------------------------------------------------------------------------- データ
def read_index(data, ticker: str) -> pd.Series:
    """指数の日ごとの終値。無い・形が違うなら NotReady（取りに行かない）。"""
    p = Path(data) / FILE
    if not p.exists():
        raise base.NotReady(f"{p} が無い（./cfd stocks --fetch）")
    try:
        t = pd.read_csv(p, index_col=0)
    except Exception as e:
        raise base.NotReady(f"{p} を読めない（{type(e).__name__}）") from e
    if ticker not in t.columns:
        raise base.NotReady(f"{p} に {ticker} の列が無い（./cfd stocks --fetch）")
    idx = pd.to_datetime(pd.Index(t.index.astype(str)).str[:10], format="%Y-%m-%d", errors="coerce")    # 時刻や時差が付いていても、日付で見る
    if idx.isna().any() or not pd.api.types.is_numeric_dtype(t[ticker]):
        raise base.NotReady(f"{p} の日付か {ticker} の値に、読めないものがある")
    c = pd.Series(t[ticker].to_numpy(dtype=float), index=idx.normalize()).dropna().sort_index()
    if c.empty or not c.index.is_unique or (c <= 0).any():
        raise base.NotReady(f"{p} の {ticker} が空・同じ日が 2 回ある・0 以下の値がある、のどれか")
    return c


def month_ends(daily: pd.Series) -> pd.Series:
    """日ごとの終値 → 月末の終値（月の最後の営業日の値）。最後の日の属する月は終わっていないので落とす。月が抜けていたら NotReady。"""
    s = pd.Series(daily).dropna().sort_index()
    if s.empty:
        return s
    m = base.finished_months(s.groupby(pd.DatetimeIndex(s.index).map(base.month_end)).last(), s.index[-1])
    if len(m) and len(m) != len(pd.period_range(m.index[0], m.index[-1], freq="M")):
        raise base.NotReady("月末の終値に、抜けている月がある")
    return m


# --------------------------------------------------------------------------- 8.1 持つ年数ごとの元本割れ
def lump(m: pd.Series, years: int) -> pd.Series:
    """まとめて買って N 年持ったときの「終わりの値 ÷ 始めの値」（始めた月ごと）。"""
    return (m.shift(-12 * years) / m).dropna()


def saving(m: pd.Series, years: int) -> pd.Series:
    """毎月末に同じ額を N 年（12N 回）積み立てたときの「終わりの値 ÷ 入れた合計」（始めた月ごと。最後に入れた月の次の月末で見る）。"""
    n = 12 * years
    ratio = ((1.0 / m).rolling(n).sum().shift(1) * m / n).dropna()      # 行 j: j−n〜j−1 の月に買った口数 × j の月末の値
    return pd.Series(ratio.to_numpy(), index=m.index[: len(ratio)])


def shortfall(ratio: pd.Series) -> dict:
    """元本を割って終わった割合・いちばん悪かった回・真ん中。"""
    if ratio.empty:
        return {"n": 0}
    below = int((ratio < 1.0).sum())
    return {"n": len(ratio), "below": below, "share": below / len(ratio), "worst": float(ratio.min()), "worst_start": ratio.idxmin(),
            "median": float(ratio.median())}


# --------------------------------------------------------------------------- 8.2 戻し方 3 通り
def mix_path(r: pd.Series, share: float, how: str, band: float = BAND) -> tuple[pd.Series, pd.Series]:
    """株（月のリターン r）と、利息 0 の置き場所を share で持ち始めたときの、月末ごとの資産（始め 1）と株の割合。

    how: "drift" 放置／"yearly" 12 月末に必ず戻す／"band" 12 月末に、株の割合が share から band より外れていたら戻す。
    """
    if how not in WAYS:
        raise ValueError(f"戻し方 {how} は無い（{list(WAYS)} から）")
    stock, cash = share, 1.0 - share
    values, shares = [], []
    for ts, x in r.items():
        stock *= 1.0 + x
        total = stock + cash
        if ts.month == 12 and (how == "yearly" or (how == "band" and abs(stock / total - share) > band)):
            stock, cash = total * share, total * (1.0 - share)
        values.append(total)
        shares.append(stock / total)
    return pd.Series(values, index=r.index), pd.Series(shares, index=r.index)


def windows(m: pd.Series, share: float, how: str, years: int = WINDOW_YEARS, band: float = BAND) -> pd.DataFrame:
    """毎年 1 月の初め（前の年の 12 月末の値）から years 年持った回ごとの、最大の下げ（月末の値で）・終わりの株の割合・年率。"""
    r = m.pct_change()
    rows = {}
    for start in m.index[m.index.month == 12]:
        end = base.month_end(start + pd.DateOffset(years=years))
        if end not in m.index:
            continue
        value, held = mix_path(r[(r.index > start) & (r.index <= end)], share, how, band)
        peak = np.maximum.accumulate(np.concatenate([[1.0], value.to_numpy()]))[1:]
        rows[start.year + 1] = {"max_dd": float((1.0 - value.to_numpy() / peak).max()), "end_share": float(held.iloc[-1]),
                                "annual": float(value.iloc[-1] ** (1.0 / years) - 1.0)}
    return pd.DataFrame.from_dict(rows, orient="index", columns=["max_dd", "end_share", "annual"])


def summary(w: pd.DataFrame) -> dict:
    """回ごとの数字の、真ん中と最悪。"""
    if w.empty:
        return {"n": 0}
    return {"n": len(w), "dd_median": float(w["max_dd"].median()), "dd_worst": float(w["max_dd"].max()), "dd_worst_start": int(w["max_dd"].idxmax()),
            "share_min": float(w["end_share"].min()), "share_max": float(w["end_share"].max()), "annual_median": float(w["annual"].median())}


# --------------------------------------------------------------------------- まとめて計算・報告
def run(data) -> dict:
    """2 つの点検を計算する。指数のデータが無ければ、その指数は理由だけ返す。"""
    out: dict = {"hold": {}, "skipped": {}, "mix": {}}
    monthly = {}
    for ticker, (label, since) in INDEXES.items():
        try:
            m = month_ends(read_index(data, ticker)[since:])
        except base.NotReady as e:
            out["skipped"][label] = str(e)
            continue
        if len(m) < 12 * min(YEARS) + 2:
            out["skipped"][label] = "月末の終値が 1 年ぶんに届かない"
            continue
        monthly[ticker] = m
        out["hold"][label] = {"start": m.index[0], "end": m.index[-1],
                              "rows": {n: {"lump": shortfall(lump(m, n)), "saving": shortfall(saving(m, n))} for n in YEARS}}
    if STOCK in monthly:
        m = monthly[STOCK]
        out["mix"] = {"start": m.index[0], "end": m.index[-1],
                      "rows": {s: {how: summary(windows(m, s, how)) for how in WAYS} for s in SHARES}}
    return out


def _pct(x, digits: int = 0) -> str:
    return "–" if x is None or not np.isfinite(x) else f"{x * 100:.{digits}f}%".replace("-", "−")


def _change(ratio: float) -> str:
    """終わりの値 ÷ 入れた額 → 増えた・減った割合（符号つき）。"""
    return f"{(ratio - 1) * 100:+.0f}%".replace("-", "−")


def _share(s: dict) -> str:
    """元本を割って終わった割合。0 ではないのに四捨五入で 0% になるときは、回数で出す（「いちばん悪かった回」と食い違って見えないように）。"""
    if s["below"] and s["share"] < 0.005:
        return f"1% 未満（{s['below']} 回）"
    return _pct(s["share"])


def _hold_row(n: int, kind: str, s: dict) -> str:
    if not s.get("n"):
        return f"| {n} 年 | {kind} | 0 | – | – | – |"
    return (f"| {n} 年 | {kind} | {s['n']} | **{_share(s)}** | {_change(s['worst'])}（{s['worst_start']:%Y-%m} に始めた回） | "
            f"{_change(s['median'])} |")


def report_lines(res: dict, made: str) -> list[str]:
    L = [f"# リスクとお金の置き方の点検 — 作成 {made}", "",
         "決め方は docs/risk_plan.md 8 章（結果を見る前に決めたもの）。**判定はしない。** 過去の数字で、先も同じとは限らない。", "",
         "## 1. 持つ年数ごとの元本割れ", "",
         "始める月を 1 か月ずつずらして数えた。**配当・物価・為替・費用・税は入っていない**（配当を入れれば、割れる割合は小さくなる。円で米国株を持つ人には、為替の動きが加わる）。", ""]
    for label, h in res["hold"].items():
        L += [f"### {label}（{h['start']:%Y-%m}〜{h['end']:%Y-%m}）", "",
              "| 持つ年数 | 買い方 | 数えた回 | 元本を割って終わった割合 | いちばん悪かった回 | 真ん中の回 |", "|---|---|---|---|---|---|"]
        for n, row in h["rows"].items():
            L += [_hold_row(n, "まとめて買う", row["lump"]), _hold_row(n, "毎月積み立てる", row["saving"])]
        L.append("")
    for label, why in res["skipped"].items():
        L += [f"- {label}: 数えられなかった（{why}）", ""]
    L += ["## 2. 戻し方 3 通りの比べ", ""]
    mix = res["mix"]
    if not mix:
        L += ["- S&P500 のデータが無いので、数えられなかった", ""]
        return L
    intro = (f"株（S&P500。{mix['start']:%Y-%m}〜{mix['end']:%Y-%m}）と、元本が動かない置き場所（利息 0）を、決めた割合で持ち始めて {WINDOW_YEARS} 年。"
             "始める年を 1 年ずつずらした。途中でお金は足さない。費用と税は入れていない。")
    L += [intro, "",
          "**見るのは、下げと、株の割合のぶれ。** 年率は参考（どれが儲かるかを選ぶための表ではない）。", "",
          f"| 株の割合 | 戻し方 | 数えた回 | {WINDOW_YEARS} 年の中の最大の下げ: 真ん中 | 最悪（始めた年） | 終わりの株の割合 | 年率の真ん中（参考） |", "|---|---|---|---|---|---|---|"]
    for share, rows in mix["rows"].items():
        for how, s in rows.items():
            if not s.get("n"):
                L.append(f"| {_pct(share)} | {WAYS[how]} | 0 | – | – | – | – |")
                continue
            L.append(f"| {_pct(share)} | {WAYS[how]} | {s['n']} | {_pct(s['dd_median'])} | {_pct(s['dd_worst'])}（{s['dd_worst_start']} 年） | "
                     f"{_pct(s['share_min'])}〜{_pct(s['share_max'])} | {_pct(s['annual_median'], 1)} |")
    L += ["", "- 最大の下げは月末の値で測った（月の途中の下げは入っていない）",
          "- 放置では、株が上がるほど株の割合が増え、下げも大きくなる。戻す決まりは、その割合を決めた所に引き戻すためのもの", ""]
    return L
