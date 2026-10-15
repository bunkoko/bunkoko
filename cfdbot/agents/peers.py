"""似た会社への波及（docs/text_signals.md の 1A・1B）。

E1 の事象（通期の純利益の会社予想を 5% 以上引き上げた開示。訂正は使わない）の判断の日に、まだ今期の決算を出していない
「似た会社」を、その日の引けで買って 5 日持つ。似た会社は 1A が同じ東証 33 業種、1B が文章（有報の「事業の内容」）の
埋め込みで近い 5 社。1B − 1A が、埋め込みの上乗せ。どちらも株 × 日の「選んだか」の表を作り、評価は evaluator の book_pnl。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- 決まり（docs/text_signals.md 3 章。結果を見る前に決めた）
UP = 0.05                    # E1: 通期の純利益の予想を 5% 以上引き上げ（docs/agents.md 10.2 と同じ）
DOWN = -0.05                 # 参考（判定しない）: 5% 以上の引き下げ
REPORTED_WINDOW = 20         # その日を含む直近 20 営業日に、決算短信か業績予想の修正の数字の開示があれば「もう出した」
REPORT_KINDS = (1, 2, 3, 4, 5)   # 第 1〜3 四半期・本決算・業績予想の修正（配当予想の修正 6 は数えない）
K_TEXT = 5                   # 1B: 文章で近い 5 社
START_1A, SPLIT_1A = "2016-10-01", "2021-01-01"
CHUNK_CHARS = 1_000          # 長い文書は 1,000 字ずつに切って埋め込み、平均する
MIN_DOC_CHARS = 200          # これより短い文書は使わない（読めなかった・空）
TRIALS = 83                  # 80 ＋ 1A ＋ 1B ＋ 3（docs/text_signals.md 2 章）


def tradable(open_: pd.DataFrame, close: pd.DataFrame) -> np.ndarray:
    """銘柄選定と同じ「その日に売買できる株」（終値と、前の日の引け → 寄りの値がある）。"""
    from .. import stocks_short as H

    return (close.notna() & H.parts(open_, close)["night"].notna()).to_numpy()


def event_matrices(table: pd.DataFrame, shape: tuple[int, int]) -> dict[str, np.ndarray]:
    """earnings.daily の表（pos / j / fc_rev / kind）から、株 × 日の E1・下方修正・数字の開示があった日の表。"""
    up, down, rep = (np.zeros(shape, bool) for _ in range(3))
    pos, j = table["pos"].to_numpy(int), table["j"].to_numpy(int)
    rev = table["fc_rev"].to_numpy(float)
    kind = table["kind"].to_numpy(float)
    up[pos, j] = np.nan_to_num(rev, nan=-np.inf) >= UP
    down[pos, j] = np.nan_to_num(rev, nan=np.inf) <= DOWN
    rep[pos, j] = np.isin(kind, REPORT_KINDS)
    return {"up": up, "down": down, "reported": rep}


def recently_reported(rep: np.ndarray, window: int = REPORTED_WINDOW) -> np.ndarray:
    """その日を含む直近 window 営業日に、数字の開示があったか（その日の判断の時点で分かっていること）。"""
    c = np.cumsum(rep.astype(np.int32), axis=0)
    prev = np.vstack([np.zeros((window, rep.shape[1]), np.int32), c[:-window]]) if len(c) > window else np.zeros_like(c)
    return (c - prev[:len(c)]) > 0


def candidates(tradable_: np.ndarray, reported_recent: np.ndarray, events: np.ndarray) -> np.ndarray:
    """似た会社として買ってよい株: 売買でき、直近に決算を出しておらず、その日の事象の会社でない。"""
    return tradable_ & ~reported_recent & ~events


def industry_peers(events: np.ndarray, sectors: np.ndarray, cand: np.ndarray) -> np.ndarray:
    """1A: その日に事象のあった会社と同じ 33 業種の、候補の株すべて。"""
    out = np.zeros_like(cand)
    for t in np.flatnonzero(events.any(axis=1)):
        secs = set(sectors[events[t]]) - {""}
        if secs:
            out[t] = cand[t] & np.isin(sectors, list(secs))
    return out


def other_industry_pool(events: np.ndarray, sectors: np.ndarray, cand: np.ndarray) -> np.ndarray:
    """1A の偶然: その日に事象の無い業種の候補（同じ日・同じ「まだ出していない」条件で、業種だけが違う）。"""
    out = np.zeros_like(cand)
    for t in np.flatnonzero(events.any(axis=1)):
        secs = set(sectors[events[t]])
        out[t] = cand[t] & ~np.isin(sectors, list(secs))
    return out


def text_peers(events: np.ndarray, sim: np.ndarray, cand: np.ndarray, k: int = K_TEXT) -> np.ndarray:
    """1B: その日に事象のあった会社ごとに、文章の近さの順に、候補の株から k 社（近さの無い会社は使わない）。"""
    out = np.zeros_like(cand)
    order = np.argsort(-np.nan_to_num(sim, nan=-np.inf), axis=1)
    has = np.isfinite(sim)
    for t in np.flatnonzero(events.any(axis=1)):
        for i in np.flatnonzero(events[t]):
            if not has[i].any():
                continue
            row = [j for j in order[i] if j != i and has[i, j] and cand[t, j]][:k]
            out[t, row] = True
    return out


def permute(sim: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """1B の偶然: 会社の名札をでたらめに付け替えた近さの表（近さの形はそのまま、どの会社が近いかだけが合わなくなる）。"""
    p = rng.permutation(len(sim))
    return sim[np.ix_(p, p)]


# --------------------------------------------------------------------------- 文書（有報の「事業の内容」）と近さ
def clean_doc(text: str, company: str = "") -> str:
    """埋め込む前の文書: 全角・半角をそろえ、その会社の名前を消し、数字を 0 にする（どの会社か・いつかを分かりにくくする）。"""
    t = unicodedata.normalize("NFKC", str(text))
    name = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(company)))
    for n in {name, re.sub(r"(株式会社|\(株\))", "", name)}:
        if len(n) >= 2:
            t = t.replace(n, "")
    t = re.sub(r"\d+", "0", t)
    return re.sub(r"\s+", " ", t).strip()


def doc_vectors(texts: list[str], encode: Callable[[list[str]], np.ndarray]) -> np.ndarray:
    """文書ごとに、1,000 字ずつに切って埋め込んだものの平均（長さ 1 にそろえる）。"""
    rows = []
    for t in texts:
        parts = [t[i:i + CHUNK_CHARS] for i in range(0, len(t), CHUNK_CHARS)] or [""]
        v = np.asarray(encode(parts), dtype=np.float32).mean(axis=0)
        n = np.linalg.norm(v)
        rows.append(v / n if n > 0 else v)
    return np.vstack(rows) if rows else np.zeros((0, 0), np.float32)


def similarity(tickers: list[str], doc_tickers: list[str], vecs: np.ndarray) -> np.ndarray:
    """株の並び（tickers）にそろえたコサインの近さ（文書の無い株は NaN、自分自身も NaN）。"""
    n = len(tickers)
    sim = np.full((n, n), np.nan, np.float32)
    pos = {t: k for k, t in enumerate(tickers)}
    idx = [pos[t] for t in doc_tickers if t in pos]
    keep = [k for k, t in enumerate(doc_tickers) if t in pos]
    if not idx:
        return sim
    v = vecs[keep]
    sim[np.ix_(idx, idx)] = v @ v.T
    np.fill_diagonal(sim, np.nan)
    return sim


def start_after_docs(doc_dates: pd.Series, index: pd.DatetimeIndex, earliest: str = START_1A) -> pd.Timestamp:
    """1B の判定の期間の始まり: 使う文書のうち一番新しいものの日の翌営業日（2016-10 より前にはしない）。"""
    last = pd.to_datetime(doc_dates).max()
    after = index[index > last]
    start = after[0] if len(after) else index[-1]
    return max(start, pd.Timestamp(earliest))
