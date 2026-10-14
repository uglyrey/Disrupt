'use strict';
// Time zone of the account. Loaded before reminders.js; app.js calls start() after every sign-in.
// 1. The zone comes from /api/v1/auth/me. When it is empty, the zone of the device clock is sent once, quietly.
// 2. When the device zone has another UTC offset than the saved one, a one-line banner offers to change it.
// 3. The «Часовой пояс» dialog lets a person pick any zone from /api/v1/timezones.
// Other scripts use window.BerestaTimezone and the "beresta:timezone" event on document.
(() => {
  const get = id => document.getElementById(id);
  const DISMISS_KEY = 'beresta.timezoneDismissed';
  // Used only while /api/v1/timezones is not loaded or failed.
  const FALLBACK = [['Europe/Kaliningrad', 'Калининград'], ['Europe/Moscow', 'Москва'], ['Europe/Samara', 'Самара'],
    ['Asia/Yekaterinburg', 'Екатеринбург'], ['Asia/Omsk', 'Омск'], ['Asia/Novosibirsk', 'Новосибирск'],
    ['Asia/Krasnoyarsk', 'Красноярск'], ['Asia/Irkutsk', 'Иркутск'], ['Asia/Yakutsk', 'Якутск'],
    ['Asia/Vladivostok', 'Владивосток'], ['Asia/Magadan', 'Магадан'], ['Asia/Kamchatka', 'Камчатка'],
    ['Europe/Minsk', 'Минск'], ['Europe/Kyiv', 'Киев'], ['Asia/Almaty', 'Алматы'], ['Asia/Tashkent', 'Ташкент'],
    ['Asia/Tbilisi', 'Тбилиси'], ['Asia/Yerevan', 'Ереван'], ['Asia/Baku', 'Баку'], ['Europe/Berlin', 'Берлин'],
    ['Europe/London', 'Лондон'], ['America/New_York', 'Нью-Йорк'], ['UTC', 'UTC']];
  const fallbackLabels = new Map(FALLBACK);

  let api = null, account = {timezone: null, source: null}, supported = false;
  let rows = null, byId = new Map(), loading = null;
  let session = 0, autoSent = false, choice = null, saving = false, openToken = 0, shown = '';

  /* ---------- Zones and offsets ---------- */
  function validZone(zone) {
    if (typeof zone !== 'string' || !zone) return false;
    try { new Intl.DateTimeFormat('en-CA', {timeZone: zone}); return true; } catch (_) { return false; }
  }
  function browserZone() {
    try {
      const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      return validZone(zone) ? zone : '';
    } catch (_) { return ''; }
  }
  // UTC offset of a zone right now, in minutes. null for a zone this browser does not know.
  function offsetMinutes(zone, at = Date.now()) {
    if (!validZone(zone)) return null;
    try {
      const parts = new Intl.DateTimeFormat('en-CA', {timeZone: zone, hourCycle: 'h23', year: 'numeric', month: '2-digit',
        day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'}).formatToParts(new Date(at));
      const part = type => Number(parts.find(item => item.type === type).value);
      const wall = Date.UTC(part('year'), part('month') - 1, part('day'), part('hour'), part('minute'), part('second'));
      return Math.round((wall - Math.floor(at / 1000) * 1000) / 60000);
    } catch (_) { return null; }
  }
  function utc(minutes) {
    if (typeof minutes !== 'number' || Number.isNaN(minutes)) return '';
    if (minutes === 0) return 'UTC';
    const abs = Math.abs(minutes), hours = Math.floor(abs / 60), rest = abs % 60;
    return `UTC${minutes < 0 ? '-' : '+'}${hours}${rest ? ':' + String(rest).padStart(2, '0') : ''}`;
  }
  function cityName(zone) { return String(zone).split('/').pop().replace(/_/g, ' '); }
  function fallbackRows() {
    return FALLBACK.map(([id, label]) => ({id, label, offset_minutes: offsetMinutes(id)})).filter(row => row.offset_minutes !== null)
      .sort((a, b) => a.offset_minutes - b.offset_minutes || a.label.localeCompare(b.label, 'ru'));
  }
  // The label of a known zone, or '' when nothing is known about it.
  function label(zone) {
    const row = byId.get(zone);
    return row ? row.label : (fallbackLabels.get(zone) || '');
  }
  function describe(zone) {
    const minutes = byId.has(zone) ? byId.get(zone).offset_minutes : offsetMinutes(zone);
    return `${label(zone) || cityName(zone)}${minutes === null ? '' : ` (${utc(minutes)})`}`;
  }
  function announce() { document.dispatchEvent(new CustomEvent('beresta:timezone')); }
  // One cached request for the whole page. A failed request gives the short built-in list and is tried again later.
  function zones() {
    if (rows) return Promise.resolve(rows);
    if (!loading) {
      loading = Promise.resolve().then(() => api('/api/v1/timezones')).then(data => {
        const list = Array.isArray(data && data.zones) ? data.zones.filter(row => row && typeof row.id === 'string' &&
          typeof row.label === 'string' && typeof row.offset_minutes === 'number') : [];
        if (!list.length) throw new Error('empty list');
        rows = list; byId = new Map(list.map(row => [row.id, row])); announce();
        return rows;
      }).catch(() => fallbackRows()).finally(() => { loading = null; });
    }
    return loading;
  }
  function knownZones() { return rows || fallbackRows(); }

  /* ---------- Dismissal of the banner ---------- */
  function readDismissed() {
    let value = '';
    try { value = (window.berestaConsent && window.berestaConsent.read(DISMISS_KEY)) || ''; } catch (_) { value = ''; }
    if (!value) { try { value = window.sessionStorage.getItem(DISMISS_KEY) || ''; } catch (_) { value = ''; } }
    return value;
  }
  function writeDismissed(zone) {
    try { if (window.berestaConsent) window.berestaConsent.write(DISMISS_KEY, zone); } catch (_) { /* storage may be blocked */ }
    try { window.sessionStorage.setItem(DISMISS_KEY, zone); } catch (_) { /* kept by the consent helper, if at all */ }
  }

  /* ---------- Writing the zone ---------- */
  async function write(zone, source) {
    const data = await api('/api/v1/account/timezone', {method: 'PATCH', body: JSON.stringify({timezone: zone, source})});
    return data && typeof data.timezone === 'string' ? data : {timezone: zone, source};
  }
  function failure(error) {
    return error && error.status && typeof error.message === 'string' && error.message
      ? error.message : 'Не удалось сохранить. Проверьте соединение и попробуйте ещё раз.';
  }
  function setAccount(data) {
    account = {timezone: data && data.timezone ? data.timezone : null, source: data && data.source ? data.source : null};
    announce(); evaluate(); drawCurrent();
  }
  async function autoDetect() {
    if (autoSent) return;
    autoSent = true;
    const zone = browserZone(), mine = session;
    if (!zone) return;
    try {
      const data = await write(zone, 'auto');
      if (mine === session) setAccount(data);
    } catch (_) { /* silent: the server falls back to Moscow */ }
  }

  /* ---------- Banner ---------- */
  function hideBanner() {
    const banner = get('timezone-banner');
    if (banner) banner.hidden = true;
    shown = '';
  }
  async function evaluate() {
    const banner = get('timezone-banner');
    if (!banner) return;
    const saved = account.timezone, device = browserZone();
    if (!saved || !device || saved === device || readDismissed() === device) return hideBanner();
    // Names are not compared, only offsets, so Europe/Kiev and Europe/Kyiv do not nag.
    const now = Date.now(), a = offsetMinutes(saved, now), b = offsetMinutes(device, now);
    if (a === null || b === null || a === b) return hideBanner();
    const mine = session;
    await zones();
    if (mine !== session || account.timezone !== saved) return;
    get('timezone-banner-text').textContent = `Похоже, вы сейчас в другом часовом поясе: ${describe(device)}. Сменить?`;
    banner.hidden = false; shown = device;
    get('timezone-banner-change').disabled = false;
  }
  async function changeFromBanner() {
    const device = shown || browserZone(), button = get('timezone-banner-change');
    if (!device || button.disabled) return;
    button.disabled = true;
    const mine = session;
    try {
      const data = await write(device, 'manual');
      if (mine !== session) return;
      hideBanner(); setAccount(data);
    } catch (error) {
      if (mine === session) get('timezone-banner-text').textContent = failure(error);
    } finally { button.disabled = false; }
  }
  function keepFromBanner() {
    if (shown) writeDismissed(shown);
    hideBanner();
  }

  /* ---------- Dialog ---------- */
  const fold = text => String(text).toLowerCase().replace(/ё/g, 'е');
  // «+7», «UTC+7», «+07:00», «-3:30» find zones with exactly this offset.
  function offsetQuery(query) {
    const match = /^(?:utc|gmt)?\s*([+\-−])\s*(\d{1,2})(?::?(\d{2}))?$/.exec(query);
    if (!match) return null;
    const minutes = Number(match[2]) * 60 + Number(match[3] || 0);
    return match[1] === '+' ? minutes : -minutes;
  }
  function matches(row, query) {
    if (!query) return true;
    const exact = offsetQuery(query);
    if (exact !== null) return row.offset_minutes === exact;
    const text = fold(`${row.label} ${row.id} ${row.id.replace(/_/g, ' ')} ${utc(row.offset_minutes)}`);
    return query.split(/\s+/).every(part => text.includes(part));
  }
  function visibleItems() { return [...get('timezone-list').querySelectorAll('.timezone-item')]; }
  function drawCurrent() {
    const line = get('timezone-current');
    if (!line) return;
    line.textContent = account.timezone ? `Сейчас: ${describe(account.timezone)}`
      : `Пока не задан. Напоминания и ИИ считают по Москве (${utc(offsetMinutes('Europe/Moscow'))}).`;
    controls();
  }
  function controls() {
    const save = get('timezone-save');
    if (!save) return;
    save.disabled = saving || !choice || choice === account.timezone;
    get('timezone-auto').disabled = saving;
  }
  function pick(zone) {
    choice = zone;
    for (const item of visibleItems()) {
      const on = item.dataset.zone === zone;
      item.setAttribute('aria-pressed', String(on));
    }
    controls();
  }
  function drawList() {
    const box = get('timezone-list'), query = fold(get('timezone-search').value.trim());
    const found = (rows || (loading ? [] : fallbackRows())).filter(row => matches(row, query));
    const fragment = document.createDocumentFragment();
    const focusable = found.find(row => row.id === choice) || found.find(row => row.id === account.timezone) || found[0];
    for (const row of found) {
      const item = document.createElement('button');
      item.type = 'button'; item.className = 'timezone-item'; item.dataset.zone = row.id;
      item.setAttribute('aria-pressed', String(row.id === choice));
      if (row.id === account.timezone) item.setAttribute('aria-current', 'true');
      item.tabIndex = row === focusable ? 0 : -1;
      for (const [className, text] of [['timezone-name', row.label], ['timezone-id', row.id], ['timezone-offset', utc(row.offset_minutes)]]) {
        const part = document.createElement('span'); part.className = className; part.textContent = text; item.append(part);
      }
      fragment.append(item);
    }
    box.replaceChildren(fragment);
    get('timezone-empty').textContent = !rows && loading ? 'Загружаем список…' : 'Ничего не найдено. Попробуйте город, пояс или смещение, например +7.';
    get('timezone-empty').hidden = found.length > 0;
    controls();
  }
  function moveFocus(from, step) {
    const items = visibleItems();
    if (!items.length) return;
    const index = from ? items.indexOf(from) : -1;
    const next = step === 'first' ? items[0] : step === 'last' ? items.at(-1)
      : items[Math.max(0, Math.min(items.length - 1, index + step))];
    for (const item of items) item.tabIndex = item === next ? 0 : -1;
    next.focus();
    if (next.scrollIntoView) next.scrollIntoView({block: 'nearest'});
  }
  function say(text = '') { get('timezone-status').textContent = text; }
  async function saveChoice(zone, done) {
    if (saving) return;
    saving = true; say('Сохраняем…'); controls();
    const mine = session;
    try {
      const data = await write(zone, 'manual');
      if (mine !== session) return;
      choice = null; hideBanner(); setAccount(data); drawList();
      say(done(data.timezone));
    } catch (error) {
      if (mine === session) say(failure(error));
    } finally { saving = false; controls(); }
  }
  async function openDialog(event) {
    const dialog = get('timezone-dialog');
    if (!dialog || dialog.open) return;
    choice = null; say(''); get('timezone-search').value = '';
    dialog._opener = (event && event.currentTarget) || document.activeElement;
    dialog.showModal();
    const token = ++openToken, pending = zones();
    drawCurrent(); drawList();
    await pending;
    if (token !== openToken || !dialog.open) return;
    drawList(); drawCurrent();
    const current = get('timezone-list').querySelector('[aria-current="true"]');
    // Only the list scrolls, not the dialog behind it.
    if (current) get('timezone-list').scrollTop = Math.max(0, current.offsetTop - get('timezone-list').clientHeight / 2);
  }
  function bindDialog() {
    const opener = get('open-timezone');
    if (opener) opener.onclick = openDialog;
    if (!get('timezone-dialog')) return;
    get('timezone-dialog').addEventListener('close', () => { openToken++; });
    get('timezone-search').addEventListener('input', () => { drawList(); get('timezone-list').scrollTop = 0; });
    get('timezone-search').addEventListener('keydown', event => {
      if (event.key === 'ArrowDown') { event.preventDefault(); const mark = get('timezone-list').querySelector('[tabindex="0"]'); moveFocus(mark, 0); }
      if (event.key === 'Enter') { event.preventDefault(); confirmChoice(); }
    });
    get('timezone-list').addEventListener('click', event => {
      const item = event.target.closest('.timezone-item');
      if (item) { pick(item.dataset.zone); say(''); }
    });
    get('timezone-list').addEventListener('keydown', event => {
      const item = event.target.closest('.timezone-item');
      if (!item) return;
      const steps = {ArrowDown: 1, ArrowUp: -1, PageDown: 8, PageUp: -8, Home: 'first', End: 'last'};
      if (!(event.key in steps)) return;
      event.preventDefault(); moveFocus(item, steps[event.key]);
    });
    // Enter in the search field picks the first match, a second Enter saves. (A disabled Save button blocks the browser's own Enter.)
    function confirmChoice() {
      if (!choice) {
        const first = visibleItems()[0];
        if (first && get('timezone-search').value.trim()) { pick(first.dataset.zone); say(`Выбрано: ${describe(first.dataset.zone)}. Нажмите «Сохранить».`); }
        return;
      }
      if (choice !== account.timezone) saveChoice(choice, zone => `Готово. Теперь часовой пояс: ${describe(zone)}.`);
    }
    get('timezone-form').addEventListener('submit', event => { event.preventDefault(); confirmChoice(); });
    get('timezone-auto').addEventListener('click', () => {
      const zone = browserZone();
      if (!zone) return say('Не удалось определить пояс по устройству. Выберите его из списка.');
      saveChoice(zone, saved => `Определили по устройству: ${describe(saved)}.`);
    });
  }

  /* ---------- Entry points ---------- */
  function reset() {
    session++; account = {timezone: null, source: null}; supported = false; autoSent = false; choice = null; saving = false;
    hideBanner();
    const dialog = get('timezone-dialog');
    if (dialog && dialog.open) dialog.close();
  }
  // `user` is the answer of /api/v1/auth/me, `request` is the app's api() with the CSRF header.
  function start(user, request) {
    reset(); api = request;
    // A server that does not send the field yet is left alone.
    supported = Boolean(user) && Object.prototype.hasOwnProperty.call(user, 'timezone');
    if (!supported) return;
    account = {timezone: user.timezone || null, source: null};
    announce(); drawCurrent();
    if (account.timezone) evaluate(); else autoDetect();
  }

  if (get('timezone-banner')) {
    get('timezone-banner-change').addEventListener('click', changeFromBanner);
    get('timezone-banner-keep').addEventListener('click', keepFromBanner);
  }
  bindDialog();
  if (get('workspace') && typeof MutationObserver === 'function') {
    new MutationObserver(() => { if (get('workspace').hidden) reset(); }).observe(get('workspace'), {attributes: true, attributeFilter: ['hidden']});
  }
  window.BerestaTimezone = {
    start, zones, knownZones, label, describe, utc, validZone, browserZone, offsetMinutes,
    accountZone: () => account.timezone || '',
  };
})();
