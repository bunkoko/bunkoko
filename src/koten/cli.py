"""koten コマンド（SPEC §11）。マイルストーンごとにコマンドを足していく。"""

from __future__ import annotations

import os
import time
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


def _require_input(path, previous: str) -> None:
    """前の工程の出力が無ければ、何を先に実行すればよいかを出して止める。"""
    if not path.exists():
        typer.secho(
            f"{path} がありません。先に `koten {previous}` を最後まで実行してください。",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)


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
    user_agent = http.get("user_agent", "koten-videos/0.1 (+contact: {contact})")
    contact = os.environ.get("KOTEN_CONTACT", "").strip()
    if "{contact}" in user_agent:
        user_agent = user_agent.replace("{contact}", contact)
    looks_valid = "@" in contact or "://" in contact
    if ("contact:" in user_agent and not looks_valid) or "example.com" in user_agent:
        # SPEC §8.1：User-Agent に連絡先を入れる。連絡先が無いまま外部サイトへ取りに行かない
        typer.secho(
            f"連絡先（KOTEN_CONTACT）が正しく設定されていません（今の値：{contact or 'なし'}）。"
            "リポジトリ直下の .env に、メールアドレスか URL を書いてから実行してください",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)
    fetcher = Fetcher(
        hdd / "raw" / source,
        user_agent=user_agent,
        per_host_per_sec=s.section("rate_limit").get("per_host_per_sec", 1),
        timeout_sec=http.get("timeout_sec", 30),
    )
    r = aozora.ingest(
        hdd, s.sources["aozora"], fetcher, limit=limit, refresh_list=refresh_list, log=typer.echo
    )
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
    _require_input(hdd / "clean" / source / "works.parquet", f"ingest {source}")
    from koten.normalize import aozora

    typer.echo(f"正規化 {aozora.normalize(hdd)} 作品")


@app.command()
def chunk(source: str, settings: SettingsOpt = None) -> None:
    """チャンク化して chunks/<source>.parquet に書く。"""
    _check_source(source)
    s = _settings(settings)
    hdd = _hdd(s)
    _require_input(hdd / "clean" / source / "texts.parquet", f"normalize {source}")
    from koten.chunk.chunker import chunk_source

    conf = s.section("chunk")
    n = chunk_source(hdd, source, conf.get("min_chars", 200), conf.get("max_chars", 600))
    typer.echo(f"チャンク {n} 件")


def _augment_dir(s: Settings, hdd):
    from koten.augment.extract import llm_slug

    return (
        hdd
        / "augment"
        / llm_slug(s.section("llm.augment").get("model", ""))
        / s.section("augment").get("prompt", "augment_v1")
    )


@app.command()
def augment(
    limit: Annotated[int | None, typer.Option(help="今回処理するチャンクの上限")] = None,
    model: Annotated[
        str | None, typer.Option(help="ローカルLLMのモデル（既定は settings の llm.augment.model）")
    ] = None,
    resume: Annotated[bool, typer.Option(help="処理済みのチャンクを飛ばす（常に有効）")] = True,
    settings: SettingsOpt = None,
) -> None:
    """引用候補の抽出と付加情報（ローカルLLM。夜間向け）。中断しても続きから再開する。"""
    s = _settings(settings)
    hdd = _hdd(s)
    from koten.parquet import data_files

    if not data_files(hdd / "chunks", "*.parquet"):
        _require_input(hdd / "chunks" / "aozora.parquet", "chunk aozora")
    from koten.augment.extract import AugmentConfig, load_themes, run_augment
    from koten.llm import LLMError, describe_stats, make_llm

    conf = s.section("llm.augment")
    model = model or conf.get("model")
    if model in (None, "", "要確認"):
        typer.secho(
            "settings.toml の [llm.augment] model を、"
            "ローカルLLMで読み込んだモデル名にしてください",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)
    llm = make_llm(conf, model)
    cfg = AugmentConfig.from_settings(s.section("augment"))
    started = time.monotonic()
    try:
        r = run_augment(
            hdd,
            s.config_dir.parent / "prompts",
            load_themes(s.config_dir),
            llm,
            cfg,
            limit=limit,
            log=typer.echo,
        )
    except LLMError as e:
        typer.secho(str(e), fg="red", err=True)
        if stats := describe_stats(getattr(llm, "stats", {})):
            typer.echo(stats)
        raise typer.Exit(1) from e
    elapsed = time.monotonic() - started
    per_chunk = elapsed / r.processed if r.processed else 0.0
    typer.echo(
        f"処理 {r.processed} チャンク（失敗 {r.failed}）、候補 {r.candidates} 件。"
        f"捨てた一節：{dict(r.dropped) or 'なし'}"
    )
    typer.echo(f"時間 {elapsed:.0f}秒（1チャンクあたり {per_chunk:.1f}秒）・モデル {model}")
    if stats := describe_stats(getattr(llm, "stats", {})):
        typer.echo(stats)


@app.command()
def candidates(
    model: Annotated[
        str | None, typer.Option(help="ローカルLLMのモデル（既定は settings の llm.augment.model）")
    ] = None,
    n: Annotated[int, typer.Option(help="表示する件数")] = 5,
    seed: Annotated[int, typer.Option(help="選び方を変えるときの数")] = 0,
    settings: SettingsOpt = None,
) -> None:
    """抜き出した引用候補を何件か表示する（モデルの比べ用）。"""
    import random

    from koten.augment.extract import iter_candidates, llm_slug

    s = _settings(settings)
    hdd = _hdd(s)
    model = model or s.section("llm.augment").get("model", "")
    adir = hdd / "augment" / llm_slug(model) / s.section("augment").get("prompt", "augment_v1")
    _require_input(adir, f"augment --model {model}")
    rows = list(iter_candidates(adir))
    typer.echo(f"{model}：候補 {len(rows)} 件")
    for r in random.Random(seed).sample(rows, min(n, len(rows))):
        flag = f"　※注意：{r['caution_reason']}" if r["caution"] else ""
        typer.echo(
            f"\n■ {r['candidate_id']}（{r['section']}）引用映え {r['quotability']:.1f}{flag}"
        )
        typer.echo(f"  「{r['quote_text']}」")
        typer.echo(f"  要旨：{r['summary_ja']}")
        typer.echo(f"  テーマ：{'、'.join(r['themes'])}　悩み：{' / '.join(r['worries'])}")


@app.command()
def compare(
    models: Annotated[
        str, typer.Option(help="比べるモデルをカンマ区切りで（例：gemma4:26b,gemma3:27b）")
    ],
    n: Annotated[int, typer.Option(help="比べるチャンクの数（どのモデルも処理したものから）")] = 10,
    show: Annotated[bool, typer.Option(help="チャンクごとの並べ表示も画面に出す")] = False,
    settings: SettingsOpt = None,
) -> None:
    """ローカルLLMのモデルを、同じチャンクの結果で比べる。並べ表示は HDD の exports/ に書く。"""
    from koten.augment.compare import compare_models, summary_table, write_report

    s = _settings(settings)
    hdd = _hdd(s)
    names = [m.strip() for m in models.split(",") if m.strip()]
    try:
        summaries, markdown, chunk_ids = compare_models(
            hdd, s.section("augment").get("prompt", "augment_v1"), names, n
        )
    except FileNotFoundError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e
    if not chunk_ids:
        typer.secho("どのモデルも処理したチャンクがありません", fg="red", err=True)
        raise typer.Exit(2)
    typer.echo(f"どのモデルも処理したチャンク {len(chunk_ids)} 件で比べました：")
    typer.echo("  " + "、".join(chunk_ids))
    typer.echo(summary_table(summaries))
    typer.echo(f"\nチャンクごとの並べ表示：{write_report(hdd, markdown)}")
    if show:
        typer.echo("\n" + markdown)


@app.command()
def embed(
    model: Annotated[
        str | None, typer.Option(help="埋め込みモデル（既定は settings の embed.model）")
    ] = None,
    settings: SettingsOpt = None,
) -> None:
    """引用候補を埋め込み、HDD の emb/<model>/ に書く。"""
    s = _settings(settings)
    hdd = _hdd(s)
    adir = _augment_dir(s, hdd)
    _require_input(adir, "augment")
    from koten.embed.encoder import make_encoder
    from koten.embed.run import run_embed

    conf = s.section("embed")
    enc = make_encoder(
        model or conf["model"],
        device=conf.get("device", "auto"),
        batch_size=conf.get("batch_size", 32),
    )
    r = run_embed(hdd, adir, enc, passage_prefix=conf.get("passage_prefix", ""), log=typer.echo)
    typer.echo(f"埋め込み {r.embedded} 件（埋め込み済み {r.skipped} 件）：{r.out_dir}")


index_app = typer.Typer(no_args_is_help=True, help="索引（SSD）")
app.add_typer(index_app, name="index")


@index_app.command("build")
def index_build(
    model: Annotated[
        str | None, typer.Option(help="埋め込みモデル（既定は settings の embed.model）")
    ] = None,
    settings: SettingsOpt = None,
) -> None:
    """HDD の候補と埋め込みから、SSD に候補表・密ベクトル・BM25 の索引を作る。"""
    s = _settings(settings)
    hdd = _hdd(s)
    adir = _augment_dir(s, hdd)
    _require_input(adir, "augment")
    from koten.index.build import build_index

    meta = build_index(
        hdd,
        s.ssd_root,
        adir,
        model or s.section("embed")["model"],
        tokenizer=s.section("bm25").get("tokenizer", "sudachi"),
        log=typer.echo,
    )
    typer.echo(f"索引 {meta['count']} 件（{meta['model']}・{meta['tokenizer']}）")


@index_app.command("query")
def index_query(
    text: str,
    k: Annotated[int, typer.Option(help="表示する件数")] = 5,
    settings: SettingsOpt = None,
) -> None:
    """索引の確かめ用：密ベクトルと BM25 で別々に引いた上位を表示する（SSD だけを読む）。"""
    s = _settings(settings)
    from koten.embed.encoder import make_encoder
    from koten.index.search import HotIndex

    conf = s.section("embed")
    idx = HotIndex(s.ssd_root, conf["model"])
    enc = make_encoder(conf["model"], device=conf.get("device", "auto"))
    qvec = enc.encode([conf.get("query_prefix", "") + text])[0]
    for label, hits in (
        ("密ベクトル", idx.search_dense(qvec, k)),
        ("BM25", idx.search_bm25(text, k)),
    ):
        typer.echo(f"■ {label}")
        for h in hits:
            r = h.row
            typer.echo(
                f"  {h.score:.3f} {r['title']}（{'・'.join(r['author_names'])}）{r['section']}"
            )
            typer.echo(f"      「{r['quote_text']}」")
            typer.echo(f"      要旨：{r['summary_ja']}")


db_app = typer.Typer(no_args_is_help=True, help="運用データの SQLite（SSD）")
app.add_typer(db_app, name="db")


@db_app.command("init")
def db_init(settings: SettingsOpt = None) -> None:
    """SSD に SQLite を作り（あれば表を足すだけ）、config/stats.csv を読み込む。"""
    from koten.db import StatsError, connect, db_path, load_stats

    s = _settings(settings)
    conn = connect(s.ssd_root)
    try:
        n = load_stats(conn, s.config_dir / "stats.csv")
    except StatsError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e
    typer.echo(f"データベース：{db_path(s.ssd_root)}")
    typer.echo(f"統計 {n} 件を読み込みました（config/stats.csv）")


worries_app = typer.Typer(no_args_is_help=True, help="悩みマスター（SPEC §9）")
app.add_typer(worries_app, name="worries")


@worries_app.command("import")
def worries_import(
    from_: Annotated[
        str, typer.Option("--from", help="CSV のパス（text, source_type, source_ref, demand）")
    ],
    settings: SettingsOpt = None,
) -> None:
    """悩みを CSV から取り込む（status=new）。同じ文は入れない。"""
    from pathlib import Path

    from koten.db import connect
    from koten.worries.master import import_csv

    if from_ == "comments":
        typer.secho("YouTube のコメントからの取り込みは M9 で足します", fg="red", err=True)
        raise typer.Exit(2)
    s = _settings(settings)
    r = import_csv(connect(s.ssd_root), Path(from_))
    typer.echo(f"取り込み {r.added} 件、同じ文があり飛ばした {r.duplicated} 件")
    for msg in r.skipped:
        typer.secho(f"  飛ばした行：{msg}", fg="yellow", err=True)


@worries_app.command("normalize")
def worries_normalize(
    limit: Annotated[int | None, typer.Option(help="今回正規化する件数の上限")] = None,
    settings: SettingsOpt = None,
) -> None:
    """新しい悩みを正規化し、除外を判定し、似た悩みをクラスタにまとめる。"""
    from koten.augment.extract import load_themes
    from koten.db import connect
    from koten.embed.encoder import make_encoder
    from koten.llm import LLMError, make_llm
    from koten.worries.master import (
        WorriesConfig,
        cluster_worries,
        load_blocked_keywords,
        run_normalize,
    )

    s = _settings(settings)
    conn = connect(s.ssd_root)
    cfg = WorriesConfig.from_settings(s.section("worries"))
    llm = make_llm(s.section("llm.worries"))
    try:
        r = run_normalize(
            conn,
            llm,
            s.config_dir.parent / "prompts",
            load_themes(s.config_dir),
            load_blocked_keywords(s.config_dir),
            cfg,
            limit=limit,
            log=typer.echo,
        )
    except LLMError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    typer.echo(
        f"正規化 {r.active} 件、除外 {r.blocked} 件 {r.blocked_by or ''}、"
        f"正規化できなかった {r.errors} 件"
    )
    conf = s.section("embed")
    enc = make_encoder(conf["model"], device=conf.get("device", "auto"))
    n = cluster_worries(conn, enc, cfg.cluster_threshold, query_prefix=conf.get("query_prefix", ""))
    total = conn.execute("SELECT COUNT(*) FROM worries WHERE status = 'active'").fetchone()[0]
    typer.echo(
        f"クラスタ {n} 個（使える悩み {total} 件。類似度 {cfg.cluster_threshold} 以上で同じ）"
    )


@worries_app.command("list")
def worries_list(
    status: Annotated[
        str, typer.Option(help="ranked（優先度順）/ active / blocked / error / new / all")
    ] = "ranked",
    top: Annotated[int, typer.Option(help="表示する件数")] = 30,
    settings: SettingsOpt = None,
) -> None:
    """悩みを一覧にする。既定は、回の主役にできる悩みの優先度順。"""
    import json

    from koten.db import connect
    from koten.worries.master import WorriesConfig, load_theme_weights, ranked_worries

    s = _settings(settings)
    conn = connect(s.ssd_root)
    if status == "ranked":
        cfg = WorriesConfig.from_settings(s.section("worries"))
        rows = ranked_worries(conn, load_theme_weights(s.config_dir), cfg.novelty_days)[:top]
        for r in rows:
            members = conn.execute(
                "SELECT COUNT(*) FROM worries WHERE cluster_id = ?", (r["worry_id"],)
            ).fetchone()[0]
            typer.echo(
                f"{r['score']:5.2f} {r['worry_id']} {r['text_general']}"
                f"（{'・'.join(r['themes'])}／{r['audience']}／似た悩み {members - 1} 件）"
            )
        return
    where = "" if status == "all" else "WHERE status = ?"
    params = () if status == "all" else (status,)
    rows = conn.execute(
        f"SELECT * FROM worries {where} ORDER BY cluster_id, is_representative DESC, worry_id",
        params,
    ).fetchall()[:top]
    for r in rows:
        mark = "◎" if r["is_representative"] else ("・" if r["cluster_id"] else " ")
        themes = "・".join(json.loads(r["themes"] or "[]"))
        extra = r["blocked_reason"] or r["error"] or f"{themes}／{r['audience'] or ''}"
        cluster = f" [{r['cluster_id']}]" if r["cluster_id"] else ""
        typer.echo(
            f"{mark} {r['worry_id']} {r['status']:7} {r['text_general'] or r['text_raw']}"
            f"{cluster}（{extra}）"
        )


@app.command()
def serve(
    port: Annotated[int | None, typer.Option(help="ポート（既定は settings の api.port）")] = None,
    settings: SettingsOpt = None,
) -> None:
    """ローカルAPIを 127.0.0.1 で起動する（SSD だけを読み、HDD には触れない）。"""
    import uvicorn

    from koten.api.app import create_app

    s = _settings(settings)
    application = create_app(s.ssd_root, s.section("embed")["model"])
    uvicorn.run(application, host="127.0.0.1", port=port or s.section("api").get("port", 8765))


if __name__ == "__main__":
    app()
