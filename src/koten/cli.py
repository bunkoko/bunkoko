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
        typer.echo(f"\n{i:2}. {r['title']}（{'・'.join(r['author_names'])}）{r['section'] or ''}")
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
    """回の企画（plan_json）を作り、回を planned で保存する。"""
    import json

    from koten.plan.planner import PlanError

    p = _planner(_settings(settings))
    formats = [format.upper()] if format else p.allowed_formats()
    result, errors = None, []
    for fmt in formats:
        try:
            result = p.plan(fmt, worry_id=worry, theme=theme, work_id=work, force=force)
            break
        except PlanError as e:
            errors.append(f"形式{fmt}：{e}")
            typer.echo(f"  形式{fmt} は作れません：{e}")
    if result is None:
        typer.secho("企画を作れませんでした。この悩みは後回しにします", fg="red", err=True)
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
        typer.echo(f"  {c['chapter_id']} {c['role']}（{c['target_sec']}秒）{stats}{extra}")
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
    judge = gates.FaithfulnessJudge(make_llm(s.section("llm.judge")), prompts, cfg.judge_prompt)
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
    if r.review_required:
        typer.secho("人の確認が要ります（確認画面で見る）", fg="yellow")
    if r.state != "script_done":
        raise typer.Exit(1)
    typer.echo(f"台本を見る：uv run koten script --episode {episode} --show")


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
