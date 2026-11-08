"""M2r（その株だけの動きで学ぶ M2。docs/agents.md 15 章、./cfd agents residual）の試験。作ったデータだけを使う。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

from cfdbot import stocks_short as H  # noqa: E402
from cfdbot.agents import residual as R  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAST = {"max_iter": 30, "learning_rate": 0.1, "max_leaf_nodes": 15, "min_samples_leaf": 200, "early_stopping": False, "random_state": 0}


def _script():
    spec = importlib.util.spec_from_file_location("agents_residual", ROOT / "scripts" / "agents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _market(n_stocks=60, n_groups=6, start="2015-01-05", end="2019-12-27", seed=0, reversal=0.0, sector_ar=0.0, sector_vol=1.0):
    """寄りと引け・業種・本当のベータ。値動き = ベータ × 市場 + 業種 + その株だけ。reversal > 0 なら、その株だけの動きが次の日に戻る。"""
    idx = pd.bdate_range(start, end)
    rng = np.random.default_rng(seed)
    cols = [f"{1000 + k}.T" for k in range(n_stocks)]
    beta = rng.uniform(0.4, 1.8, n_stocks)
    group = np.arange(n_stocks) % n_groups
    sector = pd.Series([f"業種{g}" for g in group], index=cols)
    n = len(idx)
    mk_n, mk_d = rng.normal(0.0003, 0.005, n), rng.normal(0.0, 0.009, n)
    s_n, s_d = np.zeros((n, n_groups)), np.zeros((n, n_groups))
    for t in range(n):
        s_n[t] = rng.normal(0, 0.003 * sector_vol, n_groups) + (sector_ar * s_n[t - 1] if t else 0)
        s_d[t] = rng.normal(0, 0.006 * sector_vol, n_groups) + (sector_ar * s_d[t - 1] if t else 0)
    open_, close = np.empty((n, n_stocks)), np.empty((n, n_stocks))
    prev, idio_prev = np.full(n_stocks, 1000.0), np.zeros(n_stocks)
    for t in range(n):
        e_n = rng.normal(0, 0.006, n_stocks)
        e_d = rng.normal(0, 0.010, n_stocks) - reversal * idio_prev
        night = beta * mk_n[t] + s_n[t, group] + e_n
        day = beta * mk_d[t] + s_d[t, group] + e_d
        open_[t] = prev * (1 + night)
        close[t] = open_[t] * (1 + day)
        prev, idio_prev = close[t], e_n + e_d
    return pd.DataFrame(open_, idx, cols), pd.DataFrame(close, idx, cols), sector, pd.Series(beta, index=cols)


def test_betas_are_found_from_the_past_only():
    open_, close, sector, beta = _market(seed=1)
    cc = H.parts(open_, close)["cc"]
    b = R.betas(cc)
    est = b.iloc[-1]
    rel = beta / beta.mean()                                                          # 市場 = 全銘柄の平均なので、平均のベータで割った値になる
    assert np.corrcoef(est, beta)[0, 1] > 0.95 and (est - rel).abs().median() < 0.1
    assert (b.iloc[:R.BETA_MIN] == 1.0).all().all()                                  # たまるまでは 1
    t = 400
    bent = cc.copy()
    bent.iloc[t] *= 5.0
    b2 = R.betas(bent)
    assert np.allclose(b.iloc[t], b2.iloc[t]) and not np.allclose(b.iloc[t + 1], b2.iloc[t + 1])   # t 日のベータは t−1 日まで


def test_residual_takes_out_the_market_and_the_industry():
    open_, close, sector, beta = _market(seed=2)
    cc = H.parts(open_, close)["cc"]
    b = R.betas(cc)
    g = R.groups(close.columns, sector)
    e = R.residual(cc, b, g).iloc[300:]
    m = cc.mean(axis=1).iloc[300:]
    corr = e.apply(lambda col: np.corrcoef(col, m)[0, 1])
    raw = cc.iloc[300:].apply(lambda col: np.corrcoef(col, m)[0, 1])
    assert raw.mean() > 0.4 and corr.abs().mean() < 0.1                              # 市場との関係が消える
    per_group = e.T.groupby(g).mean().T
    assert per_group.abs().max().max() < 1e-12                                        # どの日も業種の平均は 0
    small = sector.copy()
    small.iloc[:3] = "小さい業種"                                                      # 3 社だけの業種 → 全銘柄の平均を引く
    g2 = R.groups(close.columns, small)
    g2_idx = np.flatnonzero(g2 == "小さい業種")
    e2 = R.residual(cc, b, g2).iloc[300:]
    a = cc.iloc[300:] - b.iloc[300:].mul(m, axis=0)
    assert np.allclose(e2.iloc[:, g2_idx], a.iloc[:, g2_idx].sub(a.mean(axis=1), axis=0))
    assert R.groups(pd.Index(["X.T"]), pd.Series(dtype=object))[0] == "?X.T"


def test_inputs_and_answer_do_not_look_ahead():
    open_, close, sector, _ = _market(seed=3)
    names, x0 = H.features(open_, close)
    _, x = R.features(open_, close, sector)
    keep = [i for i, k in enumerate(names) if k not in R.RESIDUAL_INPUTS]
    assert len(keep) == 10 and np.array_equal(x[:, :, keep], x0[:, :, keep], equal_nan=True)   # ほかの 10 個は M2 と同じ
    changed = [names.index(k) for k in R.RESIDUAL_INPUTS]
    assert not np.allclose(np.nan_to_num(x[:, :, changed]), np.nan_to_num(x0[:, :, changed]))
    t = 700
    bent = close.copy()
    bent.iloc[t:] *= np.random.default_rng(9).uniform(0.7, 1.3, bent.iloc[t:].shape)     # t 日の引けから先を変える
    _, xb = R.features(open_, bent, sector)
    assert np.array_equal(x[: t + 1], xb[: t + 1], equal_nan=True)                    # t 日までの入力は変わらない（t 日は 13:30 まで）
    beta = R.betas(H.parts(open_, close)["cc"])
    g = R.groups(close.columns, sector)
    y = R.target(close, beta, g)
    late = close.copy()
    late.iloc[t + 6:] *= 2.0
    y2 = R.target(late, beta, g)
    assert np.allclose(y.iloc[: t + 1], y2.iloc[: t + 1], equal_nan=True) and y.iloc[-5:].isna().all().all()


def _judge(**kw):
    open_, close, sector, _ = _market(n_stocks=80, n_groups=8, **kw)
    sc = R.scores(open_, close, sector, params=FAST, log=lambda s: None)
    v = R.evaluate(sc["base"], sc["new"], open_, close, sc["beta"], sc["y_raw"], sc["y_res"], placebo=20)
    return v, pd.DataFrame(v["ic"]).dropna(how="all").mean()


def test_a_planted_own_reversal_is_found_better_by_m2r():
    """その株だけの動きが戻り、業種の動きは大きいが続かない市場では、M2r のほうが M2 より良い（効きを仕込めば見つかる）。"""
    v, ic = _judge(seed=6, reversal=0.35, sector_vol=3.0)
    assert ic["new_res"] > ic["base_res"] + 0.03 and v["new"]["net"] > v["base"]["net"]
    assert v["mean"] > 0 and v["t"] > 1.65 and v["placebo"] >= 0.95
    assert v["base"]["turnover"] > 0 and 0.3 < v["beta"]["all"] < 2.0


def test_m2r_gives_up_what_whole_industries_do():
    """作りの上の性質: 業種全体の動きが続く市場では、M2 はそれを使えるが、M2r は使わない（答えと入力から業種の分を引くため）。"""
    v, ic = _judge(seed=4, reversal=0.35, sector_ar=0.9)
    assert ic["base_raw"] > 0.3 and v["base"]["net"] > v["new"]["net"] and v["mean"] < 0


def test_residual_command_writes_the_report_once(tmp_path, monkeypatch, capsys):
    open_, close, sector, _ = _market(seed=5)
    data = tmp_path / "stocks"
    data.mkdir()
    open_.to_csv(data / "jp_open.csv")
    close.to_csv(data / "jp_close.csv")
    pd.DataFrame({"ticker": close.columns, "name": [f"会社{k}" for k in range(close.shape[1])], "sector": sector.to_numpy()}).to_csv(
        data / "universe_jp.csv", index=False)
    mod = _script()
    monkeypatch.setattr(H, "GBM_PARAMS", FAST)
    argv = ["agents.py", "residual", "--root", str(tmp_path / "agents"), "--data", str(data), "--out", str(tmp_path / "out"), "--placebo", "5"]
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit, match="もう 1 回計算してある（2026-11-08"):        # 判定は記録済み
        mod.main()
    monkeypatch.setattr(R, "RESULT", {})                                              # 記録する前の形で 1 回だけ動かす
    mod.main()
    out = capsys.readouterr().out
    path = Path(out.split("保存先: ")[-1].split("（")[0].strip())
    text = path.read_text(encoding="utf-8")
    assert "| M2r |" in text and "| 2.50 |" in text and "（試した数 91）" in text and "業種 6 区分（5 社以上の業種 6" in text
    assert "| M2（今の作り方） |" in text and "| M2r（その株だけの動き） |" in text and "手で買う形（週 1 回・10 銘柄・1 日遅れ" in text
    assert "| **平均** |" in text and (path.parent / "residual_scores.csv.gz").exists()
    with pytest.raises(SystemExit, match="もう 1 回計算してある"):                   # 1 回だけ
        mod.main()
