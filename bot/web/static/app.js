const csrfToken = document.body.dataset.csrf || '';
let toastTimer;
let userRecords = [];
let calendarState = null;
let calendarCursor = new Date();
let matrixMode = 'edit';
let matrixAnchor = null;
let matrixExtent = null;
let matrixPointerDown = false;
let matrixClipboard = null;
let matrixChanges = new Map();
let editMode = false;
let modulePreferences = { quality_enabled: true, calendar_enabled: true };
const managerRole = document.body.dataset.manager === 'true';
const userRole = document.body.dataset.role || '';
const isMentor = userRole === 'mentor';
let baristaPreview = false;
let matrixDayWidth = null;
let editType = 'swap';
let templateReorderMode = false;
let activeTemplate = null;
let shiftTemplates = [];
let calendarAutoScroll = false;
let matrixAutoMonth = null;
const MATRIX_MIN_DAY = 18;
const MATRIX_MAX_DAY = 92;
const MATRIX_COMPACT = 46;
const MATRIX_ZOOM_STORAGE = 'matrix-day-width';
// Встроенный шаблон: снимает смену (soft delete). В БД не хранится и не удаляется.
const DAY_OFF_TEMPLATE = { id: 'dayoff', name: 'Выходной', builtin: true };

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[character]);
}

/* Кружок аватара: картинка, если есть файл (avatar_rev > 0), иначе инициал. */
function avatarMarkup(userId, avatarRev, name) {
  if (avatarRev) return `<img src="/avatars/${userId}.jpg?v=${avatarRev}" alt="" loading="lazy">`;
  return escapeHtml((name || '?').slice(0, 1).toUpperCase());
}

function showToast(message) {
  const toast = document.querySelector('#toast');
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add('is-visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('is-visible'), 3200);
}

let activeRequests = 0;
let progressBar = null;

function syncProgress() {
  if (!progressBar) {
    progressBar = document.createElement('div');
    progressBar.className = 'api-progress';
    document.body.appendChild(progressBar);
  }
  progressBar.classList.toggle('is-active', activeRequests > 0);
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function api(path, options = {}) {
  activeRequests += 1;
  syncProgress();
  try {
    const headers = { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) };
    if (options.method && options.method !== 'GET') headers['X-CSRF-Token'] = csrfToken;
    let response;
    try {
      response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
    } catch (networkError) {
      // Контейнер мог перезапускаться (деплой) — один раз тихо повторяем GET.
      if (!options.method || options.method === 'GET') {
        await delay(900);
        return api(path, { ...options, retried: true });
      }
      throw networkError;
    }
    if (response.status >= 500 && (!options.method || options.method === 'GET') && !options.retried) {
      await delay(900);
      return api(path, { ...options, retried: true });
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Не удалось выполнить запрос');
    return data;
  } finally {
    activeRequests = Math.max(0, activeRequests - 1);
    syncProgress();
  }
}

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
    animateNumber(document.querySelector('#metric-reviews'), data.review_count);
    animateNumber(document.querySelector('#metric-average'), data.average, 1);
    animateNumber(document.querySelector('#metric-staff'), data.active_users);
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
  const pendingCount = users.filter((user) => user.access_pending).length;
  count.textContent = `${activeCount} активных · ${pendingCount} ожидают · ${users.length} всего`;
  if (!users.length) { list.innerHTML = '<p class="empty-state">Сотрудников пока нет</p>'; return; }
  const roleNames = { barista: 'Бариста', senior: 'Старший', mentor: 'Наставник', guest: 'Ожидает доступа' };
  list.innerHTML = users.map((user) => {
    const shown = user.display_name || user.name;
    return `
    <article class="user-row ${user.is_active ? '' : 'is-inactive'} ${user.access_pending ? 'is-pending' : ''}" data-user-id="${user.id}">
      <div class="user-person"><span class="user-avatar">${avatarMarkup(user.id, user.avatar_rev, shown)}</span><strong title="${escapeHtml(shown)}">${escapeHtml(shown)}</strong></div>
      <span class="user-detail user-iiko">${user.iiko_id ? `Iiko ${escapeHtml(user.iiko_id)}` : 'Iiko не указан'}</span>
      <span class="user-detail user-telegram">${user.telegram_username ? `@${escapeHtml(user.telegram_username)}` : 'Telegram не привязан'}</span>
      <span class="user-detail user-role">${roleNames[user.role] || escapeHtml(user.role)}</span>
      <span class="user-detail user-code">${user.access_code ? `Код ${escapeHtml(user.access_code)}` : 'Кода нет'}</span>
      <div class="user-actions"><button class="user-edit" type="button" data-rotate-code>Новый код</button><button class="user-edit" type="button" data-edit>Изменить</button><button class="user-toggle ${user.is_active ? 'is-active' : ''}" type="button" data-active="${user.is_active ? 'true' : 'false'}" ${user.access_pending ? 'disabled title="Сначала назначьте роль через редактирование"' : ''}>${user.is_active ? 'Активен' : user.access_pending ? 'Ожидает роль' : 'Выдать доступ'}</button></div>
    </article>`; }).join('');
}

async function loadUsers() {
  try { renderUsers(await api('/api/users')); }
  catch (error) { showToast(error.message); }
}

const monthNames = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];
const weekdayNames = ['ПН', 'ВТ', 'СР', 'ЧТ', 'ПТ', 'СБ', 'ВС'];

function localDateString(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function displayDate(value, includeYear = false) {
  const date = new Date(`${value}T12:00:00`);
  return new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', ...(includeYear ? { year: 'numeric' } : {}) }).format(date);
}

/* Нарезка диапазона публикации. Должна совпадать с split_period() в
   bot/web/schedule_snapshot.py: окна выравниваются по длине (01–15 → 01–08, 09–15). */
const SNAPSHOT_CHUNK_DAYS = 8;
const MAX_PUBLISH_DAYS = 92;

function parseIsoDate(value) {
  if (!value) return null;
  const date = new Date(`${value}T12:00:00`);
  return Number.isNaN(date.getTime()) ? null : date;
}

function splitPublishPeriod(startValue, endValue) {
  const start = parseIsoDate(startValue);
  const end = parseIsoDate(endValue);
  if (!start || !end) return [];
  const first = start <= end ? start : end;
  const last = start <= end ? end : start;
  const total = Math.round((last - first) / 86400000) + 1;
  if (total < 1 || total > MAX_PUBLISH_DAYS) return [];
  const chunks = Math.ceil(total / SNAPSHOT_CHUNK_DAYS);
  const base = Math.floor(total / chunks);
  const extra = total % chunks;
  const windows = [];
  let cursor = new Date(first);
  for (let index = 0; index < chunks; index += 1) {
    const windowEnd = new Date(cursor);
    windowEnd.setDate(windowEnd.getDate() + base + (index < extra ? 1 : 0) - 1);
    windows.push([new Date(cursor), windowEnd]);
    cursor = new Date(windowEnd);
    cursor.setDate(cursor.getDate() + 1);
  }
  return windows;
}

function formatDayMonth(date) {
  return new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit' }).format(date);
}

function renderUpcoming(shifts) {
  const list = document.querySelector('#upcoming-shifts');
  if (!list) return;
  if (!shifts.length) { list.innerHTML = '<p class="empty-state">Ближайших смен нет</p>'; return; }
  list.innerHTML = shifts.map((shift) => `
    <div class="upcoming-item"><span class="upcoming-date">${escapeHtml(displayDate(shift.date))}</span>
      <span class="upcoming-time">${escapeHtml(shift.start)}–${escapeHtml(shift.end)}</span>
      <span class="upcoming-point">${escapeHtml(shift.point)}</span></div>`).join('');
}

function updateEditControls() {
  if (!editMode) activeTemplate = null;
  document.body.classList.toggle('calendar-editing', editMode);
  const swapsEntry = document.querySelector('#edit-swaps-toggle');
  const scheduleEntry = document.querySelector('#edit-schedule-toggle');
  if (swapsEntry) swapsEntry.hidden = editMode;
  if (scheduleEntry) scheduleEntry.hidden = editMode || !isMentor || baristaPreview;
  const tools = document.querySelector('.matrix-edit-tools');
  if (tools) tools.hidden = !editMode;
  const hasChanges = matrixChanges.size > 0;
  const swapSave = document.querySelector('#matrix-save');
  if (swapSave) { swapSave.hidden = editType !== 'swap'; swapSave.disabled = !editMode || !hasChanges; }
  const draftSave = document.querySelector('#matrix-save-draft');
  if (draftSave) { draftSave.hidden = editType !== 'schedule'; draftSave.disabled = !editMode || !hasChanges; }
  const publishButton = document.querySelector('#matrix-publish');
  // Публиковать можно и без правок — только снимки за выбранный диапазон.
  if (publishButton) { publishButton.hidden = editType !== 'schedule'; publishButton.disabled = !editMode; }
  const summary = document.querySelector('#matrix-status');
  if (summary && !editMode) summary.textContent = 'Режим просмотра';
  const templates = document.querySelector('#matrix-templates');
  if (templates) templates.hidden = !editMode;
  renderTemplateBar();
}

function cellChangeKey(userId, date) {
  return `${userId}|${date}`;
}

function getEffectiveShift(employeeId, date) {
  const pending = matrixChanges.get(cellChangeKey(employeeId, date));
  if (pending === null) return null;
  const saved = calendarState?.shifts.find((shift) => shift.employee_id === employeeId && shift.date === date);
  return pending ? { ...saved, start: pending.start_time, end: pending.end_time, ...pending, employee_id: employeeId, date } : saved;
}

function setDraftCell(userId, date, value, redraw = true) {
  const key = cellChangeKey(userId, date);
  const saved = calendarState?.shifts.find((shift) => shift.employee_id === userId && shift.date === date);
  if (!value && !saved) matrixChanges.delete(key);
  else if (value && saved && value.start_time === saved.start && value.end_time === saved.end && value.point === saved.point) matrixChanges.delete(key);
  else matrixChanges.set(key, value);
  if (redraw) {
    renderCalendar(calendarState);
    updateEditControls();
  }
}

function describeChange(row) {
  const date = displayDate(row.shift_date, true);
  if (row.action === 'created') return `${date} · ${row.employee_name}: ${row.new_start_time}–${row.new_end_time}, ${row.new_point}`;
  if (row.action === 'deleted') return `${date} · ${row.employee_name}: выходной (было ${row.old_start_time}–${row.old_end_time}, ${row.old_point})`;
  return `${date} · ${row.employee_name}: ${row.old_start_time}–${row.old_end_time}, ${row.old_point} → ${row.new_start_time}–${row.new_end_time}, ${row.new_point}`;
}

async function loadShiftHistory() {
  const list = document.querySelector('#shift-history');
  if (!list || !calendarState) return;
  try {
    const entries = await api(`/api/shift-history?year=${calendarState.year}&month=${calendarState.month}`);
    document.querySelector('#history-count').textContent = entries.length;
    list.innerHTML = entries.length ? entries.map(historyEntryHtml).join('')
      : '<p class="empty-state">Изменений в этом месяце пока нет</p>';
  } catch (error) { showToast(error.message); }
}

function historySnapshotUrls(row) {
  if (row.snapshot_paths) {
    try {
      const parsed = JSON.parse(row.snapshot_paths);
      if (Array.isArray(parsed) && parsed.length) return parsed;
    } catch { /* старые записи и битый JSON — падаем на snapshot_path */ }
  }
  return row.snapshot_path ? [row.snapshot_path] : [];
}

function historyEntryHtml(row) {
  const actor = `${escapeHtml(row.actor_name)} · ${escapeHtml(String(row.created_at).slice(0, 16))}`;
  if (row.action === 'published' || row.change_type === 'schedule') {
    const period = row.period_start && row.period_end
      ? `${displayDate(row.period_start)} – ${displayDate(row.period_end, true)}`
      : displayDate(row.shift_date, true);
    const count = row.changes_count ?? 0;
    const shots = historySnapshotUrls(row);
    const shot = shots.length
      ? `<div class="history-shots">${shots.map((url) => `<a class="history-shot" href="${escapeHtml(url)}" target="_blank" rel="noopener"><img src="${escapeHtml(url)}" alt="Снимок расписания" loading="lazy"></a>`).join('')}</div>`
      : '<span class="history-shot-missing">снимок удалён по сроку хранения</span>';
    const shotsLabel = shots.length > 1 ? ` · снимков: ${shots.length}` : '';
    return `<article class="history-row is-schedule"><span class="history-change">Расписание · ${escapeHtml(period)} · правок: ${count}${shotsLabel}</span><span class="history-actor">${actor}</span>${shot}</article>`;
  }
  return `<article class="history-row"><span class="history-change">${escapeHtml(describeChange(row))}</span><span class="history-actor">${actor}</span></article>`;
}

async function saveScheduleChanges(publish = true, period = null) {
  // Публикация возможна и без правок — тогда нужен только диапазон дат.
  if (!matrixChanges.size && !(publish && period)) return;
  const operations = [...matrixChanges.entries()].map(([key, value]) => {
    const [userId, date] = key.split('|');
    return value ? { user_id: Number(userId), date, ...value } : { user_id: Number(userId), date, delete: true };
  });
  const body = editType === 'schedule'
    ? { operations, change_type: 'schedule', publish, ...(period ? { publish_start: period.start, publish_end: period.end } : {}) }
    : { operations, change_type: 'swap' };
  try {
    const result = await api('/api/shifts/save', { method: 'POST', body: JSON.stringify(body) });
    matrixChanges.clear();
    editMode = false;
    matrixAnchor = null;
    matrixExtent = null;
    activeTemplate = null;
    updateEditControls();
    await loadCalendar();
    const status = document.querySelector('#matrix-status');
    const shots = result.snapshots?.length ?? 0;
    const savedText = result.change_type === 'schedule'
      ? (publish ? `Опубликовано · снимков: ${shots}` : 'Расписание сохранено')
      : `Сохранено ${result.logged} правок`;
    if (status) status.textContent = savedText;
    showToast(savedText);
  } catch (error) { showToast(error.message); }
}

/* Окно публикации: диапазон дат + пресеты «1–15» и «15–конец месяца». */
function publishMonthBounds() {
  const year = calendarCursor.getFullYear();
  const month = calendarCursor.getMonth();
  return {
    first: localDateString(new Date(year, month, 1)),
    middle: localDateString(new Date(year, month, 15)),
    last: localDateString(new Date(year, month + 1, 0)),
  };
}

function updatePublishPreview() {
  const form = document.querySelector('#publish-form');
  const preview = document.querySelector('#publish-preview');
  const errorNode = document.querySelector('#publish-form-error');
  if (!form || !preview) return [];
  const start = form.elements.publish_start.value;
  const end = form.elements.publish_end.value;
  errorNode.hidden = true;
  const windows = splitPublishPeriod(start, end);
  if (!start || !end) {
    preview.textContent = 'Выберите начало и конец диапазона';
    return [];
  }
  if (!windows.length) {
    preview.textContent = '';
    errorNode.textContent = `Диапазон должен быть от 1 до ${MAX_PUBLISH_DAYS} дней`;
    errorNode.hidden = false;
    return [];
  }
  const plan = windows.map(([from, to]) => `${formatDayMonth(from)}–${formatDayMonth(to)}`).join(', ');
  const employees = calendarState?.employees?.length ?? 0;
  preview.textContent = `Снимков: ${windows.length} · ${plan} · сотрудников: ${employees}`;
  return windows;
}

function openPublishDialog() {
  const dialog = document.querySelector('#publish-dialog');
  const form = document.querySelector('#publish-form');
  if (!dialog || !form) return;
  const bounds = publishMonthBounds();
  form.reset();
  form.elements.publish_start.value = bounds.first;
  form.elements.publish_end.value = bounds.middle;
  document.querySelector('#publish-form-error').hidden = true;
  updatePublishPreview();
  dialog.showModal();
}

function discardScheduleChanges() {
  matrixChanges.clear();
  editMode = false;
  matrixAnchor = null;
  matrixExtent = null;
  updateEditControls();
  if (calendarState) renderCalendar(calendarState);
}

function applyModuleVisibility() {
  const summary = document.querySelector('#calendar-summary');
  const quality = document.querySelector('#quality-dashboard');
  if (summary) summary.hidden = !modulePreferences.calendar_enabled;
  if (quality) quality.hidden = !modulePreferences.quality_enabled;
  document.querySelectorAll('[data-view="calendar"]').forEach((button) => { button.hidden = !modulePreferences.calendar_enabled; });
  if (!modulePreferences.calendar_enabled && document.querySelector('#calendar-view')?.classList.contains('is-visible')) setView('profile');
}

function applyBaristaPreview(enabled) {
  baristaPreview = enabled;
  document.body.classList.toggle('barista-preview', enabled);
  document.querySelectorAll('[data-view="team"], [data-view="control"]').forEach((button) => { button.hidden = enabled; });
  const toggle = document.querySelector('#barista-preview-toggle');
  if (toggle) toggle.checked = enabled;
  const roleLabel = document.querySelector('.account-copy span');
  if (roleLabel) roleLabel.textContent = enabled ? 'Бариста · предпросмотр' : document.body.dataset.roleLabel;
  if (enabled && (document.querySelector('#team-view')?.classList.contains('is-visible') || document.querySelector('#control-view')?.classList.contains('is-visible'))) setView('overview');
  // Черновик не сбрасываем: бариста тоже может делать «Замену». Но у него нет
  // инструментов наставника (шаблоны и тип правки) — обновляем панель.
  updateEditControls();
}

async function loadPreferences() {
  try {
    modulePreferences = await api('/api/preferences');
    document.querySelector('#quality-module-toggle').checked = modulePreferences.quality_enabled;
    document.querySelector('#calendar-module-toggle').checked = modulePreferences.calendar_enabled;
    applyModuleVisibility();
    applyServerAppearance(modulePreferences);
  } catch (error) { showToast(error.message); }
  applyBaristaPreview(managerRole && localStorage.getItem('barista-preview') === 'true');
}

/* --- Масштаб матрицы ---------------------------------------------------
   Зум управляет плотностью: шириной колонки-дня и высотой строки. Шапка
   (день недели, число) и колонка имён остаются фиксированного размера —
   именно это даёт «весь месяц на экране, но подписи читаемы». */

function nameColumnWidth() {
  return window.matchMedia('(max-width: 760px)').matches ? 118 : 158;
}

function matrixScroll() {
  return document.querySelector('.matrix-scroll');
}

function employeeRowCount() {
  return (calendarState?.employees || []).filter((person) => person.iiko_id !== null && person.iiko_id !== undefined).length || 1;
}

function clampDayWidth(value) {
  return Math.min(MATRIX_MAX_DAY, Math.max(MATRIX_MIN_DAY, Math.round(value)));
}

function defaultDayWidth() {
  const scroll = matrixScroll();
  const available = Math.max(120, (scroll?.clientWidth || 0) - nameColumnWidth() - 4);
  return clampDayWidth(Math.min(62, available / 5));
}

function currentDayWidth() {
  return matrixDayWidth ?? defaultDayWidth();
}

function applyMatrixZoom() {
  const scroll = matrixScroll();
  if (!scroll) return;
  const dayWidth = clampDayWidth(currentDayWidth());
  const rows = employeeRowCount();
  const budget = scroll.clientHeight - 45;
  const fitRow = budget > 0 ? Math.floor(budget / rows) : 53;
  const rowHeight = Math.max(30, Math.min(53, Math.floor(Math.min(dayWidth * 0.85, fitRow))));
  scroll.style.setProperty('--day-w', `${dayWidth}px`);
  scroll.style.setProperty('--row-h', `${rowHeight}px`);
  scroll.classList.toggle('is-compact', dayWidth < MATRIX_COMPACT);
}

function setMatrixZoom(dayWidth, persist = true) {
  matrixDayWidth = clampDayWidth(dayWidth);
  applyMatrixZoom();
  if (persist) {
    try { localStorage.setItem(MATRIX_ZOOM_STORAGE, String(matrixDayWidth)); } catch (error) { /* приватный режим */ }
  }
}

function fitMatrixMonth() {
  const days = calendarState?.days_in_month || 31;
  const scroll = matrixScroll();
  const available = Math.max(120, (scroll?.clientWidth || 0) - nameColumnWidth() - 4);
  setMatrixZoom(available / days);
}

function scrollMatrixToToday() {
  const scroll = matrixScroll();
  if (!scroll) return;
  const cell = scroll.querySelector('.matrix-cell-wrap.is-today');
  // Если «сегодня» в отображаемом месяце нет — показываем начало месяца.
  scroll.scrollLeft = cell ? Math.max(0, cell.offsetLeft - nameColumnWidth()) : 0;
}

function compactHour(value) {
  return String(value || '').replace(/^0/, '').slice(0, 5);
}

function touchDistance(touches) {
  const dx = touches[0].clientX - touches[1].clientX;
  const dy = touches[0].clientY - touches[1].clientY;
  return Math.hypot(dx, dy) || 1;
}

/* --- Шаблоны смен и тип правки ---------------------------------------- */

function templateLabel(template) {
  return `${template.name} · ${template.start}–${template.end} ${template.point}`;
}

function calendarEmployees() {
  return (calendarState?.employees || []).filter((person) => person.iiko_id !== null && person.iiko_id !== undefined);
}

function cellDateFromColumn(column) {
  if (!calendarState) return null;
  const day = column + 1;
  if (day < 1 || day > calendarState.days_in_month) return null;
  return `${calendarState.year}-${String(calendarState.month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
}

function stageTemplate(template, employeeId, date, redraw = true) {
  if (template.builtin) { setDraftCell(employeeId, date, null, redraw); return; }
  setDraftCell(employeeId, date, { start_time: template.start, end_time: template.end, point: template.point }, redraw);
}

function applyActiveTemplateToSelection() {
  if (!activeTemplate) return 0;
  const bounds = matrixSelectionBounds();
  if (!bounds || !calendarState) return 0;
  const employees = calendarEmployees();
  let staged = 0;
  for (let row = bounds.top; row <= bounds.bottom; row += 1) {
    const employee = employees[row];
    if (!employee) continue;
    for (let col = bounds.left; col <= bounds.right; col += 1) {
      const date = cellDateFromColumn(col);
      if (!date) continue;
      stageTemplate(activeTemplate, employee.id, date, false);
      staged += 1;
    }
  }
  if (staged) { renderCalendar(calendarState); updateEditControls(); }
  return staged;
}

function templateClass(template, active) {
  if (template.builtin) return `matrix-template is-dayoff ${active ? 'is-active' : ''}`;
  const point = template.point === 'УЯ' ? 'point-uy' : 'point-de';
  return `matrix-template ${point} ${active ? 'is-active' : ''}`;
}

function renderTemplateBar() {
  const bar = document.querySelector('#matrix-templates');
  if (!bar) return;
  if (!editMode) { bar.hidden = true; bar.innerHTML = ''; return; }
  bar.hidden = false;
  const templates = [DAY_OFF_TEMPLATE, ...shiftTemplates];
  const buttons = templates.map((template) => {
    const active = activeTemplate && activeTemplate.id === template.id;
    const title = template.builtin ? 'Выходной: снять смену' : templateLabel(template);
    return `<button type="button" class="${templateClass(template, active)}" data-template-id="${template.id}" title="${escapeHtml(title)}">${escapeHtml(template.name)}</button>`;
  }).join('');
  const hint = shiftTemplates.length ? '' : '<span class="matrix-template-empty">свои шаблоны — в личном кабинете</span>';
  bar.innerHTML = `<span class="matrix-templates-label">ШАБЛОНЫ</span>${buttons}${hint}`;
  bar.querySelectorAll('[data-template-id]').forEach((button) => {
    button.addEventListener('click', () => onTemplateButton(button.dataset.templateId));
  });
}

function onTemplateButton(templateId) {
  const template = templateId === DAY_OFF_TEMPLATE.id
    ? DAY_OFF_TEMPLATE
    : shiftTemplates.find((item) => String(item.id) === templateId);
  if (!template) return;
  const status = document.querySelector('#matrix-status');
  if (activeTemplate && activeTemplate.id === template.id) {
    activeTemplate = null;
    renderTemplateBar();
    if (status) status.textContent = 'Шаблон снят';
    return;
  }
  activeTemplate = template;
  // В режиме «Диапазон» с выделением — сразу заполняем выделенные ячейки.
  const filled = matrixMode === 'select' ? applyActiveTemplateToSelection() : 0;
  renderTemplateBar();
  if (status) {
    status.textContent = filled
      ? `Шаблон «${template.name}»: заполнено ячеек — ${filled}`
      : `Шаблон «${template.name}»: нажмите по ячейке или выделите диапазон`;
  }
}

async function loadShiftTemplates() {
  try {
    shiftTemplates = await api('/api/shift-templates');
  } catch (error) {
    shiftTemplates = [];
  }
  renderTemplateBar();
  renderTemplateSettings();
}

/* --- Настройка шаблонов в личном кабинете ----------------------------- */

function renderTemplateSettings() {
  const list = document.querySelector('#template-list');
  if (!list) return;
  if (!shiftTemplates.length) {
    list.innerHTML = '<p class="empty-state">Шаблонов пока нет. Встроенный «Выходной» уже доступен в режиме правок.</p>';
    return;
  }
  list.innerHTML = shiftTemplates.map((template, index) => `
    <div class="template-row" data-template-row="${template.id}" draggable="true">
      <span class="template-drag" title="Перетащите, чтобы изменить порядок" aria-hidden="true">⠿</span>
      <span class="template-row-name"><i class="point-swatch ${template.point === 'УЯ' ? 'point-uy' : 'point-de'}"></i>${escapeHtml(template.name)}</span>
      <span class="template-row-meta">${escapeHtml(template.start)}–${escapeHtml(template.end)} · ${escapeHtml(template.point)}</span>
      <span class="template-row-actions">
        ${templateReorderMode ? `
          <button class="button button-quiet template-move" type="button" data-template-move="up" data-template-id="${template.id}" ${index === 0 ? 'disabled' : ''} title="Выше">▲</button>
          <button class="button button-quiet template-move" type="button" data-template-move="down" data-template-id="${template.id}" ${index === shiftTemplates.length - 1 ? 'disabled' : ''} title="Ниже">▼</button>
        ` : `
          <button class="button button-quiet" type="button" data-template-edit="${template.id}">Изменить</button>
          <button class="button button-quiet" type="button" data-template-delete="${template.id}">Удалить</button>
        `}
      </span>
    </div>`).join('');
}

function resetTemplateForm() {
  const form = document.querySelector('#template-form');
  if (!form) return;
  form.reset();
  form.elements.template_id.value = '';
  form.elements.point.value = 'УЯ';
  const error = document.querySelector('#template-form-error');
  if (error) { error.textContent = ''; error.hidden = true; }
  const submit = document.querySelector('#template-submit');
  if (submit) submit.textContent = 'Добавить шаблон';
  const cancel = document.querySelector('#template-cancel');
  if (cancel) cancel.hidden = true;
}

function editTemplate(template) {
  const form = document.querySelector('#template-form');
  if (!form) return;
  form.elements.template_id.value = String(template.id);
  form.elements.name.value = template.name;
  form.elements.start_time.value = template.start;
  form.elements.end_time.value = template.end;
  form.elements.point.value = template.point;
  const submit = document.querySelector('#template-submit');
  if (submit) submit.textContent = 'Сохранить шаблон';
  const cancel = document.querySelector('#template-cancel');
  if (cancel) cancel.hidden = false;
  form.scrollIntoView({ behavior: 'smooth', block: 'center' });
  form.elements.name.focus();
}

function setupShiftTemplates() {
  const form = document.querySelector('#template-form');
  if (!form) return;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const error = document.querySelector('#template-form-error');
    const showError = (message) => { if (error) { error.textContent = message; error.hidden = !message; } };
    const templateId = form.elements.template_id.value;
    const payload = {
      name: form.elements.name.value.trim(),
      start_time: form.elements.start_time.value,
      end_time: form.elements.end_time.value,
      point: form.elements.point.value,
    };
    if (!payload.name) { showError('Укажите название'); return; }
    if (!payload.start_time || !payload.end_time) { showError('Укажите время'); return; }
    try {
      await api(templateId ? `/api/shift-templates/${templateId}` : '/api/shift-templates', {
        method: templateId ? 'PATCH' : 'POST', body: JSON.stringify(payload),
      });
      resetTemplateForm();
      await loadShiftTemplates();
      showToast(templateId ? 'Шаблон обновлён' : 'Шаблон добавлен');
    } catch (requestError) { showError(requestError.message); }
  });
  document.querySelector('#template-cancel')?.addEventListener('click', resetTemplateForm);
  document.querySelector('#template-order-toggle')?.addEventListener('click', () => {
    templateReorderMode = !templateReorderMode;
    const button = document.querySelector('#template-order-toggle');
    if (button) button.textContent = templateReorderMode ? 'Готово' : 'Изменить порядок';
    renderTemplateSettings();
  });
  document.querySelector('#template-list')?.addEventListener('click', async (event) => {
    const moveButton = event.target.closest('[data-template-move]');
    if (moveButton) {
      await moveTemplate(moveButton.dataset.templateId, moveButton.dataset.templateMove);
      return;
    }
    const editButton = event.target.closest('[data-template-edit]');
    if (editButton) {
      const template = shiftTemplates.find((item) => String(item.id) === editButton.dataset.templateEdit);
      if (template) editTemplate(template);
      return;
    }
    const deleteButton = event.target.closest('[data-template-delete]');
    if (!deleteButton) return;
    const template = shiftTemplates.find((item) => String(item.id) === deleteButton.dataset.templateDelete);
    if (!template || !window.confirm(`Удалить шаблон «${template.name}»?`)) return;
    try {
      await api(`/api/shift-templates/${template.id}`, { method: 'DELETE' });
      if (activeTemplate && activeTemplate.id === template.id) activeTemplate = null;
      await loadShiftTemplates();
      showToast('Шаблон удалён');
    } catch (requestError) { showToast(requestError.message); }
  });
  setupTemplateDragAndDrop();
}

/* --- Порядок шаблонов: drag-and-drop ---------------------------------- */

function setupTemplateDragAndDrop() {
  const list = document.querySelector('#template-list');
  if (!list) return;
  let dragged = null;
  list.addEventListener('dragstart', (event) => {
    const row = event.target.closest('[data-template-row]');
    if (!row) return;
    dragged = row;
    row.classList.add('is-dragging');
    event.dataTransfer.effectAllowed = 'move';
    try { event.dataTransfer.setData('text/plain', row.dataset.templateRow); } catch (error) { /* Safari */ }
  });
  list.addEventListener('dragover', (event) => {
    if (!dragged) return;
    event.preventDefault();
    list.querySelectorAll('.is-drop-target').forEach((item) => item.classList.remove('is-drop-target'));
    const row = event.target.closest('[data-template-row]');
    if (row && row !== dragged) row.classList.add('is-drop-target');
  });
  list.addEventListener('drop', async (event) => {
    if (!dragged) return;
    event.preventDefault();
    const target = event.target.closest('[data-template-row]');
    if (target && target !== dragged) {
      const rect = target.getBoundingClientRect();
      const insertAfter = event.clientY > rect.top + rect.height / 2;
      list.insertBefore(dragged, insertAfter ? target.nextSibling : target);
      await persistTemplateOrder();
    }
  });
  list.addEventListener('dragend', () => {
    if (dragged) dragged.classList.remove('is-dragging');
    list.querySelectorAll('.is-drop-target').forEach((item) => item.classList.remove('is-drop-target'));
    dragged = null;
  });
}

async function persistOrder(ids) {
  try {
    shiftTemplates = await api('/api/shift-templates/reorder', { method: 'POST', body: JSON.stringify({ order: ids }) });
    renderTemplateSettings();
    renderTemplateBar();
    showToast('Порядок шаблонов сохранён');
  } catch (error) { showToast(error.message); }
}

async function moveTemplate(templateId, direction) {
  const index = shiftTemplates.findIndex((item) => String(item.id) === String(templateId));
  const target = direction === 'up' ? index - 1 : index + 1;
  if (index < 0 || target < 0 || target >= shiftTemplates.length) return;
  const next = [...shiftTemplates];
  [next[index], next[target]] = [next[target], next[index]];
  shiftTemplates = next;
  renderTemplateSettings();
  await persistOrder(next.map((item) => item.id));
}

async function persistTemplateOrder() {
  const list = document.querySelector('#template-list');
  if (!list) return;
  const order = [...list.querySelectorAll('[data-template-row]')].map((row) => Number(row.dataset.templateRow));
  await persistOrder(order);
}

/* --- Каналы Telegram (панель управления) ------------------------------ */

async function loadChannelSettings() {
  const form = document.querySelector('#channels-form');
  if (!form) return;
  try {
    const settings = await api('/api/app-settings');
    form.elements.announce_chat.value = settings.announce_chat || '';
    form.elements.swap_chat.value = settings.swap_chat || '';
  } catch (error) { /* секции нет — прав нет */ }
}

function setupChannelSettings() {
  const form = document.querySelector('#channels-form');
  if (!form) return;
  const error = document.querySelector('#channels-form-error');
  const showError = (message) => { if (error) { error.textContent = message; error.hidden = !message; } };
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    showError('');
    try {
      const saved = await api('/api/app-settings', {
        method: 'PATCH',
        body: JSON.stringify({
          announce_chat: form.elements.announce_chat.value.trim(),
          swap_chat: form.elements.swap_chat.value.trim(),
        }),
      });
      form.elements.announce_chat.value = saved.announce_chat || '';
      form.elements.swap_chat.value = saved.swap_chat || '';
      showToast('Каналы сохранены');
    } catch (requestError) { showError(requestError.message); }
  });
  loadChannelSettings();
}

function renderCalendar(data) {
  const grid = document.querySelector('#calendar-grid');
  if (!grid) return;
  const scrollNode = matrixScroll();
  const previousScrollLeft = scrollNode ? scrollNode.scrollLeft : 0;
  document.querySelector('#calendar-month-title').textContent = `${monthNames[data.month - 1]} ${data.year}`;
  const today = localDateString(new Date());
  const employees = data.employees.filter((person) => person.iiko_id !== null && person.iiko_id !== undefined);
  const dayHeaders = Array.from({ length: data.days_in_month }, (_, index) => {
    const day = index + 1;
    const date = new Date(data.year, data.month - 1, day);
    const weekday = weekdayNames[(data.first_weekday + index) % 7];
    return `<th class="matrix-day-header ${weekday === 'СБ' || weekday === 'ВС' ? 'is-weekend' : ''} ${localDateString(date) === today ? 'is-today' : ''}"><span>${weekday}</span><strong>${day}</strong></th>`;
  }).join('');
  const bodyRows = employees.map((employee, rowIndex) => {
    const cells = Array.from({ length: data.days_in_month }, (_, index) => {
      const day = index + 1;
      const cellDate = `${data.year}-${String(data.month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
      const cellShifts = data.shifts.filter((shift) => shift.employee_id === employee.id && shift.date === cellDate);
      const changeKey = `${employee.id}|${cellDate}`;
      const pending = matrixChanges.get(changeKey);
      const shift = pending === null ? null : pending ? {
        ...cellShifts[0], start: pending.start_time, end: pending.end_time, ...pending,
      } : cellShifts[0];
      const pointClass = shift?.point === 'УЯ' ? 'point-uy-cell' : shift ? 'point-de-cell' : '';
      const shiftContents = shift
        ? `<span class="matrix-shift-time">${escapeHtml(shift.start)}–${escapeHtml(shift.end)}</span><span class="matrix-shift-compact">${escapeHtml(compactHour(shift.start))}</span><span class="matrix-shift-point">${escapeHtml(shift.point)}</span>${cellShifts.length > 1 ? `<span class="matrix-shift-extra">+${cellShifts.length - 1}</span>` : ''}`
        : '<span class="matrix-empty-mark">Вых</span>';
      const rowData = `data-cell-date="${cellDate}" data-employee-id="${employee.id}" ${editMode ? `data-matrix-cell data-matrix-row="${rowIndex}" data-matrix-col="${day - 1}"` : ''}`;
      const pendingClass = pending !== undefined ? 'is-pending' : '';
      return `<td class="matrix-cell-wrap ${cellDate === today ? 'is-today' : ''}"><button type="button" class="matrix-cell ${shift ? 'has-shift' : 'is-empty'} ${pointClass} ${pendingClass}" ${editMode ? rowData : ''} data-cell-date="${cellDate}" data-employee-id="${employee.id}" data-shift-id="${shift?.id || ''}" aria-label="${escapeHtml(employee.name)}, ${day} ${monthNames[data.month - 1]}${shift ? `, ${shift.start}–${shift.end}, ${shift.point}` : ', выходной'}">${shiftContents}</button></td>`;
    }).join('');
    return `<tr><th class="matrix-employee sticky-column"><span class="matrix-avatar">${avatarMarkup(employee.id, employee.avatar_rev, employee.name)}</span><span class="matrix-employee-name" title="${escapeHtml(employee.name)}">${escapeHtml(employee.name)}</span></th>${cells}</tr>`;
  }).join('');
  grid.innerHTML = `<thead><tr><th class="matrix-name-header sticky-column">БАРИСТА</th>${dayHeaders}</tr></thead><tbody>${bodyRows || `<tr><td class="matrix-no-employees" colspan="${data.days_in_month + 1}">Нет активных сотрудников с iiko ID</td></tr>`}</tbody>`;
  document.querySelector('#matrix-summary')?.replaceChildren(document.createTextNode(`${employees.length} сотрудников · ${data.days_in_month} дней`));
  matrixAnchor = null;
  matrixExtent = null;
  updateMatrixSelection();
  applyMatrixZoom();
  const monthKey = `${data.year}-${data.month}`;
  const viewVisible = !document.querySelector('#calendar-view')?.hidden;
  if (viewVisible && (calendarAutoScroll || matrixAutoMonth !== monthKey)) {
    calendarAutoScroll = false;
    matrixAutoMonth = monthKey;
    // Синхронно: чтение offsetLeft форсирует пересчёт, а rAF не выполняется
    // в фоновой (невидимой) вкладке.
    scrollMatrixToToday();
  } else if (scrollNode) {
    // Перерисовка черновика не должна сбрасывать позицию скролла в начало.
    scrollNode.scrollLeft = previousScrollLeft;
  }

  const stats = data.month_stats;
  animateNumber(document.querySelector('#hours-total'), stats.total_hours, 1);
  animateNumber(document.querySelector('#hours-worked'), stats.worked_hours, 1);
  animateNumber(document.querySelector('#hours-remaining'), stats.remaining_hours, 1);
  renderUpcoming(stats.upcoming);
  const employeeSelect = document.querySelector('#shift-employee');
  if (employeeSelect) {
    employeeSelect.innerHTML = employees.map((person) => `<option value="${person.id}">${escapeHtml(person.name)}</option>`).join('');
  }
}

function updateMatrixSelection() {
  const grid = document.querySelector('#calendar-grid');
  if (!grid) return;
  const bounds = matrixAnchor && matrixExtent ? {
    top: Math.min(matrixAnchor.row, matrixExtent.row),
    bottom: Math.max(matrixAnchor.row, matrixExtent.row),
    left: Math.min(matrixAnchor.col, matrixExtent.col),
    right: Math.max(matrixAnchor.col, matrixExtent.col),
  } : null;
  grid.querySelectorAll('[data-matrix-cell]').forEach((cell) => {
    const row = Number(cell.dataset.matrixRow);
    const col = Number(cell.dataset.matrixCol);
    cell.classList.toggle('is-selected', Boolean(bounds && row >= bounds.top && row <= bounds.bottom && col >= bounds.left && col <= bounds.right));
  });
  const selected = Boolean(bounds);
  const copyButton = document.querySelector('#matrix-copy');
  const pasteButton = document.querySelector('#matrix-paste');
  if (copyButton) copyButton.disabled = !editMode || !selected;
  if (pasteButton) pasteButton.disabled = !editMode || !selected || !matrixClipboard;
  const saveButton = document.querySelector('#matrix-save');
  if (saveButton) saveButton.disabled = !editMode || matrixChanges.size === 0;
}

function matrixSelectionBounds() {
  if (!matrixAnchor || !matrixExtent) return null;
  return {
    top: Math.min(matrixAnchor.row, matrixExtent.row), bottom: Math.max(matrixAnchor.row, matrixExtent.row),
    left: Math.min(matrixAnchor.col, matrixExtent.col), right: Math.max(matrixAnchor.col, matrixExtent.col),
  };
}

function copyMatrixSelection() {
  const bounds = matrixSelectionBounds();
  if (!bounds || !calendarState) return;
  const employees = calendarState.employees.filter((person) => person.iiko_id !== null && person.iiko_id !== undefined);
  matrixClipboard = [];
  for (let row = bounds.top; row <= bounds.bottom; row += 1) {
    const employee = employees[row];
    const clipboardRow = [];
    for (let col = bounds.left; col <= bounds.right; col += 1) {
      const date = `${calendarState.year}-${String(calendarState.month).padStart(2, '0')}-${String(col + 1).padStart(2, '0')}`;
      const shift = getEffectiveShift(employee?.id, date);
      clipboardRow.push(shift ? { start_time: shift.start, end_time: shift.end, point: shift.point } : null);
    }
    matrixClipboard.push(clipboardRow);
  }
  const status = document.querySelector('#matrix-status');
  if (status) status.textContent = 'Диапазон скопирован';
  const tsv = matrixClipboard.map((row) => row.map((cell) => cell ? `${cell.start_time}–${cell.end_time} ${cell.point}` : '').join('\t')).join('\n');
  navigator.clipboard?.writeText(tsv).catch(() => {});
  updateMatrixSelection();
}

async function pasteMatrixSelection() {
  if (!matrixAnchor || !matrixClipboard || !calendarState) return;
  const employees = calendarState.employees.filter((person) => person.iiko_id !== null && person.iiko_id !== undefined);
  let staged = 0;
  for (let rowOffset = 0; rowOffset < matrixClipboard.length; rowOffset += 1) {
    const employee = employees[matrixAnchor.row + rowOffset];
    if (!employee) continue;
    for (let colOffset = 0; colOffset < matrixClipboard[rowOffset].length; colOffset += 1) {
      const day = matrixAnchor.col + colOffset + 1;
      if (day > calendarState.days_in_month) continue;
      const date = `${calendarState.year}-${String(calendarState.month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
      const source = matrixClipboard[rowOffset][colOffset];
      setDraftCell(employee.id, date, source ? { ...source } : null, false);
      staged += 1;
    }
  }
  if (!staged) return;
  renderCalendar(calendarState);
  updateEditControls();
  const status = document.querySelector('#matrix-status');
  if (status) status.textContent = `Вставлено ячеек: ${staged}. Нажмите «Сохранить изменения».`;
}

async function loadCalendar() {
  try {
    const year = calendarCursor.getFullYear();
    const month = calendarCursor.getMonth() + 1;
    calendarState = await api(`/api/calendar?year=${year}&month=${month}`);
    calendarAutoScroll = true;
    renderCalendar(calendarState);
    await loadShiftHistory();
  } catch (error) { showToast(error.message); }
}

function openShiftDialog(date, shift = null, employeeId = null) {
  const dialog = document.querySelector('#shift-dialog');
  const form = document.querySelector('#shift-form');
  if (!dialog || !form) return;
  form.reset();
  document.querySelector('#shift-dialog-title').textContent = shift ? 'Изменить смену' : 'Новая смена';
  document.querySelector('#shift-dialog-kicker').textContent = 'ЧЕРНОВИК ГРАФИКА';
  document.querySelector('#shift-form-error').hidden = true;
  form.elements.date.value = shift?.date || date || localDateString(new Date());
  if (employeeId) form.elements.user_id.value = employeeId;
  if (shift) {
    form.elements.user_id.value = shift.employee_id;
    form.elements.start_time.value = shift.start;
    form.elements.end_time.value = shift.end;
    form.querySelector(`input[name="point"][value="${shift.point}"]`).checked = true;
  }
  document.querySelector('#delete-shift-button').textContent = 'Выходной';
  dialog.showModal();
}

function prefersReducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/* Плавный подчёт числа: от текущего значения к целевому. При reduced-motion —
   сразу финальное значение (никакой анимации). */
function animateNumber(node, value, decimals = 0) {
  if (!node) return;
  if (value === null || value === undefined || value === '') { node.textContent = '—'; return; }
  const target = Number(value);
  if (!Number.isFinite(target)) { node.textContent = String(value); return; }
  const format = (number) => (decimals > 0 ? number.toFixed(decimals) : String(Math.round(number)));
  if (prefersReducedMotion()) { node.textContent = format(target); return; }
  const from = Number(String(node.textContent).replace(',', '.')) || 0;
  const duration = 650;
  const started = performance.now();
  const step = (now) => {
    const progress = Math.min(1, (now - started) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    node.textContent = format(from + (target - from) * eased);
    if (progress < 1) window.requestAnimationFrame(step);
  };
  window.requestAnimationFrame(step);
}

function setView(name) {
  if (name === 'calendar' && !modulePreferences.calendar_enabled) name = 'profile';
  if (name === 'control' && !managerRole) name = 'profile';
  const apply = () => {
    document.querySelectorAll('[data-view]').forEach((button) => button.classList.toggle('is-active', button.dataset.view === name));
    // Нижний док: пункты чата подсвечивает сам chat.js, при уходе с чата гасим их.
    if (name !== 'chat') document.querySelectorAll('[data-chat-open]').forEach((button) => button.classList.remove('is-active'));
    document.querySelectorAll('.view').forEach((view) => {
      const active = view.id === `${name}-view`;
      view.classList.toggle('is-visible', active);
      view.hidden = !active;
    });
    const title = document.querySelector('#page-title');
    if (title) {
      title.textContent =
        name === 'team' ? 'Управление командой' :
        name === 'calendar' ? 'График смен' :
        name === 'control' ? 'Панель управления' :
        name === 'announcements' ? 'Анонсы' :
        name === 'chat' ? 'Чат' :
        name === 'profile' ? 'Личный кабинет' :
        `Добрый день, ${title.dataset.name}`;
    }
    document.body.classList.toggle('settings-active', name === 'profile' || name === 'control');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };
  if (typeof document.startViewTransition === 'function' && !prefersReducedMotion()) {
    document.startViewTransition(apply);
  } else {
    apply();
  }
  if (name === 'team') loadUsers();
  if (name === 'chat') window.NeftChat?.open();
  if (name === 'announcements') openAnnouncements();
  if (name === 'calendar' && modulePreferences.calendar_enabled) loadCalendar();
  if (name === 'control' && managerRole) { loadStoriesAdmin(); renderAnnouncementCategories(); }
  closeDrawer();
}

/* --- Модерация историй гостей -------------------------------------------
   Публичная страница живёт по прямой ссылке /stories и в навигации её нет.
   Здесь только инструменты наставника: публикация, правка и удаление. */

const storiesAdmin = { filter: 'all', items: [], stats: null };

function formatStoryDate(value) {
  try {
    return new Date(value).toLocaleString('ru-RU', {
      day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  } catch (error) {
    return '';
  }
}

function storiesAdminVisible() {
  if (storiesAdmin.filter === 'pending') return storiesAdmin.items.filter((story) => !story.published);
  if (storiesAdmin.filter === 'published') return storiesAdmin.items.filter((story) => story.published);
  return storiesAdmin.items;
}

function renderStoriesAdmin() {
  const list = document.querySelector('#stories-admin-list');
  if (!list) return;

  const stats = document.querySelector('#stories-admin-stats');
  if (stats) {
    stats.textContent = storiesAdmin.stats
      ? `МОДЕРАЦИЯ: ${storiesAdmin.stats.pending} · ВСЕГО: ${storiesAdmin.stats.total}`
      : '—';
  }

  list.innerHTML = '';
  const items = storiesAdminVisible();
  if (!items.length) {
    list.innerHTML = '<p class="story-admin-empty">Историй нет.</p>';
    return;
  }
  items.forEach((story) => list.appendChild(buildStoryAdminCard(story)));
}

function buildStoryAdminCard(story) {
  const card = document.createElement('article');
  card.className = 'story-admin-card' + (story.published ? ' is-published' : '');
  card.dataset.storyId = story.id;

  card.innerHTML = `
    <div class="story-admin-main">
      <div class="story-admin-thumb${story.photo ? '' : ' is-empty'}">
        ${story.photo ? `<img src="${escapeHtml(story.photo)}" alt="Фото из истории" loading="lazy" decoding="async">` : ''}
      </div>
      <div class="story-admin-body">
        <div class="story-admin-head">
          <span class="story-admin-name">${escapeHtml(story.name || 'Гость НЕФТИ')}</span>
          <span class="story-admin-badge ${story.published ? 'is-published' : 'is-pending'}">${story.published ? 'Опубликовано' : 'На модерации'}</span>
          <span class="story-admin-date">${escapeHtml(formatStoryDate(story.createdAt))}</span>
        </div>
        <p class="story-admin-text">${escapeHtml(story.text)}</p>
      </div>
    </div>
    <div class="story-admin-actions">
      <button class="button ${story.published ? 'button-quiet' : 'button-primary'}" type="button" data-story-publish>${story.published ? 'Скрыть из карусели' : 'Опубликовать'}</button>
      <button class="button button-quiet" type="button" data-story-edit>Редактировать</button>
      <button class="button button-quiet" type="button" data-story-delete>Удалить</button>
    </div>
    <form class="story-admin-edit" hidden>
      <label>Имя гостя<input type="text" name="name" maxlength="40" value="${escapeHtml(story.name || '')}"></label>
      <label>Текст истории<textarea name="text" maxlength="600" rows="5">${escapeHtml(story.text)}</textarea></label>
      <div class="story-admin-photo-row">
        <label class="button button-quiet">Заменить фото<input type="file" name="photo" accept="image/png,image/jpeg,image/webp,image/gif" hidden></label>
        <button class="button button-quiet" type="button" data-story-photo-remove ${story.photo ? '' : 'disabled'}>Удалить фото</button>
        <span class="story-admin-photo-name">${story.photo ? 'Текущее фото сохранено' : 'Фото не выбрано'}</span>
      </div>
      <p class="story-admin-error" hidden></p>
      <div class="story-admin-edit-actions">
        <button class="button button-quiet" type="button" data-story-cancel>Отмена</button>
        <button class="button button-primary" type="submit">Сохранить</button>
      </div>
    </form>`;

  wireStoryAdminCard(card, story);
  return card;
}

function wireStoryAdminCard(card, story) {
  const status = document.querySelector('#stories-admin-status');
  const setStatus = (message) => { if (status) status.textContent = message; };
  const editForm = card.querySelector('.story-admin-edit');
  const errorNode = card.querySelector('.story-admin-error');
  const fileInput = card.querySelector('input[name="photo"]');
  const photoName = card.querySelector('.story-admin-photo-name');
  const removePhotoButton = card.querySelector('[data-story-photo-remove]');

  // photo = '' — не менять, dataURL — заменить, null — удалить
  let newPhoto = '';
  let removePhoto = false;

  const showError = (message) => { errorNode.textContent = message; errorNode.hidden = !message; };

  card.querySelector('[data-story-publish]').addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      const data = await api(`/api/stories/moderation/${story.id}`, {
        method: 'PATCH', body: JSON.stringify({ published: !story.published }),
      });
      applyStoriesAdminResponse(data);
      showToast(data.story.published ? 'История опубликована' : 'История снята с публикации');
    } catch (error) {
      showToast(error.message);
      event.currentTarget.disabled = false;
    }
  });

  card.querySelector('[data-story-delete]').addEventListener('click', async () => {
    if (!window.confirm('Удалить историю безвозвратно?')) return;
    try {
      const data = await api(`/api/stories/moderation/${story.id}`, { method: 'DELETE' });
      applyStoriesAdminResponse(data);
      showToast('История удалена');
    } catch (error) {
      showToast(error.message);
    }
  });

  card.querySelector('[data-story-edit]').addEventListener('click', () => {
    if (!editForm.hidden) { editForm.hidden = true; return; }
    editForm.elements.name.value = story.name || '';
    editForm.elements.text.value = story.text;
    newPhoto = '';
    removePhoto = false;
    fileInput.value = '';
    photoName.textContent = story.photo ? 'Текущее фото сохранено' : 'Фото не выбрано';
    removePhotoButton.disabled = !story.photo;
    showError('');
    editForm.hidden = false;
    editForm.elements.text.focus();
  });

  card.querySelector('[data-story-cancel]').addEventListener('click', () => { editForm.hidden = true; });
  editForm.elements.text.addEventListener('input', () => showError(''));
  editForm.elements.name.addEventListener('input', () => showError(''));

  fileInput.addEventListener('change', () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    if (!file.type.startsWith('image/')) { showError('Можно приложить только изображение'); return; }
    if (file.size > 5 * 1024 * 1024) { showError('Фотография слишком большая: максимум 5 МБ'); return; }
    const reader = new FileReader();
    reader.onload = () => {
      newPhoto = reader.result;
      removePhoto = false;
      removePhotoButton.disabled = false;
      photoName.textContent = `Новое фото: ${file.name}`;
      showError('');
    };
    reader.readAsDataURL(file);
  });

  removePhotoButton.addEventListener('click', () => {
    removePhoto = true;
    newPhoto = '';
    fileInput.value = '';
    photoName.textContent = 'Фото будет удалено';
    showError('');
  });

  editForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const name = editForm.elements.name.value.trim();
    const text = editForm.elements.text.value.trim();
    if (!name) { showError('Укажите имя'); return; }
    if (!text) { showError('История не может быть пустой'); return; }

    const payload = { name, text };
    if (newPhoto) payload.photo = newPhoto;
    else if (removePhoto) payload.photo = null;

    const submitButton = editForm.querySelector('button[type="submit"]');
    submitButton.disabled = true;
    try {
      const data = await api(`/api/stories/moderation/${story.id}`, { method: 'PATCH', body: JSON.stringify(payload) });
      applyStoriesAdminResponse(data);
      setStatus('Изменения сохранены');
      showToast('История обновлена');
    } catch (error) {
      showError(error.message);
      submitButton.disabled = false;
    }
  });
}

function applyStoriesAdminResponse(data) {
  const updated = data?.story;
  if (updated) {
    const index = storiesAdmin.items.findIndex((item) => item.id === updated.id);
    if (index === -1) storiesAdmin.items.unshift(updated);
    else storiesAdmin.items[index] = updated;
  }
  if (data?.stats) storiesAdmin.stats = data.stats;
  renderStoriesAdmin();
}

async function loadStoriesAdmin() {
  const list = document.querySelector('#stories-admin-list');
  if (!list) return;
  const status = document.querySelector('#stories-admin-status');
  if (status) status.textContent = 'Загрузка историй…';
  try {
    const data = await api('/api/stories/moderation');
    storiesAdmin.items = data.stories || [];
    storiesAdmin.stats = data.stats || null;
    if (status) status.textContent = '';
    renderStoriesAdmin();
  } catch (error) {
    if (status) status.textContent = error.message;
  }
}

function setupStoriesAdmin() {
  document.querySelectorAll('[data-stories-filter]').forEach((button) => {
    button.addEventListener('click', () => {
      storiesAdmin.filter = button.dataset.storiesFilter;
      document.querySelectorAll('[data-stories-filter]').forEach((item) => item.classList.toggle('is-active', item === button));
      renderStoriesAdmin();
    });
  });
  loadStoriesAdmin();
}

/* --- Оформление: тема и акцент -------------------------------------------
   Применяются мгновенно и сохраняются в localStorage (то же делает
   theme-boot.js до первой отрисовки). Серверная синхронизация по аккаунту —
   отдельным шагом поверх этих же ключей. */

const THEME_KEY = 'neft-theme';
const ACCENT_KEY = 'neft-accent';
const DEFAULT_ACCENT = '#f47369';
const ACCENT_PRESETS = ['#f47369', '#f4a259', '#e9c46a', '#2a9d8f', '#4a7ede', '#9b6bd6'];

function storedItem(key) { try { return localStorage.getItem(key); } catch (error) { return null; } }
function storeItem(key, value) { try { localStorage.setItem(key, value); } catch (error) { /* приватный режим */ } }

function systemTheme() {
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

function contrastOn(hex) {
  const match = /^#?([0-9a-f]{6})$/i.exec(String(hex || ''));
  if (!match) return '#2a1310';
  const value = parseInt(match[1], 16);
  const channel = (part) => { part /= 255; return part <= 0.03928 ? part / 12.92 : Math.pow((part + 0.055) / 1.055, 2.4); };
  const luminance = 0.2126 * channel((value >> 16) & 255) + 0.7152 * channel((value >> 8) & 255) + 0.0722 * channel(value & 255);
  return luminance > 0.3 ? '#241512' : '#ffffff';
}

function applyThemePreference(pref) {
  document.documentElement.setAttribute('data-theme', pref === 'light' || pref === 'dark' ? pref : systemTheme());
  syncThemeColor();
}

/* <meta name="theme-color"> — окраска интерфейса браузера под фон темы. */
function syncThemeColor() {
  const meta = document.querySelector('meta[name="theme-color"]');
  if (!meta) return;
  const background = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
  if (background) meta.setAttribute('content', background);
}

function applyAccent(hex) {
  document.documentElement.style.setProperty('--accent', hex);
  document.documentElement.style.setProperty('--accent-contrast', contrastOn(hex));
}

function syncAppearanceControls() {
  const pref = storedItem(THEME_KEY) || 'system';
  document.querySelectorAll('[data-theme-choice]').forEach((button) => button.classList.toggle('is-selected', button.dataset.themeChoice === pref));
  const storedAccent = String(storedItem(ACCENT_KEY) || '').toLowerCase();
  const current = validAccent(storedAccent) ? storedAccent : DEFAULT_ACCENT;
  document.querySelectorAll('#accent-row [data-accent]').forEach((swatch) => swatch.classList.toggle('is-selected', swatch.dataset.accent === current));
}

/* Серверные значения — источник истины (синхронизация между устройствами),
   localStorage остаётся кэшем для мгновенного старта. */
/* Акцент — только корректный #rrggbb: пустое/битое значение не сохраняем и не
   применяем, иначе палитра выглядит «сломанной» и цвет будто сбрасывается. */
function validAccent(value) {
  return /^#[0-9a-f]{6}$/i.test(String(value || ''));
}

function applyServerAppearance(prefs) {
  if (prefs && prefs.theme) { storeItem(THEME_KEY, prefs.theme); applyThemePreference(prefs.theme); }
  if (prefs && validAccent(prefs.accent)) { storeItem(ACCENT_KEY, prefs.accent); applyAccent(prefs.accent); }
  syncAppearanceControls();
}

function persistAppearance(values) {
  api('/api/preferences', { method: 'PATCH', body: JSON.stringify(values) }).catch(() => { /* не критично */ });
}

function renderAccentSwatches() {
  const row = document.querySelector('#accent-row');
  if (!row) return;
  const storedAccent = String(storedItem(ACCENT_KEY) || '').toLowerCase();
  const current = validAccent(storedAccent) ? storedAccent : DEFAULT_ACCENT;
  const swatches = ACCENT_PRESETS
    .map((color) => `<button type="button" class="accent-swatch" style="--swatch:${color}" data-accent="${color}" aria-label="Акцент ${color}"></button>`)
    .join('');
  row.innerHTML = `${swatches}<label class="accent-custom">Свой<input type="color" id="accent-custom-input" value="${/^#[0-9a-f]{6}$/.test(current) ? current : DEFAULT_ACCENT}" aria-label="Свой акцентный цвет"></label>`;
  row.querySelectorAll('[data-accent]').forEach((swatch) => swatch.addEventListener('click', () => {
    storeItem(ACCENT_KEY, swatch.dataset.accent);
    applyAccent(swatch.dataset.accent);
    syncAppearanceControls();
    persistAppearance({ accent: swatch.dataset.accent });
  }));
  const custom = row.querySelector('#accent-custom-input');
  // Пока тянем ползунок — только локальное превью (без запросов).
  custom?.addEventListener('input', () => {
    const color = custom.value.toLowerCase();
    if (!validAccent(color)) return;
    storeItem(ACCENT_KEY, color);
    applyAccent(color);
    syncAppearanceControls();
  });
  // На сервер уходит один PATCH — когда выбор завершён (change).
  custom?.addEventListener('change', () => {
    const color = custom.value.toLowerCase();
    if (!validAccent(color)) return;
    persistAppearance({ accent: color });
  });
}

function setupAppearance() {
  document.querySelectorAll('[data-theme-choice]').forEach((button) => button.addEventListener('click', () => {
    storeItem(THEME_KEY, button.dataset.themeChoice);
    applyThemePreference(button.dataset.themeChoice);
    syncAppearanceControls();
    persistAppearance({ theme: button.dataset.themeChoice });
  }));
  renderAccentSwatches();
  syncAppearanceControls();
}

/* --- Профиль: имя и фото ------------------------------------------------ */

function setupProfile() {
  const nameInput = document.querySelector('#display-name-input');
  const nameSave = document.querySelector('#display-name-save');
  const note = document.querySelector('#account-note');
  const fileInput = document.querySelector('#avatar-input');
  const uploadButton = document.querySelector('#avatar-upload');
  const resetButton = document.querySelector('#avatar-reset');
  if (!nameInput || !nameSave) return;

  const setNote = (message) => { if (note) note.textContent = message; };
  const refreshNames = (shown) => {
    document.querySelectorAll('.account-copy strong').forEach((node) => { node.textContent = shown; });
    const title = document.querySelector('#page-title');
    if (title) { title.dataset.name = shown; title.textContent = `Добрый день, ${shown}`; }
  };

  nameSave.addEventListener('click', async () => {
    setNote('');
    try {
      const result = await api('/api/profile', { method: 'PATCH', body: JSON.stringify({ display_name: nameInput.value }) });
      refreshNames(result.display_name || document.body.dataset.userName || '');
      setNote('Имя сохранено');
    } catch (error) { setNote(error.message); }
  });

  uploadButton?.addEventListener('click', () => fileInput?.click());
  fileInput?.addEventListener('change', async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    setNote('Загружаем фото…');
    try {
      const response = await fetch('/api/profile/avatar', {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': file.type || 'application/octet-stream' },
        body: file,
        credentials: 'same-origin',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить фото');
      window.location.reload();
    } catch (error) { setNote(error.message); }
  });

  resetButton?.addEventListener('click', async () => {
    setNote('');
    try { await api('/api/profile/avatar', { method: 'DELETE' }); window.location.reload(); }
    catch (error) { setNote(error.message); }
  });

  // Код доступа: смена не выкидывает текущее устройство (session_epoch не растёт),
  // а «выйти на всех устройствах» — наоборот, гасит все остальные сессии.
  const codeNode = document.querySelector('#access-code-value');
  const codeNote = document.querySelector('#access-code-note');
  const setCodeNote = (message) => { if (codeNote) codeNote.textContent = message; };
  document.querySelector('#access-code-refresh')?.addEventListener('click', async () => {
    setCodeNote('');
    try {
      const result = await api('/api/profile/code', { method: 'POST' });
      if (codeNode) codeNode.textContent = result.code;
      setCodeNote('Новый код сохранён. Старый больше не действует.');
    } catch (error) { setCodeNote(error.message); }
  });
  document.querySelector('#logout-all')?.addEventListener('click', async () => {
    setCodeNote('');
    try {
      await api('/api/session/logout-all', { method: 'POST' });
      setCodeNote('Остальные устройства вышли из системы.');
    } catch (error) { setCodeNote(error.message); }
  });
}

/* --- Сэндвич-drawer ------------------------------------------------------ */

function closeDrawer() {
  const drawer = document.querySelector('#drawer');
  if (!drawer) return;
  drawer.classList.remove('is-open');
  drawer.setAttribute('aria-hidden', 'true');
  document.querySelector('#drawer-backdrop')?.classList.remove('is-open');
  document.querySelector('#drawer-open')?.setAttribute('aria-expanded', 'false');
  document.body.classList.remove('drawer-open');
}

function setupDrawer() {
  const drawer = document.querySelector('#drawer');
  const backdrop = document.querySelector('#drawer-backdrop');
  const openButton = document.querySelector('#drawer-open');
  if (!drawer || !backdrop || !openButton) return;
  let lastFocus = null;

  const open = () => {
    lastFocus = document.activeElement;
    backdrop.classList.add('is-open');
    drawer.classList.add('is-open');
    drawer.setAttribute('aria-hidden', 'false');
    openButton.setAttribute('aria-expanded', 'true');
    document.body.classList.add('drawer-open');
    drawer.querySelector('button, a')?.focus();
  };

  openButton.addEventListener('click', open);
  document.querySelector('#drawer-close')?.addEventListener('click', () => { closeDrawer(); lastFocus?.focus?.(); });
  backdrop.addEventListener('click', closeDrawer);

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && drawer.classList.contains('is-open')) {
      closeDrawer();
      lastFocus?.focus?.();
      return;
    }
    if (event.key !== 'Tab' || !drawer.classList.contains('is-open')) return;
    const focusables = drawer.querySelectorAll('button, a[href], input, [tabindex]:not([tabindex="-1"])');
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });

  // Закрытие свайпом влево.
  let startX = null;
  drawer.addEventListener('pointerdown', (event) => { startX = event.clientX; });
  drawer.addEventListener('pointerup', (event) => {
    if (startX !== null && startX - event.clientX > 60) closeDrawer();
    startX = null;
  });

  // На широких экранах drawer не нужен — закрываем при возврате к десктопу.
  window.addEventListener('resize', () => { if (window.innerWidth > 760) closeDrawer(); });
}

/* --- Лента анонсов -------------------------------------------------------
   Категории: расписание, замены, событие, общая информация. Записи о графике и
   заменах создаёт сервер при сохранении, остальные публикует наставник.
   «Колокольчик» — набор категорий, которые считаются в счётчике непрочитанного. */

/* Фильтр по умолчанию — «Важное»: расписание, события и общая информация
   без замен, чтобы главная не тонула в рутине. */
const IMPORTANT_FILTER = 'important';
const announcementsState = {
  items: [],
  unread: { total: 0, by_category: {}, enabled: [] },
  categories: [],
  canPublish: false,
  filter: IMPORTANT_FILTER,
};

/* Слайд-шоу на главной: один анонс за раз. */
const slideshow = { index: 0, timer: null, items: [], paused: false };

function announcementCategory(code) {
  return (announcementsState.categories || []).find((item) => item.code === code) || null;
}

function announcementCategoryLabel(code) {
  const found = announcementCategory(code);
  return found ? found.label : code;
}

function announcementTime(iso) {
  const date = iso ? new Date(iso) : null;
  if (!date || Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('ru-RU', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function announcementMarkup(item, options = {}) {
  const category = announcementCategory(item.category);
  const color = (category && category.color) || '#7b8794';
  const snapshots = ((item.payload || {}).snapshots || []).slice(0, 6);
  const gallery = snapshots.length
    ? `<div class="announcement-gallery">${snapshots.map((url) => `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer"><img src="${escapeHtml(url)}" alt="" loading="lazy"></a>`).join('')}</div>`
    : '';
  const when = item.start_at
    ? `<span class="announcement-when"><svg class="icon" aria-hidden="true"><use href="#i-calendar"></use></svg>${escapeHtml(announcementTime(item.start_at))}</span>`
    : '';
  const pin = item.pinned
    ? '<span class="announcement-pin" title="Закреплено"><svg class="icon" aria-hidden="true"><use href="#i-pin"></use></svg></span>'
    : '';
  const actions = options.manage && announcementsState.canPublish
    ? `<div class="announcement-actions">
         <button class="text-button" type="button" data-announcement-pin="${item.id}" data-pinned="${item.pinned ? 'true' : 'false'}">${item.pinned ? 'Открепить' : 'Закрепить'}</button>
         <button class="text-button" type="button" data-announcement-edit="${item.id}">Изменить</button>
         <button class="text-button" type="button" data-announcement-delete="${item.id}">Удалить</button>
       </div>`
    : '';
  const meta = `${escapeHtml(announcementTime(item.created_at))}${item.actor ? ` · ${escapeHtml(item.actor)}` : ''}`;
  return `
    <article class="announcement ${item.is_read ? '' : 'is-unread'} ${item.pinned ? 'is-pinned' : ''}"
             style="--announcement-color: ${escapeHtml(color)}"
             data-announcement-id="${item.id}" data-category="${escapeHtml(item.category)}">
      <header class="announcement-top">${pin}<span class="announcement-chip">${escapeHtml(announcementCategoryLabel(item.category))}</span><h4 class="announcement-title">${escapeHtml(item.title)}</h4>${when}<span class="announcement-meta">${meta}</span></header>
      ${item.body ? `<p class="announcement-body">${escapeHtml(item.body)}</p>` : ''}
      ${gallery}
      <footer class="announcement-foot"><span class="announcement-meta announcement-meta-mobile">${meta}</span>${actions}</footer>
    </article>`;
}

function visibleAnnouncements(limit) {
  const filter = announcementsState.filter;
  const items = filter === 'all'
    ? announcementsState.items
    : filter === IMPORTANT_FILTER
      ? announcementsState.items.filter((item) => item.important)
      : announcementsState.items.filter((item) => item.category === filter);
  return limit ? items.slice(0, limit) : items;
}

function renderAnnouncementBadge() {
  const total = Number((announcementsState.unread || {}).total || 0);
  document.querySelectorAll('.nav-badge').forEach((badge) => {
    badge.hidden = total === 0;
    badge.textContent = total > 99 ? '99+' : String(total);
  });
  const label = document.querySelector('#announcements-unread-label');
  if (label) label.textContent = total ? `Непрочитанных: ${total}` : 'Всё прочитано';
  const enabled = (announcementsState.unread || {}).enabled || [];
  document.querySelectorAll('[data-ann-bell]').forEach((button) => {
    const on = enabled.includes(button.dataset.annBell);
    button.classList.toggle('is-selected', on);
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

function renderAnnouncementFilters() {
  const chips = [`<button class="chip ${announcementsState.filter === IMPORTANT_FILTER ? 'is-selected' : ''}" type="button" data-ann-filter="${IMPORTANT_FILTER}">Важное</button>`,
                 `<button class="chip ${announcementsState.filter === 'all' ? 'is-selected' : ''}" type="button" data-ann-filter="all">Все</button>`];
  for (const item of announcementsState.categories || []) {
    chips.push(`<button class="chip ${announcementsState.filter === item.code ? 'is-selected' : ''}" type="button" data-ann-filter="${escapeHtml(item.code)}">${escapeHtml(item.label)}</button>`);
  }
  const markup = chips.join('');
  for (const selector of ['#announcements-preview-filter', '#announcements-filters']) {
    const target = document.querySelector(selector);
    if (target) target.innerHTML = markup;
  }
}

function renderAnnouncementBell() {
  const target = document.querySelector('#announcements-bell');
  if (!target) return;
  const enabled = (announcementsState.unread || {}).enabled || [];
  target.innerHTML = (announcementsState.categories || []).map((item) => `
    <button class="chip chip-toggle ${enabled.includes(item.code) ? 'is-selected' : ''}" type="button"
            data-ann-bell="${escapeHtml(item.code)}" aria-pressed="${enabled.includes(item.code) ? 'true' : 'false'}">
      <svg class="icon" aria-hidden="true"><use href="#i-bell"></use></svg>${escapeHtml(item.label)}
    </button>`).join('');
}

/* Закреплённые держим всегда, остальное — за последнюю неделю. */
function slideshowItems() {
  const weekAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
  return announcementsState.items.filter((item) => {
    if (item.pinned) return true;
    const created = item.created_at ? new Date(item.created_at).getTime() : 0;
    return created >= weekAgo;
  });
}

function stopSlideshow() {
  if (slideshow.timer) { clearInterval(slideshow.timer); slideshow.timer = null; }
}

function startSlideshow() {
  stopSlideshow();
  if (slideshow.paused || slideshow.items.length < 2) return;
  slideshow.timer = setInterval(() => shiftSlide(1), 5000);
}

function shiftSlide(step) {
  if (!slideshow.items.length) return;
  slideshow.index = (slideshow.index + step + slideshow.items.length) % slideshow.items.length;
  renderAnnouncementsPreview();
}

function renderAnnouncementsPreview() {
  const node = document.querySelector('#announcements-preview-list');
  if (!node) return;
  slideshow.items = slideshowItems();
  if (!slideshow.items.length) {
    node.innerHTML = '<p class="empty-state">Пока пусто</p>';
    stopSlideshow();
    return;
  }
  if (slideshow.index >= slideshow.items.length) slideshow.index = 0;
  const total = slideshow.items.length;
  const nav = total > 1
    ? `<div class="slideshow-nav"><button class="icon-button" type="button" data-slide-step="-1" aria-label="Предыдущий анонс"><svg class="icon" aria-hidden="true"><use href="#i-chevron-left"></use></svg></button><span class="slideshow-count">${slideshow.index + 1} / ${total}</span><button class="icon-button" type="button" data-slide-step="1" aria-label="Следующий анонс"><svg class="icon" aria-hidden="true"><use href="#i-chevron-right"></use></svg></button></div>${'<div class="slideshow-dots">' + slideshow.items.map((item, index) => `<button type="button" class="slideshow-dot ${index === slideshow.index ? 'is-active' : ''}" data-slide="${index}" aria-label="Анонс ${index + 1}"></button>`).join('') + '</div>'}`
    : '';
  node.innerHTML = `<div class="slideshow">${announcementMarkup(slideshow.items[slideshow.index])}${nav}</div>`;
  startSlideshow();
}

function renderAnnouncements() {
  const list = document.querySelector('#announcements-list');
  if (list) {
    const items = visibleAnnouncements(0);
    list.innerHTML = items.length
      ? items.map((item) => announcementMarkup(item, { manage: true })).join('')
      : '<p class="empty-state">Анонсов пока нет</p>';
  }
  renderAnnouncementFilters();
  renderAnnouncementBell();
  renderAnnouncementsPreview();
  renderAnnouncementBadge();
}

function renderAnnouncementCategories() {
  const node = document.querySelector('#announcements-categories');
  if (!node) return;
  const categories = announcementsState.categories || [];
  if (!categories.length) {
    node.innerHTML = '<p class="empty-state">Категории не загружены</p>';
    return;
  }
  node.innerHTML = categories.map((item) => `
    <article class="announcement-category ${item.system ? 'is-system' : ''}">
      <span class="category-swatch" style="--announcement-color: ${escapeHtml(item.color)}"></span>
      <strong>${escapeHtml(item.label)}</strong>
      <span class="category-flags">${item.important ? 'важное' : 'вне «Важного»'}${item.system ? ' · системная' : ''}</span>
      ${item.system ? '' : `<div class="category-actions">
        <button class="text-button" type="button" data-category-edit="${escapeHtml(item.code)}">Изменить</button>
        <button class="text-button" type="button" data-category-delete="${escapeHtml(item.code)}">Удалить</button>
      </div>`}
    </article>`).join('');
}

function openAnnouncementDialog(item = null) {
  const dialog = document.querySelector('#announcement-dialog');
  const form = document.querySelector('#announcement-form');
  if (!dialog || !form) return;
  const errorNode = document.querySelector('#announcement-form-error');
  form.reset();
  form.dataset.announcementId = item ? String(item.id) : '';
  form.elements.category.innerHTML = (announcementsState.categories || [])
    .map((entry) => `<option value="${escapeHtml(entry.code)}">${escapeHtml(entry.label)}</option>`).join('');
  const kicker = document.querySelector('#announcement-dialog-kicker');
  const title = document.querySelector('#announcement-dialog-title');
  const submit = document.querySelector('#announcement-submit');
  if (kicker) kicker.textContent = item ? 'ПРАВКА АНОНСА' : 'НОВОЕ ОБЪЯВЛЕНИЕ';
  if (title) title.textContent = item ? 'Изменить анонс' : 'Анонс';
  if (submit) submit.textContent = item ? 'Сохранить' : 'Опубликовать';
  if (item) {
    form.elements.category.value = item.category;
    form.elements.title.value = item.title;
    form.elements.body.value = item.body || '';
    form.elements.start_at.value = item.start_at ? item.start_at.slice(0, 16) : '';
    form.elements.pinned.checked = Boolean(item.pinned);
  } else {
    form.elements.category.value = (announcementsState.categories.find((entry) => entry.code === 'event') || {}).code || '';
  }
  if (errorNode) errorNode.hidden = true;
  dialog.showModal();
}

function openCategoryDialog(item = null) {
  const dialog = document.querySelector('#announcement-category-dialog');
  const form = document.querySelector('#announcement-category-form');
  if (!dialog || !form) return;
  const errorNode = document.querySelector('#announcement-category-error');
  form.reset();
  form.dataset.code = item ? item.code : '';
  const title = document.querySelector('#announcement-category-title');
  const submit = document.querySelector('#announcement-category-submit');
  if (title) title.textContent = item ? 'Изменить категорию' : 'Новая категория';
  if (submit) submit.textContent = item ? 'Сохранить' : 'Создать';
  form.elements.color.value = item ? item.color : '#7b8794';
  form.elements.important.checked = item ? Boolean(item.important) : true;
  if (item) form.elements.label.value = item.label;
  if (errorNode) errorNode.hidden = true;
  dialog.showModal();
}

/* Клик по карточке открывает анонс в разделе «Анонсы» и подсвечивает его. */
async function openAnnouncementCard(id) {
  announcementsState.filter = 'all';
  setView('announcements');
  await openAnnouncements();
  const target = document.querySelector(`[data-announcement-id="${id}"]`);
  if (!target) return;
  target.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'center' });
  target.classList.add('is-highlighted');
  setTimeout(() => target.classList.remove('is-highlighted'), 2200);
}

async function loadAnnouncements() {
  try {
    const data = await api('/api/announcements?limit=50');
    announcementsState.items = data.items || [];
    announcementsState.unread = data.unread || announcementsState.unread;
    announcementsState.categories = data.categories || [];
    announcementsState.canPublish = Boolean(data.can_publish);
    renderAnnouncements();
    renderAnnouncementCategories();
  } catch (error) { showToast(error.message); }
}

async function markAnnouncementsRead() {
  try {
    const data = await api('/api/announcements/read', { method: 'POST', body: JSON.stringify({}) });
    if (data.unread) { announcementsState.unread = data.unread; renderAnnouncementBadge(); }
  } catch (error) { showToast(error.message); }
}

/* --- Push на устройстве -------------------------------------------------
   Ключ VAPID берём у сервера, подписку отдаём туда же. На телефоне уведомления
   приходят только в установленное приложение (iOS 16.4+), поэтому про это честно
   написано в интерфейсе. */

const pushState = { key: '', supported: false, subscribed: false, devices: 0 };

/* Сайт открыт как установленное приложение (standalone), а не во вкладке браузера.
   Та же проверка, что в static/pwa-install.js — баннер установки показывается
   ровно наоборот, поэтому они не пересекаются. */
function isStandaloneMode() {
  return window.matchMedia('(display-mode: standalone)').matches
    || window.matchMedia('(display-mode: fullscreen)').matches
    || window.matchMedia('(display-mode: minimal-ui)').matches
    || navigator.standalone === true;
}

function pushPermissionGranted() {
  return typeof Notification !== 'undefined' && Notification.permission === 'granted';
}

function setPushStatus(message) {
  const node = document.querySelector('#push-status');
  if (node) node.textContent = message;
}

function urlBase64ToUint8Array(base64) {
  const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4);
  const raw = atob(padded.replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from([...raw].map((character) => character.charCodeAt(0)));
}

/* navigator.serviceWorker.ready не разрешается, если воркер не зарегистрирован
   (например, во встроенном браузере) — поэтому с ограничением по времени. */
async function pushRegistration() {
  if (!('serviceWorker' in navigator)) return null;
  return Promise.race([
    navigator.serviceWorker.ready,
    new Promise((resolve) => setTimeout(() => resolve(null), 2500)),
  ]);
}

function renderPushState() {
  const enableButton = document.querySelector('#push-enable');
  const disableButton = document.querySelector('#push-disable');
  const testButton = document.querySelector('#push-test');
  const granted = pushPermissionGranted();
  if (pushState.subscribed) {
    setPushStatus(pushState.devices > 1
      ? `Уведомления включены · устройств: ${pushState.devices}`
      : 'Уведомления включены на этом устройстве');
  } else if (!pushState.supported) {
    setPushStatus('Этот браузер не поддерживает уведомления');
  } else {
    setPushStatus('Уведомления на этом устройстве выключены');
  }
  // «Включить» держим в панели анонсов, только пока разрешение ещё не выдано.
  if (enableButton) enableButton.hidden = pushState.subscribed || !pushState.supported || granted;
  if (disableButton) disableButton.hidden = !pushState.subscribed;
  if (testButton) testButton.hidden = !pushState.subscribed;
  renderPushEntryPoints();
}

async function refreshPushState() {
  try {
    const data = await api('/api/push/key');
    pushState.key = data.key || '';
    pushState.devices = Number(data.devices || 0);
  } catch (error) {
    setPushStatus(error.message);
    return;
  }
  pushState.supported = window.isSecureContext && 'serviceWorker' in navigator && 'PushManager' in window;
  const registration = pushState.supported ? await pushRegistration() : null;
  if (!registration) pushState.supported = false;
  try {
    const subscription = registration ? await registration.pushManager.getSubscription() : null;
    pushState.subscribed = Boolean(subscription);
  } catch (error) {
    pushState.subscribed = false;
  }
  renderPushState();
}

async function enablePush() {
  if (!pushState.supported) { setPushStatus('Этот браузер не поддерживает уведомления'); return; }
  try {
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') { setPushStatus('Разрешение на уведомления не выдано'); return; }
    const registration = await pushRegistration();
    if (!registration) { setPushStatus('Служебный воркер не готов — обновите страницу'); return; }
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(pushState.key),
    });
    await api('/api/push/subscribe', { method: 'POST', body: JSON.stringify(subscription.toJSON()) });
    await refreshPushState();
    showToast('Уведомления включены на этом устройстве');
  } catch (error) {
    setPushStatus(error.message);
  }
}

async function disablePush() {
  try {
    const registration = await pushRegistration();
    const subscription = registration ? await registration.pushManager.getSubscription() : null;
    if (subscription) {
      await api('/api/push/subscribe', { method: 'DELETE', body: JSON.stringify({ endpoint: subscription.endpoint }) });
      await subscription.unsubscribe();
    }
    await refreshPushState();
    showToast('Уведомления на этом устройстве отключены');
  } catch (error) {
    setPushStatus(error.message);
  }
}

/* --- PWA-баннер и кнопка в профиле: включение уведомлений -----------------
   Показываем только когда сайт запущен как приложение (standalone) и пока
   уведомления не включены. С баннером установки не пересекается: тот, наоборот,
   предлагает установить приложение ВНЕ standalone. */

const PUSH_PROMPT_KEY = 'neft-push-prompt-dismissed';

function pushPromptDismissed() {
  try { return localStorage.getItem(PUSH_PROMPT_KEY) === '1'; } catch (error) { return false; }
}

function hidePushPrompt() {
  const prompt = document.querySelector('#push-prompt');
  if (prompt) prompt.hidden = true;
}

function dismissPushPrompt() {
  // «Позже» помним: баннер показываем один раз, дальше — кнопка в анонсах и профиле.
  try { localStorage.setItem(PUSH_PROMPT_KEY, '1'); } catch (error) { /* приватный режим */ }
  hidePushPrompt();
}

function renderPushEntryPoints() {
  const standalone = isStandaloneMode();
  const granted = pushPermissionGranted();
  const enabled = pushState.subscribed;
  const ready = standalone && pushState.supported;

  const panel = document.querySelector('#profile-push-panel');
  const enableProfile = document.querySelector('#push-enable-profile');
  const disableProfile = document.querySelector('#push-disable-profile');
  const statusProfile = document.querySelector('#push-status-profile');
  if (panel) panel.hidden = !ready;
  if (enableProfile) enableProfile.hidden = enabled || granted;
  if (disableProfile) disableProfile.hidden = !enabled;
  if (statusProfile) {
    statusProfile.textContent = enabled
      ? (pushState.devices > 1
        ? `Уведомления включены · устройств: ${pushState.devices}`
        : 'Уведомления включены на этом устройстве')
      : 'Уведомления на этом устройстве выключены';
  }

  const prompt = document.querySelector('#push-prompt');
  if (!prompt) return;
  // Не показываем поверх баннера установки (страховка на случай странной детекции режима).
  const installBanner = document.querySelector('#pwa-install');
  const installVisible = Boolean(installBanner && !installBanner.hidden);
  prompt.hidden = !(ready && !enabled && !granted && !pushPromptDismissed() && !installVisible);
}

/* Ссылка из уведомления: открываем нужный раздел и — если указан — анонс. */
function applyDeepLink(url) {
  try {
    const params = new URL(url, window.location.origin).searchParams;
    const announcement = params.get('announcement');
    const view = params.get('view');
    if (announcement) openAnnouncementCard(Number(announcement));
    else if (view) setView(view);
    else return;
    // Убираем служебные параметры из адреса, чтобы они не тянулись за навигацией.
    window.history.replaceState({}, '', window.location.pathname);
  } catch (error) { /* некорректная ссылка — просто остаёмся на месте */ }
}

/* Открытие раздела = «я посмотрел ленту»: снимаем счётчик, но показываем
   непрочитанное в самом списке, чтобы не терять контекст. */
async function openAnnouncements() {
  await loadAnnouncements();
  if (Number((announcementsState.unread || {}).total || 0) > 0) await markAnnouncementsRead();
}

function setupAnnouncements() {
  document.addEventListener('click', async (event) => {
    const step = event.target.closest('[data-slide-step]');
    if (step) { shiftSlide(Number(step.dataset.slideStep) || 1); return; }

    const dot = event.target.closest('[data-slide]');
    if (dot) { slideshow.index = Number(dot.dataset.slide) || 0; renderAnnouncementsPreview(); return; }

    const filter = event.target.closest('[data-ann-filter]');
    if (filter) {
      announcementsState.filter = filter.dataset.annFilter;
      renderAnnouncements();
      return;
    }

    const bell = event.target.closest('[data-ann-bell]');
    if (bell) {
      const enabled = new Set((announcementsState.unread || {}).enabled || []);
      const name = bell.dataset.annBell;
      if (enabled.has(name)) enabled.delete(name); else enabled.add(name);
      try {
        const data = await api('/api/announcements/settings', { method: 'PATCH', body: JSON.stringify({ enabled: [...enabled] }) });
        if (data.unread) { announcementsState.unread = data.unread; renderAnnouncementBell(); renderAnnouncementBadge(); }
      } catch (error) { showToast(error.message); }
      return;
    }

    const pin = event.target.closest('[data-announcement-pin]');
    if (pin) {
      const next = pin.dataset.pinned !== 'true';
      try {
        await api(`/api/announcements/${pin.dataset.announcementPin}`, { method: 'PATCH', body: JSON.stringify({ pinned: next }) });
        await loadAnnouncements();
        showToast(next ? 'Анонс закреплён' : 'Анонс откреплён');
      } catch (error) { showToast(error.message); }
      return;
    }

    const edit = event.target.closest('[data-announcement-edit]');
    if (edit) {
      const item = announcementsState.items.find((entry) => String(entry.id) === edit.dataset.announcementEdit);
      if (item) openAnnouncementDialog(item);
      return;
    }

    const remove = event.target.closest('[data-announcement-delete]');
    if (remove) {
      try {
        await api(`/api/announcements/${remove.dataset.announcementDelete}`, { method: 'DELETE' });
        await loadAnnouncements();
        showToast('Анонс удалён');
      } catch (error) { showToast(error.message); }
      return;
    }

    const categoryEdit = event.target.closest('[data-category-edit]');
    if (categoryEdit) {
      const item = (announcementsState.categories || []).find((entry) => entry.code === categoryEdit.dataset.categoryEdit);
      if (item) openCategoryDialog(item);
      return;
    }

    const categoryDelete = event.target.closest('[data-category-delete]');
    if (categoryDelete) {
      try {
        await api(`/api/announcements/categories/${categoryDelete.dataset.categoryDelete}`, { method: 'DELETE' });
        await loadAnnouncements();
        showToast('Категория удалена');
      } catch (error) { showToast(error.message); }
      return;
    }

    if (event.target.closest('#announcements-read-all')) { await markAnnouncementsRead(); return; }

    // Важно: проверяем последним — кнопки внутри карточки уже обработаны выше.
    const card = event.target.closest('[data-announcement-id]');
    if (card) openAnnouncementCard(Number(card.dataset.announcementId));
  });

  const dialog = document.querySelector('#announcement-dialog');
  const form = document.querySelector('#announcement-form');
  const errorNode = document.querySelector('#announcement-form-error');
  document.querySelector('#announcements-add')?.addEventListener('click', () => openAnnouncementDialog(null));
  form?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const current = event.currentTarget;
    const payload = Object.fromEntries(new FormData(current).entries());
    payload.pinned = Boolean(current.elements.pinned?.checked);
    const id = current.dataset.announcementId;
    try {
      if (id) await api(`/api/announcements/${id}`, { method: 'PATCH', body: JSON.stringify(payload) });
      else await api('/api/announcements', { method: 'POST', body: JSON.stringify(payload) });
      dialog?.close();
      await loadAnnouncements();
      showToast(id ? 'Анонс обновлён' : 'Анонс опубликован');
    } catch (error) {
      if (errorNode) { errorNode.textContent = error.message; errorNode.hidden = false; }
    }
  });
  document.querySelectorAll('#announcement-dialog .dialog-close').forEach((button) => button.addEventListener('click', () => dialog?.close()));
  dialog?.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });

  const categoryDialog = document.querySelector('#announcement-category-dialog');
  const categoryForm = document.querySelector('#announcement-category-form');
  const categoryError = document.querySelector('#announcement-category-error');
  document.querySelector('#announcements-category-add')?.addEventListener('click', () => openCategoryDialog(null));
  categoryForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const current = event.currentTarget;
    const payload = Object.fromEntries(new FormData(current).entries());
    payload.important = Boolean(current.elements.important?.checked);
    const code = current.dataset.code;
    try {
      if (code) await api(`/api/announcements/categories/${code}`, { method: 'PATCH', body: JSON.stringify(payload) });
      else await api('/api/announcements/categories', { method: 'POST', body: JSON.stringify(payload) });
      categoryDialog?.close();
      await loadAnnouncements();
      showToast(code ? 'Категория обновлена' : 'Категория создана');
    } catch (error) {
      if (categoryError) { categoryError.textContent = error.message; categoryError.hidden = false; }
    }
  });
  document.querySelectorAll('#announcement-category-dialog .dialog-close').forEach((button) => button.addEventListener('click', () => categoryDialog?.close()));
  categoryDialog?.addEventListener('click', (event) => { if (event.target === categoryDialog) categoryDialog.close(); });

  // Свайп по слайд-шоу и пауза, пока вкладка не на экране.
  const preview = document.querySelector('#announcements-preview-list');
  if (preview) {
    let startX = null;
    preview.addEventListener('pointerdown', (event) => { startX = event.clientX; slideshow.paused = true; stopSlideshow(); });
    preview.addEventListener('pointerup', (event) => {
      if (startX !== null && Math.abs(event.clientX - startX) > 40) shiftSlide(event.clientX < startX ? 1 : -1);
      startX = null;
      slideshow.paused = false;
      startSlideshow();
    });
    preview.addEventListener('pointercancel', () => { startX = null; slideshow.paused = false; startSlideshow(); });
  }
  document.addEventListener('visibilitychange', () => {
    slideshow.paused = document.hidden;
    if (document.hidden) stopSlideshow(); else startSlideshow();
  });

  // Календарь анонсов: отдельная ссылка от личного календаря смен.
  const feedDialog = document.querySelector('#announcements-feed-dialog');
  document.querySelector('#announcements-subscribe')?.addEventListener('click', async () => {
    try {
      const links = await api('/api/announcements/feed-link');
      const open = document.querySelector('#announcements-webcal-open');
      if (open) open.href = links.webcal_url;
      const copy = document.querySelector('#announcements-feed-copy');
      if (copy) copy.dataset.url = links.https_url;
      const status = document.querySelector('#announcements-feed-status');
      if (status) status.textContent = '';
      feedDialog?.showModal();
    } catch (error) { showToast(error.message); }
  });
  document.querySelector('#announcements-feed-copy')?.addEventListener('click', async (event) => {
    const status = document.querySelector('#announcements-feed-status');
    try {
      await navigator.clipboard.writeText(event.currentTarget.dataset.url);
      if (status) status.textContent = 'Ссылка скопирована';
    } catch { if (status) status.textContent = event.currentTarget.dataset.url || ''; }
  });
  document.querySelectorAll('#announcements-feed-dialog .dialog-close').forEach((button) => button.addEventListener('click', () => feedDialog?.close()));
  feedDialog?.addEventListener('click', (event) => { if (event.target === feedDialog) feedDialog.close(); });

  document.querySelector('#push-enable')?.addEventListener('click', enablePush);
  document.querySelector('#push-disable')?.addEventListener('click', disablePush);
  document.querySelector('#push-test')?.addEventListener('click', async () => {
    setPushStatus('Отправляем…');
    try {
      const result = await api('/api/push/test', { method: 'POST' });
      setPushStatus(`Уведомление отправлено на устройств: ${result.delivered}`);
    } catch (error) { setPushStatus(error.message); }
  });
  // Профиль и PWA-баннер про уведомления (показываются только в standalone).
  document.querySelector('#push-enable-profile')?.addEventListener('click', enablePush);
  document.querySelector('#push-disable-profile')?.addEventListener('click', disablePush);
  document.querySelector('#push-prompt-enable')?.addEventListener('click', enablePush);
  document.querySelector('#push-prompt-later')?.addEventListener('click', dismissPushPrompt);
  document.querySelector('#push-prompt-close')?.addEventListener('click', dismissPushPrompt);
  document.querySelector('#push-prompt')?.addEventListener('click', (event) => {
    if (event.target === event.currentTarget) dismissPushPrompt();
  });
  document.addEventListener('keydown', (event) => {
    const prompt = document.querySelector('#push-prompt');
    if (event.key === 'Escape' && prompt && !prompt.hidden) dismissPushPrompt();
  });
  refreshPushState();
}

function setupDashboard() {
  document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => setView(button.dataset.view)));
  setupAppearance();
  setupDrawer();
  setupProfile();
  setupAnnouncements();
  loadAnnouncements();
  document.querySelectorAll('[data-period]').forEach((button) => button.addEventListener('click', () => {
    document.querySelectorAll('[data-period]').forEach((item) => item.classList.toggle('is-selected', item === button));
    loadDashboard(button.dataset.period);
  }));

  const logout = async () => {
    try { await api('/api/logout', { method: 'POST' }); window.location.reload(); }
    catch (error) { showToast(error.message); }
  };
  document.querySelector('#logout-button')?.addEventListener('click', logout);
  document.querySelector('#drawer-logout')?.addEventListener('click', logout);

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
    const rotateButton = event.target.closest('[data-rotate-code]');
    if (rotateButton) {
      const row = rotateButton.closest('[data-user-id]');
      try {
        const result = await api(`/api/users/${row.dataset.userId}`, { method: 'PATCH', body: JSON.stringify({ regenerate_code: true }) });
        await loadUsers();
        showToast(`Новый код доступа: ${result.code}`);
      } catch (error) { showToast(error.message); }
      return;
    }
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

  calendarCursor = new Date();
  const storedZoom = Number(localStorage.getItem(MATRIX_ZOOM_STORAGE));
  matrixDayWidth = Number.isFinite(storedZoom) && storedZoom > 0 ? clampDayWidth(storedZoom) : null;
  document.querySelector('#month-previous')?.addEventListener('click', () => {
    calendarCursor = new Date(calendarCursor.getFullYear(), calendarCursor.getMonth() - 1, 1);
    loadCalendar();
  });
  document.querySelector('#month-next')?.addEventListener('click', () => {
    calendarCursor = new Date(calendarCursor.getFullYear(), calendarCursor.getMonth() + 1, 1);
    loadCalendar();
  });
  const startEditing = (type) => {
    if (editMode) return;
    editType = type;
    editMode = true;
    matrixAnchor = null; matrixExtent = null;
    matrixMode = 'edit';
    document.querySelectorAll('[data-matrix-mode]').forEach((button) => button.classList.toggle('is-selected', button.dataset.matrixMode === matrixMode));
    renderCalendar(calendarState);
    updateEditControls();
    const status = document.querySelector('#matrix-status');
    if (status) status.textContent = type === 'schedule'
      ? 'Расписание: «Сохранить» — тихо, «Опубликовать» — снимки за период'
      : 'Замены: изменения пока не сохранены';
  };
  document.querySelector('#edit-swaps-toggle')?.addEventListener('click', () => startEditing('swap'));
  document.querySelector('#edit-schedule-toggle')?.addEventListener('click', () => startEditing('schedule'));
  document.querySelector('#matrix-save')?.addEventListener('click', () => saveScheduleChanges(true));
  document.querySelector('#matrix-save-draft')?.addEventListener('click', () => saveScheduleChanges(false));
  document.querySelector('#matrix-publish')?.addEventListener('click', openPublishDialog);
  document.querySelectorAll('[data-publish-preset]').forEach((button) => button.addEventListener('click', () => {
    const form = document.querySelector('#publish-form');
    if (!form) return;
    const bounds = publishMonthBounds();
    const second = button.dataset.publishPreset === 'second';
    form.elements.publish_start.value = second ? bounds.middle : bounds.first;
    form.elements.publish_end.value = second ? bounds.last : bounds.middle;
    updatePublishPreview();
  }));
  document.querySelector('#publish-form')?.addEventListener('input', updatePublishPreview);
  document.querySelector('#publish-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const windows = updatePublishPreview();
    if (!windows.length) return;
    const period = { start: form.elements.publish_start.value, end: form.elements.publish_end.value };
    form.closest('dialog')?.close();
    await saveScheduleChanges(true, period);
  });
  document.querySelector('#matrix-discard')?.addEventListener('click', discardScheduleChanges);
  const matrix = document.querySelector('#calendar-grid');
  matrix?.addEventListener('pointerdown', (event) => {
    if (!editMode || matrixMode !== 'select') return;
    const cell = event.target.closest('[data-matrix-cell]');
    if (!cell) return;
    const point = { row: Number(cell.dataset.matrixRow), col: Number(cell.dataset.matrixCol) };
    if (!event.shiftKey || !matrixAnchor) matrixAnchor = point;
    matrixExtent = point;
    matrixPointerDown = true;
    updateMatrixSelection();
    event.preventDefault();
  });
  matrix?.addEventListener('pointerover', (event) => {
    if (!matrixPointerDown || !editMode || matrixMode !== 'select') return;
    const cell = event.target.closest('[data-matrix-cell]');
    if (!cell) return;
    matrixExtent = { row: Number(cell.dataset.matrixRow), col: Number(cell.dataset.matrixCol) };
    updateMatrixSelection();
  });
  document.addEventListener('pointerup', () => { matrixPointerDown = false; });
  matrix?.addEventListener('click', (event) => {
    if (!editMode) return;
    const status = document.querySelector('#matrix-status');
    const cell = event.target.closest('[data-cell-date]');
    if (!cell) {
      // Клик по пустому месту матрицы снимает активный шаблон.
      if (activeTemplate) { activeTemplate = null; renderTemplateBar(); }
      return;
    }
    if (activeTemplate) {
      // В режиме «Диапазон» с выделением шаблон ложится на все выделенные ячейки.
      if (matrixMode === 'select' && matrixSelectionBounds()) {
        const filled = applyActiveTemplateToSelection();
        if (status) status.textContent = `Шаблон «${activeTemplate.name}»: заполнено ячеек — ${filled}`;
        return;
      }
      stageTemplate(activeTemplate, Number(cell.dataset.employeeId), cell.dataset.cellDate);
      if (status) status.textContent = `Шаблон «${activeTemplate.name}» · нажмите другую ячейку или Esc`;
      return;
    }
    if (matrixMode === 'select') return;
    const shift = getEffectiveShift(Number(cell.dataset.employeeId), cell.dataset.cellDate);
    openShiftDialog(cell.dataset.cellDate, shift || null, Number(cell.dataset.employeeId));
  });
  document.querySelectorAll('[data-matrix-mode]')?.forEach((button) => button.addEventListener('click', () => {
    matrixMode = button.dataset.matrixMode;
    document.querySelectorAll('[data-matrix-mode]').forEach((item) => item.classList.toggle('is-selected', item === button));
    matrixAnchor = null; matrixExtent = null; updateMatrixSelection();
    const status = document.querySelector('#matrix-status');
    if (status) status.textContent = matrixMode === 'select'
      ? 'Выделите диапазон, затем нажмите шаблон или ячейку'
      : 'Нажмите ячейку, чтобы изменить смену';
  }));
  document.querySelector('#matrix-copy')?.addEventListener('click', copyMatrixSelection);
  document.querySelector('#matrix-paste')?.addEventListener('click', pasteMatrixSelection);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && activeTemplate) {
      activeTemplate = null;
      renderTemplateBar();
      const status = document.querySelector('#matrix-status');
      if (status) status.textContent = 'Шаблон снят';
    }
  });
  document.querySelector('#matrix-zoom-in')?.addEventListener('click', () => setMatrixZoom(currentDayWidth() * 1.25));
  document.querySelector('#matrix-zoom-out')?.addEventListener('click', () => setMatrixZoom(currentDayWidth() / 1.25));
  document.querySelector('#matrix-zoom-fit')?.addEventListener('click', fitMatrixMonth);
  const scroll = matrixScroll();
  scroll?.addEventListener('wheel', (event) => {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    setMatrixZoom(currentDayWidth() * (event.deltaY < 0 ? 1.15 : 1 / 1.15));
  }, { passive: false });
  let pinchStartDistance = 0;
  let pinchStartDayWidth = 0;
  scroll?.addEventListener('touchstart', (event) => {
    if (event.touches.length !== 2) return;
    pinchStartDistance = touchDistance(event.touches);
    pinchStartDayWidth = currentDayWidth();
  }, { passive: true });
  scroll?.addEventListener('touchmove', (event) => {
    if (event.touches.length !== 2 || !pinchStartDistance) return;
    event.preventDefault();
    setMatrixZoom(pinchStartDayWidth * (touchDistance(event.touches) / pinchStartDistance), false);
  }, { passive: false });
  scroll?.addEventListener('touchend', (event) => {
    if (event.touches.length >= 2) return;
    pinchStartDistance = 0;
    if (matrixDayWidth) {
      try { localStorage.setItem(MATRIX_ZOOM_STORAGE, String(matrixDayWidth)); } catch (error) { /* приватный режим */ }
    }
  });
  if (window.ResizeObserver && scroll) new ResizeObserver(() => applyMatrixZoom()).observe(scroll);
  document.addEventListener('keydown', (event) => {
    if (!editMode || matrixMode !== 'select' || !(event.ctrlKey || event.metaKey)) return;
    if (event.target.closest('input, textarea, select, [contenteditable="true"]')) return;
    if (event.key.toLowerCase() === 'c') { event.preventDefault(); copyMatrixSelection(); }
    if (event.key.toLowerCase() === 'v') { event.preventDefault(); pasteMatrixSelection(); }
  });

  const shiftDialog = document.querySelector('#shift-dialog');
  const shiftForm = document.querySelector('#shift-form');
  shiftForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = Object.fromEntries(new FormData(form).entries());
    payload.user_id = Number(payload.user_id);
    setDraftCell(payload.user_id, payload.date, {
      start_time: payload.start_time,
      end_time: payload.end_time,
      point: payload.point,
    });
    shiftDialog.close();
    const status = document.querySelector('#matrix-status');
    if (status) status.textContent = 'Смена добавлена в черновик';
  });
  document.querySelector('#delete-shift-button')?.addEventListener('click', async () => {
    const userId = Number(shiftForm.elements.user_id.value);
    const date = shiftForm.elements.date.value;
    setDraftCell(userId, date, null);
    shiftDialog.close();
    const status = document.querySelector('#matrix-status');
    if (status) status.textContent = 'Ячейка отмечена как выходной';
  });

  const feedDialog = document.querySelector('#feed-dialog');
  document.querySelector('#calendar-subscribe')?.addEventListener('click', async () => {
    try {
      const links = await api('/api/calendar/feed-link');
      document.querySelector('#webcal-open').href = links.webcal_url;
      document.querySelector('#feed-copy').dataset.url = links.https_url;
      document.querySelector('#feed-status').textContent = '';
      feedDialog.showModal();
    } catch (error) { showToast(error.message); }
  });
  document.querySelector('#feed-copy')?.addEventListener('click', async (event) => {
    try {
      await navigator.clipboard.writeText(event.currentTarget.dataset.url);
      document.querySelector('#feed-status').textContent = 'Ссылка скопирована';
    } catch { document.querySelector('#feed-status').textContent = event.currentTarget.dataset.url; }
  });

  for (const [id, key] of [['quality-module-toggle', 'quality_enabled'], ['calendar-module-toggle', 'calendar_enabled']]) {
    document.querySelector(`#${id}`)?.addEventListener('change', async (event) => {
      const previous = modulePreferences[key];
      modulePreferences[key] = event.currentTarget.checked;
      applyModuleVisibility();
      try {
        modulePreferences = await api('/api/preferences', { method: 'PATCH', body: JSON.stringify({ [key]: modulePreferences[key] }) });
        if (key === 'calendar_enabled' && modulePreferences.calendar_enabled) loadCalendar();
        if (key === 'quality_enabled' && modulePreferences.quality_enabled) loadDashboard();
        showToast('Настройка сохранена');
      } catch (error) {
        modulePreferences[key] = previous;
        event.currentTarget.checked = previous;
        applyModuleVisibility();
        showToast(error.message);
      }
    });
  }
  document.querySelector('#barista-preview-toggle')?.addEventListener('change', (event) => {
    applyBaristaPreview(event.currentTarget.checked);
    localStorage.setItem('barista-preview', String(event.currentTarget.checked));
  });

  document.querySelectorAll('.dialog-close').forEach((button) => button.addEventListener('click', () => button.closest('dialog')?.close()));
  document.querySelectorAll('dialog').forEach((dialog) => dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); }));

  updateEditControls();
  loadPreferences().then(() => {
    if (modulePreferences.calendar_enabled) loadCalendar();
    if (modulePreferences.quality_enabled) loadDashboard();
  });
  loadShiftTemplates();
  if (managerRole) {
    setupStoriesAdmin();
    setupShiftTemplates();
    setupChannelSettings();
  }
}

/* Автовход после подтверждения в Telegram: страница входа сама опрашивает
   статус и перезагружается — без ручного «Обновить» (была гонка: человек
   возвращался раньше, чем бот успевал подтвердить заявку). */
function setupTelegramWait() {
  if (document.body.dataset.tgPending !== 'true') return;
  let attempts = 0;
  const poll = async () => {
    attempts += 1;
    try {
      const data = await api('/api/auth/telegram/status');
      if (data.status === 'approved' || data.status === 'denied') { window.location.reload(); return; }
      if (data.status === 'unknown') return;
    } catch (error) { /* сервер временно недоступен — просто ждём дальше */ }
    if (attempts < 200) setTimeout(poll, 3000);
  };
  setTimeout(poll, 2500);
}

/* Оболочка приложения в кеше телефона: сохранённое на домашний экран приложение
   открывается мгновенно, а сеть догоняет в фоне (см. static/sw.js). Версию
   берём из адреса самого app.js — при деплое она меняется, и старый кеш
   выбрасывается автоматически. */
function setupServiceWorker() {
  if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
  const script = Array.from(document.scripts).find((node) => node.src && node.src.includes('/static/app.js'));
  const version = script ? new URL(script.src).searchParams.get('v') || '' : '';
  navigator.serviceWorker.addEventListener('message', (event) => {
    // Сервер вернул другую страницу входа (сессия истекла или наоборот) —
    // кеш показал бы не тот экран, поэтому перезагружаемся.
    if (event.data === 'reload') window.location.reload();
    // Тап по push-уведомлению просит открыть раздел или конкретный анонс.
    if (event.data && event.data.type === 'navigate') applyDeepLink(event.data.url);
  });
  navigator.serviceWorker.register(`/sw.js?v=${version}`, { scope: '/' }).catch(() => {
    // Не получилось — сайт работает как раньше, просто без мгновенного старта.
  });
}

/* Мини-API для отдельных модулей (chat.js, touch.js): общие помощники в одном
   месте, чтобы не дублировать их в отдельных файлах. */
window.Neft = {
  api,
  showToast,
  escapeHtml,
  avatarMarkup,
  setView,
  closeDrawer,
  get user() {
    return {
      id: Number(document.body.dataset.userId || 0),
      name: document.body.dataset.userName || '',
      role: document.body.dataset.role || '',
      label: document.body.dataset.roleLabel || '',
      manager: document.body.dataset.manager === 'true',
    };
  },
};

setupServiceWorker();

if (document.body.dataset.authenticated === 'true') {
  setupDashboard();
  applyDeepLink(window.location.href);
} else setupTelegramWait();
