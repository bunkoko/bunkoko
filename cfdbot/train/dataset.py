"""data/ フォルダの CSV を自動で見つけて読み込む。

ファイル名の先頭が MT5 のシンボル名（例: XAGUSD_H1.csv, XTIUSD-H1.csv, xauusd.csv）なら、
config の symbol_map で銘柄キーに変換する。時間足がファイル名に入っていれば、
設定の時間足（既定 H1）と違うファイルは読み飛ばす。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..backtest import infer_timeframe
from ..data import load_mt5_csv
from ..events import ET, TRADING_DAY_ROLL_ET
from .config import TIMEFRAMES, DataConfig

_TF_TOKEN = re.compile(r"^(M\d+|H\d+|D1|W1|MN1)$")


@dataclass
class Dataset:
    prices: dict[str, pd.DataFrame]         # 銘柄キー → OHLC（UTC）
    files: dict[str, Path]                  # 銘柄キー（と "FX"）→ ファイル
    fx: pd.Series | None                    # USD/JPY の終値（無ければ None）
    timeframe: pd.Timedelta
    digest: str                             # データの指紋（キャッシュのキー）
    skipped: list[str] = field(default_factory=list)

    @property
    def symbols(self) -> list[str]:
        return sorted(self.prices)


def _parse_name(path: Path, symbol_map: dict[str, str]) -> tuple[str | None, str | None]:
    tokens = [t for t in re.split(r"[_\-\s.]+", path.stem.upper()) if t]
    tf = next((t for t in tokens if _TF_TOKEN.match(t)), None)
    head = tokens[0] if tokens else ""
    key = symbol_map.get(head)
    if key is None:  # "XAGUSDPRO" のような接尾辞付き
        key = next((v for k, v in symbol_map.items() if head.startswith(k)), None)
    return key, tf


def file_digest(paths: list[Path], extra: str = "") -> str:
    h = hashlib.sha256(extra.encode())
    for p in sorted(paths):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def load_dataset(cfg: DataConfig, base_dir: Path | None = None) -> Dataset:
    root = Path(cfg.dir) if base_dir is None else base_dir / cfg.dir
    if not root.exists():
        raise FileNotFoundError(f"データフォルダが無い: {root}（MT5 から書き出した CSV を置く）")
    want_tf = cfg.timeframe.upper()
    tf_delta = pd.Timedelta(TIMEFRAMES[want_tf])
    files: dict[str, Path] = {}
    skipped: list[str] = []
    for path in sorted(root.glob("*.csv")):
        key, tf = _parse_name(path, cfg.symbol_map)
        if key is None:
            skipped.append(f"{path.name}: 銘柄名が分からない（symbol_map に追加）")
            continue
        if tf is not None and tf != want_tf:
            skipped.append(f"{path.name}: 時間足 {tf} は対象外（{want_tf} を学習）")
            continue
        if key in files:
            skipped.append(f"{path.name}: {key} は {files[key].name} を使用")
            continue
        files[key] = path
    if not [k for k in files if k != "FX"]:
        raise FileNotFoundError(f"{root} に学習できる {want_tf} の CSV が無い")

    prices: dict[str, pd.DataFrame] = {}
    fx = None
    for key, path in files.items():
        df = load_mt5_csv(path, server_tz=cfg.server_tz)
        actual = infer_timeframe(df.index)
        if actual != tf_delta:
            raise ValueError(f"{path.name}: 時間足が {actual}（設定は {want_tf}）")
        if key == "FX":
            fx = df["close"]
        else:
            prices[key] = df
    digest = file_digest(list(files.values()), extra=f"{cfg.server_tz}|{want_tf}")
    return Dataset(prices, files, fx, tf_delta, digest, skipped)


def trading_days(prices: dict[str, pd.DataFrame], timeframe: pd.Timedelta) -> pd.DatetimeIndex:
    """全銘柄の足の終了時刻から取引日（17:00 ET 区切り）の一覧を作る。"""
    keys = []
    for df in prices.values():
        close_et = (df.index + timeframe).tz_convert(ET)
        keys.append((close_et + pd.Timedelta(hours=24 - TRADING_DAY_ROLL_ET)).normalize().tz_localize(None))
    return pd.DatetimeIndex(np.unique(np.concatenate([k.values for k in keys])))
