"""LLM の呼び出し。

付加情報づけ（augment）は Mac 上のローカルLLMで夜間に回す（SPEC §8.3）。
Ollama は独自の /api/chat で呼ぶ（思考モードの切り替え・文脈長・出力長の上限・速度の記録ができる）。
LM Studio などは OpenAI 互換の /chat/completions で呼ぶ。
テストでは同じ形の偽物（complete を持つオブジェクト）に差し替える。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Protocol

import httpx


class LLMError(RuntimeError):
    pass


class ChatLLM(Protocol):
    name: str

    def complete(self, system: str, user: str, schema: dict[str, Any] | None = None) -> str: ...


class OllamaLLM:
    """Ollama の /api/chat を呼ぶ。stats に入出力のトークン数と時間（秒）を積み上げる。"""

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        think: bool | None = False,
        num_ctx: int | None = 4096,
        num_predict: int | None = 1024,
        temperature: float = 0.2,
        keep_alive: str = "10m",
        timeout_sec: float = 300.0,
        client: httpx.Client | None = None,
    ):
        # 設定に /v1（OpenAI 互換）が付いていても、Ollama 独自の API の場所にする
        self.endpoint = re.sub(r"/v1/?$", "", endpoint.rstrip("/"))
        self.model = model
        self.name = model
        self.think = think
        self.options = {
            k: v
            for k, v in {
                "temperature": temperature,
                "num_ctx": num_ctx,
                "num_predict": num_predict,
            }.items()
            if v is not None
        }
        self.keep_alive = keep_alive
        self.client = client or httpx.Client(timeout=timeout_sec)
        self.stats: Counter[str] = Counter()

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        try:
            return self.client.post(f"{self.endpoint}/api/chat", json=body)
        except httpx.TimeoutException as e:
            raise LLMError(
                f"{self.model} の応答が時間切れ（{e}）。"
                "思考モードが止まっているか（think = false）、"
                "モデルがメモリに収まっているか（ollama ps）を確かめてください"
            ) from e
        except httpx.HTTPError as e:
            raise LLMError(f"{self.endpoint} に接続できません：{e}") from e

    def complete(self, system: str, user: str, schema: dict[str, Any] | None = None) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": schema if schema is not None else "json",
            "options": self.options,
            "keep_alive": self.keep_alive,
        }
        if self.think is not None:
            body["think"] = self.think
        r = self._post(body)
        if r.status_code == 400 and "think" in r.text and "think" in body:
            # 思考モードの無いモデルは think の指定を受け付けないことがあるので、外して送り直す
            self.think = None
            del body["think"]
            r = self._post(body)
        if r.status_code >= 400:
            raise LLMError(f"{self.model} がエラーを返しました（{r.status_code}）：{r.text[:300]}")
        try:
            data = r.json()
            content = data["message"]["content"] or ""
        except (ValueError, KeyError, TypeError) as e:
            raise LLMError(f"{self.model} の応答を読めません：{r.text[:300]}") from e
        self.stats["calls"] += 1
        self.stats["prompt_tokens"] += int(data.get("prompt_eval_count") or 0)
        self.stats["output_tokens"] += int(data.get("eval_count") or 0)
        self.stats["prompt_ns"] += int(data.get("prompt_eval_duration") or 0)
        self.stats["output_ns"] += int(data.get("eval_duration") or 0)
        self.stats["load_ns"] += int(data.get("load_duration") or 0)
        return content


def describe_stats(stats: Counter[str]) -> str:
    """Ollama の stats を「入力・出力のトークン数と速度」の1行にする。"""
    calls = stats.get("calls", 0)
    if not calls:
        return ""
    out_s = stats["output_ns"] / 1e9
    in_s = stats["prompt_ns"] / 1e9
    out_rate = stats["output_tokens"] / out_s if out_s else 0.0
    in_rate = stats["prompt_tokens"] / in_s if in_s else 0.0
    return (
        f"1回あたり 入力 {stats['prompt_tokens'] / calls:.0f}トークン・"
        f"出力 {stats['output_tokens'] / calls:.0f}トークン。"
        f"生成 {out_rate:.1f}トークン/秒、入力の読み込み {in_rate:.0f}トークン/秒、"
        f"モデルの読み込み {stats['load_ns'] / 1e9:.0f}秒"
    )


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


def make_llm(conf: dict[str, Any], model: str | None = None) -> ChatLLM:
    """settings.toml の [llm.<役割>] から、ローカルLLMの呼び出し口を作る。"""
    model = model or conf.get("model", "")
    if conf.get("provider") == "ollama":
        return OllamaLLM(
            conf["endpoint"],
            model,
            think=conf.get("think", False),
            num_ctx=conf.get("num_ctx", 4096),
            num_predict=conf.get("num_predict", 1024),
            keep_alive=conf.get("keep_alive", "10m"),
            timeout_sec=conf.get("timeout_sec", 300),
        )
    if conf.get("provider") == "openai_compatible":
        return OpenAICompatLLM(
            conf["endpoint"],
            model,
            response_format=conf.get("response_format", "json_schema"),
            timeout_sec=conf.get("timeout_sec", 300),
        )
    raise ValueError(f"このLLMの provider には対応していません：{conf.get('provider')}")
