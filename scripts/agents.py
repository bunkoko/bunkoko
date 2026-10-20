"""役割の違う 5 つの AI を動かす（./cfd agents <役割>）。設計と判定の条件は docs/agents.md。

    python scripts/agents.py collect --backfill   # 情報収集（初回。490 社の過去の適時開示と題名の埋め込み。20〜40 分）
    python scripts/agents.py collect              # 情報収集（毎日。直近 5 日分の一覧）
    python scripts/agents.py collect --docs       # ＋本文（PDF を文字にしたもの）と決算の数字（XBRL）。直近 7 日分を 1 秒に 1 件で取る
    python scripts/agents.py collect --xbrl-history --max-docs 40000   # 過去の決算の数字（2016-10〜。1 秒に 1 件なので 6〜7 時間）
    python scripts/agents.py ir                   # IR 評価
    python scripts/agents.py select               # 銘柄選定
    python scripts/agents.py decide               # 判断
    python scripts/agents.py evaluate             # 判断の評価（報告書を作る）
    python scripts/agents.py run [--prices]       # 上を順に（--prices は先に値段も取り直す）
    python scripts/agents.py earnings             # 決算の数字で評価する（10 章の E1〜E3。collect --xbrl-history の後）
    python scripts/agents.py extra [--fetch]      # まだ入れていなかった項目（出来高・割安さ・業種・進捗率）を足して確かめる（11 章の F1〜F4）
    python scripts/agents.py status
    python scripts/agents.py check                # 台帳の点検（自動実行が動いたか・抜けた日）と、先のデータの途中経過・止める決まり（12 章）
    python scripts/agents.py orders               # 日本株の予行演習: 今日の注文の一覧を作る（注文は出さない。docs/stocks_live.md）
    python scripts/agents.py orders --setup --equity 300000 [--deadline 13:30] [--real]   # 予行演習の設定（--real は空運転の条件を満たした後だけ）
    python scripts/agents.py orders --fill 7203 買い 3 2850 [--fee 0] [--status 約定] [--at 引け] [--on 日付]   # 約定を書き足す
    python scripts/agents.py orders --report      # 記録上の資産・止める決まり・空運転の進み具合・コスト（c*）の途中経過
    python scripts/agents.py manual               # 今週の手で買う候補（M2 を週 1 回・10 銘柄・1 日遅れにした形。docs/agents.md 13 章）
    python scripts/agents.py manual --test        # その形の過去の検証（M2a・M2b。数分）
    python scripts/agents.py premium [--test]     # 決算発表をまたいで持つ（J7）の先の記録の途中経過（--test は使っていない決算での過去の検証）
    python scripts/agents.py schedule --job collect [--off]   # 毎日 8:00 に collect --docs を動かすよう登録する／外す（Mac の launchd）
    python scripts/agents.py schedule --job run [--off]       # 平日 13:05 に run --prices を動かすよう登録する／外す

それぞれの AI は別のモジュールで、ファイル（data/agents/）だけでやりとりする。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot import stocks as S  # noqa: E402
from cfdbot.agents import paths  # noqa: E402

MODEL = "qwen3-0.6b"
JST = "Asia/Tokyo"
LABEL = "com.bunkoko.agents"
HISTORY_FROM = "2016-10-01"   # 上場会社情報サービスに残っているのは約 10 年分（2026-10-13 に確かめた: 2016-09 までは無く、2016-10-11 の分はある）
JOBS = {   # 自動実行: 名前 → （launchd の名前、動かすコマンド、時刻、説明）
    "run": (LABEL, ["agents", "run", "--prices"], [{"Weekday": d, "Hour": 13, "Minute": 5} for d in range(1, 6)], "平日 13:05 に ./cfd agents run --prices"),
    "collect": (LABEL + ".collect", ["agents", "collect", "--docs"], {"Hour": 8, "Minute": 0}, "毎日 8:00 に ./cfd agents collect --docs"),
}


def wide(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, index_col=0, parse_dates=True)


def prices(data: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not (data / "jp_close.csv").exists():
        sys.exit("data/stocks が無い。先に ./cfd stocks --fetch を実行する")
    return S.drop_closed_days(wide(data / "jp_open.csv"), wide(data / "jp_close.csv"))


def pct(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}%}" if x is not None and np.isfinite(x) else "–"


def f2(x: float) -> str:
    return f"{x:.2f}" if x is not None and np.isfinite(x) else "–"


# --------------------------------------------------------------------------- 情報収集
def cmd_collect(args) -> None:
    from cfdbot.agents import collector as C

    p = paths(args.root)
    uni = pd.read_csv(Path(args.data) / "universe_jp.csv")
    tickers = set(uni["ticker"])
    df = C.load_disclosures(p["disclosures"])
    p["disclosures"].parent.mkdir(parents=True, exist_ok=True)
    before = len(df)
    if args.backfill:
        done = set(df["ticker"]) if not args.force else set()
        todo = [t for t in uni["ticker"] if t not in done]
        print(f"情報収集: 過去の開示を {len(todo)} 社ぶん取る（やのしん TDnet WEB-API）", flush=True)
        for i, t in enumerate(todo):
            try:
                df = C.merge_disclosures(df, C.fetch_company(t), tickers, prefer_new=True)
            except Exception as e:  # noqa: BLE001
                print(f"  {t}: 取れない（{str(e)[:60]}）", flush=True)
            if (i + 1) % 25 == 0 or i == len(todo) - 1:
                df.to_csv(p["disclosures"], index=False)
                print(f"  {i + 1}/{len(todo)} 社、{len(df):,} 件", flush=True)
            time.sleep(0.5)
    else:
        today = pd.Timestamp.now(tz=JST).tz_localize(None).normalize()
        for k in range(args.days):
            try:
                df = C.merge_disclosures(df, C.fetch_day(today - pd.Timedelta(days=k)), tickers, prefer_new=True)
            except Exception as e:  # noqa: BLE001
                print(f"  {today - pd.Timedelta(days=k):%Y-%m-%d}: 一覧が取れない（{str(e)[:60]}）", flush=True)
            time.sleep(0.5)
        df.to_csv(p["disclosures"], index=False)
    print(f"情報収集: 開示 {len(df):,} 件（+{len(df) - before:,}）、{df['ticker'].nunique()} 社、{df['time'].min()[:10]}〜{df['time'].max()[:16]}", flush=True)
    if not args.no_embed:
        from cfdbot.news_embed import make_embedder

        keys = C.title_keys(df)
        have, _ = C.load_title_vectors(p["titles"])
        if set(keys) - set(have) - {""}:
            added = C.embed_new_titles(keys, p["titles"], make_embedder(MODEL), log=lambda s: print(s, flush=True))
            print(f"情報収集: 題名 {added:,} 種類を新しく埋め込んだ（そろえた後の題名は全部で {keys.nunique():,} 種類）", flush=True)
        else:
            print("情報収集: 新しい題名は無い", flush=True)
    if args.docs or args.xbrl_history:
        ir_root = p["disclosures"].parent
        if args.xbrl_history:
            want = df[(df["xbrl"] != "") & (df["time"] >= HISTORY_FROM)]
            print(f"情報収集: 過去の決算の数字（XBRL）{len(want):,} 件のうち、まだ無いものを {C.INTERVAL:g} 秒に 1 件ずつ取る（最大 {args.max_docs:,} 件）", flush=True)
            got = C.collect_docs(want, ir_root, pdf=False, max_docs=args.max_docs, log=lambda s: print(s, flush=True))
        else:
            since = (pd.Timestamp.now(tz=JST).tz_localize(None) - pd.Timedelta(days=args.docs_days)).strftime("%Y-%m-%d")
            want = df[df["time"] >= since]
            print(f"情報収集: 直近 {args.docs_days} 日の開示 {len(want):,} 件の本文と数字を、{C.INTERVAL:g} 秒に 1 件ずつ取る（東証 上場会社情報サービス）", flush=True)
            got = C.collect_docs(want, ir_root, max_docs=args.max_docs, log=lambda s: print(s, flush=True))
        if got.get("busy"):
            print("情報収集: ほかの書類の取得が動いているので、今回は取らない（次の回に取る）", flush=True)
        else:
            print(f"情報収集: 本文 +{got['text']}、数字 +{got['xbrl']}、まだ載っていない {got['missing']}、残り {got['left']}", flush=True)


# --------------------------------------------------------------------------- IR 評価
def cmd_ir(args) -> None:
    from cfdbot.agents import collector as C
    from cfdbot.agents import ir_scorer as I

    p = paths(args.root)
    _, close = prices(Path(args.data))
    disc = C.load_disclosures(p["disclosures"])
    titles, vecs = C.load_title_vectors(p["titles"])
    if disc.empty or not len(titles):
        sys.exit("開示か埋め込みが無い。先に ./cfd agents collect --backfill を実行する")
    table, mat = I.daily_ir(disc, C.title_keys(disc), titles, vecs, close.index, list(close.columns))
    print(f"IR 評価: 開示のあった 株 × 日 {len(table):,} 件（{close.index[table['pos'].min()]:%Y-%m}〜）", flush=True)
    score = I.walk_forward(table, mat, I.target_rank(close), close.index, log=lambda s: print(s, flush=True))
    out = pd.DataFrame({"date": close.index[table["pos"].to_numpy()], "ticker": close.columns[table["j"].to_numpy()],
                        "score": score, "n": table["n"].to_numpy(), "titles": table["titles"].to_numpy()})
    out = out[np.isfinite(out["score"])]
    out.to_csv(p["ir_scores"], index=False, float_format="%.6f")
    print(f"IR 評価: 点数 {len(out):,} 件を書いた（{out['date'].min():%Y-%m-%d}〜{out['date'].max():%Y-%m-%d}）", flush=True)


# --------------------------------------------------------------------------- 銘柄選定
def cmd_select(args) -> None:
    from cfdbot.agents import selector

    p = paths(args.root)
    open_, close = prices(Path(args.data))
    sc = selector.scores(open_, close, log=lambda s: print(s, flush=True))
    sc = sc[sc.notna().any(axis=1)]
    p["selector"].parent.mkdir(parents=True, exist_ok=True)
    sc.to_csv(p["selector"], float_format="%.5f")
    print(f"銘柄選定: 点数を書いた（{sc.index[0]:%Y-%m-%d}〜{sc.index[-1]:%Y-%m-%d}、{sc.shape[1]} 銘柄）", flush=True)


# --------------------------------------------------------------------------- 判断
def cmd_decide(args) -> None:
    from cfdbot.agents import decider as D

    p = paths(args.root)
    if not (p["selector"].exists() and p["ir_scores"].exists()):
        sys.exit("点数のファイルが無い。先に ./cfd agents select と ./cfd agents ir を実行する")
    sel = wide(p["selector"])
    ir_long = pd.read_csv(p["ir_scores"], parse_dates=["date"])
    ir = D.ir_matrix(ir_long, sel.index, sel.columns)
    bk = D.books(sel, ir)
    table = D.decisions_table(bk, sel, ir, "2016-01-01")
    table.to_csv(p["decisions"], index=False, float_format="%.5f")
    last = sel.index[-1]
    names = pd.read_csv(Path(args.data) / "universe_jp.csv").set_index("ticker")["name"].to_dict()
    today = D.explain(table[(table["date"] == last) & table["book"].isin(["combined", "selector"])],
                      ir_long[ir_long["date"] == last], names)
    today.insert(0, "decided_at", pd.Timestamp.now(tz=JST).tz_localize(None).strftime("%Y-%m-%d %H:%M:%S"))
    old = pd.read_csv(p["ledger"], dtype=str) if p["ledger"].exists() else pd.DataFrame()
    if len(old) and f"{last:%Y-%m-%d}" in set(old["date"].str[:10]):
        print(f"判断: {last:%Y-%m-%d} の判断は台帳にもうある（書き足さない）", flush=True)
    else:
        today.assign(date=today["date"].dt.strftime("%Y-%m-%d")).to_csv(p["ledger"], mode="a", header=not p["ledger"].exists(), index=False,
                                                                        float_format="%.5f")
        print(f"判断: {last:%Y-%m-%d} の判断 {int((today['book'] == 'combined').sum())} 銘柄を台帳に書き足した", flush=True)
    # 台帳に足す帳（でたらめ・E1。docs/agents.md 12 章）は別のファイルに書く。ledger.csv の書き方・中身は変えない。
    # 失敗しても ledger.csv はもう書いてあるので、理由を 1 行出して続ける
    try:
        write_extra_books(p, last, sel, bk, names, today["decided_at"].iloc[0] if len(today) else None)
    except Exception as e:  # noqa: BLE001
        print(f"判断: でたらめ・E1 の帳は書けなかった（{type(e).__name__}: {str(e)[:120]}）。ledger.csv は書いてある", flush=True)
    n = table.groupby("book")["date"].nunique()
    print(f"判断: 記録 {len(table):,} 行（{table['date'].min():%Y-%m-%d}〜{last:%Y-%m-%d}。" + "、".join(f"{k} {v} 日" for k, v in n.items()) + "）", flush=True)


def write_extra_books(p: dict, last: pd.Timestamp, sel: pd.DataFrame, bk: dict, names: dict, stamp: str | None) -> None:
    """でたらめの帳と E1 の帳を data/agents/ledger_extra.csv に書き足す（同じ日は二度書かない。書いた時刻を入れる）。"""
    from cfdbot.agents import decider as D

    path = p["ledger_extra"]
    old = pd.read_csv(path, dtype=str) if path.exists() else pd.DataFrame()
    if len(old) and f"{last:%Y-%m-%d}" in set(old["date"].str[:10]):
        print(f"判断: {last:%Y-%m-%d} のでたらめ・E1 の帳はもうある（書き足さない）", flush=True)
        return
    revisions = pd.read_csv(p["revisions"], parse_dates=["date"]) if p["revisions"].exists() else None
    if revisions is None:
        print("判断: E1 の帳は、決算の数字の表（revisions_recent.csv）が無いので書かない（./cfd agents run で作られる）", flush=True)
    extra = D.extra_rows(last, sel.iloc[-1], int(bk["selector"][-1].sum()), revisions, names)
    try:                                   # 手で注文する形の帳（週の最初の営業日だけ。docs/agents.md 13.3）
        from cfdbot.agents import manual as M

        extra = pd.concat([extra, M.manual_rows(last, sel.iloc[-1], sel.index, names)], ignore_index=True)
    except Exception as e:  # noqa: BLE001
        print(f"判断: 手で買う候補の帳は書けなかった（{type(e).__name__}: {str(e)[:120]}）。でたらめ・E1 の帳は書く", flush=True)
    extra.insert(0, "decided_at", stamp or pd.Timestamp.now(tz=JST).tz_localize(None).strftime("%Y-%m-%d %H:%M:%S"))
    extra.assign(date=pd.to_datetime(extra["date"]).dt.strftime("%Y-%m-%d")).to_csv(path, mode="a", header=not path.exists(), index=False,
                                                                                   float_format="%.5f")
    n = extra["book"].value_counts()
    print(f"判断: でたらめの帳 {int(n.get(D.RANDOM_BOOK, 0))}・E1 の帳 {int(n.get(D.E1_BOOK, 0))}・手で買う候補 {int(n.get('m2_manual', 0))} 銘柄を "
          f"{path.name} に書き足した", flush=True)


def read_ledgers(p: dict) -> pd.DataFrame:
    """台帳（ledger.csv）と、足した帳（ledger_extra.csv）を合わせて読む。どちらも無ければ空。"""
    parts = [pd.read_csv(p[k], dtype={"ticker": str}) for k in ("ledger", "ledger_extra") if p[k].exists()]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


# --------------------------------------------------------------------------- 判断の評価
def cmd_evaluate(args) -> None:
    from cfdbot.agents import evaluator as E

    p = paths(args.root)
    for k in ("decisions", "ir_scores", "selector", "disclosures"):
        if not p[k].exists():
            sys.exit(f"{p[k]} が無い。先に ./cfd agents run を実行する")
    _, close = prices(Path(args.data))
    cc = E.returns(close)
    dec = pd.read_csv(p["decisions"], parse_dates=["date"])
    ir_long = pd.read_csv(p["ir_scores"], parse_dates=["date"])
    sel_w = wide(p["selector"]).reindex(index=close.index, columns=close.columns).to_numpy(dtype=float)
    ir_w = np.full(sel_w.shape, np.nan)
    t, j = close.index.get_indexer(ir_long["date"]), close.columns.get_indexer(ir_long["ticker"])
    ok = (t >= 0) & (j >= 0)
    ir_w[t[ok], j[ok]] = ir_long["score"].to_numpy()[ok]
    disc = pd.read_csv(p["disclosures"], dtype=str, keep_default_na=False)
    period = np.asarray(close.index >= E.START)
    rng = np.random.default_rng(11)
    n_trials = E.PRIOR_TRIALS + E.NEW_TRIALS
    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    # 記録が決まりどおりかの点検
    recorded = {b: E.to_matrix(dec, b, close.index, close.columns) for b in ("selector", "combined", "ir")}
    expected = E.expected_books(sel_w, ir_w)
    audit = {b: int((recorded[b][period] != expected[b][period]).sum()) for b in recorded}
    pnl = {b: E.book_pnl(recorded[b], cc)[period] for b in recorded}

    # A1: IR だけで選ぶ
    print("A1 の偶然との比較…", flush=True)
    ir_m = np.where(np.isfinite(sel_w), ir_w, np.nan)                          # 銘柄選定の点数がある株の IR だけ
    pool = np.isfinite(ir_m)
    k_ir = recorded["ir"].sum(axis=1)
    null1 = np.array([E.book_pnl(E.random_among(pool, k_ir, rng), cc, 0.0)["gross"][period].mean() for _ in range(args.placebo)])
    d1 = pnl["ir"]["d"].resample("ME").sum()
    a1 = E.verdict(d1, float(np.mean(null1 < pnl["ir"]["gross"].mean())), n_trials)
    # A2: 足した点数 − 銘柄選定だけ
    print("A2 の偶然との比較…", flush=True)
    base = pnl["selector"]["gross"].mean()
    null2 = np.empty(args.placebo)
    for i in range(args.placebo):
        fake = E.top_k(sel_w + np.nan_to_num(E.shuffle_rows(ir_m, rng)), E.TOP_SHARE, E.MIN_VALID)
        null2[i] = E.book_pnl(fake, cc, 0.0)["gross"][period].mean() - base
    d2 = (pnl["combined"]["d"] - pnl["selector"]["d"]).resample("ME").sum()
    a2 = E.verdict(d2, float(np.mean(null2 < pnl["combined"]["gross"].mean() - base)), n_trials)
    d1.to_csv(out / "d_A1.csv", header=["d"])
    d2.to_csv(out / "d_A2.csv", header=["d"])

    say("# 5 つの AI（情報収集・IR 評価・銘柄選定・判断・判断の評価）の採点")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　設計と判定の条件: docs/agents.md（結果を見る前に決めたもの）")
    say(f"データ: 日本株 {close.shape[1]} 銘柄（〜{close.index[-1]:%Y-%m-%d}）、適時開示 {len(disc):,} 件（{disc['time'].min()[:10]}〜{disc['time'].max()[:10]}）、"
        f"IR の点数 {len(ir_long):,} 件。コスト: 往復 {pct(E.COST, 2)}。試した数: {E.PRIOR_TRIALS} ＋ {E.NEW_TRIALS} ＝ {n_trials}"
        f"（全期間の t 値の基準 {f2(S.expected_max_z(n_trials))}）")
    say("\n## 1. 判定\n")
    say("| 記号 | 仮説 | 期間 | 差（年率） | 前半 | 後半 | t 値（全期間） | t 値（後半） | 偶然より良い割合 | 2025-07 以降 | 判定 |")
    say("|---|---|---|---|---|---|---|---|---|---|---|")
    for key, label, v in (("A1", "IR 評価 AI は当たる（開示のあった株の、IR の点数の上位 20% を 5 日持つ − 全銘柄の平均）", a1),
                          ("A2", "判断 AI（値動き＋IR）は、銘柄選定 AI だけより良い", a2)):
        say(f"| {key} | {label} | {v['start']:%Y-%m}〜{v['end']:%Y-%m}（後半 {E.SPLIT[:4]}〜） | {pct(v['mean'] * 12)} | {pct(v['mean1'] * 12)} | "
            f"{pct(v['mean2'] * 12)} | {f2(v['t'])} | {f2(v['t2'])} | {pct(v['placebo'], 0)} | {pct(v['strict_mean'] * 12)} | **{v['verdict']}** |")
    say("\n## 2. それぞれの AI の働き（2016 年から。全銘柄の平均との差、年率）\n")
    say("| 買い方 | 担当 | コストを引く前 | 1 日に入れ替える割合 | コストを引いた後 | 後半（2021〜） | 2025-07 以降 | 持っている割合 |")
    say("|---|---|---|---|---|---|---|---|")
    for b, who in (("selector", "銘柄選定 AI だけ（値動き）"), ("ir", "IR 評価 AI だけ（開示のあった株）"), ("combined", "判断 AI（値動き＋IR）")):
        x = pnl[b]
        say(f"| {b} | {who} | {pct(x['gross'].mean() * 252)} | {pct(x['turnover'].mean(), 1)} | {pct(x['d'].mean() * 252)} | "
            f"{pct(x['d'][x.index >= E.SPLIT].mean() * 252)} | {pct(x['d'][x.index >= E.STRICT_FROM].mean() * 252)} | {pct(x['invested'].mean(), 0)} |")
    same = (recorded["combined"][period] & recorded["selector"][period]).sum() / max(recorded["selector"][period].sum(), 1)
    say("")
    say(f"- 判断 AI が銘柄選定 AI と同じ銘柄を選んだ割合 {pct(same, 0)}（残りが IR で入れ替わった分）。IR だけの買い方は 1 日平均 {k_ir[period].mean():.1f} 銘柄")
    say(f"- 記録の点検（評価 AI が点数のファイルから計算し直したものとの食い違い）: " + "、".join(f"{b} {v} 件" for b, v in audit.items()))
    y = (np.log(close.shift(-E.HOLD) / close).rank(axis=1, pct=True) - 0.5).to_numpy()
    rows = pd.DataFrame({"year": close.index.year[t[ok]], "score": ir_long["score"].to_numpy()[ok], "y": y[t[ok], j[ok]]}).dropna()
    ic = rows.groupby("year").apply(lambda g: g["score"].corr(g["y"]), include_groups=False)
    say("- IR の点数と、実際のその後 5 日の順位の相関（年ごと。0 = 当たっていない）: " + "、".join(f"{yr}: {v:+.3f}" for yr, v in ic.items()))

    say("\n## 3. 参考: 題名の言葉で分けた種類ごとの、その後 5 日の値動き（AI を使わない分け方。判定しない）\n")
    say("引けで買って 5 営業日後の引けまでの「株 − 全銘柄の平均」。コストを引く前（往復 0.10% を超えないと利益にならない）。\n")
    say("| 種類 | 回数 | 平均 | t 値 | 2016〜2020 | 2021〜 |")
    say("|---|---|---|---|---|---|")
    for r in E.keyword_table(disc, cc).itertuples():
        say(f"| {r.kind} | {r.n:,} | {pct(r.mean, 2)} | {f2(r.t)} | {pct(r.mean1, 2)} | {pct(r.mean2, 2)} |")

    say("\n## 4. 台帳（その日の 15:25 より前に書かれた、本当の「先の」判断）\n")
    ledger = pd.read_csv(p["ledger"], dtype={"ticker": str}) if p["ledger"].exists() else pd.DataFrame()
    summ = E.ledger_summary(ledger, cc)
    if summ.empty:
        say("- まだ無い（`./cfd agents run` を営業日の 13:00〜15:25 に動かすと、その日の判断が書き足される）")
    for r in summ.itertuples():
        say(f"- {r.book}: {r.days} 日分（うち 5 日後の値段で採点できた {r.scored_days} 日）。5 日後の「株 − 全銘柄の平均」の平均 {pct(r.mean_excess, 2)}")
    if len(ledger):
        last = ledger[ledger["date"] == ledger["date"].max()]
        with_ir = last[(last["book"] == "combined") & last["ir"].notna()]
        say(f"\n最後の判断（{ledger['date'].max()}、書いた時刻 {last['decided_at'].iloc[0]}）: 判断 AI の買い {int((last['book'] == 'combined').sum())} 銘柄。"
            f"うち開示が点数に入ったもの {len(with_ir)} 銘柄")
        for r in with_ir.sort_values("ir", ascending=False).head(15).itertuples():
            say(f"  - {r.name}（{str(r.ticker).replace('.T', '')}）: {r.reason}")

    say("\n## 5. 読み方・限界\n")
    say("- 判定は 5 つの条件すべて（前半・後半ともプラス、後半だけで t 値 1.65 以上、全期間の t 値が試した数で割り引いた基準以上、偶然より良い割合 95% 以上、2025-07 以降もプラス）")
    say("- 開示は題名だけ（本文は見ていない）。買うのは開示から 2 時間半以上たった後。今の構成銘柄（生存者バイアス）。税金は入れていない")
    say("- 評価 AI は、ほかの AI のプログラムを使わず、記録・点数・値段のファイルだけで計算している")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    pd.DataFrame([dict(a1, key="A1"), dict(a2, key="A2")]).drop(columns=["start", "end"]).to_csv(out / "verdicts.csv", index=False)
    print(f"\n保存先: {out}/report.md")


# --------------------------------------------------------------------------- 決算の数字（docs/agents.md 10 章の E1〜E3）
def cmd_earnings(args) -> None:
    """IR 評価（数字版）→ 判断 → 判断の評価 を順に。それぞれ前の段階が書いたファイルだけを読む。"""
    from cfdbot.agents import collector as C
    from cfdbot.agents import decider as D
    from cfdbot.agents import earnings as N
    from cfdbot.agents import evaluator as E
    from cfdbot.agents import ir_scorer as I

    p = paths(args.root)
    if not p["selector"].exists():
        sys.exit("銘柄選定の点数が無い。先に ./cfd agents select を実行する")
    _, close = prices(Path(args.data))
    # ---- IR 評価（数字版）: 入力と点数を書く
    disc = C.load_disclosures(p["disclosures"])
    feat = N.build(disc, p["disclosures"].parent, log=lambda s: print(s, flush=True))
    if feat.empty:
        sys.exit("決算の数字が無い。先に ./cfd agents collect --xbrl-history を実行する")
    table = N.daily(feat, close.index, list(close.columns))
    score = N.walk_forward(table, I.target_rank(close), close.index, log=lambda s: print(s, flush=True))
    numbers = table.assign(date=close.index[table["pos"].to_numpy()], ticker=close.columns[table["j"].to_numpy()], score=score)
    numbers[["date", "ticker", "score"] + N.FEATURES].to_csv(p["ir_numbers"], index=False, float_format="%.6f")
    print(f"IR 評価（数字）: 開示 {len(feat):,} 回 → 株 × 日 {len(numbers):,} 件（{numbers['date'].min():%Y-%m-%d}〜{numbers['date'].max():%Y-%m-%d}）、"
          f"点数つき {int(np.isfinite(score).sum()):,} 件", flush=True)

    # ---- 判断: 点数のファイルだけを読んで、買いの記録を書く
    sel = wide(p["selector"])
    num = pd.read_csv(p["ir_numbers"], parse_dates=["date"])
    ir = D.ir_matrix(num, sel.index, sel.columns)
    up = D.ir_matrix(num.assign(score=(num["fc_rev"] >= N.UP_REVISION).astype(float)), sel.index, sel.columns)
    bk = D.books(sel, ir)
    bk = {"selector": bk["selector"], "ir_num": bk["ir"], "combined_num": bk["combined"],
          "e1": (up.to_numpy() == 1.0) & sel.notna().to_numpy()}
    first = f"{num['date'].min():%Y-%m-%d}"
    D.decisions_table(bk, sel, ir, first).to_csv(p["decisions_numbers"], index=False, float_format="%.5f")

    # ---- 判断の評価: 記録・点数・値段のファイルだけで採点する
    cc = E.returns(close)
    dec = pd.read_csv(p["decisions_numbers"], parse_dates=["date"])
    sel_w = wide(p["selector"]).reindex(index=close.index, columns=close.columns).to_numpy(dtype=float)
    t, j = close.index.get_indexer(num["date"]), close.columns.get_indexer(num["ticker"])
    ok = (t >= 0) & (j >= 0)
    ir_w, rev_w = np.full(sel_w.shape, np.nan), np.full(sel_w.shape, np.nan)
    ir_w[t[ok], j[ok]] = num["score"].to_numpy()[ok]
    rev_w[t[ok], j[ok]] = num["fc_rev"].to_numpy()[ok]
    has = np.zeros(sel_w.shape, dtype=bool)
    has[t[ok], j[ok]] = True
    eligible = np.isfinite(sel_w)
    rec = {b: E.to_matrix(dec, b, close.index, close.columns) for b in ("selector", "ir_num", "combined_num", "e1")}
    exp = E.expected_books(sel_w, ir_w)
    m0 = np.asarray(close.index >= first)                                          # 記録を書いた期間だけを点検する
    audit = {"selector": int((rec["selector"] != exp["selector"])[m0].sum()), "ir_num": int((rec["ir_num"] != exp["ir"])[m0].sum()),
             "combined_num": int((rec["combined_num"] != exp["combined"])[m0].sum()),
             "e1": int((rec["e1"] != ((rev_w >= N.UP_REVISION) & eligible))[m0].sum())}
    rng = np.random.default_rng(11)
    n_trials = 69
    results = []

    def judge(key: str, label: str, daily: pd.Series, start: str, split: str, null: np.ndarray, real: float) -> None:
        d = daily[daily.index >= start].resample("ME").sum()
        v = S.judge(d, split, n_trials, float(np.mean(null < real)))
        v.update(key=key, label=label, split=split)
        results.append(v)
        print(f"  {key} {v['verdict']}", flush=True)

    periods = {"E1": (first, "2021-01-01"), "E2": ("2019-01-01", "2023-01-01"), "E3": ("2019-01-01", "2023-01-01")}
    pnl = {b: E.book_pnl(rec[b], cc) for b in rec}
    m1 = np.asarray(close.index >= periods["E1"][0])
    null = np.array([E.book_pnl(E.random_among(has & eligible, rec["e1"].sum(axis=1), rng), cc, 0.0)["gross"][m1].mean() for _ in range(args.placebo)])
    judge("E1", "通期の純利益の予想を 5% 以上引き上げた開示の後、5 日持つと全銘柄の平均より上がる", pnl["e1"]["d"], *periods["E1"], null, pnl["e1"]["gross"][m1].mean())
    m2 = np.asarray(close.index >= periods["E2"][0])
    pool = np.isfinite(ir_w) & eligible
    null = np.array([E.book_pnl(E.random_among(pool, rec["ir_num"].sum(axis=1), rng), cc, 0.0)["gross"][m2].mean() for _ in range(args.placebo)])
    judge("E2", "数字で学んだ IR 評価 AI は当たる（開示のあった株の、点数の上位 20% を 5 日持つ）", pnl["ir_num"]["d"], *periods["E2"], null, pnl["ir_num"]["gross"][m2].mean())
    base = pnl["selector"]["gross"][m2].mean()
    ir_m = np.where(eligible, ir_w, np.nan)
    null = np.array([E.book_pnl(E.top_k(sel_w + np.nan_to_num(E.shuffle_rows(ir_m, rng)), E.TOP_SHARE, E.MIN_VALID), cc, 0.0)["gross"][m2].mean() - base
                     for _ in range(args.placebo)])
    judge("E3", "判断 AI（値動き＋数字の IR 点数）は、銘柄選定 AI だけより良い", pnl["combined_num"]["d"] - pnl["selector"]["d"], *periods["E3"],
          null, pnl["combined_num"]["gross"][m2].mean() - base)

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    say("# 決算の数字で評価する（E1〜E3）")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　仮説と判定の条件: docs/agents.md 10 章（結果を見る前に決めたもの）")
    say(f"データ: 決算短信・予想の修正の XBRL {len(feat):,} 回（{numbers['ticker'].nunique()} 銘柄、{first}〜{numbers['date'].max():%Y-%m-%d}）。"
        f"コスト: 往復 {pct(E.COST, 2)}。試した数: 66 ＋ 3 ＝ {n_trials}（全期間の t 値の基準 {f2(S.expected_max_z(n_trials))}）")
    say("\n## 1. 判定\n")
    say("| 記号 | 仮説 | 期間 | 差（年率） | 前半 | 後半 | t 値（全期間） | t 値（後半） | 偶然より良い割合 | 判定 |")
    say("|---|---|---|---|---|---|---|---|---|---|")
    for v in results:
        say(f"| {v['key']} | {v['label']} | {v['start']:%Y-%m}〜{v['end']:%Y-%m}（後半 {v['split'][:4]}〜） | {pct(v['mean'] * 12)} | {pct(v['mean1'] * 12)} | "
            f"{pct(v['mean2'] * 12)} | {f2(v['t'])} | {f2(v['t2'])} | {pct(v['placebo'], 0)} | **{v['verdict']}** |")
    say("\n## 2. 細かい数字（全銘柄の平均との差、年率）\n")
    say("| 買い方 | 期間 | コストを引く前 | 1 日に入れ替える割合 | コストを引いた後 | 1 日に買う数 | 持っている割合 |")
    say("|---|---|---|---|---|---|---|")
    for b, label, m in (("e1", "E1: 5% 以上の上方修正", m1), ("ir_num", "E2: 数字の IR 点数の上位 20%", m2), ("combined_num", "E3: 値動き＋数字", m2),
                        ("selector", "銘柄選定 AI だけ", m2)):
        x = pnl[b][m]
        say(f"| {label} | {x.index[0]:%Y-%m}〜 | {pct(x['gross'].mean() * 252)} | {pct(x['turnover'].mean(), 1)} | {pct(x['d'].mean() * 252)} | "
            f"{rec[b][m].sum(axis=1).mean():.1f} | {pct(x['invested'].mean(), 0)} |")
    say("")
    say("- 記録の点検（評価 AI が点数のファイルから計算し直したものとの食い違い）: " + "、".join(f"{b} {v} 件" for b, v in audit.items()))
    y = I.target_rank(close)
    rows = pd.DataFrame({"year": close.index.year[t[ok]], "score": num["score"].to_numpy()[ok], "y": y[t[ok], j[ok]]}).dropna()
    ic = rows.groupby("year").apply(lambda g: g["score"].corr(g["y"]), include_groups=False)
    say("- 数字の IR 点数と、実際のその後 5 日の順位の相関（年ごと）: " + "、".join(f"{yr}: {v:+.3f}" for yr, v in ic.items()))
    say("\n## 3. 参考（判定しない）: 予想の変化のあと 5 日・20 日の「株 − 全銘柄の平均」（コストを引く前）\n")
    ex5, ex20 = E.excess_forward(cc, 5), E.excess_forward(cc, 20)
    say("| 条件 | 回数 | 5 日後の平均 | t 値 | 20 日後の平均 | t 値 |")
    say("|---|---|---|---|---|---|")
    for label, mask in (("通期の純利益の予想を 5% 以上引き上げ", rev_w >= N.UP_REVISION), ("−5%〜+5%（ほぼ据え置き）", np.abs(rev_w) < N.UP_REVISION),
                        ("5% 以上引き下げ", rev_w <= -N.UP_REVISION), ("予想の変化が分からない開示", has & ~np.isfinite(rev_w))):
        mk = mask & eligible
        a, b = ex5[mk], ex20[mk]
        say(f"| {label} | {int(mk.sum()):,} | {pct(np.nanmean(a), 2)} | {f2(S.t_value(a))} | {pct(np.nanmean(b), 2)} | {f2(S.t_value(b))} |")
    kinds = {1: "第 1 四半期", 2: "第 2 四半期", 3: "第 3 四半期", 4: "本決算", 5: "業績予想の修正", 6: "配当予想の修正"}
    say("\n開示の種類ごとの回数: " + "、".join(f"{kinds.get(int(k), k)} {v:,}" for k, v in num["kind"].value_counts().sort_index().items())
        + f"。予想の変化率が計算できたもの {int(num['fc_rev'].notna().sum()):,} 件")
    say("\n## 4. 読み方・限界\n")
    say("- 判定は 4 つの条件すべて（前半・後半ともプラス、後半だけで t 値 1.65 以上、全期間の t 値が試した数で割り引いた基準以上、偶然より良い割合 95% 以上）")
    say("- 数字は 2016-10 からしか残っていない。今の構成銘柄（生存者バイアス）。買うのは開示から 2 時間半以上たった後。税金は入れていない")
    say("- 表 3 は開示ごとに 1 回と数えた平均で、同じ日に重なる開示どうしの関係（同じ日は同じ向きに動きやすい）を考えていないので、t 値は大きめに出る")
    (out / "earnings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    pd.DataFrame(results).drop(columns=["start", "end"]).to_csv(out / "verdicts_earnings.csv", index=False)
    print(f"\n保存先: {out}/earnings.md")


# --------------------------------------------------------------------------- まだ入れていなかった項目（11 章）
def cmd_extra(args) -> None:
    """入力を作る → 銘柄選定・IR 評価（数字）を入力を足して学び直す → 点数のファイルだけで採点する（F1〜F4）。"""
    from cfdbot import stocks_short as H
    from cfdbot.agents import collector as C
    from cfdbot.agents import earnings as N
    from cfdbot.agents import evaluator as E
    from cfdbot.agents import extra as X
    from cfdbot.agents import ir_scorer as I
    from cfdbot.agents import selector

    def log(text: str) -> None:
        print(text, flush=True)

    p = paths(args.root)
    data = Path(args.data)
    for key, hint in (("selector", "./cfd agents select"), ("ir_numbers", "./cfd agents earnings")):
        if not p[key].exists():
            sys.exit(f"{p[key]} が無い。先に {hint} を実行する")
    open_, close = prices(data)
    tickers = list(close.columns)
    raw_dir = data / "jp_raw"
    if args.fetch:
        for _ in range(3):                                                         # 取れなかった銘柄は、あと 2 回まで試す
            done, failed = X.fetch_raw(tickers, raw_dir, log=log)
            if failed == 0:
                break
    if not raw_dir.exists() or not any(raw_dir.glob("*.csv")):
        sys.exit("出来高のデータが無い。先に ./cfd agents extra --fetch を実行する（1 秒に 1 社で 10 分ほど）")
    rawclose, volume, splits = X.load_raw(raw_dir, close.index, tickers)
    rawclose.to_csv(data / "jp_rawclose.csv", float_format="%.4f")
    volume.to_csv(data / "jp_volume.csv", float_format="%.0f")
    splits.to_csv(data / "jp_splits.csv", index=False)
    have_raw = int(rawclose.notna().any().sum())
    log(f"出来高・終値・分割: {have_raw}/{len(tickers)} 銘柄、分割 {len(splits)} 回")

    # ---- 入力を作る（どれも、その日の 13:30 までに分かる値）
    cc = E.returns(close)
    sector = pd.read_csv(data / "universe_jp.csv").set_index("ticker")["sector"]
    vol_f = X.volume_features(cc, rawclose, volume)
    ind_f = X.industry_features(close, sector)
    disc = C.load_disclosures(p["disclosures"])
    feat = N.build(disc, p["disclosures"].parent, log=log, extra=True, skip_corrections=True)   # 訂正の開示（古い期の数字の出し直し）は使わない
    if feat.empty:
        sys.exit("決算の数字が無い。先に ./cfd agents collect --xbrl-history を実行する")
    have = {q.stem for q in (p["disclosures"].parent / "xbrl").glob("*/*.csv")}                 # 数字を保存してある開示だけで数える
    n_corr = int(N.is_correction(disc[disc["xbrl"].str.replace(".zip", "", regex=False).isin(have)]["title"]).sum())
    fund_f = X.fundamental_panel(feat[["ticker", "time"] + N.STATE + N.STATE_TIMES], I.decision_pos(feat["time"], close.index), rawclose, splits)

    # ---- 銘柄選定: 入力を足した版を学び直して、点数を書く
    for key, extra, first_year, label in (("selector_volume", vol_f, H.FIRST_TEST_YEAR, "出来高を足した版"),
                                          ("selector_fundamental", {**fund_f, **ind_f}, 2019, "割安さ・大きさ・業種を足した版")):
        if args.reuse and p[key].exists():
            continue
        log(f"銘柄選定（{label}）")
        sc = selector.scores_extra(open_, close, extra, first_year=first_year, log=log)
        sc[sc.notna().any(axis=1)].to_csv(p[key], float_format="%.5f")

    # ---- IR 評価（数字）: 入力を 12 個にして学び直して、点数を書く
    cols = N.FEATURES + N.EXTRA
    table = N.daily(feat, close.index, tickers, features=N.FEATURES + N.EXTRA[:3])
    pos, j = table["pos"].to_numpy(), table["j"].to_numpy()
    night = H.parts(open_, close)["night"]
    table["ep_fc"], table["size"] = fund_f["ep_fc"].to_numpy()[pos, j], fund_f["size"].to_numpy()[pos, j]
    table["gap_ex"] = night.sub(night.mean(axis=1), axis=0).to_numpy()[pos, j]
    log("IR 評価（数字・入力 12 個）")
    score = N.walk_forward(table, I.target_rank(close), close.index, log=log, features=cols)
    log("IR 評価（数字・入力 6 個のまま。同じ開示で学び直す）")
    score6 = N.walk_forward(table, I.target_rank(close), close.index, log=log)
    numbers = table.assign(date=close.index[pos], ticker=close.columns[j], score=score, score6=score6)
    numbers[["date", "ticker", "score", "score6"] + cols].to_csv(p["ir_numbers_extra"], index=False, float_format="%.6f")

    # ---- 判断の評価: 点数のファイルと値段だけで採点する
    index = close.index

    def matrix(path: Path) -> np.ndarray:
        return wide(path).reindex(index=index, columns=close.columns).to_numpy(dtype=float)

    def from_rows(d: pd.DataFrame, col: str) -> np.ndarray:
        t, k = index.get_indexer(d["date"]), close.columns.get_indexer(d["ticker"])
        ok = (t >= 0) & (k >= 0)
        w = np.full(close.shape, np.nan)
        w[t[ok], k[ok]] = d[col].to_numpy(dtype=float)[ok]
        return w

    sel_w = matrix(p["selector"])
    eligible = np.isfinite(sel_w)
    num = pd.read_csv(p["ir_numbers_extra"], parse_dates=["date"])
    old = pd.read_csv(p["ir_numbers"], parse_dates=["date"])
    new_ir, old_ir = np.where(eligible, from_rows(num, "score"), np.nan), np.where(eligible, from_rows(num, "score6"), np.nan)
    rec_ir, rec_rev = np.where(eligible, from_rows(old, "score"), np.nan), from_rows(old, "fc_rev")     # E1〜E3 の記録（訂正の開示を含む）
    prog, rev = from_rows(num, "prog_diff"), from_rows(num, "fc_rev")
    rng = np.random.default_rng(11)
    n_trials = 73
    results, detail = [], []

    def judge(key: str, label: str, daily: pd.Series, start: str, split: str, null: np.ndarray, real: float) -> None:
        d = daily[daily.index >= start].resample("ME").sum()
        v = S.judge(d, split, n_trials, float(np.mean(null < real)))
        v.update(key=key, label=label, split=split)
        results.append(v)
        log(f"  {key} {v['verdict']}")

    def counter(start: str, hold: int = E.HOLD):
        """start からだけの表にして数える（偶然との比べ方を速くするため。本物も同じ数え方で比べる）。返すのは（最初の行, 数える関数）。"""
        i0 = int(index.searchsorted(pd.Timestamp(start)))
        part = cc.iloc[i0:]
        return i0, lambda book: float(E.book_pnl(book, part, 0.0, hold)["gross"].mean())

    def note(label: str, book: np.ndarray, pnl: pd.DataFrame, start: str) -> None:
        m = np.asarray(index >= start)
        x = pnl[m]
        detail.append((label, f"{x.index[0]:%Y-%m}〜", x["gross"].mean() * 252, x["turnover"].mean(), x["d"].mean() * 252, book[m].sum(axis=1).mean(), x["invested"].mean()))

    base_book = E.top_k(sel_w, E.TOP_SHARE, E.MIN_VALID)
    pnl_base = E.book_pnl(base_book, cc)
    for key, path_key, label, name, start, split in (
            ("F1", "selector_volume", "銘柄選定 AI に出来高の入力 5 個を足すと良くなる", "出来高を足した版", "2010-01-01", "2018-01-01"),
            ("F2", "selector_fundamental", "銘柄選定 AI に割安さ・大きさ・業種の入力 9 個を足すと良くなる", "割安さ・大きさ・業種を足した版", "2019-01-01", "2023-01-01")):
        new_w = matrix(p[path_key])
        book = E.top_k(new_w, E.TOP_SHARE, E.MIN_VALID)
        pnl_new = E.book_pnl(book, cc)
        i0, gross = counter(start)
        diff, base_gross = (new_w - sel_w)[i0:], gross(base_book[i0:])
        null = np.array([gross(E.top_k(sel_w[i0:] + E.shuffle_rows(diff, rng), E.TOP_SHARE, E.MIN_VALID)) - base_gross for _ in range(args.placebo)])
        judge(key, label, pnl_new["d"] - pnl_base["d"], start, split, null, gross(book[i0:]) - base_gross)
        note(f"{key}: {name}", book, pnl_new, start)
        note(f"{key} の比べる相手: 今の銘柄選定 AI", base_book, pnl_base, start)

    hold3, start3 = 60, "2017-07-01"
    unchanged = np.abs(rev) < N.UP_REVISION                                          # 通期の純利益の予想は据え置き
    f3 = (prog >= N.PROGRESS_UP) & unchanged & eligible
    pool3 = np.isfinite(prog) & eligible
    pnl3 = E.book_pnl(f3, cc, E.COST, hold3)
    i0, gross = counter(start3, hold3)
    null = np.array([gross(E.random_among(pool3[i0:], f3[i0:].sum(axis=1), rng)) for _ in range(args.placebo)])
    judge("F3", "進捗率が前年より 5 ポイント以上高いのに通期の予想を据え置いた株を、60 営業日持つと全銘柄の平均より上がる", pnl3["d"], start3, "2022-01-01",
          null, gross(f3[i0:]))
    note("F3: 進捗率が高い・予想は据え置き（60 日持つ）", f3, pnl3, start3)

    start4 = "2019-01-01"
    book_new, book_old = E.top_k(new_ir, E.IR_TOP, E.IR_MIN), E.top_k(old_ir, E.IR_TOP, E.IR_MIN)
    pnl_new, pnl_old = E.book_pnl(book_new, cc), E.book_pnl(book_old, cc)
    i0, gross = counter(start4)
    null = np.array([gross(E.random_among(np.isfinite(new_ir)[i0:], book_new[i0:].sum(axis=1), rng)) for _ in range(args.placebo)])
    judge("F4", "数字の IR 評価 AI に入力 6 個を足すと良くなる（点数の上位 20% を 5 日持つ）", pnl_new["d"] - pnl_old["d"], start4, "2023-01-01",
          null, gross(book_new[i0:]))
    note("F4: 入力 12 個の版", book_new, pnl_new, start4)
    note("F4 の比べる相手: 入力 6 個の版（同じ開示で学び直し）", book_old, pnl_old, start4)

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    say("# まだ入れていなかった項目を足す（F1〜F4）")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　仮説と判定の条件: docs/agents.md 11 章（結果を見る前に決めたもの）")
    say(f"データ: 日本株 {len(tickers)} 銘柄（〜{index[-1]:%Y-%m-%d}）。出来高・分割は Yahoo の日足（{have_raw} 銘柄、分割 {len(splits)} 回）、"
        f"決算の数字は XBRL {len(feat):,} 回。コスト: 往復 {pct(E.COST, 2)}。試した数: 69 ＋ 4 ＝ {n_trials}（全期間の t 値の基準 {f2(S.expected_max_z(n_trials))}）")
    say("\n## 1. 判定\n")
    say("| 記号 | 仮説 | 期間 | 差（年率） | 前半 | 後半 | t 値（全期間） | t 値（後半） | 偶然より良い割合 | 判定 |")
    say("|---|---|---|---|---|---|---|---|---|---|")
    for v in results:
        say(f"| {v['key']} | {v['label']} | {v['start']:%Y-%m}〜{v['end']:%Y-%m}（後半 {v['split'][:4]}〜） | {pct(v['mean'] * 12)} | {pct(v['mean1'] * 12)} | "
            f"{pct(v['mean2'] * 12)} | {f2(v['t'])} | {f2(v['t2'])} | {pct(v['placebo'], 0)} | **{v['verdict']}** |")
    say("\n## 2. 細かい数字（全銘柄の平均との差、年率）\n")
    say("| 買い方 | 期間 | コストを引く前 | 1 日に入れ替える割合 | コストを引いた後 | 1 日に買う数 | 持っている割合 |")
    say("|---|---|---|---|---|---|---|")
    for label, period, g, turn, d, names, invested in detail:
        say(f"| {label} | {period} | {pct(g)} | {pct(turn, 1)} | {pct(d)} | {names:.1f} | {pct(invested, 0)} |")
    y = I.target_rank(close)
    say("")
    for label, w in (("入力 12 個の版", new_ir), ("入力 6 個の版", old_ir)):
        t, k = np.nonzero(np.isfinite(w) & np.isfinite(y))
        rows = pd.DataFrame({"year": index.year[t], "score": w[t, k], "y": y[t, k]})
        ic = rows.groupby("year").apply(lambda g: g["score"].corr(g["y"]), include_groups=False)
        say(f"- {label}の点数と、実際のその後 5 日の順位の相関（年ごと）: " + "、".join(f"{yr}: {v:+.3f}" for yr, v in ic.items()))
    same = old.merge(num, on=["date", "ticker"], how="outer", suffixes=("_old", "_new"), indicator=True)
    both = same[same["_merge"] == "both"]
    say(f"- 訂正の開示 {n_corr:,} 回を除いた。株 × 日の行は {len(num):,} 件（E1〜E3 の記録は {len(old):,} 件）。どちらにもある {len(both):,} 件のうち、"
        f"通期の予想の変化率が違う行 {int((~np.isclose(both['fc_rev_old'], both['fc_rev_new'], equal_nan=True, atol=1e-5)).sum())} 件")

    say("\n## 3. 参考（判定しない）: 入力ごとの「上位 20% − 下位 20%」の、その後の値動きの差（コストを引く前）\n")
    say("数字は、その入力が大きい株のグループと小さい株のグループの、その後 5 日・20 日の値動きの差。重なりを避けるため、5 日おき・20 日おきの日だけで数えた。\n")
    ex = {h: E.excess_forward(cc, h) for h in (5, 20, 60)}
    say("| 入力 | 期間 | 5 日後の差 | t 値 | 20 日後の差 | t 値 |")
    say("|---|---|---|---|---|---|")
    for names, feats, start in ((X.VOLUME, vol_f, "2010-01-01"), (X.FUNDAMENTAL, fund_f, "2017-01-01"), (X.INDUSTRY, ind_f, "2010-01-01")):
        m = np.asarray(index >= start)
        for name in names:
            a, b = X.spread(feats[name], ex[5], m, 5), X.spread(feats[name], ex[20], m, 20)
            say(f"| {X.LABELS[name]} | {start[:4]}〜 | {pct(a[0], 2)} | {f2(a[1])} | {pct(b[0], 2)} | {f2(b[1])} |")

    say("\n## 4. 参考（判定しない）: 進捗率と出来高の急増\n")
    say("| 条件（第 1〜3 四半期の決算短信） | 回数 | 5 日後の平均 | 20 日後の平均 | 60 日後の平均 | t 値（60 日） |")
    say("|---|---|---|---|---|---|")
    m3 = np.asarray(index >= start3)[:, None]
    for label, mask in (("進捗率が前年より 5 ポイント以上高い・予想は据え置き（F3）", f3), ("進捗率の差が ±5 ポイント未満・予想は据え置き", (np.abs(prog) < N.PROGRESS_UP) & unchanged & eligible),
                        ("進捗率が前年より 5 ポイント以上低い・予想は据え置き", (prog <= -N.PROGRESS_UP) & unchanged & eligible),
                        ("進捗率の差が計算できた開示すべて", pool3)):
        mk = mask & m3
        say(f"| {label} | {int(mk.sum()):,} | {pct(np.nanmean(ex[5][mk]), 2)} | {pct(np.nanmean(ex[20][mk]), 2)} | {pct(np.nanmean(ex[60][mk]), 2)} | {f2(S.t_value(ex[60][mk]))} |")
    say("")
    say("| 買い方 | 期間 | コストを引く前（年率） | コストを引いた後（年率） | 1 日に買う数 | 持っている割合 |")
    say("|---|---|---|---|---|---|")
    surge = X.volume_surge(volume).to_numpy() & eligible
    for label, book, hold, start in (("F3 を 5 日持つ形にした場合", f3, 5, start3), ("F3 を 20 日持つ形にした場合", f3, 20, start3),
                                     ("出来高の急増（前日の出来高が 50 日の上位 10%）の後、20 日持つ", surge, 20, "2010-01-01")):
        m = np.asarray(index >= start)
        x = E.book_pnl(book, cc, E.COST, hold)[m]
        say(f"| {label} | {start[:7]}〜 | {pct(x['gross'].mean() * 252)} | {pct(x['d'].mean() * 252)} | {book[m].sum(axis=1).mean():.1f} | {pct(x['invested'].mean(), 0)} |")
    cover = np.isfinite(fund_f["ep_fc"].to_numpy())[np.asarray(index >= "2019-01-01")].mean()
    say(f"\n進捗率の差が計算できた開示 {int(np.isfinite(prog).sum()):,} 回（うち 2017-07 から {int((np.isfinite(prog) & m3).sum()):,} 回）。"
        f"予想益回りが作れた割合（2019 年から、株 × 日）{pct(cover, 0)}")
    say("\n## 5. 参考（判定しない）: 訂正の開示を除くと、10 章の E1・E2 はどう変わるか\n")
    say("10 章の E1〜E3 は、過去の決算短信の「訂正」を新しい開示として数えていた（2026-10-13 の夜、11 章の準備で気づいた）。判定は 10.5 のまま変えない。\n")
    say("| 買い方 | 期間 | 訂正を含む（10.5 の記録と同じ作り） | t 値 | 訂正を除く | t 値 |")
    say("|---|---|---|---|---|---|")
    for label, with_c, without_c, start in (("E1: 通期の純利益の予想を 5% 以上引き上げ → 5 日持つ", (rec_rev >= N.UP_REVISION) & eligible, (rev >= N.UP_REVISION) & eligible, "2016-10-01"),
                                            ("E2: 数字の IR 点数（入力 6 個）の上位 20% → 5 日持つ", E.top_k(rec_ir, E.IR_TOP, E.IR_MIN), book_old, start4)):
        cells = []
        for book in (with_c, without_c):
            d = E.book_pnl(book, cc)["d"]
            d = d[d.index >= start].resample("ME").sum()
            cells += [pct(d.mean() * 12), f2(S.t_value(d.to_numpy()))]
        say(f"| {label} | {start[:7]}〜 | " + " | ".join(cells) + " |")
    say("\n## 6. 読み方・限界\n")
    say("- 判定は 4 つの条件すべて（前半・後半ともプラス、後半だけで t 値 1.65 以上、全期間の t 値が試した数で割り引いた基準以上、偶然より良い割合 95% 以上）")
    say("- F1・F2・F4 の「差」は、入力を足した版から今の版を引いたもの。足した版そのものの成績は 2 章")
    say("- 割安さ・大きさは、決算短信の株数と Yahoo の株式分割の記録から作った。分割の前後で数え方がずれた会社があると、その会社の値は次の決算までまちがう")
    say("- 今の構成銘柄（生存者バイアス）。決算の数字は 2016-10 から。税金は入れていない")
    say("- 3〜5 章は探して集計した表で、開示どうしの重なりも考えていないので、t 値は大きめに出る。ここで良く見えたものは、これからのデータで確かめ直す")
    (out / "extra.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    pd.DataFrame(results).drop(columns=["start", "end"]).to_csv(out / "verdicts_extra.csv", index=False)
    print(f"\n保存先: {out}/extra.md")


# --------------------------------------------------------------------------- まとめて・状態
def run_stamp(what: str) -> None:
    """自動実行の段階の時刻を run.log に残す（./cfd agents check と orders --report が、何分かかったかを読む）。"""
    print(f"[{pd.Timestamp.now(tz=JST).tz_localize(None):%Y-%m-%d %H:%M:%S}] 自動実行: {what}", flush=True)


def cmd_run(args) -> None:
    """値段 → 決算の数字の表 → （予行演習）E1 の一覧と今日の注文の一覧・知らせ → 情報収集 → IR → 銘柄選定 → 判断（台帳）→ 評価。

    E1 の一覧に要るのは、決算の数字の表と、銘柄選定の点数の付く株（値段だけで分かる）なので、時間のかかる学び直しの前に作る
    （docs/stocks_live.md 4.2）。台帳（ledger.csv・ledger_extra.csv）の中身と書く順番・時刻の決まりは変えない。
    予行演習の部分が失敗しても、台帳は書く。
    """
    run_stamp("始め")
    if args.prices:
        sys.path.insert(0, str(ROOT / "scripts"))
        import stocks_study as M

        data = Path(args.data)
        uni = pd.read_csv(data / "universe_jp.csv")
        print("値段を取り直す（日本株）…", flush=True)
        o, c, dv = M.fetch_daily(uni["ticker"].tolist(), "2000-01-01")
        o.to_csv(data / "jp_open.csv", float_format="%.6g")
        c.to_csv(data / "jp_close.csv", float_format="%.6g")
        dv.to_csv(data / "jp_div.csv", index=False)
        run_stamp("値段")
    cmd_numbers(args)
    run_stamp("決算の数字の表")
    try:
        if live_early(args):
            run_stamp("E1 の一覧と注文の一覧")
    except Exception as e:  # noqa: BLE001
        print(f"予行演習: 先に作る一覧は作れなかった（{type(e).__name__}: {str(e)[:120]}）。判断の後にもう一度作る", flush=True)
    for name, f in (("情報収集", cmd_collect), ("IR 評価", cmd_ir), ("銘柄選定", cmd_select), ("判断（台帳）", cmd_decide)):
        f(args)
        run_stamp(name)
    try:
        live_after_decide(args)
    except Exception as e:  # noqa: BLE001
        print(f"予行演習: 判断の後の確かめができなかった（{type(e).__name__}: {str(e)[:120]}）。台帳は書いてある", flush=True)
    cmd_evaluate(args)
    run_stamp("終わり")


def live_early(args) -> bool:
    """予行演習の設定があれば、E1 の一覧を先に作り（decider.e1_preview）、今日の注文の一覧を書いて iPhone に知らせる。作ったら True。"""
    from cfdbot.agents import decider as D
    from cfdbot.agents import live as L

    p = paths(args.root)
    files = L.LiveFiles(p["root"] / "live")
    cfg = files.config()
    if cfg is None:
        print("予行演習: 設定が無いので、注文の一覧は作らない", flush=True)
        return False
    if not p["revisions"].exists():
        print("予行演習: 決算の数字の表が無いので、先に作る一覧は作らない（判断の後に台帳から作る）", flush=True)
        return False
    open_, close = prices(Path(args.data))
    now = pd.Timestamp.now(tz=JST).tz_localize(None) if not getattr(args, "now", None) else pd.Timestamp(args.now)
    rev = pd.read_csv(p["revisions"], parse_dates=["date"])
    names = pd.read_csv(Path(args.data) / "universe_jp.csv").set_index("ticker")["name"].to_dict()
    day, e1 = D.e1_preview(open_, close, rev, names)
    if day is None or day != now.normalize():
        print(f"予行演習: 今日の値段がまだ無い（最後の日 {day:%Y-%m-%d}）ので、先に作る一覧は作らない" if day is not None else
              "予行演習: 値段が無いので、先に作る一覧は作らない", flush=True)
        return False
    cal = L.calendar(close.index, now.normalize())
    r = orders_today(p, files, cfg, close, cal, now, e1_override=e1, decided_override=f"{now:%Y-%m-%d %H:%M:%S}",
                     source="先に作った E1 の一覧", say=lambda s: print(s, flush=True))
    if r["written"]:
        send_notice(files, cfg, r["rows"], now)
    return True


def live_after_decide(args) -> None:
    """判断（台帳）の後: 今日の一覧がまだ無ければ台帳から作って知らせる。先に作った一覧があれば、台帳の E1 の帳と同じかを確かめる。"""
    from cfdbot.agents import live as L

    p = paths(args.root)
    files = L.LiveFiles(p["root"] / "live")
    cfg = files.config()
    if cfg is None:
        return
    _, close = prices(Path(args.data))
    now = pd.Timestamp.now(tz=JST).tz_localize(None) if not getattr(args, "now", None) else pd.Timestamp(args.now)
    today = now.normalize()
    plan = files.plan()
    mode = cfg.get("mode", "dry")
    done = plan[(plan["mode"] == mode) & (plan["date"] == today)] if len(plan) else plan
    if not len(done):
        cal = L.calendar(close.index, today)
        r = orders_today(p, files, cfg, close, cal, now, say=lambda s: print(s, flush=True))
        if r["written"]:
            send_notice(files, cfg, r["rows"], now)
        return
    diff = e1_mismatch(p, done, today)
    if diff:
        print(f"予行演習: 先に作った E1 の一覧と、台帳の E1 の帳が違う（{diff}）。チームに戻す（docs/stocks_live.md 4.2）", flush=True)
        title = f"株の予行演習（確かめ）{now:%m/%d}"
        notify_send(files, title, f"先に知らせた一覧と、後で書いた台帳の E1 が違いました（{diff}）。注文はそのままでよい。Claude に「予行演習の様子は？」と聞いてください")
    else:
        print("予行演習: 先に作った E1 の一覧は、台帳の E1 の帳と同じ", flush=True)


def e1_mismatch(p: dict, day_plan: pd.DataFrame, day: pd.Timestamp) -> str:
    """その日の一覧の E1 の株（買い・買わない・延長の行）と、台帳の E1 の帳の株の違い（同じなら空）。一覧が台帳から作られていれば空。"""
    from cfdbot.agents import live as L

    if "source" not in day_plan or not (day_plan["source"] == "先に作った E1 の一覧").any() or not p["ledger_extra"].exists():
        return ""
    ex = pd.read_csv(p["ledger_extra"], dtype={"ticker": str})
    ex = ex[(pd.to_datetime(ex["date"].astype(str).str[:10]) == day)]
    if not len(ex):
        return ""
    a = set(day_plan.loc[day_plan["side"].isin(L.E1_SIDES), "ticker"].astype(str))
    b = set(ex.loc[ex["book"] == "e1", "ticker"].astype(str))
    if a == b:
        return ""
    return "、".join(x for x in (("一覧だけ: " + " ".join(sorted(a - b))) if a - b else "",
                                ("台帳だけ: " + " ".join(sorted(b - a))) if b - a else "") if x)


def run_timeline(log: Path) -> list[tuple[str, pd.Timestamp]]:
    """run.log の最後の自動実行の、段階ごとの時刻（run_stamp の行）。無ければ空。"""
    import re

    if not log.exists():
        return []
    pat = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] 自動実行: (.+)$")
    steps: list[tuple[str, pd.Timestamp]] = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = pat.match(line.strip())
        if not m:
            continue
        if m.group(2) == "始め":
            steps = []
        steps.append((m.group(2), pd.Timestamp(m.group(1))))
    return steps


def timeline_text(steps: list[tuple[str, pd.Timestamp]]) -> str:
    if not steps:
        return "時刻の記録がまだ無い（2026-10-15 より後の自動実行から run.log に残る）"
    t0 = steps[0][1]
    parts = [f"{t0:%m-%d %H:%M:%S} に始め"] + [f"{name} {(t - t0).total_seconds() / 60:.1f} 分（{t:%H:%M}）" for name, t in steps[1:]]
    return "、".join(parts)


def cmd_numbers(args) -> None:
    """E1 の帳（docs/agents.md 12 章）のために、直近の決算の数字から予想の変化率の表を書く。失敗しても M2 の帳は書けるよう、止めない。"""
    from cfdbot.agents import collector as C
    from cfdbot.agents import earnings as N

    p = paths(args.root)
    try:
        _, close = prices(Path(args.data))
        disc = C.load_disclosures(p["disclosures"])
        rev = N.recent_revisions(disc, p["disclosures"].parent, close.index, list(close.columns), log=lambda s: None)
        rev.to_csv(p["revisions"], index=False, float_format="%.6f")
        today = rev[rev["date"] == close.index[-1]] if len(rev) else rev
        up = int((today["fc_rev"] >= N.UP_REVISION).sum()) if len(today) else 0
        print(f"決算の数字: 直近 {N.RECENT_DAYS} 日の開示から {len(rev):,} 行。{close.index[-1]:%Y-%m-%d} の判断に使える開示 {len(today)} 社"
              f"（予想を 5% 以上引き上げ {up} 社）", flush=True)
    except Exception as e:  # noqa: BLE001
        p["revisions"].unlink(missing_ok=True)                                     # 古い表を使わない（判断は「表が無い」と書く）
        print(f"決算の数字: 表を作れなかった（{str(e)[:120]}）。今日の E1 の帳は書かない", flush=True)


# --------------------------------------------------------------------------- M2 を手で注文する形（docs/agents.md 13 章）
def cmd_manual(args) -> None:
    """今週の手で買う候補（既定）か、13.2 の過去の検証（--test）。"""
    if args.test:
        manual_test(args)
    else:
        manual_show(args)


def manual_show(args) -> None:
    from cfdbot.agents import manual as M

    p = paths(args.root)
    ex = pd.read_csv(p["ledger_extra"], dtype={"ticker": str}) if p["ledger_extra"].exists() else pd.DataFrame()
    w = M.this_week(ex)
    print("# 今週の手で買う候補（M2 を週 1 回・10 銘柄・1 日遅れにした形。docs/agents.md 13 章）")
    print("")
    if w["date"] is None:
        print(f"- まだ無い（週の最初の営業日の 13:05 の自動実行が {p['ledger_extra'].name} に帳 {M.BOOK} を書く）")
        return
    now = pd.Timestamp(args.now) if args.now else pd.Timestamp.now(tz=JST).tz_localize(None)
    this_monday = (now.normalize() - pd.Timedelta(days=now.weekday()))
    if w["date"] < this_monday:
        print(f"- ⚠ 今週の分がまだ無い（最新は {w['date']:%Y-%m-%d} の週）。週の最初の営業日の 13:05 の後にもう一度。"
              "動かなかったなら ./cfd agents check で自動実行の様子を見る")
    print(f"- 決めた日: {w['date']:%Y-%m-%d}（{w['decided_at']} に記録）。**買うのは次の営業日**。SBI の S 株なら 10:30〜13:50 に成行で出すと"
          "その日の引けの値段（14:00 より後だと翌朝の寄りになり、検証した形とずれる）")
    print("- 等分に買う（たとえば 10 万円なら 1 銘柄 1 万円ぶんの株数）。次の週の買いの日に入れ替える")
    print("")
    print("| 順位 | 銘柄 | 番号 | 点数 | 前の週から |")
    print("|---|---|---|---|---|")
    rows = w["rows"].sort_values("selector", ascending=False)
    for i, r in enumerate(rows.itertuples(), 1):
        mark = "持ち続ける" if str(r.ticker) in w["keep"] else ("新しく買う" if w["prev_date"] is not None else "買う")
        print(f"| {i} | {r.name} | {str(r.ticker).replace('.T', '')} | {float(r.selector):+.3f} | {mark} |")
    if w["prev_date"] is not None:
        print("")
        sells = "、".join(f"{w['names'].get(t, t)}（{t.replace('.T', '')}）" for t in w["sell"]) or "なし"
        print(f"- 売る（{w['prev_date']:%Y-%m-%d} の週にあって今週に無い）: {sells}")
    print("")
    print("- M2 は「まだ判断できない」。13.2 の M2b が「使えそう」になるまでは、検証中のルールが選んだ銘柄で、勧めではない。"
          "買うなら余剰資金の中で、金額の上限を先に決める")


def manual_test(args) -> None:
    from cfdbot.agents import manual as M

    p = paths(args.root)
    if not p["selector"].exists():
        sys.exit(f"{p['selector']} が無い。先に ./cfd agents select（または run）を実行する")
    open_, close = prices(Path(args.data))
    score = wide(p["selector"])
    print("M2a・M2b を計算する（偶然との比べ 300 回ずつ。数分）…", flush=True)
    r = M.evaluate(score, open_, close, placebo=args.placebo)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    a0, a1 = r["period"]
    say("# M2 を手で注文する形の過去の検証（M2a・M2b）")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　判定の条件: docs/agents.md 13.2（結果を見る前に決めたもの）")
    say(f"データ: 日本株 {close.shape[1]} 銘柄、M2 の点数（{p['selector'].name}）、{a0:%Y-%m}〜{a1:%Y-%m}（後半 {M.SPLIT[:4]}〜）。"
        f"コスト 往復 {pct(M.COST, 2)}。差 = 持っている株 − 全銘柄の平均 − 入れ替えた割合 × コスト")
    say("")
    say("## 1. 判定")
    say("")
    say("| 記号 | 形 | 差（年率） | 前半 | 後半 | t 値（全期間） | t 値（後半） | 基準 | 偶然より良い割合 | 判定 |")
    say("|---|---|---|---|---|---|---|---|---|---|")
    for key, label in (("M2a", "1 日遅れの 5 日保有（上位 10%）"), ("M2b", "週 1 回・10 銘柄・1 日遅れ")):
        v = r[key]
        say(f"| {key} | {label} | {pct(v['mean'] * 12)} | {pct(v['mean1'] * 12)} | {pct(v['mean2'] * 12)} | {f2(v['t'])} | {f2(v['t2'])} | "
            f"{f2(v['threshold'])} | {pct(v['placebo'], 0)} | **{v['verdict']}** |")
    say("")
    for key in ("M2a", "M2b"):
        v = r[key]
        say(f"- {key}: コストを引く前 年 {pct(v['gross_ann'])}、1 日に入れ替える割合 {pct(v['turnover'], 1)}、コストを引いた後 年 {pct(v['net_ann'])}。"
            f"そのまま持った成績（市場の値動きを含む）: 年 {pct(v['total']['ann'])}・シャープ {f2(v['total']['sharpe'])}・最大の下げ {pct(v['total']['mdd'])}"
            f"（全銘柄を持ち続けた場合: 年 {pct(v['hold']['ann'])}・{f2(v['hold']['sharpe'])}・{pct(v['hold']['mdd'])}）")
        yr = v["by_year"]
        say("  - 年ごとの d: " + "、".join(f"{y}: {x:+.1%}" for y, x in yr.items()) + f"（マイナスの年 {int((yr < 0).sum())}/{len(yr)}）")
    say("")
    say("## 2. 参考（判定しない）: 遅れと形でどれだけ変わるか")
    say("")
    say("| 形 | コストを引く前（年率） | コストを引いた後 | 後半（2018〜） | 1 日に入れ替える割合 |")
    say("|---|---|---|---|---|")
    for name, x in r["ref"].items():
        say(f"| {name} | {pct(x['gross_ann'])} | {pct(x['net_ann'])} | {pct(x['net_late'])} | {pct(x['turnover'], 1)} |")
    say("")
    say("## 3. 読み方・限界（docs/agents.md 13.4）")
    say("")
    say("- 今の TOPIX500 の銘柄だけ（生存者バイアス）。下がった株を買う側に寄る M2 では、過去の成績が良く出やすい")
    say("- 10 銘柄は少なく、1 社の出来事で大きく動く。税金は入れていない。S 株の受付の時刻・受注停止で出せない週がある")
    say(f"- 試した数: M2a {M.TRIALS['M2a']}・M2b {M.TRIALS['M2b']}（81 ＋ 1C・案 2 ＋ C3 ＋ この 2 つ）")
    say("- 先の記録（帳 m2_manual）の判定の条件は、この結果を見た後・先の記録を採点する前に docs/agents.md 13.5 に書く（M2b が「使えない」なら判定しない）")
    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "manual.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("")
    print(f"保存先: {out / 'manual.md'}")


# --------------------------------------------------------------------------- J7 決算発表をまたいで持つ（docs/earnings_premium.md）
def cmd_premium(args) -> None:
    """3 章の過去の検証（--test。使っていない決算だけ）か、4 章の先の記録の途中経過（既定）。"""
    from cfdbot import earnings_premium as EP

    p = paths(args.root)
    if not p["disclosures"].exists():
        sys.exit(f"{p['disclosures']} が無い。先に ./cfd agents collect --backfill を実行する")
    disc = pd.read_csv(p["disclosures"], dtype=str, keep_default_na=False)
    open_, close = prices(Path(args.data))
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    say("# J7 決算発表をまたいで持つ")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　決まり: docs/earnings_premium.md（結果を見る前に決めたもの）。お金は動かしていない")
    say("- 決算短信が値段に入る最初の日 t0 の前の営業日の引けで買い、t0 の次の営業日の引けで売る。差 = 持っている株 − 全銘柄の平均 − 入れ替えた割合 × 0.10%")
    say("")
    if args.test:
        earn_path = Path(args.data) / "jp_earnings.csv"
        if not earn_path.exists():
            sys.exit(f"{earn_path}（docs/stocks.md の C7 で使った Yahoo の決算日）が無い。どこまでが「見た」決算か分からないので計算しない"
                     "（./cfd stocks --fetch で取る）")
        print(f"使っていない決算だけで計算する（偶然との比べ {args.placebo} 回。数分）…", flush=True)
        r = EP.evaluate_past(disc, pd.read_csv(earn_path), open_, close, placebo=min(args.placebo, EP.PLACEBO))
        say("## 1. 過去の検証（3 章。C7 で見た Yahoo の決算日より前の決算だけ）")
        say("")
        say(f"- 使う決算: {r['events']:,} 回（{r['firms']} 社）" + (f"、{r['start']:%Y-%m}〜{r['end']:%Y-%m}（{r['months']} か月）" if r.get("months") else "")
            + f"。見たので使わない決算: {r['seen_events']:,} 回")
        if "judge" in r:
            j = r["judge"]
            say("")
            say("| 差（年率） | 前半 | 後半 | t 値（全期間） | t 値（後半） | 基準 | 偶然より良い割合 | 判定 |")
            say("|---|---|---|---|---|---|---|---|")
            say(f"| {pct(j['mean'] * 12)} | {pct(j['mean1'] * 12)} | {pct(j['mean2'] * 12)} | {f2(j['t'])} | {f2(j['t2'])} | {f2(j['threshold'])} | "
                f"{pct(j['placebo'], 0)} | **{j['verdict']}** |")
            say(f"- 前半・後半の境目: {r['split']:%Y-%m}。試した数 {EP.TRIALS}")
        else:
            say(f"- **{r['verdict']}**")
        say("")
        say("参考（判定しない）")
        say("")
        say(f"- 使う決算の 2 日の平均（株 − 全銘柄の平均、コストを引く前）: {pct(r['two_day'], 3)}。見た決算では {pct(r['seen_two_day'], 3)}"
            "（docs/stocks.md の「+0.15%」に当たる）")
        if r.get("months"):
            say(f"- 使う期間の差の年率: コストを引く前 {pct(r['gross_ann'])}・引いた後 {pct(r['net_ann'])}・コスト 0 {pct(r['zero_cost_ann'])}。"
                f"1 日に入れ替える割合 {pct(r['turnover'], 1)}")
        if "seen" in r:
            sn = r["seen"]
            say(f"- 見た期間（{sn['start']:%Y-%m}〜{sn['end']:%Y-%m}）の同じ計算: コストを引く前 {pct(sn['gross_ann'])}・引いた後 {pct(sn['net_ann'])}")
    else:
        r = EP.forward(disc, open_, close)
        say(f"## 1. 先の記録（4 章。{EP.START_FORWARD} から）")
        say("")
        say(f"- 営業日 {r['days']} 日・決算 {r['events']} 回（値段は {close.index[-1]:%Y-%m-%d} まで）")
        if len(r["daily"]):
            say(f"- 合計（コストの後）{pct(float(r['daily'].sum()), 2)}、2 日の平均（コストを引く前）{pct(r['two_day'], 3)}")
            say("- 月ごと: " + "、".join(f"{d:%Y-%m} {v:+.2%}" for d, v in r["monthly"].items()))
        say(f"- 判定: **{r['verdict']}**（250・500・750 営業日の時点だけ。間の数字を見て何も変えない。試した数に入れない）")
    say("")
    say("限界: 発表の日を前もって知っている前提（予定日はずれることがある）。今の TOPIX500 の銘柄だけ。税金は入れていない")
    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "premium.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("")
    print(f"保存先: {out / 'premium.md'}")


def cmd_status(args) -> None:
    p = paths(args.root)
    for label, key, col in (("情報収集（開示）", "disclosures", "time"), ("IR 評価（点数）", "ir_scores", "date"),
                            ("判断（記録）", "decisions", "date"), ("判断（台帳）", "ledger", "date")):
        if p[key].exists():
            d = pd.read_csv(p[key], usecols=[col], dtype=str)[col]
            print(f"{label}: {len(d):,} 行、{d.min()[:10]}〜{d.max()[:16]}")
        else:
            print(f"{label}: まだ無い")
    ir_root = p["disclosures"].parent
    print(f"情報収集（本文）: {sum(1 for _ in ir_root.glob('text/*/*.txt')):,} 件、（決算の数字）: {sum(1 for _ in ir_root.glob('xbrl/*/*.csv')):,} 件")
    if p["selector"].exists():
        d = pd.read_csv(p["selector"], usecols=[0]).iloc[:, 0]
        print(f"銘柄選定（点数）: {len(d):,} 日、{d.min()[:10]}〜{d.max()[:10]}")
    else:
        print("銘柄選定（点数）: まだ無い")
    reports = sorted(Path(args.out).glob("2*/report.md"))
    print(f"判断の評価（報告書）: {reports[-1] if reports else 'まだ無い'}")


def schedule_state(job: str = "run") -> list[str]:
    """自動実行（launchd）の登録と、最後に動いたときの終わり方。Mac 以外では「確かめられない」と返す。"""
    import os
    import subprocess

    label = JOBS[job][0]
    if sys.platform != "darwin":
        return [f"自動実行（{label}）: この環境は Mac ではないので確かめられない"]
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    lines = [f"自動実行の登録ファイル: {'ある' if plist.exists() else '無い（./cfd agents schedule --job ' + job + ' で登録）'}（{plist}）"]
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True)
    if r.returncode != 0:
        lines.append(f"launchd: {label} は読み込まれていない")
        return lines
    keep = [t.strip() for t in r.stdout.splitlines() if any(k in t for k in ("runs =", "last exit code", "state ="))]
    lines.append("launchd: " + ("、".join(keep) if keep else "読み込まれている（回数・終わり方の行は無い）"))
    return lines


def cmd_check(args) -> None:
    """台帳の点検と、先のデータの途中経過・止める決まり（docs/agents.md 12 章）。値段・台帳・記録のファイルだけを読む。"""
    from cfdbot.agents import evaluator as E

    p = paths(args.root)
    now = pd.Timestamp.now(tz=JST).tz_localize(None) if not args.now else pd.Timestamp(args.now)
    lines: list[str] = []

    def say(t: str = "") -> None:
        print(t, flush=True)
        lines.append(t)

    say("# 台帳の点検と、先のデータの途中経過")
    say(f"作成: {now:%Y-%m-%d %H:%M}　決まり: docs/agents.md 12 章（結果を見る前に決めたもの）")
    say("\n## 1. 自動実行\n")
    for t in schedule_state("run"):
        say(f"- {t}")
    log = p["root"] / "run.log"
    if log.exists():
        mt = pd.Timestamp(log.stat().st_mtime, unit="s", tz="UTC").tz_convert(JST).tz_localize(None)
        tail = [t for t in log.read_text(encoding="utf-8", errors="replace").splitlines() if t.strip()][-3:]
        say(f"- 記録（{log}）の最後の書き込み: {mt:%Y-%m-%d %H:%M}")
        for t in tail:
            say(f"  - {t[:160]}")
    else:
        say(f"- 記録（{log}）が無い。自動実行がまだ一度も動いていないか、登録の前")
    say(f"- 最後の自動実行の時刻: {timeline_text(run_timeline(log))}")

    ledger = read_ledgers(p)
    try:
        _, close = prices(Path(args.data))
    except SystemExit:
        close = pd.DataFrame()
    cal = close.index if len(close) else pd.DatetimeIndex([])
    h = E.ledger_health(ledger, cal, now)
    say("\n## 2. 台帳\n")
    if not h["rows"]:
        say(f"- 台帳（{p['ledger']}・{p['ledger_extra'].name}）がまだ無い。平日の 13:05 の自動実行か `./cfd agents run --prices` で書かれる")
    else:
        say(f"- {h['rows']:,} 行。先の判断として数える日（その日の 15:25 より前に書いた日）: {h['counted_days']} 日"
            f"（{h['first']:%Y-%m-%d}〜{h['last']:%Y-%m-%d}）。最後に書いた時刻: {h['last_written']}" if h["first"] is not None
            else f"- {h['rows']:,} 行あるが、15:25 より前に書いた日が無い（先の判断として数える日は 0）")
        if h["missing"]:
            say(f"- **抜けた営業日 {len(h['missing'])} 日**: " + "、".join(f"{d:%m-%d}" for d in h["missing"][:15])
                + ("…" if len(h["missing"]) > 15 else "") + "（Mac が眠っていた・Yahoo が取れなかった など。その日は買わなかったことになる）")
        else:
            say("- 抜けた営業日: 無し")
        if h["missing_weekdays"]:
            say(f"- 値段のデータより後で、書かれていない平日 {len(h['missing_weekdays'])} 日: "
                + "、".join(f"{d:%m-%d}" for d in h["missing_weekdays"][:10]) + "（祝日を含むかもしれない。値段の取り直しが止まっている可能性）")
        if h["late"]:
            say("- 15:25 より後に書いた日（数えない）: " + "、".join(f"{d:%Y-%m-%d}" for d in h["late"][:10]))
        for b, v in h["books"].items():
            say(f"- 帳 {b}: {v['days']} 日（{v['first']:%Y-%m-%d}〜）、1 日平均 {v['names_per_day']:.1f} 銘柄")

    say("\n## 3. 先のデータの途中経過と、止める決まり\n")
    if not len(close) or not h["counted_days"]:
        say("- まだ計算できない（値段か、数える日のある台帳が無い）")
    else:
        cc = E.returns(close)
        sigma_w = float("nan")
        if p["selector"].exists():
            hist = wide(p["selector"]).reindex(index=close.index, columns=close.columns).to_numpy(dtype=float)
            sigma_w = E.history_sigma(E.top_k(hist, E.TOP_SHARE, E.MIN_VALID), cc)
        say("全銘柄の平均との差（年率）。コスト前が止める決まりに使う数字。コスト後は往復 0.10%（判定に使ったものと同じ）。")
        say("\n| 帳 | 始まり | 数えた日 | コスト前 | コスト後 | 1 日の入れ替え | 月の数 | 今の月ごとの t | 判定 |")
        say("|---|---|---|---|---|---|---|---|---|")
        watch = None
        for b in E.FORWARD_BOOKS:
            r = E.forward_book(ledger, b, cc)
            if r is None:
                say(f"| {b} | – | 0 | – | – | – | – | – | まだ記録が無い |")
                continue
            cs = E.forward_cusum(r, sigma_w) if b == "selector" else None
            watch = cs if b == "selector" else watch
            say(f"| {b} | {r['start']:%Y-%m-%d} | {r['days']} | {pct(r['gross_ann'])} | {pct(r['net_ann'])} | {pct(r['turnover'], 0)} | "
                f"{r['months']} | {f2(r['t'])} | {E.forward_verdict(r, b, cs)} |")
        say("")
        if watch is not None:
            say(f"- 毎週の見張り（M2 の帳、累積和）: {watch['weeks']} 週、今の累積和 {watch['last']:.2f}・これまでの最大 {watch['max']:.2f}"
                f"（閾値 {E.CUSUM_H}。週の差の標準偏差は {E.SIGMA_FROM[:7]}〜{E.SIGMA_TO[:7]} の記録から {watch['sigma_w']:.4%}）")
        elif p["selector"].exists():
            say("- 毎週の見張り: 過去の記録から値動きの大きさを測れなかった（値段か銘柄選定の点数が足りない）")
        say(f"- 判定は {', '.join(str(n) for n in E.LOOK_DAYS)} 日の時点（その日までの損益）だけで行う。その間の数字は途中経過で、見て何かを変えることはしない")
        say(f"- 閉じる: M2（selector）か E1 の帳の、コスト前の月ごとの t が {E.STOP_T} 未満。または M2 の帳の毎週の見張りが閾値 {E.CUSUM_H} を超えた")
        say("- お金を入れる判断は、この台帳だけでは決めない（docs/survivorship.md の判定と、測ったコストを合わせてから。金額を増やすのは、台帳が "
            + "・".join(f"{b} {n} 日" for b, n in E.MIN_RECORD_DAYS.items()) + " を超えてから。docs/agents.md 12.4）")
    out = Path(args.out) / "check"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{now:%Y%m%d-%H%M}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {path}")


# --------------------------------------------------------------------------- 日本株の予行演習（docs/stocks_live.md）
def norm_ticker(t: str) -> str:
    t = str(t).strip().upper()
    return t if "." in t else f"{t}.T"


def e1_closed_reason(p: dict, close: pd.DataFrame) -> str | None:
    """E1 の帳が 12.3 で閉じていれば、その判定の文。計算できなければ None（止める理由にはしない）。"""
    from cfdbot.agents import evaluator as E

    try:
        ledger = read_ledgers(p)
        if ledger.empty:
            return None
        v = E.forward_verdict(E.forward_book(ledger, "e1", E.returns(close)), "e1")
        return v if v.startswith("閉じる") else None
    except Exception:  # noqa: BLE001
        return None


def show_rows(rows: pd.DataFrame | list[dict], cfg: dict, now: pd.Timestamp, say) -> None:
    from cfdbot.agents import live as L

    df = pd.DataFrame(rows)
    if df.empty:
        return
    dl = L.deadline_ts(cfg, now.normalize())
    left = f"（あと {(dl - now).total_seconds() / 60:.0f} 分）" if dl is not None and dl > now else ""
    say(f"出す時刻: {L.TARGET_TIME} までを目安に、遅くとも {cfg.get('deadline') or L.RULE_DEADLINE}{left}（公式の締めは {L.OFFICIAL_CUTOFF}）。"
        "S 株・成行・期間は当日中。**預り区分は「特定」**。送信の前に、銘柄の番号・買いか売りか・株数をこの一覧と見比べる。"
        "受注停止で出せなかったら --status 受注停止")
    for side, title in (("売り", "売る（5 営業日たった株）"), ("買い", "買う（E1）"), ("延長", "売らずに持ち続ける（また E1 が出た）"),
                        ("買わない", "買わない（理由）"), ("なし", "注文なし")):
        g = df[df["side"] == side]
        if not len(g):
            continue
        say(f"\n## {title}\n")
        for r in g.itertuples():
            name = f" {r.name}" if isinstance(r.name, str) and r.name else ""
            if side == "買い":
                mark = L.core_mark(getattr(r, "reason", ""))
                say(f"- {r.ticker}{name} **{int(r.shares)} 株**（前の営業日の終値 {float(r.prev_close):,.0f} 円 → 約 {int(r.shares) * float(r.prev_close):,.0f} 円。"
                    f"売る日の目安 {pd.Timestamp(r.due):%m-%d}" + (f"。E1 の印: {mark}" if mark else "") + "）")
            elif side == "売り":
                say(f"- {r.ticker}{name} **{int(r.shares)} 株**" + (f"（{r.note}）" if isinstance(r.note, str) and r.note else ""))
            elif side == "延長":
                say(f"- {r.ticker}{name} {int(r.shares)} 株。注文は出さない。売る日の目安を {pd.Timestamp(r.due):%m-%d} にする")
            elif side == "買わない":
                say(f"- {r.ticker}{name}: {r.note}")
            else:
                say(f"- {r.reason}")


def cmd_orders(args) -> None:
    """日本株の少額の本番（予行演習）の、今日の注文の一覧・約定の記録・報告（docs/stocks_live.md）。注文は出さない。"""
    from cfdbot.agents import live as L

    p = paths(args.root)
    files = L.LiveFiles(p["root"] / "live")
    now = pd.Timestamp.now(tz=JST).tz_localize(None) if not args.now else pd.Timestamp(args.now)
    today = now.normalize()
    if args.import_csv:
        print("未対応: SBI の約定履歴の CSV の形は、最初の実物を見てから作る（docs/stocks_live.md 6 章）。それまでは --fill で 1 件ずつ書く")
        return
    if args.setup:
        orders_setup(args, files, now)
        return
    if args.notify_test:                                  # 予行演習の設定の前でも試せる
        title, text = "株の予行演習（知らせの試し）", "この知らせが iPhone に届けば、設定はできています（docs/stocks_live_daily.md の 5 章）"
        for kind, ok, detail in notify_send(files, title, text):
            print(f"知らせ（{kind}）: {detail}")
        return
    cfg = files.config()
    if cfg is None:
        sys.exit("予行演習の設定がまだ無い。始めると決めたら: ./cfd agents orders --setup --equity 300000（docs/stocks_live.md 3.1・9 章）")
    _, close = prices(Path(args.data))
    cal = L.calendar(close.index, today)
    if args.void:
        if str(args.void) not in set(files.fills(include_voided=True)["id"].astype(str)):
            sys.exit(f"番号 {args.void} の約定の行が無い（--report か fills.csv の id の列で確かめる）")
        files.append(files.fills_path, [{"id": files.next_fill_id(), "recorded_at": f"{now:%Y-%m-%d %H:%M:%S}", "mode": cfg.get("mode", "dry"),
                                         "date": today, "voids": str(args.void), "memo": args.memo or "取り消し"}], L.FILL_COLUMNS)
        print(f"番号 {args.void} の約定の行を取り消した（元の行は消さずに残る）。正しい行は --fill で書き足す")
        return
    plan, fills = files.plan(), files.fills()
    mode = cfg.get("mode", "dry")
    ev = L.events(mode, plan, fills, close)
    lots = L.open_lots(ev, plan, fills, mode, cal)
    if args.fill:
        orders_fill(args, files, cfg, plan, lots, now)
        return
    if args.report:
        orders_report(args, p, files, cfg, plan, fills, ev, lots, close, cal, now)
        return

    r = orders_today(p, files, cfg, close, cal, now, sells_only=args.sells_only)
    if args.notify:
        send_notice(files, cfg, r["rows"], now, force=True)


def orders_today(p: dict, files, cfg: dict, close: pd.DataFrame, cal, now: pd.Timestamp, e1_override: pd.DataFrame | None = None,
                 decided_override: str | None = None, source: str = "台帳", sells_only: bool = False, say=None) -> dict:
    """今日の注文の一覧（止める決まりの確認 → 書いてあれば表示 → 無ければ作って plan.csv に書き足す）。

    e1_override を渡せば、台帳の E1 の行の代わりにそれを使う（13:05 の自動実行が先に作った E1 の一覧。同じ作り・同じ入力）。
    返り値: rows（今日の行）・written（今書いたか）・judged（判断があったか）。
    """
    from cfdbot.agents import live as L

    lines: list[str] = []

    def out(t: str = "") -> None:
        (say or print)(t)
        lines.append(t)

    today = now.normalize()
    plan, fills = files.plan(), files.fills()
    mode = cfg.get("mode", "dry")
    ev = L.events(mode, plan, fills, close)
    lots = L.open_lots(ev, plan, fills, mode, cal)
    label = "本物" if mode == "real" else "空運転（注文は出さない）"
    out(f"# 今日（{today:%Y-%m-%d}）の株の注文（予行演習・{label}）\n")
    res = {"rows": [], "written": False, "judged": False, "lines": lines}
    if today.weekday() >= 5:
        out("今日は土日。注文は無い")
        return res
    stop = files.stop_reason()
    cash, equity = L.cash_and_equity(float(cfg["equity_jpy"]), ev, lots, close)
    errs = L.mistakes(mode, plan, fills, lots, cal, today)
    rt = L.round_trips(fills, plan, files.refs(), close, cal) if mode == "real" else None
    hits = L.stop_checks(cfg, (cash, equity), errs, lots, close, cal, today, e1_closed_reason(p, close), rt)
    if hits and mode == "real" and not stop:
        files.place_stop("・".join(hits), now)
        stop = files.stop_reason()
        out("**止める決まりに当たったので STOP を置いた**: " + "・".join(hits) + "\n")
    elif hits and mode != "real":
        out("（空運転なので STOP は置かない。本物なら止める）: " + "・".join(hits) + "\n")
    if stop:
        out(f"**STOP がある**（{stop}）。新しく買わない。持っている株は予定の日に売る。消すのは利用者がはっきり頼んだときだけ\n")
    done = plan[(plan["mode"] == mode) & (plan["date"] == today)] if len(plan) else plan
    if len(done):
        out(f"今日の一覧は {done['written_at'].iloc[0]} にもう書いてある（同じ日は二度書かない）。書いた内容:\n")
        show_rows(done, cfg, now, out)
        res.update(rows=done.to_dict("records"), judged=True)
    else:
        if e1_override is not None:
            e1, decided, judged = e1_override, decided_override, True
        else:
            ex = pd.read_csv(p["ledger_extra"], dtype={"ticker": str}) if p["ledger_extra"].exists() else pd.DataFrame()
            day_rows = ex[pd.to_datetime(ex["date"].astype(str).str[:10]) == today] if len(ex) else ex
            judged = len(day_rows) > 0
            e1 = day_rows[day_rows["book"] == "e1"][["ticker", "name", "reason"]] if judged else None
            decided = str(day_rows["decided_at"].max()) if judged else None
        names = dict(zip(plan["ticker"], plan["name"])) if len(plan) else {}
        rows = L.make_plan(today, now, cfg, e1, decided, close, cal, lots, cash, stop, names=names, source=source)
        if judged or sells_only:
            files.append(files.plan_path, rows, L.PLAN_COLUMNS)
            out(f"判断の時刻 {decided}（{source}）。一覧を {files.plan_path} に書き足した\n" if judged else
                "今日の判断が無いので、売りだけの一覧を書き足した（この日は後から買いを足さない）\n")
        else:
            out("今日の判断（13:05 の自動実行）がまだ台帳に無い。書かれた後にもう一度（売りだけ先に書くなら --sells-only）。いまの見込み:\n")
        show_rows(rows, cfg, now, out)
        res.update(rows=rows, written=bool(judged or sells_only), judged=judged)
    x = float(cfg["equity_jpy"])
    out(f"\n記録上の資産 {equity:,.0f} 円（入れた金額 {x:,.0f} 円の {equity / x:.1%}。{L.DD_STOP:.0%} を下回ったら止める）、現金 {cash:,.0f} 円、"
        f"持っている株 {len(lots)} 銘柄")
    rec = L.recent_mistakes(errs, cal, today)
    if rec:
        out(f"直近 {L.ERR_WINDOW} 営業日の誤り {len(rec)} 回（{L.ERR_MAX} 回で止める）: " + "、".join(f"{d:%m-%d} {t}" for d, t in rec))
    return res


def notify_send(files, title: str, text: str) -> list[tuple[str, bool, str]]:
    """知らせを送る（設定 ~/.bunkoko/notify.json が無ければ送らない）。結果は data/agents/live/notify.log にも書く。"""
    from cfdbot import notify as N

    return N.notify(title, text, record=files.root / "notify.log")


def send_notice(files, cfg: dict, rows, now: pd.Timestamp, force: bool = False) -> None:
    """今日の一覧を iPhone に知らせる。注文の無い日は、設定で send_empty が true のときだけ（force なら送る）。"""
    from cfdbot import notify as N
    from cfdbot.agents import live as L

    settings, why = N.load_settings()
    if settings is None:
        print(f"知らせ: {why}", flush=True)
        return
    title, text, has = L.notice_text(rows, cfg, now)
    if not (has or force or settings.get("send_empty")):
        print("知らせ: 今日は出す注文が無いので送らない（send_empty が false）", flush=True)
        return
    for kind, ok, detail in N.notify(title, text, settings=settings, record=files.root / "notify.log"):
        print(f"知らせ（{kind}）: {detail}{'' if ok else '。一覧は plan.csv に書いてある'}", flush=True)


def orders_setup(args, files, now: pd.Timestamp) -> None:
    from cfdbot.agents import live as L

    cfg = files.config()
    if args.real:
        if cfg is None:
            sys.exit("先に --setup --equity で空運転を始める")
        if cfg.get("mode") == "real":
            print(f"もう本物で動いている（{cfg.get('started')} から）")
            return
        st = L.dry_run_status(files.plan(), cfg)
        if not st["ok"]:
            sys.exit("まだ本物にできない（docs/stocks_live.md 4.5）: " + "・".join(st["reasons"]))
        cfg.update(mode="real", started=f"{now:%Y-%m-%d}")
        files.save_config(cfg)
        print(f"本物に切り替えた（{now:%Y-%m-%d}。入れた金額 {cfg['equity_jpy']:,} 円。これから先は金額を変えない）")
        return
    if args.equity is not None and not (L.EQUITY_RANGE[0] <= args.equity <= L.EQUITY_RANGE[1]):
        sys.exit(f"金額は {L.EQUITY_RANGE[0]:,}〜{L.EQUITY_RANGE[1]:,} 円（docs/stocks_live.md 3.1）")
    if args.deadline is not None:
        try:
            h, m = (int(v) for v in args.deadline.split(":"))
            assert 9 <= h <= 15 and 0 <= m < 60
        except Exception:  # noqa: BLE001
            sys.exit("注文の締めは 13:50 のように書く")
        if pd.Timestamp(f"2000-01-01 {args.deadline}") > pd.Timestamp(f"2000-01-01 {L.OFFICIAL_CUTOFF}"):
            sys.exit(f"S 株でその日の終値になるのは {L.OFFICIAL_CUTOFF} までの注文（docs/stocks_live.md 4.1）。決まりは {L.RULE_DEADLINE}")
    exclude = None if args.exclude is None else [norm_ticker(x) for x in str(args.exclude).replace("、", ",").split(",") if x.strip()]
    if cfg is None:
        cfg = {"equity_jpy": int(args.equity or L.DEFAULT_EQUITY), "mode": "dry", "started": None, "deadline": args.deadline,
               "form": L.FORM, "max_names": L.MAX_NAMES, "exclude": exclude or [], "created": f"{now:%Y-%m-%d %H:%M}"}
    else:
        if args.equity is not None and int(args.equity) != int(cfg["equity_jpy"]):
            if cfg.get("mode") == "real":
                sys.exit("本物を始めた後は金額を変えない（docs/stocks_live.md 3.1・3.3）")
            cfg["equity_jpy"] = int(args.equity)
        if args.deadline is not None:
            cfg["deadline"] = args.deadline
        if exclude is not None:
            cfg["exclude"] = exclude
    files.save_config(cfg)
    if cfg.get("deadline") and pd.Timestamp(f"2000-01-01 {cfg['deadline']}") < pd.Timestamp(f"2000-01-01 {L.EARLIEST_DEADLINE}"):
        print(f"**注意**: 受付の終わり {cfg['deadline']} は {L.EARLIEST_DEADLINE} より前なので、13:05 の判断の後に {L.MARGIN_MIN} 分の余裕が取れず、"
              "買える日が無い。形を変えるなら別に事前登録する（docs/stocks_live.md 4.1）")
    print(f"予行演習の設定: {files.config_path}（{'本物' if cfg['mode'] == 'real' else '空運転'}、入れる金額 {cfg['equity_jpy']:,} 円、"
          f"1 日に買うのは {L.daily_budget(cfg['equity_jpy']):,} 円・{L.MAX_NAMES} 銘柄まで、注文の締め {cfg.get('deadline') or '未記入'}、"
          f"予行演習の外で持っている株 {', '.join(cfg.get('exclude', [])) or 'なし'}）")


def orders_fill(args, files, cfg: dict, plan: pd.DataFrame, lots: dict, now: pd.Timestamp) -> None:
    from cfdbot.agents import live as L

    if cfg.get("mode") != "real":
        sys.exit("空運転の間は約定を書かない（本物は --setup --real の後）")
    ticker, side, shares, price = args.fill
    ticker, side = norm_ticker(ticker), L.SIDES.get(side.strip().lower(), L.SIDES.get(side.strip()))
    if side is None:
        sys.exit("向きは 買い か 売り（buy / sell）")
    try:
        shares, price = int(shares), float(price)
    except ValueError:
        sys.exit("株数は整数、値段は数（例: 3 2850.5）")
    day = pd.Timestamp(args.on).normalize() if args.on else now.normalize()
    status = args.status
    if status == "約定" and (shares <= 0 or price <= 0):
        sys.exit("約定なら株数と値段が要る（約定しなかったなら --status 不成立）")
    err, stop_now = L.check_fill(cfg, plan, lots, day, ticker, side, shares, price, status, args.at, files.stop_reason(), files.fills())
    err = "、".join(e for e in (err, args.error or "") if e)
    fid = files.next_fill_id()
    files.append(files.fills_path, [{"id": fid, "recorded_at": f"{now:%Y-%m-%d %H:%M:%S}", "mode": "real", "date": day, "ticker": ticker, "side": side,
                                     "shares": shares, "price": price, "fee_yen": float(args.fee or 0), "status": status, "at": args.at,
                                     "error": err, "voids": None, "memo": args.memo}], L.FILL_COLUMNS)
    print(f"書き足した（番号 {fid}）: {day:%Y-%m-%d} {ticker} {side} {shares} 株 {price:,.2f} 円 手数料 {float(args.fee or 0):,.0f} 円（{status}・{args.at}）")
    if err:
        print(f"**誤り**: {err}（docs/stocks_live.md 3.2。直近 {L.ERR_WINDOW} 営業日に {L.ERR_MAX} 回で止める）")
    if stop_now:
        files.place_stop(f"1 回の誤りで大きすぎる約定（{ticker} {shares} 株 {price:,.0f} 円）", now)
        print("**すぐ STOP を置いた**。その株は次に約定できる時点で売る（docs/stocks_live.md 3.2）")


def orders_report(args, p: dict, files, cfg: dict, plan, fills, ev, lots: dict, close, cal, now: pd.Timestamp) -> None:
    from cfdbot.agents import live as L

    today = now.normalize()
    mode = cfg.get("mode", "dry")
    lines: list[str] = []

    def say(t: str = "") -> None:
        print(t, flush=True)
        lines.append(t)

    say("# 日本株の予行演習の様子")
    say(f"作成: {now:%Y-%m-%d %H:%M}　決まり: docs/stocks_live.md（始める前に決めたもの）\n")
    say("## 1. 設定と止める決まり\n")
    x = float(cfg["equity_jpy"])
    say(f"- {'本物（' + str(cfg.get('started')) + ' から）' if mode == 'real' else '空運転'}。入れた金額 {x:,.0f} 円、1 日に買うのは "
        f"{L.daily_budget(x):,.0f} 円・{L.MAX_NAMES} 銘柄まで、注文の締め {cfg.get('deadline') or '未記入'}、"
        f"予行演習の外で持っている株 {', '.join(cfg.get('exclude', [])) or 'なし'}")
    stop = files.stop_reason()
    say(f"- STOP: {'ある（' + stop + '）' if stop else '無い'}")
    say(f"- 最後の自動実行の時刻（一覧が書けるまで）: {timeline_text(run_timeline(p['root'] / 'run.log'))}")
    nlog = files.root / "notify.log"
    if nlog.exists():
        last = [x for x in nlog.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()][-3:]
        say("- 最後の知らせ: " + " / ".join(x.replace("\t", " ")[:120] for x in last))
    else:
        say("- 知らせ: まだ送っていない（設定は ~/.bunkoko/notify.json。docs/stocks_live_daily.md の 5 章）")
    days = sorted(set(plan.loc[plan["source"] == "先に作った E1 の一覧", "date"])) if len(plan) and "source" in plan else []
    bad = [d for d in days if e1_mismatch(p, plan[plan["date"] == d], d)]
    if days:
        say(f"- 先に作った E1 の一覧と台帳の E1 の帳が違った日: {len(bad)} 日（{len(days)} 日のうち）" + ("".join(f" {d:%m-%d}" for d in bad)))
    cash, equity = L.cash_and_equity(x, ev, lots, close)
    say(f"- 記録上の資産 {equity:,.0f} 円（{equity / x:.1%}。{L.DD_STOP:.0%} を下回ったら止める）、現金 {cash:,.0f} 円")
    errs = L.mistakes(mode, plan, fills, lots, cal, today)
    rec = L.recent_mistakes(errs, cal, today)
    say(f"- 直近 {L.ERR_WINDOW} 営業日の誤り {len(rec)} 回（{L.ERR_MAX} 回で止める）" + ("".join(f"\n  - {d:%m-%d} {t}" for d, t in rec)))
    hits = L.stop_checks(cfg, (cash, equity), errs, lots, close, cal, today, e1_closed_reason(p, close),
                         L.round_trips(fills, plan, files.refs(), close, cal) if mode == "real" else None)
    say("- 止める決まりに当たっているもの: " + ("・".join(hits) if hits else "無し"))
    say("\n## 2. 持っている株\n")
    if not lots:
        say("- 無し")
    for lot in sorted(lots.values(), key=lambda v: v.due):
        late = L.bdays_after(cal, lot.due, today)
        say(f"- {lot.ticker} {lot.shares} 株（{lot.buy_date:%m-%d} に買った。売る日 {lot.due:%m-%d}" + (f"、{late} 営業日遅れ" if late else "") + "）")
    say("\n## 3. 空運転の進み具合（本物に切り替える条件。docs/stocks_live.md 4.5）\n")
    st = L.dry_run_status(plan, cfg)
    say(f"- 空運転の日 {st['days']} 日（{L.DRY_MIN_DAYS} 日以上）、E1 の行 {st['e1_rows']} 行（{L.DRY_MIN_ROWS} 行以上。{st['e1_days']} 日分）")
    say(f"- 受付の終わりの {L.MARGIN_MIN} 分前までに一覧を作れた日: {pct(st['margin_share'], 0)}（{L.DRY_MARGIN_SHARE:.0%} 以上）。"
        f"台帳の判断がその時刻までに書かれていた日: {pct(st['decided_share'], 0)}")
    say("- " + ("条件を満たした（本物にするのは利用者がはっきり頼んだときだけ: --setup --real）" if st["ok"] else "まだ: " + "・".join(st["reasons"])))
    say("\n## 4. コスト（c*）の途中経過（docs/stocks_live.md 5 章）\n")
    if mode != "real":
        say("- 本物を始めてから数える")
    else:
        if not args.no_fetch:
            try:
                for m in L.update_refs(files, fills, plan, today, now, cal):
                    say(f"- {m}")
            except Exception as e:  # noqa: BLE001
                say(f"- 基準の値段を取れなかった（{type(e).__name__}: {str(e)[:100]}）。次の回にまた取る")
        rt = L.round_trips(fills, plan, files.refs(), close, cal)
        cs = L.c_star(rt)
        say(f"- 出した買いの注文 {cs['orders']} 回（約定 {cs['filled']}・不成立 {cs['unfilled']}。不成立のうち、その日に +{L.LIMIT_UP_DAY:.0%} 以上"
            f"上がっていたもの {cs['limit_up']}）。決めるのは {L.FIX_ORDERS} 回が片づいたとき")
        say(f"- 往復が済んで計算できたもの {int(rt['c'].notna().sum()) if len(rt) else 0} 回。外したもの {int((rt['excluded'] != '').sum()) if len(rt) else 0} 回")
        if cs["fixed"]:
            say(f"- **決める時点に届いた**（最初の {cs['k']} 回）: c* = **{cs['c_star']:.3%}**（{cs['rule']}）。往復 {cs['n']} 回の平均 {cs['mean']:.3%}・"
                f"標準誤差 {cs['se']:.3%}、不成立 {cs['nf']} 回（{cs['p_nf']:.1%}）")
            say(f"- docs/agents.md 6 章の「往復 0.10% 以内」: コスト＋時刻のずれ {cs['c6']:.3%} → {'満たす' if cs['ok6'] else '満たさない（お金を入れない）'}"
                f"（時刻のずれの平均 {cs['mean_t']:.3%}）")
            say("- この値を docs/stocks_live.md 10 章に書き写す。後から数が増えても計算し直さない")
        elif cs["gave_up"]:
            say(f"- {L.GIVE_UP_ORDERS} 回でも往復が {L.MIN_ROUNDTRIPS} 回に届かなかった。c* は決めない（判定は 0.10%。6 章の条件は満たされない）")
        else:
            say(f"- まだ決めない。途中の数字（見て何も変えない）: 平均 {pct(cs['mean_now'], 3)}・標準誤差 {pct(cs['se_now'], 3)}")
        say("- J-Quants（12 週間遅れの公式の値）との照らし合わせは、後から別に行う（docs/stocks_live.md 5.1）")
    out = Path(args.out) / "live"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{now:%Y%m%d-%H%M}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {path}")


def make_plist(root: Path, log: Path, job: str = "run") -> dict:
    """run: 平日の 13:05 に、その日の寄りまでの値段と 13:00 までの開示で判断し、台帳に書き足す。collect: 毎日 8:00 に開示・本文・数字を集める。"""
    label, command, when, _ = JOBS[job]
    return {"Label": label, "ProgramArguments": [str(root / "cfd")] + command, "WorkingDirectory": str(root),
            "StartCalendarInterval": when, "StandardOutPath": str(log), "StandardErrorPath": str(log),
            "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"}}


def cmd_schedule(args) -> None:
    import os
    import plistlib
    import subprocess

    if sys.platform != "darwin":
        raise SystemExit("自動実行の登録は Mac だけ（launchd）。他では cron などで同じコマンドを動かす")
    label, _, _, what = JOBS[args.job]
    uid, plist = os.getuid(), Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
    if args.off:
        plist.unlink(missing_ok=True)
        print(f"自動実行（{what}）を外した")
        return
    log = ROOT / paths(args.root)["root"] / f"{args.job}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    plist.parent.mkdir(parents=True, exist_ok=True)
    with open(plist, "wb") as f:
        plistlib.dump(make_plist(ROOT, log, args.job), f)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"launchctl に断られた: {r.stderr.strip()}")
    print(f"{what} を動かすよう登録した（記録: {log}）。Mac が眠っていた場合は、起きたときに動く")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["collect", "ir", "select", "decide", "evaluate", "run", "status", "schedule", "earnings", "extra",
                                        "check", "orders", "manual", "premium"])
    p.add_argument("--fetch", action="store_true", help="extra: 先に出来高・終値・株式分割を Yahoo から取る（1 秒に 1 社。取得済みは飛ばす）")
    p.add_argument("--reuse", action="store_true", help="extra: 入力を足した銘柄選定の点数が書いてあれば、学び直さずに使う")
    p.add_argument("--off", action="store_true", help="schedule: 自動実行を外す")
    p.add_argument("--job", choices=list(JOBS), default="run", help="schedule: どの自動実行か（run = 平日 13:05 の判断、collect = 毎日 8:00 の収集）")
    p.add_argument("--docs", action="store_true", help="collect: 本文（PDF）と決算の数字（XBRL）も取る")
    p.add_argument("--docs-days", type=int, default=7, help="collect --docs: さかのぼる日数")
    p.add_argument("--xbrl-history", action="store_true", help="collect: 過去の決算の数字（2016〜）を取る（時間がかかる）")
    p.add_argument("--max-docs", type=int, default=300, help="collect: 1 回に取る書類の上限")
    p.add_argument("--root", default="data/agents")
    p.add_argument("--data", default="data/stocks")
    p.add_argument("--out", default="output/agents")
    p.add_argument("--backfill", action="store_true", help="collect: 過去の開示を 1 社ずつ取る（初回）")
    p.add_argument("--force", action="store_true", help="collect --backfill: 取得済みの会社も取り直す")
    p.add_argument("--days", type=int, default=5, help="collect: さかのぼる日数（公式ページ）")
    p.add_argument("--no-embed", action="store_true", help="collect: 埋め込みをしない")
    p.add_argument("--prices", action="store_true", help="run: 先に値段を取り直す")
    p.add_argument("--placebo", type=int, default=300)
    p.add_argument("--now", help="check・orders: 今の時刻として扱う時刻（試験用。例: 2026-10-20 16:00）")
    p.add_argument("--setup", action="store_true", help="orders: 予行演習の設定を作る・直す（金額・受付の終わり）")
    p.add_argument("--equity", type=int, help="orders --setup: 口座に入れる金額（10〜50 万円。既定 30 万円）")
    p.add_argument("--deadline", help="orders --setup: S 株でその日の引けで約定する受付の終わり（例 13:30。口座の画面で確かめた時刻）")
    p.add_argument("--real", action="store_true", help="orders --setup: 空運転から本物に切り替える（条件を満たした後だけ）")
    p.add_argument("--fill", nargs=4, metavar=("銘柄", "向き", "株数", "値段"), help="orders: 約定を書き足す（例 7203 買い 3 2850）")
    p.add_argument("--fee", type=float, default=0.0, help="orders --fill: 手数料（税込、円）")
    p.add_argument("--status", default="約定", choices=["約定", "不成立", "出さなかった", "受注停止", "取り消し"], help="orders --fill: 結果")
    p.add_argument("--at", default="引け", choices=["引け", "寄り", "後場寄り"], help="orders --fill: 約定した時刻（引け＝後場終値）")
    p.add_argument("--on", help="orders --fill: 約定の日（既定は今日）")
    p.add_argument("--error", help="orders --fill: 誤りの中身（自分で気づいたとき）")
    p.add_argument("--memo", help="orders --fill: メモ（注文番号など）")
    p.add_argument("--void", help="orders: この番号（fills.csv の id）の約定の行を取り消す（元の行は残る）")
    p.add_argument("--import", dest="import_csv", help="orders: SBI の約定履歴の CSV を取り込む（まだ未対応）")
    p.add_argument("--report", action="store_true", help="orders: 予行演習の様子（output/agents/live/<日時>.md）")
    p.add_argument("--no-fetch", action="store_true", help="orders --report: 基準の値段を Yahoo から取らない")
    p.add_argument("--sells-only", action="store_true", help="orders: 今日の判断が無いとき、売りだけの一覧を書く")
    p.add_argument("--notify", action="store_true", help="orders: 今日の一覧を iPhone にもう一度知らせる（設定 ~/.bunkoko/notify.json）")
    p.add_argument("--notify-test", action="store_true", help="orders: 知らせの試し（設定ができているか）")
    p.add_argument("--test", action="store_true", help="manual・premium: 過去の検証（docs/agents.md 13.2・docs/earnings_premium.md 3 章。数分）")
    p.add_argument("--exclude", help="orders --setup: 予行演習の外ですでに持っている株（例 7203,6758。空にするなら \"\"）")
    args = p.parse_args()
    {"collect": cmd_collect, "ir": cmd_ir, "select": cmd_select, "decide": cmd_decide, "evaluate": cmd_evaluate,
     "run": cmd_run, "status": cmd_status, "schedule": cmd_schedule, "earnings": cmd_earnings, "extra": cmd_extra,
     "check": cmd_check, "orders": cmd_orders, "manual": cmd_manual, "premium": cmd_premium}[args.command](args)


if __name__ == "__main__":
    main()
