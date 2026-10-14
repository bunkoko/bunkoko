"""デモ口座で動く EA のログ（エキスパートタブ）と、同じデータ・同じ構成の Python を 1 日ずつ照らし合わせる。

EA は確定足ごとに 1 行書く（CfdCommodityEA.mq5 の LogDailyCheck）:

    2026.10.06 の足を判断: 終値 2650.12 → 買いのシグナル、直近 20 本の動き +0.53 倍（1.5 倍超の向きには入らない）、保有 0

ログは MT5 のデータフォルダの MQL5/Logs/<YYYYMMDD>.log（UTF-16）。1 行の正確な形（タブ・時刻・発信元
`CfdCommodityEA (XAUUSD.ps01,D1)`）は版で違いうるので、読み取りは寛容にする:
発信元は行のどこにあってもよく、見つからない行は「発信元が読めない」として数える。

照らし合わせの基準（docs/operations.md 12 章に事前登録）:
- 終値: EA の表示桁の丸め（半桁）以内
- シグナル: 有無と向きが同じ
- 行き過ぎの値: EA の表示（小数 2 桁）の丸め以内（0.006）
- 保有数: 同じ
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

EXT_TOL = 0.006   # EA は %+.2f で書く（丸めの半分 0.005 ＋ 計算の小さな差）

_SOURCE = re.compile(r"CfdCommodityEA\s*\(\s*([^,()\s]+)\s*,\s*([^)]+?)\s*\)")
_OTHER_SOURCE = re.compile(r"(\S+)\s*\(\s*[^,()\s]+\s*,\s*[^)]+?\s*\)")   # 他の EA・スクリプト「名前 (銘柄,時間足)」
_STAMP = re.compile(r"(?<!\d)(\d{4})\.(\d{2})\.(\d{2})\s+\d{1,2}:\d{2}:\d{2}")  # 行の頭の日時（タブからのコピー・保存したジャーナル）
_TIME = re.compile(r"(?<!\d)(\d{1,2}:\d{2}:\d{2})(?:\.\d+)?")
_JUDGE = re.compile(
    r"(\d{4})[.\-/](\d{2})[.\-/](\d{2})\s*の足を判断\s*[:：]\s*終値\s*(-?[\d.]+)\s*(?:→|->)\s*"
    r"(買いのシグナル|売りのシグナル|シグナルなし)"
    r"(?:\s*[、,]\s*直近\s*(\d+)\s*本の動き\s*([+\-]?[\d.]+)\s*倍(?:\s*[（(]\s*([\d.]+)\s*倍超[^）)]*[）)])?)?"
    r"\s*[、,]\s*保有\s*(\d+)")
_SIGNALS = {"買いのシグナル": 1, "売りのシグナル": -1, "シグナルなし": 0}
_TF_ALIASES = {"DAILY": "D1", "H1": "H1", "H4": "H4", "D1": "D1", "W1": "W1", "WEEKLY": "W1"}

# EA の他の行のうち、確認に使うもの。(正規表現, 種類, 重さ)。重さ: 要確認 > 注意 > 情報
ISSUE_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"order failed"), "発注の失敗", "要確認"),
    (re.compile(r"発注拒否"), "発注拒否", "要確認"),
    (re.compile(r"決済失敗"), "決済失敗", "要確認"),
    (re.compile(r"modify failed"), "損切りの変更の失敗", "要確認"),
    (re.compile(r"最大DD到達"), "最大DD で新規停止", "要確認"),
    (re.compile(r"not enough bars"), "足が足りない", "要確認"),
    (re.compile(r"param file not found|unknown param|読めない日時"), "設定の読み込みの問題", "要確認"),
    (re.compile(r"このパラメータは\s*\d+\s*分足用"), "チャートの時間足が違う", "要確認"),
    (re.compile(r"entry\s+(?:-?\d+\s+)?rejected"), "エントリーの見送り", "注意"),
    (re.compile(r"stale bar"), "遅れた足のため見送り", "注意"),
    (re.compile(r"pending entry expired"), "スプレッド待ちのまま見送り", "注意"),
    (re.compile(r"webhook failed"), "通知（webhook）の失敗", "注意"),
    (re.compile(r"spread too wide"), "スプレッドが広く待機", "情報"),
    (re.compile(r"CfdCommodityEA start"), "EA の起動", "情報"),
    (re.compile(r"state restored from history"), "建玉の状態を履歴から復元", "情報"),
]


@dataclass
class EaLog:
    judgments: pd.DataFrame          # chart, tf, bar_date, close, close_dec, signal, ext, ext_period, ext_max, held, file, log_date, time
    events: pd.DataFrame             # chart, tf, log_date, time, kind, severity, text
    files: list[Path] = field(default_factory=list)
    first_date: date | None = None   # ログを書いた日（行の頭の日時、無ければファイル名の日付）の最初・最後
    last_date: date | None = None
    unattributed: list = field(default_factory=list)   # 「の足を判断」があるのに発信元が読めない行（書いた日）
    unparsed: list[str] = field(default_factory=list)  # 「の足を判断」があるのに形が読めない行（最大 5 行）
    undated: list[str] = field(default_factory=list)   # 書いた日が分からないファイル（名前に日付が無く、行にも日時が無い）


def read_text(path: Path) -> str:
    """MT5 のログ（UTF-16 LE、BOM あり・なし）を読む。UTF-8 にも対応。"""
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")   # 書き込みの途中で読んでも止まらない
    if len(raw) >= 4 and raw[1:2] == b"\x00" and raw[3:4] == b"\x00":
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def file_date(path: Path) -> date | None:
    m = re.search(r"(\d{8})", Path(path).stem)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def parse_lines(lines: list[str], fdate: date | None = None, fname: str = "") -> tuple[list[dict], list[dict], list, list[str]]:
    """戻り値: (判断の行, 他の行, 発信元が読めない判断の行の日付, 形が読めない判断の行)。

    書いた日は、行の頭に日時があればそれ、無ければファイル名の日付（fdate）。
    """
    judg, events, unattributed, unparsed = [], [], [], []
    for line in lines:
        if not line.strip():
            continue
        src = _SOURCE.search(line)
        if src is None and _OTHER_SOURCE.search(line):
            continue   # 他の EA・スクリプトの行
        chart, tf = (src.group(1), _TF_ALIASES.get(src.group(2).upper(), src.group(2).upper())) if src else ("", "")
        head = line[: src.start()] if src else line
        st = _STAMP.search(head)
        ldate = date(int(st.group(1)), int(st.group(2)), int(st.group(3))) if st else fdate
        tm = _TIME.search(head) or _TIME.search(line)
        when = tm.group(1) if tm else ""
        if "の足を判断" in line:
            m = _JUDGE.search(line)
            if not m:
                if len(unparsed) < 5:
                    unparsed.append(line.strip())
                continue
            if not src:
                unattributed.append(ldate)
                continue
            close_txt = m.group(4)
            judg.append({
                "chart": chart, "tf": tf,
                "bar_date": date(int(m.group(1)), int(m.group(2)), int(m.group(3))),
                "close": float(close_txt),
                "close_dec": len(close_txt.split(".", 1)[1]) if "." in close_txt else 0,
                "signal": _SIGNALS[m.group(5)],
                "ext": float(m.group(7)) if m.group(7) else np.nan,
                "ext_period": int(m.group(6)) if m.group(6) else 0,
                "ext_max": float(m.group(8)) if m.group(8) else np.nan,
                "held": int(m.group(9)),
                "file": fname, "log_date": ldate, "time": when,
            })
            continue
        if src is None and "CfdCommodityEA" not in line and not any(p.search(line) for p, _, s in ISSUE_PATTERNS
                                                                    if s == "要確認"):
            continue   # 発信元が読めず、要確認の言葉も無い行
        for pat, kind, sev in ISSUE_PATTERNS:
            if pat.search(line):
                text = line.split(src.group(0), 1)[1].strip() if src else line.strip()
                events.append({"chart": chart, "tf": tf, "log_date": ldate, "time": when, "kind": kind,
                               "severity": sev, "text": text})
                break
    return judg, events, unattributed, unparsed


def read_logs(paths: list[Path]) -> EaLog:
    judg, events, unattributed, unparsed, undated = [], [], [], [], []
    dates = []
    for path in sorted(paths, key=lambda p: (file_date(p) or date.min, p.name)):
        d = file_date(path)
        j, e, u, bad = parse_lines(read_text(path).splitlines(), d, path.name)
        got = [r["log_date"] for r in j + e if r["log_date"] is not None] + [x for x in u if x is not None]
        if d:
            dates.append(d)
        dates += got
        if d is None and not got and (j or e or u):
            undated.append(path.name)
        judg += j
        events += e
        unattributed += u
        unparsed += bad[: max(0, 5 - len(unparsed))]
    jcols = ["chart", "tf", "bar_date", "close", "close_dec", "signal", "ext", "ext_period", "ext_max", "held",
             "file", "log_date", "time"]
    ecols = ["chart", "tf", "log_date", "time", "kind", "severity", "text"]
    return EaLog(pd.DataFrame(judg, columns=jcols), pd.DataFrame(events, columns=ecols), sorted(paths),
                 min(dates, default=None), max(dates, default=None), unattributed, unparsed, undated)


# --------------------------------------------------------------------------- Python 側

def server_dates(index: pd.DatetimeIndex, server_tz) -> list[date]:
    """足の始まり（UTC）を MT5 サーバーの日付にする（EA のログの日付と同じ）。"""
    if isinstance(server_tz, (int, float)):
        wall = index + pd.Timedelta(hours=float(server_tz))
    elif server_tz == "ny_close":
        wall = index.tz_convert("America/New_York") + pd.Timedelta(hours=7)
    else:
        wall = index.tz_convert(server_tz)
    return [t.date() for t in wall]


def held_counts(index: pd.DatetimeIndex, bar: pd.Timedelta, trades: pd.DataFrame, symbol: str) -> np.ndarray:
    """各足の確定時点で持っている建玉の数（EA は足の確定後、決済・新規の前に数える）。"""
    out = np.zeros(len(index), dtype=int)
    if trades is None or trades.empty:
        return out
    t = trades[trades["symbol"] == symbol]
    close_time = index + bar
    for _, tr in t.iterrows():
        ent, ext = tr["entry_time"], tr["exit_time"]
        held = close_time > ent
        if pd.notna(ext):
            held &= close_time <= ext
        out += np.asarray(held, dtype=int)
    return out


def python_daily(sig: pd.DataFrame, server_tz, bar: pd.Timedelta, trades: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Python の 1 足ごとの判断（EA のログと同じ項目）。"""
    out = pd.DataFrame({
        "bar_date": server_dates(sig.index, server_tz),
        "py_close": sig["close"].to_numpy(float),
        "py_signal": sig["entry"].to_numpy(int),
        "py_ext": sig["ext_z"].to_numpy(float) if "ext_z" in sig else np.full(len(sig), np.nan),
        "py_held": held_counts(sig.index, bar, trades, symbol),
    }, index=sig.index)
    return out.drop_duplicates("bar_date", keep="last")


# --------------------------------------------------------------------------- 照らし合わせ

def compare_symbol(ea: pd.DataFrame, py: pd.DataFrame, start: date, last_log: date | None,
                   upto: date | None = None) -> pd.DataFrame:
    """1 銘柄について 1 日ずつ照らし合わせる。

    ea: その銘柄の判断の行（read_logs の judgments。同じ足が 2 回あれば最後を使う。空でもよい）
    py: python_daily
    対象: start 以降の足（Python・EA のどちらかにある足）。
    EA の判断が無い足は、次の足がログの最後の日までに始まっていれば「抜け」、そうでなければ「判断待ち」。
    upto: Python の値を比べる最後の足（書き出したときに途中だった足より前）。それより後は「Python のデータに無い」
    """
    dup = ea[ea.duplicated("bar_date", keep=False)]
    e = ea.drop_duplicates("bar_date", keep="last").set_index("bar_date")
    p_all = py.set_index("bar_date")
    next_bar = dict(zip(p_all.index[:-1], p_all.index[1:]))   # 抜けの判定には途中の足も使う（足が始まったことは確か）
    p = p_all[p_all.index <= upto] if upto is not None else p_all
    dates = sorted(d for d in set(p.index) | set(e.index) if d >= start)
    rows = []
    for d in dates:
        r: dict = {"bar_date": d}
        has_ea, has_py = d in e.index, d in p.index
        if has_ea:
            x = e.loc[d]
            r.update(ea_close=x["close"], ea_signal=int(x["signal"]), ea_ext=x["ext"], ea_held=int(x["held"]),
                     close_dec=int(x["close_dec"]), ea_time=f"{x['log_date']} {x['time']}".strip(),
                     twice=bool((dup["bar_date"] == d).any()))
        if has_py:
            y = p.loc[d]
            r.update(py_close=y["py_close"], py_signal=int(y["py_signal"]), py_ext=y["py_ext"],
                     py_held=int(y["py_held"]))
        if not has_py:
            r["status"] = "Python のデータに無い"
        elif not has_ea:
            nb = next_bar.get(d)
            r["status"] = "抜け" if (nb is not None and last_log is not None and nb <= last_log) else "判断待ち"
        else:
            diffs = []
            tol = 0.5 * 10 ** (-r["close_dec"]) + 1e-9 * max(1.0, abs(r["py_close"]))
            if not abs(r["ea_close"] - r["py_close"]) <= tol:
                diffs.append("終値")
            if r["ea_signal"] != r["py_signal"]:
                diffs.append("シグナル")
            ea_ext, py_ext = r["ea_ext"], r["py_ext"]
            if np.isnan(ea_ext) != np.isnan(py_ext) or (not np.isnan(ea_ext) and abs(ea_ext - py_ext) > EXT_TOL):
                diffs.append("行き過ぎ")
            if r["ea_held"] != r["py_held"]:
                diffs.append("保有数")
            r["diffs"] = "・".join(diffs)
            r["status"] = "差あり" if diffs else "一致"
        rows.append(r)
    cols = ["bar_date", "status", "diffs", "ea_close", "py_close", "ea_signal", "py_signal", "ea_ext", "py_ext",
            "ea_held", "py_held", "close_dec", "ea_time", "twice"]
    return pd.DataFrame(rows).reindex(columns=cols)


def shifted_close_match(ea: pd.DataFrame, py: pd.DataFrame) -> int | None:
    """終値が日付を 1 本ずらすと合うか（日付の対応＝server_tz の設定違いの手がかり）。合うずらし方（-1/+1）を返す。"""
    if ea.empty or py.empty:
        return None
    e = ea.drop_duplicates("bar_date", keep="last").set_index("bar_date")["close"]
    p = py.set_index("bar_date")["py_close"]
    pos = {d: i for i, d in enumerate(p.index)}
    best = None
    for k in (-1, 1):
        hit = n = 0
        for d, c in e.items():
            if d in pos and 0 <= pos[d] + k < len(p):
                n += 1
                hit += abs(p.iloc[pos[d] + k] - c) <= 1e-6 * max(1.0, abs(c))
        if n >= 3 and hit / n >= 0.8:
            best = k
    return best


def reasons(sym: str, days: pd.DataFrame, events: pd.DataFrame, shift: int | None,
            rejections: pd.DataFrame | None = None) -> list[str]:
    """差の理由の候補（確かなものではなく、まず見るところ）。"""
    out: list[str] = []
    if days["twice"].fillna(False).astype(bool).any():
        out.append("同じ足を 2 回判断した記録がある → 同じ銘柄のチャートに EA が 2 つある、か再起動で記録が消えた（要確認）")
    diff = days[days["status"] == "差あり"]
    if diff.empty:
        return out
    gaps = set(days.loc[days["status"] == "抜け", "bar_date"])
    after_gap = {d for d in diff["bar_date"] if any(g < d for g in gaps)}
    close_bad = diff[diff["diffs"].str.contains("終値")]
    if len(close_bad):
        if shift is not None:
            out.append(f"終値が 1 本{'前' if shift < 0 else '後'}の足と合う → 日付の対応がずれている"
                       "（config/train.toml の [data] server_tz を確認。フィリップは 9）")
        elif len(close_bad) == len(days[days["status"].isin(["一致", "差あり"])]):
            out.append("終値がすべて違う → 別の銘柄・別のサーバーのデータを比べている可能性"
                       "（symbol_map と、書き出した口座が EA と同じかを確認）")
        else:
            out.append(f"終値の差 {len(close_bad)} 日 → EA が判断した後に足が修正された・書き出しが古い"
                       "（CfdExportBars で書き出し直して ./cfd data）")
    sig_bad = diff[diff["diffs"].str.contains("シグナル")]
    if len(sig_bad):
        near = sig_bad[(sig_bad["py_ext"].abs() - 1.5).abs() < 0.05]
        only_sig = sig_bad[~sig_bad["diffs"].str.contains("終値")]
        if len(near):
            out.append(f"シグナルの差 {len(near)} 日は行き過ぎの値が境目（1.5 倍）の近く → 計算の小さな差で入る・入らないが分かれた")
        if len(only_sig) > len(near):
            out.append("終値が同じなのにシグナルが違う日がある → 過去 55 日の高値・安値のデータの差か、EA と Python の計算の違い"
                       "（./cfd compare でテスターと突き合わせる）")
    ext_bad = diff[diff["diffs"].str.contains("行き過ぎ") & ~diff["diffs"].str.contains("終値")]
    if len(ext_bad):
        if ext_bad["ea_ext"].isna().any():
            out.append("EA が行き過ぎの値を出していない日がある → EA の足の本数が足りない（ea_calc_bars）")
        else:
            out.append(f"行き過ぎの値の差 {len(ext_bad)} 日 → 過去 1 年分の終値のどこかがデータで違う（書き出し直して確認）")
    held_bad = diff[diff["diffs"].str.contains("保有数")]
    if len(held_bad):
        ev_kinds = set(events["kind"]) if len(events) else set()
        if after_gap & set(held_bad["bar_date"]):
            out.append("保有数の差が EA の記録の抜けた日より後にある → Mac が止まっていた間の足を EA は処理しない"
                       "（その間のエントリー・決済が Python とずれる）")
        if ev_kinds & {"エントリーの見送り", "遅れた足のため見送り", "スプレッド待ちのまま見送り"}:
            out.append("EA が見送った行がある（下の「EA の他の行」）→ Python は同じ日に見送っていなければ保有数がずれる")
        if rejections is not None and not rejections.empty:
            out.append("Python 側が見送ったエントリーがある（理由は下）→ 資金（--equity）が口座と違うと数量の見送りが変わる")
        more_ea = held_bad[held_bad["ea_held"] > held_bad["py_held"]]
        more_py = held_bad[held_bad["ea_held"] < held_bad["py_held"]]
        if len(more_ea):
            out.append("EA の方が多く持っている日がある → 手で建てた・同じ銘柄のチャートに EA を 2 つ貼った・"
                       "Python では損切りに掛かった（約定の再現は M5 なので、きわどい損切りは分かれうる）")
        if len(more_py):
            out.append("Python の方が多く持っている日がある → EA が発注できなかった・手で決済した・"
                       "週明けの窓開けで損切りされた時刻の違い")
    return out
