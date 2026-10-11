/* МОБИЛЬНЫЕ ТАЧ-ЖЕСТЫ.
 *
 *   • В чате (открытая переписка) свайп от левого края возвращает к списку
 *     чатов: тред «съезжает» вправо, показывая контакты/темы. Сэндвич-меню
 *     открывается только кнопкой (свайпом — нет).
 *   • Сэндвич-меню закрывается свайпом влево (по нему же).
 *   • Pull-to-Refresh — собственный индикатор (в iOS-PWA нативного нет);
 *     работает только когда страница в самом верху и чат не открыт.
 *
 * Файл самостоятельный: использует DOM и `NeftChat.back()` (если есть).
 * На устройствах без тача ничего не делает.
 */
(function () {
  'use strict';

  const touchCapable = ('ontouchstart' in window) || (navigator.maxTouchPoints || 0) > 0;
  if (!touchCapable) return;

  const EDGE = 30;             // «горячая зона» у левого края
  const CHAT_OPEN_FRACTION = 0.3;
  const CLOSE_DISTANCE = 60;   // свайп влево, достаточный для закрытия меню
  const PTR_TRIGGER = 84;
  const PTR_MAX = 140;
  const MOBILE_QUERY = '(max-width: 760px)';

  const drawer = document.getElementById('drawer');
  const backdrop = document.getElementById('drawer-backdrop');
  const chatView = document.getElementById('chat-view');
  const chatMain = document.getElementById('chat-main');

  const drawerWidth = () => (drawer ? drawer.getBoundingClientRect().width || 320 : 320);
  const isDrawerOpen = () => Boolean(drawer && drawer.classList.contains('is-open'));
  const dialogOpen = () => Boolean(document.querySelector('dialog[open]'));
  const inMatrix = (target) => Boolean(target && target.closest && target.closest('.matrix-scroll'));
  const isMobile = () => window.matchMedia(MOBILE_QUERY).matches;
  const chatVisible = () => Boolean(chatView && !chatView.hidden && chatView.classList.contains('is-visible'));
  const chatThreadOpen = () => chatVisible() && isMobile()
    && Boolean(chatView && chatView.classList.contains('is-thread-open'));

  /* --- Индикатор Pull-to-Refresh ----------------------------------------- */

  let ptr = document.getElementById('ptr-indicator');
  if (!ptr) {
    ptr = document.createElement('div');
    ptr.id = 'ptr-indicator';
    ptr.className = 'ptr-indicator';
    ptr.setAttribute('aria-hidden', 'true');
    ptr.innerHTML = '<span class="ptr-spinner"></span>';
    document.body.appendChild(ptr);
  }
  const spinner = ptr.querySelector('.ptr-spinner');
  let pull = 0;

  function showPtr(value) {
    pull = value;
    ptr.classList.add('is-visible');
    ptr.style.transform = `translateY(${Math.min(value, PTR_MAX)}px)`;
    ptr.classList.toggle('is-ready', value >= PTR_TRIGGER);
    if (spinner) spinner.style.transform = `rotate(${Math.min(value, PTR_MAX) * 1.6}deg)`;
  }

  function hidePtr() {
    pull = 0;
    ptr.classList.remove('is-visible', 'is-ready');
    ptr.style.transform = '';
    if (spinner) spinner.style.transform = '';
  }

  /* --- Сэндвич-меню: предпросмотр свайпом влево (закрытие) ---------------- */

  function previewDrawer(progress) {
    if (!drawer) return;
    const clamped = Math.max(0, Math.min(1, progress));
    drawer.classList.add('is-dragging');
    drawer.style.transform = `translateX(${(-102 + 102 * clamped).toFixed(2)}%)`;
    if (backdrop) {
      backdrop.style.opacity = String(clamped);
      backdrop.style.pointerEvents = clamped > 0.02 ? 'auto' : 'none';
    }
  }

  function settleDrawer(open) {
    if (!drawer) return;
    drawer.classList.remove('is-dragging');
    if (open) {
      drawer.style.transform = 'translateX(0)';
      document.getElementById('drawer-open')?.click();
      setTimeout(() => {
        drawer.style.transform = '';
        if (backdrop) { backdrop.style.opacity = ''; backdrop.style.pointerEvents = ''; }
      }, 320);
    } else {
      drawer.style.transform = '';
      if (backdrop) { backdrop.style.opacity = ''; backdrop.style.pointerEvents = ''; }
    }
  }

  /* --- Чат: свайп к списку ------------------------------------------------ */

  function previewChat(offset) {
    if (!chatMain) return;
    chatMain.classList.add('is-dragging');
    chatMain.style.transform = `translateX(${Math.max(0, offset)}px)`;
  }

  function settleChat(open) {
    if (!chatMain) return;
    chatMain.classList.remove('is-dragging');
    if (open) {
      // back() снимает класс треда и инлайновый сдвиг — тред уезжает вправо.
      window.NeftChat?.back?.();
    } else {
      chatMain.style.transform = 'translateX(0)';
      setTimeout(() => { chatMain.style.transform = ''; }, 330);
    }
  }

  /* --- Жесты -------------------------------------------------------------- */

  let mode = null;   // 'drawer-close' | 'chat' | 'ptr' | 'ptr-maybe'
  let startX = 0;
  let startY = 0;
  let lastX = 0;

  document.addEventListener('touchstart', (event) => {
    if (event.touches.length !== 1) { mode = null; return; }
    const touch = event.touches[0];
    startX = lastX = touch.clientX;
    startY = touch.clientY;
    if (dialogOpen()) { mode = null; return; }

    if (isDrawerOpen()) {
      mode = drawer && drawer.contains(event.target) ? 'drawer-close' : null;
      return;
    }
    if (startX < EDGE && chatThreadOpen()) { mode = 'chat'; return; }
    if (!chatVisible() && window.scrollY <= 0 && !inMatrix(event.target)) { mode = 'ptr-maybe'; return; }
    mode = null;
  }, { passive: true });

  document.addEventListener('touchmove', (event) => {
    if (!mode || event.touches.length !== 1) return;
    const touch = event.touches[0];
    const dx = touch.clientX - startX;
    const dy = touch.clientY - startY;
    lastX = touch.clientX;

    if (mode === 'chat') {
      if (dx <= 0 || Math.abs(dy) > Math.abs(dx)) return;
      event.preventDefault();
      previewChat(dx);
      return;
    }
    if (mode === 'drawer-close') {
      if (dx >= 0 || Math.abs(dy) > Math.abs(dx)) return;
      event.preventDefault();
      previewDrawer(1 - Math.abs(dx) / (drawerWidth() * 0.9));
      return;
    }
    if (mode === 'ptr-maybe') {
      if (dy <= 0 || Math.abs(dx) > Math.abs(dy)) { mode = null; return; }
      event.preventDefault();
      mode = 'ptr';
      showPtr(dy * 0.5);
      return;
    }
    if (mode === 'ptr') {
      event.preventDefault();
      showPtr(Math.max(0, dy * 0.5));
    }
  }, { passive: false });

  document.addEventListener('touchend', () => {
    if (mode === 'chat') {
      settleChat(lastX - startX > window.innerWidth * CHAT_OPEN_FRACTION);
    } else if (mode === 'drawer-close') {
      if (startX - lastX > CLOSE_DISTANCE) window.Neft?.closeDrawer?.();
      else settleDrawer(false);
    } else if (mode === 'ptr') {
      if (pull >= PTR_TRIGGER) {
        ptr.classList.add('is-releasing');
        window.location.reload();
        return;
      }
      hidePtr();
    }
    mode = null;
  }, { passive: true });

  document.addEventListener('touchcancel', () => {
    if (mode === 'ptr') hidePtr();
    if (mode === 'drawer-close') settleDrawer(false);
    if (mode === 'chat') settleChat(false);
    mode = null;
  }, { passive: true });
})();
