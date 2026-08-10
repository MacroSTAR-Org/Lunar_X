/* ============================================================
 * 背景：点阵网络
 *
 *   · 点之间按距离连线，越近越亮
 *   · 鼠标靠近时把附近的点吸过去，并向鼠标额外连线；移开后弹回原位
 *   · 整体叠加两组不同频率的正弦，做出水波一样的自由起伏
 *
 * 运动模型 = 自由漂移(home) + 水波偏移 + 鼠标吸引 → 目标点，
 * 再用弹簧阻尼逼近目标。弹簧是"能弹回来"的关键，直接赋值会显得僵硬。
 *
 * 性能：点数按视口面积算并封顶；连线是 O(n²) 距离判定但只有落在
 * 阈值内的才真正描边（通常两三百条）；页面不可见时暂停 rAF；
 * 系统开启"减少动态效果"时只画一帧静态图。
 * ============================================================ */
(function () {
  'use strict';

  const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const CFG = {
    areaPerPoint: 15000,   // 每个点摊到多少 px²，越小越密
    minPoints: 28,
    maxPoints: 110,
    linkDist: 132,         // 点与点的连线阈值
    mouseDist: 175,        // 鼠标吸附/连线半径
    mousePull: 0.32,       // 吸附强度（0~1，1 为直接贴到鼠标上）
    spring: 0.055,         // 弹簧系数
    damping: 0.88,         // 阻尼，越小越"黏"
    drift: 0.12,           // 自由漂移速度上限
    waveAmp: 13,           // 水波振幅
  };

  const canvas = document.createElement('canvas');
  canvas.className = 'bg-canvas';
  canvas.setAttribute('aria-hidden', 'true');
  (document.body || document.documentElement).appendChild(canvas);
  const ctx = canvas.getContext('2d', { alpha: true });

  let W = 0, H = 0, dpr = 1;
  let points = [];
  let rgb = '255,255,255';
  const mouse = { x: 0, y: 0, active: false };
  let raf = null;

  /** 主题色：画布读不到 CSS 变量，从 body 上取前景色再转成 rgb 分量 */
  function readColor() {
    const v = getComputedStyle(document.body).getPropertyValue('--fg-0').trim();
    const m = v.match(/^#?([0-9a-f]{6})$/i);
    if (m) {
      const n = parseInt(m[1], 16);
      rgb = [(n >> 16) & 255, (n >> 8) & 255, n & 255].join(',');
      return;
    }
    const m2 = v.match(/(\d+)[,\s]+(\d+)[,\s]+(\d+)/);
    rgb = m2 ? `${m2[1]},${m2[2]},${m2[3]}` : '255,255,255';
  }

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);   // 高 DPI 屏封顶，省像素填充
    W = window.innerWidth;
    H = window.innerHeight;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    build();
  }

  function build() {
    const want = Math.max(CFG.minPoints,
                 Math.min(CFG.maxPoints, Math.round(W * H / CFG.areaPerPoint)));
    points = [];
    for (let i = 0; i < want; i++) {
      const hx = Math.random() * W;
      const hy = Math.random() * H;
      points.push({
        hx: hx, hy: hy,           // 漂移中的"家"
        x: hx, y: hy,             // 实际绘制位置
        vx: 0, vy: 0,
        dx: (Math.random() - 0.5) * CFG.drift * 2,
        dy: (Math.random() - 0.5) * CFG.drift * 2,
        ph: Math.random() * Math.PI * 2,   // 相位错开，避免整片同步摆动
        r: 0.9 + Math.random() * 0.9,
      });
    }
  }

  function step(t) {
    ctx.clearRect(0, 0, W, H);

    const link2 = CFG.linkDist * CFG.linkDist;
    const mdist2 = CFG.mouseDist * CFG.mouseDist;

    for (const p of points) {
      // 自由漂移，出界从另一侧绕回
      p.hx += p.dx;
      p.hy += p.dy;
      if (p.hx < -40) p.hx = W + 40; else if (p.hx > W + 40) p.hx = -40;
      if (p.hy < -40) p.hy = H + 40; else if (p.hy > H + 40) p.hy = -40;

      // 水波：两组频率不同、方向不同的正弦叠加，避免看出规律
      const wx = Math.sin(t * 0.00042 + p.hy * 0.0062 + p.ph) * CFG.waveAmp;
      const wy = Math.sin(t * 0.00061 + p.hx * 0.0048 + p.ph) * CFG.waveAmp * 0.8;
      let tx = p.hx + wx;
      let ty = p.hy + wy;

      // 鼠标吸附：半径内按距离线性加权拉向光标
      if (mouse.active) {
        const dx = mouse.x - tx, dy = mouse.y - ty;
        const d2 = dx * dx + dy * dy;
        if (d2 < mdist2) {
          const f = (1 - Math.sqrt(d2) / CFG.mouseDist) * CFG.mousePull;
          tx += dx * f;
          ty += dy * f;
        }
      }

      // 弹簧阻尼逼近目标：鼠标移开后能自然弹回
      p.vx = (p.vx + (tx - p.x) * CFG.spring) * CFG.damping;
      p.vy = (p.vy + (ty - p.y) * CFG.spring) * CFG.damping;
      p.x += p.vx;
      p.y += p.vy;
    }

    // 点与点的连线
    ctx.lineWidth = 1;
    for (let i = 0; i < points.length; i++) {
      const a = points[i];
      for (let j = i + 1; j < points.length; j++) {
        const b = points[j];
        const dx = a.x - b.x;
        if (dx > CFG.linkDist || dx < -CFG.linkDist) continue;   // 先用 x 快速排除
        const dy = a.y - b.y;
        const d2 = dx * dx + dy * dy;
        if (d2 > link2) continue;
        const alpha = (1 - Math.sqrt(d2) / CFG.linkDist) * 0.20;
        ctx.strokeStyle = `rgba(${rgb},${alpha.toFixed(3)})`;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
    }

    // 鼠标连线（更亮，作为"吸附"的视觉反馈）
    if (mouse.active) {
      for (const p of points) {
        const dx = mouse.x - p.x, dy = mouse.y - p.y;
        const d2 = dx * dx + dy * dy;
        if (d2 > mdist2) continue;
        const alpha = (1 - Math.sqrt(d2) / CFG.mouseDist) * 0.45;
        ctx.strokeStyle = `rgba(${rgb},${alpha.toFixed(3)})`;
        ctx.beginPath();
        ctx.moveTo(mouse.x, mouse.y);
        ctx.lineTo(p.x, p.y);
        ctx.stroke();
      }
    }

    // 点本体，靠近鼠标的稍亮稍大
    for (const p of points) {
      let a = 0.42, r = p.r;
      if (mouse.active) {
        const dx = mouse.x - p.x, dy = mouse.y - p.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < mdist2) {
          const k = 1 - Math.sqrt(d2) / CFG.mouseDist;
          a += k * 0.45;
          r += k * 1.1;
        }
      }
      ctx.fillStyle = `rgba(${rgb},${a.toFixed(3)})`;
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function loop(t) {
    step(t);
    raf = requestAnimationFrame(loop);
  }

  function start() {
    if (raf === null) raf = requestAnimationFrame(loop);
  }
  function stop() {
    if (raf !== null) { cancelAnimationFrame(raf); raf = null; }
  }

  // ---------- 事件 ----------
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { resize(); if (REDUCED) step(0); }, 150);
  });

  // 用 window 监听：画布是 pointer-events:none，事件只会落在上层 UI 上
  window.addEventListener('pointermove', (e) => {
    mouse.x = e.clientX;
    mouse.y = e.clientY;
    mouse.active = true;
  }, { passive: true });
  window.addEventListener('pointerleave', () => { mouse.active = false; });
  window.addEventListener('blur', () => { mouse.active = false; });

  // 切到后台就停，别在看不见的时候烧 CPU
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stop();
    else if (!REDUCED) start();
  });

  window.addEventListener('lunarx-theme-change', () => {
    readColor();
    if (REDUCED) step(0);
  });

  // 个性化偏好：关闭背景动效时停止动画并清屏，打开时恢复
  function bgEnabled() {
    try {
      const raw = JSON.parse(localStorage.getItem('lunarx_prefs') || '{}');
      return raw.bg_effects !== false;
    } catch (e) {
      return true;
    }
  }
  function applyBg() {
    if (bgEnabled()) { canvas.style.display = ''; start(); }
    else { stop(); canvas.style.display = 'none'; }
  }
  window.addEventListener('lunarx-prefs-change', applyBg);

  readColor();
  resize();
  if (REDUCED || !bgEnabled()) { if (REDUCED) step(0); else canvas.style.display = 'none'; }
  else start();

  // 暴露句柄，方便在控制台里实时调参数（改 CFG 下一帧就生效）
  window.LunarBg = { cfg: CFG, mouse: mouse, points: () => points, start: start, stop: stop };
})();
