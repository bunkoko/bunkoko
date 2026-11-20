"""束ねる検証の、柱ごとの月の系列を作って data/bundle/series/ に書く（docs/bundle.md 2.2・3 章の 1）。

元の道具は「系列を書き出すため」に動かすだけで、判定はやり直さない（道具が出す判定の表は、画面に出さずに記録のファイルへ流す）。
成績の数字は画面に出さない。作れなかった柱は理由を返して、ほかの柱は続ける。

最後まで終わったときだけ「終わった印」（cfdbot.bundle.SEAL）を書く。途中で止まった・書き出しの間に元のデータが書き換わった
（毎日の自動実行と重なった）ときは印を書かないので、判定には進めない。道具が自分でデータを取り直す道には入らない。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from . import bundle as B
from .wide import base

PYTHON = sys.executable                       # 元の道具を動かす Python（今動いているものと同じ）


class ExportError(Exception):
    """系列を作れなかった（理由を日本語で）。ほかの柱の書き出しは続ける。"""


# --------------------------------------------------------------------------- そろえる・書く
def write_series(folder, key: str, monthly_d: pd.Series) -> None:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"month": [f"{x:%Y-%m-%d}" for x in monthly_d.index], "d": monthly_d.to_numpy()}).to_csv(folder / f"{key}.csv", index=False)


def monthly(d: pd.Series, last_day=None) -> pd.Series:
    """月ごとの系列を、月末の日付にそろえる。last_day（データの最後の日）を渡すと、その日の属する月から後は落とす（終わっていない月）。"""
    d = pd.Series(d).dropna()
    idx = pd.DatetimeIndex(pd.to_datetime(d.index)).map(base.month_end)
    if idx.has_duplicates:
        raise ExportError("同じ月に値が 2 つある（月ごとの系列のはず）")
    out = pd.Series(d.to_numpy(dtype=float), index=idx).sort_index()
    return out if last_day is None else base.finished_months(out, last_day)


def _days(index) -> pd.DatetimeIndex:
    """道具が書いた日付（時刻の帯つきのことがある）を、帯なしの日付に。"""
    return pd.DatetimeIndex(pd.to_datetime(index, utc=True)).tz_convert(None).normalize()


def read_daily(path: Path, column: str) -> pd.Series:
    if not Path(path).exists():
        raise ExportError(f"{Path(path).name} が無い")
    df = pd.read_csv(path, index_col=0)
    if column not in df.columns:
        raise ExportError(f"{Path(path).name} に列 {column} が無い")
    s = pd.Series(pd.to_numeric(df[column], errors="coerce").to_numpy(), index=_days(df.index)).dropna()
    return s[~s.index.duplicated(keep="last")].sort_index()


def read_d(folder: Path, key: str) -> pd.Series:
    """株の道具の d_<記号>.csv（1 列目が日付、列 d）。"""
    return read_daily(Path(folder) / f"d_{key}.csv", "d")


# --------------------------------------------------------------------------- 道具の出力を拾う
def pick_trend(folder) -> dict[str, pd.Series]:
    """./cfd trend --variants base: 期間A（Yahoo の先物）と期間B（フィリップ）の基準（base）をつなぐ。"""
    a = read_daily(Path(folder) / "returns_A.csv", "base")
    if a.empty:
        raise ExportError("期間A（Yahoo の先物）の成績が空（期間B だけの短い系列にはしない）")
    if not (Path(folder) / "returns_B.csv").exists():
        raise ExportError("期間B（フィリップのデータ）の成績が無い。data/ にフィリップの日足があるかを確かめる")
    b = read_daily(Path(folder) / "returns_B.csv", "base")
    return {"EA": B.to_monthly(pd.concat([a, b[b.index > a.index[-1]]]))}


def pick_carry(folder) -> dict[str, pd.Series]:
    return {k: B.to_monthly(read_daily(Path(folder) / f"{k}_cfd.csv", "ret")) for k in ("carry", "combo")}


def pick_crypto(folder) -> dict[str, pd.Series]:
    return {"C2": B.to_monthly(read_daily(Path(folder) / "returns_crypto_gmo.csv", "ret"))}


def pick_stocks(folder) -> dict[str, pd.Series]:
    return {k: monthly(read_d(folder, k)) for k in ("C3", "C1", "S1")}


def pick_shortterm(folder, last_day) -> dict[str, pd.Series]:
    return {k: monthly(read_d(folder, k), last_day) for k in ("M2", "M1")}


def pick_agents(folder) -> dict[str, pd.Series]:
    """./cfd agents series: 日ごとの d_<記号>.csv。作れなかったものはファイルが無い。"""
    return {k: B.to_monthly(read_d(folder, k)) for k in ("M2a", "M2b", "E1", "E2", "J7") if (Path(folder) / f"d_{k}.csv").exists()}


def pick_text(folder, last_day) -> dict[str, pd.Series]:
    return {"1C": monthly(read_d(folder, "1C"), last_day)}


# --------------------------------------------------------------------------- 道具を動かす
def run_tool(root, script: str, args: list[str], out, log_path) -> Path:
    """scripts/<script> を --out <out> で動かし、新しくできた出力のフォルダを返す。画面には何も出さない（記録は log_path）。"""
    root, out, log_path = Path(root), Path(out), Path(log_path)
    out.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in out.iterdir()}
    with open(log_path, "w", encoding="utf-8") as f:
        done = subprocess.run([PYTHON, str(root / "scripts" / script), *args, "--out", str(out)], cwd=root, stdout=f, stderr=subprocess.STDOUT, check=False)
    if done.returncode != 0:
        said = [x.strip() for x in log_path.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
        raise ExportError(f"{script} が途中で止まった（道具の最後の行: {said[-1][:200] if said else '何も言わなかった'}）")
    dirs = sorted(p for p in out.iterdir() if p.is_dir())
    new = [p for p in dirs if p.name not in before]
    if new:
        return new[-1]
    if dirs:                                   # 同じ分にもう一度動かすと、同じ名前（日時）のフォルダに書く。いちばん新しいものを拾う
        return max(dirs, key=lambda p: p.stat().st_mtime)
    raise ExportError(f"{script} の出力のフォルダが見つからない（記録: {log_path}）")


@dataclass
class Ctx:
    root: Path                                 # リポジトリの根
    work: Path                                 # 道具の出力と記録の置き場（data/bundle/export）
    log: Callable[[str], None]

    def tool(self, name: str, script: str, args: list[str] | None = None) -> Path:
        shutil.rmtree(self.work / name, ignore_errors=True)      # 前の書き出しの出力は消す（今回の道具が書かなかったときに、古い数字を拾わない）
        return run_tool(self.root, script, args or [], self.work / name, self.work / f"{name}.log")


def jp_last_day(root: Path) -> pd.Timestamp:
    """日本株の値段の表の最後の日（月が終わっているかを決めるため）。"""
    path = Path(root) / "data" / "stocks" / "jp_close.csv"
    if not path.exists():
        raise ExportError(f"{path} が無い")
    return pd.Timestamp(_days(pd.read_csv(path, usecols=[0]).iloc[:, 0])[-1])


def _w7(ctx: Ctx) -> dict[str, pd.Series]:
    from .wide import w7

    try:
        return {"W7": monthly(w7.compute(ctx.root / "data").d)}
    except base.NotReady as e:
        raise ExportError(str(e)) from e


def _shortterm(ctx: Ctx) -> dict[str, pd.Series]:
    last = jp_last_day(ctx.root)               # 道具を動かす前に読む（動いている間に値段の表が新しくなっても、終わっていない月を古い側で決める）
    return pick_shortterm(ctx.tool("shortterm", "stocks_short_study.py"), last)


def _edinet(ctx: Ctx) -> dict[str, pd.Series]:
    last = jp_last_day(ctx.root)
    return pick_text(ctx.tool("edinet", "edinet.py", ["series"]), last)


SOURCES: dict[str, Callable[[Ctx], dict[str, pd.Series]]] = {
    "trend": lambda c: pick_trend(c.tool("trend", "trend_study.py", ["--variants", "base"])),
    "crypto": lambda c: pick_crypto(c.tool("crypto", "crypto_study.py")),
    "carry": lambda c: pick_carry(c.tool("carry", "carry_study.py")),
    "stocks": lambda c: pick_stocks(c.tool("stocks", "stocks_study.py")),
    "shortterm": _shortterm,
    "agents": lambda c: pick_agents(c.tool("agents", "agents.py", ["series"])),
    "wide": _w7,
    "edinet": _edinet,
}
MAKES: dict[str, tuple[str, ...]] = {          # どの道具が、登録のどの系列を作るか（16 本を 1 回ずつ）
    "trend": ("EA",), "crypto": ("C2",), "carry": ("carry", "combo"), "stocks": ("C3", "C1", "S1"), "shortterm": ("M2", "M1"),
    "agents": ("M2a", "M2b", "E1", "E2", "J7"), "wide": ("W7",), "edinet": ("1C",),
}
NEEDS: dict[str, tuple[str, ...]] = {          # 無いと、道具が自分で取り直しに行く・作れないファイル。無ければ道具を動かさない
    "trend": ("data/universe/GOLD.csv", "data/universe/SILVER.csv", "data/universe/WTI.csv", "data/universe/BRENT.csv"),
    "crypto": ("data/universe/BTC.csv", "data/universe/ETH.csv"),
    "carry": ("data/carry/wti_curve.csv",),
    "stocks": ("data/stocks/us_close.csv", "data/stocks/jp_close.csv", "data/stocks/jp_value.csv"),
    "shortterm": ("data/stocks/jp_close.csv", "data/stocks/jp_open.csv"),
    "agents": ("data/stocks/jp_close.csv", "data/stocks/jp_open.csv"),
    "edinet": ("data/stocks/jp_close.csv",),
}
QUIET = ("agents", "edinet")                   # 系列だけを書く道具（記録に成績の数字が出ない）。作れなかった理由の行を、記録から拾ってよい
WATCH = ("data/stocks/*.csv", "data/agents/*.csv", "data/agents/ir/disclosures.csv", "data/universe/*.csv", "data/carry/*.csv")
# ↑ 書き出しの間に書き換わっていないかを見る元データ（毎日の自動実行が書くもの）


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """元データのファイルの大きさと更新の時刻。"""
    out = {}
    for pattern in WATCH:
        for p in sorted(Path(root).glob(pattern)):
            st = p.stat()
            out[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


def skipped_why(log_path: Path, key: str) -> str | None:
    """系列だけを書く道具が、その系列を「作れない」と言った行（無ければ None）。"""
    if not Path(log_path).exists():
        return None
    for line in Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines():
        head, _, _ = line.partition(":")
        if "作れない" in line and key in head.strip().split("・"):
            return line.strip()[:200]
    return None


# --------------------------------------------------------------------------- まとめて書き出す
def export(root, sources: dict | None = None, log: Callable[[str], None] = print, made: str = "",
           needs: dict | None = None) -> tuple[dict[str, str], list[str]]:
    """柱ごとの系列を作って書く。返すのは、登録の 16 本それぞれの様子（成績の数字は入れない）と、書き出しの間に書き換わった元データ。

    最後まで終わり、元データが書き換わっていなければ、終わった印を書く（印が無ければ、点検も判定も進まない）。
    """
    root = Path(root)
    sources, needs = SOURCES if sources is None else sources, NEEDS if needs is None else needs
    folder = root / B.SERIES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / B.SEAL).unlink(missing_ok=True)  # 前の回の印と系列は消す（途中で止まった回・作れなかった柱に、古いものを混ぜない）
    for k in B.SERIES_NAMES:
        (folder / f"{k}.csv").unlink(missing_ok=True)
    ctx = Ctx(root=root, work=root / B.DIR / "export", log=log)
    before = snapshot(root)
    status: dict[str, str] = {}
    for name, make in sources.items():
        keys = MAKES.get(name, ())
        lacking = [p for p in needs.get(name, ()) if not (root / p).exists()]
        if lacking:
            status.update({k: f"作れなかった: {'・'.join(lacking)} が無い（道具は動かしていない。書き出しではデータを取り直さない）" for k in keys})
            continue
        log(f"{name}: 系列を作る…")
        try:
            got = make(ctx)
        except ExportError as e:
            status.update({k: f"作れなかった: {e}" for k in keys})
            continue
        except Exception as e:  # noqa: BLE001  1 つの柱で落ちても、ほかは続ける
            status.update({k: f"作れなかった（{type(e).__name__}: {str(e)[:160]}）" for k in keys})
            continue
        for key in keys:
            s = got.get(key)
            if s is None:
                why = skipped_why(ctx.work / f"{name}.log", key) if name in QUIET else None
                status[key] = f"作れなかった: {why or f'道具（{name}）がこの系列を書かなかった'}"
            elif s.empty:
                status[key] = "作れなかった: 値が 1 つも無い"
            else:
                write_series(folder, key, s)
                status[key] = f"書いた: {s.index[0]:%Y-%m}〜{s.index[-1]:%Y-%m}（{len(s)} か月）"
    status = {k: status.get(k, "作れなかった: 作る道具を動かしていない") for k in B.SERIES_NAMES}
    after = snapshot(root)
    changed = [k for k, v in before.items() if after.get(k) != v]
    if not changed:
        B.seal(folder, status, made)
    return status, changed
