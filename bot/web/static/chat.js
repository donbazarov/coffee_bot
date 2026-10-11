/* ЧАТ: WebSocket-клиент и интерфейс (общий канал + личные сообщения).
 *
 * Зависит от window.Neft (app.js): api/showToast/escapeHtml/avatarMarkup/user.
 * Подключается к серверу по /chat/ws, сам переподключается при обрыве
 * (телефон уснул, сеть мигнула, контейнер перезапустился) и держит heartbeat.
 *
 * История: при открытии канала — последние 30 сообщений через HTTP; при
 * прокрутке вверх догружаем старее по `before_id`. Картинка сначала уходит на
 * /chat/upload (сырое тело), затем в WebSocket летит только её путь.
 */
(function () {
  'use strict';

  const view = document.getElementById('chat-view');
  if (!view || !window.Neft) return; // страница входа/гость — чата нет

  const { api, escapeHtml, avatarMarkup, showToast } = window.Neft;
  const ME = window.Neft.user.id;
  const PAGE = 30;
  const MAX_IMAGE_BYTES = 8 * 1024 * 1024;

  const el = {
    log: document.getElementById('chat-log'),
    state: document.getElementById('chat-state'),
    form: document.getElementById('chat-form'),
    input: document.getElementById('chat-input'),
    attach: document.getElementById('chat-attach'),
    file: document.getElementById('chat-file'),
    peers: document.getElementById('chat-peers'),
    hint: document.getElementById('chat-peer-hint'),
    tabs: Array.from(document.querySelectorAll('[data-chat-tab]')),
  };
  if (!el.log || !el.form || !el.input) return;

  const state = {
    channel: 'general',
    peerId: null,
    peers: [],
    people: new Map(),   // id -> { name, display_name, avatar_rev }
    messages: [],
    hasMore: false,
    loading: false,
    initialized: false,
    socket: null,
    reconnectDelay: 1000,
    reconnectTimer: null,
    heartbeat: null,
  };

  const personName = (id) => {
    const person = state.people.get(Number(id));
    return person ? (person.display_name || person.name) : 'Сотрудник';
  };
  const personAvatarRev = (id) => Number(state.people.get(Number(id))?.avatar_rev || 0);

  function setStatus(text, kind) {
    el.state.textContent = text || '';
    el.state.hidden = !text;
    el.state.classList.toggle('is-error', kind === 'error');
  }

  /* --- Разметка сообщений ------------------------------------------------- */

  function formatTime(iso) {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }

  function dayLabel(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    const sameDay = (a, b) => a.toDateString() === b.toDateString();
    const now = new Date();
    if (sameDay(date, now)) return 'Сегодня';
    if (sameDay(date, new Date(now.getTime() - 86400000))) return 'Вчера';
    return date.toLocaleDateString('ru-RU', { day: '2-digit', month: 'long' });
  }

  function linkify(text) {
    return escapeHtml(text)
      .replace(/(@[\w.\-]{1,64})/g, '<span class="chat-mention">$1</span>')
      .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  }

  function messageHtml(message) {
    const mine = Number(message.sender_id) === ME;
    const image = message.image_url
      ? `<a class="chat-image" href="${escapeHtml(message.image_url)}" target="_blank" rel="noopener"><img src="${escapeHtml(message.image_url)}" alt="Изображение" loading="lazy"></a>`
      : '';
    const text = message.text ? `<p class="chat-text">${linkify(message.text)}</p>` : '';
    return `<article class="chat-msg${mine ? ' is-mine' : ''}" data-id="${message.id}">
      ${mine ? '' : `<span class="chat-avatar">${avatarMarkup(message.sender_id, personAvatarRev(message.sender_id), personName(message.sender_id))}</span>`}
      <div class="chat-bubble">
        ${mine ? '' : `<span class="chat-author">${escapeHtml(personName(message.sender_id))}</span>`}
        ${image}${text}
        <time class="chat-time">${formatTime(message.created_at)}</time>
      </div>
    </article>`;
  }

  function nearBottom() {
    return el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight < 90;
  }

  function scrollToBottom() {
    el.log.scrollTop = el.log.scrollHeight;
  }

  function render({ preserveScroll = false } = {}) {
    const previousHeight = el.log.scrollHeight;
    let lastDay = null;
    el.log.innerHTML = state.messages.map((message) => {
      const day = String(message.created_at || '').slice(0, 10);
      let separator = '';
      if (day && day !== lastDay) {
        separator = `<div class="chat-day">${dayLabel(message.created_at)}</div>`;
        lastDay = day;
      }
      return separator + messageHtml(message);
    }).join('');
    if (preserveScroll) el.log.scrollTop = el.log.scrollHeight - previousHeight;
    else scrollToBottom();
  }

  function pushMessage(message) {
    if (state.messages.some((item) => item.id === message.id)) return;
    const stick = nearBottom();
    state.messages.push(message);
    render({ preserveScroll: !stick });
  }

  /* --- Загрузка истории --------------------------------------------------- */

  function query() {
    const params = new URLSearchParams({ channel: state.channel, limit: String(PAGE) });
    if (state.channel === 'dm' && state.peerId) params.set('peer_id', String(state.peerId));
    return params;
  }

  async function loadMessages({ older = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    const params = query();
    if (older && state.messages.length) params.set('before_id', String(state.messages[0].id));
    try {
      const data = await api(`/chat/messages?${params.toString()}`);
      state.hasMore = Boolean(data.has_more);
      const list = data.messages || [];
      state.messages = older ? list.concat(state.messages) : list;
      render({ preserveScroll: older });
      setStatus('');
    } catch (error) {
      setStatus(error.message, 'error');
    } finally {
      state.loading = false;
    }
  }

  async function loadPeers() {
    try {
      const data = await api('/chat/users');
      state.peers = data.users || [];
      state.peers.forEach((person) => state.people.set(Number(person.id), person));
      el.peers.innerHTML = state.peers.length
        ? state.peers.map((person) => `<button class="chat-peer" type="button" data-peer-id="${person.id}">
            <span class="chat-avatar">${avatarMarkup(person.id, person.avatar_rev, person.display_name || person.name)}</span>
            <span class="chat-peer-name">${escapeHtml(person.display_name || person.name)}</span>
          </button>`).join('')
        : '<p class="empty-state">Пока никого нет</p>';
      highlightPeer();
    } catch (error) {
      el.peers.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    }
  }

  function highlightPeer() {
    el.peers.querySelectorAll('.chat-peer').forEach((node) => {
      node.classList.toggle('is-active', Number(node.dataset.peerId) === Number(state.peerId));
    });
  }

  function selectChannel(channel, peerId = null) {
    state.channel = channel;
    state.peerId = channel === 'dm' ? peerId : null;
    el.tabs.forEach((tab) => {
      const active = tab.dataset.chatTab === channel;
      tab.classList.toggle('is-selected', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    el.peers.hidden = channel !== 'dm';
    const needsPeer = channel === 'dm' && !state.peerId;
    el.form.hidden = needsPeer;
    el.hint.hidden = !needsPeer;
    highlightPeer();
    if (needsPeer) {
      state.messages = [];
      el.log.innerHTML = '';
      setStatus('');
      el.hint.textContent = 'Выберите сотрудника, чтобы открыть личную переписку.';
      return;
    }
    state.messages = [];
    state.hasMore = false;
    loadMessages();
  }

  /* --- WebSocket ---------------------------------------------------------- */

  function socketUrl() {
    return `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/chat/ws`;
  }

  function connect() {
    if (state.socket && (state.socket.readyState === WebSocket.CONNECTING || state.socket.readyState === WebSocket.OPEN)) return;
    clearTimeout(state.reconnectTimer);
    let socket;
    try {
      socket = new WebSocket(socketUrl());
    } catch (error) {
      scheduleReconnect();
      return;
    }
    state.socket = socket;
    socket.addEventListener('open', () => {
      state.reconnectDelay = 1000;
      setStatus('');
      startHeartbeat();
    });
    socket.addEventListener('message', (event) => {
      let payload;
      try { payload = JSON.parse(event.data); } catch (error) { return; }
      if (payload.type === 'message' && payload.message) handleIncoming(payload.message);
      else if (payload.type === 'error') showToast(payload.detail || 'Ошибка чата');
    });
    socket.addEventListener('close', () => {
      stopHeartbeat();
      state.socket = null;
      setStatus('Связь потеряна — переподключаемся…', 'error');
      scheduleReconnect();
    });
    socket.addEventListener('error', () => { try { socket.close(); } catch (error) { /* уже закрыт */ } });
  }

  function scheduleReconnect() {
    clearTimeout(state.reconnectTimer);
    const delay = Math.min(state.reconnectDelay, 15000);
    state.reconnectTimer = setTimeout(connect, delay);
    state.reconnectDelay = Math.min(Math.round(delay * 1.7), 15000);
  }

  function startHeartbeat() {
    stopHeartbeat();
    state.heartbeat = setInterval(() => sendRaw({ type: 'ping' }), 25000);
  }

  function stopHeartbeat() {
    if (state.heartbeat) { clearInterval(state.heartbeat); state.heartbeat = null; }
  }

  function sendRaw(payload) {
    if (state.socket && state.socket.readyState === WebSocket.OPEN) {
      state.socket.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }

  function relevant(message) {
    if (state.channel === 'general') return message.recipient_id === null;
    if (message.recipient_id === null) return false;
    const peer = Number(state.peerId);
    return (Number(message.sender_id) === ME && Number(message.recipient_id) === peer)
      || (Number(message.sender_id) === peer && Number(message.recipient_id) === ME);
  }

  function handleIncoming(message) {
    if (relevant(message)) pushMessage(message);
  }

  function sendMessage(text, imageUrl) {
    const payload = { type: 'message', text: text || '', image_url: imageUrl || null };
    if (state.channel === 'dm') payload.recipient_id = state.peerId;
    if (!sendRaw(payload)) {
      showToast('Нет связи с чатом — сообщение не отправлено');
      return false;
    }
    return true;
  }

  /* --- Ввод и вложения ---------------------------------------------------- */

  function autoGrow() {
    el.input.style.height = 'auto';
    el.input.style.height = `${Math.min(el.input.scrollHeight, 132)}px`;
  }

  el.input.addEventListener('input', autoGrow);
  el.input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      el.form.requestSubmit();
    }
  });

  el.form.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = el.input.value.trim();
    if (!text) return;
    if (sendMessage(text, null)) {
      el.input.value = '';
      autoGrow();
    }
  });

  el.attach.addEventListener('click', () => el.file.click());
  el.file.addEventListener('change', async () => {
    const file = el.file.files && el.file.files[0];
    el.file.value = '';
    if (!file) return;
    if (!file.type.startsWith('image/')) { showToast('Можно прикрепить только изображение'); return; }
    if (file.size > MAX_IMAGE_BYTES) { showToast('Изображение больше 8 МБ'); return; }
    if (state.channel === 'dm' && !state.peerId) { showToast('Сначала выберите собеседника'); return; }
    setStatus('Отправляем изображение…');
    try {
      const uploaded = await api('/chat/upload', {
        method: 'POST',
        body: file,
        headers: { 'Content-Type': file.type || 'application/octet-stream' },
      });
      sendMessage('', uploaded.url);
      setStatus('');
    } catch (error) {
      setStatus('');
      showToast(error.message);
    }
  });

  el.log.addEventListener('scroll', () => {
    if (el.log.scrollTop <= 24 && state.hasMore && !state.loading) loadMessages({ older: true });
  });

  document.addEventListener('visibilitychange', () => { if (!document.hidden) connect(); });
  window.addEventListener('online', connect);

  /* --- Настройка уведомлений чата (в профиле) ----------------------------- */

  function setupNotifySwitch() {
    const container = document.getElementById('chat-notify-switch');
    if (!container) return;
    const note = document.getElementById('chat-notify-note');
    const buttons = Array.from(container.querySelectorAll('[data-chat-notify]'));
    const apply = (value) => buttons.forEach((button) => button.classList.toggle('is-selected', button.dataset.chatNotify === value));
    api('/api/preferences')
      .then((prefs) => apply(prefs.chat_notify || 'dm_mentions'))
      .catch(() => apply('dm_mentions'));
    buttons.forEach((button) => button.addEventListener('click', async () => {
      const value = button.dataset.chatNotify;
      apply(value);
      if (note) note.textContent = '';
      try {
        await api('/api/preferences', { method: 'PATCH', body: JSON.stringify({ chat_notify: value }) });
        if (note) note.textContent = 'Сохранено';
      } catch (error) {
        if (note) note.textContent = error.message;
      }
    }));
  }

  /* --- Открытие ----------------------------------------------------------- */

  async function open() {
    connect();
    if (!state.initialized) {
      state.initialized = true;
      setupNotifySwitch();
      el.tabs.forEach((tab) => tab.addEventListener('click', () => selectChannel(tab.dataset.chatTab)));
      el.peers.addEventListener('click', (event) => {
        const button = event.target.closest('[data-peer-id]');
        if (button) selectChannel('dm', Number(button.dataset.peerId));
      });
      await loadPeers();
      selectChannel('general');
    } else if (!state.messages.length && !(state.channel === 'dm' && !state.peerId)) {
      loadMessages();
    } else {
      scrollToBottom();
    }
    el.input.focus();
  }

  window.NeftChat = { open };
})();
