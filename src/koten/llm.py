"""LLM の呼び出し。

付加情報づけ（augment）は Mac 上のローカルLLM（OpenAI互換API）で夜間に回す（SPEC §8.3）。
テストでは同じ形の偽物（complete を持つオブジェクト）に差し替える。
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

import httpx


class LLMError(RuntimeError):
    pass


class ChatLLM(Protocol):
    name: str

    def complete(self, system: str, user: str, schema: dict[str, Any] | None = None) -> str: ...


class OpenAICompatLLM:
    """LM Studio・Ollama などの OpenAI 互換 /chat/completions を呼ぶ。"""

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        response_format: str = "json_schema",
        temperature: float = 0.2,
        timeout_sec: float = 300.0,
        client: httpx.Client | None = None,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.name = model
        self.response_format = response_format
        self.temperature = temperature
        self.client = client or httpx.Client(timeout=timeout_sec)

    def complete(self, system: str, user: str, schema: dict[str, Any] | None = None) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.response_format == "json_schema" and schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "strict": True, "schema": schema},
            }
        elif self.response_format == "json_object":
            body["response_format"] = {"type": "json_object"}
        try:
            r = self.client.post(f"{self.endpoint}/chat/completions", json=body)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"] or ""
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            raise LLMError(f"{self.endpoint} の呼び出しに失敗：{e}") from e


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
_THINK = re.compile(r"<think>.*?</think>", re.S)


def parse_json_object(content: str) -> dict[str, Any]:
    """応答から JSON オブジェクトを取り出す。

    コードフェンス、前後の文章、考える途中経過（<think>…</think>）があっても読む。
    """
    text = _FENCE.sub("", _THINK.sub("", content).strip())
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("JSON がオブジェクトではない")
    return value


def load_prompt(prompts_dir, name: str) -> tuple[str, str]:
    """prompts/<name>.md を「# system」と「# user」の2つに分ける。"""
    raw = (prompts_dir / f"{name}.md").read_text("utf-8")
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    m = re.search(r"^# system\s*$(.*?)^# user\s*$(.*)", raw, flags=re.S | re.M)
    if not m:
        raise ValueError(f"{name}.md に「# system」と「# user」の見出しがありません")
    return m.group(1).strip(), m.group(2).strip()


def fill(template: str, **values: Any) -> str:
    """{name} だけを差し込む（JSON の例の { } はそのまま残す）。"""
    return re.sub(
        r"\{(\w+)\}",
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
        template,
    )
