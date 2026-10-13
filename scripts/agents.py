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
    python scripts/agents.py status
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
    n = table.groupby("book")["date"].nunique()
    print(f"判断: 記録 {len(table):,} 行（{table['date'].min():%Y-%m-%d}〜{last:%Y-%m-%d}。" + "、".join(f"{k} {v} 日" for k, v in n.items()) + "）", flush=True)


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


# --------------------------------------------------------------------------- まとめて・状態
def cmd_run(args) -> None:
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
    for f in (cmd_collect, cmd_ir, cmd_select, cmd_decide, cmd_evaluate):
        f(args)


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
    p.add_argument("command", choices=["collect", "ir", "select", "decide", "evaluate", "run", "status", "schedule", "earnings"])
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
    args = p.parse_args()
    {"collect": cmd_collect, "ir": cmd_ir, "select": cmd_select, "decide": cmd_decide, "evaluate": cmd_evaluate,
     "run": cmd_run, "status": cmd_status, "schedule": cmd_schedule, "earnings": cmd_earnings}[args.command](args)


if __name__ == "__main__":
    main()
