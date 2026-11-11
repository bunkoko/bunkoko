"""質（収益性の高い会社）に傾ける検証 Q1。docs/quality.md（結果を見る前に決めたもの）。

フレンチ教授のデータ集の、日本の「規模 × 営業の収益性」の 6 組のうち「大型 × 収益性の上位 30%」から、
日本の市場全体を引く（日本の割安株 C3 と同じ形・同じ判定）。1 回だけ計算し、動かし直さない。

ここでは読み取り・形の点検・計算・1 回だけの印をする（取得と報告の書き出しは scripts/quality_study.py）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import stocks as S

FILE_PORT, FILE_MKT = "Japan_6_Portfolios_ME_OP", "Japan_3_Factors"   # フレンチ教授のデータ集のファイル（_CSV.zip を付けて取る）
SECTION, COLUMN = "Value Weight", "BIG HiOP"                         # 時価総額の加重・大型 × 収益性の上位 30%
N_COLUMNS = 6
TURN, COST = 0.05, S.COST_JP          # C3 と同じ置き方（月に 5% 入れ替え × 0.10%）
SPLIT = "2008-01-01"                  # C3 と同じ（〜2007／2008〜）
MAX_TRIALS = 99                       # 打ち間違いへの備え（今わかっている予定は 97 まで）
PRIOR_TRIALS = 92                     # 登録した日（2026-11-11）までに試した数。Q1 は 92 より前の番号にはならない
START_BY, MIN_MONTHS, MAX_ABS, MIN_CORR = "1991-07-31", 360, 0.50, 0.80   # 形の点検（docs/quality.md 4 章）
MARKER = Path("data") / "quality" / "Q1_DONE.json"   # 1 回だけの印（リポジトリの根から。報告の保存先とは別）
RESULT: dict = {}                     # 1 回計算して docs/quality.md 9 章に書いたら、ここにも書く（{"date":…, "verdict":…, "detail":…}）。
                                      # 書いてあれば、印のファイルが無い別の機械でも動かさない


class QualityError(Exception):
    """データが取れない・形が違う。何も計算せずに止まる。"""


# --------------------------------------------------------------------------- 読み取りと形の点検
def _table(blob: bytes, name: str, section: str | None) -> pd.DataFrame:
    try:
        return S.read_french_zip(blob, section)
    except Exception as e:  # noqa: BLE001  zip でない・表が無い、など
        raise QualityError(f"{name} を読めない（{type(e).__name__}: {str(e)[:100]}）。取れたものがデータ集の zip ではない。"
                           "回線の不調かもしれないので、時間をおいてもう一度（取れなかったのと同じ扱い。docs/quality.md 4 章）") from e


def load(port_blob: bytes, mkt_blob: bytes) -> tuple[pd.Series, pd.Series, dict]:
    """2 つのファイルから「大型 × 収益性の上位 30%」と市場全体の月次リターンを取り出す。

    形が登録と違えば QualityError（差は計算しない）。返す dict は形の情報だけ（リターンの数字は入れない）。
    """
    port = _table(port_blob, FILE_PORT, SECTION)
    cols = list(port.columns)
    if len(cols) != N_COLUMNS or COLUMN not in cols:
        raise QualityError(f"{FILE_PORT} の列が登録と違う（{N_COLUMNS} 列で「{COLUMN}」がある、のはず。実際: {cols}）")
    p = port[COLUMN]
    if p.isna().all():
        raise QualityError(f"「{COLUMN}」が空")
    p = p.loc[p.first_valid_index():p.last_valid_index()]
    months = pd.period_range(p.index[0], p.index[-1], freq="M")
    if p.isna().any() or len(p) != len(months) or not (p.index.to_period("M") == months).all():
        raise QualityError(f"「{COLUMN}」の月が続いていない（欠け {int(p.isna().sum())}、{len(p)} 行、{p.index[0]:%Y-%m}〜{p.index[-1]:%Y-%m}）")
    if p.index[0] > pd.Timestamp(START_BY) or len(p) < MIN_MONTHS:
        raise QualityError(f"期間が登録と違う（{p.index[0]:%Y-%m} から {len(p)} か月。{START_BY[:7]} までに始まり {MIN_MONTHS} か月以上、のはず）")
    fac = _table(mkt_blob, FILE_MKT, None)
    if "Mkt-RF" not in fac.columns or "RF" not in fac.columns:
        raise QualityError(f"{FILE_MKT} の列が登録と違う（Mkt-RF と RF があるはず。実際: {list(fac.columns)}）")
    if not fac.index.is_unique:
        raise QualityError(f"{FILE_MKT} に同じ月が 2 回ある")
    m = (fac["Mkt-RF"] + fac["RF"]).reindex(p.index)
    if m.isna().any():
        raise QualityError(f"市場全体のリターンが {int(m.isna().sum())} か月分欠けている（2 つのファイルの期間が合わない）")
    if float(p.abs().max()) > MAX_ABS or float(m.abs().max()) > MAX_ABS:
        raise QualityError(f"1 か月に ±{MAX_ABS:.0%} を超える値がある（単位が % でない、など）")
    ratio = float(p.std() / m.std()) if float(m.std()) > 0 else float("nan")
    if not 0.5 <= ratio <= 2.0:                      # 片方だけ単位が違う（1/100 など）への備え。差の平均は見ない
        raise QualityError(f"組と市場全体の値動きの大きさが合わない（標準偏差の比 {ratio:.2f}。0.5〜2.0 のはず。単位の取り違えかもしれない）")
    corr = float(np.corrcoef(p.to_numpy(), m.to_numpy())[0, 1])
    if not corr >= MIN_CORR:
        raise QualityError(f"組と市場全体の動きが合わない（相関 {corr:.2f}。{MIN_CORR:.2f} 以上のはず。別の地域・別の通貨のファイルかもしれない）")
    return p, m, {"columns": cols, "start": f"{p.index[0]:%Y-%m}", "end": f"{p.index[-1]:%Y-%m}", "months": len(p), "corr": corr}


# --------------------------------------------------------------------------- 計算
def q1(port: pd.Series, mkt: pd.Series, trials: int) -> dict:
    """Q1 の判定（docs/stocks.md 5 章の条件 1〜3。偶然との比べは作れないので C3 と同じく無し）。"""
    if not PRIOR_TRIALS < trials <= MAX_TRIALS:
        raise QualityError(f"試した数は {PRIOR_TRIALS + 1}〜{MAX_TRIALS} のはず（92 は行き過ぎの回避の確かめ直しの番号。docs/quality.md 5 章）")
    g = (port - mkt).dropna()
    d = g - TURN * COST
    j = S.judge(d, SPLIT, trials)
    j.update(key="Q1", trials=trials, gross=float(g.mean()), last10=float(g.tail(120).mean()), last3=float(g.tail(36).mean()))
    return j


def _pct(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}%}" if x is not None and np.isfinite(x) else "–"


def _f2(x: float) -> str:
    return f"{x:.2f}" if x is not None and np.isfinite(x) else "–"


def summary(j: dict) -> str:
    return f"コストの後 年 {_pct(j['mean'] * 12)}、t {_f2(j['t'])}（後半 {_f2(j['t2'])}）、基準 {_f2(j['threshold'])}"


def report_lines(j: dict, info: dict, made: str) -> list[str]:
    return [
        "# 質（収益性の高い会社）に傾ける検証 Q1",
        "",
        f"作成 {made}。仮説と判定の条件は docs/quality.md（結果を見る前に決めたもの）。この計算は 1 回だけで、動かし直さない。",
        "",
        "## 1. 判定",
        "",
        f"**Q1（日本・大型・収益性の上位 30% − 日本の市場全体）は「{j['verdict']}」**",
        "",
        "| 記号 | 差（全期間。コスト込み） | 前半 → 後半 | t 値 全期間／後半 | 基準 | 判定 |",
        "|---|---|---|---|---|---|",
        f"| Q1 | 年 {_pct(j['mean'] * 12)} | {_pct(j['mean1'] * 12)} → {_pct(j['mean2'] * 12)} | {_f2(j['t'])}／{_f2(j['t2'])} | "
        f"{_f2(j['threshold'])}（{j['trials']} 個） | {j['verdict']} |",
        "",
        f"- 期間 {j['start']:%Y-%m}〜{j['end']:%Y-%m}（{j['n']} か月）。前半は 〜2007-12、後半は 2008-01〜",
        "- 条件（docs/stocks.md 5 章）: 1 前半・後半とも差がプラス、2 後半だけで t 値 1.65 以上、3 全期間の t 値が基準以上。"
        "偶然との比べは作れないので無し（C3 と同じ）",
        f"- 試した数 {j['trials']}（全期間の t 値の基準 {j['threshold']:.3f}）",
        "",
        "## 2. 参考（判定には使わない）",
        "",
        f"- コストを引く前の差 年 {_pct(j['gross'] * 12)}。直近 10 年は 年 {_pct(j['last10'] * 12)}、直近 3 年は 年 {_pct(j['last3'] * 12)}",
        f"- コストは月に {TURN:.0%} 入れ替え × {COST:.2%}（C3 と同じ置き方）",
        "",
        "## 3. データと注意",
        "",
        f"- フレンチ教授のデータ集の {FILE_PORT}（「{COLUMN}」、時価総額の加重）と {FILE_MKT}（Mkt-RF ＋ RF）。"
        f"{info['start']}〜{info['end']} の {info['months']} か月。列: {'、'.join(info['columns'])}",
        "- つぶれた会社・上場をやめた会社も入っている。ドル建て（市場との差をとるので為替はほぼ打ち消される）。税金は入れていない",
        "- 紙の上の組で、そのまま買える商品ではない。「使えそう」でも、買える商品（ETF など）での確かめを別に登録するまで、お金は入れない",
        "- 「使えそう」以外は勧めない。結果を見て質の決め方（別の指標・別の組・質 × 割安）を替えて試し直さない",
    ]


# --------------------------------------------------------------------------- 1 回だけの印
def done(marker: Path) -> dict | None:
    """もう計算してあれば、その記録（無ければ None）。プログラムに書いた RESULT が先。"""
    if RESULT:
        return dict(RESULT)
    if marker.exists():
        try:
            rec = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001  壊れた印も「計算した」とみなす（消して動かし直さない）
            return {"date": "?", "verdict": "?", "detail": f"{marker} が読めない"}
        if not (isinstance(rec, dict) and rec.get("verdict")):    # 中身が空・変でも、印があれば止める
            return {"date": "?", "verdict": "?", "detail": f"{marker} の中身が変"}
        return rec
    return None


def mark(marker: Path, j: dict, lines: list[str], made: str) -> None:
    """判定を出したら、まず印を書く（報告の中身も入れておく。表示や保存の途中で止まっても結果が残るように）。"""
    marker.parent.mkdir(parents=True, exist_ok=True)
    rec = {"date": made, "verdict": j["verdict"], "detail": summary(j), "trials": j["trials"], "report": lines}
    marker.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
