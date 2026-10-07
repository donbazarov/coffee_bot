const csrfToken = document.body.dataset.csrf || '';
let toastTimer;
let userRecords = [];

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[character]);
}

function showToast(message) {
  const toast = document.querySelector('#toast');
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add('is-visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('is-visible'), 3200);
}

async function api(path, options = {}) {
  const headers = { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) };
  if (options.method && options.method !== 'GET') headers['X-CSRF-Token'] = csrfToken;
  const response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || 'Не удалось выполнить запрос');
  return data;
}

window.telegramLogin = async function (telegramUser) {
  const errorNode = document.querySelector('#login-error');
  try {
    const response = await fetch('/api/auth/telegram', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin', body: JSON.stringify(telegramUser)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Не удалось войти');
    window.location.reload();
  } catch (error) {
    if (errorNode) { errorNode.textContent = error.message; errorNode.hidden = false; }
  }
};

function renderChart(days) {
  const chart = document.querySelector('#weekly-chart');
  if (!chart) return;
  const max = Math.max(1, ...days.map((day) => day.count));
  chart.innerHTML = days.map((day, index) => `
    <div class="chart-column" title="${escapeHtml(day.day)}: ${day.count} оценок">
      <div class="chart-bar-wrap"><div class="chart-bar" style="height:${Math.max(2, day.count / max * 100)}%;animation-delay:${index * 35}ms"></div></div>
      <span class="chart-day">${escapeHtml(day.day)}</span>
    </div>`).join('');
}

function renderLeaderboard(users) {
  const node = document.querySelector('#leaderboard');
  if (!node) return;
  if (!users.length) { node.innerHTML = '<p class="empty-state">За выбранный период оценок пока нет</p>'; return; }
  node.innerHTML = users.map((person, index) => `
    <div class="leader-row"><span class="leader-rank">${String(index + 1).padStart(2, '0')}</span>
      <span class="leader-name" title="${escapeHtml(person.name)}">${escapeHtml(person.name)}</span>
      <span class="leader-count">${person.count} оц.</span><span class="leader-score">${person.average ?? '—'}</span>
    </div>`).join('');
}

function renderRecent(reviews) {
  const node = document.querySelector('#recent-reviews');
  if (!node) return;
  if (!reviews.length) { node.innerHTML = '<p class="empty-state">Оценок за этот период пока нет</p>'; return; }
  node.innerHTML = reviews.map((review) => {
    const date = review.created_at ? String(review.created_at).slice(0, 16).replace('T', ' ') : '';
    const type = [review.drink_type, review.point].filter(Boolean).join(' · ');
    return `<article class="review-row">
      <div class="review-person"><strong>${escapeHtml(review.barista)}</strong><span>${escapeHtml(review.author || 'Команда')}</span></div>
      <span class="review-meta">${escapeHtml(type || review.category)}</span>
      <span class="review-time">${escapeHtml(date)}</span>
      <span class="review-score">${review.score ?? '—'} / 5</span>
    </article>`;
  }).join('');
}

async function loadDashboard(period = '30d') {
  try {
    const data = await api(`/api/dashboard?period=${encodeURIComponent(period)}`);
    document.querySelector('#metric-reviews').textContent = data.review_count;
    document.querySelector('#metric-average').textContent = data.average ?? '—';
    document.querySelector('#metric-staff').textContent = data.active_users;
    renderChart(data.daily);
    renderLeaderboard(data.leaderboard);
    renderRecent(data.recent);
  } catch (error) { showToast(error.message); }
}

function renderUsers(users) {
  userRecords = users;
  const list = document.querySelector('#user-list');
  const count = document.querySelector('#team-count');
  if (!list || !count) return;
  const activeCount = users.filter((user) => user.is_active).length;
  count.textContent = `${activeCount} активных · ${users.length} всего`;
  if (!users.length) { list.innerHTML = '<p class="empty-state">Сотрудников пока нет</p>'; return; }
  const roleNames = { barista: 'Бариста', senior: 'Старший', mentor: 'Наставник' };
  list.innerHTML = users.map((user) => `
    <article class="user-row ${user.is_active ? '' : 'is-inactive'}" data-user-id="${user.id}">
      <div class="user-person"><span class="user-avatar">${escapeHtml((user.name || '?').slice(0, 1).toUpperCase())}</span><strong title="${escapeHtml(user.name)}">${escapeHtml(user.name)}</strong></div>
      <span class="user-detail user-iiko">${user.iiko_id ? `Iiko ${escapeHtml(user.iiko_id)}` : 'Iiko не указан'}</span>
      <span class="user-detail user-telegram">${user.telegram_username ? `@${escapeHtml(user.telegram_username)}` : 'Telegram не привязан'}</span>
      <span class="user-detail user-role">${roleNames[user.role] || escapeHtml(user.role)}</span>
      <div class="user-actions"><button class="user-edit" type="button" data-edit>Изменить</button><button class="user-toggle ${user.is_active ? 'is-active' : ''}" type="button" data-active="${user.is_active ? 'true' : 'false'}">${user.is_active ? 'Активен' : 'Восстановить'}</button></div>
    </article>`).join('');
}

async function loadUsers() {
  try { renderUsers(await api('/api/users')); }
  catch (error) { showToast(error.message); }
}

function setView(name) {
  document.querySelectorAll('[data-view]').forEach((button) => button.classList.toggle('is-active', button.dataset.view === name));
  document.querySelectorAll('.view').forEach((view) => {
    const active = view.id === `${name}-view`;
    view.classList.toggle('is-visible', active);
    view.hidden = !active;
  });
  const title = document.querySelector('#page-title');
  if (title) title.textContent = name === 'team' ? 'Управление командой' : `Добрый день, ${title.dataset.name}`;
  if (name === 'team') loadUsers();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function setupDashboard() {
  document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => setView(button.dataset.view)));
  document.querySelectorAll('[data-period]').forEach((button) => button.addEventListener('click', () => {
    document.querySelectorAll('[data-period]').forEach((item) => item.classList.toggle('is-selected', item === button));
    loadDashboard(button.dataset.period);
  }));

  document.querySelector('#logout-button')?.addEventListener('click', async () => {
    try { await api('/api/logout', { method: 'POST' }); window.location.reload(); }
    catch (error) { showToast(error.message); }
  });

  const dialog = document.querySelector('#user-dialog');
  const form = document.querySelector('#user-form');
  const formError = document.querySelector('#user-form-error');
  const setDialogMode = (user = null) => {
    form.reset();
    formError.hidden = true;
    form.dataset.userId = user?.id || '';
    document.querySelector('#user-dialog-kicker').textContent = user ? 'УЧЁТНАЯ ЗАПИСЬ' : 'НОВАЯ ЗАПИСЬ';
    document.querySelector('#user-dialog-title').textContent = user ? 'Изменить сотрудника' : 'Сотрудник';
    document.querySelector('#user-form-submit').textContent = user ? 'Сохранить' : 'Добавить';
    if (user) {
      form.elements.name.value = user.name;
      form.elements.role.value = user.role;
      form.elements.iiko_id.value = user.iiko_id ?? '';
      form.elements.telegram_username.value = user.telegram_username ?? '';
    }
    dialog.showModal();
  };
  document.querySelector('#add-user-button')?.addEventListener('click', () => setDialogMode());
  document.querySelectorAll('.dialog-close').forEach((button) => button.addEventListener('click', () => dialog?.close()));
  dialog?.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });

  form?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const errorNode = formError;
    const formData = new FormData(form);
    const payload = Object.fromEntries(formData.entries());
    payload.iiko_id = payload.iiko_id || null;
    payload.telegram_username = payload.telegram_username || null;
    try {
      const userId = form.dataset.userId;
      await api(userId ? `/api/users/${userId}` : '/api/users', { method: userId ? 'PATCH' : 'POST', body: JSON.stringify(payload) });
      dialog.close(); await loadUsers(); showToast(userId ? 'Изменения сохранены' : 'Сотрудник добавлен');
    } catch (error) { errorNode.textContent = error.message; errorNode.hidden = false; }
  });

  document.querySelector('#user-list')?.addEventListener('click', async (event) => {
    const editButton = event.target.closest('[data-edit]');
    if (editButton) {
      const row = editButton.closest('[data-user-id]');
      setDialogMode(userRecords.find((user) => String(user.id) === row.dataset.userId));
      return;
    }
    const button = event.target.closest('[data-active]');
    if (!button) return;
    const row = button.closest('[data-user-id]');
    try {
      await api(`/api/users/${row.dataset.userId}`, { method: 'PATCH', body: JSON.stringify({ is_active: button.dataset.active !== 'true' }) });
      await loadUsers(); showToast('Статус доступа обновлён');
    } catch (error) { showToast(error.message); }
  });

  loadDashboard();
}

if (document.body.dataset.authenticated === 'true') setupDashboard();
