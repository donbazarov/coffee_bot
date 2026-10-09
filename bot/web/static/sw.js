/* НЕФТЬ · Crew Desk — Service Worker (app shell).
 *
 * Зачем: приложение, сохранённое на домашний экран, при каждом запуске заново
 * скачивало HTML, CSS и JS. Если в этот момент соединение до сервера «висит»
 * (у нас бывает до половины новых соединений), первый экран не появлялся
 * 10–15 секунд. Теперь оболочка лежит на телефоне и рисуется мгновенно, а сеть
 * догоняет в фоне.
 *
 * Правила простые:
 *   • /static/* — из кеша (ссылки версионированные: app.css?v=<hash>);
 *   • переход между страницами — сначала сеть (3 с), потом кеш;
 *   • /api/, /auth/, /avatars/, не-GET и «обновить страницу» — всегда в сеть.
 *
 * Версия кеша берётся из ?v= в адресе самого файла (его подставляет app.js),
 * поэтому после деплоя старый кеш удаляется автоматически.
 */

const VERSION = new URL(self.location.href).searchParams.get('v') || 'dev';
const CACHE = `neft-shell-${VERSION}`;

/* Сколько ждём сервер при переходе на страницу, прежде чем показать кеш. */
const NAVIGATION_TIMEOUT_MS = 3000;

/* Столько даём каждому файлу при укладке оболочки: на «зависшем» соединении
   установка воркера не должна висеть бесконечно. */
const ASSET_TIMEOUT_MS = 5000;

/* Что не трогаем вообще: данные, авторизация и фото должны быть свежими. */
const PASS_THROUGH = ['/api/', '/auth/', '/avatars/'];

/* Оболочка приложения: кладётся в кеш при установке. */
const SHELL = [
  `/static/fonts.css?v=${VERSION}`,
  `/static/theme-boot.js?v=${VERSION}`,
  `/static/app.css?v=${VERSION}`,
  `/static/app.js?v=${VERSION}`,
  '/static/icons/logo.svg',
  '/static/icons/favicon.ico',
  '/static/icons/favicon-96x96.png',
  '/static/icons/apple-touch-icon.png',
  '/site.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // Часть файлов может не дойти — установку из-за этого не срываем:
    // без Service Worker'а мы снова останемся с медленным первым экраном.
    await Promise.allSettled(SHELL.map((url) => withTimeout(cache.add(url), ASSET_TIMEOUT_MS)));
    // Стартовая страница как запасной вариант, если сеть не ответит вообще.
    try {
      await withTimeout(cache.add('/'), ASSET_TIMEOUT_MS);
    } catch (error) {
      // Страница попадёт в кеш при первом успешном переходе.
    }
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (PASS_THROUGH.some((prefix) => url.pathname.startsWith(prefix))) return;

  if (url.pathname.startsWith('/static/')) {
    event.respondWith(fromCache(request));
    return;
  }

  if (request.mode === 'navigate') {
    // Явное обновление страницы пользователем («потянуть вниз», Cmd+R) —
    // не подсовываем кеш, иначе из него не выбраться.
    if (request.cache === 'reload') return;
    event.respondWith(navigateFirst(request));
  }
});

/* — статика ——————————————————————————————————————————————————————————————— */

async function fromCache(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response && response.ok) cache.put(request, response.clone());
    return response;
  } catch (error) {
    // Новая версия файла не доехала — показываем предыдущую, но стили останутся.
    const previous = await cache.match(request, { ignoreSearch: true });
    if (previous) return previous;
    throw error;
  }
}

/* — переходы между страницами ——————————————————————————————————————————— */

async function navigateFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await withTimeout(fetch(request), NAVIGATION_TIMEOUT_MS);
    if (response && response.ok) cache.put(request, response.clone());
    return response;
  } catch (error) {
    const cached = (await cache.match(request)) || (await cache.match('/'));
    if (!cached) throw error;
    // Отдаём кеш сразу, а сеть доспрашиваем в фоне.
    refreshInBackground(request, cached.clone());
    return cached;
  }
}

async function refreshInBackground(request, cachedCopy) {
  try {
    const fresh = await fetch(request);
    if (!fresh || !fresh.ok) return;
    const [freshHtml, cachedHtml] = await Promise.all([
      fresh.clone().text(),
      cachedCopy.text(),
    ]);
    const cache = await caches.open(CACHE);
    await cache.put(request, fresh);
    // Если на сервере сменилось состояние входа (истекла сессия или наоборот),
    // кеш покажет не тот экран — в этом единственном случае перезагружаем.
    if (loginState(freshHtml) !== loginState(cachedHtml)) {
      const clients = await self.clients.matchAll({ type: 'window' });
      clients.forEach((client) => client.postMessage('reload'));
    }
  } catch (error) {
    // Сеть так и не ответила — оставляем кеш до следующего запуска.
  }
}

function loginState(html) {
  const match = /data-authenticated="([^"]*)"/.exec(html || '');
  return match ? match[1] : '';
}

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout')), ms);
    promise.then(
      (value) => { clearTimeout(timer); resolve(value); },
      (error) => { clearTimeout(timer); reject(error); },
    );
  });
}
