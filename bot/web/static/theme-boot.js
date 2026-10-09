/* =========================================================================
   Тема и акцент применяются ДО первой отрисовки — чтобы не было вспышки
   неверной темы при загрузке.

   Скрипт синхронный и внешний: CSP разрешает только 'self', поэтому
   инлайновый <script> в <head> заблокировался бы. Значения читаются из
   localStorage (устройство), по умолчанию — «как в системе».

   Значения ключей: 'neft-theme' = system|light|dark, 'neft-accent' = #rrggbb.
   (В Релизе 2 те же ключи будут синхронизироваться с настройками аккаунта.)
   ========================================================================= */
(function () {
  'use strict';

  var root = document.documentElement;
  var THEME_KEY = 'neft-theme';
  var ACCENT_KEY = 'neft-accent';
  var DEFAULT_ACCENT = '#f47369';

  function readStored(key) {
    try { return localStorage.getItem(key); } catch (error) { return null; }
  }

  function systemTheme() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }

  /* Контрастный цвет текста на плашке акцента — по относительной яркости
     (WCAG, с линеаризацией каналов). Порог подобран так, чтобы коралловый и
     янтарный акценты получали тёмный текст, а синий/бирюзовый — светлый. */
  function contrastOn(hex) {
    var match = /^#?([0-9a-f]{6})$/i.exec(String(hex || ''));
    if (!match) return '#2a1310';
    var value = parseInt(match[1], 16);
    var channel = function (part) { part /= 255; return part <= 0.03928 ? part / 12.92 : Math.pow((part + 0.055) / 1.055, 2.4); };
    var luminance = 0.2126 * channel((value >> 16) & 255) + 0.7152 * channel((value >> 8) & 255) + 0.0722 * channel(value & 255);
    return luminance > 0.3 ? '#241512' : '#ffffff';
  }

  var pref = readStored(THEME_KEY) || 'system';
  var theme = pref === 'light' || pref === 'dark' ? pref : systemTheme();
  root.setAttribute('data-theme', theme);

  /* Тёмная полоса интерфейса браузера — под фон темы (CSS ещё не загружен,
     поэтому берём готовые значения фонов из токенов). */
  var metaColor = document.querySelector('meta[name="theme-color"]');
  if (metaColor) metaColor.setAttribute('content', theme === 'light' ? '#f4f1ea' : '#10110f');

  var accent = readStored(ACCENT_KEY) || DEFAULT_ACCENT;
  if (/^#[0-9a-f]{6}$/i.test(accent)) {
    root.style.setProperty('--accent', accent);
    root.style.setProperty('--accent-contrast', contrastOn(accent));
  }

  /* Пока выбрано «как в системе» — следим за сменой темы ОС. */
  if (window.matchMedia) {
    var query = window.matchMedia('(prefers-color-scheme: light)');
    var onChange = function () {
      if ((readStored(THEME_KEY) || 'system') === 'system') {
        root.setAttribute('data-theme', systemTheme());
      }
    };
    if (query.addEventListener) query.addEventListener('change', onChange);
    else if (query.addListener) query.addListener(onChange);
  }
})();
