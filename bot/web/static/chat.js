/* ЧАТ: личные переписки и темы общего чата.
 *
 * Зависит от window.Neft (app.js): api/showToast/escapeHtml/avatarMarkup/setView/user.
 * Одно представление `#chat-view` работает в двух режимах:
 *   • список  — контакты (вкладка «Личные») или темы (вкладка «Общие»);
 *   • тред     — открытая переписка (на мобильном перекрывает список).
 * На мобильном список — «левая страница»: её открывает свайп от левого края
 * (см. touch.js) и кнопка «назад» в шапке треда.
 *
 * История — по 30 сообщений, старые догружаются вверх. Картинка сначала
 * уходит на POST /chat/upload, затем в WebSocket летит только её путь.
 */
(function () {
  'use strict';

  const view = document.getElementById('chat-view');
  if (!view || !window.Neft) return; // страница входа/гость — чата нет

  const { api, escapeHtml, avatarMarkup, showToast, setView } = window.Neft;
  const ME = window.Neft.user.id;
  const MANAGER = Boolean(window.Neft.user.manager);
  const PAGE = 30;
  const MAX_IMAGE_BYTES = 8 * 1024 * 1024;

  const el = {
    list: document.getElementById('chat-list'),
    main: document.getElementById('chat-main'),
    log: document.getElementById('chat-log'),
    state: document.getElementById('chat-state'),
    form: document.getElementById('chat-form'),
    input: document.getElementById('chat-input'),
    attach: document.getElementById('chat-attach'),
    file: document.getElementById('chat-file'),
    hint: document.getElementById('chat-peer-hint'),
    back: document.getElementById('chat-back'),
    title: document.getElementById('chat-thread-title'),
    rename: document.getElementById('chat-rename'),
    tabs: Array.from(document.querySelectorAll('[data-chat-tab]')),
    dockItems: Array.from(document.querySelectorAll('[data-chat-open]')),
  };
  if (!el.list || !el.log || !el.form || !el.input) return;

  const roomDialog = document.getElementById('room-dialog');
  const roomForm = document.getElementById('room-form');
  const roomError = document.getElementById('room-form-error');

  const state = {
    channel: 'dm',     // 'dm' (личные) | 'room' (темы)
    peerId: null,
    roomId: null,
    people: new Map(), // id -> сотрудник (имена/аватарки в тредах)
    contacts: [],
    rooms: [],
    messages: [],
    hasMore: false,
    loading: false,
    listedOnce: false,
    threadOpen: false,
    initialized: false,
    editingRoom: null,
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
  const roomById = (id) => state.rooms.find((item) => Number(item.id) === Number(id));

  function setStatus(text, kind) {
    el.state.textContent = text || '';
    el.state.hidden = !text;
    el.state.classList.toggle('is-error', kind === 'error');
  }

  async function safe(task) {
    try { await task(); } catch (error) { /* тихо: сеть может мигнуть */ }
  }

  /* --- Форматирование ----------------------------------------------------- */

  function formatTime(iso) {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }

  function listTime(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    const now = new Date();
    if (date.toDateString() === now.toDateString()) return formatTime(iso);
    if (date.getFullYear() === now.getFullYear()) return date.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' });
    return date.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit' });
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

  /* --- Список чатов ------------------------------------------------------- */

  function contactRow(person) {
    const name = person.display_name || person.name;
    return `<button class="chat-item" type="button" data-peer-id="${person.id}">
      <span class="chat-avatar">${avatarMarkup(person.id, person.avatar_rev, name)}</span>
      <span class="chat-item-body">
        <span class="chat-item-top"><span class="chat-item-name">${escapeHtml(name)}</span><time class="chat-item-time">${listTime(person.last_at)}</time></span>
        <span class="chat-item-preview">${escapeHtml(person.last_text || 'Нет сообщений')}</span>
      </span>
    </button>`;
  }

  function roomRow(room) {
    return `<button class="chat-item" type="button" data-room-id="${room.id}">
      <span class="chat-avatar chat-avatar-room"><svg class="icon" aria-hidden="true"><use href="#i-hash"></use></svg></span>
      <span class="chat-item-body">
        <span class="chat-item-top"><span class="chat-item-name">${escapeHtml(room.name)}</span><time class="chat-item-time">${listTime(room.last_at)}</time></span>
        <span class="chat-item-preview">${escapeHtml(room.last_text || 'Нет сообщений')}</span>
      </span>
    </button>`;
  }

  function renderList() {
    const items = state.channel === 'dm' ? state.contacts.map(contactRow) : state.rooms.map(roomRow);
    el.list.innerHTML = items.length
      ? items.join('')
      : `<p class="empty-state">${state.channel === 'dm' ? 'Сотрудников нет' : 'Тем пока нет'}</p>`;
    highlightListItem();
  }

  function highlightListItem() {
    el.list.querySelectorAll('.chat-item').forEach((node) => {
      const active = state.channel === 'dm'
        ? Number(node.dataset.peerId) === Number(state.peerId)
        : Number(node.dataset.roomId) === Number(state.roomId);
      node.classList.toggle('is-active', active);
    });
  }

  async function loadContacts() {
    const data = await api('/chat/users');
    state.contacts = data.users || [];
    state.contacts.forEach((person) => state.people.set(Number(person.id), person));
  }

  async function loadRooms() {
    const data = await api('/chat/rooms');
    state.rooms = data.rooms || [];
  }

  function syncTabs() {
    el.tabs.forEach((tab) => {
      const on = tab.dataset.chatTab === state.channel;
      tab.classList.toggle('is-selected', on);
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    el.dockItems.forEach((item) => item.classList.toggle('is-active', item.dataset.chatOpen === state.channel));
  }

  /* --- Список как экран --------------------------------------------------- */

  async function openList(channel) {
    state.channel = channel;
    state.listedOnce = true;
    state.threadOpen = false;
    state.peerId = null;
    state.roomId = null;
    view.classList.remove('is-thread-open');
    el.main.style.transform = '';
    syncTabs();
    setStatus('');
    el.title.textContent = channel === 'dm' ? 'Личные сообщения' : 'Темы общего чата';
    el.rename.hidden = true;
    el.form.hidden = true;
    el.log.innerHTML = '';
    state.messages = [];
    el.hint.hidden = false;
    el.hint.textContent = channel === 'dm'
      ? 'Выберите сотрудника, чтобы открыть переписку.'
      : 'Выберите тему, чтобы открыть чат.';
    try {
      if (channel === 'dm') await loadContacts(); else await loadRooms();
      renderList();
    } catch (error) {
      el.list.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    }
  }

  function back() {
    state.threadOpen = false;
    view.classList.remove('is-thread-open');
    el.main.style.transform = '';
    highlightListItem();
    if (state.channel === 'dm') safe(loadContacts).then(renderList);
    else safe(loadRooms).then(renderList);
  }

  /* --- Тред --------------------------------------------------------------- */

  async function openThread(target) {
    state.channel = target.roomId != null ? 'room' : 'dm';
    state.peerId = target.peerId != null ? Number(target.peerId) : null;
    state.roomId = target.roomId != null ? Number(target.roomId) : null;
    if (state.channel === 'room' && !state.rooms.length) await safe(loadRooms);
    if (state.channel === 'dm' && !state.contacts.length) await safe(loadContacts);

    state.threadOpen = true;
    view.classList.add('is-thread-open');
    el.main.style.transform = '';
    syncTabs();
    el.hint.hidden = true;
    el.form.hidden = false;
    el.log.innerHTML = '';
    state.messages = [];
    state.hasMore = false;

    const person = state.channel === 'dm' ? state.people.get(Number(state.peerId)) : null;
    const room = state.channel === 'room' ? roomById(state.roomId) : null;
    el.title.textContent = person ? (person.display_name || person.name) : (room ? room.name : 'Чат');
    el.rename.hidden = !(state.channel === 'room' && MANAGER);
    highlightListItem();
    await loadMessages();
    el.input.focus();
  }

  function query() {
    const params = new URLSearchParams({ channel: state.channel, limit: String(PAGE) });
    if (state.channel === 'dm') params.set('peer_id', String(state.peerId));
    else params.set('room_id', String(state.roomId));
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

  /* --- Отрисовка сообщений ------------------------------------------------ */

  function nearBottom() {
    return el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight < 90;
  }

  function scrollToBottom() {
    el.log.scrollTop = el.log.scrollHeight;
  }

  function messageHtml(message) {
    const mine = Number(message.sender_id) === ME;
    const image = message.image_url
      ? `<a class="chat-image" href="${escapeHtml(message.image_url)}" target="_blank" rel="noopener"><img src="${escapeHtml(message.image_url)}" alt="Изображение" loading="lazy"></a>`
      : '';
    const text = message.text ? `<p class="chat-text">${linkify(message.text)}</p>` : '';
    const author = !mine && state.channel === 'room'
      ? `<span class="chat-author">${escapeHtml(personName(message.sender_id))}</span>`
      : '';
    return `<article class="chat-msg${mine ? ' is-mine' : ''}" data-id="${message.id}">
      ${mine ? '' : `<span class="chat-avatar">${avatarMarkup(message.sender_id, personAvatarRev(message.sender_id), personName(message.sender_id))}</span>`}
      <div class="chat-bubble">
        ${author}${image}${text}
        <time class="chat-time">${formatTime(message.created_at)}</time>
      </div>
    </article>`;
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
    updateActiveRow(message);
  }

  function updateActiveRow(message) {
    const selector = state.channel === 'dm' ? `[data-peer-id="${state.peerId}"]` : `[data-room-id="${state.roomId}"]`;
    const row = el.list.querySelector(selector);
    if (!row) return;
    const time = row.querySelector('.chat-item-time');
    const preview = row.querySelector('.chat-item-preview');
    if (time) time.textContent = listTime(message.created_at);
    if (preview) preview.textContent = (message.text || 'Фото').slice(0, 80);
  }

  /* --- WebSocket ---------------------------------------------------------- */

  function socketUrl() {
    return `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/chat/ws`;
  }

  function sendRaw(payload) {
    if (state.socket && state.socket.readyState === WebSocket.OPEN) {
      state.socket.send(JSON.stringify(payload));
      return true;
    }
    return false;
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

  function relevant(message) {
    if (state.channel === 'room') {
      return message.recipient_id === null && Number(message.room_id) === Number(state.roomId);
    }
    if (message.recipient_id === null) return false;
    const peer = Number(state.peerId);
    return (Number(message.sender_id) === ME && Number(message.recipient_id) === peer)
      || (Number(message.sender_id) === peer && Number(message.recipient_id) === ME);
  }

  function handleIncoming(message) {
    if (!state.threadOpen || !relevant(message)) return;
    pushMessage(message);
  }

  function sendMessage(text, imageUrl) {
    const payload = { type: 'message', text: text || '', image_url: imageUrl || null };
    if (state.channel === 'dm') payload.recipient_id = state.peerId;
    else payload.room_id = state.roomId;
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

  function wireComposer() {
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
      if (!state.threadOpen) { showToast('Сначала выберите чат'); return; }
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
  }

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

  /* --- Управление темами (панель управления) ------------------------------ */

  async function renderRoomAdmin() {
    const list = document.getElementById('chat-room-list');
    if (!list || !MANAGER) return;
    try {
      const data = await api('/chat/rooms');
      state.rooms = data.rooms || [];
      list.innerHTML = state.rooms.length
        ? state.rooms.map((room) => `<div class="room-row">
            <span class="room-row-name">${escapeHtml(room.name)}</span>
            <button class="text-button" type="button" data-room-edit="${room.id}">Переименовать</button>
          </div>`).join('')
        : '<p class="empty-state">Тем пока нет</p>';
    } catch (error) {
      list.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    }
  }

  function openRoomDialog(room) {
    if (!roomDialog || !roomForm) return;
    state.editingRoom = room || null;
    roomForm.reset();
    if (roomError) roomError.hidden = true;
    document.getElementById('room-dialog-kicker').textContent = state.editingRoom ? 'ТЕМА' : 'НОВАЯ ТЕМА';
    document.getElementById('room-dialog-title').textContent = state.editingRoom ? 'Переименовать тему' : 'Новая тема';
    if (state.editingRoom) roomForm.elements.name.value = state.editingRoom.name;
    roomDialog.showModal();
  }

  async function submitRoom(event) {
    event.preventDefault();
    const name = roomForm.elements.name.value.trim();
    if (!name) return;
    if (roomError) roomError.hidden = true;
    const editing = state.editingRoom;
    try {
      if (editing) await api(`/chat/rooms/${editing.id}`, { method: 'PATCH', body: JSON.stringify({ name }) });
      else await api('/chat/rooms', { method: 'POST', body: JSON.stringify({ name }) });
      roomDialog.close();
      const note = document.getElementById('chat-room-note');
      if (note) note.textContent = editing ? 'Тема переименована' : 'Тема создана';
      showToast(editing ? 'Тема переименована' : 'Тема создана');
      state.editingRoom = null;
      if (editing && state.channel === 'room' && Number(state.roomId) === Number(editing.id)) {
        el.title.textContent = name;
        state.rooms = state.rooms.map((room) => (Number(room.id) === Number(editing.id) ? { ...room, name } : room));
      }
      await renderRoomAdmin();
      if (state.threadOpen && state.channel === 'room') await safe(loadRooms).then(renderList);
    } catch (error) {
      if (roomError) {
        roomError.textContent = error.message;
        roomError.hidden = false;
      }
    }
  }

  /* --- Разметка и события ------------------------------------------------- */

  function wireUi() {
    el.tabs.forEach((tab) => tab.addEventListener('click', () => openList(tab.dataset.chatTab)));
    el.dockItems.forEach((item) => item.addEventListener('click', () => {
      setView('chat');
      openList(item.dataset.chatOpen);
    }));
    el.list.addEventListener('click', (event) => {
      const item = event.target.closest('.chat-item');
      if (!item) return;
      if (item.dataset.roomId) openThread({ roomId: Number(item.dataset.roomId) });
      else if (item.dataset.peerId) openThread({ peerId: Number(item.dataset.peerId) });
    });
    el.back.addEventListener('click', back);
    el.rename.addEventListener('click', () => {
      const room = roomById(state.roomId);
      if (room) openRoomDialog(room);
    });
    document.getElementById('chat-room-add')?.addEventListener('click', () => openRoomDialog(null));
    document.getElementById('chat-room-list')?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-room-edit]');
      if (!button) return;
      openRoomDialog(roomById(button.dataset.roomEdit) || { id: Number(button.dataset.roomEdit), name: '' });
    });
    document.querySelectorAll('#room-dialog .dialog-close').forEach((button) => button.addEventListener('click', () => roomDialog?.close()));
    roomDialog?.addEventListener('click', (event) => { if (event.target === roomDialog) roomDialog.close(); });
    roomForm?.addEventListener('submit', submitRoom);

    wireComposer();
    document.addEventListener('visibilitychange', () => { if (!document.hidden) connect(); });
    window.addEventListener('online', connect);
  }

  /* --- Открытие ----------------------------------------------------------- */

  function deepLink() {
    const params = new URLSearchParams(window.location.search);
    const peer = params.get('peer');
    const room = params.get('room');
    if (peer && Number(peer) !== ME) return { peerId: Number(peer) };
    if (room) return { roomId: Number(room) };
    return null;
  }

  async function open() {
    connect();
    if (!state.initialized) {
      state.initialized = true;
      await safe(loadContacts);   // каталог нужен и для имён авторов в темах
    }
    if (MANAGER) safe(renderRoomAdmin);
    const link = deepLink();
    if (link) {
      await openThread(link);
      return;
    }
    if (!state.threadOpen && !state.listedOnce) await openList(state.channel);
    el.input.focus();
  }

  // Слушатели ставим сразу: нижний док должен открывать чат ещё до первого захода.
  wireUi();
  setupNotifySwitch();

  window.NeftChat = { open, back, openList, openThread };
})();
