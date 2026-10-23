"""koten コマンド（SPEC §11）。マイルストーンごとにコマンドを足していく。"""

from __future__ import annotations

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
    user_agent = http.get("user_agent", "koten-videos")
    if "example.com" in user_agent:
        # SPEC §8.1：User-Agent に連絡先を入れる。仮の値のまま外部サイトへ取りに行かない
        typer.secho(
            "settings.toml の [http] user_agent の連絡先が仮の値（example.com）のままです。"
            "自分のメールアドレスなどに書き換えてから実行してください",
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
    if not any((hdd / "chunks").glob("*.parquet")):
        _require_input(hdd / "chunks" / "aozora.parquet", "chunk aozora")
    from koten.augment.extract import AugmentConfig, load_themes, run_augment
    from koten.llm import LLMError, OllamaLLM, OpenAICompatLLM, describe_stats

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
    if conf.get("provider") == "ollama":
        llm = OllamaLLM(
            conf["endpoint"],
            model,
            think=conf.get("think", False),
            num_ctx=conf.get("num_ctx", 4096),
            num_predict=conf.get("num_predict", 1024),
            keep_alive=conf.get("keep_alive", "10m"),
            timeout_sec=conf.get("timeout_sec", 300),
        )
    else:
        llm = OpenAICompatLLM(
            conf["endpoint"],
            model,
            response_format=conf.get("response_format", "json_schema"),
            timeout_sec=conf.get("timeout_sec", 300),
        )
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
