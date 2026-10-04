"""外部データ（金利・ドル・株価・他の商品・投機筋の建玉）をまとめて取得して data/context/ に保存する。

    python scripts/fetch_context.py                 # 全部（数分。何度実行してもよい＝最新まで取り直す）
    python scripts/fetch_context.py --only fred,cftc
    python scripts/fetch_context.py --since 2000-01-01

取得元はどれも無料・登録不要: FRED（米連銀のデータベース）、Yahoo Finance、CFTC（米商品先物取引委員会）。
研究（scripts/feature_study.py）に使う。価格データ（MT5 の書き出し）と同じく git には入れない。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot.context import (CATALOG, ContextStore, _get, cot_series, cot_urls, fetch_fred,  # noqa: E402
                            fetch_yahoo, load_mt5_catalog, mt5_context_files, parse_cot)
from cfdbot.data import load_mt5_csv  # noqa: E402
from cfdbot.train.config import load_train_config  # noqa: E402


def _save(df: pd.DataFrame, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return f"{df['date'].iloc[0]}〜{df['date'].iloc[-1]}（{len(df):,} 行）" if len(df) else "0 行"


def fetch_cot(store: ContextStore, since: str) -> pd.DataFrame:
    raw_dir = store.root / "cftc" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    this_year = pd.Timestamp.now().year
    tables = []
    for url in cot_urls(max(pd.Timestamp(since).year, 2006), this_year):
        cache = raw_dir / url.rsplit("/", 1)[1]
        fresh = cache.exists() and (str(this_year) not in cache.name and str(this_year - 1) not in cache.name)
        if not fresh:  # 今年と去年の分は毎回取り直す（訂正が入るため）
            cache.write_bytes(_get(url, timeout=120))
            time.sleep(0.5)
        tables.append(parse_cot(cache.read_bytes()))
    return pd.concat(tables, ignore_index=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dest", default="data/context")
    p.add_argument("--since", default="2000-01-01", help="Yahoo の取得開始日（FRED は全期間）")
    p.add_argument("--only", default="", help="取得元を絞る（fred,yahoo,cftc）")
    p.add_argument("--config", default="config/train.toml")
    args = p.parse_args()

    store = ContextStore(args.dest)
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    rows, failed = [], []
    cot_table = None
    for spec in CATALOG:
        if only and spec.source not in only:
            continue
        try:
            if spec.source == "fred":
                df = fetch_fred(spec.code)
            elif spec.source == "yahoo":
                df = fetch_yahoo(spec.code, args.since)
                time.sleep(0.4)
            else:
                if cot_table is None:
                    print("CFTC の建玉明細を取得中（初回は数分かかる）…", flush=True)
                    cot_table = fetch_cot(store, args.since)
                df = cot_series(cot_table, spec.code)
            if df.empty:
                raise RuntimeError("データが空")
            span = _save(df, store.path(spec))
            rows.append((spec.label, spec.key, spec.source, span))
            print(f"  ✓ {spec.label}（{spec.key}）: {span}", flush=True)
        except Exception as e:  # noqa: BLE001  1 つ失敗しても残りは続ける
            failed.append((spec.label, spec.key, spec.source, str(e)[:160]))
            print(f"  ✗ {spec.label}（{spec.key}）: 取得できない — {str(e)[:160]}", flush=True)
    print(f"\n{len(rows)} 系列を {store.root}/ に保存した" + (f"、{len(failed)} 系列は失敗" if failed else ""))
    if failed:
        srcs = {f[2] for f in failed}
        if "yahoo" in srcs and len([f for f in failed if f[2] == "yahoo"]) > 5:
            print("Yahoo がまとめて失敗した場合: 少し時間をおいて再実行する。続くときは\n"
                  "  UV_PYTHON_INSTALL_DIR=\"$PWD/.python\" uv pip install --python .venv/bin/python yfinance\n"
                  "で yfinance を入れると、そちらで取り直す")
        print("失敗した系列は研究で使わないだけなので、そのまま先に進んでよい（表示を送ってくれれば対応する）")
    show_mt5(args)
    print("\n次: ./cfd study（外部データで絞り込んだときの成績を比べる。5 分前後）")


def show_mt5(args) -> None:
    """MT5 のサーバーにある銘柄（EA から直接使えるもの）を表示する。"""
    cfg = load_train_config(args.config)
    cat = load_mt5_catalog(Path(cfg.data.dir) / "symbols_all.txt")
    print("\n===== MT5 のサーバーにある銘柄（EA から直接使える外部データ）")
    if cat is None:
        print("  まだ一覧が無い。MT5 で CfdExportBars（0.14 以降）を実行し、./cfd data で取り込むと表示される")
        return
    print(f"  全 {len(cat)} 銘柄: " + " / ".join(f"{g or '（なし）'} {n}" for g, n in cat["group"].value_counts().items()))
    if len(cat) <= 120:
        for g, rows in cat.groupby("group"):
            print(f"  [{g}] " + "、".join(f"{r.symbol}（{r.description}）" if r.description else r.symbol
                                          for r in rows.itertuples()))
    files = mt5_context_files(args.dest)
    if files:
        spans = []
        for name, path in files.items():
            try:
                df = load_mt5_csv(path, server_tz=cfg.data.server_tz)
                spans.append(f"{name} {df.index[0]:%Y-%m}〜（{len(df):,} 本）")
            except ValueError as e:
                spans.append(f"{name}（読めない: {e}）")
        print("  日足を書き出した関連銘柄: " + "、".join(spans))


if __name__ == "__main__":
    main()
