"""J-Quants API（JPX 総研の公式。V2）の確認用。docs/survivorship.md の段階 1（無料プランで 1 時間の確認）。

公式の文書で確かめた形（チームの調査役の第 2 回 B、2026-10-15）だけを使う:
- ベース URL `https://api.jquants.com/v2`、認証はヘッダー `x-api-key: <キー>`
- 上場銘柄一覧 `/equities/master?date=`（規模区分 `ScaleCat`）、日足 `/equities/bars/daily?date=`（日付だけで全銘柄）・`?code=&from=&to=`、
  財務 `/fins/summary?date=`（`DiscDate`・`DiscTime`・`DocType`・会社予想 `FSales`・`FOP`・`FNP`）
- 続きは `pagination_key`。状態 200 成功・210 データ無し・400 引数の誤り・403 権限なし（プラン外・キーの誤り）・429 回数の上限（2 分待つ）・500
- Free は 1 分 5 回、直近 12 週間を除く 2 年

項目名・応答の包み（`data`）は決め打ちにせず、返ってきたものをそのまま報告する（確かめられなかった 7 点を、実物で確かめるため）。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

BASE = "https://api.jquants.com/v2"
KEY_FILE = Path.home() / ".bunkoko" / "jquants.json"
FREE_INTERVAL = 13.0             # Free は 1 分 5 回 → 13 秒おき
WAIT_429 = 120.0                 # 429 の後は 2 分待つ（公式: 最低 1 分、安全には約 2 分）
MAX_PAGES = 30
TOPIX500 = ("TOPIX Core30", "TOPIX Large70", "TOPIX Mid400")
STATUS = {200: "成功", 210: "データ無し・一部のみ（取得できる期間の外・存在しないコードなど）", 400: "引数の誤り",
          403: "権限なし（契約したプランに含まれない・キーの誤り・パスの誤り）", 429: "回数の上限を超えた", 500: "サーバーの誤り"}


class Stop(RuntimeError):
    """続けると相手に迷惑か、意味が無いので止める（429 が続いた・キーが無いなど）。"""


def load_key(path: Path = KEY_FILE) -> str | None:
    """API キー。環境変数 JQUANTS_API_KEY か、~/.bunkoko/jquants.json の {"api_key": ...}（このリポジトリの外。権限 600）。"""
    k = os.environ.get("JQUANTS_API_KEY", "").strip()
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


def http_getter(key: str, timeout: float = 60.0) -> Callable[[str, dict], tuple[int, bytes]]:
    def get(path: str, params: dict) -> tuple[int, bytes]:
        url = f"{BASE}{path}" + (f"?{urlencode(params)}" if params else "")
        req = Request(url, headers={"x-api-key": key, "User-Agent": "bunkoko-check/1.0"})
        try:
            with urlopen(req, timeout=timeout) as r:
                return int(r.status), r.read()
        except HTTPError as e:
            return int(e.code), e.read() if hasattr(e, "read") else b""
        except URLError as e:
            raise Stop(f"つながらない（{e.reason}）") from e
    return get


def records(payload) -> tuple[list[dict], str | None]:
    """応答の中の行の一覧と、その入れ物の名前（`data` のはず。違えばその名前を返す）。"""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)], "(配列そのもの)"
    if not isinstance(payload, dict):
        return [], None
    if isinstance(payload.get("data"), list):
        return [r for r in payload["data"] if isinstance(r, dict)], "data"
    for k, v in payload.items():
        if isinstance(v, list) and (not v or isinstance(v[0], dict)):
            return [r for r in v if isinstance(r, dict)], k
    return [], None


@dataclass
class Result:
    path: str
    params: dict
    status: int
    rows: list[dict] = field(default_factory=list)
    container: str | None = None
    pages: int = 0
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200

    def fields(self) -> list[str]:
        seen: list[str] = []
        for r in self.rows[:200]:
            for k in r:
                if k not in seen:
                    seen.append(k)
        return seen


class Client:
    def __init__(self, getter: Callable[[str, dict], tuple[int, bytes]], interval: float = FREE_INTERVAL,
                 sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print):
        self.getter, self.interval, self.sleep, self.log = getter, interval, sleep, log
        self.calls = 0
        self._last: float | None = None

    def _call(self, path: str, params: dict) -> tuple[int, object, str]:
        for attempt in range(2):
            if self._last is not None:
                self.sleep(self.interval)
            status, body = self.getter(path, params)
            self.calls += 1
            self._last = 1.0
            text = body.decode("utf-8", errors="replace") if body else ""
            try:
                payload = json.loads(text) if text else None
            except ValueError:
                payload = None
            if status == 429 and attempt == 0:
                self.log(f"  回数の上限（429）。{WAIT_429:.0f} 秒待って 1 回だけやり直す")
                self.sleep(WAIT_429)
                continue
            if status == 429:
                raise Stop("回数の上限（429）が続いた。約 5 分は全部の呼び出しが断られることがあるので、時間をおいてやり直す")
            msg = ""
            if isinstance(payload, dict) and status != 200:
                msg = str(payload.get("message") or payload.get("error") or "")[:200]
            elif status != 200:
                msg = text[:200]
            return status, payload, msg
        raise Stop("ここには来ない")

    def get(self, path: str, params: dict | None = None, max_pages: int = MAX_PAGES) -> Result:
        """続き（pagination_key）があれば max_pages まで続けて取る。"""
        params = dict(params or {})
        res = Result(path, params, 0)
        key = None
        while True:
            q = dict(params, **({"pagination_key": key} if key else {}))
            status, payload, msg = self._call(path, q)
            res.status, res.message = status, msg
            if status != 200:
                return res
            rows, cont = records(payload)
            res.rows.extend(rows)
            res.container = res.container or cont
            res.pages += 1
            key = payload.get("pagination_key") if isinstance(payload, dict) else None
            if not key or res.pages >= max_pages:
                if key:
                    res.message = f"続きがあったが {max_pages} ページで止めた"
                return res


# --------------------------------------------------------------------------- 確認の中身
def pick(fields: list[str], *names: str) -> str | None:
    """候補の名前のうち、実際に返ってきた項目名（大文字小文字は区別しない）。"""
    low = {f.lower(): f for f in fields}
    return next((low[n.lower()] for n in names if n.lower() in low), None)


def check_dates(today: pd.Timestamp) -> dict[str, pd.Timestamp]:
    """Free の窓（直近 12 週間を除く 2 年）の中の日付。old = 窓の始まりの近く、new = 窓の終わりの近く。財務は決算の多い日を 3 つ。"""
    today = pd.Timestamp(today).normalize()
    old = today - pd.DateOffset(years=2) + pd.Timedelta(days=35)
    new = today - pd.Timedelta(weeks=12) - pd.Timedelta(days=14)
    old, new = _weekday(old), _weekday(new)
    fins = []
    for y in range(today.year - 2, today.year + 1):
        for m, d in ((2, 13), (5, 14), (8, 13), (11, 13)):
            t = _weekday(pd.Timestamp(year=y, month=m, day=d))
            if old <= t <= new:
                fins.append(t)
    return {"old": old, "new": new, "fins": fins[-3:]}


def _weekday(t: pd.Timestamp) -> pd.Timestamp:
    while t.weekday() >= 5:
        t += pd.Timedelta(days=1)
    return t


def run_check(client: Client, today: pd.Timestamp, n_codes: int = 3, log: Callable[[str], None] = print) -> dict:
    """段階 1 の確認。返ってきたものを数えて、7 つの「確かめられなかった点」に答える材料を集める。"""
    d = check_dates(today)
    out: dict = {"dates": d, "calls": []}

    def get(path, params, **kw):
        log(f"呼び出し: {path} {params}")
        r = client.get(path, params, **kw)
        out["calls"].append({"path": path, "params": params, "status": r.status, "rows": len(r.rows), "container": r.container,
                             "pages": r.pages, "message": r.message, "fields": r.fields()})
        log(f"  → {r.status}（{STATUS.get(r.status, '不明')}）{len(r.rows):,} 行" + (f"。{r.message}" if r.message else ""))
        return r

    fmt = lambda t: f"{t:%Y-%m-%d}"   # noqa: E731
    m_old = get("/equities/master", {"date": fmt(d["old"])})
    if m_old.status in (400, 403) or not m_old.ok:
        out["stopped"] = f"上場銘柄一覧が取れない（{m_old.status}: {STATUS.get(m_old.status, '')}）。キーとプランを確かめる"
        return out
    m_new = get("/equities/master", {"date": fmt(d["new"])})
    fo = m_old.fields()
    code_f, scale_f = pick(fo, "Code", "LocalCode"), pick(fo, "ScaleCat", "ScaleCategory")
    mkt_f, mktnm_f = pick(fo, "Mkt", "MarketCode"), pick(fo, "MktNm", "MarketCodeName")
    out["master_fields"], out["code_field"], out["scale_field"] = fo, code_f, scale_f
    if code_f is None:
        out["stopped"] = "上場銘柄一覧に銘柄コードの項目が見つからない（項目名を報告するだけで止める）"
        return out
    old_codes = {str(r.get(code_f)) for r in m_old.rows}
    new_codes = {str(r.get(code_f)) for r in m_new.rows} if m_new.ok else set()
    scale = {str(r.get(code_f)): str(r.get(scale_f)) for r in m_old.rows} if scale_f else {}
    out["scale_values"] = pd.Series(list(scale.values())).value_counts().to_dict() if scale else {}
    if mkt_f:
        out["markets"] = (pd.DataFrame(m_old.rows)[[c for c in (mkt_f, mktnm_f) if c]].value_counts().head(12)
                          .reset_index().astype(str).values.tolist())
    t500_old = {c for c, s in scale.items() if s in TOPIX500}
    gone = sorted(old_codes - new_codes) if m_new.ok else []
    out.update(n_old=len(old_codes), n_new=len(new_codes), n_gone=len(gone), n_added=len(new_codes - old_codes) if m_new.ok else 0,
               n_t500_old=len(t500_old), gone_t500=sorted(set(gone) & t500_old))
    # 日付だけの日足（その日の全銘柄）
    b_old = get("/equities/bars/daily", {"date": fmt(d["old"])})
    fb = b_old.fields()
    bcode = pick(fb, "Code", "LocalCode")
    out["bars_fields"] = fb
    out["bars_adj"] = {"AdjC": pick(fb, "AdjC", "AdjustmentClose") is not None, "AdjFactor": pick(fb, "AdjFactor", "AdjustmentFactor") is not None}
    have = {str(r.get(bcode)) for r in b_old.rows} if bcode else set()
    out["gone_with_bars_by_date"] = len(set(gone) & have)
    # 消えた会社を、コードで期間指定して取れるか
    sample = (out["gone_t500"] or gone)[:n_codes]
    got = {}
    for c in sample:
        r = get("/equities/bars/daily", {"code": c, "from": fmt(d["old"]), "to": fmt(d["old"] + pd.Timedelta(days=31))})
        got[c] = (r.status, len(r.rows))
    out["gone_by_code"] = got
    # 財務（決算の多い日）
    fins, fin_rows = [], []
    for t in d["fins"]:
        r = get("/fins/summary", {"date": fmt(t)})
        fin_rows.extend(r.rows)
        f = r.fields()
        dt = pick(f, "DocType", "TypeOfDocument")
        fins.append({"date": fmt(t), "status": r.status, "rows": len(r.rows), "fields": f,
                     "doc_types": pd.Series([str(x.get(dt)) for x in r.rows]).value_counts().to_dict() if dt and r.rows else {},
                     "forecast": {n: pick(f, n) is not None for n in ("FSales", "FOP", "FNP", "FEPS", "DiscDate", "DiscTime")}})
    out["fins"] = fins
    out["e1_probe"] = probe_revisions(get, fin_rows, d, fmt, n_codes)
    out["calls_total"] = client.calls
    return out


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def probe_revisions(get, fin_rows: list[dict], d: dict, fmt, n_codes: int = 3) -> dict:
    """E1 を作り直せるかの、操作での確かめ（docs/survivorship.md 4.4）。

    3 日の財務の行のうち、書類の種類が決算短信（FinancialStatements を含む）でない行を数え、その会社（多くて n_codes 社）の財務を期間で取り、
    同じ会社の直前の開示（同じ決算期の項目があれば、同じ決算期の中で）から、通期の純利益の会社予想 FNP が変わっているかを数える。
    書類の種類の名前の意味で決めず、「決算短信でない開示で予想が動いたか」で決める。最後の判断は人が check.md を読んで書く。
    """
    out = {"non_fs": 0, "codes": [], "changed": 0, "checked": 0, "fy_field": None, "fields_seen": []}
    if not fin_rows:
        return out
    f = list(dict.fromkeys(k for r in fin_rows[:200] for k in r))
    dt, code_f, fnp = pick(f, "DocType", "TypeOfDocument"), pick(f, "Code", "LocalCode"), pick(f, "FNP", "ForecastProfit")
    out["fields_seen"] = f
    if dt is None or code_f is None or fnp is None:
        out["missing"] = [n for n, v in (("DocType", dt), ("Code", code_f), ("FNP", fnp)) if v is None]
        return out
    non_fs = [r for r in fin_rows if "financialstatements" not in str(r.get(dt, "")).lower()]
    out["non_fs"] = len(non_fs)
    codes = list(dict.fromkeys(str(r.get(code_f)) for r in non_fs))[:n_codes]
    out["codes"] = codes
    for c in codes:
        r = get("/fins/summary", {"code": c})
        if not r.ok or not r.rows:
            continue
        rf = r.fields()
        dd, tt = pick(rf, "DiscDate", "DisclosedDate"), pick(rf, "DiscTime", "DisclosedTime")
        fy = pick(rf, "CurFYEn", "CurrentFiscalYearEndDate")
        out["fy_field"] = out["fy_field"] or fy
        rows = sorted(r.rows, key=lambda x: (str(x.get(dd, "")), str(x.get(tt, ""))))
        target_dates = {str(x.get(pick(list(x), "DiscDate", "DisclosedDate") or "", "")) for x in non_fs if str(x.get(code_f)) == c}
        for i, row in enumerate(rows):
            if "financialstatements" in str(row.get(dt, "")).lower() or str(row.get(dd, "")) not in target_dates:
                continue
            prev = [p for p in rows[:i] if fy is None or str(p.get(fy)) == str(row.get(fy))]
            prev = [p for p in prev if pd.notna(_num(p.get(fnp)))]
            if not prev or pd.isna(_num(row.get(fnp))):
                continue
            out["checked"] += 1
            if _num(prev[-1].get(fnp)) != _num(row.get(fnp)):
                out["changed"] += 1
    return out



def report(res: dict, today: pd.Timestamp) -> list[str]:
    """確認の結果を日本語の報告にする（docs/survivorship.md 3.1 の 7 点）。"""
    L: list[str] = []
    say = L.append
    d = res["dates"]
    say("# J-Quants の無料プランでの確認（docs/survivorship.md の段階 1）")
    say(f"作成: {today:%Y-%m-%d}。使った日付: 古い一覧 {d['old']:%Y-%m-%d}、新しい一覧 {d['new']:%Y-%m-%d}、"
        f"財務 {', '.join(f'{t:%Y-%m-%d}' for t in d['fins']) or '（窓の中に決算の多い日が無い）'}")
    say("\n## 1. 呼び出しの記録\n")
    say("| 入口 | 引数 | 状態 | 行 | 入れ物 | ページ | メッセージ |")
    say("|---|---|---|---|---|---|---|")
    for c in res["calls"]:
        say(f"| {c['path']} | {c['params']} | {c['status']}（{STATUS.get(c['status'], '不明')}） | {c['rows']:,} | {c['container'] or '–'} | {c['pages']} | {c['message'] or ''} |")
    if res.get("stopped"):
        say(f"\n**止めた: {res['stopped']}**")
        return L
    say("\n## 2. 答え（確かめられなかった 7 点）\n")
    gone, g500 = res.get("n_gone", 0), res.get("gone_t500", [])
    a1 = (f"**入る**: 古い一覧の {res['n_old']:,} 社のうち、新しい一覧に無い会社が {gone:,} 社（うち古い日に TOPIX500 だった会社 {len(g500)} 社）"
          if gone else "**確かめられなかった**: 新しい一覧に無い会社が 0 社（窓の中で廃止が無かったか、一覧が今の会社だけを返している）")
    say(f"1. 過去の日付の一覧に、後で消えた会社が入るか: {a1}。新しく増えた会社 {res.get('n_added', 0):,} 社")
    say(f"2. 日足の項目名: {', '.join(res.get('bars_fields', [])) or '（行が無い）'}")
    say(f"3. 市場区分のコード（上位）: {res.get('markets', '（項目が無い）')}")
    for f in res.get("fins", []):
        say(f"4. 財務 {f['date']}（{f['status']}、{f['rows']} 行）の書類の種類: {f['doc_types'] or '（無し）'}。"
            f"会社予想などの項目: " + "、".join(f"{k} {'あり' if v else '無し'}" for k, v in f["forecast"].items()))
    pr = res.get("e1_probe", {})
    if pr.get("missing"):
        say(f"   → E1 の確かめ（4.4）: 財務に {', '.join(pr['missing'])} の項目が無く、操作での確かめができなかった")
    else:
        say(f"   → E1 の確かめ（4.4、操作で決める）: 3 日の財務のうち、決算短信（FinancialStatements を含む種類）でない行 {pr.get('non_fs', 0)} 件。"
            f"そのうち {len(pr.get('codes', []))} 社を期間で取り、直前の開示と比べられた {pr.get('checked', 0)} 件のうち、通期の純利益の会社予想（FNP）が"
            f"変わっていたもの **{pr.get('changed', 0)} 件**（同じ決算期の項目: {pr.get('fy_field') or '見つからず、直前の開示と比べた'}）")
        say("     1 件以上なら「決算短信でない開示で予想の変化が取れる」= S-E1 を作る側。0 件なら作らない側。どちらにするかは、この報告を人が読んで "
            "docs/survivorship.md 7 章に書く（機械では決めない）")
    adj = res.get("bars_adj", {})
    say(f"5. 日付だけの全銘柄の日足に、調整後の終値（AdjC）{'あり' if adj.get('AdjC') else '無し'}・調整係数（AdjFactor）{'あり' if adj.get('AdjFactor') else '無し'}。"
        f"古い日の全銘柄の日足に、消えた会社の行 {res.get('gone_with_bars_by_date', 0)} 社分")
    byc = res.get("gone_by_code", {})
    say("6. 消えた会社をコードで期間指定して取れるか: " + ("、".join(f"{c} → {s}・{n} 行" for c, (s, n) in byc.items()) if byc else "試す会社が無かった"))
    conts = sorted({c['container'] for c in res["calls"] if c['container']})
    say(f"7. 応答の入れ物の名前: {', '.join(conts) or '–'}（公式の例は data）")
    say(f"\n- 規模区分の値（古い一覧）: {res.get('scale_values', {})}")
    say(f"- 呼び出しは全部で {res.get('calls_total', 0)} 回（ページ分けを含む。Free は 1 分 5 回なので 13 秒おき）")
    say("\n## 3. 読み方\n")
    say("- 1 が「入る」で 6 が取れれば、時点ごとの TOPIX500（後で消えた会社を含む）を作れる。段階 2（有料 1 か月）に進むかを利用者に聞く")
    say("- 無料の窓は直近の 2 年だけなので、ここで分かるのは仕組みまで。偏りの大きさ（2010 年代）は有料の段階でしか分からない")
    say("- 取ったデータはこのリポジトリに入れない（J-Quants の規約: 個人の私的利用のみ・再配布は不可）。この報告は数・項目名・銘柄コードだけ。"
        "公開のリポジトリに移すときは、銘柄コードの一覧（消えた会社など）を消す")
    return L
