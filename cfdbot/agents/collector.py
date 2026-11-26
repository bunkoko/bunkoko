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
import urllib.error
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


def fail_reason(e: BaseException) -> str | None:
    """書類を取れなかった理由を短く（HTTP 502・時間切れ・つながらない…）。「サイトに無い」（404・410）は None。"""
    import socket

    if isinstance(e, urllib.error.HTTPError):
        return None if e.code in (404, 410) else f"HTTP {e.code}"
    if isinstance(e, (TimeoutError, socket.timeout)) or "timed out" in str(e).lower():
        return "時間切れ"
    if isinstance(e, urllib.error.URLError):
        return f"つながらない（{str(e.reason)[:40]}）"
    if isinstance(e, ValueError) and "ではないものが返ってきた" in str(e):
        return str(e)[:90]
    return f"{type(e).__name__}（{str(e)[:40]}）"


def not_a_document(kind: str, blob: bytes) -> str | None:
    """返ってきたものが書類でなければ、その理由（先頭の文字つき）。書類なら None。kind は "text"（PDF）か "xbrl"（zip）。

    サイトが、書類の代わりにお断りや不調のページ（HTML）を「成功」として返すことがある。空の本文・空の数字として残すと
    「取った」ことになり、取り直せなくなる。PDF の印（%PDF）は、先頭から 1,024 バイトまでにあればよい。
    """
    want, ok = ("PDF", b"%PDF" in blob[:1024]) if kind == "text" else ("zip", blob[:2] == b"PK")
    if ok:
        return None
    head = "".join(c if c.isprintable() else " " for c in blob[:50].decode("ascii", "replace"))
    return f"{want} ではないものが返ってきた（{head.strip()}）"


def reason_summary(reasons: list[str]) -> str:
    """理由ごとの数（出てきた順）: 「HTTP 502 ×6、時間切れ ×4」。"""
    count: dict[str, int] = {}
    for r in reasons:
        count[r] = count.get(r, 0) + 1
    return "、".join(f"{r} ×{n}" for r, n in count.items())


# 東証のサイトに断られたとき（2026-11-25: 過去分を 18 時間続けて取った後、HTTP 503 で断られ、7 時間後も 3 件目で断られた）。
# サイトは「高頻度・高負荷に繋がる可能性のある自動取得等はご遠慮いただいております」としている（docs/agents.md 3 章）。
REFUSED = (403, 429, 503)                 # サイトに断られた印
REFUSED_FILE = ".site_refused.json"       # 最後に断られた時刻の覚え（data/agents/ir の下。その機械の時計）
REFUSED_SEEN: str | None = "2026-11-25 20:15"   # 実物で最後に断られた時刻（Mac の側に覚えが無いときのために、ここにも書く）
REFUSE_TRIES, REFUSE_TOTAL = 3, 6         # 毎日の収集: 続けて 3 回・合わせて 6 回断られたら、その回はやめる（たたみかけない）
REFUSE_WAIT, REFUSE_WAIT_MAX = 60.0, 600.0      # 断られたら待つ秒数（60・120…。サイトが待つ長さを言ってきたら、それに従う。10 分より長ければやめる）
GENTLE_FACTOR, GENTLE_DAYS = 10, 14       # 断られてから 14 日は、毎日の収集も間隔を 10 倍にあけて取る（1 秒 → 10 秒に 1 件）
CLEAN_NEED = 3                            # 断られた後、毎日の収集が断られずに書類を受け取れた日が、これだけそろったら、過去分をまとめて取ってよい
#                                           （利用者の決め 2026-11-25: 「朝の収集が 3 回続けて無事なら始める」。その前の決まりは「7 日休む」で、根拠の無い見当だった）
CLEAN_LOOK_BACK = 62                      # 無事だった日を数えるときに見る置き場所: 断られた日の 62 日前の月から後（毎日の収集が書く所。古い月は過去分の取得が書く所なので見ない）


def note_refused(root: Path, code: int, when) -> None:
    """サイトに断られた時刻を覚える（毎日の収集はしばらくゆっくり取り、過去分の取得は休む）。"""
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (REFUSED_FILE + ".tmp")
    tmp.write_text(json.dumps({"at": f"{when:%Y-%m-%d %H:%M:%S}", "code": int(code)}), encoding="utf-8")
    tmp.replace(root / REFUSED_FILE)


def last_refused(root: Path, when):
    """最後にサイトに断られた時刻（when より後の時刻は数えない）。断られたことが無ければ None。"""
    from datetime import datetime

    times = []
    if REFUSED_SEEN:
        times.append(datetime.fromisoformat(REFUSED_SEEN))
    try:
        times.append(datetime.fromisoformat(json.loads((root / REFUSED_FILE).read_text(encoding="utf-8"))["at"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    times = [x for x in times if x <= when]
    return max(times) if times else None


def refuse_wait(e, n: int) -> float | None:
    """n 回続けて断られたときに待つ秒数。サイトが待つ長さ（Retry-After）を言ってきたら、それに従う。長すぎるときは None（その回はやめる）。"""
    try:
        asked = float((e.headers or {}).get("Retry-After") or 0)
    except (AttributeError, TypeError, ValueError):
        asked = 0.0
    if asked > REFUSE_WAIT_MAX:
        return None
    return asked if asked > 0 else min(REFUSE_WAIT * 2 ** (n - 1), REFUSE_WAIT_MAX)


def clean_days(root: Path, when) -> list[str]:
    """最後に断られた日より後で、毎日の収集がサイトから書類を受け取れた日（YYYY-MM-DD。古い順。when より先は数えない）。

    証拠は、決算の数字（xbrl）と本文のファイルが書かれた日。道具が別に覚えを書くのではなく、ファイルの書かれた時刻を見るので、
    道具を新しくする前の朝の分も数えられる。断られた日そのものは数えない（その日に、待って取り直せた分があっても）。
    書類が 1 件も無かった朝は、証拠が無いので数えない（数え直しにもならない）。断られたことが無ければ、空。
    過去分の取得は、この日数がそろうまで動かないので、この間に書かれるのは毎日の収集の分だけ。
    """
    import os
    from datetime import datetime, timedelta

    seen = last_refused(root, when)
    if seen is None:
        return []
    first = f"{seen - timedelta(days=CLEAN_LOOK_BACK):%Y-%m}"
    days: set[str] = set()
    for folder, suffix in ((root / "xbrl", ".csv"), (text_root(root), ".txt")):
        try:
            months = [m for m in os.scandir(folder) if re.fullmatch(r"\d{4}-\d{2}", m.name) and m.name >= first and m.is_dir()]
        except OSError:                                         # まだ無い・外付けのディスクがつながっていない
            continue
        for month in months:
            try:
                entries = list(os.scandir(month.path))
            except OSError:
                continue
            for e in entries:
                if not e.name.endswith(suffix) or e.name.startswith("._"):
                    continue
                try:
                    t = datetime.fromtimestamp(e.stat().st_mtime)
                except OSError:
                    continue
                if t.date() > seen.date() and t <= when:
                    days.add(f"{t:%Y-%m-%d}")
    return sorted(days)


def history_gate(root: Path, when) -> dict | None:
    """過去分をまとめて取ってよいか。よければ None。まだなら、断られた時刻 since・無事だった日 days・要る日数 need。"""
    seen = last_refused(root, when)
    if seen is None:
        return None
    days = clean_days(root, when)
    return None if len(days) >= CLEAN_NEED else {"since": seen, "days": days, "need": CLEAN_NEED}


def gate_line(root: Path, when) -> str | None:
    """断られた後の様子を 1 行で（./cfd agents status と、毎朝の収集の記録に出す）。断られたことが無い・済んで日がたった、なら None。"""
    from datetime import timedelta

    seen = last_refused(root, when)
    if seen is None:
        return None
    days = clean_days(root, when)
    short = "・".join(d[5:] for d in days[:CLEAN_NEED])
    if len(days) < CLEAN_NEED:
        return (f"サイトに断られたので（{seen:%m-%d %H:%M}）、過去分はまだまとめて取らない。毎日の収集が、断られずに書類を受け取れた日が "
                f"{CLEAN_NEED} 日そろったら始められる（今 {len(days)} 日{': ' + short if days else ''}。間に断られたら 0 から数え直す。利用者の決め 2026-11-25）")
    if when - seen < timedelta(days=GENTLE_DAYS):
        return (f"サイトに断られた後（{seen:%m-%d %H:%M}）、毎日の収集が、断られずに書類を受け取れた日が {CLEAN_NEED} 日そろった（{short}）。"
                f"過去分の取得を始められる（1 日に {HISTORY_DAY_MAX:,} 件まで。始めるのは、利用者が頼んだとき）")
    return None


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


class PdfPartMissing(RuntimeError):
    """PDF を読む部品が入っていない（空の本文と取り違えないための印）。"""


PDF_PART_HINT = "PDF を読む部品が足りない（保護つきの PDF を読むには cryptography が要る）。./cfd update で入れる"
NO_TEXT = "文字が入っていない（画像だけの PDF など）"


def crypto_ready() -> bool:
    """PDF を読む部品と、保護（AES の暗号）を解く部品が入っているか。

    上場会社の書類には、編集を止める保護つきの PDF が多い（開くのにパスワードは要らない）。解く部品が無いと読めず、
    2026-11-26 までは、それを黙って空の本文として残していた（取ってある本文の 15%。docs/agents.md 10.7）。
    """
    from importlib.util import find_spec

    return find_spec("pypdf") is not None and (find_spec("cryptography") is not None or find_spec("Crypto") is not None)


def _open_pdf(blob: bytes):
    from pypdf import PdfReader

    return PdfReader(io.BytesIO(blob))


def pdf_text_why(blob: bytes, pages: int = TEXT_PAGES, max_chars: int = TEXT_MAX) -> tuple[str, str]:
    """PDF の最初の数ページを文字にする。返すのは（本文, 取れなかった理由）。取れたら、理由は空。

    読む部品が無いときは PdfPartMissing を上げる（空の本文と取り違えない）。
    """
    import logging

    try:
        from pypdf.errors import DependencyError, FileNotDecryptedError
    except ImportError as e:
        raise PdfPartMissing(PDF_PART_HINT) from e
    logging.getLogger("pypdf").setLevel(logging.ERROR)          # 作りの変わった PDF への注意書き（Multiple definitions…）で記録を埋めない
    try:
        reader = _open_pdf(blob)
        text = "\n".join((pg.extract_text() or "") for pg in reader.pages[:pages])
    except (DependencyError, ImportError) as e:                 # 保護を解く部品（cryptography）が無い
        raise PdfPartMissing(PDF_PART_HINT) from e
    except FileNotDecryptedError:
        return "", "開くのにパスワードが要る"
    except Exception as e:  # noqa: BLE001
        return "", f"読めない作り（{type(e).__name__}）"
    text = re.sub(r"[ \t\u3000]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()[:max_chars]
    return text, "" if text else NO_TEXT


def pdf_text(blob: bytes, pages: int = TEXT_PAGES, max_chars: int = TEXT_MAX) -> str:
    """PDF の最初の数ページを文字にする。文字が取れなければ空（理由は pdf_text_why）。読む部品が無いときは PdfPartMissing。"""
    return pdf_text_why(blob, pages, max_chars)[0]


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


PROBE_SECONDS = 60.0                      # 「書けるか」の確かめを待つ長さ（眠っていた外付けのディスクが回り出すのに十分な長さ）


def _can_write(p: Path) -> bool:
    """フォルダ p に、小さなファイルを実際に書いて消せるか。PROBE_SECONDS 待っても返事が無ければ、書けないとする。

    権限の印（os.access）だけでは分からないことがある: macOS は、自動実行（launchd）から外付けのディスクへの書き込みを
    止めることがあり、そのときも印は「書ける」のまま。「許可しますか」の窓を出して待たせている間は、書き込みが返ってこない
    （誰も Mac の前にいない朝に、収集ごと止まらないよう、別の糸で試して時間で切る）。
    """
    import os
    import threading

    ok: list[bool] = []

    def probe() -> None:
        f = p / f".write_test_{os.getpid()}"
        try:
            f.write_bytes(b"")
            f.unlink()
        except OSError:
            return
        ok.append(True)

    th = threading.Thread(target=probe, daemon=True)
    th.start()
    th.join(PROBE_SECONDS)
    return bool(ok)


def text_place(root: Path) -> dict:
    """本文の置き場所の様子: path（リンクなら行き先）・link（リンクか）・seen（フォルダが見えるか）・ready（今、書けるか）。

    リンクの行き先が無い（外付けのディスクがつながっていない）と seen も ready も False。見えていても書けなければ ready は False
    （実際に書いて確かめる）。ready でない日は本文を取りに行かない。
    """
    import os

    p = text_root(root)
    link = p.is_symlink()
    try:
        p.mkdir(parents=True, exist_ok=True)                    # 行き先の無いリンクは、ここで失敗する
        seen = p.is_dir()
    except OSError:
        seen = False
    return {"path": os.path.realpath(p) if link else str(p), "link": link, "seen": bool(seen), "ready": bool(seen and _can_write(p))}


def text_place_line(place: dict) -> str:
    """本文の置き場所を 1 行で（./cfd agents status と、収集の記録に出す）。"""
    if not place["link"]:
        return f"本文の置き場所: {place['path']}"
    if place["ready"]:
        return f"本文の置き場所: {place['path']}（外付けのディスクへのリンク。つながっている。書ける）"
    if place.get("seen"):
        return (f"⚠ 本文の置き場所: {place['path']}（外付けのディスクへのリンク。つながっているが、書けない。Mac の画面に許可を求める窓が出ていないか、"
                "「システム設定 → プライバシーとセキュリティ」で外付けのディスクへの書き込みが止められていないかを見る。書けるまで本文は取らない）")
    return (f"⚠ 本文の置き場所: {place['path']}（外付けのディスクへのリンク。つながっていない。つながるまで本文は取らない。"
            "つないであるのにこの行が出るときは、Mac の「システム設定 → プライバシーとセキュリティ」を見る）")


def _write_text(path: Path, text: str) -> None:
    """本文を書く。書き終えてから名前を付ける（その名前のファイルがあることが「取った」印なので、書きかけを残さない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


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
                 sleep: Callable[[float], None] = time.sleep, now=None) -> dict[str, int]:
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
        from datetime import datetime

        return _collect_docs(disc, root, pdf, xbrl, max_docs, interval, get, log, done, sleep, now or datetime.now)
    finally:
        docs_unlock(root)


def _wait(started: float, interval: float) -> None:
    """取り始めてから interval 秒たつまで待つ（取得にかかった時間は引く）。"""
    rest = interval - (time.monotonic() - started)
    if rest > 0:
        time.sleep(rest)


def _collect_docs(disc, root, pdf, xbrl, max_docs, interval, get, log, done, sleep, now) -> dict[str, int]:
    from datetime import timedelta

    if pdf and not crypto_ready():                              # 部品が無いと、保護つきの書類の本文が空になる。取りに行かない（数字は取る）
        log(f"  ⚠ {PDF_PART_HINT}。今回は本文を取らず、数字だけ取る（本文は、部品を入れた後の回に取る）")
        pdf = False
        done["no_pdf_part"] = 1
    if pdf:
        place = text_place(root)
        if not place["ready"]:                                  # 書けない所へは取りに行かない（取っても残せない）。数字は取る
            why = "見えているのに書けない" if place["seen"] else "外付けのディスクがつながっていない？"
            log(f"  ⚠ 本文の置き場所（{place['path']}）に書けない（{why}）。今回は本文を取らず、数字だけ取る")
            pdf = False
            done["no_text_place"] = 1
    numbers, texts = [], []
    for r in disc.itertuples(index=False):
        if xbrl and r.xbrl and not facts_path(root, r).exists():
            numbers.append(("xbrl", r, doc_url(r.ticker, r.xbrl), facts_path(root, r)))
        if pdf and not text_path(root, r).exists():
            texts.append(("text", r, doc_url(r.ticker, f"{r.id}.pdf"), text_path(root, r)))
    jobs = (numbers + texts)[:max_docs]                         # 決算の数字が先（その日の 13:05 の E1 の一覧に要る。サイトが少ししか通さない朝でも、数字を先に取る）
    left = max(len(numbers) + len(texts) - max_docs, 0)
    pace = interval
    seen = last_refused(root, now())
    if seen is not None and now() - seen < timedelta(days=GENTLE_DAYS):
        pace = interval * GENTLE_FACTOR
        log(f"  サイトに断られた後（{seen:%m-%d %H:%M}）なので、しばらくはゆっくり取る（{pace:g} 秒に 1 件。断られてから {GENTLE_DAYS} 日間）")
    odd: list[str] = []                                         # 「まだ載っていない」（404）とは別の、取れなかった理由
    k = row = total = 0                                         # row・total: 続けて・合わせて、断られた数
    while k < len(jobs):
        kind, r, url, path = jobs[k]
        if kind == "text" and not pdf:                          # 途中で本文を書けなくなった。その後の本文は取りに行かない
            k += 1
            continue
        started = time.monotonic()
        try:
            blob = get(url)
            bad = not_a_document(kind, blob)
            if bad:                                             # お断りや不調のページが「成功」として返ってきた。残さない（次の回にもう一度取る）
                raise ValueError(bad)
        except Exception as e:  # noqa: BLE001
            if isinstance(e, urllib.error.HTTPError) and e.code in REFUSED:           # 断られた: たたみかけない
                row, total = row + 1, total + 1
                note_refused(root, e.code, now())
                pause = refuse_wait(e, row)
                if pause is None or row >= REFUSE_TRIES or total >= REFUSE_TOTAL:
                    done["refused"], done["why_refused"] = total, f"HTTP {e.code}"
                    left += len(jobs) - k
                    log(f"  ⚠ サイトに断られた（HTTP {e.code}。この回で {total} 回）。今回はここでやめる（残りは次の回に取る。たたみかけない）")
                    break
                pace = min(max(pace * 2, interval * GENTLE_FACTOR), interval * 60)
                log(f"  サイトに断られた（HTTP {e.code}）。{pause:.0f} 秒待って、同じ書類からゆっくり続ける（{pace:g} 秒に 1 件）")
                sleep(pause)
                continue                                        # 同じ書類をもう一度
            why = fail_reason(e)
            if why is None:
                done["missing"] += 1
            else:
                odd.append(why)
            _wait(started, pace)
            k += 1
            continue
        row = 0
        if kind == "text":
            try:
                _write_text(path, pdf_text(blob))
            except PdfPartMissing as e:                         # 読む部品が無い。空の本文を残さない
                log(f"  ⚠ {e}。今回は、この後の本文を取りに行かず、数字だけ取る")
                pdf = False
                done["no_pdf_part"] = 1
                _wait(started, pace)
                k += 1
                continue
            except OSError as e:                                # 外付けのディスクが外れた・書き込みを止められた。数字の収集は続ける
                log(f"  ⚠ 本文を書けなかった（{str(e)[:60]}）。今回は、この後の本文を取りに行かず、数字だけ取る")
                pdf = False
                done["no_text_place"] = 1
                _wait(started, pace)
                k += 1
                continue
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            summary = xbrl_summary(blob)
            if summary:
                path.with_suffix(".zip").write_bytes(summary)                        # サマリーの原本（読み直し用）
            xbrl_facts(blob).to_csv(path, index=False)
        done[kind] += 1
        if (k + 1) % 50 == 0:
            log(f"  書類 {k + 1}/{len(jobs)}（本文 {done['text']}・数字 {done['xbrl']}・まだ無い {done['missing']}）")
        _wait(started, pace)
        k += 1
    done["left"] = left
    if odd:                                                     # 決算の数字が抜けているおそれがあるので、数と理由を出す
        done["failed"], done["why"] = len(odd), reason_summary(odd)
        log(f"  ⚠ 取れなかった {len(odd)} 件（{done['why']}）。「まだ載っていない」とは別（サイトに断られた・回線やサイトの不調）。次の回にもう一度試す")
    return done


# --------------------------------------------------------------------------- 過去の本文をまとめて取る（docs/agents.md 10.1。2026-11-24）
MISS_FILE = "text_missing.csv"            # サイトに無かった書類（404）の控え。過去分では、もう取りに行かない
QUIET_FROM, QUIET_TO = (7, 50), (8, 45)   # 毎朝 8:00 の収集に場所をゆずる時間（その機械の時計）。この間は取らない・錠も持たない
HISTORY_CHUNK = 200                       # 1 度の錠で取る数（3 分ちょっと。終わるたびに錠を放す）
HISTORY_INTERVAL = 10.0                   # 過去分をまとめて取るときの間隔（秒）。毎日の収集（1 秒）より長くあける（2026-11-25 に断られた後の決まり）
HISTORY_DAY_MAX = 2000                    # 過去分を 1 日に取りに行く数（断られたのは、1 日に 6 万件ほど続けて取った後だった。その 30 分の 1）
DAY_FILE = ".text_history.day.json"       # その日に取りに行った数の覚え
MAX_FAILS = 10                            # 続けてこれだけ取れなければ止める（回線・サイトの不調）
FAIL_WAIT, FAIL_WAIT_MAX = 15.0, 300.0    # 取れなかったら、次を試す前にあける間（秒）。続けて取れないたびに倍にする（15・30・60…5 分まで）
PDF_EACH_MB = 0.6                         # 原本 1 件に見ておく大きさ（測っていない見当。0.3〜1MB の幅の中ほど。9 万件で 55GB ほど）
PDF_KEEP_FREE_GB = 5                      # HDD に必ず残しておく空き（いっぱいにしない。毎日の収集の本文も、同じ HDD に書く）


HISTORY_LOCK = ".text_history.lock"       # 過去の本文の取得が、動いている間ずっと持つ印（中身は道具の番号）


def history_hold(root: Path):
    """過去の本文の取得は 1 本だけ: 動いている間ずっと持つ印を取る。取れたら、持ち続けるためのファイルを返す。もう動いていれば None。

    書類の錠（.docs.lock）が守るのは「同時に 1 本」だけで、区切りごとに放す。2 本目が動くと、2 本が交代で同じ書類を取りに行ってしまう。
    印は OS の錠（flock）なので、道具が止められても・機械が落ちても残らない（番号だけ同じになった別の道具と取り違えない）。
    """
    import fcntl
    import os

    root.mkdir(parents=True, exist_ok=True)
    f = open(root / HISTORY_LOCK, "a+", encoding="utf-8")  # noqa: SIM115  持ち続けるために開いたままにする
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def history_release(hold) -> None:
    """印を放す（番号も消す）。"""
    try:
        hold.seek(0)
        hold.truncate()
        hold.flush()
    finally:
        hold.close()


def history_running(root: Path) -> int | None:
    """過去の本文の取得が今動いていれば、その道具の番号（読めなければ 0）。動いていなければ None。"""
    import fcntl

    path = root / HISTORY_LOCK
    if not path.exists():
        return None
    try:
        f = open(path, encoding="utf-8")  # noqa: SIM115
    except OSError:                                             # 見た直後に消えた ＝ 動いていない
        return None
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:                                         # だれかが持っている ＝ 動いている
            try:
                return int(f.read().strip() or 0)
            except ValueError:
                return 0
        fcntl.flock(f, fcntl.LOCK_UN)
    return None


def day_count(root: Path, day: str) -> int:
    """その日（YYYY-MM-DD）に、過去分の取得がサイトへ取りに行った数。"""
    try:
        rec = json.loads((root / DAY_FILE).read_text(encoding="utf-8"))
        return int(rec["n"]) if rec.get("day") == day else 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return 0


def day_note(root: Path, day: str, n: int) -> None:
    tmp = root / (DAY_FILE + ".tmp")
    tmp.write_text(json.dumps({"day": day, "n": int(n)}), encoding="utf-8")
    tmp.replace(root / DAY_FILE)


def pdf_root(root: Path) -> Path:
    """過去分の PDF の原本の置き場所: 本文の置き場所（リンクの行き先）のとなりの ir_pdf。"""
    import os

    return Path(os.path.realpath(text_root(root))).parent / "ir_pdf"


def pdf_need(n_docs: int) -> int:
    """原本も残して n_docs 件取るのに要る空き（バイト）: 1 件 PDF_EACH_MB の見当 ＋ 残しておく PDF_KEEP_FREE_GB。

    件数に合わせる（全部なら 60GB ほど）。途中で止めて続きから取るとき、残りが少しなのに全部の分を求めて止まらないように。
    """
    return int(n_docs * PDF_EACH_MB * 2 ** 20) + PDF_KEEP_FREE_GB * 2 ** 30


BIG_FILE = 10 * 2 ** 20                    # 「大きい書類」の目安（10MB）。いくつあって、中身のどれだけを占めるかを数える


def disk_use(folder: Path, suffix: str, keep: int = 500, big: int = BIG_FILE) -> dict:
    """folder/<年-月>/ の下の suffix のファイル: 件数 n・中身の合計 size・ディスクの上で使っている場所 used・いちばん大きいもの biggest（バイト）、
    big より大きいものの件数 big_n と中身の合計 big_size、書いた時刻（新しい keep 件。古い順）。

    中身の大きさと、使っている場所は違う: ディスクの形式によっては、小さなファイルも 1 つで何 MB もの場所を使う。
    """
    import os

    n = size = used = biggest = big_n = big_size = 0
    times: list[float] = []
    try:
        months = [m for m in os.scandir(folder) if m.is_dir()]
    except OSError:
        months = []
    for month in months:
        try:
            entries = list(os.scandir(month.path))
        except OSError:
            continue
        for e in entries:
            if not e.name.endswith(suffix) or e.name.startswith("._"):
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            n, size, used, biggest = n + 1, size + st.st_size, used + st.st_blocks * 512, max(biggest, st.st_size)
            if st.st_size > big:
                big_n, big_size = big_n + 1, big_size + st.st_size
            times.append(st.st_mtime)
    return {"n": n, "size": size, "used": used, "biggest": biggest, "big_n": big_n, "big_size": big_size, "times": sorted(times)[-keep:]}


def pace_seconds(times: list[float]) -> float | None:
    """続けて書いたファイルの間隔の、まん中の値（秒）。休んでいた間（毎朝の休み・止めていた間）は、まん中の値なら数に入らない。

    20 件に満たなければ None（少なすぎて分からない）。
    """
    if len(times) < 20:
        return None
    from itertools import pairwise

    gaps = sorted(b - a for a, b in pairwise(times))
    return float(gaps[len(gaps) // 2])


def span_text(seconds: float) -> str:
    """時間の長さを、人が言う形で（50 分・26.5 時間・4.3 日）。"""
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} 分"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} 時間"
    return f"{seconds / 86400:.1f} 日"


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
    _write_text(text_path(root, r), pdf_text(blob))             # 原本より後に書く（本文のファイルがあることが「取った」印）


def collect_text_history(disc: pd.DataFrame, root: Path, since: str, max_docs: int | None = None, keep_pdf: bool = True,
                         interval: float = INTERVAL, get: Callable[[str], bytes] | None = None, log: Callable[[str], None] = print,
                         now=None, sleep: Callable[[float], None] = time.sleep, free: Callable[[str], int] | None = None,
                         day_max: int | None = None) -> dict:
    """過去の開示の本文（PDF の最初の数ページを文字にしたもの。毎日の収集と同じ形）を、新しい順にまとめて取る。

    決まり: interval 秒に 1 件・同時に 1 本だけ（錠）。毎朝 8:00 の収集の時間（QUIET）は取らず、錠も持たない
    （毎朝の収集は、ほかの取得が動いていると書類を取らずに終わる。決算の数字が抜けると、その日の E1 の記録が傷む）。
    サイトに無かった書類（404）は控えて、次からは取りに行かない。断られたら（REFUSED）すぐ止める。
    断られた後は、毎日の収集が CLEAN_NEED 日、断られずに書類を受け取れるまで取らない（history_gate。取っている間も、区切りごとに確かめる）。
    置き場所は外付けのディスクへのリンクであること（2026-11-20 利用者の決め）。keep_pdf なら原本もそのとなりに残す。
    返すのは、取れた数（text）・サイトに無かった数（missing）・取れなかった数（failed）・残り（left）・止まった理由（stopped）。
    """
    import shutil
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

    def shut() -> dict | None:
        """断られた後で、まだ取ってはいけない間なら、止まる理由（毎日の収集を守る。確かめの 1 件も取らない）。"""
        gate = history_gate(root, now())
        if gate is None:
            return None
        return {"stopped": "cooling", "since": f"{gate['since']:%Y-%m-%d %H:%M}", "clean": len(gate["days"]), "need": gate["need"]}

    jobs = history_jobs(disc, root, since, history_until(now()))
    done["left"] = len(jobs)
    if not crypto_ready():                                      # 部品が無いと、保護つきの書類の本文が空になる。取りに行かない
        return dict(done, stopped="no_pdf_part")
    closed = shut()
    if closed:
        return dict(done, **closed)
    todo = len(jobs) if max_docs is None else min(len(jobs), max_docs)
    cap = HISTORY_DAY_MAX if day_max is None else day_max
    asked: dict[str, int] = {}                                  # 日ごとに、サイトへ取りに行った数（覚えてある分に足していく）

    def used(day: str) -> int:
        if day not in asked:
            asked[day] = day_count(root, day)
        return asked[day]

    if todo and used(f"{now():%Y-%m-%d}") >= cap:
        return dict(done, stopped="day_done")

    def room() -> int | None:
        """置き場所の空き（バイト）。ディスクが外れていて測れなければ None。"""
        try:
            return free(place["path"])
        except OSError:
            return None

    if keep_pdf and todo:
        have = room()
        if have is None:
            return dict(done, stopped="disk")
        if have < pdf_need(todo):
            return dict(done, stopped="no_space", need_gb=pdf_need(todo) / 2 ** 30, free_gb=have / 2 ** 30)
    i = fails = 0
    told = False
    spent = {"n": 0, "all": 0.0, "get": 0.0, "save": 0.0}     # この回に、1 件ずつにかかった時間（秒。待ちも入れる）
    recent: list[str] = []                                      # 続けて取れなかった理由（取れたら空に戻す）
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
        closed = shut()
        if closed:                                              # 取っている間に、毎日の収集が断られた。過去分も止める
            log("  毎日の収集がサイトに断られたので、過去分の取得も止める")
            done.update(closed)
            break
        if not docs_lock(root):
            log("  ほかの書類の取得が動いている。1 分待つ")
            sleep(60.0)
            continue
        try:
            for r in jobs[i:min(i + HISTORY_CHUNK, todo)]:
                if in_quiet(now()) or wanted(root):
                    break
                have = room() if keep_pdf else 0
                if have is None:                                # 書類と書類の間で、ディスクが外れた
                    done["stopped"] = "disk"
                    break
                if keep_pdf and have < PDF_KEEP_FREE_GB * 2 ** 30:                     # 原本が見当より大きかった・ほかのものが増えた。いっぱいにしない
                    done.update(stopped="no_space", need_gb=pdf_need(todo - i) / 2 ** 30, free_gb=have / 2 ** 30)
                    break
                day = f"{now():%Y-%m-%d}"
                if used(day) >= cap:                            # 今日の分は取り終えた（1 日に取りに行く数を決めてある）
                    done["stopped"] = "day_done"
                    break
                asked[day] += 1
                started = time.monotonic()
                blob = None
                try:
                    blob = get(doc_url(r.ticker, f"{r.id}.pdf"))
                    bad = not_a_document("text", blob)
                    if bad:
                        raise ValueError(bad)
                except urllib.error.HTTPError as e:
                    blob = None
                    if e.code in REFUSED:
                        done["stopped"], done["why"] = "refused", f"HTTP {e.code}"
                        note_refused(root, e.code, now())       # 覚えておく: 過去分は毎日の収集が CLEAN_NEED 日無事になるまで休み、毎日の収集はしばらくゆっくり取る
                        break
                    if e.code in (404, 410):
                        with open(root / MISS_FILE, "a", encoding="utf-8") as f:
                            f.write(f"{r.id}\n")
                        done["missing"] += 1
                        fails = 0
                        recent.clear()
                    else:
                        done["failed"] += 1
                        fails += 1
                        recent.append(f"HTTP {e.code}")
                except Exception as e:  # noqa: BLE001  回線が切れた・途中で切れた・PDF でない。次の機会にもう一度試す
                    blob = None
                    done["failed"] += 1
                    fails += 1
                    recent.append(fail_reason(e) or type(e).__name__)
                fetched = time.monotonic()
                if blob is not None:
                    fails = 0
                    recent.clear()
                    try:
                        _save_history(root, r, blob, keep_pdf)
                    except PdfPartMissing:
                        done["stopped"] = "no_pdf_part"
                        break
                    except OSError:
                        done["stopped"] = "disk"
                        break
                    done["text"] += 1
                saved = time.monotonic()
                i += 1
                if fails >= MAX_FAILS:
                    done["stopped"], done["why"] = "network", reason_summary(recent)
                    break
                _wait(started, interval)
                if fails:                                       # 取れなかった: 間をあけてから次を試す（たたみかけない。少しの不調なら、止まらずに済む）
                    pause = min(FAIL_WAIT * 2 ** (fails - 1), FAIL_WAIT_MAX)
                    log(f"  取れなかった（{recent[-1]}）。{pause:.0f} 秒あけて次を試す（続けて {fails} 件目）")
                    waited = 0.0
                    while waited < pause and not wanted(root) and not in_quiet(now()):      # 待つ間も、毎日の収集が来たらすぐゆずる
                        step = min(5.0, pause - waited)
                        sleep(step)
                        waited += step
                spent["n"] += 1
                spent["all"] += time.monotonic() - started
                spent["get"] += fetched - started
                spent["save"] += saved - fetched
                if i % HISTORY_CHUNK == 0:
                    left = room() if keep_pdf else None
                    space = f"・HDD の空き {left / 2 ** 30:,.0f}GB" if left is not None else ""
                    per = spent["all"] / spent["n"]
                    log(f"  過去の本文 {i:,}/{todo:,}（取れた {done['text']:,}・サイトに無い {done['missing']:,}・取れなかった {done['failed']:,}{space}）。"
                        f"1 件 {per:.1f} 秒（受け取り {spent['get'] / spent['n']:.1f}・読み取りと書き込み {spent['save'] / spent['n']:.1f}）。"
                        f"この速さで、あと {span_text(per * (todo - i))}")
        finally:
            docs_unlock(root)
            for day, n in asked.items():
                if n > day_count(root, day):
                    day_note(root, day, n)                      # いちばん新しい日の分が残る（日をまたいだら、新しい日の数）
    done["left"] = len(jobs) - done["text"] - done["missing"]
    return done


def reread_empty(root: Path, write: bool = True, limit: int | None = None, log: Callable[[str], None] | None = print, every: int = 500) -> dict:
    """空の本文を、残してある原本の PDF から読み直す（サイトには取りに行かない）。文字のある本文には触らない。

    write=False なら数えるだけで、何も書かない。limit を渡すと、原本のある分から、全体に散らばるようにその数だけ読んでみる。
    書き直したファイルの時刻は、元のままにする（断られた後の日数の数え clean_days は、本文のファイルが書かれた日を見る）。
    返すのは、空の本文の数 empty・原本が無くて読み直せない数 no_pdf・読んでみた数 tried・文字が取れた数 fixed・
    やはり取れない数 still と、その理由ごとの数 why・始められなかった理由 stopped。
    """
    import os

    done = {"empty": 0, "no_pdf": 0, "tried": 0, "fixed": 0, "still": 0, "why": {}, "stopped": None}
    folder = text_root(root)
    if not folder.is_dir():                                     # 行き先の無いリンク ＝ 外付けのディスクがつながっていない
        return dict(done, stopped="no_text_place")
    if not crypto_ready():
        return dict(done, stopped="no_pdf_part")
    raw, jobs = pdf_root(root), []
    for month in sorted((m for m in os.scandir(folder) if m.is_dir()), key=lambda m: m.name, reverse=True):
        for e in sorted(os.scandir(month.path), key=lambda e: e.name):
            if not e.name.endswith(".txt") or e.name.startswith("._") or e.stat().st_size:
                continue
            done["empty"] += 1
            pdf = raw / month.name / f"{e.name[:-4]}.pdf"
            if pdf.exists():
                jobs.append((Path(e.path), pdf))
            else:
                done["no_pdf"] += 1
    if limit is not None and len(jobs) > limit:
        jobs = [jobs[int(k * len(jobs) / limit)] for k in range(limit)]
    for path, pdf in jobs:
        try:
            text, why = pdf_text_why(pdf.read_bytes())
        except OSError:
            text, why = "", "原本を読めない"
        done["tried"] += 1
        if text:
            done["fixed"] += 1
            if write:
                st = path.stat()
                _write_text(path, text)
                os.utime(path, (st.st_atime, st.st_mtime))
        else:
            done["still"] += 1
            done["why"][why] = done["why"].get(why, 0) + 1
        if log and done["tried"] % every == 0:
            log(f"  読み直し {done['tried']:,}/{len(jobs):,}（文字が取れた {done['fixed']:,}・やはり取れない {done['still']:,}）")
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
