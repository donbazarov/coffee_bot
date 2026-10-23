/* =========================================================================
   НЕФТЬ · Истории гостей — клиентская логика главной страницы
   ========================================================================= */

(() => {
  'use strict';

  const API_STORIES = '/stories/api/stories';
  const API_ME = '/stories/api/me';
  const API_MINE = '/stories/api/stories/mine';
  const MAX_PHOTO_BYTES = 5 * 1024 * 1024;

  // Скорость автопрокрутки и лимит символов приходят с сервера вместе со
  // списком историй и кэшируются в localStorage — в офлайне берём их оттуда.
  // Значения по умолчанию совпадают с серверными (stories_service.DEFAULT_SETTINGS).
  const CACHE_KEY = 'neft-cache:stories';
  const DEFAULT_AUTOPLAY_MS = 7000;
  const DEFAULT_MAX_CHARS = 600;
  const DEFAULT_CAROUSEL_WIDTH = 900;
  const ADAPTIVE_WIDTH = 'min(1120px, 88vw)';   // «адаптивная ширина» из панели модерации
  let autoplayMs = DEFAULT_AUTOPLAY_MS;
  let maxChars = DEFAULT_MAX_CHARS;
  let serverAutoplayMs = DEFAULT_AUTOPLAY_MS;   // скорость из панели модерации

  // Личные настройки гостя: масштаб интерфейса, автораскрытие историй и своя
  // скорость слайд-шоу. Лежат в кэше устройства и важнее серверных значений.
  const PREFS_KEY = 'neft-cache:stories:prefs';
  const SCALE_MIN = 85;
  const SCALE_MAX = 150;
  const DEFAULT_SCALE = 100;
  const MIN_AUTOPLAY_SEC = 1;
  const MAX_AUTOPLAY_SEC = 60;
  let uiScale = DEFAULT_SCALE;
  let autoExpand = false;
  let autoplaySec = null;       // null — берём скорость, настроенную в кофейне

  const CTA_EDIT_PUBLISHED = 'Ваша история уже опубликована — её можно изменить. После правок она снова уйдёт на проверку.';
  const CTA_EDIT_PENDING = 'Ваша история ждёт проверки — её можно изменить. После правок она снова уйдёт на проверку.';

  function ctaNew() {
    return `До ${maxChars} символов и одна фотография. История появится после проверки.`;
  }

  function readCache(key) {
    try {
      const raw = localStorage.getItem(key);
      return raw == null ? null : JSON.parse(raw);
    } catch (error) {
      return null;
    }
  }

  function writeCache(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (error) {
      /* localStorage недоступен (приватный режим) — кэш пропускаем. */
    }
  }

  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* =====================================================================
     0. Личные настройки устройства (шестерёнка в шапке)
     ===================================================================== */

  /** Сколько секунд в миллисекундах (нижняя граница — как у сервера). */
  function toSeconds(ms) {
    return Math.max(MIN_AUTOPLAY_SEC, Math.round(ms / 1000));
  }

  /** Масштаб интерфейса: zoom на <html> тянет за собой всю страницу. */
  function applyUiScale() {
    document.documentElement.style.setProperty('--ui-scale', String(uiScale / 100));
    refreshAll();      // климп текста и высота карточек считаются в новых px
  }

  function savePrefs() {
    writeCache(PREFS_KEY, { scale: uiScale, autoExpand, autoplaySec });
  }

  /** Читаем настройки устройства и сразу применяем их (работает и в офлайне). */
  function loadPrefs() {
    const stored = readCache(PREFS_KEY);
    if (stored) {
      const scale = Number(stored.scale);
      uiScale = Number.isFinite(scale)
        ? Math.min(SCALE_MAX, Math.max(SCALE_MIN, Math.round(scale / 5) * 5))
        : DEFAULT_SCALE;
      autoExpand = stored.autoExpand === true;
      const seconds = Number(stored.autoplaySec);
      autoplaySec = Number.isFinite(seconds) && seconds > 0
        ? Math.min(MAX_AUTOPLAY_SEC, Math.max(MIN_AUTOPLAY_SEC, Math.round(seconds)))
        : null;
    }
    if (autoplaySec !== null) autoplayMs = autoplaySec * 1000;
    applyUiScale();
    syncSettingsForm();
  }

  /* =====================================================================
     1. Фон — разноцветные кривые Безье «радужные масляные разводы»
     ===================================================================== */

  const canvas = document.getElementById('oil-canvas');
  const ctx = canvas.getContext('2d');

  // Каждый слой — это «мазок масла» со своей амплитудой, скоростью и оттенком.
  const layers = [
    { base: 0.16, amp: 90, sway: 60, freq: 1.1, speed: 0.10, hue: 205, hueSpeed: 6, thickness: 120, alpha: 0.10, phase: 0.0 },
    { base: 0.32, amp: 70, sway: 84, freq: 0.8, speed: 0.14, hue: 285, hueSpeed: 8, thickness: 150, alpha: 0.09, phase: 1.7 },
    { base: 0.48, amp: 110, sway: 50, freq: 1.4, speed: 0.09, hue: 160, hueSpeed: 5, thickness: 110, alpha: 0.08, phase: 3.1 },
    { base: 0.64, amp: 80, sway: 70, freq: 1.0, speed: 0.12, hue: 330, hueSpeed: 7, thickness: 135, alpha: 0.09, phase: 4.6 },
    { base: 0.80, amp: 95, sway: 55, freq: 1.2, speed: 0.11, hue: 45, hueSpeed: 4, thickness: 120, alpha: 0.08, phase: 6.0 },
  ];

  let width = 0;
  let height = 0;

  function resizeCanvas() {
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (reduceMotion) drawBackground(0);
  }

  function wave(t, layer, k) {
    const p = k + layer.phase;
    return (
      layer.amp * Math.sin(t * layer.speed + p * layer.freq) +
      layer.sway * Math.cos(t * layer.speed * 0.63 + p * layer.freq * 0.5)
    );
  }

  /** Рисует один «мазок» из последовательных кубических кривых Безье. */
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
        x0 + step, yBase + wave(t, layer, i + 1)
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

  const clockStart = performance.now();
  let lastFrame = 0;
  let rafId = null;

  function loop(now) {
    // Ограничиваем частоту ~30 к/с — плавно и не грузит слабые устройства.
    if (now - lastFrame >= 33) {
      lastFrame = now;
      drawBackground((now - clockStart) / 1000);
    }
    rafId = requestAnimationFrame(loop);
  }

  function startBackground() {
    if (reduceMotion || rafId !== null) return;
    rafId = requestAnimationFrame(loop);
  }

  function stopBackground() {
    if (rafId !== null) {
      cancelAnimationFrame(rafId);
      rafId = null;
    }
  }

  window.addEventListener('resize', resizeCanvas);
  document.addEventListener('visibilitychange', () => {
    document.hidden ? stopBackground() : startBackground();
  });

  resizeCanvas();
  startBackground();

  /* =====================================================================
     2. Утилиты
     ===================================================================== */

  async function api(url, options = {}) {
    const response = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'Что-то пошло не так');
    return data;
  }

  function formatDate(iso) {
    try {
      return new Date(iso).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' });
    } catch {
      return '';
    }
  }

  function show(element) { element.hidden = false; }
  function hide(element) { element.hidden = true; }

  /* =====================================================================
     3. Карусель опубликованных историй
     ===================================================================== */

  const carousel = {
    viewport: document.getElementById('carousel-viewport'),
    track: document.getElementById('carousel-track'),
    template: document.getElementById('slide-template'),
    empty: document.getElementById('carousel-empty'),
    controls: document.getElementById('carousel-controls'),
    progress: document.getElementById('progress'),
    progressBar: document.getElementById('progress-bar'),
    dots: document.getElementById('dots'),
    stories: [],
    index: 0,
    timer: null,
  };

  function renderCarousel(stories) {
    carousel.stories = stories;
    carousel.track.innerHTML = '';
    carousel.dots.innerHTML = '';

    if (!stories.length) {
      show(carousel.empty);
      hide(carousel.controls);
      hide(carousel.progress);
      carousel.viewport.style.height = '';
      return;
    }

    hide(carousel.empty);
    show(carousel.controls);
    show(carousel.progress);

    stories.forEach((story, i) => {
      const node = carousel.template.content.firstElementChild.cloneNode(true);
      node.dataset.index = String(i);

      const name = story.name || 'Гость НЕФТИ';
      node.querySelector('.slide-text').textContent = story.text;
      node.querySelector('.slide-author').textContent = name;
      node.querySelector('.slide-date').textContent = formatDate(story.createdAt);

      let photoReady = null;   // фото уже в памяти: обработчик вызовем сразу после вставки
      if (story.photo) {
        const img = node.querySelector('.slide-photo img');
        img.src = story.photo;
        img.alt = `Фотография из истории гостя по имени ${name}`;
        // Пропорции снимка известны только после загрузки — тогда и решаем,
        // вертикальный ли он (раскладка в две колонки) и нужна ли кнопка
        // «Показать полностью».
        const onPhotoLoad = () => {
          applyPhotoOrientation(node, img);
          applyExpandedPhotoHeight(node);
          if (carousel.track.children[carousel.index] === node) syncViewportHeight();
        };
        img.addEventListener('load', onPhotoLoad);
        // Первый рендер идёт из кэша устройства: фото часто уже в памяти браузера,
        // и событие load может не прийти. Тогда вызываем обработчик сами — но
        // только после вставки слайда в документ, иначе размеры он не отдаёт.
        if (img.complete) photoReady = onPhotoLoad;
      } else {
        node.classList.add('no-photo');
      }

      carousel.track.appendChild(node);
      if (photoReady) photoReady();

      const dot = document.createElement('button');
      dot.className = 'dot-btn';
      dot.type = 'button';
      dot.setAttribute('role', 'tab');
      dot.setAttribute('aria-label', `История ${i + 1}`);
      dot.addEventListener('click', () => goTo(i, true));
      carousel.dots.appendChild(dot);
    });

    carousel.index = 0;
    updateSlides();
    scheduleAutoplay();
  }

  /** Предел высоты колонки фото: сверхвысокие панорамы не раздувают карточку. */
  const PORTRAIT_PHOTO_MAX_RATIO = 0.76;

  /** Высота, которую колонка фото заняла бы по своим пропорциям. В две колонки она
      растянута на всю высоту карточки (align-self: stretch) и своего размера не
      отдаёт, поэтому считаем от ширины колонки и натуральных размеров снимка.
      Нижнюю границу берём из CSS (min-height), чтобы JS и стили не расходились. */
  function naturalPhotoHeight(photo) {
    const floor = parseFloat(getComputedStyle(photo).minHeight) || 0;
    const img = photo.querySelector('img');
    if (!img || !img.naturalWidth || !photo.clientWidth) return floor;

    const natural = photo.clientWidth * (img.naturalHeight / img.naturalWidth);
    return Math.max(floor, Math.min(natural, window.innerHeight * PORTRAIT_PHOTO_MAX_RATIO));
  }

  /** Высота карточки: фото + текст + рамка.
      offsetHeight не зависит от transform, поэтому мерить можно безопасно. */
  function measureSlideHeight(slide) {
    const photo = slide.querySelector('.slide-photo');
    const content = slide.querySelector('.slide-content');
    const photoShown = Boolean(photo) && getComputedStyle(photo).display !== 'none';

    // В две колонки (вертикальное фото на десктопе) высота карточки — максимум из двух:
    // колонка фото растянута (её offsetHeight равен высоте карточки и для замера
    // бесполезен), а колонка текста не растянута и отдаёт собственную высоту — по ней
    // карточка и растёт. Источник правды о раскладке — реальный flex-direction,
    // поэтому JS и медиазапросы не разъезжаются.
    if (getComputedStyle(slide).flexDirection === 'row') {
      return Math.max(photoShown ? naturalPhotoHeight(photo) : 0, content.offsetHeight) + 2;
    }

    const photoHeight = photoShown ? photo.offsetHeight : 0;
    return photoHeight + content.offsetHeight + 2;
  }

  /** Подгоняем высоту карусели под активную карточку, чтобы ничего не обрезалось. */
  function syncViewportHeight() {
    if (!carousel.track.children.length) return;

    // Меряем сразу (чтение offsetHeight само вызывает reflow), не полагаясь
    // на requestAnimationFrame — он не срабатывает в фоновой вкладке.
    const apply = () => {
      const current = carousel.track.children[carousel.index];
      if (!current) return;
      carousel.viewport.style.height = measureSlideHeight(current) + 'px';
    };

    apply();
    requestAnimationFrame(apply);   // и ещё раз после перерисовки/догрузки
  }

  /** Сколько строк текста показываем в свёрнутой карточке. */
  function clampLines() {
    return window.innerWidth <= 640 ? 5 : 6;
  }

  /** Нужна ли кнопка «Показать полностью»: длинный текст или приложенное фото. */
  function refreshClampState(slide) {
    if (slide.classList.contains('is-expanded')) return;   // в раскрытом виде мерить нечего

    // Число строк держим в CSS-переменной, чтобы JS и стили не расходились.
    const lines = clampLines();
    slide.style.setProperty('--clamp-lines', String(lines));

    const text = slide.querySelector('.slide-text');
    const lineHeight = parseFloat(getComputedStyle(text).lineHeight) || 24;

    // Естественную высоту меряем без климпа: снимаем класс и возвращаем обратно
    // в том же кадре, поэтому мигания не видно.
    const wasClamped = slide.classList.contains('is-text-clamped');
    slide.classList.remove('is-text-clamped');
    const naturalHeight = text.scrollHeight;
    if (wasClamped) slide.classList.add('is-text-clamped');

    const textOverflows = naturalHeight > lineHeight * lines + 2;
    const hasPhoto = !slide.classList.contains('no-photo');

    // Текст сворачиваем сразу, не дожидаясь загрузки фотографии.
    slide.classList.toggle('is-text-clamped', textOverflows);
    // Кнопка нужна и при длинном тексте, и при фото: object-fit: cover
    // всё равно обрезает снимок по краям карточки.
    slide.classList.toggle('is-clampable', textOverflows || hasPhoto);
  }

  /** Вертикальный снимок на десктопе показываем в две колонки: фото слева,
      текст справа. Само по себе это лишь вешает класс — раскладка живёт в
      десктопном медиазапросе styles.css, поэтому на узких экранах её нет. */
  function applyPhotoOrientation(slide, img) {
    if (!img || !img.naturalWidth) return;
    const portrait = img.naturalHeight > img.naturalWidth;
    if (portrait === slide.classList.contains('is-portrait')) return;   // ничего не изменилось

    slide.classList.toggle('is-portrait', portrait);
    // Ширина колонки текста изменилась — пересчитываем климп и высоту фото.
    refreshClampState(slide);
    applyExpandedPhotoHeight(slide);
  }

  function refreshAll() {
    for (const slide of carousel.track.children) {
      refreshClampState(slide);
      // При повороте экрана и смене ширины пересчитываем высоту раскрытого фото:
      // для свёрнутых функция сама сбрасывает ранее выставленный style.height.
      applyExpandedPhotoHeight(slide);
    }
    syncViewportHeight();
  }

  /** В раскрытом виде высота фото идёт по пропорциям снимка, без обрезки. */
  function applyExpandedPhotoHeight(slide) {
    const photo = slide.querySelector('.slide-photo');
    const img = photo && photo.querySelector('img');
    if (!photo || !img || slide.classList.contains('no-photo')) return;

    // В две колонки (вертикальное фото на десктопе) высоту колонки задают
    // пропорции снимка, контент и align-items: stretch — inline-высота из JS
    // с ними конфликтует, поэтому не выставляем её и сбрасываем прежнюю.
    if (getComputedStyle(slide).flexDirection === 'row') {
      photo.style.height = '';
      return;
    }

    // Свёрнутое состояние — высота берётся из CSS.
    if (!slide.classList.contains('is-expanded') || !img.naturalWidth || !photo.clientWidth) {
      photo.style.height = '';
      return;
    }

    const limit = window.innerHeight * (window.innerWidth <= 640 ? 0.55 : 0.7);
    const natural = photo.clientWidth * (img.naturalHeight / img.naturalWidth);
    photo.style.height = Math.round(Math.min(limit, natural)) + 'px';
  }

  function setSlideExpanded(slide, expanded) {
    slide.classList.toggle('is-expanded', expanded);
    applyExpandedPhotoHeight(slide);
    const button = slide.querySelector('.slide-more');
    if (button) {
      button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
      button.querySelector('.slide-more-label').textContent = expanded ? 'Свернуть' : 'Показать полностью';
    }
  }

  function collapseAll() {
    for (const slide of carousel.track.children) setSlideExpanded(slide, false);
  }

  function updateSlides() {
    const slides = carousel.track.children;
    for (let i = 0; i < slides.length; i++) {
      const slide = slides[i];
      slide.classList.toggle('is-active', i === carousel.index);
      slide.classList.toggle('is-prev', i < carousel.index);
      slide.classList.toggle('is-next', i > carousel.index);
      slide.setAttribute('aria-hidden', i === carousel.index ? 'false' : 'true');
    }
    [...carousel.dots.children].forEach((dot, i) => {
      dot.classList.toggle('is-active', i === carousel.index);
      dot.setAttribute('aria-selected', i === carousel.index ? 'true' : 'false');
    });

    const active = slides[carousel.index];
    if (active) {
      refreshClampState(active);
      // Автооткрытие: история показывается целиком, без «показать полностью».
      if (autoExpand) setSlideExpanded(active, true);
    }
    syncViewportHeight();
  }

  function goTo(index, manual = false) {
    const total = carousel.stories.length;
    if (!total) return;
    carousel.index = (index + total) % total;
    collapseAll();                       // открываем каждую историю свёрнутой
    updateSlides();
    restartProgress();
    if (manual) scheduleAutoplay();
  }

  function next() { goTo(carousel.index + 1); }
  function prev() { goTo(carousel.index - 1); }

  function scheduleAutoplay() {
    clearTimeout(carousel.timer);
    restartProgress();
    if (reduceMotion || carousel.stories.length < 2) {
      carousel.progressBar.style.width = carousel.stories.length ? '100%' : '0';
      return;
    }
    carousel.timer = setTimeout(() => {
      next();
      scheduleAutoplay();
    }, autoplayMs);
  }

  function restartProgress() {
    const bar = carousel.progressBar;
    if (!carousel.stories.length || reduceMotion || carousel.stories.length < 2) return;
    bar.style.transition = 'none';
    bar.style.width = '0%';
    void bar.offsetWidth;                       // принудительный reflow
    bar.style.transition = `width ${autoplayMs}ms linear`;
    bar.style.width = '100%';
  }

  function pauseAutoplay() {
    clearTimeout(carousel.timer);
    carousel.timer = null;
    carousel.progressBar.style.transition = 'none';
    carousel.progressBar.style.width = '0%';
  }

  document.getElementById('next-btn').addEventListener('click', () => goTo(carousel.index + 1, true));
  document.getElementById('prev-btn').addEventListener('click', () => goTo(carousel.index - 1, true));

  carousel.viewport.addEventListener('mouseenter', pauseAutoplay);
  carousel.viewport.addEventListener('mouseleave', () => scheduleAutoplay());
  carousel.viewport.addEventListener('focusin', pauseAutoplay);
  carousel.viewport.addEventListener('focusout', () => scheduleAutoplay());

  // Свайп на мобильных
  let touchStartX = null;
  carousel.viewport.addEventListener('touchstart', (e) => {
    touchStartX = e.changedTouches[0].clientX;
    pauseAutoplay();
  }, { passive: true });
  carousel.viewport.addEventListener('touchend', (e) => {
    if (touchStartX === null) return;
    const delta = e.changedTouches[0].clientX - touchStartX;
    if (Math.abs(delta) > 45) goTo(carousel.index + (delta < 0 ? 1 : -1), true);
    else scheduleAutoplay();
    touchStartX = null;
  }, { passive: true });
  carousel.viewport.addEventListener('touchcancel', () => {
    touchStartX = null;
    scheduleAutoplay();
  }, { passive: true });

  // Стрелки клавиатуры
  document.addEventListener('keydown', (e) => {
    if (!document.getElementById('editor-modal').hidden) return;
    if (!document.getElementById('settings-modal').hidden) return;   // открыто окошко настроек
    if (e.key === 'ArrowRight') goTo(carousel.index + 1, true);
    if (e.key === 'ArrowLeft') goTo(carousel.index - 1, true);
  });

  // Раскрытие карточки: текст целиком и фото без обрезки
  carousel.track.addEventListener('click', (event) => {
    const button = event.target.closest('.slide-more');
    if (!button) return;
    const slide = button.closest('.slide');
    const expanded = !slide.classList.contains('is-expanded');
    setSlideExpanded(slide, expanded);
    if (expanded) pauseAutoplay();     // пока гость читает, слайдшоу стоит
    else scheduleAutoplay();
    syncViewportHeight();
  });

  // Истории не выделяются и не отдают контекстное меню, поэтому долгое
  // нажатие на телефоне только останавливает слайдшоу.
  carousel.viewport.addEventListener('contextmenu', (event) => event.preventDefault());
  carousel.viewport.addEventListener('selectstart', (event) => event.preventDefault());
  carousel.viewport.addEventListener('dragstart', (event) => event.preventDefault());

  // Пересчёт высоты при повороте экрана и после загрузки шрифтов
  window.addEventListener('resize', refreshAll);
  document.fonts?.ready.then(refreshAll).catch(() => {});

  /* =====================================================================
     4. Редактор истории
     ===================================================================== */

  const modal = document.getElementById('editor-modal');
  const editorView = document.getElementById('editor-view');
  const thanksView = document.getElementById('thanks-view');
  const textarea = document.getElementById('story-text');
  const counter = document.getElementById('counter');
  const meter = document.querySelector('.meter');
  const meterFill = document.getElementById('meter-fill');
  const photoInput = document.getElementById('photo-input');
  const photoName = document.getElementById('photo-name');
  const photoPreview = document.getElementById('photo-preview');
  const photoPreviewImg = document.getElementById('photo-preview-img');
  const formError = document.getElementById('form-error');
  const submitBtn = document.getElementById('submit-btn');

  const nameInput = document.getElementById('guest-name');
  const editorTitle = document.getElementById('editor-title');
  const editorSubtitle = document.getElementById('editor-subtitle');
  const thanksTitle = document.getElementById('thanks-title');
  const thanksText = document.getElementById('thanks-text');
  const thanksNote = document.getElementById('thanks-note');
  const shareBtn = document.getElementById('share-btn');
  const headerShare = document.getElementById('header-share');
  const ctaNote = document.getElementById('cta-note');

  // История, уже отправленная с этого устройства. Сервер узнаёт устройство
  // по httpOnly-куке, поэтому в JS её значение недоступно — только факт.
  let guest = { hasStory: false, story: null };

  let photoDataUrl = null;      // новое фото в виде data-URL
  let photoRemoved = false;     // гость убрал уже приложенное фото
  let hasExistingPhoto = false; // у истории на сервере уже есть фото
  let lastFocused = null;

  function isEditMode() { return guest.hasStory; }

  function updateCta() {
    const edit = isEditMode();
    shareBtn.textContent = edit ? 'Изменить мою историю' : 'Поделиться своей историей';
    headerShare.textContent = edit ? 'Изменить' : 'Поделиться';

    if (!edit) {
      ctaNote.textContent = ctaNew();
      return;
    }
    // Показываем гостю, что сейчас происходит с его историей.
    const published = Boolean(guest.story && guest.story.published);
    ctaNote.textContent = published ? CTA_EDIT_PUBLISHED : CTA_EDIT_PENDING;
  }

  function updateMeter() {
    const length = textarea.value.length;
    counter.textContent = `${length} / ${maxChars}`;
    const percent = Math.min(100, (length / maxChars) * 100);
    meterFill.style.width = percent + '%';
    meter.classList.toggle('is-warn', length > maxChars * 0.8 && length < maxChars);
    meter.classList.toggle('is-limit', length >= maxChars);
  }

  function clearError() { hide(formError); formError.textContent = ''; }
  function setError(message) { formError.textContent = message; show(formError); }

  function resetForm() {
    const edit = isEditMode();
    const story = guest.story;

    editorTitle.textContent = edit ? 'Изменить мою историю' : 'Расскажите свою историю';
    editorSubtitle.textContent = edit
      ? 'После правок история снова уйдёт на проверку.'
      : 'Мы проверим её и опубликуем в общей карусели.';

    nameInput.value = edit && story ? story.name : '';
    textarea.value = edit && story ? story.text : '';
    submitBtn.textContent = edit ? 'Сохранить изменения' : 'Отправить историю';

    // Фото: в режиме правки показываем уже приложенное
    photoDataUrl = null;
    photoRemoved = false;
    hasExistingPhoto = Boolean(edit && story && story.photo);
    photoInput.value = '';
    if (hasExistingPhoto) {
      photoPreviewImg.src = story.photo;
      photoPreviewImg.alt = 'Фотография из вашей истории';
      photoName.textContent = 'Текущее фото';
      show(photoPreview);
    } else {
      photoPreviewImg.removeAttribute('src');
      photoName.textContent = 'Фотография не выбрана';
      hide(photoPreview);
    }

    clearError();
    submitBtn.disabled = false;
    updateMeter();
  }

  function openModal() {
    lastFocused = document.activeElement;
    resetForm();
    hide(thanksView);
    show(editorView);
    show(modal);
    document.body.style.overflow = 'hidden';
    pauseAutoplay();
    setTimeout(() => (nameInput.value ? textarea : nameInput).focus(), 60);
  }

  function closeModal() {
    hide(modal);
    document.body.style.overflow = '';
    if (lastFocused instanceof HTMLElement) lastFocused.focus();
    scheduleAutoplay();
  }

  document.getElementById('share-btn').addEventListener('click', openModal);
  document.getElementById('header-share').addEventListener('click', openModal);
  document.getElementById('editor-close').addEventListener('click', closeModal);
  document.getElementById('thanks-done').addEventListener('click', closeModal);
  modal.querySelector('[data-close]').addEventListener('click', closeModal);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modal.hidden) closeModal();
  });

  textarea.addEventListener('input', () => {
    updateMeter();
    if (textarea.value.trim()) clearError();
  });

  nameInput.addEventListener('input', () => {
    if (nameInput.value.trim()) clearError();
  });

  // Фото: только одно изображение
  photoInput.addEventListener('change', () => {
    const file = photoInput.files && photoInput.files[0];
    if (!file) return;

    if (!file.type.startsWith('image/')) {
      setError('Можно приложить только изображение');
      photoInput.value = '';
      return;
    }
    if (file.size > MAX_PHOTO_BYTES) {
      setError('Фотография слишком большая: максимум 5 МБ');
      photoInput.value = '';
      return;
    }

    const reader = new FileReader();
    reader.onload = () => {
      photoDataUrl = reader.result;
      photoRemoved = false;
      photoPreviewImg.src = photoDataUrl;
      photoName.textContent = file.name;
      show(photoPreview);
      clearError();
    };
    reader.readAsDataURL(file);
  });

  document.getElementById('photo-remove').addEventListener('click', () => {
    photoDataUrl = null;
    photoInput.value = '';
    hide(photoPreview);
    photoPreviewImg.removeAttribute('src');
    if (hasExistingPhoto) {
      photoRemoved = true;                 // фото удалится при сохранении
      photoName.textContent = 'Фото будет удалено';
    } else {
      photoName.textContent = 'Фотография не выбрана';
    }
  });

  submitBtn.addEventListener('click', async () => {
    const edit = isEditMode();
    const name = nameInput.value.trim();
    const text = textarea.value.trim();

    if (!name) {
      setError('Укажите имя');
      nameInput.focus();
      return;
    }
    if (!text) {
      setError('История не может быть пустой');
      textarea.focus();
      return;
    }
    if (textarea.value.length > maxChars) {
      setError(`Слишком длинная история: максимум ${maxChars} символов`);
      return;
    }

    const payload = { name, text };
    if (edit) {
      // "" — оставить текущее фото, null — удалить, data-URL — заменить
      payload.photo = photoDataUrl ? photoDataUrl : (photoRemoved ? null : '');
    } else {
      payload.photo = photoDataUrl;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = edit ? 'Сохраняем…' : 'Отправляем…';
    clearError();

    try {
      await api(edit ? API_MINE : API_STORIES, {
        method: edit ? 'PATCH' : 'POST',
        body: JSON.stringify(payload),
      });

      await loadGuest();               // сервер уже помнит устройство
      updateCta();

      if (edit) {
        thanksTitle.textContent = 'Изменения сохранены';
        thanksText.textContent = 'История снова ушла на проверку. После публикации она появится в карусели обновлённой.';
        thanksNote.textContent = 'Спасибо, что дополнили свою историю.';
      } else {
        // Купон показываем только при первой отправке истории
        thanksTitle.textContent = 'Спасибо! История отправлена';
        thanksText.textContent = 'Ты можешь получить купон на бесплатный напиток при показе этого экрана бариста.';
        thanksNote.textContent = 'После проверки история появится в общей карусели.';
      }

      hide(editorView);
      show(thanksView);
    } catch (error) {
      setError(error.message);
      submitBtn.disabled = false;
      submitBtn.textContent = edit ? 'Сохранить изменения' : 'Отправить историю';
    }
  });

  /* =====================================================================
    4b. Окошко настроек: масштаб, автораскрытие, скорость слайд-шоу
     ===================================================================== */

  const settingsModal = document.getElementById('settings-modal');
  const settingsBtn = document.getElementById('header-settings');
  const scaleInput = document.getElementById('scale-input');
  const scaleValue = document.getElementById('scale-value');
  const autoExpandInput = document.getElementById('autoexpand-input');
  const speedInput = document.getElementById('speed-input');
  const speedHint = document.getElementById('speed-hint');

  let settingsLastFocused = null;

  /** Заполняем окошко текущими значениями. Скорость показываем в секундах. */
  function syncSettingsForm() {
    scaleInput.value = String(uiScale);
    scaleValue.textContent = uiScale + '%';
    autoExpandInput.checked = autoExpand;
    speedInput.value = String(autoplaySec === null ? toSeconds(serverAutoplayMs) : autoplaySec);
    updateSpeedHint();
  }

  function updateSpeedHint() {
    speedHint.textContent = autoplaySec === null
      ? `Сейчас — как настроено в кофейне: ${toSeconds(serverAutoplayMs)} с`
      : `Ваша скорость: ${autoplaySec} с. Вернуть настройку кофейни — кнопкой «Сбросить».`;
  }

  /** Раскрыть или свернуть активную историю и пересчитать высоту карусели. */
  function refreshActiveSlide(expanded) {
    const active = carousel.track.children[carousel.index];
    if (!active) return;
    setSlideExpanded(active, expanded);
    refreshClampState(active);
    syncViewportHeight();
  }

  function openSettings() {
    settingsLastFocused = document.activeElement;
    syncSettingsForm();
    show(settingsModal);
    settingsBtn.setAttribute('aria-expanded', 'true');
    document.body.style.overflow = 'hidden';
    pauseAutoplay();                 // пока гость настраивает, слайдшоу стоит
    setTimeout(() => scaleInput.focus(), 60);
  }

  function closeSettings() {
    hide(settingsModal);
    settingsBtn.setAttribute('aria-expanded', 'false');
    document.body.style.overflow = '';
    if (settingsLastFocused instanceof HTMLElement) settingsLastFocused.focus();
    scheduleAutoplay();
  }

  settingsBtn.addEventListener('click', openSettings);
  document.getElementById('settings-close').addEventListener('click', closeSettings);
  document.getElementById('settings-done').addEventListener('click', closeSettings);
  settingsModal.querySelector('[data-close]').addEventListener('click', closeSettings);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !settingsModal.hidden) closeSettings();
  });

  // Масштаб применяем на каждое движение ползунка — гость сразу видит результат.
  scaleInput.addEventListener('input', () => {
    uiScale = Number(scaleInput.value) || DEFAULT_SCALE;
    scaleValue.textContent = uiScale + '%';
    applyUiScale();
    savePrefs();
  });

  autoExpandInput.addEventListener('change', () => {
    autoExpand = autoExpandInput.checked;
    savePrefs();
    refreshActiveSlide(autoExpand);
  });

  // Скорость в секундах. Ровно серверное значение — значит, от переопределения
  // отказываемся: пусть работает настройка кофейни.
  speedInput.addEventListener('change', () => {
    const value = Math.round(Number(speedInput.value));
    const serverSeconds = toSeconds(serverAutoplayMs);
    autoplaySec = !Number.isFinite(value) || value <= 0 || value === serverSeconds
      ? null
      : Math.min(MAX_AUTOPLAY_SEC, Math.max(MIN_AUTOPLAY_SEC, value));
    autoplayMs = autoplaySec === null ? serverAutoplayMs : autoplaySec * 1000;
    speedInput.value = String(toSeconds(autoplayMs));
    savePrefs();
    updateSpeedHint();
    // Пока окошко открыто, слайд-шоу стоит — запустим его при закрытии.
    if (settingsModal.hidden) scheduleAutoplay();
  });

  document.getElementById('settings-reset').addEventListener('click', () => {
    uiScale = DEFAULT_SCALE;
    autoExpand = false;
    autoplaySec = null;
    autoplayMs = serverAutoplayMs;
    applyUiScale();
    savePrefs();
    syncSettingsForm();
    refreshActiveSlide(false);
    if (settingsModal.hidden) scheduleAutoplay();
  });

  /* =====================================================================
     5. Старт
     ===================================================================== */

  async function loadGuest() {
    try {
      const data = await api(API_ME);
      guest = { hasStory: Boolean(data.hasStory), story: data.story || null };
    } catch {
      guest = { hasStory: false, story: null };
    }
    updateCta();
  }

  /** Ширина карточки: число из настроек кофейни либо «адаптивная» формула.
      Переменную читает CSS (.carousel/.stage/.hero), поэтому менять её достаточно здесь. */
  function applyCarouselWidth(width, adaptive) {
    const pixels = Number(width);
    const value = adaptive === true
      ? ADAPTIVE_WIDTH
      : `${Number.isFinite(pixels) && pixels > 0 ? pixels : DEFAULT_CAROUSEL_WIDTH}px`;
    document.documentElement.style.setProperty('--content-cap', value);
  }

  /** Применяем настройки страницы (скорость прокрутки, лимит символов, ширина карточки). */
  function applySettings(settings) {
    if (!settings) return;
    const ms = Number(settings.autoplay_ms);
    const chars = Number(settings.max_text_length);
    serverAutoplayMs = Number.isFinite(ms) && ms >= 1000 ? ms : DEFAULT_AUTOPLAY_MS;
    autoplayMs = autoplaySec === null ? serverAutoplayMs : autoplaySec * 1000;
    maxChars = Number.isFinite(chars) && chars >= 50 ? chars : DEFAULT_MAX_CHARS;

    // Ширину ставим до отрисовки карусели: от неё зависит и перенос текста,
    // и высота карточки, которую считает measureSlideHeight().
    applyCarouselWidth(settings.carousel_width, settings.carousel_width_auto);

    textarea.maxLength = maxChars;
    updateMeter();
    updateCta();
    scheduleAutoplay();   // перезапуск с новой скоростью, если карусель уже есть
  }

  async function loadStories() {
    // 1. Сразу рисуем то, что лежит в кэше — не ждём сеть (офлайн-режим).
    const cached = readCache(CACHE_KEY);
    const cachedStories = cached && Array.isArray(cached.stories) ? cached.stories : null;
    if (cached) applySettings(cached.settings);
    if (cachedStories && cachedStories.length) renderCarousel(cachedStories);

    // 2. Догоняем сетью: обновляем кэш и перерисовываем.
    try {
      const data = await api(API_STORIES);
      const stories = data.stories || [];
      writeCache(CACHE_KEY, { stories, settings: data.settings || null });
      applySettings(data.settings);
      renderCarousel(stories);
    } catch {
      // Сеть недоступна. Если кэш уже показан — оставляем его на экране.
      if (cachedStories && cachedStories.length) return;
      show(carousel.empty);
      hide(carousel.controls);
      hide(carousel.progress);
      carousel.empty.querySelector('p').textContent = 'Не удалось загрузить истории. Обновите страницу.';
    }
  }

  /* Оболочка в кеше телефона: гостевая страница открывается офлайн. Версию
     берём из адреса самого файла — при деплое она меняется, и старый кеш
     выбрасывается (тот же Service Worker, что и на основном сайте). */
  function setupServiceWorker() {
    if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
    const script = Array.from(document.scripts).find((node) => node.src && node.src.includes('/static/stories/app.js'));
    const version = script ? new URL(script.src).searchParams.get('v') || '' : '';
    navigator.serviceWorker.register(`/sw.js?v=${version}`, { scope: '/' }).catch(() => {
      // Не получилось — страница работает как раньше, просто без офлайна.
    });
  }

  document.getElementById('year').textContent = String(new Date().getFullYear());
  loadPrefs();                     // масштаб, автораскрытие и своя скорость слайд-шоу
  textarea.maxLength = maxChars;
  updateMeter();
  updateCta();
  loadGuest();
  loadStories();
  setupServiceWorker();
})();
