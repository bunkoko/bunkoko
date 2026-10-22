"""koten doctor（SPEC §11・§13 M0）：HDD・SSD・VOICEVOX・LLM・埋め込みモデルの疎通と空き容量。"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from koten.config import Settings

OK, WARN, NG = "OK", "注意", "NG"


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _free(path: Path) -> str:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return f"空き {usage.free / 1024**3:.1f}GB / {usage.total / 1024**3:.1f}GB"


def check_hdd(s: Settings) -> Check:
    root = s.hdd_root
    if not root.parent.exists():
        return Check("HDD", NG, f"未接続（{root.parent} がありません）")
    state = "" if root.exists() else "（<HDD_ROOT> は未作成。初回の取得で作ります）"
    return Check("HDD", OK, f"{root} {_free(root)}{state}")


def check_ssd(s: Settings) -> Check:
    root = s.ssd_root
    state = "" if root.exists() else "（未作成）"
    return Check("SSD", OK if root.exists() else WARN, f"{root} {_free(root)}{state}")


def check_voicevox(s: Settings, client: httpx.Client) -> Check:
    endpoint = s.section("voicevox").get("endpoint", "")
    try:
        r = client.get(f"{endpoint.rstrip('/')}/version")
        r.raise_for_status()
        return Check("VOICEVOX", OK, f"{endpoint} version {r.text.strip(chr(34))}")
    except httpx.HTTPError as e:
        return Check("VOICEVOX", NG, f"{endpoint} に接続できません（{type(e).__name__}）")


def check_llm(s: Settings, role: str, client: httpx.Client) -> Check:
    conf = s.section(f"llm.{role}")
    provider, model = conf.get("provider"), conf.get("model", "")
    name = f"LLM（{role}）"
    if provider == "anthropic":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return Check(name, OK, f"anthropic {model}（APIキー設定済み）")
        return Check(name, NG, f"anthropic {model}（ANTHROPIC_API_KEY が未設定。.env に書く）")
    endpoint = conf.get("endpoint", "")
    try:
        if provider == "ollama":
            base = endpoint.rstrip("/").removesuffix("/v1")
            r = client.get(f"{base}/api/tags")
            r.raise_for_status()
            models = [m.get("name") for m in r.json().get("models", [])]
        else:
            r = client.get(f"{endpoint.rstrip('/')}/models")
            r.raise_for_status()
            models = [m.get("id") for m in r.json().get("data", [])]
    except (httpx.HTTPError, ValueError) as e:
        return Check(name, NG, f"{endpoint} に接続できません（{type(e).__name__}）")
    if model in models:
        return Check(name, OK, f"{endpoint} {model}")
    return Check(
        name, WARN, f"{endpoint} 稼働中。設定のモデル「{model}」は未ロード（{len(models)}件）"
    )


def check_embed(s: Settings) -> Check:
    model = s.section("embed").get("model", "")
    hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    cached = hf_home / "hub" / ("models--" + model.replace("/", "--"))
    if cached.exists():
        return Check("埋め込みモデル", OK, f"{model}（ダウンロード済み）")
    return Check("埋め込みモデル", WARN, f"{model}（未ダウンロード。M2 の koten embed で取得）")


def check_ffmpeg() -> Check:
    path = shutil.which("ffmpeg")
    return Check("ffmpeg", OK if path else NG, path or "見つかりません（brew install ffmpeg）")


def run_checks(s: Settings, client: httpx.Client | None = None) -> list[Check]:
    client = client or httpx.Client(timeout=3.0)
    return [
        check_hdd(s),
        check_ssd(s),
        check_voicevox(s, client),
        check_llm(s, "augment", client),
        check_llm(s, "script", client),
        check_llm(s, "judge", client),
        check_embed(s),
        check_ffmpeg(),
    ]
