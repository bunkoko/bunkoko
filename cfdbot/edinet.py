"""EDINET API v2（金融庁）から、有価証券報告書の本文の節を取る（docs/text_signals.md 8 章）。1C と案 2 で同じ部品を使う。

調査役の第 6 回（公式の文の検索の要約。仕様書の PDF の本文は未読）で分かった形だけを使い、違えば check が日本語で報告する:
- ベース `https://api.edinet-fsa.go.jp/api/v2/`。キーはクエリの `Subscription-Key`
- 書類の一覧 `documents.json?date=YYYY-MM-DD&type=2`（1 日ずつ。`results` に docID・secCode（5 桁）・docTypeCode・submitDateTime・
  periodStart・periodEnd・csvFlag など）。有報は docTypeCode 120（訂正 130 は使わない）
- 書類 `documents/{docID}?type=5`（XBRL を CSV にした zip。UTF-16・タブ区切り。csvFlag が "1" のときだけ）、無ければ type=1（XBRL の zip）
- 本文の節（テキストブロックの要素。値は HTML の断片）: 事業の内容 DescriptionOfBusinessTextBlock・事業等のリスク BusinessRisksTextBlock・
  経営者による分析 ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock
- 受理から 10 年さかのぼれる。本文が XBRL に入ったのは 2013-09 から
- 回数の公式の上限は無いが「短時間の大量のアクセス」は禁止 → 一覧は 2 秒に 1 件、書類は 3 秒に 1 件、同時に 1 本。断られたら 10・30・60 分待つ
キーはこのリポジトリの外（~/.bunkoko/edinet.json、権限 600）。取った本文は data/edinet（コミットしない）。
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

BASE = "https://api.edinet-fsa.go.jp/api/v2/"
KEY_FILE = Path.home() / ".bunkoko" / "edinet.json"
LIST_INTERVAL, DOC_INTERVAL = 2.0, 3.0         # 一覧は 2 秒に 1 件、書類は 3 秒に 1 件（同時に 1 本）
WAITS = (600.0, 1800.0, 3600.0)               # 断られた・つながらないとき、10・30・60 分待ってやり直す
YUHO = "120"                                   # 有価証券報告書（訂正 130 は使わない）
SECTIONS = {"business": "DescriptionOfBusinessTextBlock", "risks": "BusinessRisksTextBlock",
            "mdna": "ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock"}
SECTION_NAMES = {"business": "事業の内容", "risks": "事業等のリスク", "mdna": "経営者による財政状態・経営成績等の分析"}
RETENTION_YEARS = 10
INDEX_COLUMNS = ["docID", "secCode", "ticker", "filerName", "submitDateTime", "periodStart", "periodEnd", "docTypeCode",
                 "source", "chars_business", "chars_risks", "chars_mdna", "fetched_at"]
CAND_COLUMNS = ["docID", "secCode", "ticker", "filerName", "submitDateTime", "periodStart", "periodEnd", "docTypeCode",
                "csvFlag", "xbrlFlag", "withdrawalStatus", "listed_on"]


class Stop(RuntimeError):
    """続けると相手に迷惑か、意味が無いので止める（キーが無い・断られ続けた）。"""


# --------------------------------------------------------------------------- キー
def load_key(path: Path = KEY_FILE) -> str | None:
    k = os.environ.get("EDINET_API_KEY", "").strip()
    if k:
        return k
    if path.exists():
        try:
            return str(json.loads(path.read_text(encoding="utf-8")).get("api_key", "")).strip() or None
        except (OSError, ValueError):
            return None
    return None


def save_key(key: str, path: Path = KEY_FILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"api_key": key.strip()}), encoding="utf-8")
    path.chmod(0o600)
    return path


def http_getter(key: str, timeout: float = 120.0) -> Callable[[str, dict], tuple[int, bytes]]:
    """getter(path, params) → (状態, 中身)。キーはクエリに入れる（記録・画面には出さない）。"""
    def get(path: str, params: dict) -> tuple[int, bytes]:
        url = BASE + path + "?" + urlencode({**params, "Subscription-Key": key})
        req = Request(url, headers={"User-Agent": "bunkoko-research/1.0"})
        try:
            with urlopen(req, timeout=timeout) as r:
                return int(r.status), r.read()
        except HTTPError as e:
            return int(e.code), e.read() if hasattr(e, "read") else b""
        except URLError as e:
            return -1, str(e.reason).encode("utf-8", "replace")
    return get


def ticker_code(ticker: str) -> str:
    """7203.T → 72030（EDINET の secCode は 5 桁）。"""
    return str(ticker).split(".")[0].upper() + "0"


# --------------------------------------------------------------------------- 呼び出し（間隔と、断られたときの待ち）
class Client:
    def __init__(self, getter: Callable[[str, dict], tuple[int, bytes]], sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print,
                 list_interval: float = LIST_INTERVAL, doc_interval: float = DOC_INTERVAL, waits: tuple = WAITS):
        self.getter, self.sleep, self.clock, self.log = getter, sleep, clock, log
        self.list_interval, self.doc_interval, self.waits = list_interval, doc_interval, waits
        self.calls = 0
        self._last: float | None = None

    def _call(self, path: str, params: dict, interval: float) -> tuple[int, bytes]:
        for attempt in range(len(self.waits) + 1):
            if self._last is not None:
                rest = interval - (self.clock() - self._last)
                if rest > 0:
                    self.sleep(rest)
            self._last = self.clock()
            status, body = self.getter(path, params)
            self.calls += 1
            if status in (429, -1) or status >= 500:
                if attempt == len(self.waits):
                    raise Stop(f"断られ続けた（状態 {status}）。時間をおいて、同じコマンドで続きから")
                self.log(f"  EDINET が状態 {status}。{self.waits[attempt] / 60:.0f} 分待ってやり直す")
                self.sleep(self.waits[attempt])
                continue
            if status in (401, 403):
                raise Stop(f"キーが通らない（状態 {status}）。./cfd edinet key で入れ直す")
            return status, body
        raise Stop("やり直しの回数を使い切った")

    def documents(self, day: pd.Timestamp) -> tuple[int, dict]:
        status, body = self._call("documents.json", {"date": f"{pd.Timestamp(day):%Y-%m-%d}", "type": 2}, self.list_interval)
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            payload = {}
        return status, payload

    def document(self, doc_id: str, kind: int) -> tuple[int, bytes]:
        return self._call(f"documents/{doc_id}", {"type": kind}, self.doc_interval)


def results(payload: dict) -> list[dict]:
    r = payload.get("results") if isinstance(payload, dict) else None
    return [x for x in r if isinstance(x, dict)] if isinstance(r, list) else []


def list_status(payload: dict) -> str:
    """一覧の応答の metadata の状態（"200" のはず）。"""
    m = payload.get("metadata") if isinstance(payload, dict) else None
    return str(m.get("status")) if isinstance(m, dict) else ""


# --------------------------------------------------------------------------- 本文の節を取り出す
def html_to_text(s: str) -> str:
    t = html.unescape(str(s))
    t = re.sub(r"(?i)<\s*(br|/p|/div|/tr|/li|/h\d)\s*/?>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t　\xa0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def sections_from_csv_zip(blob: bytes) -> dict[str, str]:
    """type=5（XBRL を CSV にした zip。UTF-16・タブ区切り）から、3 つの節の文章。見つからない節は入れない。"""
    out: dict[str, list[str]] = {}
    want = {v: k for k, v in SECTIONS.items()}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            if not name.lower().endswith(".csv"):
                continue
            raw = z.read(name)
            try:
                text = raw.decode("utf-16")
            except UnicodeDecodeError:
                text = raw.decode("utf-8", "replace")
            rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
            if not rows:
                continue
            head = [h.strip().strip('"') for h in rows[0]]
            ei = next((i for i, h in enumerate(head) if "要素ID" in h or h.lower() == "element id"), 0)
            vi = next((i for i, h in enumerate(head) if h == "値" or h.lower() == "value"), len(head) - 1)
            for r in rows[1:]:
                if len(r) <= max(ei, vi):
                    continue
                key = want.get(str(r[ei]).split(":")[-1].strip())
                if key:
                    t = html_to_text(r[vi])
                    if t and t not in out.get(key, []):
                        out.setdefault(key, []).append(t)
    return {k: "\n".join(v) for k, v in out.items()}


def sections_from_xbrl_zip(blob: bytes) -> dict[str, str]:
    """type=1（XBRL の zip）の本文の XBRL（PublicDoc の .xbrl）から、3 つの節の文章。"""
    out: dict[str, str] = {}
    pat = re.compile(r"<(?:[\w-]+:)?(" + "|".join(SECTIONS.values()) + r")\b[^>]*>(.*?)</(?:[\w-]+:)?\1>", re.S)
    want = {v: k for k, v in SECTIONS.items()}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            if not name.lower().endswith(".xbrl") or "auditdoc" in name.lower():
                continue
            text = z.read(name).decode("utf-8", "replace")
            for m in pat.finditer(text):
                key = want[m.group(1)]
                t = html_to_text(m.group(2))
                if t and len(t) > len(out.get(key, "")):
                    out[key] = t
    return out


# --------------------------------------------------------------------------- 置き場（data/edinet。コミットしない）
class Store:
    def __init__(self, root: str | Path = "data/edinet"):
        self.root = Path(root)

    @property
    def listed_path(self) -> Path:
        return self.root / "listed.csv"           # 一覧を見た日（同じ日は二度見ない）

    @property
    def cand_path(self) -> Path:
        return self.root / "candidates.csv"       # 一覧で見つけた、対象の会社の有報

    @property
    def index_path(self) -> Path:
        return self.root / "index.csv"            # 本文を取った有報

    def doc_path(self, ticker: str, doc_id: str) -> Path:
        return self.root / "docs" / ticker / f"{doc_id}.json"

    def _read(self, path: Path, cols: list[str]) -> pd.DataFrame:
        if not path.exists():
            return pd.DataFrame(columns=cols)
        return pd.read_csv(path, dtype=str, keep_default_na=False)

    def _append(self, path: Path, rows: list[dict], cols: list[str]) -> None:
        if not rows:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with open(path, "a", encoding="utf-8", newline="") as f:
            pd.DataFrame(rows, columns=cols).to_csv(f, header=new, index=False)

    def listed_days(self) -> set[str]:
        return set(self._read(self.listed_path, ["date"])["date"])

    def candidates(self) -> pd.DataFrame:
        return self._read(self.cand_path, CAND_COLUMNS).drop_duplicates("docID")

    def index(self) -> pd.DataFrame:
        return self._read(self.index_path, INDEX_COLUMNS).drop_duplicates("docID", keep="last")

    def load_doc(self, ticker: str, doc_id: str) -> dict[str, str]:
        p = self.doc_path(ticker, doc_id)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def lock(self) -> bool:
        p = self.root / ".lock"
        if p.exists():
            try:
                os.kill(int(p.read_text().strip() or 0), 0)
                return False
            except (OSError, ValueError):
                pass
        self.root.mkdir(parents=True, exist_ok=True)
        p.write_text(str(os.getpid()))
        return True

    def unlock(self) -> None:
        (self.root / ".lock").unlink(missing_ok=True)


def keep_candidate(r: dict, codes: set[str]) -> bool:
    return (str(r.get("docTypeCode")) == YUHO and str(r.get("secCode") or "") in codes
            and str(r.get("withdrawalStatus") or "0") in ("0", ""))


def fetch_lists(client: Client, store: Store, days: list[pd.Timestamp], codes: dict[str, str], now: pd.Timestamp,
                log: Callable[[str], None] = print) -> int:
    """まだ見ていない日の一覧を見て、対象の会社の有報を candidates.csv に書き足す。見た日数を返す。"""
    seen, n = store.listed_days(), 0
    for d in days:
        key = f"{d:%Y-%m-%d}"
        if key in seen:
            continue
        status, payload = client.documents(d)
        rs = results(payload) if status == 200 else []
        keep = [r for r in rs if keep_candidate(r, set(codes))]
        store._append(store.cand_path, [{**{c: r.get(c, "") for c in CAND_COLUMNS}, "ticker": codes[str(r["secCode"])],
                                         "listed_on": key} for r in keep], CAND_COLUMNS)
        store._append(store.listed_path, [{"date": key, "status": status, "n": len(rs), "kept": len(keep),
                                           "checked_at": f"{now:%Y-%m-%d %H:%M}"}], ["date", "status", "n", "kept", "checked_at"])
        n += 1
        if n % 50 == 0:
            log(f"  一覧 {n} 日（{key} まで）")
    return n


def fetch_docs(client: Client, store: Store, now: pd.Timestamp, max_docs: int = 10_000,
               log: Callable[[str], None] = print) -> dict[str, int]:
    """candidates.csv の有報のうち、まだ本文を取っていないものを取る（type=5 → 無ければ type=1）。"""
    done = set(store.index()["docID"])
    todo = store.candidates()
    todo = todo[~todo["docID"].isin(done)]
    got = {"docs": 0, "missing": 0, "left": max(len(todo) - max_docs, 0)}
    for k, r in enumerate(todo.head(max_docs).itertuples(index=False)):
        secs, source = {}, ""
        if str(r.csvFlag) == "1":
            status, blob = client.document(r.docID, 5)
            if status == 200 and blob[:2] == b"PK":
                secs, source = sections_from_csv_zip(blob), "csv"
        if not secs and str(r.xbrlFlag or "1") == "1":
            status, blob = client.document(r.docID, 1)
            if status == 200 and blob[:2] == b"PK":
                secs, source = sections_from_xbrl_zip(blob), "xbrl"
        p = store.doc_path(r.ticker, r.docID)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(secs, ensure_ascii=False), encoding="utf-8")
        store._append(store.index_path, [{"docID": r.docID, "secCode": r.secCode, "ticker": r.ticker, "filerName": r.filerName,
                                          "submitDateTime": r.submitDateTime, "periodStart": r.periodStart, "periodEnd": r.periodEnd,
                                          "docTypeCode": r.docTypeCode, "source": source or "none",
                                          **{f"chars_{s}": len(secs.get(s, "")) for s in SECTIONS},
                                          "fetched_at": f"{now:%Y-%m-%d %H:%M}"}], INDEX_COLUMNS)
        got["docs" if secs else "missing"] += 1
        if (k + 1) % 50 == 0:
            log(f"  有報 {k + 1}/{min(len(todo), max_docs)}（本文あり {got['docs']}・本文なし {got['missing']}）")
    return got


def fetch_days(today: pd.Timestamp, years: int = RETENTION_YEARS) -> list[pd.Timestamp]:
    """見る日: 10 年前（受理から 10 年が閲覧の期間）の 1 週間後から今日までの平日。"""
    start = (pd.Timestamp(today) - pd.DateOffset(years=years) + pd.Timedelta(days=7)).normalize()
    return list(pd.bdate_range(start, pd.Timestamp(today).normalize()))


# --------------------------------------------------------------------------- 確かめる（./cfd edinet check）
def run_check(client: Client, today: pd.Timestamp, codes: dict[str, str], log: Callable[[str], None] = print) -> dict:
    """① 一番古い取れる日（10 年前の前後を二分法で）② 3 つの年の有報 1 件ずつで、3 つの節が取れるか ③ 一覧の項目名。"""
    res: dict = {"probes": [], "samples": [], "fields": [], "oldest": None}

    def has_list(d: pd.Timestamp) -> bool:
        status, payload = client.documents(d)
        rs = results(payload) if status == 200 else []
        res["probes"].append({"date": f"{d:%Y-%m-%d}", "status": status, "meta": list_status(payload), "n": len(rs)})
        if rs and not res["fields"]:
            res["fields"] = list(rs[0].keys())
        return len(rs) > 0

    lo = (today - pd.DateOffset(years=RETENTION_YEARS) - pd.Timedelta(days=60)).normalize()
    hi = (today - pd.DateOffset(years=RETENTION_YEARS) + pd.Timedelta(days=60)).normalize()
    days = list(pd.bdate_range(lo, hi))
    if has_list(days[0]):
        res["oldest"] = f"{days[0]:%Y-%m-%d} より前（10 年と 2 か月より前も取れる）"
    elif not has_list(days[-1]):
        res["oldest"] = f"{days[-1]:%Y-%m-%d} より後（10 年より短い）"
    else:
        a, b = 0, len(days) - 1          # days[a] は空、days[b] はある
        while b - a > 1:
            m = (a + b) // 2
            if has_list(days[m]):
                b = m
            else:
                a = m
        res["oldest"] = f"{days[b]:%Y-%m-%d}（その前の平日 {days[a]:%Y-%m-%d} は空。祝日のこともある）"
    for year in (today.year - 9, today.year - 6, today.year - 2):
        for day in pd.bdate_range(f"{year}-06-24", f"{year}-06-30")[::-1]:   # 3 月決算の会社の有報が多い日
            status, payload = client.documents(day)
            rs = [r for r in results(payload) if keep_candidate(r, set(codes))] if status == 200 else []
            if not rs:
                continue
            r = rs[0]
            item = {"year": year, "date": f"{day:%Y-%m-%d}", "docID": r.get("docID"), "filer": r.get("filerName"),
                    "csvFlag": r.get("csvFlag"), "xbrlFlag": r.get("xbrlFlag")}
            secs, src = {}, ""
            if str(r.get("csvFlag")) == "1":
                st, blob = client.document(str(r["docID"]), 5)
                item["type5"] = st
                if st == 200 and blob[:2] == b"PK":
                    secs, src = sections_from_csv_zip(blob), "csv"
            if not secs:
                st, blob = client.document(str(r["docID"]), 1)
                item["type1"] = st
                if st == 200 and blob[:2] == b"PK":
                    secs, src = sections_from_xbrl_zip(blob), "xbrl"
            item.update(source=src or "none", **{f"chars_{k}": len(secs.get(k, "")) for k in SECTIONS},
                        **{f"head_{k}": secs.get(k, "")[:60] for k in SECTIONS})
            res["samples"].append(item)
            break
        else:
            res["samples"].append({"year": year, "date": "–", "docID": None, "source": "対象の会社の有報が見つからなかった"})
    res["calls"] = client.calls
    return res


def report(res: dict, today: pd.Timestamp) -> list[str]:
    L = [f"# EDINET の確かめ（{today:%Y-%m-%d}）", "",
         "決まり: docs/text_signals.md 8 章。一覧は 2 秒に 1 件、書類は 3 秒に 1 件。キーはこのリポジトリの外。", "",
         "## 1. 何年分さかのぼれるか", "", f"- 一番古い、一覧が空でない日: **{res.get('oldest')}**",
         f"- 呼び出し {res.get('calls', 0)} 回。見た日: " + "、".join(f"{p['date']}（{p['status']}・{p['n']} 件）" for p in res["probes"]), "",
         "## 2. 本文の節が取れるか（3 つの年の有報 1 件ずつ）", "",
         "| 年 | 日 | 会社 | 取り方 | 事業の内容 | 事業等のリスク | 経営者による分析 |", "|---|---|---|---|---|---|---|"]
    for s in res["samples"]:
        L.append(f"| {s['year']} | {s.get('date')} | {s.get('filer') or '–'} | {s.get('source')} | "
                 + " | ".join(f"{s.get('chars_' + k, 0):,} 字" for k in SECTIONS) + " |")
    L += ["", "## 3. 一覧の項目名（1 件目）", "", "- " + ("、".join(res["fields"]) if res["fields"] else "取れなかった"), "",
          "## 4. 読み方", "",
          "- 1 で 2016-10 ごろまで取れ、2 で 3 つの年とも「事業の内容」「事業等のリスク」が数百字以上あれば、`./cfd edinet fetch` で集める"
          "（10 年分で一覧 約 2,500 日・有報 約 4,900 件。一覧 約 1.5 時間・有報 約 4 時間。止めても続きから）",
          "- 取れない節・項目名の違いがあれば、この報告を開発側に渡す（プログラムを実物に合わせて直す）"]
    return L
