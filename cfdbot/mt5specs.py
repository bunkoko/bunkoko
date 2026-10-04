"""MT5 の銘柄仕様・口座情報（スクリプト CfdExportBars が書き出す）を読み、銘柄仕様に反映する。

手で「仕様」画面を見てメモ・換算しなくて済むようにする:
- 最小ロット・刻み・上限 × 契約サイズ → min_qty / qty_step / max_qty（単位: オンス・バレル）
- 1 ロットの必要証拠金 ÷ 1 ロットの建玉金額 → margin_rate
- スワップ（ポイント・口座通貨・年率など方式ごと）→ 年率コスト financing_long / financing_short
- ストップレベル（ポイント）× point → stop_level
"""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

from .instruments import Instrument

SPECS_FILE = "symbol_specs.txt"
ACCOUNT_FILE = "account_info.txt"


def _num(v: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return math.nan


def load_specs(path: str | Path) -> dict[str, dict[str, str]]:
    """タブ区切りの銘柄仕様を {MT5 の銘柄名: {項目: 値}} で返す。"""
    path = Path(path)
    if not path.is_file():
        return {}
    lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    if not lines:
        return {}
    head = lines[0].split("\t")
    out = {}
    for ln in lines[1:]:
        row = dict(zip(head, ln.split("\t")))
        if row.get("symbol"):
            out[row["symbol"]] = row
    return out


def load_account(path: str | Path) -> dict[str, str]:
    path = Path(path)
    if not path.is_file():
        return {}
    out = {}
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def spec_for(key: str, specs: dict[str, dict[str, str]], symbol_map: dict[str, str]) -> dict[str, str] | None:
    """銘柄キー（例: SILVER）に当たる MT5 の銘柄の仕様。名前の後ろに記号が付く業者（XAGUSD.ph など）にも対応。"""
    for name, row in specs.items():
        head = name.upper()
        mapped = symbol_map.get(head) or next((v for k, v in symbol_map.items() if head.startswith(k)), None)
        if mapped == key:
            return row
    return None


def annual_financing(row: dict[str, str], side: str) -> float | None:
    """スワップを年率コスト（正 = 支払い）に換算する。換算できない方式なら None。"""
    swap = _num(row.get(f"swap_{side}"))
    mode = row.get("swap_mode", "")
    bid, ask = _num(row.get("bid")), _num(row.get("ask"))
    price = (bid + ask) / 2 if bid > 0 and ask > 0 else math.nan
    contract = _num(row.get("contract_size"))
    notional_dep = _notional_deposit(row)
    if math.isnan(swap):
        return None
    if mode.endswith("DISABLED"):
        return 0.0
    if mode.endswith("POINTS") and price > 0:
        rate = swap * _num(row.get("point")) * 365 / price
    elif mode.endswith("CURRENCY_DEPOSIT") and notional_dep > 0:
        rate = swap * 365 / notional_dep
    elif (mode.endswith("CURRENCY_PROFIT") or (mode.endswith("CURRENCY_MARGIN")
          and row.get("currency_margin") == row.get("currency_profit"))) and price > 0 and contract > 0:
        rate = swap * 365 / (price * contract)
    elif mode.endswith("INTEREST_CURRENT") or mode.endswith("INTEREST_OPEN"):
        rate = swap / 100
    else:
        return None
    return round(-rate, 6)   # MT5 はマイナスが支払い。こちらはプラスが支払い


def _notional_deposit(row: dict[str, str]) -> float:
    """1 ロットの建玉金額（口座通貨）。tick_value は 1 ロット・1 ティックあたりの口座通貨の損益。"""
    bid, ask = _num(row.get("bid")), _num(row.get("ask"))
    tick_size, tick_value = _num(row.get("tick_size")), _num(row.get("tick_value"))
    if not (bid > 0 and ask > 0 and tick_size > 0 and tick_value > 0):
        return math.nan
    return (bid + ask) / 2 * tick_value / tick_size


def apply_spec(inst: Instrument, row: dict[str, str]) -> tuple[Instrument, list[str]]:
    """MT5 の仕様で銘柄仕様を上書きする。戻り値: (新しい銘柄仕様, 反映できなかった項目のメモ)"""
    notes: list[str] = []
    contract = _num(row.get("contract_size"))
    if not contract > 0:
        return inst, ["契約サイズが読めないので反映しない"]
    vmin, vstep, vmax = (_num(row.get(k)) for k in ("volume_min", "volume_step", "volume_max"))
    point, tick = _num(row.get("point")), _num(row.get("tick_size"))
    stops = _num(row.get("stops_level"))
    changes: dict = {"mt5_symbol": row["symbol"], "quote_ccy": row.get("currency_profit") or inst.quote_ccy}
    if vmin > 0:
        changes["min_qty"] = round(vmin * contract, 10)
    if vstep > 0:
        changes["qty_step"] = round(vstep * contract, 10)
    if vmax > 0:
        changes["max_qty"] = round(vmax * contract, 10)
    if point > 0:
        changes["point"] = point
    if tick > 0:
        changes["tick_size"] = tick
    if stops >= 0 and point > 0:
        changes["stop_level"] = round(stops * point, 10)
    margin, notional = _num(row.get("margin_1lot")), _notional_deposit(row)
    if margin > 0 and notional > 0:
        changes["margin_rate"] = round(margin / notional, 4)
    else:
        notes.append("証拠金率は計算できなかった（市場が開いているときに再実行）")
    for side in ("long", "short"):
        f = annual_financing(row, side)
        if f is None:
            notes.append(f"スワップ（{side}）は方式 {row.get('swap_mode')} のため換算できなかった")
        else:
            changes[f"financing_{side}"] = f
    exp = _num(row.get("expiration"))
    changes["has_expiry"] = bool(exp > 0)
    note = "仕様は MT5 から自動取得" + ("（" + "。".join(notes) + "）" if notes else "") + "。slippage は要実測"
    return replace(inst, note=note, **changes), notes


def describe_spec(inst: Instrument, row: dict[str, str]) -> str:
    lots = _num(row.get("volume_min"))
    return (f"  仕様（MT5 から）: 契約 {inst.min_qty / lots if lots > 0 else math.nan:g}{inst.unit}/ロット、"
            f"最小 {row.get('volume_min')} ロット（{inst.min_qty:g}{inst.unit}）、刻み {inst.qty_step:g}{inst.unit}、"
            f"証拠金率 {inst.margin_rate:.1%}、ストップレベル {inst.stop_level:g}、"
            f"保有コスト 年率 買 {inst.financing_long:+.2%} / 売 {inst.financing_short:+.2%}"
            + ("、限月あり" if inst.has_expiry else ""))


def describe_account(acc: dict[str, str]) -> list[str]:
    if not acc:
        return []
    mode = acc.get("margin_mode", "")
    kind = "ネッティング" if "NETTING" in mode else "ヘッジング" if "HEDGING" in mode else mode
    trade = acc.get("trade_mode", "")
    kind_acc = "デモ" if "DEMO" in trade else "本口座" if "REAL" in trade else trade
    return [f"口座: {acc.get('company', '?')} / サーバー {acc.get('server', '?')} / {kind_acc} / "
            f"通貨 {acc.get('currency', '?')} / レバレッジ 1:{acc.get('leverage', '?')} / {kind}",
            f"MT5: build {acc.get('build', '?')} / チャートの最大バー数 {acc.get('max_bars', '?')}"]
