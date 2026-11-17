"""広げた候補の判定・組み合わせ・1 回だけの印（docs/wide.md 3・5・7 章）。作ったデータだけで確かめる。"""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdbot.wide import core as C
from cfdbot.wide.base import Check, NotReady, Stream

ROOT = Path(__file__).resolve().parents[1]


def months(n, start="2000-01-31"):
    return pd.date_range(start, periods=n, freq="ME")


def stream(key, values, events=None, start="2000-01-31"):
    v = np.asarray(values, dtype=float)
    return Stream(key=key, d=pd.Series(v, index=months(len(v), start)), events=events, gross=float(v.mean()))


def test_threshold_and_trials():
    assert C.TRIALS == 101 and C.PRIOR_TRIALS == 93
    assert abs(C.THRESHOLD - 2.5341) < 1e-4       # 参考の列だけ
    assert C.T_MIN == 1.65 and C.P_FIRST == 0.05  # 判定は 1 回目として（2026-11-12 利用者の決め）
    assert C.RESULT == {}


def test_halves_odd_gives_second_one_more():
    a, b = C.halves(pd.Series(range(61), index=months(61)))
    assert len(a) == 30 and len(b) == 31 and b.iloc[0] == 30


def test_judge_verdicts():
    rng = np.random.default_rng(1)
    strong = stream("W1", 0.01 + 0.01 * rng.standard_normal(120))
    assert C.judge(strong)["verdict"] == "使えそう"
    weak = stream("W1", 0.0005 + 0.01 * rng.standard_normal(120))
    assert C.judge(weak)["verdict"] in ("使えない", "まだ判断できない")
    neg = stream("W1", -0.01 + 0.01 * rng.standard_normal(120))
    assert C.judge(neg)["verdict"] == "使えない"
    # 前半だけ良い → 条件 1 を満たさない
    half = stream("W1", np.r_[0.02 + 0.01 * rng.standard_normal(60), -0.005 + 0.001 * rng.standard_normal(60)])
    assert C.judge(half)["verdict"] == "使えない"


def test_judge_maybe_when_t_between():
    # 全期間の t 値が 1.65〜2.534 で、前半・後半ともプラス → まだ判断できない
    base = np.tile([0.03, -0.025], 60)            # 平均 0.0025、標準偏差 ≈ 0.0276 → t ≈ 0.99
    d = base + 0.0020                             # 平均 0.0045 → t ≈ 1.78
    j = C.judge(stream("W2", d, events=200))
    assert 1.65 <= j["t"] < C.THRESHOLD and j["t2"] < 1.65
    assert j["verdict"] == "まだ判断できない" and j["verdict_ref"] == "まだ判断できない"


def test_first_time_bar_does_not_depend_on_trials():
    # 全期間 t ≈ 2.4・後半 t ≈ 1.70: 1 回目としては「使えそう」、通し番号で数えると（2.534 に届かず）「まだ判断できない」
    d = np.tile([0.03, -0.025], 60) + 0.00355
    j = C.judge(stream("W1", d))
    assert 2.3 < j["t"] < C.THRESHOLD and j["t2"] >= 1.65
    assert j["verdict"] == "使えそう" and j["verdict_ref"] == "まだ判断できない"


def test_judge_short():
    assert C.judge(stream("W1", np.full(59, 0.01)))["verdict"] == C.SHORT
    assert C.judge(stream("W5", np.full(120, 0.01), events=29))["verdict"] == C.SHORT
    assert C.judge(stream("W5", 0.01 + 0.001 * np.sin(np.arange(120)), events=30))["verdict"] != C.SHORT


def test_short_rule_shows_no_return_numbers():
    rng = np.random.default_rng(31)
    s = {k: stream(k, 0.01 * rng.standard_normal(120)) for k in ("W1", "W3", "W7")}
    s["W6"] = stream("W6", np.full(58, 0.0321), events=42)          # 月が 60 に届かない
    s["W6"].gross = 0.0331
    single = {k: C.judge(v) for k, v in s.items()}
    j = single["W6"]
    assert j["verdict"] == C.SHORT and all(np.isnan(j[x]) for x in ("mean", "mean1", "mean2", "t", "t2"))
    wx = C.judge_combos(single, s, n_flips=200)
    assert wx["rules"] == ["W1", "W3", "W7"]                         # 組み合わせにも入らない
    skipped = {k: "作ったデータに無い" for k in C.RULES if k not in s}
    text = "\n".join(C.report_lines(single, wx, s, skipped, "2026-11-17 10:00"))
    row = next(x for x in text.splitlines() if x.startswith("| W6 |"))
    assert "58 か月・42 件" in row and C.SHORT in row and "%" not in row
    assert "3.21%" not in text and "3.31%" not in text              # 平均も、コストの前の平均も出さない
    assert "W6: 2000-01〜2004-10。数が足りないので判定せず、成績の数字も出さない" in text


def test_sigma_uses_previous_months_only():
    d = pd.Series(np.r_[np.zeros(11), 1.0, 0.0, 0.0], index=months(14))
    s = C.sigmas(d)
    assert s.iloc[:12].isna().all()               # 12 か月分（前の月まで）がそろうまでは無い
    assert s.iloc[12] == pytest.approx(np.std(np.r_[np.zeros(11), 1.0], ddof=1))
    d2 = d.copy()
    d2.iloc[13] = 100.0                           # 先の月を変えても、その月の σ は変わらない
    assert C.sigmas(d2).iloc[13] == s.iloc[13]


def test_sigma_window_is_calendar_months_when_a_month_is_missing():
    d = pd.Series(np.arange(50, dtype=float), index=months(50))
    gap = d.drop(d.index[20])                     # 1 か月だけ作らなかった
    s = C.sigmas(gap)
    t = d.index[45]
    expect = d.reindex(months(50)).drop(d.index[20]).loc[d.index[9]:d.index[44]].std(ddof=1)
    assert s[t] == pytest.approx(expect)          # 窓は 36 暦月（値のある月は 35）
    assert d.index[20] not in s.index


def test_combo_series_equal_risk_weights():
    rng = np.random.default_rng(2)
    a = pd.Series(0.01 * rng.standard_normal(100), index=months(100))
    b = pd.Series(0.03 * rng.standard_normal(100), index=months(100))
    c = C.combo_series({"W1": a, "W2": b}, ("W1", "W2"))
    t = c.index[20]
    sa, sb = C.sigmas(a)[t], C.sigmas(b)[t]
    wa = (1 / sa) / (1 / sa + 1 / sb)
    assert c[t] == pytest.approx(wa * a[t] + (1 - wa) * b[t])
    assert c.index[0] == a.index[12]              # σ がそろう月から


def test_combo_only_common_months():
    a = pd.Series(np.sin(np.arange(100)) / 100, index=months(100))
    b = pd.Series(np.cos(np.arange(80)) / 100, index=months(80, "2003-05-31"))
    c = C.combo_series({"W1": a, "W2": b}, ("W1", "W2"))
    assert c.index[0] == b.index[12] and c.index[-1] == a.index[-1]


def test_flip_pvalue_noise_and_signal():
    rng = np.random.default_rng(3)
    noise = {("A", "B"): pd.Series(rng.standard_normal(200), index=months(200)),
             ("A", "C"): pd.Series(rng.standard_normal(200), index=months(200))}
    best = max(C.t_value(s) for s in noise.values())
    assert C.flip_pvalue(noise, best, n=2000) > 0.01
    sig = {("A", "B"): pd.Series(1.0 + 0.1 * rng.standard_normal(200), index=months(200))}
    assert C.flip_pvalue(sig, C.t_value(sig[("A", "B")]), n=2000) == pytest.approx(1 / 2001)


def test_flip_matches_slow_version():
    rng = np.random.default_rng(4)
    ser = {("A", "B"): pd.Series(0.2 + rng.standard_normal(60), index=months(60)),
           ("A", "C"): pd.Series(0.1 + rng.standard_normal(70), index=months(70, "1999-03-31"))}
    best = max(C.t_value(s) for s in ser.values())
    fast = C.flip_pvalue(ser, best, n=300, seed=7)
    m = pd.concat(ser, axis=1, sort=True)
    r = np.random.default_rng(7)
    hits = 0
    for start in range(0, 300, 1000):
        signs = r.choice((-1.0, 1.0), size=(min(1000, 300 - start), len(m)))
        for s in signs:
            hits += max(C.t_value(m[c] * s) for c in m.columns) >= best - 1e-12
    assert fast == pytest.approx((hits + 1) / 301)


def slow_counts(series, n, seed):
    m = pd.concat(series, axis=1, sort=True)
    r = np.random.default_rng(seed)
    out = []
    for start in range(0, n, 1000):
        for sg in r.choice((-1.0, 1.0), size=(min(1000, n - start), len(m))):
            k = 0
            for c in m.columns:
                v = (m[c] * sg).dropna()
                a, b = C.halves(v)
                k += C.label(bool(a.mean() > 0 and b.mean() > 0), C.t_value(v), C.t_value(b), C.T_MIN) == "使えそう"
            out.append(k)
    return np.array(out)


def test_flip_counts_match_slow_version():
    rng = np.random.default_rng(10)
    ser = {"A": pd.Series(0.3 + rng.standard_normal(61), index=months(61)),
           "B": pd.Series(0.1 + rng.standard_normal(70), index=months(70, "1999-03-31")),
           "C": pd.Series(rng.standard_normal(80), index=months(80, "1998-06-30"))}
    assert (C.flip_counts(ser, n=200, seed=3) == slow_counts(ser, 200, 3)).all()


def test_chance_noise_and_signal():
    rng = np.random.default_rng(11)
    noise = {k: pd.Series(rng.standard_normal(200), index=months(200)) for k in "ABCDEFG"}
    c = C.flip_counts(noise, n=2000)
    assert 0.0 < c.mean() < 0.3                   # でたらめでは 7 つのうち平均 0.1 個ほど
    ch = C.chance(2, c)
    assert ch["real"] == 2 and ch["atleast"] < 0.05 and ch["any"] == pytest.approx((c >= 1).mean())
    assert C.chance(0, c)["atleast"] == 1.0


def test_combo_table_labels_each_combo_first_time():
    rng = np.random.default_rng(12)
    s = {"W1": stream("W1", 0.01 + 0.01 * rng.standard_normal(150)), "W3": stream("W3", 0.01 * rng.standard_normal(150)),
         "W7": stream("W7", 0.01 * rng.standard_normal(150))}
    single = {k: C.judge(v) for k, v in s.items()}
    wx = C.judge_combos(single, s, n_flips=300)
    t = wx["table"]
    assert set(t["first"]) <= {"使えそう", "まだ判断できない", "使えない"}
    for r in t.itertuples(index=False):
        assert r.first == C.label(r.mean1 > 0 and r.mean2 > 0, r.t, r.t2, C.T_MIN)
    assert wx["chance"]["real"] == int((t["first"] == "使えそう").sum())
    assert wx["verdict"] == "使えそう" and wx["verdict_ref"] in ("使えそう", "まだ判断できない", "使えない")


def test_best_member_is_measured_on_the_combo_months():
    rng = np.random.default_rng(21)
    # A は始めの 40 か月だけ大きく儲かる（全期間の t 値は高い）。B は 31 か月目から始まるので、組の月は A の良い時期を含まない
    a = pd.Series(np.r_[0.05 + 0.01 * rng.standard_normal(40), 0.01 * rng.standard_normal(90)], index=months(130))
    b = pd.Series(0.002 + 0.01 * rng.standard_normal(100), index=months(100, "2002-07-31"))
    table, series, _ = C.combos({"W1": a, "W2": b})
    row = table.iloc[0]
    idx = series[("W1", "W2")].index
    assert idx[0] == b.index[12]                                   # σ がそろう月から
    ta, tb = C.t_value(a.reindex(idx)), C.t_value(b.reindex(idx))
    assert C.t_value(a) > 5 and ta < 2                             # 全期間と同じ月とで大きく違う
    assert row["t_member"] == pytest.approx(max(ta, tb))
    assert row["member"] == ("W1" if ta >= tb else "W2")
    assert bool(row["lift"]) == bool(row["t"] > max(ta, tb))
    assert C.best_member({"W1": a, "W2": b}, ("W1", "W2"), idx) == (row["member"], row["t_member"])


def test_lift_yes_for_two_independent_good_rules_and_no_for_good_plus_noise():
    rng = np.random.default_rng(22)
    good1 = stream("W1", 0.003 + 0.01 * rng.standard_normal(200))
    good2 = stream("W3", 0.003 + 0.01 * rng.standard_normal(200))
    noise = stream("W7", 0.01 * rng.standard_normal(200))
    s = {"W1": good1, "W3": good2, "W7": noise}
    single = {k: C.judge(v) for k, v in s.items()}
    wx = C.judge_combos(single, s, n_flips=300)
    t = wx["table"].set_index("combo")
    assert bool(t.loc["W1+W3", "lift"])                            # 別々に良い 2 つを足すと、どちらの単独より良い
    assert not bool(t.loc["W1+W7", "lift"])                        # 良いものに中身の無いものを足しても、単独より良くならない
    for r in wx["table"].itertuples(index=False):
        assert bool(r.lift) == bool(r.t > r.t_member)
    good = wx["table"]["first"] == "使えそう"
    assert wx["good_lift"] == int((good & wx["table"]["lift"]).sum())
    assert wx["no_lift"] == (not bool(wx["table"].set_index("combo").loc[wx["best"]["combo"], "lift"]))
    skipped = {k: "作ったデータに無い" for k in C.RULES if k not in s}
    text = "\n".join(C.report_lines(single, wx, s, skipped, "2026-11-17 10:00"))
    assert "中身の一番（同じ月）" in text and "| 上乗せ |" in text and "1 回目として「使えそう」の組（" in text
    assert "上乗せのある組は" in text


def test_best_member_ignores_rules_without_a_t_value():
    idx = months(80)
    const = pd.Series(0.0, index=idx)                              # 事象が 1 つも無い月ばかり（値がすべて 0）→ t 値は計算できない
    rng = np.random.default_rng(23)
    other = pd.Series(0.01 * rng.standard_normal(80), index=idx)
    k, t = C.best_member({"W1": const, "W2": other}, ("W1", "W2"), idx)
    assert k == "W2" and t == pytest.approx(C.t_value(other))


def test_judge_combos_cannot_with_one_rule():
    s = {"W1": stream("W1", np.full(100, 0.01))}
    single = {"W1": C.judge(s["W1"])}
    assert C.judge_combos(single, s)["verdict"] == "作れない"


def test_judge_combos_excludes_short_and_counts():
    rng = np.random.default_rng(5)
    s = {k: stream(k, 0.002 + 0.01 * rng.standard_normal(150)) for k in ("W1", "W3", "W7")}
    s["W5"] = stream("W5", 0.01 * rng.standard_normal(150), events=10)      # 数が足りない → 組に入れない
    single = {k: C.judge(v) for k, v in s.items()}
    wx = C.judge_combos(single, s, n_flips=500)
    assert wx["rules"] == ["W1", "W3", "W7"] and wx["n_combos"] == 4
    assert wx["verdict"] in ("使えそう", "まだ判断できない", "使えない")
    assert C.trials_used(single, wx) == 93 + 3 + 1


def test_judge_combos_all_losing_gives_p_one_without_crash():
    # どの組も大きく負けると、どの入れ替えの一番も本物の一番以上になり p = 1。正規分布の値に直すところで落ちない
    rng = np.random.default_rng(9)
    s = {k: stream(k, -0.01 + 0.01 * rng.standard_normal(150)) for k in ("W1", "W3", "W7")}
    single = {k: C.judge(v) for k, v in s.items()}
    wx = C.judge_combos(single, s, n_flips=300)
    assert wx["p"] == 1.0 and wx["verdict"] == "使えない" and wx["z"] == float("-inf")
    skipped = {k: "試し" for k in C.RULES if k not in s}
    text = "\n".join(C.report_lines(single, wx, s, skipped, "2026-11-12 10:00"))
    assert "p = 1.0000" in text


def test_report_has_all_rules():
    rng = np.random.default_rng(6)
    s = {k: stream(k, 0.01 * rng.standard_normal(100)) for k in ("W1", "W2", "W3", "W4", "W5", "W7")}
    single = {k: C.judge(v) for k, v in s.items()}
    wx = C.judge_combos(single, s, n_flips=200)
    lines = C.report_lines(single, wx, s, {"W6": "一覧が 95% に届かない"}, "2026-11-12 10:00")
    text = "\n".join(lines)
    for k in C.RULES:
        assert f"| {k} |" in text
    assert "外した（一覧が 95% に届かない）" in text and "| WX |" in text
    assert "判定（1 回目として）" in text and "偶然でもいくつ通るか" in text and "参考の列" in text


# --------------------------------------------------------------------------- 実行の道具（scripts/wide_study.py）
def load_script():
    spec = importlib.util.spec_from_file_location("wide_study", ROOT / "scripts" / "wide_study.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeRule:
    def __init__(self, key, ok=True, values=None, events=None):
        self.key, self.ok, self.values, self.events, self.computed = key, ok, values, events, 0

    def check(self, data):
        return Check(key=self.key, ok=self.ok, lines=["期間 2000-01〜"], why="" if self.ok else "データが無い")

    def compute(self, data):
        self.computed += 1
        if self.values is None:
            raise NotReady("無い")
        return stream(self.key, self.values, self.events)


@pytest.fixture
def fake(monkeypatch):
    rng = np.random.default_rng(8)
    rules = {k: FakeRule(k, values=0.01 * rng.standard_normal(100)) for k in C.RULES}
    m = load_script()
    monkeypatch.setattr(m, "rule", lambda k: rules[k])
    monkeypatch.setattr(C, "N_FLIPS", 200)
    return m, rules


def test_run_writes_marker_then_stops(fake, tmp_path):
    m, rules = fake
    path = m.main(["--out", str(tmp_path / "out")], root=tmp_path)
    assert path.exists() and "## 1. 判定" in path.read_text(encoding="utf-8")
    rec = json.loads((tmp_path / C.MARKER).read_text(encoding="utf-8"))
    assert set(rec["verdicts"]) == set(C.RULES) | {"WX"}
    with pytest.raises(SystemExit):
        m.main([], root=tmp_path)
    assert all(r.computed == 1 for r in rules.values())


def test_check_never_computes(fake, tmp_path):
    m, rules = fake
    assert m.main(["--check"], root=tmp_path) is None
    assert all(r.computed == 0 for r in rules.values())
    assert not (tmp_path / C.MARKER).exists()


def test_failing_check_needs_without(fake, tmp_path):
    m, rules = fake
    rules["W6"].ok = False
    with pytest.raises(SystemExit) as e:
        m.main([], root=tmp_path)
    assert "W6" in str(e.value) and not (tmp_path / C.MARKER).exists()
    path = m.main(["--without", "W6", "--out", str(tmp_path / "o")], root=tmp_path)
    assert "外した（データが無い）" in path.read_text(encoding="utf-8")
    assert rules["W6"].computed == 0


def test_cannot_drop_passing_rule(fake, tmp_path):
    m, rules = fake
    with pytest.raises(SystemExit) as e:
        m.main(["--without", "W1"], root=tmp_path)
    assert "外せない" in str(e.value)
    assert all(r.computed == 0 for r in rules.values())


def test_not_ready_stops_without_marker(fake, tmp_path):
    m, rules = fake
    rules["W3"].values = None
    with pytest.raises(SystemExit):
        m.main([], root=tmp_path)
    assert not (tmp_path / C.MARKER).exists()


def test_result_constant_stops(fake, tmp_path, monkeypatch):
    m, rules = fake
    monkeypatch.setattr(C, "RESULT", {"date": "2026-11-20", "verdicts": {"W1": "使えない"}})
    with pytest.raises(SystemExit):
        m.main([], root=tmp_path)
    assert all(r.computed == 0 for r in rules.values())


def test_broken_marker_stops(fake, tmp_path):
    m, _ = fake
    (tmp_path / C.MARKER).parent.mkdir(parents=True)
    (tmp_path / C.MARKER).write_text("{", encoding="utf-8")
    with pytest.raises(SystemExit):
        m.main([], root=tmp_path)


def test_fetch_keeps_the_mac_awake(fake, tmp_path, monkeypatch):
    m, _ = fake
    calls = []
    monkeypatch.setattr(m.sys, "platform", "darwin")
    monkeypatch.setattr(m.subprocess, "Popen", lambda cmd, *a, **k: calls.append(cmd))
    assert m.main(["--fetch-edinet"], root=tmp_path) is None
    assert calls and calls[0][:2] == ["caffeinate", "-i"] and calls[0][2] == "-w"
    calls.clear()
    m.main(["--check"], root=tmp_path)             # 点検と判定では呼ばない
    assert calls == []


def test_fetch_goes_on_when_caffeinate_is_missing(fake, tmp_path, monkeypatch):
    m, _ = fake

    def boom(*a, **k):
        raise FileNotFoundError("caffeinate")

    monkeypatch.setattr(m.sys, "platform", "darwin")
    monkeypatch.setattr(m.subprocess, "Popen", boom)
    assert m.main(["--fetch"], root=tmp_path) is None
