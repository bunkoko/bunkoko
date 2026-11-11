"""J-Quants API（JPX 総研の公式。V2）の確認と取得。docs/survivorship.md の段階 1（無料プランで 1 時間の確認）と、段階 2 の取得の道具（8.6）。

公式の文書で確かめた形（チームの調査役の第 2 回 B、2026-10-15）だけを使う:
- ベース URL `https://api.jquants.com/v2`、認証はヘッダー `x-api-key: <キー>`
- 上場銘柄一覧 `/equities/master?date=`（規模区分 `ScaleCat`）、日足 `/equities/bars/daily?date=`（日付だけで全銘柄）・`?code=&from=&to=`、
  財務 `/fins/summary?date=`（`DiscDate`・`DiscTime`・`DocType`・会社予想 `FSales`・`FOP`・`FNP`）
- 続きは `pagination_key`。状態 200 成功・210 データ無し・400 引数の誤り・403 権限なし（プラン外・キーの誤り）・429 回数の上限（2 分待つ）・500
- Free は 1 分 5 回、直近 12 週間を除く 2 年

項目名・応答の包み（`data`）は決め打ちにせず、返ってきたものをそのまま報告する（確かめられなかった 7 点を、実物で確かめるため）。
"""

from __future__ import annotations

import http.client
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

BASE = "https://api.jquants.com/v2"
KEY_FILE = Path.home() / ".bunkoko" / "jquants.json"
LOCK_FILE = KEY_FILE.parent / "jquants.lock"   # 同時に 1 本だけ。置き場所ごとでなくキーと同じ所に 1 つ（check も fetch も同じ印を見る）
NET_RETRY_WAIT = 30.0            # 通信の例外（時間切れ・切断）は、これだけ待って 1 回だけやり直す
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


def http_getter(key: str, timeout: float = 60.0, sleep: Callable[[float], None] = time.sleep,
                opener: Callable | None = None) -> Callable[[str, dict], tuple[int, bytes]]:
    """1 回の呼び出し。通信の例外（時間切れ・切断・途中で切れた応答）は、NET_RETRY_WAIT 秒待って 1 回だけやり直し、続けば Stop にする。

    誤りの文に出すのは例外の種類と理由だけ（URL・ヘッダー・キーは出さない）。
    """
    opener = opener or urlopen

    def get(path: str, params: dict) -> tuple[int, bytes]:
        url = f"{BASE}{path}" + (f"?{urlencode(params)}" if params else "")
        req = Request(url, headers={"x-api-key": key, "User-Agent": "bunkoko-check/1.0"})
        for attempt in range(2):
            try:
                try:
                    with opener(req, timeout=timeout) as r:
                        return int(r.status), r.read()
                except HTTPError as e:                   # 状態つきの断り（403・429・500 など）は、そのまま返す
                    return int(e.code), e.read() if hasattr(e, "read") else b""
            except (OSError, http.client.HTTPException) as e:     # URLError・TimeoutError・ConnectionResetError・IncompleteRead など
                why = str(getattr(e, "reason", "") or type(e).__name__)[:120]
                if attempt == 0:
                    sleep(NET_RETRY_WAIT)
                    continue
                raise Stop(f"つながらない（{why}）。同じ命令で続きから取れる") from e
        raise Stop("ここには来ない")
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
                 sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print,
                 path_interval: dict[str, float] | None = None):
        self.getter, self.interval, self.sleep, self.log = getter, max(float(interval), MIN_INTERVAL), sleep, log
        self.path_interval = dict(path_interval or {})      # 入口ごとの、これより速くしない間隔（足す分。path_floor は必ず効く）
        self.calls = 0
        self._last: float | None = None

    def _call(self, path: str, params: dict) -> tuple[int, object, str]:
        for attempt in range(2):
            if self._last is not None:
                self.sleep(max(self.interval, path_floor(path), self.path_interval.get(path, 0.0)))
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
                if res.pages:                            # 何ページか取れた後の断り（210 を含む）は「途中で切れた」。行なしとは読ませない
                    res.message = f"{res.pages} ページ目の後で切れた（{status}: {msg or STATUS.get(status, '不明')}）"[:200]
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


# --------------------------------------------------------------------------- 取得（段階 2 の道具。docs/survivorship.md 8.6）
# 使う入口は、段階 1（2026-11-08、Free）で実物を確かめた 3 つだけ: 上場銘柄一覧・日足・財務を、どれも `date` 1 つで呼ぶ。
# 配当金・指数の構成などは入口の名前を確かめられていないので、ここには書かない（確かめた後に `--also 名前=入口` で足せる）。
DATA_DIR = Path("data/jquants")                      # コミットしない（.gitignore の data/。規約: 再配布は不可）
FETCH_KINDS = {"bars": "/equities/bars/daily", "fins": "/fins/summary", "master": "/equities/master"}
# 呼び出しの間隔（秒）。Free は 1 分 5 回 → 13 秒。Premium は 1 分 500 回 → 余裕を見て 0.15 秒（1 分 400 回）。
# Light・Standard の回数は確かめられていないので、Free と同じ遅い側にしておく（--interval で変えられるが MIN_INTERVAL より速くはならない）
PLAN_INTERVAL = {"free": 13.0, "light": 13.0, "standard": 13.0, "premium": 0.15}
PATH_INTERVAL = {"/fins/summary": 1.2}               # 財務は全プラン 1 分 60 回 → 1.2 秒（1 分 50 回）
SLOW_INTERVAL = 1.2                                  # 財務の仲間（/fins/ で始まる入口）と、確かめていない入口（--also）の、これより速くしない間隔
FAST_PATHS = ("/equities/bars/daily", "/equities/master")   # プランの間隔だけで呼んでよいと分かっている入口（2 章。それ以外は遅い側）
MIN_INTERVAL = 0.12
FETCH_MAX_PAGES = 200                                # 1 日分がこれより多いページに分かれたら、欠けたまま保存せずに「失敗」にする
EMPTY_RECHECK_DAYS = 7                               # 新しい日の「行なし」は、まだ出ていないだけかもしれないので、次の回にもう一度聞く
MAX_REFUSALS = 3                                     # 続けてこの回数断られたら止める（キー・プラン・期間の誤り。相手に迷惑をかけない）
MAX_HOLIDAY_RUN = 6                                  # 月末からさかのぼる休みの日の上限（平日）。これより続いたら、月末の日を決めない
STATE_FILE = "state.json"


def path_floor(path: str) -> float:
    """入口ごとの、これより速くしない間隔（秒）。2026-11-11 点検の指摘で直した: 名前がぴったり同じ入口だけでなく、
    `/fins/` で始まる入口すべてと、確かめていない入口（--also で足したもの）を遅い側（1.2 秒）にする。"""
    return 0.0 if path in FAST_PATHS else max(SLOW_INTERVAL, PATH_INTERVAL.get(path, 0.0))


def call_interval(plan_interval: float, path: str) -> float:
    return max(float(plan_interval), MIN_INTERVAL, path_floor(path))


def plan_interval(plan: str, asked: float | None = None) -> float:
    """プランごとの間隔。`--interval` は遅くする方にだけ効く（2026-11-11 点検の指摘で直した。速くはならない）。"""
    return max(PLAN_INTERVAL[plan], float(asked) if asked is not None else 0.0)


def weekdays(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    """start〜end の平日（祝日は分からないので入る。取ってみて行が無ければ「行なし」と覚える）。"""
    start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    return list(pd.bdate_range(start, end)) if start <= end else []


def free_window(today: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Free で取れる期間（直近 12 週間を除く 2 年）。"""
    today = pd.Timestamp(today).normalize()
    return today - pd.DateOffset(years=2), today - pd.Timedelta(weeks=12)


def load_state(root: Path) -> dict:
    f = Path(root) / STATE_FILE
    if not f.exists():
        return {"version": 1, "items": {}}
    bad = Stop(f"{f} が読めない（壊れている・形が違う）。名前を変えて退けてから、もう一度動かす（取った日の分は、ファイルから数え直さない）")
    try:
        st = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise bad from None
    # 形が違うもの（[]・{}・items が表でない）も、黙って空から始めない（2026-11-11 点検の指摘で直した）
    if not isinstance(st, dict) or not isinstance(st.get("items"), dict):
        raise bad
    if not all(isinstance(v, dict) and all(isinstance(e, dict) for e in v.values()) for v in st["items"].values()):
        raise bad
    return st


def save_state(root: Path, state: dict) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    tmp.replace(root / STATE_FILE)


def day_file(root: Path, kind: str, day: str) -> Path:
    return Path(root) / kind / f"{day}.csv.gz"


def read_day(root: Path, kind: str, day: str) -> pd.DataFrame:
    """取った 1 日分。文字のまま読む（銘柄コードの頭の 0・英字つきのコードをこわさない。数にするのは使う側）。"""
    return pd.read_csv(day_file(root, kind, day), dtype=str, compression="gzip")


def _write_day(root: Path, kind: str, day: str, rows: list[dict]) -> Path:
    f = day_file(root, kind, day)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    pd.DataFrame(rows).to_csv(tmp, index=False, compression="gzip")
    tmp.replace(f)                                   # 途中で止まっても、半分だけのファイルは残らない
    return f


def _has_rows(entry: dict | None) -> bool:
    return bool(entry) and entry.get("status") == 200 and int(entry.get("rows", 0)) > 0


def _is_empty(entry: dict | None) -> bool:
    return bool(entry) and entry.get("status") in (200, 210) and int(entry.get("rows", 0)) == 0


def _drop_file(root: Path, kind: str, day: str) -> None:
    day_file(root, kind, day).unlink(missing_ok=True)


def last_weekday(month: pd.Period) -> pd.Timestamp:
    """その月の最後の平日。"""
    t = month.to_timestamp(how="end").normalize()
    while t.weekday() >= 5:
        t -= pd.Timedelta(days=1)
    return t


def item_done(entry: dict | None, root: Path, kind: str, day: str, plan: str, now: pd.Timestamp, retry_empty: bool = False) -> bool:
    """もう取らなくてよいか。行のある日は、同じプランで取ったファイルがあれば済み。「行なし」は、同じプランで聞いた古い日だけ済み。

    プランを替えたら全部取り直す（調整後の値は「取った日」が基準のはずなので、Free で試した分と有料で取った分を混ぜない）。
    """
    if _has_rows(entry):
        return entry.get("plan") == plan and day_file(root, kind, day).exists()
    if _is_empty(entry) and not retry_empty:
        return entry.get("plan") == plan and pd.Timestamp(day) <= pd.Timestamp(now).normalize() - pd.Timedelta(days=EMPTY_RECHECK_DAYS)
    return False


def month_end_days(state: dict, start: pd.Timestamp, end: pd.Timestamp) -> tuple[list[str], list[str]]:
    """月ごとの、最後に取引のあった日（全銘柄の日足に行があった日）。docs/survivorship.md 8.2 の 3。

    一覧（master）は取引のあった日だけ指定して取る（休みの日を指定すると何が返るかを確かめられていないため）。
    月末までの平日が全部「行あり」か「行なし」に決まっていない月は決めない（2 つ目の返り値に月を入れる）。
    """
    bars = state.get("items", {}).get("bars", {})
    start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    days, undecided = [], []
    for m in pd.period_range(start, end, freq="M"):
        last = last_weekday(m)                       # 暦の月末でなく最後の平日と比べる（月末が土日の月を落とさない。2026-11-11 点検の指摘で直した）
        if last > end:
            continue                                 # 月の途中までしか頼まれていない月は、月末がまだ分からない
        found = None
        # 月末からさかのぼる。休みが MAX_HOLIDAY_RUN より続いたら決めない（取れる期間の外を「行なし」と覚えただけかもしれない）
        for t in list(reversed(weekdays(max(start, m.to_timestamp(how="start").normalize()), last)))[:MAX_HOLIDAY_RUN + 1]:
            e = bars.get(f"{t:%Y-%m-%d}")
            if _has_rows(e):
                found = f"{t:%Y-%m-%d}"
                break
            if not _is_empty(e):
                break                                # まだ取っていない・失敗した日がある
        if found:
            days.append(found)
        else:
            undecided.append(str(m))
    return days, undecided


def fetch_plan(state: dict, root: Path, start: pd.Timestamp, end: pd.Timestamp, kinds: list[str], plan: str, now: pd.Timestamp,
               retry_empty: bool = False) -> dict[str, list[str]]:
    """これから取る日（種類ごと）。master は、その時点の日足の記録から決まる月末の日だけ。"""
    items = state.get("items", {})
    todo: dict[str, list[str]] = {}
    for k in kinds:
        days = month_end_days(state, start, end)[0] if k == "master" else [f"{t:%Y-%m-%d}" for t in weekdays(start, end)]
        todo[k] = [d for d in days if not item_done(items.get(k, {}).get(d), root, k, d, plan, now, retry_empty)]
    return todo


def run_fetch(client: Client, start: pd.Timestamp, end: pd.Timestamp, kinds: list[str], plan: str, root: Path = DATA_DIR,
              now: pd.Timestamp | None = None, max_calls: int | None = None, retry_empty: bool = False,
              paths: dict[str, str] | None = None, log: Callable[[str], None] = print) -> dict:
    """日付ごとに取って `root/<種類>/<日付>.csv.gz` に置く。済んだ日は飛ばすので、何度動かしてもよい（続きから）。

    順番は 日足 → 財務 →（そのほか）→ 一覧。一覧は、日足で「取引のあった日」が分かってから、月ごとの最後の取引日だけを取る。
    返り値: 種類ごとの {取った・行なし・失敗}、止めた理由（あれば）、呼び出しの数。
    """
    paths = dict(FETCH_KINDS, **(paths or {}))
    unknown = [k for k in kinds if k not in paths]
    if unknown:
        raise Stop(f"知らない種類: {', '.join(unknown)}（使えるのは {', '.join(paths)}）")
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="Asia/Tokyo").tz_localize(None)
    root = Path(root)
    state = load_state(root)
    items = state.setdefault("items", {})
    order = [k for k in kinds if k != "master"] + (["master"] if "master" in kinds else [])
    out: dict = {"kinds": {k: {"saved": 0, "empty": 0, "failed": 0, "rows": 0} for k in order}, "stopped": None, "undecided_months": []}
    refusals, since_save = 0, 0
    try:
        for k in order:
            if k == "master":
                out["undecided_months"] = month_end_days(state, start, end)[1]
            todo = fetch_plan(state, root, start, end, [k], plan, now, retry_empty)[k]
            log(f"{k}: これから取る日 {len(todo):,}" + (f"（{todo[0]}〜{todo[-1]}）" if todo else "（全部済み）"))
            for day in todo:
                if max_calls is not None and client.calls >= max_calls:
                    out["stopped"] = f"呼び出しの上限（{max_calls} 回）まで取った。同じ命令で続きから取れる"
                    return out
                r = client.get(paths[k], {"date": day}, max_pages=FETCH_MAX_PAGES)
                entry = {"status": r.status, "rows": len(r.rows), "pages": r.pages, "plan": plan, "at": f"{now:%Y-%m-%d %H:%M}"}
                cut = r.message.startswith("続きがあった")
                if r.ok and r.rows and not cut:
                    date_f = pick(r.fields(), "Date")
                    if date_f:                       # 返ってきた行の日付（指定と違えば、status で知らせる。確かめ直しでは使わない）
                        got = sorted({str(x.get(date_f))[:10] for x in r.rows})
                        entry["dates"] = got[:3]
                    _write_day(root, k, day, r.rows)
                    out["kinds"][k]["saved"] += 1
                    out["kinds"][k]["rows"] += len(r.rows)
                    refusals = 0
                    if k == "master":                # 一覧は月に 1 つだけ（その月の最後の取引日）。同じ月の古い日の分は消す
                        for other in [d for d in items.get(k, {}) if d[:7] == day[:7] and d != day]:
                            _drop_file(root, k, other)
                            del items[k][other]
                elif (r.ok and not cut and not r.rows) or (r.status == 210 and r.pages == 0 and not r.rows):
                    entry["rows"] = 0                # 1 ページ目から行が無いときだけ「行なし」（休みの日のはず。status が別の出所と突き合わせる）
                    _drop_file(root, k, day)         # 前のプランで取ったファイルを残さない
                    out["kinds"][k]["empty"] += 1
                    refusals = 0
                else:                                # 断り・ページの途中で切れた（行が 1 行でもある状態で 200 以外）・ページが多すぎる
                    partial = bool(r.rows) and not r.ok
                    entry.update(status=-1 if (cut or partial) else r.status, rows=0,
                                 message=(r.message or STATUS.get(r.status, "不明"))[:200])
                    _drop_file(root, k, day)
                    out["kinds"][k]["failed"] += 1
                    refusals += 1
                    log(f"  {k} {day}: 取れなかった（{r.status}: {entry['message']}）")
                items.setdefault(k, {})[day] = entry
                since_save += 1
                if since_save >= 25:
                    save_state(root, state)
                    since_save = 0
                if refusals >= MAX_REFUSALS:
                    out["stopped"] = (f"続けて {MAX_REFUSALS} 回断られたので止めた（最後は {r.status}: {STATUS.get(r.status, '不明')}）。"
                                      "キー・プラン・期間（--from --to）を確かめる")
                    return out
    except Stop as e:
        out["stopped"] = str(e)
    except (OSError, http.client.HTTPException) as e:    # 通信の例外がここまで上がってきても、長い誤りの表示で落とさない
        out["stopped"] = f"途中で止まった（{type(e).__name__}。回線かファイルの書き込みの誤り）。同じ命令で続きから取れる"
    finally:
        out["calls"] = client.calls
        save_state(root, state)
    return out


def read_trading_days(path: Path) -> set[str]:
    """別の出所の営業日（CSV。`Date`・`date`・`日付` の列、無ければ 1 列目）。行なしの平日と突き合わせるためだけに使う。"""
    try:
        df = pd.read_csv(path, dtype=str)
    except (OSError, ValueError) as e:
        raise Stop(f"{path} が読めない（{type(e).__name__}）") from e
    col = next((c for c in df.columns if str(c).strip().lower() in ("date", "datetime", "日付")), df.columns[0] if len(df.columns) else None)
    if col is None:
        raise Stop(f"{path} に列が無い")
    t = pd.to_datetime(df[col].str.slice(0, 10), errors="coerce").dropna()
    if len(t) < 20:
        raise Stop(f"{path} の {col} の列から日付を 20 日分も読めなかった（営業日の一覧として使えない）")
    return {f"{x:%Y-%m-%d}" for x in t}


EMPTY_PER_YEAR = (10, 25)                            # 1 年の「行なしの平日」の見当（祝日と年末年始で 15〜20 日）。外れた年は ⚠


def fetch_status(root: Path = DATA_DIR, trading_days: set[str] | None = None) -> dict:
    """何を何件取ったか。state.json とファイルを数える（ネットにはつながない）。

    2026-11-11 点検の指摘で足した照らし合わせ（`checks`）: 取引のあった日が「行なし」と覚えられていないか（財務に行がある・年の数が見当から
    外れる・別の出所の営業日と合わない）、月末の一覧の抜けと重なり、記録に無いファイル。
    """
    root = Path(root)
    state = load_state(root)
    out: dict = {"root": str(root), "kinds": {}, "bytes": 0, "checks": {}}
    for k, ent in sorted(state.get("items", {}).items()):
        have = sorted(d for d, e in ent.items() if _has_rows(e))
        empty = sorted(d for d, e in ent.items() if _is_empty(e))
        failed = sorted(d for d, e in ent.items() if not _has_rows(e) and not _is_empty(e))
        lost = [d for d in have if not day_file(root, k, d).exists()]
        size = sum(day_file(root, k, d).stat().st_size for d in have if d not in lost)
        wrong = [d for d in have if ent[d].get("dates") and ent[d]["dates"] != [d]]
        stray = sorted(f.name[:-len(".csv.gz")] for f in (root / k).glob("*.csv.gz") if f.name[:-len(".csv.gz")] not in have)
        gaps: list[str] = []
        if have and k != "master":                   # 取った最初の日〜最後の日の間で、まだ聞いていない・失敗した平日
            seen = set(have) | set(empty)
            gaps = [f"{t:%Y-%m-%d}" for t in weekdays(pd.Timestamp(have[0]), pd.Timestamp(have[-1])) if f"{t:%Y-%m-%d}" not in seen]
        out["kinds"][k] = {"days": len(have), "first": have[0] if have else None, "last": have[-1] if have else None,
                           "rows": sum(int(ent[d]["rows"]) for d in have), "empty": len(empty), "failed": failed, "lost": lost,
                           "wrong_date": wrong, "gaps": gaps, "stray": stray, "bytes": size,
                           "plans": sorted({str(e.get("plan")) for e in ent.values() if e.get("plan")})}
        out["bytes"] += size
    out["checks"] = _cross_checks(state, trading_days)
    return out


def _cross_checks(state: dict, trading_days: set[str] | None) -> dict:
    """日足の「行なし」が本当に休みの日か、月末の一覧がそろっているかの照らし合わせ（docs/survivorship.md 8.2 の 8）。"""
    items = state.get("items", {})
    bars, fins, master = items.get("bars", {}), items.get("fins", {}), items.get("master", {})
    have = sorted(d for d, e in bars.items() if _has_rows(e))
    empty = sorted(d for d, e in bars.items() if _is_empty(e))
    c: dict = {"empty_days": empty, "empty_with_fins": [], "isolated_empty": [], "odd_years": [], "checked": trading_days is not None,
               "traded_but_empty": [], "empty_outside_calendar": [], "master_missing": [], "master_undecided": [], "master_extra": []}
    if not have:
        return c
    hs, es = set(have), set(empty)
    c["empty_with_fins"] = [d for d in empty if _has_rows(fins.get(d))]
    one = pd.offsets.BDay(1)
    c["isolated_empty"] = [d for d in empty if f"{pd.Timestamp(d) - one:%Y-%m-%d}" in hs and f"{pd.Timestamp(d) + one:%Y-%m-%d}" in hs]
    seen = hs | es
    first, last = pd.Timestamp(min(seen)), pd.Timestamp(max(seen))
    for y in range(first.year, last.year + 1):
        days = [f"{t:%Y-%m-%d}" for t in weekdays(pd.Timestamp(year=y, month=1, day=1), pd.Timestamp(year=y, month=12, day=31))]
        n = sum(d in es for d in days)
        full = all(d in seen for d in days)
        if n > EMPTY_PER_YEAR[1] or (full and n < EMPTY_PER_YEAR[0]):
            c["odd_years"].append(f"{y} 年（{n} 日）")
    if trading_days is not None:
        lo, hi = min(trading_days), max(trading_days)
        c["traded_but_empty"] = [d for d in empty if d in trading_days]
        c["empty_outside_calendar"] = [d for d in empty if not lo <= d <= hi]
    # 月末の一覧: 日足が月の最後の平日まで決まっている月には、その月の最後の取引日の一覧が 1 つだけあるはず
    expect, undecided = month_end_days(state, pd.Timestamp(have[0]), last)
    c["master_undecided"] = undecided
    c["master_missing"] = [d for d in expect if not _has_rows(master.get(d))]
    c["master_extra"] = sorted(d for d, e in master.items() if _has_rows(e) and d not in set(expect))
    return c


def status_lines(st: dict) -> list[str]:
    L = [f"J-Quants から取ったもの（{st['root']}。コミットしない）"]
    if not st["kinds"]:
        return L + ["- まだ何も取っていない（./cfd jquants fetch --from … --to …）"]
    names = {"bars": "日足（全銘柄）", "fins": "財務（開示）", "master": "上場銘柄一覧（月末）"}

    def show(xs: list[str]) -> str:
        return "、".join(xs[:5]) + (f" ほか {len(xs) - 5}" if len(xs) > 5 else "")

    def every(xs: list[str]) -> str:                 # 疑わしい日は全部の日付を出す
        return "、".join(xs)

    for k, v in st["kinds"].items():
        L.append(f"- {names.get(k, k)}: {v['days']:,} 日分・{v['rows']:,} 行（{v['first'] or '–'}〜{v['last'] or '–'}）、"
                 f"行なしの日 {v['empty']:,}、{v['bytes'] / 1e6:,.1f} MB、プラン {'・'.join(v['plans']) or '–'}")
        if v["failed"]:
            L.append(f"  - ⚠ 取れなかった日 {len(v['failed'])}: {show(v['failed'])}（同じ命令でもう一度）")
        if v["gaps"]:
            L.append(f"  - ⚠ 間の抜けた平日 {len(v['gaps'])}: {show(v['gaps'])}")
        if v["lost"]:
            L.append(f"  - ⚠ 記録にあるのにファイルが無い日 {len(v['lost'])}: {show(v['lost'])}（同じ命令で取り直す）")
        if v.get("stray"):
            L.append(f"  - ⚠ 記録に無いのにファイルがある日 {len(v['stray'])}: {show(v['stray'])}（前のプランの残りなど。計算では読まない。消してよい）")
        if v["wrong_date"]:
            L.append(f"  - ⚠ 返ってきた行の日付が指定と違う日 {len(v['wrong_date'])}: {show(v['wrong_date'])}（確かめ直しでは使わない）")
        if len(v["plans"]) > 1:
            L.append("  - ⚠ プランの違う回が混ざっている（調整後の値の基準がずれるおそれ。判定の計算は 1 つのプランで取り直した分だけで行う。"
                     "docs/survivorship.md 8.2 の 5）")
    c = st.get("checks") or {}
    retry = "`--retry-empty` を付けて取り直す"
    if c.get("empty_with_fins"):
        L.append(f"- ⚠ 日足が行なしなのに、財務に開示のある平日 {len(c['empty_with_fins'])}: {every(c['empty_with_fins'])}"
                 f"（取引のあった日のはず。{retry}）")
    if c.get("odd_years"):
        L.append(f"- ⚠ 行なしの平日の数が見当（年 {EMPTY_PER_YEAR[0]}〜{EMPTY_PER_YEAR[1]} 日）から外れた年: {every(c['odd_years'])}")
    if c.get("traded_but_empty"):
        L.append(f"- ⚠ 別の出所では取引のあった日なのに、日足が行なしの日 {len(c['traded_but_empty'])}: {every(c['traded_but_empty'])}（{retry}）")
    if c.get("empty_outside_calendar"):
        L.append(f"- ⚠ 別の出所の営業日の期間の外にあって、確かめられない行なしの日 {len(c['empty_outside_calendar'])}: "
                 f"{show(c['empty_outside_calendar'])}")
    if c.get("empty_days") and not c.get("checked"):
        L.append(f"- ⚠ 行なしの平日 {len(c['empty_days'])} 日を、別の出所の営業日とまだ突き合わせていない"
                 "（`./cfd jquants status --trading-days <1 列目が日付の CSV>`。docs/survivorship.md 8.2 の 8）"
                 + (f"。前後の平日は行ありの単発の行なし {len(c['isolated_empty'])}: {every(c['isolated_empty'])}" if c.get("isolated_empty") else ""))
    elif c.get("empty_days") and not c.get("traded_but_empty") and not c.get("empty_outside_calendar"):
        L.append(f"- 行なしの平日 {len(c['empty_days'])} 日は、別の出所の営業日に 1 日も入っていない（休みの日）")
    if c.get("master_undecided"):
        L.append(f"- ⚠ 月の最後の取引日を決められない月 {len(c['master_undecided'])}: {show(c['master_undecided'])}（日足の抜け・失敗、または行なしが続きすぎ）")
    if c.get("master_missing"):
        L.append(f"- ⚠ 月末の一覧が無い月 {len(c['master_missing'])}（その月の最後の取引日）: {show(c['master_missing'])}（同じ命令でもう一度）")
    if c.get("master_extra"):
        L.append(f"- ⚠ その月の最後の取引日でない日の一覧 {len(c['master_extra'])}: {show(c['master_extra'])}"
                 "（一覧は月に 1 つだけ使う。日足を取り直した後に同じ命令でもう一度動かすと、古い方は消える）")
    L.append(f"- 合わせて {st['bytes'] / 1e6:,.1f} MB")
    n = sum("⚠" in x for x in L)
    L.append(f"- ⚠ は {n} 行。1 行でも残っていれば、判定の計算はしない（docs/survivorship.md 8.2 の 8）" if n else "- ⚠ は無い")
    return L
