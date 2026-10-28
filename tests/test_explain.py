"""学習の部品の中身（SHAP 値。docs/agents.md 14 章、./cfd agents shap）の試験。作ったデータだけを使う。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("shap")
pytest.importorskip("matplotlib")

from cfdbot import stocks_short as H  # noqa: E402
from cfdbot.agents import earnings as N  # noqa: E402
from cfdbot.agents import explain as XP  # noqa: E402
from cfdbot.agents import ir_scorer as I  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAST = dict(max_iter=20, learning_rate=0.1, max_leaf_nodes=15, min_samples_leaf=200, early_stopping=False, random_state=0)


def _script():
    spec = importlib.util.spec_from_file_location("agents_explain", ROOT / "scripts" / "agents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _prices(n_stocks=60, start="2016-01-04", end="2019-12-27", seed=0):
    idx = pd.bdate_range(start, end)
    rng = np.random.default_rng(seed)
    cols = [f"{1000 + k}.T" for k in range(n_stocks)]
    night = rng.normal(0.0005, 0.01, (len(idx), n_stocks))
    day = rng.normal(0, 0.015, (len(idx), n_stocks))
    close = np.empty_like(night)
    open_ = np.empty_like(night)
    prev = np.full(n_stocks, 1000.0)
    for t in range(len(idx)):
        open_[t] = prev * (1 + night[t])
        close[t] = open_[t] * (1 + day[t])
        prev = close[t]
    return pd.DataFrame(open_, idx, cols), pd.DataFrame(close, idx, cols)


def test_planted_inputs_come_first_with_the_right_direction_and_pairs_are_found(tmp_path):
    from sklearn.ensemble import HistGradientBoostingRegressor

    rng = np.random.default_rng(1)
    X = rng.normal(size=(6000, 5))
    X[:, 4] = X[:, 3] + rng.normal(0, 0.1, 6000)                                          # よく似た組
    X[rng.random(6000) < 0.05, 2] = np.nan                                                 # 欠けがあっても動く
    y = 2 * X[:, 0] - X[:, 1] + rng.normal(0, 0.1, 6000)
    model = HistGradientBoostingRegressor(max_iter=50, random_state=0).fit(X, y)
    names = ["a", "b", "c", "d", "e"]
    sv, pred, err = XP.explain(model, X[XP.pick(len(X), 2000, seed=0)])
    xs = X[XP.pick(len(X), 2000, seed=0)]
    s = XP.summary(sv, xs, names)
    assert list(s["name"][:2]) == ["a", "b"] and err < 1e-8                               # SHAP 値を足すと点数に戻る
    assert s.set_index("name").at["a", "direction"] > 0.9 and s.set_index("name").at["b", "direction"] < -0.9
    assert s["share"].sum() == pytest.approx(1.0) and s.set_index("name").at["c", "missing"] > 0
    assert [(a, b) for a, b, _ in XP.similar_pairs(xs, names)] == [("d", "e")]
    png = XP.beeswarm(sv, xs, names, tmp_path / "b.png", "test")
    assert png.stat().st_size > 5000
    assert np.array_equal(XP.pick(100, 30, seed=3), XP.pick(100, 30, seed=3)) and len(XP.pick(10, 30, seed=0)) == 10


def test_the_year_models_are_the_same_as_the_ledger_scores():
    """M2・決算の数字とも、見る部品は walk_forward（台帳に書く点数）と同じ切り方で、同じ点数を出す。"""
    open_, close = _prices()
    _, x = H.features(open_, close)
    _, y5 = H.targets(open_, close)
    tradable = (close.notna() & H.parts(open_, close)["night"].notna()).to_numpy()
    pred = H.walk_forward(x, y5, tradable, first_year=2019, embargo=H.EMBARGO, params=FAST, log=lambda s: None)
    r = XP.m2_year(x, y5, tradable, 2019, params=FAST)
    d, s = r["rows"]
    mine = r["model"].predict(x[d, s][:, r["use"]])
    assert np.allclose(mine, pred.to_numpy()[d, s], atol=1e-6) and r["train_end"] < pd.Timestamp("2019-01-01")
    assert XP.m2_year(x, y5, tradable, 2030, params=FAST) is None

    rng = np.random.default_rng(2)
    n = 6000
    table = pd.DataFrame({"pos": rng.integers(0, len(close) - 6, n), "j": rng.integers(0, close.shape[1], n),
                          **{f: rng.normal(size=n) for f in N.FEATURES[:-1]}, "kind": rng.integers(1, 7, n).astype(float)})
    y = I.target_rank(close)
    score = N.walk_forward(table, y, close.index, first_year=2019, params=FAST, log=lambda s: None)
    rn = XP.numbers_year(table, y, close.index, 2019, params=FAST)
    rows = rn["rows"]
    got = rn["model"].predict(table.loc[rows, N.FEATURES].to_numpy(dtype=float)[:, rn["use"]])
    assert np.allclose(got, score[rows], atol=1e-9)


def test_shap_command_writes_the_report_and_both_pictures(tmp_path, monkeypatch, capsys):
    open_, close = _prices()
    data, root = tmp_path / "stocks", tmp_path / "agents"
    data.mkdir()
    root.mkdir()
    open_.to_csv(data / "jp_open.csv")
    close.to_csv(data / "jp_close.csv")
    mod = _script()
    monkeypatch.setattr(H, "GBM_PARAMS", FAST)                                              # 試験を速くする（作り方は同じ）
    monkeypatch.setattr(N, "GBM_PARAMS", FAST)
    base = ["--root", str(root), "--data", str(data), "--out", str(tmp_path / "out")]
    monkeypatch.setattr("sys.argv", ["agents.py", "select", *base])
    mod.main()                                                                              # 13:05 と同じ点数を先に書く
    rng = np.random.default_rng(3)
    n = 5000
    num = pd.DataFrame({"date": close.index[rng.integers(0, len(close) - 6, n)], "ticker": close.columns[rng.integers(0, 60, n)],
                        **{f: rng.normal(size=n) for f in N.FEATURES[:-1]}, "kind": rng.integers(1, 7, n)})
    tab = num.assign(pos=close.index.get_indexer(num["date"]), j=close.columns.get_indexer(num["ticker"]))
    num["score"] = N.walk_forward(tab, I.target_rank(close), close.index, log=lambda s: None)
    num[["date", "ticker", "score"] + N.FEATURES].to_csv(root / "ir_numbers.csv", index=False, float_format="%.6f")
    monkeypatch.setattr("sys.argv", ["agents.py", "shap", *base, "--years", "2", "--sample", "400"])
    mod.main()
    out = capsys.readouterr().out
    path = Path(out.split("保存先: ")[-1].split("（")[0].strip())
    text = path.read_text(encoding="utf-8")
    assert "## 1. M2" in text and "## 2. 決算の数字" in text and "![bee swarm](shap_m2.png)" in text
    assert (path.parent / "shap_m2.png").stat().st_size > 5000 and (path.parent / "shap_numbers.png").exists()
    assert "年ごとの影響の大きさの順位" in text and "| `night_20` |" in text and "夜の平均・20 日" in text
    m2_note = text.split("selector_scores.csv）との差は最大 ")[1].split("（")[0]
    nb_note = text.split("ir_numbers.csv）との差は最大 ")[1].split("（")[0]
    assert float(m2_note) < 1e-4 and float(nb_note) < 1e-4                                 # 見た部品は、台帳の点数を出した部品と同じ
    summ = pd.read_csv(path.parent / "shap_m2_summary.csv")
    assert set(summ["year"]) == {2018, 2019} and summ.groupby("year")["share"].sum().round(6).eq(1).all()


def test_shap_command_without_the_numbers_file_still_writes_m2(tmp_path, monkeypatch, capsys):
    open_, close = _prices(start="2017-01-02")
    data, root = tmp_path / "stocks", tmp_path / "agents"
    data.mkdir()
    root.mkdir()
    open_.to_csv(data / "jp_open.csv")
    close.to_csv(data / "jp_close.csv")
    mod = _script()
    monkeypatch.setattr(H, "GBM_PARAMS", FAST)
    monkeypatch.setattr("sys.argv", ["agents.py", "shap", "--root", str(root), "--data", str(data), "--out", str(tmp_path / "out"),
                                     "--years", "1", "--sample", "300"])
    mod.main()
    out = capsys.readouterr().out
    assert "ir_numbers.csv` が無い" in out and "selector_scores.csv）が無いので比べていない" in out and "## 1. M2" in out
