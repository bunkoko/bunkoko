"""koten コマンド（SPEC §11）。マイルストーンごとにコマンドを足していく。"""

from __future__ import annotations

from typing import Annotated

import typer

from koten import doctor as doctor_mod
from koten.config import HddNotConnectedError, Settings, load_settings
from koten.net import Fetcher

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)

SUPPORTED_SOURCES = {"aozora"}
SettingsOpt = Annotated[
    str | None,
    typer.Option("--settings", help="settings.toml のパス（既定は config/settings.toml）"),
]


def _settings(path: str | None) -> Settings:
    return load_settings(path)


def _hdd(s: Settings):
    try:
        return s.require_hdd()
    except HddNotConnectedError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from e


def _check_source(source: str) -> None:
    if source not in SUPPORTED_SOURCES:
        typer.secho(
            f"{source} はまだ対応していません（NDL・Gutenberg・論文は M8）。", fg="red", err=True
        )
        raise typer.Exit(2)


@app.command()
def doctor(settings: SettingsOpt = None) -> None:
    """HDD・SSD・VOICEVOX・LLM・埋め込みモデルの疎通と空き容量を表示する。"""
    s = _settings(settings)
    typer.echo(f"設定: {s.path}")
    colors = {doctor_mod.OK: "green", doctor_mod.WARN: "yellow", doctor_mod.NG: "red"}
    for c in doctor_mod.run_checks(s):
        typer.echo(f"  {typer.style(f'[{c.status}]', fg=colors[c.status])} {c.name}: {c.detail}")


@app.command()
def ingest(
    source: str,
    limit: Annotated[int | None, typer.Option(help="今回取得するテキストの上限")] = None,
    refresh_list: Annotated[bool, typer.Option(help="作品CSVを取り直す（ETagで確認）")] = False,
    settings: SettingsOpt = None,
) -> None:
    """原本を取得して raw/<source>/ へ保存する。"""
    _check_source(source)
    s = _settings(settings)
    hdd = _hdd(s)
    from koten.ingest import aozora

    http = s.section("http")
    fetcher = Fetcher(
        hdd / "raw" / source,
        user_agent=http.get("user_agent", "koten-videos"),
        per_host_per_sec=s.section("rate_limit").get("per_host_per_sec", 1),
        timeout_sec=http.get("timeout_sec", 30),
    )
    r = aozora.ingest(hdd, s.sources["aozora"], fetcher, limit=limit, refresh_list=refresh_list)
    typer.echo(
        f"対象 {r.selected} 作品（使用可 {r.usable}）："
        f"今回取得 {r.fetched}、取得済み {r.skipped}、本文を保存しない {r.blocked}"
    )
    for err in r.errors:
        typer.secho(f"  取得失敗：{err}", fg="yellow", err=True)


@app.command()
def normalize(source: str, settings: SettingsOpt = None) -> None:
    """正規化して clean/<source>/texts.parquet に書く。"""
    _check_source(source)
    hdd = _hdd(_settings(settings))
    from koten.normalize import aozora

    typer.echo(f"正規化 {aozora.normalize(hdd)} 作品")


@app.command()
def chunk(source: str, settings: SettingsOpt = None) -> None:
    """チャンク化して chunks/<source>.parquet に書く。"""
    _check_source(source)
    s = _settings(settings)
    hdd = _hdd(s)
    from koten.chunk.chunker import chunk_source

    conf = s.section("chunk")
    n = chunk_source(hdd, source, conf.get("min_chars", 200), conf.get("max_chars", 600))
    typer.echo(f"チャンク {n} 件")


if __name__ == "__main__":
    app()
