'use strict';
// Time zone of the account on the web: quiet detection, the travel banner, the settings dialog and the default zone
// of the reminder form. The server is a mock that follows the shared contract (GET /api/v1/timezones,
// PATCH /api/v1/account/timezone, "timezone" in /api/v1/auth/me). No layout in jsdom: attributes, text and calls.
const assert = require('node:assert/strict'), fs = require('fs'), path = require('path'), {JSDOM} = require('jsdom');
const root = path.join(__dirname, '../app/static');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const reply = (data, status = 200) => ({ok: status < 400, status, headers: new Headers(), json: async () => structuredClone(data)});
async function settled() { for (let i = 0; i < 30; i++) await new Promise(resolve => setImmediate(resolve)); }
async function closeWindow(t) {
  for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve));
  t.dom.window.close();
}

const ZONES = [['Europe/Moscow', 'Москва', 180], ['Europe/Minsk', 'Минск', 180], ['Asia/Kolkata', 'Калькутта', 330],
  ['Asia/Omsk', 'Омск', 360], ['Asia/Novosibirsk', 'Новосибирск', 420], ['Asia/Bangkok', 'Бангкок', 420], ['UTC', 'UTC', 0]]
  .sort((a, b) => a[2] - b[2]).map(([id, label, offset_minutes]) => ({id, label, offset_minutes,
    offset: `${offset_minutes < 0 ? '-' : '+'}${String(Math.floor(Math.abs(offset_minutes) / 60)).padStart(2, '0')}:${String(Math.abs(offset_minutes) % 60).padStart(2, '0')}`}));

// `me` is the saved zone in /auth/me (null = nothing saved, 'absent' = the server does not send the field).
// `browser` is what the device clock says. `zonesFail` and `patchFails` break the two timezone routes.
function boot({me = 'Europe/Moscow', browser = 'Asia/Novosibirsk', zonesFail = false, patchFails = false, reminders = false} = {}) {
  const dom = new JSDOM(read('index.html'), {url: 'https://beresta.invalid/', runScripts: 'outside-only'}), w = dom.window;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  try { Object.defineProperty(w.crypto, 'randomUUID', {value: require('node:crypto').randomUUID, configurable: true}); } catch { /* already provided */ }
  w.confirm = () => true;
  const env = {browser};
  // The device zone: only a call without an explicit zone is changed, like Intl.DateTimeFormat().resolvedOptions().
  const Real = w.Intl.DateTimeFormat;
  function Fake(locales, options) {
    const format = new Real(locales, options);
    if (!options || !options.timeZone) {
      const resolved = format.resolvedOptions.bind(format);
      format.resolvedOptions = () => ({...resolved(), timeZone: env.browser});
    }
    return format;
  }
  Fake.prototype = Real.prototype; Fake.supportedLocalesOf = Real.supportedLocalesOf;
  w.Intl.DateTimeFormat = Fake;
  const state = {calls: [], patches: [], account: {timezone: me === 'absent' ? null : me, source: me && me !== 'absent' ? 'manual' : null},
    zonesCalls: 0, zonesFail, patchFails};
  const note = {id: 'n1', capture_id: 'k1', channel: 'web', input_kind: 'text', title: 'Заметка', markdown: 'Текст',
    original_text: 'Исходник', conclusions: [], version: 1, provider: 'manual', category_id: null,
    category_name: null, structure_confirmed_at: null, created_at: 1791220000, updated_at: 1791220000, items: []};
  w.fetch = async (input, options = {}) => {
    const url = new URL(input, w.location.href), method = options.method || 'GET';
    const body = options.body && typeof options.body === 'string' ? JSON.parse(options.body) : null;
    state.calls.push({url, method, body, options});
    const path_ = url.pathname;
    if (path_ === '/health' || path_ === '/api/v1/provider/usage') return reply({simulation: true});
    if (path_ === '/api/v1/auth/me') {
      const user = {username: 'tester', csrf_token: 'csrf'};
      if (me !== 'absent') user.timezone = state.account.timezone;
      return reply(user);
    }
    if (path_ === '/api/v1/timezones') {
      state.zonesCalls++;
      return state.zonesFail ? reply({detail: 'Не удалось выполнить запрос'}, 500) : reply({zones: ZONES});
    }
    if (path_ === '/api/v1/account/timezone' && method === 'PATCH') {
      state.patches.push({body, csrf: options.headers && options.headers['X-CSRF-Token']});
      if (state.patchFails) return reply({detail: 'Не удалось выполнить запрос'}, 500);
      const known = ZONES.find(row => row.id === body.timezone);
      if (!known && !/^[A-Za-z_]+\/[A-Za-z_]+$/.test(body.timezone) && body.timezone !== 'UTC') return reply({detail: 'Неизвестный пояс'}, 422);
      if (!(body.source === 'auto' && state.account.timezone)) state.account = {timezone: body.timezone, source: body.source};
      return reply({...state.account, label: known ? known.label : null});
    }
    if (path_ === '/api/v1/categories' || path_ === '/api/v1/jobs') return reply([]);
    if (path_ === '/api/v1/notes') return reply([{id: 'n1', title: 'Заметка', version: 1, updated_at: 1791220000, category_id: null, channel: 'web', input_kind: 'text'}]);
    if (path_ === '/api/v1/notes/n1') return reply(note);
    if (path_ === '/api/v1/notes/n1/reminders') return reply([]);
    if (path_ === '/api/v1/telegram/links') return reply({identities: [], pending: []});
    if (path_.endsWith('/opened') || path_.endsWith('/original-opened') || path_ === '/api/v1/search/events') return reply(null, 204);
    return reply({detail: 'Не найдено'}, 404);
  };
  const $ = id => w.document.getElementById(id);
  // app.js and workspace.js share top-level names (currentNote), so they go through one eval, as in the other tests.
  // app.js is strict, so its api() is handed out on window.
  for (const name of ['cookie-consent.js', 'timezone.js', ...(reminders ? ['reminders.js'] : [])]) w.eval(read(name));
  w.eval(read('app.js') + '\n' + read('workspace.js') + '\nwindow.__api = api;');
  const wait = ms => new Promise(resolve => w.setTimeout(resolve, ms));
  const until = async (check, what) => { for (let i = 0; i < 300; i++) { if (check()) return; await wait(5); } throw new Error(`Timed out: ${what}`); };
  // A new sign-in with a given answer of /auth/me, as app.js does it.
  const signIn = zone => { w.BerestaTimezone.start({username: 't', csrf_token: 'csrf', timezone: zone}, w.__api); };
  return {dom, w, $, env, state, wait, until, signIn};
}
const banner = t => !t.$('timezone-banner').hidden;
const bannerText = t => t.$('timezone-banner-text').textContent;
const items = t => [...t.$('timezone-list').querySelectorAll('.timezone-item')];
const ids = t => items(t).map(item => item.dataset.zone);
function type(t, text) { t.$('timezone-search').value = text; t.$('timezone-search').dispatchEvent(new t.w.Event('input', {bubbles: true})); }
function key(t, target, init) {
  const event = new t.w.KeyboardEvent('keydown', {bubbles: true, cancelable: true, ...init});
  target.dispatchEvent(event); return event;
}
const submit = t => t.$('timezone-form').dispatchEvent(new t.w.Event('submit', {bubbles: true, cancelable: true}));

/* ---------- Detection ---------- */
async function detection() {
  // Nothing saved: the device zone is sent once, with the CSRF header, quietly.
  let t = boot({me: null, browser: 'Asia/Omsk'});
  try {
    await settled();
    assert.equal(t.state.patches.length, 1);
    assert.deepEqual(t.state.patches[0].body, {timezone: 'Asia/Omsk', source: 'auto'});
    assert.equal(t.state.patches[0].csrf, 'csrf');
    assert.equal(t.w.BerestaTimezone.accountZone(), 'Asia/Omsk');
    assert.equal(banner(t), false, 'no banner after a quiet detection');
    assert.equal(t.$('message').textContent, '', 'no message either');
    // Later events, the dialog and the banner check do not send it again.
    t.w.document.dispatchEvent(new t.w.CustomEvent('beresta:timezone'));
    t.$('open-timezone').click(); await settled(); t.$('timezone-dialog').close(); await settled();
    assert.equal(t.state.patches.length, 1, 'only once');
  } finally { await closeWindow(t); }
  // A saved zone is never overwritten by detection.
  t = boot({me: 'Europe/Moscow', browser: 'Asia/Omsk'});
  try { await settled(); assert.equal(t.state.patches.length, 0); } finally { await closeWindow(t); }
  // A browser without a valid zone: nothing is sent, the account stays empty.
  for (const bad of ['', 'Mars/Base', 'not a zone']) {
    t = boot({me: null, browser: bad});
    try {
      await settled();
      assert.equal(t.state.patches.length, 0, `no PATCH for "${bad}"`);
      assert.equal(t.w.BerestaTimezone.accountZone(), '');
      assert.equal(banner(t), false);
    } finally { await closeWindow(t); }
  }
  // A failed request is silent and is not repeated.
  t = boot({me: null, browser: 'Asia/Omsk', patchFails: true});
  try {
    await settled();
    assert.equal(t.state.patches.length, 1); assert.equal(banner(t), false); assert.equal(t.$('message').textContent, '');
    t.w.document.dispatchEvent(new t.w.CustomEvent('beresta:timezone')); await settled();
    assert.equal(t.state.patches.length, 1);
  } finally { await closeWindow(t); }
  // A server that does not send the field yet is left alone.
  t = boot({me: 'absent', browser: 'Asia/Omsk'});
  try { await settled(); assert.equal(t.state.patches.length, 0); assert.equal(banner(t), false); } finally { await closeWindow(t); }
}

/* ---------- Travel banner ---------- */
async function travelBanner() {
  let t = boot({me: 'Europe/Moscow', browser: 'Asia/Novosibirsk'});
  try {
    await settled();
    assert.equal(banner(t), true);
    assert.equal(bannerText(t), 'Похоже, вы сейчас в другом часовом поясе: Новосибирск (UTC+7). Сменить?');
    assert.equal(t.state.patches.length, 0, 'the banner asks, it does not change anything');
    assert.deepEqual([...t.$('timezone-banner').querySelectorAll('button')].map(b => b.textContent), ['Сменить', 'Оставить']);
    assert.equal(t.$('timezone-banner').querySelector('dialog'), null, 'not a modal');
    t.$('timezone-banner-change').click(); await settled();
    assert.equal(t.state.patches.length, 1);
    assert.deepEqual(t.state.patches[0].body, {timezone: 'Asia/Novosibirsk', source: 'manual'});
    assert.equal(banner(t), false); assert.equal(t.w.BerestaTimezone.accountZone(), 'Asia/Novosibirsk');
  } finally { await closeWindow(t); }
  // Names of one offset do not nag: aliases and different cities on the same offset.
  for (const [saved, device] of [['Europe/Kyiv', 'Europe/Kiev'], ['Asia/Kolkata', 'Asia/Calcutta'], ['Europe/Moscow', 'Europe/Minsk']]) {
    t = boot({me: saved, browser: device});
    try { await settled(); assert.equal(banner(t), false, `${saved} / ${device}`); assert.equal(t.state.patches.length, 0); } finally { await closeWindow(t); }
  }
  // «Оставить» closes it and remembers the answer for this device zone.
  t = boot({me: 'Europe/Moscow', browser: 'Asia/Novosibirsk'});
  try {
    await settled();
    t.$('timezone-banner-keep').click();
    assert.equal(banner(t), false); assert.equal(t.state.patches.length, 0);
    assert.equal(t.w.sessionStorage.getItem('beresta.timezoneDismissed'), 'Asia/Novosibirsk');
    t.signIn('Europe/Moscow'); await settled();
    assert.equal(banner(t), false, 'does not come back on the next load');
    t.env.browser = 'Asia/Omsk'; t.signIn('Europe/Moscow'); await settled();
    assert.equal(banner(t), true, 'another device zone asks again');
    assert.match(bannerText(t), /Омск \(UTC\+6\)/);
    // The answer is also kept through the consent-aware storage helper.
    assert.equal(t.w.berestaConsent.read('beresta.timezoneDismissed'), 'Asia/Novosibirsk');
  } finally { await closeWindow(t); }
  // A failed change keeps the banner and tells why.
  t = boot({me: 'Europe/Moscow', browser: 'Asia/Novosibirsk', patchFails: true});
  try {
    await settled(); t.$('timezone-banner-change').click(); await settled();
    assert.equal(banner(t), true); assert.equal(t.$('timezone-banner-change').disabled, false);
    assert.match(bannerText(t), /Не удалось/); assert.equal(t.w.BerestaTimezone.accountZone(), 'Europe/Moscow');
  } finally { await closeWindow(t); }
  // Signing out hides it.
  t = boot({me: 'Europe/Moscow', browser: 'Asia/Novosibirsk'});
  try { await settled(); assert.equal(banner(t), true); t.$('workspace').hidden = true; await settled(); assert.equal(banner(t), false); } finally { await closeWindow(t); }
}

/* ---------- Settings dialog ---------- */
async function settings() {
  const t = boot({me: 'Europe/Moscow', browser: 'Asia/Omsk'});
  const {w, $} = t;
  try {
    await settled();
    t.$('timezone-banner-keep').click();
    assert.ok($('open-timezone') && $('nav-menu').contains($('open-timezone')), 'a row in the account menu');
    assert.equal($('open-timezone').textContent, 'Часовой пояс');
    assert.ok($('general-menu').querySelector('[data-proxy="open-timezone"]'), 'and in the phone menu');
    assert.equal(w.document.querySelector('#timezone-dialog [style]'), null, 'no inline styles');
    assert.equal(w.document.querySelector('#timezone-dialog script'), null);
    $('open-timezone').click();
    await t.until(() => items(t).length === ZONES.length, 'the list');
    assert.equal($('timezone-dialog').open, true);
    assert.equal($('timezone-current').textContent, 'Сейчас: Москва (UTC+3)');
    assert.deepEqual(ids(t), ZONES.map(row => row.id), 'sorted as the server sorts them');
    assert.equal(t.state.zonesCalls, 1);
    assert.equal(items(t).filter(item => item.tabIndex === 0).length, 1, 'one tab stop in the list');
    assert.equal($('timezone-save').disabled, true);
    assert.equal(items(t).find(item => item.dataset.zone === 'Europe/Moscow').getAttribute('aria-current'), 'true');
    const novosibirsk = items(t).find(item => item.dataset.zone === 'Asia/Novosibirsk');
    assert.deepEqual([...novosibirsk.children].map(c => c.textContent), ['Новосибирск', 'Asia/Novosibirsk', 'UTC+7']);

    // Search: offset, Russian label, IANA id.
    type(t, '+7'); assert.deepEqual(ids(t).sort(), ['Asia/Bangkok', 'Asia/Novosibirsk']);
    type(t, 'UTC+7'); assert.equal(ids(t).length, 2);
    type(t, '+07:00'); assert.equal(ids(t).length, 2);
    type(t, '+3'); assert.deepEqual(ids(t).sort(), ['Europe/Minsk', 'Europe/Moscow']);
    type(t, '+5'); assert.deepEqual(ids(t), [], '+5 is not +5:30');
    assert.equal($('timezone-empty').hidden, false);
    type(t, '+5:30'); assert.deepEqual(ids(t), ['Asia/Kolkata']);
    type(t, 'новосиб'); assert.deepEqual(ids(t), ['Asia/Novosibirsk']);
    type(t, 'ОМСК'); assert.deepEqual(ids(t), ['Asia/Omsk']);
    type(t, 'asia/omsk'); assert.deepEqual(ids(t), ['Asia/Omsk']);
    type(t, 'novosibirsk'); assert.deepEqual(ids(t), ['Asia/Novosibirsk']);
    type(t, 'utc'); assert.ok(ids(t).includes('UTC'));
    type(t, 'zzz'); assert.equal(ids(t).length, 0); assert.equal($('timezone-empty').hidden, false);
    type(t, ''); assert.equal(ids(t).length, ZONES.length); assert.equal($('timezone-empty').hidden, true);

    // Keyboard: arrows, Home, End, one tab stop that moves with the focus.
    type(t, '');
    $('timezone-search').focus(); key(t, $('timezone-search'), {key: 'ArrowDown'});
    const first = w.document.activeElement; assert.ok(first.classList.contains('timezone-item'));
    assert.equal(first.dataset.zone, 'Europe/Moscow', 'the focus starts on the current zone');
    const start = items(t).indexOf(first);
    key(t, first, {key: 'ArrowDown'});
    assert.equal(w.document.activeElement, items(t)[start + 1]); assert.equal(items(t)[start + 1].tabIndex, 0); assert.equal(items(t)[start].tabIndex, -1);
    key(t, w.document.activeElement, {key: 'End'}); assert.equal(w.document.activeElement, items(t).at(-1));
    key(t, w.document.activeElement, {key: 'Home'}); assert.equal(w.document.activeElement, items(t)[0]);
    key(t, w.document.activeElement, {key: 'ArrowUp'}); assert.equal(w.document.activeElement, items(t)[0], 'stops at the top');

    // Pick and save: manual.
    items(t).find(item => item.dataset.zone === 'Asia/Novosibirsk').click();
    assert.equal(items(t).find(item => item.dataset.zone === 'Asia/Novosibirsk').getAttribute('aria-pressed'), 'true');
    assert.equal($('timezone-save').disabled, false);
    items(t).find(item => item.dataset.zone === 'Europe/Moscow').click();
    assert.equal($('timezone-save').disabled, true, 'the current zone needs no saving');
    items(t).find(item => item.dataset.zone === 'Asia/Novosibirsk').click();
    submit(t); await settled();
    assert.equal(t.state.patches.length, 1);
    assert.deepEqual(t.state.patches[0].body, {timezone: 'Asia/Novosibirsk', source: 'manual'});
    assert.equal($('timezone-current').textContent, 'Сейчас: Новосибирск (UTC+7)');
    assert.match($('timezone-status').textContent, /Новосибирск \(UTC\+7\)/);
    assert.equal($('timezone-save').disabled, true);
    assert.equal(w.BerestaTimezone.accountZone(), 'Asia/Novosibirsk');

    // «Определить автоматически» uses the device zone and writes it as manual.
    $('timezone-auto').click(); await settled();
    assert.deepEqual(t.state.patches[1].body, {timezone: 'Asia/Omsk', source: 'manual'});
    assert.equal($('timezone-current').textContent, 'Сейчас: Омск (UTC+6)');
    // Without a usable device zone it only says so.
    t.env.browser = 'Mars/Base'; $('timezone-auto').click(); await settled();
    assert.equal(t.state.patches.length, 2); assert.match($('timezone-status').textContent, /Выберите его из списка/);
    // Enter in the search picks the first match, a second Enter saves it.
    type(t, 'калькут'); key(t, $('timezone-search'), {key: 'Enter'});
    assert.equal(items(t)[0].getAttribute('aria-pressed'), 'true'); assert.equal(t.state.patches.length, 2);
    key(t, $('timezone-search'), {key: 'Enter'}); await settled();
    assert.deepEqual(t.state.patches[2].body, {timezone: 'Asia/Kolkata', source: 'manual'});
    // A failed save keeps the dialog and the old zone.
    t.state.patchFails = true; type(t, ''); items(t).find(item => item.dataset.zone === 'UTC').click(); submit(t); await settled();
    assert.equal($('timezone-dialog').open, true); assert.match($('timezone-status').textContent, /Не удалось/);
    assert.equal(w.BerestaTimezone.accountZone(), 'Asia/Kolkata');
    // Close: the focus goes back to the row that opened it.
    $('timezone-dialog').close();
    assert.equal(w.document.activeElement, $('open-timezone'));
    assert.equal(t.state.zonesCalls, 1, 'one cached request for everything');
  } finally { await closeWindow(t); }
}

/* ---------- A failed list ---------- */
async function failedList() {
  const t = boot({me: 'Asia/Novosibirsk', browser: 'Asia/Novosibirsk', zonesFail: true});
  try {
    await settled();
    t.$('open-timezone').click();
    await t.until(() => items(t).length > 0, 'the built-in list');
    assert.ok(items(t).length < 40, 'a short list, not the full one');
    assert.ok(ids(t).includes('Europe/Moscow') && ids(t).includes('Asia/Novosibirsk'));
    assert.equal($label(t, 'Asia/Novosibirsk'), 'Новосибирск');
    assert.equal(t.$('timezone-current').textContent, 'Сейчас: Новосибирск (UTC+7)');
    type(t, '+7'); assert.ok(ids(t).includes('Asia/Novosibirsk'));
    // The list is fetched again the next time, and then the full one shows.
    t.state.zonesFail = false; t.$('timezone-dialog').close(); t.$('open-timezone').click();
    await t.until(() => items(t).length === ZONES.length, 'the full list after a retry');
  } finally { await closeWindow(t); }
}
const $label = (t, id) => items(t).find(item => item.dataset.zone === id).querySelector('.timezone-name').textContent;

/* ---------- Reminder form ---------- */
async function reminderForm() {
  const open = async t => {
    await settled();
    t.$('notes').querySelector('button').click();
    await t.until(() => !t.$('title').disabled && t.$('title').value === 'Заметка', 'the note');
    await settled();
    t.$('reminder-new').click();
  };
  // The account zone, not the zone of the device.
  let t = boot({me: 'Asia/Novosibirsk', browser: 'Europe/Moscow', reminders: true});
  try {
    await open(t);
    assert.equal(t.$('reminder-zone').value, 'Asia/Novosibirsk');
    assert.equal(t.$('reminder-zone-name').textContent, 'Новосибирск (Asia/Novosibirsk)');
    await t.until(() => t.$('reminder-zones').options.length === ZONES.length, 'suggestions from the API');
    const option = [...t.$('reminder-zones').options].find(o => o.value === 'Asia/Omsk');
    assert.ok(option && option.text.includes('Омск') && option.text.includes('UTC+6'));
    assert.equal(t.state.zonesCalls, 1);
    t.$('reminder-discard').click();
    t.$('reminder-new').click(); await settled();
    assert.equal(t.state.zonesCalls, 1, 'the list is fetched once for the dialog, the form and the banner');
  } finally { await closeWindow(t); }
  // The list failed: the short built-in list is the suggestion and the zone line still has a name.
  t = boot({me: 'Asia/Novosibirsk', browser: 'Europe/Moscow', zonesFail: true, reminders: true});
  try {
    await open(t);
    assert.equal(t.$('reminder-zone').value, 'Asia/Novosibirsk');
    assert.equal(t.$('reminder-zone-name').textContent, 'Новосибирск (Asia/Novosibirsk)');
    const values = [...t.$('reminder-zones').options].map(o => o.value);
    assert.ok(values.length > 3 && values.includes('Europe/Moscow') && values.includes('Asia/Vladivostok'));
  } finally { await closeWindow(t); }
  // Nothing saved yet: the device zone, then Moscow.
  t = boot({me: null, browser: 'Asia/Omsk', patchFails: true, reminders: true});
  try { await open(t); assert.equal(t.$('reminder-zone').value, 'Asia/Omsk'); } finally { await closeWindow(t); }
  t = boot({me: null, browser: 'Mars/Base', reminders: true});
  try { await open(t); assert.equal(t.$('reminder-zone').value, 'Europe/Moscow'); } finally { await closeWindow(t); }
}

module.exports = async () => {
  await detection();
  await travelBanner();
  await settings();
  await failedList();
  await reminderForm();
  console.log('Time zone checks passed: quiet detection once, invalid device zone, travel banner with aliases and dismissal, settings dialog search and keyboard, manual save, automatic detection, failed list, reminder form default zone.');
};
if (require.main === module) module.exports().catch(error => { console.error(error); process.exitCode = 1; });
