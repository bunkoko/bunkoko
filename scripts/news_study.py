"""ニュースを集めて、売買に役立つかを確かめる（./cfd news。設定と判定の条件は docs/news.md）。

    ./cfd news setup       Mac の準備: 埋め込みの部品とモデル（初回は数分）、15 分ごとの自動の収集
    ./cfd news backfill    GDELT の過去の分をまとめて取る（見出し 90 日・キーワードの推移 2017 年〜。数時間）
    ./cfd news collect     1 回だけ集める（自動の収集が 15 分ごとに実行するもの）
    ./cfd news embed       見出しを埋め込む（終わった日の分だけ）
    ./cfd news report      仮説を確かめて output/news/<日時>/report.md に書く（埋め込みも先に行う）
    ./cfd news status      集めた量と、自動の収集が動いているか
    ./cfd news schedule [--off]   自動の収集を入れる（外す）
"""

from __future__ import annotations

import argparse
import contextlib
import os
import plistlib
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot.news import (BLOCK, TIME_FMT, TIMELINE_MODES, TIMELINE_QUERIES, GdeltClient, MarketDaily,  # noqa: E402
                         NewsStore, Throttled, ann_sharpe, collect_articles, collect_yahoo, daily_timeline,
                         flags_at, hold_returns, shift_days, spikes, update_timelines, values_at, vol_ratios)
from cfdbot.stats import deflated_threshold  # noqa: E402

LABEL = "com.bunkoko.news"
TIMELINE_FIRST = pd.Timestamp("2017-01-01", tz="UTC")
PRICE_MARKETS = {"WTI": "CL=F", "BRENT": "BZ=F", "GOLD": "GC=F", "SILVER": "SI=F"}
COST = (0.0008, 0.03)            # 往復コスト・建玉の金利（年）。docs/universe.md の今の 4 商品と同じ
MIN_TRADING_DAYS = 250           # これより短い期間では判定しない（途中経過として表示だけ）
MIN_EVENTS = 10                  # 急増がこれより少なければ判定しない
PRIOR_TRIALS = 21                # これまでに試した数（docs/news.md）
SEED = 20261009


def now_utc() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").floor("s")


@contextlib.contextmanager
def gdelt_lock(store: NewsStore, wait: bool):
    """GDELT に同時に 2 つから問い合わせない（自動の収集とまとめての取得が重なると断られる）。"""
    import fcntl

    p = store.root / ".gdelt.lock"
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


# --------------------------------------------------------------------------- 集める
def cmd_collect(args) -> None:
    store, now = NewsStore(args.root), now_utc()
    msg = []
    if not args.no_yahoo:
        msg.append(f"Yahoo +{collect_yahoo(store, now)}")
    if not args.no_gdelt:
        with gdelt_lock(store, wait=False) as ok:
            if not ok:
                msg.append("GDELT は別の取得中なので省いた")
            else:
                client = GdeltClient(log=lambda s: None)
                st = store.state()
                since = pd.Timestamp(st["live_until"]) - pd.Timedelta(minutes=15) if "live_until" in st \
                    else now - pd.Timedelta(hours=2)
                since = max(since, now - pd.Timedelta(days=80))
                try:
                    added, capped = collect_articles(client, store, since, now, pd.Timedelta(hours=3), "live_until",
                                                     log=lambda s: None)
                    msg.append(f"GDELT +{added}" + (f"（上限 250 件に {capped} 回）" if capped else ""))
                    checked = pd.Timestamp(st["timeline_checked"]) if "timeline_checked" in st else None
                    if checked is None or now - checked > pd.Timedelta(hours=6):
                        tl = 0
                        for q in TIMELINE_QUERIES:
                            for mode in TIMELINE_MODES:
                                old = store.load_timeline(q.key, mode)
                                start = (old.index[-1].floor("D") - pd.Timedelta(days=2)) if len(old) \
                                    else now.floor("D") - pd.Timedelta(days=7)
                                tl += update_timelines(client, store, start, now, [q], [mode], log=lambda s: None)
                        store.save_state(timeline_checked=now.strftime(TIME_FMT))
                        msg.append(f"推移 +{tl}")
                except Throttled as e:
                    msg.append(f"GDELT に断られた（{e}）。次の回に続きから取る")
    print(f"{now:%Y-%m-%d %H:%M} UTC 収集: " + "、".join(msg), flush=True)


def cmd_backfill(args) -> None:
    store, now = NewsStore(args.root), now_utc()
    log = lambda s: print(s, flush=True)  # noqa: E731
    if sys.platform == "darwin":     # 取り終わるまで Mac を眠らせない（ふたを閉じると眠る）
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    with gdelt_lock(store, wait=True):
        client = GdeltClient(log=log)
        st = store.state()
        start = (now - pd.Timedelta(days=args.days)).floor(BLOCK)
        if "backfill_until" in st and pd.Timestamp(st.get("backfill_from", now)) <= start + pd.Timedelta(days=7):
            start = max(start, pd.Timestamp(st["backfill_until"]))
            log(f"前回の続きから: {start:%Y-%m-%d %H:%M} UTC")
        else:
            store.save_state(backfill_from=start.strftime(TIME_FMT))
        n_req = int(np.ceil((now - start) / BLOCK)) * 4
        if not args.no_articles and start < now:
            log(f"見出し: {start:%Y-%m-%d}〜（{n_req} 回の問い合わせ、約 {n_req * 6 / 3600:.1f} 時間。途中で止めても続きから取れる）")
            try:
                added, capped = collect_articles(client, store, start, now, BLOCK, "backfill_until", log=log)
                log(f"見出し +{added}（上限 250 件に {capped} 回達した。6 時間ごとに最新の 250 件までしか取れない）")
            except Throttled as e:
                raise SystemExit(f"GDELT に断られ続けた（{e}）。時間をおいてもう一度 ./cfd news backfill（続きから取る）")
        if not args.no_timeline:
            first = pd.Timestamp(args.timeline_since, tz="UTC")
            log(f"キーワードの推移: {first:%Y-%m}〜（3 か月ずつ。{len(TIMELINE_QUERIES) * len(TIMELINE_MODES)} 本）")
            got = 0
            try:
                for q in TIMELINE_QUERIES:
                    for mode in TIMELINE_MODES:
                        old = store.load_timeline(q.key, mode)
                        head_end = old.index[0] if len(old) else now
                        if head_end - first > pd.Timedelta(days=2):
                            got += update_timelines(client, store, first, head_end, [q], [mode], log=log)
                        if len(old):
                            got += update_timelines(client, store, old.index[-1].floor("D") - pd.Timedelta(days=2),
                                                    now, [q], [mode], log=log)
                        tl = store.load_timeline(q.key, mode)
                        log(f"  {q.key} {mode}: " + (f"{tl.index[0]:%Y-%m-%d}〜{tl.index[-1]:%Y-%m-%d}（{len(tl)} 点）"
                                                    if len(tl) else "無し"))
            except Throttled as e:
                raise SystemExit(f"GDELT に断られ続けた（{e}）。時間をおいてもう一度 ./cfd news backfill")
            store.save_state(timeline_checked=now.strftime(TIME_FMT))
            log(f"推移 +{got} 点")
        log(f"問い合わせ {client.requests} 回")


# --------------------------------------------------------------------------- 埋め込み
def cmd_embed(args) -> None:
    from cfdbot.news_embed import embed_days, make_embedder

    store = NewsStore(args.root)
    emb = make_embedder(args.model)
    t0 = time.time()
    n = embed_days(store, emb, now_utc().floor("D"))
    dt = time.time() - t0
    print(f"埋め込み（{args.model}）: +{n} 件、{dt:.0f} 秒" + (f"（1 秒に {n / dt:.0f} 件）" if n and dt > 0 else ""))


def cmd_selftest(args) -> None:
    """モデルが正しく動くかの確かめ: 例の見出しが、思った話題の代表の文に一番近いか。"""
    from cfdbot.news_embed import anchor_vectors, make_embedder, topic_scores

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
    print(f"モデル {args.model}（{dev}）")
    for i, (text, want) in enumerate(examples):
        best = max(sc, key=lambda k: sc[k][i])
        ok += best == want
        print(f"  {'○' if best == want else '×'} {text} → {best}（{sc[best][i]:.2f}）")
    t0 = time.time()
    emb.encode([f"Oil prices move as traders weigh supply outlook {i}" for i in range(1000)])
    print(f"正解 {ok}/{len(examples)}。速さ: 1,000 件で {time.time() - t0:.1f} 秒")


# --------------------------------------------------------------------------- Mac の準備・自動の収集
def cmd_setup(args) -> None:
    py = ROOT / ".venv" / "bin" / "python"
    env = os.environ | {"UV_PYTHON_INSTALL_DIR": str(ROOT / ".python")}
    if args.model != "hash":
        print("== 1/3 埋め込みの部品（sentence-transformers・PyTorch。初回は数分）", flush=True)
        subprocess.run(["uv", "pip", "install", "--python", str(py), "-e", ".[dev,research,news]", "-q"],
                       cwd=ROOT, env=env, check=True)
    print(f"== 2/3 モデル {args.model} の取得（初回のみ）と確かめ", flush=True)
    subprocess.run([str(py), str(Path(__file__)), "selftest", "--model", args.model], cwd=ROOT, check=True)
    print("== 3/3 15 分ごとの自動の収集", flush=True)
    cmd_schedule(argparse.Namespace(off=False, root=args.root))
    print("\n準備完了。過去の分は ./cfd news backfill（数時間。Mac をスリープさせない）")


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


def cmd_status(args) -> None:
    store = NewsStore(args.root)
    days = store.days()
    if not days:
        print("まだ見出しが無い。./cfd news collect か ./cfd news backfill を実行する")
    else:
        print(f"見出し: {days[0]:%Y-%m-%d}〜{days[-1]:%Y-%m-%d}（{len(days)} 日）")
        print("直近の日ごとの件数（GDELT / Yahoo）:")
        for d in days[-10:]:
            df = store.load_day(d)
            print(f"  {d:%Y-%m-%d}  {int((df['source'] == 'gdelt').sum()):5d} / {int((df['source'] == 'yahoo').sum()):4d}")
    st = store.state()
    for k, label in (("live_until", "毎回の収集（GDELT）"), ("backfill_until", "まとめての取得"),
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

    def vol(self, days: pd.DatetimeIndex, flags_by_market: dict[str, np.ndarray]) -> dict:
        """急増（印）の翌日の |値動き| ÷ ふだんの値動き（市場をまとめた平均）。"""
        first = days[0] + pd.Timedelta(days=1)

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
                  kind: str) -> dict:
        """日ごとの向き（kind="value": 値をそのまま、"flag": 印の日に買い）で h 日持った成績（市場の平均）。"""
        first = (days[0] + pd.Timedelta(days=1)).tz_localize(None)

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


def gate_test(args, flags_by_symbol: dict[str, tuple[pd.DatetimeIndex, np.ndarray]], say) -> dict | None:
    """今のタートルで、急増の翌日（最初の判断）は新規に入らない。期間B（フィリップ）の、ニュースのある期間で比べる。"""
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
    start = max(min(starts), per.start)
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
        n = {"gdelt": 0, "yahoo": 0}
        for d in days:
            src = store.load_day(d)["source"].value_counts()
            for k in n:
                n[k] += int(src.get(k, 0))
        say(f"\n- 見出し: {days[0]:%Y-%m-%d}〜{days[-1]:%Y-%m-%d}（{len(days)} 日、GDELT {n['gdelt']:,}・Yahoo {n['yahoo']:,} 件）")

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

    for key in [args.model] + ([] if args.model == "hash" else ["hash"]):
        try:
            emb = make_embedder(key)
        except (SystemExit, Exception) as e:  # noqa: BLE001  部品が無い・モデルを取れない
            say(f"### {key}\n\n- 使えなかった: {str(e)[:200]}\n")
            continue
        n = embed_days(store, emb, today)
        feats = daily_features(load_day_vectors(store, key, today), anchor_vectors(emb))
        role = "（AI を使わない比較用。判定はしない）" if key == "hash" else "（判定に使うモデル）"
        say(f"### {key} {role}\n\n- {MODELS[key].note}。今回埋め込んだ見出し +{n}")
        if feats.empty or "share_geo" not in feats:
            say(f"- 特徴を作れる日がまだ無い（見出しが {20} 日分以上要る）\n")
            continue
        say(f"- 特徴: {feats['share_geo'].first_valid_index():%Y-%m-%d}〜{feats.index[-1]:%Y-%m-%d}")
        sp_geo = spikes(feats["share_geo"]).to_numpy()
        sp_oil = spikes(feats["novelty_oil"]).to_numpy() if "novelty_oil" in feats else np.zeros(len(feats), bool)
        sp_gold = spikes(feats["novelty_gold"]).to_numpy() if "novelty_gold" in feats else np.zeros(len(feats), bool)
        say(f"- 急増: 地政学 {int(sp_geo.sum())} 日・原油の珍しさ {int(sp_oil.sum())} 日・金の珍しさ {int(sp_gold.sum())} 日")
        rows = []
        r = ev.vol(feats.index, {k: sp_geo for k in ("WTI", "BRENT", "GOLD")})
        rows.append(("emb_geo_vol", "地政学の見出しの割合の急増 → 翌日の値動きの大きさ（÷ふだん）", r, vol_verdict(r)))
        r = ev.vol(feats.index, {"WTI": sp_oil, "BRENT": sp_oil, "GOLD": sp_gold, "SILVER": sp_gold})
        rows.append(("novelty_vol", "原油（金）の見出しの珍しさの急増 → 翌日の値動きの大きさ", r, vol_verdict(r)))
        say("\n| 仮説 | 内容 | 回数 | 値 | 前半 | 後半 | 偶然より良い割合 | 判定 |")
        say("|---|---|---|---|---|---|---|---|")
        for name, label, r, v in rows:
            v = "参考" if key == "hash" else v
            say(f"| {name} | {label} | {r['n']} | {fmt(r['value'])} | {fmt(r['h1'])} | {fmt(r['h2'])} | {pct(r['pct'])} | {v} |")
        if key != "hash" and not args.no_gate:
            fl = {"WTI": sp_geo | sp_oil, "BRENT": sp_geo | sp_oil, "GOLD": sp_geo | sp_gold, "SILVER": sp_geo | sp_gold}
            g = gate_test(args, {s: (feats.index, f) for s, f in fl.items()}, say)
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
    c.add_argument("--no-gdelt", action="store_true")
    c.add_argument("--no-yahoo", action="store_true")
    b = sub.add_parser("backfill", help="GDELT の過去の分をまとめて取る")
    b.add_argument("--days", type=int, default=90, help="見出しをさかのぼる日数（GDELT は 3 か月ほどまで）")
    b.add_argument("--timeline-since", default=str(TIMELINE_FIRST.date()), help="キーワードの推移の開始日")
    b.add_argument("--no-articles", action="store_true")
    b.add_argument("--no-timeline", action="store_true")
    for name in ("embed", "selftest", "setup"):
        s = sub.add_parser(name)
        s.add_argument("--model", default="qwen3-0.6b")
    s = sub.add_parser("schedule", help="15 分ごとの自動の収集を入れる")
    s.add_argument("--off", action="store_true", help="外す")
    sub.add_parser("status")
    r = sub.add_parser("report")
    r.add_argument("--model", default="qwen3-0.6b", help="判定に使う埋め込みのモデル（結果を見る前に決めたもの）")
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
     "setup": cmd_setup, "schedule": cmd_schedule, "status": cmd_status, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
