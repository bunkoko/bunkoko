"""案 2-US の CLI の通し（作ったデータと作り物の SEC。docs/us_text.md 9 章）。名乗りの文字が記録・報告に出ないことも確かめる。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdbot import sec as SEC

ROOT = Path(__file__).resolve().parents[1]
N = 310
UA = "Secret Person secret.person@example.com"


def _mod():
    spec = importlib.util.spec_from_file_location("edgar_flow", ROOT / "scripts" / "edgar.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _world(tmp_path):
    root = tmp_path / "edgar"
    (root / "universe").mkdir(parents=True)
    ends = pd.date_range("2021-12-31", "2026-08-31", freq="ME")
    mem = pd.DataFrame([(m, f"S{i}", f"Firm{i} Inc", ["Energy", "Utilities", "Materials", "Financials"][i % 4], i + 1, f"S{i}")
                        for m in ends for i in range(N)], columns=["month", "ticker", "name", "sector", "cik", "symbol"])
    mem.to_csv(root / "universe" / "members.csv", index=False)
    idx = pd.bdate_range("2021-11-01", "2026-09-30")
    rng = np.random.default_rng(0)
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.01, (len(idx), N)), axis=0)), index=idx, columns=[f"S{i}" for i in range(N)])
    close.to_csv(root / "us_close.csv")
    words = [f"w{k}" for k in range(400)]

    def doc(cik, year):
        r = np.random.default_rng(cik * 100 + (year if cik % 3 == 0 else 0))        # 3 の倍数の会社は毎年書き直す
        body = " ".join(r.choice(words, 400))
        return (f"<html><body><p>Table of Contents</p><p>Item 1A. Risk Factors 12</p><p>Item 2. Properties 30</p>"
                f"<p>Item 1A. Risk Factors</p><p>Firm{cik - 1} Inc faces risks. {body}</p><p>Item 1B. Unresolved Staff Comments</p>"
                f"<p>Item 2. Properties</p></body></html>").encode()

    def getter(url):
        if "submissions/CIK" in url:
            cik = int(url.split("CIK")[1][:10])
            years = list(range(2019, 2026))
            rec = {"accessionNumber": [f"{cik:010d}-{y % 100:02d}-000001" for y in years], "form": ["10-K"] * len(years),
                   "filingDate": [f"{y + 1}-02-20" for y in years], "reportDate": [f"{y}-12-31" for y in years],
                   "acceptanceDateTime": [f"{y + 1}-02-20T16:05:00.000Z" for y in years], "primaryDocument": ["k.htm"] * len(years)}
            return 200, json.dumps({"name": f"FIRM{cik - 1} INC", "filings": {"recent": rec, "files": []}}).encode()
        part = url.split("/Archives/edgar/data/")[1].split("/")
        cik, acc = int(part[0]), part[1]
        return 200, doc(cik, 2000 + int(acc[10:12]))
    return root, getter


def test_check_fetch_status_lazy_flow_and_user_agent_never_written(tmp_path, monkeypatch, capsys):
    root, getter = _world(tmp_path)
    mod = _mod()
    monkeypatch.setattr(SEC, "KEY_FILE", tmp_path / "home" / "sec.json")
    SEC.save_user_agent(UA, SEC.KEY_FILE)
    base = ["--root", str(root), "--out", str(tmp_path / "out")]
    nosleep = lambda s: None  # noqa: E731
    check = mod.main(["check", *base], getter=getter, sleep=nosleep).read_text(encoding="utf-8")
    assert "点検 2" in check and "始まり: Item 1A. Risk Factors" in check and "指令役" in check
    with pytest.raises(SystemExit, match="まだ終わっていない"):
        mod.main(["lazy", *base])                                                      # 取得が途中なら止める
    mod.main(["fetch", *base], getter=getter, sleep=nosleep)
    mod.main(["status", *base])
    out = capsys.readouterr().out
    assert "点検 3" in out and "通った" in out
    lazy = mod.main(["lazy", *base, "--today", "2026-10-21"]).read_text(encoding="utf-8")
    assert "## 1. 判定" in lazy and "基準" in lazy and "2.485" in lazy
    with pytest.raises(SystemExit, match="動かし直さない"):
        mod.main(["lazy", *base, "--today", "2026-10-21"])                             # 1 回だけ
    for f in list(root.rglob("*")) + list((tmp_path / "out").rglob("*")):
        if f.is_file():
            raw = f.read_bytes()
            if f.suffix == ".gz":
                import gzip
                raw = gzip.decompress(raw)
            assert b"secret.person" not in raw and b"Secret Person" not in raw            # 名乗りはどこにも残らない
    assert "secret.person" not in out


def test_key_rejects_non_ascii_and_partial_month_is_cut_by_the_price_date(tmp_path, monkeypatch):
    mod = _mod()
    answers = iter(["山田 太郎", "taro@example.com"])
    monkeypatch.setattr(mod.getpass, "getpass", lambda prompt="": next(answers))
    with pytest.raises(SystemExit, match="半角"):
        mod.main(["key"])
    root, getter = _world(tmp_path)
    close = pd.read_csv(root / "us_close.csv", index_col=0, parse_dates=True)
    close[close.index <= "2026-09-17"].to_csv(root / "us_close.csv")                # 値段は 9 月の途中まで
    base = ["--root", str(root), "--out", str(tmp_path / "out")]
    nosleep = lambda s: None  # noqa: E731
    mod.main(["fetch", *base], getter=getter, sleep=nosleep)
    text = mod.main(["lazy", *base, "--today", "2026-11-02"]).read_text(encoding="utf-8")
    assert "値段は 2026-09-17 まで" in text and "〜2026-08（" in text                   # 9 月（途中まで）は持った月に入らない


def test_a_changed_extractor_refetches_documents(tmp_path, monkeypatch):
    root, getter = _world(tmp_path)
    mod = _mod()
    base = ["--root", str(root), "--out", str(tmp_path / "out")]
    nosleep = lambda s: None  # noqa: E731
    mod.main(["check", *base], getter=getter, sleep=nosleep)
    n1 = len(SEC.Store(root / "sec").index())
    monkeypatch.setattr(SEC, "EXTRACT_VERSION", "2")
    calls = []
    mod.main(["check", *base], getter=lambda u: (calls.append(u), getter(u))[1], sleep=nosleep)
    assert n1 == 60 and sum("/Archives/" in u for u in calls) == 60                   # 取り出し方を変えたら、くじの 60 件を取り直す


@pytest.mark.parametrize("when, found", [("after", True), ("before", False)])
def test_planted_effect_is_found_through_the_cli_and_the_future_is_not_used(tmp_path, monkeypatch, when, found):
    """英字の単語の 10-K で通しを流す。前年から変えていない会社が受付の後に上がる世界では「使えそう」、
    受付の前にだけ上がる世界（先読みの罠）では見えない（2026-10-21、本番の前の確かめ）。"""
    root, getter0 = _world(tmp_path)
    rng = np.random.default_rng(42)
    vocab = np.array(sorted({"".join(rng.choice(list("abcdefghijklmnopqrstuvwxyz"), rng.integers(4, 10))) for _ in range(3000)}))
    years = range(2019, 2026)
    share = {(c, y): float(rng.uniform(0, 0.8)) for c in range(1, N + 1) for y in years}       # 前年から入れ替えた割合
    texts = {}
    for c in range(1, N + 1):
        r = np.random.default_rng(c)
        cur = r.choice(vocab, 600)
        for y in years:
            if y > 2019:
                cur = cur.copy()
                k = int(share[(c, y)] * len(cur))
                cur[r.choice(len(cur), k, replace=False)] = r.choice(vocab, k)
            texts[(c, y)] = " ".join(cur)
    idx = pd.bdate_range("2021-11-01", "2026-09-30")
    ret = np.random.default_rng(7).normal(0, 0.015, (len(idx), N))
    for (c, y), v in share.items():
        if v < 0.16 and y > 2019:
            a = pd.Timestamp(f"{y + 1}-02-20")                                                 # 受付の日
            span = (idx > a) & (idx <= a + pd.Timedelta(days=365)) if when == "after" else (idx > a - pd.Timedelta(days=365)) & (idx <= a)
            ret[span, c - 1] += 0.015 / 21                                                     # 月 +1.5%
    pd.DataFrame(100 * np.exp(np.cumsum(ret, axis=0)), index=idx, columns=[f"S{i}" for i in range(N)]).to_csv(root / "us_close.csv")

    def getter(url):
        if "submissions/CIK" in url:
            return getter0(url)
        cik, acc = url.split("/Archives/edgar/data/")[1].split("/")[:2]
        body = texts[(int(cik), 2000 + int(acc[10:12]))]                                       # 番号の 11〜12 文字目 = 期末の年
        return 200, (f"<html><body><p>Item 1A. Risk Factors</p><p>Firm{int(cik) - 1} Inc faces risks. {body}</p>"
                     f"<p>Item 1B. Unresolved Staff Comments</p></body></html>").encode()

    mod = _mod()
    monkeypatch.setattr(SEC, "KEY_FILE", tmp_path / "home" / "sec.json")
    SEC.save_user_agent(UA, SEC.KEY_FILE)
    base = ["--root", str(root), "--out", str(tmp_path / "out")]
    mod.main(["fetch", *base], getter=getter, sleep=lambda s: None)
    verdict = mod.main(["lazy", *base, "--today", "2026-10-21"]).read_text(encoding="utf-8").split("## 1. 判定")[1].split("\n- ")[0]
    assert ("**使えそう**" in verdict) == found
