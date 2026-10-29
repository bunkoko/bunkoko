"""koten コマンド（SPEC §11）。マイルストーンごとにコマンドを足していく。"""

from __future__ import annotations

import os
import time
from typing import Annotated

import typer

from koten import doctor as doctor_mod
from koten.authors import author_label
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


def _make_fetcher(s: Settings, raw_dir):
    """外部サイトへの取得（net.py）。User-Agent に連絡先が無ければ取りに行かない（SPEC §8.1）。"""
    http = s.section("http")
    user_agent = http.get("user_agent", "koten-videos/0.1 (+contact: {contact})")
    contact = os.environ.get("KOTEN_CONTACT", "").strip()
    if "{contact}" in user_agent:
        user_agent = user_agent.replace("{contact}", contact)
    looks_valid = "@" in contact or "://" in contact
    if ("contact:" in user_agent and not looks_valid) or "example.com" in user_agent:
        typer.secho(
            f"連絡先（KOTEN_CONTACT）が正しく設定されていません（今の値：{contact or 'なし'}）。"
            "リポジトリ直下の .env に、メールアドレスか URL を書いてから実行してください",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)
    return Fetcher(
        raw_dir,
        user_agent=user_agent,
        per_host_per_sec=s.section("rate_limit").get("per_host_per_sec", 1),
        timeout_sec=http.get("timeout_sec", 30),
    )


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

    fetcher = _make_fetcher(s, hdd / "raw" / source)
    r = aozora.ingest(
        hdd, s.sources["aozora"], fetcher, limit=limit, refresh_list=refresh_list, log=typer.echo
    )
    typer.echo(
        f"対象 {r.selected} 作品（使用可 {r.usable}）："
        f"今回取得 {r.fetched}、取得済み {r.skipped}、本文を保存しない {r.blocked}"
    )
    for err in r.errors:
        typer.secho(f"  取得失敗：{err}", fg="yellow", err=True)
    if r.removed:
        typer.secho(
            f"権利の判定が厳しくなり、本文を消した作品が {len(r.removed)} 件あります。"
            "normalize・chunk・index build をやり直し、"
            "付加情報は forget-works で取り除いてください：",
            fg="yellow",
        )
        for line in r.removed:
            typer.echo(f"  {line}")


@app.command()
def normalize(source: str, settings: SettingsOpt = None) -> None:
    """正規化して clean/<source>/texts.parquet に書く。"""
    _check_source(source)
    hdd = _hdd(_settings(settings))
    _require_input(hdd / "clean" / source / "works.parquet", f"ingest {source}")
    from koten.normalize import aozora

    typer.echo(f"正規化 {aozora.normalize(hdd)} 作品")
    from koten.augment.forget import read_changed

    changed = read_changed(aozora.changed_works_path(hdd))
    if changed:
        typer.secho(
            f"本文か章が変わった作品が {len(changed)} 件あります。チャンク化のあと、"
            "uv run koten forget-works --changed で付加情報を作り直せます",
            fg="yellow",
        )


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


@app.command("forget-works")
def forget_works_cmd(
    changed: Annotated[
        bool, typer.Option(help="正規化で本文が変わった作品（changed_works.json）を対象にする")
    ] = False,
    work: Annotated[list[str] | None, typer.Option(help="作品ID（くり返し指定できる）")] = None,
    apply: Annotated[bool, typer.Option(help="実際に取り除く（付けなければ数えるだけ）")] = False,
    settings: SettingsOpt = None,
) -> None:
    """作品の付加情報（候補・処理済み・埋め込み）を取り除き、作り直せるようにする。"""
    from koten.augment.forget import episodes_using, forget_works, read_changed
    from koten.db import connect
    from koten.normalize.aozora import changed_works_path

    s = _settings(settings)
    hdd = _hdd(s)
    targets = set(work or [])
    if changed:
        targets |= read_changed(changed_works_path(hdd))
    if not targets:
        typer.echo("対象の作品がありません（本文が変わった作品はありません）")
        return
    users = episodes_using(connect(s.ssd_root), targets)
    if users:
        typer.secho(
            "この作品の一節を使っている回があります。先に取り消してください"
            f"（koten episode discard --episode …）：{'、'.join(users)}",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)
    r = forget_works(hdd, _augment_dir(s, hdd), targets, apply=apply)
    verb = "取り除きました" if apply else "取り除く対象（--apply で実行）"
    typer.echo(
        f"作品 {len(targets)} 件：候補 {r.candidates} 件・処理済みチャンク {r.done_chunks} 件・"
        f"埋め込み {r.embeddings} 件を{verb}"
    )
    if apply:
        if changed:
            changed_works_path(hdd).unlink(missing_ok=True)
        typer.echo("続けて：uv run koten augment → uv run koten embed → uv run koten index build")


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
                f"  {h.score:.3f} {r['title']}（{author_label(r, aliases=True)}）{r['section']}"
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
                f"（{'、'.join(r['themes'])}／{r['audience']}／似た悩み {members - 1} 件）"
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
        themes = "、".join(json.loads(r["themes"] or "[]"))
        extra = r["blocked_reason"] or r["error"] or f"{themes}／{r['audience'] or ''}"
        cluster = f" [{r['cluster_id']}]" if r["cluster_id"] else ""
        typer.echo(
            f"{mark} {r['worry_id']} {r['status']:7} {r['text_general'] or r['text_raw']}"
            f"{cluster}（{extra}）"
        )


AUTO_PLAN_WORRIES = 5  # 悩みを省いた企画で試す、優先度の上位の件数


def _planner(s: Settings, *, log=None):
    """検索・企画に使う部品（SSD の索引と DB、埋め込み、LLM、リランク）をそろえる。"""
    from koten.augment.extract import load_themes
    from koten.db import connect
    from koten.embed.encoder import make_encoder
    from koten.index.search import HotIndex
    from koten.llm import make_llm
    from koten.plan.planner import PlanConfig, Planner
    from koten.retrieve.search import make_reranker

    emb = s.section("embed")
    rec, ep, ret = s.section("recency"), s.section("episode"), s.section("retrieve")
    cfg = PlanConfig(
        query_prompt=ret.get("query_prompt", "query_v1"),
        select_prompt=ret.get("select_prompt", "select_v1"),
        rrf_k=ret.get("rrf_k", 60),
        fused_top=ret.get("fused_top", 50),
        shortlist=ret.get("shortlist", 10),
        work_episodes=rec.get("work_episodes", 10),
        author_episodes=rec.get("author_episodes", 5),
        worry_cluster_days=rec.get("worry_cluster_days", 30),
        same_format_in_row_max=ep.get("same_format_in_row_max", 2),
        query_prefix=emb.get("query_prefix", ""),
        passage_prefix=emb.get("passage_prefix", ""),
    )
    try:
        index = HotIndex(s.ssd_root, emb["model"])
    except FileNotFoundError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e
    rr = s.section("rerank")
    return Planner(
        conn=connect(s.ssd_root),
        index=index,
        encoder=make_encoder(emb["model"], device=emb.get("device", "auto")),
        llm=make_llm(s.section("llm.retrieve")),
        reranker=make_reranker(rr.get("model"), device=rr.get("device", "auto")),
        prompts_dir=s.config_dir.parent / "prompts",
        config_dir=s.config_dir,
        vocab=load_themes(s.config_dir),
        cfg=cfg,
        log=log or typer.echo,
    )


@app.command()
def retrieve(
    worry: Annotated[str, typer.Option(help="悩みの ID（例：W0001）")],
    top: Annotated[int, typer.Option(help="表示する件数")] = 10,
    explain: Annotated[bool, typer.Option(help="クエリと絞り込みの内訳も出す")] = False,
    settings: SettingsOpt = None,
) -> None:
    """悩みに合う一節の上位を、選んだ理由（どの検索で何位か・リランクの点）と一緒に出す。"""
    from koten.plan.planner import PlanError
    from koten.retrieve.search import filter_report

    p = _planner(_settings(settings))
    try:
        w = p.worry(worry)
    except PlanError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e
    q = p.queries(w)
    flt = p.base_filter()
    typer.echo(f"悩み：{w['worry_id']} {w['text_general']}（{'、'.join(q.themes)}）")
    if explain:
        typer.echo(f"  言い換え：{q.paraphrase}")
        typer.echo(f"  HyDE：{q.hyde}")
        if q.fallback:
            typer.secho("  ※ LLM の出力が使えず、悩みの文だけで検索しました", fg="yellow")
        typer.echo(
            "絞り込み：" + "、".join(f"{k} {v}" for k, v in filter_report(p.index, flt).items())
        )
        typer.echo(
            f"リランク：{p.reranker.name if p.reranker else 'なし（rerank.model が未設定）'}"
        )
    for i, h in enumerate(p.search(q, flt)[:top], start=1):
        r = h.row
        score = f"リランク {h.rerank:.3f}" if h.rerank is not None else f"RRF {h.rrf:.4f}"
        typer.echo(f"\n{i:2}. {r['title']}（{author_label(r, aliases=True)}）{r['section'] or ''}")
        typer.echo(f"    「{r['quote_text']}」")
        typer.echo(f"    要旨：{r['summary_ja']}")
        typer.echo(f"    理由：{score}／{h.provenance()}")


@app.command()
def plan(
    worry: Annotated[
        str | None, typer.Option(help="悩みの ID（形式C ではテーマの代わりにも使える）")
    ] = None,
    format: Annotated[
        str | None, typer.Option(help="A〜D（省略すると、続かないように選ぶ）")
    ] = None,
    theme: Annotated[str | None, typer.Option(help="形式C のテーマ")] = None,
    work: Annotated[str | None, typer.Option(help="形式B の作品（例：aozora:001914）")] = None,
    dry_run: Annotated[bool, typer.Option(help="DB に保存しない")] = False,
    force: Annotated[bool, typer.Option(help="同じクラスタの悩みの30日の間隔を無視する")] = False,
    show_json: Annotated[bool, typer.Option("--json", help="plan_json をそのまま出す")] = False,
    settings: SettingsOpt = None,
) -> None:
    """回の企画（plan_json）を作り、回を planned で保存する。

    悩みもテーマも省くと、優先度の高い悩みから順に（上位5件まで）企画を試す。
    """
    import json

    from koten.plan.planner import PlanError
    from koten.worries.master import WorriesConfig, load_theme_weights, ranked_worries

    s = _settings(settings)
    p = _planner(s)
    formats = [format.upper()] if format else p.allowed_formats()
    auto = worry is None and theme is None
    worry_ids: list[str | None] = [worry]
    if auto:
        wcfg = WorriesConfig.from_settings(s.section("worries"))
        ranked = ranked_worries(p.conn, load_theme_weights(s.config_dir), wcfg.novelty_days)
        worry_ids = [w["worry_id"] for w in ranked[:AUTO_PLAN_WORRIES]]
    result = None
    for wid in worry_ids:
        for fmt in formats:
            try:
                result = p.plan(fmt, worry_id=wid, theme=theme, work_id=work, force=force)
                break
            except PlanError as e:
                typer.echo(f"  {wid + '：' if auto else ''}形式{fmt} は作れません：{e}")
        if result is not None:
            break
    if result is None:
        msg = "企画を作れませんでした。この悩みは後回しにします"
        if auto:
            msg = f"優先度の上位{len(worry_ids)}件の悩みで、企画を作れませんでした"
        typer.secho(msg, fg="red", err=True)
        raise typer.Exit(2)
    if not dry_run:
        result["episode_id"] = p.save(result)
    if show_json:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    rows = {x["candidate_id"]: x for x in result["selection"]}
    minutes, seconds = divmod(result["target_total_sec"], 60)
    chosen_by = "LLM" if result["selected_by"] == "llm" else "検索の順位"
    typer.echo(
        f"{result.get('episode_id', '（保存していません）')} 形式{result['format']} "
        f"{result['format_name']}／テーマ {'、'.join(result['theme'])}／"
        f"目安 {minutes}分{seconds}秒／選び方 {chosen_by}"
    )
    for w in result["worries"]:
        typer.echo(f"  悩み {w['worry_id']}：{w['text']}")
    for c in result["chapters"]:
        extra = "　※人の一言" if c.get("human_take") else ""
        stats = f"　統計 {', '.join(c['stat_ids'])}" if c["stat_ids"] else ""
        refs = f"　語釈・解釈 {', '.join(c['ref_ids'])}" if c.get("ref_ids") else ""
        typer.echo(f"  {c['chapter_id']} {c['role']}（{c['target_sec']}秒）{stats}{refs}{extra}")
        for cid in c["candidate_ids"]:
            x = rows[cid]
            side = f"[{x['side']}] " if x["side"] else ""
            who = "・".join(x["authors"])
            typer.echo(f"      {side}{x['title']}（{who}）→ {x['worry_id']}：{x['reason']}")
    if result.get("question"):
        typer.echo(f"  問いかけ：{result['question']}")


eval_app = typer.Typer(no_args_is_help=True, help="検索の評価（SPEC §12）")
app.add_typer(eval_app, name="eval")


def _eval_runner(p):
    from koten.eval.retrieval import EVAL_FILTER
    from koten.retrieve.search import apply_rerank, hybrid_search

    cache: dict = {}

    def run(worry_id: str, cfg, k: int) -> list[str]:
        if worry_id not in cache:
            cache[worry_id] = p.queries(p.worry(worry_id))
        q = cache[worry_id]
        items = hybrid_search(
            p.index,
            p.encoder,
            q,
            EVAL_FILTER,
            query_prefix=p.cfg.query_prefix,
            passage_prefix=p.cfg.passage_prefix,
            queries=cfg.queries,
            retrievers=cfg.retrievers,
            rrf_k=p.cfg.rrf_k,
            top=p.cfg.fused_top,
        )
        if cfg.rerank:
            items = apply_rerank(items, p.reranker, q.text)
        return [s.candidate_id for s in items[:k]]

    return run


def _eval_configs(p):
    from koten.eval.retrieval import CONFIGS

    return [c for c in CONFIGS if not c.rerank or p.reranker is not None]


@eval_app.command("export")
def eval_export(
    n: Annotated[int, typer.Option(help="評価に使う悩みの数（優先度の高い代表から）")] = 20,
    out: Annotated[str, typer.Option(help="書き出す CSV")] = "tests/eval/retrieval.csv",
    settings: SettingsOpt = None,
) -> None:
    """印付け用の CSV を書き出す（すでに付けた点は残す）。grade 列に 1・0 を付ける。"""
    from pathlib import Path

    from koten.eval.retrieval import export_pool
    from koten.worries.master import load_theme_weights, ranked_worries

    s = _settings(settings)
    p = _planner(s)
    worries = ranked_worries(p.conn, load_theme_weights(s.config_dir), 90)[:n]
    total, judged = export_pool(
        Path(out),
        worries,
        _eval_runner(p),
        lambda cid: p._row({"candidate_id": cid}),
        _eval_configs(p),
    )
    typer.echo(
        f"{out} に {len(worries)} 件の悩み・{total} 行を書きました（点が付いている行 {judged}）"
    )
    typer.echo("grade 列に 1＝合う、0＝合わない を付けて保存してください")


@eval_app.command("retrieval")
def eval_retrieval(
    file: Annotated[str, typer.Option(help="点を付けた評価セット")] = "tests/eval/retrieval.csv",
    by: Annotated[str, typer.Option(help="human（人の点）か llm（LLM の点）")] = "human",
    settings: SettingsOpt = None,
) -> None:
    """評価セットで、やり方ごとの nDCG@10・Recall@20 を出す。"""
    from pathlib import Path

    from koten.eval.retrieval import evaluate, read_eval, report

    column = {"human": "grade", "llm": "llm_grade"}.get(by)
    if column is None:
        typer.secho("--by は human か llm です", fg="red", err=True)
        raise typer.Exit(2)
    p = _planner(_settings(settings))
    grades = read_eval(Path(file), column)
    if not grades:
        typer.secho(f"{file} の {column} 列に点の付いた行がありません", fg="red", err=True)
        raise typer.Exit(2)
    who = "人" if by == "human" else "LLM"
    total = sum(len(g) for g in grades.values())
    typer.echo(f"評価セット（{who}の点）：悩み {len(grades)} 件・点 {total} 件")
    typer.echo(report(evaluate(grades, _eval_runner(p), _eval_configs(p))))


@eval_app.command("grade")
def eval_grade(
    file: Annotated[str, typer.Option(help="評価セット")] = "tests/eval/retrieval.csv",
    redo: Annotated[bool, typer.Option(help="LLM の点がある行も判定し直す")] = False,
    worry: Annotated[
        list[str] | None, typer.Option(help="この悩みだけ判定する（くり返し指定できる）")
    ] = None,
    settings: SettingsOpt = None,
) -> None:
    """評価セットに、LLM（既定は Claude Code の Haiku）が「合う／合わない」を付ける。"""
    from pathlib import Path

    from koten.eval.grader import grade_file
    from koten.llm import LLMError, describe_stats, make_llm

    s = _settings(settings)
    if not Path(file).exists():
        _require_input(Path(file), "eval export")
    llm = make_llm(s.section("llm.grader"))
    typer.echo(f"grader：{llm.name}")
    try:
        r = grade_file(
            Path(file),
            llm,
            s.config_dir.parent / "prompts",
            s.section("eval").get("grader_prompt", "grader_v1"),
            redo=redo,
            worry_ids=set(worry) if worry else None,
            log=typer.echo,
        )
    except LLMError as e:
        typer.secho(f"{e}（ここまでの判定は保存しました）", fg="red", err=True)
        raise typer.Exit(1) from e
    typer.echo(f"判定 {r.graded} 件（悩み {r.worries} 件）、判定済みで飛ばした {r.skipped} 件")
    if r.missing:
        typer.secho(
            f"判定が返ってこなかった {len(r.missing)} 件（もう一度実行すると判定し直します）",
            fg="yellow",
        )
    stats = getattr(llm, "stats", {})
    if stats.get("calls"):
        typer.echo(
            f"呼び出し {stats['calls']} 回・合計 {stats['duration_ms'] / 1000:.0f}秒"
            + (
                f"・参考の料金 ${stats['cost_micro_usd'] / 1e6:.3f}"
                if stats["cost_micro_usd"]
                else ""
            )
        )
    elif line := describe_stats(stats):
        typer.echo(line)


@eval_app.command("agreement")
def eval_agreement(
    file: Annotated[str, typer.Option(help="評価セット")] = "tests/eval/retrieval.csv",
    show: Annotated[int, typer.Option(help="表示する食い違いの数")] = 10,
    settings: SettingsOpt = None,
) -> None:
    """人の点と LLM の点の一致（一致率・κ）と、食い違った組み合わせを出す。"""
    from pathlib import Path

    from koten.eval.grader import agreement

    a = agreement(Path(file))
    if a.n == 0:
        typer.secho("人の点と LLM の点が両方ある行がありません", fg="red", err=True)
        raise typer.Exit(2)

    def pct(x: float | None) -> str:
        return "—" if x is None else f"{x:.0%}"

    kappa = "—" if a.kappa is None else f"{a.kappa:.2f}"
    typer.echo(f"両方に点がある {a.n} 件：一致率 {a.accuracy:.0%}・κ {kappa}")
    typer.echo(
        f"  両方「合う」{a.both_yes}／両方「合わない」{a.both_no}／"
        f"人だけ「合う」{a.human_only}／LLMだけ「合う」{a.llm_only}"
    )
    typer.echo(
        f"  LLM が「合う」とした中で人も「合う」：{pct(a.llm_precision)}、"
        f"人が「合う」とした中で LLM も「合う」：{pct(a.llm_recall)}"
    )
    if a.kappa is not None:
        verdict = (
            "LLM の点を広い評価に使ってよい目安（κ 0.6 以上）"
            if a.kappa >= 0.6
            else "LLM の点だけで評価するのはまだ早い（κ 0.6 未満）。食い違いを見てください"
        )
        typer.echo(f"  → {verdict}")
    for r in a.disagreements[:show]:
        who = "人だけ「合う」" if r["grade"] in ("1", "2") else "LLMだけ「合う」"
        typer.echo(f"\n[{who}] {r['worry_id']} {r['worry_text']}")
        typer.echo(f"    {r['title']}「{r['quote_text'][:60]}」")
        typer.echo(f"    LLM の理由：{r['llm_reason']}")


@app.command()
def script(
    episode: Annotated[str, typer.Option(help="回の ID（例：E20261101-01）")],
    show: Annotated[
        bool, typer.Option(help="作らずに、保存済みの台本を差し込んだ形で出す")
    ] = False,
    force: Annotated[bool, typer.Option(help="script_done などの回も作り直す")] = False,
    settings: SettingsOpt = None,
) -> None:
    """台本JSONを作り、検証ゲート G1〜G6 を通す（planned → script_done、不合格なら error）。"""
    import json

    from koten.db import connect
    from koten.llm import LLMError, make_llm
    from koten.script import gates
    from koten.script.data import EpisodeData
    from koten.script.run import ScriptStateError, render, run_script
    from koten.script.writer import ScriptConfig, ScriptWriter
    from koten.worries.master import load_blocked_keywords

    s = _settings(settings)
    conn = connect(s.ssd_root)
    if show:
        row = conn.execute(
            "SELECT state, state_msg, script_json FROM episodes WHERE episode_id = ?", (episode,)
        ).fetchone()
        if row is None or not row["script_json"]:
            typer.secho(f"{episode} の台本はまだありません", fg="red", err=True)
            raise typer.Exit(2)
        data = EpisodeData.load(conn, s.ssd_root, episode)
        typer.echo(f"状態：{row['state']}（{row['state_msg']}）")
        for line in render(json.loads(row["script_json"]), gates.resolver_for(data)):
            typer.echo(line)
        return
    cfg = ScriptConfig.from_settings(s.section("script"), s.section("episode"))
    prompts = s.config_dir.parent / "prompts"
    writer = ScriptWriter(make_llm(s.section("llm.script")), prompts, cfg)
    judge = gates.FaithfulnessJudge(
        make_llm(s.section("llm.judge")), prompts, cfg.judge_prompt, cfg.judge_facts_prompt
    )
    rec = s.section("recency")
    try:
        r = run_script(
            conn,
            s.ssd_root,
            episode,
            writer,
            judge,
            load_blocked_keywords(s.config_dir),
            cfg,
            work_episodes=rec.get("work_episodes", 10),
            author_episodes=rec.get("author_episodes", 5),
            force=force,
            log=typer.echo,
        )
    except ScriptStateError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e
    except LLMError as e:
        typer.secho(f"{e}（回の状態は変えていません）", fg="red", err=True)
        raise typer.Exit(1) from e
    est = r.verification.get("length_estimate")
    if est:
        m, sec = divmod(est["est_sec"], 60)
        typer.echo(f"文字数 {est['chars']}（読み上げの見積もり 約{m}分{sec}秒）")
    for issue in (r.verification.get("viewing") or {}).get("issues", []):
        typer.secho(f"視聴の注意：{issue}", fg="yellow")
    if r.review_required:
        typer.secho("人の確認が要ります（確認画面で見る）", fg="yellow")
    if r.state != "script_done":
        raise typer.Exit(1)
    typer.echo(f"台本を見る：uv run koten script --episode {episode} --show")


facts_app = typer.Typer(no_args_is_help=True, help="背景の事実（作者の歩み・作品の成り立ち・時代）")
app.add_typer(facts_app, name="facts")


@facts_app.command("build")
def facts_build(settings: SettingsOpt = None) -> None:
    """HDD の書誌と config/facts.csv・fact_sources.csv から、SSD の事実の表を作り直す。"""
    from koten.db import connect
    from koten.facts.store import FactsError, build_facts
    from koten.worries.master import keyword_block, load_blocked_keywords

    s = _settings(settings)
    hdd = _hdd(s)
    categories = load_blocked_keywords(s.config_dir)
    try:
        r = build_facts(
            connect(s.ssd_root),
            hdd,
            s.config_dir,
            blocked=lambda text: keyword_block(text, categories),
        )
    except FactsError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e
    typer.echo(
        f"人物 {r.persons}・作品 {r.works}・自動の事実 {r.auto}・人が確かめた事実 {r.curated}"
    )


@facts_app.command("list")
def facts_list(
    subject: Annotated[
        str | None, typer.Option(help="作品ID か人物ID（例：aozora:001704、aozora:person:000280）")
    ] = None,
    episode: Annotated[str | None, typer.Option(help="回の企画に付いた事実")] = None,
    settings: SettingsOpt = None,
) -> None:
    """事実を一覧にする（状態・出典つき）。"""
    import json

    from koten.db import connect
    from koten.facts.select import canonical
    from koten.facts.store import load_facts, load_sources

    conn = connect(_settings(settings).ssd_root)
    if episode:
        row = conn.execute(
            "SELECT plan_json FROM episodes WHERE episode_id = ?", (episode,)
        ).fetchone()
        if row is None:
            typer.secho(f"{episode} という回はありません", fg="red", err=True)
            raise typer.Exit(2)
        plan = json.loads(row["plan_json"])
        ids = [f for c in plan["chapters"] for f in c.get("fact_ids", [])]
    elif subject:
        subjects = [subject, *canonical(conn, [subject])]
        marks = ",".join("?" for _ in subjects)
        ids = [
            r[0]
            for r in conn.execute(
                f"SELECT fact_id FROM facts WHERE subject_id IN ({marks}) ORDER BY fact_id",
                subjects,
            )
        ]
    else:
        typer.secho("--subject か --episode を指定してください", fg="red", err=True)
        raise typer.Exit(2)
    facts, sources = load_facts(conn, ids), load_sources(conn, ids)
    for fid in ids:
        f = facts.get(fid)
        if f is None:
            typer.echo(f"{fid}（表にありません。koten facts build を実行）")
            continue
        typer.echo(f"{fid}［{f.status}・{f.kind}］{f.text_ja}")
        for src in sources.get(fid, []):
            typer.echo(
                f"    {src['role']}：{src['title'] or src['kind']} {src['url'] or src['locator']}"
            )


@facts_app.command("coverage")
def facts_coverage(settings: SettingsOpt = None) -> None:
    """どれだけの作品・人物に事実があるか（人が確かめた事実と自動の事実）。"""
    from koten.db import connect

    conn = connect(_settings(settings).ssd_root)
    for label, origin in (("人が確かめた事実", "curated"), ("自動の事実", "auto")):
        rows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM facts WHERE origin = ? GROUP BY kind ORDER BY n DESC",
            (origin,),
        ).fetchall()
        total = sum(r["n"] for r in rows)
        kinds = "、".join(f"{r['kind']} {r['n']}" for r in rows) or "なし"
        typer.echo(f"{label} {total} 件（{kinds}）")
    works = conn.execute("SELECT COUNT(*) FROM works_meta").fetchone()[0]
    with_curated = conn.execute(
        "SELECT COUNT(DISTINCT subject_id) FROM facts "
        "WHERE origin = 'curated' AND subject_type = 'work'"
    ).fetchone()[0]
    typer.echo(f"作品 {works} のうち、人が確かめた事実のある作品 {with_curated}")


@facts_app.command("todo")
def facts_todo(
    episode: Annotated[str, typer.Option(help="回の ID")],
    settings: SettingsOpt = None,
) -> None:
    """回で読む作品・作者について、事実の数と、調べるときの手がかり（URL）を出す。"""
    import json
    from urllib.parse import quote

    from koten.db import connect
    from koten.facts.select import canonical

    conn = connect(_settings(settings).ssd_root)
    row = conn.execute("SELECT plan_json FROM episodes WHERE episode_id = ?", (episode,)).fetchone()
    if row is None:
        typer.secho(f"{episode} という回はありません", fg="red", err=True)
        raise typer.Exit(2)
    plan = json.loads(row["plan_json"])

    def count(subject: str) -> str:
        r = conn.execute(
            "SELECT SUM(origin = 'curated'), SUM(origin = 'auto') FROM facts WHERE subject_id = ?",
            (subject,),
        ).fetchone()
        return f"確かめた事実 {r[0] or 0}・自動 {r[1] or 0}"

    seen: set[str] = set()
    for sel in plan["selection"]:
        if sel["work_id"] in seen:
            continue
        seen.add(sel["work_id"])
        meta = conn.execute(
            "SELECT * FROM works_meta WHERE work_id = ?", (sel["work_id"],)
        ).fetchone()
        typer.echo(f"『{sel['title']}』（{sel['work_id']}）：{count(sel['work_id'])}")
        if meta and meta["url"]:
            typer.echo(f"    図書カード：{meta['url']}")
        for pid in canonical(conn, sel.get("author_ids") or []):
            person = conn.execute("SELECT name FROM persons WHERE person_id = ?", (pid,)).fetchone()
            name = person["name"] if person else pid
            typer.echo(f"    {name}（{pid}）：{count(pid)}")
            typer.echo(
                f"      手がかり：https://ja.wikipedia.org/wiki/Special:Search?search={quote(name)}"
            )
    typer.echo(f"下書きを作る：uv run koten facts draft --episode {episode}")


@facts_app.command("attach")
def facts_attach(
    episode: Annotated[str, typer.Option(help="回の ID")],
    force: Annotated[bool, typer.Option(help="script_done などの回にも付け直す")] = False,
    settings: SettingsOpt = None,
) -> None:
    """作った企画に、今の事実の表から背景の事実を付け直す（一節の選び方は変えない）。"""
    import json

    import yaml

    from koten.db import connect
    from koten.plan.planner import refresh_facts

    s = _settings(settings)
    conn = connect(s.ssd_root)
    row = conn.execute(
        "SELECT state, plan_json FROM episodes WHERE episode_id = ?", (episode,)
    ).fetchone()
    if row is None:
        typer.secho(f"{episode} という回はありません", fg="red", err=True)
        raise typer.Exit(2)
    if row["state"] not in ("planned", "error") and not force:
        typer.secho(
            f"{episode} は {row['state']} です（付け直すと台本と合わなくなるので、"
            "--force を付けて、あとで koten script --force で作り直す）",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)
    formats = yaml.safe_load((s.config_dir / "formats.yaml").read_text("utf-8"))["formats"]
    plan = refresh_facts(conn, json.loads(row["plan_json"]), formats)
    with conn:
        conn.execute(
            "UPDATE episodes SET plan_json = ? WHERE episode_id = ?",
            (json.dumps(plan, ensure_ascii=False), episode),
        )
    for c in plan["chapters"]:
        facts = "、".join(c["fact_ids"]) or "なし"
        typer.echo(f"  {c['chapter_id']} {c['role']}（{c['target_sec']}秒）背景の事実：{facts}")
    typer.echo(f"台本を作る：uv run koten script --episode {episode}{' --force' if force else ''}")


@facts_app.command("draft")
def facts_draft(
    episode: Annotated[str | None, typer.Option(help="回で読む作品と作者の下書きを作る")] = None,
    subject: Annotated[
        str | None, typer.Option(help="作品ID か人物ID（例：aozora:001704、aozora:person:000280）")
    ] = None,
    settings: SettingsOpt = None,
) -> None:
    """Wikidata とウィキペディアを手がかりに、AI が事実を下書きし、確かめ待ちの CSV に出す。"""
    import json
    from datetime import datetime

    from koten.db import connect
    from koten.facts.draft import (
        DraftConfig,
        config_fact_ids,
        draft_subject,
        person_subject,
        subjects_for_episode,
        work_subject,
        write_review,
    )
    from koten.llm import LLMError, make_llm

    s = _settings(settings)
    hdd = _hdd(s)
    conn = connect(s.ssd_root)
    if episode:
        row = conn.execute(
            "SELECT plan_json FROM episodes WHERE episode_id = ?", (episode,)
        ).fetchone()
        if row is None:
            typer.secho(f"{episode} という回はありません", fg="red", err=True)
            raise typer.Exit(2)
        subjects = subjects_for_episode(conn, json.loads(row["plan_json"]))
        name = episode
    elif subject:
        subjects = [
            person_subject(conn, subject) if ":person:" in subject else work_subject(conn, subject)
        ]
        subjects = [x for x in subjects if x.name]
        name = subject.replace(":", "_")
    else:
        typer.secho("--episode か --subject を指定してください", fg="red", err=True)
        raise typer.Exit(2)
    if not subjects:
        typer.secho("対象が書誌に見つかりません（koten facts build が先）", fg="red", err=True)
        raise typer.Exit(2)
    conf = s.section("facts")
    cfg = DraftConfig(**{k: v for k, v in conf.items() if k in DraftConfig.__dataclass_fields__})
    wikidata = _make_fetcher(s, hdd / "raw" / "wikidata")
    wikipedia = _make_fetcher(s, hdd / "raw" / "wikipedia")
    import httpx

    from koten.net import ApiError

    llm = make_llm(s.section("llm.facts"))
    review_dir = hdd / "exports" / "facts"
    taken = config_fact_ids(s.config_dir, review_dir)
    rows: list = []
    failed: list[str] = []
    for subj in subjects:
        typer.echo(f"{subj.name}（{subj.subject_id}）を調べています")
        try:
            rows += draft_subject(
                subj,
                wikidata=wikidata,
                wikipedia=wikipedia,
                llm=llm,
                prompts_dir=s.config_dir.parent / "prompts",
                cfg=cfg,
                taken_ids=taken,
                log=typer.echo,
            )
        except (LLMError, httpx.HTTPError, ApiError, KeyError, ValueError) as e:
            # 1人の失敗で、ほかの下書き（使った利用枠）を捨てない
            failed.append(subj.name)
            typer.secho(f"  {subj.name}：下書きできませんでした（{e}）", fg="red", err=True)
    if rows:
        out = review_dir / f"review_{name}_{datetime.now():%Y%m%d-%H%M}.csv"
        write_review(out, rows)
        typer.echo(f"下書き {len(rows)} 件：{out}")
    else:
        typer.echo("下書きはありません")
    typer.echo(
        "確かめ方：各行を、ウィキペディア以外の資料（本・国会図書館の書誌・青空文庫の本文など）で"
        "確かめ、confirm_kind・confirm_title・confirm_url か confirm_locator・confirm_license・"
        "checked_by を書き、使う行の accept に 1 を書く。"
        "そのあと koten facts accept --from で取り込む"
    )
    if failed:
        typer.secho(
            f"下書きできなかった対象：{'、'.join(failed)}（もう一度実行できます）", fg="red"
        )
        raise typer.Exit(1)


def _review_file(s: Settings, given: str | None):
    """確かめる下書きの CSV。指定が無ければ、いちばん新しい下書き。"""
    from pathlib import Path

    review_dir = _hdd(s) / "exports" / "facts"
    if given:
        path = Path(given)
        if not path.exists():
            typer.secho(f"ファイルが見つかりません：{path}", fg="red", err=True)
            files = sorted(review_dir.glob("review_*.csv"))
            if files:
                typer.echo("下書きの一覧：")
                for f in files[-5:]:
                    typer.echo(f"  {f}")
            raise typer.Exit(2)
        return path
    files = sorted(review_dir.glob("review_*.csv"), key=lambda f: f.stat().st_mtime)
    if not files:
        typer.secho(
            f"下書きがありません（{review_dir}）。koten facts draft が先", fg="red", err=True
        )
        raise typer.Exit(2)
    typer.echo(f"下書き：{files[-1]}")
    return files[-1]


@facts_app.command("review")
def facts_review(
    from_: Annotated[
        str | None, typer.Option("--from", help="下書きの CSV（省くと、いちばん新しいもの）")
    ] = None,
    settings: SettingsOpt = None,
) -> None:
    """下書きを1件ずつ見やすく出す（確かめるときに、手がかりと一緒に読む）。"""
    import csv

    s = _settings(settings)
    path = _review_file(s, from_)
    with path.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for i, r in enumerate(rows, start=1):
        mark = "取り込む" if (r.get("accept") or "").strip() else "まだ"
        typer.echo(f"\n[{i}] {r['fact_id']}（{r['kind']}・{r['scope']}）［{mark}］")
        typer.echo(f"  {r['text_ja']}")
        if r.get("problems"):
            typer.secho(f"  形の問題：{r['problems']}（このままでは取り込めない）", fg="yellow")
        if r.get("lead_ref"):
            typer.echo(f"  記事が挙げる出典：{r['lead_ref']}")
        if r.get("confirm_hint"):
            typer.echo(f"  確かめる手がかり：{r['confirm_hint']}")
        if r.get("confirm_kind"):
            where = r.get("confirm_url") or r.get("confirm_locator")
            typer.echo(f"  確かめた出典：{r['confirm_kind']} {r.get('confirm_title', '')} {where}")
    typer.echo(f"\n{len(rows)} 件。確かめた行の accept に 1 を書き、koten facts accept で取り込む")


@facts_app.command("accept")
def facts_accept(
    from_: Annotated[
        str | None,
        typer.Option("--from", help="確かめた下書きの CSV（省くと、いちばん新しいもの）"),
    ] = None,
    settings: SettingsOpt = None,
) -> None:
    """確かめた下書き（accept に印のある行）を config/facts.csv・fact_sources.csv に足す。"""
    from koten.db import connect
    from koten.facts.draft import accept_review
    from koten.facts.store import FactsError
    from koten.worries.master import keyword_block, load_blocked_keywords

    s = _settings(settings)
    path = _review_file(s, from_)
    conn = connect(s.ssd_root)
    known = {r[0] for r in conn.execute("SELECT person_id FROM persons")}
    known |= {r[0] for r in conn.execute("SELECT canonical_id FROM persons")}
    known |= {r[0] for r in conn.execute("SELECT work_id FROM works_meta")}
    categories = load_blocked_keywords(s.config_dir)
    try:
        n = accept_review(
            path,
            s.config_dir,
            known,
            blocked=lambda text: keyword_block(text, categories),
        )
    except FactsError as e:
        typer.secho(f"{e}\n（config の CSV は変えていません）", fg="red", err=True)
        raise typer.Exit(2) from e
    if n == 0:
        typer.secho(
            "accept に 1 の行がありません。下書きを確かめて、使う行の accept に 1 を書いてください",
            fg="yellow",
        )
        return
    typer.echo(
        f"{n} 件を config/facts.csv に足しました。uv run koten facts build で SSD に入れます"
    )


ref_app = typer.Typer(
    no_args_is_help=True, help="資料の台帳（時代の解釈・昔の辞書・古い訳）と、その本文"
)
app.add_typer(ref_app, name="ref")


def _registry(s: Settings):
    from koten.ref.registry import RefError, read_registry

    try:
        return read_registry(s.config_dir)
    except RefError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e


@ref_app.command("check")
def ref_check(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="判定の理由を全部出す")] = False,
    settings: SettingsOpt = None,
) -> None:
    """config/ref_works.csv・ref_people.csv を読み、1冊ずつ権利を判定する（何も書き込まない）。"""
    from collections import Counter

    works = _registry(_settings(settings))
    if not works:
        typer.echo("台帳（config/ref_works.csv）に資料がありません")
        return
    for w in works:
        mark = "使える" if w.usable else "使えない"
        review = ""
        if w.reviewed_license:
            review = f"（判定 {w.computed} → 人の確認 {w.reviewed_by} {w.reviewed_at}）"
        typer.secho(
            f"{w.ref_id}  {w.label()}  {w.license} {mark}{review}",
            fg="green" if w.usable else "yellow",
        )
        if verbose or not w.usable:
            typer.echo(f"    {w.computed_reason}")
        if w.computed == "NEEDS_REVIEW" and not w.reviewed_license:
            typer.echo(
                "    → 確かめたら ref_works.csv の reviewed_license に PD_JP か COPYRIGHTED と、"
                "reviewed_by・reviewed_at・review_note を書く"
            )
    count = Counter(str(w.license) for w in works)
    typer.echo("、".join(f"{k} {v}" for k, v in sorted(count.items())))


@ref_app.command("build")
def ref_build(settings: SettingsOpt = None) -> None:
    """台帳と、確かめた本文（renderings.csv・dict_entries.csv）と、取り込んだ古い英訳（HDD）から
    SSD の表を作り直す。"""
    from koten.db import connect
    from koten.ref.registry import RefError
    from koten.ref.standardebooks import editions, forget_unusable, load_imported
    from koten.ref.store import build_refs
    from koten.ref.texts import TextsError

    s = _settings(settings)
    works = {w.ref_id: w for w in _registry(s)}
    try:
        hdd = s.require_hdd()
    except HddNotConnectedError:
        # 辞書の語釈だけを直したときなど。取り込んだ英訳は SSD にあるものを残す
        hdd, imported = None, None
        typer.secho(
            "HDD が未接続なので、取り込んだ古い英訳は SSD にあるもの（使える資料の分）を残します。"
            "使えなくなった資料の HDD の本文は、HDD をつないで ref build をしたときに消します",
            fg="yellow",
        )
    if hdd is not None:
        removed = forget_unusable(hdd, works, editions(s.sources.get("standardebooks") or {}))
        for line in removed:
            typer.secho(f"使えない資料の、取り込んだ本文を消しました：{line}", fg="yellow")
        imported = load_imported(hdd)
    try:
        r = build_refs(connect(s.ssd_root), s.config_dir, imported)
    except (RefError, TextsError) as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e
    typer.echo(
        f"資料 {r.works}（使える {r.usable}）・時代の解釈 {r.renderings}・"
        f"取り込んだ本文 {r.imported}・辞書の語釈 {r.entries}"
    )


@ref_app.command("fetch")
def ref_fetch(
    ref: Annotated[
        list[str] | None, typer.Option(help="台帳の ref_id（くり返し指定できる。省くと全部）")
    ] = None,
    settings: SettingsOpt = None,
) -> None:
    """古い英訳（Standard Ebooks）を net.py で取り、一節ごとに分けて HDD に置く。"""
    import httpx

    from koten.ref.standardebooks import EditionError, editions, import_edition

    s = _settings(settings)
    hdd = _hdd(s)
    conf = s.sources.get("standardebooks") or {}
    eds = editions(conf)
    works = {w.ref_id: w for w in _registry(s)}
    targets = ref or list(eds)
    unknown = [t for t in targets if t not in eds]
    if unknown:
        typer.secho(
            f"sources.yaml の standardebooks.editions に無い ref_id：{'、'.join(unknown)}",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)
    fetcher = None
    failed = False
    for ref_id in targets:
        work = works.get(ref_id)
        if work is None:
            typer.secho(
                f"{ref_id}：台帳（config/ref_works.csv）にありません。"
                "先に書いて ref check で確かめる",
                fg="yellow",
            )
            failed = True
            continue
        if not work.usable:
            typer.secho(
                f"{ref_id}：{work.license}（{work.computed_reason}）。"
                "使えると判定されるまで取りません",
                fg="yellow",
            )
            failed = True
            continue
        fetcher = fetcher or _make_fetcher(s, hdd / "raw" / "standardebooks")
        try:
            r = import_edition(hdd, conf, eds[ref_id], work, fetcher, log=typer.echo)
        except (EditionError, httpx.HTTPError) as e:
            typer.secho(f"{ref_id}：{e}", fg="red", err=True)
            failed = True
            continue
        typer.echo(
            f"{ref_id}：ファイル {r.files}（今回取得 {r.fetched}）・一節 {r.units} → {r.path}"
        )
    if failed:
        raise typer.Exit(1)
    typer.echo("uv run koten ref build で SSD に入れます")


@ref_app.command("attach")
def ref_attach(
    episode: Annotated[str, typer.Option(help="回の ID")],
    chapter: Annotated[str, typer.Option(help="章の ID（例：ch2。koten plan の出力にある）")],
    ref_id: Annotated[
        list[str], typer.Option("--id", help="語釈（J-…）か解釈（I-…）の ID。くり返し指定できる")
    ],
    remove: Annotated[bool, typer.Option(help="付けるのではなく外す")] = False,
    force: Annotated[bool, typer.Option(help="script_done などの回にも付ける")] = False,
    settings: SettingsOpt = None,
) -> None:
    """企画の章に、昔の辞書の語釈・時代の解釈（確かめ済み）を付ける・外す。"""
    import json

    import yaml

    from koten.db import connect
    from koten.plan.planner import ref_sec
    from koten.ref.store import load_refs, ref_shas
    from koten.ref.texts import USABLE_STATUS

    s = _settings(settings)
    conn = connect(s.ssd_root)
    row = conn.execute(
        "SELECT state, plan_json FROM episodes WHERE episode_id = ?", (episode,)
    ).fetchone()
    if row is None:
        typer.secho(f"{episode} という回はありません", fg="red", err=True)
        raise typer.Exit(2)
    if row["state"] not in ("planned", "error") and not force:
        typer.secho(
            f"{episode} は {row['state']} です（付けると台本と合わなくなるので、"
            "--force を付けて、あとで koten script --force で作り直す）",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)
    plan = json.loads(row["plan_json"])
    ch = next((c for c in plan["chapters"] if c["chapter_id"] == chapter), None)
    if ch is None:
        ids = "、".join(c["chapter_id"] for c in plan["chapters"])
        typer.secho(f"{chapter} という章はありません（{ids}）", fg="red", err=True)
        raise typer.Exit(2)
    if not remove:
        found = load_refs(conn, ref_id)
        bad = [
            f"{rid}（{'無いか、使えない資料' if rid not in found else found[rid]['status']}）"
            for rid in ref_id
            if rid not in found or found[rid]["status"] != USABLE_STATUS
        ]
        if bad:
            typer.secho(
                f"付けられるのは、確かめ済み（verified）で使える資料の語釈・解釈だけです："
                f"{'、'.join(bad)}（koten ref build のあとか、ref show --all で状態を見る）",
                fg="red",
                err=True,
            )
            raise typer.Exit(2)
    formats = yaml.safe_load((s.config_dir / "formats.yaml").read_text("utf-8"))["formats"]
    spec = next((c for c in formats[plan["format"]]["chapters"] if c["role"] == ch["role"]), {})
    before = ref_sec(spec, ch)
    current = list(ch.get("ref_ids") or [])
    if remove:
        ch["ref_ids"] = [r for r in current if r not in ref_id]
    else:
        ch["ref_ids"] = current + [r for r in ref_id if r not in current]
    after = ref_sec(spec, ch)
    # 語釈・解釈が付いた分だけ、章の目安を伸ばす（外したら戻す）
    ch["target_sec"] = int(ch["target_sec"] + (sum(after) - sum(before)) / 2)
    plan["target_total_sec"] = sum(c["target_sec"] for c in plan["chapters"])
    plan["refs"] = ref_shas(conn, [r for c in plan["chapters"] for r in c.get("ref_ids", [])])
    plan["schema_version"] = "1.2"
    with conn:
        conn.execute(
            "UPDATE episodes SET plan_json = ? WHERE episode_id = ?",
            (json.dumps(plan, ensure_ascii=False), episode),
        )
    refs = "、".join(ch["ref_ids"]) or "なし"
    typer.echo(f"  {chapter} {ch['role']}（{ch['target_sec']}秒）語釈・解釈：{refs}")
    typer.echo(f"台本を作る：uv run koten script --episode {episode}{' --force' if force else ''}")


@ref_app.command("show")
def ref_show(
    passage: Annotated[str | None, typer.Option(help="一節の番号（例：lunyu:1.1）")] = None,
    word: Annotated[
        str | None, typer.Option(help="語（例：普通）。見出し語を含む語釈を出す")
    ] = None,
    all_status: Annotated[
        bool, typer.Option("--all", help="確かめ中（transcribed・imported）のものも出す")
    ] = False,
    settings: SettingsOpt = None,
) -> None:
    """SSD に入った時代の解釈・昔の辞書の語釈を読む（koten ref build のあと）。"""
    from koten.db import connect
    from koten.ref.store import entries_for_words, renderings_for_passage

    if not (passage or word):
        typer.secho("--passage か --word を付けてください", fg="red", err=True)
        raise typer.Exit(2)
    conn = connect(_settings(settings).ssd_root)
    rows = []
    if passage:
        rows += renderings_for_passage(conn, passage, verified_only=not all_status)
    if word:
        rows += entries_for_words(conn, word, verified_only=not all_status)
    if not rows:
        typer.echo("見つかりません（koten ref build のあとか、--all で確かめ中のものも見る）")
        return
    for r in rows:
        rid = r.get("rendering_id") or r.get("entry_id")
        head = r.get("passage_key") or f"{r['headword']}（{r['headword_kana']}）{r['sense'] or ''}"
        typer.secho(
            f"{rid}  {head}  [{r['status']}]", fg="green" if r["status"] == "verified" else "yellow"
        )
        typer.echo(f"    {r['display_text'] or r['text']}")
        if r.get("reading"):
            typer.echo(f"    読み：{r['reading']}")
        typer.echo(f"    出典：{r['ref_credit']}　{r['locator']}")


episode_app = typer.Typer(no_args_is_help=True, help="回（episodes）")
app.add_typer(episode_app, name="episode")


@episode_app.command("list")
def episode_list(
    top: Annotated[int, typer.Option(help="表示する件数（新しい順）")] = 20,
    settings: SettingsOpt = None,
) -> None:
    """回を新しい順に、状態と悩みと一緒に出す。"""
    import json

    from koten.db import connect

    conn = connect(_settings(settings).ssd_root)
    rows = conn.execute(
        "SELECT * FROM episodes ORDER BY created_at DESC, episode_id DESC LIMIT ?", (top,)
    ).fetchall()
    for r in rows:
        review = "　※要確認" if r["review_required"] else ""
        worries = "、".join(json.loads(r["worry_ids"]))
        typer.echo(f"{r['episode_id']} 形式{r['format']} {r['state']:11} {worries}{review}")
        if r["state_msg"]:
            typer.echo(f"    {r['state_msg'][:120]}")


@episode_app.command("discard")
def episode_discard(
    episode: Annotated[str, typer.Option(help="取り消す回の ID")],
    settings: SettingsOpt = None,
) -> None:
    """試しに作った回などを取り消す（discarded）。再使用間隔と形式の連続の数え方から外れる。"""
    from koten.db import connect

    conn = connect(_settings(settings).ssd_root)
    row = conn.execute("SELECT state FROM episodes WHERE episode_id = ?", (episode,)).fetchone()
    if row is None:
        typer.secho(f"{episode} という回はありません", fg="red", err=True)
        raise typer.Exit(2)
    if row["state"] == "published":
        typer.secho("公開済みの回は取り消せません", fg="red", err=True)
        raise typer.Exit(2)
    with conn:
        conn.execute(
            "UPDATE episodes SET state = 'discarded', state_msg = ? WHERE episode_id = ?",
            (f"取り消し（元の状態：{row['state']}）", episode),
        )
    typer.echo(f"{episode} を取り消しました（元の状態：{row['state']}）")


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
