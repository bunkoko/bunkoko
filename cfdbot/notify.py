"""監視・通知。n8n などの Webhook に JSON を POST する。

EA 側は MT5 のプッシュ通知（SendNotification）と WebRequest で同じ Webhook に送れる。
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

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
