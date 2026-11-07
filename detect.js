'use strict';
/**
 * カードの4隅の自動検出
 *
 * 1. 粗検出: 縮小画像でエッジ(勾配)を求め、ハフ変換で直線を抽出。
 *    ほぼ平行な2本 × 2組 から作れる四角形のうち、4辺がエッジに沿っていて
 *    縦横比がカードに近いものを選ぶ。
 * 2. 精密化: 元解像度で各辺の中央部(丸い角を避ける)のエッジ点を集めて
 *    直線を当てはめ(外れ値除去つき)、隣り合う辺の交点を隅とする。
 */
const CardDetect = (() => {
  const DEG = Math.PI / 180;
  const SMALL_MAX = 800;

  // ---------------------------------------------------------------------------
  // 縮小・平滑化・勾配
  // ---------------------------------------------------------------------------
  function downscale(srcCanvas, maxSide) {
    const k = Math.min(1, maxSide / Math.max(srcCanvas.width, srcCanvas.height));
    const c = document.createElement('canvas');
    c.width = Math.max(1, Math.round(srcCanvas.width * k));
    c.height = Math.max(1, Math.round(srcCanvas.height * k));
    const ctx = c.getContext('2d');
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(srcCanvas, 0, 0, c.width, c.height);
    return ctx.getImageData(0, 0, c.width, c.height);
  }

  /** 3チャンネルを [1,4,6,4,1] で平滑化した Float32Array を返す */
  function blurChannels(img) {
    const { width: W, height: H, data } = img;
    const n = W * H;
    const kern = [1, 4, 6, 4, 1];
    const chans = [];
    for (let ch = 0; ch < 3; ch++) {
      const a = new Float32Array(n);
      for (let i = 0; i < n; i++) a[i] = data[i * 4 + ch];
      const tmp = new Float32Array(n);
      for (let y = 0; y < H; y++) {
        for (let x = 0; x < W; x++) {
          let s = 0;
          for (let j = -2; j <= 2; j++) {
            const xx = Math.min(W - 1, Math.max(0, x + j));
            s += a[y * W + xx] * kern[j + 2];
          }
          tmp[y * W + x] = s / 16;
        }
      }
      for (let y = 0; y < H; y++) {
        for (let x = 0; x < W; x++) {
          let s = 0;
          for (let j = -2; j <= 2; j++) {
            const yy = Math.min(H - 1, Math.max(0, y + j));
            s += tmp[yy * W + x] * kern[j + 2];
          }
          a[y * W + x] = s / 16;
        }
      }
      chans.push(a);
    }
    return chans;
  }

  /** 色チャンネルごとの Sobel のうち最大のものを採用した勾配 */
  function gradients(img) {
    const W = img.width;
    const H = img.height;
    const chans = blurChannels(img);
    const mag = new Float32Array(W * H);
    const gxA = new Float32Array(W * H);
    const gyA = new Float32Array(W * H);
    for (let y = 2; y < H - 2; y++) {
      for (let x = 2; x < W - 2; x++) {
        const i = y * W + x;
        let best = 0;
        let bgx = 0;
        let bgy = 0;
        for (let ch = 0; ch < 3; ch++) {
          const a = chans[ch];
          const gx = (a[i - W + 1] + 2 * a[i + 1] + a[i + W + 1]) - (a[i - W - 1] + 2 * a[i - 1] + a[i + W - 1]);
          const gy = (a[i + W - 1] + 2 * a[i + W] + a[i + W + 1]) - (a[i - W - 1] + 2 * a[i - W] + a[i - W + 1]);
          const m = gx * gx + gy * gy;
          if (m > best) { best = m; bgx = gx; bgy = gy; }
        }
        mag[i] = Math.sqrt(best) / 4;
        gxA[i] = bgx;
        gyA[i] = bgy;
      }
    }
    // 強さの分布から閾値を決める
    const sample = [];
    const stride = Math.max(1, Math.floor((W * H) / 40000));
    for (let i = 0; i < W * H; i += stride) sample.push(mag[i]);
    sample.sort((a, b) => a - b);
    const p99 = sample[Math.floor(sample.length * 0.99)] || 1;
    const T = Math.max(6, p99 * 0.12);
    return { W, H, mag, gx: gxA, gy: gyA, T, p99 };
  }

  /** 勾配方向の非極大抑制で細線化したエッジ画素を返す(g.edge にマスクも保存) */
  function edgePixels(g) {
    const { W, H, mag, gx, gy, T } = g;
    const out = [];
    const edge = new Uint8Array(W * H);
    // 画像の外周付近(写真の端・周辺減光など)は使わない
    const mg = Math.max(4, Math.round(Math.max(W, H) * 0.006));
    for (let y = mg; y < H - mg; y++) {
      for (let x = mg; x < W - mg; x++) {
        const i = y * W + x;
        const m = mag[i];
        if (m < T) continue;
        const ax = Math.abs(gx[i]);
        const ay = Math.abs(gy[i]);
        let n1;
        let n2;
        if (ax > ay * 2.414) { n1 = mag[i - 1]; n2 = mag[i + 1]; } else if (ay > ax * 2.414) { n1 = mag[i - W]; n2 = mag[i + W]; } else if ((gx[i] > 0) === (gy[i] > 0)) { n1 = mag[i - W - 1]; n2 = mag[i + W + 1]; } else { n1 = mag[i - W + 1]; n2 = mag[i + W - 1]; }
        if (m >= n1 && m >= n2) { out.push(i); edge[i] = 1; }
      }
    }
    g.edge = edge;
    return out;
  }

  // ---------------------------------------------------------------------------
  // ハフ変換
  // ---------------------------------------------------------------------------
  const N_THETA = 360; // 0.5°刻み
  const COS = new Float64Array(N_THETA);
  const SIN = new Float64Array(N_THETA);
  for (let t = 0; t < N_THETA; t++) {
    COS[t] = Math.cos((t * Math.PI) / N_THETA);
    SIN[t] = Math.sin((t * Math.PI) / N_THETA);
  }

  function angDiff(a, b) {
    let d = Math.abs(a - b) % Math.PI;
    return Math.min(d, Math.PI - d);
  }

  function hough(g, pixels) {
    const { W, H, mag, gx, gy, p99 } = g;
    const diag = Math.ceil(Math.hypot(W, H));
    const nRho = diag * 2 + 1;
    const acc = new Float32Array(N_THETA * nRho);
    const cap = p99;
    for (const i of pixels) {
      const x = (i % W) + 0.5;
      const y = Math.floor(i / W) + 0.5;
      let th = Math.atan2(gy[i], gx[i]);
      if (th < 0) th += Math.PI;
      const ti = Math.round((th / Math.PI) * N_THETA);
      const w = Math.min(mag[i], cap);
      for (let d = -4; d <= 4; d++) {
        const t = (ti + d + N_THETA) % N_THETA;
        const r = Math.round(x * COS[t] + y * SIN[t]) + diag;
        acc[t * nRho + r] += w;
      }
    }

    let max = 0;
    for (let i = 0; i < acc.length; i++) if (acc[i] > max) max = acc[i];
    const thr = max * 0.12;
    const peaks = [];
    for (let t = 0; t < N_THETA; t++) {
      for (let r = 2; r < nRho - 2; r++) {
        const v = acc[t * nRho + r];
        if (v < thr) continue;
        let isMax = true;
        for (let dt = -3; dt <= 3 && isMax; dt++) {
          const tt = (t + dt + N_THETA) % N_THETA;
          for (let dr = -3; dr <= 3; dr++) {
            if (!dt && !dr) continue;
            const o = acc[tt * nRho + r + dr];
            if (o > v || (o === v && (dt < 0 || (dt === 0 && dr < 0)))) { isMax = false; break; }
          }
        }
        if (isMax) peaks.push({ t, r, v });
      }
    }
    peaks.sort((a, b) => b.v - a.v);

    const lines = [];
    for (const p of peaks) {
      const th = (p.t * Math.PI) / N_THETA;
      const rho = p.r - diag;
      const dup = lines.some((l) => {
        const d = Math.abs(th - l.th);
        if (d < 2 * DEG) return Math.abs(rho - l.rho) < 3;
        if (Math.PI - d < 2 * DEG) return Math.abs(rho + l.rho) < 3;
        return false;
      });
      if (dup) continue;
      lines.push({ th, rho, c: Math.cos(th), s: Math.sin(th), v: p.v });
      if (lines.length >= 50) break;
    }
    return lines;
  }

  function intersect(a, b) {
    const det = a.c * b.s - a.s * b.c;
    if (Math.abs(det) < 1e-9) return null;
    return [(a.rho * b.s - a.s * b.rho) / det, (a.c * b.rho - a.rho * b.c) / det];
  }

  // ---------------------------------------------------------------------------
  // 四角形の候補評価
  // ---------------------------------------------------------------------------
  /**
   * 辺 P→Q 上の t∈ts の位置に、辺と垂直な向きの強いエッジがどれだけあるか(0〜1)。
   * 画像外の位置は数えない。
   */
  function supportAt(g, P, Q, ts) {
    const { W, H, edge, gx, gy } = g;
    const dx = Q[0] - P[0];
    const dy = Q[1] - P[1];
    const len = Math.hypot(dx, dy);
    if (len < 1) return 0;
    const nx = -dy / len;
    const ny = dx / len;
    const cosTol = Math.cos(25 * DEG);
    let hits = 0;
    let count = 0;
    for (const t of ts) {
      const bx = P[0] + dx * t;
      const by = P[1] + dy * t;
      if (bx < 4 || by < 4 || bx >= W - 4 || by >= H - 4) continue;
      count++;
      for (let o = -2; o <= 2; o++) {
        const x = Math.floor(bx + nx * o);
        const y = Math.floor(by + ny * o);
        const i = y * W + x;
        if (!edge[i]) continue;
        const gl = Math.hypot(gx[i], gy[i]) || 1;
        if (Math.abs((gx[i] * nx + gy[i] * ny) / gl) >= cosTol) { hits++; break; }
      }
    }
    return count ? hits / count : 0;
  }

  const range = (a, b, n) => Array.from({ length: n }, (_, k) => a + ((b - a) * k) / (n - 1));
  const T_SIDE = range(0.07, 0.93, 40);
  // 角の先(辺の延長上)。本物のカードの辺はここで途切れる
  const T_EXT = [...range(-0.3, -0.05, 10), ...range(1.05, 1.3, 10)];

  /** 辺に沿ったエッジの割合 */
  function sideSupport(g, P, Q) {
    return supportAt(g, P, Q, T_SIDE);
  }

  /** 辺 P→Q 上の t∈ts の位置での、辺に垂直な方向の勾配の強さの平均 */
  function normalGradient(g, P, Q, ts) {
    const { W, H, gx, gy } = g;
    const dx = Q[0] - P[0];
    const dy = Q[1] - P[1];
    const len = Math.hypot(dx, dy);
    if (len < 1) return 0;
    const nx = -dy / len;
    const ny = dx / len;
    let sum = 0;
    let count = 0;
    for (const t of ts) {
      const bx = P[0] + dx * t;
      const by = P[1] + dy * t;
      if (bx < 4 || by < 4 || bx >= W - 4 || by >= H - 4) continue;
      count++;
      let best = 0;
      for (let o = -2; o <= 2; o++) {
        const i = Math.floor(by + ny * o) * W + Math.floor(bx + nx * o);
        const v = Math.abs(gx[i] * nx + gy[i] * ny);
        if (v > best) best = v;
      }
      sum += best;
    }
    return count ? sum / count : 0;
  }

  /**
   * 角の先へ延長した部分のエッジの強さ ÷ 辺の上のエッジの強さ(0〜1)。
   * カードの辺は角で途切れるので小さく、背景の模様の直線は延長上も同じ強さなので大きくなる。
   */
  function extensionRatio(g, P, Q) {
    const onSide = normalGradient(g, P, Q, T_SIDE);
    if (onSide <= 0) return 1;
    return Math.min(1, normalGradient(g, P, Q, T_EXT) / onSide);
  }

  function polyArea(q) {
    let a = 0;
    for (let i = 0; i < 4; i++) {
      const p = q[i];
      const n = q[(i + 1) % 4];
      a += p[0] * n[1] - n[0] * p[1];
    }
    return Math.abs(a) / 2;
  }

  /**
   * 遠近で歪んだ四角形が、実物では どんな縦横比(短辺/長辺)の長方形かを推定する
   * (Zhang & He, "Whiteboard scanning and image enhancement" の方法。主点は画像中心と仮定)。
   * 焦点距離は4隅から推定できるが、カードを一方向にだけ傾けた場合などは推定できないため、
   * ありうる焦点距離の範囲で計算し、target に最も近い値を返す。
   */
  function rectAspect(q, W, H, target, raw = false) {
    const u0 = W / 2;
    const v0 = H / 2;
    const m = (p) => [p[0] - u0, p[1] - v0, 1];
    const m1 = m(q[0]);
    const m2 = m(q[1]);
    const m3 = m(q[3]);
    const m4 = m(q[2]);
    const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
    const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    const dist = Geo.dist;
    const s1 = (dist(q[0], q[1]) + dist(q[3], q[2])) / 2;
    const s2 = (dist(q[0], q[3]) + dist(q[1], q[2])) / 2;
    // 上辺の長さ / 左辺の長さ(見かけ)
    const naiveRaw = s1 / s2;
    const naive = raw ? naiveRaw : Math.min(s1, s2) / Math.max(s1, s2);

    const c14 = cross(m1, m4);
    const k2 = dot(c14, m3) / dot(cross(m2, m4), m3);
    const k3 = dot(c14, m2) / dot(cross(m3, m4), m2);
    if (!Number.isFinite(k2) || !Number.isFinite(k3)) return naive;
    const n2 = [k2 * m2[0] - m1[0], k2 * m2[1] - m1[1], k2 * m2[2] - m1[2]];
    const n3 = [k3 * m3[0] - m1[0], k3 * m3[1] - m1[1], k3 * m3[2] - m1[2]];
    const ratio = (f2) => {
      const w2 = (n2[0] ** 2 + n2[1] ** 2) / f2 + n2[2] ** 2;
      const h2 = (n3[0] ** 2 + n3[1] ** 2) / f2 + n3[2] ** 2;
      const r = Math.sqrt(w2 / h2);
      return Number.isFinite(r) && r > 0 ? r : null;
    };
    const norm = (r) => Math.min(r, 1 / r);

    const maxDim = Math.max(W, H);
    const fs = [0.6, 0.8, 1, 1.25, 1.6, 2, 2.6, 3.5].map((k) => (k * maxDim) ** 2);
    const den = n2[2] * n3[2];
    if (Math.abs(den) > 1e-9) {
      const f2 = -(n2[0] * n3[0] + n2[1] * n3[1]) / den;
      if (f2 > (maxDim * 0.3) ** 2 && f2 < (maxDim * 6) ** 2) fs.push(f2);
    }
    const rs = fs.map(ratio).filter((r) => r !== null);
    if (!rs.length) return naive;
    let best = rs[rs.length - 1];
    if (target !== undefined) {
      best = rs.reduce((b, r) => (Math.abs(norm(r) - target) < Math.abs(norm(b) - target) ? r : b));
    }
    return raw ? best : norm(best);
  }

  /**
   * 4隅(左上・右上・右下・左下の順)が、実物で横長(上辺の方が長い)かどうか。
   * 斜めから撮ると見かけの縦横が逆転することがあるため、遠近を考慮して判定する。
   */
  function isLandscape(q, W, H, aspect) {
    return rectAspect(q, W, H, aspect, true) > 1;
  }

  function bestQuad(g, lines, aspect, debug) {
    const { W, H } = g;
    const maxDim = Math.max(W, H);
    const cx = W / 2;
    const cy = H / 2;
    const pairs = [];
    for (let i = 0; i < lines.length; i++) {
      for (let j = i + 1; j < lines.length; j++) {
        const a = lines[i];
        const b = lines[j];
        if (angDiff(a.th, b.th) > 25 * DEG) continue;
        // a 上で画像中心に最も近い点から b までの距離
        const da = cx * a.c + cy * a.s - a.rho;
        const px = cx - da * a.c;
        const py = cy - da * a.s;
        const sep = Math.abs(px * b.c + py * b.s - b.rho);
        if (sep < maxDim * 0.04) continue;
        pairs.push({ a, b });
      }
    }

    const cands = [];
    const dist = Geo.dist;
    for (let i = 0; i < pairs.length; i++) {
      for (let j = i + 1; j < pairs.length; j++) {
        const P = pairs[i];
        const Q = pairs[j];
        if (angDiff(P.a.th, Q.a.th) < 50 * DEG) continue;
        const pts = [intersect(P.a, Q.a), intersect(P.a, Q.b), intersect(P.b, Q.b), intersect(P.b, Q.a)];
        if (pts.some((p) => !p)) continue;
        if (pts.some(([x, y]) => x < -0.05 * W || y < -0.05 * H || x > 1.05 * W || y > 1.05 * H)) continue;
        const q = Geo.orderCorners(pts);
        if (!Geo.isConvex(q)) continue;
        const area = polyArea(q);
        if (area < W * H * 0.008) continue;
        const s1 = (dist(q[0], q[1]) + dist(q[3], q[2])) / 2;
        const s2 = (dist(q[0], q[3]) + dist(q[1], q[2])) / 2;
        if (Math.min(s1, s2) / Math.max(s1, s2) < 0.3) continue;
        const r = rectAspect(q, W, H, aspect);
        const sup = [0, 1, 2, 3].map((k) => {
          const a = q[k];
          const b = q[(k + 1) % 4];
          const s0 = sideSupport(g, a, b);
          return s0 < 0.5 ? s0 : s0 * (1 - 0.7 * extensionRatio(g, a, b) ** 2);
        });
        const minS = Math.min(...sup);
        if (minS < 0.4) continue;
        const geo = Math.sqrt(Math.sqrt(sup[0] * sup[1] * sup[2] * sup[3]));
        const aspectOk = Math.abs(r - aspect) < 0.1;
        const aspectScore = Math.exp(-(((r - aspect) / 0.25) ** 2));
        const support = geo * minS;
        cands.push({ q, area, support, aspectOk, score: support * aspectScore });
      }
    }
    if (debug) debug.push(...cands);
    if (!cands.length) return null;
    // 縦横比がカードに近い候補のうち、辺の一致度がほぼ最高のものの中で最も大きいもの
    // (= ふちの内側の線ではなくカードの外形) を選ぶ
    const plausible = cands.filter((c) => c.aspectOk);
    if (plausible.length) {
      const bestSup = Math.max(...plausible.map((c) => c.support));
      const good = plausible.filter((c) => c.support >= bestSup * 0.9);
      good.sort((a, b) => b.area - a.area);
      return good[0];
    }
    cands.sort((a, b) => b.score - a.score);
    return cands[0];
  }

  // ---------------------------------------------------------------------------
  // 元解像度での精密化
  // ---------------------------------------------------------------------------
  function sampler(img) {
    const { width: W, height: H, data: d } = img;
    return (x, y, out) => {
      x -= 0.5;
      y -= 0.5;
      if (x < 0) x = 0; if (y < 0) y = 0;
      if (x > W - 1) x = W - 1; if (y > H - 1) y = H - 1;
      const x0 = Math.floor(x);
      const y0 = Math.floor(y);
      const x1 = Math.min(W - 1, x0 + 1);
      const y1 = Math.min(H - 1, y0 + 1);
      const fx = x - x0;
      const fy = y - y0;
      const i00 = (y0 * W + x0) * 4;
      const i10 = (y0 * W + x1) * 4;
      const i01 = (y1 * W + x0) * 4;
      const i11 = (y1 * W + x1) * 4;
      for (let c = 0; c < 3; c++) {
        out[c] += (d[i00 + c] * (1 - fx) + d[i10 + c] * fx) * (1 - fy) + (d[i01 + c] * (1 - fx) + d[i11 + c] * fx) * fy;
      }
    };
  }

  /** 点群に直線を当てはめる(全最小二乗 + 外れ値除去)。{p:[x,y], d:[dx,dy]} */
  function fitLine(pts) {
    let use = pts;
    let line = null;
    for (let iter = 0; iter < 4; iter++) {
      if (use.length < 8) return null;
      let mx = 0;
      let my = 0;
      for (const [x, y] of use) { mx += x; my += y; }
      mx /= use.length;
      my /= use.length;
      let sxx = 0;
      let sxy = 0;
      let syy = 0;
      for (const [x, y] of use) {
        sxx += (x - mx) ** 2;
        sxy += (x - mx) * (y - my);
        syy += (y - my) ** 2;
      }
      const th = 0.5 * Math.atan2(2 * sxy, sxx - syy);
      line = { p: [mx, my], d: [Math.cos(th), Math.sin(th)] };
      const res = (q) => Math.abs((q[0] - mx) * -line.d[1] + (q[1] - my) * line.d[0]);
      const rs = pts.map(res).sort((a, b) => a - b);
      const med = rs[Math.floor(rs.length / 2)];
      const lim = Math.max(1.0, med * 2.5);
      const next = pts.filter((q) => res(q) <= lim);
      if (next.length === use.length) break;
      use = next;
    }
    if (use.length < pts.length * 0.4) return null;
    return line;
  }

  function refineSide(sample, P, Q, w) {
    const dx = Q[0] - P[0];
    const dy = Q[1] - P[1];
    const len = Math.hypot(dx, dy);
    if (len < 20) return null;
    const tx = dx / len;
    const ty = dy / len;
    const nx = -ty;
    const ny = tx;
    const N = Math.round(Math.min(120, Math.max(20, len / 8)));
    const step = 0.5;
    const nS = Math.floor((2 * w) / step) + 1;
    const prof = new Array(nS);
    const col = [0, 0, 0];
    const pts = [];
    for (let k = 0; k < N; k++) {
      const t = 0.12 + (0.76 * k) / (N - 1);
      const bx = P[0] + dx * t;
      const by = P[1] + dy * t;
      for (let s = 0; s < nS; s++) {
        const o = -w + s * step;
        col[0] = 0; col[1] = 0; col[2] = 0;
        for (let u = -2; u <= 2; u += 2) sample(bx + nx * o + tx * u, by + ny * o + ty * u, col);
        prof[s] = [col[0] / 3, col[1] / 3, col[2] / 3];
      }
      let best = 0;
      let bi = -1;
      const dif = new Float64Array(nS);
      for (let s = 2; s < nS - 2; s++) {
        const a = prof[s - 2];
        const b = prof[s + 2];
        dif[s] = Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]);
        if (dif[s] > best) { best = dif[s]; bi = s; }
      }
      if (bi < 3 || bi > nS - 4 || best < 24) continue;
      let off = 0;
      const den = dif[bi - 1] - 2 * dif[bi] + dif[bi + 1];
      if (den < 0) off = Math.max(-0.5, Math.min(0.5, (0.5 * (dif[bi - 1] - dif[bi + 1])) / den));
      const o = -w + (bi + off) * step;
      pts.push([bx + nx * o, by + ny * o]);
    }
    if (pts.length < N * 0.4) return null;
    return fitLine(pts);
  }

  function lineIntersect(a, b) {
    const det = a.d[0] * b.d[1] - a.d[1] * b.d[0];
    if (Math.abs(det) < 1e-9) return null;
    const t = ((b.p[0] - a.p[0]) * b.d[1] - (b.p[1] - a.p[1]) * b.d[0]) / det;
    return [a.p[0] + a.d[0] * t, a.p[1] + a.d[1] * t];
  }

  /**
   * 4隅の推定値を元画像のエッジに合わせて精密化する。
   * @param {ImageData} img 元画像
   * @param {number[][]} corners 4隅の推定値
   * @param {number} w 各辺の法線方向の探索幅(px)
   * @returns {number[][]|null}
   */
  function refine(img, corners, w) {
    const q = Geo.orderCorners(corners);
    if (!Geo.isConvex(q)) return null;
    const sample = sampler(img);
    const sides = [];
    for (let i = 0; i < 4; i++) {
      const s = refineSide(sample, q[i], q[(i + 1) % 4], w);
      if (!s) return null;
      sides.push(s);
    }
    const out = [];
    for (let i = 0; i < 4; i++) {
      const p = lineIntersect(sides[(i + 3) % 4], sides[i]);
      if (!p) return null;
      if (Geo.dist(p, q[i]) > w * 3 + 4) return null;
      out.push(p);
    }
    return Geo.isConvex(out) ? out : null;
  }

  /**
   * カードの4隅を検出する。
   * @param {HTMLCanvasElement} srcCanvas 元画像
   * @param {ImageData} srcData 元画像の ImageData
   * @param {number} aspect カードの 短辺/長辺
   * @returns {{corners:number[][], refined:boolean}|null}
   */
  function detect(srcCanvas, srcData, aspect = 63 / 88) {
    const small = downscale(srcCanvas, SMALL_MAX);
    const f = srcCanvas.width / small.width;
    const g = gradients(small);
    const lines = hough(g, edgePixels(g));
    const quad = bestQuad(g, lines, aspect);
    if (!quad) return null;
    const coarse = quad.q.map(([x, y]) => [x * f, y * f]);
    const refined = refine(srcData, coarse, Math.max(6, f * 2.5));
    return { corners: refined || coarse, refined: !!refined };
  }

  return { detect, refine, isLandscape, _internals: { downscale, gradients, edgePixels, hough, bestQuad, sideSupport, rectAspect } };
})();
