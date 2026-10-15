"""監視・通知。n8n などの Webhook に JSON を POST する（WebhookNotifier）。利用者の iPhone への知らせ（notify）。

EA 側は MT5 のプッシュ通知（SendNotification）と WebRequest で同じ Webhook に送れる。
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("cfdbot")


class WebhookNotifier:
    def __init__(self, url: str | None, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout

    def send(self, event: str, **payload: Any) -> bool:
        body = {"source": "cfdbot", "event": event, **payload}
        log.info("notify %s %s", event, payload)
        if not self.url:
            return False
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except OSError as e:  # 通知の失敗で処理全体を止めない
            log.warning("webhook failed: %s", e)
            return False


# --------------------------------------------------------------------------- 利用者への知らせ（日本株の予行演習など。docs/stocks_live_daily.md）
# 設定はこのリポジトリの外の ~/.bunkoko/notify.json。無ければ何も送らない。知らせ方（channels）は差し替えられる:
#   {"enabled": true, "send_empty": false,
#    "channels": [{"type": "imessage", "to": "+8190XXXXXXXX"}]}
# type: imessage（Mac の「メッセージ」を AppleScript で）・mail（SMTP。パスワードは "password" か、Mac のキーチェーンの名前 "keychain"）・
#       ntfy（ntfy の話題に送る）・webhook（JSON を POST）・command（任意のコマンドに本文を標準入力で渡す）
NOTIFY_FILE = Path.home() / ".bunkoko" / "notify.json"
TYPES = ("imessage", "mail", "ntfy", "webhook", "command")
_IMESSAGE = ['on run argv', 'set theText to item 1 of argv', 'set theTarget to item 2 of argv',
             'tell application "Messages"', 'set svc to 1st account whose service type = iMessage',
             'send theText to participant theTarget of svc', 'end tell', 'end run']


def load_settings(path: Path | None = None) -> tuple[dict | None, str]:
    """設定と、その説明（無い・止めてある・読めない・権限が広い）。使えないときは (None, 理由)。"""
    path = Path(path or NOTIFY_FILE)
    if not path.exists():
        return None, f"知らせの設定が無いので送らない（{path}。docs/stocks_live_daily.md の 6 章）"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"知らせの設定が読めない（{path}: {str(e)[:80]}）"
    if not cfg.get("enabled", True):
        return None, "知らせの設定で enabled が false なので送らない"
    chans = [c for c in cfg.get("channels", []) if isinstance(c, dict)]
    bad = [str(c.get("type")) for c in chans if c.get("type") not in TYPES]
    if not chans or bad:
        return None, f"知らせ方（channels）が無いか、知らない種類（{', '.join(bad) or 'なし'}）。使えるのは {', '.join(TYPES)}"
    note = ""
    try:
        if os.name == "posix" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            note = f"（注意: {path} はほかの人も読める。chmod 600 にする）"
    except OSError:
        pass
    return {**cfg, "channels": chans}, note


def _send_one(ch: dict, title: str, text: str, runner: Callable, opener: Callable) -> tuple[bool, str]:
    kind = ch["type"]
    if kind == "imessage":
        to = str(ch.get("to", "")).strip()
        if not to:
            return False, "imessage: 送り先（to。電話番号か Apple ID）が無い"
        args = ["osascript"] + sum((["-e", line] for line in _IMESSAGE), []) + [f"{title}\n{text}", to]
        r = runner(args, capture_output=True, text=True, timeout=30)
        return r.returncode == 0, "imessage: 送った" if r.returncode == 0 else f"imessage: 送れなかった（{(r.stderr or '').strip()[:160]}）"
    if kind == "mail":
        import smtplib
        from email.message import EmailMessage

        pw = ch.get("password")
        if not pw and ch.get("keychain"):
            r = runner(["security", "find-generic-password", "-s", str(ch["keychain"]), "-w"], capture_output=True, text=True, timeout=10)
            pw = (r.stdout or "").strip() if r.returncode == 0 else None
        if not (ch.get("to") and ch.get("smtp_host") and ch.get("user") and pw):
            return False, "mail: to・smtp_host・user と、password か keychain が要る"
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, ch.get("from", ch["user"]), ch["to"]
        msg.set_content(text)
        with smtplib.SMTP(ch["smtp_host"], int(ch.get("smtp_port", 587)), timeout=20) as s:
            s.starttls()
            s.login(ch["user"], pw)
            s.send_message(msg)
        return True, "mail: 送った"
    if kind == "ntfy":
        topic = str(ch.get("topic", "")).strip()
        if not topic:
            return False, "ntfy: 話題（topic）が無い"
        body = json.dumps({"topic": topic, "title": title, "message": text}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(str(ch.get("server", "https://ntfy.sh")).rstrip("/") + "/", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with opener(req, timeout=15) as resp:
            return 200 <= resp.status < 300, f"ntfy: {resp.status}"
    if kind == "webhook":
        req = urllib.request.Request(str(ch["url"]), method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"source": "cfdbot", "event": "notice", "title": title, "text": text},
                                                     ensure_ascii=False).encode("utf-8"))
        with opener(req, timeout=15) as resp:
            return 200 <= resp.status < 300, f"webhook: {resp.status}"
    argv = ch.get("argv")
    if not isinstance(argv, list) or not argv:
        return False, "command: argv（コマンドと引数の並び）が無い"
    r = runner([str(a) for a in argv], input=text, capture_output=True, text=True, timeout=60, env={**os.environ, "NOTIFY_TITLE": title})
    return r.returncode == 0, f"command: 終わりの番号 {r.returncode}"


def notify(title: str, text: str, settings: dict | None = None, path: Path | None = None,
           runner: Callable = subprocess.run, opener: Callable = urllib.request.urlopen) -> list[tuple[str, bool, str]]:
    """設定のすべての知らせ方で送る。失敗しても例外を出さない（呼んだ側の処理を止めない）。返り値: (種類, 送れたか, 説明)。"""
    if settings is None:
        settings, why = load_settings(path)
        if settings is None:
            return [("なし", False, why)]
    out = []
    for ch in settings["channels"]:
        try:
            ok, detail = _send_one(ch, title, text, runner, opener)
        except Exception as e:  # noqa: BLE001  知らせの失敗で処理全体を止めない
            ok, detail = False, f"{ch.get('type')}: 送れなかった（{type(e).__name__}: {str(e)[:120]}）"
        log.info("notify %s %s", ch.get("type"), detail)
        out.append((str(ch.get("type")), ok, detail))
    return out
