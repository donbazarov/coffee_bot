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
// Глобальное состояние модулей (вкл/выкл разделов) и выбранный модуль панели управления.
let modulesState = {};
const controlState = { current: null, treeOpen: true };
const managerRole = document.body.dataset.manager === 'true';
const userRole = document.body.dataset.role || '';
const isMentor = userRole === 'mentor';
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

/* --- Глобальный статус сети и UI-кеш для офлайн-режима ------------------ */

/* Индикатор в правом углу шапки: зелёный — связь есть, жёлтый — офлайн.
   `timestamp` — время последней успешной синхронизации, отображается как ОФЛАЙН (HH:MM). */
function setNetworkStatus(isOnline, timestamp = null) {
  const status = document.querySelector('#app-network-status');
  const text = document.querySelector('#app-network-text');
  if (!status) return;
  status.classList.toggle('is-offline', !isOnline);
  if (!text) return;
  if (isOnline) {
    text.textContent = 'СЕРВИС АКТИВЕН';
    return;
  }
  const offlineTime = timestamp instanceof Date ? timestamp : new Date(timestamp);
  const hasTime = timestamp != null && !Number.isNaN(offlineTime.getTime());
  text.textContent = hasTime
    ? `ОФЛАЙН (${String(offlineTime.getHours()).padStart(2, '0')}:${String(offlineTime.getMinutes()).padStart(2, '0')})`
    : 'ОФЛАЙН';
}

const CACHE_KEY_ANNOUNCEMENTS = 'neft-cache:announcements';
const CACHE_KEY_CALENDAR = 'neft-cache:calendar';
const CACHE_KEY_LAST_SYNC = 'neft-cache:last-sync';

function readCache(key) {
  try {
    const raw = localStorage.getItem(key);
    return raw == null ? null : JSON.parse(raw);
  } catch (error) {
    return null;
  }
}

function writeCache(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (error) {
    /* localStorage может быть недоступен (приватный режим) — кеш пропускаем. */
  }
}

function lastSyncTimestamp() {
  const stored = readCache(CACHE_KEY_LAST_SYNC);
  return stored == null ? null : new Date(stored);
}

/* Кастомная ошибка сети/серверной недоступности: не «ломает» UI, а лишь
   переводит приложение в офлайн-режим и сигнализирует об этом через флаг. */
function offlineError(message) {
  const error = new Error(message);
  error.isOffline = true;
  return error;
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
      if ((!options.method || options.method === 'GET') && !options.retried) {
        await delay(900);
        return api(path, { ...options, retried: true });
      }
      setNetworkStatus(false);
      throw offlineError('Связь с сервером потеряна');
    }
    if (response.status >= 500 && (!options.method || options.method === 'GET') && !options.retried) {
      await delay(900);
      return api(path, { ...options, retried: true });
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      // 5xx после повтора — сервер недоступен; 4xx — обычная ошибка бизнес-логики.
      if (response.status >= 500) {
        setNetworkStatus(false);
        throw offlineError(data.detail || 'Сервер временно недоступен');
      }
      throw new Error(data.detail || 'Не удалось выполнить запрос');
    }
    setNetworkStatus(true);
    return data;
  } finally {
    activeRequests = Math.max(0, activeRequests - 1);
    syncProgress();
  }
}

/* --- Развитие ---------------------------------------------------------- */

const BRANCH_ICONS = { sensory: '☕', milk: '🥛', standards: '📋', team: '🤝' };
let developmentState = null;
let evaluationPhotoUrl = null;
/* Достижения: 3D-витрина медали (model-viewer) и сетка наград. */
const ACHIEVEMENT_GOLD = '#d4af37';
// Тот же золотой в формате model-viewer ([r,g,b,a], 0..1) — на случай пустого
// texture_color в справочнике наград.
const ACHIEVEMENT_GOLD_RGBA = [0xd4 / 255, 0xaf / 255, 0x37 / 255, 1];
let achievementsState = [];
let selectedAchievementCode = null;
// Цвет, назначенный до события `load`: model.materials появляется только после
// загрузки .glb, поэтому держим его здесь и применяем в обработчике.
let pendingAchievementColor = null;
let achievementsBound = false;

function branchRow(branch) {
  const level = branch.level;
  const percent = Number(branch.progress) || 0;
  return `
    <article class="branch-card${branch.kind === 'social' ? ' is-social' : ''}">
      <div class="branch-head">
        <span class="branch-icon" aria-hidden="true">${BRANCH_ICONS[branch.code] || '◆'}</span>
        <div class="branch-copy">
          <strong>${escapeHtml(branch.name)}</strong>
          <span>${escapeHtml(branch.description)}</span>
        </div>
        <span class="branch-level">${level}<small>ур.</small></span>
      </div>
      <div class="branch-track"><i style="width:${Math.min(100, percent)}%"></i></div>
      <div class="branch-foot"><span>${Number(branch.xp)} XP</span><span>${branch.xp_to_next ? `до ${branch.level + 1} ур. — ${branch.xp_to_next}` : 'максимум'}</span></div>
    </article>`;
}

/* `#rgb`/`#rrggbb` из API → [r,g,b,a] для PBR-материала (пусто/битое → золото). */
function hexToRgba(value) {
  const match = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(value || '').trim());
  if (!match) return ACHIEVEMENT_GOLD_RGBA;
  const hex = match[1].length === 3
    ? match[1].replace(/./g, (symbol) => symbol + symbol)
    : match[1];
  return [
    parseInt(hex.slice(0, 2), 16) / 255,
    parseInt(hex.slice(2, 4), 16) / 255,
    parseInt(hex.slice(4, 6), 16) / 255,
    1,
  ];
}

/* Красит медаль в цвет награды. false — модель ещё грузится. */
function paintAchievementModel(rgba) {
  const materials = document.querySelector('#achievement-viewer')?.model?.materials;
  if (!materials || !materials.length) return false;
  materials.forEach((material) => {
    // PBR-API model-viewer: baseColorFactor умножается на текстуры модели,
    // поэтому один и тот же .glb перекрашивается без отдельных ассетов.
    material.pbrMetallicRoughness?.setBaseColorFactor?.(rgba);
  });
  return true;
}

function applyAchievementColor(color) {
  const rgba = hexToRgba(color);
  pendingAchievementColor = paintAchievementModel(rgba) ? null : rgba;
}

function achievementCard(achievement, isLocked) {
  const progress = achievement.progress || null;
  const percent = progress ? Math.max(2, Math.min(100, Number(progress.percent) || 0)) : 0;
  const state = isLocked
    ? '<span class="achievement-state"><svg class="icon" aria-hidden="true"><use href="#i-lock"></use></svg>НЕ ОТКРЫТА</span>'
    : '<span class="achievement-state"><svg class="icon" aria-hidden="true"><use href="#i-spark"></use></svg>ПОЛУЧЕНА</span>';
  return `
    <button type="button" class="achievement-card${isLocked ? ' is-locked' : ''}" data-code="${escapeHtml(achievement.code)}" aria-pressed="false" style="--card-color:${escapeHtml(achievement.texture_color || ACHIEVEMENT_GOLD)}">
      <span class="achievement-dot" aria-hidden="true"></span>
      <span class="achievement-name">${escapeHtml(achievement.name || achievement.code)}</span>
      ${state}
      ${progress ? `<span class="achievement-progress"><i style="width:${percent}%"></i></span>
      <span class="achievement-progress-label">${Number(progress.current)} / ${Number(progress.target)}</span>` : ''}
    </button>`;
}

/* Подпись под витриной: дата получения либо прогресс до цели. */
function achievementMeta(achievement, isLocked) {
  const progress = achievement.progress || null;
  if (isLocked) {
    if (!progress || !progress.target) return 'Цель награды пока не измеряется';
    return `Прогресс: ${Number(progress.current)} из ${Number(progress.target)} · ${Math.round(Number(progress.percent) || 0)}%`;
  }
  const date = String(achievement.unlocked_at || '').slice(0, 10);
  return date ? `Получена ${displayDate(date, true)}` : 'Получена';
}

function selectAchievement(achievement, isLocked) {
  if (!achievement) return;
  selectedAchievementCode = achievement.code;
  const title = document.querySelector('#achievement-title');
  const desc = document.querySelector('#achievement-desc');
  const meta = document.querySelector('#achievement-meta');
  if (title) title.textContent = achievement.name || 'Награда';
  if (desc) desc.textContent = achievement.description || 'Описание награды появится позже.';
  if (meta) {
    meta.textContent = achievementMeta(achievement, isLocked);
    meta.hidden = false;
  }
  document.querySelectorAll('#achievements-grid .achievement-card').forEach((card) => {
    const active = card.dataset.code === achievement.code;
    card.classList.toggle('is-active', active);
    card.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  applyAchievementColor(achievement.texture_color);
}

/* Полученные и заблокированные награды — один список для сетки. */
function renderAchievements(data) {
  const grid = document.querySelector('#achievements-grid');
  if (!grid) return;
  const unlocked = (data.achievements || []).map((achievement) => ({ ...achievement, locked: false }));
  const locked = (data.next_achievements || []).map((achievement) => ({ ...achievement, locked: true }));
  achievementsState = [...unlocked, ...locked];

  const counter = document.querySelector('#achievements-counter');
  if (counter) {
    counter.textContent = achievementsState.length ? `${unlocked.length} ИЗ ${achievementsState.length}` : '—';
  }
  grid.innerHTML = achievementsState.length
    ? achievementsState.map((achievement) => achievementCard(achievement, achievement.locked)).join('')
    : '<p class="empty-state">Награды появятся после первых оценок</p>';

  // Автовыбор при первом входе на вкладку: прошлая награда → первая полученная
  // → первая заблокированная (у стажёра полученных может не быть вовсе).
  const selected = achievementsState.find((achievement) => achievement.code === selectedAchievementCode)
    || unlocked[0] || locked[0] || null;
  if (selected) selectAchievement(selected, selected.locked);
}

/* Подписки вешаем один раз: renderDevelopment() вызывается при каждом входе
   на вкладку «Развитие». */
function setupAchievements() {
  if (achievementsBound) return;
  achievementsBound = true;
  const viewer = document.querySelector('#achievement-viewer');
  const hero = document.querySelector('#achievements-hero');
  const veil = document.querySelector('#achievement-veil');
  viewer?.addEventListener('load', () => {
    hero?.classList.add('is-ready');
    if (!pendingAchievementColor) return;
    const rgba = pendingAchievementColor;
    pendingAchievementColor = null;
    paintAchievementModel(rgba);
  });
  viewer?.addEventListener('error', () => {
    // Медаль не доехала: витрина остаётся декоративным плейсхолдером, а список
    // наград и тексты продолжают работать — раздел не выглядит сломанным.
    hero?.classList.remove('is-ready');
    if (veil) veil.textContent = '3D НЕДОСТУПНО';
  });
  document.querySelector('#achievements-grid')?.addEventListener('click', (event) => {
    const card = event.target.closest('.achievement-card');
    if (!card) return;
    const achievement = achievementsState.find((item) => item.code === card.dataset.code);
    if (achievement) selectAchievement(achievement, achievement.locked);
  });
}

function renderDevelopment(data) {
  developmentState = data;
  const panel = document.querySelector('#level-panel');
  const number = document.querySelector('#level-number');
  const title = document.querySelector('#level-title');
  const sub = document.querySelector('#level-sub');
  const bar = document.querySelector('#level-bar');
  const ring = document.querySelector('#level-ring-value');
  const ringLength = 2 * Math.PI * 52;
  if (panel) {
    panel.classList.toggle('is-newbie', data.role === 'newbie');
    panel.classList.toggle('is-mentor', data.role === 'mentor');
  }
  // Бейдж стажёра скрыт, у наставника вместо цифры — череп (см. разметку).
  if (number) number.textContent = data.role === 'newbie' || data.role === 'mentor' ? '' : String(data.level);
  if (ring) {
    ring.style.strokeDasharray = String(ringLength);
    ring.style.strokeDashoffset = String(ringLength * (1 - Math.min(100, Number(data.account_progress) || 0) / 100));
  }
  if (title) {
    title.textContent = data.role === 'newbie'
      ? 'Уровень появится после аттестации'
      : data.role === 'mentor'
        ? 'Наставник'
        : data.level_name || `Уровень ${data.level}`;
  }
  if (sub) {
    sub.textContent = data.role === 'newbie'
      ? 'Вы стажёр: расписание видно, прогресс заморожен до перехода в «Бариста»'
      : data.role === 'mentor'
        ? 'Высшая ступень: оценивайте команду и растите ветки сотрудников'
        : `${Number(data.account_xp)} XP · до следующего уровня ${Number(data.account_xp_to_next)} XP`;
  }
  if (bar) bar.style.width = `${Math.min(100, Number(data.account_progress) || 0)}%`;

  const grid = document.querySelector('#branch-grid');
  if (grid) {
    const branches = data.branches || [];
    grid.innerHTML = branches.length
      ? branches.map(branchRow).join('')
      : '<p class="empty-state">Ветки ещё не настроены</p>';
  }

  // Награды: полученные + ближайшие заблокированные с прогрессом
  // (/api/dashboard/me → gamification.achievements_for_user).
  renderAchievements(data);

  const recent = document.querySelector('#my-evaluations');
  if (recent) {
    const rows = data.recent || [];
    recent.innerHTML = rows.length ? rows.map((evaluation) => {
      const date = evaluation.created_at ? String(evaluation.created_at).slice(0, 16).replace('T', ' ') : '';
      const scores = (evaluation.branches || [])
        .map((item) => `${escapeHtml(item.name)} ${item.score}`).join(' · ');
      return `<article class="review-row">
        <div class="review-person"><strong>${escapeHtml(evaluation.evaluator || 'Наставник')}</strong><span>${escapeHtml(date)}</span></div>
        <span class="review-meta">${scores || '—'}</span>
        <span class="review-time">${evaluation.photo_url ? `<a href="${escapeHtml(evaluation.photo_url)}" target="_blank" rel="noopener">Фото</a>` : ''}</span>
        <span class="review-score">+${Number(evaluation.xp_awarded)} XP</span>
      </article>`;
    }).join('') : '<p class="empty-state">Оценок пока нет</p>';
  }
}

async function loadDevelopment() {
  try { renderDevelopment(await api('/api/dashboard/me')); }
  catch (error) { showToast(error.message); }
}

/* --- Контроль качества -------------------------------------------------- */

// Дискретные значения селектора оценок: 0 и пороги 2.0–5.0 (шаг 0.5).
const SCORE_STEPS = [0, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0];
// Наборы параметров по категории напитка (совпадают с бэкендом DRINK_PARAMS).
// Подпись «×2» отражает множитель: параметр входит в среднее ветки дважды.
const DRINK_PARAMS = {
  'черный кофе': [
    { code: 'presentation', label: 'Подача' },
    { code: 'balance', label: 'Баланс ×2' },
    { code: 'body', label: 'Тело' },
    { code: 'bouquet', label: 'Букет' },
    { code: 'aftertaste', label: 'Послевкусие' },
    { code: 'description_accuracy', label: 'Точность описания ×2' },
  ],
  'кофе с молоком': [
    { code: 'presentation', label: 'Подача' },
    { code: 'balance', label: 'Баланс' },
    { code: 'bouquet', label: 'Букет' },
    { code: 'latte_art', label: 'Латте-арт' },
    { code: 'milk_quality', label: 'Качество молока ×2' },
    { code: 'description_accuracy', label: 'Точность описания ×2' },
  ],
  'авторский': [
    { code: 'presentation', label: 'Подача' },
    { code: 'balance', label: 'Баланс' },
    { code: 'appearance', label: 'Внешний вид' },
    { code: 'description_accuracy', label: 'Точность описания' },
  ],
};
const DEFAULT_DRINK_PARAMS = [
  { code: 'presentation', label: 'Подача' },
  { code: 'balance', label: 'Баланс' },
  { code: 'description_accuracy', label: 'Точность описания' },
];
// Критерии порядка и чистоты заведения (все идут в ветку «Стандарты»).
const CLEANLINESS_PARAMS = [
  { code: 'shift_order', label: 'Порядок на смене' },
  { code: 'work_zones', label: 'Чистота рабочих зон' },
  { code: 'hall_cleanliness', label: 'Чистота зала' },
  { code: 'restroom', label: 'Туалет' },
  { code: 'hygiene', label: 'Гигиена' },
  { code: 'service', label: 'Сервис' },
  { code: 'appearance', label: 'Внешний вид' },
];

let qualityType = 'drink';
let qualityDrinks = [];
let qualityDrinkCards = new Map();
let qualitySelectedEmployee = 0;
// Режим правки: id редактируемой оценки (null — создание новой).
let qualityEditingId = null;
// Пагинация истории сессий: по 10 записей, догрузка кнопкой «Показать ещё».
const qualitySessionsState = { offset: 0, limit: 10, items: [], hasMore: false, loading: false };

/* Набор параметров для текущего режима: чистота или категория выбранного напитка. */
function paramSpec() {
  if (qualityType === 'cleanliness') return CLEANLINESS_PARAMS;
  const drinkId = Number(document.querySelector('#evaluation-drink')?.value || 0);
  const category = qualityDrinkCards.get(drinkId)?.category;
  return DRINK_PARAMS[category] || DEFAULT_DRINK_PARAMS;
}

/* Панель параметров: на каждый критерий — ряд кнопок [0][2.0]…[5.0]. */
function renderQualityParams() {
  const container = document.querySelector('#evaluation-params');
  if (!container) return;
  container.innerHTML = paramSpec().map((param) => `
    <div class="evaluation-branch" data-param="${escapeHtml(param.code)}">
      <div class="evaluation-branch-head"><strong>${escapeHtml(param.label)}</strong></div>
      <div class="evaluation-score-row" role="group" aria-label="${escapeHtml(param.label)}">
        ${SCORE_STEPS.map((value) => `<button type="button" class="evaluation-score" data-score="${value}" aria-pressed="false">${value.toFixed(1)}</button>`).join('')}
      </div>
    </div>`).join('');
}

/* Быстрые чипы «Топ-5 напитков» — выбор одним тапом. */
function renderTopDrinks() {
  const container = document.querySelector('#quality-top-drinks');
  if (!container) return;
  const top = qualityDrinks.filter((drink) => drink.usage > 0).slice(0, 5);
  if (!top.length) { container.hidden = true; container.innerHTML = ''; return; }
  container.hidden = false;
  container.innerHTML = '<span class="quality-chips-label">Часто оценивают</span>'
    + top.map((drink) => `<button type="button" class="quality-chip" data-drink-id="${drink.id}">${escapeHtml(drink.title)}</button>`).join('');
}

function resetQualityDialog() {
  evaluationPhotoUrl = null;
  qualitySelectedEmployee = 0;
  qualityEditingId = null;
  document.querySelector('#evaluation-form')?.reset();
  // Возврат диалога в режим «Новая оценка»: показываем переключатель типа,
  // кнопку «Ещё оценка» и выбор бариста на смене.
  setText('#evaluation-dialog .dialog-top h2', 'Новая оценка');
  const submit = document.querySelector('#evaluation-submit');
  if (submit) submit.textContent = 'Сохранить';
  document.querySelector('#evaluation-dialog .quality-type')?.removeAttribute('hidden');
  const addAnother = document.querySelector('#quality-add-another');
  if (addAnother) addAnother.hidden = false;
  document.querySelector('#evaluation-dialog .quality-staff')?.removeAttribute('hidden');
  const editingLabel = document.querySelector('#quality-editing-employee');
  if (editingLabel) editingLabel.hidden = true;
  const errorNode = document.querySelector('#evaluation-form-error');
  if (errorNode) { errorNode.hidden = true; errorNode.textContent = ''; }
  const preview = document.querySelector('#evaluation-photo-preview');
  if (preview) { preview.hidden = true; preview.querySelector('img').removeAttribute('src'); }
  document.querySelector('#quality-staff-fallback')?.setAttribute('hidden', '');
  // Наставник по умолчанию оценивает напиток; старший — только чистоту.
  const defaultType = isMentor ? 'drink' : 'cleanliness';
  document.querySelectorAll('#evaluation-dialog [data-quality-type]').forEach((button) => {
    button.classList.toggle('is-selected', button.dataset.qualityType === defaultType);
  });
  setQualityType(defaultType);
}

/* Переключение «Напиток» ⇄ «Порядок и чистота»: панель полей и набор параметров. */
function setQualityType(type) {
  qualityType = type === 'cleanliness' ? 'cleanliness' : 'drink';
  const drinkFields = document.querySelector('#quality-drink-fields');
  const hint = document.querySelector('#quality-hint');
  if (drinkFields) drinkFields.hidden = qualityType === 'cleanliness';
  if (hint) {
    hint.textContent = qualityType === 'cleanliness'
      ? 'Баллы за заведение получат все бариста, кто сейчас на смене по графику.'
      : 'Выберите параметры и выставьте баллы. Ниже 4.0 опыт не начисляется.';
  }
  renderQualityParams();
  // В режиме правки бариста уже зафиксирован — список смены не подгружаем.
  if (qualityType === 'drink' && !qualityEditingId) loadQualityActiveStaff();
}

async function fillQualityEmployees() {
  const select = document.querySelector('#evaluation-employee');
  if (!select) return;
  try {
    const users = await api('/api/users');
    const me = Number(document.body.dataset.userId || 0);
    const staff = users.filter((user) => user.is_active && user.role !== 'mentor' && Number(user.id) !== me);
    select.innerHTML = staff.length
      ? '<option value="">— выберите —</option>'
        + staff.map((user) => `<option value="${user.id}">${escapeHtml(user.display_name || user.name)} — ${escapeHtml(user.role_name || user.role)}</option>`).join('')
      : '<option value="">Нет сотрудников для оценки</option>';
  } catch (error) {
    select.innerHTML = '<option value="">Не удалось загрузить список</option>';
    showToast(error.message);
  }
}

/* Напитки по частоте оценок (/api/evaluations/drinks): селект + чипы «Топ-5».
   Зерно остаётся необязательной привязкой из базы знаний. */
async function fillQualityDrinks() {
  const drinkSelect = document.querySelector('#evaluation-drink');
  const beanSelect = document.querySelector('#evaluation-bean');
  if (!drinkSelect || !beanSelect) return;
  try {
    const [drinksData, options] = await Promise.all([
      api('/api/evaluations/drinks'),
      api('/api/evaluations/options'),
    ]);
    qualityDrinks = drinksData.all || [];
    qualityDrinkCards = new Map(qualityDrinks.map((drink) => [Number(drink.id), drink]));
    drinkSelect.innerHTML = '<option value="">— выберите напиток —</option>'
      + qualityDrinks.map((drink) => `<option value="${drink.id}">${escapeHtml(drink.title)} — ${escapeHtml(drink.category)}</option>`).join('');
    drinkSelect.disabled = !qualityDrinks.length;
    const beans = options.beans || [];
    beanSelect.innerHTML = '<option value="">— не указан —</option>'
      + beans.map((bean) => `<option value="${bean.id}">${escapeHtml(bean.name)} — ${escapeHtml(bean.roast_type)}</option>`).join('');
    beanSelect.disabled = !beans.length;
    renderTopDrinks();
    renderQualityParams();
  } catch (error) {
    showToast(error.message);
  }
}

/* Бариста, которые по графику сейчас на смене в выбранной точке. */
async function loadQualityActiveStaff() {
  const container = document.querySelector('#quality-active-staff');
  if (!container || qualityType !== 'drink') return;
  const point = document.querySelector('#quality-point')?.value || 'ДЕ';
  container.innerHTML = '<span class="quality-staff-empty">Ищем бариста на смене…</span>';
  try {
    const data = await api(`/api/shifts/active-staff?point=${encodeURIComponent(point)}`);
    const active = data.active || [];
    if (!active.length) {
      container.innerHTML = '<span class="quality-staff-empty">По графику на смене никого нет</span>';
      qualitySelectedEmployee = 0;
      return;
    }
    if (!active.some((person) => Number(person.id) === qualitySelectedEmployee)) {
      qualitySelectedEmployee = Number(active[0].id);
    }
    container.innerHTML = active.map((person) => {
      const start = String(person.shift_start || '').slice(11, 16);
      const end = String(person.shift_end || '').slice(11, 16);
      const selected = Number(person.id) === qualitySelectedEmployee ? ' is-selected' : '';
      return `<button type="button" class="quality-staff-chip${selected}" data-staff-id="${person.id}"><strong>${escapeHtml(person.name)}</strong><span>${escapeHtml(start)}–${escapeHtml(end)}</span></button>`;
    }).join('');
  } catch (error) {
    container.innerHTML = '<span class="quality-staff-empty">Не удалось загрузить смену</span>';
    showToast(error.message);
  }
}

/* Аналитика наставника: агрегаты команды, без средних оценок по сотрудникам
   (/api/evaluations/analytics). Интерфейс намеренно не показывает «средние» —
   фокус на прогрессе, а не на демотивирующих цифрах. */
async function loadEvaluationAnalytics() {
  if (!managerRole) return;
  const container = document.querySelector('#evaluation-analytics');
  if (!container) return;
  try {
    renderEvaluationAnalytics(await api('/api/evaluations/analytics'));
  } catch (error) {
    showToast(error.message);
  }
}

function renderEvaluationAnalytics(data) {
  const container = document.querySelector('#evaluation-analytics');
  if (!container) return;
  const branches = data.branches || [];
  const branchesHtml = branches.length
    ? `<div class="analytics-branches">${branches.map((branch) => `
        <div class="analytics-branch"><span>${escapeHtml(branch.name)}</span><strong>${Number(branch.xp)} XP</strong></div>`).join('')}</div>`
    : '<p class="empty-state">Оценок за период пока нет</p>';
  const drinks = data.drinks || [];
  const drinksHtml = drinks.length
    ? `<div class="analytics-drinks">${drinks.map((drink) => `
        <span class="analytics-chip">${escapeHtml(drink.title)} · ${Number(drink.evaluations)}</span>`).join('')}</div>`
    : '';
  const recent = data.recent || [];
  const recentHtml = recent.length
    ? recent.map((row) => {
        const date = row.created_at ? String(row.created_at).slice(0, 16).replace('T', ' ') : '';
        const subject = [row.drink, row.bean].filter(Boolean).map(escapeHtml).join(' · ') || 'Без привязки';
        return `<article class="review-row">
          <div class="review-person"><strong>${escapeHtml(row.employee || '')}</strong><span>${escapeHtml(date)} · ${escapeHtml(row.evaluator || '')}</span></div>
          <span class="review-meta">${subject}</span>
          <span class="review-score">+${Number(row.xp_awarded || 0)} XP</span>
        </article>`;
      }).join('')
    : '';
  container.innerHTML = `
    <div class="analytics-summary">
      <div><strong>${Number(data.evaluations || 0)}</strong><span>оценок</span></div>
      <div><strong>${Number(data.xp_awarded || 0)}</strong><span>XP начислено</span></div>
    </div>
    ${branchesHtml}
    ${drinksHtml}
    ${recentHtml ? `<div class="review-list">${recentHtml}</div>` : ''}`;
}

/* --- Аналитика команды и качества (дашборд наставника) ------------------
   Три сводки бэкенда (/api/analytics/*): тепловая карта навыков, зоны риска
   по напиткам и чистоте, выработка часов по точкам. Без сторонних графических
   библиотек — только таблицы, чипы и CSS-прогресс-бары. */

const CACHE_KEY_ANALYTICS_TEAM = 'neft-cache:analytics-team';
const CACHE_KEY_ANALYTICS_DRINKS = 'neft-cache:analytics-drinks';
const CACHE_KEY_ANALYTICS_SHIFTS = 'neft-cache:analytics-shifts';

const analyticsState = {
  days: 30,
  year: new Date().getFullYear(),
  month: new Date().getMonth() + 1,
  team: null,
  drinks: null,
  shifts: null,
};

const ANALYTICS_RISK_LABEL = { high: 'Высокий риск', medium: 'Средний риск', low: 'Норма', unknown: 'Нет данных' };
const ANALYTICS_ATTENTION_LABEL = { deficit: 'Недосмотр', normal: 'Норма', overload: 'Перебор', unknown: 'Нет данных' };

/* Цветовой класс по среднему баллу: <4.0 — красный, <4.5 — жёлтый, 4.5+ — зелёный. */
function analyticsScoreClass(value) {
  if (value == null) return 'is-empty';
  if (value < 4) return 'is-low';
  if (value < 4.5) return 'is-mid';
  return 'is-high';
}

function analyticsScore(value) {
  return value == null ? '—' : Number(value).toFixed(2);
}

/* Класс прогресс-бара по уровню риска: высокий риск — красный, низкий — зелёный.
   Не путать с analyticsScoreClass: там «is-high» = хороший балл (зелёный). */
function analyticsRiskBarClass(risk) {
  if (risk === 'high') return 'is-low';
  if (risk === 'medium') return 'is-mid';
  if (risk === 'low') return 'is-high';
  return 'is-unknown';
}

function analyticsDaysLabel(days) {
  return `${Number(days) || 30} ДНЕЙ`;
}

async function loadAnalytics() {
  if (!managerRole) return;
  await Promise.allSettled([loadTeamOverview(), loadDrinksQuality(), loadShiftsReport()]);
}

async function loadTeamOverview() {
  const container = document.querySelector('#analytics-team');
  if (!container) return;
  const cacheKey = `${CACHE_KEY_ANALYTICS_TEAM}:${analyticsState.days}`;
  const cached = readCache(cacheKey);
  if (cached) {
    analyticsState.team = cached;
    renderTeamOverview();
  }
  try {
    const data = await api(`/api/analytics/team-overview?days=${analyticsState.days}`);
    analyticsState.team = data;
    writeCache(cacheKey, data);
    writeCache(CACHE_KEY_LAST_SYNC, Date.now());
    renderTeamOverview();
  } catch (error) {
    if (!analyticsState.team) container.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    if (!error.isOffline) showToast(error.message);
  }
}

async function loadDrinksQuality() {
  const container = document.querySelector('#analytics-drinks');
  if (!container) return;
  const cacheKey = `${CACHE_KEY_ANALYTICS_DRINKS}:${analyticsState.days}`;
  const cached = readCache(cacheKey);
  if (cached) {
    analyticsState.drinks = cached;
    renderDrinksQuality();
  }
  try {
    const data = await api(`/api/analytics/drinks-quality?days=${analyticsState.days}`);
    analyticsState.drinks = data;
    writeCache(cacheKey, data);
    writeCache(CACHE_KEY_LAST_SYNC, Date.now());
    renderDrinksQuality();
  } catch (error) {
    if (!analyticsState.drinks) container.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    if (!error.isOffline) showToast(error.message);
  }
}

async function loadShiftsReport() {
  const container = document.querySelector('#analytics-shifts');
  if (!container) return;
  const cacheKey = `${CACHE_KEY_ANALYTICS_SHIFTS}:${analyticsState.year}-${analyticsState.month}`;
  const cached = readCache(cacheKey);
  if (cached) {
    analyticsState.shifts = cached;
    renderShiftsReport();
  }
  try {
    const data = await api(`/api/analytics/shifts-report?year=${analyticsState.year}&month=${analyticsState.month}`);
    analyticsState.shifts = data;
    writeCache(cacheKey, data);
    writeCache(CACHE_KEY_LAST_SYNC, Date.now());
    renderShiftsReport();
  } catch (error) {
    if (!analyticsState.shifts) container.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    if (!error.isOffline) showToast(error.message);
  }
}

function renderTeamOverview() {
  const container = document.querySelector('#analytics-team');
  if (!container) return;
  const windowNode = document.querySelector('#analytics-team-window');
  if (windowNode) windowNode.textContent = analyticsDaysLabel(analyticsState.days);
  const data = analyticsState.team;
  if (!data) {
    container.innerHTML = '<p class="empty-state">Нет данных за период</p>';
    return;
  }
  const team = data.team || {};
  const attention = team.attention || {};
  const branches = data.branches || [];
  const employees = data.employees || [];
  const coverage = Math.round((Number(team.coverage) || 0) * 100);

  const summary = `
    <div class="analytics-summary">
      <div class="analytics-metric"><span>СОТРУДНИКОВ</span><strong>${Number(team.total) || 0}</strong><small>активных</small></div>
      <div class="analytics-metric"><span>ОЦЕНЕНО</span><strong>${Number(team.evaluated) || 0}</strong><small>из ${Number(team.total) || 0}</small></div>
      <div class="analytics-metric"><span>ОЦЕНОК</span><strong>${Number(team.evaluations) || 0}</strong><small>за период</small></div>
      <div class="analytics-metric"><span>ОХВАТ</span><strong>${coverage}%</strong><small>сотрудников</small></div>
      <div class="analytics-metric analytics-attention is-${escapeHtml(attention.level || 'unknown')}">
        <span>ВНИМАНИЕ</span><strong>${Number(attention.index || 0).toFixed(2)}</strong>
        <small>${escapeHtml(ANALYTICS_ATTENTION_LABEL[attention.level] || '—')} · цель ${Number(attention.target) || 0}</small>
      </div>
    </div>`;

  const head = `<tr><th class="heat-name">Сотрудник</th>${branches.map((branch) => `<th>${escapeHtml(branch.name)}</th>`).join('')}<th>Средний</th><th>Оценок</th><th>Последняя</th></tr>`;
  const rows = employees.length
    ? employees.map((employee) => {
        const cells = branches.map((branch) => {
          const value = employee.averages ? employee.averages[branch.code] : null;
          return `<td class="heat-cell ${analyticsScoreClass(value)}">${analyticsScore(value)}</td>`;
        }).join('');
        const last = employee.last_evaluated_at ? String(employee.last_evaluated_at).slice(0, 10) : '—';
        return `<tr>
          <td class="heat-name"><strong>${escapeHtml(employee.name)}</strong><small>${escapeHtml(employee.role_name || employee.role || '')}</small></td>
          ${cells}
          <td class="heat-cell ${analyticsScoreClass(employee.overall)}">${analyticsScore(employee.overall)}</td>
          <td class="heat-count">${Number(employee.evaluations) || 0}</td>
          <td class="heat-last">${escapeHtml(last)}</td>
        </tr>`;
      }).join('')
    : `<tr><td class="heat-empty" colspan="${branches.length + 4}">Оценок за период пока нет</td></tr>`;

  container.innerHTML = `${summary}
    <div class="heatmap-wrap">
      <table class="heatmap">
        <thead>${head}</thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <p class="analytics-legend"><span class="heat-cell is-low">ниже 4.0</span><span class="heat-cell is-mid">4.0–4.5</span><span class="heat-cell is-high">4.5+</span><span class="heat-cell is-empty">нет оценок</span></p>`;
}

function renderDrinksQuality() {
  const container = document.querySelector('#analytics-drinks');
  if (!container) return;
  const windowNode = document.querySelector('#analytics-drinks-window');
  if (windowNode) windowNode.textContent = analyticsDaysLabel(analyticsState.days);
  const data = analyticsState.drinks;
  if (!data) {
    container.innerHTML = '<p class="empty-state">Нет данных за период</p>';
    return;
  }
  const drinks = data.drinks || [];
  const params = data.problem_params || [];
  const cleanliness = data.cleanliness_by_point || [];

  const drinksHtml = drinks.length
    ? `<div class="analytics-rank">${drinks.map((drink) => {
        const percent = drink.average == null ? 0 : Math.max(2, Math.min(100, (Number(drink.average) / 5) * 100));
        return `<div class="analytics-rank-row">
          <div class="analytics-rank-head"><strong>${escapeHtml(drink.title)}</strong><span class="analytics-risk is-${escapeHtml(drink.risk)}">${escapeHtml(ANALYTICS_RISK_LABEL[drink.risk] || '')}</span></div>
          <div class="analytics-bar"><i class="${analyticsRiskBarClass(drink.risk)}" style="width:${percent}%"></i></div>
          <div class="analytics-rank-meta"><span>${escapeHtml(drink.category || '—')}</span><span>${analyticsScore(drink.average)} · ${Number(drink.evaluations) || 0} оц.</span></div>
        </div>`;
      }).join('')}</div>`
    : '<p class="empty-state">Напитки за период не оценивались</p>';

  const paramsHtml = params.length
    ? `<div class="analytics-params">${params.map((param) => {
        const percent = Math.max(2, Math.min(100, (Number(param.average) / 5) * 100));
        return `<div class="analytics-param">
          <span class="analytics-param-label">${escapeHtml(param.label)}</span>
          <div class="analytics-bar"><i class="${analyticsScoreClass(param.average)}" style="width:${percent}%"></i></div>
          <b>${analyticsScore(param.average)}</b>
        </div>`;
      }).join('')}</div>`
    : '<p class="empty-state">Параметры не оценивались</p>';

  const cleanHtml = cleanliness.length
    ? `<div class="analytics-clean">${cleanliness.map((row) => {
        const percent = Math.max(2, Math.min(100, (Number(row.average) / 5) * 100));
        const pointClass = row.point === 'УЯ' ? 'is-uy' : 'is-de';
        return `<div class="analytics-clean-row">
          <span class="analytics-point-tag ${pointClass}">${escapeHtml(row.point)}</span>
          <div class="analytics-bar"><i class="${analyticsScoreClass(row.average)}" style="width:${percent}%"></i></div>
          <b>${analyticsScore(row.average)}</b>
          <small>${Number(row.evaluations) || 0} оц.</small>
        </div>`;
      }).join('')}</div>`
    : '<p class="empty-state">Оценок чистоты пока нет</p>';

  container.innerHTML = `
    <div class="analytics-grid">
      <div class="analytics-col">
        <h4 class="analytics-subtitle">Напитки-аутсайдеры</h4>
        ${drinksHtml}
      </div>
      <div class="analytics-col">
        <h4 class="analytics-subtitle">Проседающие параметры (топ-10)</h4>
        ${paramsHtml}
        <h4 class="analytics-subtitle">Чистота и порядок по точкам</h4>
        ${cleanHtml}
      </div>
    </div>`;
}

function renderShiftsReport() {
  const container = document.querySelector('#analytics-shifts');
  if (!container) return;
  const periodNode = document.querySelector('#analytics-shifts-period');
  if (periodNode) periodNode.textContent = `${monthNames[analyticsState.month - 1] || ''} ${analyticsState.year}`.toUpperCase();
  const data = analyticsState.shifts;
  if (!data) {
    container.innerHTML = '<p class="empty-state">Нет данных за месяц</p>';
    return;
  }
  const points = data.points || [];
  const team = data.team || {};
  const cards = points.map((point) => {
    const fact = Number(point.fact_hours) || 0;
    const plan = Number(point.plan_hours) || 0;
    const percent = plan > 0 ? Math.max(2, Math.min(100, (fact / plan) * 100)) : 0;
    const delta = Number(point.delta_hours) || 0;
    const quality = point.quality || {};
    const pointClass = point.point === 'УЯ' ? 'is-uy' : 'is-de';
    return `<article class="analytics-point ${pointClass}">
      <div class="analytics-point-head"><span class="analytics-point-tag ${pointClass}">${escapeHtml(point.point)}</span><span class="analytics-point-shifts">${Number(point.shift_count) || 0} смен</span></div>
      <div class="analytics-point-hours"><strong>${fact.toFixed(1)}</strong><span>/ ${plan.toFixed(1)} ч</span></div>
      <div class="analytics-bar"><i class="${delta < 0 ? 'is-low' : 'is-high'}" style="width:${percent}%"></i></div>
      <div class="analytics-point-meta"><span>Δ ${delta > 0 ? '+' : ''}${delta.toFixed(1)} ч</span><span>ср. смена ${point.avg_shift_hours == null ? '—' : Number(point.avg_shift_hours).toFixed(1)} ч</span></div>
      <div class="analytics-point-quality">
        <div><span>Качество</span><b class="${analyticsScoreClass(quality.average)}">${analyticsScore(quality.average)}</b></div>
        <div><span>Напитки</span><b class="${analyticsScoreClass(quality.drinks_average)}">${analyticsScore(quality.drinks_average)}</b></div>
        <div><span>Чистота</span><b class="${analyticsScoreClass(quality.cleanliness_average)}">${analyticsScore(quality.cleanliness_average)}</b></div>
        <div><span>Оценок</span><b>${Number(quality.evaluations) || 0}</b></div>
      </div>
    </article>`;
  }).join('');

  const teamLine = `<div class="analytics-summary">
    <div class="analytics-metric"><span>ФАКТ</span><strong>${team.fact == null ? '—' : Number(team.fact).toFixed(1)}</strong><small>часов</small></div>
    <div class="analytics-metric"><span>Σ ОПТ</span><strong>${team.opt == null ? '—' : Number(team.opt).toFixed(1)}</strong><small>планка</small></div>
    <div class="analytics-metric"><span>Σ МИН</span><strong>${team.min == null ? '—' : Number(team.min).toFixed(1)}</strong><small>потребность</small></div>
    <div class="analytics-metric"><span>Σ МАКС</span><strong>${team.max == null ? '—' : Number(team.max).toFixed(1)}</strong><small>лимит</small></div>
  </div>`;

  container.innerHTML = `${teamLine}
    <div class="analytics-points">${cards || '<p class="empty-state">Смен за месяц нет</p>'}</div>
    ${data.note ? `<p class="analytics-note">${escapeHtml(data.note)}</p>` : ''}`;
}

function renderAnalyticsSelectors() {
  const monthSelect = document.querySelector('#analytics-month');
  const yearSelect = document.querySelector('#analytics-year');
  if (!monthSelect || !yearSelect) return;
  monthSelect.innerHTML = monthNames
    .map((name, index) => `<option value="${index + 1}"${analyticsState.month === index + 1 ? ' selected' : ''}>${name}</option>`)
    .join('');
  const now = new Date();
  const years = [];
  for (let year = now.getFullYear() - 1; year <= now.getFullYear() + 2; year += 1) {
    years.push(`<option value="${year}"${analyticsState.year === year ? ' selected' : ''}>${year}</option>`);
  }
  yearSelect.innerHTML = years.join('');
}

function setupAnalytics() {
  const view = document.querySelector('#analytics-view');
  if (!view) return;
  renderAnalyticsSelectors();
  view.querySelectorAll('[data-analytics-days]').forEach((button) => {
    button.addEventListener('click', () => {
      const days = Number(button.dataset.analyticsDays) || 30;
      if (days === analyticsState.days) return;
      analyticsState.days = days;
      view.querySelectorAll('[data-analytics-days]').forEach((item) => item.classList.toggle('is-selected', item === button));
      loadTeamOverview();
      loadDrinksQuality();
    });
  });
  const monthSelect = document.querySelector('#analytics-month');
  const yearSelect = document.querySelector('#analytics-year');
  const applyPeriod = () => {
    const month = Number(monthSelect?.value);
    const year = Number(yearSelect?.value);
    if (!month || !year) return;
    analyticsState.month = month;
    analyticsState.year = year;
    loadShiftsReport();
  };
  monthSelect?.addEventListener('change', applyPeriod);
  yearSelect?.addEventListener('change', applyPeriod);
}

async function uploadEvaluationPhoto(file) {
  if (!file) return;
  const preview = document.querySelector('#evaluation-photo-preview');
  const errorNode = document.querySelector('#evaluation-form-error');
  const image = preview?.querySelector('img');
  if (errorNode) errorNode.hidden = true;
  try {
    // Сырое тело, как у аватара: multipart не нужен, Content-Type — от файла.
    const response = await fetch('/api/evaluations/photo', {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
      credentials: 'same-origin',
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить фото');
    evaluationPhotoUrl = data.url;
    if (image) image.src = data.url;
    if (preview) preview.hidden = false;
  } catch (error) {
    if (errorNode) { errorNode.textContent = error.message; errorNode.hidden = false; }
  }
}

function setupDevelopment() {
  const dialog = document.querySelector('#evaluation-dialog');
  const newButton = document.querySelector('#quality-new');
  if (!dialog || !newButton) return;
  setupDevelopmentTabs();
  // Старший ведёт только чистоту и порядок: прячем вариант «Напиток».
  if (!isMentor) {
    document.querySelector('#evaluation-dialog [data-quality-type="drink"]')?.setAttribute('hidden', '');
  }

  newButton.addEventListener('click', async () => {
    resetQualityDialog();
    dialog.showModal();
    if (!developmentState) loadDevelopment();
    // Напитки оценивает только наставник — для старшего грузим лишь параметры чистоты.
    if (isMentor) {
      await fillQualityDrinks();
      fillQualityEmployees();
      loadQualityActiveStaff();
    }
  });

  // Переключатель типа оценки «Напиток» ⇄ «Порядок и чистота».
  document.querySelector('#evaluation-dialog .quality-type')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-quality-type]');
    if (!button) return;
    document.querySelectorAll('#evaluation-dialog [data-quality-type]').forEach((item) => {
      item.classList.toggle('is-selected', item === button);
    });
    setQualityType(button.dataset.qualityType);
  });

  // Смена точки — перезагружаем бариста на смене; смена напитка — параметры.
  document.querySelector('#quality-point')?.addEventListener('change', loadQualityActiveStaff);
  document.querySelector('#evaluation-drink')?.addEventListener('change', renderQualityParams);

  // Быстрые чипы «Топ-5 напитков».
  document.querySelector('#quality-top-drinks')?.addEventListener('click', (event) => {
    const chip = event.target.closest('.quality-chip');
    if (!chip) return;
    const select = document.querySelector('#evaluation-drink');
    if (select) select.value = chip.dataset.drinkId;
    renderQualityParams();
  });

  // Бариста на смене: переключаем выбор чипом.
  document.querySelector('#quality-active-staff')?.addEventListener('click', (event) => {
    const chip = event.target.closest('.quality-staff-chip');
    if (!chip) return;
    qualitySelectedEmployee = Number(chip.dataset.staffId);
    chip.parentElement?.querySelectorAll('.quality-staff-chip').forEach((item) => {
      item.classList.toggle('is-selected', item === chip);
    });
  });

  // Фоллбек: ручной выбор из полного списка сотрудников.
  document.querySelector('#quality-other-barista')?.addEventListener('click', () => {
    const fallback = document.querySelector('#quality-staff-fallback');
    if (fallback) fallback.hidden = !fallback.hidden;
  });

  // Баллы: одна кнопка на параметр.
  document.querySelector('#evaluation-params')?.addEventListener('click', (event) => {
    const button = event.target.closest('.evaluation-score');
    if (!button) return;
    const row = button.closest('.evaluation-branch');
    row?.querySelectorAll('.evaluation-score').forEach((item) => {
      const selected = item === button;
      item.classList.toggle('is-selected', selected);
      item.setAttribute('aria-pressed', String(selected));
    });
  });

  document.querySelector('#evaluation-photo-input')?.addEventListener('change', (event) => {
    uploadEvaluationPhoto(event.currentTarget.files?.[0]);
  });
  document.querySelector('#evaluation-photo-remove')?.addEventListener('click', () => {
    evaluationPhotoUrl = null;
    const preview = document.querySelector('#evaluation-photo-preview');
    if (preview) { preview.hidden = true; preview.querySelector('img').removeAttribute('src'); }
    const input = document.querySelector('#evaluation-photo-input');
    if (input) input.value = '';
  });

  document.querySelector('#evaluation-form')?.addEventListener('submit', (event) => {
    event.preventDefault();
    submitQualityEvaluation(dialog, false);
  });
  document.querySelector('#quality-add-another')?.addEventListener('click', () => {
    submitQualityEvaluation(dialog, true);
  });

  // Правка задним числом: кнопка «Изменить» в строке истории (делегирование).
  document.querySelector('#quality-sessions')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-edit-id]');
    if (!button) return;
    const id = Number(button.dataset.editId);
    const item = qualitySessionsState.items.find((row) => Number(row.id) === id);
    if (item) openQualityEditor(item);
  });
  // Догрузка следующей страницы истории.
  document.querySelector('#quality-sessions-more')?.addEventListener('click', () => loadQualitySessions(false));
}

/* Вкладки раздела «Развитие»: «Мой прогресс» / «Контроль качества». */
function setDevelopmentTab(tab) {
  const quality = tab === 'quality';
  document.querySelectorAll('#development-view [data-development-tab]').forEach((button) => {
    button.classList.toggle('is-selected', button.dataset.developmentTab === (quality ? 'quality' : 'progress'));
  });
  const progress = document.querySelector('#development-progress-panel');
  const qualityPanel = document.querySelector('#quality-panel');
  if (progress) progress.hidden = quality;
  if (qualityPanel) qualityPanel.hidden = !quality;
  setText('#development-eyebrow', quality ? 'КОНТРОЛЬ КАЧЕСТВА' : 'ПРОКАЧКА');
  setText('#development-title', quality ? 'Оценка качества' : 'Развитие');
  if (quality) { loadQualitySessions(); loadEvaluationAnalytics(); }
}

function setupDevelopmentTabs() {
  const tabs = document.querySelector('#development-view .development-tabs');
  if (!tabs) return;
  tabs.addEventListener('click', (event) => {
    const button = event.target.closest('[data-development-tab]');
    if (button) setDevelopmentTab(button.dataset.developmentTab);
  });
}

/* История сессий контроля качества: по 10 записей, догрузка кнопкой «Показать ещё». */
async function loadQualitySessions(reset = true) {
  const container = document.querySelector('#quality-sessions');
  if (!container || qualitySessionsState.loading) return;
  if (reset) { qualitySessionsState.offset = 0; qualitySessionsState.items = []; }
  qualitySessionsState.loading = true;
  const moreButton = document.querySelector('#quality-sessions-more');
  if (moreButton) moreButton.disabled = true;
  try {
    const url = `/api/evaluations/sessions?limit=${qualitySessionsState.limit}&offset=${qualitySessionsState.offset}`;
    const data = await api(url);
    const items = data.items || [];
    qualitySessionsState.items = reset ? items : qualitySessionsState.items.concat(items);
    qualitySessionsState.hasMore = Boolean(data.has_more);
    qualitySessionsState.offset = qualitySessionsState.items.length;
    renderQualitySessions();
  } catch (error) {
    if (reset) container.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
  } finally {
    qualitySessionsState.loading = false;
    if (moreButton) moreButton.disabled = false;
  }
}

function renderQualitySessions() {
  const container = document.querySelector('#quality-sessions');
  if (!container) return;
  const items = qualitySessionsState.items;
  const moreButton = document.querySelector('#quality-sessions-more');
  const countLabel = document.querySelector('#quality-sessions-count');
  // Правка доступна только наставнику.
  const canEdit = isMentor;
  if (!items.length) {
    container.innerHTML = '<p class="empty-state">Оценочных сессий пока нет</p>';
    if (moreButton) moreButton.hidden = true;
    if (countLabel) countLabel.textContent = 'ПО 10';
    return;
  }
  container.innerHTML = items.map((item) => {
    const date = String(item.created_at || '').slice(0, 16).replace('T', ' ');
    const overall = item.overall == null ? '—' : Number(item.overall).toFixed(1);
    const subject = item.evaluation_type === 'cleanliness'
      ? 'Порядок и чистота'
      : escapeHtml(item.drink || 'Напиток');
    const category = item.category ? ` · ${escapeHtml(item.category)}` : '';
    const edited = item.updated_at ? ' · <span class="session-edited">изменено</span>' : '';
    const point = item.shift_point ? `<span class="session-point">${escapeHtml(item.shift_point)}</span>` : '';
    return `
      <article class="session-row">
        <div class="session-main"><strong>${escapeHtml(item.employee || '')}</strong><span>${subject}${category}${edited}</span></div>
        <div class="session-meta">${point}<span>${escapeHtml(date)}</span><span>${escapeHtml(item.evaluator || '')}</span></div>
        <span class="session-score">${overall}</span>
        <span class="session-xp">+${Number(item.xp_awarded || 0)} XP</span>
        ${canEdit ? `<button class="icon-button session-edit" type="button" data-edit-id="${item.id}" aria-label="Изменить оценку" title="Изменить оценку"><svg class="icon" aria-hidden="true"><use href="#i-edit"></use></svg></button>` : ''}
      </article>`;
  }).join('');
  if (moreButton) moreButton.hidden = !qualitySessionsState.hasMore;
  if (countLabel) countLabel.textContent = qualitySessionsState.hasMore ? `${items.length}+` : String(items.length);
}

/* Открытие диалога оценки в режиме правки: баллы, комментарии, фото (и напиток). */
function openQualityEditor(item) {
  const dialog = document.querySelector('#evaluation-dialog');
  if (!dialog) return;
  resetQualityDialog();
  qualityEditingId = item.id;

  setText('#evaluation-dialog .dialog-top h2', 'Редактировать оценку');
  const submit = document.querySelector('#evaluation-submit');
  if (submit) submit.textContent = 'Сохранить изменения';
  document.querySelector('#evaluation-dialog .quality-type')?.setAttribute('hidden', '');
  const addAnother = document.querySelector('#quality-add-another');
  if (addAnother) addAnother.hidden = true;
  const editingLabel = document.querySelector('#quality-editing-employee');
  if (editingLabel) {
    editingLabel.textContent = `Сотрудник: ${item.employee || ''}`;
    editingLabel.hidden = false;
  }

  const pointSelect = document.querySelector('#quality-point');
  if (pointSelect && item.shift_point) pointSelect.value = item.shift_point;
  document.querySelectorAll('#evaluation-dialog [data-quality-type]').forEach((button) => {
    button.classList.toggle('is-selected', button.dataset.qualityType === item.evaluation_type);
  });
  setQualityType(item.evaluation_type);

  if (item.evaluation_type !== 'cleanliness') {
    document.querySelector('#evaluation-dialog .quality-staff')?.setAttribute('hidden', '');
    document.querySelector('#quality-staff-fallback')?.setAttribute('hidden', '');
    const drinkSelect = document.querySelector('#evaluation-drink');
    const beanSelect = document.querySelector('#evaluation-bean');
    if (drinkSelect && item.drink_id) drinkSelect.value = String(item.drink_id);
    if (beanSelect && item.bean_id) beanSelect.value = String(item.bean_id);
    renderQualityParams();
  }
  applyQualityScores(item.params || {});

  const comment = document.querySelector('#quality-drink-comment');
  if (comment) comment.value = item.comment || '';
  const mentor = document.querySelector('#quality-mentor-comment');
  if (mentor) mentor.value = item.mentor_comment || '';
  evaluationPhotoUrl = item.photo_url || null;
  const preview = document.querySelector('#evaluation-photo-preview');
  if (preview) {
    if (evaluationPhotoUrl) { preview.querySelector('img').src = evaluationPhotoUrl; preview.hidden = false; }
    else { preview.hidden = true; preview.querySelector('img').removeAttribute('src'); }
  }
  const errorNode = document.querySelector('#evaluation-form-error');
  if (errorNode) { errorNode.hidden = true; errorNode.textContent = ''; }
  dialog.showModal();
}

/* Проставить сохранённые баллы в кнопки параметров. */
function applyQualityScores(params) {
  document.querySelectorAll('#evaluation-params .evaluation-branch').forEach((row) => {
    const value = params[row.dataset.param];
    row.querySelectorAll('.evaluation-score').forEach((button) => {
      const selected = value != null && Number(button.dataset.score) === Number(value);
      button.classList.toggle('is-selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
  });
}

/* Отправка оценки. keepOpen — «Ещё оценка»: сохраняем без закрытия окна. */
async function submitQualityEvaluation(dialog, keepOpen) {
  const type = qualityType;
  const errorNode = document.querySelector('#evaluation-form-error');
  const setError = (message) => { if (errorNode) { errorNode.textContent = message; errorNode.hidden = false; } };
  if (errorNode) errorNode.hidden = true;

  const scores = {};
  document.querySelectorAll('#evaluation-params .evaluation-branch').forEach((row) => {
    const selected = row.querySelector('.evaluation-score.is-selected');
    if (selected) scores[row.dataset.param] = Number(selected.dataset.score);
  });
  if (!Object.keys(scores).length) { setError('Выставьте хотя бы один балл'); return; }

  const payload = {
    evaluation_type: type,
    point: document.querySelector('#quality-point')?.value || null,
    scores,
    photo_url: evaluationPhotoUrl,
    comment: document.querySelector('#quality-drink-comment')?.value.trim() || null,
    mentor_comment: document.querySelector('#quality-mentor-comment')?.value.trim() || null,
  };

  if (type === 'drink') {
    const drinkId = Number(document.querySelector('#evaluation-drink')?.value || 0);
    if (!drinkId) { setError('Выберите напиток'); return; }
    payload.drink_id = drinkId;
    payload.bean_id = Number(document.querySelector('#evaluation-bean')?.value || 0) || null;
    payload.category = qualityDrinkCards.get(drinkId)?.category || null;
    // При правке сотрудник зафиксирован — менять его нельзя.
    if (!qualityEditingId) {
      const employeeId = qualitySelectedEmployee || Number(document.querySelector('#evaluation-employee')?.value || 0);
      if (!employeeId) { setError('Выберите бариста'); return; }
      payload.employee_id = employeeId;
    }
  }

  const editingId = qualityEditingId;
  const submit = document.querySelector('#evaluation-submit');
  const addAnother = document.querySelector('#quality-add-another');
  if (submit) submit.disabled = true;
  if (addAnother) addAnother.disabled = true;
  try {
    const result = editingId
      ? await api(`/api/evaluations/${editingId}`, { method: 'PATCH', body: JSON.stringify(payload) })
      : await api('/api/evaluations', { method: 'POST', body: JSON.stringify(payload) });
    const overall = result.overall != null ? ` (${Number(result.overall).toFixed(1)})` : '';
    const xp = result.total_xp ? ` · +${result.total_xp} XP` : '';
    showToast(editingId ? `Оценка обновлена${overall}${xp}` : `Оценка сохранена${overall}${xp}`);
    if (keepOpen && type === 'drink' && !editingId) {
      resetQualityDialog();
    } else {
      dialog.close();
    }
    loadDevelopment();
    loadQualitySessions(true);
    loadEvaluationAnalytics();
  } catch (error) {
    setError(error.message);
  } finally {
    if (submit) submit.disabled = false;
    if (addAnother) addAnother.disabled = false;
  }
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
  const roleNames = { newbie: 'Стажёр', barista: 'Бариста', senior: 'Старший', mentor: 'Наставник', guest: 'Ожидает доступа' };
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

/* --- Команда: суб-табы «Доступ и роли» / «Расчет часов» ---------------- */

const WORKLOAD_POINTS = [
  { code: 'УЯ', cls: 'is-uy' },
  { code: 'ДЕ', cls: 'is-de' },
];
const SATURATION_LABEL = {
  plan: { overload: 'Перепланировано', normal: 'Норма', deficit: 'Недопланировано', unknown: 'Нет данных' },
  norms: { overload: 'Штат с запасом', normal: 'Штата хватает', deficit: 'Дефицит штата', unknown: 'Нет данных' },
};
// Совпадают с SATURATION_DEFICIT / SATURATION_OVERLOAD в workload_service.py.
const SATURATION_DEFICIT = 0.9;
const SATURATION_OVERLOAD = 1.1;
const DEFAULT_WORKLOAD_HOURS = { min_hours: 80, opt_hours: 130, max_hours: 170 };

let workloadState = null;
let workloadRequiredTotal = 0;
let workloadCursor = { year: new Date().getFullYear(), month: new Date().getMonth() + 1 };
// Доля дней с пересменом (ползунок «n% дней с пересменом») — отдельно для каждой точки.
const workloadOverlap = { 'УЯ': 100, 'ДЕ': 100 };

function setText(selector, value) {
  const node = document.querySelector(selector);
  if (node) node.textContent = value;
}

function workloadHours(value) {
  const number = Number(value) || 0;
  const rounded = Math.round(number * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

function setTeamTab(tab) {
  document.querySelectorAll('#team-view [data-team-tab]').forEach((button) => {
    button.classList.toggle('is-selected', button.dataset.teamTab === tab);
  });
  const access = document.querySelector('#team-access-panel');
  const workload = document.querySelector('#team-workload-panel');
  if (access) access.hidden = tab !== 'access';
  if (workload) workload.hidden = tab !== 'workload';
  setText('#team-eyebrow', tab === 'workload' ? 'РАСЧЕТ ЧАСОВ' : 'ДОСТУП И РОЛИ');
  setText('#team-title', tab === 'workload' ? 'Кадровая насыщенность' : 'Команда');
  const addButton = document.querySelector('#add-user-button');
  if (addButton) addButton.hidden = tab === 'workload';
  if (tab === 'workload') loadWorkload();
}

function setupTeamTabs() {
  const tabs = document.querySelector('#team-view .team-tabs');
  if (!tabs) return;
  tabs.addEventListener('click', (event) => {
    const button = event.target.closest('[data-team-tab]');
    if (button) setTeamTab(button.dataset.teamTab);
  });
  renderWorkloadSelectors();
  const monthSelect = document.querySelector('#workload-month');
  const yearSelect = document.querySelector('#workload-year');
  const applyPeriod = () => {
    const month = Number(monthSelect?.value);
    const year = Number(yearSelect?.value);
    if (!month || !year) return;
    workloadCursor = { year, month };
    loadWorkload();
  };
  monthSelect?.addEventListener('change', applyPeriod);
  yearSelect?.addEventListener('change', applyPeriod);
}

/* Месяц и год — нативные select: их рисует сам браузер (в том числе на iOS),
   поэтому не нужны ни кастомный календарь, ни ограничение по «±6 месяцев». */
function renderWorkloadSelectors() {
  const monthSelect = document.querySelector('#workload-month');
  const yearSelect = document.querySelector('#workload-year');
  if (!monthSelect || !yearSelect) return;
  monthSelect.innerHTML = monthNames
    .map((name, index) => `<option value="${index + 1}"${workloadCursor.month === index + 1 ? ' selected' : ''}>${name}</option>`)
    .join('');
  const now = new Date();
  const years = [];
  for (let year = now.getFullYear() - 1; year <= now.getFullYear() + 2; year += 1) {
    years.push(`<option value="${year}"${workloadCursor.year === year ? ' selected' : ''}>${year}</option>`);
  }
  yearSelect.innerHTML = years.join('');
}

async function loadWorkload() {
  const source = document.querySelector('#workload-source');
  if (source) source.textContent = 'Загрузка…';
  try {
    workloadState = await api(`/api/team/workload-stats?year=${workloadCursor.year}&month=${workloadCursor.month}`);
    renderWorkload();
    if (source) {
      source.textContent = workloadState.calendar.source === 'isdayoff'
        ? 'Производственный календарь: isdayoff.ru · Татарстан'
        : 'Производственный календарь: встроенный · Татарстан';
    }
  } catch (error) {
    workloadState = null;
    if (source) source.textContent = error.message;
    renderWorkload();
  }
}

function renderWorkload() {
  const state = workloadState;
  if (!state) {
    ['#workload-total-days', '#workload-working-days', '#workload-holiday-days', '#workload-fact',
      '#workload-min', '#workload-opt', '#workload-max', '#workload-required'].forEach((selector) => setText(selector, '—'));
    const points = document.querySelector('#workload-points');
    if (points) points.innerHTML = '<p class="empty-state">Не удалось загрузить расчёт</p>';
    const list = document.querySelector('#workload-team-list');
    if (list) list.innerHTML = '';
    const grid = document.querySelector('#workload-quota-grid');
    if (grid) grid.innerHTML = '';
    resetWorkloadIndicator();
    return;
  }

  const calendar = state.calendar;
  setText('#workload-total-days', calendar.total_days);
  setText('#workload-working-days', calendar.working_days);
  setText('#workload-holiday-days', calendar.holidays);
  const note = document.querySelector('#workload-calendar-note');
  if (note) {
    note.textContent = calendar.source === 'isdayoff'
      ? 'IsDayOff · Татарстан'
      : calendar.source === 'fallback' ? 'Офлайн-календарь · Татарстан' : 'IsDayOff + праздники РТ';
  }

  const totals = state.totals;
  setText('#workload-fact', workloadHours(totals.fact));
  setText('#workload-min', totals.min);
  setText('#workload-opt', totals.opt);
  setText('#workload-max', totals.max);

  renderWorkloadTeam(state.employees);
  // Точки считают «Итого необходимо», которое нужно индикатору в режиме
  // «по потребности», поэтому индикатор обновляем после них.
  renderWorkloadPoints();
  renderWorkloadRequired();
  renderWorkloadIndicator();
  renderWorkloadWarning(state);
}

/* Пустой ответ/ошибка: гасим все изменяемые части карточки команды. */
function resetWorkloadIndicator() {
  const badge = document.querySelector('#workload-badge');
  if (badge) { badge.textContent = '—'; badge.className = 'workload-badge'; }
  setText('#workload-basis', '—');
  [document.querySelector('#workload-track-fill'), document.querySelector('#workload-track-ghost')]
    .forEach((bar) => { if (bar) { bar.style.width = '0%'; bar.className = ''; } });
  const track = document.querySelector('#workload-track');
  if (track) track.hidden = false;
  setText('#workload-quota', '');
  const warning = document.querySelector('#workload-warning');
  if (warning) { warning.hidden = true; warning.textContent = ''; }
}

function levelFromRatio(ratio) {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return 'unknown';
  if (ratio < SATURATION_DEFICIT) return 'deficit';
  if (ratio > SATURATION_OVERLOAD) return 'overload';
  return 'normal';
}

function plural(count, one, few, many) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

/* Индикатор насыщенности. База зависит от того, насколько проставлен месяц:
   полный график — факт, частичный — прогноз по дням недели, пустой — потребность
   точек (её считаем здесь: она зависит от ползунков «n% дней с пересменом»). */
function workloadIndicator() {
  const state = workloadState;
  if (!state) return null;
  const plan = state.plan || { state: 'empty' };
  const totals = state.totals || {};
  const byPlan = plan.state === 'complete' || plan.state === 'partial';
  const ratio = byPlan
    ? (state.saturation?.ratio ?? null)
    : (workloadRequiredTotal > 0 ? (totals.opt || 0) / workloadRequiredTotal : null);
  // Для графика уровень берём с сервера: он считает его по неокруглённому
  // отношению (клиент видит уже округлённые 3 знака и на пороге ошибётся).
  const serverLevel = state.saturation?.level;
  const level = byPlan && serverLevel && serverLevel !== 'unknown'
    ? serverLevel
    : levelFromRatio(ratio);
  return {
    basis: byPlan ? plan.state : 'norms',
    ratio,
    level,
    projected: Number(plan.projected_fact) || 0,
    lastDay: plan.last_shift_day,
  };
}

function workloadQuotaText(state, indicator) {
  const totals = state.totals || {};
  const opt = totals.opt || 0;
  if (indicator.basis === 'fact') {
    return opt > 0
      ? `Проставлено ${workloadHours(totals.fact)} ч — это ${Math.round((totals.fact / opt) * 100)}% от оптимальной планки команды`
      : 'Нормо-часы команды не заданы — процент не считается';
  }
  if (indicator.basis === 'forecast') {
    const percent = opt > 0 ? `, это ${Math.round((indicator.projected / opt) * 100)}% от оптимальной планки` : '';
    return `Проставлено ${workloadHours(totals.fact)} ч (график на 1–${indicator.lastDay}). Прогноз на конец месяца — ${workloadHours(indicator.projected)} ч${percent}`;
  }
  if (workloadRequiredTotal > 0) {
    return `График на месяц ещё не проставлен. Штат закрывает ${Math.round((opt / workloadRequiredTotal) * 100)}% потребности точек`;
  }
  return 'График на месяц ещё не проставлен. Заполните шаблоны смен, чтобы посчитать потребность';
}

function renderWorkloadIndicator() {
  const state = workloadState;
  const indicator = workloadIndicator();
  if (!state || !indicator) return;

  const labelSet = indicator.basis === 'norms' ? SATURATION_LABEL.norms : SATURATION_LABEL.plan;
  const badge = document.querySelector('#workload-badge');
  if (badge) {
    badge.textContent = labelSet[indicator.level] || labelSet.unknown;
    badge.className = `workload-badge is-${indicator.level}`;
  }
  const basisText = indicator.basis === 'fact'
    ? 'Факт · месяц проставлен полностью'
    : indicator.basis === 'forecast' ? `Прогноз · график проставлен на 1–${indicator.lastDay}`
      : 'Потребность · графика на месяц ещё нет';
  setText('#workload-basis', basisText);

  const totals = state.totals || {};
  const opt = totals.opt || 0;
  const showTrack = indicator.basis !== 'norms';
  const track = document.querySelector('#workload-track');
  if (track) track.hidden = !showTrack;
  const solid = showTrack && opt > 0 ? (totals.fact / opt) * 100 : 0;
  // Полупрозрачный довесок полосы — прогноз до конца месяца.
  const planned = showTrack && opt > 0 ? (indicator.projected / opt) * 100 : 0;
  const fill = document.querySelector('#workload-track-fill');
  if (fill) {
    fill.style.width = `${Math.min(100, Math.round(solid))}%`;
    fill.className = `is-${indicator.level}`;
  }
  const ghost = document.querySelector('#workload-track-ghost');
  if (ghost) {
    ghost.style.width = `${Math.min(100, Math.round(planned))}%`;
    ghost.className = `is-${indicator.level}`;
  }
  setText('#workload-quota', workloadQuotaText(state, indicator));
}

function renderWorkloadWarning(state) {
  const warning = document.querySelector('#workload-warning');
  if (!warning) return;
  const count = Number(state.employees_without_iiko) || 0;
  warning.hidden = count === 0;
  warning.textContent = count === 0 ? ''
    : `${count} ${plural(count, 'сотрудник', 'сотрудника', 'сотрудников')} без Iiko ID — их смены в расчёт не попадают`;
}

function renderWorkloadTeam(employees) {
  const list = document.querySelector('#workload-team-list');
  if (!list) return;
  if (!employees.length) {
    list.innerHTML = '<p class="empty-state">Активных сотрудников нет</p>';
    return;
  }
  const head = '<div class="workload-row is-head"><span class="workload-person">Сотрудник</span>'
    + '<span class="workload-cell">факт</span><span class="workload-cell">мин</span>'
    + '<span class="workload-cell">опт</span><span class="workload-cell">макс</span></div>';
  const rows = employees.map((employee) => `
    <div class="workload-row" data-user-id="${employee.id}">
      <span class="workload-person"><span class="user-avatar">${avatarMarkup(employee.id, employee.avatar_rev, employee.name)}</span><strong title="${escapeHtml(employee.name)}">${escapeHtml(employee.name)}</strong><small>${escapeHtml(employee.role_name || '')}</small></span>
      <span class="workload-cell is-fact" title="Проставлено в графике">${workloadHours(employee.fact_hours)}</span>
      <span class="workload-cell" title="Минимум часов в месяц">${employee.min_hours}</span>
      <span class="workload-cell" title="Оптимум часов в месяц">${employee.opt_hours}</span>
      <span class="workload-cell" title="Максимум часов в месяц">${employee.max_hours}</span>
    </div>`).join('');
  list.innerHTML = head + rows;
}

function pointCard(point, config) {
  const share = workloadOverlap[point];
  return `
    <section class="panel settings-panel workload-point ${config.cls}" data-point="${point}">
      <div class="panel-heading">
        <div><p class="eyebrow">ТОЧКА</p><h3>${point}</h3></div>
        <span class="workload-point-total"><strong data-point-total>—</strong><small>часов всего</small></span>
      </div>
      <div class="workload-point-grid">
        <div class="workload-point-line"><span>Утро в будни</span><b data-point-morning>—</b></div>
        <div class="workload-point-line"><span>Утро в выходные</span><b data-point-weekend>—</b></div>
        <div class="workload-point-line"><span>Пересмен</span><b data-point-overlap>—</b></div>
        <div class="workload-point-line"><span>Вечер</span><b data-point-evening>—</b></div>
      </div>
      <label class="workload-slider">
        <span class="workload-slider-value"><b data-point-share>${share}%</b> дней с пересменом</span>
        <input type="range" min="0" max="100" step="5" value="${share}" data-point-range data-point-code="${point}" aria-label="${point}: доля дней с пересменом">
      </label>
    </section>`;
}

function renderWorkloadPoints() {
  const container = document.querySelector('#workload-points');
  if (!container || !workloadState) return;
  container.innerHTML = WORKLOAD_POINTS.map((config) => pointCard(config.code, config)).join('');
  recalcWorkloadPoints();
  container.querySelectorAll('[data-point-range]').forEach((input) => {
    input.addEventListener('input', () => {
      workloadOverlap[input.dataset.pointCode] = Number(input.value);
      recalcWorkloadPoints();
    });
  });
}

/* Часы точки при текущей доле пересменов n%:
   mult = 2 − n/100 (n=100% → ×1, n=0% → ×2). Ползунок двигает часы пересмена,
   утра (будни и выходные) и вечера, а значит и итог по точке. */
function pointNumbers(code) {
  const means = workloadState.points[code] || {};
  const calendar = workloadState.calendar;
  const share = workloadOverlap[code] / 100;
  const mult = 2 - share;
  const morning = calendar.working_days * (means.morning_weekday_hours || 0) * mult;
  const weekend = calendar.holidays * (means.morning_weekend_hours || 0) * mult;
  const overlap = calendar.total_days * (means.overlap_hours || 0) * share;
  const evening = calendar.total_days * (means.evening_hours || 0) * mult;
  return { morning, weekend, overlap, evening, total: morning + weekend + overlap + evening };
}

function recalcWorkloadPoints() {
  if (!workloadState) return;
  let grand = 0;
  WORKLOAD_POINTS.forEach((config) => {
    const card = document.querySelector(`.workload-point[data-point="${config.code}"]`);
    if (!card) return;
    const numbers = pointNumbers(config.code);
    grand += numbers.total;
    const set = (selector, value) => { const node = card.querySelector(selector); if (node) node.textContent = value; };
    set('[data-point-morning]', workloadHours(numbers.morning));
    set('[data-point-weekend]', workloadHours(numbers.weekend));
    set('[data-point-overlap]', workloadHours(numbers.overlap));
    set('[data-point-evening]', workloadHours(numbers.evening));
    set('[data-point-total]', workloadHours(numbers.total));
    set('[data-point-share]', `${workloadOverlap[config.code]}%`);
  });
  workloadRequiredTotal = Math.round(grand);
  renderWorkloadRequired();
  // В режиме «по потребности» индикатор зависит от ползунков — обновляем и его.
  renderWorkloadIndicator();
}

function renderWorkloadRequired() {
  setText('#workload-required', workloadRequiredTotal);
  const grid = document.querySelector('#workload-quota-grid');
  if (!grid || !workloadState) return;
  const totals = workloadState.totals;
  // Квота считается от гипотетической потребности точек, а не от факта:
  // сколько команда закрывает своими минимумом/оптимумом/максимумом.
  const required = workloadRequiredTotal || 0;
  const item = (label, target) => {
    const percent = required > 0 ? Math.round((target / required) * 100) : 0;
    return `<div class="workload-quota-item"><span>${label}</span><strong>${percent}%</strong><small>${target} из ${required} ч</small></div>`;
  };
  grid.innerHTML = item('Σ Мин', totals.min) + item('Σ Опт', totals.opt) + item('Σ Макс', totals.max);
}

/* --- Нормо-часы: личный кабинет и диалог сотрудника -------------------- */

function applyWorkloadFields(form, values) {
  if (!form) return;
  const data = values || DEFAULT_WORKLOAD_HOURS;
  form.elements.min_hours.value = data.min_hours;
  form.elements.opt_hours.value = data.opt_hours;
  form.elements.max_hours.value = data.max_hours;
}

async function loadMyWorkload() {
  const form = document.querySelector('#workload-form');
  if (!form) return;
  const userId = document.body.dataset.userId;
  if (!userId) return;
  try {
    applyWorkloadFields(form, await api(`/api/users/${userId}/workload`));
  } catch (error) { applyWorkloadFields(form, null); }
}

function setupWorkloadForm() {
  const form = document.querySelector('#workload-form');
  if (!form) return;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const errorNode = document.querySelector('#workload-form-error');
    const note = document.querySelector('#workload-note');
    const showError = (message) => { if (errorNode) { errorNode.textContent = message; errorNode.hidden = !message; } };
    showError('');
    const userId = document.body.dataset.userId;
    if (!userId) return;
    const payload = {
      min_hours: form.elements.min_hours.value,
      opt_hours: form.elements.opt_hours.value,
      max_hours: form.elements.max_hours.value,
    };
    if (Number(payload.min_hours) > Number(payload.opt_hours) || Number(payload.opt_hours) > Number(payload.max_hours)) {
      showError('Нужно, чтобы минимум ≤ оптимум ≤ максимум');
      return;
    }
    const submit = document.querySelector('#workload-submit');
    if (submit) submit.disabled = true;
    try {
      await api(`/api/users/${userId}/workload`, { method: 'PATCH', body: JSON.stringify(payload) });
      if (note) note.textContent = 'Нормо-часы сохранены';
      showToast('Нормо-часы сохранены');
      if (workloadState) loadWorkload();
    } catch (requestError) { showError(requestError.message); }
    finally { if (submit) submit.disabled = false; }
  });
  loadMyWorkload();
}

async function loadUserWorkloadFields(form, userId) {
  try {
    applyWorkloadFields(form, await api(`/api/users/${userId}/workload`));
  } catch (error) { applyWorkloadFields(form, null); }
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
  if (!editMode && shiftDrag) cancelShiftDrag();
  document.body.classList.toggle('calendar-editing', editMode);
  const swapsEntry = document.querySelector('#edit-swaps-toggle');
  const scheduleEntry = document.querySelector('#edit-schedule-toggle');
  // Стажёр только смотрит график: сервер тоже отклонит правки (PermissionError).
  const canEdit = userRole !== 'newbie';
  if (swapsEntry) swapsEntry.hidden = editMode || !canEdit;
  if (scheduleEntry) scheduleEntry.hidden = editMode || !isMentor;
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

/* --- Перенос смен перетаскиванием ----------------------------------------
   Работает в режиме правок («Замены» и «Расписание») при режиме «Ячейка».
   Тянем заполненную ячейку на любую другую (другой человек или другой день):

     * цель пустая — смена переезжает, исходная становится выходным;
     * цель занята — смены меняются людьми (обмен).

   Правки идут в тот же черновик, что и обычные клики: на сервер они уходят
   только после «Сохранить»/«Сохранить изменения».
   На тач-устройствах перенос начинается с долгого нажатия, и пока смена
   «в руке», страница не прокручивается.
*/
const DRAG_HOLD_MS = 350;
const DRAG_TOUCH_SLOP = 12;
const DRAG_MOUSE_SLOP = 6;

let shiftDrag = null;
let suppressCellClick = false;

function employeeNameById(id) {
  return calendarState?.employees?.find((person) => person.id === id)?.name || '—';
}

function startShiftDrag(event, cell) {
  const source = { employeeId: Number(cell.dataset.employeeId), date: cell.dataset.cellDate };
  const shift = getEffectiveShift(source.employeeId, source.date);
  if (!shift) return false;
  const ghost = document.createElement('div');
  ghost.className = 'shift-drag-ghost';
  ghost.innerHTML = `<strong>${escapeHtml(shift.start)}–${escapeHtml(shift.end)}</strong><span>${escapeHtml(shift.point)}</span>`;
  ghost.hidden = true;
  document.body.appendChild(ghost);
  shiftDrag = {
    source, ghost, sourceEl: cell, targetEl: null, active: false,
    dragEl: cell.closest('table'),
    pointerId: event.pointerId, pointerType: event.pointerType,
    originX: event.clientX, originY: event.clientY, holdTimer: 0,
  };
  // Тач: перенос только после удержания, иначе это обычный скролл.
  if (event.pointerType === 'touch') {
    shiftDrag.holdTimer = window.setTimeout(() => {
      if (shiftDrag && !shiftDrag.active) activateShiftDrag();
    }, DRAG_HOLD_MS);
  }
  return true;
}

function activateShiftDrag() {
  if (!shiftDrag) return;
  shiftDrag.active = true;
  // Захват указателя ставим только когда перенос реально начался. Если захватить
  // на pointerdown, браузер перенаправит последующий click на таблицу матрицы,
  // и одиночный клик по заполненной ячейке (шаблон «Выходной», окно смены)
  // перестанет работать.
  try { shiftDrag.dragEl?.setPointerCapture(shiftDrag.pointerId); } catch (error) { /* указателя уже нет */ }
  shiftDrag.ghost.hidden = false;
  shiftDrag.sourceEl?.classList.add('is-drag-source');
  document.body.classList.add('shift-dragging');
  const status = document.querySelector('#matrix-status');
  if (status) status.textContent = 'Перенос смены: отпустите на нужной ячейке, Esc — отмена';
  if (navigator.vibrate) { try { navigator.vibrate(15); } catch (error) { /* не критично */ } }
}

function cancelShiftDrag() {
  const drag = shiftDrag;
  shiftDrag = null;
  if (!drag) return;
  window.clearTimeout(drag.holdTimer);
  try { drag.dragEl?.releasePointerCapture(drag.pointerId); } catch (error) { /* не был захвачен */ }
  drag.ghost.remove();
  drag.sourceEl?.classList.remove('is-drag-source');
  drag.targetEl?.classList.remove('is-drop-target');
  document.body.classList.remove('shift-dragging');
}

function updateDropTarget(x, y) {
  if (!shiftDrag) return;
  const cell = document.elementFromPoint(x, y)?.closest('[data-matrix-cell]') || null;
  if (shiftDrag.targetEl === cell) return;
  shiftDrag.targetEl?.classList.remove('is-drop-target');
  shiftDrag.targetEl = cell;
  cell?.classList.add('is-drop-target');
}

function finishShiftDrag(commit) {
  const drag = shiftDrag;
  if (!drag) return false;
  const wasActive = drag.active;
  const source = drag.source;
  const target = drag.targetEl
    ? { employeeId: Number(drag.targetEl.dataset.employeeId), date: drag.targetEl.dataset.cellDate }
    : null;
  cancelShiftDrag();
  if (!commit || !wasActive) return false;
  // Клик, который браузер шлёт после отпускания, не должен открывать окно смены.
  suppressCellClick = true;
  window.setTimeout(() => { suppressCellClick = false; }, 400);
  return target ? applyShiftDrop(source, target) : false;
}

function applyShiftDrop(source, target) {
  if (!calendarState) return false;
  if (source.employeeId === target.employeeId && source.date === target.date) return false;
  const from = getEffectiveShift(source.employeeId, source.date);
  if (!from) return false;
  const to = getEffectiveShift(target.employeeId, target.date);
  setDraftCell(target.employeeId, target.date,
    { start_time: from.start, end_time: from.end, point: from.point }, false);
  if (to) {
    setDraftCell(source.employeeId, source.date,
      { start_time: to.start, end_time: to.end, point: to.point }, false);
  } else {
    setDraftCell(source.employeeId, source.date, null, false);
  }
  renderCalendar(calendarState);
  updateEditControls();
  const fromWho = `${employeeNameById(source.employeeId)} ${displayDate(source.date)}`;
  const toWho = `${employeeNameById(target.employeeId)} ${displayDate(target.date)}`;
  const status = document.querySelector('#matrix-status');
  if (status) status.textContent = to
    ? `Обмен сменами: ${fromWho} ⇄ ${toWho}`
    : `Перенос смены: ${fromWho} → ${toWho}`;
  return true;
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

/* Модули: разделы выключаются глобально (флаг в web_app_settings). Сервер уже
   не рендерит выключенные разделы, а здесь держим состояние в актуальном виде
   после переключений в панели управления. */
function applyModuleVisibility() {
  for (const module of Object.values(modulesState)) {
    if (!module.view) continue;
    document.querySelectorAll(`[data-view="${module.view}"]`).forEach((button) => { button.hidden = !module.enabled; });
    if (!module.enabled && document.querySelector(`#${module.view}-view`)?.classList.contains('is-visible')) setView('profile');
  }
  const summary = document.querySelector('#calendar-summary');
  if (summary && modulesState.calendar) summary.hidden = !modulesState.calendar.enabled;
}

async function loadModules() {
  try {
    const data = await api('/api/modules');
    modulesState = {};
    (data.modules || []).forEach((module) => { modulesState[module.code] = module; });
    applyModuleVisibility();
    renderControlNav();
  } catch (error) { /* навигация остаётся серверной — это не критично */ }
}

async function loadPreferences() {
  try {
    applyServerAppearance(await api('/api/preferences'));
  } catch (error) { showToast(error.message); }
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

/* Флаги типа смены: по ним «Расчет часов» раскладывает нагрузку УЯ/ДЕ. */
const TEMPLATE_FLAG_LABELS = [
  ['is_morning', 'Утро'],
  ['is_evening', 'Вечер'],
  ['is_overlap', 'Пересмен'],
  ['is_weekend', 'Выходные'],
];

function templateFlagTags(template) {
  return TEMPLATE_FLAG_LABELS
    .filter(([key]) => template[key])
    .map(([, label]) => `<i class="template-flag">${label}</i>`)
    .join('');
}

function readTemplateFlags(form) {
  const flags = {};
  TEMPLATE_FLAG_LABELS.forEach(([key]) => {
    const input = form.elements[key];
    flags[key] = Boolean(input && input.checked);
  });
  return flags;
}

function writeTemplateFlags(form, template) {
  TEMPLATE_FLAG_LABELS.forEach(([key]) => {
    const input = form.elements[key];
    if (input) input.checked = Boolean(template && template[key]);
  });
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
      <span class="template-row-meta">${escapeHtml(template.start)}–${escapeHtml(template.end)} · ${escapeHtml(template.point)}${templateFlagTags(template)}</span>
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
  writeTemplateFlags(form, null);
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
  writeTemplateFlags(form, template);
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
      ...readTemplateFlags(form),
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
  const year = calendarCursor.getFullYear();
  const month = calendarCursor.getMonth() + 1;
  const cacheKey = `${CACHE_KEY_CALENDAR}:${year}-${month}`;

  // Офлайн-режим: сначала мгновенно показываем сохранённую копию графика.
  const cached = readCache(cacheKey);
  if (cached && cached.year === year && cached.month === month) {
    calendarState = cached;
    calendarAutoScroll = true;
    renderCalendar(calendarState);
  }

  try {
    const data = await api(`/api/calendar?year=${year}&month=${month}`);
    calendarState = data;
    writeCache(cacheKey, data);
    writeCache(CACHE_KEY_LAST_SYNC, Date.now());
    calendarAutoScroll = true;
    renderCalendar(calendarState);
    await loadShiftHistory();
  } catch (error) {
    if (error.isOffline) {
      setNetworkStatus(false, lastSyncTimestamp());
      showToast('Связь с сервером потеряна. Показана сохраненная копия');
      return;
    }
    showToast(error.message);
  }
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
  const targetModule = Object.values(modulesState).find((module) => module.view === name);
  if (targetModule && !targetModule.enabled) name = 'profile';
  if (name === 'control' && !managerRole) name = 'profile';
  if (name === 'analytics' && !managerRole) name = 'profile';
  const apply = () => {
    const isChat = name === 'chat';
    // В режиме чата документ прибит к видимой области (см. app.css) —
    // прокрутки у него нет, поэтому и scrollTo(0, 0) больше не нужен.
    document.documentElement.classList.toggle('is-chat-open', isChat);
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
        name === 'development' ? 'Развитие' :
        name === 'control' ? 'Панель управления' :
        name === 'analytics' ? 'Аналитика команды' :
        name === 'announcements' ? 'Анонсы' :
        name === 'kb' ? 'База знаний' :
        name === 'drinks' ? 'Описание напитков' :
        name === 'chat' ? 'Чат' :
        name === 'profile' ? 'Личный кабинет' :
        `Добрый день, ${title.dataset.name}`;
    }
    document.body.classList.toggle('settings-active', name === 'profile' || name === 'control');
    // Прогресс мог измениться, пока сотрудник был в другом разделе.
    if (name === 'development') {
      loadDevelopment();
      loadEvaluationAnalytics();
      if (managerRole && document.querySelector('#quality-panel')?.hidden === false) loadQualitySessions();
    }
    if (!isChat) window.scrollTo({ top: 0, behavior: 'smooth' });
  };
  // View Transitions и стекло конфликтуют: пока идёт переход (~0.26s), элементы с
  // backdrop-filter не могут семплировать реальный фон и рисуются без блюра — он
  // «догружается» уже после завершения. В режиме стекла меняем раздел сразу.
  const canTransition = typeof document.startViewTransition === 'function'
    && !prefersReducedMotion()
    && !glassEnabled();
  if (canTransition) {
    document.startViewTransition(apply);
  } else {
    apply();
  }
  if (name === 'team') {
    loadUsers();
    // Возврат на «Расчет часов» — освежаем цифры, они могли измениться.
    if (document.querySelector('#team-workload-panel')?.hidden === false) loadWorkload();
  }
  if (name === 'chat') window.NeftChat?.open();
  if (name === 'announcements') openAnnouncements();
  if (name === 'kb') openKnowledgeBase();
  else kbUpdateTreeFab();
  if (name === 'drinks') openDrinks();
  if (name === 'calendar') loadCalendar();
  if (name === 'control' && managerRole) { loadStoriesAdmin(); renderAnnouncementCategories(); ensureControlSelection(); }
  if (name === 'analytics') loadAnalytics();
  closeDrawer();
}

/* --- Модерация историй гостей -------------------------------------------
   Публичная страница живёт по прямой ссылке /stories и в навигации её нет.
   Здесь только инструменты наставника: публикация, правка и удаление. */

const storiesAdmin = { filter: 'all', items: [], stats: null, settings: null };

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
  card.draggable = true;

  const textLimit = (storiesAdmin.settings && storiesAdmin.settings.max_text_length) || 600;

  card.innerHTML = `
    <div class="story-admin-main">
      <div class="story-admin-thumb${story.photo ? '' : ' is-empty'}">
        ${story.photo ? `<img src="${escapeHtml(story.photo)}" alt="Фото из истории" loading="lazy" decoding="async">` : ''}
      </div>
      <div class="story-admin-body">
        <div class="story-admin-head">
          <span class="story-admin-drag" title="Перетащите, чтобы изменить порядок" aria-hidden="true">⠿</span>
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
      <label>Текст истории<textarea name="text" maxlength="${textLimit}" rows="5">${escapeHtml(story.text)}</textarea></label>
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
    // Пока открыт редактор, карточку не перетаскиваем — иначе выделять текст
    // в textarea неудобно.
    card.draggable = false;
    editForm.elements.text.focus();
  });

  card.querySelector('[data-story-cancel]').addEventListener('click', () => {
    editForm.hidden = true;
    card.draggable = true;
  });
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
    storiesAdmin.settings = data.settings || null;
    fillStoriesSettingsForm();
    if (status) status.textContent = '';
    renderStoriesAdmin();
  } catch (error) {
    if (status) status.textContent = error.message;
  }
}

/* --- Настройки страницы историй (скорость прокрутки, лимит текста) -------- */

function fillStoriesSettingsForm() {
  const form = document.querySelector('#stories-settings-form');
  if (!form || !storiesAdmin.settings) return;
  form.elements.autoplay_ms.value = storiesAdmin.settings.autoplay_ms;
  form.elements.max_text_length.value = storiesAdmin.settings.max_text_length;
  form.elements.carousel_width.value = storiesAdmin.settings.carousel_width;
  form.elements.carousel_width_auto.checked = storiesAdmin.settings.carousel_width_auto === true;
  syncStoriesWidthInput();
}

/* Пока включена «адаптивная ширина», поле с числом заблокировано. */
function syncStoriesWidthInput() {
  const form = document.querySelector('#stories-settings-form');
  if (!form) return;
  form.elements.carousel_width.disabled = form.elements.carousel_width_auto.checked;
}

function setupStoriesSettings() {
  const form = document.querySelector('#stories-settings-form');
  if (!form) return;
  form.elements.carousel_width_auto.addEventListener('change', syncStoriesWidthInput);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const error = document.querySelector('#stories-settings-error');
    const showError = (message) => { if (error) { error.textContent = message; error.hidden = !message; } };
    showError('');
    const payload = {
      autoplay_ms: Number(form.elements.autoplay_ms.value),
      max_text_length: Number(form.elements.max_text_length.value),
      carousel_width: Number(form.elements.carousel_width.value),
      carousel_width_auto: form.elements.carousel_width_auto.checked,
    };
    // Ширина нужна только в ручном режиме: в адаптивном она задана формулой.
    if (!Number.isFinite(payload.autoplay_ms) || !Number.isFinite(payload.max_text_length)
      || (!payload.carousel_width_auto && !(payload.carousel_width > 0))) {
      showError('Укажите числа'); return;
    }
    try {
      const data = await api('/api/stories/moderation/settings', { method: 'PATCH', body: JSON.stringify(payload) });
      storiesAdmin.settings = data.settings || storiesAdmin.settings;
      fillStoriesSettingsForm();
      renderStoriesAdmin();          // обновляем maxlength в формах правки
      showToast('Настройки историй сохранены');
    } catch (requestError) { showError(requestError.message); }
  });
}

/* --- Порядок историй: drag-and-drop (по аналогии с шаблонами смен) -------- */

function setupStoriesAdminDragAndDrop() {
  const list = document.querySelector('#stories-admin-list');
  if (!list) return;
  let dragged = null;
  list.addEventListener('dragstart', (event) => {
    const row = event.target.closest('[data-story-id]');
    if (!row) return;
    dragged = row;
    row.classList.add('is-dragging');
    event.dataTransfer.effectAllowed = 'move';
    try { event.dataTransfer.setData('text/plain', row.dataset.storyId); } catch (error) { /* Safari */ }
  });
  list.addEventListener('dragover', (event) => {
    if (!dragged) return;
    event.preventDefault();
    list.querySelectorAll('.is-drop-target').forEach((item) => item.classList.remove('is-drop-target'));
    const row = event.target.closest('[data-story-id]');
    if (row && row !== dragged) row.classList.add('is-drop-target');
  });
  list.addEventListener('drop', async (event) => {
    if (!dragged) return;
    event.preventDefault();
    const target = event.target.closest('[data-story-id]');
    if (target && target !== dragged) {
      const rect = target.getBoundingClientRect();
      const insertAfter = event.clientY > rect.top + rect.height / 2;
      list.insertBefore(dragged, insertAfter ? target.nextSibling : target);
      await persistStoriesOrder();
    }
  });
  list.addEventListener('dragend', () => {
    if (dragged) dragged.classList.remove('is-dragging');
    list.querySelectorAll('.is-drop-target').forEach((item) => item.classList.remove('is-drop-target'));
    dragged = null;
  });
}

async function persistStoriesOrder() {
  const list = document.querySelector('#stories-admin-list');
  if (!list) return;
  const order = [...list.querySelectorAll('[data-story-id]')].map((row) => row.dataset.storyId);
  try {
    const data = await api('/api/stories/moderation/order', { method: 'PATCH', body: JSON.stringify({ order }) });
    storiesAdmin.items = data.stories || storiesAdmin.items;
    if (data.stats) storiesAdmin.stats = data.stats;
    showToast('Порядок историй сохранён');
  } catch (error) { showToast(error.message); }
}

function setupStoriesAdmin() {
  document.querySelectorAll('[data-stories-filter]').forEach((button) => {
    button.addEventListener('click', () => {
      storiesAdmin.filter = button.dataset.storiesFilter;
      document.querySelectorAll('[data-stories-filter]').forEach((item) => item.classList.toggle('is-active', item === button));
      renderStoriesAdmin();
    });
  });
  setupStoriesSettings();
  setupStoriesAdminDragAndDrop();
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
const GLASS_KEY = 'neft-glass';

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

/* Liquid Glass — основной вид интерфейса. Галочка в личном кабинете включает
   «классическую тему» (стекло выключено); выбор живёт на этом устройстве
   (localStorage 'neft-glass'), а флаг data-glass="on" до первой отрисовки
   ставит theme-boot.js. Оформление — в CSS, фон — в static/glass-bg.js. */
function glassEnabled() { return storedItem(GLASS_KEY) !== 'false'; }

function applyGlassPreference(on) {
  if (on) document.documentElement.setAttribute('data-glass', 'on');
  else document.documentElement.removeAttribute('data-glass');
}

function setupGlass() {
  const box = document.querySelector('#classic-theme-toggle');
  if (box) {
    box.checked = !glassEnabled();
    box.addEventListener('change', () => {
      applyGlassPreference(!box.checked);
      storeItem(GLASS_KEY, box.checked ? 'false' : 'true');
    });
  }
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
  setupGlass();
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

/* --- Сэндвич-drawer / боковая навигация ---------------------------------- */

const SIDEBAR_PINNED_QUERY = '(min-width: 1024px)';

function isSidebarPinned() {
  return window.matchMedia(SIDEBAR_PINNED_QUERY).matches;
}

function closeDrawer() {
  const drawer = document.querySelector('#drawer');
  if (!drawer) return;
  if (isSidebarPinned()) {
    drawer.setAttribute('aria-hidden', 'false');
    document.querySelector('#drawer-backdrop')?.classList.remove('is-open');
    document.body.classList.remove('drawer-open');
    return;
  }
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
  const media = window.matchMedia(SIDEBAR_PINNED_QUERY);

  const syncSidebarMode = () => {
    const pinned = media.matches;
    document.body.classList.toggle('is-sidebar-pinned', pinned);
    if (pinned) {
      drawer.classList.remove('is-open');
      drawer.setAttribute('aria-hidden', 'false');
      backdrop.classList.remove('is-open');
      openButton.setAttribute('aria-expanded', 'false');
      document.body.classList.remove('drawer-open');
    } else if (!drawer.classList.contains('is-open')) {
      drawer.setAttribute('aria-hidden', 'true');
    }
  };

  const open = () => {
    if (isSidebarPinned()) return;
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
    if (isSidebarPinned()) return;
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

  // Закрытие свайпом влево (только в режиме offcanvas).
  let startX = null;
  drawer.addEventListener('pointerdown', (event) => {
    if (isSidebarPinned()) { startX = null; return; }
    startX = event.clientX;
  });
  drawer.addEventListener('pointerup', (event) => {
    if (!isSidebarPinned() && startX !== null && startX - event.clientX > 60) closeDrawer();
    startX = null;
  });

  syncSidebarMode();
  if (media.addEventListener) media.addEventListener('change', syncSidebarMode);
  else media.addListener(syncSidebarMode);
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

/* Текст анонса: экранируем, затем подставляем ссылки на статьи БЗ и обычные
   URL. `white-space: pre-line` на карточке сохраняет переводы строк как есть. */
function announcementBodyMarkup(text) {
  if (!text) return '';
  return kbLinkify(escapeHtml(text))
    .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
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
      ${item.body ? `<p class="announcement-body">${announcementBodyMarkup(item.body)}</p>` : ''}
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

/* Закреплённые держим всегда, остальное — за последнюю неделю.
   Превью на «Обзоре» — та же лента, поэтому сначала уважаем выбранный
   фильтр, и только потом отсекаем старое. */
function slideshowItems() {
  const weekAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
  return visibleAnnouncements(0).filter((item) => {
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
  slideshowGoTo(slideshow.index + step);
  startSlideshow();
}

/* Узел трека слайд-шоу. */
function slideshowTrack() {
  return document.querySelector('#announcements-preview-list .slideshow-track');
}

/* Сдвигаем трек на нужную карточку. При drag (animate=false) карточка следует
   за пальцем без transition — иначе была бы вязкая «резинка». */
function moveTrack(offset, animate) {
  const track = slideshowTrack();
  if (!track) return;
  track.classList.toggle('is-dragging', !animate);
  const width = track.offsetWidth || 0;
  track.style.transform = `translateX(${offset - slideshow.index * width}px)`;
}

/* Высота вьюпорта = высота активной карточки: соседние панели плавно
   выезжают наверх/вниз (transition: height в CSS). */
function syncPreviewHeight() {
  const preview = document.querySelector('#announcements-preview-list');
  const viewport = preview && preview.querySelector('.slideshow-viewport');
  const track = preview && preview.querySelector('.slideshow-track');
  if (!viewport || !track) return;
  const active = track.children[slideshow.index];
  const card = active && active.firstElementChild;
  if (!card) return;
  const apply = () => { viewport.style.height = card.offsetHeight + 'px'; };
  apply();
  requestAnimationFrame(apply);   // и ещё раз после догрузки картинок/перерисовки
}

/* Переключение слайдов только классами — без перерисовки DOM, иначе анимация
   cover-scale не успевала бы проиграться. */
function updateSlides() {
  const track = slideshowTrack();
  if (!track) return;
  const slides = [...track.children];
  const total = slides.length;
  slides.forEach((slide, index) => {
    const active = index === slideshow.index;
    slide.classList.toggle('is-active', active);
    slide.classList.toggle('is-prev', index < slideshow.index);
    slide.classList.toggle('is-next', index > slideshow.index);
    slide.setAttribute('aria-hidden', active ? 'false' : 'true');
  });
  const count = document.querySelector('#announcements-preview-list [data-slide-count]');
  if (count) count.textContent = `${slideshow.index + 1} / ${total}`;
  document.querySelectorAll('#announcements-preview-list .slideshow-dot').forEach((dot, index) => {
    const active = index === slideshow.index;
    dot.classList.toggle('is-active', active);
    dot.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  moveTrack(0, true);
  syncPreviewHeight();
}

/* Перейти к слайду по индексу (с зацикливанием) без перерисовки DOM. */
function slideshowGoTo(index) {
  const total = slideshow.items.length || 1;
  slideshow.index = ((index % total) + total) % total;
  updateSlides();
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
  const slides = slideshow.items
    .map((item) => `<article class="announcement-slide">${announcementMarkup(item)}</article>`)
    .join('');
  const nav = total > 1
    ? `<div class="slideshow-nav"><button class="icon-button" type="button" data-slide-step="-1" aria-label="Предыдущий анонс"><svg class="icon" aria-hidden="true"><use href="#i-chevron-left"></use></svg></button><span class="slideshow-count" data-slide-count>${slideshow.index + 1} / ${total}</span><button class="icon-button" type="button" data-slide-step="1" aria-label="Следующий анонс"><svg class="icon" aria-hidden="true"><use href="#i-chevron-right"></use></svg></button></div>${'<div class="slideshow-dots">' + slideshow.items.map((item, index) => `<button type="button" class="slideshow-dot" data-slide="${index}" aria-label="Анонс ${index + 1}"></button>`).join('') + '</div>'}`
    : '';
  node.innerHTML = `<div class="slideshow"><div class="slideshow-viewport"><div class="slideshow-track">${slides}</div></div>${nav}</div>`;
  updateSlides();
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
  clearDraftNotice(form);
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
  // Черновик восстанавливаем только для нового анонса (правку не подменяем).
  const draft = item ? null : readDraft('announcement');
  if (draft) {
    applyFields(form, draft.fields);
    renderDraftNotice(form, 'announcement', () => form.reset());
  }
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
  // Целимся сначала в полный список: в превью на «Обзоре» те же id, но
  // карточка может быть на невидимом слайде, и подсветка была бы не видна.
  const target = document.querySelector(`#announcements-list [data-announcement-id="${id}"]`)
    || document.querySelector(`[data-announcement-id="${id}"]`);
  if (!target) return;
  target.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'center' });
  target.classList.add('is-highlighted');
  setTimeout(() => target.classList.remove('is-highlighted'), 2200);
}

async function loadAnnouncements() {
  // Офлайн-режим: мгновенно показываем сохранённую копию анонсов.
  const cached = readCache(CACHE_KEY_ANNOUNCEMENTS);
  if (cached && Array.isArray(cached.items)) {
    announcementsState.items = cached.items;
    announcementsState.unread = cached.unread || announcementsState.unread;
    announcementsState.categories = cached.categories || [];
    announcementsState.canPublish = Boolean(cached.canPublish);
    renderAnnouncements();
    renderAnnouncementCategories();
  }

  try {
    const data = await api('/api/announcements?limit=50');
    announcementsState.items = data.items || [];
    announcementsState.unread = data.unread || announcementsState.unread;
    announcementsState.categories = data.categories || [];
    announcementsState.canPublish = Boolean(data.can_publish);
    writeCache(CACHE_KEY_ANNOUNCEMENTS, {
      items: announcementsState.items,
      unread: announcementsState.unread,
      categories: announcementsState.categories,
      canPublish: announcementsState.canPublish,
    });
    writeCache(CACHE_KEY_LAST_SYNC, Date.now());
    renderAnnouncements();
    renderAnnouncementCategories();
    return true;
  } catch (error) {
    if (error.isOffline) {
      setNetworkStatus(false, lastSyncTimestamp());
      showToast('Связь с сервером потеряна. Показана сохраненная копия');
    } else {
      showToast(error.message);
    }
    return false;
  }
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
    const page = params.get('page');
    const view = params.get('view');
    const share = params.get('share');
    const shareError = params.get('share_error');
    // chat.js грузится после app.js, поэтому запоминаем разобранную ссылку для него
    // (адрес к этому моменту уже очищен).
    if (window.Neft) {
      window.Neft.deepLink = {
        view: view || '',
        announcement: announcement ? Number(announcement) : null,
        peer: params.get('peer') || '',
        room: params.get('room') || '',
        share: share || '',
        shareError: shareError || '',
      };
    }
    if (announcement) openAnnouncementCard(Number(announcement));
    else if (page) openKbPageFromLink(Number(page));
    else if (view) setView(view);
    else if (!share && !shareError) return;
    // Убираем служебные параметры из адреса, чтобы они не тянулись за навигацией.
    // Заявку из «Поделиться» оставляем: её читает chat.js (он грузится после app.js).
    const keep = new URLSearchParams();
    if (share) keep.set('share', share);
    if (shareError) keep.set('share_error', shareError);
    const tail = keep.toString();
    window.history.replaceState({}, '', window.location.pathname + (tail ? `?${tail}` : ''));
  } catch (error) { /* некорректная ссылка — просто остаёмся на месте */ }
}

/* Открытие раздела = «я посмотрел ленту»: снимаем счётчик, но показываем
   непрочитанное в самом списке, чтобы не терять контекст. */
async function openAnnouncements() {
  const fresh = await loadAnnouncements();
  // «Прочитано» фиксируем только при живом соединении: в офлайне POST не пройдёт
  // и просто зашумит вторым тостом поверх «показана сохраненная копия».
  if (fresh && Number((announcementsState.unread || {}).total || 0) > 0) await markAnnouncementsRead();
}

function setupAnnouncements() {
  document.addEventListener('click', async (event) => {
    const step = event.target.closest('[data-slide-step]');
    if (step) { shiftSlide(Number(step.dataset.slideStep) || 1); return; }

    const dot = event.target.closest('[data-slide]');
    if (dot) { slideshowGoTo(Number(dot.dataset.slide) || 0); startSlideshow(); return; }

    const filter = event.target.closest('[data-ann-filter]');
    if (filter) {
      announcementsState.filter = filter.dataset.annFilter;
      // Смена фильтра меняет состав превью — начинаем с первого анонса.
      slideshow.index = 0;
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
      if (!id) clearDraft('announcement');
      dialog?.close();
      await loadAnnouncements();
      showToast(id ? 'Анонс обновлён' : 'Анонс опубликован');
    } catch (error) {
      if (errorNode) { errorNode.textContent = error.message; errorNode.hidden = false; }
    }
  });
  // Закрытие — только кнопками .dialog-close: клик по краю окна не должен
  // закрывать форму (на десктопе это сплошные случайные промахи).
  document.querySelectorAll('#announcement-dialog .dialog-close').forEach((button) => button.addEventListener('click', () => dialog?.close()));
  // Ссылки на статьи: `@` в тексте анонса ищет страницу базы знаний.
  attachPageLinkSuggest(document.querySelector('#announcement-form textarea[name="body"]'), 'textarea');
  dialog?.addEventListener('close', pageLinkClose);

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

  // Свайп по слайд-шоу (как в Instagram): карточка следует за пальцем, а на
  // отпускании дощёлкивает к соседней. Пока тянем — автослайд на паузе.
  const preview = document.querySelector('#announcements-preview-list');
  if (preview) {
    const DRAG_SLOP = 8;        // порог, после которого начинаем тянуть карточку
    const SNAP_FRACTION = 0.25; // доля ширины, достаточная для перелистывания
    const FLICK = 0.5;          // px/мс — быстрый флик листает без полного сдвига
    let drag = null;
    let suppressClick = false;  // свайп не должен превращаться в клик по карточке

    const slideWidth = () => (slideshowTrack()?.offsetWidth || preview.clientWidth || 1);

    preview.addEventListener('click', (event) => {
      if (!suppressClick) return;
      event.preventDefault();
      event.stopPropagation();
      suppressClick = false;
    }, { capture: true });

    preview.addEventListener('pointerdown', (event) => {
      if (event.button) { drag = null; return; }          // только основная кнопка/касание
      if (!event.target.closest('.slideshow-viewport')) { drag = null; return; }
      if (event.target.closest('a, button')) { drag = null; return; }
      drag = { x: event.clientX, y: event.clientY, axis: null, lastX: event.clientX, lastT: performance.now() };
      slideshow.paused = true;
      stopSlideshow();
      preview.setPointerCapture?.(event.pointerId);
    });

    preview.addEventListener('pointermove', (event) => {
      if (!drag) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (!drag.axis) {
        if (Math.abs(dx) < DRAG_SLOP && Math.abs(dy) < DRAG_SLOP) return;
        drag.axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
      }
      if (drag.axis !== 'x') return;                      // вертикаль отдаём скроллу
      event.preventDefault();
      drag.lastX = event.clientX;
      drag.lastT = performance.now();
      moveTrack(dx, false);
    });

    const settle = (event) => {
      if (!drag) return;
      const state = drag;
      drag = null;
      slideshow.paused = false;
      if (state.axis === 'x') {
        const endX = event ? event.clientX : state.lastX;
        const dx = endX - state.x;
        const dt = Math.max(1, performance.now() - state.lastT);
        const velocity = (endX - state.lastX) / dt;
        const passed = Math.abs(dx) > slideWidth() * SNAP_FRACTION || Math.abs(velocity) > FLICK;
        if (passed) {
          const goNext = Math.abs(velocity) > FLICK ? velocity < 0 : dx < 0;
          slideshowGoTo(slideshow.index + (goNext ? 1 : -1));
        } else {
          updateSlides();                                 // откат к текущей карточке
        }
        // Свайп не должен превращаться в клик по карточке.
        if (Math.abs(dx) > DRAG_SLOP) {
          suppressClick = true;
          setTimeout(() => { suppressClick = false; }, 400);
        }
      } else {
        updateSlides();
      }
      startSlideshow();
    };

    preview.addEventListener('pointerup', settle);
    preview.addEventListener('pointercancel', () => settle(null));
  }
  // При смене ширины карточка переливается — пересчитываем высоту вьюпорта.
  let previewResizeRaf = 0;
  window.addEventListener('resize', () => {
    if (previewResizeRaf) return;
    previewResizeRaf = requestAnimationFrame(() => { previewResizeRaf = 0; syncPreviewHeight(); });
  });
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

/* --- База знаний (Notion-lite) --------------------------------------------
   Разделы задаются на сервере (настройка «kb» в панели управления): «Зерно»
   (beans) и «Напитки» (drinks) — системные (привязаны к другим модулям),
   остальные — кастомные. Список приходит в `/api/kb/pages` (`sections`) и
   хранится в `kbState.sections`; KB_SECTIONS — запасной набор до первой
   загрузки. Дерево строится из parent_id, чтение доступно всем сотрудникам,
   а правка — только senior/mentor (managerRole); запись повторно проверяет
   бэкенд. */

const CACHE_KEY_KB_LIST = 'neft-cache:kb-list';
const CACHE_KEY_KB_PAGE = 'neft-cache:kb-page';

// Системные ключи совпадают с серверными (SYSTEM_SECTIONS); кастомные разделы
// подтянутся из `/api/kb/pages`. Здесь — запасной набор для первого (в т.ч.
// офлайн) показа, пока серверный список ещё не получен.
const KB_SECTIONS = [
  { key: 'wiki', label: 'Вики', system: false },
  { key: 'standards', label: 'Стандарты', system: false },
  { key: 'beans', label: 'Зерно', system: true },
  { key: 'drinks', label: 'Напитки', system: true },
];
const KB_SYSTEM_KEYS = ['beans', 'drinks'];
const KB_CATEGORY_ALL = 'all';
const KB_ROAST_LABELS = { espresso: 'Эспрессо', filter: 'Фильтр', omni: 'Омни' };

/* Палитра Pantone из ТЗ. hex — приблизительный, только для свотча; значения
   свои (не пользовательские), поэтому в inline-style подставляются безопасно. */
const KB_PANTONE = [
  { value: 'Pantone 178 C', hex: '#f36b5f' },
  { value: 'Pantone 153 C', hex: '#a35c27' },
  { value: 'Pantone 7406 C', hex: '#e3a82b' },
  { value: 'Pantone 1485 C', hex: '#f0a06b' },
  { value: 'Pantone 7499 C', hex: '#f2f0d6' },
  { value: 'Pantone 7746 C', hex: '#a8b33d' },
  { value: 'Pantone 359 C', hex: '#77bd4a' },
  { value: 'Pantone 325 C', hex: '#4cbfa9' },
  { value: 'Pantone 277 C', hex: '#9fb8de' },
  { value: 'Pantone 2577 C', hex: '#c39cd0' },
  { value: 'Pantone 517 C', hex: '#e8c4d4' },
  { value: 'Pantone 191 C', hex: '#f0658a' },
  { value: 'Pantone 423 C', hex: '#92938d' },
];
const KB_PANTONE_OTHER = 'другое';
// Оглавление: раскрытые разделы/узлы запоминаем в localStorage, максимум тегов
// и длина одной метки совпадают с лимитами бэкенда (`MAX_TAGS`/`MAX_TAG`).
const KB_EXPANDED_KEY = 'neft-kb-expanded';
const KB_TREE_OPEN_KEY = 'neft-kb-tree-open';
const KB_MAX_TAGS = 12;
const KB_MAX_TAG_LEN = 40;

const kbState = {
  items: [],
  sections: KB_SECTIONS.slice(),
  categories: KB_SECTIONS.map((item) => item.key),
  filter: KB_CATEGORY_ALL,
  query: '',
  selectedId: null,
  current: null,
  // Страница, которую нужно открыть сразу после загрузки оглавления (переход по
  // ссылке `@[Заголовок](kb:12)` из анонса или чата).
  pendingId: null,
  recipesPageId: null,
  ready: false,
  canEdit: managerRole,
  expanded: new Set(),
  openCategories: new Set(),
  expandedLoaded: false,
  treeOpen: true,
};

/* Карусель страниц базы знаний: свайп/стрелки открывают соседнюю страницу из
   оглавления со слайд-анимацией. Автосмены нет. `busy` — идёт переход и загрузка,
   `pending` — направление для первого рендера после перехода. */
const kbNav = { busy: false, pending: 0 };

function kbCategoryLabel(key) {
  const found = kbState.sections.find((item) => item.key === key);
  return found ? found.label : key;
}

/* Системный раздел («Зерно»/«Напитки»): у него своя карточка и отдельные поля;
   кастомные разделы — обычный текстовый раздел с редактором. */
function kbIsSystemSection(key) {
  return KB_SYSTEM_KEYS.includes(key);
}

/* Применяет разделы с сервера: подписи для фильтров/заголовков и ключи для
   фильтрации дерева. Пустой список игнорируем — остаётся текущий (запасной). */
function kbApplySections(sections) {
  if (!Array.isArray(sections) || !sections.length) return;
  kbState.sections = sections
    .filter((item) => item && item.key)
    .map((item) => ({ key: String(item.key), label: item.label || String(item.key), system: Boolean(item.system) }));
  kbState.categories = kbState.sections.map((item) => item.key);
}

/* Пересобирает выпадающий список разделов формы (источник — kbState.sections). */
function kbRenderCategorySelect() {
  const select = document.querySelector('#kb-field-category');
  if (!select) return;
  const previous = select.value;
  select.innerHTML = kbState.sections
    .map((item) => `<option value="${escapeHtml(item.key)}">${escapeHtml(item.label)}</option>`)
    .join('');
  if (kbState.sections.some((item) => item.key === previous)) select.value = previous;
  else select.selectedIndex = 0;
}

function kbPantoneHex(value) {
  if (typeof value === 'string' && /^#[0-9a-f]{3,8}$/i.test(value.trim())) return value.trim();
  const found = KB_PANTONE.find((item) => item.value === value);
  return found ? found.hex : '#92938d';
}

/* Краткая метка справочника рядом с пунктом дерева: свотч обжарки — для зерна,
   категория — для напитка. */
function kbReferenceBadge(reference) {
  if (!reference) return '';
  if (reference.type === 'beans') {
    const roast = kbRoastLabel(reference.roast_type) || reference.roast_type || '';
    return `<span class="kb-ref"><i class="kb-swatch" style="--kb-swatch:${kbPantoneHex(reference.pantone_color)}"></i>${escapeHtml(roast)}</span>`;
  }
  if (reference.type === 'drinks') {
    return `<span class="kb-ref kb-ref-plain">${escapeHtml(reference.category || '')}</span>`;
  }
  return '';
}

function kbFilterChips() {
  const node = document.querySelector('#kb-filters');
  if (!node) return;
  const chips = [`<button class="chip ${kbState.filter === KB_CATEGORY_ALL ? 'is-selected' : ''}" type="button" data-kb-filter="${KB_CATEGORY_ALL}">Все</button>`];
  for (const key of kbState.categories) {
    chips.push(`<button class="chip ${kbState.filter === key ? 'is-selected' : ''}" type="button" data-kb-filter="${key}">${escapeHtml(kbCategoryLabel(key))}</button>`);
  }
  node.innerHTML = chips.join('');
}

/* Подсветка совпадений поиска: экранируем текст и оборачиваем вхождения
   в <mark>. Пустой запрос — просто экранированный текст. */
function kbHighlight(value, query) {
  const text = String(value == null ? '' : value);
  const term = String(query || '').trim();
  if (!term) return escapeHtml(text);
  const lower = text.toLowerCase();
  const needle = term.toLowerCase();
  let result = '';
  let cursor = 0;
  let index = lower.indexOf(needle);
  while (index !== -1) {
    result += escapeHtml(text.slice(cursor, index));
    result += `<mark class="kb-mark">${escapeHtml(text.slice(index, index + needle.length))}</mark>`;
    cursor = index + needle.length;
    index = lower.indexOf(needle, cursor);
  }
  return result + escapeHtml(text.slice(cursor));
}

/* --- Оглавление: раскрытие разделов и узлов (fold down) ------------------- */

function kbLoadExpanded() {
  if (kbState.expandedLoaded) return;
  kbState.expandedLoaded = true;
  let stored = null;
  try { stored = JSON.parse(localStorage.getItem(KB_EXPANDED_KEY) || 'null'); } catch (error) { stored = null; }
  if (stored && typeof stored === 'object') {
    if (Array.isArray(stored.nodes)) kbState.expanded = new Set(stored.nodes.map(Number));
    if (Array.isArray(stored.categories)) kbState.openCategories = new Set(stored.categories.map(String));
  } else {
    // Первый вход: разворачиваем все разделы, чтобы оглавление не казалось пустым.
    kbState.openCategories = new Set(kbState.categories);
  }
}

function kbSaveExpanded() {
  try {
    localStorage.setItem(KB_EXPANDED_KEY, JSON.stringify({
      nodes: [...kbState.expanded],
      categories: [...kbState.openCategories],
    }));
  } catch (error) { /* приватный режим — просто не запоминаем */ }
}

function kbToggleNode(id) {
  const open = !kbState.expanded.has(id);
  if (open) kbState.expanded.add(id); else kbState.expanded.delete(id);
  kbSaveExpanded();
  // Класс переключаем на уже существующем узле, а не перерисовываем дерево:
  // при перерисовке элемент рождается сразу с нужным классом и CSS-переход
  // `grid-template-rows` не запускается (нет анимации).
  const toggle = document.querySelector(`[data-kb-toggle="${id}"]`);
  const fold = toggle && toggle.closest('.kb-leaf') ? toggle.closest('.kb-leaf').querySelector(':scope > .kb-fold') : null;
  if (toggle) { toggle.classList.toggle('is-open', open); toggle.setAttribute('aria-expanded', open ? 'true' : 'false'); }
  if (fold) fold.classList.toggle('is-open', open);
}

function kbToggleCategory(key) {
  const open = !kbState.openCategories.has(key);
  if (open) kbState.openCategories.add(key); else kbState.openCategories.delete(key);
  kbSaveExpanded();
  const head = document.querySelector(`.kb-category-head[data-kb-category-toggle="${key}"]`);
  const fold = head && head.closest('.kb-category') ? head.closest('.kb-category').querySelector('.kb-fold') : null;
  if (head) head.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (fold) fold.classList.toggle('is-open', open);
}

/* --- Панель оглавления: сворачивание целиком + плавающая кнопка ----------- */

/* Узкий экран — там, где оглавление и статья стоят друг под другом. */
function kbIsNarrow() {
  return window.innerWidth < 900;
}

function kbLoadTreeOpen() {
  let stored = null;
  try { stored = localStorage.getItem(KB_TREE_OPEN_KEY); } catch (error) { stored = null; }
  kbState.treeOpen = stored === null ? true : stored === '1';
}

/* Сворачивает/разворачивает всю панель оглавления (анимирует grid-rows). */
function kbSetTreeOpen(open, persist = true) {
  kbState.treeOpen = open;
  if (persist) { try { localStorage.setItem(KB_TREE_OPEN_KEY, open ? '1' : '0'); } catch (error) { /* приватный режим */ } }
  const fold = document.querySelector('#kb-tree-fold');
  if (fold) fold.classList.toggle('is-open', open);
  const button = document.querySelector('#kb-tree-toggle');
  if (button) {
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    button.setAttribute('aria-label', open ? 'Свернуть оглавление' : 'Развернуть оглавление');
  }
  kbUpdateTreeFab();
}

/* Плавающая кнопка «Оглавление»: только на узком экране, только в разделе «База
   знаний», когда панель свёрнута и её шапка ушла вверх при прокрутке. */
function kbUpdateTreeFab() {
  const fab = document.querySelector('#kb-tree-fab');
  if (!fab) return;
  const view = document.querySelector('#kb-view');
  const inView = Boolean(view) && !view.hidden;
  const panel = document.querySelector('.kb-tree-panel');
  const headerOut = panel ? panel.getBoundingClientRect().bottom <= 0 : false;
  fab.classList.toggle('is-visible', inView && kbIsNarrow() && !kbState.treeOpen && headerOut);
}

function setupKbTreeFab() {
  const fab = document.querySelector('#kb-tree-fab');
  if (fab && !fab.dataset.bound) {
    fab.dataset.bound = '1';
    fab.addEventListener('click', () => {
      kbSetTreeOpen(true);
      document.querySelector('.kb-tree-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }
  const panel = document.querySelector('.kb-tree-panel');
  if (panel && !panel.dataset.fabObserved && 'IntersectionObserver' in window) {
    panel.dataset.fabObserved = '1';
    new IntersectionObserver(() => kbUpdateTreeFab(), { threshold: 0 }).observe(panel);
  }
  if (!document.body.dataset.kbFabResize) {
    document.body.dataset.kbFabResize = '1';
    window.addEventListener('resize', kbUpdateTreeFab);
  }
}

/* Разворачивает путь к странице, чтобы выбранный узел был виден в дереве. */
function kbExpandPath(id) {
  const byId = new Map(kbState.items.map((item) => [item.id, item]));
  let node = byId.get(Number(id));
  const guard = new Set();
  while (node && node.parent_id != null && byId.has(node.parent_id) && !guard.has(node.id)) {
    guard.add(node.id);
    kbState.expanded.add(node.parent_id);
    kbState.openCategories.add(node.category);
    node = byId.get(node.parent_id);
  }
  if (node) kbState.openCategories.add(node.category);
}

/* Дерево одной категории: корни — страницы без родителя (или с родителем вне
   выборки), дети — по parent_id. Узлы с детьми сворачиваются (fold down). */
function kbCategoryTree(items) {
  const ids = new Set(items.map((item) => item.id));
  const byParent = (parentId) => items
    .filter((item) => item.parent_id === parentId)
    .sort((a, b) => String(a.title).localeCompare(String(b.title), 'ru'));
  const build = (list, depth) => `<ul class="kb-group">${list.map((item) => {
    const active = item.id === kbState.selectedId;
    const children = byParent(item.id);
    const hasChildren = children.length > 0;
    const open = hasChildren && (kbState.expanded.has(item.id) || Boolean(kbState.query));
    const toggle = `<button class="kb-node-toggle${open ? ' is-open' : ''}${hasChildren ? '' : ' kb-node-toggle-empty'}" type="button"${hasChildren ? ` data-kb-toggle="${item.id}"` : ''} aria-expanded="${open ? 'true' : 'false'}" aria-label="${open ? 'Свернуть' : 'Развернуть'}"${hasChildren ? '' : ' tabindex="-1" aria-hidden="true"'}><svg class="icon" aria-hidden="true"><use href="#i-chevron-right"></use></svg></button>`;
    return `<li class="kb-leaf"><div class="kb-node-row"><button class="kb-node${active ? ' is-active' : ''}" type="button"${active ? ' aria-current="true"' : ''} data-kb-open="${item.id}" style="--kb-depth:${depth}"><span class="kb-node-label">${kbHighlight(item.title, kbState.query)}</span>${kbReferenceBadge(item.reference)}</button>${toggle}</div>${hasChildren ? `<div class="kb-fold${open ? ' is-open' : ''}"><div class="kb-fold-inner">${build(children, depth + 1)}</div></div>` : ''}</li>`;
  }).join('')}</ul>`;
  const roots = items
    .filter((item) => item.parent_id == null || !ids.has(item.parent_id))
    .sort((a, b) => String(a.title).localeCompare(String(b.title), 'ru'));
  return build(roots, 0);
}

/* Страница попадает в поиск по названию, тегам или имени связанного справочника. */
function kbMatchesQuery(item) {
  const query = kbState.query.trim().toLowerCase();
  if (!query) return true;
  if (String(item.title || '').toLowerCase().includes(query)) return true;
  if (Array.isArray(item.tags) && item.tags.some((tag) => String(tag).toLowerCase().includes(query))) return true;
  const reference = item.reference;
  return Boolean(reference && reference.name && String(reference.name).toLowerCase().includes(query));
}

function renderKbTree() {
  const node = document.querySelector('#kb-tree');
  if (!node) return;
  const query = kbState.query.trim();
  const base = kbState.filter === KB_CATEGORY_ALL ? kbState.items : kbState.items.filter((item) => item.category === kbState.filter);
  const visible = query ? base.filter(kbMatchesQuery) : base;
  const count = document.querySelector('#kb-count');
  if (count) count.textContent = String(visible.length);
  if (!visible.length) {
    node.innerHTML = `<p class="kb-empty kb-empty-tree">${query ? 'Ничего не найдено' : 'Пока нет страниц'}</p>`;
    kbRenderPageNav();
    return;
  }
  const categories = kbState.filter === KB_CATEGORY_ALL ? kbState.categories : [kbState.filter];
  const parts = [];
  for (const key of categories) {
    const items = visible.filter((item) => item.category === key);
    if (!items.length) continue;
    const open = Boolean(query) || kbState.openCategories.has(key);
    parts.push(`<section class="kb-category" data-kb-category="${key}"><button class="kb-category-head" type="button" data-kb-category-toggle="${key}" aria-expanded="${open ? 'true' : 'false'}"><span class="kb-category-title">${escapeHtml(kbCategoryLabel(key))}</span><span class="kb-category-count">${items.length}</span><svg class="icon kb-category-chevron" aria-hidden="true"><use href="#i-chevron-right"></use></svg></button><div class="kb-fold${open ? ' is-open' : ''}"><div class="kb-fold-inner">${kbCategoryTree(items)}</div></div></section>`);
  }
  node.innerHTML = parts.join('');
  kbRenderPageNav();
}

function kbTime(iso) {
  const date = iso ? new Date(iso) : null;
  if (!date || Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('ru-RU', { day: '2-digit', month: 'short', year: 'numeric' });
}

function kbFieldMarkup(label, value) {
  if (value == null || String(value).trim() === '') return '';
  return `<div class="kb-field"><p class="kb-field-label">${escapeHtml(label)}</p><p class="kb-field-value">${escapeHtml(value)}</p></div>`;
}

function kbBeanMarkup(bean) {
  if (!bean) return '';
  const pantone = bean.pantone_color
    ? `<div class="kb-pantone"><i class="kb-swatch kb-swatch-lg" style="--kb-swatch:${kbPantoneHex(bean.pantone_color)}"></i><div><strong>${escapeHtml(bean.pantone_color)}</strong><small>Pantone · фирменный цвет зерна</small></div></div>`
    : '';
  const roast = kbRoastLabel(bean.roast_type) || bean.roast_type;
  const status = Number(bean.is_active) ? '' : '<span class="kb-badge kb-badge-muted">Не активно</span>';
  return `${pantone}<div class="kb-fields">${kbFieldMarkup('Обжарка', roast)}${kbFieldMarkup('Описание', bean.description)}</div>${status}`;
}

/* История рецептов зерна: как его описывали на сменах (клик → карточка ленты). */
function kbRecipeNumbers(recipe) {
  return [
    recipe.dose != null ? `вход ${recipe.dose} г` : '',
    recipe.yield != null ? `выход ${recipe.yield} г` : '',
    recipe.water != null ? `вода ${recipe.water} мл` : '',
    recipe.time != null ? `${recipe.time} с` : '',
    recipe.temp != null ? `${recipe.temp} °C` : '',
  ].filter(Boolean).join(' · ');
}

function kbRecipeHistoryMarkup(items) {
  const head = '<p class="kb-field-label">История рецептов</p>';
  if (!items.length) {
    return `${head}<p class="kb-empty">Пока это зерно описывали без рецептуры.</p>`;
  }
  const rows = items.map((item) => {
    const title = item.drink && item.drink.title ? `${item.drink.title} · ` : '';
    const meta = [
      kbTime(item.created_at),
      item.author ? item.author.name : '',
      drinkPointLabel(item.point),
      item.is_espresso_recipe ? 'как эспрессо' : '',
    ].filter(Boolean);
    return `<button class="kb-recipe-row" type="button" data-kb-recipe="${item.description_id}">
      <span class="kb-recipe-main">${escapeHtml(title + (kbRecipeNumbers(item.recipe) || 'рецепт не указан'))}</span>
      <span class="kb-recipe-meta">${escapeHtml(meta.join(' · '))}</span>
    </button>`;
  }).join('');
  return `${head}<div class="kb-recipe-list">${rows}</div>`;
}

async function loadKbRecipes(pageId) {
  const box = document.querySelector('#kb-recipe-history');
  if (!box) return;
  kbState.recipesPageId = Number(pageId);
  try {
    const data = await api(`/api/kb/pages/${pageId}/recipes?limit=10`);
    // Страницу могли переключить, пока шёл запрос.
    if (kbState.recipesPageId !== Number(pageId)) return;
    box.innerHTML = kbRecipeHistoryMarkup(data.items || []);
  } catch (error) {
    box.innerHTML = `<p class="kb-empty">${escapeHtml(error.message)}</p>`;
  }
}

/* Блок подачи: заголовок, фото (если есть) и текст. */
function kbServeMarkup(label, text, photo) {
  const hasText = text != null && String(text).trim() !== '';
  if (!hasText && !photo) return '';
  const image = photo
    ? `<img class="kb-serve-photo" src="${escapeHtml(photo)}" alt="${escapeHtml(label)}" loading="lazy" data-media-url="${escapeHtml(photo)}" data-media-caption="${escapeHtml(label)}">`
    : '';
  const body = hasText ? `<p class="kb-field-value">${escapeHtml(text)}</p>` : '';
  return `<div class="kb-serve" data-media-gallery><p class="kb-field-label">${escapeHtml(label)}</p>${image}${body}</div>`;
}

function kbDrinkMarkup(drink) {
  if (!drink) return '';
  const status = Number(drink.is_active) ? '' : '<span class="kb-badge kb-badge-muted">Не активно</span>';
  const serve = kbServeMarkup('Подача здесь', drink.serve_here, drink.serve_here_photo)
    + kbServeMarkup('Подача с собой', drink.serve_takeaway, drink.serve_takeaway_photo);
  const serveBlock = serve ? `<div class="kb-serve-grid">${serve}</div>` : '';
  return `<div class="kb-fields">${kbFieldMarkup('Категория', drink.category)}${kbFieldMarkup('Технологическая карта (ТТК)', drink.ttk)}${kbFieldMarkup('Состав', drink.ingredients)}${kbFieldMarkup('Описание', drink.description)}</div>${serveBlock}${status}`;
}

/* --- Рендер содержимого страницы -----------------------------------------
   Хранимый текст — Markdown с набором безопасных токенов форматирования,
   которые вставляет тулбар: **жирный**, *курсив*, «### » заголовок, списки
   «* » и «1. », цитата «> », цвет [color:#hex]…[/color], фон
   [bg:#hex]…[/bg] и выравнивание [center|right|left]…[/…]. Произвольный HTML
   не допускается: сначала всё экранируется, затем подставляются только наши
   теги (значение цвета проходит проверку на HEX). */

const KB_INLINE_HEX = /^#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
const KB_BLOCK_PREFIX = /^(\s*)(?:#{1,3}\s+|[*-]\s+|\d+\.\s+|>\s?)/;

/* Ссылка на другую статью. В тексте живёт маркер `@[Заголовок](kb:12)` — его
   вставляет подсказка по `@`; на экране это ссылка в раздел «База знаний».
   Подпись — свободный текст: `@[статье о чае](kb:3)` совершенно законно, переход
   всё равно по id, и при сохранении подпись не переписывается.
   Формат единый для статей, анонсов и чата, поэтому и рендер один — `kbLinkify`.
   Маркер текстовый намеренно: он переживает и Markdown-строку, и блоки
   Editor.js, и обычный `TEXT` в базе без правок санитайзера. */
const KB_PAGE_LINK_RE = /@\[([^\]\n]{1,200})\]\(kb:(\d{1,12})\)/g;

function kbPageHref(id) {
  return `/?view=kb&page=${Number(id)}`;
}

/* Подпись по умолчанию при вставке — заголовок статьи. В готовом тексте её
   можно переписать руками; чистим только символы, ломающие разбор и HTML
   (`&`/`<` в блоках Editor.js дали бы двойное экранирование). */
function kbPageLinkTitle(value) {
  const title = String(value == null ? '' : value)
    .replace(/[\[\]()<>&\r\n]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 120);
  return title || 'Страница';
}

/* Маркеры → ссылки. На вход — уже экранированный текст: HTML не пере-экранируем.
   `@` из маркера на экран не выводим: читателю достаточно иконки статьи и
   подписи, а символ `@` выглядел как упоминание сотрудника. */
function kbLinkify(text) {
  const value = String(text == null ? '' : text);
  if (!value.includes('@[')) return value;
  return value.replace(KB_PAGE_LINK_RE, (match, title, id) =>
    `<a class="neft-page-link" href="${kbPageHref(id)}" data-kb-page="${Number(id)}"><svg class="icon" aria-hidden="true"><use href="#i-book"></use></svg>${title}</a>`);
}

function kbInlineFormat(text) {
  const html = text
    .replace(/\[color:(#[0-9a-fA-F]{3,8})\]([\s\S]*?)\[\/color\]/g, (match, hex, inner) => (KB_INLINE_HEX.test(hex) ? `<span style="color:${hex}">${inner}</span>` : inner))
    .replace(/\[bg:(#[0-9a-fA-F]{3,8})\]([\s\S]*?)\[\/bg\]/g, (match, hex, inner) => (KB_INLINE_HEX.test(hex) ? `<span style="background:${hex}">${inner}</span>` : inner))
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  return kbLinkify(html);
}

function kbContentToHtml(text) {
  const lines = escapeHtml(text).replace(/\r\n?/g, '\n').split('\n');
  const parts = [];
  let list = null;
  const closeList = () => { if (list) { parts.push(`</${list}>`); list = null; } };
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) { closeList(); continue; }
    let align = '';
    let body = line;
    const openAlign = body.match(/^\[(center|right|left)\]/i);
    const closeAlign = body.match(/\[\/(center|right|left)\]$/i);
    if (openAlign && closeAlign && openAlign[1].toLowerCase() === closeAlign[1].toLowerCase()) {
      align = ` style="text-align:${openAlign[1].toLowerCase()}"`;
      body = body.slice(openAlign[0].length, body.length - closeAlign[0].length).trim();
    }
    const heading = body.match(/^(#{1,3})\s+(.*)$/);
    const bullet = body.match(/^[*-]\s+(.*)$/);
    const numbered = body.match(/^\d+\.\s+(.*)$/);
    const quote = body.match(/^&gt;\s?(.*)$/);
    if (heading) {
      closeList();
      const tag = ['h2', 'h3', 'h4'][heading[1].length - 1] || 'h4';
      parts.push(`<${tag}${align}>${kbInlineFormat(heading[2])}</${tag}>`);
    } else if (bullet) {
      if (list !== 'ul') { closeList(); parts.push('<ul>'); list = 'ul'; }
      parts.push(`<li>${kbInlineFormat(bullet[1])}</li>`);
    } else if (numbered) {
      if (list !== 'ol') { closeList(); parts.push('<ol>'); list = 'ol'; }
      parts.push(`<li>${kbInlineFormat(numbered[1])}</li>`);
    } else if (quote) {
      closeList();
      parts.push(`<blockquote>${kbInlineFormat(quote[1])}</blockquote>`);
    } else {
      closeList();
      parts.push(`<p${align}>${kbInlineFormat(body)}</p>`);
    }
  }
  closeList();
  return parts.join('');
}

/* Безопасный инлайн-рендер содержимого блока: сначала экранируем ВСЁ, затем
   возвращаем только простые теги, которые ставит Editor.js и его тулбар. */
function kbSanitizeInline(value) {
  const escaped = escapeHtml(String(value == null ? '' : value));
  const html = escaped
    .replace(/&lt;(\/?)(b|strong|i|em|u|s|code|mark|br|sub|sup)(\s*\/?)&gt;/gi, '<$1$2$3>')
    .replace(/&lt;a href=&quot;(https?:\/\/[^"&]+)&quot;&gt;/gi, '<a href="$1" target="_blank" rel="noopener noreferrer">')
    .replace(/&lt;\/a&gt;/gi, '</a>');
  return kbLinkify(html);
}

/* Картинку из блока принимаем только с нашего фото-эндпоинта (или data:image) —
   произвольные URL и инъекции в src не пропускаем. */
function kbSafeMediaUrl(value) {
  const url = String(value == null ? '' : value).trim();
  if (/^\/api\/kb\/photos\/[0-9a-f]{32}\.webp$/.test(url)) return url;
  if (/^data:image\/(png|jpe?g|webp|gif);base64,[a-z0-9+/=]+$/i.test(url)) return url;
  return '';
}

/* Режим чтения: JSON-блоки Editor.js → безопасный HTML для бариста и стажёров
   (управляющие роли видят те же блоки, но в интерактивном редакторе). */
function kbBlocksToHtml(blocks) {
  if (!Array.isArray(blocks)) return '';
  const parts = [];
  for (const block of blocks) {
    if (!block || typeof block !== 'object') continue;
    const data = block.data || {};
    const type = block.type;
    if (type === 'header') {
      const level = Math.min(4, Math.max(2, Number(data.level) || 2));
      parts.push(`<h${level}>${kbSanitizeInline(data.text)}</h${level}>`);
    } else if (type === 'paragraph') {
      parts.push(`<p>${kbSanitizeInline(data.text)}</p>`);
    } else if (type === 'list') {
      const tag = data.style === 'ordered' ? 'ol' : 'ul';
      const items = Array.isArray(data.items) ? data.items : [];
      const rows = items.map((item) => `<li>${kbSanitizeInline(typeof item === 'string' ? item : (item && item.content) || '')}</li>`).join('');
      parts.push(`<${tag}>${rows}</${tag}>`);
    } else if (type === 'checklist') {
      const items = Array.isArray(data.items) ? data.items : [];
      const rows = items.map((item) => {
        const checked = item && item.checked ? ' is-checked' : '';
        return `<li class="kb-check${checked}"><span class="kb-check-box" aria-hidden="true"></span>${kbSanitizeInline((item && item.text) || '')}</li>`;
      }).join('');
      parts.push(`<ul class="kb-checklist">${rows}</ul>`);
    } else if (type === 'table') {
      const rows = Array.isArray(data.content) ? data.content : [];
      const withHeadings = Boolean(data.withHeadings);
      const body = rows.map((row, index) => {
        const cell = withHeadings && index === 0 ? 'th' : 'td';
        const cells = (Array.isArray(row) ? row : []).map((value) => `<${cell}>${kbSanitizeInline(value)}</${cell}>`).join('');
        return `<tr>${cells}</tr>`;
      }).join('');
      parts.push(`<div class="kb-table-wrap"><table>${body}</table></div>`);
    } else if (type === 'quote') {
      const caption = data.caption ? `<cite>${escapeHtml(String(data.caption))}</cite>` : '';
      parts.push(`<blockquote>${kbSanitizeInline(data.text)}${caption}</blockquote>`);
    } else if (type === 'delimiter') {
      parts.push('<div class="kb-delimiter" role="presentation">***</div>');
    } else if (type === 'image') {
      const url = kbSafeMediaUrl(data.url);
      if (url) {
        const caption = data.caption ? `<figcaption>${escapeHtml(String(data.caption))}</figcaption>` : '';
        parts.push(`<figure class="kb-image"><img src="${escapeHtml(url)}" alt="${escapeHtml(String(data.caption || ''))}" loading="lazy" data-media-url="${escapeHtml(url)}">${caption}</figure>`);
      }
    }
  }
  return parts.join('');
}

/* Тело страницы (wiki/standards): новый формат — блоки Editor.js, легаси —
   Markdown-строка или JSON с полем text. Рендерим читаемо в обоих случаях. */
function kbBodyMarkup(page) {
  const raw = page.content_json;
  if (!raw) return '';
  let parsed = null;
  try { parsed = JSON.parse(raw); } catch (error) { parsed = null; }
  let html;
  if (parsed && typeof parsed === 'object' && Array.isArray(parsed.blocks)) {
    html = kbBlocksToHtml(parsed.blocks);
  } else {
    let text = raw;
    if (typeof parsed === 'string') text = parsed;
    else if (parsed && typeof parsed === 'object' && typeof parsed.text === 'string') text = parsed.text;
    else if (parsed && typeof parsed === 'object') text = JSON.stringify(parsed, null, 2);
    html = kbContentToHtml(text);
  }
  if (!html) return '';
  return `<div class="kb-field"><p class="kb-field-label">Содержимое</p><div class="kb-field-value kb-content">${html}</div></div>`;
}

function renderKbPage() {
  const node = document.querySelector('#kb-page');
  if (!node) return;
  const status = document.querySelector('#kb-tree-status');
  if (status) status.textContent = '';
  // Навигация свайпом/стрелками: старый контент уводим отдельным слоем, новый
  // выезжает сбоку — как слайд в карусели, только страницы грузятся поштучно.
  const animate = kbNav.pending !== 0 && Boolean(node.firstElementChild) && !prefersReducedMotion();
  const direction = kbNav.pending || 1;
  kbNav.pending = 0;
  if (!animate) node.classList.remove('kb-no-transition', 'is-enter-next', 'is-enter-prev');
  const page = kbState.current;
  if (!page) {
    node.innerHTML = '<p class="kb-empty">Выберите страницу слева, чтобы открыть её. Зерно — фирменные цвета Pantone, напитки — ТТК, состав и правила подачи.</p>';
    kbRenderPageNav();
    return;
  }
  const meta = [`<span class="kb-chip">${escapeHtml(kbCategoryLabel(page.category))}</span>`];
  if (Array.isArray(page.tags) && page.tags.length) {
    for (const tag of page.tags) {
      meta.push(`<button class="kb-tag" type="button" data-kb-tag="${escapeHtml(tag)}" title="Показать страницы с тегом">${kbHighlight(tag, kbState.query)}</button>`);
    }
  }
  if (page.author_name) meta.push(`<span class="kb-meta">${escapeHtml(page.author_name)}</span>`);
  if (page.updated_at) meta.push(`<span class="kb-meta">обновлено ${escapeHtml(kbTime(page.updated_at))}</span>`);
  const actions = kbState.canEdit
    ? `<div class="kb-actions"><button class="button button-quiet" type="button" data-kb-edit="${page.id}"><svg class="icon" aria-hidden="true"><use href="#i-edit"></use></svg> Редактировать</button><button class="button button-quiet kb-danger" type="button" data-kb-delete="${page.id}"><svg class="icon" aria-hidden="true"><use href="#i-trash"></use></svg> Удалить</button></div>`
    : '';
  let body = '';
  if (page.category === 'beans') {
    body = kbBeanMarkup(page.bean)
      + '<div class="kb-recipe-history" id="kb-recipe-history"><p class="kb-empty">Загрузка истории…</p></div>';
  } else if (page.category === 'drinks') body = kbDrinkMarkup(page.drink);
  const content = kbBodyMarkup(page);
  let outgoing = null;
  if (animate) {
    node.parentElement?.querySelector('.kb-page-outgoing')?.remove();
    outgoing = document.createElement('div');
    outgoing.className = `kb-page-outgoing ${direction > 0 ? 'is-next' : 'is-prev'}`;
    while (node.firstChild) outgoing.appendChild(node.firstChild);
    // Снимок не должен плодить дубли id (например, #kb-recipe-history).
    outgoing.querySelectorAll('[id]').forEach((el) => el.removeAttribute('id'));
    (node.parentElement || node).appendChild(outgoing);
    node.classList.add('kb-no-transition', direction > 0 ? 'is-enter-next' : 'is-enter-prev');
    void node.offsetWidth;   // фиксируем стартовое смещение без анимации
    node.classList.remove('kb-no-transition');
  }
  node.innerHTML = `<header class="kb-page-head"><div><p class="eyebrow">${escapeHtml(kbCategoryLabel(page.category))}</p><h3 class="kb-page-title">${escapeHtml(page.title)}</h3><div class="kb-meta-row">${meta.join('')}</div></div>${actions}</header><div class="kb-page-body">${body}${content}</div>`;
  if (page.category === 'beans') loadKbRecipes(page.id);
  if (outgoing) {
    requestAnimationFrame(() => {
      outgoing.classList.add('is-leaving');
      node.classList.remove('is-enter-next', 'is-enter-prev');
    });
    setTimeout(() => outgoing.remove(), 360);
  }
  kbRenderPageNav();
}

/* Порядок страниц так, как они показаны в оглавлении: по разделам, внутри —
   корни и их потомки по алфавиту. Учитывает фильтр раздела и строку поиска. */
function kbOrderedIds() {
  const query = kbState.query.trim();
  const base = kbState.filter === KB_CATEGORY_ALL ? kbState.items : kbState.items.filter((item) => item.category === kbState.filter);
  const visible = query ? base.filter(kbMatchesQuery) : base;
  const categories = kbState.filter === KB_CATEGORY_ALL ? kbState.categories : [kbState.filter];
  const order = [];
  for (const key of categories) {
    const items = visible.filter((item) => item.category === key);
    if (!items.length) continue;
    const ids = new Set(items.map((item) => item.id));
    const byParent = (parentId) => items
      .filter((item) => item.parent_id === parentId)
      .sort((a, b) => String(a.title).localeCompare(String(b.title), 'ru'));
    const roots = items
      .filter((item) => item.parent_id == null || !ids.has(item.parent_id))
      .sort((a, b) => String(a.title).localeCompare(String(b.title), 'ru'));
    const seen = new Set();
    const walk = (item) => {
      if (seen.has(item.id)) return;
      seen.add(item.id);
      order.push(item.id);
      for (const child of byParent(item.id)) walk(child);
    };
    for (const root of roots) walk(root);
  }
  return order;
}

/* Стрелки-соседи и счётчик «N / всего» под статьёй. */
function kbRenderPageNav() {
  const nav = document.querySelector('#kb-page-nav');
  if (!nav) return;
  const order = kbOrderedIds();
  const total = order.length;
  const index = order.indexOf(kbState.selectedId);
  if (total <= 1 || index === -1) { nav.hidden = true; nav.innerHTML = ''; return; }
  const atStart = index <= 0;
  const atEnd = index >= total - 1;
  nav.hidden = false;
  nav.innerHTML = `<div class="slideshow-nav"><button class="icon-button" type="button" data-kb-page-step="-1" aria-label="Предыдущая страница"${atStart ? ' disabled' : ''}><svg class="icon" aria-hidden="true"><use href="#i-chevron-left"></use></svg></button><span class="slideshow-count">${index + 1} / ${total}</span><button class="icon-button" type="button" data-kb-page-step="1" aria-label="Следующая страница"${atEnd ? ' disabled' : ''}><svg class="icon" aria-hidden="true"><use href="#i-chevron-right"></use></svg></button></div>`;
}

/* Перейти к соседней странице из оглавления (без зацикливания: на краях стоп). */
function kbNavigatePage(step) {
  const order = kbOrderedIds();
  const total = order.length;
  if (total <= 1) return;
  const index = order.indexOf(kbState.selectedId);
  const nextIndex = index === -1 ? (step > 0 ? 0 : total - 1) : index + step;
  if (nextIndex < 0 || nextIndex >= total) return;
  const targetId = order[nextIndex];
  if (targetId === kbState.selectedId) return;
  kbSwitchPage(targetId, step);
}

/* Запускает переход: направление запоминаем для первого рендера новой страницы. */
async function kbSwitchPage(id, step) {
  if (kbNav.busy) return;
  kbNav.busy = true;
  kbNav.pending = step > 0 ? 1 : -1;
  try {
    await openKbPage(id);
  } finally {
    kbNav.busy = false;
    if (kbNav.pending) kbNav.pending = 0;
  }
}

async function openKbPage(id) {
  const numeric = Number(id);
  kbState.selectedId = numeric;
  kbExpandPath(numeric);
  renderKbTree();
  // На телефоне при открытии статьи сворачиваем оглавление до шапки, чтобы
  // текст сразу был перед глазами (развернуть можно кнопкой или через FAB).
  if (kbIsNarrow()) kbSetTreeOpen(false);
  const status = document.querySelector('#kb-tree-status');
  if (status) status.textContent = 'Загрузка страницы…';
  const cached = readCache(`${CACHE_KEY_KB_PAGE}:${numeric}`);
  if (cached) { kbState.current = cached; renderKbPage(); }
  try {
    const page = await api(`/api/kb/pages/${numeric}`);
    kbState.current = page;
    writeCache(`${CACHE_KEY_KB_PAGE}:${numeric}`, page);
    if (status) status.textContent = '';
    renderKbPage();
  } catch (error) {
    if (error.isOffline) {
      setNetworkStatus(false, lastSyncTimestamp());
      if (status && !cached) status.textContent = 'Нет связи — страница ещё не сохранена для офлайна';
    } else {
      showToast(error.message);
    }
  }
}

async function loadKnowledgeBase() {
  // Офлайн-режим: сразу показываем сохранённую структуру разделов.
  const cached = readCache(CACHE_KEY_KB_LIST);
  if (cached && Array.isArray(cached.items)) {
    kbState.items = cached.items;
    if (Array.isArray(cached.sections) && cached.sections.length) kbApplySections(cached.sections);
    else if (Array.isArray(cached.categories) && cached.categories.length) kbState.categories = cached.categories;
    kbFilterChips();
    kbRenderCategorySelect();
    kbRenderTagSuggestions();
    renderKbTree();
  }
  try {
    const data = await api('/api/kb/pages');
    kbState.items = data.items || [];
    kbApplySections(data.sections);
    writeCache(CACHE_KEY_KB_LIST, { items: kbState.items, sections: kbState.sections });
    writeCache(CACHE_KEY_LAST_SYNC, Date.now());
    kbFilterChips();
    kbRenderCategorySelect();
    kbRenderTagSuggestions();
    renderKbTree();
    return true;
  } catch (error) {
    if (error.isOffline) {
      setNetworkStatus(false, lastSyncTimestamp());
      if (!cached) showToast('Связь с сервером потеряна');
    } else {
      showToast(error.message);
    }
    return false;
  }
}

/* Открытие раздела: первая загрузка рисует каркас, а пустой раздел сразу
   открывает первую доступную страницу, чтобы не было пустого экрана. */
let kbOpenTask = null;

async function openKnowledgeBase() {
  // Переход по ссылке вызывает раздел и «сам себя» — ждём один и тот же прогон,
  // иначе оглавление грузится дважды и страницы перебивают друг друга.
  if (kbOpenTask) return kbOpenTask;
  kbOpenTask = (async () => {
    kbLoadExpanded();
    kbLoadTreeOpen();
    if (!kbState.ready) { kbFilterChips(); renderKbTree(); kbState.ready = true; }
    setupKbTreeFab();
    kbSetTreeOpen(kbState.treeOpen, false);
    const ok = await loadKnowledgeBase();
    // `pendingId` — цель перехода по ссылке; иначе показываем первую статью.
    const target = kbState.pendingId || (ok && !kbState.current && kbState.items.length ? kbState.items[0].id : null);
    kbState.pendingId = null;
    if (target) await openKbPage(target);
    kbUpdateTreeFab();
  })();
  try {
    return await kbOpenTask;
  } finally {
    kbOpenTask = null;
  }
}

/* Переход по ссылке `@[Заголовок](kb:12)` из любого модуля: открываем раздел,
   снимаем фильтр (иначе страница не видна в оглавлении) и показываем статью. */
async function openKbPageFromLink(id) {
  const numeric = Number(id);
  if (!numeric) return;
  kbState.pendingId = numeric;
  // Фильтр раздела мог скрыть страницу в оглавлении — возвращаем «Все» заранее,
  // чтобы оглавление, счётчик «N / всего» и переходы строились по полному списку.
  kbState.filter = KB_CATEGORY_ALL;
  setView('kb');                 // запускает `openKnowledgeBase` — он прочтёт pendingId
  await openKnowledgeBase();
  kbState.pendingId = null;
  kbFilterChips();
  renderKbTree();
  // Модуль мог быть выключен (тогда раздел не открылся) — доводим вручную.
  if (Number(kbState.current?.id) !== numeric) await openKbPage(numeric);
  // Адрес оставляем «перезагружаемым»: та же ссылка работает в новой вкладке.
  try {
    window.history.replaceState({}, '', kbPageHref(numeric));
  } catch (error) { /* file:// и приватный режим — просто не пишем адрес */ }
}

/* Делегированный клик: ссылки на статьи живут в статьях, анонсах и чате.
   Ctrl/Cmd/Shift не перехватываем — браузер откроет ссылку в новой вкладке. */
document.addEventListener('click', (event) => {
  const target = event.target instanceof Element ? event.target : null;
  const link = target && target.closest('[data-kb-page]');
  if (!link || event.defaultPrevented) return;
  if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  // Ссылка на статью — это переход, а не клик по карточке анонса или сообщению:
  // обработчик карточки подписан на document позже, гасим его для этого клика.
  event.stopImmediatePropagation();
  openKbPageFromLink(link.dataset.kbPage);
});

/* Рисует палитру Pantone свотчами + кнопку «Другое» (единый источник — KB_PANTONE). */
function kbRenderPantone() {
  const picker = document.querySelector('#kb-pantone-picker');
  if (!picker || picker.dataset.filled) return;
  const swatches = KB_PANTONE.map((item) =>
    `<button class="pantone-swatch" type="button" role="radio" aria-checked="false" data-pantone="${escapeHtml(item.value)}" style="--swatch:${item.hex}" title="${escapeHtml(item.value)}" aria-label="${escapeHtml(item.value)}"></button>`,
  );
  swatches.push(`<button class="pantone-swatch pantone-swatch-other" type="button" role="radio" aria-checked="false" data-pantone="${escapeHtml(KB_PANTONE_OTHER)}" title="Другое — свой цвет" aria-label="Другое — свой цвет"><svg class="icon" aria-hidden="true"><use href="#i-plus"></use></svg></button>`);
  picker.innerHTML = swatches.join('');
  picker.dataset.filled = '1';
}

/* Показывает блок ручного цвета, когда выбран пункт «Другое». */
function kbSyncPantoneCustom() {
  const hidden = document.querySelector('#kb-field-pantone');
  const custom = document.querySelector('#kb-pantone-custom');
  if (custom) custom.hidden = (hidden ? hidden.value : '') !== KB_PANTONE_OTHER;
}

/* Выбирает свотч Pantone и синхронизирует скрытое значение формы. */
function kbSelectPantone(value) {
  const normalized = value || KB_PANTONE[0].value;
  const hidden = document.querySelector('#kb-field-pantone');
  if (hidden) hidden.value = normalized;
  document.querySelectorAll('#kb-pantone-picker .pantone-swatch').forEach((node) => {
    const active = node.dataset.pantone === normalized;
    node.classList.toggle('is-selected', active);
    node.setAttribute('aria-checked', active ? 'true' : 'false');
  });
  kbSyncPantoneCustom();
}

/* Разбирает значение обжарки в список кодов (поддерживает CSV и массивы). */
function kbRoastValues(value) {
  const raw = Array.isArray(value) ? value : String(value || '').split(',');
  return raw.map((item) => String(item).trim()).filter((item) => KB_ROAST_LABELS[item]);
}

/* Человекочитаемая метка: один тип или несколько через « · ». */
function kbRoastLabel(value) {
  return kbRoastValues(value).map((code) => KB_ROAST_LABELS[code]).join(' · ');
}

/* Ставит выбранные типы обжарки в чипах — типов может быть несколько. */
function kbSetRoast(value) {
  const values = kbRoastValues(value);
  // Скрытое поле лежит рядом с чипами (сосед #kb-roast внутри формы), а не
  // внутри него, поэтому ищем в области формы, иначе значение обжарки не
  // сохраняется и при отправке уходит дефолтный «espresso».
  const hidden = document.querySelector('#kb-page-form input[name="roast_type"]');
  if (hidden) hidden.value = values.join(',');
  document.querySelectorAll('#kb-roast .kb-seg-button').forEach((node) => {
    const active = values.includes(node.dataset.roast);
    node.classList.toggle('is-selected', active);
    node.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

/* Переключает один тип обжарки, не сбрасывая остальные. */
function kbToggleRoast(value) {
  const button = document.querySelector(`#kb-roast .kb-seg-button[data-roast="${value}"]`);
  if (!button) return;
  const active = !button.classList.contains('is-selected');
  button.classList.toggle('is-selected', active);
  button.setAttribute('aria-pressed', active ? 'true' : 'false');
  const hidden = document.querySelector('#kb-page-form input[name="roast_type"]');
  if (hidden) {
    hidden.value = [...document.querySelectorAll('#kb-roast .kb-seg-button.is-selected')]
      .map((node) => node.dataset.roast).join(',');
  }
}

/* Показывает поля выбранной категории и «другой» Pantone. */
function kbSyncFields() {
  const form = document.querySelector('#kb-page-form');
  if (!form) return;
  const category = form.elements.category.value;
  form.querySelectorAll('[data-kb-for]').forEach((field) => {
    field.hidden = !String(field.dataset.kbFor).split(/\s+/).includes(category);
  });
  // Поле текста показываем для любого кастомного (несистемного) раздела.
  form.querySelectorAll('[data-kb-for-custom]').forEach((field) => {
    field.hidden = kbIsSystemSection(category);
  });
  const hidden = document.querySelector('#kb-field-pantone');
  if (category === 'beans' && hidden && !hidden.value) kbSelectPantone(KB_PANTONE[0].value);
  else kbSyncPantoneCustom();
}

function kbFormError(message) {
  const node = document.querySelector('#kb-form-error');
  if (!node) { showToast(message); return; }
  node.textContent = message;
  node.hidden = false;
}

function openKbDialog(page) {
  const dialog = document.querySelector('#kb-page-dialog');
  const form = document.querySelector('#kb-page-form');
  if (!dialog || !form) return;
  kbRenderPantone();
  form.reset();
  kbRenderCategorySelect();
  kbResetDrinkPhotos();
  clearDraftNotice(form);
  const errorNode = document.querySelector('#kb-form-error');
  if (errorNode) errorNode.hidden = true;
  form.dataset.pageId = page ? String(page.id) : '';
  kbSetDialogTags(page && Array.isArray(page.tags) ? page.tags : []);
  document.querySelector('#kb-dialog-kicker').textContent = page ? 'ПРАВКА СТРАНИЦЫ' : 'НОВАЯ СТРАНИЦА';
  document.querySelector('#kb-dialog-title').textContent = page ? 'Изменить страницу' : 'Страница';
  document.querySelector('#kb-form-submit').textContent = page ? 'Сохранить' : 'Создать';
  form.elements.is_active.checked = true;
  if (!page) {
    form.elements.category.value = kbState.filter !== KB_CATEGORY_ALL ? kbState.filter : 'beans';
    form.elements.parent_id.value = '';
    kbSetRoast('espresso');
    kbSelectPantone(KB_PANTONE[0].value);
    form.elements.pantone_custom.value = '';
    form.elements.pantone_hex.value = KB_PANTONE[0].hex;
  } else {
    form.elements.category.value = page.category;
    form.elements.parent_id.value = page.parent_id != null ? String(page.parent_id) : '';
    form.elements.title.value = page.title || '';
    if (page.category === 'beans' && page.bean) {
      kbSetRoast(page.bean.roast_type || 'espresso');
      const pantone = page.bean.pantone_color || '';
      const known = KB_PANTONE.some((item) => item.value === pantone);
      const isHex = /^#[0-9a-f]{3,8}$/i.test(pantone);
      if (known) {
        kbSelectPantone(pantone);
        form.elements.pantone_custom.value = '';
      } else if (isHex) {
        kbSelectPantone(KB_PANTONE_OTHER);
        form.elements.pantone_hex.value = pantone;
        form.elements.pantone_custom.value = '';
      } else if (pantone) {
        kbSelectPantone(KB_PANTONE_OTHER);
        form.elements.pantone_custom.value = pantone;
      } else {
        kbSelectPantone(KB_PANTONE[0].value);
        form.elements.pantone_custom.value = '';
      }
      form.elements.bean_description.value = page.bean.description || '';
      form.elements.is_active.checked = Number(page.bean.is_active) !== 0;
    } else if (page.category === 'drinks' && page.drink) {
      form.elements.drink_category.value = page.drink.category || 'черный кофе';
      form.elements.ttk.value = page.drink.ttk || '';
      form.elements.ingredients.value = page.drink.ingredients || '';
      form.elements.serve_here.value = page.drink.serve_here || '';
      form.elements.serve_takeaway.value = page.drink.serve_takeaway || '';
      kbSetDrinkPhoto('serve_here', page.drink.serve_here_photo);
      kbSetDrinkPhoto('serve_takeaway', page.drink.serve_takeaway_photo);
      form.elements.drink_description.value = page.drink.description || '';
      form.elements.is_active.checked = Number(page.drink.is_active) !== 0;
    } else {
      form.elements.content_json.value = page.content_json || '';
    }
  }
  kbSyncFields();
  // Черновик — только для новой страницы; правку существующей не подменяем.
  const draft = page ? null : readDraft('kb');
  if (draft) {
    applyFields(form, draft.fields);
    kbSetDrinkPhoto('serve_here', (draft.fields || {}).serve_here_photo || '');
    kbSetDrinkPhoto('serve_takeaway', (draft.fields || {}).serve_takeaway_photo || '');
    kbSyncFields();
    // Теги черновика лежат в скрытом поле строкой JSON — вернём их в чипсы.
    try { kbSetDialogTags(JSON.parse(form.elements.tags.value || '[]')); } catch (error) { kbSetDialogTags([]); }
  }
  dialog.showModal();
  kbSyncEditorForCategory();
  if (draft) {
    renderDraftNotice(form, 'kb', () => { form.reset(); kbResetDrinkPhotos(); kbSyncFields(); });
  }
}

async function submitKbForm(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const errorNode = document.querySelector('#kb-form-error');
  if (errorNode) errorNode.hidden = true;
  const category = form.elements.category.value;
  const title = form.elements.title.value.trim();
  if (!title) { kbFormError('Укажите название страницы'); return; }
  const payload = { title, category };
  const parentId = form.elements.parent_id.value;
  payload.parent_id = parentId ? Number(parentId) : null;
  payload.tags = kbDialogTags.slice();
  if (category === 'beans') {
    let pantone = form.elements.pantone_select.value;
    if (pantone === KB_PANTONE_OTHER) {
      const label = form.elements.pantone_custom.value.trim();
      pantone = label || (form.elements.pantone_hex ? form.elements.pantone_hex.value.trim() : '');
    }
    payload.bean = {
      name: title,
      roast_type: kbRoastValues(form.elements.roast_type.value),
      pantone_color: pantone || null,
      description: form.elements.bean_description.value.trim() || null,
      is_active: form.elements.is_active.checked,
    };
  } else if (category === 'drinks') {
    payload.drink = {
      title,
      category: form.elements.drink_category.value,
      ttk: form.elements.ttk.value.trim() || null,
      ingredients: form.elements.ingredients.value.trim() || null,
      description: form.elements.drink_description.value.trim() || null,
      serve_here: form.elements.serve_here.value.trim() || null,
      serve_takeaway: form.elements.serve_takeaway.value.trim() || null,
      serve_here_photo: form.elements.serve_here_photo.value || null,
      serve_takeaway_photo: form.elements.serve_takeaway_photo.value || null,
      is_active: form.elements.is_active.checked,
    };
  } else {
    payload.content_json = await kbCollectContent();
  }
  const pageId = form.dataset.pageId;
  const submit = document.querySelector('#kb-form-submit');
  if (submit) submit.disabled = true;
  try {
    let targetId = pageId ? Number(pageId) : null;
    if (pageId) {
      await api(`/api/kb/pages/${pageId}`, { method: 'PUT', body: JSON.stringify(payload) });
      showToast('Страница сохранена');
    } else {
      const data = await api('/api/kb/pages', { method: 'POST', body: JSON.stringify(payload) });
      targetId = Number(data.id);
      showToast('Страница создана');
    }
    document.querySelector('#kb-page-dialog')?.close();
    clearDraft('kb');
    await loadKnowledgeBase();
    if (targetId) openKbPage(targetId);
  } catch (error) {
    kbFormError(error.message);
  } finally {
    if (submit) submit.disabled = false;
  }
}

async function deleteKbPage(id) {
  const page = kbState.items.find((item) => item.id === id)
    || (kbState.current && kbState.current.id === id ? kbState.current : null);
  const name = page ? page.title : 'страницу';
  if (!window.confirm(`Удалить «${name}»? Действие необратимо.`)) return;
  try {
    await api(`/api/kb/pages/${id}`, { method: 'DELETE' });
    showToast('Страница удалена');
    kbState.current = null;
    kbState.selectedId = null;
    renderKbPage();
    await loadKnowledgeBase();
  } catch (error) {
    showToast(error.message);
  }
}

/* --- Форматирование текста страницы (Markdown-тулбар) --------------------- */

let kbEditorBound = false;
let kbPhotosBound = false;
let kbTagsBound = false;
// Теги текущего диалога (строки) и инстанс блочного редактора Editor.js.
let kbDialogTags = [];
let kbEditorInstance = null;

/* Обновляет индикаторы цвета на кнопках тулбара под текущие значения пикеров. */
function syncKbToolChips() {
  const pairs = [['#kb-color-text', '#kb-color-text-chip'], ['#kb-color-bg', '#kb-color-bg-chip']];
  for (const [inputId, chipId] of pairs) {
    const input = document.querySelector(inputId);
    const chip = document.querySelector(chipId);
    if (input && chip) chip.style.setProperty('--swatch', input.value);
  }
}

function kbSetSelection(textarea, start, end) {
  textarea.focus();
  textarea.setSelectionRange(start, end);
}

/* Оборачивает выделение обёртками before/after; при пустом выделении подставляет
   плейсхолдер, чтобы было видно, что именно форматируется. */
function kbWrapSelection(textarea, before, after, placeholder) {
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const selected = textarea.value.slice(start, end) || placeholder || '';
  textarea.setRangeText(before + selected + after, start, end, 'end');
  const selStart = start + before.length;
  kbSetSelection(textarea, selStart, selStart + selected.length);
}

/* Префиксует каждую строку выделения блочным маркером (список, заголовок,
   цитата), попутно снимая прежний маркер — типы не наслаиваются. */
function kbPrefixLines(textarea, prefix) {
  const start = textarea.value.lastIndexOf('\n', textarea.selectionStart - 1) + 1;
  let end = textarea.value.indexOf('\n', textarea.selectionEnd);
  if (end === -1) end = textarea.value.length;
  const block = textarea.value.slice(start, end);
  const insert = block.split('\n')
    .map((line) => `${prefix}${line.replace(KB_BLOCK_PREFIX, '$1')}`)
    .join('\n');
  textarea.setRangeText(insert, start, end, 'end');
  kbSetSelection(textarea, start, start + insert.length);
}

/* Снимает Markdown-разметку и токены форматирования с выделения (или строки). */
function kbClearFormatting(textarea) {
  let start = textarea.selectionStart;
  let end = textarea.selectionEnd;
  if (start === end) {
    start = textarea.value.lastIndexOf('\n', start - 1) + 1;
    let lineEnd = textarea.value.indexOf('\n', end);
    if (lineEnd === -1) lineEnd = textarea.value.length;
    end = lineEnd;
  }
  const cleaned = textarea.value.slice(start, end)
    .replace(/\[color:#[0-9a-fA-F]{3,8}\]/gi, '').replace(/\[\/color\]/gi, '')
    .replace(/\[bg:#[0-9a-fA-F]{3,8}\]/gi, '').replace(/\[\/bg\]/gi, '')
    .replace(/\[\/?(?:center|right|left)\]/gi, '')
    .replace(/\*\*/g, '').replace(/`/g, '')
    .replace(/\*([^*\n]+)\*/g, '$1')
    .replace(/^(\s*)(?:#{1,3}\s+|[*-]\s+|\d+\.\s+|>\s?)/gm, '$1');
  textarea.setRangeText(cleaned, start, end, 'end');
  kbSetSelection(textarea, start, start + cleaned.length);
}

/* Применяет инструмент тулбара к textarea. */
function kbApplyFormat(textarea, cmd, color) {
  if (!textarea) return;
  if (cmd === 'bold') kbWrapSelection(textarea, '**', '**', 'текст');
  else if (cmd === 'italic') kbWrapSelection(textarea, '*', '*', 'текст');
  else if (cmd === 'heading') kbPrefixLines(textarea, '### ');
  else if (cmd === 'ul') kbPrefixLines(textarea, '* ');
  else if (cmd === 'ol') kbPrefixLines(textarea, '1. ');
  else if (cmd === 'quote') kbPrefixLines(textarea, '> ');
  else if (cmd === 'color') kbWrapSelection(textarea, `[color:${color}]`, '[/color]', 'текст');
  else if (cmd === 'bg') kbWrapSelection(textarea, `[bg:${color}]`, '[/bg]', 'текст');
  else if (cmd === 'align-left' || cmd === 'align-center' || cmd === 'align-right') {
    const side = cmd.slice('align-'.length);
    kbWrapSelection(textarea, `[${side}]`, `[/${side}]`, 'текст');
  } else if (cmd === 'clear') kbClearFormatting(textarea);
}

/* Подключает тулбар редактора к textarea страницы. */
function setupKbEditor() {
  const toolbar = document.querySelector('#kb-toolbar');
  const textarea = document.querySelector('#kb-page-form textarea[name="content_json"]');
  if (!toolbar || !textarea || kbEditorBound) return;
  kbEditorBound = true;
  toolbar.addEventListener('click', (event) => {
    const button = event.target.closest('[data-kb-cmd]');
    if (!button) return;
    event.preventDefault();
    kbApplyFormat(textarea, button.dataset.kbCmd);
  });
  const textColor = document.querySelector('#kb-color-text');
  const bgColor = document.querySelector('#kb-color-bg');
  textColor?.addEventListener('change', () => { kbApplyFormat(textarea, 'color', textColor.value); syncKbToolChips(); });
  bgColor?.addEventListener('change', () => { kbApplyFormat(textarea, 'bg', bgColor.value); syncKbToolChips(); });
  syncKbToolChips();
  // Ссылки через `@`: в системных разделах («Зерно»/«Напитки») статья пишется
  // в этом textarea, поэтому подсказка нужна именно здесь.
  attachPageLinkSuggest(textarea, 'textarea');
}

/* --- Блок-редактор страниц (Editor.js, локальный вендоринг) ---------------
   Интерактивный редактор поднимаем только для wiki/standards и только когда
   библиотека загрузилась (управляющие роли). Иначе остаётся прежняя textarea. */

/* Кастомный Image Tool: грузит фото на штатный `POST /api/kb/photos`
   (raw body + CSRF) и вставляет блок с нашим URL — без multipart. */
class KbImageTool {
  constructor({ data }) {
    this.data = { url: (data && data.url) || '', caption: (data && data.caption) || '' };
  }

  static get toolbox() {
    return {
      title: 'Фото',
      icon: '<svg width="17" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>',
    };
  }

  render() {
    const wrap = document.createElement('div');
    wrap.className = 'kb-image-tool';
    if (this.data.url) wrap.appendChild(this._figure());
    const label = document.createElement('label');
    label.className = 'kb-photo-pick';
    label.textContent = this.data.url ? 'Заменить фото' : 'Загрузить фото';
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    label.appendChild(input);
    const status = document.createElement('p');
    status.className = 'kb-image-tool-status';
    input.addEventListener('change', () => this._upload(input.files && input.files[0], wrap));
    wrap.appendChild(label);
    wrap.appendChild(status);
    return wrap;
  }

  _figure() {
    const figure = document.createElement('figure');
    figure.className = 'kb-image';
    const image = document.createElement('img');
    image.src = this.data.url;
    image.alt = this.data.caption || '';
    image.loading = 'lazy';
    image.setAttribute('data-media-url', this.data.url);
    figure.appendChild(image);
    if (this.data.caption) {
      const caption = document.createElement('figcaption');
      caption.textContent = this.data.caption;
      figure.appendChild(caption);
    }
    return figure;
  }

  async _upload(file, wrap) {
    if (!file) return;
    const status = wrap.querySelector('.kb-image-tool-status');
    if (status) status.textContent = 'Загрузка…';
    try {
      const response = await fetch('/api/kb/photos', {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': file.type || 'application/octet-stream' },
        body: file,
        credentials: 'same-origin',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить фото');
      this.data.url = data.url;
      const label = wrap.querySelector('.kb-photo-pick');
      wrap.querySelector('figure.kb-image')?.remove();
      wrap.insertBefore(this._figure(), label || wrap.firstChild);
      if (label) label.firstChild.nodeValue = 'Заменить фото';
      if (status) status.textContent = '';
    } catch (error) {
      if (status) status.textContent = error.message;
    }
  }

  save() { return { url: this.data.url, caption: this.data.caption }; }
}

/* Русская локализация интерфейса Editor.js.
   Ключи словаря — оригинальные английские строки (так их ищет I18n._t),
   поэтому переводим только значения. `ui` — ядро (подсказки тулбара, тулбокс,
   поиск, настройки блока), `toolNames` — подписи блоков в тулбоксе,
   `tools.<ключ>` — надписи плагинов, `blockTunes.<ключ>` — меню блока. */
function kbEditorMessages() {
  return {
    ui: {
      blockTunes: { toggler: { 'Click to tune': 'Настроить блок', 'or drag to move': 'или перетащите мышью' } },
      inlineToolbar: { converter: { 'Convert to': 'Преобразовать в' } },
      toolbar: { toolbox: { Add: 'Добавить блок' } },
      popover: { Filter: 'Поиск', 'Nothing found': 'Ничего не найдено', 'Convert to': 'Преобразовать в' },
    },
    toolNames: {
      Text: 'Текст',
      Link: 'Ссылка',
      Bold: 'Полужирный',
      Italic: 'Курсив',
      Heading: 'Заголовок',
      'Unordered List': 'Маркированный список',
      'Ordered List': 'Нумерованный список',
      Checklist: 'Чек-лист',
      Table: 'Таблица',
      Quote: 'Цитата',
      Delimiter: 'Разделитель',
    },
    tools: {
      link: { 'Add a link': 'Вставьте ссылку' },
      stub: { 'The block can not be displayed correctly.': 'Блок не может быть отображён корректно' },
      header: { 'Heading 2': 'Заголовок 2', 'Heading 3': 'Заголовок 3', 'Heading 4': 'Заголовок 4' },
      list: {
        Unordered: 'Маркированный',
        Ordered: 'Нумерованный',
        Checklist: 'Чек-лист',
        'Start with': 'Начать с',
        'Counter type': 'Тип нумерации',
        Numeric: 'Цифры',
        'Lower Roman': 'Римские строчные',
        'Upper Roman': 'Римские заглавные',
        'Lower Alpha': 'Латинские строчные',
        'Upper Alpha': 'Латинские заглавные',
      },
      table: {
        Heading: 'Заголовок',
        'Add column to left': 'Вставить столбец слева',
        'Add column to right': 'Вставить столбец справа',
        'Add row above': 'Вставить строку выше',
        'Add row below': 'Вставить строку ниже',
        'Delete column': 'Удалить столбец',
        'Delete row': 'Удалить строку',
        'With headings': 'С заголовками',
        'Without headings': 'Без заголовков',
        Collapse: 'Свернуть',
        Stretch: 'Растянуть',
      },
      quote: {
        'Enter a quote': 'Введите цитату',
        'Enter a caption': 'Подпись (необязательно)',
        'Align Left': 'По левому краю',
        'Align Center': 'По центру',
      },
    },
    blockTunes: {
      delete: { Delete: 'Удалить', 'Click to delete': 'Нажмите, чтобы удалить' },
      moveUp: { 'Move up': 'Переместить вверх' },
      moveDown: { 'Move down': 'Переместить вниз' },
      'move-up': { 'Move up': 'Переместить вверх' },
      'move-down': { 'Move down': 'Переместить вниз' },
    },
  };
}

/* Инструменты Editor.js: базовые плагины + кастомная картинка. */
function kbEditorTools() {
  return {
    header: { class: window.Header, inlineToolbar: true, config: { levels: [2, 3, 4], defaultLevel: 2, placeholder: 'Заголовок' } },
    list: { class: window.EditorjsList, inlineToolbar: true, config: { defaultStyle: 'unordered' } },
    checklist: { class: window.Checklist, inlineToolbar: true },
    table: { class: window.Table, inlineToolbar: true },
    quote: { class: window.Quote, inlineToolbar: true },
    delimiter: { class: window.Delimiter },
    image: { class: KbImageTool },
  };
}

/* Читает текущее содержимое в блоки Editor.js: новый JSON — как есть, легаси
   текст/Markdown — одним параграфом, чтобы ничего не потерять. */
function kbParseContentToBlocks() {
  const textarea = document.querySelector('#kb-page-form textarea[name="content_json"]');
  const raw = textarea ? textarea.value.trim() : '';
  if (!raw) return [];
  let parsed = null;
  try { parsed = JSON.parse(raw); } catch (error) { parsed = null; }
  if (parsed && typeof parsed === 'object' && Array.isArray(parsed.blocks)) {
    return parsed.blocks.filter((block) => block && typeof block.type === 'string');
  }
  return [{ type: 'paragraph', data: { text: escapeHtml(raw).replace(/\n/g, '<br>') } }];
}

/* Доступны ли ядро Editor.js и нужные плагины (иначе — фолбэк-textarea). */
function kbEditorAvailable() {
  return typeof window.EditorJS === 'function'
    && typeof window.Header === 'function'
    && typeof window.EditorjsList === 'function'
    && typeof window.Checklist === 'function'
    && typeof window.Table === 'function'
    && typeof window.Quote === 'function'
    && typeof window.Delimiter === 'function';
}

function kbEnsureEditor() {
  if (kbEditorInstance || !kbEditorAvailable()) return;
  const mount = document.querySelector('#kb-editor');
  if (!mount) return;
  try {
    kbEditorInstance = new window.EditorJS({
      holder: mount,
      data: { blocks: kbParseContentToBlocks() },
      placeholder: 'Начните писать или нажмите «+» слева…',
      minHeight: 120,
      i18n: { messages: kbEditorMessages() },
      tools: kbEditorTools(),
    });
    // `@` внутри блоков: подписка на контейнер, потому что блоки создаются и
    // удаляются динамически, а их события всплывают до redactor.
    attachPageLinkSuggest(mount.querySelector('.codex-editor__redactor') || mount, 'editable');
  } catch (error) {
    kbEditorInstance = null;
    showToast('Не удалось открыть редактор — используется простой режим');
  }
}

function kbDestroyEditor() {
  if (kbEditorInstance) {
    try { kbEditorInstance.destroy(); } catch (error) { /* Editor.js мог не успеть */ }
    kbEditorInstance = null;
  }
  pageLinkClose();
  const mount = document.querySelector('#kb-editor');
  if (mount) mount.innerHTML = '';
}

/* Переключает редактор/фолбэк под категорию и доступность библиотеки. */
function kbSyncEditorForCategory() {
  const form = document.querySelector('#kb-page-form');
  if (!form) return;
  const category = form.elements.category.value;
  const usesEditor = !kbIsSystemSection(category);
  const canUseEditor = kbEditorAvailable();
  const fallback = document.querySelector('#kb-editor-fallback');
  const mount = document.querySelector('#kb-editor');
  if (usesEditor && canUseEditor) {
    if (fallback) fallback.hidden = true;
    if (mount) mount.hidden = false;
    kbEnsureEditor();
    // Не поднялся (сбой инициализации) — возвращаем текстовый фолбэк.
    if (!kbEditorInstance) {
      if (fallback) fallback.hidden = false;
      if (mount) mount.hidden = true;
    }
  } else {
    kbDestroyEditor();
    if (fallback) fallback.hidden = false;
    if (mount) mount.hidden = true;
  }
}

/* Собирает содержимое для сохранения: блоки Editor.js в JSON либо текст из
   фолбэк-textarea. */
async function kbCollectContent() {
  const textarea = document.querySelector('#kb-page-form textarea[name="content_json"]');
  if (kbEditorInstance) {
    try {
      const data = await kbEditorInstance.save();
      const blocks = (data && data.blocks) || [];
      if (blocks.length) return JSON.stringify({ time: Date.now(), blocks, version: '2.31.7' });
      return null;
    } catch (error) { /* редактор не готов — падаем на textarea */ }
  }
  return textarea ? (textarea.value.trim() || null) : null;
}

/* --- Ссылки на статьи через `@` -------------------------------------------
   Один компонент на все поля: редактор статьи (textarea и блоки Editor.js),
   текст анонса и (в chat.js) панель ввода чата. Печатаете `@` — ищем страницу
   по заголовку и вставляем маркер `@[Заголовок](kb:12)`, который `kbLinkify`
   превращает в ссылку. Поиск — `/api/kb/link-targets`; без сети берём
   оглавление из кэша, который пишет `loadKnowledgeBase`. */

const KB_LINK_LIMIT = 6;
const kbLinkCache = new Map();

async function kbSearchPages(query) {
  const key = String(query == null ? '' : query).trim().toLowerCase();
  if (kbLinkCache.has(key)) return kbLinkCache.get(key);
  let items;
  try {
    const data = await api(`/api/kb/link-targets?q=${encodeURIComponent(key)}&limit=${KB_LINK_LIMIT}`);
    items = (data.items || []).map((item) => ({
      id: Number(item.id),
      title: String(item.title || ''),
      category: item.category,
      category_label: item.category_label || kbCategoryLabel(item.category),
    }));
  } catch (error) {
    items = kbLocalLinkTargets(key);
  }
  if (kbLinkCache.size > 40) kbLinkCache.clear();
  kbLinkCache.set(key, items);
  return items;
}

/* Офлайн-фолбэк: та же копия оглавления, что показывает сам раздел. */
function kbLocalLinkTargets(key) {
  const cached = readCache(CACHE_KEY_KB_LIST);
  const items = cached && Array.isArray(cached.items) ? cached.items : kbState.items;
  return (items || [])
    .filter((item) => !key || String(item.title || '').toLowerCase().includes(key))
    .slice(0, KB_LINK_LIMIT)
    .map((item) => ({
      id: Number(item.id),
      title: String(item.title || ''),
      category: item.category,
      category_label: kbCategoryLabel(item.category),
    }));
}

const pageLinkPicker = { owner: null, mode: 'textarea', items: [], index: 0, context: null, anchor: null, seq: 0, timer: null };
let pageLinkPopup = null;

/* Подсказка живёт в том же контейнере, что и поле: диалог — top layer, а стекло
   задаёт ему containing block, поэтому координаты считаем от хозяина. */
function pageLinkPopupNode(owner) {
  const host = owner && owner.closest ? (owner.closest('dialog') || document.body) : document.body;
  if (!pageLinkPopup) {
    pageLinkPopup = document.createElement('div');
    pageLinkPopup.className = 'page-link-popup';
    pageLinkPopup.setAttribute('role', 'listbox');
    pageLinkPopup.setAttribute('aria-label', 'Ссылка на статью базы знаний');
    pageLinkPopup.hidden = true;
    pageLinkPopup.addEventListener('pointerdown', (event) => {
      const button = event.target.closest('[data-page-link-index]');
      if (!button) return;
      event.preventDefault();          // не уводим фокус с поля ввода
      pageLinkApply(Number(button.dataset.pageLinkIndex));
    });
  }
  if (pageLinkPopup.parentElement !== host) host.appendChild(pageLinkPopup);
  pageLinkPopup.classList.toggle('is-fixed', host === document.body);
  return pageLinkPopup;
}

function positionPageLinkPopup(node, anchor) {
  const host = node.parentElement || document.body;
  const fixed = node.classList.contains('is-fixed');
  const box = fixed
    ? { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight }
    : host.getBoundingClientRect();
  const width = Math.max(200, Math.min(360, anchor.right - anchor.left));
  const spaceAbove = anchor.top - box.top - 8;
  const spaceBelow = box.bottom - anchor.bottom - 8;
  // Над полем места мало — раскрываемся вниз (в диалоге поле бывает у шапки).
  const below = spaceAbove < 180 && spaceBelow > spaceAbove;
  const offsetTop = fixed ? 0 : host.scrollTop;
  const offsetLeft = fixed ? 0 : host.scrollLeft;
  node.style.width = `${width}px`;
  node.style.maxHeight = `${Math.max(120, Math.min(280, below ? spaceBelow : spaceAbove))}px`;
  node.style.left = `${Math.min(Math.max(box.left + 8, anchor.left), Math.max(box.left + 8, box.right - width - 8)) - box.left + offsetLeft}px`;
  node.style.top = `${(below ? anchor.bottom + 6 : anchor.top - 6) - box.top + offsetTop}px`;
  node.style.transform = below ? 'none' : 'translateY(-100%)';
}

function pageLinkRender() {
  const node = pageLinkPopupNode(pageLinkPicker.owner);
  const items = pageLinkPicker.items;
  if (!items.length) { pageLinkClose(); return; }
  const active = pageLinkPicker.index;
  node.innerHTML = '<p class="page-link-group">База знаний</p>' + items.map((item, index) => `
    <button class="page-link-item${index === active ? ' is-active' : ''}" type="button" role="option" aria-selected="${index === active}" data-page-link-index="${index}">
      <svg class="icon" aria-hidden="true"><use href="#i-book"></use></svg>
      <span class="page-link-name">${escapeHtml(item.title)}</span>
      <span class="page-link-section">${escapeHtml(item.category_label || '')}</span>
    </button>`).join('');
  node.hidden = false;
  positionPageLinkPopup(node, pageLinkPicker.anchor);
  // Подсветку держим в поле зрения вручную: `scrollIntoView` проскроллил бы
  // ещё и диалог-родитель, а это выглядит как «прыжок» формы.
  const focused = node.querySelector('.page-link-item.is-active');
  if (focused) node.scrollTop = Math.max(0, focused.offsetTop - node.clientHeight / 2);
}

/* Токен `@…` перед каретой. Пробелы внутри разрешены: заголовки у статей
   многословные, поэтому поиск идёт по набранной фразе целиком. */
const KB_LINK_TOKEN_RE = /(?:^|\s)@([^@\n\]()]{0,40})$/;

function textareaLinkContext(node) {
  const value = node.value || '';
  const caret = node.selectionStart == null ? value.length : node.selectionStart;
  const match = KB_LINK_TOKEN_RE.exec(value.slice(0, caret));
  if (!match) return null;
  return { query: match[1].trim(), start: caret - match[1].length - 1, caret };
}

function editableRange(node) {
  const selection = window.getSelection();
  if (!selection || !selection.rangeCount) return null;
  const range = selection.getRangeAt(0);
  return node.contains(range.startContainer) ? range : null;
}

function editableLinkContext(node) {
  const range = editableRange(node);
  if (!range || !range.collapsed || range.startContainer.nodeType !== 3) return null;
  const before = range.startContainer.nodeValue.slice(0, range.startOffset);
  const match = KB_LINK_TOKEN_RE.exec(before);
  if (!match) return null;
  return {
    query: match[1].trim(),
    startNode: range.startContainer,
    startOffset: range.startOffset - match[1].length - 1,
    endNode: range.startContainer,
    endOffset: range.startOffset,
    range,
  };
}

function fieldAnchor(node) {
  const rect = node && node.getBoundingClientRect ? node.getBoundingClientRect() : null;
  if (!rect) return { left: 8, right: 208, top: window.innerHeight / 2, bottom: window.innerHeight / 2 };
  return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
}

/* Каретка внутри contenteditable: у свёрнутого range в части браузеров нет
   клиентских прямоугольников, поэтому падаем на блок, в котором стоит каретка. */
function caretAnchor(node) {
  const range = editableRange(node);
  const rect = range && range.getClientRects()[0];
  if (rect) return { left: rect.left, right: rect.left + 1, top: rect.top, bottom: rect.bottom };
  const holder = range && range.startContainer.parentElement
    ? (range.startContainer.parentElement.closest('.ce-block') || range.startContainer.parentElement)
    : node;
  return fieldAnchor(holder);
}

function pageLinkUpdate(node, mode) {
  const context = mode === 'editable' ? editableLinkContext(node) : textareaLinkContext(node);
  if (!context) {
    if (pageLinkPicker.owner === node) pageLinkClose();
    return;
  }
  pageLinkPicker.owner = node;
  pageLinkPicker.mode = mode;
  pageLinkPicker.context = context;
  pageLinkPicker.anchor = mode === 'editable' ? caretAnchor(node) : fieldAnchor(node);
  const seq = ++pageLinkPicker.seq;
  clearTimeout(pageLinkPicker.timer);
  pageLinkPicker.timer = setTimeout(async () => {
    const items = await kbSearchPages(context.query);
    // Пока искали, могли набрать ещё символ или выбрать другое поле — тогда молчим.
    if (seq !== pageLinkPicker.seq || pageLinkPicker.owner !== node) return;
    pageLinkPicker.items = items;
    pageLinkPicker.index = 0;
    pageLinkRender();
  }, 150);
}

function pageLinkClose() {
  pageLinkPicker.owner = null;
  pageLinkPicker.items = [];
  pageLinkPicker.index = 0;
  pageLinkPicker.context = null;
  pageLinkPicker.seq += 1;            // ответы «в полёте» больше не применяем
  clearTimeout(pageLinkPicker.timer);
  if (pageLinkPopup) { pageLinkPopup.hidden = true; pageLinkPopup.innerHTML = ''; }
}

function pageLinkApply(index) {
  const item = pageLinkPicker.items[index];
  const node = pageLinkPicker.owner;
  const context = pageLinkPicker.context;
  if (!item || !node || !context) { pageLinkClose(); return; }
  const marker = `@[${kbPageLinkTitle(item.title)}](kb:${Number(item.id)}) `;
  if (pageLinkPicker.mode === 'editable') {
    try {
      context.range.setStart(context.startNode, context.startOffset);
      context.range.setEnd(context.endNode, context.endOffset);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(context.range);
      // Вставка «как с клавиатуры»: Editor.js получает input и сохраняет текст.
      document.execCommand('insertText', false, marker);
    } catch (error) { /* браузер без execCommand — поле остаётся как было */ }
    // Фокус не трогаем: он уже в блоке, а `focus()` на контейнере сбросил бы каретку.
  } else {
    const value = node.value || '';
    node.value = value.slice(0, context.start) + marker + value.slice(context.caret);
    const caret = context.start + marker.length;
    try { node.setSelectionRange(caret, caret); } catch (error) { /* не поддерживается */ }
    node.focus();
    node.dispatchEvent(new Event('input', { bubbles: true }));
  }
  pageLinkClose();
}

/* Слушатель один на документ: подсказка реагирует только на своё поле, а
   capture нужен, чтобы Enter/Escape не улетели в диалог или Editor.js первыми. */
document.addEventListener('keydown', (event) => {
  const owner = pageLinkPicker.owner;
  if (!owner || !pageLinkPicker.items.length) return;
  if (event.target !== owner && !(owner.contains && owner.contains(event.target))) return;
  const count = pageLinkPicker.items.length;
  if (event.key === 'ArrowDown') {
    event.preventDefault();
    pageLinkPicker.index = (pageLinkPicker.index + 1) % count;
    pageLinkRender();
  } else if (event.key === 'ArrowUp') {
    event.preventDefault();
    pageLinkPicker.index = (pageLinkPicker.index - 1 + count) % count;
    pageLinkRender();
  } else if (event.key === 'Enter' || event.key === 'Tab') {
    event.preventDefault();
    pageLinkApply(pageLinkPicker.index);
  } else if (event.key === 'Escape') {
    event.preventDefault();
    pageLinkClose();
  }
}, true);

/* Подключает подсказку: `textarea` — обычный ввод, `editable` — контейнер
   Editor.js (каретка живёт внутри contenteditable, а события всплывают). */
function attachPageLinkSuggest(node, mode = 'textarea') {
  if (!node || node.dataset.pageLinks === '1') return;
  node.dataset.pageLinks = '1';
  node.addEventListener('input', () => pageLinkUpdate(node, mode));
  node.addEventListener('focusout', (event) => {
    if (pageLinkPicker.owner !== node) return;
    const next = event.relatedTarget;
    if (next && node.contains(next)) return;
    pageLinkClose();
  });
}

/* --- Теги страницы: чипсы в диалоге --------------------------------------- */

function kbNormalizeTag(value) {
  return String(value == null ? '' : value).replace(/\s+/g, ' ').trim().slice(0, KB_MAX_TAG_LEN);
}

function kbSetDialogTags(tags) {
  const seen = new Set();
  const list = [];
  for (const item of (Array.isArray(tags) ? tags : [])) {
    const tag = kbNormalizeTag(item);
    const key = tag.toLowerCase();
    if (!tag || seen.has(key)) continue;
    seen.add(key);
    list.push(tag);
    if (list.length >= KB_MAX_TAGS) break;
  }
  kbDialogTags = list;
  kbRenderTagChips();
}

function kbRenderTagChips() {
  const holder = document.querySelector('#kb-tag-chips');
  if (holder) {
    holder.innerHTML = kbDialogTags.map((tag) => `<span class="kb-tag-chip">${escapeHtml(tag)}<button class="kb-tag-remove" type="button" data-kb-tag-remove="${escapeHtml(tag)}" aria-label="Убрать тег ${escapeHtml(tag)}"><svg class="icon" aria-hidden="true"><use href="#i-close"></use></svg></button></span>`).join('');
  }
  const form = document.querySelector('#kb-page-form');
  const hidden = form ? form.elements.tags : null;
  if (hidden) hidden.value = JSON.stringify(kbDialogTags);
}

function kbAddDialogTag(value) {
  const tag = kbNormalizeTag(value);
  if (!tag) return;
  const key = tag.toLowerCase();
  if (kbDialogTags.some((item) => item.toLowerCase() === key)) return;
  if (kbDialogTags.length >= KB_MAX_TAGS) { showToast(`Не больше ${KB_MAX_TAGS} тегов`); return; }
  kbDialogTags.push(tag);
  kbRenderTagChips();
}

function kbRemoveDialogTag(tag) {
  const key = kbNormalizeTag(tag).toLowerCase();
  kbDialogTags = kbDialogTags.filter((item) => item.toLowerCase() !== key);
  kbRenderTagChips();
}

/* Подсказки тегов: уникальные метки из всех загруженных страниц. */
function kbRenderTagSuggestions() {
  const list = document.querySelector('#kb-tag-suggestions');
  if (!list) return;
  const seen = new Set();
  const tags = [];
  for (const item of kbState.items) {
    for (const tag of (Array.isArray(item.tags) ? item.tags : [])) {
      const key = String(tag).toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      tags.push(String(tag));
    }
  }
  tags.sort((a, b) => a.localeCompare(b, 'ru'));
  list.innerHTML = tags.map((tag) => `<option value="${escapeHtml(tag)}"></option>`).join('');
}

function setupKbTags() {
  const input = document.querySelector('#kb-tag-input');
  if (!input || kbTagsBound) return;
  kbTagsBound = true;
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ',') {
      event.preventDefault();
      kbAddDialogTag(input.value);
      input.value = '';
    } else if (event.key === 'Backspace' && !input.value && kbDialogTags.length) {
      kbRemoveDialogTag(kbDialogTags[kbDialogTags.length - 1]);
    }
  });
  input.addEventListener('blur', () => { if (input.value.trim()) { kbAddDialogTag(input.value); input.value = ''; } });
  document.querySelector('#kb-tag-chips')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-kb-tag-remove]');
    if (button) kbRemoveDialogTag(button.dataset.kbTagRemove);
  });
  const hidden = document.querySelector('#kb-page-form input[name="tags"]');
  if (hidden) hidden.value = '[]';
}

/* --- Фото подач напитка (raw body + CSRF, как у фото оценок) --------------- */

async function kbUploadDrinkPhoto(field, file) {
  if (!file || !field) return;
  const errorNode = document.querySelector('#kb-form-error');
  if (errorNode) errorNode.hidden = true;
  try {
    const response = await fetch('/api/kb/photos', {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
      credentials: 'same-origin',
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить фото');
    kbSetDrinkPhoto(field, data.url);
  } catch (error) {
    if (errorNode) { errorNode.textContent = error.message; errorNode.hidden = false; }
    else showToast(error.message);
  }
}

/* Проставляет скрытое поле и превью фото подачи. */
function kbSetDrinkPhoto(field, url) {
  const form = document.querySelector('#kb-page-form');
  const hidden = form ? form.elements[`${field}_photo`] : null;
  if (hidden) hidden.value = url || '';
  const preview = document.querySelector(`#kb-photo-${field}`);
  const image = preview ? preview.querySelector('img') : null;
  if (image) {
    if (url) { image.src = url; image.dataset.mediaUrl = url; }
    else { image.removeAttribute('src'); delete image.dataset.mediaUrl; }
  }
  if (preview) preview.hidden = !url;
}

/* Сбрасывает превью и скрытые поля фото подач при открытии диалога. */
function kbResetDrinkPhotos() {
  kbSetDrinkPhoto('serve_here', '');
  kbSetDrinkPhoto('serve_takeaway', '');
}

/* Подключает инпуты и кнопки «Убрать» для фото подач (один раз). */
function setupKbDrinkPhotos() {
  if (kbPhotosBound) return;
  kbPhotosBound = true;
  document.querySelectorAll('[data-kb-photo]').forEach((input) => {
    input.addEventListener('change', (event) => {
      kbUploadDrinkPhoto(event.currentTarget.dataset.kbPhoto, event.currentTarget.files?.[0]);
    });
  });
  document.querySelectorAll('[data-kb-photo-remove]').forEach((button) => {
    button.addEventListener('click', () => {
      const field = button.dataset.kbPhotoRemove;
      kbSetDrinkPhoto(field, '');
      const input = document.querySelector(`[data-kb-photo="${field}"]`);
      if (input) input.value = '';
    });
  });
}

function setupKnowledgeBase() {
  document.querySelector('#kb-filters')?.addEventListener('click', (event) => {
    const chip = event.target.closest('[data-kb-filter]');
    if (!chip) return;
    kbState.filter = chip.dataset.kbFilter;
    kbFilterChips();
    renderKbTree();
  });
  document.querySelector('#kb-tree')?.addEventListener('click', (event) => {
    const toggle = event.target.closest('[data-kb-toggle]');
    if (toggle) { kbToggleNode(Number(toggle.dataset.kbToggle)); return; }
    const category = event.target.closest('[data-kb-category-toggle]');
    if (category) { kbToggleCategory(category.dataset.kbCategoryToggle); return; }
    const node = event.target.closest('[data-kb-open]');
    if (node) openKbPage(Number(node.dataset.kbOpen));
  });
  document.querySelector('#kb-add-page')?.addEventListener('click', () => openKbDialog(null));
  document.querySelector('#kb-page')?.addEventListener('click', (event) => {
    const tag = event.target.closest('[data-kb-tag]');
    if (tag) {
      const root = document.querySelector('#kb-search');
      kbState.query = tag.dataset.kbTag;
      if (root) root.value = kbState.query;
      renderKbTree();
      return;
    }
    const recipe = event.target.closest('[data-kb-recipe]');
    if (recipe) { openDrinkCard(Number(recipe.dataset.kbRecipe)); return; }
    const edit = event.target.closest('[data-kb-edit]');
    if (edit) { openKbDialog(kbState.current); return; }
    const remove = event.target.closest('[data-kb-delete]');
    if (remove) deleteKbPage(Number(remove.dataset.kbDelete));
  });
  document.querySelector('#kb-page-nav')?.addEventListener('click', (event) => {
    const step = event.target.closest('[data-kb-page-step]');
    if (step) kbNavigatePage(Number(step.dataset.kbPageStep) || 1);
  });

  // Свайп по статье: влево — следующая страница из оглавления, вправо — предыдущая.
  const kbPage = document.querySelector('#kb-page');
  if (kbPage && !kbPage.dataset.swipeBound) {
    kbPage.dataset.swipeBound = '1';
    const DRAG_SLOP = 10;       // порог, после которого считаем движение свайпом
    const SNAP_FRACTION = 0.22; // доля ширины, достаточная для перелистывания
    const FLICK = 0.45;         // px/мс — быстрый флик листает без полного сдвига
    let drag = null;
    let suppressClick = false;  // свайп не должен превращаться в клик по статье

    const pageWidth = () => kbPage.clientWidth || 1;

    kbPage.addEventListener('click', (event) => {
      if (!suppressClick) return;
      event.preventDefault();
      event.stopPropagation();
      suppressClick = false;
    }, { capture: true });

    kbPage.addEventListener('pointerdown', (event) => {
      if (event.button) { drag = null; return; }            // только основная кнопка/касание
      if (event.target.closest('a, button, input, select, textarea, label, img, .kb-table-wrap')) { drag = null; return; }
      drag = { x: event.clientX, y: event.clientY, axis: null, lastX: event.clientX, lastT: performance.now() };
      kbPage.setPointerCapture?.(event.pointerId);
    });

    kbPage.addEventListener('pointermove', (event) => {
      if (!drag) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (!drag.axis) {
        if (Math.abs(dx) < DRAG_SLOP && Math.abs(dy) < DRAG_SLOP) return;
        drag.axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
      }
      if (drag.axis !== 'x') return;                        // вертикаль отдаём скроллу
      event.preventDefault();
      drag.lastX = event.clientX;
      drag.lastT = performance.now();
    });

    const settle = (event) => {
      if (!drag) return;
      const state = drag;
      drag = null;
      if (state.axis !== 'x') return;
      const endX = event ? event.clientX : state.lastX;
      const dx = endX - state.x;
      const dt = Math.max(1, performance.now() - state.lastT);
      const velocity = (endX - state.lastX) / dt;
      const passed = Math.abs(dx) > pageWidth() * SNAP_FRACTION || Math.abs(velocity) > FLICK;
      // Свайп не должен превращаться в клик по тегу/кнопке внутри статьи.
      if (Math.abs(dx) > DRAG_SLOP) {
        suppressClick = true;
        setTimeout(() => { suppressClick = false; }, 400);
      }
      if (!passed) return;
      const goNext = Math.abs(velocity) > FLICK ? velocity < 0 : dx < 0;
      kbNavigatePage(goNext ? 1 : -1);
    };
    kbPage.addEventListener('pointerup', settle);
    kbPage.addEventListener('pointercancel', () => settle(null));
  }
  const search = document.querySelector('#kb-search');
  if (search) {
    let searchTimer;
    search.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { kbState.query = search.value.trim(); renderKbTree(); }, 120);
    });
  }
  document.querySelector('#kb-pantone-picker')?.addEventListener('click', (event) => {
    const swatch = event.target.closest('[data-pantone]');
    if (swatch) kbSelectPantone(swatch.dataset.pantone);
  });
  document.querySelector('#kb-roast')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-roast]');
    if (button) kbToggleRoast(button.dataset.roast);
  });
  setupKbEditor();
  setupKbDrinkPhotos();
  setupKbTags();
  document.querySelector('#kb-tree-toggle')?.addEventListener('click', () => kbSetTreeOpen(!kbState.treeOpen));
  setupKbTreeFab();
  const form = document.querySelector('#kb-page-form');
  if (form) {
    kbRenderCategorySelect();
    form.elements.category.addEventListener('change', () => { kbSyncFields(); kbSyncEditorForCategory(); });
    form.addEventListener('submit', submitKbForm);
  }
  // Закрыли диалог — гасим Editor.js, чтобы не держать DOM и слушатели.
  document.querySelector('#kb-page-dialog')?.addEventListener('close', kbDestroyEditor);
}

/* --- Черновики форм (localStorage) ---------------------------------------
   Планшет общий, поэтому ключ включает id пользователя. TTL — сутки: черновик
   переживает закрытие модалки и блокировку экрана, но мусор не копится. Один
   черновик на модуль (перезаписывается). */
const DRAFT_TTL_MS = 24 * 60 * 60 * 1000;

function draftKey(scope) {
  return `neft-draft:${document.body.dataset.userId || '0'}:${scope}`;
}

function readDraft(scope) {
  try {
    const raw = localStorage.getItem(draftKey(scope));
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (!data || typeof data !== 'object' || !data.savedAt) return null;
    if (Date.now() - Number(data.savedAt) > DRAFT_TTL_MS) {
      localStorage.removeItem(draftKey(scope));
      return null;
    }
    return data;
  } catch (error) {
    return null;
  }
}

function saveDraft(scope, data) {
  try {
    if (!data) { localStorage.removeItem(draftKey(scope)); return; }
    localStorage.setItem(draftKey(scope), JSON.stringify({ ...data, savedAt: Date.now() }));
  } catch (error) {
    /* Приватный режим — черновики просто не работают. */
  }
}

function clearDraft(scope) {
  try { localStorage.removeItem(draftKey(scope)); } catch (error) { /* приватный режим */ }
}

/* Поля формы как объект `{name: value}` — годится и анонсам, и базе знаний. */
function serializeFields(form) {
  const data = {};
  Array.from(form.elements).forEach((node) => {
    if (!node.name || ['file', 'submit', 'button', 'reset'].includes(node.type)) return;
    if (node.type === 'checkbox') { data[node.name] = node.checked; return; }
    if (node.type === 'radio') {
      if (node.checked || !(node.name in data)) data[node.name] = node.checked ? node.value : '';
      return;
    }
    data[node.name] = node.value;
  });
  return data;
}

function applyFields(form, data) {
  if (!data) return;
  Array.from(form.elements).forEach((node) => {
    if (!node.name || !(node.name in data)) return;
    if (node.type === 'checkbox') { node.checked = Boolean(data[node.name]); return; }
    if (node.type === 'radio') { node.checked = String(data[node.name]) === node.value; return; }
    node.value = data[node.name] == null ? '' : String(data[node.name]);
  });
}

function clearDraftNotice(form) {
  form.querySelector('[data-draft-notice]')?.remove();
}

/* Плашка «черновик восстановлен» внутри формы. */
function renderDraftNotice(form, scope, onClear) {
  clearDraftNotice(form);
  form.insertAdjacentHTML('afterbegin',
    '<div class="draft-notice" data-draft-notice><span>Черновик восстановлен — можно продолжить с того же места.</span><button class="button button-quiet" type="button" data-draft-clear>Очистить</button></div>');
  form.querySelector('[data-draft-clear]')?.addEventListener('click', () => {
    clearDraft(scope);
    clearDraftNotice(form);
    if (onClear) onClear();
  });
}

/* Автосохранение: debounce на ввод, а также на закрытие модалки и уход в фон
   (мобильный сценарий «заблокировал телефон» модалку не закрывает). */
function bindDraftAutosave(form, scope, serialize) {
  if (!form || form.dataset.draftBound === scope) return;
  form.dataset.draftBound = scope;
  let timer = null;
  const flush = () => {
    if (timer) { clearTimeout(timer); timer = null; }
    saveDraft(scope, serialize());
  };
  const schedule = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(flush, 400);
  };
  form.addEventListener('input', schedule);
  form.addEventListener('change', schedule);
  form.closest('dialog')?.addEventListener('close', flush);
  document.addEventListener('visibilitychange', () => { if (document.hidden) flush(); });
  window.addEventListener('pagehide', flush);
  return flush;
}

/* --- Черновик «Описание напитков» (строки, шкалы, заметка, фото) --------- */
function serializeDrinkDraft() {
  const items = Array.from(document.querySelectorAll('#drink-items .drink-item-form')).map((row) => {
    const fields = {};
    row.querySelectorAll('[data-drink-field]').forEach((node) => {
      fields[node.dataset.drinkField] = node.type === 'checkbox' ? node.checked : node.value;
    });
    const scales = {};
    row.querySelectorAll('[data-drink-scale]').forEach((node) => {
      scales[node.dataset.drinkScale] = node.value;
    });
    return { category: row.dataset.category, fields, scales };
  });
  const photos = {};
  ['latte_art', 'meme'].forEach((kind) => {
    photos[kind] = (drinkState.photos[kind] || []).map((entry) => ({ url: entry.url, author_id: entry.author_id }));
  });
  const partnerRow = document.querySelector('#drink-partner-row');
  return {
    items,
    note: document.querySelector('#drink-note')?.value || '',
    point: document.querySelector('#drink-point')?.value || '',
    partner: document.querySelector('#drink-partner')?.value || '',
    manual: Boolean(partnerRow && !partnerRow.hidden),
    photos,
  };
}

function restoreDrinkDraft(draft) {
  const container = document.querySelector('#drink-items');
  if (!container) return;
  container.innerHTML = '';
  (draft.items || []).forEach((entry) => {
    addDrinkItemRow(entry.category);
    const row = container.lastElementChild;
    if (!row) return;
    Object.entries(entry.fields || {}).forEach(([field, value]) => {
      const node = row.querySelector(`[data-drink-field="${field}"]`);
      if (!node) return;
      if (node.type === 'checkbox') node.checked = Boolean(value);
      else node.value = value == null ? '' : String(value);
    });
    Object.entries(entry.scales || {}).forEach(([attr, value]) => {
      const node = row.querySelector(`[data-drink-scale="${attr}"]`);
      if (!node) return;
      node.value = value;
      const list = (drinkState.scales || DRINK_FALLBACK_SCALES)[node.dataset.drinkGroup] || [];
      const out = node.parentElement.querySelector('output');
      if (out) out.textContent = list[Number(node.value)] || '';
    });
  });
  const note = document.querySelector('#drink-note');
  if (note) note.value = draft.note || '';
  const manualRow = document.querySelector('#drink-partner-row');
  if (manualRow && draft.manual) manualRow.hidden = false;
  const point = document.querySelector('#drink-point');
  if (point && draft.point) point.value = draft.point;
  const partner = document.querySelector('#drink-partner');
  if (partner && draft.partner !== undefined) partner.value = draft.partner || '';
  ['latte_art', 'meme'].forEach((kind) => {
    drinkState.photos[kind] = ((draft.photos || {})[kind] || []).map((entry) => ({
      url: entry.url, author_id: entry.author_id, author_name: drinkBaristaName(entry.author_id),
    }));
  });
  // Если справочник напарников не загрузился (офлайн), не отфильтровываем фото
  // по паре — иначе потеряли бы снимки напарника.
  if (partner && partner.options.length > 1) {
    refreshDrinkBaristas();
  } else {
    drinkState.baristas = drinkBaristas();
    renderDrinkUploads();
    renderDrinkPreviews('latte_art');
    renderDrinkPreviews('meme');
  }
  drinkSyncEspresso();
}

/* Кнопка «Очистить» в черновике: возвращаем форму к состоянию «с нуля». */
function resetDrinkDraftForm() {
  const container = document.querySelector('#drink-items');
  if (container) container.innerHTML = '';
  addDrinkItemRow('черный кофе');
  const note = document.querySelector('#drink-note');
  if (note) note.value = '';
  drinkState.photos = { latte_art: [], meme: [] };
  renderDrinkPreviews('latte_art');
  renderDrinkPreviews('meme');
}

/* Есть ли в форме что сохранять: пустую форму черновиком не считаем, иначе
   плашка «черновик восстановлен» всплывала бы после простого открытия модалки. */
function drinkDraftHasContent(draft) {
  if (!draft) return false;
  if (String(draft.note || '').trim()) return true;
  if (((draft.photos || {}).latte_art || []).length) return true;
  if (((draft.photos || {}).meme || []).length) return true;
  if ((draft.items || []).length > 1) return true;
  return (draft.items || []).some((item) =>
    Object.values(item.fields || {}).some((value) => value !== '' && value !== false));
}

/* --- Подключение черновиков к формам ------------------------------------- */
function setupFormDrafts() {
  const drinkForm = document.querySelector('#drink-form');
  if (drinkForm) {
    drinkState.flushDraft = bindDraftAutosave(drinkForm, 'drink', () => {
      const draft = serializeDrinkDraft();
      return drinkDraftHasContent(draft) ? draft : null;
    });
  }
  const kbForm = document.querySelector('#kb-page-form');
  if (kbForm) {
    // Правим существующую страницу — черновик не нужен и только мешает.
    bindDraftAutosave(kbForm, 'kb', () => (kbForm.dataset.pageId ? null : { fields: serializeFields(kbForm) }));
  }
  const announcementForm = document.querySelector('#announcement-form');
  if (announcementForm) {
    bindDraftAutosave(announcementForm, 'announcement',
      () => (announcementForm.dataset.announcementId ? null : { fields: serializeFields(announcementForm) }));
  }
}

/* --- Просмотр изображений (лайтбокс) ------------------------------------
   Один диалог на всё приложение: зум (колесо / пинч / двойной тап),
   панорамирование и свайп по галерее. Жесты — через Pointer Events, поэтому
   одинаково работают мышью и пальцем. Галерея — ближайший предок с
   `data-media-gallery`, элементы — с `data-media-url`. */
const MEDIA_MAX_SCALE = 6;
const MEDIA_SWIPE_SLOP = 60;
const MEDIA_TAP_SLOP = 10;
const mediaState = {
  items: [], index: 0, scale: 1, x: 0, y: 0,
  pointers: new Map(), pinchStart: 0, pinchScale: 1,
  dragOrigin: null, moved: false, lastTap: 0,
};

function mediaStageRect() {
  return document.querySelector('#media-viewer-stage')?.getBoundingClientRect() || null;
}

function mediaApplyTransform() {
  const image = document.querySelector('#media-viewer-image');
  if (!image) return;
  image.style.transform = `translate3d(${mediaState.x}px, ${mediaState.y}px, 0) scale(${mediaState.scale})`;
}

/* Держим картинку в границах: при увеличении сдвиг не больше её «запаса». */
function mediaClampPan() {
  const rect = mediaStageRect();
  if (!rect) return;
  const maxX = Math.max(0, (rect.width * (mediaState.scale - 1)) / 2);
  const maxY = Math.max(0, (rect.height * (mediaState.scale - 1)) / 2);
  mediaState.x = Math.min(maxX, Math.max(-maxX, mediaState.x));
  mediaState.y = Math.min(maxY, Math.max(-maxY, mediaState.y));
}

function mediaResetZoom() {
  mediaState.scale = 1;
  mediaState.x = 0;
  mediaState.y = 0;
  mediaApplyTransform();
}

function mediaZoom(factor) {
  mediaState.scale = Math.min(MEDIA_MAX_SCALE, Math.max(1, mediaState.scale * factor));
  if (mediaState.scale === 1) { mediaState.x = 0; mediaState.y = 0; }
  mediaClampPan();
  mediaApplyTransform();
}

function mediaRenderItem() {
  const item = mediaState.items[mediaState.index];
  if (!item) return;
  const image = document.querySelector('#media-viewer-image');
  const count = document.querySelector('#media-viewer-count');
  const open = document.querySelector('#media-viewer-open');
  const hint = document.querySelector('#media-viewer-hint');
  if (image) {
    image.src = item.url;
    image.alt = item.caption || 'Просмотр изображения';
  }
  if (count) count.textContent = mediaState.items.length > 1 ? `${mediaState.index + 1} / ${mediaState.items.length}` : '';
  if (open) open.href = item.url;
  if (hint) hint.hidden = mediaState.items.length < 2;
  mediaResetZoom();
}

function mediaStep(delta) {
  const total = mediaState.items.length;
  if (total < 2) return;
  mediaState.index = (mediaState.index + delta + total) % total;
  mediaRenderItem();
}

function openMediaViewer(items, index = 0) {
  const list = (items || [])
    .map((item) => (typeof item === 'string' ? { url: item } : item))
    .filter((item) => item && item.url);
  if (!list.length) return;
  mediaState.items = list;
  mediaState.index = Math.max(0, Math.min(Number(index) || 0, list.length - 1));
  mediaRenderItem();
  const dialog = document.querySelector('#media-viewer');
  if (dialog && !dialog.open) dialog.showModal();
}

function setupMediaViewer() {
  const dialog = document.querySelector('#media-viewer');
  const stage = document.querySelector('#media-viewer-stage');
  if (!dialog || !stage) return;
  // Страховка: вьюер должен быть закрыт до первого показа, иначе он перекроет UI.
  if (dialog.open) dialog.close();
  const image = document.querySelector('#media-viewer-image');
  document.querySelector('#media-viewer-close')?.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { mediaState.items = []; mediaState.pointers.clear(); mediaResetZoom(); });

  // Клик по любому изображению в галерее открывает просмотр. Фото оценок и
  // превью в формах не помечаем в разметке — их (единственное) берём из <img>.
  document.addEventListener('click', (event) => {
    if (event.target.closest('button, a')) return;
    let target = event.target.closest('[data-media-url]');
    if (!target) {
      const image = event.target.closest('img');
      const holder = image && image.closest('#evaluation-photo-preview, .kb-photo-preview');
      if (!holder || !image.src) return;
      openMediaViewer([{ url: image.src, caption: image.alt || '' }], 0);
      return;
    }
    const gallery = target.closest('[data-media-gallery]');
    const nodes = gallery ? Array.from(gallery.querySelectorAll('[data-media-url]')) : [target];
    openMediaViewer(
      nodes.map((node) => ({ url: node.dataset.mediaUrl, caption: node.dataset.mediaCaption || '' })),
      nodes.indexOf(target),
    );
  });

  dialog.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowRight') mediaStep(1);
    else if (event.key === 'ArrowLeft') mediaStep(-1);
    else if (event.key === '+' || event.key === '=') mediaZoom(1.4);
    else if (event.key === '-') mediaZoom(1 / 1.4);
  });

  stage.addEventListener('wheel', (event) => {
    event.preventDefault();
    mediaZoom(event.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });

  const pointerDistance = () => {
    const points = Array.from(mediaState.pointers.values());
    return Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
  };

  stage.addEventListener('pointerdown', (event) => {
    stage.setPointerCapture?.(event.pointerId);
    mediaState.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (mediaState.pointers.size === 1) {
      mediaState.dragOrigin = { x: event.clientX, y: event.clientY, panX: mediaState.x, panY: mediaState.y };
      mediaState.moved = false;
    } else if (mediaState.pointers.size === 2) {
      mediaState.pinchStart = pointerDistance();
      mediaState.pinchScale = mediaState.scale;
    }
  });

  stage.addEventListener('pointermove', (event) => {
    const point = mediaState.pointers.get(event.pointerId);
    if (!point) return;
    mediaState.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (mediaState.pointers.size >= 2) {
      if (mediaState.pinchStart > 0) {
        const factor = pointerDistance() / mediaState.pinchStart;
        mediaState.scale = Math.min(MEDIA_MAX_SCALE, Math.max(1, mediaState.pinchScale * factor));
        if (image) image.classList.add('is-dragging');
        mediaClampPan();
        mediaApplyTransform();
      }
      mediaState.moved = true;
      return;
    }
    const origin = mediaState.dragOrigin;
    if (!origin) return;
    const dx = event.clientX - origin.x;
    const dy = event.clientY - origin.y;
    if (Math.abs(dx) > MEDIA_TAP_SLOP || Math.abs(dy) > MEDIA_TAP_SLOP) mediaState.moved = true;
    if (mediaState.scale > 1) {
      mediaState.x = origin.panX + dx;
      mediaState.y = origin.panY + dy;
      mediaClampPan();
      if (image) image.classList.add('is-dragging');
      mediaApplyTransform();
    }
  });

  const releasePointer = (event) => {
    mediaState.pointers.delete(event.pointerId);
    if (mediaState.pointers.size < 2) mediaState.pinchStart = 0;
    if (image) image.classList.remove('is-dragging');
    if (mediaState.pointers.size > 0) return;
    const origin = mediaState.dragOrigin;
    mediaState.dragOrigin = null;
    if (!origin) return;
    const dx = event.clientX - origin.x;
    const dy = event.clientY - origin.y;
    if (!mediaState.moved) {
      // Одиночный тап — ничего; двойной тап — зум.
      const now = Date.now();
      if (now - mediaState.lastTap < 320) {
        mediaState.lastTap = 0;
        mediaZoom(mediaState.scale > 1 ? 1 / mediaState.scale : 2.5);
        return;
      }
      mediaState.lastTap = now;
      return;
    }
    if (mediaState.scale <= 1.01 && Math.abs(dx) > MEDIA_SWIPE_SLOP && Math.abs(dx) > Math.abs(dy)) {
      mediaState.x = 0;
      mediaState.y = 0;
      mediaStep(dx < 0 ? 1 : -1);
    }
  };

  stage.addEventListener('pointerup', releasePointer);
  stage.addEventListener('pointercancel', releasePointer);
}
/* --- Описание напитков -------------------------------------------------- */

const DRINK_POINT_LABELS = { 'ДЕ': 'Декабристов', 'УЯ': 'Университетская', 'другое': 'Другое' };
const DRINK_ITEM_LABELS = { 'черный кофе': 'Чёрный кофе', 'кофе с молоком': 'Молочный напиток' };
const DRINK_LABELS = { acidity: 'Кислотность', sweetness: 'Сладость', bitterness: 'Горечь' };
const drinkState = {
  items: [], points: [], authors: [], point: '', author: '', state: 'all',
  offset: 0, limit: 20, hasMore: false, loaded: false,
  options: null, shift: null, baristas: [], photos: { latte_art: [], meme: [] }, tab: 'feed',
  memes: { items: [], authors: [], author: '', offset: 0, limit: 30, hasMore: false, loaded: false, locked: [], lockedCount: 0 },
};

/* Карусель карточек-описаний: индекс текущей карточки. Автосмены нет —
   листаем только вручную (стрелки, точки, свайп). */
const drinkCarousel = { index: 0 };

function drinkPointLabel(point) {
  return DRINK_POINT_LABELS[point] || point || 'Другое';
}

function formatDrinkDate(value) {
  try {
    return new Date(value).toLocaleString('ru-RU', {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
    });
  } catch (error) {
    return '';
  }
}

async function openDrinks() {
  if (!drinkState.loaded) renderDrinkFilters();
  setDrinksTab(drinkState.tab || 'feed');
  await loadDrinks(!drinkState.loaded);
  if (drinkState.tab === 'memes' && !drinkState.memes.loaded) loadMemes(true);
}

/* Переход из базы знаний (история рецептов) или из галереи мемов в карточку ленты.
   `focusQuiz` — доскроллить до викторины внутри карточки. */
async function openDrinkCard(descriptionId, focusQuiz = false) {
  const target = Number(descriptionId);
  if (!target) return;
  drinkState.point = '';
  drinkState.author = '';
  drinkState.state = 'all';
  drinkState.loaded = false;
  setView('drinks');
  setDrinksTab('feed');
  await loadDrinks(true);
  const find = () => document.querySelector(`[data-drink-id="${target}"]`);
  let card = find();
  let guard = 0;
  while (!card && drinkState.hasMore && guard < 6) {
    guard += 1;
    await loadDrinks(false);
    card = find();
  }
  if (!card) { showToast('Не удалось найти описание в ленте'); return; }
  // Карточка могла оказаться на скрытом слайде — переводим карусель на неё.
  const cardIndex = drinkState.items.findIndex((item) => item.id === target);
  if (cardIndex >= 0) { drinkCarousel.index = cardIndex; drinkCarouselUpdate(); }
  if (focusQuiz) {
    const quiz = card.querySelector('[data-quiz-id]');
    if (quiz) {
      quiz.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'center' });
      quiz.classList.add('is-highlighted');
      setTimeout(() => quiz.classList.remove('is-highlighted'), 2200);
      return;
    }
  }
  card.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'center' });
  card.classList.add('is-highlighted');
  setTimeout(() => card.classList.remove('is-highlighted'), 2200);
}

async function loadDrinks(reset = false) {
  const feed = document.querySelector('#drinks-feed');
  if (reset) {
    drinkState.offset = 0;
    drinkState.items = [];
    drinkCarousel.index = 0;
    if (feed) feed.innerHTML = '<p class="empty-state">Загрузка…</p>';
  }
  try {
    const params = new URLSearchParams({ limit: String(drinkState.limit), offset: String(drinkState.offset) });
    if (drinkState.point) params.set('point', drinkState.point);
    if (drinkState.author) params.set('author_id', drinkState.author);
    if (drinkState.state && drinkState.state !== 'all') params.set('state', drinkState.state);
    const data = await api(`/api/drink-descriptions?${params.toString()}`);
    drinkState.points = data.points || [];
    drinkState.authors = data.authors || [];
    drinkState.hasMore = Boolean(data.has_more);
    drinkState.items = reset ? (data.items || []) : drinkState.items.concat(data.items || []);
    drinkState.offset = drinkState.items.length;
    drinkState.loaded = true;
    renderDrinkFilters();
    renderDrinksFeed();
  } catch (error) {
    if (feed) feed.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
  }
}

function renderDrinkFilters() {
  const chips = document.querySelector('#drinks-point-filter');
  if (chips) {
    const options = [{ value: '', label: 'Все' }].concat(
      (drinkState.points || []).map((point) => ({ value: point, label: drinkPointLabel(point) })),
    );
    chips.innerHTML = options.map((option) =>
      `<button class="chip${drinkState.point === option.value ? ' is-selected' : ''}" type="button" data-drink-point="${escapeHtml(option.value)}">${escapeHtml(option.label)}</button>`).join('');
  }
  const select = document.querySelector('#drinks-author-filter');
  if (select) {
    const current = drinkState.author;
    select.innerHTML = '<option value="">Все</option>' + (drinkState.authors || []).map(
      (author) => `<option value="${author.id}">${escapeHtml(author.name)}</option>`).join('');
    select.value = current;
  }
  const states = document.querySelector('#drinks-state-filter');
  if (states) {
    const options = [
      { value: 'all', label: 'Все' },
      { value: 'unvoted', label: 'Не голосовал' },
      { value: 'voted', label: 'Голосовал' },
    ];
    states.innerHTML = options.map((option) =>
      `<button class="chip${drinkState.state === option.value ? ' is-selected' : ''}" type="button" data-drink-state="${option.value}">${escapeHtml(option.label)}</button>`).join('');
  }
}

function renderDrinksFeed() {
  const feed = document.querySelector('#drinks-feed');
  const more = document.querySelector('#drinks-more');
  if (!feed) return;
  if (!drinkState.items.length) {
    feed.innerHTML = '<p class="empty-state">Пока нет описаний смен. Станьте первым!</p>';
    drinkCarousel.index = 0;
    if (more) more.hidden = !drinkState.hasMore;
    return;
  }
  if (drinkCarousel.index >= drinkState.items.length) drinkCarousel.index = 0;
  const total = drinkState.items.length;
  const slides = drinkState.items
    .map((desc) => `<article class="drink-slide">${drinkCardMarkup(desc)}</article>`)
    .join('');
  // Точки показываем, пока их немного: на длинной ленте они превращаются в
  // частокол, ориентиром остаются стрелки и счётчик «N / всего».
  const dots = total > 1 && total <= 12
    ? `<div class="slideshow-dots">${drinkState.items.map((desc, index) => `<button type="button" class="slideshow-dot" data-drink-slide="${index}" aria-label="Описание ${index + 1}"></button>`).join('')}</div>`
    : '';
  const nav = total > 1
    ? `<div class="slideshow-nav"><button class="icon-button" type="button" data-drink-slide-step="-1" aria-label="Предыдущее описание"><svg class="icon" aria-hidden="true"><use href="#i-chevron-left"></use></svg></button><span class="slideshow-count" data-drink-slide-count>${drinkCarousel.index + 1} / ${total}</span><button class="icon-button" type="button" data-drink-slide-step="1" aria-label="Следующее описание"><svg class="icon" aria-hidden="true"><use href="#i-chevron-right"></use></svg></button></div>${dots}`
    : '';
  feed.innerHTML = `<div class="slideshow"><div class="slideshow-viewport"><div class="slideshow-track">${slides}</div></div>${nav}</div>`;
  drinkCarouselUpdate();
  if (more) more.hidden = !drinkState.hasMore;
}

/* Узел трека карусели описаний. */
function drinkCarouselTrack() {
  return document.querySelector('#drinks-feed .slideshow-track');
}

/* Сдвиг трека: при drag (animate=false) карточка следует за пальцем без transition. */
function drinkCarouselMove(offset, animate) {
  const track = drinkCarouselTrack();
  if (!track) return;
  track.classList.toggle('is-dragging', !animate);
  const width = track.offsetWidth || 0;
  track.style.transform = `translateX(${offset - drinkCarousel.index * width}px)`;
}

/* Высота вьюпорта = высота активной карточки: соседние панели плавно
   подъезжают (transition: height в CSS). */
function drinkCarouselSyncHeight() {
  const feed = document.querySelector('#drinks-feed');
  const viewport = feed && feed.querySelector('.slideshow-viewport');
  const track = feed && feed.querySelector('.slideshow-track');
  if (!viewport || !track) return;
  const active = track.children[drinkCarousel.index];
  const card = active && active.firstElementChild;
  if (!card) return;
  const apply = () => { viewport.style.height = card.offsetHeight + 'px'; };
  apply();
  requestAnimationFrame(apply);   // и ещё раз после догрузки картинок/перерисовки
}

/* Переключение слайдов только классами — без перерисовки DOM, иначе анимация
   cover-scale не успевала бы проиграться. */
function drinkCarouselUpdate() {
  const feed = document.querySelector('#drinks-feed');
  const track = drinkCarouselTrack();
  if (!feed || !track) return;
  const slides = [...track.children];
  const total = slides.length;
  slides.forEach((slide, index) => {
    const active = index === drinkCarousel.index;
    slide.classList.toggle('is-active', active);
    slide.classList.toggle('is-prev', index < drinkCarousel.index);
    slide.classList.toggle('is-next', index > drinkCarousel.index);
    slide.setAttribute('aria-hidden', active ? 'false' : 'true');
  });
  const count = feed.querySelector('[data-drink-slide-count]');
  if (count) count.textContent = `${drinkCarousel.index + 1} / ${total}`;
  feed.querySelectorAll('.slideshow-dot').forEach((dot, index) =>
    dot.classList.toggle('is-active', index === drinkCarousel.index));
  drinkCarouselMove(0, true);
  drinkCarouselSyncHeight();
}

/* Перейти к карточке по индексу (с зацикливанием) без перерисовки DOM. */
function drinkCarouselGoTo(index) {
  const total = drinkState.items.length || 1;
  drinkCarousel.index = ((index % total) + total) % total;
  drinkCarouselUpdate();
}

function shiftDrinkSlide(step) {
  drinkCarouselGoTo(drinkCarousel.index + step);
}

function drinkItemView(item) {
  const recipe = item.recipe || {};
  const numbers = [
    recipe.dose != null ? `вход ${recipe.dose} г` : '',
    recipe.yield != null ? `выход ${recipe.yield} г` : '',
    recipe.water != null ? `вода ${recipe.water} мл` : '',
    recipe.time != null ? `${recipe.time} с` : '',
    recipe.temp != null ? `${recipe.temp} °C` : '',
  ].filter(Boolean).join(' · ');
  const texts = item.balance_texts || {};
  const details = [];
  if (item.bean) details.push(`Зерно: ${item.bean.name}`);
  if (item.category === 'черный кофе') {
    const balance = item.balance || {};
    ['acidity', 'sweetness', 'bitterness'].forEach((key) => {
      if (balance[key]) details.push(`${DRINK_LABELS[key]}: ${balance[key]}${texts[key] ? ` (${texts[key]})` : ''}`);
    });
    if (item.body && item.body.intensity) details.push(`Тело: ${item.body.intensity}${item.body.texture ? ` (${item.body.texture})` : ''}`);
  } else {
    if (item.balance_score) details.push(`Баланс: ${item.balance_score}`);
    if (item.milk_drop) details.push(`Молочная капля: ${item.milk_drop}`);
  }
  if (item.bouquet) details.push(`Букет: ${item.bouquet}`);
  if (item.category === 'черный кофе' && item.aftertaste && item.aftertaste.duration) {
    details.push(`Послевкусие: ${item.aftertaste.duration}${item.aftertaste.quality ? ` (${item.aftertaste.quality})` : ''}`);
  }
  const title = item.drink ? item.drink.title : '';
  return `<div class="drink-item">
    <div class="drink-item-head"><span class="drink-item-cat">${escapeHtml(DRINK_ITEM_LABELS[item.category] || item.category)}</span><span class="drink-item-bean">${escapeHtml(title)}</span></div>
    ${numbers ? `<p class="drink-item-recipe">${escapeHtml(numbers)}</p>` : ''}
    ${details.length ? `<p class="drink-item-details">${escapeHtml(details.join(' · '))}</p>` : ''}
  </div>`;
}

function drinkPhotosBlock(desc, kind, quiz) {
  const photos = ((desc.photos || {})[kind]) || [];
  if (!photos.length) return '';
  // Автор на фото раскрываем только после ответа, иначе викторина теряет смысл.
  // Если викторины нет (одно фото или все от одного бариста) — показываем сразу.
  const revealed = !quiz || Boolean(quiz.correct_option_id);
  const figures = photos.map((photo, index) => {
    const caption = revealed ? drinkPhotoAuthor(desc, photo.author_id) : '';
    return `
    <figure class="drink-photo" data-media-url="${escapeHtml(photo.url)}" data-media-caption="${escapeHtml(caption || `Фото ${index + 1}`)}">
      <span class="drink-photo-index">${index + 1}</span>
      <img src="${escapeHtml(photo.url)}" alt="Фото" loading="lazy">
      ${caption ? `<figcaption class="drink-photo-author">${escapeHtml(caption)}</figcaption>` : ''}
    </figure>`;
  }).join('');
  return `<div class="drink-block" data-kind="${kind}">
    <div class="drink-photos" data-media-gallery>${figures}</div>
    ${quiz ? drinkQuizMarkup(quiz) : ''}
  </div>`;
}

/* Имя автора фото: ищем среди участников смены (автор поста и напарник). */
function drinkPhotoAuthor(desc, authorId) {
  if (!authorId) return '';
  const people = [desc.author, desc.partner].filter(Boolean);
  const found = people.find((person) => Number(person.id) === Number(authorId));
  return found ? found.name : '';
}

/* Статус голосования по викторинам карточки. */
function drinkVoteBadge(desc) {
  const quizzes = desc.quizzes || [];
  if (!quizzes.length) return '';
  const pending = quizzes.filter((quiz) => !quiz.voted).length;
  if (!pending) return '<span class="drink-vote-badge is-done">✓ Вы проголосовали</span>';
  return `<button class="drink-vote-badge is-pending" type="button" data-drink-vote-jump="${desc.id}">Голосовать: ${pending}</button>`;
}

function drinkCardMarkup(desc) {
  const items = (desc.items || []).map(drinkItemView).join('');
  const quizByKind = {};
  (desc.quizzes || []).forEach((quiz) => { quizByKind[quiz.kind] = quiz; });
  const partner = desc.partner ? ` · напарник: ${escapeHtml(desc.partner.name)}` : '';
  return `<article class="drink-card" data-drink-id="${desc.id}">
    <header class="drink-card-head">
      <div><strong>${escapeHtml(desc.author.name)}</strong><span class="drink-meta">${escapeHtml(drinkPointLabel(desc.point))}${partner}</span></div>
      <div class="drink-card-status">${drinkVoteBadge(desc)}<time class="drink-date">${escapeHtml(formatDrinkDate(desc.created_at))}</time></div>
    </header>
    <div class="drink-card-items">${items}</div>
    ${desc.note ? `<p class="drink-card-note">${escapeHtml(desc.note)}</p>` : ''}
    ${drinkPhotosBlock(desc, 'latte_art', quizByKind.latte_art)}
    ${drinkPhotosBlock(desc, 'meme', quizByKind.meme)}
  </article>`;
}

function drinkQuizMarkup(quiz) {
  const options = (quiz.options || []).map((option) => {
    const classes = ['drink-quiz-option'];
    if (option.is_correct) classes.push('is-correct');
    if (quiz.my_option_id === option.id) classes.push('is-mine');
    const voters = option.voters || [];
    const votersMarkup = voters.length
      ? `<span class="drink-quiz-voters">${escapeHtml(voters.join(', '))}</span>` : '';
    return `<button class="${classes.join(' ')}" type="button" data-drink-vote="${quiz.id}" data-option="${option.id}"${quiz.voted ? ' disabled' : ''}>
      <span class="drink-quiz-option-text">${escapeHtml(option.text)}</span>
      <span class="drink-quiz-meta"><span class="drink-quiz-votes">${option.votes}</span>${votersMarkup}</span>
    </button>`;
  }).join('');
  const done = quiz.voted ? '<span class="drink-quiz-done">вы ответили</span>' : '';
  const total = quiz.votes ? `<span class="drink-quiz-total">голосов: ${quiz.votes}</span>` : '';
  return `<div class="drink-quiz" data-quiz-id="${quiz.id}">
    <p class="drink-quiz-question">${escapeHtml(quiz.question || 'Где чей латте-арт?')}${done}${total}</p>
    <div class="drink-quiz-options">${options}</div>
  </div>`;
}

async function voteDrinkQuiz(descriptionId, quizId, optionId) {
  try {
    const data = await api(`/api/drink-descriptions/${descriptionId}/quizzes/${quizId}/vote`, {
      method: 'POST',
      body: JSON.stringify({ option_id: optionId }),
    });
    const desc = drinkState.items.find((item) => item.id === descriptionId);
    if (desc) {
      const merged = new Map((data.quizzes || []).map((quiz) => [quiz.id, quiz]));
      desc.quizzes = (desc.quizzes || []).map((quiz) => merged.get(quiz.id) || quiz);
      renderDrinksFeed();
    }
    // Голос открывает мемы этого поста в «трофейной комнате» галереи.
    const hadLocked = drinkState.memes.loaded && Number(drinkState.memes.lockedCount) > 0;
    drinkState.memes.loaded = false;
    if (drinkState.tab === 'memes') loadMemes(true);
    const note = data.correct
      ? (data.xp ? `Верно! +${data.xp} опыта в «Команду»` : 'Верно!')
      : 'Не угадали — но голос учтён';
    showToast(hadLocked ? `${note} · Галерея мемов пополнилась` : note);
  } catch (error) {
    showToast(error.message);
  }
}

function drinkWidgetSummary(desc) {
  const parts = (desc.items || []).map((item) =>
    (item.bean && item.bean.name) || (item.category === 'кофе с молоком' ? 'молочный' : 'чёрный'));
  return parts.join(' · ') || 'Описание смены';
}

function drinkWidgetVote(desc) {
  const quizzes = desc.quizzes || [];
  if (!quizzes.length) return '';
  const pending = quizzes.filter((quiz) => !quiz.voted).length;
  if (!pending) return '<span class="drinks-widget-vote is-done">✓ Проголосовали</span>';
  return `<span class="drinks-widget-vote">Голосовать: ${pending}</span>`;
}

async function loadDrinksWidget() {
  const list = document.querySelector('#drinks-widget-list');
  if (!list) return;
  try {
    const data = await api('/api/drink-descriptions?limit=3');
    const items = data.items || [];
    if (!items.length) {
      list.innerHTML = '<p class="empty-state">Пока нет описаний смен.</p>';
      return;
    }
    list.innerHTML = items.map((desc) => `
      <button class="drinks-widget-card" type="button" data-view="drinks">
        <span class="drinks-widget-point">${escapeHtml(drinkPointLabel(desc.point))}</span>
        <strong>${escapeHtml(desc.author.name)}</strong>
        <span class="drinks-widget-note">${escapeHtml(drinkWidgetSummary(desc))}</span>
        ${drinkWidgetVote(desc)}
      </button>`).join('');
    list.querySelectorAll('[data-view]').forEach((button) =>
      button.addEventListener('click', () => setView(button.dataset.view)));
  } catch (error) {
    list.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
  }
}

const DRINK_FALLBACK_SCALES = {
  balance: ['низкая', 'ниже среднего', 'средняя', 'выше среднего', 'высокая'],
  body: ['легкое', 'среднее', 'плотное'],
  aftertaste: ['короткое', 'среднее', 'долгое'],
  milk_drop: ['отсутствует', 'небольшая', 'большая'],
  balance_score: ['в молоко', 'нейтральный', 'в кофе'],
};

function drinkScaleBlock(label, attr, group, values) {
  const list = values && values.length ? values : DRINK_FALLBACK_SCALES[group] || ['—'];
  const index = Math.floor((list.length - 1) / 2);
  return `<label class="drink-scale"><span>${escapeHtml(label)}</span>
    <input type="range" min="0" max="${Math.max(0, list.length - 1)}" step="1" value="${index}" data-drink-scale="${escapeHtml(attr)}" data-drink-group="${escapeHtml(group)}">
    <output>${escapeHtml(list[index] || '')}</output>
  </label>`;
}

function drinkDrinkOptions(category) {
  const drinks = (drinkState.options && drinkState.options.drinks) || [];
  return '<option value="">— напиток не указан —</option>' + drinks
    .filter((drink) => drink.category === category)
    .map((drink) => `<option value="${drink.id}">${escapeHtml(drink.title)}</option>`).join('');
}

function drinkScaleWithText(label, attr, group, textAttr, values, placeholder) {
  const list = values && values.length ? values : DRINK_FALLBACK_SCALES[group] || ['—'];
  const index = Math.floor((list.length - 1) / 2);
  return `<div class="drink-scale-block">
    <label class="drink-scale"><span>${escapeHtml(label)}</span>
      <input type="range" min="0" max="${Math.max(0, list.length - 1)}" step="1" value="${index}" data-drink-scale="${escapeHtml(attr)}" data-drink-group="${escapeHtml(group)}">
      <output>${escapeHtml(list[index] || '')}</output>
    </label>
    <input class="drink-scale-text" type="text" maxlength="400" data-drink-field="${escapeHtml(textAttr)}" placeholder="${escapeHtml(placeholder || 'Характер — как он ощущается')}">
  </div>`;
}

function drinkItemFormMarkup(category) {
  const milk = category === 'кофе с молоком';
  const scales = drinkState.scales || DRINK_FALLBACK_SCALES;
  const beans = (drinkState.options && drinkState.options.beans) || [];
  const beanOptions = '<option value="">— зерно не указано —</option>' + beans.map(
    (bean) => `<option value="${bean.id}">${escapeHtml(bean.name)}</option>`).join('');
  const parts = [];
  parts.push(`<div class="drink-refs">
    <label class="drink-field-group"><span class="drink-field-label">Напиток <span class="optional">из базы знаний</span></span><select data-drink-field="drink_id">${drinkDrinkOptions(category)}</select></label>
    <label class="drink-field-group"><span class="drink-field-label">Зерно</span><select data-drink-field="bean_id">${beanOptions}</select></label>
  </div>`);
  parts.push(`<div class="drink-recipe">
    <label>Вход, г<input type="number" min="0" step="0.1" inputmode="decimal" data-drink-field="recipe_dose"></label>
    <label>Выход, г<input type="number" min="0" step="0.1" inputmode="decimal" data-drink-field="recipe_yield"></label>
    ${milk ? '' : '<label>Вода, мл<input type="number" min="0" step="1" inputmode="decimal" data-drink-field="recipe_water"></label>'}
    <label>Время, с<input type="number" min="0" step="0.1" inputmode="decimal" data-drink-field="recipe_time"></label>
    <label>Темп., °C<input type="number" min="0" step="0.1" inputmode="decimal" data-drink-field="recipe_temp"></label>
  </div>`);
  if (milk) {
    parts.push('<label class="checkbox-row drink-espresso"><input type="checkbox" data-drink-field="is_espresso_recipe"><span>Рецепт как на эспрессо</span></label>');
    parts.push(drinkScaleBlock('Баланс в молоке', 'balance_score', 'balance_score', scales.balance_score));
    parts.push(drinkScaleBlock('Молочная капля', 'milk_drop', 'milk_drop', scales.milk_drop));
    parts.push('<label class="drink-text">Букет<input type="text" maxlength="400" data-drink-field="bouquet" placeholder="Карамель, орех…"></label>');
  } else {
    parts.push('<div class="drink-scales">'
      + drinkScaleWithText('Кислотность', 'balance_acidity', 'balance', 'balance_acidity_text', scales.balance, 'Характер кислотности')
      + drinkScaleWithText('Сладость', 'balance_sweetness', 'balance', 'balance_sweetness_text', scales.balance, 'Характер сладости')
      + drinkScaleWithText('Горечь', 'balance_bitterness', 'balance', 'balance_bitterness_text', scales.balance, 'Характер горечи') + '</div>');
    parts.push(drinkScaleBlock('Тело', 'body_intensity', 'body', scales.body));
    parts.push('<label class="drink-text">Текстура<input type="text" maxlength="400" data-drink-field="body_texture" placeholder="Бархатистое…"></label>');
    parts.push('<label class="drink-text">Букет<input type="text" maxlength="400" data-drink-field="bouquet" placeholder="Шоколад, цитрус…"></label>');
    parts.push(drinkScaleBlock('Послевкусие', 'aftertaste_duration', 'aftertaste', scales.aftertaste));
    parts.push('<label class="drink-text">Качество послевкусия<input type="text" maxlength="400" data-drink-field="aftertaste_quality"></label>');
  }
  return `<section class="drink-item-form" data-category="${escapeHtml(category)}">
    <div class="drink-item-form-head"><span class="drink-item-cat">${escapeHtml(DRINK_ITEM_LABELS[category] || category)}</span><button class="button button-quiet drink-item-remove" type="button">Убрать</button></div>
    ${parts.join('')}
  </section>`;
}

function addDrinkItemRow(category) {
  const container = document.querySelector('#drink-items');
  if (container) container.insertAdjacentHTML('beforeend', drinkItemFormMarkup(category));
}

function drinkSyncEspresso() {
  const rows = Array.from(document.querySelectorAll('#drink-items .drink-item-form'));
  const black = rows.find((row) => row.dataset.category === 'черный кофе');
  const source = {};
  if (black) {
    // Зерно копируем вместе с рецептом: молочный напиток на том же эспрессо.
    ['bean_id', 'recipe_dose', 'recipe_yield', 'recipe_time', 'recipe_temp'].forEach((field) => {
      const input = black.querySelector(`[data-drink-field="${field}"]`);
      source[field] = input ? input.value : '';
    });
  }
  rows.forEach((row) => {
    if (row.dataset.category !== 'кофе с молоком') return;
    const toggle = row.querySelector('[data-drink-field="is_espresso_recipe"]');
    const on = Boolean(toggle && toggle.checked);
    ['recipe_dose', 'recipe_yield', 'recipe_time', 'recipe_temp'].forEach((field) => {
      const input = row.querySelector(`[data-drink-field="${field}"]`);
      if (!input) return;
      if (on) input.value = source[field] ?? '';
      input.disabled = on;
    });
    // Зерно копируем только когда у эспрессо оно выбрано — иначе не затираем своё.
    const bean = row.querySelector('[data-drink-field="bean_id"]');
    if (bean) {
      if (on && source.bean_id) bean.value = source.bean_id;
      bean.disabled = on;
    }
  });
}

function fillDrinkShift(shift, options) {
  const pointSelect = document.querySelector('#drink-point');
  if (pointSelect) {
    pointSelect.innerHTML = (options.points || []).map((point) =>
      `<option value="${escapeHtml(point)}">${escapeHtml(drinkPointLabel(point))}</option>`).join('');
    pointSelect.value = shift.point || 'другое';
  }
  const partnerSelect = document.querySelector('#drink-partner');
  if (partnerSelect) {
    partnerSelect.innerHTML = '<option value="">— без напарника —</option>' + (shift.candidates || []).map((person) =>
      `<option value="${person.id}">${escapeHtml(person.name)}${person.on_shift ? ' (на смене)' : ''}</option>`).join('');
    const first = (shift.partners || [])[0];
    partnerSelect.value = first ? String(first.id) : '';
  }
  const status = document.querySelector('#drink-shift');
  if (status) {
    status.textContent = shift.on_shift
      ? `Смена: ${drinkPointLabel(shift.point)} · напарников на смене: ${(shift.partners || []).length}`
      : 'Смена не найдена — выберите напарника вручную';
  }
  const toggle = document.querySelector('#drink-manual-toggle');
  if (toggle) {
    toggle.hidden = false;
    toggle.textContent = shift.on_shift ? 'Изменить вручную' : 'Выбрать вручную';
  }
  // Фоллбэк: если смену не определили, сразу показываем выбор точки и напарника.
  const row = document.querySelector('#drink-partner-row');
  if (row && !shift.on_shift) row.hidden = false;
}

async function openDrinkDialog() {
  const dialog = document.querySelector('#drink-dialog');
  if (!dialog) return;
  const form = document.querySelector('#drink-form');
  if (form) form.reset();
  const container = document.querySelector('#drink-items');
  if (container) container.innerHTML = '';
  const errorNode = document.querySelector('#drink-form-error');
  if (errorNode) errorNode.hidden = true;
  if (form) clearDraftNotice(form);
  const draft = readDraft('drink');
  drinkState.photos = { latte_art: [], meme: [] };
  drinkState.baristas = [];
  resetDrinkPhoto('latte_art');
  resetDrinkPhoto('meme');
  renderDrinkUploads();
  const manualRow = document.querySelector('#drink-partner-row');
  if (manualRow) manualRow.hidden = true;
  const toggle = document.querySelector('#drink-manual-toggle');
  if (toggle) { toggle.hidden = false; toggle.textContent = 'Выбрать вручную'; }
  const status = document.querySelector('#drink-shift');
  if (status) status.textContent = 'Определяем смену…';
  dialog.showModal();
  try {
    const [options, shift] = await Promise.all([
      api('/api/drink-descriptions/options'),
      api('/api/drink-descriptions/shift'),
    ]);
    drinkState.options = options;
    drinkState.shift = shift;
    drinkState.scales = Object.assign({}, DRINK_FALLBACK_SCALES, options.scales || {});
    fillDrinkShift(shift, options);
    refreshDrinkBaristas();
    if (draft) {
      restoreDrinkDraft(draft);
      if (form) renderDraftNotice(form, 'drink', resetDrinkDraftForm);
    }
  } catch (error) {
    if (errorNode) { errorNode.textContent = error.message; errorNode.hidden = false; }
    // Фоллбэк: даже без ответа сервера даём заполнить точку и напарника вручную.
    if (manualRow) manualRow.hidden = false;
    if (status) status.textContent = 'Не удалось определить смену — выберите вручную';
    refreshDrinkBaristas();
    if (draft) {
      restoreDrinkDraft(draft);
      if (form) renderDraftNotice(form, 'drink', resetDrinkDraftForm);
    }
  }
  if (!draft) addDrinkItemRow('черный кофе');
}

function showDrinkError(message) {
  const node = document.querySelector('#drink-form-error');
  if (node) {
    node.textContent = message;
    node.hidden = false;
  }
}

function drinkReadScale(input) {
  const list = (drinkState.scales || DRINK_FALLBACK_SCALES)[input.dataset.drinkGroup] || [];
  return list[Number(input.value)] || null;
}

function collectDrinkPayload() {
  const rows = Array.from(document.querySelectorAll('#drink-items .drink-item-form'));
  if (!rows.length) {
    showDrinkError('Добавьте хотя бы один напиток');
    return null;
  }
  const items = rows.map((row) => {
    const read = (name) => {
      const node = row.querySelector(`[data-drink-field="${name}"]`);
      return node ? node.value.trim() : '';
    };
    const readNumber = (name) => {
      const raw = read(name);
      if (raw === '') return null;
      const value = Number(raw);
      return Number.isFinite(value) ? value : null;
    };
    const item = { category: row.dataset.category };
    if (read('drink_id')) item.drink_id = Number(read('drink_id'));
    if (read('bean_id')) item.bean_id = Number(read('bean_id'));
    ['recipe_dose', 'recipe_yield', 'recipe_water', 'recipe_time', 'recipe_temp'].forEach((key) => {
      const value = readNumber(key);
      if (value != null) item[key] = value;
    });
    row.querySelectorAll('[data-drink-scale]').forEach((input) => {
      const value = drinkReadScale(input);
      if (value) item[input.dataset.drinkScale] = value;
    });
    if (item.category === 'кофе с молоком') {
      const toggle = row.querySelector('[data-drink-field="is_espresso_recipe"]');
      item.is_espresso_recipe = Boolean(toggle && toggle.checked);
    }
    ['body_texture', 'aftertaste_quality', 'bouquet',
      'balance_acidity_text', 'balance_sweetness_text', 'balance_bitterness_text'].forEach((key) => {
      const value = read(key);
      if (value) item[key] = value;
    });
    return item;
  });
  const point = document.querySelector('#drink-point')?.value || 'другое';
  const partner = document.querySelector('#drink-partner')?.value || '';
  const note = document.querySelector('#drink-note')?.value.trim() || '';
  const payload = { point, items };
  if (partner) payload.partner_id = Number(partner);
  if (note) payload.note = note;
  const photoPayload = (kind) => (drinkState.photos[kind] || []).map((entry) => ({
    url: entry.url, author_id: Number(entry.author_id),
  }));
  if ((drinkState.photos.latte_art || []).length) payload.latte_art_photos = photoPayload('latte_art');
  if ((drinkState.photos.meme || []).length) payload.meme_photos = photoPayload('meme');
  return payload;
}

async function uploadDrinkPhoto(kind, authorId, files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  const authorName = drinkBaristaName(authorId);
  for (const file of list) {
    if ((drinkState.photos[kind] || []).length >= 6) {
      showDrinkError('Не больше 6 фото на каждую подборку');
      break;
    }
    try {
      const response = await fetch('/api/drink-descriptions/photos', {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': file.type || 'application/octet-stream' },
        body: file,
        credentials: 'same-origin',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить фото');
      drinkState.photos[kind] = (drinkState.photos[kind] || []).concat({
        url: data.url, author_id: Number(authorId), author_name: authorName,
      });
      renderDrinkPreviews(kind);
    } catch (error) {
      showDrinkError(error.message);
    }
  }
  drinkState.flushDraft?.();
}

async function submitDrinkForm(event) {
  event.preventDefault();
  const payload = collectDrinkPayload();
  if (!payload) return;
  const submit = document.querySelector('#drink-submit');
  if (submit) submit.disabled = true;
  try {
    const data = await api('/api/drink-descriptions', { method: 'POST', body: JSON.stringify(payload) });
    clearDraft('drink');
    document.querySelector('#drink-dialog')?.close();
    const xp = data.xp ? Object.values(data.xp).reduce((sum, value) => sum + value, 0) : 0;
    const partnerXp = data.partner_xp
      ? Object.values(data.partner_xp).reduce((sum, value) => sum + value, 0) : 0;
    const toastParts = ['Описание опубликовано'];
    if (xp) toastParts.push(`+${xp} опыта`);
    if (partnerXp) toastParts.push(`напарнику +${partnerXp}`);
    showToast(toastParts.join(' · '));
    await loadDrinks(true);
    loadDrinksWidget();
  } catch (error) {
    showDrinkError(error.message);
  } finally {
    if (submit) submit.disabled = false;
  }
}

function applyDrinkTemplate(text) {
  const rows = Array.from(document.querySelectorAll('#drink-items .drink-item-form'));
  const black = rows.find((row) => row.dataset.category === 'черный кофе');
  const milk = rows.find((row) => row.dataset.category === 'кофе с молоком');
  if (!black) return;
  const pairs = {};
  text.split(/\r?\n/).forEach((line) => {
    const match = line.match(/^\s*([^:：]+)\s*[:：]\s*(.+?)\s*$/);
    if (match) pairs[match[1].trim().toLowerCase()] = match[2].trim();
  });
  const pick = (...keys) => {
    for (const key of keys) { if (pairs[key] != null) return pairs[key]; }
    return null;
  };
  const setInput = (row, name, value) => {
    if (value == null) return;
    const node = row.querySelector(`[data-drink-field="${name}"]`);
    if (node) node.value = value;
  };
  const setScale = (row, attr, group, value) => {
    if (value == null) return;
    const input = row.querySelector(`[data-drink-scale="${attr}"]`);
    if (!input) return;
    const list = (drinkState.scales || DRINK_FALLBACK_SCALES)[group] || [];
    const index = list.findIndex((item) => item.toLowerCase() === String(value).toLowerCase());
    if (index < 0) return;
    input.value = index;
    const out = input.parentElement.querySelector('output');
    if (out) out.textContent = list[index];
  };
  const beanName = pick('зерно', 'bean');
  if (beanName) {
    const select = black.querySelector('[data-drink-field="bean_id"]');
    const option = select ? Array.from(select.options).find(
      (opt) => opt.textContent.trim().toLowerCase() === beanName.toLowerCase()) : null;
    if (option) select.value = option.value;
  }
  setInput(black, 'recipe_dose', pick('вход', 'доза', 'кофе на входе', 'dose'));
  setInput(black, 'recipe_yield', pick('выход', 'кофе на выходе', 'yield'));
  setInput(black, 'recipe_water', pick('вода', 'water'));
  setInput(black, 'recipe_time', pick('время', 'time'));
  setInput(black, 'recipe_temp', pick('температура', 'темп', 'temp'));
  setScale(black, 'balance_acidity', 'balance', pick('кислотность', 'acidity'));
  setScale(black, 'balance_sweetness', 'balance', pick('сладость', 'sweetness'));
  setScale(black, 'balance_bitterness', 'balance', pick('горечь', 'bitterness'));
  setScale(black, 'body_intensity', 'body', pick('тело', 'body'));
  setInput(black, 'body_texture', pick('текстура', 'текстура'));
  setScale(black, 'aftertaste_duration', 'aftertaste', pick('послевкусие', 'aftertaste'));
  setInput(black, 'aftertaste_quality', pick('качество послевкусия'));
  setInput(black, 'bouquet', pick('букет', 'bouquet'));
  if (milk) {
    setScale(milk, 'balance_score', 'balance_score', pick('баланс', 'balance'));
    setScale(milk, 'milk_drop', 'milk_drop', pick('молочная капля', 'капля'));
    setInput(milk, 'bouquet', pick('букет молока'));
  }
  showToast('Поля заполнены из текста — проверьте значения');
}

async function pasteDrinkTemplate() {
  let text = '';
  try {
    if (navigator.clipboard && navigator.clipboard.readText) text = await navigator.clipboard.readText();
  } catch (error) {
    text = '';
  }
  if (!text.trim()) text = window.prompt('Вставьте текст описания (ключ: значение)') || '';
  if (!text.trim()) return;
  applyDrinkTemplate(text);
}

function drinkBaristaName(authorId) {
  const person = (drinkState.baristas || []).find((item) => item.id === Number(authorId));
  return person ? person.name : 'Бариста';
}

function drinkBaristas() {
  const me = { id: Number(document.body.dataset.userId) || 0, name: document.body.dataset.userName || 'Вы' };
  const list = [me];
  const select = document.querySelector('#drink-partner');
  const partnerId = select ? Number(select.value) : 0;
  if (partnerId && partnerId !== me.id) {
    const option = select.options[select.selectedIndex];
    list.push({ id: partnerId, name: option ? option.textContent.replace(' (на смене)', '').trim() : 'Напарник' });
  }
  return list;
}

function renderDrinkUploads() {
  document.querySelectorAll('[data-upload-kind]').forEach((block) => {
    const container = block.querySelector('[data-upload-baristas]');
    if (!container) return;
    const kind = block.dataset.uploadKind;
    container.innerHTML = (drinkState.baristas || []).map((person) =>
      `<label class="evaluation-photo-pick drink-upload-pick"><input type="file" accept="image/*" multiple data-drink-photo-input="${escapeHtml(kind)}" data-author-id="${person.id}"><span>${escapeHtml(person.name)}</span></label>`).join('');
  });
}

function renderDrinkPreviews(kind) {
  const container = document.querySelector(`[data-previews="${kind}"]`);
  if (!container) return;
  const entries = drinkState.photos[kind] || [];
  container.dataset.mediaGallery = '';
  container.innerHTML = entries.map((entry, index) => {
    const author = entry.author_name || drinkBaristaName(entry.author_id);
    return `
    <div class="drink-photo-preview">
      <span class="drink-photo-index">${index + 1}</span>
      <img src="${escapeHtml(entry.url)}" alt="Фото" data-media-url="${escapeHtml(entry.url)}" data-media-caption="${escapeHtml(author)}">
      <span class="drink-photo-author">${escapeHtml(author)}</span>
      <button class="button button-quiet" type="button" data-drink-photo-remove="${escapeHtml(kind)}" data-index="${index}">Убрать</button>
    </div>`;
  }).join('');
}

function resetDrinkPhoto(kind) {
  drinkState.photos[kind] = [];
  renderDrinkPreviews(kind);
}

function refreshDrinkBaristas() {
  drinkState.baristas = drinkBaristas();
  const ids = new Set(drinkState.baristas.map((person) => person.id));
  ['latte_art', 'meme'].forEach((kind) => {
    drinkState.photos[kind] = (drinkState.photos[kind] || []).filter((entry) => ids.has(Number(entry.author_id)));
    renderDrinkPreviews(kind);
  });
  renderDrinkUploads();
}

async function loadMemes(reset = false) {
  const grid = document.querySelector('#drinks-memes');
  if (reset) {
    drinkState.memes.offset = 0;
    drinkState.memes.items = [];
    if (grid) grid.innerHTML = '<p class="empty-state">Загрузка…</p>';
  }
  try {
    const params = new URLSearchParams({ limit: String(drinkState.memes.limit), offset: String(drinkState.memes.offset) });
    if (drinkState.memes.author) params.set('author_id', drinkState.memes.author);
    const data = await api(`/api/drink-descriptions/memes?${params.toString()}`);
    drinkState.memes.authors = data.authors || [];
    drinkState.memes.hasMore = Boolean(data.has_more);
    // Трофейная комната: открытые мемы + счётчик и заглушки закрытых.
    drinkState.memes.locked = data.locked || [];
    drinkState.memes.lockedCount = Number(data.locked_count) || 0;
    drinkState.memes.items = reset ? (data.items || []) : drinkState.memes.items.concat(data.items || []);
    drinkState.memes.offset = drinkState.memes.items.length;
    drinkState.memes.loaded = true;
    renderMemes();
  } catch (error) {
    if (grid) grid.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
  }
}

function memeCardMarkup(meme) {
  return `
    <figure class="meme-card" data-media-url="${escapeHtml(meme.url)}" data-media-caption="${escapeHtml(`${meme.author.name} · ${drinkPointLabel(meme.point)}`)}">
      <img src="${escapeHtml(meme.url)}" alt="Мем смены" loading="lazy">
      <figcaption><strong>${escapeHtml(meme.author.name)}</strong><span>${escapeHtml(drinkPointLabel(meme.point))} · ${escapeHtml(formatDrinkDate(meme.created_at))}</span></figcaption>
    </figure>`;
}

/* Закрытый мем: ни картинки, ни автора — только приглашение к викторине. */
function lockedMemeMarkup(entry) {
  const many = entry.count > 1 ? ` · ${entry.count} шт.` : '';
  return `
    <figure class="meme-card is-locked">
      <div class="meme-lock" aria-hidden="true">🔒</div>
      <figcaption>
        <strong>Мемы скрыты</strong>
        <span>${escapeHtml(drinkPointLabel(entry.point))} · ${escapeHtml(formatDrinkDate(entry.created_at))}${many}</span>
        <button class="button button-quiet meme-locked-cta" type="button" data-meme-unlock="${entry.description_id}">Проголосовать и открыть</button>
      </figcaption>
    </figure>`;
}

function renderMemes() {
  const grid = document.querySelector('#drinks-memes');
  const more = document.querySelector('#drinks-memes-more');
  const count = document.querySelector('#drinks-memes-count');
  const select = document.querySelector('#drinks-memes-author');
  const authors = drinkState.memes.authors || [];
  if (select) {
    const current = drinkState.memes.author;
    select.innerHTML = '<option value="">Все</option>' + authors.map(
      (author) => `<option value="${author.id}">${escapeHtml(author.name)}</option>`).join('');
    select.value = current;
    // Фильтровать нечего, пока ничего не открыто — селект прячем целиком.
    const field = select.closest('.drinks-author');
    if (field) field.hidden = !authors.length;
  }
  const opened = drinkState.memes.items.length;
  const lockedCount = drinkState.memes.lockedCount || 0;
  if (count) {
    const parts = [];
    if (opened) parts.push(`ОТКРЫТО: ${opened}`);
    if (lockedCount) parts.push(`🔒 СКРЫТО: ${lockedCount}`);
    count.textContent = parts.join(' · ');
  }
  if (grid) {
    const tiles = drinkState.memes.items.map(memeCardMarkup).join('');
    // Заглушки показываем только без фильтра по автору — иначе смысл теряется.
    const placeholders = drinkState.memes.author ? '' : (drinkState.memes.locked || []).map(lockedMemeMarkup).join('');
    if (!tiles && !placeholders) {
      grid.innerHTML = '<p class="empty-state">Пока нет мемов смен.</p>';
    } else if (!tiles) {
      grid.innerHTML = `${placeholders}<p class="empty-state">Галерея откроется, как только проголосуете в викторинах смен — ответ неважен, важен голос.</p>`;
    } else {
      grid.innerHTML = tiles + placeholders;
    }
  }
  if (more) more.hidden = !drinkState.memes.hasMore;
}

function setDrinksTab(tab) {
  drinkState.tab = tab === 'memes' ? 'memes' : 'feed';
  document.querySelectorAll('[data-drinks-tab]').forEach((button) =>
    button.classList.toggle('is-selected', button.dataset.drinksTab === drinkState.tab));
  const feedPanel = document.querySelector('#drinks-feed-panel');
  const memesPanel = document.querySelector('#drinks-memes-panel');
  if (feedPanel) feedPanel.hidden = drinkState.tab !== 'feed';
  if (memesPanel) memesPanel.hidden = drinkState.tab !== 'memes';
  if (drinkState.tab === 'memes' && !drinkState.memes.loaded) loadMemes(true);
}

function setupDrinks() {
  document.querySelector('#drinks-create')?.addEventListener('click', openDrinkDialog);
  document.querySelectorAll('[data-drinks-tab]').forEach((button) =>
    button.addEventListener('click', () => setDrinksTab(button.dataset.drinksTab)));
  document.querySelector('#drinks-memes-more')?.addEventListener('click', () => loadMemes(false));
  document.querySelector('#drinks-memes')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-meme-unlock]');
    if (!button) return;
    openDrinkCard(Number(button.dataset.memeUnlock), true);
  });
  document.querySelector('#drinks-memes-author')?.addEventListener('change', (event) => {
    drinkState.memes.author = event.target.value;
    loadMemes(true);
  });
  document.querySelector('#drinks-more')?.addEventListener('click', () => loadDrinks(false));
  document.querySelector('#drinks-point-filter')?.addEventListener('click', (event) => {
    const chip = event.target.closest('[data-drink-point]');
    if (!chip) return;
    drinkState.point = chip.dataset.drinkPoint;
    renderDrinkFilters();
    loadDrinks(true);
  });
  document.querySelector('#drinks-author-filter')?.addEventListener('change', (event) => {
    drinkState.author = event.target.value;
    loadDrinks(true);
  });
  document.querySelector('#drinks-state-filter')?.addEventListener('click', (event) => {
    const chip = event.target.closest('[data-drink-state]');
    if (!chip) return;
    drinkState.state = chip.dataset.drinkState;
    renderDrinkFilters();
    loadDrinks(true);
  });
  const drinksFeed = document.querySelector('#drinks-feed');
  drinksFeed?.addEventListener('click', (event) => {
    const step = event.target.closest('[data-drink-slide-step]');
    if (step) { shiftDrinkSlide(Number(step.dataset.drinkSlideStep) || 1); return; }

    const dot = event.target.closest('[data-drink-slide]');
    if (dot) { drinkCarouselGoTo(Number(dot.dataset.drinkSlide) || 0); return; }

    const jump = event.target.closest('[data-drink-vote-jump]');
    if (jump) {
      jump.closest('[data-drink-id]')?.querySelector('[data-quiz-id]')
        ?.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'center' });
      return;
    }
    const button = event.target.closest('[data-drink-vote]');
    const card = button ? button.closest('[data-drink-id]') : null;
    if (!button || !card || button.disabled) return;
    voteDrinkQuiz(Number(card.dataset.drinkId), Number(button.dataset.drinkVote), Number(button.dataset.option));
  });

  // Свайп по карусели описаний: карточка следует за пальцем, а на отпускании
  // дощёлкивает к соседней. Автосмены нет — только ручное листание.
  if (drinksFeed) {
    const DRAG_SLOP = 8;        // порог, после которого начинаем тянуть карточку
    const SNAP_FRACTION = 0.25; // доля ширины, достаточная для перелистывания
    const FLICK = 0.5;          // px/мс — быстрый флик листает без полного сдвига
    let drag = null;
    let suppressClick = false;  // свайп не должен превращаться в клик по карточке

    const slideWidth = () => (drinkCarouselTrack()?.offsetWidth || drinksFeed.clientWidth || 1);

    drinksFeed.addEventListener('click', (event) => {
      if (!suppressClick) return;
      event.preventDefault();
      event.stopPropagation();
      suppressClick = false;
    }, { capture: true });

    drinksFeed.addEventListener('pointerdown', (event) => {
      if (event.button) { drag = null; return; }          // только основная кнопка/касание
      if (!event.target.closest('.slideshow-viewport')) { drag = null; return; }
      if (event.target.closest('a, button')) { drag = null; return; }
      drag = { x: event.clientX, y: event.clientY, axis: null, lastX: event.clientX, lastT: performance.now() };
      drinksFeed.setPointerCapture?.(event.pointerId);
    });

    drinksFeed.addEventListener('pointermove', (event) => {
      if (!drag) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (!drag.axis) {
        if (Math.abs(dx) < DRAG_SLOP && Math.abs(dy) < DRAG_SLOP) return;
        drag.axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
      }
      if (drag.axis !== 'x') return;                      // вертикаль отдаём скроллу
      event.preventDefault();
      drag.lastX = event.clientX;
      drag.lastT = performance.now();
      drinkCarouselMove(dx, false);
    });

    const settle = (event) => {
      if (!drag) return;
      const state = drag;
      drag = null;
      if (state.axis === 'x') {
        const endX = event ? event.clientX : state.lastX;
        const dx = endX - state.x;
        const dt = Math.max(1, performance.now() - state.lastT);
        const velocity = (endX - state.lastX) / dt;
        const passed = Math.abs(dx) > slideWidth() * SNAP_FRACTION || Math.abs(velocity) > FLICK;
        if (passed) {
          const goNext = Math.abs(velocity) > FLICK ? velocity < 0 : dx < 0;
          drinkCarouselGoTo(drinkCarousel.index + (goNext ? 1 : -1));
        } else {
          drinkCarouselUpdate();                          // откат к текущей карточке
        }
        // Свайп не должен превращаться в клик по карточке.
        if (Math.abs(dx) > DRAG_SLOP) {
          suppressClick = true;
          setTimeout(() => { suppressClick = false; }, 400);
        }
      } else {
        drinkCarouselUpdate();
      }
    };
    drinksFeed.addEventListener('pointerup', settle);
    drinksFeed.addEventListener('pointercancel', () => settle(null));

    // Картинки внутри карточки догружаются позже — пересчитываем высоту вьюпорта,
    // чтобы соседние панели не «прыгали».
    drinksFeed.addEventListener('load', (event) => {
      if (event.target.tagName === 'IMG') drinkCarouselSyncHeight();
    }, true);
  }

  let drinksResizeRaf = 0;
  window.addEventListener('resize', () => {
    if (drinksResizeRaf) return;
    drinksResizeRaf = requestAnimationFrame(() => { drinksResizeRaf = 0; drinkCarouselSyncHeight(); });
  });
  const dialog = document.querySelector('#drink-dialog');
  if (!dialog) return;
  document.querySelector('#drink-add-milk')?.addEventListener('click', () => addDrinkItemRow('кофе с молоком'));
  const items = document.querySelector('#drink-items');
  items?.addEventListener('click', (event) => {
    if (!event.target.closest('.drink-item-remove')) return;
    if (document.querySelectorAll('#drink-items .drink-item-form').length <= 1) {
      showToast('Нужен хотя бы один напиток');
      return;
    }
    event.target.closest('.drink-item-form')?.remove();
    drinkSyncEspresso();
  });
  items?.addEventListener('change', (event) => {
    if (event.target.matches('[data-drink-field="is_espresso_recipe"]')) drinkSyncEspresso();
  });
  items?.addEventListener('input', (event) => {
    const input = event.target;
    if (input.matches('[data-drink-scale]')) {
      const list = (drinkState.scales || DRINK_FALLBACK_SCALES)[input.dataset.drinkGroup] || [];
      const out = input.parentElement.querySelector('output');
      if (out) out.textContent = list[Number(input.value)] || '';
    } else if (input.matches('[data-drink-field="recipe_dose"], [data-drink-field="recipe_yield"], [data-drink-field="recipe_time"], [data-drink-field="recipe_temp"]')) {
      drinkSyncEspresso();
    }
  });
  dialog.addEventListener('change', (event) => {
    const input = event.target.closest('[data-drink-photo-input]');
    if (!input) return;
    uploadDrinkPhoto(input.dataset.drinkPhotoInput, Number(input.dataset.authorId), input.files);
    input.value = '';
  });
  dialog.addEventListener('click', (event) => {
    const remove = event.target.closest('[data-drink-photo-remove]');
    if (!remove) return;
    const kind = remove.dataset.drinkPhotoRemove;
    const index = Number(remove.dataset.index);
    drinkState.photos[kind] = (drinkState.photos[kind] || []).filter((_, position) => position !== index);
    renderDrinkPreviews(kind);
    drinkState.flushDraft?.();
  });
  document.querySelector('#drink-partner')?.addEventListener('change', refreshDrinkBaristas);
  document.querySelector('#drink-manual-toggle')?.addEventListener('click', () => {
    const row = document.querySelector('#drink-partner-row');
    if (row) row.hidden = !row.hidden;
  });
  document.querySelector('#drink-paste')?.addEventListener('click', pasteDrinkTemplate);
  document.querySelector('#drink-form')?.addEventListener('submit', submitDrinkForm);
}

/* --- Панель управления: навигация по модулям и вкл/выкл ------------------- */

function controlIsNarrow() {
  return window.innerWidth < 900;
}

/* Подсвечивает активный пункт списка и обновляет бейджи «Вкл»/«Выкл». */
function renderControlNav() {
  const list = document.querySelector('#control-nav');
  if (!list) return;
  const items = list.querySelectorAll('[data-control-open]');
  const count = document.querySelector('#control-count');
  if (count) count.textContent = String(items.length);
  items.forEach((item) => {
    const module = modulesState[item.dataset.controlOpen];
    if (module) {
      item.classList.toggle('is-disabled', !module.enabled);
      const badge = item.querySelector('[data-control-badge]');
      if (badge) badge.textContent = module.enabled ? 'Вкл' : 'Выкл';
    }
    item.classList.toggle('is-active', item.dataset.controlOpen === controlState.current);
  });
}

function controlSetTreeOpen(open) {
  controlState.treeOpen = open;
  document.querySelector('#control-tree-fold')?.classList.toggle('is-open', open);
  const button = document.querySelector('#control-tree-toggle');
  if (button) {
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    button.setAttribute('aria-label', open ? 'Свернуть список модулей' : 'Развернуть список модулей');
  }
  controlUpdateFab();
}

/* Плавающая кнопка «Модули»: только на узком экране, когда список свёрнут и
   его шапка ушла вверх при прокрутке — как «Оглавление» в базе знаний. */
function controlUpdateFab() {
  const fab = document.querySelector('#control-tree-fab');
  if (!fab) return;
  const view = document.querySelector('#control-view');
  const inView = Boolean(view) && !view.hidden;
  const panel = document.querySelector('.control-nav-panel');
  const headerOut = panel ? panel.getBoundingClientRect().bottom <= 0 : false;
  fab.classList.toggle('is-visible', inView && controlIsNarrow() && !controlState.treeOpen && headerOut);
}

/* Показывает блоки выбранного модуля и подсвечивает пункт списка. */
function selectControlModule(code) {
  controlState.current = code;
  document.querySelectorAll('[data-control-module]').forEach((block) => { block.hidden = block.dataset.controlModule !== code; });
  renderControlNav();
}

function ensureControlSelection() {
  if (!controlState.current || !document.querySelector(`#control-nav [data-control-open="${controlState.current}"]`)) {
    const first = document.querySelector('#control-nav [data-control-open]');
    controlState.current = first ? first.dataset.controlOpen : null;
  }
  if (controlState.current) selectControlModule(controlState.current);
}

function setupControlPanel() {
  const nav = document.querySelector('#control-nav');
  if (!nav) return;
  nav.addEventListener('click', (event) => {
    const item = event.target.closest('[data-control-open]');
    if (item) selectControlModule(item.dataset.controlOpen);
  });
  document.querySelectorAll('[data-module-toggle]').forEach((input) => {
    if (input.dataset.bound) return;
    input.dataset.bound = '1';
    input.addEventListener('change', async (event) => {
      const box = event.currentTarget;
      const code = box.dataset.moduleToggle;
      box.disabled = true;
      try {
        await api(`/api/modules/${code}`, { method: 'PATCH', body: JSON.stringify({ enabled: box.checked }) });
        // Включение возвращает раздел в навигацию, выключение — убирает его.
        // Сервер рендерит навигацию по флагам модулей, поэтому перезагружаем
        // страницу, оставаясь в панели управления (якорь #control).
        location.hash = 'control';
        window.location.reload();
      } catch (error) {
        box.checked = !box.checked;
        box.disabled = false;
        showToast(error.message);
      }
    });
  });
  document.querySelector('#control-tree-toggle')?.addEventListener('click', () => controlSetTreeOpen(!controlState.treeOpen));
  const fab = document.querySelector('#control-tree-fab');
  if (fab && !fab.dataset.bound) {
    fab.dataset.bound = '1';
    fab.addEventListener('click', () => {
      controlSetTreeOpen(true);
      document.querySelector('.control-nav-panel')?.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'start' });
    });
  }
  const panel = document.querySelector('.control-nav-panel');
  if (panel && !panel.dataset.fabObserved && 'IntersectionObserver' in window) {
    panel.dataset.fabObserved = '1';
    new IntersectionObserver(() => controlUpdateFab(), { threshold: 0 }).observe(panel);
  }
  if (!document.body.dataset.controlFabResize) {
    document.body.dataset.controlFabResize = '1';
    window.addEventListener('resize', controlUpdateFab);
  }
  // Настройки модуля (бывшие константы): один submit на форму модуля.
  document.querySelectorAll('[data-module-settings]').forEach((form) => {
    if (form.dataset.bound) return;
    form.dataset.bound = '1';
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const code = form.dataset.moduleSettings;
      const error = form.querySelector('.template-form-error');
      if (error) error.hidden = true;
      const payload = {};
      form.querySelectorAll('input[name]').forEach((input) => {
        payload[input.name] = input.type === 'number' ? Number(input.value) : input.value;
      });
      const submit = form.querySelector('button[type="submit"]');
      if (submit) submit.disabled = true;
      try {
        await api(`/api/modules/${code}/settings`, { method: 'PATCH', body: JSON.stringify(payload) });
        showToast('Настройки сохранены');
      } catch (err) {
        if (error) { error.textContent = err.message; error.hidden = false; }
        else showToast(err.message);
      } finally {
        if (submit) submit.disabled = false;
      }
    });
  });
}

function setupDashboard() {
  document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => setView(button.dataset.view)));
  setupAppearance();
  setupDrawer();
  setupProfile();
  setupControlPanel();
  setupWorkloadForm();
  setupAnalytics();
  setupAnnouncements();
  loadAnnouncements();
  setupKnowledgeBase();
  setupDrinks();
  setupMediaViewer();
  setupFormDrafts();
  loadDrinksWidget();
  setupDevelopment();
  setupAchievements();
  loadDevelopment();
  loadEvaluationAnalytics();

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
      loadUserWorkloadFields(form, user.id);
    } else {
      applyWorkloadFields(form, null);
    }
    dialog.showModal();
  };
  document.querySelector('#add-user-button')?.addEventListener('click', () => setDialogMode());
  document.querySelectorAll('#user-dialog .dialog-close').forEach((button) => button.addEventListener('click', () => dialog?.close()));

  form?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const errorNode = formError;
    const formData = new FormData(form);
    const payload = Object.fromEntries(formData.entries());
    // Нормо-часы уходят отдельным PATCH: у пользователя их принимает /workload.
    const workload = {
      min_hours: payload.min_hours,
      opt_hours: payload.opt_hours,
      max_hours: payload.max_hours,
    };
    delete payload.min_hours;
    delete payload.opt_hours;
    delete payload.max_hours;
    payload.iiko_id = payload.iiko_id || null;
    payload.telegram_username = payload.telegram_username || null;
    try {
      const userId = form.dataset.userId;
      const saved = await api(userId ? `/api/users/${userId}` : '/api/users', { method: userId ? 'PATCH' : 'POST', body: JSON.stringify(payload) });
      const targetId = userId || saved?.id;
      if (targetId && workload.opt_hours) {
        await api(`/api/users/${targetId}/workload`, { method: 'PATCH', body: JSON.stringify(workload) });
      }
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
    if (editMode || userRole === 'newbie') return;
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
  // --- Перенос смен ---
  matrix?.addEventListener('pointerdown', (event) => {
    // Только в режиме ячейки, только с заполненной ячейки, только левая кнопка.
    if (!editMode || matrixMode !== 'edit' || event.button > 0) return;
    const cell = event.target.closest('[data-matrix-cell]');
    if (!cell) return;
    startShiftDrag(event, cell);
  });
  matrix?.addEventListener('pointermove', (event) => {
    const drag = shiftDrag;
    if (!drag || event.pointerId !== drag.pointerId) return;
    const dx = Math.abs(event.clientX - drag.originX);
    const dy = Math.abs(event.clientY - drag.originY);
    if (!drag.active) {
      if (drag.pointerType === 'touch') {
        // Палец сдвинулся до удержания — это скролл, а не перенос.
        if (Math.max(dx, dy) > DRAG_TOUCH_SLOP) cancelShiftDrag();
        return;
      }
      if (Math.max(dx, dy) < DRAG_MOUSE_SLOP) return;
      activateShiftDrag();
    }
    event.preventDefault();
    drag.ghost.style.left = `${event.clientX}px`;
    drag.ghost.style.top = `${event.clientY}px`;
    updateDropTarget(event.clientX, event.clientY);
  });
  matrix?.addEventListener('pointerup', (event) => {
    if (!shiftDrag || event.pointerId !== shiftDrag.pointerId) return;
    finishShiftDrag(true);
  });
  matrix?.addEventListener('pointercancel', () => { if (shiftDrag) finishShiftDrag(false); });
  // Пока смена «в руке», окно зафиксировано: прокрутки нет (в т.ч. на мобильных).
  document.addEventListener('touchmove', (event) => {
    if (shiftDrag?.active) event.preventDefault();
  }, { passive: false });
  document.addEventListener('contextmenu', (event) => {
    if (shiftDrag?.active) event.preventDefault();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && shiftDrag) finishShiftDrag(false);
  });
  matrix?.addEventListener('click', (event) => {
    if (!editMode || suppressCellClick) return;
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
      : 'Нажмите ячейку или перетащите смену на другую';
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
  // Второй палец — это зум, а не перенос: снимаем незапущенный перенос.
  scroll?.addEventListener('touchstart', (event) => {
    if (event.touches.length === 2 && shiftDrag && !shiftDrag.active) cancelShiftDrag();
  }, { passive: true });
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

  document.querySelector('#barista-preview-toggle')?.addEventListener('change', async (event) => {
    const box = event.currentTarget;
    try {
      await api('/api/preview', { method: 'POST', body: JSON.stringify({ role: box.checked ? 'barista' : '' }) });
      window.location.reload();
    } catch (error) {
      box.checked = !box.checked;
      showToast(error.message);
    }
  });

  // Все диалоги закрываются только кнопками .dialog-close (или Esc): клик по
  // краю окна/подложке больше не закрывает форму.
  document.querySelectorAll('.dialog-close').forEach((button) => button.addEventListener('click', () => button.closest('dialog')?.close()));

  updateEditControls();
  loadPreferences();
  loadModules().then(() => {
    if (!modulesState.calendar || modulesState.calendar.enabled) loadCalendar();
    if (managerRole && location.hash === '#control') setView('control');
  });
  loadShiftTemplates();
  if (managerRole) {
    setupStoriesAdmin();
    setupShiftTemplates();
    setupChannelSettings();
    setupTeamTabs();
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
  openMediaViewer,
  // Ссылки на статьи базы знаний: рендер маркеров и подсказка `@` (chat.js).
  kbLinkify,
  kbPageLinkTitle,
  kbSearchPages,
  openKbPage: openKbPageFromLink,
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
