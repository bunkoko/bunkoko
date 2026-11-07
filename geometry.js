'use strict';
/**
 * 幾何計算・画像処理ユーティリティ
 *  - ホモグラフィ(射影変換)の計算
 *  - 射影変換による画像の補正(逆写像 + バイリニア補間)
 *  - 補正後画像からの直線エッジ検出
 */
const Geo = (() => {
  /** ガウスの消去法(部分ピボット選択)で Ax = b を解く */
  function solve(A, b) {
    const n = b.length;
    const M = A.map((row, i) => [...row, b[i]]);
    for (let c = 0; c < n; c++) {
      let p = c;
      for (let r = c + 1; r < n; r++) {
        if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
      }
      if (Math.abs(M[p][c]) < 1e-12) return null;
      [M[c], M[p]] = [M[p], M[c]];
      for (let r = 0; r < n; r++) {
        if (r === c) continue;
        const f = M[r][c] / M[c][c];
        if (f === 0) continue;
        for (let k = c; k <= n; k++) M[r][k] -= f * M[c][k];
      }
    }
    return M.map((row, i) => row[n] / row[i]);
  }

  /** 4点の対応 from[i] -> to[i] を満たすホモグラフィ H (3x3, 行優先, 長さ9) */
  function homography(from, to) {
    const A = [];
    const b = [];
    for (let i = 0; i < 4; i++) {
      const [x, y] = from[i];
      const [u, v] = to[i];
      A.push([x, y, 1, 0, 0, 0, -u * x, -u * y]);
      b.push(u);
      A.push([0, 0, 0, x, y, 1, -v * x, -v * y]);
      b.push(v);
    }
    const h = solve(A, b);
    if (!h) return null;
    return [...h, 1];
  }

  function apply(H, x, y) {
    const w = H[6] * x + H[7] * y + H[8];
    return [(H[0] * x + H[1] * y + H[2]) / w, (H[3] * x + H[4] * y + H[5]) / w];
  }

  /**
   * 射影変換で画像を補正する。
   * @param {ImageData} src 元画像
   * @param {number[]} H 出力座標 -> 元画像座標 のホモグラフィ
   * @param {number} W 出力幅
   * @param {number} Hh 出力高さ
   */
  function warp(src, H, W, Hh) {
    const out = new ImageData(W, Hh);
    const o = out.data;
    const s = src.data;
    const sw = src.width;
    const sh = src.height;
    let idx = 0;
    for (let j = 0; j < Hh; j++) {
      const v = j + 0.5;
      let nx = H[0] * 0.5 + H[1] * v + H[2];
      let ny = H[3] * 0.5 + H[4] * v + H[5];
      let nw = H[6] * 0.5 + H[7] * v + H[8];
      for (let i = 0; i < W; i++, idx += 4) {
        // ピクセル中心基準の座標に変換
        const x = nx / nw - 0.5;
        const y = ny / nw - 0.5;
        nx += H[0];
        ny += H[3];
        nw += H[6];
        if (x < -0.5 || y < -0.5 || x > sw - 0.5 || y > sh - 0.5) {
          o[idx] = 40; o[idx + 1] = 40; o[idx + 2] = 40; o[idx + 3] = 255;
          continue;
        }
        let x0 = Math.floor(x);
        let y0 = Math.floor(y);
        const fx = x - x0;
        const fy = y - y0;
        let x1 = x0 + 1;
        let y1 = y0 + 1;
        if (x0 < 0) x0 = 0;
        if (y0 < 0) y0 = 0;
        if (x1 > sw - 1) x1 = sw - 1;
        if (y1 > sh - 1) y1 = sh - 1;
        const i00 = (y0 * sw + x0) * 4;
        const i10 = (y0 * sw + x1) * 4;
        const i01 = (y1 * sw + x0) * 4;
        const i11 = (y1 * sw + x1) * 4;
        const w00 = (1 - fx) * (1 - fy);
        const w10 = fx * (1 - fy);
        const w01 = (1 - fx) * fy;
        const w11 = fx * fy;
        o[idx] = s[i00] * w00 + s[i10] * w10 + s[i01] * w01 + s[i11] * w11;
        o[idx + 1] = s[i00 + 1] * w00 + s[i10 + 1] * w10 + s[i01 + 1] * w01 + s[i11 + 1] * w11;
        o[idx + 2] = s[i00 + 2] * w00 + s[i10 + 2] * w10 + s[i01 + 2] * w01 + s[i11 + 2] * w11;
        o[idx + 3] = 255;
      }
    }
    return out;
  }

  /**
   * 軸に垂直な直線エッジを検出する。
   * axis='x' なら縦線(x座標)を、axis='y' なら横線(y座標)を探す。
   * [from, to] の範囲を探索し、[a0, a1] の範囲(直交方向)で勾配を積算する。
   * mode: 'max' = 最大のエッジ / 'first' = 小さい座標側から最初の強いエッジ / 'last' = 大きい座標側から最初の強いエッジ
   * 戻り値: ピクセル境界基準の連続座標(ピクセル i は [i, i+1] を占める)。検出できなければ null。
   */
  function findEdge(img, axis, from, to, a0, a1, mode) {
    const W = img.width;
    const Hh = img.height;
    const d = img.data;
    const lim = axis === 'x' ? W : Hh;
    const alim = axis === 'x' ? Hh : W;
    from = Math.max(2, Math.floor(from));
    to = Math.min(lim - 3, Math.ceil(to));
    a0 = Math.max(0, Math.floor(a0));
    a1 = Math.min(alim - 1, Math.ceil(a1));
    const n = to - from + 1;
    if (n < 5 || a1 <= a0) return null;

    const raw = new Float64Array(n);
    const step = Math.max(1, Math.floor((a1 - a0) / 500));
    let count = 0;
    for (let a = a0; a <= a1; a += step) {
      count++;
      for (let k = 0; k < n; k++) {
        const t = from + k;
        let i1;
        let i2;
        if (axis === 'x') {
          i1 = (a * W + t - 1) * 4;
          i2 = (a * W + t + 1) * 4;
        } else {
          i1 = ((t - 1) * W + a) * 4;
          i2 = ((t + 1) * W + a) * 4;
        }
        raw[k] += Math.abs(d[i1] - d[i2]) + Math.abs(d[i1 + 1] - d[i2 + 1]) + Math.abs(d[i1 + 2] - d[i2 + 2]);
      }
    }

    // 平滑化
    const p = new Float64Array(n);
    const kernel = [1, 2, 3, 2, 1];
    for (let k = 0; k < n; k++) {
      let sum = 0;
      let wsum = 0;
      for (let j = -2; j <= 2; j++) {
        const kk = k + j;
        if (kk < 0 || kk >= n) continue;
        sum += raw[kk] * kernel[j + 2];
        wsum += kernel[j + 2];
      }
      p[k] = sum / wsum;
    }

    let max = 0;
    let mi = -1;
    for (let k = 0; k < n; k++) {
      if (p[k] > max) { max = p[k]; mi = k; }
    }
    // 1行あたりの平均勾配が小さすぎる場合はエッジ無しとみなす
    if (mi < 0 || max / count < 8) return null;

    let pick = mi;
    if (mode === 'first' || mode === 'last') {
      const thr = max * 0.45;
      const isPeak = (k) => (k === 0 || p[k] >= p[k - 1]) && (k === n - 1 || p[k] >= p[k + 1]);
      if (mode === 'first') {
        for (let k = 0; k < n; k++) {
          if (p[k] >= thr && isPeak(k)) { pick = k; break; }
        }
      } else {
        for (let k = n - 1; k >= 0; k--) {
          if (p[k] >= thr && isPeak(k)) { pick = k; break; }
        }
      }
    }

    // 放物線フィッティングでサブピクセル精度に
    let off = 0;
    if (pick > 0 && pick < n - 1) {
      const y0 = p[pick - 1];
      const y1 = p[pick];
      const y2 = p[pick + 1];
      const den = y0 - 2 * y1 + y2;
      if (den < 0) off = Math.max(-0.5, Math.min(0.5, 0.5 * (y0 - y2) / den));
    }
    return from + pick + off + 0.5;
  }

  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);

  /** 4点を 左上・右上・右下・左下 の順(時計回り)に並べ替える */
  function orderCorners(pts) {
    const cx = pts.reduce((s, p) => s + p[0], 0) / 4;
    const cy = pts.reduce((s, p) => s + p[1], 0) / 4;
    const s = pts.slice().sort(
      (a, b) => Math.atan2(a[1] - cy, a[0] - cx) - Math.atan2(b[1] - cy, b[0] - cx),
    );
    let k = 0;
    for (let i = 1; i < 4; i++) {
      if (s[i][0] + s[i][1] < s[k][0] + s[k][1]) k = i;
    }
    return [0, 1, 2, 3].map((i) => s[(i + k) % 4].slice());
  }

  function isConvex(q) {
    let sign = 0;
    for (let i = 0; i < 4; i++) {
      const a = q[i];
      const b = q[(i + 1) % 4];
      const c = q[(i + 2) % 4];
      const cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]);
      if (Math.abs(cross) < 1e-9) return false;
      const sg = Math.sign(cross);
      if (sign === 0) sign = sg;
      else if (sg !== sign) return false;
    }
    return true;
  }

  return { solve, homography, apply, warp, findEdge, orderCorners, isConvex, dist };
})();

if (typeof module !== 'undefined') module.exports = Geo;
