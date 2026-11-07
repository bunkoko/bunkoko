'use strict';
(() => {
  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------------------
  // 設定
  // ---------------------------------------------------------------------------
  const CARD_SIZES = {
    std: { w: 63, h: 88 },
    ygo: { w: 59, h: 86 },
  };

  // PSA のセンタリング基準（大きい側の割合 % の上限）
  const PSA = {
    front: [[10, 55], [9, 60], [8, 65], [7, 70], [6, 80], [5, 85], [4, 85], [3, 90]],
    back: [[10, 75], [9, 90], [8, 90], [7, 90], [6, 90], [5, 90], [4, 90], [3, 90]],
  };

  const LINES = {
    oL: { label: '外側の縁・左', axis: 'x', outer: true },
    oR: { label: '外側の縁・右', axis: 'x', outer: true },
    oT: { label: '外側の縁・上', axis: 'y', outer: true },
    oB: { label: '外側の縁・下', axis: 'y', outer: true },
    iL: { label: 'ふちの内側・左', axis: 'x', outer: false },
    iR: { label: 'ふちの内側・右', axis: 'x', outer: false },
    iT: { label: 'ふちの内側・上', axis: 'y', outer: false },
    iB: { label: 'ふちの内側・下', axis: 'y', outer: false },
  };

  const COLOR = { outer: '#00e5ff', inner: '#ff3d9a', sel: '#ffe600', shadow: 'rgba(0,0,0,0.75)' };
  const MAX_SRC = 4000; // 読み込み画像の長辺の上限(px)
  const PAD_RATIO = 0.06; // 補正画像のカード周囲の余白(カード幅比)

  const state = {
    src: null, // 元画像(縮小済み)の canvas
    srcData: null, // その ImageData
    corners: null, // [[x,y] x4] 元画像座標
    activeCorner: 0,
    rot: 0, // 90°回転の回数
    rect: null, // 補正後画像の情報
    lines: null, // 線の位置(補正画像の座標)
    selLine: 'iL',
    side: 'front',
    size: 'std',
  };

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  // ---------------------------------------------------------------------------
  // Canvas ビューア（表示のフィット・ドラッグ・拡大鏡）
  // ---------------------------------------------------------------------------
  class Viewer {
    constructor(canvas, opts) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.opts = opts;
      this.T = { s: 1, ox: 0, oy: 0 };
      this.cssW = 0;
      this.cssH = 0;
      this.dpr = 1;
      this.drag = null;
      this.raf = 0;
      canvas.addEventListener('pointerdown', (e) => this.onDown(e));
      canvas.addEventListener('pointermove', (e) => this.onMove(e));
      canvas.addEventListener('pointerup', (e) => this.onUp(e));
      canvas.addEventListener('pointercancel', (e) => this.onUp(e));
    }

    fit() {
      const size = this.opts.size();
      if (!size) return;
      const [iw, ih] = size;
      const wrap = this.canvas.parentElement;
      const maxW = wrap.clientWidth;
      if (!maxW) return;
      const maxH = Math.max(320, window.innerHeight * 0.75);
      const s = Math.min(maxW / iw, maxH / ih);
      this.cssW = Math.max(1, Math.round(iw * s));
      this.cssH = Math.max(1, Math.round(ih * s));
      this.dpr = window.devicePixelRatio || 1;
      this.canvas.style.width = `${this.cssW}px`;
      this.canvas.style.height = `${this.cssH}px`;
      this.canvas.width = Math.round(this.cssW * this.dpr);
      this.canvas.height = Math.round(this.cssH * this.dpr);
      this.T = { s, ox: 0, oy: 0 };
      this.render(true);
    }

    pointer(e) {
      const r = this.canvas.getBoundingClientRect();
      const cx = e.clientX - r.left;
      const cy = e.clientY - r.top;
      return { cx, cy, x: cx / this.T.s, y: cy / this.T.s };
    }

    onDown(e) {
      const p = this.pointer(e);
      const hit = this.opts.hit(p, this.T, e.pointerType);
      if (!hit) return;
      e.preventDefault();
      this.canvas.setPointerCapture(e.pointerId);
      this.drag = { id: e.pointerId, target: hit.target, dx: hit.ax - p.x, dy: hit.ay - p.y, x: hit.ax, y: hit.ay };
      if (this.opts.select) this.opts.select(hit.target);
      this.render();
    }

    onMove(e) {
      if (!this.drag || e.pointerId !== this.drag.id) {
        if (e.pointerType === 'mouse') {
          const hit = this.opts.hit(this.pointer(e), this.T, e.pointerType);
          this.canvas.style.cursor = hit ? (hit.cursor || 'grab') : 'default';
        }
        return;
      }
      e.preventDefault();
      const p = this.pointer(e);
      this.drag.x = p.x + this.drag.dx;
      this.drag.y = p.y + this.drag.dy;
      this.opts.drag(this.drag.target, this.drag.x, this.drag.y);
      this.render();
    }

    onUp(e) {
      if (!this.drag || e.pointerId !== this.drag.id) return;
      this.drag = null;
      if (this.opts.end) this.opts.end();
      this.render();
    }

    render(now) {
      if (now) {
        cancelAnimationFrame(this.raf);
        this.raf = 0;
        this.paint();
        return;
      }
      if (this.raf) return;
      this.raf = requestAnimationFrame(() => {
        this.raf = 0;
        this.paint();
      });
    }

    paint() {
      if (!this.cssW) return;
      const ctx = this.ctx;
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.cssW, this.cssH);
      this.opts.draw(ctx, this.T, { loupe: false, view: this });
      if (this.drag) this.paintLoupe(this.drag.x, this.drag.y);
    }

    paintLoupe(px, py) {
      const ctx = this.ctx;
      const R = Math.round(clamp(Math.min(this.cssW, this.cssH) * 0.2, 50, 90));
      const sx = px * this.T.s;
      const sy = py * this.T.s;
      // 指で隠れないよう、ドラッグ位置と反対側の上部に表示
      let cx = sx < this.cssW / 2 ? this.cssW - R - 10 : R + 10;
      let cy = R + 10;
      if (sy < R * 2 + 30 && Math.abs(sx - cx) < R * 2) cy = this.cssH - R - 10;
      const s = Math.min(Math.max(this.T.s * 4, 1.5), this.T.s * 12);
      const LT = { s, ox: cx - px * s, oy: cy - py * s };

      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.clip();
      ctx.fillStyle = '#111';
      ctx.fillRect(cx - R, cy - R, R * 2, R * 2);
      this.opts.draw(ctx, LT, { loupe: true, view: this });
      // 十字線
      ctx.strokeStyle = 'rgba(255,255,255,0.9)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(cx - 8, cy); ctx.lineTo(cx - 3, cy);
      ctx.moveTo(cx + 3, cy); ctx.lineTo(cx + 8, cy);
      ctx.moveTo(cx, cy - 8); ctx.lineTo(cx, cy - 3);
      ctx.moveTo(cx, cy + 3); ctx.lineTo(cx, cy + 8);
      ctx.stroke();
      ctx.restore();

      ctx.lineWidth = 3;
      ctx.strokeStyle = '#fff';
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.stroke();
      ctx.lineWidth = 1;
      ctx.strokeStyle = 'rgba(0,0,0,0.6)';
      ctx.beginPath();
      ctx.arc(cx, cy, R + 1.5, 0, Math.PI * 2);
      ctx.stroke();
    }
  }

  // ---------------------------------------------------------------------------
  // 描画ヘルパ
  // ---------------------------------------------------------------------------
  function strokeLine(ctx, x1, y1, x2, y2, color, width, dash) {
    ctx.save();
    if (dash) ctx.setLineDash(dash);
    ctx.lineCap = 'butt';
    ctx.strokeStyle = COLOR.shadow;
    ctx.lineWidth = width + 2;
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
    ctx.restore();
  }

  function drawImage(ctx, img, T) {
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(img, T.ox, T.oy, img.width * T.s, img.height * T.s);
  }

  // ---------------------------------------------------------------------------
  // STEP 2: 4隅の指定
  // ---------------------------------------------------------------------------
  function drawCorners(ctx, T, { loupe, view }) {
    if (!state.src) return;
    drawImage(ctx, state.src, T);
    const q = state.corners.map(([x, y]) => [T.ox + x * T.s, T.oy + y * T.s]);

    if (!loupe) {
      // カード外を暗くする
      ctx.save();
      ctx.fillStyle = 'rgba(0,0,0,0.35)';
      ctx.beginPath();
      ctx.rect(0, 0, view.cssW, view.cssH);
      ctx.moveTo(q[0][0], q[0][1]);
      for (let i = 1; i < 4; i++) ctx.lineTo(q[i][0], q[i][1]);
      ctx.closePath();
      ctx.fill('evenodd');
      ctx.restore();
    }

    // 辺の延長線(点線)と辺
    for (let i = 0; i < 4; i++) {
      const a = q[i];
      const b = q[(i + 1) % 4];
      const dx = b[0] - a[0];
      const dy = b[1] - a[1];
      const e = 0.3;
      strokeLine(ctx, a[0] - dx * e, a[1] - dy * e, b[0] + dx * e, b[1] + dy * e, COLOR.outer, 1, [6, 4]);
    }

    if (!loupe) {
      q.forEach(([x, y], i) => {
        const active = i === state.activeCorner;
        ctx.save();
        ctx.beginPath();
        ctx.arc(x, y, 13, 0, Math.PI * 2);
        ctx.fillStyle = active ? 'rgba(255,230,0,0.18)' : 'rgba(0,229,255,0.15)';
        ctx.fill();
        ctx.lineWidth = 3;
        ctx.strokeStyle = COLOR.shadow;
        ctx.stroke();
        ctx.lineWidth = 2;
        ctx.strokeStyle = active ? COLOR.sel : COLOR.outer;
        ctx.stroke();
        ctx.fillStyle = active ? COLOR.sel : '#fff';
        ctx.beginPath();
        ctx.arc(x, y, 2, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      });
    }
  }

  function hitCorners(p, T, pointerType) {
    if (!state.corners) return null;
    const r = pointerType === 'mouse' ? 18 : 40;
    let best = -1;
    let bd = Infinity;
    state.corners.forEach(([x, y], i) => {
      const d = Math.hypot(x * T.s - p.cx, y * T.s - p.cy);
      if (d < bd) { bd = d; best = i; }
    });
    if (best < 0 || bd > r) return null;
    const [x, y] = state.corners[best];
    return { target: best, ax: x, ay: y, cursor: 'move' };
  }

  const cornerViewer = new Viewer($('cornerCanvas'), {
    size: () => (state.src ? [state.src.width, state.src.height] : null),
    draw: drawCorners,
    hit: hitCorners,
    select: (i) => { state.activeCorner = i; },
    drag: (i, x, y) => {
      state.corners[i] = [clamp(x, 0, state.src.width), clamp(y, 0, state.src.height)];
      markStale();
    },
  });

  function defaultCorners() {
    const W = state.src.width;
    const H = state.src.height;
    const size = CARD_SIZES[state.size];
    const ar = size.w / size.h;
    let h = H * 0.7;
    let w = h * ar;
    if (w > W * 0.7) { w = W * 0.7; h = w / ar; }
    const x0 = (W - w) / 2;
    const y0 = (H - h) / 2;
    return [[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h]];
  }

  function markStale() {
    if (state.rect) $('staleWarn').classList.remove('hidden');
  }

  // ---------------------------------------------------------------------------
  // 遠近補正
  // ---------------------------------------------------------------------------
  function buildRect() {
    const warnEl = $('cornerWarn');
    warnEl.classList.add('hidden');

    let q = Geo.orderCorners(state.corners);
    if (!Geo.isConvex(q)) {
      warnEl.textContent = '4隅の位置が不正です（四角形がねじれています）。ハンドルの位置を確認してください。';
      warnEl.classList.remove('hidden');
      return false;
    }
    const d = Geo.dist;
    // 横長に写っている場合は縦向きになるよう回転
    if (d(q[0], q[1]) + d(q[3], q[2]) > d(q[0], q[3]) + d(q[1], q[2])) q.push(q.shift());
    for (let i = 0; i < state.rot % 4; i++) q.push(q.shift());

    const size = CARD_SIZES[state.size];
    const estW = Math.max(
      d(q[0], q[1]), d(q[3], q[2]),
      Math.max(d(q[0], q[3]), d(q[1], q[2])) * size.w / size.h,
    );
    const cardW = Math.round(clamp(estW, 500, 1500));
    const cardH = Math.round(cardW * size.h / size.w);
    const pad = Math.round(cardW * PAD_RATIO);
    const W = cardW + pad * 2;
    const H = cardH + pad * 2;

    // 大幅に縮小する場合は、エイリアシングを避けるため元画像を先に縮小しておく
    let srcData = state.srcData;
    let k = 1;
    if (cardW / estW < 0.7) {
      k = Math.min(1, (cardW / estW) * 1.4);
      const c = document.createElement('canvas');
      c.width = Math.max(1, Math.round(state.src.width * k));
      c.height = Math.max(1, Math.round(state.src.height * k));
      const cx = c.getContext('2d');
      cx.imageSmoothingQuality = 'high';
      cx.drawImage(state.src, 0, 0, c.width, c.height);
      srcData = cx.getImageData(0, 0, c.width, c.height);
      k = c.width / state.src.width;
    }
    const srcQ = q.map(([x, y]) => [x * k, y * k]);
    const dstQ = [[pad, pad], [pad + cardW, pad], [pad + cardW, pad + cardH], [pad, pad + cardH]];
    const Hm = Geo.homography(dstQ, srcQ);
    if (!Hm) {
      warnEl.textContent = '遠近補正の計算に失敗しました。4隅の位置を確認してください。';
      warnEl.classList.remove('hidden');
      return false;
    }
    const data = Geo.warp(srcData, Hm, W, H);
    const canvas = document.createElement('canvas');
    canvas.width = W;
    canvas.height = H;
    canvas.getContext('2d').putImageData(data, 0, 0);
    state.rect = { canvas, data, W, H, pad, cardW, cardH };
    $('staleWarn').classList.add('hidden');
    return true;
  }

  // ---------------------------------------------------------------------------
  // STEP 3: 線の調整
  // ---------------------------------------------------------------------------
  function knobPos(key) {
    const r = state.rect;
    const info = LINES[key];
    const f = info.outer ? 0.32 : 0.68;
    return info.axis === 'x' ? [state.lines[key], r.H * f] : [r.W * f, state.lines[key]];
  }

  function drawMeasure(ctx, T, { loupe, lineWidth }) {
    const r = state.rect;
    const l = state.lines;
    if (!r || !l) return;
    drawImage(ctx, r.canvas, T);
    const X = (x) => T.ox + x * T.s;
    const Y = (y) => T.oy + y * T.s;
    const lw = lineWidth || 1.5;

    // ふちの領域を薄く着色
    ctx.save();
    ctx.fillStyle = 'rgba(255,61,154,0.13)';
    ctx.beginPath();
    ctx.rect(X(l.oL), Y(l.oT), (l.oR - l.oL) * T.s, (l.oB - l.oT) * T.s);
    ctx.rect(X(l.iL), Y(l.iT), (l.iR - l.iL) * T.s, (l.iB - l.iT) * T.s);
    ctx.fill('evenodd');
    ctx.restore();

    const x0 = X(0);
    const x1 = X(r.W);
    const y0 = Y(0);
    const y1 = Y(r.H);
    // 選択中の線は最後に描く
    const keys = Object.keys(LINES).filter((k) => k !== state.selLine).concat(state.selLine);
    for (const key of keys) {
      const info = LINES[key];
      const sel = key === state.selLine && !lineWidth;
      const color = sel ? COLOR.sel : (info.outer ? COLOR.outer : COLOR.inner);
      if (info.axis === 'x') strokeLine(ctx, X(l[key]), y0, X(l[key]), y1, color, sel ? lw + 0.5 : lw);
      else strokeLine(ctx, x0, Y(l[key]), x1, Y(l[key]), color, sel ? lw + 0.5 : lw);
    }

    if (!loupe && !lineWidth) {
      for (const key of keys) {
        const info = LINES[key];
        const [kx, ky] = knobPos(key);
        const sel = key === state.selLine;
        ctx.save();
        ctx.beginPath();
        ctx.arc(X(kx), Y(ky), 8, 0, Math.PI * 2);
        ctx.fillStyle = sel ? COLOR.sel : (info.outer ? COLOR.outer : COLOR.inner);
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = COLOR.shadow;
        ctx.stroke();
        ctx.restore();
      }
    }
  }

  function hitMeasure(p, T, pointerType) {
    if (!state.lines) return null;
    const touch = pointerType !== 'mouse';
    const kr = touch ? 26 : 14;
    const lr = touch ? 16 : 7;
    let best = null;
    let bd = Infinity;
    for (const key of Object.keys(LINES)) {
      const [kx, ky] = knobPos(key);
      const d = Math.hypot(kx * T.s - p.cx, ky * T.s - p.cy);
      if (d < kr && d < bd) { bd = d; best = { target: key, ax: kx, ay: ky }; }
    }
    if (best) return { ...best, cursor: LINES[best.target].axis === 'x' ? 'ew-resize' : 'ns-resize' };
    for (const key of Object.keys(LINES)) {
      const info = LINES[key];
      const v = state.lines[key] * T.s;
      const d = Math.abs(info.axis === 'x' ? v - p.cx : v - p.cy);
      if (d < lr && d < bd) {
        bd = d;
        best = info.axis === 'x'
          ? { target: key, ax: state.lines[key], ay: p.y }
          : { target: key, ax: p.x, ay: state.lines[key] };
      }
    }
    if (best) return { ...best, cursor: LINES[best.target].axis === 'x' ? 'ew-resize' : 'ns-resize' };
    return null;
  }

  function setLine(key, v) {
    const r = state.rect;
    const max = LINES[key].axis === 'x' ? r.W : r.H;
    state.lines[key] = clamp(v, 0, max);
  }

  const measureViewer = new Viewer($('measureCanvas'), {
    size: () => (state.rect ? [state.rect.W, state.rect.H] : null),
    draw: drawMeasure,
    hit: hitMeasure,
    select: (key) => selectLine(key),
    drag: (key, x, y) => {
      setLine(key, LINES[key].axis === 'x' ? x : y);
      updateResults();
    },
  });

  function selectLine(key) {
    state.selLine = key;
    $('lineSelect').value = key;
    measureViewer.render();
  }

  function defaultLines() {
    const r = state.rect;
    const bx = r.cardW * 0.048;
    const by = r.cardH * 0.035;
    return {
      oL: r.pad, oR: r.pad + r.cardW, oT: r.pad, oB: r.pad + r.cardH,
      iL: r.pad + bx, iR: r.pad + r.cardW - bx, iT: r.pad + by, iB: r.pad + r.cardH - by,
    };
  }

  function autoDetect() {
    const r = state.rect;
    const img = r.data;
    const l = defaultLines();
    const { pad: p, cardW: cw, cardH: ch } = r;
    const win = cw * 0.035;
    const find = (axis, from, to, a0, a1, mode) => Geo.findEdge(img, axis, from, to, a0, a1, mode);

    // 外側の縁(カードと背景の境目)
    const ya = p + ch * 0.15;
    const yb = p + ch * 0.85;
    const xa = p + cw * 0.15;
    const xb = p + cw * 0.85;
    l.oL = find('x', p - win, p + win, ya, yb, 'max') ?? l.oL;
    l.oR = find('x', p + cw - win, p + cw + win, ya, yb, 'max') ?? l.oR;
    l.oT = find('y', p - win, p + win, xa, xb, 'max') ?? l.oT;
    l.oB = find('y', p + ch - win, p + ch + win, xa, xb, 'max') ?? l.oB;

    // ふちの内側(外側から見て最初に現れる強いエッジ)
    const w2 = l.oR - l.oL;
    const h2 = l.oB - l.oT;
    const ya2 = l.oT + h2 * 0.18;
    const yb2 = l.oB - h2 * 0.18;
    const xa2 = l.oL + w2 * 0.18;
    const xb2 = l.oR - w2 * 0.18;
    const def = defaultLines();
    l.iL = find('x', l.oL + w2 * 0.012, l.oL + w2 * 0.17, ya2, yb2, 'first') ?? (l.oL + (def.iL - def.oL));
    l.iR = find('x', l.oR - w2 * 0.17, l.oR - w2 * 0.012, ya2, yb2, 'last') ?? (l.oR - (def.oR - def.iR));
    l.iT = find('y', l.oT + h2 * 0.01, l.oT + h2 * 0.14, xa2, xb2, 'first') ?? (l.oT + (def.iT - def.oT));
    l.iB = find('y', l.oB - h2 * 0.14, l.oB - h2 * 0.01, xa2, xb2, 'last') ?? (l.oB - (def.oB - def.iB));
    state.lines = l;
  }

  // ---------------------------------------------------------------------------
  // 計算と結果表示
  // ---------------------------------------------------------------------------
  function measure() {
    const l = state.lines;
    const size = CARD_SIZES[state.size];
    const L = l.iL - l.oL;
    const R = l.oR - l.iR;
    const T = l.iT - l.oT;
    const B = l.oB - l.iB;
    const mmx = size.w / (l.oR - l.oL);
    const mmy = size.h / (l.oB - l.oT);
    const valid = L > 0 && R > 0 && T > 0 && B > 0 && l.oR > l.oL && l.oB > l.oT;
    const lr = (100 * L) / (L + R);
    const tb = (100 * T) / (T + B);
    const worst = Math.max(lr, 100 - lr, tb, 100 - tb);
    return { L, R, T, B, Lmm: L * mmx, Rmm: R * mmx, Tmm: T * mmy, Bmm: B * mmy, lr, tb, worst, valid };
  }

  function psaGrade(worst, side) {
    for (const [g, lim] of PSA[side]) {
      if (worst <= lim + 1e-9) return g;
    }
    return null;
  }

  const f1 = (v) => v.toFixed(1);
  const f2 = (v) => v.toFixed(2);

  function ratioCard(title, a, b, aName, bName, amm, bmm) {
    return `
      <div class="res-card">
        <div class="label">${title}</div>
        <div class="ratio">${f1(a)} <small>:</small> ${f1(b)}</div>
        <div class="bar"><div class="fill" style="width:${clamp(a, 0, 100)}%"></div><div class="mid"></div></div>
        <div class="mm">${aName} ${f2(amm)} mm ／ ${bName} ${f2(bmm)} mm</div>
      </div>`;
  }

  function updateResults() {
    if (!state.lines) return;
    const m = measure();
    const el = $('results');
    if (!m.valid) {
      el.innerHTML = '<p class="result-error">線の位置が不正です（ふちの内側の線が外側の縁より外にあります）。線を調整してください。</p>';
      return;
    }
    const g = psaGrade(m.worst, state.side);
    const sideName = state.side === 'front' ? '表面' : '裏面';
    const cls = g === 10 ? 'g-ok' : (g !== null && g >= 8 ? 'g-mid' : 'g-bad');
    const gradeText = g === null ? 'PSA 2 以下相当' : `PSA ${g} 相当まで`;
    const big = Math.max(m.worst, 100 - m.worst);
    el.innerHTML = `
      <div class="res-grid">
        ${ratioCard('左右（左 : 右）', m.lr, 100 - m.lr, '左', '右', m.Lmm, m.Rmm)}
        ${ratioCard('上下（上 : 下）', m.tb, 100 - m.tb, '上', '下', m.Tmm, m.Bmm)}
      </div>
      <div class="grade">
        <span class="sub">センタリングの目安（${sideName}・PSA基準）</span>
        <span class="big ${cls}">${gradeText}</span>
        <span class="sub">最も偏っている方向：${f1(big)} / ${f1(100 - big)}</span>
      </div>`;
  }

  // ---------------------------------------------------------------------------
  // 結果画像の保存
  // ---------------------------------------------------------------------------
  function saveImage() {
    const r = state.rect;
    if (!r || !state.lines) return;
    const m = measure();
    const band = Math.round(r.W * 0.16);
    const c = document.createElement('canvas');
    c.width = r.W;
    c.height = r.H + band;
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#12151f';
    ctx.fillRect(0, 0, c.width, c.height);
    drawMeasure(ctx, { s: 1, ox: 0, oy: 0 }, { loupe: true, lineWidth: Math.max(2, r.W / 600) });

    const fs = Math.round(band * 0.24);
    ctx.fillStyle = '#e8ebf3';
    ctx.textBaseline = 'middle';
    ctx.font = `bold ${fs}px system-ui, sans-serif`;
    const x = Math.round(r.W * 0.05);
    if (m.valid) {
      ctx.fillText(`左右 ${f1(m.lr)} : ${f1(100 - m.lr)}   上下 ${f1(m.tb)} : ${f1(100 - m.tb)}`, x, r.H + band * 0.33);
      const g = psaGrade(m.worst, state.side);
      ctx.font = `${Math.round(fs * 0.75)}px system-ui, sans-serif`;
      ctx.fillStyle = '#a3abc2';
      const side = state.side === 'front' ? '表面' : '裏面';
      ctx.fillText(`${side} / PSA基準の目安: ${g === null ? 'PSA 2以下' : `PSA ${g}`}相当`, x, r.H + band * 0.72);
    }

    c.toBlob((blob) => {
      if (!blob) return;
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'centering-result.png';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    }, 'image/png');
  }

  // ---------------------------------------------------------------------------
  // 画像の読み込み
  // ---------------------------------------------------------------------------
  function loadImageSource(img) {
    const iw = img.naturalWidth || img.width;
    const ih = img.naturalHeight || img.height;
    const k = Math.min(1, MAX_SRC / Math.max(iw, ih));
    const c = document.createElement('canvas');
    c.width = Math.round(iw * k);
    c.height = Math.round(ih * k);
    const ctx = c.getContext('2d');
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(img, 0, 0, c.width, c.height);
    state.src = c;
    state.srcData = ctx.getImageData(0, 0, c.width, c.height);
    state.corners = defaultCorners();
    state.activeCorner = 0;
    state.rot = 0;
    state.rect = null;
    state.lines = null;
    $('step2').classList.remove('hidden');
    $('step3').classList.add('hidden');
    $('step4').classList.add('hidden');
    $('staleWarn').classList.add('hidden');
    $('cornerWarn').classList.add('hidden');
    cornerViewer.fit();
    $('step2').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function loadURL(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => { loadImageSource(img); resolve(); };
      img.onerror = () => reject(new Error('画像を読み込めませんでした'));
      img.src = url;
    });
  }

  function loadFile(file) {
    if (!file || !file.type.startsWith('image/')) {
      if (file) alert('画像ファイルを選択してください。');
      return;
    }
    const url = URL.createObjectURL(file);
    loadURL(url)
      .catch(() => alert('この画像形式は読み込めませんでした。JPEG や PNG で保存し直してお試しください。'))
      .finally(() => URL.revokeObjectURL(url));
  }

  function runMeasure({ detect = true } = {}) {
    if (!buildRect()) return;
    $('step3').classList.remove('hidden');
    $('step4').classList.remove('hidden');
    if (detect || !state.lines) autoDetect();
    measureViewer.fit();
    updateResults();
  }

  // ---------------------------------------------------------------------------
  // イベント
  // ---------------------------------------------------------------------------
  $('fileInput').addEventListener('change', (e) => {
    loadFile(e.target.files[0]);
    e.target.value = '';
  });

  const drop = $('dropZone');
  ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => {
    e.preventDefault();
    drop.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, (e) => {
    e.preventDefault();
    drop.classList.remove('over');
  }));
  drop.addEventListener('drop', (e) => loadFile(e.dataTransfer.files[0]));
  document.addEventListener('dragover', (e) => e.preventDefault());
  document.addEventListener('drop', (e) => {
    e.preventDefault();
    if (e.dataTransfer.files[0]) loadFile(e.dataTransfer.files[0]);
  });
  document.addEventListener('paste', (e) => {
    const item = [...(e.clipboardData?.items || [])].find((it) => it.type.startsWith('image/'));
    if (item) loadFile(item.getAsFile());
  });

  $('cardSize').addEventListener('change', (e) => {
    state.size = e.target.value;
    if (state.rect) runMeasure();
  });

  $('resetCorners').addEventListener('click', () => {
    if (!state.src) return;
    state.corners = defaultCorners();
    markStale();
    cornerViewer.render();
  });

  $('doMeasure').addEventListener('click', () => {
    runMeasure();
    $('step3').scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  $('autoDetect').addEventListener('click', () => {
    if (!state.rect) return;
    autoDetect();
    measureViewer.render();
    updateResults();
  });

  $('rotate').addEventListener('click', () => {
    if (!state.rect) return;
    state.rot = (state.rot + 1) % 4;
    runMeasure();
  });

  $('saveImage').addEventListener('click', saveImage);

  const sel = $('lineSelect');
  for (const [key, info] of Object.entries(LINES)) {
    const o = document.createElement('option');
    o.value = key;
    o.textContent = info.label;
    sel.appendChild(o);
  }
  sel.value = state.selLine;
  sel.addEventListener('change', () => selectLine(sel.value));

  function nudge(delta) {
    if (!state.lines) return;
    setLine(state.selLine, state.lines[state.selLine] + delta);
    measureViewer.render();
    updateResults();
  }
  document.querySelectorAll('[data-nudge]').forEach((b) => {
    b.addEventListener('click', () => nudge(parseFloat(b.dataset.nudge)));
  });

  document.querySelectorAll('input[name="side"]').forEach((r) => {
    r.addEventListener('change', () => {
      state.side = r.value;
      updateResults();
    });
  });

  // キーボードでの微調整
  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
    const dir = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
    if (!dir) return;
    const mul = e.shiftKey ? 5 : 1;

    // 測定画面が表示中で画面内にあれば線を、そうでなければ隅を動かす
    const step3 = $('step3');
    const rect3 = step3.getBoundingClientRect();
    const inView3 = !step3.classList.contains('hidden') && rect3.top < window.innerHeight * 0.6 && rect3.bottom > 0;
    if (inView3 && state.lines) {
      const axis = LINES[state.selLine].axis;
      const d = axis === 'x' ? dir[0] : dir[1];
      if (!d) return;
      e.preventDefault();
      nudge(d * 0.5 * mul);
    } else if (state.corners && !$('step2').classList.contains('hidden')) {
      e.preventDefault();
      const i = state.activeCorner;
      const stepPx = Math.max(1, 1 / cornerViewer.T.s) * mul * 0.5;
      state.corners[i] = [
        clamp(state.corners[i][0] + dir[0] * stepPx, 0, state.src.width),
        clamp(state.corners[i][1] + dir[1] * stepPx, 0, state.src.height),
      ];
      markStale();
      cornerViewer.render();
    }
  });

  let resizeTimer = 0;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      cornerViewer.fit();
      measureViewer.fit();
    }, 150);
  });

  // テスト・デバッグ用
  window.CenteringApp = {
    state,
    loadURL,
    runMeasure,
    measure,
    setCorners: (c) => { state.corners = c.map((p) => p.slice()); cornerViewer.render(); },
  };
})();
