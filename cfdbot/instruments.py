"""銘柄仕様（最小単位・証拠金率・コストなど）。

数量は「単位」（銀=オンス、原油=バレル）で扱う。MT5 の「ロット」とは
lots = qty / contract_size の関係。EA 側は MT5 のシンボル情報
(SYMBOL_VOLUME_MIN など) を直接読むので、ここの値はバックテスト用。

スプレッド・スリッページ・金利調整額などの値は **仮置き** であり、
デモ口座で実測した値に置き換えること（verified=False の項目）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class Instrument:
    symbol: str                 # このツール内での銘柄キー（例: "WTI"）
    description: str
    unit: str                   # "oz" / "bbl"
    min_qty: float              # 最小取引数量（単位）
    qty_step: float             # 数量刻み（単位）
    max_qty: float              # 1ポジションの上限（証券会社ルール）
    margin_rate: float          # 証拠金率 (0.05 = レバレッジ20倍)
    spread: float               # 平常時スプレッド（価格単位, USD）
    slippage: float             # 成行・逆指値の想定スリッページ（価格単位）
    tick_size: float
    point: float | None = None  # MT5 の point（CSV の <SPREAD> 列の換算用）。None なら tick_size
    stop_level: float = 0.0     # 現在値から逆指値までの最小距離（価格単位）
    financing_long: float = 0.03   # 年率コスト（正=支払い）。金利調整額/キャリングコストの近似
    financing_short: float = 0.03
    cluster: str = "other"      # 相関の高いグループ（"energy" / "metals"）
    event_tags: tuple[str, ...] = ()   # 影響を受ける指標イベントのタグ（"oil", "usd_macro"）
    has_expiry: bool = False    # 先物ベースCFD（限月あり）か
    quote_ccy: str = "USD"
    mt5_symbol: str = ""        # 証券会社の MT5 上のシンボル名（要確認）
    verified: bool = False      # 公式情報・実測で確認済みか
    note: str = ""

    @property
    def point_size(self) -> float:
        return self.point if self.point is not None else self.tick_size

    def round_qty_down(self, qty: float) -> float:
        steps = int(qty / self.qty_step + 1e-9)
        return round(steps * self.qty_step, 10)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["event_tags"] = list(self.event_tags)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Instrument":
        d = dict(d)
        d["event_tags"] = tuple(d.get("event_tags", ()))
        return cls(**d)


_UNVERIFIED = "スプレッド・スリッページ・金利は仮置き。デモ口座で実測して置き換えること"

# フィリップ・キャピタル証券（フィリップMT5）想定。満期なし・金利調整額のみ。
PHILLIP: dict[str, Instrument] = {
    "SILVER": Instrument(
        symbol="SILVER", description="銀 (XAG/USD)", unit="oz",
        min_qty=10, qty_step=10, max_qty=5000, margin_rate=0.05,
        spread=0.030, slippage=0.010, tick_size=0.001,
        cluster="metals", event_tags=("usd_macro",), note=_UNVERIFIED,
    ),
    "GOLD": Instrument(
        symbol="GOLD", description="金 (XAU/USD)", unit="oz",
        min_qty=1, qty_step=1, max_qty=500, margin_rate=0.05,
        spread=0.40, slippage=0.10, tick_size=0.01,
        cluster="metals", event_tags=("usd_macro",),
        note="最小単位は未確認（仮に1oz）。" + _UNVERIFIED,
    ),
    "WTI": Instrument(
        symbol="WTI", description="WTI原油", unit="bbl",
        min_qty=10, qty_step=10, max_qty=5000, margin_rate=0.05,
        spread=0.040, slippage=0.020, tick_size=0.01,
        cluster="energy", event_tags=("oil",), note=_UNVERIFIED,
    ),
    "BRENT": Instrument(
        symbol="BRENT", description="ブレント原油", unit="bbl",
        min_qty=10, qty_step=10, max_qty=5000, margin_rate=0.05,
        spread=0.040, slippage=0.020, tick_size=0.01,
        cluster="energy", event_tags=("oil",), note=_UNVERIFIED,
    ),
}

# サクソバンク証券（OpenAPI）想定。先物ベースで限月あり、最小単位25。
SAXO: dict[str, Instrument] = {
    k: replace(
        v,
        min_qty=25 if k in ("SILVER", "WTI", "BRENT") else v.min_qty,
        qty_step=1 if k in ("SILVER", "WTI", "BRENT") else v.qty_step,
        has_expiry=True,
        note="先物ベース（限月交代あり）。数量刻みは未確認。" + _UNVERIFIED,
    )
    for k, v in PHILLIP.items()
}

BROKERS = {"phillip": PHILLIP, "saxo": SAXO}


def get_instruments(broker: str = "phillip") -> dict[str, Instrument]:
    try:
        return dict(BROKERS[broker])
    except KeyError:
        raise ValueError(f"unknown broker '{broker}' (choose from {list(BROKERS)})") from None


def load_instruments(path: str | Path) -> dict[str, Instrument]:
    """JSON（{symbol: {...}}）から銘柄仕様を読み込む。実測値で上書きしたい場合に使う。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: Instrument.from_dict({"symbol": k, **v}) for k, v in raw.items()}


def save_instruments(instruments: dict[str, Instrument], path: str | Path) -> None:
    data = {}
    for k, inst in instruments.items():
        d = inst.to_dict()
        d.pop("symbol")
        data[k] = d
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
