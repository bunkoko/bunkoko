"""リスクとお金の置き方の点検（docs/risk_plan.md 8 章。結果を見る前に決めたもの）。判定はしない・試した数に入れない。

8.1 持つ年数ごとの元本割れ: 指数を N 年持った（まとめて買う・毎月積み立てる）とき、元本を割って終わった割合。
8.2 戻し方 3 通りの比べ: 株と「元本が動かない置き場所」を決めた割合で 20 年持つとき、放置・年 1 回戻す・幅を超えたら戻す、の下げと割合のぶれ。
8.3 足した分: 株の割合 80・90・100% と、円で見た版（S&P500 × ドル円）。
8.4 足した分: 柱（束ねる検証で書き出した家族ごとの月の系列）と株の指数の相関、株 60% の行。柱の成績は出さない。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .wide import base, w7

YEARS = (1, 3, 5, 10, 15, 20)                  # 持つ年数（8.1）
INDEXES = {"^GSPC": ("S&P500（米国。ドル建て・配当なし）", "1950-01-01"), "^N225": ("日経平均（日本。円建て・配当なし）", "1965-01-01")}
SHARES = (0.3, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)   # 株の割合（8.2。80% から上は 8.3、60% は 8.4 で足した）
LINK_MIN = 24                                  # 柱と指数の相関は、重なる月がこれ以上あるときだけ出す（8.4）
YEN_SHORT = "S&P500（円で見て）"               # 8.4 の表の列の名前
YEN, YEN_SINCE, USDJPY = "S&P500 を円で見たもの（S&P500 × ドル円。配当なし）", "1971-01-01", "DEXJPUS"     # 8.3
VIEWS = {"yen": "円で見た場合（S&P500 × ドル円。円の預金と組む）", "usd": "ドルで見た場合（S&P500 そのまま）"}
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


def read_usdjpy(data) -> pd.Series:
    """ドル円（1 ドル = 何円）の日ごとの値。./cfd wide --fetch が取った FRED のファイルを読む（取りに行かない）。"""
    return w7._read(Path(data), USDJPY)


def in_yen(m_usd: pd.Series, usdjpy_daily: pd.Series) -> pd.Series:
    """ドル建ての月末の値 × その月の最後のドル円 ＝ 円で見た月末の値（どちらにも値のある月だけ）。"""
    rate = month_ends(usdjpy_daily)
    both = m_usd.index.intersection(rate.index)
    return (m_usd[both] * rate[both]).sort_index()


def pillar_links(series_dir, indexes: dict[str, pd.Series]) -> dict | None:
    """8.4: 束ねる検証で書き出した家族ごとの月の系列と、株の指数の月の変化率の相関。

    返すのは、家族 → {指数の名前: (相関, 重なる月の数)}。相関と月の数だけで、柱の成績は出さない。系列が無ければ None。
    """
    from . import bundle as B

    try:
        series, _ = B.load_series(series_dir)
    except base.NotReady:
        return None
    if not series:
        return None
    out = {}
    for f, s in B.family_series(series, B.FAMILIES).items():
        row = {}
        for name, r in indexes.items():
            both = pd.concat([s, r], axis=1, join="inner").dropna()
            row[name] = (float(both.iloc[:, 0].corr(both.iloc[:, 1])) if len(both) >= LINK_MIN else float("nan"), len(both))
        out[f] = row
    return out


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
def _hold_block(m: pd.Series) -> dict:
    return {"start": m.index[0], "end": m.index[-1],
            "rows": {n: {"lump": shortfall(lump(m, n)), "saving": shortfall(saving(m, n))} for n in YEARS}}


def _mix_block(m: pd.Series) -> dict:
    return {"start": m.index[0], "end": m.index[-1], "rows": {s: {how: summary(windows(m, s, how)) for how in WAYS} for s in SHARES}}


def run(data) -> dict:
    """2 つの点検を計算する。データの無い指数・ドル円は、理由だけ返して先へ進む。"""
    out: dict = {"hold": {}, "skipped": {}, "mix": {}, "links": None}
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
        out["hold"][label] = _hold_block(m)
    if STOCK not in monthly:
        return out
    yen = None
    try:                                                          # 8.3: 円で見た S&P500
        yen = in_yen(monthly[STOCK], read_usdjpy(data)[YEN_SINCE:])
        if len(yen) < 12 * min(YEARS) + 2:
            yen = None
            raise base.NotReady("ドル円と重なる月が 1 年ぶんに届かない")
        out["hold"][YEN] = _hold_block(yen)
        out["mix"][VIEWS["yen"]] = _mix_block(yen)                # 円で見た版を先に（円で暮らす人には、こちらが実感に近い）
    except base.NotReady as e:
        out["skipped"][YEN] = str(e)
    out["mix"][VIEWS["usd"]] = _mix_block(monthly[STOCK])
    indexes = {YEN_SHORT: yen.pct_change().dropna()} if yen is not None else {"S&P500（ドルで見て）": monthly[STOCK].pct_change().dropna()}
    if "^N225" in monthly:
        indexes["日経平均"] = monthly["^N225"].pct_change().dropna()
    out["links"] = pillar_links(Path(data) / "bundle" / "series", indexes)       # 8.4
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
         ("始める月を 1 か月ずつずらして数えた。**配当・物価・費用・税は入っていない**（配当を入れれば、割れる割合は小さくなる）。"
          "S&P500 と日経平均はその国のお金で見た数字。円で米国株を持つ人の目に近いのは「円で見たもの」の表。"), ""]
    for label, h in res["hold"].items():
        L += [f"### {label}（{h['start']:%Y-%m}〜{h['end']:%Y-%m}）", "",
              "| 持つ年数 | 買い方 | 数えた回 | 元本を割って終わった割合 | いちばん悪かった回 | 真ん中の回 |", "|---|---|---|---|---|---|"]
        for n, row in h["rows"].items():
            L += [_hold_row(n, "まとめて買う", row["lump"]), _hold_row(n, "毎月積み立てる", row["saving"])]
        L.append("")
    for label, why in res["skipped"].items():
        L += [f"- {label}: 数えられなかった（{why}）", ""]
    L += ["## 2. 戻し方 3 通りの比べ", ""]
    if not res["mix"]:
        L += ["- S&P500 のデータが無いので、数えられなかった", ""]
        return L
    intro = (f"株と、元本が動かない置き場所（利息 0）を、決めた割合で持ち始めて {WINDOW_YEARS} 年。始める年を 1 年ずつずらした。"
             "途中でお金は足さない。費用と税は入れていない。")
    L += [intro, "",
          "**見るのは、下げと、株の割合のぶれ。** 年率は参考（どれが儲かるかを選ぶための表ではない）。", ""]
    for view, mix in res["mix"].items():
        L += [f"### {view}（{mix['start']:%Y-%m}〜{mix['end']:%Y-%m}）", "",
              f"| 株の割合 | 戻し方 | 数えた回 | {WINDOW_YEARS} 年の中の最大の下げ: 真ん中 | 最悪（始めた年） | 終わりの株の割合 | 年率の真ん中（参考） |",
              "|---|---|---|---|---|---|---|"]
        for share, rows in mix["rows"].items():
            for how, s in rows.items():
                if share >= 1.0 and how != "drift":               # 株だけなら戻す先が無く、3 つとも同じ → 1 行
                    continue
                name = "株だけ（戻す先が無い）" if share >= 1.0 else WAYS[how]
                if not s.get("n"):
                    L.append(f"| {_pct(share)} | {name} | 0 | – | – | – | – |")
                    continue
                L.append(f"| {_pct(share)} | {name} | {s['n']} | {_pct(s['dd_median'])} | {_pct(s['dd_worst'])}（{s['dd_worst_start']} 年） | "
                         f"{_pct(s['share_min'])}〜{_pct(s['share_max'])} | {_pct(s['annual_median'], 1)} |")
        L.append("")
    L += ["- 最大の下げは月末の値で測った（月の途中の下げは入っていない）",
          "- 放置では、株が上がるほど株の割合が増え、下げも大きくなる。戻す決まりは、その割合を決めた所に引き戻すためのもの",
          "- 円で見た版は、株が下げる時に円高が重なると、ドルで見た下げより大きくなる（逆に円安なら小さくなる）", ""]
    return L + _link_lines(res.get("links"))


def _link_lines(links: dict | None) -> list[str]:
    """8.4: 柱と株の指数の相関（相関と月の数だけ）。"""
    from . import bundle as B

    L = ["## 3. 柱と株の指数の相関", ""]
    if not links:
        return [*L, "- 束ねる検証の系列が無い（`data/bundle/series/`）ので、数えられなかった", ""]
    names = list(next(iter(links.values())))
    L += ["束ねる検証で書き出した家族ごとの月の系列と、株の指数の月の変化率の相関（どちらにも値のある月）。**柱の成績は出していない。**",
          "プラスが大きい柱は、株が下げる月に一緒に負けやすい。マイナスなら逆に動きやすい。0 に近ければ、関わりが薄い。", "",
          "| 柱 | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for f in B.FAMILIES:
        if f not in links:
            continue
        cells = [("–" if not np.isfinite(c) else f"{c:.2f}".replace("-", "−")) + f"（{n} か月）" for c, n in (links[f][k] for k in names)]
        L.append(f"| {f} {B.FAMILY_NAMES[f]} | " + " | ".join(cells) + " |")
    L += ["", f"- 重なる月が {LINK_MIN} に届かないものは「–」", "- ふだんの月の相関なので、大きく荒れた時にも同じとは限らない", ""]
    return L
