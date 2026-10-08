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
  const pendingCount = users.filter((user) => user.access_pending).length;
  count.textContent = `${activeCount} активных · ${pendingCount} ожидают · ${users.length} всего`;
  if (!users.length) { list.innerHTML = '<p class="empty-state">Сотрудников пока нет</p>'; return; }
  const roleNames = { barista: 'Бариста', senior: 'Старший', mentor: 'Наставник', guest: 'Ожидает доступа' };
  list.innerHTML = users.map((user) => `
    <article class="user-row ${user.is_active ? '' : 'is-inactive'} ${user.access_pending ? 'is-pending' : ''}" data-user-id="${user.id}">
      <div class="user-person"><span class="user-avatar">${escapeHtml((user.name || '?').slice(0, 1).toUpperCase())}</span><strong title="${escapeHtml(user.name)}">${escapeHtml(user.name)}</strong></div>
      <span class="user-detail user-iiko">${user.iiko_id ? `Iiko ${escapeHtml(user.iiko_id)}` : 'Iiko не указан'}</span>
      <span class="user-detail user-telegram">${user.telegram_username ? `@${escapeHtml(user.telegram_username)}` : 'Telegram не привязан'}</span>
      <span class="user-detail user-role">${roleNames[user.role] || escapeHtml(user.role)}</span>
      <div class="user-actions"><button class="user-edit" type="button" data-edit>Изменить</button><button class="user-toggle ${user.is_active ? 'is-active' : ''}" type="button" data-active="${user.is_active ? 'true' : 'false'}" ${user.access_pending ? 'disabled title="Сначала назначьте роль через редактирование"' : ''}>${user.is_active ? 'Активен' : user.access_pending ? 'Ожидает роль' : 'Выдать доступ'}</button></div>
    </article>`).join('');
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
  if (publishButton) { publishButton.hidden = editType !== 'schedule'; publishButton.disabled = !editMode || !hasChanges; }
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

function historyEntryHtml(row) {
  const actor = `${escapeHtml(row.actor_name)} · ${escapeHtml(String(row.created_at).slice(0, 16))}`;
  if (row.action === 'published' || row.change_type === 'schedule') {
    const period = row.period_start && row.period_end
      ? `${displayDate(row.period_start)} – ${displayDate(row.period_end, true)}`
      : displayDate(row.shift_date, true);
    const count = row.changes_count ?? 0;
    const shot = row.snapshot_path
      ? `<a class="history-shot" href="${escapeHtml(row.snapshot_path)}" target="_blank" rel="noopener"><img src="${escapeHtml(row.snapshot_path)}" alt="Снимок расписания" loading="lazy"></a>`
      : '<span class="history-shot-missing">снимок удалён по сроку хранения</span>';
    return `<article class="history-row is-schedule"><span class="history-change">Расписание · ${escapeHtml(period)} · правок: ${count}</span><span class="history-actor">${actor}</span>${shot}</article>`;
  }
  return `<article class="history-row"><span class="history-change">${escapeHtml(describeChange(row))}</span><span class="history-actor">${actor}</span></article>`;
}

async function saveScheduleChanges(publish = true) {
  if (!matrixChanges.size) return;
  const operations = [...matrixChanges.entries()].map(([key, value]) => {
    const [userId, date] = key.split('|');
    return value ? { user_id: Number(userId), date, ...value } : { user_id: Number(userId), date, delete: true };
  });
  const body = editType === 'schedule'
    ? { operations, change_type: 'schedule', publish }
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
    const savedText = result.change_type === 'schedule'
      ? (publish ? 'Расписание опубликовано' : 'Расписание сохранено')
      : `Сохранено ${result.logged} правок`;
    if (status) status.textContent = savedText;
    showToast(savedText);
  } catch (error) { showToast(error.message); }
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
  if (!modulePreferences.calendar_enabled && document.querySelector('#calendar-view')?.classList.contains('is-visible')) setView('settings');
}

function applyBaristaPreview(enabled) {
  baristaPreview = enabled;
  document.body.classList.toggle('barista-preview', enabled);
  document.querySelectorAll('[data-view="team"]').forEach((button) => { button.hidden = enabled; });
  const toggle = document.querySelector('#barista-preview-toggle');
  if (toggle) toggle.checked = enabled;
  const roleLabel = document.querySelector('.account-copy span');
  if (roleLabel) roleLabel.textContent = enabled ? 'Бариста · предпросмотр' : document.body.dataset.roleLabel;
  if (enabled && document.querySelector('#team-view')?.classList.contains('is-visible')) setView('overview');
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
    return `<tr><th class="matrix-employee sticky-column"><span class="matrix-avatar">${escapeHtml((employee.name || '?').slice(0, 1).toUpperCase())}</span><span class="matrix-employee-name" title="${escapeHtml(employee.name)}">${escapeHtml(employee.name)}</span></th>${cells}</tr>`;
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
  document.querySelector('#hours-total').textContent = stats.total_hours;
  document.querySelector('#hours-worked').textContent = stats.worked_hours;
  document.querySelector('#hours-remaining').textContent = stats.remaining_hours;
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

function setView(name) {
  if (name === 'calendar' && !modulePreferences.calendar_enabled) name = 'settings';
  document.querySelectorAll('[data-view]').forEach((button) => button.classList.toggle('is-active', button.dataset.view === name));
  document.querySelectorAll('.view').forEach((view) => {
    const active = view.id === `${name}-view`;
    view.classList.toggle('is-visible', active);
    view.hidden = !active;
  });
  const title = document.querySelector('#page-title');
  if (title) title.textContent = name === 'team' ? 'Управление командой' : name === 'calendar' ? 'График смен' : name === 'settings' ? 'Панель управления' : `Добрый день, ${title.dataset.name}`;
  document.body.classList.toggle('settings-active', name === 'settings');
  if (name === 'team') loadUsers();
  if (name === 'calendar' && modulePreferences.calendar_enabled) loadCalendar();
  if (name === 'settings' && managerRole) loadStoriesAdmin();
  window.scrollTo({ top: 0, behavior: 'smooth' });
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
      ? 'Расписание: «Сохранить» — тихо, «Опубликовать» — со снимком'
      : 'Замены: изменения пока не сохранены';
  };
  document.querySelector('#edit-swaps-toggle')?.addEventListener('click', () => startEditing('swap'));
  document.querySelector('#edit-schedule-toggle')?.addEventListener('click', () => startEditing('schedule'));
  document.querySelector('#matrix-save')?.addEventListener('click', () => saveScheduleChanges(true));
  document.querySelector('#matrix-save-draft')?.addEventListener('click', () => saveScheduleChanges(false));
  document.querySelector('#matrix-publish')?.addEventListener('click', () => saveScheduleChanges(true));
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

if (document.body.dataset.authenticated === 'true') setupDashboard();
