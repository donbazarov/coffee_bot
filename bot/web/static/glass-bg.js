/* =========================================================================
   Liquid Glass — анимированный фон «радужные масляные разводы».

   Даёт режиму «жидкого стекла» то, что нужно преломлять: на однотонном фоне
   backdrop-filter размывает пустоту, и стекло выглядит как «грязное». Здесь —
   те же медленные кривые Безье, что и на странице историй (static/stories),
   вынесенные в самостоятельный модуль.

   Правила:
     • рисуем ТОЛЬКО когда на <html> стоит data-glass="on" (галочка в ЛК);
       переключение ловим через MutationObserver — без связки с app.js;
     • ~30 к/с на десктопе и ~20 к/с с меньшим разрешением на мобильных,
       пауза в свёрнутой вкладке, один статичный кадр при reduced-motion.

   Управление: window.NeftGlassBg.refresh() — пересчитать состояние вручную.
   ========================================================================= */
(function () {
  'use strict';

  const canvas = document.getElementById('glass-bg');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  const root = document.documentElement;
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  // Мобильные и узкие экраны: фон рисуем, но реже и в меньшем разрешении.
  // Полностью отключать нельзя — без него стекло выглядит плоским.
  const lowPower = window.matchMedia('(pointer: coarse)').matches
    || window.matchMedia('(max-width: 760px)').matches;
  const frameInterval = lowPower ? 50 : 33;   // ~20 к/с на мобильных, ~30 на десктопе
  const maxDpr = lowPower ? 1 : 1.5;

  // Каждый слой — «мазок масла» со своей амплитудой, скоростью и оттенком.
  const layers = [
    { base: 0.16, amp: 90, sway: 60, freq: 1.1, speed: 0.10, hue: 205, hueSpeed: 6, thickness: 120, alpha: 0.10, phase: 0.0 },
    { base: 0.32, amp: 70, sway: 84, freq: 0.8, speed: 0.14, hue: 285, hueSpeed: 8, thickness: 150, alpha: 0.09, phase: 1.7 },
    { base: 0.48, amp: 110, sway: 50, freq: 1.4, speed: 0.09, hue: 160, hueSpeed: 5, thickness: 110, alpha: 0.08, phase: 3.1 },
    { base: 0.64, amp: 80, sway: 70, freq: 1.0, speed: 0.12, hue: 330, hueSpeed: 7, thickness: 135, alpha: 0.09, phase: 4.6 },
    { base: 0.80, amp: 95, sway: 55, freq: 1.2, speed: 0.11, hue: 45, hueSpeed: 4, thickness: 120, alpha: 0.08, phase: 6.0 },
  ];

  let width = 0;
  let height = 0;
  let rafId = null;
  let lastFrame = 0;
  const clockStart = performance.now();

  const enabled = () => root.getAttribute('data-glass') === 'on'
    && document.body.getAttribute('data-authenticated') === 'true';

  function resizeCanvas() {
    const dpr = Math.min(window.devicePixelRatio || 1, maxDpr);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function wave(t, layer, k) {
    const p = k + layer.phase;
    return (
      layer.amp * Math.sin(t * layer.speed + p * layer.freq) +
      layer.sway * Math.cos(t * layer.speed * 0.63 + p * layer.freq * 0.5)
    );
  }

  /* Один «мазок» из последовательных кубических кривых Безье. */
  function strokeRibbon(t, layer) {
    const segments = 4;
    const span = width + 160;
    const step = span / segments;
    const yBase = height * layer.base;
    const hue = (layer.hue + t * layer.hueSpeed) % 360;

    ctx.beginPath();
    ctx.moveTo(-80, yBase + wave(t, layer, 0));
    for (let i = 0; i < segments; i++) {
      const x0 = -80 + step * i;
      ctx.bezierCurveTo(
        x0 + step * 0.34, yBase + wave(t, layer, i + 0.34),
        x0 + step * 0.68, yBase + wave(t, layer, i + 0.68),
        x0 + step, yBase + wave(t, layer, i + 1),
      );
    }
    ctx.strokeStyle = `hsla(${hue}, 88%, 62%, ${layer.alpha})`;
    ctx.lineWidth = layer.thickness;
    ctx.lineCap = 'round';
    ctx.stroke();
  }

  function drawBackground(t) {
    ctx.clearRect(0, 0, width, height);
    ctx.globalCompositeOperation = 'lighter';
    for (const layer of layers) {
      // Три близких по цвету прохода дают эффект радужного перелива.
      strokeRibbon(t, layer);
      strokeRibbon(t * 1.04, { ...layer, hue: layer.hue + 26, thickness: layer.thickness * 0.55, alpha: layer.alpha * 0.7 });
      strokeRibbon(t * 0.96, { ...layer, hue: layer.hue - 24, thickness: layer.thickness * 0.35, alpha: layer.alpha * 0.6 });
    }
    ctx.globalCompositeOperation = 'source-over';
  }

  function loop(now) {
    if (now - lastFrame >= frameInterval) {   // кадровый лимит: плавно и без нагрузки
      lastFrame = now;
      drawBackground((now - clockStart) / 1000);
    }
    rafId = requestAnimationFrame(loop);
  }

  function start() {
    if (rafId !== null) return;
    rafId = requestAnimationFrame(loop);
  }

  function stop() {
    if (rafId !== null) {
      cancelAnimationFrame(rafId);
      rafId = null;
    }
  }

  /* Текущее состояние: рисуем, статичный кадр или вообще молчим. */
  function refresh() {
    if (!enabled()) { stop(); return; }
    resizeCanvas();
    if (reduceMotion) { drawBackground(0); stop(); return; }
    start();
  }

  window.addEventListener('resize', () => { if (enabled() && !reduceMotion) resizeCanvas(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stop();
    else if (enabled() && !reduceMotion) start();
  });
  // Реакция на галочку Liquid Glass в личном кабинете.
  new MutationObserver(refresh).observe(root, { attributes: true, attributeFilter: ['data-glass'] });

  refresh();

  window.NeftGlassBg = { refresh };
})();
