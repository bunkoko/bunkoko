"""data/ に置いた CSV の点検（MT5 から書き出した直後に使う）。

- 読み込めたファイル・期間・本数、M5 がどこまで遡れるか
- サーバー時刻の方式が合っているか（週の始まりが日曜夕方・毎日の休止が 17 時台か。米東部時間）
- 平日の大きな欠け
- 実際のスプレッド（M5 のスプレッド列から。時間帯ごとの広がりも）→ 銘柄仕様ファイルに書き出せる
- MT5 の銘柄仕様・口座情報（CfdExportBars が書き出した symbol_specs.txt / account_info.txt）があれば反映
- 時間足ごとのコスト
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

from ..events import ET
from ..instruments import Instrument, get_instruments, load_instruments
from ..mt5specs import (ACCOUNT_FILE, SPECS_FILE, apply_spec, describe_account, describe_spec, load_account,
                        load_specs, spec_for)
from .config import TIMEFRAMES, TrainConfig, tf_minutes
from .costs import timeframe_costs
from .dataset import Dataset, load_dataset


@dataclass
class CheckResult:
    lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    measured: dict[str, Instrument] = field(default_factory=dict)
    costs: pd.DataFrame | None = None

    def info(self, msg: str) -> None:
        self.lines.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        self.lines.append("⚠ " + msg)


def _week_starts(df: pd.DataFrame) -> pd.DatetimeIndex:
    gaps = df.index.to_series().diff() > pd.Timedelta(hours=24)
    return df.index[gaps.to_numpy()].tz_convert(ET)


# 取引所ごとの時間（米東部時間）: (日曜の再開の時, 毎日の休止の始まりの時, 休止の長さ)
# CME（金・銀・WTI）は日曜 18 時再開・毎日 17〜18 時休止。ICE ブレントは日曜 20 時再開・毎日 18〜20 時休止
SESSIONS = {"BRENT": (20, 18, 2)}
DEFAULT_SESSION = (18, 17, 1)
WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _wrap(hours: int, period: int) -> int:
    """時間差を -period/2 〜 period/2 に収める（24 時間・1 週間の周期）。"""
    return (hours + period // 2) % period - period // 2


def _fix_hint(shift: int, server_tz) -> str:
    """表示が shift 時間早い（正）/遅い（負）ときの直し方。"""
    way = f"{abs(shift)} 時間{'早く' if shift > 0 else '遅く'}読まれている"
    if isinstance(server_tz, (int, float)):
        return (f"{way}。[data] server_tz を {server_tz - shift:g} にして再点検する"
                "（夏と冬で 1 時間ずれるなら \"ny_close\" が正しい）")
    if server_tz == "ny_close":  # 期間の大半を占める夏時間では UTC+3 と同じ
        return (f"{way}。[data] server_tz を {3 - shift:g} にして再点検する"
                "（日本の業者は 9 = 日本時間のことが多い）")
    return f"{way}。[data] server_tz を数値（UTC からの時差。例: 2 や 3、日本時間なら 9）にして再点検する"


def check_time(sym: str, tf: str, df: pd.DataFrame, res: CheckResult, server_tz="ny_close") -> None:
    open_h, break_h, _ = SESSIONS.get(sym, DEFAULT_SESSION)
    starts = _week_starts(df)
    if len(starts) >= 4:
        # 再開は日曜 open_h 時 ET（足の時刻の付け方で 1 時間前になる業者もある）
        ok = ((starts.weekday == 6) & (starts.hour >= open_h - 1) & (starts.hour <= open_h)).mean()
        weekly = pd.Series(starts.weekday * 24 + starts.hour)
        mode = int(weekly.mode().iloc[0])
        common = f"{WEEKDAYS_JA[mode // 24]} {mode % 24}時"
        if ok >= 0.8:
            res.info(f"  時刻: 週の始まりは米東部時間の日曜 {open_h} 時前後（最多 {common}）→ OK")
        else:
            hint = _fix_hint(_wrap(6 * 24 + open_h - mode, 168), server_tz)
            res.warn(f"{sym} {tf}: 週の始まりが米東部時間の日曜 {open_h} 時前後になっていない（最多 {common}）。{hint}")
    if tf_minutes(tf) <= 60:
        et = df.index.tz_convert(ET)
        weekday = (et.weekday <= 3)  # 月〜木
        counts = pd.Series(et.hour[weekday]).value_counts().reindex(range(24), fill_value=0)
        quiet = int(counts.idxmin())
        if counts.min() < counts.median() * 0.5:
            if quiet == break_h:
                res.info(f"  時刻: 毎日の休止は米東部時間 {break_h} 時台 → OK")
            else:
                res.warn(f"{sym} {tf}: 毎日の休止が米東部時間 {quiet} 時台にある（{break_h} 時台のはず）。"
                         + _fix_hint(_wrap(break_h - quiet, 24), server_tz))


def check_gaps(sym: str, tf: str, df: pd.DataFrame, res: CheckResult) -> None:
    _, break_h, length = SESSIONS.get(sym, DEFAULT_SESSION)
    step = pd.Timedelta(TIMEFRAMES[tf])
    diff = df.index.to_series().diff()
    et = df.index.tz_convert(ET)
    big = (diff > max(step * 3, pd.Timedelta(hours=2))) & (diff < pd.Timedelta(hours=24))
    # 毎日の休止明けの足は除く（夏冬の切り替えのずれも見込んで 2 時間広めに）
    after_break = (et.hour >= break_h) & (et.hour <= break_h + length + 2)
    big &= ~pd.Series(after_break, index=df.index)
    n = int(big.sum())
    if n > 10:
        worst = diff[big].sort_values(ascending=False).head(3)
        ex = ", ".join(f"{t.tz_convert(ET):%Y-%m-%d %H:%M}（{d}）" for t, d in worst.items())
        res.warn(f"{sym} {tf}: 平日に 2 時間以上の欠けが {n} か所（例: {ex}）。祝日・早仕舞いなら問題ない")


def check_spread(sym: str, tf: str, df: pd.DataFrame, inst: Instrument, res: CheckResult,
                 point_from_mt5: bool = False) -> float | None:
    if "spread" not in df:
        res.warn(f"{sym} {tf}: スプレッド列が無い。デモ口座で実測して銘柄仕様に入れる")
        return None
    sp = df["spread"].to_numpy(float) * inst.point_size
    if np.nanmax(sp) <= 0:
        res.warn(f"{sym} {tf}: スプレッド列が全部 0（記録されていない）。デモ口座で実測する")
        return None
    et_hour = df.index.tz_convert(ET).hour
    by_hour = pd.Series(sp, index=et_hour).groupby(level=0).median()
    med, p90 = float(np.median(sp)), float(np.percentile(sp, 90))
    wide = by_hour.sort_values(ascending=False).head(3)
    jst = lambda h: (h + 13) % 24  # noqa: E731  夏時間の目安（冬は +14）
    wide_txt = "、".join(f"米東部 {h} 時台（日本 {jst(h)} 時台）{v:.4g}" for h, v in wide.items())
    res.info(f"  スプレッド: 中央値 {med:.4g} / 上位10% {p90:.4g}（銘柄仕様の値 {inst.spread:.4g}）")
    res.info(f"  広がる時間帯: {wide_txt}")
    if not point_from_mt5 and (med > inst.spread * 1.5 or med < inst.spread * 0.5):
        res.warn(f"{sym}: 実測スプレッド {med:.4g} が銘柄仕様 {inst.spread:.4g} と大きく違う。"
                 "point（桁数）が MT5 の仕様と合っているかも確認する")
    return med


def run_check(cfg: TrainConfig) -> CheckResult:
    res = CheckResult()
    ds: Dataset = load_dataset(cfg.data)
    inst = get_instruments(cfg.data.broker)
    if cfg.data.instruments:
        inst.update(load_instruments(cfg.data.instruments))
    root = Path(cfg.data.dir)
    specs = load_specs(root / SPECS_FILE)
    for line in describe_account(load_account(root / ACCOUNT_FILE)):
        res.info(line)
    for s in ds.skipped:
        res.warn(f"読み飛ばし: {s}")
    res.info("円換算: " + ("USDJPY のデータを使う" if ds.fx is not None else f"固定 {cfg.data.fx} 円（USDJPY を置くと実レート）"))
    for sym in sorted(ds.frames):
        if sym not in inst:
            res.warn(f"{sym}: 銘柄仕様が無い（config の instruments に追加）")
            continue
        res.info(f"\n■ {sym}")
        row = spec_for(sym, specs, cfg.data.symbol_map) if specs else None
        if row is not None:
            inst[sym], notes = apply_spec(inst[sym], row)
            res.info(describe_spec(inst[sym], row))
            for n in notes:
                res.warn(f"{sym}: {n}")
            res.measured[sym] = inst[sym]
        elif specs:
            res.warn(f"{sym}: {SPECS_FILE} にこの銘柄の仕様が無い（CfdExportBars の symbols を確認）")
        for tf in ds.file_timeframes(sym):
            df = ds.frames[sym][tf]
            res.info(f"  {tf}: {df.index[0]:%Y-%m-%d} 〜 {df.index[-1]:%Y-%m-%d}（{len(df):,} 本）")
        finest = ds.file_timeframes(sym)[0]
        check_time(sym, finest, ds.frames[sym][finest], res, cfg.data.server_tz)
        check_gaps(sym, finest, ds.frames[sym][finest], res)
        for tf in cfg.data.signal_timeframes:
            frame = ds.signal_frame(sym, tf)
            if frame is None:
                res.warn(f"{sym}: 売買の足 {tf} を用意できない（{tf} かそれより細かい足を置く）")
                continue
            fine_tf = ds.fine_timeframe(sym, tf)
            src = "ファイル" if tf in ds.frames[sym] else "細かい足から作成"
            if fine_tf:
                fine = ds.frames[sym][fine_tf]
                cover = "" if fine.index[0] <= frame.index[0] + pd.Timedelta(days=7) else \
                    f"（{fine_tf} は {fine.index[0]:%Y-%m-%d} から。それ以前は {tf} の高値・安値で推定）"
                res.info(f"  売買 {tf}（{src}）/ 約定の再現 {fine_tf}{cover}")
            else:
                res.warn(f"{sym}: 売買 {tf} の約定を再現する細かい足（{cfg.data.fill_timeframe}）が無い")
        med = check_spread(sym, finest, ds.frames[sym][finest], inst[sym], res, point_from_mt5=row is not None)
        if med is not None:
            base_note = inst[sym].note if row is not None else "slippage・金利は要実測"
            res.measured[sym] = replace(inst[sym], spread=round(med, 6),
                                        note=f"spread は {finest} のスプレッド列の中央値。{base_note}")
    res.costs = timeframe_costs(ds, inst)
    return res


def write_measured(res: CheckResult, base: dict[str, Instrument], path: str | Path) -> Path:
    merged = {**base, **res.measured}
    data = {}
    for k, v in merged.items():
        d = v.to_dict()
        d.pop("symbol")
        data[k] = d
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return Path(path)
