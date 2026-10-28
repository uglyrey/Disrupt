'use strict';
// The sign-in window (two buttons, email code, consent step, old login, recovery), the password dialogs and the eye icon.
// There is no layout in jsdom. The checks look at attributes, focus, calls and the style sources, not at pixels.
// app.js is strict mode, so every script goes into ONE eval and shares its scope.
const assert = require('node:assert/strict'), fs = require('fs'), path = require('path'), {JSDOM} = require('jsdom');
const root = path.join(__dirname, '../app/static');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const reply = (data, status = 200) => ({ok: status < 400, status, headers: new Headers(), json: async () => structuredClone(data)});
async function settled() { for (let i = 0; i < 30; i++) await new Promise(resolve => setImmediate(resolve)); }
const VERSION = '2026-10-28';
const user = (extra = {}) => ({id: 'u1', username: 'ivan', csrf_token: 'csrf-1', policy_current: true, policy_version: VERSION, deletion_requested: false, has_password: true, ...extra});
const SCRIPTS = ['app.js', 'workspace.js', 'password-field.js', 'signin.js', 'account-password.js', 'landing.js'];

// `routes` maps «METHOD /path» to a function that gets the JSON body and returns a reply. `me` is what /auth/me answers.
async function boot({routes = {}, options = {code_login_enabled: true}, me = null} = {}) {
  const dom = new JSDOM(read('index.html'), {url: 'https://beresta.invalid/', runScripts: 'outside-only'}), w = dom.window;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  try { Object.defineProperty(w.crypto, 'randomUUID', {value: require('node:crypto').randomUUID, configurable: true}); } catch { /* already provided */ }
  w.matchMedia = query => ({media: query, matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}});
  w.localStorage.setItem('beresta.cookie-consent.v1', JSON.stringify({choice: 'necessary', version: 1, at: '2026-10-12T09:00:00.000Z'}));
  // A manual clock: the resend wait is checked without waiting.
  const clock = {now: 1800000000000, timers: [], id: 0};
  w.Date.now = () => clock.now;
  w.setTimeout = (fn, ms = 0) => { const id = ++clock.id; clock.timers.push({id, at: clock.now + ms, fn}); return id; };
  w.clearTimeout = id => { clock.timers = clock.timers.filter(timer => timer.id !== id); };
  const advance = async ms => {
    const end = clock.now + ms;
    for (;;) {
      const due = clock.timers.filter(timer => timer.at <= end).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      clock.timers = clock.timers.filter(timer => timer !== due);
      clock.now = Math.max(clock.now, due.at);
      await due.fn();
    }
    clock.now = end; await settled();
  };
  const state = {me, options, calls: []};
  w.fetch = async (input, init = {}) => {
    const url = new URL(input, w.location.href), method = init.method || 'GET';
    const body = init.body && typeof init.body === 'string' ? JSON.parse(init.body) : null;
    state.calls.push({path: url.pathname, method, body});
    if (url.pathname === '/health' || url.pathname === '/api/v1/provider/usage') return reply({simulation: true});
    if (url.pathname === '/api/v1/auth/me') return state.me ? reply(state.me) : reply({detail: 'Войдите в приложение'}, 401);
    if (url.pathname === '/api/v1/auth/email/options') return state.options ? reply(state.options) : reply({detail: 'нет'}, 500);
    if (['/api/v1/categories', '/api/v1/jobs', '/api/v1/notes'].includes(url.pathname)) return reply([]);
    const route = routes[`${method} ${url.pathname}`];
    if (route) return route(body, state);
    return reply({detail: 'Не найдено'}, 404);
  };
  w.eval(SCRIPTS.map(read).join('\n'));
  await settled();
  const $ = id => w.document.getElementById(id);
  const callsTo = suffix => state.calls.filter(call => call.path.endsWith(suffix));
  const submit = async form => { await form.onsubmit({preventDefault() {}, target: form, submitter: null}); await settled(); };
  const type = (id, value) => { $(id).value = value; $(id).dispatchEvent(new w.Event('input', {bubbles: true})); };
  return {dom, w, $, state, clock, advance, callsTo, submit, type, close: () => dom.window.close()};
}
const visible = node => !node.hidden && !node.closest('[hidden]');
const startAnswer = () => reply({expires_at: '2026-10-15T10:10:00+00:00', resend_after: 60}, 202);

/* ---------- Static: the markup and the style sources ---------- */
function staticChecks() {
  const index = read('index.html');
  assert.ok(!/\sstyle\s*=/.test(index), 'no inline styles');
  assert.ok(!/<script(?![^>]*\ssrc=)/.test(index), 'no inline scripts');
  for (const name of ['password-field.js', 'signin.js', 'account-password.js']) assert.ok(index.includes(`/static/${name}`), name);
  for (const name of ['password-field.css', 'signin.css']) assert.ok(index.includes(`/static/${name}`), name);
  assert.ok(!index.includes('email-auth'), 'the old email dialog is gone');
  const doc = new JSDOM(index).window.document;
  assert.equal(doc.getElementById('landing-register'), null);
  assert.equal(doc.getElementById('landing-register-bottom'), null);
  assert.equal(doc.getElementById('email-auth-dialog'), null);
  const hero = doc.querySelectorAll('.landing-hero .landing-cta button');
  assert.equal(hero.length, 1, 'one button left in the hero');
  assert.equal(hero[0].id, 'landing-login');
  assert.ok(hero[0].closest('.landing-cta').classList.contains('landing-cta-phone'));
  assert.equal(hero[0].textContent.trim(), 'Войти');
  assert.ok(!/Создать аккаунт/.test(doc.querySelector('.landing-hero').textContent), 'no sign-up button text in the hero');
  // The hero button shows only while the window sits below the text, that is where the landing grid has one column.
  const signin = read('signin.css'), landing = read('landing.css');
  assert.match(signin, /\.landing-cta\.landing-cta-phone\{display:none\}/);
  const breakpoint = /@media\(max-width:(\d+)px\)\{\.landing-top\{grid-template-columns:1fr/.exec(landing)[1];
  assert.ok(signin.includes(`@media(max-width:${breakpoint}px){.landing-cta.landing-cta-phone{display:flex}}`), 'same breakpoint as the one-column landing');
  // Main buttons: the same box, an icon each.
  const email = doc.getElementById('signin-email'), telegram = doc.getElementById('tg-login');
  for (const button of [email, telegram]) {
    assert.ok(button.classList.contains('signin-button'));
    assert.ok(button.querySelector('svg[aria-hidden="true"]'), 'icon');
    assert.equal(button.type, 'button');
    assert.equal(button.closest('#signin-main').id, 'signin-main');
  }
  assert.equal(email.textContent.trim(), 'Войти по email');
  assert.equal(telegram.textContent.trim(), 'Войти через Telegram');
  assert.match(signin, /\.signin-buttons \.signin-button\{[^}]*min-height:52px/);
  // The code field.
  const field = doc.getElementById('email-code');
  assert.equal(field.getAttribute('autocomplete'), 'one-time-code');
  assert.equal(field.getAttribute('inputmode'), 'numeric');
  assert.equal(field.maxLength, 6);
  // Small links at the bottom of the window.
  assert.equal(doc.getElementById('show-password-login').textContent.trim(), 'Войти по логину и паролю');
  assert.equal(doc.getElementById('open-recovery').textContent.trim(), 'Восстановить аккаунт');
  assert.ok(doc.getElementById('signin-links').contains(doc.getElementById('open-recovery')));
  // The consent texts and the version are the ones of the registration form.
  assert.equal(doc.getElementById('accept-policy').dataset.policyVersion, VERSION);
  assert.ok(doc.getElementById('email-consent').closest('label').querySelector('a[href="/privacy"]'));
  assert.equal(doc.getElementById('email-age'), null);
  assert.equal(doc.getElementById('confirm-age'), null);
  assert.equal(doc.getElementById('email-marketing').checked, false);
  assert.equal(doc.getElementById('email-consent').checked, false);
  // Account menu.
  assert.ok(doc.querySelector('#general-menu [data-proxy="open-password"]'));
  assert.equal(doc.getElementById('open-password').textContent.trim(), 'Сменить пароль');
  assert.match(doc.getElementById('nopassword-text').textContent, /У вас нет пароля, вы входите через Telegram или по коду из почты\./);
  assert.equal(doc.getElementById('nopassword-ok').textContent.trim(), 'Понятно');
  assert.match(doc.querySelector('#recovery-dialog p').textContent, /Опишите, как вы входили и что случилось\. Мы ответим на почту\./);
  doc.defaultView.close();
}

/* ---------- Two buttons and the way to the window ---------- */
async function mainStage() {
  const t = await boot();
  const {$, w} = t;
  try {
    assert.equal($('auth-card').dataset.stage, 'main');
    assert.equal(visible($('signin-email')) && visible($('tg-login')), true);
    assert.equal($('signin-email').disabled, false);
    assert.equal($('signin-email-note').hidden, true);
    assert.equal($('signin-password').hidden, true);
    assert.equal($('email-form').hidden && $('code-form').hidden && $('consent-form').hidden, true);
    assert.equal(visible($('signin-links')), true);
    // The hero and the bottom buttons take the visitor to the window and focus its first control.
    let scrolls = 0;
    $('auth-card').scrollIntoView = () => { scrolls++; };
    $('landing-login').click();
    assert.equal(scrolls, 1); assert.equal(w.document.activeElement.id, 'signin-email');
    $('tg-login').focus();
    $('landing-login-bottom').click();
    assert.equal(scrolls, 2); assert.equal(w.document.activeElement.id, 'signin-email');
  } finally { t.close(); }
}

/* ---------- Email code: new address, consent step, resend ---------- */
async function emailCodeWithConsent() {
  let attempt = 0;
  const t = await boot({routes: {
    'POST /api/v1/auth/email/code/start': () => startAnswer(),
    'POST /api/v1/auth/email/code/confirm': body => {
      attempt++;
      if (!body.accept_policy) return reply({detail: 'Чтобы создать аккаунт, дайте согласие на обработку данных', code: 'consent_required'}, 409);
      return reply(user({has_password: false}));
    },
  }});
  const {$, w} = t;
  try {
    $('signin-email').click();
    assert.equal($('auth-card').dataset.stage, 'email');
    assert.equal(visible($('email-form')), true); assert.equal($('auth-form').hidden, true);
    assert.equal(w.document.activeElement.id, 'email-address');
    // A bad address is stopped before any request.
    t.type('email-address', 'not-an-address');
    await t.submit($('email-form'));
    assert.equal(t.callsTo('/code/start').length, 0);
    assert.match($('auth-message').textContent, /адрес почты/);
    t.type('email-address', '  Ivan@Example.Test ');
    await t.submit($('email-form'));
    assert.deepEqual(t.callsTo('/code/start').at(-1).body, {email: 'ivan@example.test'});
    assert.equal($('auth-card').dataset.stage, 'code');
    assert.match($('code-lead').textContent, /ivan@example\.test/);
    assert.equal(w.document.activeElement.id, 'email-code');
    // Resend waits a minute and shows the countdown.
    assert.equal($('code-resend').disabled, true);
    assert.match($('code-resend').textContent, /через 60 с/);
    await t.advance(30000);
    assert.match($('code-resend').textContent, /через 30 с/); assert.equal($('code-resend').disabled, true);
    $('code-resend').click(); await t.advance(0);
    assert.equal(t.callsTo('/code/start').length, 1, 'a click before the wait ends sends nothing');
    await t.advance(30000);
    assert.equal($('code-resend').disabled, false); assert.equal($('code-resend').textContent, 'Отправить ещё раз');
    $('code-resend').click(); await settled();
    assert.equal(t.callsTo('/code/start').length, 2);
    assert.match($('auth-info').textContent, /ещё раз/);
    assert.equal($('code-resend').disabled, true, 'the wait starts again');
    // Paste works with spaces, letters are dropped, only six digits stay.
    const paste = text => {
      const event = new w.Event('paste', {bubbles: true, cancelable: true});
      Object.defineProperty(event, 'clipboardData', {value: {getData: () => text}});
      $('email-code').dispatchEvent(event);
      return event;
    };
    assert.equal(paste('123 456').defaultPrevented, true); assert.equal($('email-code').value, '123456');
    paste('код: 98-76-54-32'); assert.equal($('email-code').value, '987654');
    t.type('email-code', '12ab'); assert.equal($('email-code').value, '12');
    // Short code: no request.
    await t.submit($('code-form'));
    assert.equal(t.callsTo('/code/confirm').length, 0);
    assert.match($('auth-message').textContent, /шесть цифр/);
    // A new address: the server asks for consent first, the code is sent again with it.
    t.type('email-code', '123456');
    await t.submit($('code-form'));
    assert.deepEqual(t.callsTo('/code/confirm')[0].body, {code: '123456'});
    assert.equal($('auth-card').dataset.stage, 'consent');
    assert.equal(visible($('consent-form')), true); assert.equal($('auth').hidden, false);
    await t.submit($('consent-form'));
    assert.equal(t.callsTo('/code/confirm').length, 1);
    assert.match($('auth-message').textContent, /согласие на обработку персональных данных/);
    $('email-consent').checked = true;
    await t.submit($('consent-form'));
    assert.deepEqual(t.callsTo('/code/confirm')[1].body, {code: '123456', accept_policy: true, policy_version: VERSION, marketing_opt_in: false});
    assert.equal(attempt, 2);
    assert.equal($('auth').hidden, true, 'signed in: the landing is gone');
    assert.equal($('username').textContent, 'ivan');
    assert.equal($('email-code').value, '');
  } finally { t.close(); }
}
/* ---------- Email code: existing account, ticked boxes, wrong code ---------- */
async function emailCodeExisting() {
  const answers = [];
  const t = await boot({routes: {
    'POST /api/v1/auth/email/code/start': () => startAnswer(),
    'POST /api/v1/auth/email/code/confirm': body => answers.shift()(body),
  }});
  const {$} = t;
  try {
    // Wrong code: a readable error, the person stays on the code step. The text is never parsed as HTML.
    $('signin-email').click(); t.type('email-address', 'ivan@example.test'); await t.submit($('email-form'));
    answers.push(() => reply({detail: '<img src=x onerror=alert(1)>Код не подошёл'}, 400));
    t.type('email-code', '000001'); await t.submit($('code-form'));
    assert.equal($('auth-card').dataset.stage, 'code');
    assert.equal($('auth-message').hidden, false); assert.match($('auth-message').textContent, /Код не подошёл/);
    assert.equal($('auth-message').querySelector('img'), null);
    assert.equal($('auth').hidden, false);
    // An existing account signs in with the code alone.
    answers.push(() => reply(user({has_password: false})));
    t.type('email-code', '000002'); await t.submit($('code-form'));
    assert.deepEqual(t.callsTo('/code/confirm').at(-1).body, {code: '000002'});
    assert.equal($('auth').hidden, true);
  } finally { t.close(); }
  // Consent on the main step travels with the code and no second step appears.
  const ticked = await boot({routes: {
    'POST /api/v1/auth/email/code/start': () => startAnswer(),
    'POST /api/v1/auth/email/code/confirm': () => reply(user({has_password: false})),
  }});
  try {
    ticked.$('accept-policy').checked = true; ticked.$('marketing-consent').checked = true;
    ticked.$('signin-email').click(); ticked.type('email-address', 'new@example.test'); await ticked.submit(ticked.$('email-form'));
    ticked.type('email-code', '123123'); await ticked.submit(ticked.$('code-form'));
    assert.deepEqual(ticked.callsTo('/code/confirm').at(-1).body, {code: '123123', accept_policy: true, policy_version: VERSION, marketing_opt_in: true});
    assert.equal(ticked.$('auth').hidden, true);
  } finally { ticked.close(); }
  // «Другая почта» goes back to the address step; «Назад» to the two buttons.
  const back = await boot({routes: {'POST /api/v1/auth/email/code/start': () => startAnswer()}});
  try {
    back.$('signin-email').click(); back.type('email-address', 'ivan@example.test'); await back.submit(back.$('email-form'));
    back.$('code-change').click(); assert.equal(back.$('auth-card').dataset.stage, 'email');
    back.$('email-back').click(); assert.equal(back.$('auth-card').dataset.stage, 'main');
    assert.equal(visible(back.$('signin-email')), true);
    // A server refusal (too many letters) is shown and the person stays on the address step.
  } finally { back.close(); }
  const refused = await boot({routes: {'POST /api/v1/auth/email/code/start': () => reply({detail: 'Подождите перед новым письмом'}, 429)}});
  try {
    refused.$('signin-email').click(); refused.type('email-address', 'ivan@example.test'); await refused.submit(refused.$('email-form'));
    assert.equal(refused.$('auth-card').dataset.stage, 'email');
    assert.match(refused.$('auth-message').textContent, /Подождите/);
    assert.equal(refused.$('email-send').disabled, false, 'the form unlocks after a refusal');
  } finally { refused.close(); }
}

/* ---------- Email unavailable ---------- */
async function emailOff() {
  const t = await boot({options: {code_login_enabled: false}});
  const {$, w} = t;
  try {
    assert.equal($('signin-email').disabled, true);
    assert.equal($('signin-email-note').hidden, false);
    assert.equal($('signin-email-note').textContent.trim(), 'Вход по почте скоро заработает');
    $('signin-email').click();
    assert.equal($('auth-card').dataset.stage, 'main', 'a disabled button opens nothing');
    assert.equal($('tg-login').disabled, false, 'Telegram stays');
    $('landing-login').click();
    assert.equal(w.document.activeElement.id, 'tg-login', 'focus goes to the working button');
  } finally { t.close(); }
  // Without any answer the button stays and the server decides.
  const silent = await boot({options: null});
  try { assert.equal(silent.$('signin-email').disabled, false); } finally { silent.close(); }
}

/* ---------- Old login ---------- */
async function oldLogin() {
  const t = await boot({routes: {'POST /api/v1/auth/login': () => reply(user())}});
  const {$, w} = t;
  try {
    $('show-password-login').click();
    assert.equal($('auth-card').dataset.stage, 'password');
    assert.equal(visible($('signin-password')), true); assert.equal($('signin-main').hidden, true);
    assert.equal($('auth-form').querySelectorAll('button[type=submit]').length, 1, 'login only: the form no longer registers');
    assert.equal($('auth-form').querySelector('button[type=submit]').value, 'login');
    assert.equal($('forgot-password').type, 'button');
    assert.equal(w.document.activeElement.id, 'login');
    $('password-back').click();
    assert.equal($('auth-card').dataset.stage, 'main');
    $('show-password-login').click();
    t.type('login', 'ivan'); t.type('password', 'test-only-password-123');
    await $('auth-form').onsubmit({preventDefault() {}, target: $('auth-form'), submitter: {value: 'login'}}); await settled();
    assert.deepEqual(t.callsTo('/auth/login').at(-1).body, {username: 'ivan', password: 'test-only-password-123'});
    assert.equal($('auth').hidden, true);
  } finally { t.close(); }
}

/* ---------- Recovery request ---------- */
async function recovery() {
  const t = await boot({routes: {'POST /api/v1/support/recovery': () => reply({status: 'ok', message: 'Запрос отправлен. Мы напишем вам на почту'})}});
  const {$} = t;
  try {
    t.type('email-address', 'typed@example.test');
    $('open-recovery').click();
    assert.equal($('recovery-dialog').open, true);
    assert.equal($('recovery-email').value, 'typed@example.test', 'the address already typed is offered');
    t.type('recovery-email', 'bad');
    await t.submit($('recovery-form'));
    assert.match($('recovery-status').textContent, /адрес почты/);
    t.type('recovery-email', 'me@example.test');
    await t.submit($('recovery-form'));
    assert.match($('recovery-status').textContent, /Опишите/);
    t.type('recovery-text', 'я'.repeat(2001));
    await t.submit($('recovery-form'));
    assert.match($('recovery-status').textContent, /2000/);
    t.type('recovery-text', 'Входил через Telegram, аккаунт пропал.');
    await t.submit($('recovery-form'));
    assert.match($('recovery-status').textContent, /согласие/);
    assert.equal(t.callsTo('/support/recovery').length, 0);
    $('recovery-consent').checked = true;
    await t.submit($('recovery-form'));
    assert.deepEqual(t.callsTo('/support/recovery').at(-1).body, {email: 'me@example.test', text: 'Входил через Telegram, аккаунт пропал.', accept_processing: true});
    assert.equal($('recovery-status').textContent, 'Запрос отправлен. Мы напишем вам на почту');
    assert.equal($('recovery-submit').disabled, true, 'no second copy of the same request');
    assert.equal($('recovery-text').value, '');
    // A closed dialog starts clean and a new request can be sent.
    $('recovery-dialog').close();
    assert.equal($('recovery-status').textContent, ''); assert.equal($('recovery-consent').checked, false);
    $('open-recovery').click();
    assert.equal($('recovery-submit').disabled, false);
  } finally { t.close(); }
  const limited = await boot({routes: {'POST /api/v1/support/recovery': () => reply({detail: 'Вы уже отправляли запросы. Подождите час и попробуйте снова'}, 429)}});
  try {
    limited.$('open-recovery').click();
    limited.type('recovery-email', 'me@example.test'); limited.type('recovery-text', 'Помогите'); limited.$('recovery-consent').checked = true;
    await limited.submit(limited.$('recovery-form'));
    assert.match(limited.$('recovery-status').textContent, /Подождите час/);
    assert.equal(limited.$('recovery-submit').disabled, false);
  } finally { limited.close(); }
}

/* ---------- Password change ---------- */
async function passwordChange() {
  let outcome = () => reply({status: 'ok'});
  const t = await boot({me: user(), routes: {'POST /api/v1/account/password': body => outcome(body)}});
  const {$} = t;
  try {
    assert.equal($('workspace').hidden, false, 'signed in');
    $('open-password').click(); await settled();
    assert.equal($('password-dialog').open, true);
    assert.equal(t.callsTo('/auth/me').length >= 2, true, 'the flag is read fresh');
    const old = 'old-password-123', next = 'brand-new-password-456';
    // Every check stops before the request.
    await t.submit($('password-form'));
    assert.equal($('password-old-error').textContent, 'Введите текущий пароль.');
    t.type('password-old', old); t.type('password-new', 'short'); t.type('password-new2', 'short');
    await t.submit($('password-form'));
    assert.match($('password-status').textContent, /от 10 до 128/);
    t.type('password-new', next); t.type('password-new2', next + 'x');
    await t.submit($('password-form'));
    assert.equal($('password-new2-error').textContent, 'Пароли не совпадают'); assert.equal($('password-new2-error').hidden, false);
    assert.equal($('password-new2').hasAttribute('aria-invalid'), true);
    t.type('password-new', old); t.type('password-new2', old);
    await t.submit($('password-form'));
    assert.match($('password-status').textContent, /отличаться/);
    assert.equal(t.callsTo('/account/password').length, 0);
    // The server says the old password is wrong.
    t.type('password-new', next); t.type('password-new2', next);
    outcome = () => reply({detail: 'Неверный текущий пароль'}, 403);
    await t.submit($('password-form'));
    assert.equal($('password-old-error').textContent, 'Неверный текущий пароль'); assert.equal($('password-old-error').hidden, false);
    assert.equal($('password-new2-error').hidden, true, 'old errors are cleared');
    assert.equal($('password-submit').disabled, false);
    // Success clears the fields and tells the person.
    outcome = () => reply({status: 'ok'});
    await t.submit($('password-form'));
    assert.deepEqual(t.callsTo('/account/password').at(-1).body, {old_password: old, new_password: next});
    assert.match($('password-status').textContent, /Пароль изменён/);
    assert.equal($('password-old').value + $('password-new').value + $('password-new2').value, '');
    assert.equal($('password-old-error').hidden, true);
    // Closing the dialog leaves nothing behind.
    t.type('password-old', 'leftover'); $('password-dialog').close();
    assert.equal($('password-old').value, ''); assert.equal($('password-status').textContent, '');
    // The server learned meanwhile that the account has no password.
    $('open-password').click(); await settled();
    t.type('password-old', old); t.type('password-new', next); t.type('password-new2', next);
    outcome = () => reply({detail: 'У аккаунта нет пароля', code: 'no_password'}, 409);
    await t.submit($('password-form'));
    assert.equal($('password-dialog').open, false); assert.equal($('nopassword-dialog').open, true);
  } finally { t.close(); }
}

async function noPasswordPopup() {
  const t = await boot({me: user({has_password: false})});
  const {$} = t;
  try {
    $('open-password').click(); await settled();
    assert.equal($('nopassword-dialog').open, true);
    assert.equal($('password-dialog').open, false, 'no password form for this account');
    assert.equal($('nopassword-text').textContent, 'У вас нет пароля, вы входите через Telegram или по коду из почты.');
    $('nopassword-ok').click();
    assert.equal($('nopassword-dialog').open, false);
    assert.equal(t.callsTo('/account/password').length, 0);
  } finally { t.close(); }
  const failing = await boot({me: user()});
  try {
    failing.state.me = null;
    failing.$('open-password').click(); await settled();
    assert.equal(failing.$('password-dialog').open, false, 'no dialog when the account cannot be read');
    assert.equal(failing.$('nopassword-dialog').open, false);
  } finally { failing.close(); }
}

/* ---------- Eye icon ---------- */
async function eyeIcon() {
  const t = await boot({me: user()});
  const {$, w} = t;
  try {
    const fields = [...w.document.querySelectorAll('input[data-password-field]')];
    const ids = fields.map(field => field.id).sort();
    for (const id of ['password', 'password-old', 'password-new', 'password-new2', 'delete-password', 'reset-password', 'reset-password2']) assert.ok(ids.includes(id), id + ' has the eye');
    assert.equal(w.document.querySelectorAll('input[type=password]:not([data-password-field])').length, 0, 'no field without it');
    for (const field of fields) {
      const wrap = field.parentNode, button = wrap.querySelector('.password-toggle');
      assert.ok(wrap.classList.contains('password-field'), field.id);
      assert.equal(wrap.lastElementChild, button, 'the button follows the field, so it sits on the right: ' + field.id);
      assert.equal(button.type, 'button');
      assert.equal(button.getAttribute('aria-label'), 'Показать пароль');
      assert.equal(button.getAttribute('aria-pressed'), 'false');
      assert.ok(button.querySelector('svg[aria-hidden="true"]'));
      let submits = 0;
      const form = field.closest('form');
      if (form) form.addEventListener('submit', () => { submits++; });
      const press = new w.Event('mousedown', {bubbles: true, cancelable: true});
      button.dispatchEvent(press);
      assert.equal(press.defaultPrevented, true, 'a press does not take focus from the field');
      field.value = 'abcdef'; field.focus(); field.setSelectionRange(2, 4);
      button.click();
      assert.equal(field.type, 'text', field.id);
      assert.equal(button.getAttribute('aria-label'), 'Скрыть пароль'); assert.equal(button.getAttribute('aria-pressed'), 'true');
      assert.equal(w.document.activeElement, field, 'focus stays in the field');
      assert.deepEqual([field.selectionStart, field.selectionEnd], [2, 4], 'the caret stays');
      assert.equal(field.value, 'abcdef');
      assert.equal(submits, 0, 'the button never submits');
      button.click();
      assert.equal(field.type, 'password'); assert.equal(button.getAttribute('aria-label'), 'Показать пароль');
      assert.equal(button.getAttribute('aria-pressed'), 'false');
    }
    // A shown password is hidden again when the form is sent and when its dialog closes.
    const form = $('password-form');
    $('password-new').parentNode.querySelector('.password-toggle').click();
    assert.equal($('password-new').type, 'text');
    form.dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true}));
    assert.equal($('password-new').type, 'password');
    assert.equal($('password-new').parentNode.querySelector('.password-toggle').getAttribute('aria-pressed'), 'false');
    $('password-old').parentNode.querySelector('.password-toggle').click();
    $('password-dialog').open = true; $('password-dialog').close();
    assert.equal($('password-old').type, 'password');
    // Fields that appear later get the eye too, without a second one on the old fields.
    const late = w.document.createElement('form');
    late.innerHTML = '<label>Секрет<input id="late-password" type="password" autocomplete="new-password"></label>';
    w.document.body.append(late);
    await settled();
    const lateField = $('late-password');
    assert.equal(lateField.parentNode.className, 'password-field');
    assert.equal(lateField.parentNode.querySelectorAll('.password-toggle').length, 1);
    assert.equal($('password').parentNode.querySelectorAll('.password-toggle').length, 1);
    const nested = w.document.createElement('div');
    nested.innerHTML = '<div><input id="deep-password" type="password"></div>';
    w.document.body.append(nested);
    await settled();
    assert.ok($('deep-password').parentNode.classList.contains('password-field'));
    // Browsers add their own eye to a password field. The style hides it.
    assert.match(read('password-field.css'), /::-ms-reveal[^{]*\{display:none\}/);
    assert.match(read('password-field.css'), /\.password-field \.password-toggle\{position:absolute;[^}]*right:4px/);
  } finally { t.close(); }
}

module.exports = async () => {
  staticChecks();
  await mainStage();
  await emailCodeWithConsent();
  await emailCodeExisting();
  await emailOff();
  await oldLogin();
  await recovery();
  await passwordChange();
  await noPasswordPopup();
  await eyeIcon();
  console.log('Sign-in checks passed: two buttons, email code with consent step, resend wait, paste, disabled email, old login, recovery request, password change, no-password popup, eye icon on every password field.');
};
if (require.main === module) module.exports().catch(error => { console.error(error); process.exitCode = 1; });
