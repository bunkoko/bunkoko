"""設定の読み込み（SPEC §11）。

config/settings.toml を既定とし、環境変数 KOTEN_SETTINGS で別のファイルを指定できる。
APIキーはリポジトリ直下の .env から環境変数に読み込む。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"


class HddNotConnectedError(RuntimeError):
    """外付けHDDが未接続のときに、HDDを使うコマンドを止めるための例外（SPEC §5）。"""


@dataclass
class Settings:
    raw: dict[str, Any]
    config_dir: Path
    path: Path
    _sources: dict[str, Any] | None = field(default=None, repr=False)

    def section(self, name: str) -> dict[str, Any]:
        node: Any = self.raw
        for key in name.split("."):
            node = node.get(key, {})
        return node

    def _path(self, key: str) -> Path:
        value = self.raw["paths"][key]
        return Path(os.path.expanduser(value)).resolve()

    @property
    def hdd_root(self) -> Path:
        return self._path("hdd_root")

    @property
    def ssd_root(self) -> Path:
        return self._path("ssd_root")

    @property
    def sources(self) -> dict[str, Any]:
        if self._sources is None:
            self._sources = yaml.safe_load((self.config_dir / "sources.yaml").read_text("utf-8"))
        return self._sources

    def require_hdd(self) -> Path:
        """HDDを使うコマンドの入口で呼ぶ。未接続なら止める。"""
        root = self.hdd_root
        # /Volumes/<名前> のようにマウント先が無ければ未接続とみなす。<HDD_ROOT> 自体は作ってよい
        if not root.parent.exists():
            raise HddNotConnectedError(
                f"HDDが未接続です（{root.parent} がありません）。"
                "取得・付加・埋め込み・レンダリング系のコマンドは止めます。"
            )
        root.mkdir(parents=True, exist_ok=True)
        return root


def load_settings(path: str | Path | None = None) -> Settings:
    load_dotenv(REPO_ROOT / ".env", override=False)
    if path is None:
        path = os.environ.get("KOTEN_SETTINGS") or DEFAULT_CONFIG_DIR / "settings.toml"
    path = Path(path).resolve()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    # sources.yaml などは settings.toml と同じフォルダのものを優先し、
    # 無ければ既定の config/ を使う
    config_dir = path.parent if (path.parent / "sources.yaml").exists() else DEFAULT_CONFIG_DIR
    return Settings(raw=raw, config_dir=config_dir, path=path)
