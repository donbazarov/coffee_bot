/* ПРОМПТ УСТАНОВКИ PWA (ванильный JS, без библиотек).
 *
 * Показываем баннер «установите приложение» мобильным посетителям, которые
 * открыли сайт в браузере, а не как установленное приложение:
 *   • Android/Chrome — перехватываем beforeinstallprompt и по кнопке вызываем
 *     системное окно установки (deferredPrompt.prompt());
 *   • iOS/Safari — beforeinstallprompt там не поддерживается, поэтому сразу
 *     показываем пошаговую инструкцию «Поделиться → На экран Домой».
 *
 * Условия показа: устройство мобильное (User-Agent ИЛИ ширина экрана ≤ 768)
 * И сайт НЕ в режиме standalone. Закрытие не запоминается — баннер вернётся,
 * пока приложение не установлено.
 *
 * Разметка (#pwa-install) лежит в templates/index.html и рендерится только для
 * авторизованных сотрудников, поэтому на странице входа баннера нет.
 */
(function () {
  'use strict';

  var banner = document.getElementById('pwa-install');
  if (!banner) return; // страница входа/гость — разметки нет

  var stepsEl = document.getElementById('pwa-install-steps');
  var acceptBtn = document.getElementById('pwa-install-accept');
  var dismissBtn = document.getElementById('pwa-install-dismiss');
  var closeBtn = document.getElementById('pwa-install-close');

  var ua = navigator.userAgent || '';
  // iPadOS 13+ представляется как «Macintosh» — отличаем по числу точек касания.
  var isIOS = /iPad|iPhone|iPod/.test(ua) ||
    (/Macintosh/.test(ua) && (navigator.maxTouchPoints || 0) > 1);
  var isMobileUA = /Android|iPhone|iPad|iPod|Mobile|Windows Phone|Opera Mini/i.test(ua);

  function isMobile() {
    return isMobileUA || window.innerWidth <= 768;
  }

  function isStandalone() {
    return window.matchMedia('(display-mode: standalone)').matches ||
      window.matchMedia('(display-mode: fullscreen)').matches ||
      window.matchMedia('(display-mode: minimal-ui)').matches ||
      navigator.standalone === true;
  }

  // Не мобильное устройство или уже запущено как приложение — баннер не нужен.
  if (!isMobile() || isStandalone()) return;

  var deferredPrompt = null;
  var lastFocused = null;

  function show(mode) {
    banner.classList.toggle('is-ios', mode === 'ios');
    if (stepsEl) stepsEl.hidden = mode !== 'ios';
    lastFocused = document.activeElement;
    banner.hidden = false;
    var target = mode === 'ios' ? closeBtn : (acceptBtn || closeBtn);
    if (target) target.focus();
  }

  function hide() {
    banner.hidden = true;
    if (lastFocused && typeof lastFocused.focus === 'function') lastFocused.focus();
  }

  /* — Android / Chromium: системное окно установки ——————————————————————— */

  window.addEventListener('beforeinstallprompt', function (event) {
    // Гасим встроенную мини-плашку Chrome — показываем свой баннер.
    event.preventDefault();
    deferredPrompt = event;
    show('android');
  });

  if (acceptBtn) {
    acceptBtn.addEventListener('click', function () {
      if (!deferredPrompt) return;
      deferredPrompt.prompt();
      var choice = deferredPrompt.userChoice;
      deferredPrompt = null;
      hide();
      if (choice && typeof choice.then === 'function') {
        choice.then(function () { /* результат не важен: баннер уже скрыт */ });
      }
    });
  }

  // Пользователь установил приложение (в т.ч. вне кнопки) — прячем баннер.
  window.addEventListener('appinstalled', hide);

  /* — iOS / Safari: пошаговая инструкция ————————————————————————————————— */

  if (isIOS) show('ios');

  /* — Закрытие баннера ——————————————————————————————————————————————————— */

  [dismissBtn, closeBtn].forEach(function (btn) {
    if (btn) btn.addEventListener('click', hide);
  });

  // Клик по затемнённому фону (вне карточки) тоже закрывает баннер.
  banner.addEventListener('click', function (event) {
    if (event.target === banner) hide();
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && !banner.hidden) hide();
  });

  // Если приложение открылось как standalone (например, только что установлено
  // и запущено с домашнего экрана) — баннер держать не нужно.
  var standaloneQuery = window.matchMedia('(display-mode: standalone)');
  var onStandaloneChange = function (event) { if (event.matches) hide(); };
  if (standaloneQuery.addEventListener) standaloneQuery.addEventListener('change', onStandaloneChange);
  else if (standaloneQuery.addListener) standaloneQuery.addListener(onStandaloneChange);
})();
