"""暗号資産の少額の本番（docs/crypto.md 8 章）。GMOコインの非公開 API で、記録だけの売買と同じ向きに建玉をそろえる。

毎朝（記録の直後）に 1 回: 記録の向き（買い・売り・なし）と口座の建玉を比べ、違えば成行で閉じて建て直す。
既定は空運転（注文を出さず、出すはずの注文を記録する）。--real で本物。data/crypto/live/STOP があれば何もしない。
API のキーは ~/.bunkoko/gmo_api.json（このリポジトリの外）に置く。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .crypto import SYMBOLS, Decision, PaperStore, decide, frame

PRIVATE = "https://api.coin.z.com/private"
PUBLIC = "https://api.coin.z.com/public"
KEY_FILE = Path.home() / ".bunkoko" / "gmo_api.json"
LOG_COLUMNS = ["time", "mode", "symbol", "action", "side", "size", "target", "held", "order_id", "note"]
MAX_ORDERS_PER_RUN = 4           # 1 回の実行で出す注文の上限（2 銘柄 × 閉じる＋建てる）
DEFAULT_CONFIG = {
    "equity_jpy": 1_000_000,         # 量を決める資金（口座の残高ではなく、この戦略に割り当てた額）。BTC の最小 0.01 を 1% の損失で持つには 60 万円以上要る
    "max_notional_jpy": 2_000_000,   # 1 銘柄の名目の上限（資金の 2 倍）
    "symbols": ["BTC_JPY", "ETH_JPY"],
    "max_drawdown_stop": 0.20,       # 記録上の資産がこの割合を下回ったら STOP を置く（手で外すまで何もしない）
}


# --------------------------------------------------------------------------- API
def sign(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    """GMOコインの署名: HMAC-SHA256(秘密鍵, タイムスタンプ + メソッド + パス + 本文)。"""
    return hmac.new(secret.encode(), (timestamp + method + path + body).encode(), hashlib.sha256).hexdigest()


def load_keys(path: Path = KEY_FILE) -> dict:
    if not path.exists():
        raise SystemExit(f"API のキーが無い: {path} に api_key と api_secret を書いた JSON を置く（権限はこのファイルだけ 600）")
    d = json.loads(path.read_text(encoding="utf-8"))
    if not d.get("api_key") or not d.get("api_secret"):
        raise SystemExit(f"{path} に api_key と api_secret が要る")
    return d


def http_json(method: str, url: str, headers: dict, body: str | None = None, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, data=body.encode() if body else None, method=method,
                                 headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"status": e.code, "messages": [{"message_string": e.read().decode("utf-8", errors="replace")[:200]}]}


class GmoClient:
    """非公開 API の薄い包み。request は差し替え可能（テスト・空運転）。"""

    def __init__(self, api_key: str, api_secret: str, request: Callable = http_json, clock: Callable[[], float] = time.time):
        self.key, self.secret, self.request, self.clock = api_key, api_secret, request, clock

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        ts = str(int(self.clock() * 1000))
        text = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = {"API-KEY": self.key, "API-TIMESTAMP": ts, "API-SIGN": sign(self.secret, ts, method, path, text)}
        url = PRIVATE + path
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        d = self.request(method, url, headers, text or None)
        if d.get("status") != 0:
            raise RuntimeError(f"GMOコインの API が断った（{method} {path}）: {str(d.get('messages', d))[:200]}")
        return d["data"]

    def status(self) -> str:
        d = self.request("GET", PUBLIC + "/v1/status", {}, None)
        return str(d.get("data", {}).get("status", "UNKNOWN"))

    def margin(self) -> dict:
        return self._call("GET", "/v1/account/margin")

    def positions(self, symbol: str) -> list[dict]:
        d = self._call("GET", "/v1/positionSummary", {"symbol": symbol})
        rows = d.get("list", d) if isinstance(d, dict) else d
        return list(rows or [])

    def open_order(self, symbol: str, side: str, size: str) -> str:
        return str(self._call("POST", "/v1/order", body={"symbol": symbol, "side": side, "executionType": "MARKET",
                                                         "size": size}))

    def close_bulk(self, symbol: str, side: str, size: str) -> str:
        return str(self._call("POST", "/v1/closeBulkOrder", body={"symbol": symbol, "side": side,
                                                                  "executionType": "MARKET", "size": size}))


# --------------------------------------------------------------------------- 建玉の照合
@dataclass
class Held:
    side: int        # +1 買い・−1 売り・0 なし
    size: float


def held_from_summary(rows: list[dict]) -> Held:
    """positionSummary の行（side・sumPositionQuantity）から、今持っている向きと数量。両建てなら差し引き。"""
    long = sum(float(r.get("sumPositionQuantity", 0) or 0) for r in rows if str(r.get("side", "")).upper() == "BUY")
    short = sum(float(r.get("sumPositionQuantity", 0) or 0) for r in rows if str(r.get("side", "")).upper() == "SELL")
    net = round(long - short, 8)
    if abs(net) < 1e-9:
        return Held(0, 0.0)
    return Held(1 if net > 0 else -1, abs(net))


def round_size(symbol: str, qty: float) -> float:
    step = SYMBOLS[symbol][2]
    return float(np.floor(qty / step + 1e-9) * step)


def target_size(symbol: str, d: Decision, cfg: dict) -> float:
    """記録の量を、最小の単位に丸め、名目の上限で抑える。"""
    if d.position == 0 or d.close <= 0:
        return 0.0
    cap = cfg["max_notional_jpy"] / d.close
    return round_size(symbol, min(d.qty, cap))


def plan_actions(symbol: str, held: Held, want_side: int, want_size: float) -> list[dict]:
    """今の建玉を記録の向きにそろえる注文の並び（閉じる → 建てる）。向きが同じなら何もしない（量の差は追わない）。"""
    acts = []
    if held.side != 0 and held.side != want_side:
        acts.append({"action": "close", "side": "SELL" if held.side > 0 else "BUY", "size": held.size})
    if want_side != 0 and held.side != want_side and want_size > 0:
        acts.append({"action": "open", "side": "BUY" if want_side > 0 else "SELL", "size": want_size})
    return acts


# --------------------------------------------------------------------------- 1 日 1 回
class LiveStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    @property
    def stop_file(self) -> Path:
        return self.root / "STOP"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def log_path(self) -> Path:
        return self.root / "log.csv"

    def config(self) -> dict:
        if self.config_path.exists():
            return {**DEFAULT_CONFIG, **json.loads(self.config_path.read_text(encoding="utf-8"))}
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        return dict(DEFAULT_CONFIG)

    def append(self, rows: list[dict]) -> None:
        if not rows:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        new = not self.log_path.exists()
        with open(self.log_path, "a", encoding="utf-8", newline="") as f:
            pd.DataFrame(rows, columns=LOG_COLUMNS).to_csv(f, header=new, index=False)

    def load_log(self) -> pd.DataFrame:
        return pd.read_csv(self.log_path) if self.log_path.exists() else pd.DataFrame(columns=LOG_COLUMNS)


def live_run(paper: PaperStore, live: LiveStore, client: GmoClient | None, real: bool = False,
             now: pd.Timestamp | None = None, log: Callable[[str], None] = print) -> list[dict]:
    """記録の向きに建玉をそろえる。client が None か real=False なら空運転（注文は出さず記録だけ）。"""
    now = now or pd.Timestamp.now(tz="UTC")
    cfg = live.config()
    mode = "real" if (real and client is not None) else "dry"
    rows: list[dict] = []

    def note(symbol, action, side="", size=0.0, target="", held="", order_id="", text=""):
        rows.append({"time": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": mode, "symbol": symbol, "action": action,
                     "side": side, "size": size, "target": target, "held": held, "order_id": order_id, "note": text})
        log(f"[{mode}] {symbol} {action} {side} {size} {text}".strip())

    if live.stop_file.exists():
        note("*", "skip", text="STOP があるので何もしない")
        live.append(rows)
        return rows
    if client is not None:
        try:
            st = client.status()
        except Exception as e:  # noqa: BLE001
            st = f"ERROR {str(e)[:80]}"
        if st != "OPEN":
            note("*", "skip", text=f"取引所の状態が {st}（OPEN でない）。今日は何もしない")
            live.append(rows)
            return rows

    frames = {s: frame(paper.load_klines(s), now) for s in cfg["symbols"]}
    decisions = decide(frames, float(cfg["equity_jpy"]))
    n_orders = 0
    for sym in cfg["symbols"]:
        d = decisions.get(sym)
        if d is None:
            note(sym, "skip", text="足が足りない")
            continue
        want_size = target_size(sym, d, cfg)
        if client is not None:
            try:
                held = held_from_summary(client.positions(sym))
            except Exception as e:  # noqa: BLE001
                note(sym, "error", text=f"建玉を読めない: {str(e)[:120]}")
                continue
        else:
            held = Held(0, 0.0)
        acts = plan_actions(sym, held, d.position, want_size)
        if not acts:
            note(sym, "hold", target=d.position, held=f"{held.side}:{held.size}", text="向きが同じ。何もしない")
            continue
        for a in acts:
            if n_orders >= MAX_ORDERS_PER_RUN:
                note(sym, "skip", a["side"], a["size"], text="1 回の注文の上限")
                continue
            size = f"{a['size']:.4f}".rstrip("0").rstrip(".")
            if mode == "real":
                try:
                    oid = (client.close_bulk(sym, a["side"], size) if a["action"] == "close"
                           else client.open_order(sym, a["side"], size))
                    n_orders += 1
                    note(sym, a["action"], a["side"], a["size"], d.position, f"{held.side}:{held.size}", oid, "成行")
                except Exception as e:  # noqa: BLE001
                    note(sym, "error", a["side"], a["size"], d.position, f"{held.side}:{held.size}", "",
                         f"注文が通らない: {str(e)[:120]}")
                    break
            else:
                n_orders += 1
                note(sym, a["action"], a["side"], a["size"], d.position, f"{held.side}:{held.size}", "dry", "空運転（出していない）")
    live.append(rows)
    return rows
