"""ニュースを集めて、売買に役立つかを確かめる（./cfd news。設定と判定の条件は docs/news.md）。

  モデルを決める（先に）:
    ./cfd news setup       埋め込みの部品を入れる（初回は数分）
    ./cfd news compare     モデルの精度を比べ、判定に使うモデルを決める（生データ 160 ファイルで。価格は使わない。10〜20 分）
  データを取って確かめる（モデルを決めた後）:
    ./cfd news schedule    15 分ごとの自動の収集を入れる（--off で外す）
    ./cfd news backfill    GDELT の過去の分をまとめて取る（推移 2017 年〜・生データの見出し 2019-10〜・一覧 90 日。数時間）
    ./cfd news collect     1 回だけ集める（自動の収集が 15 分ごとに実行するもの）
    ./cfd news embed       見出しを埋め込む（終わった日の分だけ）
    ./cfd news report      仮説を確かめて output/news/<日時>/report.md に書く（埋め込みも先に行う）
    ./cfd news status      集めた量と、自動の収集が動いているか
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot.news import (BLOCK, GKG_FIRST, TIME_FMT, TIMELINE_MODES, TIMELINE_QUERIES, GdeltClient,  # noqa: E402
                         MarketDaily, NewsStore, Throttled, ann_sharpe, collect_articles, collect_gkg, collect_yahoo,
                         daily_timeline, flags_at, gkg_slots, hold_returns, shift_days, spikes, update_timelines,
                         values_at, vol_ratios)
from cfdbot.news_embed import COMPARE_MODELS  # noqa: E402
from cfdbot.stats import deflated_threshold  # noqa: E402

LABEL = "com.bunkoko.news"
TIMELINE_FIRST = pd.Timestamp("2017-01-01", tz="UTC")
PRICE_MARKETS = {"WTI": "CL=F", "BRENT": "BZ=F", "GOLD": "GC=F", "SILVER": "SI=F"}
COST = (0.0008, 0.03)            # 往復コスト・建玉の金利（年）。docs/universe.md の今の 4 商品と同じ
MIN_TRADING_DAYS = 250           # これより短い期間では判定しない（途中経過として表示だけ）
MIN_EVENTS = 10                  # 急増がこれより少なければ判定しない
PRIOR_TRIALS = 21                # これまでに試した数（docs/news.md）
SEED = 20261009
MIN_FREE_GB = 10                 # まとめての取得を始める前に要る空き容量（余裕を見て）


def now_utc() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").floor("s")


@contextlib.contextmanager
def file_lock(store: NewsStore, name: str, wait: bool):
    """同じ取得を同時に 2 つ動かさない（GDELT の API は重なると断られる。同じファイルに 2 つから書かない）。"""
    import fcntl

    p = store.root / f".{name}.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        f.close()


def keep_awake() -> None:
    """このコマンドが終わるまで Mac を眠らせない（画面は消えてよい。ノートはふたを閉じると眠る）。"""
    if sys.platform == "darwin":
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])


def free_gb(path: Path) -> float:
    p = Path(path).resolve()
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free / 1e9


# --------------------------------------------------------------------------- 集める
def cmd_collect(args) -> None:
    store, now = NewsStore(args.root), now_utc()
    msg = []
    quiet = lambda s: None  # noqa: E731
    if not args.no_yahoo:
        msg.append(f"Yahoo +{collect_yahoo(store, now)}")
    if not args.no_gkg:
        with file_lock(store, "gkg", wait=False) as ok:
            if not ok:
                msg.append("生データは別の取得中なので省いた")
            else:
                st = store.state()
                done = [pd.Timestamp(st[k]) for k in ("gkg_until", "gkg_backfill_until") if k in st]
                since = max(done) + pd.Timedelta(minutes=1) if done else now - pd.Timedelta(days=1)
                try:
                    added, n, missing = collect_gkg(store, since, now - pd.Timedelta(minutes=20), "gkg_until",
                                                    log=quiet)
                    msg.append(f"生データ +{added}（{n} ファイル）")
                except RuntimeError as e:
                    msg.append(str(e))
    if not args.no_gdelt:
        with file_lock(store, "gdelt", wait=False) as ok:
            if not ok:
                msg.append("GDELT は別の取得中なので省いた")
            else:
                client = GdeltClient(log=quiet)
                st = store.state()
                since = pd.Timestamp(st["live_until"]) - pd.Timedelta(minutes=15) if "live_until" in st \
                    else now - pd.Timedelta(hours=2)
                since = max(since, now - pd.Timedelta(days=80))
                try:
                    added, capped = collect_articles(client, store, since, now, pd.Timedelta(hours=3), "live_until",
                                                     log=quiet)
                    msg.append(f"GDELT +{added}" + (f"（上限 250 件に {capped} 回）" if capped else ""))
                    checked = pd.Timestamp(st["timeline_checked"]) if "timeline_checked" in st else None
                    if checked is None or now - checked > pd.Timedelta(hours=6):
                        tl = 0
                        for q in TIMELINE_QUERIES:
                            for mode in TIMELINE_MODES:
                                old = store.load_timeline(q.key, mode)
                                start = (old.index[-1].floor("D") - pd.Timedelta(days=2)) if len(old) \
                                    else now.floor("D") - pd.Timedelta(days=7)
                                tl += update_timelines(client, store, start, now, [q], [mode], log=quiet)
                        store.save_state(timeline_checked=now.strftime(TIME_FMT))
                        msg.append(f"推移 +{tl}")
                except Throttled as e:
                    msg.append(f"GDELT に断られた（{e}）。次の回に続きから取る")
    print(f"{now:%Y-%m-%d %H:%M} UTC 収集: " + "、".join(msg), flush=True)


def backfill_timelines(store: NewsStore, client: GdeltClient, first: pd.Timestamp, now: pd.Timestamp, log) -> None:
    log(f"\n== キーワードの推移: {first:%Y-%m}〜（3 か月ずつ。{len(TIMELINE_QUERIES) * len(TIMELINE_MODES)} 本。30 分ほど）")
    got = 0
    for q in TIMELINE_QUERIES:
        for mode in TIMELINE_MODES:
            old = store.load_timeline(q.key, mode)
            head_end = old.index[0] if len(old) else now
            if head_end - first > pd.Timedelta(days=2):
                got += update_timelines(client, store, first, head_end, [q], [mode], log=log)
            if len(old):
                got += update_timelines(client, store, old.index[-1].floor("D") - pd.Timedelta(days=2), now,
                                        [q], [mode], log=log)
            tl = store.load_timeline(q.key, mode)
            log(f"  {q.key} {mode}: " + (f"{tl.index[0]:%Y-%m-%d}〜{tl.index[-1]:%Y-%m-%d}（{len(tl)} 点）"
                                        if len(tl) else "無し"))
    store.save_state(timeline_checked=now.strftime(TIME_FMT))
    log(f"推移 +{got} 点")


def backfill_gkg(store: NewsStore, first: pd.Timestamp, now: pd.Timestamp, log) -> None:
    st = store.state()
    start = pd.Timestamp(st["gkg_backfill_until"]) + pd.Timedelta(minutes=1) if "gkg_backfill_until" in st else first
    n = len(gkg_slots(start, now))
    log(f"\n== GDELT の生データの見出し: {start:%Y-%m-%d}〜（{n:,} ファイル、ダウンロード 約 {n * 6 / 1000:.0f}GB。"
        "読んだら捨てるので保存は小さい。回線しだいで数時間。止めても続きから取れる）")
    if n:
        added, done, missing = collect_gkg(store, start, now - pd.Timedelta(minutes=20), "gkg_backfill_until", log=log)
        log(f"見出し +{added:,}（{done:,} ファイル。GDELT 側に無かったもの {missing}）")


def backfill_articles(store: NewsStore, client: GdeltClient, days: int, now: pd.Timestamp, log) -> None:
    st = store.state()
    start = (now - pd.Timedelta(days=days)).floor(BLOCK)
    if "backfill_until" in st and pd.Timestamp(st.get("backfill_from", now)) <= start + pd.Timedelta(days=7):
        start = max(start, pd.Timestamp(st["backfill_until"]))
    else:
        store.save_state(backfill_from=start.strftime(TIME_FMT))
    if start >= now:
        return
    n_req = int(np.ceil((now - start) / BLOCK)) * 4
    log(f"\n== GDELT の記事の一覧: {start:%Y-%m-%d}〜（{n_req} 回の問い合わせ、約 {n_req * 6 / 3600:.1f} 時間。"
        "止めても続きから取れる）")
    added, capped = collect_articles(client, store, start, now, BLOCK, "backfill_until", log=log)
    log(f"見出し +{added}（上限 250 件に {capped} 回達した。6 時間ごとに最新の 250 件までしか取れない）")


def cmd_backfill(args) -> None:
    store, now = NewsStore(args.root), now_utc()
    log = lambda s: print(s, flush=True)  # noqa: E731
    free = free_gb(store.root)
    log(f"空き容量: {free:.0f}GB（このコマンドで増えるのは 1GB ほど。埋め込みとモデルを合わせても 5GB ほど）")
    if free < MIN_FREE_GB:
        raise SystemExit(f"空きが {MIN_FREE_GB}GB より少ないので止めた。不要なファイルを消してからもう一度")
    keep_awake()
    problems = []
    if not args.no_timeline or not args.no_articles:
        with file_lock(store, "gdelt", wait=True):
            client = GdeltClient(log=log)
            try:
                if not args.no_timeline:
                    backfill_timelines(store, client, pd.Timestamp(args.timeline_since, tz="UTC"), now, log)
            except Throttled as e:
                problems.append(f"キーワードの推移: GDELT に断られ続けた（{e}）")
    if not args.no_gkg:
        with file_lock(store, "gkg", wait=True):
            try:
                backfill_gkg(store, pd.Timestamp(args.gkg_since, tz="UTC"), now_utc(), log)
            except RuntimeError as e:
                problems.append(f"生データ: {e}")
    if not args.no_articles:
        with file_lock(store, "gdelt", wait=True):
            client = GdeltClient(log=log)
            try:
                backfill_articles(store, client, args.days, now_utc(), log)
            except Throttled as e:
                problems.append(f"記事の一覧: GDELT に断られ続けた（{e}）")
    if problems:
        log("\n⚠ 取り切れなかったもの（時間をおいて ./cfd news backfill をもう一度。続きから取る）:")
        for p in problems:
            log(f"  - {p}")
    else:
        log("\n取り終わった。次は ./cfd news report")


# --------------------------------------------------------------------------- 埋め込み
def cmd_embed(args) -> None:
    from cfdbot.news_embed import embed_days, make_embedder

    store = NewsStore(args.root)
    args.model = args.model or chosen_model(store)
    emb = make_embedder(args.model)
    t0 = time.time()
    n = embed_days(store, emb, now_utc().floor("D"))
    dt = time.time() - t0
    print(f"埋め込み（{args.model}）: +{n} 件、{dt:.0f} 秒" + (f"（1 秒に {n / dt:.0f} 件）" if n and dt > 0 else ""))


def cmd_selftest(args) -> None:
    """モデルが正しく動くかの確かめ: 例の見出しが、思った話題の代表の文に一番近いか。"""
    from cfdbot.news_embed import anchor_vectors, make_embedder, topic_scores

    args.model = args.model or chosen_model(NewsStore(args.root))
    emb = make_embedder(args.model)
    dev = getattr(emb, "device", "cpu")
    examples = [("OPEC+ agrees to reduce crude output by 1 million barrels", "oil"),
                ("Brent jumps after drone strike on Saudi refinery", "geo"),
                ("Gold climbs to an all-time high on safe-haven buying", "gold"),
                ("Fed hikes rates by 25 basis points", "macro"),
                ("中東でミサイル攻撃、情勢が緊迫", "geo"),
                ("金相場が史上最高値", "gold")]
    anchors = anchor_vectors(emb)
    vecs = emb.encode([e[0] for e in examples])
    sc = topic_scores(vecs, anchors)
    ok = 0
    print(f"モデル {args.model}（{dev}、指示: {getattr(emb, 'prompt_name', None) or 'なし'}）")
    for i, (text, want) in enumerate(examples):
        best = max(sc, key=lambda k: sc[k][i])
        ok += best == want
        print(f"  {'○' if best == want else '×'} {text} → {best}（{sc[best][i]:.2f}）")
    t0 = time.time()
    emb.encode([f"Oil prices move as traders weigh supply outlook {i}" for i in range(1000)])
    print(f"正解 {ok}/{len(examples)}。速さ: 1,000 件で {time.time() - t0:.1f} 秒")


def choice_path(store: NewsStore) -> Path:
    return store.root / "model_choice.json"


def chosen_model(store: NewsStore) -> str:
    from cfdbot.news_embed import DEFAULT_MODEL

    p = choice_path(store)
    return json.loads(p.read_text(encoding="utf-8"))["model"] if p.exists() else DEFAULT_MODEL


COMPARE_FILES = 160          # 比べるために読む生データのファイルの数（1 ファイル 35 件ほど → 5,000 件ほど）


def fetch_compare_sample(store: NewsStore, n_files: int = COMPARE_FILES, seed: int = SEED,
                         log=print) -> int:
    """比べるための見出しを、2019-10〜今の生データのファイルから n_files 個（ばらばらの時刻）取る。

    まとめての取得（backfill）とは別の置き場（data/news/compare）。取ったファイルは飛ばすので、何度実行してもよい。
    """
    slots = gkg_slots(GKG_FIRST, now_utc() - pd.Timedelta(days=1))
    rng = np.random.default_rng(seed)
    pick = sorted(pd.Timestamp(slots[i]) for i in rng.choice(len(slots), min(n_files, len(slots)), replace=False))
    stats = store.load_gkg_stats()
    have = set(stats.index) if len(stats) else set()
    todo = [t for t in pick if t not in have]
    if todo:
        log(f"比べるための見出し: 生データ {len(todo)} ファイル（2019-10〜今からばらばらに。ダウンロード 約 {len(todo) * 6 / 1000:.1f}GB、"
            "読んだら捨てる。数分）")
    added = 0
    for t in todo:
        added += collect_gkg(store, t, t, "compare_last", log=lambda s: None)[0]
    return added


def compare_sample(store: NewsStore, n: int, seed: int = SEED) -> pd.DataFrame:
    """比べるための見出し（GDELT の生データ。テーマが付いているもの）を、全期間からばらばらに n 件。"""
    rng = np.random.default_rng(seed)
    days = store.days("gkg")
    pick = sorted(rng.choice(len(days), min(len(days), 400), replace=False)) if days else []
    parts = []
    for i in pick:
        d = days[i]
        h, a = store.load_day(d, "gkg"), store.load_gkg_articles(d)
        if len(h) and len(a):
            parts.append(h[["id", "title"]].merge(a[["id", "themes"]], on="id"))
    if not parts:
        return pd.DataFrame(columns=["id", "title", "themes"])
    df = pd.concat(parts, ignore_index=True).drop_duplicates("id")
    return df.sample(min(n, len(df)), random_state=seed).reset_index(drop=True)


def why_failed(e: BaseException) -> str:
    """モデルが使えなかった理由（よくあるものは、どうすればよいかも）。"""
    text = str(e) or type(e).__name__
    if "401" in text or "gated" in text.lower():
        return "Hugging Face へのログインが要る（利用規約に同意して hf auth login）"
    if "requires the PIL" in text or "Pillow" in text or "torchvision" in text or "Could not import module" in text:
        return "画像の部品（Pillow・torchvision）が無い → ./cfd update && ./cfd news setup"
    return text.splitlines()[0][:120]


def cmd_compare(args) -> None:
    """モデルの精度を比べる（価格は使わない）。標準より 0.02 以上良いモデルがあれば、判定に使うモデルを替える。"""
    from cfdbot.news_embed import (COMPARE_MARGIN, DEFAULT_MODEL, MODELS, THEME_KEYS, anchor_vectors, choose_model,
                                   judge_from, make_embedder, theme_labels, topic_aucs, topic_scores)

    keep_awake()
    store = NewsStore(args.root)
    cstore = NewsStore(Path(args.root) / "compare")
    fetch_compare_sample(cstore)
    sample = compare_sample(cstore, args.n)
    if len(sample) < 500:
        raise SystemExit(f"比べる見出しが足りない（{len(sample)} 件）。ネットにつないでもう一度 ./cfd news compare")
    labels = theme_labels(sample["themes"])
    lines: list[str] = []

    def say(t: str = "") -> None:
        print(t, flush=True)
        lines.append(t)

    say(f"# 埋め込みのモデルの比べ（{datetime.now():%Y-%m-%d %H:%M}）")
    say(f"\n見出し {len(sample):,} 件（GDELT の生データ 2019-10〜今からばらばらに）。答えは GDELT が本文から付けたテーマ:")
    for k, v in THEME_KEYS.items():
        say(f"- {k}: {int(labels[k].sum()):,} 件（{', '.join(sorted(v))}）")
    say("\n点数 = 話題ごとの AUC の平均（そのテーマの見出しほど、その話題の代表の文に近いと判定できたか。"
        "0.5 = でたらめ、1 = 完全）\n")
    say("| モデル | 公開 | 点数 | " + " | ".join(THEME_KEYS) + " | 1,000 件の時間 | 指示 |")
    say("|---|---|---|" + "---|" * len(THEME_KEYS) + "---|---|")
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    scores: dict[str, float] = {}
    for key in models + (["hash"] if "hash" not in models else []):
        spec = MODELS.get(key)
        if spec is None:
            say(f"| {key} | | 無いモデル | " + " | " * len(THEME_KEYS) + " |")
            continue
        try:
            emb = make_embedder(key)
            t0 = time.time()
            vecs = emb.encode(sample["title"].tolist())
            sec = (time.time() - t0) / len(sample) * 1000
            aucs = topic_aucs(topic_scores(vecs, anchor_vectors(emb)), labels)
        except (SystemExit, Exception) as e:  # noqa: BLE001  取れない・動かないモデルは飛ばす
            reason = why_failed(e)
            say(f"| {key} | {spec.released or '–'} | 使えなかった: {reason} | " + " | " * len(THEME_KEYS) + " |")
            continue
        score = float(np.nanmean(list(aucs.values())))
        scores[key] = score
        prompt = getattr(emb, "prompt_name", None) or ("query: " if spec.prefix else "なし")
        say(f"| {key} | {spec.released or '–'} | **{score:.3f}** | " + " | ".join(f"{aucs[k]:.3f}" for k in THEME_KEYS)
            + f" | {sec:.1f} 秒 | {prompt} |")
        if hasattr(emb, "release"):
            emb.release()
        del emb
    if not any(k != "hash" for k in scores):
        raise SystemExit("AI のモデルが 1 つも動かなかった（上の表の理由を見る）。部品が無ければ ./cfd news setup")
    pick = choose_model(scores)
    say(f"\n決まり（docs/news.md 7 章）: 標準の {DEFAULT_MODEL} より {COMPARE_MARGIN} 以上良いモデルがあれば、その中で一番良いもの。"
        "hash は比較用で選ばない")
    say(f"→ 判定に使うモデル: **{pick}**（判定の期間: {judge_from(pick):%Y-%m-%d}〜）")
    p = choice_path(store)
    if p.exists() and not args.force:
        old = chosen_model(store)
        say(f"\n※ 前に決めたモデル {old} のまま（決め直すのは、まだ ./cfd news report を見ていないときだけ。--force）")
    else:
        p.write_text(json.dumps({"model": pick, "scores": scores, "n": len(sample),
                                 "decided": now_utc().strftime(TIME_FMT)}, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"compare-{datetime.now():%Y%m%d-%H%M}.md"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n書き出し: {f}")
    print("次は ./cfd news schedule && ./cfd news backfill && ./cfd news report（データの取得と判定）")


# --------------------------------------------------------------------------- Mac の準備・自動の収集
def cmd_setup(args) -> None:
    """埋め込みの部品だけを入れる（モデルとデータは取らない）。"""
    py = ROOT / ".venv" / "bin" / "python"
    env = os.environ | {"UV_PYTHON_INSTALL_DIR": str(ROOT / ".python")}
    print("埋め込みの部品（sentence-transformers・PyTorch。初回は数分）", flush=True)
    subprocess.run(["uv", "pip", "install", "--python", str(py), "-e", ".[dev,research,news]", "-q"],
                   cwd=ROOT, env=env, check=True)
    subprocess.run([str(py), "-c", "import sentence_transformers, torch; "
                    "print('入った: sentence-transformers', sentence_transformers.__version__, '・PyTorch', "
                    "torch.__version__, '・GPU（MPS）', '使える' if torch.backends.mps.is_available() else '使えない')"],
                   cwd=ROOT, check=True)
    print("\n次は ./cfd news compare（モデルを比べて決める）")


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def make_plist(root: Path, log: Path, interval: int = 900) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(root / "cfd"), "news", "collect"],
        "WorkingDirectory": str(root),
        "StartInterval": interval,
        "RunAtLoad": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    }


def cmd_schedule(args) -> None:
    if sys.platform != "darwin":
        raise SystemExit("自動の収集は Mac だけ（launchd）。他では cron などで ./cfd news collect を 15 分ごとに")
    uid = os.getuid()
    p = plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    if args.off:
        p.unlink(missing_ok=True)
        print("自動の収集を外した")
        return
    protected = [Path.home() / d for d in ("Documents", "Desktop", "Downloads", "Library/Mobile Documents")]
    if any(ROOT.is_relative_to(d) for d in protected):
        print(f"⚠ {ROOT} は「書類・デスクトップ・ダウンロード・iCloud」の中にある。macOS が自動の実行からの読み書きを"
              "止めることがある。./cfd news status で収集が進まなければ、フォルダをホーム直下に移す")
    log = Path(args.root).resolve() / "collect.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        plistlib.dump(make_plist(ROOT, log), f)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(p)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"launchctl に断られた: {r.stderr.strip()}")
    print(f"15 分ごとに ./cfd news collect を実行する（記録: {log}）。Mac がスリープ中は止まり、起きたら続きから取る")


def dir_size_gb(path: Path) -> float:
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file()) / 1e9 if Path(path).exists() else 0.0


def cmd_status(args) -> None:
    store = NewsStore(args.root)
    print(f"空き容量: {free_gb(store.root):.0f}GB（ニュースのデータが使っている量: {dir_size_gb(store.root):.2f}GB）")
    days = store.days()
    if not days:
        print("まだ見出しが無い。./cfd news collect か ./cfd news backfill を実行する")
    else:
        print(f"見出し: {days[0]:%Y-%m-%d}〜{days[-1]:%Y-%m-%d}（{len(days)} 日）")
        print("直近の日ごとの件数（生データ / GDELT の一覧 / Yahoo）:")
        for d in days[-10:]:
            c = store.counts(d)
            print(f"  {d:%Y-%m-%d}  {c.get('gkg', 0):5d} / {c.get('gdelt', 0):5d} / {c.get('yahoo', 0):4d}")
    st = store.state()
    for k, label in (("gkg_until", "毎回の収集（生データ）"), ("gkg_backfill_until", "まとめての取得（生データ）"),
                     ("live_until", "毎回の収集（GDELT の一覧）"), ("backfill_until", "まとめての取得（GDELT の一覧）"),
                     ("timeline_checked", "推移の確認")):
        if k in st:
            age = now_utc() - pd.Timestamp(st[k])
            print(f"{label}: {pd.Timestamp(st[k]):%Y-%m-%d %H:%M} UTC まで（{age.total_seconds() / 3600:.1f} 時間前）")
    for q in TIMELINE_QUERIES:
        tl = store.load_timeline(q.key, "timelinevolraw")
        if len(tl):
            print(f"推移 {q.key}: {tl.index[0]:%Y-%m-%d}〜{tl.index[-1]:%Y-%m-%d}")
    emb_root = store.root / "emb"
    if emb_root.exists():
        for m in sorted(p.name for p in emb_root.iterdir() if p.is_dir()):
            print(f"埋め込み {m}: {len(list((emb_root / m).glob('*/*.npz')))} 日")
    if sys.platform == "darwin":
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
        print("自動の収集: " + ("入っている" if r.returncode == 0 else "入っていない（./cfd news schedule）"))
        log = store.root / "collect.log"
        last = log.read_text(encoding="utf-8", errors="replace").strip().splitlines() if log.exists() else []
        if last:
            print("最後の記録: " + last[-1])


# --------------------------------------------------------------------------- 検証
def load_markets(args) -> dict[str, MarketDaily]:
    from cfdbot.context import fetch_yahoo
    from cfdbot.leadlag import read_yahoo

    out = {}
    for key, ticker in PRICE_MARKETS.items():
        p = Path(args.universe) / f"{key}.csv"
        fresh = p.exists() and (now_utc().tz_localize(None) - pd.Timestamp(read_yahoo(p)[0].index[-1])).days <= 4
        if not fresh and not args.no_fetch:
            try:
                fetch_yahoo(ticker, "2000-01-01").to_csv(p, index=False)
            except Exception as e:  # noqa: BLE001
                print(f"⚠ {key} の価格を取れなかった（{e}）。手元のデータで続ける"[:200])
        if p.exists():
            s, tz = read_yahoo(p)
            out[key] = MarketDaily.from_close(key, s, tz)
    return out


class Evaluator:
    """1 つの仮説の数字と、日の並びをずらした偶然との比較。"""

    def __init__(self, markets: dict[str, MarketDaily], placebo: int):
        self.markets, self.placebo = markets, placebo
        self.rng = np.random.default_rng(SEED)

    def _shifts(self, n: int) -> np.ndarray:
        lo = min(30, n // 4)
        return self.rng.integers(lo, max(lo + 1, n - lo), self.placebo) if n > 8 else np.array([], int)

    @staticmethod
    def _halves(dates: pd.DatetimeIndex, first: pd.Timestamp) -> pd.Timestamp:
        d = dates[dates >= first]
        return d[len(d) // 2] if len(d) else first

    @staticmethod
    def _first(days: pd.DatetimeIndex, start: pd.Timestamp | None) -> pd.Timestamp:
        f = days[0] + pd.Timedelta(days=1)
        return max(f, start) if start is not None else f

    def vol(self, days: pd.DatetimeIndex, flags_by_market: dict[str, np.ndarray],
            start: pd.Timestamp | None = None) -> dict:
        """急増（印）の翌日の |値動き| ÷ ふだんの値動き（市場をまとめた平均）。start より前の判断は数えない。"""
        first = self._first(days, start)

        def ratios(fl_by: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
            xs, ds = [], []
            for k, fl in fl_by.items():
                m = self.markets[k]
                f = flags_at(days, fl, m.decide) & (m.dates >= first.tz_localize(None))
                xs.append(vol_ratios(m, f))
                ds.append(m.dates[f & np.isfinite(np.abs(m.next_ret) / m.norm_abs)])
            return (np.concatenate(xs) if xs else np.zeros(0)), (np.concatenate(ds) if ds else np.zeros(0, "M8[ns]"))

        x, d = ratios(flags_by_market)
        mkt = next(iter(self.markets.values()))
        span = mkt.dates[mkt.dates >= first.tz_localize(None)]
        mid = self._halves(span, span[0]) if len(span) else None
        h1 = x[d < np.datetime64(mid)] if mid is not None else x
        h2 = x[d >= np.datetime64(mid)] if mid is not None else x[:0]
        null = [ratios({k: shift_days(v, s) for k, v in flags_by_market.items()})[0] for s in self._shifts(len(days))]
        null = np.array([v.mean() for v in null if len(v)])
        val = float(x.mean()) if len(x) else float("nan")
        return {"n": len(x), "value": val, "h1": float(h1.mean()) if len(h1) else np.nan,
                "h2": float(h2.mean()) if len(h2) else np.nan,
                "pct": float(np.mean(null < val)) if len(null) and np.isfinite(val) else None,
                "trading_days": len(span), "mid": mid}

    def direction(self, days: pd.DatetimeIndex, signal_days_by_market: dict[str, np.ndarray], h: int,
                  kind: str, start: pd.Timestamp | None = None) -> dict:
        """日ごとの向き（kind="value": 値をそのまま、"flag": 印の日に買い）で h 日持った成績（市場の平均）。"""
        first = self._first(days, start).tz_localize(None)

        def returns(sig_by: dict[str, np.ndarray]) -> pd.Series:
            parts = []
            for k, sd in sig_by.items():
                m = self.markets[k]
                s = values_at(days, sd, m.decide) if kind == "value" else flags_at(days, sd, m.decide).astype(float)
                r = hold_returns(m, s, h, COST)
                parts.append(r[r.index >= first])
            return pd.concat(parts, axis=1, sort=True).mean(axis=1) if parts else pd.Series(dtype=float)

        r = returns(signal_days_by_market)
        mid = self._halves(r.index, r.index[0]) if len(r) else None
        null = np.array([ann_sharpe(returns({k: shift_days(v, s) for k, v in signal_days_by_market.items()}))
                         for s in self._shifts(len(days))])
        null = null[np.isfinite(null)]
        val = ann_sharpe(r)
        return {"n": int((np.abs(r) > 0).sum()), "value": val,
                "h1": ann_sharpe(r[r.index < mid]) if mid is not None else np.nan,
                "h2": ann_sharpe(r[r.index >= mid]) if mid is not None else np.nan,
                "pct": float(np.mean(null < val)) if len(null) and np.isfinite(val) else None,
                "threshold": deflated_threshold(null, 4) if len(null) > 2 else np.nan,
                "trading_days": len(r), "mid": mid}


def vol_verdict(r: dict) -> str:
    if r["trading_days"] < MIN_TRADING_DAYS or r["n"] < MIN_EVENTS:
        return f"途中経過（{r['trading_days']} 営業日・急増 {r['n']} 回。判定は {MIN_TRADING_DAYS} 営業日・{MIN_EVENTS} 回から）"
    ok = r["value"] > 1 and r["h1"] > 1 and r["h2"] > 1 and (r["pct"] or 0) >= 0.95
    return "有望" if ok else "偶然の範囲"


def dir_verdict(r: dict) -> str:
    if r["trading_days"] < MIN_TRADING_DAYS:
        return f"途中経過（{r['trading_days']} 営業日。判定は {MIN_TRADING_DAYS} 営業日から）"
    ok = r["h1"] > 0 and r["h2"] > 0 and (r["pct"] or 0) >= 0.95 and r["value"] > r["threshold"]
    return "有望" if ok else "偶然の範囲"


def fmt(v, d: int = 2) -> str:
    return "–" if v is None or not np.isfinite(v) else f"{v:.{d}f}"


def pct(v) -> str:
    return "–" if v is None else f"{v:.0%}"


def gate_test(args, flags_by_symbol: dict[str, tuple[pd.DatetimeIndex, np.ndarray]], say,
              since: pd.Timestamp | None = None) -> dict | None:
    """今のタートルで、急増の翌日（最初の判断）は新規に入らない。期間B（フィリップ）の、ニュースのある期間
    （since 以降）で比べる。"""
    from cfdbot.features import decide_ns
    from cfdbot.study import build_setup, summarize
    from cfdbot.universe import day_returns

    try:
        setup = build_setup(args.final, args.config, args.context, 10_000_000, "2001-01-01", "2021-06-01")
    except (SystemExit, Exception) as e:  # noqa: BLE001
        say(f"- バックテストのデータが無いので省いた（{str(e)[:120]}）")
        return None
    per = setup.period_b
    starts = [d[0] + pd.Timedelta(days=1) for d, _ in flags_by_symbol.values() if len(d)]
    if not starts:
        return None
    start = max([min(starts), per.start] + ([since] if since is not None else []))
    gates, masks = {}, {}
    for s, frame in per.frames.items():
        if s not in flags_by_symbol:
            continue
        days, fl = flags_by_symbol[s]
        t = decide_ns(frame.index, per.tfs[s])
        blocked = flags_at(days, fl, t)
        gates[s] = pd.DataFrame({"long": ~blocked, "short": ~blocked}, index=frame.index)
        masks[s] = frame.index >= start
    base = per.run(setup.picks)
    test = per.run(setup.picks, gates)

    def stats(res) -> tuple[float, float, float]:
        r = day_returns(res["equity"], start)
        mid = r.index[len(r) // 2] if len(r) else None
        return ann_sharpe(r), ann_sharpe(r[r.index < mid]), ann_sharpe(r[r.index >= mid])

    b, g = stats(base), stats(test)
    rng = np.random.default_rng(SEED)
    null = []
    for _ in range(args.gate_placebo):
        sh = {}
        for s, gt in gates.items():
            m = masks[s]
            k = int(rng.integers(1, max(2, m.sum())))
            col = gt["long"].to_numpy().copy()
            col[m] = np.roll(col[m], k)
            sh[s] = pd.DataFrame({"long": col, "short": col}, index=gt.index)
        null.append(stats(per.run(setup.picks, sh))[0] - b[0])
    null = np.array([v for v in null if np.isfinite(v)])
    d = g[0] - b[0]
    n_block = int(sum((~gt["long"]).sum() for gt in gates.values()))
    trades = summarize(test, 10_000_000, list(per.frames))["trades"]
    r = day_returns(base["equity"], start)
    return {"base": b, "test": g, "pct": float(np.mean(null < d)) if len(null) else None, "blocked": n_block,
            "trades": trades, "trading_days": len(r), "start": start}


def gate_verdict(r: dict) -> str:
    if r["trading_days"] < MIN_TRADING_DAYS:
        return f"途中経過（{r['trading_days']} 営業日。判定は {MIN_TRADING_DAYS} 営業日から）"
    ok = r["test"][1] > r["base"][1] and r["test"][2] > r["base"][2] and (r["pct"] or 0) >= 0.95
    return "有望" if ok else "偶然の範囲"


def cmd_report(args) -> None:
    keep_awake()
    store = NewsStore(args.root)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    markets = load_markets(args)
    if len(markets) < 4:
        raise SystemExit("価格のデータが足りない（data/universe）。ネットにつないで ./cfd news report をやり直す")
    ev = Evaluator(markets, args.placebo)
    today = now_utc().floor("D")
    say(f"# ニュースの検証（{datetime.now():%Y-%m-%d %H:%M}）")
    say("\n判定の条件は docs/news.md（結果を見る前に決めたもの）。偶然 = ニュースの日の並びをずらした同じ計算"
        f"（{args.placebo} 回）。前半・後半 = 対象の期間を半分に分けたもの")
    days = store.days()
    if days:
        n = {"gkg": 0, "gdelt": 0, "yahoo": 0}
        for d in days:
            for k, v in store.counts(d).items():
                n[k] = n.get(k, 0) + v
        say(f"\n- 見出し: {days[0]:%Y-%m-%d}〜{days[-1]:%Y-%m-%d}（{len(days)} 日。GDELT の生データ {n['gkg']:,}・"
            f"GDELT の一覧 {n['gdelt']:,}・Yahoo {n['yahoo']:,} 件）")

    # ---------------- A. キーワードで数える（AI なし）
    say("\n## A. キーワードで数える量と論調（AI なし。docs/news.md 3 章）\n")
    tl = {q.key: daily_timeline(store.load_timeline(q.key, "timelinevolraw"), store.load_timeline(q.key, "timelinetone"))
          for q in TIMELINE_QUERIES}
    tl = {k: v[v.index < today] for k, v in tl.items() if len(v)}
    for k, v in tl.items():
        say(f"- 推移 {k}: {v.index[0]:%Y-%m-%d}〜{v.index[-1]:%Y-%m-%d}（{int(v.notna().any(axis=1).sum())} 日）")
    rows, gate_flags = [], {}
    if "geo" in tl and "share" in tl["geo"]:
        geo = tl["geo"]
        sp = spikes(geo["share"]).to_numpy()
        say(f"- 地政学のニュースの急増: {int(sp.sum())} 日")
        r = ev.vol(geo.index, {k: sp for k in ("WTI", "BRENT", "GOLD")})
        rows.append(("geo_vol", "地政学の急増 → 翌日の値動きの大きさ（÷ふだん）", r, vol_verdict(r)))
        r = ev.direction(geo.index, {k: sp for k in ("GOLD", "SILVER")}, 5, "flag")
        rows.append(("gold_haven", "地政学の急増 → 金・銀を 5 日買う（シャープ）", r, dir_verdict(r)))
        gate_flags = {s: (geo.index, sp) for s in ("GOLD", "SILVER", "WTI", "BRENT")}
    if "oil_macro" in tl and "tone" in tl["oil_macro"]:
        tone = tl["oil_macro"]["tone"]
        t5 = tone.rolling(5, min_periods=3).mean()
        sig = np.sign(t5 - t5.shift(5)).to_numpy()
        r = ev.direction(tone.index, {k: sig for k in ("WTI", "BRENT")}, 5, "value")
        rows.append(("macro_tone", "原油の需給・景気の論調が明るくなった → 原油を 5 日買う（暗くなったら売る）", r,
                     dir_verdict(r)))
    if rows:
        say("\n| 仮説 | 内容 | 回数 | 値 | 前半 | 後半 | 偶然より良い割合 | 判定 |")
        say("|---|---|---|---|---|---|---|---|")
        for name, label, r, v in rows:
            say(f"| {name} | {label} | {r['n']} | {fmt(r['value'])} | {fmt(r['h1'])} | {fmt(r['h2'])} | {pct(r['pct'])} | {v} |")
    else:
        say("- キーワードの推移がまだ無い（./cfd news backfill で取る）")
    if gate_flags and not args.no_gate:
        g = gate_test(args, gate_flags, say)
        if g:
            say(f"\nnews_gate（今のタートルで、地政学の急増の翌日は新規に入らない。期間B の {g['start']:%Y-%m-%d}〜）: "
                f"シャープ {fmt(g['base'][0])} → {fmt(g['test'][0])}（前半 {fmt(g['base'][1])} → {fmt(g['test'][1])}、"
                f"後半 {fmt(g['base'][2])} → {fmt(g['test'][2])}）、止めた足 {g['blocked']}、偶然より良い割合 {pct(g['pct'])}"
                f" → {gate_verdict(g)}")

    # ---------------- B. 埋め込み
    say("\n## B. 埋め込みで数える話題の強さ・珍しさ（docs/news.md 7 章）\n")
    from cfdbot.news_embed import MODELS, anchor_vectors, daily_features, embed_days, load_day_vectors, make_embedder

    from cfdbot.news_embed import judge_from

    model = args.model or chosen_model(store)
    judge = judge_from(model)
    say(f"判定に使うモデル: {model}（{'./cfd news compare で選んだもの' if choice_path(store).exists() else '標準'}）。"
        f"判定の期間: {judge:%Y-%m-%d}〜")
    for key in [model] + ([] if model == "hash" else ["hash"]):
        try:
            emb = make_embedder(key)
        except (SystemExit, Exception) as e:  # noqa: BLE001  部品が無い・モデルを取れない
            say(f"### {key}\n\n- 使えなかった: {str(e)[:200]}\n")
            continue
        n = embed_days(store, emb, today)
        feats = daily_features(load_day_vectors(store, key, today), anchor_vectors(emb))
        role = "（AI を使わない比較用。判定はしない）" if key == "hash" else "（判定に使うモデル）"
        say(f"### {key} {role}\n\n- {MODELS[key].note}。今回埋め込んだ見出し +{n:,}")
        if feats.empty or "share_geo" not in feats or feats["share_geo"].notna().sum() == 0:
            say("- 特徴を作れる日がまだ無い（見出しが 20 日分以上要る。./cfd news backfill）\n")
            continue
        say(f"- 特徴: {feats['share_geo'].first_valid_index():%Y-%m-%d}〜{feats.index[-1]:%Y-%m-%d}"
            f"（GDELT の生データの見出し、1 日平均 {feats['n'].mean():.0f} 件）")
        sp_geo = spikes(feats["share_geo"]).to_numpy()
        sp_oil = spikes(feats["novelty_oil"]).to_numpy() if "novelty_oil" in feats else np.zeros(len(feats), bool)
        sp_gold = spikes(feats["novelty_gold"]).to_numpy() if "novelty_gold" in feats else np.zeros(len(feats), bool)
        say(f"- 急増（全期間）: 地政学 {int(sp_geo.sum())} 日・原油の珍しさ {int(sp_oil.sum())} 日・"
            f"金の珍しさ {int(sp_gold.sum())} 日")
        say("\n| 仮説 | 期間 | 内容 | 回数 | 値 | 前半 | 後半 | 偶然より良い割合 | 判定 |")
        say("|---|---|---|---|---|---|---|---|---|")
        for label_w, since in ((f"判定 {judge:%Y-%m}〜", judge), ("参考 全期間", None)):
            rows = []
            r = ev.vol(feats.index, {k: sp_geo for k in ("WTI", "BRENT", "GOLD")}, since)
            rows.append(("emb_geo_vol", "地政学の見出しの割合の急増 → 翌日の値動きの大きさ（÷ふだん）", r, vol_verdict(r)))
            r = ev.vol(feats.index, {"WTI": sp_oil, "BRENT": sp_oil, "GOLD": sp_gold, "SILVER": sp_gold}, since)
            rows.append(("novelty_vol", "原油（金）の見出しの珍しさの急増 → 翌日の値動きの大きさ", r, vol_verdict(r)))
            for name, label, r, v in rows:
                v = "参考" if key == "hash" or since is None else v
                say(f"| {name} | {label_w} | {label} | {r['n']} | {fmt(r['value'])} | {fmt(r['h1'])} | {fmt(r['h2'])} "
                    f"| {pct(r['pct'])} | {v} |")
        if key != "hash" and not args.no_gate:
            fl = {"WTI": sp_geo | sp_oil, "BRENT": sp_geo | sp_oil, "GOLD": sp_geo | sp_gold, "SILVER": sp_geo | sp_gold}
            g = gate_test(args, {s: (feats.index, f) for s, f in fl.items()}, say, judge)
            if g:
                say(f"\nemb_gate（今のタートルで、上の急増の翌日は新規に入らない。{g['start']:%Y-%m-%d}〜）: "
                    f"シャープ {fmt(g['base'][0])} → {fmt(g['test'][0])}（前半 {fmt(g['base'][1])} → "
                    f"{fmt(g['test'][1])}、後半 {fmt(g['base'][2])} → {fmt(g['test'][2])}）、止めた足 {g['blocked']}、"
                    f"偶然より良い割合 {pct(g['pct'])} → {gate_verdict(g)}")
        say("")
    say(f"試した数: これまでの {PRIOR_TRIALS} ＋ キーワード 4 ＋ 埋め込み 3")
    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n書き出し: {out / 'report.md'}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default="data/news", help="保存先")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="1 回だけ集める")
    c.add_argument("--no-gdelt", action="store_true", help="GDELT の API（記事の一覧・推移）を省く")
    c.add_argument("--no-gkg", action="store_true", help="GDELT の生データを省く")
    c.add_argument("--no-yahoo", action="store_true")
    b = sub.add_parser("backfill", help="GDELT の過去の分をまとめて取る")
    b.add_argument("--days", type=int, default=90, help="見出しをさかのぼる日数（GDELT は 3 か月ほどまで）")
    b.add_argument("--timeline-since", default=str(TIMELINE_FIRST.date()), help="キーワードの推移の開始日")
    b.add_argument("--gkg-since", default=str(GKG_FIRST.date()), help="生データの見出しの開始日（2019-10 から入っている）")
    b.add_argument("--no-articles", action="store_true")
    b.add_argument("--no-timeline", action="store_true")
    b.add_argument("--no-gkg", action="store_true")
    sub.add_parser("setup", help="埋め込みの部品を入れる（モデルとデータは取らない）")
    for name in ("embed", "selftest"):
        s = sub.add_parser(name)
        s.add_argument("--model", default=None, help="埋め込みのモデル（省略時は ./cfd news compare で選んだもの）")
    s = sub.add_parser("schedule", help="15 分ごとの自動の収集を入れる")
    s.add_argument("--off", action="store_true", help="外す")
    sub.add_parser("status")
    c2 = sub.add_parser("compare", help="埋め込みのモデルの精度を比べ、判定に使うモデルを決める（価格は使わない）")
    c2.add_argument("--models", default=",".join(COMPARE_MODELS), help="比べるモデル（カンマ区切り。hash は必ず入る）")
    c2.add_argument("--n", type=int, default=5000, help="比べる見出しの数")
    c2.add_argument("--force", action="store_true", help="前に決めたモデルを決め直す（report を見る前だけ）")
    c2.add_argument("--out", default="output/news")
    r = sub.add_parser("report")
    r.add_argument("--model", default=None, help="判定に使う埋め込みのモデル（省略時は ./cfd news compare で選んだもの）")
    r.add_argument("--placebo", type=int, default=200)
    r.add_argument("--gate-placebo", type=int, default=30)
    r.add_argument("--no-gate", action="store_true", help="バックテスト（news_gate・emb_gate）を省く")
    r.add_argument("--no-fetch", action="store_true", help="価格を取りに行かない")
    r.add_argument("--universe", default="data/universe", help="価格（Yahoo の日足）の置き場")
    r.add_argument("--final", default="config/baselines/turtle55_d1_ext.json", help="今の構成")
    r.add_argument("--config", default="config/train.toml")
    r.add_argument("--context", default="data/context")
    r.add_argument("--out", default="output/news")
    args = p.parse_args()
    {"collect": cmd_collect, "backfill": cmd_backfill, "embed": cmd_embed, "selftest": cmd_selftest,
     "compare": cmd_compare,
     "setup": cmd_setup, "schedule": cmd_schedule, "status": cmd_status, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
