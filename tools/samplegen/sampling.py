"""乱数と割り付けの部品。種の扱い、最大剰余法、重み付き非復元抽出、IPF、制御丸め。"""

import zlib
from collections import deque
from collections.abc import Sequence

import numpy as np

SEED = 20261016


def rng_for(stage: str, seed: int | None = None) -> np.random.Generator:
    """工程ごとに独立な乱数列を返す。工程を足しても他の工程の出力が変わらない。

    seed を省くとモジュールの SEED（20261016）を使う。種を変えた確認用に、呼ぶたびに読む。
    """
    s = SEED if seed is None else seed
    return np.random.default_rng([s, zlib.crc32(stage.encode("utf-8"))])


def to_permille(percents: Sequence[float]) -> list[int]:
    """百分率（小数第1位まで）を 0.1% 単位の整数にする。合計が100%でなければ例外。"""
    out = []
    for p in percents:
        v = round(p * 10)
        if abs(p * 10 - v) > 1e-9:
            raise ValueError("百分率は小数第1位までしか使えません")
        out.append(v)
    if sum(out) != 1000:
        raise ValueError("百分率の合計が100%になっていません")
    return out


def largest_remainder(total: int, shares: Sequence[int]) -> list[int]:
    """最大剰余法。端数が同じなら前の項目を優先する（並びが固定なので結果も固定）。"""
    s = sum(shares)
    quotas = [total * x for x in shares]
    counts = [q // s for q in quotas]
    rems = [q % s for q in quotas]
    left = total - sum(counts)
    for i in sorted(range(len(shares)), key=lambda i: (-rems[i], i))[:left]:
        counts[i] += 1
    return counts


def round_half_up(x: float) -> int:
    return int(np.floor(x + 0.5))


def weighted_sample(rng: np.random.Generator, weights: Sequence[float], k: int) -> np.ndarray:
    """重み付き非復元抽出（Efraimidis-Spirakis）。選ばれた添字を昇順で返す。"""
    w = np.asarray(weights, dtype=float)
    if not np.isfinite(w).all() or (w < 0).any():
        raise ValueError("重みが有限の非負数ではありません")
    u = 1.0 - rng.random(w.shape[0])  # (0, 1]
    if k == 0:
        return np.empty(0, dtype=int)
    keys = np.full(w.shape, -np.inf)
    pos = w > 0
    keys[pos] = np.log(u[pos]) / w[pos]
    if int(np.isfinite(keys).sum()) < k:
        raise ValueError("重みが正の候補が足りません")
    chosen = np.argsort(-keys, kind="stable")[:k]
    return np.sort(chosen)


def ipf(
    weights: np.ndarray,
    col_targets: Sequence[float],
    row_targets: Sequence[float] | None = None,
    iterations: int = 1000,
    tol: float = 1e-12,
) -> np.ndarray:
    """行の合計を row_targets（省けば1）、列の合計を col_targets に合わせた行列を返す（IPF）。

    収束しない・重みが不正なら例外にする（NaN を通さない）。
    """
    w = np.asarray(weights, dtype=float)
    if not np.isfinite(w).all() or (w < 0).any() or (w.sum(axis=1) <= 0).any():
        raise ValueError("IPF の重みが不正です")
    r = np.ones(w.shape[0]) if row_targets is None else np.asarray(row_targets, dtype=float)
    c = np.asarray(col_targets, dtype=float)
    if abs(r.sum() - c.sum()) > 1e-9 or (r <= 0).any() or (c <= 0).any():
        raise ValueError("IPF の目標値が合いません")
    p = w / w.sum(axis=1, keepdims=True) * r[:, None]
    err = np.inf
    for _ in range(iterations):
        col = p.sum(axis=0)
        if (col <= 0).any():
            raise ValueError("IPF の列の合計が0です")
        p = p * (c / col)[None, :]
        p = p * (r / p.sum(axis=1))[:, None]
        err = float(np.abs(p.sum(axis=0) - c).max())
        if err < tol:
            break
    if not np.isfinite(p).all() or err > 1e-6:
        raise ValueError("IPF が収束しません")
    return p


def _round_once(frac: np.ndarray, need_r: np.ndarray, need_c: np.ndarray, priority: np.ndarray) -> np.ndarray:
    """端数のある要素に priority の小さい順に1ずつ足し、足りない行・列は増加路（最大流）で直す。"""
    n_rows, n_cols = frac.shape
    add = np.zeros(frac.shape, dtype=int)
    for flat in np.argsort(priority, axis=None, kind="stable"):
        i, c = divmod(int(flat), n_cols)
        if frac[i, c] <= 0:
            continue
        if add[i].sum() < need_r[i] and add[:, c].sum() < need_c[c]:
            add[i, c] = 1
    while True:
        left_r = need_r - add.sum(axis=1)
        left_c = need_c - add.sum(axis=0)
        if not left_r.any():
            return add
        # 増加路：不足のある行から、列→（足した行）→列…とたどって、不足のある列に着く
        parent: dict[tuple[str, int], tuple[str, int] | None] = {
            ("r", int(i)): None for i in np.flatnonzero(left_r > 0)
        }
        queue = deque(parent)
        goal = None
        while queue and goal is None:
            kind, idx = queue.popleft()
            if kind == "r":
                for c in range(n_cols):
                    node = ("c", c)
                    if frac[idx, c] > 0 and add[idx, c] == 0 and node not in parent:
                        parent[node] = (kind, idx)
                        if left_c[c] > 0:
                            goal = node
                            break
                        queue.append(node)
            else:
                for i in range(n_rows):
                    node = ("r", i)
                    if add[i, idx] == 1 and node not in parent:
                        parent[node] = (kind, idx)
                        queue.append(node)
        if goal is None:
            raise ValueError("制御丸めの解がありません")
        node = goal
        while parent[node] is not None:
            prev = parent[node]
            assert prev is not None
            if node[0] == "c":  # 行→列：足す
                add[prev[1], node[1]] = 1
            else:  # 列→行：足していたのを外す
                add[node[1], prev[1]] = 0
            node = prev


Group = tuple[np.ndarray, int, float]  # (対象の行の印, 列, 期待件数)
LOCAL_SEARCH_EPS = 1e-9  # この値より大きく下がる入れ替えだけ採る（浮動小数の差で結果が変わらないように）


def _group_coef(groups: Sequence[Group], n_rows: int, n_cols: int) -> tuple[np.ndarray, np.ndarray]:
    coef = np.zeros((n_rows, n_cols, len(groups)))
    for g, (mask, col, _) in enumerate(groups):
        coef[np.asarray(mask, dtype=bool), col, g] = 1.0
    return coef, np.array([g[2] for g in groups], dtype=float)


def _group_score(k: np.ndarray, coef: np.ndarray, exp: np.ndarray) -> float:
    totals = np.einsum("ijg,ij->g", coef, k)
    return float(((totals - exp) ** 2 / exp).sum())


def _local_search(
    k: np.ndarray, lo: np.ndarray, hi: np.ndarray, coef: np.ndarray, exp: np.ndarray, max_passes: int = 100
) -> np.ndarray:
    """2×2 の入れ替え（行 i・j と列 a・b で i,a +1 / i,b -1 / j,a -1 / j,b +1）による局所探索。

    行と列の合計、切り捨て・切り上げの範囲を守る。行の組、列の組を決まった順に走査し、
    スコアが LOCAL_SEARCH_EPS より大きく下がる最初の入れ替えを採る。動きがなくなるまで繰り返す。
    """
    k = k.copy()
    n_rows, n_cols = k.shape
    totals = np.einsum("ijg,ij->g", coef, k)
    for _ in range(max_passes):
        moved = False
        for i in range(n_rows - 1):
            for j in range(i + 1, n_rows):
                up_i, dn_i = k[i] < hi[i], k[i] > lo[i]
                up_j, dn_j = k[j] < hi[j], k[j] > lo[j]
                ok = up_i[:, None] & dn_i[None, :] & dn_j[:, None] & up_j[None, :]
                if not ok.any():
                    continue
                d = coef[i] - coef[j]  # (列, 群)
                new_totals = totals + d[:, None, :] - d[None, :, :]
                delta = (((new_totals - exp) ** 2 - (totals - exp) ** 2) / exp).sum(axis=-1)
                hits = np.flatnonzero(np.where(ok, delta, np.inf) < -LOCAL_SEARCH_EPS)
                if hits.size == 0:
                    continue
                a, b = divmod(int(hits[0]), n_cols)
                k[i, a] += 1
                k[i, b] -= 1
                k[j, a] -= 1
                k[j, b] += 1
                totals = new_totals[a, b]
                moved = True
        if not moved:
            break
    return k


def controlled_rounding(
    expected: np.ndarray,
    row_sums: Sequence[int],
    col_sums: Sequence[int],
    rng: np.random.Generator | None = None,
    tries: int = 1,
    groups: Sequence[Group] | None = None,
) -> np.ndarray:
    """制御丸め。行の合計と列の合計を両方守ったまま、各要素を切り捨て／切り上げの整数にする。

    切り捨てたあと、端数の大きい順に1ずつ足し、行と列の不足が残れば増加路（最大流）で直す。
    rng・groups を渡すと、足す順を乱数で変えた丸めを tries 通り作り、各 group（対象の行の印・列・期待件数）の
    合計が期待件数に最も近いものを選び、そのあと 2×2 の入れ替えで局所探索する。
    どの段階でも行・列の合計と、切り捨て／切り上げの範囲は守る。
    """
    e = np.asarray(expected, dtype=float)
    base = np.floor(e + 1e-9).astype(int)
    frac = np.clip(e - base, 0.0, None)
    frac[frac < 1e-9] = 0.0
    need_r = np.asarray(row_sums, dtype=int) - base.sum(axis=1)
    need_c = np.asarray(col_sums, dtype=int) - base.sum(axis=0)
    if (need_r < 0).any() or (need_c < 0).any() or need_r.sum() != need_c.sum():
        raise ValueError("制御丸めの合計が合いません")
    candidates = [_round_once(frac, need_r, need_c, -frac)]
    out = base + candidates[0]
    if rng is not None and groups and (frac > 0).any():
        coef, exp = _group_coef(groups, *e.shape)
        for _ in range(tries - 1):
            candidates.append(_round_once(frac, need_r, need_c, rng.random(frac.shape) - frac))
        out = min((base + c for c in candidates), key=lambda k: _group_score(k, coef, exp))
        out = _local_search(out, base, base + (frac > 0), coef, exp)
    if (out.sum(axis=1) != np.asarray(row_sums)).any() or (out.sum(axis=0) != np.asarray(col_sums)).any():
        raise ValueError("制御丸めの合計が合いません")
    return out


def allocate(
    rng: np.random.Generator,
    counts: Sequence[int],
    order: Sequence[int],
    weights: np.ndarray | None = None,
) -> np.ndarray:
    """行を分類に割り付ける。件数は counts のとおりにぴたりと合う。

    order の順に、残っている行から重み付き非復元抽出で選ぶ。最後の分類は残り全部。
    weights は (行数, 分類数)。None なら全部同じ重み。
    """
    n = int(sum(counts))
    if weights is not None and not np.isfinite(weights).all():
        raise ValueError("重みが有限ではありません")
    out = np.full(n, -1, dtype=int)
    remaining = np.arange(n)
    for c in order[:-1]:
        w = np.ones(len(remaining)) if weights is None else weights[remaining, c]
        picked = weighted_sample(rng, w, counts[c])
        out[remaining[picked]] = c
        remaining = np.delete(remaining, picked)
    last = order[-1]
    if len(remaining) != counts[last]:
        raise ValueError("割り付けの件数が合いません")
    if weights is not None and (weights[remaining, last] <= 0).any():
        raise ValueError("重み0の行が最後の分類に残りました")
    out[remaining] = last
    return out
