"""W3 1 年の高値に近い株。docs/wide.md 4 章 W3。

月末 t（値段の表で、その月の最後の営業日）に、点数 = 終値 ÷（t を含む直近 250 営業日の終値の最高値）を出し、
上位 10%（基準を満たす株の数 × 0.10 の切り捨て。最低 10）を等分で翌月持つ。同点は、直近 250 営業日のリターン
（その 250 日の日ごとのリターンを掛け合わせる）が高い順、それも同じなら銘柄の番号の順。比べる相手は全銘柄（その月にリターンのある株）の平均。
d = 持った株の翌月のリターンの平均 − 全銘柄の平均 − 入れ替えた割合 × 0.10%。d は得た月（t の翌月）の月末に並べる。

値段の表の読み取り（read_close）は W4 も使う。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from .. import stocks as S
from . import base

KEY = "W3"
FILE = Path("stocks") / "jp_close.csv"     # data の下。日付 × 銘柄（7203.T の形）の調整後終値（配当込み）
WINDOW, MIN_DAYS = 250, 200                # t を含む直近 250 営業日のうち 200 日以上値段のある株だけ
TOP_PCT, MIN_HOLD = 10, 10                 # 上位 10%（切り捨て）。最低 10 銘柄
COST = base.COST_JP                        # 往復 0.10%（入れ替えた割合に掛ける）
STALE_DAYS = 31                            # 値段の最後の日がこれより前なら、先に ./cfd stocks --fetch（docs/wide.md 6 章の 4）


def _list(xs, k: int = 10) -> str:
    xs = [str(x) for x in xs]
    return "、".join(xs[:k]) + (f" ほか {len(xs) - k}" if len(xs) > k else "")


def _days(raw) -> pd.DatetimeIndex:
    """日付の文字列を、時刻も時差も無い日付にする（時差の付いた日付は、その土地の日付のまま）。読めない値は NaT。W4 も使う。"""
    s = pd.Series(list(raw), dtype=object).map(lambda x: "" if pd.isna(x) else str(x).strip())
    try:
        idx = pd.DatetimeIndex(pd.to_datetime(s, format="ISO8601", errors="coerce"))
        if idx.tz is not None:
            idx = idx.tz_localize(None)
    except ValueError:                      # 時差の違う日付が混ざっている → 1 つずつ
        one = [pd.to_datetime(x, format="ISO8601", errors="coerce") for x in s]
        idx = pd.DatetimeIndex([pd.NaT if pd.isna(x) else x.tz_localize(None) if x.tzinfo else x for x in one])
    return idx.normalize()


# --------------------------------------------------------------------------- 読み取りと形の点検
def read_close(data: Path) -> tuple[pd.DataFrame, int]:
    """日本株の調整後終値と、落とした日の数。

    値の 1 つも無い日（W1・W5 と同じ）と、休場日なのに前の日の値のまま入っている日（cfdbot.stocks.drop_closed_days。
    今までの検証と同じ）は落とす。値の無い日を残すと、次の日のリターンが全部の株で消え、月末なら持つ株も決められない。
    無い・読めない・形が違う（日付でない行・7203.T の形でない列・数でない値・同じ日が 2 回・0 以下や無限・値段の無い月）なら NotReady。
    時差の付いた日付は、その土地の日付のまま使う。
    """
    path = Path(data) / FILE
    if not path.exists():
        raise base.NotReady(f"日本株の値段 {path} が無い（./cfd stocks --fetch）")
    try:
        t = pd.read_csv(path, index_col=0)
    except Exception as e:  # 空・壊れたファイル
        raise base.NotReady(f"日本株の値段 {path} を読めない（{type(e).__name__}）") from e
    idx = _days(t.index)
    if t.empty or idx.isna().any():
        raise base.NotReady(f"{path} が空か、日付として読めない行がある")
    bad = [c for c in t.columns if not str(c).endswith(".T")]
    if bad:
        raise base.NotReady(f"{path} に日本株の形（7203.T）でない列がある（{_list(bad, 5)}）")
    text = [c for c in t.columns if not pd.api.types.is_numeric_dtype(t[c])]
    if text:
        raise base.NotReady(f"{path} に数でない値の入った列がある（{_list(text, 5)}）")
    close = t.astype(float).set_axis(idx).sort_index().dropna(how="all")
    close.columns = close.columns.astype(str)
    if close.empty:
        raise base.NotReady(f"{path} に値段が 1 つも無い")
    if not close.index.is_unique:
        raise base.NotReady(f"{path} に同じ日が 2 回ある")
    if ((close <= 0) | np.isinf(close)).any().any():
        raise base.NotReady(f"{path} に 0 以下か無限の値段がある")
    kept = S.drop_closed_days(close, close)[1]
    per = kept.index.to_period("M")
    gap = pd.period_range(per[0], per[-1], freq="M").difference(per.unique())
    if len(gap):
        raise base.NotReady(f"{path} に値段の 1 日も無い月がある（{_list(str(p) for p in gap)}）。取り直す（./cfd stocks --fetch）")
    return kept, len(t) - len(kept)


def _last_month(close: pd.DataFrame) -> pd.Timestamp:
    """終わった最後の月（データの最後の日の月の前の月）の月末。"""
    return base.month_end(close.index[-1].to_period("M").to_timestamp() - pd.Timedelta(days=1))


def _stale(close: pd.DataFrame) -> str | None:
    last = close.index[-1]
    if last < pd.Timestamp(date.today()) - pd.Timedelta(days=STALE_DAYS):
        return f"値段の最後の日 {last:%Y-%m-%d} が 1 か月より前。先に ./cfd stocks --fetch"
    return None


# --------------------------------------------------------------------------- 計算
def _ends(index: pd.DatetimeIndex) -> np.ndarray:
    """月ごとの、値段の表で最後の営業日の位置。"""
    per = index.to_period("M")
    return np.flatnonzero(np.r_[per[1:] != per[:-1], True])


def _picks(close: pd.DataFrame, window: int, min_days: int, top_pct: int, min_hold: int) -> pd.DataFrame:
    """月末 t ごとに、点数の基準を満たす株の数と、持つ株（順位の順）。行は決めた日 t。

    t の引けまでの値段だけを使う。直近 window 営業日が値段の表にそろう月末から。基準を満たす株が min_hold に届かない月末は持たない。
    同点の順の「直近 window 営業日のリターン」は、その window 日それぞれの日のリターン（前の営業日の引け → その日の引け。
    ±30% にそろえる）を掛け合わせたもの（値段の無い日は 0%）。window 日の最初の日の分も入れる（window − 1 日分ではない）。
    """
    c = close.to_numpy(dtype=float)
    g = 1.0 + np.nan_to_num(close.pct_change(fill_method=None).clip(-S.CLIP, S.CLIP).to_numpy(), nan=0.0)
    cols = np.asarray(close.columns, dtype=str)
    days, rows = [], []
    for p in _ends(close.index):
        if p < window - 1:
            continue
        w = c[p - window + 1:p + 1]
        have = np.isfinite(w)
        ok = (have.sum(axis=0) >= min_days) & have[-1]
        n = int(ok.sum())
        held: list[str] = []
        if n >= min_hold:
            score = w[-1, ok] / np.nanmax(w[:, ok], axis=0)
            trail = g[p - window + 1:p + 1, ok].prod(axis=0)    # t を含む直近 window 日の、日ごとのリターン
            order = np.lexsort((cols[ok], -trail, -score))      # 点数の高い順 → 直近のリターンの高い順 → 銘柄の番号の順
            held = [str(x) for x in cols[ok][order[:max(n * top_pct // 100, min_hold)]]]
        days.append(close.index[p])
        rows.append({"eligible": n, "held": held})
    return pd.DataFrame(rows, columns=["eligible", "held"], index=pd.DatetimeIndex(days, name="date"))


def _series(close: pd.DataFrame, window: int, min_days: int, top_pct: int, min_hold: int) -> pd.DataFrame:
    """得た月（決めた月の翌月。月末の日付）ごとの、基準を満たす株の数・持った数・翌月に値段の無かった数・入れ替えた割合・
    持った株の平均（g）・全銘柄の平均（mkt）・コストの前（gross）・d。持たなかった月は gross も d も 0。"""
    picks = _picks(close, window, min_days, top_pct, min_hold)
    mret = S.monthly_returns(close)                             # ±30% にそろえた日ごとのリターンを月ごとに掛け合わせる
    mkt = mret.mean(axis=1)                                     # その月にリターンのある株の平均
    months, rows, prev = [], [], set()
    for t, n, held in zip(picks.index, picks["eligible"], picks["held"]):
        m = (t.to_period("M") + 1).to_timestamp(how="end").normalize()
        if m not in mret.index:                                 # 翌月の値段がまだ無い
            break
        g = mret.loc[m, held]
        months.append(m)
        rows.append({"eligible": n, "held": len(held), "missing": int(g.isna().sum()),
                     "turn": len(set(held) - prev) / len(held) if held else 0.0,   # 最初の月・持たなかった月の次は 100%
                     "g": float(g.fillna(0.0).mean()) if held else np.nan,       # 翌月に値段の無い株は 0%
                     "mkt": float(mkt.loc[m])})
        prev = set(held)
    out = pd.DataFrame(rows, columns=["eligible", "held", "missing", "turn", "g", "mkt"],
                       index=pd.DatetimeIndex(months, name="month"), dtype=float)
    out["gross"] = (out["g"] - out["mkt"]).where(out["held"] > 0, 0.0)
    out["d"] = out["gross"] - COST * out["turn"]
    return out


def compute(data: Path) -> base.Stream:
    close, dropped = read_close(data)
    s = base.finished_months(_series(close, WINDOW, MIN_DAYS, TOP_PCT, MIN_HOLD), close.index[-1])
    on = s.index[s["held"] > 0]
    if not len(on):
        raise base.NotReady(f"持つ株を決められた終わった月が無い（直近 {WINDOW} 営業日のうち {MIN_DAYS} 日以上値段のある株が"
                            f" {MIN_HOLD} 以上そろう月末と、その翌月の値段が要る。値段 {close.index[0]:%Y-%m-%d}〜{close.index[-1]:%Y-%m-%d}）")
    s = s.loc[on[0]:]
    if s["d"].isna().any():
        raise base.NotReady("W3 の d が計算できない月がある（全銘柄の平均が出せない。値段の形を確かめる）")
    end = _last_month(close)
    d = base.fill_months(s["d"], s.index[0], end)
    h = s[s["held"] > 0]
    notes = [
        (f"日本株の調整後終値（配当込み）{close.index[0]:%Y-%m-%d}〜{close.index[-1]:%Y-%m-%d}（{len(close)} 営業日、{close.shape[1]} 銘柄）。"
         f"値の 1 つも無い日と休場日なのに前の日の値のまま入っていた日、合わせて {dropped} 日を落とした"),
        (f"d は {d.index[0]:%Y-%m}〜{d.index[-1]:%Y-%m}（{len(d)} か月）。月末に、直近 {WINDOW} 営業日のうち {MIN_DAYS} 日以上値段のある株"
         f" {int(s['eligible'].min())}〜{int(s['eligible'].max())} 銘柄から、上位 {TOP_PCT}%（最低 {MIN_HOLD}）の"
         f" {int(h['held'].min())}〜{int(h['held'].max())} 銘柄を翌月持った"),
        (f"入れ替えた割合の平均 {h['turn'].mean():.0%}（最初の月は 100%）。入れ替えた割合 × 往復 {COST:.2%} を引いた。"
         "全銘柄の平均は、その月にリターンのある株の平均（cfdbot.stocks.monthly_returns）"),
    ]
    if (k := int((s["held"] == 0).sum())):
        notes.append(f"基準を満たす株が {MIN_HOLD} に届かず持たなかった月 {k}（d は 0）")
    if (k := int(s["missing"].sum())):
        notes.append(f"持った株で、翌月に値段が 1 日も無かったもの {k}（その月のリターンは 0% とした）")
    gross = float(s["gross"].reindex(d.index).fillna(0.0).mean())
    return base.Stream(KEY, d, events=None, gross=gross, notes=notes)


def check(data: Path) -> base.Check:
    """手元の値段の表だけを見る。期間・銘柄の数・基準を満たす株の数だけで、リターンの数字は出さない。"""
    path = Path(data) / FILE
    try:
        close, dropped = read_close(data)
    except base.NotReady as e:
        return base.Check(KEY, False, [f"ファイル {path}"], why=str(e))
    lines = [(f"ファイル {path}（{len(close)} 営業日、{close.index[0]:%Y-%m-%d}〜{close.index[-1]:%Y-%m-%d}、{close.shape[1]} 銘柄）。"
              f"値の 1 つも無い日と休場日なのに前の日の値のまま入っていた日、合わせて {dropped} 日は落とす")]
    if (warn := _stale(close)):
        lines.append(warn)
    picks = _picks(close, WINDOW, MIN_DAYS, TOP_PCT, MIN_HOLD)
    picks = picks[picks.index.to_period("M") <= close.index[-1].to_period("M") - 2]   # 翌月が終わっている月末だけ
    on = picks.index[picks["held"].map(len) > 0]
    if not len(on):
        return base.Check(KEY, False, lines, why=(f"直近 {WINDOW} 営業日のうち {MIN_DAYS} 日以上値段のある株が {MIN_HOLD} 以上そろい、"
                                                  "翌月が終わっている月末が無い（値段の期間が短い）"))
    p = picks.loc[on[0]:]
    held = p["held"].map(len)
    lines += [
        (f"月末の決め {p.index[0]:%Y-%m}〜{p.index[-1]:%Y-%m}（{len(p)} 回）。直近 {WINDOW} 営業日のうち {MIN_DAYS} 日以上値段のある株"
         f" {int(p['eligible'].min())}〜{int(p['eligible'].max())} 銘柄（中央 {p['eligible'].median():.0f}）、"
         f"持つ株 {int(held[held > 0].min())}〜{int(held.max())} 銘柄"),
        f"d を作れる月 {p.index[0].to_period('M') + 1}〜{_last_month(close):%Y-%m}（{len(p)} か月）",
    ]
    if (k := int((held == 0).sum())):
        lines.append(f"基準を満たす株が {MIN_HOLD} に届かない月末 {k}（その翌月は持たない。d は 0）")
    return base.Check(KEY, True, lines)
