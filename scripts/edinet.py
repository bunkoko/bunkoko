"""EDINET（金融庁）の有価証券報告書の本文と、文章の信号の検証（./cfd edinet）。決まりは docs/text_signals.md。

    python scripts/edinet.py key        # API キーを保存する（入力を求める。~/.bunkoko/edinet.json、権限 600。このリポジトリの外）
    python scripts/edinet.py check      # 何年分さかのぼれるか・本文の 3 つの節が取れるかを確かめて日本語で報告する（数分。呼び出し 20〜40 回）
    python scripts/edinet.py fetch      # 今の TOPIX500 の 490 社の有報（10 年分）の本文の節を集める（一覧 約 1.5 時間・有報 約 4 時間。止めても続きから）
    python scripts/edinet.py status     # 集めた量
    python scripts/edinet.py momentum   # 1C 文章で近い会社のモメンタム（月 1 回）を確かめる（埋め込みは初回 数十分）
    python scripts/edinet.py lazy       # 案 2 有報の文面の前年との差（月 1 回）を確かめる（数分）

キーは EDINET のサイトでアカウントを作り、API キーの発行の画面で出す（登録は利用者が行う。docs/todo.md）。
取った本文は data/edinet（コミットしない）。一覧は 2 秒に 1 件、書類は 3 秒に 1 件、同時に 1 本だけ。
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot import edinet as ED  # noqa: E402
from cfdbot import text_signals as TS  # noqa: E402

MODEL = "qwen3-0.6b"   # 埋め込みのモデル（docs/news.md 7 章で決めたものと同じ。近さにだけ使う）


def universe(data: Path) -> pd.DataFrame:
    p = data / "universe_jp.csv"
    if not p.exists():
        sys.exit(f"{p} が無い。先に ./cfd stocks --fetch を実行する")
    return pd.read_csv(p, dtype=str)


def codes_of(uni: pd.DataFrame) -> dict[str, str]:
    return {ED.ticker_code(t): t for t in uni["ticker"]}


def client(args, getter=None, sleep=None) -> ED.Client:
    if getter is None:
        key = ED.load_key()
        if not key:
            raise SystemExit("EDINET の API キーが無い。EDINET のサイトでアカウントを作って API キーを発行し、./cfd edinet key で保存する"
                             "（docs/todo.md の 7）")
        getter = ED.http_getter(key)
    kw = {"sleep": sleep} if sleep else {}
    return ED.Client(getter, log=lambda s: print(s, flush=True), **kw)


def prices(data: Path) -> pd.DataFrame:
    from cfdbot import stocks as S

    if not (data / "jp_close.csv").exists():
        sys.exit("data/stocks が無い。先に ./cfd stocks --fetch を実行する")
    o = pd.read_csv(data / "jp_open.csv", index_col=0, parse_dates=True)
    c = pd.read_csv(data / "jp_close.csv", index_col=0, parse_dates=True)
    return S.drop_closed_days(o, c)[1]


def month_prices(args) -> pd.DataFrame:
    """月末の終値。まだ終わっていない今月（13:05 の場中の値段のこともある）は落とす（「翌月末まで持つ」のため）。"""
    today = pd.Timestamp(args.today) if args.today else pd.Timestamp.now(tz="Asia/Tokyo").tz_localize(None)
    return TS.monthly_close(prices(Path(args.data)), today=today)


def docs_meta(store: ED.Store) -> tuple[pd.DataFrame, dict]:
    """判定に使う有報の一覧と、集めた量。取得が終わっていない・動いているときは止める（1 回きりの判定を途中のデータで使わない）。"""
    lock = store.root / ".lock"
    if lock.exists():
        try:
            os.kill(int(lock.read_text().strip() or 0), 0)
            sys.exit("有報の取得（./cfd edinet fetch）がまだ動いている。終わってから動かす（1 回きりの判定を途中のデータで使わない）")
        except (OSError, ValueError):
            pass
    idx, cand = store.index(), store.candidates()
    left = cand[~cand["docID"].isin(idx["docID"])] if len(cand) else cand
    if len(left):
        sys.exit(f"有報の本文の取得がまだ終わっていない（見つけた {len(cand):,} 件のうち、残り {len(left):,} 件）。"
                 "先に nohup ./cfd edinet fetch >> data/edinet/fetch.log 2>&1 & で終える（1 回きりの判定を途中のデータで使わない）")
    counts = {"candidates": len(cand), "fetched": len(idx), "none": int((idx["source"] == "none").sum()) if len(idx) else 0}
    idx = idx[idx["source"] != "none"]
    if idx.empty:
        sys.exit("有報の本文がまだ無い。先に ./cfd edinet fetch で集める")
    return TS.first_docs(idx).merge(idx[["docID", "filerName"]], on="docID", how="left"), counts


def counts_line(counts: dict) -> str:
    return (f"- 集めた有報: 見つけた {counts['candidates']:,} 件・本文を取った {counts['fetched']:,} 件（うち本文なし {counts['none']:,} 件）。"
            "取得は終わっている（残り 0 件）")


def section_text(store: ED.Store, meta: pd.DataFrame, names: dict[str, str], section: str) -> dict[str, str]:
    """節の文章（社名は東証の銘柄名と、その有報の提出者名の両方を消す）。"""
    out = {}
    for r in meta.itertuples(index=False):
        t = store.load_doc(r.ticker, r.docID).get(section, "")
        out[r.docID] = TS.clean_text(t, [names.get(r.ticker, ""), r.filerName or ""])
    return out


def conditions_line(v: dict) -> str:
    """4 つの条件の成否を、丸めない値で（表の丸めで境目が分からなくならないように）。"""
    ok = lambda b: "○" if b else "×"  # noqa: E731
    c1 = bool(np.isfinite(v["mean1"]) and np.isfinite(v["mean2"]) and v["mean1"] > 0 and v["mean2"] > 0)
    k, n = v.get("placebo_k"), v.get("placebo_n")
    plac = f"{k}/{n} = {v['placebo']:.4f}" if v.get("placebo") is not None else "–"
    return (f"- 条件: ① 前半 {v['mean1']:+.5f}・後半 {v['mean2']:+.5f}（月の平均）が両方プラス {ok(c1)}　"
            f"② 後半の t 値 {v['t2']:.3f} ≥ 1.65 {ok(np.isfinite(v['t2']) and v['t2'] >= 1.65)}　"
            f"③ 全期間の t 値 {v['t']:.3f} ≥ {v['threshold']:.3f} {ok(np.isfinite(v['t']) and v['t'] >= v['threshold'])}　"
            f"④ 偶然より良い割合 {plac} ≥ 0.95 {ok(v.get('placebo') is not None and v['placebo'] >= 0.95)}")


def period_line(book: pd.DataFrame) -> str:
    low = int((book["firms"] < TS.MIN_FIRMS).sum()) if "firms" in book else 0
    return (f"- 持った月: {book.index[0]:%Y-%m}〜{book.index[-1]:%Y-%m}（{len(book)} か月。まだ終わっていない今月は数えない）。"
            f"始まりの後で信号のある会社が {TS.MIN_FIRMS} 社に届かなかった月: {low} か月（決まりどおり数えた）" if len(book) else "- 持った月: 無し")


def fmt(x, d: int = 2) -> str:
    return "–" if x is None or not np.isfinite(x) else f"{x:.{d}f}"


def pct(x, d: int = 1) -> str:
    return "–" if x is None or not np.isfinite(x) else f"{x:.{d}%}"


def verdict_row(key: str, label: str, v: dict) -> str:
    return (f"| {key} | {label} | {v['months']} か月（後半 {v['split'][:7]}〜） | {pct(v['mean'] * 12)} | {pct(v['mean1'] * 12)} | "
            f"{pct(v['mean2'] * 12)} | {fmt(v['t'])} | {fmt(v['t2'])} | {pct(v['placebo'], 0)} | {pct(v['since_mean'] * 12)} | **{v['verdict']}** |")


def ref_row(label: str, book: pd.DataFrame) -> str:
    from cfdbot import stocks as S

    if book.empty:
        return f"| {label} | – | – | – | – |"
    return (f"| {label} | {len(book)} か月 | {pct(book['d'].mean() * 12)} | {fmt(S.t_value(book['d']))} | "
            f"{pct(book['turnover'].mean(), 0)} |")


HEAD = ("| 記号 | 仮説 | 期間 | 差（年率・コストの後） | 前半 | 後半 | t 値（全期間） | t 値（後半） | 偶然より良い割合 | 2025 年から | 判定 |",
        "|---|---|---|---|---|---|---|---|---|---|---|")
REF_HEAD = ("| 参考（判定しない） | 期間 | 差（年率・コストの後） | t 値 | 1 か月の入れ替え |", "|---|---|---|---|---|")


def write(out_dir: Path, name: str, lines: list[str]) -> Path:
    out = out_dir / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {path}")
    return path


def cmd_momentum(args, encoder=None) -> Path:
    """1C: 文章で近い会社のモメンタム。"""
    from cfdbot import stocks as S
    from cfdbot.news_embed import HashEmbedder, make_embedder

    store = ED.Store(args.root)
    uni = universe(Path(args.data))
    names = dict(zip(uni["ticker"], uni.get("name", uni["ticker"])))
    sectors = pd.Series(uni["sector"].to_numpy(), index=uni["ticker"]) if "sector" in uni else None
    meta, counts = docs_meta(store)
    text = section_text(store, meta, names, "business")
    empty = int(sum(1 for d in meta["docID"] if not text.get(d, "")))
    short = int(sum(1 for d in meta["docID"] if 0 < len(text.get(d, "")) < TS.MIN_CHARS))
    lines: list[str] = []

    def say(t: str = "") -> None:
        print(t, flush=True)
        lines.append(t)

    # 埋め込みの取っておき: float32 のまま。文章（前処理の後）の印とモデルが同じものだけ使い回す（初回と次で同じ近さになるように）
    cache = Path(args.root) / f"vecs_business_{MODEL}.npz"
    mark = {d: hashlib.sha256(text[d].encode("utf-8")).hexdigest()[:16] for d in meta["docID"] if text.get(d, "")}
    have: dict[str, np.ndarray] = {}
    if cache.exists():
        z = np.load(cache, allow_pickle=False)
        if "marks" in z and str(z["model"]) == MODEL:
            have = {i: v.astype(np.float32) for i, v, m in zip(z["ids"].astype(str), z["vecs"], z["marks"].astype(str)) if mark.get(i) == m}

    def save() -> None:
        ids = np.array(list(have), dtype=str)
        np.savez_compressed(cache, ids=ids, vecs=np.vstack([have[i] for i in ids]).astype(np.float32),
                            marks=np.array([mark[i] for i in ids], dtype=str), model=np.array(MODEL))

    todo = [d for d in meta["docID"] if d in mark and d not in have]
    if todo:
        enc = encoder or make_embedder(MODEL)
        print(f"埋め込み: 有報の「事業の内容」{len(todo):,} 件（{MODEL}。1,000 字ずつに切って平均）", flush=True)
        for k, d in enumerate(todo):
            have[d] = TS.doc_vector(text[d], enc.encode)
            if (k + 1) % 200 == 0:
                print(f"  {k + 1}/{len(todo)}", flush=True)
                save()
        save()
    have = {d: have[d] for d in mark}                     # 本文の空の有報には近さが無い（その月はその会社の信号なし。古い有報に戻さない）
    hashed = HashEmbedder(512)
    hash_vec = {d: TS.doc_vector(text[d], hashed.encode) for d in mark}

    mclose = month_prices(args)
    sig = TS.momentum_signals(meta, have, mclose, sectors=sectors)
    sig_hash = TS.momentum_signals(meta, hash_vec, mclose)
    start = TS.first_month(sig["main"], mclose)
    say("# 1C 文章で近い会社のモメンタム（月 1 回）")
    say("")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　決まり: docs/text_signals.md 3 章（結果を見る前に決めたもの）。試した数 {TS.TRIALS}"
        f"（全期間の t 値の基準 {fmt(S.expected_max_z(TS.TRIALS))}）")
    say(counts_line(counts))
    say(f"- 有報の「事業の内容」: {len(meta):,} 件・{meta['ticker'].nunique()} 社（{meta['submit'].min():%Y-%m}〜{meta['submit'].max():%Y-%m}）。"
        f"社名（東証の銘柄名と提出者名）と数字を消し、{MODEL} で埋め込んだ（近さにだけ使う）。"
        f"節が空の有報 {empty} 件（その会社はその有報の間、信号なし）・200 字未満 {short} 件（そのまま使う）")
    if start is None:
        say(f"- 信号のある会社が {TS.MIN_FIRMS} 社そろった月がまだ無い。判定しない")
        return write(Path(args.out), "momentum.md", lines)
    rng = np.random.default_rng(20261016)
    book = TS.monthly_book(sig["main"], mclose, start)
    null = TS.placebo(sig["main"], mclose, start, args.placebo, rng)
    v = TS.judge(book, null)
    say(f"- 判定の期間: {start:%Y-%m} 末に選んだ月から（信号のある会社が {TS.MIN_FIRMS} 社以上）。偶然 = 毎月同じ数をでたらめに選ぶ {args.placebo} 回")
    say(period_line(book))
    say("")
    say("## 1. 判定")
    say("")
    for h in HEAD:
        say(h)
    say(verdict_row("1C", f"近い {TS.K_PEERS} 社の直近 {TS.LOOKBACK} か月のリターンの上位 5 分の 1 を 1 か月持つ", v))
    say(conditions_line(v))
    say("")
    say("## 2. 参考（判定しない。試した数に入れない）")
    say("")
    for h in REF_HEAD:
        say(h)
    say(ref_row("判定の版（上の 1C）", book))
    refs = {"直近 1 か月（t−1 だけ）": sig["one_month"], "33 業種の平均を引いた版": sig["minus_sector"],
            "S2 型（近い会社 = 同じ 33 業種の仲間）": sig["s2"], "AI を使わない近さ（hash）": sig_hash["main"]}
    books = {}
    for label, s in refs.items():
        books[label] = TS.monthly_book(s, mclose, start) if s else pd.DataFrame()
        say(ref_row(label, books[label]))
    s2 = books.get("S2 型（近い会社 = 同じ 33 業種の仲間）", pd.DataFrame())
    if len(s2):
        diff = (book["d"] - s2["d"]).dropna()
        say(f"\n- 埋め込みの上乗せ（1C − S2 型）: 年率 {pct(diff.mean() * 12)}、t 値 {fmt(S.t_value(diff))}（{len(diff)} か月）。"
            "S2（日本の業種のモメンタム、6 か月）は docs/stocks.md で「使えない」だった")
    say(f"- 2025 年から（埋め込みのモデルの学習の締め切りより後の見込み）: 1C の差 年率 {pct(v['since_mean'] * 12)}（{v['since_months']} か月）")
    say("")
    say("## 3. 読み方・限界")
    say("")
    say("- 判定は 4 つの条件すべて（前半・後半ともプラス、後半だけで t 値 1.65 以上、全期間の t 値が試した数で割り引いた基準以上、偶然より良い割合 95% 以上）")
    say("- 対象は今の TOPIX500（生存者バイアス）。月 1 回の別枠（5 日保有の枠の外）。効きは文献より小さめに見込んでいる（docs/text_signals.md 3 章）")
    return write(Path(args.out), "momentum.md", lines)


def cmd_lazy(args) -> Path:
    """案 2: 有報の文面の前年との差。"""
    from cfdbot import stocks as S

    store = ED.Store(args.root)
    uni = universe(Path(args.data))
    names = dict(zip(uni["ticker"], uni.get("name", uni["ticker"])))
    meta, counts = docs_meta(store)
    lines: list[str] = []

    def say(t: str = "") -> None:
        print(t, flush=True)
        lines.append(t)

    mclose = month_prices(args)
    mdna_empty = int(sum(1 for r in meta.itertuples(index=False) if not store.load_doc(r.ticker, r.docID).get("mdna")))
    pairs = {}
    for sec in ("risks", "mdna", "business"):
        text = section_text(store, meta, names, sec)
        pairs[sec] = TS.lazy_pairs(meta, lambda d, t=text: t.get(d, ""))
    sig = {k: TS.lazy_signals(p, mclose.index) for k, p in pairs.items()}
    start = TS.first_month(sig["risks"], mclose)
    say("# 案 2 有報の文面の前年との差（月 1 回）")
    say("")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　決まり: docs/text_signals.md 4 章（結果を見る前に決めたもの）。試した数 {TS.TRIALS}"
        f"（全期間の t 値の基準 {fmt(S.expected_max_z(TS.TRIALS))}）")
    say(counts_line(counts))
    say(f"- 有報: {len(meta):,} 件・{meta['ticker'].nunique()} 社。前年と組にできた「事業等のリスク」{len(pairs['risks']):,} 組"
        f"（近さの中央値 {fmt(pairs['risks']['sim'].median(), 3) if len(pairs['risks']) else '–'}）")
    if start is None:
        say(f"- 近さのある会社が {TS.MIN_FIRMS} 社そろった月がまだ無い。判定しない")
        return write(Path(args.out), "lazy.md", lines)
    rng = np.random.default_rng(20261017)
    book = TS.monthly_book(sig["risks"], mclose, start)
    null = TS.placebo(sig["risks"], mclose, start, args.placebo, rng)
    v = TS.judge(book, null)
    say(f"- 判定の期間: {start:%Y-%m} 末に選んだ月から。偶然 = 毎月同じ数をでたらめに選ぶ {args.placebo} 回")
    say(period_line(book))
    say("")
    say("## 1. 判定")
    say("")
    for h in HEAD:
        say(h)
    say(verdict_row("案 2", "「事業等のリスク」を前年から変えていない上位 5 分の 1 を 1 か月持つ", v))
    say(conditions_line(v))
    say("")
    say("## 2. 参考（判定しない。試した数に入れない）")
    say("")
    for h in REF_HEAD:
        say(h)
    say(ref_row("判定の版（上の案 2）", book))
    say(ref_row("変えた会社（近さの下位 5 分の 1）", TS.monthly_book(sig["risks"], mclose, start, highest=False)))
    say(ref_row("「経営者による分析」の節", TS.monthly_book(sig["mdna"], mclose, start) if sig["mdna"] else pd.DataFrame()))
    say(ref_row("「事業の内容」の節", TS.monthly_book(sig["business"], mclose, start) if sig["business"] else pd.DataFrame()))
    say("")
    say(f"- 「経営者による分析」が空の有報 {mdna_empty:,} 件（多くは古い要素の名前の 2017 年ごろまでの有報。参考の行だけに効き、判定には効かない）")
    say("")
    say("## 3. 読み方・限界")
    say("")
    say("- 近さは文字 3 つ組の出現のコサイン（言語モデルを使わないので、先読みが無い）。社名と数字は消してから比べた")
    say("- 対象は今の TOPIX500（生存者バイアス。文面を大きく変えた後に上場をやめた会社は入っていない）。月 1 回の別枠")
    return write(Path(args.out), "lazy.md", lines)


def keep_awake() -> None:
    """取得が終わるまで Mac を眠らせない（画面は消えてよい。ふたを閉じると眠る）。眠ると回線が切れて「状態 -1」で止まる（2026-10-21）。"""
    if sys.platform == "darwin":
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])


def main(argv: list[str] | None = None, getter=None, sleep=None, encoder=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["key", "check", "fetch", "status", "momentum", "lazy"])
    p.add_argument("--root", default="data/edinet")
    p.add_argument("--data", default="data/stocks")
    p.add_argument("--out", default="output/edinet")
    p.add_argument("--today", help="今日として扱う日（試験用）")
    p.add_argument("--max-docs", type=int, default=6000, help="fetch: 1 回に取る有報の上限")
    p.add_argument("--lists-only", action="store_true", help="fetch: 一覧だけを見る（有報の本文は取らない）")
    p.add_argument("--placebo", type=int, default=300)
    args = p.parse_args(argv)
    today = pd.Timestamp(args.today) if args.today else pd.Timestamp.now(tz="Asia/Tokyo").tz_localize(None).normalize()

    if args.command == "key":
        key = getpass.getpass("EDINET の API キー（画面には出ません）: ").strip()
        if not key:
            raise SystemExit("キーが空だった。保存していない")
        print(f"保存した: {ED.save_key(key)}（権限 600。このリポジトリの外）")
        return None
    if args.command == "status":
        store = ED.Store(args.root)
        idx, cand = store.index(), store.candidates()
        days = store.listed_days()
        print(f"一覧を見た日: {len(days):,} 日" + (f"（{min(days)}〜{max(days)}）" if days else ""))
        print(f"見つけた有報（今の TOPIX500）: {len(cand):,} 件、本文を取った: {len(idx):,} 件"
              + (f"（本文なし {int((idx['source'] == 'none').sum())} 件）" if len(idx) else ""))
        return None
    if args.command == "momentum":
        return cmd_momentum(args, encoder)
    if args.command == "lazy":
        return cmd_lazy(args)

    cl = client(args, getter, sleep)
    codes = codes_of(universe(Path(args.data)))
    if args.command == "check":
        try:
            res = ED.run_check(cl, today, codes)
        except ED.Stop as e:
            raise SystemExit(f"止めた: {e}")
        lines = ED.report(res, today)
        path = write(Path(args.out), "check.md", lines)
        path.with_suffix(".json").write_text(json.dumps(res, ensure_ascii=False, default=str, indent=1), encoding="utf-8")
        print("\n".join(lines))
        return path
    store = ED.Store(args.root)
    if not store.lock():
        raise SystemExit("ほかの取得が動いている（同時に 1 本だけ）")
    keep_awake()
    try:
        days = ED.fetch_days(today)
        n = ED.fetch_lists(cl, store, days, codes, pd.Timestamp.now(), log=lambda s: print(s, flush=True))
        print(f"一覧: 新しく {n:,} 日を見た（{days[0]:%Y-%m-%d}〜{days[-1]:%Y-%m-%d}）。見つけた有報 {len(store.candidates()):,} 件", flush=True)
        if not args.lists_only:
            got = ED.fetch_docs(cl, store, pd.Timestamp.now(), max_docs=args.max_docs, log=lambda s: print(s, flush=True))
            print(f"有報: 本文あり {got['docs']:,}・本文なし {got['missing']:,}・残り {got['left']:,}", flush=True)
    except ED.Stop as e:
        raise SystemExit(f"止めた: {e}（同じコマンドで続きから）")
    finally:
        store.unlock()
    return None


if __name__ == "__main__":
    main()
