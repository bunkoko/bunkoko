"""判断の評価 AI: 判断の記録・点数・開示の一覧・値段（どれもファイル）だけで採点する。docs/agents.md。

ほかの 4 つの AI のモジュールは読み込まない。使うのは、損益の数え方（stocks_short）と判定の条件（stocks）だけ。
選び方の決まり（上位 10% など）も、判断 AI とは別にここに持ち、記録が決まりどおりかを点検する。
"""

from __future__ import annotations

import re
import unicodedata

import numpy as np
import pandas as pd

from ..stocks import CLIP, judge, t_value
from ..stocks_short import cohort_weights, weights_pnl

PRIOR_TRIALS, NEW_TRIALS = 64, 2
COST, HOLD = 0.0010, 5
TOP_SHARE, MIN_VALID, IR_TOP, IR_MIN = 0.10, 50, 0.20, 5
START, SPLIT, STRICT_FROM = "2016-01-01", "2021-01-01", "2025-07-01"
CUTOFF_HOUR = 13
# 参考: AI を使わない分け方（題名の言葉）。上から順に最初に当てはまったもの
KEYWORDS: tuple[tuple[str, str], ...] = (
    ("訂正", r"訂正"), ("上方修正", r"上方修正"), ("下方修正", r"下方修正"), ("増配", r"増配"), ("減配・無配", r"減配|無配"),
    ("自己株式の取得（決定）", r"自己株式.*取得.*(決定|係る事項)|自己株式.*買付"), ("自己株式の取得状況・終了", r"自己株式.*取得(状況|結果|終了)"),
    ("自己株式の消却", r"自己株式.*消却"), ("株式分割", r"株式分割"), ("公開買付け", r"公開買付"),
    ("特別損失・減損", r"特別損失|減損"), ("特別利益", r"特別利益"), ("業績予想の修正（向きは題名に無い）", r"業績予想.*修正|予想.*修正"),
    ("配当予想の修正・配当", r"配当"), ("決算短信", r"決算短信"), ("月次", r"月次"), ("中期経営計画", r"中期経営|中期計画"),
)


def returns(close: pd.DataFrame) -> pd.DataFrame:
    return close.pct_change(fill_method=None).clip(-CLIP, CLIP)


def to_matrix(decisions: pd.DataFrame, book: str, index: pd.DatetimeIndex, columns) -> np.ndarray:
    """買いの記録（date / ticker / book）を 日 × 株 の表に。"""
    d = decisions[decisions["book"] == book]
    out = np.zeros((len(index), len(columns)), dtype=bool)
    t = index.get_indexer(pd.to_datetime(d["date"]))
    j = pd.Index(columns).get_indexer(d["ticker"])
    ok = (t >= 0) & (j >= 0)
    out[t[ok], j[ok]] = True
    return out


def top_k(score: np.ndarray, share: float, min_valid: int) -> np.ndarray:
    ok = np.isfinite(score)
    n = ok.sum(axis=1)
    k = np.where(n >= min_valid, np.maximum(np.round(n * share), 1), 0).astype(int)
    order = np.argsort(np.where(ok, -score, np.inf), axis=1, kind="stable")
    rank = np.empty_like(order)
    np.put_along_axis(rank, order, np.arange(score.shape[1])[None, :], axis=1)
    return (rank < k[:, None]) & ok


def expected_books(selector: np.ndarray, ir: np.ndarray) -> dict[str, np.ndarray]:
    """決まりどおりなら選ばれるはずの銘柄（点数のファイルから計算する）。"""
    ir = np.where(np.isfinite(selector), ir, np.nan)
    return {"selector": top_k(selector, TOP_SHARE, MIN_VALID), "combined": top_k(selector + np.nan_to_num(ir), TOP_SHARE, MIN_VALID),
            "ir": top_k(ir, IR_TOP, IR_MIN)}


def book_pnl(sel: np.ndarray, cc: pd.DataFrame, cost: float = COST, hold: int = HOLD) -> pd.DataFrame:
    """hold 日保有（毎日 1/hold ずつ入れ替え）。gross = 全銘柄の平均との差、d = コストを引いた後。"""
    return weights_pnl(cc, cohort_weights(sel, hold), cost)


def shuffle_rows(values: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """日ごとに、値のある所の中身だけをでたらめに入れ替える（どの株に値があるかは変えない）。"""
    t, j = np.nonzero(np.isfinite(values))
    order = np.lexsort((rng.random(len(t)), t))
    out = np.full(values.shape, np.nan)
    out[t, j] = values[t[order], j[order]]
    return out


def random_among(pool: np.ndarray, k: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """日ごとに、pool の中からでたらめに k 個選ぶ。"""
    r = np.where(pool, rng.random(pool.shape, dtype=np.float32), np.inf)
    order = np.argsort(r, axis=1)
    rank = np.empty_like(order)
    np.put_along_axis(rank, order, np.arange(pool.shape[1])[None, :], axis=1)
    return (rank < k[:, None]) & pool


def verdict(d: pd.Series, placebo_pct: float | None, n_trials: int = PRIOR_TRIALS + NEW_TRIALS) -> dict:
    """docs/agents.md 5 章: 4 つの条件 ＋ 2025-07 以降だけの平均がプラス。"""
    j = judge(d, SPLIT, n_trials, placebo_pct)
    strict = d[d.index >= pd.Timestamp(STRICT_FROM)]
    j["strict_mean"] = float(strict.mean()) if len(strict) else np.nan
    if j["verdict"] == "使えそう" and not j["strict_mean"] > 0:
        j["verdict"] = "まだ判断できない"
    return j


def excess_forward(cc: pd.DataFrame, hold: int = HOLD) -> np.ndarray:
    """t 日の引けから hold 営業日後の引けまでの「株 − 全銘柄の平均」の合計（日 × 株）。先が足りない所は NaN。"""
    ex = cc.sub(cc.mean(axis=1), axis=0).to_numpy()
    cs = np.cumsum(np.nan_to_num(ex), axis=0)
    out = np.full(ex.shape, np.nan)
    out[:-hold] = cs[hold:] - cs[:-hold]
    out[~np.isfinite(ex)] = np.nan
    return out


def decision_pos(times: pd.Series, index: pd.DatetimeIndex) -> np.ndarray:
    """開示の時刻 → 使う営業日の位置（13:00 までならその日、後なら翌日。docs/agents.md 3 章）。"""
    t = pd.to_datetime(times)
    late = (t.dt.hour * 3600 + t.dt.minute * 60 + t.dt.second) > CUTOFF_HOUR * 3600
    return index.searchsorted((t.dt.normalize() + pd.to_timedelta(late.astype(int), unit="D")).to_numpy())


def keyword_of(title: str) -> str:
    t = unicodedata.normalize("NFKC", str(title))
    return next((name for name, pat in KEYWORDS if re.search(pat, t)), "その他")


def keyword_table(disc: pd.DataFrame, cc: pd.DataFrame) -> pd.DataFrame:
    """参考: 題名の言葉で分けた種類ごとの、その後 5 日の「株 − 全銘柄の平均」（START から。株 × 日 × 種類で 1 回と数える）。"""
    index, col = cc.index, {t: k for k, t in enumerate(cc.columns)}
    d = pd.DataFrame({"pos": decision_pos(disc["time"], index), "j": disc["ticker"].map(col),
                      "kind": [keyword_of(t) for t in disc["title"]]}).dropna()
    d = d[d["pos"] < len(index)].astype({"j": int}).drop_duplicates()
    d["ex"] = excess_forward(cc)[d["pos"].to_numpy(), d["j"].to_numpy()]
    d["date"] = index[d["pos"].to_numpy()]
    d = d[(d["date"] >= START) & d["ex"].notna()]
    rows = []
    for kind, g in d.groupby("kind"):
        a, b = g[g["date"] < SPLIT]["ex"], g[g["date"] >= SPLIT]["ex"]
        rows.append({"kind": kind, "n": len(g), "mean": g["ex"].mean(), "t": t_value(g["ex"]), "mean1": a.mean(), "mean2": b.mean()})
    return pd.DataFrame(rows).sort_values("mean", ascending=False).reset_index(drop=True)


def ledger_summary(ledger: pd.DataFrame, cc: pd.DataFrame) -> pd.DataFrame:
    """台帳（その日の 15:25 より前に書かれた判断だけ）を、5 日後の値段で採点する。book ごとの日数・平均。"""
    if ledger.empty:
        return pd.DataFrame(columns=["book", "days", "scored_days", "mean_excess"])
    date = pd.to_datetime(ledger["date"])
    live = pd.to_datetime(ledger["decided_at"]) < date + pd.Timedelta(hours=15, minutes=25)
    lg = ledger[live.to_numpy()].copy()
    ex = excess_forward(cc)
    t = cc.index.get_indexer(pd.to_datetime(lg["date"]))
    j = pd.Index(cc.columns).get_indexer(lg["ticker"])
    lg["ex"] = np.where((t >= 0) & (j >= 0), ex[np.maximum(t, 0), np.maximum(j, 0)], np.nan)
    rows = []
    for book, g in lg.groupby("book"):
        per_day = g.groupby("date")["ex"].mean()
        rows.append({"book": book, "days": int(g["date"].nunique()), "scored_days": int(per_day.notna().sum()),
                     "mean_excess": float(per_day.mean()) if per_day.notna().any() else np.nan})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- 先のデータ（台帳）の点検と、止める決まり（docs/agents.md 12 章）
FORWARD_BOOKS = ("selector", "combined", "random", "e1")   # 台帳の帳（random・e1 は 2026-10-15 に足した）
JUDGED_BOOKS = ("selector", "e1")          # 止める決まりを当てる帳（M2 と E1）
LOOK_DAYS = (250, 500, 750)                # 見る時点（数えた判断の日の数）
STOP_T = -1.0                              # コスト前の月ごとの t がこれ未満なら閉じる
RANDOM_ALARM_T = 2.58                      # でたらめの帳の |t| がこれ以上なら、データか計算の誤りを疑って点検する
LATE_AFTER = pd.Timedelta(hours=15, minutes=25)   # この時刻より後に書いた行は「先の判断」に数えない
# 毎週の見張り（CUSUM、累積和）: M2 の帳だけ。5 営業日ごとの「コスト前の差」を、過去の記録の値動きの大きさで割って足していく
BLOCK = 5                                  # 1 週 = 5 営業日（重なる 5 日保有の自己相関を週の中に閉じ込める）
BLOCKS_PER_YEAR = 252 / BLOCK
CUSUM_SR_GOOD = 0.5                        # 「良い水準」= コスト前の年のシャープ 0.5（M2 の 2018 年からの記録 約 1.0 の半分）。悪い水準 = 0
CUSUM_ARL_YEARS = 5.0                      # 良い水準のまま続いているときの、誤った警報の平均の間隔
CUSUM_H = 12.3                             # 上の 2 つから、作ったデータで決めた閾値（cusum_threshold で再現できる。docs/agents.md 12.3）
SIGMA_FROM, SIGMA_TO = "2018-01-01", "2026-09-30"   # 値動きの大きさ（週の差の標準偏差）を測る過去の記録の期間（台帳より前）
# 最小記録長（12.4 の 6）: コスト前とコスト後（c* が決まるまでは往復 0.10%）の記録のシャープで計算し、大きいほう。これより短い台帳では、お金を増やさない
MIN_RECORD_DAYS = {"selector": 2960, "e1": 2430}


def counted_rows(ledger: pd.DataFrame) -> pd.DataFrame:
    """台帳のうち、その日の 15:25 より前に書かれた行（先の判断として数える行）。"""
    if ledger is None or ledger.empty:
        return pd.DataFrame(columns=["decided_at", "date", "ticker", "book"])
    lg = ledger.copy()
    lg["date"] = pd.to_datetime(lg["date"].astype(str).str[:10])
    ok = pd.to_datetime(lg["decided_at"]) < lg["date"] + LATE_AFTER
    return lg[ok.to_numpy()]


def ledger_health(ledger: pd.DataFrame, calendar: pd.DatetimeIndex, now: pd.Timestamp) -> dict:
    """台帳の点検: 最後に書いた日、抜けた営業日、遅れて書いた日、帳ごとの日数。

    calendar は営業日（値段のある日）。その最後の日より後は、平日を営業日とみなす（祝日を含むかもしれない）。
    営業日 d は、d が今日より前か、今日で 15:25 を過ぎていれば「書かれているはずの日」。
    """
    out = {"rows": 0, "first": None, "last": None, "last_written": None, "missing": [], "missing_weekdays": [], "late": [],
           "counted_days": 0, "books": {}}
    if ledger is None or ledger.empty:
        return out
    lg = ledger.copy()
    lg["date"] = pd.to_datetime(lg["date"].astype(str).str[:10])
    out["rows"] = len(lg)
    cnt = counted_rows(ledger)
    days_all = set(lg["date"])
    days_cnt = set(cnt["date"])
    out["late"] = sorted(days_all - days_cnt)
    out["last_written"] = str(lg["decided_at"].max())
    if not days_cnt:
        return out
    first = min(days_cnt)
    out["first"], out["last"], out["counted_days"] = first, max(days_cnt), len(days_cnt)
    now = pd.Timestamp(now)
    today = now.normalize()
    due_today = now >= today + LATE_AFTER
    cal = pd.DatetimeIndex(calendar).normalize()
    in_cal = [d for d in cal if first <= d and (d < today or (d == today and due_today))]
    out["missing"] = [d for d in in_cal if d not in days_cnt]
    after = pd.bdate_range(cal[-1] + pd.Timedelta(days=1), today) if len(cal) else pd.DatetimeIndex([])
    out["missing_weekdays"] = [d for d in after if (d < today or due_today) and d not in days_cnt and d >= first]
    for b, g in cnt.groupby("book"):
        out["books"][b] = {"days": int(g["date"].nunique()), "first": g["date"].min(), "names_per_day": float(g.groupby("date").size().mean())}
    return out


def book_start(ledger: pd.DataFrame, book: str) -> pd.Timestamp | None:
    """その帳の時計の始まり。random と e1 は同時に足したので、どちらも random の最初の日（e1 は買う株の無い日が多いため）。"""
    cnt = counted_rows(ledger)
    key = "random" if book == "e1" else book
    d = cnt.loc[cnt["book"] == key, "date"]
    return d.min() if len(d) else None


def _monthly_t(gross: pd.Series) -> tuple[float, int]:
    m = gross.resample("ME").sum() if len(gross) else pd.Series(dtype=float)
    return (t_value(m) if len(m) >= 3 else float("nan")), len(m)


def forward_book(ledger: pd.DataFrame, book: str, cc: pd.DataFrame) -> dict | None:
    """1 つの帳の先のデータの成績（数えた行だけ。5 日保有・毎日 1/5 入れ替え・全銘柄の平均との差）。

    looks: 250・500・750 日の時点（その日までの損益）の、コスト前の月ごとの t。
    """
    start = book_start(ledger, book)
    if start is None:
        return None
    cnt = counted_rows(ledger)
    run_days = sorted(set(cnt.loc[cnt["date"] >= start, "date"]))      # 判断を書いた日（その帳に買う株が無かった日も数える）
    sel = to_matrix(cnt.assign(date=cnt["date"].dt.strftime("%Y-%m-%d")), book, cc.index, cc.columns)
    pnl = book_pnl(sel, cc)
    last = int(cc.index.searchsorted(run_days[-1]))
    end = cc.index[min(last + HOLD, len(cc.index) - 1)]                  # 最後の判断の分を手放すまで（その先は持っていない）
    pnl = pnl[(pnl.index > start) & (pnl.index <= end)]                   # 最初の判断の翌日から損益が出る
    t_now, months = _monthly_t(pnl["gross"])
    looks = {}
    for n in LOOK_DAYS:
        if len(run_days) >= n:
            looks[n] = _monthly_t(pnl.loc[pnl.index <= run_days[n - 1], "gross"])[0]
    return {"book": book, "start": start, "days": len(run_days), "pnl_days": len(pnl),
            "gross_ann": float(pnl["gross"].mean() * 252) if len(pnl) else np.nan,
            "net_ann": float(pnl["d"].mean() * 252) if len(pnl) else np.nan,
            "turnover": float(pnl["turnover"].mean()) if len(pnl) else np.nan,
            "months": months, "t": t_now, "looks": looks, "gross": pnl["gross"]}


def forward_verdict(r: dict | None, book: str, cusum: dict | None = None) -> str:
    """docs/agents.md 12.3 の決まり。250・500・750 日の時点（その日までの損益）だけで判定し、その間は途中経過。

    一度「閉じる」になったら、その後の数字が戻っても閉じたまま（先に決めたことを、結果を見て変えない）。
    """
    if r is None:
        return "まだ記録が無い"
    looks = r.get("looks", {})
    if book == "random":
        bad = [(n, t) for n, t in looks.items() if np.isfinite(t) and abs(t) >= RANDOM_ALARM_T]
        if bad:
            return f"点検（{bad[0][0]} 日の時点で、でたらめの帳の t が {bad[0][1]:+.2f}。データか計算の誤りを疑う）"
        return f"正常（{max(looks)} 日の時点まで）" if looks else f"途中経過（{r['days']} 日）"
    if book not in JUDGED_BOOKS:
        return f"参考（{r['days']} 日）"
    if cusum is not None and cusum.get("alarm") is not None:
        return f"閉じる（毎週の見張りの警報。{cusum['alarm']} 週目に累積和が {CUSUM_H} を超えた）"
    if not looks:
        nxt = LOOK_DAYS[0]
        return f"まだ判定しない（{r['days']} 日。{nxt} 日で 1 回目、あと {nxt - r['days']} 日）"
    for n, t in looks.items():
        if np.isfinite(t) and t < STOP_T:
            return f"閉じる（{n} 日の時点で、コスト前の月ごとの t {t:+.2f} が {STOP_T} 未満）"
    last = max(looks)
    nxt = next((n for n in LOOK_DAYS if n > last), None)
    return f"続ける（{last} 日の時点の t {looks[last]:+.2f}）" + (f"。次は {nxt} 日" if nxt else "。見る時点は終わり")



def weekly_blocks(daily: pd.Series, block: int = BLOCK) -> pd.Series:
    """日ごとの系列を、最初の日から 5 営業日ずつの重ならない合計にする（最後の欠けた週は使わない）。index は週の最後の日。"""
    d = daily.dropna()
    n = len(d) // block
    if n == 0:
        return pd.Series(dtype=float)
    v = d.to_numpy()[: n * block].reshape(n, block).sum(axis=1)
    return pd.Series(v, index=d.index[block - 1: n * block: block])


def cusum_k(sr_good: float = CUSUM_SR_GOOD) -> float:
    """良い水準（週あたり、標準偏差の単位）と 0 の中間。"""
    return sr_good / np.sqrt(BLOCKS_PER_YEAR) / 2


def cusum_path(z: np.ndarray, k: float, h: float = CUSUM_H) -> tuple[np.ndarray, int | None]:
    """下向きの累積和 S_t = max(0, S_{t-1} + k − z_t)。S が h を超えた最初の週（1 から数える）を返す。無ければ None。"""
    s, out, alarm = 0.0, np.empty(len(z)), None
    for i, x in enumerate(np.asarray(z, dtype=float)):
        s = max(0.0, s + k - x)
        out[i] = s
        if alarm is None and s > h:
            alarm = i + 1
    return out, alarm


def cusum_arl(h: float, drift: float, k: float, n_paths: int = 4000, max_len: int = 5000, seed: int = 0, df: int | None = None) -> float:
    """作ったデータで、警報が出るまでの週の平均（週ごとの z が平均 drift・標準偏差 1。df を渡せば裾の重い t 分布）。"""
    rng = np.random.default_rng(seed)
    s = np.zeros(n_paths)
    alive = np.ones(n_paths, dtype=bool)
    when = np.full(n_paths, float(max_len))
    for t in range(max_len):
        z = rng.standard_normal(n_paths) if df is None else rng.standard_t(df, n_paths) / np.sqrt(df / (df - 2))
        s = np.maximum(0.0, s + k - (z + drift))
        hit = alive & (s > h)
        when[hit] = t + 1
        alive &= ~hit
        if not alive.any():
            break
    return float(when.mean())


def cusum_threshold(sr_good: float = CUSUM_SR_GOOD, years: float = CUSUM_ARL_YEARS, n_paths: int = 3000, iters: int = 18,
                    df: int | None = None, seed: int = 0) -> float:
    """良い水準のまま続いているときの誤った警報の平均の間隔が years 年になる閾値 h を、二分法で求める（docs/agents.md 12.3 の表）。"""
    target, k, drift = years * BLOCKS_PER_YEAR, cusum_k(sr_good), sr_good / np.sqrt(BLOCKS_PER_YEAR)
    lo, hi = 1.0, 30.0
    for i in range(iters):
        mid = (lo + hi) / 2
        if cusum_arl(mid, drift, k, n_paths=n_paths, max_len=int(target * 12), seed=seed + i, df=df) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def history_sigma(selection: np.ndarray, cc: pd.DataFrame, start: str = SIGMA_FROM, end: str = SIGMA_TO) -> float:
    """毎週の見張りに使う、週の差の標準偏差。過去の記録（台帳より前）の選び方で、コスト前の差を週ごとに足して測る。"""
    g = book_pnl(selection, cc)["gross"]
    g = g[(g.index >= pd.Timestamp(start)) & (g.index <= pd.Timestamp(end))]
    w = weekly_blocks(g)
    return float(w.std(ddof=1)) if len(w) >= 20 else float("nan")


def forward_cusum(r: dict | None, sigma_w: float, h: float = CUSUM_H) -> dict | None:
    """M2 の帳の毎週の見張り。z = 週の差 ÷ sigma_w。"""
    if r is None or not np.isfinite(sigma_w) or sigma_w <= 0:
        return None
    w = weekly_blocks(r["gross"])
    path, alarm = cusum_path((w / sigma_w).to_numpy(), cusum_k(), h)
    return {"weeks": len(w), "last": float(path[-1]) if len(path) else 0.0, "max": float(path.max()) if len(path) else 0.0,
            "alarm": alarm, "sigma_w": sigma_w}
