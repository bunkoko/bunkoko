import numpy as np
import pytest

from tools.samplegen import sampling
from tools.samplegen.sampling import (
    allocate,
    controlled_rounding,
    ipf,
    largest_remainder,
    rng_for,
    to_permille,
    weighted_sample,
)


def test_largest_remainder_sums_and_ties():
    assert largest_remainder(10, [1, 1, 1]) == [4, 3, 3]  # 端数が同じなら前の項目を優先
    assert largest_remainder(1000, [350, 200, 120, 130, 100, 100]) == [350, 200, 120, 130, 100, 100]
    assert sum(largest_remainder(450, to_permille([18, 16, 14, 13, 12, 8, 8, 7, 4]))) == 450


def test_largest_remainder_within_one_of_quota():
    shares = [155, 333, 7, 505]
    counts = largest_remainder(997, shares)
    assert sum(counts) == 997
    for c, s in zip(counts, shares, strict=True):
        assert abs(c - 997 * s / 1000) < 1


def test_to_permille_rejects_bad_input():
    with pytest.raises(ValueError):
        to_permille([50, 40])
    with pytest.raises(ValueError):
        to_permille([33.33, 66.67])


def test_rng_for_is_stable_and_independent():
    a = rng_for("x").random(3)
    assert (a == rng_for("x").random(3)).all()
    assert (a != rng_for("y").random(3)).any()


def test_rng_for_follows_module_seed(monkeypatch):
    base = rng_for("x").random(2)
    monkeypatch.setattr(sampling, "SEED", 1)
    assert (rng_for("x").random(2) != base).any()
    assert (rng_for("x", seed=20261016).random(2) == base).all()


def test_weighted_sample_exact_k_and_respects_zero():
    rng = rng_for("t")
    w = np.array([0.0, 1.0, 1.0, 5.0, 0.0, 2.0])
    for _ in range(50):
        got = weighted_sample(rng, w, 3)
        assert len(set(got.tolist())) == 3
        assert not {0, 4} & set(got.tolist())
    with pytest.raises(ValueError):
        weighted_sample(rng, w, 5)


def test_weighted_sample_prefers_heavy():
    rng = rng_for("heavy")
    w = np.array([10.0] * 100 + [1.0] * 900)
    hits = sum(int((weighted_sample(rng, w, 50) < 100).sum()) for _ in range(20))
    assert hits / (20 * 50) > 0.4  # 一様なら0.1


def test_weighted_sample_rejects_nan_and_negative():
    rng = rng_for("bad")
    with pytest.raises(ValueError):
        weighted_sample(rng, [1.0, float("nan")], 1)
    with pytest.raises(ValueError):
        weighted_sample(rng, [1.0, -1.0], 1)
    with pytest.raises(ValueError):
        weighted_sample(rng, [1.0, 0.0], 2)  # 正の重みが足りない


def test_ipf_matches_margins():
    rng = rng_for("ipf")
    w = rng.random((200, 4)) + 0.1
    targets = [80, 60, 40, 20]
    p = ipf(w, targets)
    assert np.allclose(p.sum(axis=1), 1.0)
    assert np.allclose(p.sum(axis=0), targets, atol=1e-6)


def test_ipf_with_row_targets():
    w = np.array([[1.0, 2.0, 3.0], [2.0, 1.0, 1.0], [1.0, 1.0, 5.0]])
    p = ipf(w, [10, 6, 4], row_targets=[8, 7, 5])
    assert np.allclose(p.sum(axis=1), [8, 7, 5])
    assert np.allclose(p.sum(axis=0), [10, 6, 4], atol=1e-9)


def test_ipf_rejects_bad_input_instead_of_returning_nan():
    with pytest.raises(ValueError):
        ipf(np.array([[0.0, 0.0], [1.0, 1.0]]), [1.0, 1.0])  # 全部0の行
    with pytest.raises(ValueError):
        ipf(np.array([[1.0, float("nan")], [1.0, 1.0]]), [1.0, 1.0])
    with pytest.raises(ValueError):
        ipf(np.array([[1.0, 1.0], [1.0, 1.0]]), [1.0, 2.0])  # 行の合計と列の合計が合わない
    with pytest.raises(ValueError):
        ipf(np.array([[1.0, 0.0], [1.0, 0.0]]), [1.0, 1.0])  # 実現できない目標


def _check_rounding(e, rows, cols, out):
    assert (out.sum(axis=1) == rows).all()
    assert (out.sum(axis=0) == cols).all()
    assert (out >= np.floor(e + 1e-9)).all()
    assert (out <= np.ceil(e - 1e-9)).all()  # 切り捨てか切り上げのどちらか


def test_controlled_rounding_keeps_row_and_column_sums():
    rng = rng_for("cr")
    for _ in range(40):
        w = rng.random((12, 5)) + 0.05
        rows = rng.integers(3, 12, size=12)
        cols = np.full(5, rows.sum() // 5)
        cols[: rows.sum() - cols.sum()] += 1
        e = ipf(w, cols, rows)
        out = controlled_rounding(e, rows, cols)
        _check_rounding(e, rows, cols, out)


def test_controlled_rounding_needs_augmenting_paths():
    # 端数の大きい順に足すだけでは行・列の不足が残る形（増加路で直す）
    e = np.array([[0.5, 0.5, 0.0], [0.5, 0.0, 0.5], [0.0, 0.5, 0.5]])
    out = controlled_rounding(e, [1, 1, 1], [1, 1, 1])
    _check_rounding(e, [1, 1, 1], [1, 1, 1], out)
    e2 = np.array([[0.6, 0.4], [0.4, 0.6]])
    assert controlled_rounding(e2, [1, 1], [1, 1]).tolist() == [[1, 0], [0, 1]]


def _groups(e, n_groups, rng):
    """行の集まり（対象）と列の組を無作為に作り、期待件数を付ける。"""
    out = []
    for _ in range(n_groups):
        mask = rng.random(e.shape[0]) < 0.3
        mask[0] = True
        col = int(rng.integers(e.shape[1]))
        out.append((mask, col, float(e[mask, col].sum())))
    return out


def _worst(k, groups):
    return max(abs(k[m, c].sum() - exp) for m, c, exp in groups)


def test_controlled_rounding_search_gets_group_totals_close_to_expectation():
    rng = rng_for("crs")
    w = rng.random((40, 5)) + 0.05
    rows = np.full(40, 9)
    cols = np.array([72, 72, 72, 72, 72])
    e = ipf(w, cols, rows)
    groups = _groups(e, 8, rng)

    base = controlled_rounding(e, rows, cols)
    best = controlled_rounding(e, rows, cols, rng=rng_for("crs2"), tries=50, groups=groups)
    _check_rounding(e, rows, cols, best)  # 行・列の合計と、切り捨て／切り上げの範囲は守る
    assert _worst(best, groups) <= _worst(base, groups)
    assert _worst(best, groups) < 1.0  # 局所探索で、どの対象も期待件数の1件以内に収まる
    again = controlled_rounding(e, rows, cols, rng=rng_for("crs2"), tries=50, groups=groups)
    assert (best == again).all()  # 種が同じなら同じ丸め


def test_local_search_improves_a_bad_rounding_and_keeps_every_constraint():
    rng = rng_for("ls")
    e = ipf(rng.random((30, 4)) + 0.05, np.full(4, 45), np.full(30, 6))
    groups = _groups(e, 5, rng)
    coef, exp = sampling._group_coef(groups, *e.shape)
    base = np.floor(e + 1e-9).astype(int)
    frac = e - base
    start = controlled_rounding(e, np.full(30, 6), np.full(4, 45))
    lo, hi = base, base + (frac > 1e-9)
    better = sampling._local_search(start, lo, hi, coef, exp)
    assert (better.sum(axis=1) == 6).all() and (better.sum(axis=0) == 45).all()
    assert (better >= lo).all() and (better <= hi).all()
    assert sampling._group_score(better, coef, exp) <= sampling._group_score(start, coef, exp)
    # もう動きがない：もう一度かけても変わらない
    assert (sampling._local_search(better, lo, hi, coef, exp) == better).all()


def test_controlled_rounding_rejects_inconsistent_sums():
    with pytest.raises(ValueError):
        controlled_rounding(np.array([[1.5, 1.5]]), [3], [1, 1])


def test_allocate_exact_counts():
    rng = rng_for("alloc")
    w = rng.random((100, 3)) + 0.1
    out = allocate(rng, [10, 30, 60], [0, 1, 2], w)
    assert np.bincount(out).tolist() == [10, 30, 60]
    w[:, 2] = 0.0  # 最後の分類に重み0の行が残るなら例外
    with pytest.raises(ValueError):
        allocate(rng, [10, 30, 60], [0, 1, 2], w)
    w[:, 2] = np.nan
    with pytest.raises(ValueError):
        allocate(rng, [10, 30, 60], [0, 1, 2], w)
