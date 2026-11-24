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


def text_root(root: Path) -> Path:
    """本文の置き場所。外付けのディスクに置くときは、ここをそのフォルダへのリンクにする（docs/agents.md 10.1。2026-11-20 利用者の決め）。"""
    return root / "text"


def text_place(root: Path) -> dict:
    """本文の置き場所の様子: path（リンクなら行き先）・link（リンクか）・ready（今、書けるか）。

    リンクの行き先が無い（外付けのディスクがつながっていない）と ready は False。その日は本文を取りに行かない。
    """
    import os

    p = text_root(root)
    link = p.is_symlink()
    try:
        p.mkdir(parents=True, exist_ok=True)                    # 行き先の無いリンクは、ここで失敗する
        ready = p.is_dir() and os.access(p, os.W_OK)
    except OSError:
        ready = False
    return {"path": os.path.realpath(p) if link else str(p), "link": link, "ready": bool(ready)}


def text_place_line(place: dict) -> str:
    """本文の置き場所を 1 行で（./cfd agents status と、収集の記録に出す）。"""
    if not place["link"]:
        return f"本文の置き場所: {place['path']}"
    if place["ready"]:
        return f"本文の置き場所: {place['path']}（外付けのディスクへのリンク。つながっている）"
    return f"⚠ 本文の置き場所: {place['path']}（外付けのディスクへのリンク。つながっていない。つながるまで本文は取らない）"


def text_path(root: Path, row) -> Path:
    return text_root(root) / str(row.time)[:7] / f"{row.id}.txt"


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


WANT_FILE = ".docs.wanted"                # 毎日の収集が「先に取らせて」と頼む印。過去分の取得は、これを見たら錠を放して待つ


def wanted(root: Path) -> bool:
    """毎日の収集が、錠が空くのを待っているか（印を置いた道具がもう動いていなければ、印は無視して消す）。"""
    path = root / WANT_FILE
    if not path.exists():
        return False
    try:
        if _alive(int(path.read_text().strip() or 0)):
            return True
    except (OSError, ValueError):
        pass
    path.unlink(missing_ok=True)
    return False


def collect_docs(disc: pd.DataFrame, root: Path, pdf: bool = True, xbrl: bool = True, max_docs: int = 300, interval: float = INTERVAL,
                 get: Callable[[str], bytes] | None = None, log: Callable[[str], None] = print, wait: float = 0.0,
                 sleep: Callable[[float], None] = time.sleep) -> dict[str, int]:
    """disc の開示について、まだ無い本文（文字にしたもの）と数字を取って root（data/agents/ir）の下に保存する。

    PDF そのものは残さない。interval 秒に 1 件の速さで取る。取れなかったもの（まだ載っていない・消えた）は次の機会にまた試す。
    ほかの取得が動いている間は何もしない（busy = 1 を返す）。wait を渡すと、「先に取らせて」の印を置いて、その秒数まで錠が空くのを待つ
    （過去の本文の取得は、印を見ると錠を放す。毎日の収集が、長い取得のせいで書類を取り損ねないように）。
    """
    import os

    get = get or (lambda url: http_get(url, tries=1))
    done = {"text": 0, "xbrl": 0, "missing": 0}
    if not docs_lock(root):
        if wait <= 0:
            return dict(done, left=0, busy=1)
        want, waited = root / WANT_FILE, 0.0
        want.write_text(str(os.getpid()))
        try:
            while not docs_lock(root):
                if waited >= wait:
                    return dict(done, left=0, busy=1)
                sleep(5.0)
                waited += 5.0
        finally:
            want.unlink(missing_ok=True)
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
    if pdf:
        place = text_place(root)
        if not place["ready"]:                                  # 書けない所へは取りに行かない（取っても残せない）。数字は取る
            log(f"  ⚠ 本文の置き場所（{place['path']}）に書けない（外付けのディスクがつながっていない？）。今回は本文を取らず、数字だけ取る")
            pdf = False
            done["no_text_place"] = 1
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


# --------------------------------------------------------------------------- 過去の本文をまとめて取る（docs/agents.md 10.1。2026-11-24）
MISS_FILE = "text_missing.csv"            # サイトに無かった書類（404）の控え。過去分では、もう取りに行かない
QUIET_FROM, QUIET_TO = (7, 50), (8, 45)   # 毎朝 8:00 の収集に場所をゆずる時間（その機械の時計）。この間は取らない・錠も持たない
HISTORY_CHUNK = 200                       # 1 度の錠で取る数（3 分ちょっと。終わるたびに錠を放す）
REFUSED = (403, 429, 503)                 # サイトに断られた印。すぐ止める（続けて取りに行かない）
MAX_FAILS = 10                            # 続けてこれだけ取れなければ止める（回線・サイトの不調）
PDF_NEED_GB = 60                          # 原本も残すときに要る空き（見当: 9 万件 × 0.3MB ≒ 27GB の倍）


def pdf_root(root: Path) -> Path:
    """過去分の PDF の原本の置き場所: 本文の置き場所（リンクの行き先）のとなりの ir_pdf。"""
    import os

    return Path(os.path.realpath(text_root(root))).parent / "ir_pdf"


def load_missing(root: Path) -> set[str]:
    """サイトに無かった書類の id。"""
    path = root / MISS_FILE
    return set(path.read_text(encoding="utf-8").split()) if path.exists() else set()


def in_quiet(t) -> bool:
    """毎朝の収集に場所をゆずる時間か。"""
    return QUIET_FROM <= (t.hour, t.minute) < QUIET_TO


HISTORY_SKIP_DAYS = 7                     # 直近の開示は毎日の収集の持ち分（まだ載っていないことがあり、「無かった」と覚えてしまうため）


def history_jobs(disc: pd.DataFrame, root: Path, since: str, until: str | None = None) -> list:
    """since から後（until より前）の開示のうち、まだ本文が無く、「サイトに無かった」の控えにも無いもの。新しい順。"""
    miss = load_missing(root)
    df = disc[disc["time"] >= since]
    if until is not None:
        df = df[df["time"] < until]
    df = df.sort_values("time", ascending=False, kind="stable")
    return [r for r in df.itertuples(index=False) if r.id not in miss and not text_path(root, r).exists()]


def history_until(now) -> str:
    """過去分として取るのは、この日時より前の開示（直近 HISTORY_SKIP_DAYS 日は毎日の収集に任せる）。"""
    from datetime import timedelta

    return f"{now - timedelta(days=HISTORY_SKIP_DAYS):%Y-%m-%d %H:%M:%S}"


def _save_history(root: Path, r, blob: bytes, keep_pdf: bool) -> None:
    if not text_root(root).is_dir():                            # 取っている間にディスクが外れた（行き先の無いリンク）
        raise OSError("本文の置き場所が無い")
    if keep_pdf:
        raw = pdf_root(root) / str(r.time)[:7] / f"{r.id}.pdf"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(blob)
    path = text_path(root, r)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(pdf_text(blob), encoding="utf-8")
    tmp.replace(path)                                           # 本文のファイルがあることが「取った」印。書き終えてから名前を付ける（原本より後）


def collect_text_history(disc: pd.DataFrame, root: Path, since: str, max_docs: int | None = None, keep_pdf: bool = True,
                         interval: float = INTERVAL, get: Callable[[str], bytes] | None = None, log: Callable[[str], None] = print,
                         now=None, sleep: Callable[[float], None] = time.sleep, free: Callable[[str], int] | None = None) -> dict:
    """過去の開示の本文（PDF の最初の数ページを文字にしたもの。毎日の収集と同じ形）を、新しい順にまとめて取る。

    決まり: interval 秒に 1 件・同時に 1 本だけ（錠）。毎朝 8:00 の収集の時間（QUIET）は取らず、錠も持たない
    （毎朝の収集は、ほかの取得が動いていると書類を取らずに終わる。決算の数字が抜けると、その日の E1 の記録が傷む）。
    サイトに無かった書類（404）は控えて、次からは取りに行かない。断られたら（REFUSED）すぐ止める。
    置き場所は外付けのディスクへのリンクであること（2026-11-20 利用者の決め）。keep_pdf なら原本もそのとなりに残す。
    返すのは、取れた数（text）・サイトに無かった数（missing）・取れなかった数（failed）・残り（left）・止まった理由（stopped）。
    """
    import shutil
    import urllib.error
    from datetime import datetime

    now = now or datetime.now
    get = get or (lambda url: http_get(url, tries=1))
    free = free or (lambda path: shutil.disk_usage(path).free)
    done = {"text": 0, "missing": 0, "failed": 0, "left": 0, "stopped": None}
    place = text_place(root)
    if not place["link"]:
        return dict(done, stopped="not_external")
    if not place["ready"]:
        return dict(done, stopped="no_text_place")
    jobs = history_jobs(disc, root, since, history_until(now()))
    done["left"] = len(jobs)
    if keep_pdf and jobs and free(place["path"]) < PDF_NEED_GB * 2 ** 30:
        return dict(done, stopped="no_space")
    todo = len(jobs) if max_docs is None else min(len(jobs), max_docs)
    i = fails = 0
    told = False
    while i < todo and not done["stopped"]:
        t = now()
        if in_quiet(t):
            if not told:
                log(f"  毎朝 8:00 の収集に場所をゆずる（{QUIET_TO[0]}:{QUIET_TO[1]:02d} まで休む）")
                told = True
            until = t.replace(hour=QUIET_TO[0], minute=QUIET_TO[1], second=0, microsecond=0)
            sleep(max((until - t).total_seconds(), 1.0))
            continue
        told = False
        if wanted(root):
            log("  毎日の収集が待っているので、先に通す（30 秒待つ）")
            sleep(30.0)
            continue
        if not docs_lock(root):
            log("  ほかの書類の取得が動いている。1 分待つ")
            sleep(60.0)
            continue
        try:
            for r in jobs[i:min(i + HISTORY_CHUNK, todo)]:
                if in_quiet(now()) or wanted(root):
                    break
                started = time.monotonic()
                try:
                    blob = get(doc_url(r.ticker, f"{r.id}.pdf"))
                    if not blob.startswith(b"%PDF"):
                        raise ValueError("PDF ではないものが返ってきた")
                except urllib.error.HTTPError as e:
                    if e.code in REFUSED:
                        done["stopped"] = "refused"
                        break
                    if e.code in (404, 410):
                        with open(root / MISS_FILE, "a", encoding="utf-8") as f:
                            f.write(f"{r.id}\n")
                        done["missing"] += 1
                        fails = 0
                    else:
                        done["failed"] += 1
                        fails += 1
                except Exception:  # noqa: BLE001  回線が切れた・途中で切れた・PDF でない。次の機会にもう一度試す
                    done["failed"] += 1
                    fails += 1
                else:
                    fails = 0
                    try:
                        _save_history(root, r, blob, keep_pdf)
                    except OSError:
                        done["stopped"] = "disk"
                        break
                    done["text"] += 1
                i += 1
                if fails >= MAX_FAILS:
                    done["stopped"] = "network"
                    break
                if i % HISTORY_CHUNK == 0:
                    log(f"  過去の本文 {i:,}/{todo:,}（取れた {done['text']:,}・サイトに無い {done['missing']:,}・取れなかった {done['failed']:,}）")
                _wait(started, interval)
        finally:
            docs_unlock(root)
    done["left"] = len(jobs) - done["text"] - done["missing"]
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
