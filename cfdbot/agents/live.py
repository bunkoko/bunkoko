"""日本株の少額の本番（予行演習。docs/stocks_live.md）。注文は出さない（1 株単位で注文できる API が無いので、利用者が手で出す）。

- 今日の注文: 台帳の E1 の帳（ledger_extra.csv の e1）から、決まりどおりに買う株・売る株・株数を作り、data/agents/live/plan.csv に書き足す
- 約定: 利用者が伝えた結果を fills.csv に書き足す（決まりと違えば「誤り」と書く）
- 報告: 記録上の資産、止める決まり、空運転の進み具合、コスト（c*）の途中経過
どのファイルも追記だけ（直すときは「取り消す行」を足す）。止める決まりに当たったら STOP を置く。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .decider import random_pick

# --------------------------------------------------------------------------- 決まり（docs/stocks_live.md。結果を見る前に決めた）
DEFAULT_EQUITY = 300_000                 # 3.1 口座に入れる金額 X の既定
EQUITY_RANGE = (100_000, 500_000)        # 3.1 X は 10〜50 万円（50 万円は docs/agents.md 12.4 の「少額」と同じ上限）
SPLIT = 6                                # 3.1 1 日に新しく買う金額 D = X ÷ 6
MAX_NAMES = 5                            # 2.1 1 日 5 銘柄まで（多い日は日付を種にしたでたらめ）
MAX_SHARES = 99                          # 2.1 S 株は 1〜99 株。100 株以上になる株は買わない
HOLD = 5                                 # 4.3 5 営業日後の引けで売る
DD_STOP = 0.80                           # 3.2 記録上の資産が X の 80% を下回ったら STOP
ERR_WINDOW, ERR_MAX = 20, 2              # 3.2 直近 20 営業日に誤り 2 回で STOP
WRONG_FACTOR = 1.5                       # 3.2 1 回の買いの約定の金額が D の 1.5 倍を超えたら、誤りで、すぐ STOP
SELL_RETRY_MAX = 5                       # 3.2 売れない日が 5 営業日を超えたら STOP
PRICE_GAP_DAYS = 2                       # 3.2 持っている株の値段が 2 営業日続けて無ければ STOP（売買停止・上場廃止の恐れ）
MARGIN_MIN = 30                          # 4.2 受付の終わりの 30 分前までに plan.csv を書けた日だけ買う
DRY_MIN_DAYS, DRY_MIN_ROWS, DRY_MARGIN_SHARE = 10, 10, 0.8   # 4.5 空運転: 10 営業日以上・E1 の行 10 行以上・間に合った日 8 割以上
FIX_ORDERS, MIN_ROUNDTRIPS, GIVE_UP_ORDERS = 60, 30, 120     # 5.3 買いの注文 60 回で決める。往復 30 回未満なら続け、120 回で打ち切る
C_FLOOR, NF_LIMIT, LIMIT_6 = 0.0002, 0.05, 0.0010            # 5.3 下限 0.02%、不成立 5%、docs/agents.md 6 章の「往復 0.10% 以内」
REF_DAYS = 3                             # 5.1 基準の値段は約定から 3 営業日以内に取る
EARLY_TRIPS = 10                         # 4.5 の 3: 最初の 10 往復の c の平均が往復 0.10% を超えたら止める
LIMIT_UP_DAY = 0.10                      # 5.2 不成立のうち、その日の終値が前の日から +10% 以上のもの（ストップ高の日の見当）を別に数える
EARLIEST_DEADLINE = "13:35"              # 4.1 受付の終わりがこれより前だと、13:05 の判断の後に 30 分の余裕が取れない
FORM = "SBI S株・成行"
AT = ("引け", "寄り", "後場寄り")
SIDES = {"買い": "買い", "buy": "買い", "売り": "売り", "sell": "売り"}
STATUSES = ("約定", "不成立", "出さなかった", "取り消し")
PLAN_COLUMNS = ["written_at", "mode", "date", "ticker", "name", "side", "form", "shares", "budget_yen", "prev_close", "due",
                "reason", "decided_at", "margin_min", "note"]
FILL_COLUMNS = ["id", "recorded_at", "mode", "date", "ticker", "side", "shares", "price", "fee_yen", "status", "at", "error", "voids", "memo"]
REF_COLUMNS = ["date", "ticker", "open", "close", "fetched_at", "source"]
E1_SIDES = ("買い", "買わない", "延長")    # E1 の行から作った行（空運転で数える）


# --------------------------------------------------------------------------- ファイル
class LiveFiles:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def plan_path(self) -> Path:
        return self.root / "plan.csv"

    @property
    def fills_path(self) -> Path:
        return self.root / "fills.csv"

    @property
    def refs_path(self) -> Path:
        return self.root / "ref_prices.csv"

    @property
    def stop_path(self) -> Path:
        return self.root / "STOP"

    def config(self) -> dict | None:
        return json.loads(self.config_path.read_text(encoding="utf-8")) if self.config_path.exists() else None

    def save_config(self, cfg: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _read(self, path: Path, columns: list[str]) -> pd.DataFrame:
        if not path.exists():
            return pd.DataFrame(columns=columns)
        d = pd.read_csv(path, dtype={"id": str, "ticker": str, "error": str, "voids": str, "memo": str, "note": str, "reason": str})
        for c in ("date", "due"):
            if c in d:
                d[c] = pd.to_datetime(d[c])
        return d

    def plan(self) -> pd.DataFrame:
        return self._read(self.plan_path, PLAN_COLUMNS)

    def fills(self, include_voided: bool = False) -> pd.DataFrame:
        d = self._read(self.fills_path, FILL_COLUMNS)
        if include_voided or d.empty:
            return d
        voided = set(d["voids"].dropna().astype(str))
        return d[~d["id"].astype(str).isin(voided) & d["voids"].isna()].reset_index(drop=True)

    def next_fill_id(self) -> str:
        n = len(self._read(self.fills_path, FILL_COLUMNS))
        return str(n + 1)

    def refs(self) -> pd.DataFrame:
        return self._read(self.refs_path, REF_COLUMNS)

    def append(self, path: Path, rows: list[dict], columns: list[str]) -> None:
        if not rows:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        df = pd.DataFrame(rows, columns=columns)
        for c in ("date", "due"):
            if c in df:
                df[c] = pd.to_datetime(df[c]).dt.strftime("%Y-%m-%d")
        with open(path, "a", encoding="utf-8", newline="") as f:
            df.to_csv(f, header=new, index=False)

    def stop_reason(self) -> str | None:
        if not self.stop_path.exists():
            return None
        return self.stop_path.read_text(encoding="utf-8").strip() or "STOP がある"

    def place_stop(self, reason: str, now: pd.Timestamp) -> None:
        if self.stop_path.exists():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.stop_path.write_text(f"{now:%Y-%m-%d %H:%M} {reason}\n", encoding="utf-8")


# --------------------------------------------------------------------------- 営業日
def calendar(index: pd.DatetimeIndex, until: pd.Timestamp) -> pd.DatetimeIndex:
    """営業日 = 値段のある日。その最後の日より後は、平日を営業日とみなす（祝日は数えてしまう。売る日は値段のデータで数え直す）。"""
    idx = pd.DatetimeIndex(index).normalize()
    last = idx[-1] if len(idx) else pd.Timestamp(until).normalize() - pd.Timedelta(days=1)
    ahead = pd.bdate_range(last + pd.Timedelta(days=1), pd.Timestamp(until).normalize() + pd.Timedelta(days=40))
    return idx.append(ahead).unique().sort_values()


def bday_add(cal: pd.DatetimeIndex, day: pd.Timestamp, n: int) -> pd.Timestamp:
    pos = int(cal.searchsorted(pd.Timestamp(day).normalize(), side="right")) - 1   # その日（営業日でなければ前の営業日）
    return cal[min(max(pos + n, 0), len(cal) - 1)]


def bdays_after(cal: pd.DatetimeIndex, a: pd.Timestamp, b: pd.Timestamp) -> int:
    """a の翌営業日から b までの営業日の数（b が a 以前なら 0）。"""
    return max(int(cal.searchsorted(pd.Timestamp(b), side="right")) - int(cal.searchsorted(pd.Timestamp(a), side="right")), 0)


# --------------------------------------------------------------------------- 持ち株・現金
@dataclass
class Lot:
    ticker: str
    shares: int
    buy_date: pd.Timestamp
    anchor: pd.Timestamp          # 売る日を数える始まり（買った日。持っている間にまた E1 が出たら、その日）
    cost: float                   # 払った金額（手数料込み）
    due: pd.Timestamp | None = None
    sell_tries: list = field(default_factory=list)   # 予定の日から後に記録した売り（不成立など）の日


def events(mode: str, plan: pd.DataFrame, fills: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    """売り買いの出来事（date / ticker / side / shares / price / fee）。本物は fills.csv の約定、空運転は plan.csv の買い・売り（前の終値で約定したとみなす）。"""
    cols = ["date", "ticker", "side", "shares", "price", "fee"]
    if mode == "real":
        f = fills[(fills["mode"] == "real") & (fills["status"] == "約定")] if len(fills) else fills
        if not len(f):
            return pd.DataFrame(columns=cols)
        return pd.DataFrame({"date": pd.to_datetime(f["date"]), "ticker": f["ticker"].astype(str), "side": f["side"],
                             "shares": f["shares"].astype(int), "price": f["price"].astype(float),
                             "fee": f["fee_yen"].fillna(0).astype(float)})[cols]
    p = plan[(plan["mode"] == "dry") & plan["side"].isin(["買い", "売り"])] if len(plan) else plan
    if not len(p):
        return pd.DataFrame(columns=cols)
    px = []
    for r in p.itertuples():
        c = close[r.ticker].loc[:r.date].dropna() if r.ticker in close else pd.Series(dtype=float)
        px.append(float(r.prev_close) if r.side == "買い" or c.empty else float(c.iloc[-1]))
    return pd.DataFrame({"date": pd.to_datetime(p["date"]), "ticker": p["ticker"].astype(str), "side": p["side"],
                         "shares": p["shares"].astype(int), "price": px, "fee": 0.0})[cols]


def open_lots(ev: pd.DataFrame, plan: pd.DataFrame, fills: pd.DataFrame, mode: str, cal: pd.DatetimeIndex) -> dict[str, Lot]:
    """持っている株（銘柄ごとに 1 つ。持っている株は買い足さない）と、売る日。"""
    lots: dict[str, Lot] = {}
    order = {"売り": 0, "買い": 1}                       # 同じ日は売りを先に
    for r in ev.assign(o=ev["side"].map(order)).sort_values(["date", "o"]).itertuples():
        t = r.ticker
        if r.side == "買い":
            if t in lots:
                lots[t].shares += int(r.shares)
                lots[t].cost += r.shares * r.price + r.fee
            else:
                lots[t] = Lot(t, int(r.shares), r.date, r.date, r.shares * r.price + r.fee)
        elif t in lots:
            lot = lots[t]
            keep = lot.shares - int(r.shares)
            if keep <= 0:
                del lots[t]
            else:
                lot.cost *= keep / lot.shares
                lot.shares = keep
    ext = plan[(plan["mode"] == mode) & (plan["side"] == "延長")] if len(plan) else plan
    for r in ext.itertuples():
        if r.ticker in lots and r.date >= lots[r.ticker].buy_date:
            lots[r.ticker].anchor = max(lots[r.ticker].anchor, r.date)
    for lot in lots.values():
        lot.due = bday_add(cal, lot.anchor, HOLD)
        if mode == "real" and len(fills):
            s = fills[(fills["mode"] == "real") & (fills["ticker"] == lot.ticker) & (fills["side"] == "売り")
                      & fills["status"].isin(["約定", "不成立"]) & (pd.to_datetime(fills["date"]) >= lot.due)]
            lot.sell_tries = sorted(pd.to_datetime(s["date"]).tolist())
    return lots


def cash_and_equity(equity_jpy: float, ev: pd.DataFrame, lots: dict[str, Lot], close: pd.DataFrame) -> tuple[float, float]:
    """記録上の現金と資産（現金＋持っている株を最新の終値で。値段が無ければ買った値段で）。"""
    buy = ev[ev["side"] == "買い"]
    sell = ev[ev["side"] == "売り"]
    cash = equity_jpy - float((buy["shares"] * buy["price"] + buy["fee"]).sum()) + float((sell["shares"] * sell["price"] - sell["fee"]).sum())
    held = 0.0
    for lot in lots.values():
        c = close[lot.ticker].dropna() if lot.ticker in close else pd.Series(dtype=float)
        held += lot.shares * float(c.iloc[-1]) if len(c) else lot.cost
    return cash, cash + held


# --------------------------------------------------------------------------- 誤り・止める決まり
def mistakes(mode: str, plan: pd.DataFrame, fills: pd.DataFrame, lots: dict[str, Lot], cal: pd.DatetimeIndex,
             today: pd.Timestamp) -> list[tuple[pd.Timestamp, str]]:
    """誤り（日・中身）。fills.csv の「誤り」の列、売りの出し忘れ（予定の日の翌営業日を過ぎても売りの記録が無い）、
    一部だけ出さなかった買い（同じ日の計画の買いの一部だけを出した。記録は 2 営業日まで待つ）。"""
    if mode != "real":
        return []
    out: list[tuple[pd.Timestamp, str]] = []
    if len(fills):
        f = fills[fills["mode"] == "real"]
        for r in f[f["error"].notna() & (f["error"].astype(str).str.len() > 0)].itertuples():
            out.append((pd.Timestamp(r.date), f"{r.ticker} {r.side}: {r.error}"))
        for r in f[(f["side"] == "売り") & (f["status"] == "出さなかった")].itertuples():
            out.append((pd.Timestamp(r.date), f"{r.ticker} 売り: 予定の売りを出さなかった"))
    for lot in lots.values():
        if lot.due is not None and bdays_after(cal, lot.due, today) >= 2 and not lot.sell_tries:
            out.append((bday_add(cal, lot.due, 1), f"{lot.ticker} の売りの出し忘れ（予定 {lot.due:%m-%d}。記録も無い）"))
    p = plan[(plan["mode"] == "real") & (plan["side"] == "買い")] if len(plan) else plan
    for day, g in p.groupby("date"):
        if bdays_after(cal, day, today) < 2:
            continue
        f = fills[(fills["mode"] == "real") & (fills["side"] == "買い") & (pd.to_datetime(fills["date"]) == day)] if len(fills) else fills
        st = {t: set(f.loc[f["ticker"] == t, "status"]) for t in g["ticker"]}
        placed = [t for t, s in st.items() if s & {"約定", "不成立"}]
        if placed and len(placed) < len(st):
            for t in sorted(set(st) - set(placed)):
                out.append((pd.Timestamp(day), f"{t} 買い: 同じ日の計画の一部だけを出さなかった"))
    return sorted(out)


def recent_mistakes(errs: list[tuple[pd.Timestamp, str]], cal: pd.DatetimeIndex, today: pd.Timestamp) -> list[tuple[pd.Timestamp, str]]:
    start = bday_add(cal, today, -(ERR_WINDOW - 1))
    return [e for e in errs if start <= e[0] <= today]


def early_cost(rt: pd.DataFrame | None) -> str | None:
    """4.5 の 3: 往復が済んだ最初の 10 回の c の平均が、往復 0.10% を超えていれば、その理由。"""
    if rt is None or not len(rt):
        return None
    c = rt["c"].dropna()
    if len(c) < EARLY_TRIPS:
        return None
    m = float(c.iloc[:EARLY_TRIPS].mean())
    return f"最初の {EARLY_TRIPS} 往復のコストの平均 {m:.3%} が往復 {LIMIT_6:.2%} を超えた（docs/stocks_live.md 4.5 の 3）" if m > LIMIT_6 else None


def stop_checks(cfg: dict, cash_equity: tuple[float, float], errs: list, lots: dict[str, Lot], close: pd.DataFrame,
                cal: pd.DatetimeIndex, today: pd.Timestamp, e1_closed: str | None, rt: pd.DataFrame | None = None) -> list[str]:
    """止める決まり（3.3）に当たったものの理由。"""
    x = float(cfg["equity_jpy"])
    out = []
    early = early_cost(rt)
    if early:
        out.append(early)
    if cash_equity[1] < DD_STOP * x:
        out.append(f"記録上の資産 {cash_equity[1]:,.0f} 円が、入れた金額 {x:,.0f} 円の {DD_STOP:.0%} を下回った")
    rec = recent_mistakes(errs, cal, today)
    if len(rec) >= ERR_MAX:
        out.append(f"直近 {ERR_WINDOW} 営業日に注文の誤りが {len(rec)} 回（{ERR_MAX} 回で止める）")
    for lot in lots.values():
        late = bdays_after(cal, lot.due, today) if lot.due is not None else 0
        if late > SELL_RETRY_MAX:
            out.append(f"{lot.ticker} が予定の日（{lot.due:%m-%d}）から {late} 営業日売れていない（{SELL_RETRY_MAX} 営業日まで）。利用者に相談する")
        c = close[lot.ticker] if lot.ticker in close else pd.Series(dtype=float)
        tail = c.loc[c.index <= today].tail(PRICE_GAP_DAYS)
        if len(tail) == PRICE_GAP_DAYS and tail.isna().all():
            out.append(f"{lot.ticker} の値段が {PRICE_GAP_DAYS} 営業日続けて無い（売買停止・上場廃止の恐れ）。利用者に相談する")
    if e1_closed:
        out.append(f"E1 の帳が閉じた（{e1_closed}。docs/agents.md 12.3）")
    return out


# --------------------------------------------------------------------------- 今日の注文
@dataclass
class Plan:
    rows: list[dict]
    written: bool
    messages: list[str]


def deadline_ts(cfg: dict, today: pd.Timestamp) -> pd.Timestamp | None:
    d = cfg.get("deadline")
    if not d:
        return None
    h, m = (int(v) for v in str(d).split(":"))
    return today + pd.Timedelta(hours=h, minutes=m)


def make_plan(today: pd.Timestamp, now: pd.Timestamp, cfg: dict, e1_today: pd.DataFrame | None, decided_at: str | None,
              close: pd.DataFrame, cal: pd.DatetimeIndex, lots: dict[str, Lot], cash: float, stop: str | None,
              names: dict | None = None) -> list[dict]:
    """その日の行（売り・延長・買い・買わない。何も無ければ「なし」の 1 行）。e1_today が None なら判断の無い日（売りだけ）。"""
    mode = cfg.get("mode", "dry")
    x = float(cfg["equity_jpy"])
    day_budget = math.floor(x / SPLIT)
    dl = deadline_ts(cfg, today)
    margin = (dl - now).total_seconds() / 60 if dl is not None else np.nan
    before = close.loc[close.index < today]
    prev = before.iloc[-1] if len(before) else pd.Series(dtype=float)
    e1 = e1_today if e1_today is not None else pd.DataFrame(columns=["ticker", "name", "reason"])
    e1 = e1.drop_duplicates("ticker")
    names = {**(names or {}), **dict(zip(e1["ticker"], e1["name"]))}
    rows: list[dict] = []

    def row(t, side, shares=0, budget=np.nan, pc=np.nan, due=None, reason="", note=""):
        rows.append({"written_at": f"{now:%Y-%m-%d %H:%M:%S}", "mode": mode, "date": today, "ticker": t, "name": names.get(t, ""),
                     "side": side, "form": f"{FORM}・引け" if side in ("買い", "売り") else "", "shares": int(shares),
                     "budget_yen": budget, "prev_close": pc, "due": due, "reason": reason, "decided_at": decided_at or "",
                     "margin_min": round(margin, 1) if np.isfinite(margin) else np.nan, "note": note})

    again = set(e1["ticker"])
    for t, lot in sorted(lots.items()):
        if t in again:                                           # 2.1 の 3: 持っている株にまた E1 → 買い足さず、売る日を延ばす
            row(t, "延長", lot.shares, due=bday_add(cal, today, HOLD), reason="持っている株にまた E1（売らずに持ち続ける）",
                note=f"前の予定 {lot.due:%m-%d}")
        elif lot.due is not None and lot.due <= today:
            late = bdays_after(cal, lot.due, today)
            row(t, "売り", lot.shares, due=lot.due, reason="5 営業日たった",
                note=f"売りの遅れ {late} 日目（{SELL_RETRY_MAX} 日を超えたら止める）" if late else "")
    cand = sorted(set(e1["ticker"]) - set(lots))
    reasons = dict(zip(e1["ticker"], e1["reason"]))
    if e1_today is not None:
        if stop:
            for t in cand:
                row(t, "買わない", reason=reasons.get(t, ""), note=f"STOP（{stop[:60]}）")
        elif mode == "real" and dl is None:
            for t in cand:
                row(t, "買わない", reason=reasons.get(t, ""), note="受付の終わりの時刻が未記入（docs/stocks_live.md 4.1）")
        elif np.isfinite(margin) and margin < MARGIN_MIN:
            for t in cand:
                row(t, "買わない", reason=reasons.get(t, ""), note=f"間に合わない（受付の終わりまで {margin:.0f} 分。{MARGIN_MIN} 分前まで）")
        else:
            chosen = random_pick(cand, MAX_NAMES, today)
            for t in sorted(set(cand) - set(chosen)):
                row(t, "買わない", reason=reasons.get(t, ""), note=f"でたらめで外した（1 日 {MAX_NAMES} 銘柄まで。種 {today:%Y%m%d}）")
            budget = math.floor(day_budget / len(chosen)) if chosen else 0
            left = cash
            for t in chosen:
                pc = float(prev.get(t, np.nan))
                if not np.isfinite(pc) or pc <= 0:
                    row(t, "買わない", budget=budget, reason=reasons.get(t, ""), note="前の営業日の値段が無い")
                    continue
                n = math.floor(budget / pc)
                if n < 1:
                    row(t, "買わない", budget=budget, pc=pc, reason=reasons.get(t, ""), note=f"高くて買えない（1 株 {pc:,.0f} 円 > {budget:,} 円）")
                elif n > MAX_SHARES:
                    row(t, "買わない", budget=budget, pc=pc, reason=reasons.get(t, ""), note=f"安くて {MAX_SHARES} 株を超える（S 株は 1〜{MAX_SHARES} 株）")
                elif n * pc > left:
                    row(t, "買わない", budget=budget, pc=pc, reason=reasons.get(t, ""), note=f"記録上の現金が足りない（{left:,.0f} 円）")
                else:
                    left -= n * pc
                    row(t, "買い", n, budget, pc, due=bday_add(cal, today, HOLD), reason=reasons.get(t, ""))
    if not rows:
        row("", "なし", reason="今日は注文が無い" if e1_today is not None else "判断が無い日（売りだけ）")
    return rows


# --------------------------------------------------------------------------- 約定を書く
def check_fill(cfg: dict, plan: pd.DataFrame, lots: dict[str, Lot], day: pd.Timestamp, ticker: str, side: str, shares: int,
               price: float, status: str, at: str, stop: str | None, before: pd.DataFrame | None = None) -> tuple[str, bool]:
    """決まりと違えば「誤り」の中身を返す（空なら誤りではない）。2 つ目は「すぐ STOP を置く」か。"""
    if status != "約定":
        return "", False
    errs, stop_now = [], False
    if shares > MAX_SHARES:
        errs.append(f"{shares} 株（S 株は {MAX_SHARES} 株まで）")
    if side == "買い":
        p = plan[(plan["mode"] == "real") & (plan["date"] == day) & (plan["ticker"] == ticker) & (plan["side"] == "買い")] if len(plan) else plan
        if before is not None and len(before) and ((before["mode"] == "real") & (before["side"] == "買い") & (before["status"] == "約定")
                                                   & (before["ticker"] == ticker) & (pd.to_datetime(before["date"]) == day)).any():
            errs.append("同じ日の同じ株の買いが二度")
        if not len(p):
            errs.append("計画に無い買い")
        elif int(p["shares"].iloc[0]) != shares:
            errs.append(f"株数が計画（{int(p['shares'].iloc[0])} 株）と違う")
        limit = WRONG_FACTOR * math.floor(float(cfg["equity_jpy"]) / SPLIT)
        if shares * price > limit:
            errs.append(f"約定の金額 {shares * price:,.0f} 円が 1 日の金額の {WRONG_FACTOR} 倍（{limit:,.0f} 円）を超えた")
            stop_now = True
    else:
        lot = lots.get(ticker)
        if lot is None:
            errs.append("持っていない株の売り")
        else:
            if shares != lot.shares:
                errs.append(f"株数が持ち株（{lot.shares} 株）と違う")
            if lot.due is not None and day < lot.due and not stop:
                errs.append(f"予定（{lot.due:%m-%d}）より前の売り")
            if lot.due is not None and day > lot.due and not any(d < day for d in lot.sell_tries):
                errs.append(f"予定の日（{lot.due:%m-%d}）に売りを出さなかった")
    return "、".join(errs), stop_now


# --------------------------------------------------------------------------- 空運転の進み具合
def dry_run_status(plan: pd.DataFrame, cfg: dict) -> dict:
    p = plan[plan["mode"] == "dry"] if len(plan) else plan
    days = sorted(set(p["date"])) if len(p) else []
    e1 = p[p["side"].isin(E1_SIDES)] if len(p) else p
    e1_days = sorted(set(e1["date"])) if len(e1) else []
    margins = [float(e1.loc[e1["date"] == d, "margin_min"].iloc[0]) for d in e1_days]
    known = [m for m in margins if np.isfinite(m)]
    share = (sum(m >= MARGIN_MIN for m in known) / len(known)) if known else float("nan")
    dl = [deadline_ts(cfg, pd.Timestamp(d)) for d in e1_days]
    dec = [pd.to_datetime(e1.loc[e1["date"] == d, "decided_at"].iloc[0], errors="coerce") for d in e1_days]
    dm = [(a - b).total_seconds() / 60 for a, b in zip(dl, dec) if a is not None and pd.notna(b)]
    decided_share = (sum(m >= MARGIN_MIN for m in dm) / len(dm)) if dm else float("nan")
    reasons = []
    if not cfg.get("deadline"):
        reasons.append("受付の終わりの時刻が未記入（4.1）")
    if len(days) < DRY_MIN_DAYS:
        reasons.append(f"空運転の日 {len(days)} 日（{DRY_MIN_DAYS} 日まで）")
    if len(e1) < DRY_MIN_ROWS:
        reasons.append(f"E1 の行 {len(e1)} 行（{DRY_MIN_ROWS} 行まで。決算の季節を待つ）")
    if not (np.isfinite(share) and share >= DRY_MARGIN_SHARE):
        reasons.append(f"受付の終わりの {MARGIN_MIN} 分前までに注文の一覧を作れた日の割合 "
                       + (f"{share:.0%}" if np.isfinite(share) else "–") + f"（{DRY_MARGIN_SHARE:.0%} 以上が要る）")
    return {"days": len(days), "e1_rows": len(e1), "e1_days": len(e1_days), "margin_share": share, "decided_share": decided_share,
            "ok": not reasons, "reasons": reasons}


# --------------------------------------------------------------------------- コスト（c*）
def ref_lookup(refs: pd.DataFrame) -> dict[tuple[str, pd.Timestamp], tuple[float, float]]:
    if refs is None or not len(refs):
        return {}
    r = refs.sort_values("fetched_at").drop_duplicates(["date", "ticker"], keep="first")   # 先に取った値を使う（後で取ると分割で変わることがある）
    return {(str(t), pd.Timestamp(d)): (float(o), float(c)) for t, d, o, c in zip(r["ticker"], r["date"], r["open"], r["close"])}


def _ref(lk: dict, ticker: str, day: pd.Timestamp, at: str) -> float | None:
    v = lk.get((ticker, pd.Timestamp(day)))
    if v is None or at not in ("引け", "寄り"):
        return None
    x = v[1] if at == "引け" else v[0]
    return x if np.isfinite(x) and x > 0 else None


def round_trips(fills: pd.DataFrame, plan: pd.DataFrame, refs: pd.DataFrame, close: pd.DataFrame, cal: pd.DatetimeIndex) -> pd.DataFrame:
    """出した買い 1 回ごとの結果（docs/stocks_live.md 5.1〜5.3）。

    列: date / ticker / status / done（片づいたか）/ c（約定した往復のコスト）/ t（時刻のずれ）/ m（不成立の株の、紙の上の 5 日の差）/ excluded
    """
    cols = ["date", "ticker", "status", "done", "c", "t", "m", "excluded", "limit_up"]
    if fills is None or not len(fills):
        return pd.DataFrame(columns=cols)
    f = fills[fills["mode"] == "real"].copy()
    f["date"] = pd.to_datetime(f["date"])
    buys = f[(f["side"] == "買い") & f["status"].isin(["約定", "不成立"])].sort_values(["date", "recorded_at"])
    sells = f[(f["side"] == "売り") & (f["status"] == "約定")].sort_values(["date", "recorded_at"])
    lk = ref_lookup(refs)
    pr = plan[(plan["mode"] == "real")] if len(plan) else plan
    cc = close.pct_change(fill_method=None)
    out = []
    for b in buys.itertuples():
        rec = {"date": b.date, "ticker": b.ticker, "status": b.status, "done": False, "c": np.nan, "t": np.nan, "m": np.nan, "excluded": "",
               "limit_up": False}
        if b.status == "不成立":
            if b.ticker in cc and b.date in cc.index:
                rec["limit_up"] = bool(cc.at[b.date, b.ticker] >= LIMIT_UP_DAY)
            end = bday_add(cal, b.date, HOLD)
            if b.ticker in close and end <= close.index[-1] and b.date in close.index:
                w = cc.loc[(cc.index > b.date) & (cc.index <= end)]
                rec["m"] = float((1 + w[b.ticker]).prod() - 1 - ((1 + w).prod() - 1).mean())   # 株 − 全銘柄の平均（5 日）
                rec["done"] = np.isfinite(rec["m"])
            out.append(rec)
            continue
        s = sells[(sells["ticker"] == b.ticker) & (sells["date"] >= b.date)]
        got, value, fee, last = 0, 0.0, 0.0, None
        for r in s.itertuples():
            take = min(int(r.shares), int(b.shares) - got)
            got += take
            value += take * float(r.price)
            fee += float(r.fee_yen or 0) * take / max(int(r.shares), 1)
            last = r
            if got >= int(b.shares):
                break
        if got < int(b.shares) or last is None:
            out.append(rec)                                      # まだ売っていない
            continue
        if b.at == "後場寄り" or last.at == "後場寄り":
            rec.update(done=True, excluded="後場始値で約定（無料の日足に基準の値段が無い）")
            out.append(rec)
            continue
        rb, rs = _ref(lk, b.ticker, b.date, b.at), _ref(lk, b.ticker, last.date, last.at)
        ext = pr[(pr["ticker"] == b.ticker) & (pr["side"] == "延長") & (pr["date"] >= b.date) & (pr["date"] <= last.date)]
        due = bday_add(cal, max([b.date] + list(ext["date"])), HOLD)   # 紙の上の売りの日（値段のある日で数え直す）
        p0, p5 = _ref(lk, b.ticker, b.date, "引け"), _ref(lk, b.ticker, due, "引け")
        if rb is None or rs is None or p0 is None or p5 is None:
            out.append(rec)                                      # 基準の値段を待つ
            continue
        q, pb, ps = int(b.shares), float(b.price), value / got
        rec["c"] = (pb - rb) / rb + (rs - ps) / rs + (float(b.fee_yen or 0) + fee) / (q * rb)
        rec["t"] = (rb - p0) / p0 + (p5 - rs) / p5
        rec["done"] = True
        out.append(rec)
    return pd.DataFrame(out, columns=cols)


def _upper(x: np.ndarray) -> tuple[float, float, float]:
    """平均・標準誤差・「平均 ＋ 2 × 標準誤差」（下限 0.02%）。"""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return float("nan"), float("nan"), float("nan")
    m, se = float(x.mean()), float(x.std(ddof=1) / np.sqrt(len(x)))
    return m, se, max(C_FLOOR, m + 2 * se)


def c_star(rt: pd.DataFrame) -> dict:
    """c* を決める時点に届いたか、届いたら値（docs/stocks_live.md 5.3）。最初の 60 回（往復が 30 回に届かなければ 120 回まで）で決め、後から計算し直さない。"""
    out = {"orders": len(rt), "filled": int((rt["status"] == "約定").sum()) if len(rt) else 0,
           "unfilled": int((rt["status"] == "不成立").sum()) if len(rt) else 0, "fixed": False, "gave_up": False, "k": None,
           "limit_up": int(rt["limit_up"].fillna(False).astype(bool).sum()) if len(rt) and "limit_up" in rt else 0}
    n_all = rt["c"].notna().sum() if len(rt) else 0
    out["mean_now"], out["se_now"], out["upper_now"] = _upper(rt["c"]) if n_all >= 2 else (np.nan, np.nan, np.nan)
    for k in range(FIX_ORDERS, min(len(rt), GIVE_UP_ORDERS) + 1):
        w = rt.iloc[:k]
        if not w["done"].all():
            return out                                          # 最初の k 回が片づくのを待つ（その先は見ない）
        n = int(w["c"].notna().sum())
        if n >= MIN_ROUNDTRIPS:
            nf = int((w["status"] == "不成立").sum())
            p = nf / k
            m, se, up = _upper(w["c"])
            mix = np.concatenate([w["c"].dropna().to_numpy(), w.loc[w["status"] == "不成立", "m"].to_numpy()])
            m2, se2, up2 = _upper(mix)
            ct = (w["c"] + w["t"]).dropna().to_numpy()
            m6, se6, up6 = _upper(ct)
            out.update(fixed=True, k=k, n=n, nf=nf, p_nf=p, mean=m, se=se, c_star=up if p < NF_LIMIT else up2,
                       rule="約定した往復のコスト" if p < NF_LIMIT else f"不成立が {NF_LIMIT:.0%} 以上なので、不成立の株の紙の上の 5 日の差を足したもの",
                       c6=up6, mean_t=float(np.nanmean(w["t"])), ok6=bool(up6 <= LIMIT_6))
            return out
    if len(rt) >= GIVE_UP_ORDERS and rt.iloc[:GIVE_UP_ORDERS]["done"].all():
        out["gave_up"] = True                                   # 120 回でも往復が 30 回に届かない → c* は決めず、判定は 0.10%
    return out


def fetch_refs_yahoo(tickers: list[str], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Yahoo の調整していない始値・終値（auto_adjust=False）。列: date / ticker / open / close。"""
    import yfinance as yf  # type: ignore

    rows = []
    for t in tickers:
        d = yf.Ticker(t).history(start=f"{start:%Y-%m-%d}", end=f"{end + pd.Timedelta(days=1):%Y-%m-%d}", auto_adjust=False)
        if d is None or d.empty:
            continue
        idx = d.index.tz_localize(None) if d.index.tz is not None else d.index
        for day, o, c in zip(idx.normalize(), d["Open"], d["Close"]):
            rows.append({"date": day, "ticker": t, "open": float(o), "close": float(c)})
    return pd.DataFrame(rows, columns=["date", "ticker", "open", "close"])


def needed_refs(fills: pd.DataFrame, plan: pd.DataFrame, refs: pd.DataFrame, today: pd.Timestamp) -> dict[str, list[pd.Timestamp]]:
    """基準の値段がまだ無い（銘柄 → 日）。約定した日・計画の買いの日・売る予定の日（今日より前のもの）。"""
    have = set(ref_lookup(refs))
    need: dict[str, set] = {}
    if len(fills):
        f = fills[(fills["mode"] == "real") & (fills["status"] == "約定")]
        for t, d in zip(f["ticker"], pd.to_datetime(f["date"])):
            need.setdefault(str(t), set()).add(d)
    if len(plan):
        p = plan[(plan["mode"] == "real") & plan["side"].isin(["買い", "延長", "売り"])]
        for t, d, due in zip(p["ticker"], p["date"], p["due"]):
            need.setdefault(str(t), set()).add(pd.Timestamp(d))
            if pd.notna(due):
                need[str(t)].add(pd.Timestamp(due))
    return {t: sorted(d for d in ds if d < today and (t, d) not in have) for t, ds in need.items()
            if any(d < today and (t, d) not in have for d in ds)}


def update_refs(files: LiveFiles, fills: pd.DataFrame, plan: pd.DataFrame, today: pd.Timestamp, now: pd.Timestamp, cal: pd.DatetimeIndex,
                fetch: Callable[[list[str], pd.Timestamp, pd.Timestamp], pd.DataFrame] = fetch_refs_yahoo) -> list[str]:
    need = needed_refs(fills, plan, files.refs(), today)
    if not need:
        return []
    msgs = []
    start = min(min(v) for v in need.values())
    got = fetch(sorted(need), start, today)
    rows = []
    for t, days in need.items():
        g = got[got["ticker"] == t]
        for d in days:
            r = g[pd.to_datetime(g["date"]) == d]
            if not len(r):
                continue
            late = bdays_after(cal, d, today)
            rows.append({"date": d, "ticker": t, "open": float(r["open"].iloc[0]), "close": float(r["close"].iloc[0]),
                         "fetched_at": f"{now:%Y-%m-%d %H:%M:%S}", "source": "yahoo" + (f"（{late} 営業日後に取った）" if late > REF_DAYS else "")})
            if late > REF_DAYS:
                msgs.append(f"{t} {d:%Y-%m-%d} の基準の値段は {late} 営業日後に取った（{REF_DAYS} 営業日以内の決まり）")
    files.append(files.refs_path, rows, REF_COLUMNS)
    msgs.append(f"基準の値段（調整していない始値・終値）を {len(rows)} 件書き足した")
    return msgs
