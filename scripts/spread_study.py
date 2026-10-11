"""2 つの差の戻り（WTI−ブレント、金÷銀）を確かめる（./cfd spread）。

    python scripts/spread_study.py      # data/universe の日足（無ければ Yahoo から取る）。期間B はフィリップのデータがある Mac で

決まりと判定の条件は docs/spread.md（結果を見る前に決めたもの）。
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

from cfdbot.context import fetch_yahoo  # noqa: E402
from cfdbot.spread import (COST, FINANCING, HL_MAX, HL_MIN, MAX_LEV, PAIRS, REFIT, VOL_TARGET, WINDOW, Z_IN,  # noqa: E402
                           Z_STOP, load_close, ou_state, pair_returns, placebo_sharpes, positions, sharpe,
                           spread_series)
from cfdbot.stats import psr_of  # noqa: E402
from cfdbot.study import clean  # noqa: E402
from cfdbot.train.config import load_train_config  # noqa: E402
from cfdbot.train.dataset import load_dataset  # noqa: E402
from cfdbot.universe import BY_KEY, data_path  # noqa: E402

PRIOR_TRIALS, NEW_TRIALS = 35, 2


def f2(x: float) -> str:
    return f"{x:.2f}" if np.isfinite(x) else "–"


def pct(x: float, digits: int = 0) -> str:
    return f"{x:.{digits}%}" if np.isfinite(x) else "–"


def mdd(r: pd.Series) -> float:
    eq = r.cumsum()                                      # 複利にしない（資金 1 に対する累積の損益）
    return float((eq - eq.cummax()).min()) if len(eq) else float("nan")


def yahoo_closes(root: Path, keys, since: str) -> dict[str, pd.Series]:
    out = {}
    for k in keys:
        m = BY_KEY[k]
        p = data_path(root, m)
        if not p.exists():
            root.mkdir(parents=True, exist_ok=True)
            fetch_yahoo(m.ticker, since).to_csv(p, index=False)
        out[k] = load_close(p)
    return out


def phillip_closes(cfg, keys) -> dict[str, pd.Series] | None:
    try:
        ds = load_dataset(cfg.data)
    except FileNotFoundError:
        return None
    out = {}
    for k in keys:
        if k in ds.symbols:
            f = ds.signal_frame(k, "D1")
            if f is not None and len(f):
                out[k] = f["close"].astype(float)
    return out if len(out) == len(keys) else None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config/train.toml")
    p.add_argument("--data", default="data/universe")
    p.add_argument("--since", default="2000-01-01")
    p.add_argument("--split", default="2021-06-01", help="期間A の終わり・期間B の始まり")
    p.add_argument("--placebo", type=int, default=200)
    p.add_argument("--out", default="output/spread")
    args = p.parse_args()

    cfg = load_train_config(args.config)
    root = Path(args.data)
    split = pd.Timestamp(clean(args.split), tz="UTC")
    keys = sorted({k for pr in PAIRS.values() for k in pr[:2]})
    yahoo = yahoo_closes(root, keys, args.since)
    phillip = phillip_closes(cfg, keys)

    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s, flush=True)
        lines.append(s)

    say("# 2 つの差の戻り（WTI−ブレント、金÷銀）")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　判定の条件: docs/spread.md 4 章（結果を見る前に決めたもの）")
    say(f"決まり: 直近 {WINDOW} 日で OU を推定（{REFIT} 日ごと）。|z| > {Z_IN:.0f} で入り、符号が変わったら手仕舞い、|z| > {Z_STOP:.0f} で損切り。"
        f"半減期 {HL_MIN:.0f}〜{HL_MAX:.0f} 日の時だけ新規。量は年率 {VOL_TARGET:.0%}（名目は資金の {MAX_LEV:.0f} 倍まで）。"
        f"コスト 片足 往復 {COST:.2%}・年 {FINANCING:.0%}。試した数: {PRIOR_TRIALS} ＋ {NEW_TRIALS} ＝ {PRIOR_TRIALS + NEW_TRIALS}")
    say(f"期間A: Yahoo の先物 〜{split - pd.Timedelta(days=1):%Y-%m}（前半・後半は売買できる日を数で半分）。"
        + ("期間B: フィリップの日足" if phillip else "期間B: フィリップのデータが無いので Yahoo の続きを「代わり」に表示（判定は Mac で）"))

    verdicts = {}
    for name, (k1, k2, kind, label) in PAIRS.items():
        say(f"\n## {name}: {label}\n")
        rows = []
        sources = [("A", yahoo, lambda idx: idx < split), ]
        if phillip:
            sources.append(("B", phillip, lambda idx: idx >= split))
        else:
            sources.append(("B'", yahoo, lambda idx: idx >= split))
        detail = {}
        for per, closes, in_period in sources:
            p1, p2 = closes[k1], closes[k2]
            s = spread_series(p1, p2, kind)
            if per == "B" and phillip:                               # フィリップの短い履歴: 推定は Yahoo の履歴で温める
                sy = spread_series(yahoo[k1], yahoo[k2], kind)
                s_fit = pd.concat([sy[sy.index < s.index[0]], s]).sort_index()
                st = ou_state(s_fit).reindex(s.index)
            else:
                st = ou_state(s)
            pos = positions(st)
            res = pair_returns(p1, p2, s, pos.copy(), kind)
            mask = in_period(s.index) & st["z"].notna().to_numpy()
            r = res["ret"][mask]
            if len(r) < 120:
                say(f"- 期間{per}: 売買できる日が足りない（{len(r)} 日）")
                continue
            half = len(r) // 2
            s1, s2, sf = sharpe(r.iloc[:half]), sharpe(r.iloc[half:]), sharpe(r)
            ps = psr_of(r.to_numpy()) if len(r) > 60 else np.nan
            entries = int(((pos != 0) & (np.roll(pos, 1) == 0))[mask].sum())
            in_mkt = float((pos[mask] != 0).mean())
            hl = st["half_life"][mask]
            tradable = float(((hl >= HL_MIN) & (hl <= HL_MAX)).mean())
            pl = placebo_sharpes(p1, p2, s, pos, kind, mask, args.placebo) if args.placebo and per == "A" else np.array([])
            pct_better = float(np.mean(pl < sf)) if len(pl) else np.nan
            detail[per] = {"s1": s1, "s2": s2, "sf": sf, "psr": ps, "pct": pct_better}
            rows.append(f"| 期間{per} {r.index[0]:%Y-%m}〜{r.index[-1]:%Y-%m} | {f2(s1)} | {f2(s2)} | {f2(sf)} | {pct(ps)} | "
                        f"{pct(pct_better)} | {pct(mdd(r), 1)} | {entries} | {pct(in_mkt)} | {pct(tradable)} | "
                        f"{hl.median():.0f} 日 | {res['lev'][mask].max():.2f} |")
            res[mask].assign(z=st["z"][mask], half_life=hl, pos=pos[mask]).to_csv(out / f"{name}_{per.replace(chr(39), 'y')}.csv")
        say("| 期間 | シャープ 前半 | 後半 | 全期間 | PSR | 偶然より良い割合 | 最大DD | 入った回数 | 持っている日の割合 | 半減期が範囲内の日 | 半減期の中央値 | 名目÷資金 最大 |")
        say("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for row in rows:
            say(row)
        a = detail.get("A")
        if a is None:
            verdicts[name] = "判定できない"
        else:
            ok_a = a["s1"] > 0.3 and a["s2"] > 0.3 and a["psr"] >= 0.95 and a["pct"] >= 0.95
            b = detail.get("B")
            if not ok_a:
                verdicts[name] = "効いていない（期間A で条件に届かない）"
            elif b is None:
                verdicts[name] = "期間A は条件を満たす。判定は Mac の期間B で"
            else:
                verdicts[name] = "**効いている**" if b["sf"] > 0 else "効いていない（期間B でマイナス）"
        say(f"\n- 判定: {verdicts[name]}（条件: 期間A で前半・後半ともシャープ 0.3 超、PSR 95% 以上、偶然より良い割合 95% 以上、"
            "かつ期間B でプラス）")

    say("\n## 読み方・限界")
    say("- 差の「落ち着き先」は変わる（WTI−ブレントは 2011〜2014 年の輸送の制約、2020-04-20 の WTI のマイナス価格）。"
        "半減期の範囲で新規を止めているが、持っている間の急変は損切り（|z| > 4）で受ける")
    say("- 両建てなので往復コストは単品の 2 倍。先物のつなぎ足は限月の乗り換えで差が飛ぶ（期間B のフィリップのデータで確かめる）")
    say("- 量は年率 10% の変動にそろえた。名目が資金の 2 倍に当たると、それ以上は増やしていない")
    say("- 判定は差の戻りそのものだけ。効いていれば、今の 4 商品に足したときの成績と、デモでの約定・証拠金の確かめを別に行う")

    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n保存先: {out}/report.md")


if __name__ == "__main__":
    main()
