"""EA の「行き過ぎの回避」（自分の値動き）を、使っていない市場で 1 回だけ確かめ直す。仮説と判定の条件は docs/cross_asset.md 8 章。

    ./cfd extcheck              # data/universe の日足（無い市場は Yahoo から取る。Mac で。10〜30 分の見込み）。引数は付けない

基準 = 行き過ぎの回避の無いタートル 55/20 日足（turtle55_d1.json）。足した版 = 基準＋行き過ぎの回避（1.5 倍・20 日。動かさない）。
判定は、相方の確かめ（7 章）の 10 市場からプラチナ（採用のときに見た）を除いた 9 市場だけ。偶然は 2 種類・200 回ずつ:
同じ数の新規をでたらめに止める（主）、止める時期をずらす（副）。**1 回だけ計算する**（報告書か RESULT があれば止まる）。

2026-11-11 点検の指摘で直した: 取れなかった市場がある・市場が 6 未満なら、何も計算せずに止まる（報告書も書かない）。
止めは --out や今いる場所に関係なく、リポジトリの output/extcheck を見る。実物のデータ（data/universe）では、判定に効く引数
（--placebo・--start・--since・--equity・--base・--ext・--config・--out）を既定から変えると断る。ほかの置き場（作ったデータ）は
いつも「試し」で、Yahoo から取らない。
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
sys.path.insert(0, str(ROOT / "scripts"))

from partner_study import base_bt, f2, halves, pct

from cfdbot import extcheck as E
from cfdbot import partner as P
from cfdbot.stats import expected_max_z, psr_of
from cfdbot.study import clean, load_picks
from cfdbot.train.config import load_train_config

# どれも今いる場所に関係なく、リポジトリの決まった場所（2026-11-11 点検の指摘で直した）
REAL_DATA = ROOT / "data" / "universe"          # 実物のデータ。ここを読むときだけ判定になる
DONE_DIR = ROOT / "output" / "extcheck"         # 1 回だけの止めは、--out が何でも必ずここを見る
DEFAULT_OUT = str(DONE_DIR)
# 判定に効く引数の既定（8.2）。実物のデータでは、これと違えば断る
FIXED = {"base": str(ROOT / "config" / "baselines" / "turtle55_d1.json"), "ext": str(ROOT / "config" / "baselines" / "turtle55_d1_ext.json"),
         "config": str(ROOT / "config" / "train.toml"), "out": DEFAULT_OUT, "since": "2000-01-01", "start": "2001-01-01",
         "equity": 10_000_000.0, "placebo": E.PLACEBO}
PATHS = ("base", "ext", "config", "out")


def changed_args(args) -> list[str]:
    """既定（8.2 の事前登録）と違う、判定に効く引数の名前。"""
    out = []
    for k, v in FIXED.items():
        got = getattr(args, k)
        same = Path(got).resolve() == Path(v).resolve() if k in PATHS else got == v
        if not same:
            out.append(f"--{k}")
    return out


def study(group: P.Group, pk0, n_placebo: int, clean: bool = True, base: dict | None = None, log=print) -> dict:
    """基準・足した版・偶然 2 種類を計算する。clean=True が主の判定（飛びの日を除く）。"""
    if base is None:
        log(f"計算: {group.label}（基準: 行き過ぎの回避なし）")
        base = E.run(group, pk0)
    gates = E.own_gates(group.frames, clean=clean)
    log(f"計算: {group.label}（行き過ぎの回避を足す{'' if clean else '・飛びを除かない'}）")
    cand = E.run(group, pk0, gates)
    diff = cand["sharpe"] - base["sharpe"]
    rnd, shf, counts = (E.placebo_diffs(group, pk0, gates, base, n_placebo, log=log) if n_placebo
                        else (np.array([]), np.array([]), {"random_trades": float("nan"), "shift_trades": float("nan")}))
    d = (cand["returns"] - base["returns"]).dropna()
    hits = E.blocked(E.trade_spans(base["table"], group.frames), gates)
    stopped = {s: (sum(len(v) for (k, _), v in hits.items() if k == s), sum(sum(1 for x in v if x < 0) for (k, _), v in hits.items() if k == s))
               for s in group.frames}
    t = float(d.mean() / d.std(ddof=1) * np.sqrt(len(d))) if len(d) > 60 and d.std(ddof=1) > 0 else float("nan")
    return {"base": base, "cand": cand, "diff": diff, "pct_random": E.better_share(rnd, diff), "pct_shift": E.better_share(shf, diff),
            "share": P.improved_share(base, cand), "psr": psr_of(d.to_numpy()) if len(d) > 60 else float("nan"), "t": t,
            "stopped": stopped, "n": len(rnd), "counts": counts}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=FIXED["base"], help="基準の構成（行き過ぎの回避なし）")
    p.add_argument("--ext", default=FIXED["ext"], help="今の EA の構成（作りの確かめに使う）")
    p.add_argument("--config", default=FIXED["config"])
    p.add_argument("--data", default=str(REAL_DATA), help="先物の日足の置き場（./cfd universe と同じ）。ほかの場所は作ったデータの試験だけ（判定にしない・Yahoo から取らない）")
    p.add_argument("--since", default=FIXED["since"])
    p.add_argument("--start", default=FIXED["start"], help="判定の期間の始まり")
    p.add_argument("--equity", type=float, default=FIXED["equity"])
    p.add_argument("--placebo", type=int, default=FIXED["placebo"], help=f"偶然の回数（判定は {E.PLACEBO} に固定。変えるのは作ったデータの試験だけ）")
    p.add_argument("--no-fetch", action="store_true", help="無い市場を Yahoo から取らない")
    p.add_argument("--out", default=FIXED["out"])
    args = p.parse_args()

    # 1 回だけの止め。--out・今いる場所に関係なく、リポジトリの決まった場所を必ず見る
    done = sorted({*DONE_DIR.glob("*/report.md"), *Path(args.out).glob("*/report.md")})
    if E.RESULT or done:
        when = E.RESULT.get("date") if E.RESULT else str(done[-1])
        sys.exit(f"判定はもう 1 回計算してある（{when}。docs/cross_asset.md 8.8）。1 回だけの決まり（8.7）なので、動かし直さない")
    real = Path(args.data).resolve() == REAL_DATA.resolve()
    changed = changed_args(args)
    if real and changed:
        sys.exit(f"実物のデータ（{REAL_DATA}）では、判定に効く引数を既定から変えられない（変わっている: {', '.join(changed)}）。"
                 "偶然の回数・期間・資金・構成・保存先は事前登録の通りに固定（docs/cross_asset.md 8.2・8.7）。何も付けずに動かす")

    cfg = load_train_config(args.config)
    pk0 = load_picks(args.base)[0]
    pk_ext = load_picks(args.ext)[0]
    if (pk_ext.params.get("max_extension"), pk_ext.params.get("extension_period")) != (E.THRESHOLD, E.N_DAYS) or pk0.params.get("max_extension"):
        sys.exit("構成が 8.2 と合わない（基準は行き過ぎの回避なし、今の EA は 1.5 倍・20 日のはず）")
    bt = base_bt(cfg, args.equity)
    start = pd.Timestamp(clean(args.start), tz="UTC")
    root = Path(args.data)
    fetch = real and not args.no_fetch                     # 実物の置き場でだけ Yahoo から取る（ほかの場所に実物を作らない）
    judged, missing = E.load_group(E.MARKETS, root, bt, start, args.since, fetch=fetch, label=f"判定: 使っていない市場 {start:%Y-%m}〜")
    n_markets = len(judged.frames)
    # 市場が欠けたら、何も計算せずに止まる（報告書も止めの印も書かない。試した数にも入れない。8.2・8.3）
    if missing:
        sys.exit(f"取れなかった市場がある: {', '.join(missing)}。何も計算していない（報告書も書いていない）。"
                 "事前登録の 9 市場がそろわないまま判定しない（docs/cross_asset.md 8.2）。時間をおいて、同じコマンドでもう一度（取れた市場は取り直さない）")
    if n_markets < E.MIN_MARKETS:
        sys.exit(f"判定に使える市場が {n_markets}（{E.MIN_MARKETS} 未満）。3 年に満たず外した市場: {', '.join(judged.dropped) or 'なし'}。"
                 "何も計算していない（報告書も書いていない。docs/cross_asset.md 8.3）")
    seen, _ = E.load_group(E.SEEN, root, bt, start, args.since, fetch=fetch, label="参考: プラチナ（採用のときに見た市場）")

    # 作りの確かめ（8.6）を先にする: 合わなければ、判定の数字を出す前に止まる
    print(f"計算: {judged.label}（基準: 行き過ぎの回避なし）", flush=True)
    base = E.run(judged, pk0)
    as_cfg = E.run(judged, pk_ext)                                                      # 構成ファイルの「有り」をそのまま当てる
    by_gate = E.run(judged, pk0, E.own_gates(judged.frames, clean=False))               # 飛びを除かない絞り込みで足す（同じはず）
    if not E.config_matches(by_gate, as_cfg):
        sys.exit(f"作りの確かめが合わない: 飛びを除かない絞り込みで足した版（取引 {by_gate['trades']}）と、構成ファイルの「有り」をそのまま当てた版"
                 f"（取引 {as_cfg['trades']}）が同じにならない。判定は計算していない（報告書も書いていない）。道具を直してからもう一度（docs/cross_asset.md 8.6）")

    res = study(judged, pk0, args.placebo, base=base)
    raw = study(judged, pk0, min(args.placebo, 30), clean=False, base=base)             # 参考: 飛びを除かない（EA と同じ式）
    ref = (E.run(seen, pk0), E.run(seen, pk0, E.own_gates(seen.frames))) if seen.frames else None
    jumps = P.jump_share(judged.frames)
    v = E.verdict(res["diff"], res["pct_random"], res["pct_shift"], res["share"], n_markets)
    trial = not real                                       # 実物のデータ・既定の設定のときだけ判定（ここまで来た実物は既定のまま）
    if trial:
        why = ["実物のデータではない"] + ([f"事前登録と違う設定: {', '.join(changed)}"] if changed else [])
        v = f"試し（{'。'.join(why)}。判定ではない）"

    last = max(f.index.max() for f in judged.frames.values())
    lines: list[str] = []
    say = lines.append                                     # 画面に出すのは、報告書を保存した後（保存で落ちて、見たのに止めがかからない、を避ける）

    trials = E.PRIOR_TRIALS + 1
    say("# 行き過ぎの回避（自分の値動き）を、使っていない市場で確かめ直す")
    say(f"作成: {datetime.now():%Y-%m-%d %H:%M}　仮説と判定の条件: docs/cross_asset.md 8 章（結果を見る前に決めたもの。1 回だけ計算する）")
    say(f"基準: {Path(args.base).name}（行き過ぎの回避なし）。足した版: 基準＋行き過ぎの回避（{E.THRESHOLD} 倍・{E.N_DAYS} 日。動かさない）。"
        f"資金 {args.equity:,.0f} 円・1 回の損失 {bt.risk.risk_per_trade:.1%}。コスト: 往復 0.08%・建玉に年 3%。"
        f"試した数: {E.PRIOR_TRIALS} ＋ 1 ＝ {trials}")
    say(f"期間: {start:%Y-%m-%d}〜{last:%Y-%m-%d}（データの最後の日）。データ: {'data/universe（実物）' if real else '作ったデータ（試し）'}。"
        f"偶然の回数: {args.placebo}")
    say("\n## 1. 判定（使っていない市場だけ）\n")
    say("| 作り方 | 市場 | 基準のシャープ | 足したシャープ | 差 | でたらめに止めた偶然より良い割合（主） | 時期をずらした偶然より良い割合（副） | "
        "1 回あたりの R が良くなった市場 | 判定 |")
    say("|---|---|---|---|---|---|---|---|---|")
    say(f"| 主: 飛びを除く | {n_markets} | {res['base']['sharpe']:.2f} | {res['cand']['sharpe']:.2f} | {f2(res['diff'])} | "
        f"{pct(res['pct_random'])}（{res['n']} 回） | {pct(res['pct_shift'])}（{res['n']} 回） | {pct(res['share'])} | **{v}** |")
    say(f"| 参考: 飛びを除かない（EA と同じ式） | {n_markets} | {raw['base']['sharpe']:.2f} | {raw['cand']['sharpe']:.2f} | {f2(raw['diff'])} | "
        f"{pct(raw['pct_random'])}（{raw['n']} 回） | {pct(raw['pct_shift'])}（{raw['n']} 回） | {pct(raw['share'])} | 判定しない |")
    if judged.dropped:
        say(f"\n- 取れたが 3 年に満たず外した市場: {', '.join(judged.dropped)}（取れなかった市場は無い。あれば計算せずに止まる）")
    say(f"\n判定の条件（8.3）: (1) 差がプラス、(2) 同じ数の新規をでたらめに止めた偶然 {E.PLACEBO} 回より良い割合 {E.PASS_PCT:.0%} 以上、"
        f"(3) 止める時期だけをずらした偶然 {E.PLACEBO} 回より良い割合 {E.PASS_PCT:.0%} 以上、(4) 1 回あたりの R が良くなった市場が半分以上。"
        f"4 つとも満たせば「通った」。判定に残る市場が {E.MIN_MARKETS} 未満なら計算しない。(4) に当てはまる市場が無ければ「通らなかった」。"
        "「惜しい」も「通らなかった」と同じ扱い。"
        "**逆の結果が出ても、向きを反転して「効く」としない**")
    say("- 通らなかったとき: デモの間は EA を変えない。本番に進む前の区切りで、利用者が「外す・残す・外して始めデモで記録を続ける」から選ぶ（8.5。チームの案は「外す」。決めるのは利用者）")
    if not trial and np.isfinite(res["diff"]) and res["diff"] < 0 and (res["pct_random"] or 0.0) <= 0.05:
        say("- **はっきり逆の結果**（差がマイナスで、でたらめに止めた偶然より良い割合が 5% 以下）。区切りを待たずに利用者に伝える（8.5）")

    say("\n## 2. 市場ごと（判定の市場。主の作り方）\n")
    say("| 市場 | 止めた取引（うち丸ごと見送り） | 飛びとした日の割合 | 基準 取引・1 回の R | 足した 取引・1 回の R |")
    say("|---|---|---|---|---|")
    for s in judged.frames:
        n0, r0 = res["base"]["by_symbol"][s]
        n1, r1 = res["cand"]["by_symbol"][s]
        k, full = res["stopped"][s]
        say(f"| {P.MARKETS[s].label} | {k}（{full}） | {pct(jumps.get(s), 2)} | {n0}・{f2(r0)} | {n1}・{f2(r1)} |")
    say("\n- 「止めた取引」は、基準の取引のうち、合図の足で行き過ぎの回避が止めていたもの。でたらめに止める偶然は、市場ごと・向きごとにこの数と止めた長さを合わせる")

    say("\n## 3. 参考（判定しない）\n")
    h0, h1 = halves(res["base"]["returns"]), halves(res["cand"]["returns"])
    say(f"- 前半・後半のシャープ: 基準 {h0[0]:.2f}・{h0[1]:.2f}、足した {h1[0]:.2f}・{h1[1]:.2f}。取引 {res['base']['trades']} → {res['cand']['trades']}")
    say(f"- 日ごとの差の PSR {pct(res['psr'])}、t 値 {f2(res['t'])}（試した数 {trials} 個の基準 {expected_max_z(trials):.3f}。この検証は偶然との比べで判定する）")
    c = res["counts"]
    say(f"- 取引の数: 基準 {res['base']['trades']}、足した版 {res['cand']['trades']}、でたらめに止めた偶然の平均 {c['random_trades']:.0f}、"
        f"時期をずらした偶然の平均 {c['shift_trades']:.0f}。偶然の方が多ければ、偶然は本物ほど取引を減らしていない（比べが少し甘い。8.2）")
    say(f"- 作りの確かめ: 構成ファイルの「有り」（{Path(args.ext).name}）をそのまま当てたシャープ {as_cfg['sharpe']:.2f}・取引 {as_cfg['trades']}。"
        f"飛びを除かない絞り込みで足した版（{by_gate['sharpe']:.2f}・{by_gate['trades']}）と同じ（違えば、判定の前に止まる）")
    if ref is None:
        say("- プラチナ（6 章の期間 C で見た市場）: 日足が無い")
    else:
        say(f"- プラチナ（6 章の期間 C で見た市場。判定に入れない）: 基準 {ref[0]['sharpe']:.2f} → 足した {ref[1]['sharpe']:.2f}"
            f"（取引 {ref[0]['trades']} → {ref[1]['trades']}）")

    say("\n## 4. 読み方・限界\n")
    say("- この市場は、まったく見ていない市場ではない。相方の確かめ（7.6）で、行き過ぎの回避「有り」の成績は見ている。見ていなかったのは「有り」と「無し」の差")
    say("- この市場は、タートルそのものが効きにくい市場（7.6）。ここで通らなくても、金・銀・原油で効かないことを示すものではない。"
        "通っても、しきい値の調整やほかの市場への広げはしない")
    say("- Yahoo の先物はつなぎ足（限月の乗り換えの飛びが損益に入る）。コストは目安")
    say("- 次にすること: docs/cross_asset.md 8.8 に記録し、cfdbot/extcheck.py の RESULT に日付と判定を書く。**動かし直さない**")
    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")     # 先に保存（これが 1 回だけの止めの印になる）
    print("\n".join(lines), flush=True)
    pd.DataFrame({"base": res["base"]["returns"], "cand": res["cand"]["returns"]}).to_csv(out / "returns.csv")
    print(f"\n保存先: {out}/report.md")


if __name__ == "__main__":
    main()
