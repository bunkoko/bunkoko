"""data/ フォルダの CSV を自動で見つけて読み込む（複数の時間足に対応）。

ファイル名の先頭が MT5 のシンボル名（例: XAGUSD_H1.csv, XTIUSD-M5.csv, xauusd_d1.csv）なら、
config の symbol_map で銘柄キーに変換する。同じ銘柄の時間足違いは全部読み込み、用途ごとに使い分ける:

- 売買の足（signal_timeframes）: ファイルが無くても、より細かい足があれば自動で作る（例: M5 → H1）
- 約定の再現（fill_timeframe）: 売買の足より細かい足のファイル（例: H1 で売買し M5 で約定を再現）
- 上位足フィルタ: 上位足のファイル。無ければ売買の足から作る（例: H1 → D1）
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..backtest import infer_timeframe
from ..data import load_mt5_csv, resample_ohlc
from ..events import ET, trading_day_keys
from .config import TIMEFRAMES, DataConfig, tf_minutes, tf_name

_TF_TOKEN = re.compile(r"^(M\d+|H\d+|D1|W1|MN1)$")


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


@dataclass
class Dataset:
    frames: dict[str, dict[str, pd.DataFrame]]   # 銘柄キー → {時間足: OHLC}（ファイルから読んだもの）
    fx: pd.Series | None                         # USD/JPY の終値（無ければ None）
    signal_timeframes: list[str]
    fill_timeframe: str = "auto"
    files: dict[str, Path] = field(default_factory=dict)  # "銘柄@時間足" → ファイル
    digest: str = ""
    skipped: list[str] = field(default_factory=list)
    server_tz: str | float = "ny_close"          # 上位足をサーバー時刻の 0 時起点で作る（MT5 のチャートと同じ足）
    _derived: dict[tuple[str, str], pd.DataFrame] = field(default_factory=dict, repr=False)

    # -- 時間足の取り出し
    def file_timeframes(self, symbol: str) -> list[str]:
        """ファイルがある時間足（細かい順）。"""
        return sorted(self.frames.get(symbol, {}), key=tf_minutes)

    def _derive(self, symbol: str, tf: str) -> pd.DataFrame | None:
        if tf in self.frames.get(symbol, {}):
            return self.frames[symbol][tf]
        finer = [t for t in self.file_timeframes(symbol) if tf_minutes(t) < tf_minutes(tf)
                 and tf_minutes(tf) % tf_minutes(t) == 0]
        if not finer:
            return None
        key = (symbol, tf)
        if key not in self._derived:
            # 細かい中で最も粗い足から作る（計算が軽く、結果は同じ）
            self._derived[key] = resample_ohlc(self.frames[symbol][finer[-1]], TIMEFRAMES[tf], self.server_tz)
        return self._derived[key]

    def signal_frame(self, symbol: str, tf: str) -> pd.DataFrame | None:
        return self._derive(symbol, tf)

    def context_frame(self, symbol: str, tf: str) -> pd.DataFrame | None:
        """上位足フィルタ用。ファイルが無ければ細かい足から作る。"""
        return self._derive(symbol, tf)

    def fine_timeframe(self, symbol: str, tf: str) -> str | None:
        """約定の再現に使う時間足（売買の足より細かいファイル）。"""
        if self.fill_timeframe == "none":
            return None
        finer = [t for t in self.file_timeframes(symbol) if tf_minutes(t) < tf_minutes(tf)]
        if self.fill_timeframe == "auto":
            return finer[0] if finer else None
        return self.fill_timeframe if self.fill_timeframe in finer else None

    def fine_frame(self, symbol: str, tf: str) -> pd.DataFrame | None:
        t = self.fine_timeframe(symbol, tf)
        return self.frames[symbol][t] if t else None

    # -- 一覧
    @property
    def symbols(self) -> list[str]:
        """どれかの売買の足が用意できる銘柄。"""
        return sorted(s for s in self.frames if any(self.signal_frame(s, t) is not None for t in self.signal_timeframes))

    def signal_pairs(self) -> list[tuple[str, str]]:
        return [(s, t) for s in self.symbols for t in self.signal_timeframes if self.signal_frame(s, t) is not None]

    def calendar(self) -> pd.DatetimeIndex:
        """全銘柄・全売買足の足の終了時刻から取引日（17:00 ET 区切り）の一覧を作る。"""
        keys = []
        for s, t in self.signal_pairs():
            df = self.signal_frame(s, t)
            close_et = (df.index + pd.Timedelta(TIMEFRAMES[t])).tz_convert(ET)
            keys.append(trading_day_keys(close_et).values)
        return pd.DatetimeIndex(np.unique(np.concatenate(keys)))


def _read(path: Path, server_tz) -> tuple[pd.DataFrame, str]:
    df = load_mt5_csv(path, server_tz=server_tz)
    return df, tf_name(infer_timeframe(df.index))


def load_dataset(cfg: DataConfig, base_dir: Path | None = None) -> Dataset:
    root = Path(cfg.dir) if base_dir is None else base_dir / cfg.dir
    if not root.exists():
        raise FileNotFoundError(f"データフォルダが無い: {root}（MT5 から書き出した CSV を置く）")
    frames: dict[str, dict[str, pd.DataFrame]] = {}
    files: dict[str, Path] = {}
    fx_frames: dict[str, pd.DataFrame] = {}
    skipped: list[str] = []
    for path in sorted(root.glob("*.csv")):
        key, tf_hint = _parse_name(path, cfg.symbol_map)
        if key is None:
            skipped.append(f"{path.name}: 銘柄名が分からない（symbol_map に追加）")
            continue
        try:
            df, tf = _read(path, cfg.server_tz)
        except ValueError as e:
            skipped.append(f"{path.name}: {e}")
            continue
        if tf_hint and tf_hint in TIMEFRAMES and tf_hint != tf:
            raise ValueError(f"{path.name}: ファイル名は {tf_hint} だが中身は {tf}")
        target = fx_frames if key == "FX" else frames.setdefault(key, {})
        if tf in target:  # 期間を分けて書き出したファイルはつなげる（重なりは後のファイルを優先）
            merged = pd.concat([target[tf], df])
            target[tf] = merged[~merged.index.duplicated(keep="last")].sort_index()
            files[f"{key}@{tf}#{path.name}"] = path
            continue
        target[tf] = df
        files[f"{key}@{tf}"] = path
    fx = None
    if fx_frames:  # 円換算は H1 があれば H1、無ければ最も粗い足
        tf = "H1" if "H1" in fx_frames else max(fx_frames, key=tf_minutes)
        fx = fx_frames[tf]["close"]
    # 足の作り方（サーバー時刻の 0 時起点）を変えたら計算結果のキャッシュも別にする
    ds = Dataset(frames, fx, list(cfg.signal_timeframes), cfg.fill_timeframe, files,
                 file_digest(list(files.values()), extra=f"{cfg.server_tz}|bars=server-midnight"), skipped,
                 server_tz=cfg.server_tz)
    if not ds.symbols:
        raise FileNotFoundError(f"{root} に、売買の足（{cfg.signal_timeframes}）を用意できる CSV が無い")
    return ds


def dataset_from_frames(files_meta: dict[str, dict], loaded: dict[str, pd.DataFrame],
                        signal_timeframes: list[str], fill_timeframe: str,
                        server_tz: str | float = "ny_close") -> Dataset:
    """ワーカー側: 読み込んだ "銘柄@時間足" → DataFrame から Dataset を組み立てる。"""
    frames: dict[str, dict[str, pd.DataFrame]] = {}
    fx = None
    fx_frames = {}
    for key in sorted(loaded):  # 分割ファイル（"銘柄@時間足#ファイル名"）は順につなげる
        df = loaded[key]
        sym, tf = key.split("#")[0].split("@")
        target = fx_frames if sym == "FX" else frames.setdefault(sym, {})
        if tf in target:
            merged = pd.concat([target[tf], df])
            df = merged[~merged.index.duplicated(keep="last")].sort_index()
        target[tf] = df
    if fx_frames:
        tf = "H1" if "H1" in fx_frames else max(fx_frames, key=tf_minutes)
        fx = fx_frames[tf]["close"]
    return Dataset(frames, fx, signal_timeframes, fill_timeframe, server_tz=server_tz)
