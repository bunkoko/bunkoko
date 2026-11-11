"""行き過ぎの回避を、使っていない市場で確かめ直す（docs/cross_asset.md 8 章）の試験。作ったデータだけを使う。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdbot import extcheck as E
from cfdbot import partner as P
from cfdbot.study import load_picks
from cfdbot.train.config import load_train_config

ROOT = Path(__file__).resolve().parents[1]
BASE, EXT = ROOT / "config" / "baselines" / "turtle55_d1.json", ROOT / "config" / "baselines" / "turtle55_d1_ext.json"


def _load():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("extcheck_study", ROOT / "scripts" / "extcheck_study.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _returns(seed: int, days: int = 2600, after_ext: float | None = None, after_calm: float = 0.0,
             hold: tuple[int, int] = (20, 40)) -> np.ndarray:
    """作った日ごとの変化。after_ext を渡すと、55 日の高値・安値を抜けた後の動きを仕込む:

    抜けた時に行き過ぎていた（20 日でふだんの 1.5 倍超）なら、その後 hold[0]（20）日は after_ext×σ ずつ抜けた向きに動き（マイナスなら戻る）、
    行き過ぎていなければ hold[1]（40）日は after_calm×σ ずつ動く。
    """
    rng = np.random.default_rng(seed)
    sd = 0.01
    r = rng.normal(0, sd, days)
    if after_ext is None:
        return r
    x, until, drift = np.zeros(days), 0, 0.0
    for t in range(days):
        if t < until:
            r[t] += drift
        x[t] = (x[t - 1] if t else 0.0) + r[t]
        if t < 300 or t < until:
            continue
        side = 1 if x[t] > x[t - 55:t].max() else -1 if x[t] < x[t - 55:t].min() else 0
        if side:
            ext = side * (x[t] - x[t - 20]) / (r[t - 252:t + 1].std() * np.sqrt(20)) > 1.5
            drift, until = side * (after_ext if ext else after_calm) * sd, t + (hold[0] if ext else hold[1])
    return r


def _csv(path: Path, r: np.ndarray) -> None:
    c = 100 * np.exp(np.cumsum(r))
    o = np.r_[100.0, c[:-1]]
    idx = pd.bdate_range("2012-01-02", periods=len(c))
    pd.DataFrame({"date": idx.strftime("%Y-%m-%d"), "open": o, "high": np.maximum(o, c) * 1.002, "low": np.minimum(o, c) * 0.998,
                  "close": c, "volume": 1.0}).to_csv(path, index=False)


def _group(tmp: Path, n: int = 6, **kw) -> P.Group:
    mod = _load()
    tmp.mkdir(parents=True, exist_ok=True)
    for i, k in enumerate(E.MARKETS[:n]):
        _csv(tmp / f"{k}.csv", _returns(i, **kw))
    bt = mod.base_bt(load_train_config(ROOT / "config" / "train.toml"), 10_000_000)
    g, missing = E.load_group(E.MARKETS[:n], tmp, bt, pd.Timestamp("2013-01-01", tz="UTC"), "2000-01-01", fetch=False)
    assert not missing and not g.dropped
    return g


def _measure(g: P.Group, n: int = 20):
    pk0 = load_picks(BASE)[0]
    base = E.run(g, pk0)
    gates = E.own_gates(g.frames)
    cand = E.run(g, pk0, gates)
    rnd, shf, counts = E.placebo_diffs(g, pk0, gates, base, n, log=lambda s: None)
    assert base["trades"] >= counts["random_trades"] > 0 and counts["shift_trades"] > 0      # 偶然の取引の数（読むためだけ）
    diff = cand["sharpe"] - base["sharpe"]
    return base, cand, diff, rnd, shf


def test_markets_are_the_partner_ten_without_the_ones_seen_at_adoption():
    assert len(E.MARKETS) == len(set(E.MARKETS)) == 9 and "PLATINUM" not in E.MARKETS    # プラチナは 6 章の期間 C で見た
    assert set(E.MARKETS) | set(E.SEEN) == {k for pair in P.PAIRS for k in pair}
    assert not set(E.MARKETS) & {"GOLD", "SILVER", "WTI", "BRENT", "COPPER", "NATGAS", "PLATINUM"}   # 期間 A・B・C の市場は入れない
    assert (E.N_DAYS, E.THRESHOLD, E.PLACEBO, E.PASS_PCT, E.PRIOR_TRIALS) == (20, 1.5, 200, 0.95, 91)
    assert E.RESULT["date"] == "2026-11-11" and E.RESULT["verdict"] == "通らなかった"   # 2026-11-11 に 1 回計算した（docs/cross_asset.md 8.8）
    base, ext = load_picks(BASE)[0], load_picks(EXT)[0]
    rest = {k: v for k, v in ext.params.items() if k not in ("max_extension", "extension_period")}
    assert rest == base.params and (ext.params["max_extension"], ext.params["extension_period"]) == (1.5, 20)   # 違いは行き過ぎの回避だけ


def test_gate_without_jump_cleaning_is_exactly_the_ea_filter(tmp_path):
    g = _group(tmp_path, n=3)
    by_gate = E.run(g, load_picks(BASE)[0], E.own_gates(g.frames, clean=False))
    by_config = E.run(g, load_picks(EXT)[0])
    assert by_gate["trades"] == by_config["trades"] and by_gate["sharpe"] == pytest.approx(by_config["sharpe"], abs=1e-12)
    assert by_gate["trades"] < E.run(g, load_picks(BASE)[0])["trades"]                   # 止めた分だけ取引が減る


def test_gate_blocks_only_the_direction_already_run_and_ignores_a_single_jump():
    idx = pd.date_range("2015-01-01", periods=600, freq="B", tz="UTC")
    rng = np.random.default_rng(0)
    r = rng.normal(0, 0.01, 600)
    r[-20:] = 0.03                                                                       # 最後の 20 日に大きく上げた
    g = E.own_gates({"A": pd.DataFrame({"close": 100 * np.exp(np.cumsum(r))}, index=idx)})["A"]
    assert not g["long"].iloc[-1] and g["short"].iloc[-1] and g["long"].iloc[300:560].mean() > 0.85
    r = rng.normal(0, 0.01, 600)
    r[-5] = 0.15                                                                         # 1 日だけの飛び（つなぎ足の乗り換え）
    f = {"A": pd.DataFrame({"close": 100 * np.exp(np.cumsum(r))}, index=idx)}
    assert not E.own_gates(f, clean=False)["A"]["long"].iloc[-3] and E.own_gates(f)["A"]["long"].iloc[-3]   # 飛びだけで止めるのは EA の式の方


def test_random_block_matches_count_side_and_length_of_the_real_filter(tmp_path):
    g = _group(tmp_path, n=3)
    base = E.run(g, load_picks(BASE)[0])
    gates = E.own_gates(g.frames)
    spans = E.trade_spans(base["table"], g.frames)
    hits = E.blocked(spans, gates)
    assert sum(len(v) for v in spans.values()) == base["trades"] and sum(len(v) for v in hits.values()) > 5
    for (sym, col), items in spans.items():
        ok = gates[sym][col].to_numpy(bool)
        assert len(hits[(sym, col)]) == sum(1 for p, _ in items if not ok[p])            # 合図の足で止まっていた取引の数
    null = E.random_block_gates(g.frames, spans, hits, np.random.default_rng(1))
    other = E.random_block_gates(g.frames, spans, hits, np.random.default_rng(2))
    assert any(not null[s].equals(other[s]) for s in g.frames)                           # 選び方はでたらめ
    for (sym, col), items in spans.items():
        ok = null[sym][col].to_numpy(bool)
        starts = [p for p, _ in items if not ok[p]]
        assert len(starts) == len(hits[(sym, col)])                                      # 市場ごと・向きごとに同じ数を止める
        full = sum(1 for p, e in items if not ok[p:e].any())
        assert full >= sum(1 for d in hits[(sym, col)] if d < 0)                         # 丸ごと見送った数も保つ
        assert (~ok).sum() <= sum(e - p for p, e in items)                               # 基準の取引の外は止めない
    assert E.random_block_gates(g.frames, spans, {}, np.random.default_rng(1))["PALLADIUM"].all().all()   # 止めた取引が無ければ何も止めない


def test_planted_effect_is_found(tmp_path):
    g = _group(tmp_path, after_ext=-0.5, after_calm=0.3)       # 行き過ぎて抜けたら戻り、そうでなければ続く
    base, cand, diff, rnd, shf = _measure(g)
    assert diff > 0 and E.better_share(rnd, diff) >= 0.95 and E.better_share(shf, diff) >= 0.95
    v = E.verdict(diff, E.better_share(rnd, diff), E.better_share(shf, diff), P.improved_share(base, cand), len(g.frames))
    assert v.startswith("通った")


def test_no_effect_is_not_found(tmp_path):
    g = _group(tmp_path)                                       # ただのでたらめな値動き
    base, cand, diff, rnd, shf = _measure(g)
    v = E.verdict(diff, E.better_share(rnd, diff), E.better_share(shf, diff), P.improved_share(base, cand), len(g.frames))
    assert v.startswith("通らなかった")


def test_random_block_removes_the_gain_from_merely_trading_less_in_losing_markets(tmp_path):
    g = _group(tmp_path, after_ext=-0.5, after_calm=-0.5, hold=(6, 6))   # 抜けたら必ず戻る（どの取引も負ける。行き過ぎかどうかは関係ない）
    base, _, diff, rnd, shf = _measure(g)
    assert base["sharpe"] < 0 and diff > 0                     # 新規を止めるだけで、基準より良く見える
    assert E.better_share(shf, diff) >= 0.95                   # 時期をずらすだけの比べ（7 章の物差し）だと「効いた」に見えてしまう（止まる数が減るため）
    assert rnd.mean() > shf.mean() and rnd.mean() > 0          # 同じ数をでたらめに止めても、同じくらい良くなる
    assert E.better_share(rnd, diff) < 0.95                    # だから主の比べでは通らない
    assert E.verdict(diff, E.better_share(rnd, diff), E.better_share(shf, diff), 1.0, len(g.frames)).startswith("通らなかった")


def test_verdict_needs_all_four_conditions():
    assert E.verdict(0.05, 0.96, 0.97, 0.6, 9).startswith("通った")
    assert E.verdict(0.05, 0.94, 1.00, 0.6, 9).startswith("通らなかった")                # 主（でたらめに止める）が届かない
    assert E.verdict(0.05, 1.00, 0.94, 0.6, 9).startswith("通らなかった")                # 副（時期をずらす）が届かない
    assert E.verdict(0.05, 0.99, 0.99, 0.4, 9).startswith("通らなかった")
    assert E.verdict(-0.02, 1.0, 1.0, 1.0, 9).startswith("通らなかった")                 # 逆の結果を反転して「効く」とはしない
    assert E.verdict(0.05, 0.99, 0.99, 0.9, 5).startswith("判定できない（判定に残った市場が 5")
    assert E.verdict(float("nan"), None, None, None).startswith("判定できない")


def _universe(tmp_path, keys=None, short=()) -> Path:
    uni = tmp_path / "universe"
    uni.mkdir(exist_ok=True)
    for i, k in enumerate(E.MARKETS + E.SEEN):
        if (keys is None or k in keys) and not (uni / f"{k}.csv").exists():
            _csv(uni / f"{k}.csv", _returns(40 + i, days=500 if k in short else 1800))
    return uni


def _argv(tmp_path, out, placebo="3", **kw):
    uni = _universe(tmp_path, **kw)
    cfg = tmp_path / "train.toml"
    cfg.write_text(f'output_dir = "{tmp_path / "out"}"\n[data]\ndir = "{tmp_path / "data"}"\n', encoding="utf-8")
    return ["extcheck_study.py", "--config", str(cfg), "--data", str(uni), "--start", "2013-01-01", "--placebo", placebo, "--no-fetch",
            "--out", str(out)]


def _mod(tmp_path, monkeypatch):
    """道具を読み込み、1 回だけの止めの場所を試験用にする（Mac で本物を計算した後も、試験が通るように）。"""
    mod = _load()
    monkeypatch.setattr(mod, "DONE_DIR", tmp_path / "done")
    monkeypatch.setattr(E, "RESULT", {})
    return mod


def _no_yahoo(*a, **k):
    raise AssertionError("試験で Yahoo に問い合わせた")


def test_study_runs_end_to_end_and_only_once(tmp_path, monkeypatch, capsys):
    mod = _mod(tmp_path, monkeypatch)
    out = tmp_path / "extcheck"
    monkeypatch.setattr(sys, "argv", _argv(tmp_path, out))
    mod.main()
    md = next(out.glob("*/report.md")).read_text(encoding="utf-8")
    assert md.strip() in capsys.readouterr().out                                         # 画面に出るのは、保存した報告書と同じもの
    assert "## 1. 判定（使っていない市場だけ）" in md and "| 主: 飛びを除く | 9 |" in md
    head = md.split("## 1.")[1].split("判定の条件")[0]
    assert "試し（実物のデータではない。事前登録と違う設定:" in head and "--start" in head and "--placebo" in head and "判定ではない" in head
    assert "通った" not in head and "通らなかった" not in head                           # 作ったデータ・既定と違う設定は判定にしない
    assert "期間: 2013-01-01〜" in md and "（データの最後の日）" in md and "作ったデータ（試し）" in md
    assert "でたらめに止めた偶然の平均" in md and "違えば、判定の前に止まる" in md
    assert "試した数: 91 ＋ 1 ＝ 92" in md and "基準 2.501" in md and "プラチナ" in md and "8.5" in md and "決めるのは利用者" in md
    with pytest.raises(SystemExit, match="動かし直さない"):                              # 報告書があれば止まる
        mod.main()


def test_stop_is_seen_at_the_fixed_place_whatever_out_is(tmp_path, monkeypatch):
    mod = _mod(tmp_path, monkeypatch)
    assert _load().DONE_DIR == ROOT / "output" / "extcheck" and _load().REAL_DATA == ROOT / "data" / "universe"   # 今いる場所に関係ない
    (mod.DONE_DIR / "20261112-0900").mkdir(parents=True)
    (mod.DONE_DIR / "20261112-0900" / "report.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", _argv(tmp_path, tmp_path / "elsewhere"))            # --out を変えても
    monkeypatch.chdir(tmp_path)                                                          # 別の場所から動かしても
    with pytest.raises(SystemExit, match="動かし直さない"):
        mod.main()
    assert not (tmp_path / "elsewhere").exists()


def test_study_refuses_when_result_is_recorded(tmp_path, monkeypatch):
    mod = _mod(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", _argv(tmp_path, tmp_path / "x"))
    monkeypatch.setattr(E, "RESULT", {"date": "2026-11-12", "verdict": "通らなかった"})
    with pytest.raises(SystemExit, match="2026-11-12"):
        mod.main()
    assert not (tmp_path / "x").exists()


def test_load_group_separates_missing_from_short(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "fetch_yahoo", _no_yahoo)
    uni = _universe(tmp_path, keys=E.MARKETS[:7], short=E.MARKETS[:2])
    (uni / f"{E.MARKETS[8]}.csv").write_text("date,open,high,low,close,volume\n", encoding="utf-8")   # 中身の無いファイルも「取れなかった」
    bt = _load().base_bt(load_train_config(ROOT / "config" / "train.toml"), 10_000_000)
    g, missing = E.load_group(E.MARKETS, uni, bt, pd.Timestamp("2013-01-01", tz="UTC"), "2000-01-01", fetch=False, log=lambda s: None)
    assert len(g.frames) == 5 and len(g.dropped) == 2 and len(missing) == 2
    assert set(missing) == {P.MARKETS[k].label for k in E.MARKETS[7:]} and not set(missing) & set(g.dropped)


@pytest.mark.parametrize("kw, msg", [
    ({"keys": E.MARKETS[:5]}, "取れなかった市場がある"),                                 # 5 市場しか無い
    ({"keys": E.MARKETS[:8]}, "取れなかった市場がある"),                                 # 8 市場あっても、1 つ欠けたら止まる（外して判定しない）
    ({"keys": E.MARKETS, "short": E.MARKETS[:4]}, "6 未満"),                             # 全部取れたが、3 年に満たない市場を外すと 5
])
def test_nothing_is_computed_or_written_when_markets_are_missing_or_too_few(tmp_path, monkeypatch, capsys, kw, msg):
    mod = _mod(tmp_path, monkeypatch)
    monkeypatch.setattr(E, "fetch_yahoo", _no_yahoo)                                     # 作ったデータの置き場では Yahoo から取らない
    monkeypatch.setattr(E, "run", lambda *a, **k: pytest.fail("市場が足りないのに計算した"))
    out = tmp_path / "extcheck"
    argv = [a for a in _argv(tmp_path, out, **kw) if a != "--no-fetch"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit, match=msg) as e:
        mod.main()
    assert "何も計算していない" in str(e.value) and "シャープ" not in capsys.readouterr().out
    assert not out.exists() and not mod.DONE_DIR.exists()                                # 報告書も止めの印も無い


def test_three_short_markets_are_dropped_and_reported(tmp_path, monkeypatch):
    mod = _mod(tmp_path, monkeypatch)
    out = tmp_path / "extcheck"
    monkeypatch.setattr(sys, "argv", _argv(tmp_path, out, short=E.MARKETS[:3]))
    mod.main()
    md = next(out.glob("*/report.md")).read_text(encoding="utf-8")
    assert "| 主: 飛びを除く | 6 |" in md and "取れたが 3 年に満たず外した市場: " in md


def _real(tmp_path, monkeypatch, mod, placebo=3):
    """tmp の置き場を「実物のデータ」に見立てる（既定の引数も tmp に向ける）。"""
    argv = _argv(tmp_path, mod.DONE_DIR)
    monkeypatch.setattr(mod, "REAL_DATA", tmp_path / "universe")
    for k, v in {"config": argv[2], "out": str(mod.DONE_DIR), "start": "2013-01-01", "placebo": placebo}.items():
        monkeypatch.setitem(mod.FIXED, k, v)
    return ["extcheck_study.py", "--data", str(tmp_path / "universe")]


def test_real_data_refuses_any_argument_that_changes_the_verdict(tmp_path, monkeypatch):
    mod = _mod(tmp_path, monkeypatch)
    monkeypatch.setattr(E, "run", lambda *a, **k: pytest.fail("断るはずなのに計算した"))
    assert mod.FIXED["placebo"] == 200 and mod.FIXED["start"] == "2001-01-01" and mod.FIXED["equity"] == 10_000_000
    argv = _real(tmp_path, monkeypatch, mod)
    for extra in (["--placebo", "2"], ["--placebo", "250"], ["--start", "2010-01-01"], ["--since", "2005-01-01"], ["--equity", "5000000"],
                  ["--out", str(tmp_path / "other")], ["--base", str(EXT)], ["--ext", str(BASE)], ["--config", str(ROOT / "config" / "train.toml")]):
        monkeypatch.setattr(sys, "argv", argv + extra)
        with pytest.raises(SystemExit, match=f"変えられない.*{extra[0]}"):
            mod.main()
    assert not mod.DONE_DIR.exists() and not (tmp_path / "other").exists()


def test_real_data_with_default_arguments_gives_a_verdict_once(tmp_path, monkeypatch):
    mod = _mod(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", _real(tmp_path, monkeypatch, mod) + ["--no-fetch"])
    mod.main()
    md = next(mod.DONE_DIR.glob("*/report.md")).read_text(encoding="utf-8")
    head = md.split("## 1.")[1].split("判定の条件")[0]
    assert "試し" not in head and ("**通った" in head or "**通らなかった" in head) and "data/universe（実物）" in md
    with pytest.raises(SystemExit, match="動かし直さない"):
        mod.main()


def test_construction_mismatch_stops_before_the_verdict(tmp_path, monkeypatch, capsys):
    mod = _mod(tmp_path, monkeypatch)
    assert E.config_matches({"trades": 5, "sharpe": 0.3}, {"trades": 5, "sharpe": 0.3 + 1e-12})
    assert not E.config_matches({"trades": 5, "sharpe": 0.3}, {"trades": 6, "sharpe": 0.3})
    assert not E.config_matches({"trades": 5, "sharpe": 0.3}, {"trades": 5, "sharpe": 0.31})
    assert not E.config_matches({"trades": 5, "sharpe": float("nan")}, {"trades": 5, "sharpe": 0.3})
    monkeypatch.setattr(E, "config_matches", lambda a, b: False)
    monkeypatch.setattr(E, "placebo_diffs", lambda *a, **k: pytest.fail("作りが合わないのに偶然を計算した"))
    out = tmp_path / "extcheck"
    monkeypatch.setattr(sys, "argv", _argv(tmp_path, out))
    with pytest.raises(SystemExit, match="作りの確かめが合わない") as e:
        mod.main()
    assert "シャープ" not in str(e.value) and "シャープ" not in capsys.readouterr().out and not out.exists()


def test_verdict_fails_when_no_market_qualifies_for_condition_four():
    assert E.verdict(0.05, 1.0, 1.0, None, 9).startswith("通らなかった")                 # 両方で 5 回以上取引した市場が無い（8.3）
