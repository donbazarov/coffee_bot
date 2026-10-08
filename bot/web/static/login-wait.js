/* =========================================================================
   Экран ожидания подтверждения входа из приложения Telegram.

   Опрашивает /api/auth/telegram/status. Когда бот подтвердит вход,
   сервер в том же ответе ставит сессионную cookie, и страница
   переходит на нужный адрес.
   ========================================================================= */

(() => {
  'use strict';

  const POLL_INTERVAL_MS = 2000;
  const textNode = document.querySelector('#wait-text');
  const errorNode = document.querySelector('#wait-error');
  const indicator = document.querySelector('#wait-indicator');

  let stopped = false;

  function setText(message) {
    if (textNode) textNode.textContent = message;
  }

  function stop(message) {
    stopped = true;
    if (indicator) indicator.hidden = true;
    if (message && errorNode) {
      errorNode.textContent = message;
      errorNode.hidden = false;
    }
  }

  async function poll() {
    if (stopped) return;

    try {
      const response = await fetch('/api/auth/telegram/status', {
        credentials: 'same-origin',
        cache: 'no-store',
      });
      const data = await response.json().catch(() => ({}));

      if (data.status === 'approved') {
        stop();
        setText('Готово! Открываем сайт…');
        window.location.replace(data.next || '/');
        return;
      }

      if (data.status === 'denied' || data.status === 'unknown') {
        stop(data.message || 'Ссылка недействительна. Попробуйте войти заново.');
        return;
      }

      setText('Ждём подтверждения…');
    } catch (error) {
      setText('Нет связи с сервером, пробуем снова…');
    }

    setTimeout(poll, POLL_INTERVAL_MS);
  }

  poll();
})();
