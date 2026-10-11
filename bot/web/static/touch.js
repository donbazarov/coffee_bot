/* МОБИЛЬНЫЕ ТАЧ-ЖЕСТЫ.
 *
 *   • Открытие сэндвич-меню свайпом от левого края (clientX < 30) и закрытие
 *     свайпом влево. Меню «следует» за пальцем, отпускание доводит его до
 *     состояния через CSS-transition.
 *   • Pull-to-Refresh: собственный индикатор, потому что в iOS-PWA нативного
 *     обновления нет. Жест работает только когда страница в самом верху
 *     (window.scrollY === 0) и меню закрыто.
 *
 * Файл самостоятельный: использует только DOM и global `Neft.closeDrawer`
 * (если доступен). На устройствах без тача ничего не делает.
 */
(function () {
  'use strict';

  const touchCapable = ('ontouchstart' in window) || (navigator.maxTouchPoints || 0) > 0;
  if (!touchCapable) return;

  const EDGE = 30;             // «горячая зона» у левого края
  const OPEN_FRACTION = 0.33;  // доля ширины меню, после которой оно открывается
  const CLOSE_DISTANCE = 60;   // свайп влево, достаточный для закрытия
  const PTR_TRIGGER = 84;      // натяжение, после которого страница обновляется
  const PTR_MAX = 140;
  const DRAWER_TRANSITION_MS = 320;

  const drawer = document.getElementById('drawer');
  const backdrop = document.getElementById('drawer-backdrop');

  const drawerWidth = () => (drawer ? drawer.getBoundingClientRect().width || 320 : 320);
  const isDrawerOpen = () => Boolean(drawer && drawer.classList.contains('is-open'));
  const dialogOpen = () => Boolean(document.querySelector('dialog[open]'));
  const inMatrix = (target) => Boolean(target && target.closest && target.closest('.matrix-scroll'));

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

  /* --- Предпросмотр меню во время свайпа --------------------------------- */

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
      // Открытие делает штатный обработчик app.js (класс + backdrop + фокус).
      document.getElementById('drawer-open')?.click();
      setTimeout(() => {
        drawer.style.transform = '';
        if (backdrop) { backdrop.style.opacity = ''; backdrop.style.pointerEvents = ''; }
      }, DRAWER_TRANSITION_MS);
    } else {
      drawer.style.transform = '';
      if (backdrop) { backdrop.style.opacity = ''; backdrop.style.pointerEvents = ''; }
    }
  }

  /* --- Жесты -------------------------------------------------------------- */

  let mode = null;   // 'drawer' | 'drawer-close' | 'ptr' | 'ptr-maybe'
  let startX = 0;
  let startY = 0;
  let lastX = 0;
  let lastY = 0;

  document.addEventListener('touchstart', (event) => {
    if (event.touches.length !== 1) { mode = null; return; }
    const touch = event.touches[0];
    startX = lastX = touch.clientX;
    startY = lastY = touch.clientY;
    if (dialogOpen() || !drawer) { mode = null; return; }

    if (isDrawerOpen()) {
      mode = drawer.contains(event.target) ? 'drawer-close' : null;
      return;
    }
    if (startX < EDGE) { mode = 'drawer'; return; }
    if (window.scrollY <= 0 && !inMatrix(event.target)) { mode = 'ptr-maybe'; return; }
    mode = null;
  }, { passive: true });

  document.addEventListener('touchmove', (event) => {
    if (!mode || event.touches.length !== 1) return;
    const touch = event.touches[0];
    const dx = touch.clientX - startX;
    const dy = touch.clientY - startY;
    lastX = touch.clientX;
    lastY = touch.clientY;

    if (mode === 'drawer') {
      if (dx <= 0 || Math.abs(dy) > Math.abs(dx)) return; // влево/вертикально — не наше
      event.preventDefault();
      previewDrawer(dx / (drawerWidth() * 0.9));
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
    if (mode === 'drawer') {
      settleDrawer(lastX - startX > drawerWidth() * OPEN_FRACTION);
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
    if (mode === 'drawer' || mode === 'drawer-close') settleDrawer(isDrawerOpen());
    mode = null;
  }, { passive: true });
})();
