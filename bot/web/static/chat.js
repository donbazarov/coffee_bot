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
    poll: document.getElementById('chat-poll'),
    mentions: document.getElementById('chat-mentions'),
    editing: document.getElementById('chat-editing'),
    editCancel: document.getElementById('chat-edit-cancel'),
    menu: document.getElementById('chat-menu'),
  };
  if (!el.list || !el.log || !el.form || !el.input) return;

  const roomDialog = document.getElementById('room-dialog');
  const roomForm = document.getElementById('room-form');
  const roomError = document.getElementById('room-form-error');
  const roomVisibility = document.getElementById('room-visibility');
  const roomMembers = document.getElementById('room-members');

  const pollDialog = document.getElementById('poll-dialog');
  const pollForm = document.getElementById('poll-form');
  const pollOptions = document.getElementById('poll-options');
  const pollError = document.getElementById('poll-form-error');

  const state = {
    channel: 'dm',     // 'dm' (личные) | 'room' (темы)
    peerId: null,
    roomId: null,
    people: new Map(), // id -> сотрудник (имена/аватарки в тредах)
    contacts: [],
    rooms: [],
    managedRooms: [],
    messages: [],
    polls: new Map(),         // poll_id -> опрос глазами текущего пользователя
    pollLoading: new Set(),   // опросы, которые сейчас грузятся
    pollSelection: new Map(), // poll_id -> Set(option_id) — выбор до отправки
    messageEdit: null,        // сообщение, которое правим прямо сейчас
    menuMessage: null,        // сообщение в открытом меню «Изменить/Удалить»
    menuOpenedAt: 0,
    touchAt: 0,
    mentionItems: [],
    mentionIndex: 0,
    roomVisibility: 'all',
    longPress: null,
    longPressStart: null,
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
      .replace(/(@[\p{L}\p{N}_.\-]{1,64})/gu, '<span class="chat-mention">$1</span>')
      .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  }

  function plural(count, one, few, many) {
    const value = Math.abs(count) % 100;
    const tail = value % 10;
    if (value > 10 && value < 20) return many;
    if (tail > 1 && tail < 5) return few;
    if (tail === 1) return one;
    return many;
  }

  /* --- Опросы ------------------------------------------------------------- */

  function pollSelection(pollId) {
    const key = Number(pollId);
    if (!state.pollSelection.has(key)) state.pollSelection.set(key, new Set());
    return state.pollSelection.get(key);
  }

  function pollHtml(poll) {
    const options = poll.options || [];
    const total = options.reduce((sum, option) => sum + option.votes, 0);
    const mine = new Set(poll.my_votes || []);
    const picked = pollSelection(poll.id);
    const revealed = options.some((option) => Boolean(option.is_correct));
    const results = Boolean(poll.voted);
    const interactive = !poll.voted || poll.allow_change;
    const rows = options.map((option) => {
      const isMine = mine.has(option.id);
      const isPicked = picked.has(option.id);
      const percent = total ? Math.round((option.votes / total) * 100) : 0;
      const classes = ['poll-option'];
      if (isMine || isPicked) classes.push('is-mine');
      if (revealed && option.is_correct) classes.push('is-correct');
      if (results) classes.push('is-results');
      if (!interactive) classes.push('is-locked');
      const voters = !poll.anonymous && (option.voters || []).length
        ? `<span class="poll-voters">${escapeHtml(option.voters.join(', '))}</span>`
        : '';
      return `<button class="${classes.join(' ')}" type="button" data-poll-option="${option.id}" role="${poll.multiple ? 'checkbox' : 'radio'}" aria-checked="${isMine || isPicked ? 'true' : 'false'}"${interactive ? '' : ' disabled'}>
        ${results ? `<span class="poll-bar" style="width:${percent}%"></span>` : ''}
        <span class="poll-option-body"><span class="poll-option-text">${escapeHtml(option.text)}</span>${voters}</span>
        ${results ? `<span class="poll-option-count">${percent} %</span>` : ''}
      </button>`;
    }).join('');
    const note = poll.quiz && !poll.voted
      ? '<span class="poll-hint">Правильный ответ откроется после голосования</span>'
      : (poll.voted && poll.allow_change ? '<span class="poll-hint">Ответ можно изменить — нажмите на вариант</span>' : '');
    const submit = poll.multiple && interactive && picked.size
      ? `<button class="button button-primary poll-submit" type="button" data-poll-submit="${poll.id}">Готово</button>`
      : '';
    return `<div class="chat-poll" data-poll-id="${poll.id}">
      <p class="poll-question">${escapeHtml(poll.question)}</p>
      <div class="poll-options-list">${rows}</div>
      ${note}
      <div class="poll-foot"><span class="poll-total">${total} ${plural(total, 'голос', 'голоса', 'голосов')}</span>${submit}</div>
    </div>`;
  }

  function pollBlock(message) {
    if (!message.poll_id) return '';
    const poll = state.polls.get(Number(message.poll_id));
    if (poll) return pollHtml(poll);
    return `<div class="chat-poll is-loading" data-poll-id="${message.poll_id}"><p class="poll-question">Опрос</p><p class="poll-hint">Загружаем…</p></div>`;
  }

  function hydratePolls() {
    el.log.querySelectorAll('.chat-poll[data-poll-id]').forEach((node) => {
      const pollId = Number(node.dataset.pollId);
      if (state.polls.has(pollId) || state.pollLoading.has(pollId)) return;
      state.pollLoading.add(pollId);
      api(`/chat/polls/${pollId}`)
        .then((data) => {
          state.polls.set(pollId, data.poll);
          node.outerHTML = pollHtml(data.poll);
        })
        .catch(() => { node.innerHTML = '<p class="poll-hint">Опрос недоступен</p>'; })
        .then(() => state.pollLoading.delete(pollId));
    });
  }

  function replacePollNode(poll) {
    const node = el.log.querySelector(`.chat-poll[data-poll-id="${poll.id}"]`);
    if (node) node.outerHTML = pollHtml(poll);
  }

  async function votePoll(pollId, optionIds) {
    try {
      const data = await api(`/chat/polls/${Number(pollId)}/vote`, {
        method: 'POST',
        body: JSON.stringify({ option_ids: optionIds }),
      });
      state.polls.set(Number(data.poll.id), data.poll);
      state.pollSelection.delete(Number(data.poll.id));
      replacePollNode(data.poll);
    } catch (error) {
      showToast(error.message);
    }
  }

  async function refreshPoll(pollId) {
    try {
      const data = await api(`/chat/polls/${Number(pollId)}`);
      state.polls.set(Number(data.poll.id), data.poll);
      replacePollNode(data.poll);
    } catch (error) { /* опрос мог уйти вместе с сообщением */ }
  }

  function wirePolls() {
    el.log.addEventListener('click', (event) => {
      const done = event.target.closest('[data-poll-submit]');
      if (done) {
        const pollId = Number(done.dataset.pollSubmit);
        const chosen = Array.from(pollSelection(pollId));
        if (chosen.length) votePoll(pollId, chosen);
        return;
      }
      const option = event.target.closest('[data-poll-option]');
      if (!option) return;
      const block = option.closest('.chat-poll');
      if (!block) return;
      const pollId = Number(block.dataset.pollId);
      const poll = state.polls.get(pollId);
      if (!poll) return;
      if (poll.voted && !poll.allow_change) { showToast('Изменение ответа не разрешено'); return; }
      const optionId = Number(option.dataset.pollOption);
      if (poll.multiple) {
        const picked = pollSelection(pollId);
        if (picked.has(optionId)) picked.delete(optionId); else picked.add(optionId);
        replacePollNode(poll);
      } else {
        votePoll(pollId, [optionId]);
      }
    });
  }

  /* --- Упоминания через @ ------------------------------------------------- */

  function mentionQuery() {
    const value = el.input.value;
    const caret = el.input.selectionStart == null ? value.length : el.input.selectionStart;
    const match = /(?:^|\s)@([\p{L}\p{N}_.\-]*)$/u.exec(value.slice(0, caret));
    if (!match) return null;
    return { query: match[1], start: caret - match[1].length - 1, caret };
  }

  function mentionHandle(person) {
    const name = String(person.display_name || person.name || '').trim();
    const first = name.split(/\s+/)[0] || '';
    const clash = state.contacts.some((other) => Number(other.id) !== Number(person.id)
      && String(other.display_name || other.name || '').trim().split(/\s+/)[0].toLowerCase() === first.toLowerCase());
    return (clash ? name.replace(/\s+/g, '') : first) || String(person.id);
  }

  function closeMentions() {
    state.mentionItems = [];
    state.mentionIndex = 0;
    if (!el.mentions) return;
    el.mentions.hidden = true;
    el.mentions.innerHTML = '';
  }

  function renderMentions() {
    if (!el.mentions) return;
    const context = mentionQuery();
    if (!context) { closeMentions(); return; }
    const needle = context.query.toLowerCase();
    // Сначала те, чьё имя или @-handle начинается с набранного, потом остальные.
    const items = state.contacts
      .map((person) => ({
        person,
        handle: mentionHandle(person),
        name: String(person.display_name || person.name || ''),
      }))
      .filter((entry) => !needle
        || entry.name.toLowerCase().includes(needle)
        || entry.handle.toLowerCase().includes(needle))
      .sort((a, b) => {
        const rank = (entry) => (entry.handle.toLowerCase().startsWith(needle)
          || entry.name.toLowerCase().startsWith(needle) ? 0 : 1);
        return rank(a) - rank(b) || a.name.localeCompare(b.name, 'ru');
      })
      .slice(0, 6)
      .map((entry) => entry.person);
    if (!items.length) { closeMentions(); return; }
    state.mentionItems = items;
    if (state.mentionIndex >= items.length) state.mentionIndex = 0;
    el.mentions.innerHTML = items.map((person, index) => `<button class="chat-mention-item${index === state.mentionIndex ? ' is-active' : ''}" type="button" role="option" aria-selected="${index === state.mentionIndex}" data-mention-index="${index}">
      <span class="chat-avatar">${avatarMarkup(person.id, person.avatar_rev, person.display_name || person.name)}</span>
      <span class="chat-mention-name">${escapeHtml(person.display_name || person.name)}</span>
      <span class="chat-mention-handle">@${escapeHtml(mentionHandle(person))}</span>
    </button>`).join('');
    el.mentions.hidden = false;
  }

  function applyMention(index) {
    const person = state.mentionItems[index];
    const context = mentionQuery();
    if (!person || !context) { closeMentions(); return; }
    const inserted = `@${mentionHandle(person)} `;
    const value = el.input.value;
    el.input.value = value.slice(0, context.start) + inserted + value.slice(context.caret);
    const caret = context.start + inserted.length;
    el.input.setSelectionRange(caret, caret);
    closeMentions();
    autoGrow();
    el.input.focus();
  }

  function shiftMention(step) {
    if (!state.mentionItems.length) return;
    const count = state.mentionItems.length;
    state.mentionIndex = (state.mentionIndex + step + count) % count;
    renderMentions();
  }

  /* --- Правка и удаление сообщений ---------------------------------------- */

  const senior = Boolean(window.Neft.user.manager);

  function messageById(id) {
    return state.messages.find((item) => Number(item.id) === Number(id)) || null;
  }

  function applyMessageUpdate(message) {
    const index = state.messages.findIndex((item) => Number(item.id) === Number(message.id));
    if (index >= 0) state.messages[index] = { ...state.messages[index], ...message };
    const node = el.log.querySelector(`.chat-msg[data-id="${message.id}"]`);
    if (node) node.outerHTML = messageHtml(state.messages[index] || message);
    hydratePolls();
  }

  function removeMessage(id) {
    state.messages = state.messages.filter((item) => Number(item.id) !== Number(id));
    const node = el.log.querySelector(`.chat-msg[data-id="${id}"]`);
    if (node) node.remove();
    if (state.messageEdit && Number(state.messageEdit.id) === Number(id)) stopEdit();
  }

  function closeMessageMenu() {
    state.menuMessage = null;
    if (el.menu) el.menu.hidden = true;
  }

  function openMessageMenu(message, x, y, anchor) {
    if (!el.menu) return;
    const mine = Number(message.sender_id) === ME;
    const canEdit = mine && !message.image_url && !message.poll_id;
    const canDelete = mine || senior;
    if (!canEdit && !canDelete) return;
    state.menuMessage = message;
    state.menuOpenedAt = Date.now();
    el.menu.querySelector('[data-chat-menu="edit"]').hidden = !canEdit;
    el.menu.querySelector('[data-chat-menu="delete"]').hidden = !canDelete;
    el.menu.hidden = false;
    const box = el.menu.getBoundingClientRect();
    let left = x;
    let top = y;
    if (anchor) {
      // Долгое нажатие: меню встаёт над сообщением, чтобы палец при отпускании
      // не попал в пункт меню.
      left = anchor.left + anchor.width / 2 - box.width / 2;
      top = anchor.top - box.height - 8;
      if (top < 8) top = anchor.bottom + 8;
    }
    left = Math.min(Math.max(8, left), Math.max(8, window.innerWidth - box.width - 8));
    top = Math.min(Math.max(8, top), Math.max(8, window.innerHeight - box.height - 8));
    el.menu.style.left = `${Math.round(left)}px`;
    el.menu.style.top = `${Math.round(top)}px`;
  }

  function startEdit(message) {
    state.messageEdit = message;
    el.input.value = message.text || '';
    if (el.editing) el.editing.hidden = false;
    closeMentions();
    autoGrow();
    el.input.focus();
    el.input.setSelectionRange(el.input.value.length, el.input.value.length);
  }

  function stopEdit() {
    state.messageEdit = null;
    if (el.editing) el.editing.hidden = true;
    if (el.input.value) {
      el.input.value = '';
      autoGrow();
    }
  }

  async function saveEdit(text) {
    const target = state.messageEdit;
    if (!target) return false;
    try {
      const data = await api(`/chat/messages/${Number(target.id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ text }),
      });
      applyMessageUpdate(data.message);
      stopEdit();
      return true;
    } catch (error) {
      showToast(error.message);
      return false;
    }
  }

  async function deleteMessage(message) {
    const preview = String(message.text || '').slice(0, 40);
    if (typeof window.confirm === 'function' && !window.confirm(preview ? `Удалить сообщение «${preview}»?` : 'Удалить сообщение?')) return;
    try {
      await api(`/chat/messages/${Number(message.id)}`, { method: 'DELETE' });
      removeMessage(message.id);
      showToast('Сообщение удалено');
    } catch (error) {
      showToast(error.message);
    }
  }

  function cancelLongPress() {
    if (state.longPress) { clearTimeout(state.longPress); state.longPress = null; }
    state.longPressStart = null;
  }

  function wireMessageMenu() {
    el.log.addEventListener('contextmenu', (event) => {
      const bubble = event.target.closest('.chat-bubble');
      if (!bubble) return;
      // На телефоне contextmenu — это тоже долгое нажатие (и pointerType у него
      // бывает «mouse»): гасим системное меню и ждём свой таймер, который поставит
      // меню над сообщением, а не под палец.
      if (Date.now() - state.touchAt < 2000) {
        event.preventDefault();
        return;
      }
      const article = bubble.closest('.chat-msg');
      const message = article ? messageById(article.dataset.id) : null;
      if (!message) return;
      event.preventDefault();
      openMessageMenu(message, event.clientX, event.clientY);
    });
    el.log.addEventListener('pointerdown', (event) => {
      cancelLongPress();
      if (event.pointerType === 'mouse') return;
      state.touchAt = Date.now();
      const bubble = event.target.closest('.chat-bubble');
      if (!bubble) return;
      const article = bubble.closest('.chat-msg');
      const message = article ? messageById(article.dataset.id) : null;
      if (!message) return;
      state.longPressStart = { x: event.clientX, y: event.clientY };
      state.longPress = setTimeout(() => {
        state.longPress = null;
        if (navigator.vibrate) navigator.vibrate(12);
        openMessageMenu(message, 0, 0, bubble.getBoundingClientRect());
      }, 480);
    });
    el.log.addEventListener('pointermove', (event) => {
      if (!state.longPressStart) return;
      const dx = Math.abs(event.clientX - state.longPressStart.x);
      const dy = Math.abs(event.clientY - state.longPressStart.y);
      if (dx > 12 || dy > 12) cancelLongPress();
    });
    ['pointerup', 'pointercancel'].forEach((type) => el.log.addEventListener(type, cancelLongPress));
    el.log.addEventListener('scroll', cancelLongPress, { passive: true });
    el.menu?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-chat-menu]');
      if (!button) return;
      const message = state.menuMessage;
      closeMessageMenu();
      if (!message) return;
      if (button.dataset.chatMenu === 'edit') startEdit(message);
      else deleteMessage(message);
    });
    document.addEventListener('click', (event) => {
      if (!el.menu || el.menu.hidden) return;
      // Долгое нажатие на телефоне заканчивается синтетическим click — он не должен закрывать меню.
      if (Date.now() - state.menuOpenedAt < 500) return;
      if (!el.menu.contains(event.target)) closeMessageMenu();
    });
    window.addEventListener('scroll', closeMessageMenu, true);
    window.addEventListener('resize', closeMessageMenu);
    el.editCancel?.addEventListener('click', stopEdit);
  }

  /* --- Клавиатура на мобильном -------------------------------------------- */

  const mobileQuery = window.matchMedia ? window.matchMedia('(max-width: 760px)') : null;
  const isMobile = () => Boolean(mobileQuery && mobileQuery.matches);

  function measureChrome() {
    const root = document.documentElement;
    let topbarHeight = 0;
    const topbar = document.querySelector('.topbar');
    if (topbar) {
      topbarHeight = Math.round(topbar.getBoundingClientRect().height);
      if (topbarHeight > 0) root.style.setProperty('--topbar-h', `${topbarHeight}px`);
    }
    let dockHeight = 0;
    const dock = document.getElementById('dock');
    if (dock && getComputedStyle(dock).display !== 'none') {
      dockHeight = Math.round(dock.getBoundingClientRect().height);
      if (dockHeight > 0) root.style.setProperty('--dock-h', `${dockHeight}px`);
    }
    // Высоту чата считаем от реально видимой области: так поле ввода остаётся
    // над клавиатурой (она укорачивает visualViewport), а горизонтальный
    // скроллбар или адресная строка не отрезают низ.
    const viewport = window.visualViewport;
    const visible = viewport ? viewport.height : window.innerHeight;
    if (visible && topbarHeight) {
      const height = Math.max(220, Math.round(visible - topbarHeight - dockHeight));
      root.style.setProperty('--chat-h', `${height}px`);
    }
  }

  function setupKeyboard() {
    measureChrome();
    window.addEventListener('resize', measureChrome);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(measureChrome).catch(() => {});
    const viewport = window.visualViewport;
    if (!viewport) return;
    const apply = () => {
      measureChrome();
      if (state.threadOpen && document.activeElement === el.input) scrollToBottom();
    };
    viewport.addEventListener('resize', apply);
    viewport.addEventListener('scroll', apply);
    window.addEventListener('orientationchange', () => setTimeout(apply, 150));
    apply();
  }

  /* --- Диалог опроса ------------------------------------------------------ */

  const POLL_MIN = 2;
  const POLL_MAX = 12;

  function pollOptionRow() {
    const row = document.createElement('div');
    row.className = 'poll-option-row';
    row.innerHTML = `<input type="radio" class="poll-correct" name="poll-correct" aria-label="Правильный вариант" hidden>
      <input type="text" class="poll-option-input" maxlength="100" placeholder="Вариант ответа" autocomplete="off" aria-label="Вариант ответа">
      <button class="icon-button poll-option-remove" type="button" aria-label="Удалить вариант"><svg class="icon" aria-hidden="true"><use href="#i-close"></use></svg></button>`;
    row.querySelector('.poll-option-remove').addEventListener('click', () => {
      if (pollOptions.children.length <= POLL_MIN) { showToast(`Нужно минимум ${POLL_MIN} варианта`); return; }
      row.remove();
    });
    return row;
  }

  function syncPollOptions() {
    const quiz = Boolean(pollForm && pollForm.elements.quiz && pollForm.elements.quiz.checked);
    if (!pollOptions) return;
    pollOptions.querySelectorAll('.poll-correct').forEach((input) => {
      input.hidden = !quiz;
      if (!quiz) input.checked = false;
    });
  }

  function openPollDialog() {
    if (!pollDialog || !pollForm) return;
    if (!state.threadOpen) { showToast('Сначала выберите чат'); return; }
    pollForm.reset();
    pollOptions.innerHTML = '';
    for (let index = 0; index < 3; index += 1) pollOptions.append(pollOptionRow());
    syncPollOptions();
    if (pollError) pollError.hidden = true;
    pollDialog.showModal();
    pollForm.elements.question.focus();
  }

  function pollFail(message) {
    if (!pollError) return;
    pollError.textContent = message;
    pollError.hidden = false;
  }

  function submitPoll(event) {
    event.preventDefault();
    if (pollError) pollError.hidden = true;
    const question = String(pollForm.elements.question.value || '').trim();
    if (!question) { pollFail('Введите вопрос'); return; }
    const inputs = Array.from(pollOptions.querySelectorAll('.poll-option-input'));
    const values = inputs.map((input) => input.value.trim());
    const options = values.filter(Boolean);
    if (options.length < POLL_MIN) { pollFail(`Нужно минимум ${POLL_MIN} варианта ответа`); return; }
    const quiz = Boolean(pollForm.elements.quiz.checked);
    let correctIndex = null;
    if (quiz) {
      const chosen = Array.from(pollOptions.querySelectorAll('.poll-correct')).findIndex((input) => input.checked);
      if (chosen < 0 || !values[chosen]) { pollFail('Отметьте правильный вариант'); return; }
      correctIndex = values.slice(0, chosen + 1).filter(Boolean).length - 1;
    }
    const poll = {
      question,
      options,
      anonymous: Boolean(pollForm.elements.anonymous.checked),
      multiple: Boolean(pollForm.elements.multiple.checked),
      allow_change: Boolean(pollForm.elements.allow_change.checked),
      shuffle: Boolean(pollForm.elements.shuffle.checked),
      quiz,
      correct_index: correctIndex,
    };
    if (!sendMessage('', null, poll)) return;
    pollDialog.close();
    showToast('Опрос отправлен');
  }

  function wirePollDialog() {
    el.poll?.addEventListener('click', openPollDialog);
    document.getElementById('poll-add-option')?.addEventListener('click', () => {
      if (pollOptions.children.length >= POLL_MAX) { showToast(`Максимум ${POLL_MAX} вариантов`); return; }
      const row = pollOptionRow();
      pollOptions.append(row);
      syncPollOptions();
      const field = row.querySelector('.poll-option-input');
      if (field) field.focus();
    });
    pollForm?.addEventListener('change', (event) => {
      if (event.target && event.target.name === 'quiz') syncPollOptions();
    });
    pollForm?.addEventListener('submit', submitPoll);
    document.querySelectorAll('#poll-dialog .dialog-close').forEach((button) => {
      button.addEventListener('click', () => pollDialog?.close());
    });
    pollDialog?.addEventListener('click', (event) => { if (event.target === pollDialog) pollDialog.close(); });
  }

  /* --- Приватные темы ----------------------------------------------------- */

  async function ensureManagedRooms() {
    if (!MANAGER) return;
    try {
      const data = await api('/chat/rooms/manage');
      state.managedRooms = data.rooms || [];
    } catch (error) { /* панель управления просто не покажет участников */ }
  }

  function syncRoomVisibility() {
    roomVisibility?.querySelectorAll('[data-room-visibility]').forEach((button) => {
      const on = button.dataset.roomVisibility === state.roomVisibility;
      button.classList.toggle('is-selected', on);
      button.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    if (roomMembers) roomMembers.hidden = state.roomVisibility !== 'selected';
  }

  function renderRoomMembers() {
    if (!roomMembers) return;
    const people = state.contacts.slice().sort((a, b) => String(a.display_name || a.name || '')
      .localeCompare(String(b.display_name || b.name || ''), 'ru'));
    roomMembers.innerHTML = people.length
      ? people.map((person) => `<label class="room-member"><input type="checkbox" data-room-member="${person.id}"><span class="room-member-name">${escapeHtml(person.display_name || person.name)}</span></label>`).join('')
      : '<p class="empty-state">Сотрудников нет</p>';
    const selected = state.roomSelected || new Set();
    roomMembers.querySelectorAll('[data-room-member]').forEach((input) => {
      input.checked = selected.has(Number(input.dataset.roomMember));
    });
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
    const lock = room.visibility === 'selected'
      ? '<span class="chat-item-lock" title="Приватная тема"><svg class="icon" aria-hidden="true"><use href="#i-lock"></use></svg></span>'
      : '';
    return `<button class="chat-item" type="button" data-room-id="${room.id}">
      <span class="chat-avatar chat-avatar-room"><svg class="icon" aria-hidden="true"><use href="#i-hash"></use></svg></span>
      <span class="chat-item-body">
        <span class="chat-item-top">${lock}<span class="chat-item-name">${escapeHtml(room.name)}</span><time class="chat-item-time">${listTime(room.last_at)}</time></span>
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
    stopEdit();
    closeMentions();
    closeMessageMenu();
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
    stopEdit();
    closeMentions();
    closeMessageMenu();
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

    stopEdit();
    closeMentions();
    closeMessageMenu();
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
    const poll = pollBlock(message);
    const author = !mine && state.channel === 'room'
      ? `<span class="chat-author">${escapeHtml(personName(message.sender_id))}</span>`
      : '';
    const mark = message.edited_at ? ' · изменено' : '';
    return `<article class="chat-msg${mine ? ' is-mine' : ''}" data-id="${message.id}" data-own="${mine ? '1' : '0'}">
      ${mine ? '' : `<span class="chat-avatar">${avatarMarkup(message.sender_id, personAvatarRev(message.sender_id), personName(message.sender_id))}</span>`}
      <div class="chat-bubble">
        ${author}${image}${poll}${text}
        <time class="chat-time">${formatTime(message.created_at)}${mark}</time>
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
    hydratePolls();
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
    if (preview) preview.textContent = (message.poll_id ? 'Опрос' : (message.text || 'Фото')).slice(0, 80);
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
      else if (payload.type === 'message_edit' && payload.message) handleIncoming(payload.message);
      else if (payload.type === 'message_delete') handleIncoming(null, Number(payload.id));
      else if (payload.type === 'poll_update') refreshPoll(Number(payload.poll_id));
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

  function handleIncoming(message, deletedId) {
    if (!state.threadOpen) return;
    if (deletedId) { removeMessage(deletedId); return; }
    if (!message || !relevant(message)) return;
    if (messageById(message.id)) { applyMessageUpdate(message); return; }
    pushMessage(message);
  }

  function sendMessage(text, imageUrl, poll) {
    const payload = { type: 'message', text: text || '', image_url: imageUrl || null };
    if (poll) payload.poll = poll;
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
    el.input.addEventListener('input', () => {
      autoGrow();
      renderMentions();
    });
    el.input.addEventListener('keydown', (event) => {
      if (state.mentionItems.length) {
        if (event.key === 'ArrowDown') { event.preventDefault(); shiftMention(1); return; }
        if (event.key === 'ArrowUp') { event.preventDefault(); shiftMention(-1); return; }
        if (event.key === 'Enter' || event.key === 'Tab') { event.preventDefault(); applyMention(state.mentionIndex); return; }
        if (event.key === 'Escape') { event.preventDefault(); closeMentions(); return; }
      }
      if (event.key === 'Escape' && state.messageEdit) { event.preventDefault(); stopEdit(); return; }
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        el.form.requestSubmit();
      }
    });
    el.input.addEventListener('focus', () => {
      if (!isMobile()) return;
      setTimeout(() => { autoGrow(); scrollToBottom(); }, 80);
    });
    if (el.mentions) {
      el.mentions.addEventListener('mousedown', (event) => {
        const button = event.target.closest('[data-mention-index]');
        if (!button) return;
        event.preventDefault();
        applyMention(Number(button.dataset.mentionIndex));
      });
    }
    el.form.addEventListener('submit', (event) => {
      event.preventDefault();
      const text = el.input.value.trim();
      if (!text) return;
      if (state.messageEdit) {
        saveEdit(text).then((ok) => { if (ok && isMobile()) el.input.blur(); });
        return;
      }
      if (sendMessage(text, null, null)) {
        el.input.value = '';
        autoGrow();
        closeMentions();
        // На телефоне после отправки прячем клавиатуру — экран возвращается целиком.
        if (isMobile()) el.input.blur();
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
      const data = await api('/chat/rooms/manage');
      state.managedRooms = data.rooms || [];
      list.innerHTML = state.managedRooms.length
        ? state.managedRooms.map((room) => `<div class="room-row">
            <span class="room-row-name">${escapeHtml(room.name)}</span>
            <span class="room-row-meta">${room.visibility === 'selected' ? `ТОЛЬКО СВОИ · ${(room.member_ids || []).length}` : 'ВСЕ СОТРУДНИКИ'}</span>
            <button class="text-button" type="button" data-room-edit="${room.id}">Изменить</button>
          </div>`).join('')
        : '<p class="empty-state">Тем пока нет</p>';
    } catch (error) {
      list.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    }
  }

  async function openRoomDialog(source) {
    if (!roomDialog || !roomForm) return;
    let room = source ? { ...source } : null;
    if (room && room.member_ids === undefined) {
      await ensureManagedRooms();
      const full = state.managedRooms.find((item) => Number(item.id) === Number(room.id));
      if (full) room = full;
    }
    state.editingRoom = room;
    roomForm.reset();
    if (roomError) roomError.hidden = true;
    state.roomVisibility = room ? (room.visibility || 'all') : 'all';
    state.roomSelected = new Set(room && room.member_ids ? room.member_ids.map(Number) : []);
    syncRoomVisibility();
    renderRoomMembers();
    document.getElementById('room-dialog-kicker').textContent = room ? 'ТЕМА' : 'НОВАЯ ТЕМА';
    document.getElementById('room-dialog-title').textContent = room ? 'Настройки темы' : 'Новая тема';
    if (room) roomForm.elements.name.value = room.name || '';
    roomDialog.showModal();
  }

  async function submitRoom(event) {
    event.preventDefault();
    const name = roomForm.elements.name.value.trim();
    if (!name) return;
    if (roomError) roomError.hidden = true;
    const editing = state.editingRoom;
    const visibility = state.roomVisibility || 'all';
    const memberIds = visibility === 'selected' && roomMembers
      ? Array.from(roomMembers.querySelectorAll('[data-room-member]:checked')).map((input) => Number(input.dataset.roomMember))
      : [];
    if (visibility === 'selected' && !memberIds.length) {
      if (roomError) {
        roomError.textContent = 'Выберите хотя бы одного сотрудника';
        roomError.hidden = false;
      }
      return;
    }
    const body = { name, visibility, member_ids: memberIds };
    try {
      if (editing) await api(`/chat/rooms/${editing.id}`, { method: 'PATCH', body: JSON.stringify(body) });
      else await api('/chat/rooms', { method: 'POST', body: JSON.stringify(body) });
      roomDialog.close();
      const saved = editing ? 'Тема обновлена' : 'Тема создана';
      const note = document.getElementById('chat-room-note');
      if (note) note.textContent = saved;
      showToast(saved);
      if (editing && state.channel === 'room' && Number(state.roomId) === Number(editing.id)) {
        el.title.textContent = name;
      }
      state.editingRoom = null;
      await renderRoomAdmin();
      if (state.channel === 'room') await safe(loadRooms).then(renderList);
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
    roomVisibility?.querySelectorAll('[data-room-visibility]').forEach((button) => {
      button.addEventListener('click', async () => {
        state.roomVisibility = button.dataset.roomVisibility;
        if (state.roomVisibility === 'selected' && !state.contacts.length) await safe(loadContacts);
        syncRoomVisibility();
        renderRoomMembers();
      });
    });

    wireComposer();
    wireMessageMenu();
    wirePolls();
    wirePollDialog();
    setupKeyboard();
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
