"""株の短期の売買（一晩〜5 日）を確かめる（./cfd shortterm）。ルール・機械学習・ニュース・強化学習の 6 つの仮説。

    python scripts/stocks_short_study.py       # data/stocks（./cfd stocks --fetch で取ったもの）と data/news で計算する（10〜20 分）

仮説と判定の条件は docs/stocks_short.md（結果を見る前に決めたもの）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cfdbot import stocks as S  # noqa: E402
from cfdbot import stocks_short as H  # noqa: E402

ETF = "1306.T"


def wide(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, index_col=0, parse_dates=True)


def pct(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}%}" if x is not None and np.isfinite(x) else "–"


def f2(x: float) -> str:
    return f"{x:.2f}" if x is not None and np.isfinite(x) else "–"


def load_news(root: Path, model: str) -> tuple[pd.DatetimeIndex, np.ndarray, np.ndarray]:
    """見出しのベクトルを、使える日（日本時間 13:00 締め）ごとの平均と件数にする。"""
    sums: dict[pd.Timestamp, np.ndarray] = {}
    counts: dict[pd.Timestamp, int] = {}
    for csv in sorted((root / "headlines").glob("*/*.gkg.csv")):
        day = csv.name.split(".")[0]
        npz = root / "emb" / model / day[:7] / f"{day}.npz"
        if not npz.exists():
            continue
        df = pd.read_csv(csv, usecols=["id", "available"], dtype=str, keep_default_na=False, on_bad_lines="skip")
        z = np.load(npz, allow_pickle=False)
        pos = {i: k for k, i in enumerate(z["ids"].astype(str))}
        df = df[df["id"].isin(pos)].drop_duplicates("id")
        if df.empty:
            continue
        vecs = z["vecs"][[pos[i] for i in df["id"]]].astype(np.float64)
        bucket = H.news_bucket(df["available"]).to_numpy()
        for b in np.unique(bucket):
            m = bucket == b
            key = pd.Timestamp(b)
            sums[key] = sums.get(key, 0.0) + vecs[m].sum(axis=0)
            counts[key] = counts.get(key, 0) + int(m.sum())
    days = pd.DatetimeIndex(sorted(sums))
    n = np.array([counts[d] for d in days])
    return days, np.vstack([sums[d] / counts[d] for d in days]), n


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data/stocks")
    p.add_argument("--news", default="data/news")
    p.add_argument("--placebo", type=int, default=1000)
    p.add_argument("--out", default="output/shortterm")
    args = p.parse_args()

    root = Path(args.data)
    if not (root / "jp_close.csv").exists():
        sys.exit("data/stocks が無い。先に ./cfd stocks --fetch を実行する")
    n_trials = H.PRIOR_TRIALS + H.NEW_TRIALS
    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    uni = pd.read_csv(root / "universe_jp.csv").set_index("ticker")
    open_, close = S.drop_closed_days(wide(root / "jp_open.csv"), wide(root / "jp_close.csv"))
    pr = H.parts(open_, close)
    night, cc = pr["night"], pr["cc"]
    tradable_df = close.notna() & night.notna()
    tradable = tradable_df.to_numpy()
    mkt = H.market_mean(cc)
    rng = np.random.default_rng(11)
    results: list[dict] = []
    notes: dict[str, list[str]] = {}
    picks: dict[str, list[str]] = {}

    def add(key: str, label: str, d: pd.Series, split: str, null: np.ndarray | None = None, real: float | None = None) -> dict:
        pl = float(np.mean(null < real)) if null is not None and len(null) else None
        j = S.judge(d, split, n_trials, pl)
        j.update(key=key, label=label, split=split)
        results.append(j)
        d.to_csv(out / f"d_{key}.csv", header=["d"])
        print(f"  {key} {j['verdict']}", flush=True)
        return j

    def night_block(key: str, label: str, score: pd.DataFrame, start: str, split: str) -> np.ndarray:
        """夜だけ持つ形: 判定と細かい数字。選んだ行列を返す。"""
        sel = H.top_share(score, tradable_df)
        m = np.asarray(night.index >= start)
        pnl = H.night_pnl(night, sel, H.COST)[m]
        r_next = night.shift(-1).to_numpy()[m]
        null = np.empty(args.placebo)
        for i in range(args.placebo):
            s = H.random_like(sel[m], tradable[m], rng) & np.isfinite(r_next)
            k = s.sum(axis=1)
            null[i] = np.mean(np.where(k > 0, (np.nan_to_num(r_next) * s).sum(axis=1) / np.maximum(k, 1), 0.0))
        add(key, label, S.month_sum(pnl["d"]), split, null, float(pnl["gross"].mean()))
        on = pnl["names"] > 0
        net, hold = H.perf(pnl["d"]), H.perf(mkt[m])
        late = pnl[pnl.index >= split]
        top10 = H.night_pnl(night, H.top_share(score, tradable_df, count=10), H.COST)[m]
        by_year = pnl["d"].groupby(pnl.index.year).sum()
        notes.setdefault(key, []).append(
            f"選んだ株の夜のリターンは 1 晩あたり {pct(pnl['gross'][on].mean(), 3)}（{split[:4]} 年から {pct(late['gross'].mean(), 3)}）。"
            f"これが耐えられる往復コストの上限。全銘柄の平均は 1 晩 {pct(np.nanmean(r_next), 3)}。平均 {pnl['names'][on].mean():.0f} 銘柄")
        notes[key].append(
            f"コスト 0.10% を引いた後: 年 {pct(net['ann'])}、値動きの大きさ 年 {pct(net['vol'])}、シャープ {f2(net['sharpe'])}、最大の下げ {pct(net['mdd'])}。"
            f"プラスの年 {int((by_year > 0).sum())}/{len(by_year)}。同じ期間に全銘柄を持ち続けた場合: 年 {pct(hold['ann'])}、"
            f"値動き {pct(hold['vol'])}、最大の下げ {pct(hold['mdd'])}")
        notes[key].append(f"参考: 上位 10 銘柄だけにすると、コストを引いた後 年 {pct(H.perf(top10['d'])['ann'])}（1 晩あたり手数料前 {pct(top10['gross'].mean(), 3)}）")
        last = np.flatnonzero(sel[-1])
        picks[key] = [f"{close.index[-1]:%Y-%m-%d} の時点で選ばれた {len(last)} 銘柄（その日の引けで買い、次の営業日の寄りで売る形）: "
                      + "、".join(f"{uni['name'].get(close.columns[j], close.columns[j])}（{close.columns[j].replace('.T', '')}）" for j in last)]
        return sel

    def swing_block(key: str, label: str, score: pd.DataFrame, start: str, split: str) -> pd.DataFrame:
        """5 日持つ形: 判定と細かい数字。日ごとの損益を返す。"""
        sel = H.top_share(score, tradable_df)
        m = np.asarray(cc.index >= start)
        pnl = H.weights_pnl(cc, H.cohort_weights(sel), H.COST)[m]
        n = max(args.placebo * 3 // 10, 1)
        null = np.array([H.weights_pnl(cc, H.cohort_weights(H.random_like(sel, tradable, rng)))["gross"][m].mean() for _ in range(n)])
        add(key, label, S.month_sum(pnl["d"]), split, null, float(pnl["gross"].mean()))
        total = H.perf(pnl["d"] + pnl["invested"] * mkt[m])
        hold = H.perf(mkt[m])
        notes.setdefault(key, []).append(
            f"全銘柄の平均との差: コストを引く前 年 {pct(pnl['gross'].mean() * 252)}（{split[:4]} 年から {pct(pnl['gross'][pnl.index >= split].mean() * 252)}）、"
            f"1 日に入れ替える割合 {pct(pnl['turnover'].mean(), 0)}、コストを引いた後 年 {pct(pnl['d'].mean() * 252)}")
        notes[key].append(
            f"そのまま持った成績（市場の値動きを含む。コスト込み）: 年 {pct(total['ann'])}、シャープ {f2(total['sharpe'])}、最大の下げ {pct(total['mdd'])}。"
            f"全銘柄を持ち続けた場合: 年 {pct(hold['ann'])}、シャープ {f2(hold['sharpe'])}、最大の下げ {pct(hold['mdd'])}")
        return pnl

    # ---- ルール
    print("ルール（K1・K2）…", flush=True)
    night_block("K1", "夜に上がってきた株（直近 20 日の夜の平均の上位 10%）を、夜だけ持つと利益が出る",
                H.night_mean_score(night), "2001-01-01", "2014-01-01")
    swing_block("K2", "直近 5 日に下がった株（下位 10%）を 5 日持つと、全銘柄の平均より上がる",
                H.reversal_score(open_), "2001-01-01", "2014-01-01")

    # ---- 機械学習
    print("機械学習の入力を作る…", flush=True)
    names, x = H.features(open_, close)
    y1, y5 = H.targets(open_, close)
    print("M1（今夜のリターン）を毎年学び直す…", flush=True)
    pred1 = H.walk_forward(x, y1, tradable)
    print("M2（これから 5 日のリターン）を毎年学び直す…", flush=True)
    pred5 = H.walk_forward(x, y5, tradable, embargo=H.EMBARGO)
    first = f"{H.FIRST_TEST_YEAR}-01-01"
    night_block("M1", "機械学習で選んだ株（今夜の予測の上位 10%）を、夜だけ持つと利益が出る", pred1, first, "2018-01-01")
    pnl_m2 = swing_block("M2", "機械学習で選んだ株（5 日の予測の上位 10%）を 5 日持つと、全銘柄の平均より上がる", pred5, first, "2018-01-01")
    for key, pred, y in (("M1", pred1, y1), ("M2", pred5, y5)):
        ic = H.yearly_ic(pred, y).dropna()
        notes[key].append("予測と実際の順位の相関（年ごと。0 = 当たっていない）: " + "、".join(f"{y_}: {v:+.3f}" for y_, v in ic.items()))
    del x

    # ---- 強化学習
    print("強化学習（Q1）…", flush=True)
    dec = H.deciles(pred5)
    excess_next = (cc.sub(mkt, axis=0)).shift(-1).to_numpy()
    tr = np.asarray((cc.index >= first) & (cc.index < H.RL_TRAIN_END))
    pol = H.fit_policy(dec[tr], excess_next[tr], H.COST)
    test = np.asarray(cc.index >= H.RL_TRAIN_END)
    slots = int(round(np.median(tradable[test].sum(axis=1)) * H.TOP_SHARE))
    w_rl = H.policy_weights(pred5, dec, pol["keep"], slots, start=int(np.flatnonzero(test)[0]))
    pnl_rl = H.weights_pnl(cc, w_rl, H.COST)[test]
    base = pnl_m2[pnl_m2.index >= H.RL_TRAIN_END]
    add("Q1", "強化学習で決めた入れ替え方は、決め打ちの 5 日保有（M2）より良い",
        S.month_sum(pnl_rl["d"]).sub(S.month_sum(base["d"]), fill_value=0.0), "2022-01-01")
    keep = [d for d in range(H.N_DECILES) if pol["keep"][d]]
    notes["Q1"] = [
        "学んだ入れ替え方: 予測の順位（10 段階。10 が一番上）が " + "・".join(str(d + 1) for d in keep)
        + f" の間は持ち続け、それ以外に下がったら入れ替える（枠 {slots} 銘柄）",
        "学習の期間（2010〜2017）の段階ごとの翌日の「株 − 平均」: " + "、".join(f"{d + 1}: {pct(v, 3)}" for d, v in enumerate(pol["mu"])),
        f"2018 年から: 強化学習 コスト前 年 {pct(pnl_rl['gross'].mean() * 252)}・1 日の入れ替え {pct(pnl_rl['turnover'].mean(), 1)}・"
        f"コスト後 年 {pct(pnl_rl['d'].mean() * 252)}。決め打ちの 5 日保有 コスト前 年 {pct(base['gross'].mean() * 252)}・"
        f"入れ替え {pct(base['turnover'].mean(), 1)}・コスト後 年 {pct(base['d'].mean() * 252)}"]

    # ---- ニュース
    print("ニュース（N1）…", flush=True)
    model = "qwen3-0.6b"
    news_root = Path(args.news)
    misc_o, misc_c = wide(root / "misc_open.csv")[ETF].dropna(), wide(root / "misc_close.csv")[ETF].dropna()
    etf_next = (misc_o.shift(-1) / misc_c - 1).clip(-S.CLIP, S.CLIP)              # t 日の引け → t+1 日の寄り
    if (news_root / "emb" / model).exists():
        days, mean_vec, count = load_news(news_root, model)
        cal = etf_next.index[(etf_next.index >= days[0]) & (etf_next.index <= days[-1])]
        pos = pd.Series(np.arange(len(days)), index=days).reindex(cal)
        have = pos.notna().to_numpy()
        cal = cal[have]
        v, n = mean_vec[pos[have].astype(int).to_numpy()], count[pos[have].astype(int).to_numpy()]
        y = etf_next.reindex(cal)
        enough = n >= H.NEWS_MIN_HEADLINES
        is_test = np.asarray(cal >= H.NEWS_JUDGE_FROM)
        pred, thr = H.news_model(v, n, y.to_numpy(), ~is_test & enough)
        hold = (pred > thr) & enough
        pnl = H.news_pnl(y[is_test], hold[is_test], H.COST)
        d_news = pnl["d"].resample("ME").sum()
        add("N1", "ニュースで選んだ夜だけ 1306 を持つと利益が出る", d_news, "2026-03-01",
            H.news_placebo(y[is_test], hold[is_test], args.placebo), float(pnl["gross"].mean()))
        on = pnl["on"].to_numpy()
        yt = y[is_test]
        notes["N1"] = [
            f"見出し {int(count.sum()):,} 件・{len(days)} 日。学習 {int((~is_test & enough).sum())} 晩（{cal[0]:%Y-%m}〜2025-06）、判定 {int(is_test.sum())} 晩（2025-07〜{cal[-1]:%Y-%m}）",
            f"判定の期間に持った夜 {int(on.sum())} 晩。その夜の 1306 のリターンは 1 晩あたり {pct(yt[on].mean(), 3)}、持たなかった夜は {pct(yt[~on].mean(), 3)}、"
            f"全部の夜は {pct(yt.mean(), 3)}。コスト 0.10% を引いた後の合計 {pct(pnl['d'].sum())}"]
        ref = []
        for year in range(2021, 2026):
            trm = np.asarray(cal < f"{year}-01-01") & enough
            tem = np.asarray((cal >= f"{year}-01-01") & (cal < min(f"{year + 1}-01-01", H.NEWS_JUDGE_FROM)))
            if trm.sum() < 200 or tem.sum() == 0:
                continue
            p_, t_ = H.news_model(v, n, y.to_numpy(), trm)
            ref.append(H.news_pnl(y[tem], ((p_ > t_) & enough)[tem], H.COST))
        if ref:
            r = pd.concat(ref)
            notes["N1"].append(
                f"参考（毎年学び直した 2021〜2025-06。言語モデルが出来事を知っている期間なので判定しない）: 持った夜 {int(r['on'].sum())}/{len(r)}、"
                f"その夜の 1 晩あたり {pct(r['gross'][r['on']].mean(), 3)}（全部の夜 {pct(y.reindex(r.index).mean(), 3)}）、コスト後 年 {pct(r['d'].mean() * 252)}、"
                f"月ごとの t 値 {f2(S.t_value(r['d'].resample('ME').sum()))}")
    else:
        notes["N1"] = [f"{news_root}/emb/{model} が無いので計算していない（./cfd news run の後に実行する）"]

    # ---- 参考: 米国
    us_note = []
    if (root / "us_close.csv").exists():
        print("参考: 米国…", flush=True)
        uo, uc = S.drop_closed_days(wide(root / "us_open.csv"), wide(root / "us_close.csv"))
        up = H.parts(uo, uc)
        ut = uc.notna() & up["night"].notna()
        m = np.asarray(uc.index >= "2001-01-01")
        k1 = H.night_pnl(up["night"], H.top_share(H.night_mean_score(up["night"]), ut))[m]
        k2 = H.weights_pnl(up["cc"], H.cohort_weights(H.top_share(H.reversal_score(uo), ut)))[m]
        us_note = [f"K1 と同じ作り方（S&P500 の今の銘柄、2001〜）: 選んだ株の夜のリターン 1 晩あたり {pct(k1['gross'].mean(), 3)}"
                   f"（2014 年から {pct(k1['gross'][k1.index >= '2014-01-01'].mean(), 3)}）。往復 0.30% のコストには届かない" if k1["gross"].mean() < S.COST_US
                   else f"K1 と同じ作り方（S&P500、2001〜）: 1 晩あたり {pct(k1['gross'].mean(), 3)}",
                   f"K2 と同じ作り方: 全銘柄の平均との差 コスト前 年 {pct(k2['gross'].mean() * 252)}（2014 年から {pct(k2['gross'][k2.index >= '2014-01-01'].mean() * 252)}）、"
                   f"1 日の入れ替え {pct(k2['turnover'].mean(), 0)}。往復 0.30% だとコストだけで年 {pct(k2['turnover'].mean() * 252 * S.COST_US)}"]

    # ---- 報告
    say("# 株の短期の売買（一晩〜5 日）: ルール・機械学習・ニュース・強化学習")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　仮説と判定の条件: docs/stocks_short.md（結果を見る前に決めたもの）")
    say(f"データ: 日本株 {close.shape[1]} 銘柄（TOPIX500 の今の銘柄、{close.index[0]:%Y-%m-%d}〜{close.index[-1]:%Y-%m-%d}）。コスト: 往復 {pct(H.COST, 2)}。"
        f"試した数: {H.PRIOR_TRIALS} ＋ {H.NEW_TRIALS} ＝ {n_trials}（全期間の t 値の基準 {f2(S.expected_max_z(n_trials))}）")
    say("売買の形: 13:30 までに決め（前日の引けまでと当日の寄りの情報）、引けで買う。夜だけ持つ形は翌朝の寄りで売り、5 日持つ形は 5 日後の引けで売る")
    say("\n## 1. 判定の一覧\n")
    say("差 = コストを引いた後の、月ごとの損益（K1・M1・N1）か、全銘柄の平均との差（K2・M2）か、5 日保有との差（Q1）。年率に直してある。\n")
    say("| 記号 | 仮説 | 期間 | 差（全期間） | 前半 | 後半 | t 値（全期間） | t 値（後半） | 偶然より良い割合 | 判定 |")
    say("|---|---|---|---|---|---|---|---|---|---|")
    for j in results:
        say(f"| {j['key']} | {j['label']} | {j['start']:%Y-%m}〜{j['end']:%Y-%m}（後半 {j['split'][:7]}〜） | 年率 {pct(j['mean'] * 12)} | "
            f"{pct(j['mean1'] * 12)} | {pct(j['mean2'] * 12)} | {f2(j['t'])} | {f2(j['t2'])} | "
            f"{pct(j['placebo'], 0) if j['placebo'] is not None else '（比べる相手なし）'} | **{j['verdict']}** |")
    by = {v: [j["key"] for j in results if j["verdict"] == v] for v in ("使えそう", "まだ判断できない", "使えない")}
    say("")
    for v, keys in by.items():
        say(f"- {v}: {'、'.join(keys) if keys else 'なし'}")
    say("\n## 2. 仮説ごとの細かい数字\n")
    for key in ("K1", "K2", "M1", "M2", "Q1", "N1"):
        for t in notes.get(key, []):
            say(f"- {key}: {t}")
    if us_note:
        say("\n参考: 米国（判定しない。コストを引く前）\n")
        for t in us_note:
            say(f"- {t}")
    say("\n## 3. 今あてはまるもの\n")
    say("「使えそう」「まだ判断できない」と判定された、夜だけ持つ形の選び方について、最後の日に選ばれた銘柄（例）。上がることの約束ではない。\n")
    shown = False
    for key in ("K1", "M1"):
        v = next((j["verdict"] for j in results if j["key"] == key), "")
        if v in ("使えそう", "まだ判断できない"):
            shown = True
            say(f"**{key}（{v}）**\n")
            for t in picks.get(key, []):
                say(f"- {t}")
            say("")
    if not shown:
        say("- なし\n")
    say("## 4. 読み方・限界\n")
    say("- 判定は 4 つの条件すべて（前半・後半ともプラス、後半だけで t 値 1.65 以上、全期間の t 値が試した数で割り引いた基準以上、偶然より良い割合 95% 以上）")
    say("- 今の構成銘柄（つぶれた会社・外れた会社が入らない）。寄り・引けで必ず約定する前提。税金は入れていない")
    say("- 機械学習の予測は、どれも学習に使っていない年のもの（2010 年から毎年、それより前だけで学び直した）")
    say("- 「使えそう」でも、まず記録だけの売買で本当のコストを測る（docs/stocks_short.md 8 章）")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    pd.DataFrame(results).drop(columns=["start", "end"]).to_csv(out / "verdicts.csv", index=False)
    pred1.iloc[-1].dropna().sort_values(ascending=False).head(60).to_csv(out / "last_scores_m1.csv", header=["score"])
    print(f"\n保存先: {out}/report.md")


if __name__ == "__main__":
    main()
