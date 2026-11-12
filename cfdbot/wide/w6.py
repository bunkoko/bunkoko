"""W6 物言う株主の新しい取得。docs/wide.md 4 章 W6（結果を見る前に決めたもの）。

EDINET の日ごとの書類の一覧（10 年分）から、名前の一覧にある株主が新しく出した「大量保有報告書」を拾い、
出した日の次の営業日の引けで買って 20 営業日後の引けで売る。事象ごとの値 = 株 − 同じ期間の全銘柄の平均 − 0.10%。

一覧は fetch（./cfd wide --fetch-edinet）で data/wide に書く。2 秒に 1 件・同時に 1 本（cfdbot.edinet.Client）。止めても続きから。
買って持つ部品は W5（w5.py）と同じもの。
"""

from __future__ import annotations

import csv
import os
import re
import unicodedata
from pathlib import Path

import pandas as pd

from .. import edinet as ED
from . import base
from .w5 import drop_line, event_stream, hold_events, jst, load_close, span, span_line

KEY = "W6"
HOLD = 20                                         # 買いの日から売りの日までの営業日の数
DIR = Path("wide")                                # data の下
F350, FCODES = DIR / "edinet_350.csv", DIR / "edinet_codes.csv"
FLISTED, FLOCK = DIR / "edinet_listed.csv", DIR / "edinet.lock"
COLS_350 = ["docID", "submitDateTime", "filerName", "edinetCode", "issuerEdinetCode", "docDescription", "withdrawalStatus", "listed_on"]
COLS_CODES = ["edinetCode", "secCode", "filerName", "seen_on"]
COLS_LISTED = ["date", "n", "n350", "checked_at"]
DOC = "350"                                       # 大量保有報告書（変更報告書も同じ種類。書類の説明で分ける）
HOLDING = {"350", "360", "370", "380"}            # 大量保有の書類。証券の番号の対応づけには使わない（その会社自身の書類から作る）
COVER = 0.95                                      # 見るはずの平日のうち、一覧を見た日がこれに届かなければ点検を通さない
FETCH_HINT = "nohup ./cfd wide --fetch-edinet >> data/wide/edinet.log 2>&1 &"
# 名前の一覧（docs/wide.md 4 章 W6。結果を見て足したり引いたりしない）。空白と「・」を除き、全角と半角・大文字と小文字をそろえて「含む」かを見る
NAMES = ("エフィッシモ", "オアシスマネジメント", "ストラテジックキャピタル", "シティインデックスイレブンス", "南青山不動産",
         "エスグラントコーポレーション", "スリーディーインベストメントパートナーズ", "シルチェスター", "バリューアクト", "エリオット",
         "ダルトン", "ニッポンアクティブバリュー", "アセットバリューインベスターズ", "タイヨウパシフィック", "タイヨウファンド",
         "ひびきパース", "サードポイント", "キングストリート", "ファラロン", "カタリスト投資顧問", "シンフォニーフィナンシャル",
         "いちごアセットマネジメント",
         "effissimo", "oasismanagement", "strategiccapital", "3dinvestmentpartners", "silchester", "valueact", "elliott", "dalton",
         "nipponactivevalue", "assetvalueinvestors", "taiyopacific", "thirdpoint", "kingstreet", "farallon")
EXACT = ("株式会社レノ",)                          # そろえた後の名前が完全に同じときだけ（「レノ」を含む別の会社を拾わない）


# --------------------------------------------------------------------------- 名前と書類の見分け
def norm(name) -> str:
    """全角と半角をそろえ（NFKC）、空白と「・」を除き、大文字と小文字をそろえる。"""
    return re.sub(r"[\s・]", "", unicodedata.normalize("NFKC", str(name))).casefold()


_NAMES, _EXACT = tuple(norm(x) for x in NAMES), {norm(x) for x in EXACT}


def is_activist(name) -> bool:
    n = norm(name)
    return n in _EXACT or any(x in n for x in _NAMES)


def is_new_report(desc) -> bool:
    """書類の説明が「大量保有報告書」で始まり、「変更報告書」「訂正」を含まない。"""
    t = unicodedata.normalize("NFKC", str(desc)).strip()
    return t.startswith("大量保有報告書") and "変更報告書" not in t and "訂正" not in t


def issuer_tickers(codes: pd.DataFrame, tickers) -> dict[str, str]:
    """会社の EDINET の番号 → 値段の表の銘柄（証券の番号の頭 4 文字 ＋ .T）。値段の表に無いものは入れない。
    1 つの番号に 2 つ以上の銘柄が結びつくときは、最後に見たほう。"""
    c = codes.assign(edinetCode=codes["edinetCode"].str.strip(), ticker=codes["secCode"].str.strip().str[:4] + ".T")
    c = c[(c["edinetCode"] != "") & c["ticker"].isin(set(tickers))].sort_values("seen_on", kind="stable")
    return dict(zip(c["edinetCode"], c["ticker"]))


# --------------------------------------------------------------------------- 取得（Mac で。1 時間半ほど）
def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="Asia/Tokyo").tz_localize(None).normalize()


def _lock(path: Path) -> bool:
    """同時に 1 本だけ（cfdbot.edinet.Store.lock と同じ形）。動いている別の取得があれば False。"""
    if path.exists():
        try:
            pid = int(path.read_text().strip() or 0)
            if pid > 0:
                os.kill(pid, 0)
                return False
        except (OSError, ValueError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))
    return True


def _append(path: Path, rows: list[dict], cols: list[str]) -> None:
    if not rows:
        return
    new = not path.exists() or path.stat().st_size == 0          # 空のまま残ったファイルにも見出しを書く
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def _s(x) -> str:
    return "" if x is None else str(x).strip()


def fetch(data: Path, log=print, client: ED.Client | None = None, today=None, days=None) -> None:
    """まだ見ていない平日の一覧（type=2）を 1 日ずつ見て、350 の書類と、会社の EDINET の番号 → 証券の番号を書き足す。

    見る日は 10 年前の 1 週間後から昨日まで（今日の一覧はまだ増えるので見ない）。見終えた日は edinet_listed.csv に書き、
    次からは飛ばす。取れなかった日は書かない（次の実行でもう一度見る）。キーは画面にも記録にも出さない。
    """
    if client is None:
        api = ED.load_key()
        if not api:
            raise base.NotReady("EDINET の API キーが無い。Mac の前で ./cfd edinet key（docs/todo.md の 7）")
        client = ED.Client(ED.http_getter(api), log=log)
    today = pd.Timestamp(today).normalize() if today is not None else _now()
    root = Path(data)
    lock = root / FLOCK
    if not _lock(lock):
        raise RuntimeError(f"別の取得が動いている（{lock}）。終わるのを待つ（止まっているのに残っていたら、その印を消す）")
    try:
        seen = set(_table(root / FLISTED, COLS_LISTED, need=False)["date"])
        codes = _table(root / FCODES, COLS_CODES, need=False)
        known = set(zip(codes["edinetCode"], codes["secCode"]))
        todo = [pd.Timestamp(d) for d in (ED.fetch_days(today) if days is None else days)]
        todo = [d for d in todo if d < today and f"{d:%Y-%m-%d}" not in seen]
        log(f"  W6: EDINET の一覧 まだ見ていない平日 {len(todo)} 日（2 秒に 1 件。約 {len(todo) * ED.LIST_INTERVAL / 60:.0f} 分）")
        done = failed = 0
        for d in todo:
            day = f"{d:%Y-%m-%d}"
            status, payload = client.documents(d)
            meta = ED.list_status(payload)
            if status != 200 or meta != "200":
                failed += 1
                log(f"  W6: {day} の一覧が取れない（状態 {status}・{meta or '–'}）。次の実行でもう一度見る")
                continue
            rs = ED.results(payload)
            r350 = [{**{c: _s(r.get(c)) for c in COLS_350}, "listed_on": day} for r in rs if _s(r.get("docTypeCode")) == DOC]
            new = []
            for r in rs:
                pair = (_s(r.get("edinetCode")), _s(r.get("secCode")))
                if all(pair) and _s(r.get("docTypeCode")) not in HOLDING and pair not in known:
                    known.add(pair)
                    new.append({"edinetCode": pair[0], "secCode": pair[1], "filerName": _s(r.get("filerName")), "seen_on": day})
            _append(root / F350, r350, COLS_350)
            _append(root / FCODES, new, COLS_CODES)
            _append(root / FLISTED, [{"date": day, "n": len(rs), "n350": len(r350), "checked_at": f"{pd.Timestamp.now():%Y-%m-%d %H:%M}"}],
                    COLS_LISTED)
            done += 1
            if done % 50 == 0:
                log(f"  W6: 一覧 {done}/{len(todo)} 日（{day} まで）")
        log(f"  W6: 一覧を見た日 {done} 日・取れなかった日 {failed} 日")
    finally:
        lock.unlink(missing_ok=True)
    if failed:
        raise RuntimeError(f"一覧が取れなかった平日が {failed} 日ある。時間をおいて同じコマンドで続きから（{FETCH_HINT}）")


# --------------------------------------------------------------------------- 読み取りと事象
def _table(path: Path, cols: list[str], need: bool = True) -> pd.DataFrame:
    if not path.exists():
        if need:
            raise base.NotReady(f"{path} が無い。先に {FETCH_HINT}（1 時間半ほど）")
        return pd.DataFrame(columns=cols, dtype=str)
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception as e:                        # 空・壊れたファイル
        raise base.NotReady(f"{path} を読めない（{type(e).__name__}）") from e
    miss = [c for c in cols if c not in df.columns]
    if miss:
        raise base.NotReady(f"{path} に列が無い: {'、'.join(miss)}（{'・'.join(cols)} のはず）")
    return df


def _prepare(data: Path) -> dict:
    root = Path(data)
    close = load_close(root)
    listed = _table(root / FLISTED, COLS_LISTED)
    days = pd.to_datetime(listed["date"], format="%Y-%m-%d", errors="coerce")
    if days.empty or days.isna().any():
        raise base.NotReady(f"{root / FLISTED} が空か、日付でない行がある（{FETCH_HINT} で取り直す）")
    raw = _table(root / F350, COLS_350)
    # 同じ書類が 2 つの日の一覧に出ることがある（取下げなどの操作の日）。どれか 1 つでも取り下げの印があれば使わない
    gone = set(raw.loc[~raw["withdrawalStatus"].str.strip().isin(["0", ""]), "docID"])
    rows = raw.drop_duplicates("docID")
    codes = _table(root / FCODES, COLS_CODES)
    when = pd.Series(jst(rows["submitDateTime"]), index=rows.index)
    new = rows["docDescription"].map(is_new_report).astype(bool) & ~rows["docID"].isin(gone) & when.notna()
    act = new & rows["filerName"].map(is_activist).astype(bool)
    mapping = issuer_tickers(codes, close.columns)
    tick = rows["issuerEdinetCode"].str.strip().map(mapping)
    hit = act & tick.notna()
    ev = rows[hit].assign(when=when[hit], day=when[hit].dt.normalize(), ticker=tick[hit],
                          issuer=rows.loc[hit, "issuerEdinetCode"].str.strip(),
                          filer=rows.loc[hit, "edinetCode"].str.strip().where(rows.loc[hit, "edinetCode"].str.strip() != "",
                                                                                rows.loc[hit, "filerName"].map(norm)))
    ev = ev.sort_values(["when", "docID"], kind="stable").drop_duplicates(["filer", "issuer"])   # 同じ出した人 × 同じ会社は最初の 1 回
    return {"close": close, "days": pd.DatetimeIndex(days).unique().sort_values(), "rows": rows, "codes": codes, "mapping": mapping,
            "n_bad_time": int(when.isna().sum()), "n_new": int(new.sum()), "n_act": int(act.sum()), "n_hit": int(hit.sum()), "ev": ev}


def _counts(p: dict) -> str:
    return (f"350 の書類 {len(p['rows'])} 件 →「大量保有報告書」で始まり取り下げのないもの {p['n_new']} 件 → 名前の一覧に合うもの {p['n_act']} 件 → "
            f"相手の会社が値段の表の銘柄に結びつくもの {p['n_hit']} 件 → 同じ出した人 × 同じ会社の最初の 1 回 {len(p['ev'])} 件")


def compute(data: Path) -> base.Stream:
    p = _prepare(data)
    days, close = p["days"], p["close"]
    d, g, used, drop = event_stream(close, p["ev"], HOLD, days[0], days[-1] + pd.offsets.BDay(1))
    weekdays = pd.bdate_range(max(days[0], d.index[0].to_period("M").to_timestamp()), d.index[-1])
    gap = len(weekdays.difference(days))
    notes = [
        (f"期間 {d.index[0]:%Y-%m}〜{d.index[-1]:%Y-%m}（{len(d)} か月）。EDINET の一覧を見た日 {len(days)} 日（{days[0]:%Y-%m-%d}〜"
         f"{days[-1]:%Y-%m-%d}。期間の中で見ていない平日 {gap} 日）、値段の最後の日 {close.index[-1]:%Y-%m-%d}"),
        _counts(p) + f" → 使った {len(used)} 件" + (f"。提出の日時が読めない行 {p['n_bad_time']} 件は使わない" if p["n_bad_time"] else ""),
        drop_line(drop),
        (f"買い = 出した日の次の営業日の引け、売り = {HOLD} 営業日後の引け。値 = 株 − 同じ期間の全銘柄の平均 − {base.COST_JP:.2%}。"
         f"名前の一覧は {len(NAMES) + len(EXACT)} 個（登録のまま）"),
    ]
    return base.Stream(KEY, d, events=len(used), gross=float(g.mean()), notes=notes)


def check(data: Path, today=None) -> base.Check:
    try:
        p = _prepare(data)
        start, end = span(p["close"].index, HOLD, p["days"][0], p["days"][-1] + pd.offsets.BDay(1))
    except base.NotReady as e:
        return base.Check(KEY, False, [f"フォルダ {Path(data) / DIR}（{F350.name}・{FCODES.name}・{FLISTED.name}）"], why=str(e))
    today = pd.Timestamp(today).normalize() if today is not None else _now()
    expect = pd.DatetimeIndex([d for d in ED.fetch_days(today) if d < today])
    days, close = p["days"], p["close"]
    have = len(expect.intersection(days))
    share = have / len(expect) if len(expect) else 0.0
    used, drop = hold_events(close, p["ev"], HOLD)
    lines = [
        (f"一覧を見た日 {len(days)} 日（{days[0]:%Y-%m-%d}〜{days[-1]:%Y-%m-%d}）。見るはずの平日 {len(expect)} 日"
         f"（{expect[0]:%Y-%m-%d}〜{expect[-1]:%Y-%m-%d}）のうち {have} 日（{share:.1%}）"),
        _counts(p) + (f"。提出の日時が読めない行 {p['n_bad_time']} 件" if p["n_bad_time"] else ""),
        f"証券の番号の対応 {len(p['codes'])} 行（値段の表の {close.shape[1]} 銘柄のうち、結びつく会社 {len(set(p['mapping'].values()))} 社）",
        f"買いと売りの日に値段のある事象 {len(used)} 件。{drop_line(drop)}",
        span_line(used, start, end),
    ]
    if share < COVER:
        return base.Check(KEY, False, lines, why=f"一覧を見た日が見るはずの平日の {share:.1%}（{COVER:.0%} に届かない）。{FETCH_HINT} で続きから")
    return base.Check(KEY, True, lines)
