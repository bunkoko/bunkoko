"""外部データ（金利・ドル・株価・他の商品・投機筋の建玉）をまとめて取得して data/context/ に保存する。

    python scripts/fetch_context.py                 # 全部（数分。何度実行してもよい＝最新まで取り直す）
    python scripts/fetch_context.py --only fred,cftc
    python scripts/fetch_context.py --since 2000-01-01

取得元はどれも無料・登録不要: FRED（米連銀のデータベース）、Yahoo Finance、CFTC（米商品先物取引委員会）。
FRED に接続できないときは、待たずに Yahoo の近いデータ（^TNX・^VIX・物価連動債 ETF など）で代用する。
FRED の無料 API キーがあれば --fred-key で渡すと確実に取れる（config/local.toml に保存）。
研究（scripts/feature_study.py）に使う。価格データ（MT5 の書き出し）と同じく git には入れない。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import pandas as pd

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot.context import (CATALOG, FRED_ALT, ContextStore, _get, cot_series, cot_urls, fetch_alt,  # noqa: E402
                            fetch_fred, fetch_yahoo, load_mt5_catalog, mt5_context_files, parse_cot)
from cfdbot.data import load_mt5_csv  # noqa: E402
from cfdbot.train.config import load_train_config, local_config_path, set_local_option  # noqa: E402


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
            cache.write_bytes(_get(url, timeout=90, tries=2))
            time.sleep(0.5)
        tables.append(parse_cot(cache.read_bytes()))
    return pd.concat(tables, ignore_index=True)


def _fred_key(args) -> str | None:
    """FRED の API キー（任意）。--fred-key で渡すと config/local.toml に保存して次回から使う。"""
    if args.fred_key:
        key = args.fred_key.strip()
        set_local_option(args.config, "context", "fred_api_key", key)
        print("FRED の API キーを config/local.toml に保存した（git には入らない）")
        return key
    local = local_config_path(args.config)
    if local.exists():
        return (tomllib.loads(local.read_text(encoding="utf-8")).get("context") or {}).get("fred_api_key") or None
    return os.environ.get("FRED_API_KEY") or None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dest", default="data/context")
    p.add_argument("--since", default="2000-01-01", help="Yahoo の取得開始日（FRED は全期間）")
    p.add_argument("--only", default="", help="取得元を絞る（fred,yahoo,cftc）")
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--fred-key", help="FRED の API キー（任意。あると FRED から確実に取れる。一度渡せば保存される）")
    args = p.parse_args()

    store = ContextStore(args.dest)
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    api_key = _fred_key(args)
    rows, failed = [], []
    cot_table = None
    cot_error = ""
    yahoo_cache: dict[str, pd.DataFrame] = {}
    fred_down = False      # FRED に一度つながらなければ、残りは待たずに代わり（Yahoo）を使う
    yahoo_fails = 0        # Yahoo が 3 回続けて失敗したら残りは省く
    headers = {"fred": "FRED（米連銀のデータベース）", "yahoo": "Yahoo Finance", "cftc": "CFTC（投機筋の建玉）"}
    shown = set()
    for spec in CATALOG:
        if only and spec.source not in only:
            continue
        if spec.source not in shown:
            shown.add(spec.source)
            print(f"取得中: {headers[spec.source]}…", flush=True)
        note = ""
        try:
            if spec.source == "fred":
                df = None
                if not fred_down:
                    try:
                        df = fetch_fred(spec.code, api_key)
                    except Exception as e:  # noqa: BLE001
                        fred_down = True
                        print(f"  FRED に接続できない（{str(e)[:120]}）。残りは待たずに Yahoo の代わりのデータを使う",
                              flush=True)
                if df is None:
                    if spec.key not in FRED_ALT:
                        raise RuntimeError("FRED に接続できず、代わりのデータも無い")
                    if yahoo_fails >= 3:
                        raise RuntimeError("FRED にも Yahoo にも接続できない")
                    try:
                        df = fetch_alt(spec.key, args.since, yahoo_cache)
                        yahoo_fails = 0
                    except Exception:
                        yahoo_fails += 1
                        raise
                    note = f"（代わり: {df['via'].iloc[0]}）"
            elif spec.source == "yahoo":
                if yahoo_fails >= 3:
                    raise RuntimeError("Yahoo に続けて接続できないので省いた")
                try:
                    df = yahoo_cache.get(spec.code)
                    if df is None:
                        df = fetch_yahoo(spec.code, args.since)
                        time.sleep(0.4)
                    yahoo_fails = 0
                except Exception:
                    yahoo_fails += 1
                    raise
            else:
                if cot_error:
                    raise RuntimeError(cot_error)
                if cot_table is None:
                    print("  建玉明細の zip を取得中（初回は数分かかる）…", flush=True)
                    try:
                        cot_table = fetch_cot(store, args.since)
                    except Exception as e:  # noqa: BLE001  1 回失敗したら残りの銘柄は待たない
                        cot_error = f"CFTC に接続できない（{str(e)[-120:]}）"
                        raise RuntimeError(cot_error) from None
                df = cot_series(cot_table, spec.code)
            if df.empty:
                raise RuntimeError("データが空")
            span = _save(df, store.path(spec))
            rows.append((spec.label, spec.key, spec.source, span))
            print(f"  ✓ {spec.label}（{spec.key}）: {span}{note}", flush=True)
        except Exception as e:  # noqa: BLE001  1 つ失敗しても残りは続ける
            failed.append((spec.label, spec.key, spec.source, str(e)[:160]))
            print(f"  ✗ {spec.label}（{spec.key}）: 取得できない — {str(e)[:160]}", flush=True)
    print(f"\n{len(rows)} 系列を {store.root}/ に保存した" + (f"、{len(failed)} 系列は失敗" if failed else ""))
    if fred_down and not api_key:
        print("FRED には接続できなかった（混雑か、接続の制限）。代わりのデータで研究は進められる。"
              "本物の FRED のデータを使いたいときは、無料の API キーを取って\n"
              "  ./cfd context --fred-key <キー>\n"
              "で取り直す（キーは https://fredaccount.stlouisfed.org/apikeys で発行。メールアドレスの登録だけ）")
    if failed:
        if len([f for f in failed if f[2] == "yahoo"]) > 5:
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
