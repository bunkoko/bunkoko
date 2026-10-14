"""情報収集 AI: 適時開示の一覧・本文・決算の数字を集め、題名を埋め込む。値段は見ない。docs/agents.md。

取り先（docs/agents.md 3 章）
- 一覧（題名・時刻）: やのしん TDnet WEB-API（非公式・無料）。TDnet の公開サイトは自動取得を断っている（robots.txt）ので使わない
- 本文（PDF）と数字（XBRL）: 東証の上場会社情報サービス。開示の翌日から載り、数年分残る。
  「高頻度・高負荷につながる自動取得はご遠慮」とあるので、1 秒に 1 件・同時に 1 本だけで取る（INTERVAL）
"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
import unicodedata
import urllib.request
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

API = "https://webapi.yanoshin.jp/webapi/tdnet/list/"          # やのしん TDnet WEB-API（非公式・無料。2009-10〜）
JPX_DISC = "https://www2.jpx.co.jp/disc/"                      # 東証 上場会社情報サービスの書類（翌日から。数年分）
COLUMNS = ["id", "time", "ticker", "company", "title", "url", "xbrl"]
INTERVAL = 1.0                                                 # 書類は 1 秒に 1 件（取り始めてから次を取り始めるまでの秒数。利用者が 2026-10-13 に決めた）
TEXT_PAGES, TEXT_MAX = 3, 8000                                 # 本文は最初の 3 ページ・8,000 字まで残す
_FACT = re.compile(r"<ix:nonFraction([^>]*)>(.*?)</ix:nonFraction>", re.S)
_NOTE = re.compile(r"<ix:nonNumeric([^>]*)>(.*?)</ix:nonNumeric>", re.S)
_EMPTY = re.compile(r"<ix:non(?:Fraction|Numeric)[^>]*/>")     # 値の無い項目（/> で閉じる）。先に消さないと、次の項目の値を拾ってしまう
FACT_COLUMNS = ["kind", "name", "context", "value", "text"]


def http_get(url: str, tries: int = 3, timeout: int = 60) -> bytes:
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh)"})
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception:  # noqa: BLE001
            if k == tries - 1:
                raise
            time.sleep(5 * (k + 1))
    raise RuntimeError(url)


def ticker_of(code: str) -> str:
    """TDnet の会社コード（5 桁。例 72030、417A0）を Yahoo の形（7203.T）に。"""
    return str(code).strip()[:4] + ".T"


def normalize_title(title: str, company: str = "") -> str:
    """埋め込む前の題名: 全角・半角をそろえ、その会社の名前を消し、数字を 0 に置き換える（いつ・どの会社かを分からなくする）。"""
    t = unicodedata.normalize("NFKC", str(title))
    name = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(company)))
    if len(name) >= 2:
        t = t.replace(name, "")
    return re.sub(r"\s+", "", re.sub(r"\d+", "0", t))


def parse_yanoshin(payload: dict) -> pd.DataFrame:
    rows = []
    for item in payload.get("items", []):
        x = item.get("Tdnet", {})
        url = str(x.get("document_url") or "").split("rd.php?")[-1]
        pdf = str(x.get("url_report_type_summary") or "") if url.endswith(".zip") else url     # hasXBRL=1 のときは書類が zip になる
        xbrl = Path(str(x.get("url_xbrl") or "").split("rd.php?")[-1]).name
        rows.append((Path(pdf or url).stem or f"y{x.get('id')}", x.get("pubdate"), ticker_of(x.get("company_code", "")),
                     str(x.get("company_name", "")).strip(), str(x.get("title", "")).strip(), url, xbrl))
    return pd.DataFrame(rows, columns=COLUMNS)


def fetch_company(ticker: str) -> pd.DataFrame:
    """1 社の過去の開示の一覧（やのしん API）。"""
    return parse_yanoshin(json.loads(http_get(f"{API}{ticker.replace('.T', '')}.json?limit=10000")))


def fetch_day(day: pd.Timestamp) -> pd.DataFrame:
    """その日の全社の開示の一覧（やのしん API）。"""
    return parse_yanoshin(json.loads(http_get(f"{API}{day:%Y%m%d}.json?limit=10000")))


def load_disclosures(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for c in COLUMNS:
        if c not in df:
            df[c] = ""
    return df[COLUMNS]


def merge_disclosures(old: pd.DataFrame, new: pd.DataFrame, tickers: set[str] | None = None, prefer_new: bool = False) -> pd.DataFrame:
    """新しい分を足す（同じ ID は 1 つだけ。prefer_new なら新しい方を残す）。tickers を渡せば、その銘柄だけ残す。"""
    df = pd.concat([new, old] if prefer_new else [old, new], ignore_index=True)
    for c in COLUMNS:
        if c not in df:
            df[c] = ""
    df = df[COLUMNS].fillna("")
    df = df[df["time"].astype(str).str.len() >= 16]
    if tickers is not None:
        df = df[df["ticker"].isin(tickers)]
    return df.drop_duplicates("id", keep="first").sort_values(["time", "id"]).reset_index(drop=True)


# --------------------------------------------------------------------------- 本文（PDF）と数字（XBRL）
def doc_url(ticker: str, filename: str) -> str:
    """上場会社情報サービスの書類の場所（会社コード 5 桁 / ファイル名）。"""
    return f"{JPX_DISC}{ticker.replace('.T', '')}0/{filename}"


def pdf_text(blob: bytes, pages: int = TEXT_PAGES, max_chars: int = TEXT_MAX) -> str:
    """PDF の最初の数ページを文字にする。読めなければ空。"""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(blob))
        text = "\n".join((pg.extract_text() or "") for pg in reader.pages[:pages])
    except Exception:  # noqa: BLE001
        return ""
    return re.sub(r"[ \t\u3000]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()[:max_chars]


def _is_summary(member: str) -> bool:
    base = member.split("/")[-1]
    return base.startswith("tse-") and base.endswith("-ixbrl.htm")


def xbrl_summary(blob: bytes) -> bytes:
    """XBRL の zip から「サマリー」（tse-…-ixbrl.htm）だけを抜き出した小さな zip（後で読み直せるように残す）。無ければ空。"""
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except Exception:  # noqa: BLE001
        return b""
    members = [m for m in z.namelist() if _is_summary(m)]
    if not members:
        return b""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for m in members:
            out.writestr(m.split("/")[-1], z.read(m))
    return buf.getvalue()


def xbrl_facts(blob: bytes) -> pd.DataFrame:
    """XBRL の zip の「サマリー」から中身を取り出す。列: kind（書類の種類）/ name / context / value（数字）/ text（数字でない項目）。

    kind の例: qcedjpsm = 四半期決算短信（日本基準・連結）、acedifsm = 決算短信（IFRS）、rvfc = 業績予想の修正、rvdf = 配当予想の修正。
    数字でない項目（決算期の末日・四半期の番号・修正の有無など）は 60 字までを text に入れる。
    """
    rows = []
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=FACT_COLUMNS)
    for member in z.namelist():
        if not _is_summary(member):
            continue
        kind = member.split("/")[-1].split("-")[1]
        html = _EMPTY.sub("", z.read(member).decode("utf-8", "ignore"))
        for attrs, text in _FACT.findall(html):
            name, ctx = re.search(r'name="([^"]+)"', attrs), re.search(r'contextRef="([^"]+)"', attrs)
            try:
                v = float(re.sub(r"<[^>]+>", "", text).replace(",", "").strip())
            except ValueError:
                continue
            if not name or not ctx or 'xsi:nil="true"' in attrs:
                continue
            scale = re.search(r'scale="(-?\d+)"', attrs)
            v *= 10.0 ** int(scale.group(1)) if scale else 1.0
            rows.append((kind, name.group(1).split(":")[-1], ctx.group(1), -v if re.search(r'sign="-"', attrs) else v, ""))
        for attrs, text in _NOTE.findall(html):
            name, ctx = re.search(r'name="([^"]+)"', attrs), re.search(r'contextRef="([^"]+)"', attrs)
            fmt = re.search(r'format="([^"]+)"', attrs)
            t = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", text))
            if fmt and "boolean" in fmt.group(1):
                t = "true" if "true" in fmt.group(1) else "false"
            if name and ctx and t and len(t) <= 60:
                rows.append((kind, name.group(1).split(":")[-1], ctx.group(1), float("nan"), t))
    return pd.DataFrame(rows, columns=FACT_COLUMNS)


def reparse_facts(root: Path) -> int:
    """残してあるサマリーの原本（xbrl/<年-月>/<名前>.zip）から、数字の表を作り直す。作り直した数を返す。"""
    n = 0
    for z in sorted((root / "xbrl").glob("*/*.zip")):
        xbrl_facts(z.read_bytes()).to_csv(z.with_suffix(".csv"), index=False)
        n += 1
    return n


def text_path(root: Path, row) -> Path:
    return root / "text" / str(row.time)[:7] / f"{row.id}.txt"


def facts_path(root: Path, row) -> Path:
    return root / "xbrl" / str(row.time)[:7] / f"{Path(row.xbrl).stem}.csv"


def _alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def docs_lock(root: Path) -> bool:
    """書類の取得は同時に 1 本だけ（東証のサイトへ 2 本並べて取りに行かない）。取れたら True。終わったら docs_unlock。"""
    import os

    lock = root / ".docs.lock"
    if lock.exists():
        try:
            if _alive(int(lock.read_text().strip() or 0)):
                return False
        except ValueError:
            pass
    root.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))
    return True


def docs_unlock(root: Path) -> None:
    (root / ".docs.lock").unlink(missing_ok=True)


def collect_docs(disc: pd.DataFrame, root: Path, pdf: bool = True, xbrl: bool = True, max_docs: int = 300, interval: float = INTERVAL,
                 get: Callable[[str], bytes] | None = None, log: Callable[[str], None] = print) -> dict[str, int]:
    """disc の開示について、まだ無い本文（文字にしたもの）と数字を取って root（data/agents/ir）の下に保存する。

    PDF そのものは残さない。interval 秒に 1 件の速さで取る。取れなかったもの（まだ載っていない・消えた）は次の機会にまた試す。
    ほかの取得が動いている間は何もしない（busy = 1 を返す）。
    """
    get = get or (lambda url: http_get(url, tries=1))
    done = {"text": 0, "xbrl": 0, "missing": 0}
    if not docs_lock(root):
        return dict(done, left=0, busy=1)
    try:
        return _collect_docs(disc, root, pdf, xbrl, max_docs, interval, get, log, done)
    finally:
        docs_unlock(root)


def _wait(started: float, interval: float) -> None:
    """取り始めてから interval 秒たつまで待つ（取得にかかった時間は引く）。"""
    rest = interval - (time.monotonic() - started)
    if rest > 0:
        time.sleep(rest)


def _collect_docs(disc, root, pdf, xbrl, max_docs, interval, get, log, done) -> dict[str, int]:
    jobs = []
    for r in disc.itertuples(index=False):
        if pdf and not text_path(root, r).exists():
            jobs.append(("text", r, doc_url(r.ticker, f"{r.id}.pdf"), text_path(root, r)))
        if xbrl and r.xbrl and not facts_path(root, r).exists():
            jobs.append(("xbrl", r, doc_url(r.ticker, r.xbrl), facts_path(root, r)))
    for k, (kind, r, url, path) in enumerate(jobs[:max_docs]):
        started = time.monotonic()
        try:
            blob = get(url)
        except Exception:  # noqa: BLE001
            done["missing"] += 1
            _wait(started, interval)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "text":
            path.write_text(pdf_text(blob), encoding="utf-8")
        else:
            summary = xbrl_summary(blob)
            if summary:
                path.with_suffix(".zip").write_bytes(summary)                        # サマリーの原本（読み直し用）
            xbrl_facts(blob).to_csv(path, index=False)
        done[kind] += 1
        if (k + 1) % 50 == 0:
            log(f"  書類 {k + 1}/{min(len(jobs), max_docs)}（本文 {done['text']}・数字 {done['xbrl']}・まだ無い {done['missing']}）")
        _wait(started, interval)
    done["left"] = max(len(jobs) - max_docs, 0)
    return done


def title_keys(df: pd.DataFrame) -> pd.Series:
    return pd.Series([normalize_title(t, c) for t, c in zip(df["title"], df["company"])], index=df.index)


def load_title_vectors(path: Path) -> tuple[np.ndarray, np.ndarray]:
    if not path.exists():
        return np.array([], dtype=str), np.zeros((0, 0), np.float16)
    z = np.load(path, allow_pickle=False)
    return z["titles"].astype(str), z["vecs"]


def embed_new_titles(keys: pd.Series, path: Path, embedder, chunk: int = 4000, log: Callable[[str], None] = print) -> int:
    """まだ埋め込んでいない題名（そろえた後のもの）だけを埋め込んで足す。足した数を返す。"""
    have, vecs = load_title_vectors(path)
    new = sorted(set(keys) - set(have) - {""})
    for i in range(0, len(new), chunk):
        part = new[i:i + chunk]
        v = np.asarray(embedder.encode(part), dtype=np.float16)
        have = np.concatenate([have, np.array(part, dtype=str)])
        vecs = v if vecs.size == 0 else np.vstack([vecs, v])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.stem + ".tmp.npz")
        np.savez_compressed(tmp, titles=have, vecs=vecs)
        tmp.replace(path)
        log(f"  題名の埋め込み {min(i + chunk, len(new)):,}/{len(new):,}")
    return len(new)
