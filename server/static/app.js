'use strict';
/* GlassMCP dashboard — vanilla JS (ES2020), один файл, без внешних ресурсов. */
(() => {

const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

const RECONNECT_TEXT = 'Соединение с панелью потеряно, переподключение…';
const RESTART_TEXT = 'Сервер перезапускается…';
const FOCUSABLE = 'button, input, textarea, [href], [tabindex]:not([tabindex="-1"])';
const TAB_IDS = ['general', 'chatco', 'claude', 'chatgpt', 'zcode', 'cursor'];

const state = {
  status: null,
  config: null,
  conn: null,
  zones: [],
  allowed: [],
  snap: null,
  auditLines: [],
  auditFile: false,
  fails: 0,
  statusDelay: 2000,
  restartPending: false,
  lastMode: null,
  lastWorkdirKey: null,
  lastZonesKey: null,
  lastTunnelRunning: null,
  lastPublicUrl: null,
  activeTab: 'general',
  timers: {}
};

/* ================= API ================= */

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch (e) {
    const err = new Error('нет соединения');
    err.network = true;
    throw err;
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* тело не JSON */ }
  if (!res.ok || (data && data.ok === false)) {
    const err = new Error((data && data.error) || ('HTTP ' + res.status));
    err.http = res.status;
    throw err;
  }
  return data || {};
}

function handleErr(e, prefix) {
  if (e && e.network) return; // сетевые ошибки показывает оверлей
  toast((prefix ? prefix + ': ' : '') + (e && e.message ? e.message : 'неизвестная ошибка'), 'err');
}

/* ================= Утилиты ================= */

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, ch => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}
const pad = n => String(n).padStart(2, '0');

function fmtUptime(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  return pad(Math.floor(s / 3600)) + ':' + pad(Math.floor((s % 3600) / 60));
}

function fmtAgo(tsSec) {
  if (!tsSec) return 'никогда';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - Number(tsSec)));
  if (s < 10) return 'только что';
  if (s < 60) return s + ' сек назад';
  if (s < 3600) return Math.floor(s / 60) + ' мин назад';
  return Math.floor(s / 3600) + ' ч назад';
}

function toDate(v) {
  if (v == null || v === '') return null;
  if (typeof v === 'number') {
    const ms = v > 1e11 ? v : v * 1000;
    const d = new Date(ms);
    return isNaN(d.getTime()) ? null : d;
  }
  const d = new Date(v);
  return isNaN(d.getTime()) ? null : d;
}

function fmtClockD(d) { return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()); }

function fmtDate(v) {
  const d = toDate(v);
  if (!d) return String(v ?? '');
  return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}

function fmtMB(b) {
  const n = Number(b);
  if (!Number.isFinite(n) || n <= 0) return '';
  return String(Math.round((n / 1048576) * 100) / 100);
}

function num(v) {
  const n = Number(String(v ?? '').trim().replace(',', '.'));
  return Number.isFinite(n) ? n : NaN;
}

function lines(v) {
  return String(v ?? '').split('\n').map(s => s.trim()).filter(Boolean);
}

function ensureMcp(u) {
  if (!u) return '';
  let s = String(u).trim().replace(/\/+$/, '');
  if (!/\/mcp$/.test(s)) s += '/mcp';
  return s;
}

/* ================= Тосты ================= */

function toast(msg, type) {
  const box = $('#toasts');
  const t = document.createElement('div');
  t.className = 'toast ' + (type || 'info');
  t.textContent = String(msg);
  box.appendChild(t);
  const kill = () => {
    if (!t.isConnected) return;
    t.classList.add('out');
    setTimeout(() => t.remove(), 300);
  };
  t.addEventListener('click', kill);
  setTimeout(kill, 3800);
}

/* ================= Копирование ================= */

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:-9999px;left:0;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { /* нет поддержки */ }
    ta.remove();
    return ok;
  }
}

async function doCopy(text) {
  const v = String(text ?? '').trim();
  if (!v) { toast('Нечего копировать', 'warn'); return; }
  const ok = await copyText(v);
  toast(ok ? 'Скопировано' : 'Не удалось скопировать', ok ? 'ok' : 'err');
}

document.addEventListener('click', e => {
  const b = e.target.closest('[data-copy],[data-copy-el]');
  if (!b || b.disabled) return;
  let v = '';
  if (b.hasAttribute('data-copy')) v = b.getAttribute('data-copy');
  else {
    const src = document.getElementById(b.getAttribute('data-copy-el'));
    v = src ? src.textContent : '';
  }
  doCopy(v);
});

/* ================= Модалки: фокус-трап, Escape, клик по фону ================= */

let modalStack = [];
const prevFocus = new Map();

function openModal(m, focusSel) {
  if (m.classList.contains('open')) return; // уже открыта: не дублировать в стеке
  prevFocus.set(m, document.activeElement);
  m.hidden = false;
  m.classList.add('open');
  modalStack.push(m);
  const t = focusSel ? $(focusSel, m) : $(FOCUSABLE, m);
  if (t) setTimeout(() => t.focus(), 0);
}

function closeModal(m) {
  if (!m || m.hidden) return;
  m.classList.remove('open');
  m.hidden = true;
  modalStack = modalStack.filter(x => x !== m);
  if (m.id === 'modal-confirm' && confirmResolve) {
    const r = confirmResolve;
    confirmResolve = null;
    r(false);
  }
  const p = prevFocus.get(m);
  if (p && p.isConnected) p.focus();
  prevFocus.delete(m);
}

document.addEventListener('keydown', e => {
  const top = modalStack[modalStack.length - 1];
  if (!top) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    // обязательные модалки (первый запуск / удалённая папка) не закрываются
    if (top.dataset.dismissible !== 'false') closeModal(top);
    return;
  }
  if (e.key !== 'Tab') return;
  const items = $$(FOCUSABLE, top).filter(n => !n.disabled && n.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  const inside = top.contains(document.activeElement);
  if (e.shiftKey && (!inside || document.activeElement === first)) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && (!inside || document.activeElement === last)) {
    e.preventDefault();
    first.focus();
  }
});

$$('.modal-backdrop').forEach(bk => {
  bk.addEventListener('click', e => {
    if (e.target === bk && bk.dataset.dismissible !== 'false') closeModal(bk);
  });
});

/* ================= Кастомное подтверждение ================= */

let confirmResolve = null;

function confirmDialog(opts) {
  return new Promise(resolve => {
    $('#mc-title').textContent = (opts && opts.title) || 'Подтверждение';
    $('#mc-text').textContent = (opts && opts.text) || '';
    const ok = $('#mc-ok');
    ok.textContent = (opts && opts.okLabel) || 'Подтвердить';
    ok.className = 'btn ' + ((opts && opts.danger) ? 'btn-danger' : 'btn-primary');
    openModal($('#modal-confirm'), '#mc-ok');
    confirmResolve = resolve;
  });
}

$('#mc-ok').addEventListener('click', () => {
  const r = confirmResolve;
  confirmResolve = null;
  closeModal($('#modal-confirm'));
  if (r) r(true);
});
$('#mc-cancel').addEventListener('click', () => {
  const r = confirmResolve;
  confirmResolve = null;
  closeModal($('#modal-confirm'));
  if (r) r(false);
});

/* ================= Фон: живой mesh-градиент ================= */

function initCanvas() {
  const cv = $('#bg');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  if (!cv || reduced.matches) return;
  const ctx = cv.getContext('2d');
  if (!ctx) return;
  let W = 0, H = 0;
  const blobs = [
    { a: .10, r: .42, bx: .22, by: .30, ax: .18, ay: .12, T: 47, p: 0.0 },
    { a: .08, r: .36, bx: .80, by: .22, ax: .15, ay: .20, T: 38, p: 2.1 },
    { a: .07, r: .40, bx: .66, by: .74, ax: .20, ay: .15, T: 55, p: 4.2 },
    { a: .05, r: .45, bx: .26, by: .82, ax: .16, ay: .18, T: 31, p: 1.3 },
    { a: .03, r: .52, bx: .52, by: .48, ax: .22, ay: .14, T: 43, p: 3.4 }
  ];
  function draw(now) {
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#050505';
    ctx.fillRect(0, 0, W, H);
    const t = now / 1000;
    for (const b of blobs) {
      const x = (b.bx + b.ax * Math.sin((t / b.T) * Math.PI * 2 + b.p)) * W;
      const y = (b.by + b.ay * Math.cos((t / b.T) * Math.PI * 2 + b.p * 1.7)) * H;
      const rad = b.r * Math.max(W, H);
      const grad = ctx.createRadialGradient(x, y, 0, x, y, rad);
      grad.addColorStop(0, 'rgba(255,255,255,' + b.a + ')');
      grad.addColorStop(0.55, 'rgba(255,255,255,' + (b.a * 0.45).toFixed(3) + ')');
      grad.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(x, y, rad, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = window.innerWidth;
    H = window.innerHeight;
    cv.width = Math.round(W * dpr);
    cv.height = Math.round(H * dpr);
    cv.style.width = W + 'px';
    cv.style.height = H + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw(performance.now());
  }
  function loop(now) {
    if (reduced.matches) return; // полный останов при reduced motion
    if (!document.hidden) draw(now);
    requestAnimationFrame(loop);
  }
  window.addEventListener('resize', resize);
  resize();
  requestAnimationFrame(loop);
}

/* ================= Опрос статуса (2с, бэкофф до 5с, оверлей) ================= */

const ov = $('#reconnect');

function showOverlay(text) {
  $('#reconnect-text').textContent = text || RECONNECT_TEXT;
  ov.hidden = false;
}

function hideOverlay() {
  ov.hidden = true;
  $('#reconnect-text').textContent = RECONNECT_TEXT;
  state.restartPending = false;
}

function onNetFail() {
  state.fails++;
  state.statusDelay = Math.min(2000 + (state.fails - 1) * 1000, 5000);
  showOverlay(state.restartPending ? RESTART_TEXT : RECONNECT_TEXT);
}

function onNetOk() {
  state.fails = 0;
  state.statusDelay = 2000;
  if (!ov.hidden) hideOverlay();
}

let statusTimer = null;
let polling = false;

function scheduleStatus() {
  clearTimeout(statusTimer);
  statusTimer = setTimeout(pollStatus, state.statusDelay);
}

async function pollStatus() {
  if (polling) return;
  polling = true;
  try {
    const s = await api('GET', '/api/status');
    onNetOk();
    state.status = s;
    renderAll(s);
  } catch (e) {
    if (e.network) onNetFail();
    else if (state.status) toast('Статус: ' + e.message, 'err');
  } finally {
    polling = false;
    scheduleStatus();
  }
}

/* ================= Рендер: общий ================= */

function renderAll(s) {
  renderStatus(s);
  renderTunnel(s.tunnel || {});
  renderPublic(s);
  renderWorkdir(s);
  if (s.onboarding_required || s.workdir_missing) {
    // обязательный выбор папки: первый запуск или настроенная папка удалена
    openFolderPicker({ mandatory: true, missing: !!s.workdir_missing, path: s.workdir || '' });
  }
  const m = s.mode || 'full';
  if (m !== state.lastMode) {
    state.lastMode = m;
    renderAllowedTags();
  }
  const zk = m + '|' + (s.zones_count ?? 0);
  if (zk !== state.lastZonesKey) {
    state.lastZonesKey = zk;
    loadZones();
  }
  const tr = !!(s.tunnel && s.tunnel.running);
  const pub = !!(s.public_url || '');
  if (tr !== state.lastTunnelRunning || pub !== state.lastPublicUrl) {
    state.lastTunnelRunning = tr;
    state.lastPublicUrl = pub;
    // внешний адрес (туннель или свой) изменился — перечитываем данные подключения
    loadConnections();
  }
}

/* ================= Секция: Статус ================= */

function renderStatus(s) {
  const srv = s.server || {};
  const run = !!srv.running;
  $('#status-dot').className = 'big-dot ' + (run ? 'ok' : 'err');
  $('#status-title').textContent = run ? 'Сервер запущен' : 'Сервер остановлен';
  $('#status-uptime').textContent = run ? 'Аптайм ' + fmtUptime(srv.uptime_s) : '—';

  const ver = $('#status-version');
  if (srv.version) {
    ver.hidden = false;
    ver.textContent = 'v' + srv.version;
  } else ver.hidden = true;

  const ml = $('#status-mode');
  if (s.mode === 'workdir') {
    ml.innerHTML = '<span class="badge badge-folder">Режим рабочей папки</span>' +
      '<code class="mono mode-path">' + esc(s.workdir || '') + '</code>';
  } else {
    ml.innerHTML = '<span class="badge badge-full">Полный доступ (с подтверждением зон)</span>';
  }

  $('#status-url').textContent = (srv.mcp_host != null && srv.mcp_port != null)
    ? 'http://' + srv.mcp_host + ':' + srv.mcp_port + '/mcp'
    : '—';
  $('#status-tools').textContent = srv.tools_count != null ? String(srv.tools_count) : '—';

  $('#pill-dot').className = 'pill-dot ' + (run ? 'ok' : 'err');
  $('#pill-text').textContent = run ? 'Сервер запущен' : 'Сервер остановлен';
}

$('#btn-restart').addEventListener('click', async () => {
  try {
    await api('POST', '/api/server/restart');
    state.restartPending = true;
    showOverlay(RESTART_TEXT);
  } catch (e) {
    handleErr(e, 'Перезапуск');
  }
});

/* ================= Секция: Туннель ================= */

function renderTunnel(t) {
  let cls = 'idle';
  let title = 'Не подключён';
  let sub = '';
  const hasUrl = !!t.url;
  const probe = t.probe || {};
  const dead = probe.state === 'fail';
  if (dead) {
    cls = 'err';
    title = 'Туннель отвалился';
    sub = probe.last_error || 'публичный адрес не отвечает';
  } else if (t.error) {
    cls = 'err';
    title = 'Ошибка туннеля';
    sub = String(t.error);
  } else if (t.running && hasUrl) {
    cls = 'ok';
    title = 'Работает';
    sub = t.mode === 'quick' ? 'Быстрый режим' : '';
    if (probe.state === 'ok') sub += ' · проверен ' + fmtAgo(probe.last_ok_at);
    else if (probe.state === 'checking') sub += ' · проверяем…';
    else sub += ' · проверка скоро';
  } else if (t.running || t.starting) {
    cls = 'warn';
    title = 'Поднимается…';
    sub = t.binary_ok === false ? 'Загружаем утилиту туннеля…' : 'Получаем адрес…';
  }
  $('#tunnel-dot').className = 'big-dot ' + cls;
  $('#tunnel-title').textContent = title;
  $('#tunnel-sub').textContent = sub;
  const row = $('#tunnel-url-row');
  row.hidden = !hasUrl;
  // показываем и копируем URL сразу с /mcp — готовый адрес для клиента
  if (hasUrl) $('#tunnel-url').textContent = ensureMcp(t.url);
  const deadBox = $('#tunnel-dead');
  deadBox.hidden = !dead;
  if (dead) {
    deadBox.innerHTML = '<strong>Публичный адрес не отвечает.</strong> ' +
      esc(probe.last_error || '') + '<br>Последний успешный ответ: ' +
      esc(fmtAgo(probe.last_ok_at)) + '. Клиенты по этому адресу не подключатся. ' +
      '<button type="button" class="btn btn-ghost btn-sm" data-act="tunnel-reconnect">Переподключить</button>';
  }
  $('#tunnel-wait').hidden = !(t.running || t.starting) || hasUrl;
  $('#btn-tunnel-start').disabled = !!(t.running || t.starting);
  $('#btn-tunnel-stop').disabled = !(t.running || t.starting);
  $('#btn-tunnel-check').disabled = !hasUrl;
}

$('#btn-tunnel-start').addEventListener('click', async () => {
  try {
    const d = await api('POST', '/api/tunnel/start', { mode: 'quick' });
    if (d.ok === false) throw new Error(d.error || 'не удалось запустить');
    toast('Туннель запускается…', 'info');
    pollStatus();
  } catch (e) {
    handleErr(e, 'Туннель');
  }
});

$('#btn-tunnel-stop').addEventListener('click', async () => {
  try {
    await api('POST', '/api/tunnel/stop');
    toast('Туннель остановлен', 'info');
    pollStatus();
  } catch (e) {
    handleErr(e, 'Туннель');
  }
});

$('#btn-tunnel-check').addEventListener('click', async () => {
  try {
    const d = await api('POST', '/api/tunnel/check');
    const p = d.probe || {};
    if (p.state === 'ok') toast('Туннель отвечает: /health вернул ok', 'ok');
    else toast('Туннель не отвечает: ' + (p.last_error || 'нет ответа'), 'err');
    pollStatus();
  } catch (e) {
    handleErr(e, 'Проверка туннеля');
  }
});

$('#tunnel-dead').addEventListener('click', async e => {
  const b = e.target.closest('[data-act="tunnel-reconnect"]');
  if (!b) return;
  try {
    b.disabled = true;
    await api('POST', '/api/tunnel/stop');
    await api('POST', '/api/tunnel/start', { mode: 'quick' });
    toast('Туннель перезапускается… адрес будет новый', 'info');
    pollStatus();
  } catch (err) {
    handleErr(err, 'Туннель');
  }
});

/* ================= Секция: Свой адрес (IP / домен) ================= */

function renderPublic(s) {
  const url = String(s.public_url || '');
  const dot = $('#public-dot');
  if (url) {
    dot.className = 'big-dot ok';
    $('#public-title').textContent = 'Задан';
    $('#public-sub').textContent = '«Подключение» использует его вместо туннельного URL';
  } else {
    dot.className = 'big-dot idle';
    $('#public-title').textContent = 'Не задан';
    $('#public-sub').textContent = 'внешний доступ — только через туннель';
  }
  $('#public-url-row').hidden = !url;
  if (url) $('#public-url').textContent = ensureMcp(url);
  $('#btn-public-clear').disabled = !url;
  // не затирать то, что пользователь уже набирает
  const inp = $('#public-input');
  if (document.activeElement !== inp) inp.value = '';
}

async function savePublicUrl(raw) {
  try {
    const d = await api('POST', '/api/config', { public_url: raw });
    if (d.ok === false) throw new Error(d.error || 'не удалось сохранить');
    $('#public-input').value = '';
    toast(raw.trim() ? 'Свой адрес сохранён' : 'Свой адрес удалён', 'ok');
    pollStatus();
    loadConnections();
  } catch (e) {
    handleErr(e, 'Свой адрес');
  }
}

$('#btn-public-save').addEventListener('click', () => {
  const v = $('#public-input').value.trim();
  if (!v) { toast('Введи адрес: например mcp.example.com или http://1.2.3.4:8000', 'warn'); return; }
  savePublicUrl(v);
});
$('#btn-public-clear').addEventListener('click', () => savePublicUrl(''));
$('#public-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); $('#btn-public-save').click(); }
});

/* ================= Секция: Рабочая папка ================= */

function renderWorkdir(s) {
  const key = (s.mode || 'full') + '|' + (s.workdir || '') + '|' + (s.workdir_missing ? '1' : '0');
  if (key === state.lastWorkdirKey) return;
  state.lastWorkdirKey = key;
  const b = $('#workdir-body');
  const card = $('#card-workdir');
  if (s.mode === 'workdir') {
    card.classList.add('danger-accent');
    if (s.workdir_missing) {
      b.innerHTML =
        '<div class="alert alert-warn"><strong>Папка недоступна:</strong> она была удалена или переименована. Доступ к файлам заблокирован до выбора новой.</div>' +
        '<div class="kv"><span class="kv-l">Старый путь</span>' +
        '<code class="mono kv-v">' + esc(s.workdir || '') + '</code></div>' +
        '<div class="btn-row"><button type="button" class="btn btn-primary" data-act="wd-change">Выбрать новую папку</button></div>';
      return;
    }
    b.innerHTML =
      '<p class="muted small">Инструменты ограничены рабочей папкой.</p>' +
      '<div class="kv"><span class="kv-l">Путь</span>' +
      '<code class="mono kv-v" id="wd-path">' + esc(s.workdir || '') + '</code>' +
      '<button type="button" class="icon-btn" data-copy-el="wd-path" aria-label="Скопировать путь" title="Скопировать"><svg class="ic" aria-hidden="true"><use href="#i-copy"></use></svg></button></div>' +
      '<div class="alert alert-warn small">Файловые инструменты изолированы строго. ' +
      'Терминал — с оговоркой: команды выполняются внутри папки, но shell нельзя ' +
      'изолировать абсолютно (переменные окружения, обходы). Не выдавайте доступ ' +
      'недоверенным клиентам.</div>' +
      '<div class="btn-row">' +
      '<button type="button" class="btn btn-ghost" data-act="wd-change">Сменить</button>' +
      '<button type="button" class="btn btn-danger-ghost" data-act="wd-off">Отключить (полный доступ)</button>' +
      '</div>';
  } else {
    card.classList.remove('danger-accent');
    b.innerHTML =
      '<div class="alert alert-warn"><strong>Полный доступ к диску.</strong> ' +
      'Инструменты могут работать с любыми папками; каждая новая зона будет запрашивать подтверждение.</div>' +
      '<div class="btn-row"><button type="button" class="btn btn-primary" data-act="wd-pick">' +
      '<svg class="ic" aria-hidden="true"><use href="#i-folder"></use></svg>Выбрать папку</button></div>';
  }
}

async function setWorkdir(path) {
  try {
    const d = await api('POST', '/api/config', { workdir: path });
    if (d.ok === false) throw new Error(d.error || 'ошибка сохранения');
    toast(path ? 'Рабочая папка обновлена' : 'Рабочая папка отключена — включён полный доступ', 'ok');
    state.lastWorkdirKey = null;
    state.lastZonesKey = null;
    pollStatus();
    loadConfig();
    loadZones();
    loadConnections();
    return true;
  } catch (e) {
    handleErr(e, 'Не удалось изменить рабочую папку');
    return false;
  }
}

document.addEventListener('click', async e => {
  const b = e.target.closest('[data-act]');
  if (!b || b.disabled) return;
  const act = b.getAttribute('data-act');
  if (act === 'wd-pick' || act === 'wd-change') {
    openFolderPicker();
  } else if (act === 'wd-off') {
    const yes = await confirmDialog({
      title: 'Включить полный доступ?',
      text: 'Рабочая папка будет отключена: инструменты получат доступ ко всему диску, а новые зоны будут запрашивать подтверждение. Это менее безопасный режим.',
      okLabel: 'Отключить папку',
      danger: true
    });
    if (yes) await setWorkdir(null);
  }
});

/* ================= Модалка: выбор папки ================= */

let fpPath = '';

async function openFolderPicker(opts) {
  const o = opts || {};
  const modal = $('#modal-folder');
  // повторный триггер из поллинга: не сбрасывать навигацию и фокус
  if (modal.classList.contains('open')) return;
  const mandatory = !!o.mandatory;
  modal.dataset.dismissible = mandatory ? 'false' : 'true';
  $('#mf-close').hidden = mandatory;
  $('#fp-reset').hidden = mandatory;
  $('#mf-title').textContent = o.missing
    ? 'Рабочая папка недоступна'
    : (mandatory ? 'Выбери рабочую папку проекта' : 'Выбор рабочей папки');
  const sub = $('#mf-sub');
  sub.hidden = !mandatory;
  if (o.missing) {
    sub.textContent = 'Папка ' + (o.path || '') + ' была удалена или переименована. ' +
      'Доступ к файлам заблокирован — выбери новую папку. Закрыть окно без выбора нельзя.';
  } else if (mandatory) {
    sub.textContent = 'GlassMCP будет работать только внутри выбранной папки и не полезет дальше. ' +
      'Это обязательный шаг: без папки доступ к файлам закрыт.';
  } else {
    sub.textContent = '';
  }
  openModal(modal, '#fp-input');
  fpPath = '';
  $('#fp-list').innerHTML = '<p class="muted small">Загрузка…</p>';
  $('#fp-crumbs').innerHTML = '';
  $('#fp-input').value = '';
  await fpBrowse('');
}

async function fpBrowse(p) {
  try {
    const d = await api('GET', '/api/browse?path=' + encodeURIComponent(p || ''));
    fpPath = d.path || p || '';

    let html = '<button type="button" class="crumb" data-crumb="">Домашняя</button>';
    let acc = '';
    for (const seg of String(fpPath).replace(/\/+$/, '').split('/').filter(Boolean)) {
      acc += '/' + seg;
      html += '<span class="crumb-sep">›</span>' +
        '<button type="button" class="crumb" data-crumb="' + esc(acc) + '">' + esc(seg) + '</button>';
    }
    $('#fp-crumbs').innerHTML = html;

    const dirs = d.dirs || [];
    const list = $('#fp-list');
    if (d.parent) {
      list.innerHTML = '<button type="button" class="fp-item fp-up" data-dir="' + esc(d.parent) + '">…на уровень выше</button>';
    } else {
      list.innerHTML = '';
    }
    if (!dirs.length) {
      list.insertAdjacentHTML('beforeend', '<p class="muted small" style="padding:6px 10px">Вложенных папок нет</p>');
    } else {
      list.insertAdjacentHTML('beforeend', dirs.map(dd => {
        const path = typeof dd === 'string' ? dd : (dd.path || dd.name || '');
        const name = String(path).split(/[\\/]/).filter(Boolean).pop() || String(path);
        return '<button type="button" class="fp-item" data-dir="' + esc(path) + '">' +
          '<svg class="ic" aria-hidden="true"><use href="#i-folder"></use></svg>' +
          '<span>' + esc(name) + '</span></button>';
      }).join(''));
    }
    $('#fp-input').value = fpPath;
    $('#fp-reset').disabled = (state.status ? state.status.mode !== 'workdir' : true);
  } catch (e) {
    handleErr(e, 'Не удалось открыть папку');
  }
}

$('#fp-list').addEventListener('click', e => {
  const it = e.target.closest('[data-dir]');
  if (it) fpBrowse(it.getAttribute('data-dir'));
});

$('#fp-crumbs').addEventListener('click', e => {
  const c = e.target.closest('[data-crumb]');
  if (c) fpBrowse(c.getAttribute('data-crumb'));
});

$('#fp-choose').addEventListener('click', async () => {
  if (!fpPath) { toast('Сначала выбери папку в списке', 'warn'); return; }
  const ok = await setWorkdir(fpPath);
  if (ok) closeModal($('#modal-folder'));
});

async function applyManualPath() {
  const v = $('#fp-input').value.trim();
  if (!v) { toast('Укажите путь', 'warn'); return; }
  const ok = await setWorkdir(v);
  if (ok) closeModal($('#modal-folder'));
}
$('#fp-apply').addEventListener('click', applyManualPath);
$('#fp-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); applyManualPath(); }
});

$('#fp-reset').addEventListener('click', async () => {
  const yes = await confirmDialog({
    title: 'Сбросить рабочую папку?',
    text: 'Будет включён полный доступ к диску — инструменты смогут работать с любыми папками. Это менее безопасный режим.',
    okLabel: 'Сбросить',
    danger: true
  });
  if (!yes) return;
  const ok = await setWorkdir(null);
  if (ok) closeModal($('#modal-folder'));
});

$('#mf-close').addEventListener('click', () => closeModal($('#modal-folder')));

/* ================= Секция: Настройки ================= */

function readSettings() {
  return {
    max_file_bytes: Math.round(num($('#set-maxfile').value) * 1048576),
    max_output_chars: Math.round(num($('#set-maxout').value)),
    default_shell_timeout: Math.round(num($('#set-tdef').value)),
    max_shell_timeout: Math.round(num($('#set-tmax').value)),
    deny_patterns: lines($('#deny-patterns').value),
    deny_paths: lines($('#deny-paths').value),
    allowed_dirs: state.allowed.slice()
  };
}

function renderAllowedTags() {
  const editable = !state.status || state.status.mode !== 'workdir';
  const wrap = $('#allowed-tags');
  if (!state.allowed.length) {
    wrap.innerHTML = '<span class="muted small">Список пуст</span>';
  } else {
    wrap.innerHTML = state.allowed.map(d =>
      '<span class="tag"><code class="mono">' + esc(d) + '</code>' +
      (editable
        ? '<button type="button" class="tag-x" data-rm-dir="' + esc(d) + '" aria-label="Удалить путь ' + esc(d) + '"><svg class="ic" aria-hidden="true"><use href="#i-x"></use></svg></button>'
        : '') +
      '</span>'
    ).join('');
  }
  $('#allowed-input').disabled = !editable;
  $('#allowed-add').disabled = !editable;
  $('#allowed-note').textContent = editable
    ? 'Перечисленные папки не будут запрашивать подтверждение. Редактируется только в режиме полного доступа.'
    : 'В режиме рабочей папки список задаётся выбором папки и не редактируется.';
}

function fillSettings(cfg) {
  state.config = cfg || {};
  const c = state.config;
  $('#set-maxfile').value = fmtMB(c.max_file_bytes);
  $('#set-maxout').value = c.max_output_chars ?? '';
  $('#set-tdef').value = c.default_shell_timeout ?? '';
  $('#set-tmax').value = c.max_shell_timeout ?? '';
  $('#deny-patterns').value = (c.deny_patterns || []).join('\n');
  $('#deny-paths').value = (c.deny_paths || []).join('\n');
  state.allowed = (c.allowed_dirs || []).slice();
  const tv = $('#token-val');
  tv.textContent = c.token_masked || '—';
  tv.dataset.full = '';
  $('#btn-token-show').textContent = 'Показать';
  $('#btn-token-copy').hidden = true;
  $('#btn-token-copy').dataset.copy = '';
  renderAllowedTags();
  state.snap = readSettings();
}

async function loadConfig() {
  try {
    const d = await api('GET', '/api/config');
    const active = document.activeElement;
    if (active && $('#card-settings').contains(active)) return; // не затирать правки пользователя
    fillSettings(d.config || {});
  } catch (e) {
    handleErr(e, 'Настройки');
  }
}

function addAllowed() {
  if ($('#allowed-input').disabled) return;
  const inp = $('#allowed-input');
  const v = inp.value.trim();
  if (!v) return;
  if (state.allowed.includes(v)) { toast('Такой путь уже есть в списке', 'warn'); return; }
  state.allowed.push(v);
  inp.value = '';
  renderAllowedTags();
}
$('#allowed-add').addEventListener('click', addAllowed);
$('#allowed-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); addAllowed(); }
});

$('#allowed-tags').addEventListener('click', e => {
  const x = e.target.closest('[data-rm-dir]');
  if (!x) return;
  state.allowed = state.allowed.filter(p => p !== x.getAttribute('data-rm-dir'));
  renderAllowedTags();
});

let saving = false;
$('#btn-save').addEventListener('click', async () => {
  if (saving) return;
  const cur = readSettings();
  const patch = {};
  for (const k of ['max_file_bytes', 'max_output_chars', 'default_shell_timeout', 'max_shell_timeout']) {
    if (!Number.isNaN(cur[k]) && cur[k] !== state.snap[k]) patch[k] = cur[k];
  }
  if (JSON.stringify(cur.deny_patterns) !== JSON.stringify(state.snap.deny_patterns)) patch.deny_patterns = cur.deny_patterns;
  if (JSON.stringify(cur.deny_paths) !== JSON.stringify(state.snap.deny_paths)) patch.deny_paths = cur.deny_paths;
  if (JSON.stringify(cur.allowed_dirs) !== JSON.stringify(state.snap.allowed_dirs)) patch.allowed_dirs = cur.allowed_dirs;
  if (!Object.keys(patch).length) { toast('Изменений нет', 'info'); return; }
  saving = true;
  $('#btn-save').disabled = true;
  try {
    const d = await api('POST', '/api/config', patch);
    if (d.ok === false) throw new Error(d.error || 'ошибка сохранения');
    state.snap = cur;
    toast('Настройки сохранены', 'ok');
    pollStatus();
    loadConfig();
  } catch (e) {
    handleErr(e, 'Не удалось сохранить настройки');
  } finally {
    saving = false;
    $('#btn-save').disabled = false;
  }
});

/* Токен */
$('#btn-token-show').addEventListener('click', () => {
  const full = state.config ? state.config.token_full : '';
  if (!full) { toast('Полный токен недоступен', 'warn'); return; }
  const tv = $('#token-val');
  const showBtn = $('#btn-token-show');
  const copyBtn = $('#btn-token-copy');
  if (tv.dataset.full === '1') {
    tv.textContent = state.config.token_masked || '—';
    tv.dataset.full = '';
    showBtn.textContent = 'Показать';
    copyBtn.hidden = true;
    copyBtn.dataset.copy = '';
  } else {
    tv.textContent = full;
    tv.dataset.full = '1';
    showBtn.textContent = 'Скрыть';
    copyBtn.hidden = false;
    copyBtn.dataset.copy = full;
  }
});

$('#btn-token-regen').addEventListener('click', async () => {
  try {
    const d = await api('POST', '/api/config', { regenerate_token: true });
    if (d.ok === false) throw new Error(d.error || 'ошибка генерации');
    $('#mt-token').textContent = d.token || '—';
    openModal($('#modal-token'), '#mt-close');
    loadConfig();
    pollStatus();
    loadConnections();
  } catch (e) {
    handleErr(e, 'Не удалось сгенерировать токен');
  }
});

$('#mt-close').addEventListener('click', () => closeModal($('#modal-token')));

/* ================= Секция: Зоны ================= */

async function loadZones() {
  try {
    const d = await api('GET', '/api/zones');
    state.zones = d.zones || [];
    renderZones();
  } catch (e) {
    handleErr(e, 'Не удалось загрузить зоны');
  }
}

function renderZones() {
  const b = $('#zones-body');
  const folderMode = !!(state.status && state.status.mode === 'workdir');
  $('#card-zones').classList.toggle('is-disabled', folderMode);
  if (folderMode) {
    b.innerHTML = '<p class="muted">В режиме рабочей папки зоны не используются</p>';
    return;
  }
  const z = state.zones;
  if (!z.length) {
    b.innerHTML = '<p class="muted">Пока все новые папки будут спрашивать подтверждение</p>';
    return;
  }
  b.innerHTML = z.map(it =>
    '<div class="zone-row">' +
    '<code class="mono z-path">' + esc(it.zone) + '</code>' +
    '<span class="muted small">' + esc(fmtDate(it.added_at)) + '</span>' +
    '<button type="button" class="btn btn-danger-ghost btn-sm" data-zone="' + esc(it.zone) + '">Отозвать</button>' +
    '</div>'
  ).join('');
}

$('#zones-body').addEventListener('click', async e => {
  const b = e.target.closest('[data-zone]');
  if (!b || b.disabled) return;
  const z = b.getAttribute('data-zone');
  const yes = await confirmDialog({
    title: 'Отозвать зону?',
    text: 'Путь «' + z + '» больше не будет подтверждаться автоматически: при следующем обращении к нему снова появится запрос.',
    okLabel: 'Отозвать',
    danger: true
  });
  if (!yes) return;
  try {
    await api('POST', '/api/zones/revoke', { zone: z });
    toast('Зона отозвана', 'ok');
    state.lastZonesKey = null;
    loadZones();
    pollStatus();
  } catch (err) {
    handleErr(err, 'Не удалось отозвать зону');
  }
});

/* ================= Секция: Аудит ================= */

async function refreshAudit() {
  try {
    const d = await api('GET', '/api/audit?tail=200');
    state.auditLines = d.lines || [];
    state.auditFile = !!d.file_exists;
    renderAudit();
  } catch (e) {
    handleErr(e, 'Аудит');
  }
}

function parseAudit(line) {
  let o = null;
  try { o = JSON.parse(line); } catch (e) { /* строка не JSON */ }
  if (!o || typeof o !== 'object' || Array.isArray(o)) {
    const raw = String(line);
    return { time: '—', tool: '', ok: true, note: raw, search: raw.toLowerCase() };
  }
  const ts = o.ts ?? o.time ?? o.at ?? o.timestamp;
  const d = toDate(ts);
  return {
    time: d ? fmtClockD(d) : (ts != null ? String(ts) : '—'),
    tool: String(o.tool ?? o.name ?? ''),
    ok: o.ok !== false,
    note: String(o.note ?? o.message ?? o.error ?? o.path ?? o.detail ?? ''),
    search: String(line).toLowerCase()
  };
}

function renderAudit() {
  const box = $('#audit-list');
  const f = ($('#audit-filter').value || '').trim().toLowerCase();
  let rows = state.auditLines.map(parseAudit);
  if (f) rows = rows.filter(r => r.search.includes(f) || r.tool.toLowerCase().includes(f));
  if (!rows.length) {
    box.innerHTML = '<p class="muted">' + (state.auditFile ? 'Ничего не найдено по фильтру' : 'Журнал пуст') + '</p>';
    return;
  }
  box.innerHTML = rows.map(r =>
    '<div class="audit-row' + (r.ok ? '' : ' is-err') + '">' +
    '<span class="a-time">' + esc(r.time) + '</span>' +
    (r.tool ? '<span class="chip">' + esc(r.tool) + '</span>' : '<span></span>') +
    '<span class="' + (r.ok ? 'st-ok' : 'st-err') + '">' + (r.ok ? 'ok' : 'err') + '</span>' +
    '<span class="a-note">' + esc(r.note) + '</span>' +
    '</div>'
  ).join('');
}

$('#audit-filter').addEventListener('input', renderAudit);

/* ================= Секция: Логи сервера ================= */

async function refreshLogs() {
  try {
    const d = await api('GET', '/api/logs?tail=200');
    const l = d.lines || [];
    $('#logs-pre').textContent = l.length ? l.join('\n') : 'Журнал пуст';
  } catch (e) {
    handleErr(e, 'Логи');
  }
}

/* ================= Автообновление (тумблеры) ================= */

function bindAuto(inputId, fn) {
  const el = $(inputId);
  el.addEventListener('change', () => {
    if (el.checked) {
      fn();
      state.timers[el.id] = setInterval(fn, 2000);
    } else {
      clearInterval(state.timers[el.id]);
      delete state.timers[el.id];
    }
  });
}
bindAuto('#audit-auto', refreshAudit);
bindAuto('#logs-auto', refreshLogs);

/* ================= Секция: Подключение ================= */

async function loadConnections() {
  try {
    state.conn = await api('GET', '/api/connections');
    renderConnectPanel();
  } catch (e) {
    handleErr(e, 'Подключение');
  }
}

function fieldRow(label, value, copyVal) {
  if (!value) return '';
  return '<div class="field-row"><span class="fr-l">' + label + '</span>' +
    '<code class="mono fr-v">' + esc(value) + '</code>' +
    '<button type="button" class="icon-btn" data-copy="' + esc(copyVal ?? value) + '" aria-label="Скопировать: ' + label + '" title="Скопировать"><svg class="ic" aria-hidden="true"><use href="#i-copy"></use></svg></button></div>';
}

function mcpSnippet(url, token) {
  return JSON.stringify({
    mcpServers: {
      glassmcp: {
        type: 'http',
        url: url,
        headers: { Authorization: 'Bearer ' + token }
      }
    }
  }, null, 2);
}

function renderConnectPanel() {
  const s = state.status || {};
  const tun = !!(s.tunnel && s.tunnel.running);
  const pub = !!(s.public_url || '');
  $('#conn-warn').hidden = tun || pub;
  const panel = $('#conn-panel');
  if (!state.conn) {
    panel.innerHTML = '<p class="muted">Загрузка данных подключения…</p>';
    return;
  }
  const c = state.conn;
  // url_external = свой адрес (приоритет) или URL туннеля; если их нет — локальный
  const base = c.url_external || c.url_local || '';
  const u = ensureMcp(base);
  const tok = c.token || '';
  const tokMask = c.token_masked || '••••';
  const tokRow = tok ? fieldRow('Токен', tokMask, tok) : '<p class="muted small">Токен не задан</p>';
  const urlRow = fieldRow('URL', u, u);
  const coStr = u ? (u + '?token=' + tok) : '';
  const source = c.external_source || (pub ? 'custom' : (tun ? 'tunnel' : ''));
  const snippet = u && tok ? mcpSnippet(u, tok) : '';
  const snipBlock = (title) => !snippet ? '' :
    '<div class="snip-head"><span class="fr-l">' + title + '</span>' +
    '<button type="button" class="btn btn-ghost btn-sm" data-copy="' + esc(snippet) + '">Скопировать JSON</button></div>' +
    '<pre class="snip mono">' + esc(snippet) + '</pre>';

  let html = '';
  switch (state.activeTab) {
    case 'general':
      html =
        '<ol class="steps">' +
        '<li>Скопируй адрес сервера ниже.' + (source === 'custom'
          ? ' Используется твой постоянный адрес из секции «Свой адрес».'
          : source === 'tunnel'
            ? ' Используется туннельный URL — доступен и извне.'
            : ' Туннель и свой адрес не заданы: доступен только локальный адрес, работает с этого ПК.') + '</li>' +
        '<li>Убедись, что в конце адреса есть <code>/mcp</code> — при необходимости добавь.</li>' +
        '<li>Тип авторизации — <code>Bearer</code>.</li>' +
        '<li>Вставь токен в заголовок <code>Authorization</code>.</li>' +
        '</ol>' + urlRow + tokRow;
      break;
    case 'chatco':
      html =
        '<ol class="steps">' +
        '<li>Открой настройки чата и найди раздел подключения MCP.</li>' +
        '<li>Если есть только одно поле URL — используй готовую строку ниже: адрес сразу с токеном.</li>' +
        '<li>Если поддерживаются заголовки — бери URL и токен раздельно, авторизация <code>Bearer</code>.</li>' +
        '</ol>' +
        fieldRow('URL с токеном', coStr, coStr) +
        urlRow + tokRow +
        '<div class="alert alert-warn">' + (source === 'custom'
          ? 'Постоянный адрес не меняется при перезапуске туннеля.'
          : source === 'tunnel'
            ? 'URL меняется при перезапуске туннеля.'
            : 'Для доступа с других устройств подними туннель или задай свой адрес в секции «Свой адрес».') + '</div>';
      break;
    case 'claude':
      html =
        '<ol class="steps">' +
        '<li>Открой <code>Settings → Connectors → Add custom connector</code>.</li>' +
        '<li>Вставь адрес сервера — URL с <code>/mcp</code> на конце.</li>' +
        '<li>Авторизация — <code>Bearer</code>, токен ниже.</li>' +
        '</ol>' + urlRow + tokRow;
      break;
    case 'chatgpt':
      html =
        '<ol class="steps">' +
        '<li>Открой <code>Settings → Connectors → Create</code>.</li>' +
        '<li>Укажи URL сервера — поле ниже.</li>' +
        '<li>Токен передаётся через advanced-заголовки: <code>Authorization: Bearer &lt;токен&gt;</code>.</li>' +
        '<li>Для доступа извне нужен туннель или свой адрес (секции выше).</li>' +
        '</ol>' + urlRow + tokRow;
      break;
    case 'zcode':
      html =
        '<ol class="steps">' +
        '<li>Открой <code>Settings → MCP</code> и добавь новый сервер.</li>' +
        '<li>Тип — <code>http</code>, URL — поле ниже.</li>' +
        '<li>Добавь заголовок <code>Authorization</code> со значением <code>Bearer &lt;токен&gt;</code>.</li>' +
        '</ol>' + urlRow + tokRow + snipBlock('Готовый JSON');
      break;
    case 'cursor':
      html =
        '<ol class="steps">' +
        '<li>Подойдёт любой клиент с поддержкой Streamable HTTP MCP (Cursor, Cline и другие).</li>' +
        '<li>Укажи URL сервера — поле ниже.</li>' +
        '<li>Добавь заголовок <code>Authorization: Bearer &lt;токен&gt;</code>.</li>' +
        '</ol>' + urlRow + tokRow + snipBlock('Пример JSON');
      break;
    default:
      html = '<p class="muted">Нет данных</p>';
  }
  panel.innerHTML = html;
}

function selectTab(name) {
  if (!TAB_IDS.includes(name)) return;
  state.activeTab = name;
  $$('#conn-tabs .tab').forEach(t => {
    const on = t.getAttribute('data-tab') === name;
    t.setAttribute('aria-selected', on ? 'true' : 'false');
    t.tabIndex = on ? 0 : -1;
  });
  $('#conn-panel').setAttribute('aria-labelledby', 'tab-' + name);
  renderConnectPanel();
  moveUnderline();
}

$('#conn-tabs').addEventListener('click', e => {
  const t = e.target.closest('.tab');
  if (t) selectTab(t.getAttribute('data-tab'));
});

$('#conn-tabs').addEventListener('keydown', e => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
  const cur = TAB_IDS.indexOf(state.activeTab);
  let next = cur;
  if (e.key === 'ArrowLeft') next = (cur - 1 + TAB_IDS.length) % TAB_IDS.length;
  else if (e.key === 'ArrowRight') next = (cur + 1) % TAB_IDS.length;
  else if (e.key === 'Home') next = 0;
  else next = TAB_IDS.length - 1;
  e.preventDefault();
  selectTab(TAB_IDS[next]);
  $('#tab-' + TAB_IDS[next]).focus();
});

function moveUnderline() {
  const u = $('#tab-underline');
  const t = $('#conn-tabs .tab[aria-selected="true"]');
  if (!t) { u.style.width = '0px'; return; }
  u.style.left = t.offsetLeft + 'px';
  u.style.width = t.offsetWidth + 'px';
}
window.addEventListener('resize', moveUnderline);

/* ================= Запуск ================= */

initCanvas();
pollStatus();
loadConfig();
loadConnections();
loadZones();
refreshAudit();
refreshLogs();
requestAnimationFrame(moveUnderline);

})();
