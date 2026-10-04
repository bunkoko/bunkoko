"""MT5 のフォルダとのファイルのやり取り（Mac の MT5 は Wine の中にあって探しにくいので自動で探す）。

- find_terminals: MT5 のデータフォルダ（MQL5）と共通フォルダ（Common/Files）を探す
- build_presets: 学習結果の ea/*.set に magic・リスク倍率・指標の日時を入れた、そのまま使える .set を作る
  （MQL5 VPS ではファイルが引き継がれないため、EA に必要なものを全部 input に入れる）
- 共通フォルダから書き出したバー・シグナル記録を取り込む
"""

from __future__ import annotations

import os
import re
import shutil
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .events import Event

# 銘柄ごとの magic の下 3 桁。毎月の入れ替えで変わらないように固定する
# （変わると、保有中の建玉を新しい EA が管理しなくなる）
MAGIC_SLOTS = {"WTI": 1, "BRENT": 2, "SILVER": 3, "GOLD": 4}
EVENT_INPUTS = ("ea_events_utc_1", "ea_events_utc_2", "ea_events_utc_3")
EVENT_INPUT_MAX = 254 - len(EVENT_INPUTS[0])  # MT5 の input 文字列は「名前=値」で 255 文字まで
EVENT_FMT = "%Y.%m.%d %H:%M"

_DATA_PATTERNS = (
    "Program Files/*/MQL5",
    "Program Files (x86)/*/MQL5",
    "users/*/AppData/Roaming/MetaQuotes/Terminal/*/MQL5",
    "users/*/Application Data/MetaQuotes/Terminal/*/MQL5",
)
_COMMON_PATTERNS = (
    "users/*/AppData/Roaming/MetaQuotes/Terminal/Common/Files",
    "users/*/Application Data/MetaQuotes/Terminal/Common/Files",
)


@dataclass
class Terminal:
    mql5: Path               # <データフォルダ>/MQL5
    common: Path | None      # <...>/MetaQuotes/Terminal/Common/Files

    @property
    def data_dir(self) -> Path:
        return self.mql5.parent

    def last_used(self) -> float:
        """最後に動いた時刻の目安（ログの更新時刻）。"""
        times = [p.stat().st_mtime for d in (self.data_dir / "logs", self.mql5 / "Logs") if d.is_dir()
                 for p in d.iterdir()]
        return max(times, default=0.0)


def _wine_roots(home: Path) -> list[Path]:
    """Mac・Linux の Wine の C ドライブ（drive_c）。"""
    bases = [home / "Library" / "Application Support",
             home / "Library" / "Application Support" / "CrossOver" / "Bottles"]
    roots = [p / "drive_c" for b in bases if b.is_dir() for p in sorted(b.iterdir()) if (p / "drive_c").is_dir()]
    if (home / ".wine" / "drive_c").is_dir():
        roots.append(home / ".wine" / "drive_c")
    return roots


def find_terminals(home: Path | None = None, appdata: Path | None = None) -> list[Terminal]:
    """見つかった MT5 を、最近使ったものから順に返す。"""
    home = home or Path.home()
    found: list[Terminal] = []
    for root in _wine_roots(home):
        commons = [p for pat in _COMMON_PATTERNS for p in sorted(root.glob(pat)) if p.is_dir()]
        common = commons[0] if commons else None
        for pat in _DATA_PATTERNS:
            for m in sorted(root.glob(pat)):
                if (m / "Experts").is_dir():
                    found.append(Terminal(m, common))
    if appdata is None and os.name == "nt" and os.environ.get("APPDATA"):
        appdata = Path(os.environ["APPDATA"])
    if appdata is not None:
        base = appdata / "MetaQuotes" / "Terminal"
        common = base / "Common" / "Files"
        for m in sorted(base.glob("*/MQL5")):
            if (m / "Experts").is_dir():
                found.append(Terminal(m, common if common.is_dir() else None))
    found.sort(key=lambda t: -t.last_used())
    return found


def choose_terminal(found: list[Terminal], index: int | None = None, mql5: str | None = None,
                    common: str | None = None) -> Terminal:
    if mql5:
        t = Terminal(Path(mql5).expanduser(), Path(common).expanduser() if common else None)
    elif not found:
        raise SystemExit("MT5 が見つからない。MT5 を一度起動してから再実行するか、--mql5 と --common で場所を指定する"
                         "（MT5 → ファイル → データフォルダを開く で分かる）")
    else:
        i = index or 0
        if i >= len(found):
            raise SystemExit(f"--terminal は 0〜{len(found) - 1}")
        t = found[i]
        if common:
            t = Terminal(t.mql5, Path(common).expanduser())
    if not t.mql5.is_dir():
        raise SystemExit(f"フォルダが無い: {t.mql5}")
    return t


# ---------------------------------------------------------------------------
# .set（MT5 の input のプリセット。UTF-16 の key=value）

def read_set(path: str | Path) -> dict[str, str]:
    raw = Path(path).read_bytes()
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def write_set(params: dict[str, str], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in params.items()]
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-16"))  # BOM 付き UTF-16LE
    return path


def magic_for(symbol: str, base: int = 2609000) -> int:
    slot = MAGIC_SLOTS.get(symbol.upper())
    if slot is None:
        slot = 10 + zlib.crc32(symbol.upper().encode()) % 900
    return base + slot


def inline_events(events: list[Event], symbol: str, tags: list[str], start: pd.Timestamp,
                  end: pd.Timestamp) -> tuple[dict[str, str], int, pd.Timestamp | None]:
    """その銘柄に関係する指標の日時を ea_events_utc_1〜3 に詰める。

    戻り値: (input の辞書, 入れた件数, 入りきらなかったときの最後の日時（None なら全部入った）)
    """
    wanted = {t.strip() for t in tags if t.strip()} | {symbol}
    times = sorted({pd.Timestamp(e.time).tz_convert("UTC") for e in events
                    if e.tag in wanted and start <= pd.Timestamp(e.time) <= end})
    out = {k: "" for k in EVENT_INPUTS}
    used = 0
    for key in EVENT_INPUTS:
        parts: list[str] = []
        while used < len(times):
            s = times[used].strftime(EVENT_FMT)
            if len(",".join(parts + [s])) > EVENT_INPUT_MAX:
                break
            parts.append(s)
            used += 1
        out[key] = ",".join(parts)
    cut = times[used - 1] if used < len(times) and used > 0 else None
    return out, used, cut


def ea_server_tz_inputs(server_tz: str | float) -> dict[str, str]:
    """[data] server_tz を EA の入力に変換する（EA の ENUM_SERVER_TZ: 0 = NY クローズ方式、1 = 固定の時差）。"""
    if isinstance(server_tz, (int, float)):
        return {"ea_server_tz": "1", "ea_server_offset": f"{float(server_tz):g}"}
    if server_tz == "ny_close":
        return {"ea_server_tz": "0"}
    raise ValueError(f"server_tz = {server_tz!r} は EA に渡せない（\"ny_close\" か数値。日本時間なら 9）")


def _mt5_date(s: str) -> str:
    return pd.Timestamp(s).strftime("%Y.%m.%d") if s else ""


@dataclass
class Preset:
    name: str                 # 例: cfdbot_SILVER_squeeze_H1
    symbol: str
    timeframe: str
    params: dict[str, str]
    events: int = 0
    events_cut: pd.Timestamp | None = None
    notes: list[str] = field(default_factory=list)


_SET_NAME = re.compile(r"^cfdbot_([A-Za-z0-9]+)_[a-z]+_([A-Z]+\d+)$")


def build_presets(ea_dir: str | Path, events: list[Event], start: pd.Timestamp, end: pd.Timestamp,
                  magic_base: int = 2609000, risk_scale: float = 1.0, peak_since: str = "",
                  server_tz: str | float = "ny_close") -> list[Preset]:
    tz_inputs = ea_server_tz_inputs(server_tz)
    out = []
    for path in sorted(Path(ea_dir).glob("cfdbot_*.set")):
        m = _SET_NAME.match(path.stem)
        if not m:
            continue
        symbol, tf = m.group(1).upper(), m.group(2)
        params = read_set(path)
        tags = params.get("ft_event_tags", "").split(",")
        ev, n, cut = inline_events(events, symbol, tags, start, end)
        params.update({
            "ea_magic": str(magic_for(symbol, magic_base)),
            "ea_risk_scale": f"{risk_scale:g}",
            "ea_param_file": "",          # VPS ではファイルを読めないので input だけで動かす
            "ea_peak_since": _mt5_date(peak_since),  # 空 = 口座の最初から。DD 停止から再開した日
            "ea_log_signals": "false",
            **tz_inputs,
            **ev,
        })
        out.append(Preset(path.stem, symbol, tf, params, n, cut))
    return out


def install(term: Terminal, presets: list[Preset], repo: Path, events_csv: Path | None) -> list[str]:
    """EA・スクリプト・プリセット・指標カレンダーを MT5 のフォルダに置く。置いた場所の一覧を返す。"""
    done = []
    for src, sub in ((repo / "mql5" / "Experts" / "CfdCommodityEA.mq5", "Experts"),
                     (repo / "mql5" / "Scripts" / "CfdExportBars.mq5", "Scripts")):
        dst = term.mql5 / sub / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        done.append(str(dst))
    for p in presets:
        done.append(str(write_set(p.params, term.mql5 / "Presets" / f"{p.name}.set")))
        tester = {**p.params, "ea_log_signals": "true", "ea_risk_scale": "1"}
        done.append(str(write_set(tester, term.mql5 / "Profiles" / "Tester" / f"{p.name}.set")))
    if events_csv is not None and term.common is not None:
        dst = term.common / "cfdbot_events.csv"
        shutil.copy2(events_csv, dst)
        done.append(str(dst))
    return done


def fetch(src_dir: Path, pattern: str, dest: Path) -> list[Path]:
    """共通フォルダなどから pattern に合うファイルを dest にコピーする（同名は上書き）。"""
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(src_dir.glob(pattern)):
        if p.is_file():
            shutil.copy2(p, dest / p.name)
            out.append(dest / p.name)
    return out
