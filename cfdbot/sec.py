"""SEC EDGAR から、10-K の「リスク要因」（Item 1A）と「経営者による分析」（Item 7）の文字を取る（docs/us_text.md 3・9 章）。

- 会社ごとの提出の一覧 `https://data.sec.gov/submissions/CIK##########.json`（古い分は filings.files の別のページ）
- 書類 `https://www.sec.gov/Archives/edgar/data/{cik}/{番号（- を外す）}/{主な書類}`
- SEC の決まり: User-Agent に名乗る名前と連絡先を入れる。上限は 1 秒に 10 件 → ここでは 1 秒に 1 件・同時に 1 本。
  断られたら 10・30・60 分待ってやり直し、それでもだめなら止める
- 名乗る名前と連絡先はリポジトリの外（~/.bunkoko/sec.json、権限 600）だけに置き、記録・例外の文・報告・取ったファイルに出さない
- 元の HTML は残さない。節の文字だけを data/edgar/sec（コミットしない）に置く
"""

from __future__ import annotations

import gzip
import http.client
import json
import os
import re
import time
import zlib
from html import unescape
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

KEY_FILE = Path.home() / ".bunkoko" / "sec.json"
INTERVAL = 1.0                                   # 1 秒に 1 件（SEC の上限は 10 件）
WAITS = (600.0, 1800.0, 3600.0)                  # 断られた・つながらないとき、10・30・60 分待つ
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE = "https://data.sec.gov/submissions/"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{primary}"
FORMS = ("10-K", "10-K405")                      # 10-K/A（訂正）・10-KT（決算期の変更）・10-K405/A は使わない
MIN_SECTION = 200                                # これより短い節は「取れなかった」とする
CHECK_PER_YEAR = {2020: 6, **{y: 9 for y in range(2021, 2027)}}   # 点検 2 のくじ（9.1。60 件）
CHECK_SEED = 20261021
COLUMNS = ["cik", "accession", "form", "filing_date", "report_date", "acceptance", "primary"]
INDEX_COLUMNS = COLUMNS + ["chars_risks", "chars_mdna", "status", "fetched_at"]


class Stop(Exception):
    """続けると相手に迷惑か、意味が無いので止める（断られ続けた・つながらない）。"""


# --------------------------------------------------------------------------- 名乗る名前と連絡先
def load_user_agent(path: Path = KEY_FILE) -> str | None:
    try:
        return str(json.loads(Path(path).read_text(encoding="utf-8")).get("user_agent", "")).strip() or None
    except (OSError, ValueError, AttributeError):
        return None


def save_user_agent(ua: str, path: Path = KEY_FILE) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)                            # 書く前に権限を絞る
    path.write_text(json.dumps({"user_agent": ua.strip()}), encoding="utf-8")
    return path


def _decode(body: bytes, encoding: str | None) -> bytes:
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc or body[:2] == b"\x1f\x8b":
            return gzip.decompress(body)
        if "deflate" in enc:
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, EOFError, zlib.error):
        pass
    return body


def http_getter(user_agent: str, timeout: float = 120.0) -> Callable[[str], tuple[int, bytes]]:
    """getter(url) → (状態, 中身)。つながらないときは (-1, 理由)。理由に URL と名乗りは入れない。"""
    def scrub(s: str) -> bytes:
        return s.replace(user_agent, "***").encode("utf-8", "replace")

    def get(url: str) -> tuple[int, bytes]:
        req = Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        try:
            with urlopen(req, timeout=timeout) as r:
                return int(r.status), _decode(r.read(), r.headers.get("Content-Encoding"))
        except HTTPError as e:
            try:
                body = e.read() or b""
            except (OSError, http.client.HTTPException):
                body = b""
            return int(e.code), _decode(body, e.headers.get("Content-Encoding") if e.headers else None)
        except URLError as e:
            return -1, scrub(str(e.reason))
        except (OSError, http.client.HTTPException) as e:
            return -1, scrub(f"{type(e).__name__}: {e}")
    return get


# --------------------------------------------------------------------------- 呼び出し（間隔と、断られたときの待ち）
class Client:
    def __init__(self, getter: Callable[[str], tuple[int, bytes]], sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print,
                 interval: float = INTERVAL, waits: tuple = WAITS):
        self.getter, self.sleep, self.clock, self.log = getter, sleep, clock, log
        self.interval, self.waits = interval, tuple(waits)
        self.calls = 0
        self._last: float | None = None

    def get(self, url: str) -> tuple[int, bytes]:
        for attempt in range(len(self.waits) + 1):
            if self._last is not None:
                rest = self.interval - (self.clock() - self._last)
                if rest > 0:
                    self.sleep(rest)
            self._last = self.clock()
            status, body = self.getter(url)
            self.calls += 1
            if not (status in (429, 403, -1) or status >= 500):
                return status, body
            if attempt == len(self.waits):
                if status == 403:
                    raise Stop("SEC に断られ続けた（状態 403）。名乗る名前と連絡先の書き方（例: 名前 と メールアドレス）か、"
                               "速さのせいかもしれない。./cfd edgar key で確かめ、時間をおいて同じコマンドで続きから")
                if status == -1:
                    raise Stop("SEC につながらない状態が続いた。回線を確かめ、時間をおいて同じコマンドで続きから")
                raise Stop(f"SEC に断られ続けた（状態 {status}）。時間をおいて、同じコマンドで続きから")
            why = f"（つながらない: {body.decode('utf-8', 'replace')[:80]}）" if status == -1 else ""
            self.log(f"  SEC が状態 {status}{why}。{self.waits[attempt] / 60:.0f} 分待ってやり直す")
            self.sleep(self.waits[attempt])
        raise Stop("やり直しの回数を使い切った")


def _json(body: bytes) -> dict:
    try:
        d = json.loads(body.decode("utf-8")) if body else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def submissions(client: Client, cik: int) -> dict:
    """提出の一覧。古いページ（filings.files）もつないで、filings.recent と同じ形の 'filings_all' に入れる。取れなければ {}。"""
    status, body = client.get(SUBMISSIONS.format(cik=int(cik)))
    sub = _json(body) if status == 200 else {}
    if not sub:
        return {}
    filings = sub.get("filings") or {}
    recent = filings.get("recent") or {}
    keys = list(recent)
    merged = {k: list(recent.get(k) or []) for k in keys}
    sub["pages_missing"] = 0
    for f in filings.get("files") or []:
        st, b = client.get(SUBMISSIONS_PAGE + str(f.get("name", "")))
        page = _json(b) if st == 200 else {}
        if "filings" in page:                    # 念のため（古いページは平らな形のはず）
            page = (page.get("filings") or {}).get("recent") or {}
        if not page:
            sub["pages_missing"] += 1
            continue
        n = len(page.get("accessionNumber") or [])
        for k in keys:                           # 足りない項目は空でうめて、長さをそろえる
            merged[k] += (list(page.get(k) or []) + [None] * n)[:n]
    sub["filings_all"] = merged
    return sub


def parse_acceptance(s: pd.Series) -> pd.Series:
    """acceptanceDateTime（例 2023-11-02T18:08:27.000Z）→ 米東部の時刻（tz なし）。

    EDGAR の値は Z が付いていても数字は米東部の時刻（書類の受付の頁の Accepted と同じ数字）。末尾の Z を外して、そのまま米東部として読む。
    もし本当に UTC でも、外した数字は本当の米東部の時刻より 4〜5 時間遅いだけなので、「使える日」が早まることはない（先読みにならない）。
    """
    t = s.astype(str).str.replace(r"(?:\.\d+)?(?:Z|[+-]00:?00)$", "", regex=True)
    return pd.to_datetime(t, errors="coerce", format="ISO8601")


def tenk_filings(sub: dict, cik: int, since: str = "2019-01-01") -> pd.DataFrame:
    """10-K と 10-K405 で、期末が since 以降のもの。同じ期末に複数あれば最初に受け付けたもの。"""
    f = sub.get("filings_all") or (sub.get("filings") or {}).get("recent") or {}
    n = len(f.get("accessionNumber") or [])
    col = lambda k: list(f.get(k) or [None] * n)[:n]   # noqa: E731
    df = pd.DataFrame({"cik": int(cik), "accession": col("accessionNumber"), "form": col("form"),
                       "filing_date": pd.to_datetime(pd.Series(col("filingDate"), dtype=object), errors="coerce"),
                       "report_date": pd.to_datetime(pd.Series(col("reportDate"), dtype=object), errors="coerce"),
                       "acceptance": parse_acceptance(pd.Series(col("acceptanceDateTime"), dtype=object)),
                       "primary": col("primaryDocument")}, columns=COLUMNS)
    df = df[df["form"].isin(FORMS) & (df["report_date"] >= pd.Timestamp(since)) & df["primary"].astype(bool)]
    df = df.sort_values(["report_date", "acceptance", "filing_date", "accession"], kind="mergesort")
    return df.drop_duplicates("report_date", keep="first").reset_index(drop=True)


def doc_url(cik: int, accession: str, primary: str) -> str:
    return ARCHIVES.format(cik=int(cik), acc=str(accession).replace("-", ""), primary=primary)


# --------------------------------------------------------------------------- HTML → 文字、節を取り出す
_SP = r"[ \t\xa0 -​ 　]"


def html_to_text(html: str) -> str:
    t = re.sub(r"(?is)<!--.*?-->|<(script|style|head|ix:header)\b.*?</\1\s*>", " ", str(html))
    if re.search(r"(?i)<(?:p|div|br|tr|table)\b", t):                # HTML の元の改行はただの空白（行の頭の見出しと取り違えないように）
        t = re.sub(r"[\r\n]+", " ", t)
    t = re.sub(r"(?i)</?(?:p|div|br|tr|li|h[1-6]|table)\b[^>]*>", "\n", t)
    t = re.sub(r"(?i)</?t[dh]\b[^>]*>", " ", t)               # 表の升は空白でつなぐ（「Item 1A.」と「Risk Factors」が別の升）
    t = unescape(re.sub(r"<[^>]+>", "", t))                   # タグを外してから &#160; などを戻す
    t = re.sub(_SP + "+", " ", t)
    return re.sub(r" *\n[ \n]*", "\n", t).strip()


_DASH = r"[\s.:\-–—]*"
_STARTS = {"risks": rf"(?:part\s+i\b{_DASH})?item\s*1\s*a\b{_DASH}risk\s+factors",
           "mdna": rf"(?:part\s+ii\b{_DASH})?item\s*7\b{_DASH}management\W{{0,3}}s?\s*discussion"}
_ENDS = {"risks": r"(?:part\s+i\b" + _DASH + r")?item\s*(?:1\s*b|1\s*c|2)\b",
         "mdna": r"(?:part\s+ii\b" + _DASH + r")?item\s*(?:7\s*a|8)\b"}


def extract_section(text: str, kind: str) -> str:
    """kind "risks"（Item 1A から Item 1B・1C・2 の前まで）か "mdna"（Item 7 から 7A・8 の前まで）。見出しの行から返す。

    見出しは行の頭にあるものだけ。目次にも同じ見出しがあるので、次の終わりの見出しまでが一番長い候補（本文）を選ぶ。
    """
    starts = [m.start(1) for m in re.finditer(rf"(?im)^{_SP}*({_STARTS[kind]})", text)]
    ends = [m.start(1) for m in re.finditer(rf"(?im)^{_SP}*({_ENDS[kind]})", text)]
    best = (0, 0)
    for s in starts:
        e = next((e for e in ends if e > s), None)
        if e is not None and e - s > best[1] - best[0]:
            best = (s, e)
    out = text[best[0]:best[1]].strip()
    return out if len(out) >= MIN_SECTION else ""


# --------------------------------------------------------------------------- 置き場（data/edgar/sec。コミットしない）
class Store:
    def __init__(self, root: str | Path = "data/edgar/sec"):
        self.root = Path(root)

    @property
    def index_path(self) -> Path:
        return self.root / "index.csv"

    def sub_path(self, cik: int) -> Path:
        return self.root / "submissions" / f"CIK{int(cik):010d}.json.gz"

    def doc_path(self, cik: int, accession: str) -> Path:
        return self.root / "docs" / str(int(cik)) / f"{accession}.json.gz"

    @staticmethod
    def _write(path: Path, obj: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        tmp.replace(path)                         # 途中で止まっても壊れたファイルを残さない

    @staticmethod
    def _load(path: Path) -> dict:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError, EOFError):
            return {}

    def save_sub(self, cik: int, sub: dict) -> None:
        self._write(self.sub_path(cik), sub)

    def load_sub(self, cik: int) -> dict:
        return self._load(self.sub_path(cik))

    def save_doc(self, cik: int, accession: str, sections: dict) -> None:
        self._write(self.doc_path(cik, accession), {k: str(sections.get(k, "")) for k in ("risks", "mdna")})

    def load_doc(self, cik: int, accession: str) -> dict:
        return self._load(self.doc_path(cik, accession))

    def index(self) -> pd.DataFrame:
        if not self.index_path.exists():
            return pd.DataFrame(columns=INDEX_COLUMNS)
        return pd.read_csv(self.index_path, dtype=str, keep_default_na=False).drop_duplicates("accession", keep="last")

    def append_index(self, rows: list[dict]) -> None:
        if not rows:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        new = not self.index_path.exists()
        with open(self.index_path, "a", encoding="utf-8", newline="") as f:
            pd.DataFrame(rows, columns=INDEX_COLUMNS).to_csv(f, header=new, index=False)

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


def _ts(x, fmt: str) -> str:
    return "" if x is None or pd.isna(x) else pd.Timestamp(x).strftime(fmt)


def _text(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", "replace")  # 古い書類は Windows の文字のことがある


def fetch_docs(client: Client, store: Store, filings: pd.DataFrame, now: pd.Timestamp, max_docs: int = 100_000,
               log: Callable[[str], None] = print) -> dict[str, int]:
    """まだ取っていない 10-K を取り、2 つの節の文字だけを残す（止めても続きから）。"""
    done = set(store.index()["accession"])
    todo = filings[~filings["accession"].astype(str).isin(done)].drop_duplicates("accession")
    got = {"docs": 0, "empty": 0, "missing": 0, "left": max(len(todo) - max_docs, 0)}
    total = min(len(todo), max_docs)
    for k, r in enumerate(todo.head(max_docs).itertuples(index=False)):
        status, body = client.get(doc_url(r.cik, r.accession, r.primary))
        secs = {"risks": "", "mdna": ""}
        if status == 200:
            text = html_to_text(_text(body))
            secs = {kind: extract_section(text, kind) for kind in secs}
            store.save_doc(r.cik, r.accession, secs)
            st = "ok" if secs["risks"] else "empty"
        else:
            st = "missing"
        got["docs" if st == "ok" else st] += 1
        store.append_index([{"cik": int(r.cik), "accession": r.accession, "form": r.form,
                             "filing_date": _ts(r.filing_date, "%Y-%m-%d"), "report_date": _ts(r.report_date, "%Y-%m-%d"),
                             "acceptance": _ts(r.acceptance, "%Y-%m-%d %H:%M:%S"), "primary": r.primary,
                             "chars_risks": len(secs["risks"]), "chars_mdna": len(secs["mdna"]), "status": st,
                             "fetched_at": f"{pd.Timestamp(now):%Y-%m-%d %H:%M}"}])
        if (k + 1) % 50 == 0:
            log(f"  10-K {k + 1}/{total}（取れた {got['docs']}・リスク要因なし {got['empty']}・書類なし {got['missing']}）")
    return got


# --------------------------------------------------------------------------- 点検 2（くじと、目で読むための行）
def sample_for_check(filings: pd.DataFrame, per_year: dict[int, int], seed: int = CHECK_SEED) -> pd.DataFrame:
    """提出の年で層に分けたくじ。番号で並べてから決まった種で引くので、何度でも同じ結果になる。"""
    df = filings.sort_values("accession", kind="mergesort").drop_duplicates("accession").reset_index(drop=True)
    years = pd.to_datetime(df["filing_date"], errors="coerce").dt.year
    rng = np.random.default_rng(seed)
    parts = []
    for y in sorted(per_year):
        pool = df[years == y]
        k = min(int(per_year[y]), len(pool))
        if k > 0:
            parts.append(pool.iloc[np.sort(rng.choice(len(pool), size=k, replace=False))].assign(year=int(y)))
    return pd.concat(parts, ignore_index=True) if parts else df.head(0).assign(year=pd.Series(dtype=int))


def check_rows(store: Store, sample: pd.DataFrame) -> list[dict]:
    """くじの 10-K ごとに、リスク要因の文字数と始まり・終わりの 300 字（指令役が目で読む）。"""
    ix = store.index()
    idx = dict(zip(ix["accession"], ix["status"]))
    rows = []
    for r in sample.itertuples(index=False):
        risks = str(store.load_doc(r.cik, r.accession).get("risks", ""))
        year = getattr(r, "year", pd.Timestamp(r.filing_date).year)
        rows.append({"year": int(year), "cik": int(r.cik), "accession": r.accession, "chars": len(risks),
                     "head": risks[:300], "tail": risks[-300:] if risks else "", "status": idx.get(r.accession, "未取得")})
    return rows
