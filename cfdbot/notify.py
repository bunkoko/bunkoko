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
# 設定はこのリポジトリの外の ~/.bunkoko/notify.json。無ければ何も送らない。知らせ方（channels）は差し替えられ、上から順に全部で送る:
#   {"enabled": true, "send_empty": false,
#    "channels": [{"type": "imessage", "to": "自分の電話番号か Apple ID"}]}
# type（調査役の第 4 回の勧めの順）:
#   imessage  Mac の「メッセージ」を AppleScript で（無料・Apple のサーバーだけ。自分あてで iPhone が鳴るかは未確認）
#   mailapp   Mac の「メール」を AppleScript で（パスワード不要。iPhone のメールの通知を ON）。to が要る
#   pushover  Pushover（iPhone のアプリ 4.99 ドルの買い切り。月 10,000 通まで無料）。token・user が要る
#   mail      SMTP（パスワードは "password" か、Mac のキーチェーンの名前 "keychain"）
#   webhook   JSON を POST（n8n など）。command  任意のコマンドに本文を標準入力で渡す（題は環境変数 NOTIFY_TITLE）
# ntfy の公開サーバーは、話題の名前を知る人なら読めるので使わない。LINE Notify は 2025-03-31 に終わった。
NOTIFY_FILE = Path.home() / ".bunkoko" / "notify.json"
TYPES = ("imessage", "mailapp", "pushover", "mail", "webhook", "command")
TIMEOUT = 60.0        # AppleScript は、アプリが開いていないと数分止まることがある → 60 秒で打ち切る
RETRY_WAIT = 10.0     # 1 回だけやり直す前に待つ秒数
_IMESSAGE = ['on run argv', 'set theText to item 1 of argv', 'set theTarget to item 2 of argv',
             'tell application "Messages"', 'set svc to 1st account whose service type = iMessage',
             'send theText to participant theTarget of svc', 'end tell', 'end run']
_MAILAPP = ['on run argv', 'set theSubject to item 1 of argv', 'set theBody to item 2 of argv', 'set theTo to item 3 of argv',
            'tell application "Mail"', 'set m to make new outgoing message with properties {subject:theSubject, content:theBody, visible:false}',
            'tell m to make new to recipient at end of to recipients with properties {address:theTo}', 'send m', 'end tell', 'end run']


def load_settings(path: Path | None = None) -> tuple[dict | None, str]:
    """設定と、その説明（無い・止めてある・読めない・権限が広い）。使えないときは (None, 理由)。"""
    path = Path(path or NOTIFY_FILE)
    if not path.exists():
        return None, f"知らせの設定が無いので送らない（{path}。docs/stocks_live_daily.md の 5 章）"
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


def _osascript(lines: list[str], args: list[str], runner: Callable) -> tuple[bool, str]:
    cmd = ["osascript"] + sum((["-e", line] for line in lines), []) + args
    r = runner(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    return r.returncode == 0, "送った" if r.returncode == 0 else f"送れなかった（{(r.stderr or '').strip()[:160]}）"


def _post(url: str, data: bytes, content_type: str, opener: Callable) -> tuple[bool, str]:
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": content_type})
    with opener(req, timeout=TIMEOUT) as resp:
        return 200 <= resp.status < 300, f"応答 {resp.status}"


def _send_one(ch: dict, title: str, text: str, runner: Callable, opener: Callable) -> tuple[bool, str]:
    kind = ch["type"]
    if kind in ("imessage", "mailapp"):
        to = str(ch.get("to", "")).strip()
        if not to:
            return False, "送り先（to）が無い"
        if kind == "imessage":
            return _osascript(_IMESSAGE, [f"{title}\n{text}", to], runner)
        return _osascript(_MAILAPP, [title, text, to], runner)
    if kind == "pushover":
        import urllib.parse

        if not (ch.get("token") and ch.get("user")):
            return False, "token（アプリの API トークン）と user（利用者のキー）が要る"
        msg = text if len(text) <= 1000 else text[:990] + "\n…（続きは Claude に）"   # Pushover の本文は 1,024 字まで
        body = urllib.parse.urlencode({"token": ch["token"], "user": ch["user"], "title": title[:250], "message": msg}).encode("utf-8")
        return _post("https://api.pushover.net/1/messages.json", body, "application/x-www-form-urlencoded", opener)
    if kind == "mail":
        import smtplib
        from email.message import EmailMessage

        pw = ch.get("password")
        if not pw and ch.get("keychain"):
            r = runner(["security", "find-generic-password", "-s", str(ch["keychain"]), "-w"], capture_output=True, text=True, timeout=10)
            pw = (r.stdout or "").strip() if r.returncode == 0 else None
        if not (ch.get("to") and ch.get("smtp_host") and ch.get("user") and pw):
            return False, "to・smtp_host・user と、password か keychain が要る"
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, ch.get("from", ch["user"]), ch["to"]
        msg.set_content(text)
        with smtplib.SMTP(ch["smtp_host"], int(ch.get("smtp_port", 587)), timeout=TIMEOUT) as s:
            s.starttls()
            s.login(ch["user"], pw)
            s.send_message(msg)
        return True, "送った"
    if kind == "webhook":
        body = json.dumps({"source": "cfdbot", "event": "notice", "title": title, "text": text}, ensure_ascii=False).encode("utf-8")
        return _post(str(ch["url"]), body, "application/json", opener)
    argv = ch.get("argv")
    if not isinstance(argv, list) or not argv:
        return False, "argv（コマンドと引数の並び）が無い"
    r = runner([str(a) for a in argv], input=text, capture_output=True, text=True, timeout=TIMEOUT, env={**os.environ, "NOTIFY_TITLE": title})
    return r.returncode == 0, f"終わりの番号 {r.returncode}"


def notify(title: str, text: str, settings: dict | None = None, path: Path | None = None, runner: Callable = subprocess.run,
           opener: Callable = urllib.request.urlopen, sleep: Callable[[float], None] | None = None,
           record: Path | None = None) -> list[tuple[str, bool, str]]:
    """設定のすべての知らせ方で送る。1 つにつき 60 秒で打ち切り、だめなら 1 回だけやり直す。失敗しても例外を出さない
    （呼んだ側の処理を止めない）。record を渡せば、結果を 1 行ずつ書き足す。返り値: (種類, 送れたか, 説明)。"""
    import time

    if settings is None:
        settings, why = load_settings(path)
        if settings is None:
            return [("なし", False, why)]
    sleep = sleep or time.sleep
    out = []
    for ch in settings["channels"]:
        ok, detail = False, ""
        for attempt in (1, 2):
            try:
                ok, detail = _send_one(ch, title, text, runner, opener)
            except Exception as e:  # noqa: BLE001  知らせの失敗で処理全体を止めない
                ok, detail = False, f"送れなかった（{type(e).__name__}: {str(e)[:120]}）"
            if ok or attempt == 2:
                break
            sleep(RETRY_WAIT)
        detail = f"{detail}" + ("（2 回目）" if ok and attempt == 2 else "")
        log.info("notify %s %s", ch.get("type"), detail)
        out.append((str(ch.get("type")), ok, detail))
    if record is not None:
        try:
            record.parent.mkdir(parents=True, exist_ok=True)
            with open(record, "a", encoding="utf-8") as f:
                for kind, ok, detail in out:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{kind}\t{'送った' if ok else '送れなかった'}\t{detail}\t{title}\n")
        except OSError:
            pass
    return out
