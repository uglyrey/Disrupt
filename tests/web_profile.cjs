'use strict';
// «Знакомство», «Что beresta знает обо мне», the switch of the calendar offer, the suggested categories
// and the calendar icon of a reminder.
// There is no layout in jsdom. The checks look at attributes, text, focus and calls, not at pixels.
// The API of the profile is mocked after docs/profile-v1.md.
const assert = require('node:assert/strict'), fs = require('fs'), path = require('path'), {JSDOM} = require('jsdom');
const root = path.join(__dirname, '../app/static');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const reply = (data, status = 200) => ({ok: status < 400, status, headers: new Headers(), json: async () => structuredClone(data)});
async function settled() { for (let i = 0; i < 30; i++) await new Promise(resolve => setImmediate(resolve)); }
async function closeWindow(t) {
  for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve));
  t.dom.window.close();
}

const PRIVACY = 'Ответы хранятся в зашифрованном виде, в админке мы их не видим. ИИ использует их только для разбора ваших заметок. Всё можно посмотреть, исправить или удалить в разделе «Что beresta знает обо мне».';
const QUESTIONS = [
  'Чем вы сейчас занимаетесь? Учёба, работа, свои проекты, что угодно.',
  'Какие проекты, предметы и темы у вас сейчас главные?',
  'Как обычно идёт ваш день?',
  'Что для вас значит «утром», «днём», «вечером»?',
  'Какие дела вы чаще всего забываете или откладываете?',
  'Какими вам удобнее видеть заметки?',
  'Что ещё beresta стоит знать о вас, чтобы помогать лучше?',
].map((text, index) => ({id: `q${index + 1}`, text}));
const FACTS = [{id: 'f1', text: 'Учится в ГУАП, 3 курс'}, {id: 'f2', text: 'Вечером значит после 18:00'}];
const SUGGESTED = ['Учёба', 'Работа', 'Дом'];
const GENERAL = {id: 'g1', name: 'Общее', is_general: true};
const CALENDAR = 'https://calendar.google.com/calendar/render?action=TEMPLATE&text=%D0%97%D0%B2%D0%BE%D0%BD%D0%BE%D0%BA&dates=20901008T070000Z%2F20901008T073000Z';
const DRAFT_KEY = 'beresta.profile.draft.v1';

const seen = () => ({note_tools: {step: 0, done: true}, categories: {step: 0, done: true}});
const fresh = () => ({note_tools: {step: 0, done: false}, categories: {step: 0, done: false}});
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };

// `profile` overrides the server record, `me` overrides the answer of /auth/me, `gate` holds the sign-in back.
function boot({phone = false, profile = {}, me = {}, gate = null, guides, cookies = false, reminders = [], oldServer = false} = {}) {
  const dom = new JSDOM(read('index.html'), {url: 'https://beresta.invalid/', runScripts: 'outside-only'}), w = dom.window;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  try { Object.defineProperty(w.crypto, 'randomUUID', {value: require('node:crypto').randomUUID, configurable: true}); } catch { /* already provided */ }
  w.confirm = () => { throw new Error('the browser confirm() must not be used'); };
  w.noteAutosaveMs = 10; w.noteAutosaveRetryMs = 600000; w.guideDelayMs = 0;
  w.profileWaitMs = 5; w.profilePollMs = 5; w.profilePollMaxMs = 30;
  const env = {phone};
  w.matchMedia = query => ({
    media: query, matches: /max-width:\s*640px/.test(query) ? env.phone : false,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  });
  w.document.addEventListener('click', event => { if (event.target.closest && event.target.closest('a[href]')) event.preventDefault(); });
  const state = {
    calls: [], gets: 0, failures: {}, onGet: null, guides: guides || seen(), reminders,
    categories: [{...GENERAL}],
    profile: {state: 'new', questions: QUESTIONS, answers: null, facts: [], updated_at: null, calendar_offer: true,
      suggested_categories: [], categories_confirmed: true, intro: '', privacy: PRIVACY, error: null, ...profile},
    note: {id: 'n1', capture_id: 'k1', channel: 'web', input_kind: 'text', title: 'Заметка', markdown: 'Текст',
      original_text: 'Исходник', conclusions: [], version: 3, provider: 'manual', category_id: null,
      category_name: null, structure_confirmed_at: null, created_at: 1791220000, updated_at: 1791220000, items: []},
  };
  const summaries = [{id: 'n1', title: 'Заметка', version: 3, updated_at: 1791220000, category_id: null, channel: 'web', input_kind: 'text'}];
  w.fetch = async (input, options = {}) => {
    const url = new URL(input, w.location.href), method = options.method || 'GET';
    const body = options.body && typeof options.body === 'string' ? JSON.parse(options.body) : null;
    state.calls.push({url, method, body, headers: options.headers || {}});
    const p = url.pathname;
    if (p === '/health' || p === '/api/v1/provider/usage') return reply({simulation: true});
    if (p === '/api/v1/auth/me') {
      if (gate) await gate.promise;
      const user = {username: 'tester', csrf_token: 'csrf', policy_current: true,
        profile_state: state.profile.state, calendar_offer: state.profile.calendar_offer, ...me};
      if (oldServer) { delete user.profile_state; delete user.calendar_offer; }
      return reply(user);
    }
    if (p === '/api/v1/account/guides') return reply({guides: state.guides});
    if (p === '/api/v1/categories') return reply(state.categories);
    if (p === '/api/v1/jobs') return reply([]);
    if (p === '/api/v1/notes') return reply(summaries);
    if (p === '/api/v1/notes/n1/revisions') return reply([]);
    if (p === '/api/v1/notes/n1' && method === 'GET') return reply(state.note);
    if (p === '/api/v1/notes/n1/reminders') return reply(state.reminders);
    if (p === '/api/v1/telegram/links') return reply({identities: [], pending: []});
    if (p.endsWith('/opened') || p.endsWith('/original-opened') || p === '/api/v1/search/events') return reply(null, 204);
    const record = state.profile;
    if (p === '/api/v1/profile' && method === 'GET') {
      state.gets++;
      if (state.onGet) state.onGet(state.gets, record);
      return reply(record);
    }
    if (p === '/api/v1/profile' && method === 'DELETE') {
      Object.assign(record, {state: 'declined', facts: [], answers: null}); return reply({state: 'declined'});
    }
    if (p === '/api/v1/profile/answers' && method === 'POST') {
      if (state.failures.answers) return reply({detail: 'Лимит ИИ на сегодня исчерпан.'}, 429);
      Object.assign(record, {state: 'pending', answers: body.answers});
      return reply({state: 'pending'}, 202);
    }
    if (p === '/api/v1/profile/decline' && method === 'POST') {
      if (state.failures.decline) return reply({detail: 'Не удалось выполнить запрос'}, 500);
      record.state = 'declined'; return reply({state: 'declined'});
    }
    if (p === '/api/v1/profile/retry' && method === 'POST') { record.state = 'pending'; return reply({state: 'pending'}, 202); }
    if (p === '/api/v1/profile/calendar-offer' && method === 'PATCH') {
      if (state.failures.offer) return reply({detail: 'Не удалось выполнить запрос'}, 500);
      record.calendar_offer = body.calendar_offer; return reply({calendar_offer: body.calendar_offer});
    }
    if (p === '/api/v1/profile/categories' && method === 'POST') {
      if (state.failures.categories) return reply({detail: 'Не удалось выполнить запрос'}, 500);
      if (record.state !== 'ready') return reply({detail: 'Профиль ещё не готов'}, 409);
      const created = body.names.map((name, index) => ({id: `c${state.categories.length + index}`, name, is_general: false}));
      state.categories.push(...created);
      Object.assign(record, {categories_confirmed: true, suggested_categories: []});
      return reply({...record, created: body.names});
    }
    if (p === '/api/v1/profile/categories/skip' && method === 'POST') {
      if (state.failures.skip) return reply({detail: 'Не удалось выполнить запрос'}, 500);
      Object.assign(record, {categories_confirmed: true, suggested_categories: []}); return reply(record);
    }
    const fact = /^\/api\/v1\/profile\/facts\/([^/]+)$/.exec(p);
    if (fact && method === 'PATCH') {
      const row = record.facts.find(item => item.id === fact[1]); row.text = body.text; return reply(record);
    }
    if (fact && method === 'DELETE') { record.facts = record.facts.filter(item => item.id !== fact[1]); return reply(record); }
    throw new Error(`Unexpected ${method} ${p}`);
  };
  const $ = id => w.document.getElementById(id);
  $('bot-link').setAttribute('href', 'https://t.me/beresta_ru_bot');
  w.eval((cookies ? [read('cookie-consent.js')] : []).concat(['reminders.js', 'app.js', 'workspace.js', 'focus.js', 'md-editor.js', 'mobile-menu.js', 'profile.js', 'guide.js'].map(read)).join('\n')
    + '\nwindow.__api = api;');
  const wait = ms => new Promise(resolve => w.setTimeout(resolve, ms));
  const until = async (check, what) => { for (let i = 0; i < 400; i++) { if (check()) return; await wait(5); } throw new Error(`Timed out: ${what}`); };
  const profileCalls = (method, suffix = '') => state.calls.filter(c => c.method === method && c.url.pathname === `/api/v1/profile${suffix}`);
  return {dom, w, $, env, state, wait, until, profileCalls};
}

/* ---------- Helpers for the popup ---------- */
const popup = t => t.$('profile-dialog');
const action = (t, name, scope = popup(t)) => scope.querySelector(`[data-action="${name}"]`);
const press = (t, name) => { const node = action(t, name); assert.ok(node, `button ${name}`); node.click(); };
const typeInto = (t, node, text) => { node.value = text; node.dispatchEvent(new t.w.Event('input', {bubbles: true})); };
const answerBox = t => popup(t).querySelector('#profile-answer');
const progressText = t => popup(t).querySelector('.profile-count-text').textContent;
const title = t => popup(t).querySelector('#profile-title').textContent;
const readyPopup = async t => { await t.until(() => popup(t) && popup(t).open, 'popup'); };
const key = (t, target, init) => {
  const event = new t.w.KeyboardEvent('keydown', {bubbles: true, cancelable: true, ...init});
  target.dispatchEvent(event); return event;
};
const escape = (t) => {
  const cancel = new t.w.Event('cancel', {cancelable: true});
  popup(t).dispatchEvent(cancel);
  assert.equal(cancel.defaultPrevented, true, 'Esc is handled by the popup, the browser does not close it');
};
async function toLastQuestion(t) {
  press(t, 'start');
  for (let i = 1; i < QUESTIONS.length; i++) press(t, 'next');
  assert.equal(progressText(t), `${QUESTIONS.length} из ${QUESTIONS.length}`);
}
const meNew = {username: 'tester', csrf_token: 'csrf', policy_current: true, profile_state: 'new', calendar_offer: true};

/* ---------- When the popup shows ---------- */
async function onlyForNew() {
  for (const profile_state of ['declined', 'pending', 'ready', 'failed']) {
    const t = boot({profile: {state: profile_state}});
    try {
      await settled(); await t.wait(40);
      assert.equal(popup(t), null, profile_state);
      // A ready profile is read once, to know whether the categories wait for an answer.
      assert.equal(t.profileCalls('GET').length, profile_state === 'ready' ? 1 : 0, `${profile_state}: requests`);
      assert.equal(t.w.BerestaProfile.state(), profile_state);
      assert.equal(t.w.BerestaProfile.pending(), false);
    } finally { await closeWindow(t); }
  }
  // An old server does not send the field. Nothing shows.
  const old = boot({oldServer: true});
  try { await settled(); await old.wait(40); assert.equal(popup(old), null); } finally { await closeWindow(old); }
  // No questions, no popup (the text lives on the server).
  const empty = boot({profile: {questions: []}});
  try { await settled(); await empty.wait(40); assert.equal(popup(empty), null); } finally { await closeWindow(empty); }
  // The server failing does not stop the app.
  const broken = boot();
  try {
    const original = broken.w.fetch;
    broken.w.fetch = async (input, options) => { if (String(input) === '/api/v1/profile') return reply({detail: 'Не удалось выполнить запрос'}, 500); return original(input, options); };
    await broken.w.BerestaProfile.start(meNew, broken.w.__api); await broken.wait(40);
    assert.equal(popup(broken), null);
  } finally { await closeWindow(broken); }

  const t = boot();
  try {
    await readyPopup(t);
    const dialog = popup(t);
    assert.equal(dialog.tagName, 'DIALOG');
    assert.equal(dialog.getAttribute('aria-labelledby'), 'profile-title');
    assert.equal(title(t), 'Давайте познакомимся');
    assert.equal(t.profileCalls('GET').length, 1);
    assert.match(dialog.querySelector('.profile-lead').textContent, /подстроиться под вас/);
    assert.equal(dialog.querySelector('.profile-privacy').textContent, PRIVACY);
    assert.ok(!/мы ничего не видим/i.test(dialog.textContent));
    assert.deepEqual([...dialog.querySelectorAll('.profile-foot button')].map(b => b.textContent), ['Начать', 'Нет, спасибо']);
    assert.equal(dialog.querySelector('.profile-progress').hidden, true, 'no progress on the intro');
    assert.equal(t.w.document.activeElement, action(t, 'start'));
    assert.equal(t.w.BerestaProfile.pending(), true);
    // Everything is DOM nodes and plain text.
    for (const name of ['profile.js']) assert.ok(!/innerHTML|insertAdjacentHTML|outerHTML|\.style\.cssText|setAttribute\(['"]style|<script|eval\(/.test(read(name)), name);
    // It shows once per load: closing it by hand does not bring it back.
    press(t, 'start'); dialogClose(t); await t.wait(40);
    assert.equal(popup(t), null);
    assert.equal(t.w.BerestaProfile.pending(), false);
  } finally { await closeWindow(t); }
}
function dialogClose(t) { popup(t).querySelector('.profile-close').click(); }

async function waitsForOthers() {
  // Another dialog is open while the person gets in: the popup waits for it.
  const gate = deferred(), t = boot({gate});
  try {
    t.$('password-dialog').showModal();
    gate.resolve(); await settled(); await t.wait(40);
    assert.equal(popup(t), null, 'not on top of another dialog');
    assert.equal(t.w.BerestaProfile.pending(), true, 'but it is on its way');
    t.$('password-dialog').close();
    await readyPopup(t);
    assert.equal(t.$('password-dialog').open, false);
  } finally { await closeWindow(t); }

  // The policy banner comes first.
  const policy = boot({me: {policy_current: false}});
  try {
    await settled(); await policy.wait(40);
    assert.equal(policy.$('policy-banner').hidden, false);
    assert.equal(popup(policy), null, 'waits for the policy');
    policy.$('policy-banner').hidden = true;
    await readyPopup(policy);
  } finally { await closeWindow(policy); }

  // The cookie bar comes first.
  const cookies = boot({cookies: true});
  try {
    await settled(); await cookies.wait(40);
    assert.ok(cookies.$('cookie-consent'), 'the bar is there');
    assert.equal(popup(cookies), null, 'waits for the cookie choice');
    cookies.$('cookie-necessary').click();
    await readyPopup(cookies);
  } finally { await closeWindow(cookies); }

  // An open tool popover of a note is not covered either.
  const pop = boot();
  try {
    pop.w.document.body.classList.add('has-pop');
    await settled(); await pop.wait(40);
    assert.equal(popup(pop), null);
    pop.w.document.body.classList.remove('has-pop');
    await readyPopup(pop);
  } finally { await closeWindow(pop); }

  // Signing out clears everything: nothing is left open, nothing waits.
  const out = boot();
  try {
    await readyPopup(out);
    out.$('workspace').hidden = true; await settled();
    assert.equal(popup(out), null);
    assert.equal(out.w.BerestaProfile.pending(), false);
  } finally { await closeWindow(out); }
}

async function guideOrder() {
  // The tip waits for the popup.
  const t = boot({guides: fresh()});
  try {
    await readyPopup(t);
    await settled(); t.$('notes').querySelector('button').click(); await settled();
    assert.equal(t.$('title').value, 'Заметка');
    await t.wait(80);
    assert.equal(t.w.document.querySelector('.guide-bubble'), null, 'no tip while the popup is open');
    press(t, 'start'); dialogClose(t);
    await t.until(() => t.w.document.querySelector('.guide-bubble'), 'tip after the popup');
  } finally { await closeWindow(t); }

  // The popup waits for a tip that is already on the screen.
  const later = boot({guides: fresh(), profile: {state: 'declined'}});
  try {
    await settled(); later.$('notes').querySelector('button').click(); await settled();
    await later.until(() => later.w.document.querySelector('.guide-bubble'), 'tip');
    later.state.profile.state = 'new';
    await later.w.BerestaProfile.start(meNew, later.w.__api);
    await later.wait(40);
    assert.equal(popup(later), null, 'not on top of a tip');
    later.w.BerestaGuide.close();
    await readyPopup(later);
  } finally { await closeWindow(later); }
}

/* ---------- Decline ---------- */
async function declines() {
  // «Нет, спасибо»
  const t = boot();
  try {
    await readyPopup(t);
    press(t, 'decline');
    await t.until(() => popup(t) === null, 'closed');
    const post = t.profileCalls('POST', '/decline');
    assert.equal(post.length, 1);
    assert.equal(post[0].headers['X-CSRF-Token'], 'csrf');
    assert.equal(t.w.BerestaProfile.state(), 'declined');
    assert.equal(t.profileCalls('POST', '/answers').length, 0);
    // Never again: the next load says «declined», and nothing shows.
    t.state.profile.state = 'declined';
    await t.w.BerestaProfile.start({...meNew, profile_state: 'declined'}, t.w.__api); await t.wait(40);
    assert.equal(popup(t), null);
  } finally { await closeWindow(t); }

  // Esc and the cross on the first screen: a small question first, no browser confirm.
  const e = boot();
  try {
    await readyPopup(e);
    escape(e);
    assert.match(popup(e).textContent, /Больше не показывать\?/);
    assert.equal(e.profileCalls('POST', '/decline').length, 0, 'nothing is sent before the answer');
    assert.deepEqual([...popup(e).querySelectorAll('.profile-confirm button')].map(b => b.textContent), ['Да', 'Вернуться']);
    assert.equal(e.w.document.activeElement, action(e, 'confirm-back'), 'the safe choice has the focus');
    escape(e); assert.ok(action(e, 'confirm-decline'), 'a second Esc changes nothing');
    assert.equal(e.profileCalls('POST', '/decline').length, 0);
    // The Escape key itself is handled the same way.
    press(e, 'confirm-back');
    assert.ok(action(e, 'start'), 'back to the intro');
    assert.equal(popup(e).querySelector('.profile-confirm'), null);
    const keyEvent = key(e, popup(e), {key: 'Escape'});
    assert.equal(keyEvent.defaultPrevented, true);
    assert.match(popup(e).textContent, /Больше не показывать\?/);
    press(e, 'confirm-back');
    // The cross.
    dialogClose(e);
    assert.match(popup(e).textContent, /Больше не показывать\?/);
    press(e, 'confirm-decline');
    await e.until(() => popup(e) === null, 'closed after yes');
    assert.equal(e.profileCalls('POST', '/decline').length, 1);
    assert.equal(e.w.BerestaProfile.state(), 'declined');
  } finally { await closeWindow(e); }

  // A failing server keeps the popup open and says so.
  const f = boot();
  try {
    await readyPopup(f);
    f.state.failures.decline = true;
    press(f, 'decline'); await f.until(() => popup(f).querySelector('.profile-status').textContent !== 'Сохраняем…', 'error');
    assert.ok(popup(f).querySelector('.profile-status').textContent.length > 0);
    assert.ok(popup(f), 'still open');
    assert.equal(f.w.BerestaProfile.state(), 'new');
    for (const node of popup(f).querySelectorAll('.profile-foot button')) assert.equal(node.disabled, false);
  } finally { await closeWindow(f); }

  // On the next steps Esc only puts the popup off. Nothing is declined, the answers stay.
  const later = boot();
  try {
    await readyPopup(later);
    press(later, 'start'); typeInto(later, answerBox(later), 'Учусь');
    escape(later);
    assert.equal(popup(later), null);
    assert.equal(later.profileCalls('POST', '/decline').length, 0);
    assert.equal(later.w.BerestaProfile.state(), 'new');
    assert.deepEqual(JSON.parse(later.w.sessionStorage.getItem(DRAFT_KEY)).answers, {q1: 'Учусь'});
  } finally { await closeWindow(later); }
}

/* ---------- The questions ---------- */
async function fullFlow() {
  const t = boot();
  try {
    await readyPopup(t);
    press(t, 'start');
    assert.equal(title(t), 'Знакомство');
    assert.equal(progressText(t), '1 из 7');
    const bar = popup(t).querySelector('progress');
    assert.equal(bar.max, 7); assert.equal(bar.value, 1);
    assert.equal(popup(t).querySelector('.profile-progress').hidden, false);
    assert.equal(popup(t).querySelector('label[for="profile-answer"]').textContent, QUESTIONS[0].text);
    assert.equal(answerBox(t).maxLength, 1000);
    assert.equal(t.w.document.activeElement, answerBox(t));
    assert.deepEqual([...popup(t).querySelectorAll('.profile-foot button')].map(b => b.textContent), ['Назад', 'Пропустить', 'Далее']);
    typeInto(t, answerBox(t), '  Учусь в ГУАП  ');
    press(t, 'next');
    assert.equal(progressText(t), '2 из 7'); assert.equal(bar.value, 2);
    assert.equal(popup(t).querySelector('label[for="profile-answer"]').textContent, QUESTIONS[1].text);
    typeInto(t, answerBox(t), 'Диплом, кастдев');
    press(t, 'next');
    assert.equal(progressText(t), '3 из 7');
    // Skip clears what is on this step.
    typeInto(t, answerBox(t), 'это не нужно');
    press(t, 'skip');
    assert.equal(progressText(t), '4 из 7');
    // Back brings the earlier text, and the skipped step is empty.
    press(t, 'back');
    assert.equal(progressText(t), '3 из 7'); assert.equal(answerBox(t).value, '');
    press(t, 'back'); assert.equal(answerBox(t).value, 'Диплом, кастдев');
    press(t, 'next'); press(t, 'next');
    // The counter shows near the limit only.
    const counter = () => popup(t).querySelector('.profile-counter');
    assert.equal(counter().hidden, true);
    typeInto(t, answerBox(t), 'ж'.repeat(850));
    assert.equal(counter().hidden, false); assert.match(counter().textContent, /150/);
    typeInto(t, answerBox(t), 'ж'.repeat(1200));
    assert.match(counter().textContent, /\b0\b/);
    press(t, 'next'); press(t, 'next'); press(t, 'next');
    assert.equal(progressText(t), '7 из 7');
    assert.equal(popup(t).querySelector('.profile-progress progress').value, 7);
    // The last question has «Готово» and no other step after it: no channel, no radio.
    assert.equal(popup(t).querySelectorAll('input[type="radio"]').length, 0);
    assert.deepEqual([...popup(t).querySelectorAll('.profile-foot button')].map(b => b.textContent), ['Назад', 'Пропустить', 'Готово']);
    assert.ok(!/Telegram|Календар/.test(popup(t).textContent), 'no channel text in the popup');
    press(t, 'back'); assert.equal(progressText(t), '6 из 7'); press(t, 'next');
    press(t, 'done');
    await t.until(() => /Спасибо/.test(popup(t).textContent), 'final screen');
    const posts = t.profileCalls('POST', '/answers');
    assert.equal(posts.length, 1);
    assert.equal(posts[0].headers['X-CSRF-Token'], 'csrf');
    assert.deepEqual(posts[0].body, {answers: {q1: 'Учусь в ГУАП', q2: 'Диплом, кастдев', q4: 'ж'.repeat(1000)}});
    // A calm last screen.
    assert.ok(popup(t).textContent.includes('Спасибо! beresta разбирает ответы, это займёт до минуты.'));
    assert.equal(popup(t).querySelector('.profile-progress').hidden, true);
    assert.deepEqual([...popup(t).querySelectorAll('.profile-foot button')].map(b => b.textContent), ['Закрыть']);
    assert.equal(t.w.BerestaProfile.state(), 'pending');
    assert.equal(t.w.sessionStorage.getItem(DRAFT_KEY) || '', '', 'the draft is cleared after sending');
    press(t, 'close');
    assert.equal(popup(t), null);
    assert.equal(t.w.BerestaProfile.pending(), false);
  } finally { await closeWindow(t); }
}

async function emptySubmit() {
  const t = boot();
  try {
    await readyPopup(t);
    await toLastQuestion(t);
    press(t, 'done');
    assert.equal(t.profileCalls('POST', '/answers').length, 0);
    const warning = popup(t).querySelector('.profile-warning');
    assert.ok(warning); assert.equal(warning.getAttribute('role'), 'alert');
    assert.match(warning.textContent, /хотя бы один ответ/);
    assert.ok(action(t, 'decline') && action(t, 'to-questions'));
    // Spaces are not an answer either.
    press(t, 'to-questions'); assert.equal(progressText(t), '1 из 7');
    typeInto(t, answerBox(t), '   \n  ');
    for (let i = 1; i < 7; i++) press(t, 'next');
    press(t, 'done');
    assert.ok(popup(t).querySelector('.profile-warning'));
    assert.equal(t.profileCalls('POST', '/answers').length, 0);
    // The other way out: «Нет, спасибо».
    press(t, 'decline');
    await t.until(() => popup(t) === null, 'closed');
    assert.equal(t.profileCalls('POST', '/decline').length, 1);
  } finally { await closeWindow(t); }
}

async function errorsAndText() {
  const t = boot();
  try {
    await readyPopup(t);
    press(t, 'start'); typeInto(t, answerBox(t), 'Учусь');
    for (let i = 1; i < 7; i++) press(t, 'next');
    t.state.failures.answers = true;
    press(t, 'done');
    await t.until(() => popup(t).querySelector('.profile-status').textContent === 'Лимит ИИ на сегодня исчерпан.', 'limit message');
    assert.equal(progressText(t), '7 из 7', 'still on the last question');
    assert.deepEqual(JSON.parse(t.w.sessionStorage.getItem(DRAFT_KEY)).answers, {q1: 'Учусь'}, 'the answers are kept');
    for (const node of popup(t).querySelectorAll('.profile-foot button')) assert.equal(node.disabled, false);
    t.state.failures.answers = false;
    press(t, 'done');
    await t.until(() => /Спасибо/.test(popup(t).textContent), 'sent on the second try');
    assert.equal(t.profileCalls('POST', '/answers').length, 2);
  } finally { await closeWindow(t); }

  // The texts of the server win, and they are text, not markup.
  const s = boot({profile: {intro: 'Привет <b>друг</b>', privacy: 'Другая строка про данные'}});
  try {
    await readyPopup(s);
    assert.equal(popup(s).querySelector('.profile-lead').textContent, 'Привет <b>друг</b>');
    assert.equal(popup(s).querySelector('.profile-privacy').textContent, 'Другая строка про данные');
    assert.equal(popup(s).querySelector('b'), null);
  } finally { await closeWindow(s); }

  // The questions come from the server in its order, with its text.
  const q = boot({profile: {questions: [{id: 'a1', text: 'Первый <i>вопрос</i>'}, {id: 'a2', text: 'Второй'}]}});
  try {
    await readyPopup(q);
    press(q, 'start');
    assert.equal(progressText(q), '1 из 2');
    assert.equal(popup(q).querySelector('label[for="profile-answer"]').textContent, 'Первый <i>вопрос</i>');
    assert.equal(popup(q).querySelector('i'), null);
    typeInto(q, answerBox(q), 'ответ'); press(q, 'next'); press(q, 'done');
    await q.until(() => q.profileCalls('POST', '/answers').length === 1, 'sent');
    assert.deepEqual(q.profileCalls('POST', '/answers')[0].body, {answers: {a1: 'ответ'}});
  } finally { await closeWindow(q); }

  // Tab stays inside the popup.
  const f = boot();
  try {
    await readyPopup(f);
    const buttons = [...popup(f).querySelectorAll('button')];
    buttons.at(-1).focus();
    const forward = key(f, popup(f), {key: 'Tab'});
    assert.equal(forward.defaultPrevented, true); assert.equal(f.w.document.activeElement, buttons[0]);
    const backward = key(f, popup(f), {key: 'Tab', shiftKey: true});
    assert.equal(backward.defaultPrevented, true); assert.equal(f.w.document.activeElement, buttons.at(-1));
  } finally { await closeWindow(f); }
}

async function draftRestore() {
  const t = boot();
  try {
    await readyPopup(t);
    press(t, 'start'); typeInto(t, answerBox(t), 'Первое');
    press(t, 'next'); typeInto(t, answerBox(t), 'Второе');
    let saved = JSON.parse(t.w.sessionStorage.getItem(DRAFT_KEY));
    assert.deepEqual(saved, {answers: {q1: 'Первое', q2: 'Второе'}});
    for (let i = 2; i < 7; i++) press(t, 'next');
    saved = JSON.parse(t.w.sessionStorage.getItem(DRAFT_KEY));
    assert.deepEqual(saved, {answers: {q1: 'Первое', q2: 'Второе'}}, 'no channel in the draft');
    dialogClose(t);
    assert.equal(popup(t), null);
    assert.equal(t.profileCalls('POST').length, 0, 'closing sends nothing');
    // A reload: the page starts again, the answers come back.
    await t.w.BerestaProfile.start(meNew, t.w.__api);
    await readyPopup(t);
    press(t, 'start');
    assert.equal(answerBox(t).value, 'Первое');
    press(t, 'next'); assert.equal(answerBox(t).value, 'Второе');
    for (let i = 2; i < 7; i++) press(t, 'next');
    press(t, 'done');
    await t.until(() => t.profileCalls('POST', '/answers').length === 1, 'sent');
    assert.deepEqual(t.profileCalls('POST', '/answers')[0].body, {answers: {q1: 'Первое', q2: 'Второе'}});
    await t.until(() => /Спасибо/.test(popup(t).textContent), 'final');
    assert.equal(t.w.sessionStorage.getItem(DRAFT_KEY) || '', '');
  } finally { await closeWindow(t); }

  // The consent-aware storage is used when it is there; a broken storage does not stop the form.
  const consent = boot({cookies: true});
  try {
    consent.$('cookie-necessary').click();
    await readyPopup(consent);
    press(consent, 'start'); typeInto(consent, answerBox(consent), 'Тест');
    assert.equal(consent.w.localStorage.getItem(DRAFT_KEY), null, 'nothing in localStorage without «Принять все»');
    assert.match(consent.w.berestaConsent.read(DRAFT_KEY), /Тест/);
  } finally { await closeWindow(consent); }
  const blocked = boot();
  try {
    Object.defineProperty(blocked.w, 'sessionStorage', {get() { throw new Error('blocked'); }, configurable: true});
    await readyPopup(blocked);
    press(blocked, 'start'); typeInto(blocked, answerBox(blocked), 'Тест'); press(blocked, 'next'); press(blocked, 'back');
    assert.equal(answerBox(blocked).value, 'Тест');
  } finally { await closeWindow(blocked); }
  assert.ok(/beresta\.profile\.draft\.v1/.test(read('cookie-consent.js')), 'the draft is cleared when optional storage is refused');
}

/* ---------- «Что beresta знает обо мне» ---------- */
const knowledge = t => t.$('knowledge-dialog');
const facts = t => [...knowledge(t).querySelectorAll('.profile-fact')];
const offerSwitch = t => knowledge(t).querySelector('#profile-calendar-offer');
const openKnowledge = async (t, ready = '#profile-calendar-offer') => {
  // The sign-in of the page has to be over: start() resets the profile and would close a dialog opened too early.
  await settled(); await t.until(() => t.w.BerestaProfile.state() !== null, 'profile state');
  const entry = t.$('open-profile'); assert.ok(entry, 'menu entry');
  entry.click();
  await t.until(() => knowledge(t) && knowledge(t).querySelector(ready), 'knowledge dialog');
};

async function menuEntries() {
  const t = boot({profile: {state: 'declined'}});
  try {
    await settled();
    const entry = t.$('open-profile');
    assert.ok(entry && entry.closest('#nav-menu .nav-bottom'), 'in the desktop menu');
    assert.equal(entry.textContent, 'Что beresta знает обо мне');
    assert.equal(entry.type, 'button');
    const phone = t.w.document.querySelector('#general-menu [data-proxy="open-profile"]');
    assert.ok(phone, 'in the phone menu');
    assert.equal(phone.textContent, 'Что beresta знает обо мне');
    assert.ok(phone.closest('.menu-group'));
    await openKnowledge(t);
    assert.equal(knowledge(t).getAttribute('aria-labelledby'), 'knowledge-title');
    assert.equal(knowledge(t).querySelector('#knowledge-title').textContent, 'Что beresta знает обо мне');
  } finally { await closeWindow(t); }

  // The phone menu opens the same dialog.
  const p = boot({phone: true, profile: {state: 'ready', facts: FACTS}});
  try {
    await settled(); p.$('main-nav').hidden = false; await settled();
    p.$('menu-button').click();
    p.w.document.querySelector('#general-menu [data-proxy="open-profile"]').click();
    assert.equal(p.$('general-menu').hidden, true, 'the menu closes');
    await p.until(() => facts(p).length === 2, 'facts');
    knowledge(p).querySelector('.profile-close').click();
    assert.equal(knowledge(p), null);
  } finally { await closeWindow(p); }
}

async function knowledgeStates() {
  // ready
  const r = boot({profile: {state: 'ready', facts: FACTS, answers: {q1: 'Учусь'}}});
  try {
    await settled(); await r.wait(30);
    assert.equal(popup(r), null, 'no popup for a ready profile');
    await openKnowledge(r);
    assert.deepEqual(facts(r).map(li => li.querySelector('.profile-fact-text').textContent), FACTS.map(f => f.text));
    for (const li of facts(r)) assert.deepEqual([...li.querySelectorAll('button')].map(b => b.textContent), ['Изменить', 'Удалить']);
    assert.ok(action(r, 'delete-all', knowledge(r)) && action(r, 'retake', knowledge(r)));
    assert.equal(knowledge(r).querySelector('.profile-privacy').textContent, PRIVACY);
    assert.equal(knowledge(r).querySelectorAll('input[type="radio"]').length, 0, 'no channel choice');
    assert.equal(offerSwitch(r).checked, true);
    // Edit
    facts(r)[0].querySelector('[data-action="edit"]').click();
    let area = facts(r)[0].querySelector('textarea');
    assert.equal(area.value, FACTS[0].text); assert.equal(area.maxLength, 500);
    typeInto(r, area, '   ');
    action(r, 'save', knowledge(r)).click();
    assert.equal(r.profileCalls('PATCH', '/facts/f1').length, 0, 'empty text is not sent');
    assert.match(knowledge(r).querySelector('.profile-status').textContent, /не может быть пустым/);
    action(r, 'cancel', knowledge(r)).click();
    assert.equal(facts(r)[0].querySelector('.profile-fact-text').textContent, FACTS[0].text, 'cancel keeps the text');
    facts(r)[0].querySelector('[data-action="edit"]').click();
    area = facts(r)[0].querySelector('textarea');
    typeInto(r, area, 'Учится в ГУАП, 4 курс');
    action(r, 'save', knowledge(r)).click();
    await r.until(() => r.profileCalls('PATCH', '/facts/f1').length === 1 && facts(r)[0].querySelector('.profile-fact-text'), 'saved');
    const patch = r.profileCalls('PATCH', '/facts/f1')[0];
    assert.deepEqual(patch.body, {text: 'Учится в ГУАП, 4 курс'}); assert.equal(patch.headers['X-CSRF-Token'], 'csrf');
    assert.equal(facts(r)[0].querySelector('.profile-fact-text').textContent, 'Учится в ГУАП, 4 курс');
    assert.equal(knowledge(r).querySelector('.profile-status').textContent, 'Сохранено.');
    // Delete one
    facts(r)[0].querySelector('[data-action="delete"]').click();
    await r.until(() => facts(r).length === 1, 'one fact left');
    assert.equal(r.profileCalls('DELETE', '/facts/f1').length, 1);
    assert.equal(facts(r)[0].dataset.id, 'f2');
    // Delete the last one: an empty list says so.
    facts(r)[0].querySelector('[data-action="delete"]').click();
    await r.until(() => facts(r).length === 0, 'no facts');
    assert.match(knowledge(r).textContent, /Пока здесь нет ни одного вывода/);
    assert.ok(action(r, 'retake', knowledge(r)));
  } finally { await closeWindow(r); }

  // Delete everything: asked inside the dialog, no browser confirm.
  const d = boot({profile: {state: 'ready', facts: FACTS, answers: {q1: 'Учусь'}}});
  try {
    await openKnowledge(d, '.profile-fact');
    action(d, 'delete-all', knowledge(d)).click();
    assert.match(knowledge(d).textContent, /Удалить все ответы и выводы/);
    assert.equal(d.profileCalls('DELETE').length, 0, 'asked first');
    assert.equal(d.w.document.activeElement, action(d, 'cancel-delete-all', knowledge(d)));
    action(d, 'cancel-delete-all', knowledge(d)).click();
    assert.equal(knowledge(d).querySelector('.profile-confirm'), null);
    assert.equal(facts(d).length, 2);
    action(d, 'delete-all', knowledge(d)).click();
    action(d, 'confirm-delete-all', knowledge(d)).click();
    await d.until(() => facts(d).length === 0 && action(d, 'start', knowledge(d)), 'declined view');
    assert.equal(d.profileCalls('DELETE', '').filter(c => c.url.pathname === '/api/v1/profile').length, 1);
    assert.equal(d.w.BerestaProfile.state(), 'declined');
    assert.match(knowledge(d).textContent, /Вы пока не отвечали на вопросы/);
    assert.equal(d.$('profile-dialog'), null, 'the popup does not come back');
  } finally { await closeWindow(d); }

  // Retake: the popup opens with the old answers in it, the old dialog is gone.
  const k = boot({profile: {state: 'ready', facts: FACTS, answers: {q1: 'Учусь', q3: 'Встаю в семь'}}});
  try {
    await openKnowledge(k, '.profile-fact');
    action(k, 'retake', knowledge(k)).click();
    assert.equal(knowledge(k), null);
    await readyPopup(k);
    assert.equal(title(k), 'Давайте познакомимся');
    press(k, 'start'); assert.equal(answerBox(k).value, 'Учусь');
    press(k, 'next'); press(k, 'next'); assert.equal(answerBox(k).value, 'Встаю в семь');
    // A manual popup closes at once from the first screen, no question about «не показывать».
    press(k, 'back'); press(k, 'back'); press(k, 'back');
    assert.ok(action(k, 'start'));
    escape(k);
    assert.equal(popup(k), null);
    assert.equal(k.profileCalls('POST', '/decline').length, 0);
  } finally { await closeWindow(k); }

  // Declined or never answered: a short text and the way in.
  for (const profile_state of ['declined']) {
    const n = boot({profile: {state: profile_state}});
    try {
      await openKnowledge(n);
      assert.match(knowledge(n).textContent, /Вы пока не отвечали на вопросы/);
      assert.equal(facts(n).length, 0);
      action(n, 'start', knowledge(n)).click();
      assert.equal(knowledge(n), null);
      await readyPopup(n);
      escape(n); assert.equal(popup(n), null);
    } finally { await closeWindow(n); }
  }

  // The state is still «new» (a person who closed the popup): the same text.
  const nw = boot();
  try {
    await readyPopup(nw);
    press(nw, 'start'); dialogClose(nw);
    await openKnowledge(nw);
    assert.match(knowledge(nw).textContent, /Вы пока не отвечали на вопросы/);
    action(nw, 'start', knowledge(nw)).click();
    await readyPopup(nw);
  } finally { await closeWindow(nw); }
}

async function knowledgePending() {
  // pending → ready while the dialog is open
  const t = boot({profile: {state: 'pending'}});
  try {
    t.state.onGet = (count, record) => { if (count >= 3) Object.assign(record, {state: 'ready', facts: FACTS}); };
    await openKnowledge(t, '.profile-know-body .profile-lead');
    assert.equal(t.$('knowledge-dialog').querySelector('.profile-know-body .profile-lead').textContent, 'Разбираем ответы…');
    assert.equal(facts(t).length, 0);
    await t.until(() => facts(t).length === 2, 'facts after polling');
    const count = t.state.gets;
    assert.ok(count >= 3);
    await t.wait(60);
    assert.equal(t.state.gets, count, 'polling stopped when the state changed');
  } finally { await closeWindow(t); }

  // pending for good: polling stops after the limit and offers a manual check
  const slow = boot({profile: {state: 'pending'}});
  try {
    await openKnowledge(slow, '.profile-know-body .profile-lead');
    await slow.until(() => action(slow, 'check', knowledge(slow)), 'slow text');
    assert.match(knowledge(slow).textContent, /дольше обычного/);
    const count = slow.state.gets;
    assert.ok(count >= 2 && count <= 8, `a limited number of requests (${count})`);
    await slow.wait(60);
    assert.equal(slow.state.gets, count, 'no polling after the limit');
    Object.assign(slow.state.profile, {state: 'ready', facts: FACTS});
    action(slow, 'check', knowledge(slow)).click();
    await slow.until(() => facts(slow).length === 2, 'facts after a manual check');
  } finally { await closeWindow(slow); }

  // closing the dialog stops the polling
  const closed = boot({profile: {state: 'pending'}});
  try {
    await openKnowledge(closed, '.profile-know-body .profile-lead');
    knowledge(closed).querySelector('.profile-close').click();
    assert.equal(knowledge(closed), null);
    await closed.wait(10);
    const count = closed.state.gets;
    await closed.wait(60);
    assert.equal(closed.state.gets, count, 'no requests after closing');
  } finally { await closeWindow(closed); }

  // failed → retry → pending → ready
  const failed = boot({profile: {state: 'failed', answers: {q1: 'Учусь'}}});
  try {
    await openKnowledge(failed, '[data-action="retry"]');
    assert.match(knowledge(failed).textContent, /Не получилось разобрать ответы/);
    failed.state.onGet = (count, record) => { if (record.state === 'pending' && count >= 3) Object.assign(record, {state: 'ready', facts: FACTS}); };
    action(failed, 'retry', knowledge(failed)).click();
    await failed.until(() => failed.profileCalls('POST', '/retry').length === 1, 'retry sent');
    assert.equal(failed.profileCalls('POST', '/retry')[0].headers['X-CSRF-Token'], 'csrf');
    await failed.until(() => /Разбираем ответы/.test(knowledge(failed).textContent) || facts(failed).length, 'pending text');
    await failed.until(() => facts(failed).length === 2, 'facts');
  } finally { await closeWindow(failed); }
}

async function calendarOfferSwitch() {
  const t = boot({profile: {state: 'ready', facts: FACTS, calendar_offer: true}});
  try {
    await openKnowledge(t, '.profile-fact');
    const box = offerSwitch(t);
    assert.equal(box.type, 'checkbox'); assert.equal(box.getAttribute('role'), 'switch');
    assert.equal(box.closest('label').textContent.trim(), 'Предлагать Google Календарь');
    assert.ok(box.closest('label').classList.contains('switch'));
    assert.equal(knowledge(t).querySelectorAll('#profile-calendar-offer').length, 1, 'a single switch');
    assert.equal(box.checked, true);
    assert.equal(t.w.BerestaProfile.calendarOffer(), true);
    box.click();
    await t.until(() => t.profileCalls('PATCH', '/calendar-offer').length === 1, 'patch');
    const patch = t.profileCalls('PATCH', '/calendar-offer')[0];
    assert.deepEqual(patch.body, {calendar_offer: false}); assert.equal(patch.headers['X-CSRF-Token'], 'csrf');
    await t.until(() => knowledge(t).querySelector('.profile-status').textContent === 'Сохранено.', 'saved');
    assert.equal(t.w.BerestaProfile.calendarOffer(), false);
    assert.equal(t.state.profile.calendar_offer, false);
    // A failure puts the old value back and says so.
    t.state.failures.offer = true;
    box.click();
    await t.until(() => knowledge(t).querySelector('.profile-status').textContent !== 'Сохранено.' && knowledge(t).querySelector('.profile-status').textContent !== 'Сохраняем…', 'error');
    assert.equal(box.checked, false, 'reverted');
    assert.equal(t.w.BerestaProfile.calendarOffer(), false);
    // And it works again afterwards.
    t.state.failures.offer = false;
    box.click();
    await t.until(() => t.profileCalls('PATCH', '/calendar-offer').length === 3, 'third patch');
    assert.deepEqual(t.profileCalls('PATCH', '/calendar-offer')[2].body, {calendar_offer: true});
  } finally { await closeWindow(t); }

  // The start value comes from the account.
  const off = boot({profile: {state: 'ready', facts: FACTS, calendar_offer: false}});
  try {
    await openKnowledge(off, '.profile-fact');
    assert.equal(offerSwitch(off).checked, false);
    assert.equal(off.w.BerestaProfile.calendarOffer(), false);
  } finally { await closeWindow(off); }

  // The switch is there in every state, also for a person who has not answered.
  for (const profile_state of ['declined', 'pending', 'failed']) {
    const n = boot({profile: {state: profile_state}});
    try { await openKnowledge(n); assert.ok(offerSwitch(n), profile_state); } finally { await closeWindow(n); }
  }
}

/* ---------- Suggested categories ---------- */
const SUGGEST = {state: 'ready', facts: FACTS, suggested_categories: SUGGESTED, categories_confirmed: false};
const categoriesBox = t => t.w.document.querySelector('.profile-categories');
const categoryRows = t => [...categoriesBox(t).querySelectorAll('.profile-category-row')];
const bar = t => t.w.document.querySelector('.profile-bar');

async function quietBar() {
  const t = boot({profile: SUGGEST});
  try {
    await t.until(() => bar(t), 'the quiet bar');
    assert.equal(popup(t), null, 'no second big popup');
    assert.equal(knowledge(t), null);
    assert.equal(bar(t).querySelector('p').textContent, 'beresta предлагает категории под вас');
    assert.deepEqual([...bar(t).querySelectorAll('button')].map(b => b.textContent), ['Посмотреть', 'Позже']);
    assert.equal(t.profileCalls('GET').length, 1);
    // «Позже» only hides it.
    action(t, 'bar-later', bar(t)).click();
    assert.equal(bar(t), null);
    assert.equal(t.profileCalls('POST').length, 0, 'nothing is sent');
    // The dialog still has the categories.
    await openKnowledge(t, '.profile-fact');
    assert.ok(categoriesBox(t), 'the categories wait in the dialog');
  } finally { await closeWindow(t); }

  // «Посмотреть» opens the dialog, and the bar goes away.
  const o = boot({profile: SUGGEST});
  try {
    await o.until(() => bar(o), 'bar');
    action(o, 'bar-open', bar(o)).click();
    assert.equal(bar(o), null);
    await o.until(() => knowledge(o) && categoriesBox(o), 'dialog with categories');
  } finally { await closeWindow(o); }

  // Nothing to show: no bar.
  const cases = {
    'confirmed': {state: 'ready', facts: FACTS, suggested_categories: SUGGESTED, categories_confirmed: true},
    'no suggestions': {state: 'ready', facts: FACTS, suggested_categories: [], categories_confirmed: false},
    'declined': {state: 'declined', suggested_categories: SUGGESTED, categories_confirmed: false},
    'pending': {state: 'pending', suggested_categories: SUGGESTED, categories_confirmed: false},
  };
  for (const [name, profile] of Object.entries(cases)) {
    const n = boot({profile});
    try { await settled(); await n.wait(40); assert.equal(bar(n), null, name); } finally { await closeWindow(n); }
  }
  // An old server without the fields: nothing.
  const old = boot({oldServer: true, profile: {state: 'ready', facts: FACTS}});
  try { await settled(); await old.wait(40); assert.equal(bar(old), null); assert.equal(popup(old), null); } finally { await closeWindow(old); }
  // Signing out removes it.
  const out = boot({profile: SUGGEST});
  try { await out.until(() => bar(out), 'bar'); out.$('workspace').hidden = true; await settled(); assert.equal(bar(out), null); } finally { await closeWindow(out); }
}

async function categoriesInKnowledge() {
  // Create: checkboxes, a renamed row, an own row, one unchecked.
  const t = boot({profile: SUGGEST});
  try {
    await openKnowledge(t, '.profile-categories');
    const body = knowledge(t).querySelector('.profile-know-body');
    assert.equal(body.firstElementChild, categoriesBox(t), 'on top of the facts');
    assert.equal(categoriesBox(t).querySelector('h3').textContent, 'Создадим категории');
    assert.equal(categoryRows(t).length, 3);
    assert.deepEqual(categoryRows(t).map(r => r.querySelector('input[type="text"]').value), SUGGESTED);
    assert.deepEqual(categoryRows(t).map(r => r.querySelector('input[type="checkbox"]').checked), [true, true, true]);
    for (const r of categoryRows(t)) {
      assert.equal(r.querySelector('input[type="text"]').maxLength, 100);
      assert.ok(r.querySelector('input[type="text"]').getAttribute('aria-label') && r.querySelector('input[type="checkbox"]').getAttribute('aria-label'));
    }
    assert.equal(facts(t).length, 2, 'facts stay below');
    assert.deepEqual([...categoriesBox(t).querySelectorAll('.profile-category-actions button')].map(b => b.textContent), ['Создать', 'Не нужно']);
    typeInto(t, categoryRows(t)[1].querySelector('input[type="text"]'), '  Работа  и проекты ');
    categoryRows(t)[2].querySelector('input[type="checkbox"]').click();
    action(t, 'categories-add', categoriesBox(t)).click();
    assert.equal(categoryRows(t).length, 4);
    typeInto(t, categoryRows(t)[3].querySelector('input[type="text"]'), 'Спорт');
    const before = t.state.calls.filter(c => c.url.pathname === '/api/v1/categories' && c.method === 'GET').length;
    action(t, 'categories-create', categoriesBox(t)).click();
    await t.until(() => t.profileCalls('POST', '/categories').length === 1, 'create sent');
    const post = t.profileCalls('POST', '/categories')[0];
    assert.deepEqual(post.body, {names: ['Учёба', 'Работа и проекты', 'Спорт']}); assert.equal(post.headers['X-CSRF-Token'], 'csrf');
    await t.until(() => !categoriesBox(t), 'box gone');
    assert.equal(knowledge(t).querySelector('.profile-categories-result').textContent, 'Готово. Категории созданы: Учёба, Работа и проекты, Спорт.');
    assert.equal(facts(t).length, 2);
    await t.until(() => t.state.calls.filter(c => c.url.pathname === '/api/v1/categories' && c.method === 'GET').length > before, 'category list reloaded');
    assert.equal(t.profileCalls('POST', '/categories/skip').length, 0);
    assert.equal(t.w.BerestaProfile.state(), 'ready');
    // Confirmed: the next opening has no box.
    knowledge(t).querySelector('.profile-close').click();
    await openKnowledge(t, '.profile-fact');
    assert.equal(categoriesBox(t), null);
  } finally { await closeWindow(t); }

  // An empty selection cannot be sent, a duplicate is sent once, the list is limited.
  const e = boot({profile: SUGGEST});
  try {
    await openKnowledge(e, '.profile-categories');
    const create = action(e, 'categories-create', categoriesBox(e));
    for (const r of categoryRows(e)) r.querySelector('input[type="checkbox"]').click();
    assert.equal(create.disabled, true);
    categoryRows(e)[0].querySelector('input[type="checkbox"]').click();
    assert.equal(create.disabled, false);
    typeInto(e, categoryRows(e)[1].querySelector('input[type="text"]'), 'учеба');
    typeInto(e, categoryRows(e)[0].querySelector('input[type="text"]'), 'Учёба');
    create.click();
    await e.until(() => e.profileCalls('POST', '/categories').length === 1, 'sent');
    assert.deepEqual(e.profileCalls('POST', '/categories')[0].body, {names: ['Учёба']}, 'ё and е are one name; unchecked rows stay out');
  } finally { await closeWindow(e); }
  const many = boot({profile: {...SUGGEST, suggested_categories: ['а1', 'а2', 'а3', 'а4', 'а5', 'а6']}});
  try {
    await openKnowledge(many, '.profile-categories');
    const add = action(many, 'categories-add', categoriesBox(many));
    for (let i = 0; i < 10; i++) add.click();
    assert.equal(categoryRows(many).length, 10);
    assert.equal(add.disabled, true);
  } finally { await closeWindow(many); }

  // «Не нужно»
  const k = boot({profile: SUGGEST});
  try {
    await openKnowledge(k, '.profile-categories');
    action(k, 'categories-skip', categoriesBox(k)).click();
    await k.until(() => !categoriesBox(k), 'box gone');
    assert.equal(k.profileCalls('POST', '/categories/skip').length, 1);
    assert.equal(k.profileCalls('POST', '/categories/skip')[0].headers['X-CSRF-Token'], 'csrf');
    assert.equal(k.profileCalls('POST', '/categories').length, 0);
    assert.equal(knowledge(k).querySelector('.profile-categories-result').textContent, 'Хорошо, категории не создаём.');
    assert.equal(k.state.categories.length, 1, 'no category was made');
  } finally { await closeWindow(k); }

  // Failures keep the box and say so.
  const f = boot({profile: SUGGEST});
  try {
    await openKnowledge(f, '.profile-categories');
    f.state.failures.categories = true;
    action(f, 'categories-create', categoriesBox(f)).click();
    await f.until(() => categoriesBox(f).querySelector('.profile-status').textContent !== 'Создаём…' && categoriesBox(f).querySelector('.profile-status').textContent, 'error');
    assert.ok(categoriesBox(f), 'still there');
    for (const node of categoriesBox(f).querySelectorAll('button, input')) assert.equal(node.disabled, false);
    f.state.failures.categories = false; f.state.failures.skip = true;
    action(f, 'categories-skip', categoriesBox(f)).click();
    await f.until(() => f.profileCalls('POST', '/categories/skip').length === 1 && categoriesBox(f).querySelector('.profile-status').textContent !== 'Сохраняем…', 'skip error');
    assert.ok(categoriesBox(f));
    f.state.failures.skip = false;
    action(f, 'categories-create', categoriesBox(f)).click();
    await f.until(() => !categoriesBox(f), 'created on the next try');
  } finally { await closeWindow(f); }

  // Names are text, not markup.
  const x = boot({profile: {...SUGGEST, suggested_categories: ['<b>Учёба</b>']}});
  try {
    await openKnowledge(x, '.profile-categories');
    assert.equal(categoryRows(x)[0].querySelector('input[type="text"]').value, '<b>Учёба</b>');
    assert.equal(categoriesBox(x).querySelector('b'), null);
  } finally { await closeWindow(x); }
}

async function categoriesOnFinalScreen() {
  // The result arrives while the popup is open: the same box shows there.
  const t = boot();
  try {
    await readyPopup(t);
    await toLastQuestion(t);
    typeInto(t, answerBox(t), 'Учусь в ГУАП');
    t.state.onGet = (count, record) => {
      if (record.state === 'pending' && count >= 3) Object.assign(record, {state: 'ready', facts: FACTS, suggested_categories: SUGGESTED, categories_confirmed: false});
    };
    press(t, 'done');
    await t.until(() => /Спасибо/.test(popup(t).textContent), 'final screen');
    assert.deepEqual(t.profileCalls('POST', '/answers')[0].body, {answers: {q7: 'Учусь в ГУАП'}});
    assert.equal(popup(t).querySelector('.profile-categories'), null, 'not before the result');
    await t.until(() => popup(t).querySelector('.profile-categories'), 'categories in the popup');
    assert.match(popup(t).textContent, /Спасибо! Профиль готов\./);
    assert.deepEqual([...popup(t).querySelectorAll('.profile-category-row input[type="text"]')].map(n => n.value), SUGGESTED);
    assert.equal(t.w.document.activeElement, popup(t).querySelector('.profile-category-name'));
    const count = t.state.gets;
    await t.wait(60);
    assert.equal(t.state.gets, count, 'polling stopped');
    assert.equal(bar(t), null, 'no second prompt next to the popup');
    action(t, 'categories-create', popup(t)).click();
    await t.until(() => t.profileCalls('POST', '/categories').length === 1, 'sent');
    assert.deepEqual(t.profileCalls('POST', '/categories')[0].body, {names: SUGGESTED});
    await t.until(() => !popup(t).querySelector('.profile-categories'), 'box gone');
    assert.match(popup(t).querySelector('.profile-categories-result').textContent, /Категории созданы: Учёба, Работа, Дом/);
    assert.deepEqual([...popup(t).querySelectorAll('.profile-foot button')].map(b => b.textContent), ['Закрыть']);
    press(t, 'close');
    assert.equal(popup(t), null);
    await settled(); assert.equal(bar(t), null);
  } finally { await closeWindow(t); }

  // «Не нужно» in the popup.
  const s = boot();
  try {
    await readyPopup(s);
    await toLastQuestion(s); typeInto(s, answerBox(s), 'Учусь');
    s.state.onGet = (count, record) => {
      if (record.state === 'pending' && count >= 2) Object.assign(record, {state: 'ready', facts: FACTS, suggested_categories: SUGGESTED, categories_confirmed: false});
    };
    press(s, 'done');
    await s.until(() => popup(s).querySelector('.profile-categories'), 'categories');
    action(s, 'categories-skip', popup(s)).click();
    await s.until(() => popup(s).querySelector('.profile-categories-result'), 'result');
    assert.equal(popup(s).querySelector('.profile-categories-result').textContent, 'Хорошо, категории не создаём.');
    assert.equal(s.profileCalls('POST', '/categories').length, 0);
  } finally { await closeWindow(s); }

  // Ready without suggestions: no box, only the thanks.
  const n = boot();
  try {
    await readyPopup(n);
    await toLastQuestion(n); typeInto(n, answerBox(n), 'Учусь');
    n.state.onGet = (count, record) => { if (record.state === 'pending') Object.assign(record, {state: 'ready', facts: FACTS}); };
    press(n, 'done');
    await n.until(() => /Профиль готов/.test(popup(n).textContent), 'ready text');
    assert.equal(popup(n).querySelector('.profile-categories'), null);
  } finally { await closeWindow(n); }

  // The popup is closed before the result: the categories wait in the dialog.
  const c = boot();
  try {
    await readyPopup(c);
    await toLastQuestion(c); typeInto(c, answerBox(c), 'Учусь');
    press(c, 'done');
    await c.until(() => /Спасибо/.test(popup(c).textContent), 'final');
    press(c, 'close');
    Object.assign(c.state.profile, {state: 'ready', facts: FACTS, suggested_categories: SUGGESTED, categories_confirmed: false});
    await openKnowledge(c, '.profile-categories');
    assert.equal(knowledge(c).querySelector('.profile-know-body').firstElementChild, categoriesBox(c));
  } finally { await closeWindow(c); }
}

/* ---------- Calendar icon and link ---------- */
async function calendarIcon() {
  const row = (id, extra = {}) => ({id, note_id: 'n1', item_id: null, text: `Напоминание ${id}`, status: 'confirmed', delivery_status: null,
    scheduled_at: '2090-10-08T07:00:00+00:00', timezone: 'Europe/Moscow', generation: 1, time_check: null, ...extra});
  const t = boot({profile: {state: 'declined'}, reminders: [
    row('r1', {calendar_url: CALENDAR}), row('r2'), row('r3', {calendar_url: null}),
    row('r4', {calendar_url: 'javascript:alert(1)'}), row('r5', {calendar_url: 'https://evil.example/calendar/render?action=TEMPLATE'}),
    row('r6', {calendar_url: 'http://calendar.google.com/calendar/render?action=TEMPLATE'}),
    row('r7', {calendar_url: 'https://calendar.google.com.evil.example/calendar/render'}),
  ]});
  try {
    await settled(); t.$('notes').querySelector('button').click(); await settled();
    await t.until(() => t.$('reminders-list').querySelectorAll('.reminder-row').length === 7, 'reminders');
    const rows = [...t.$('reminders-list').querySelectorAll('.reminder-row')];
    const icons = [...t.$('reminders-list').querySelectorAll('a.calendar-icon')];
    assert.equal(icons.length, 1, 'only a Google link with https is drawn');
    assert.equal(rows[0].querySelector('a.calendar-icon'), icons[0]);
    assert.equal(icons[0].getAttribute('href'), new URL(CALENDAR).href);
    assert.equal(icons[0].getAttribute('aria-label'), 'Добавить в Google Календарь');
    assert.equal(icons[0].title, 'Добавить в Google Календарь');
    assert.equal(icons[0].target, '_blank');
    assert.match(icons[0].rel, /noopener/); assert.match(icons[0].rel, /noreferrer/);
    // The icon sits next to the time, not among the buttons; there is no text button any more.
    assert.ok(icons[0].closest('.reminder-time'));
    assert.ok(icons[0].closest('.reminder-time').querySelector('.reminder-when'));
    assert.equal(icons[0].closest('.reminder-actions'), null);
    const svg = icons[0].querySelector('svg');
    assert.ok(svg && svg.getAttribute('aria-hidden') === 'true');
    assert.equal(icons[0].textContent, '', 'an icon, not a word');
    assert.equal(t.$('reminders-list').querySelectorAll('.calendar-link').length, 0, 'the old text button is gone');
    assert.ok(![...t.$('reminders-list').querySelectorAll('.reminder-actions button')].some(b => /Календар/.test(b.textContent)));
    for (const other of rows.slice(1)) assert.equal(other.querySelector('a.calendar-icon'), null);
  } finally { await closeWindow(t); }

  // Without the field nothing changes in the list.
  const plain = boot({profile: {state: 'declined'}, reminders: [row('r1')]});
  try {
    await settled(); plain.$('notes').querySelector('button').click(); await settled();
    await plain.until(() => plain.$('reminders-list').querySelectorAll('.reminder-row').length === 1, 'reminder');
    assert.equal(plain.$('reminders-list').querySelectorAll('a.calendar-icon').length, 0);
    assert.deepEqual([...plain.$('reminders-list').querySelectorAll('.reminder-actions button')].map(b => b.textContent), ['Изменить', 'Отменить']);
  } finally { await closeWindow(plain); }

  // The text link at the end of the confirmation line, and the styles for both.
  const code = read('reminders.js'), css = read('profile.css');
  assert.match(code, /function sayWithCalendar/); assert.match(code, /else sayWithCalendar\(text,row\.calendar_url\)/);
  assert.match(code, /noopener noreferrer/);
  assert.ok(css.includes('a.calendar-icon') && css.includes('a.calendar-text') && css.includes('.reminder-time'));
}

/* ---------- Files ---------- */
function files() {
  const html = read('index.html');
  assert.ok(html.includes('/static/profile.css') && html.includes('/static/profile.js'));
  const order = ['mobile-menu.js', 'profile.js', 'guide.js'].map(name => html.indexOf(`/static/${name}`));
  assert.ok(order[0] > -1 && order[0] < order[1] && order[1] < order[2], 'profile.js sits between mobile-menu.js and guide.js');
  assert.ok(html.indexOf('/static/profile.css') < html.indexOf('/static/buttons.css'), 'buttons.css stays last');
  assert.ok(html.indexOf('/static/app.js') < html.indexOf('/static/profile.js'));
  assert.ok(html.includes('data-proxy="open-profile"'));
  const css = read('profile.css');
  assert.match(css, /@media\(max-width:640px\)/); assert.match(css, /100dvh/);
  // No inline styles or scripts are written into the page.
  assert.ok(!/\sstyle=|<style|onclick=/.test(read('profile.js')));
}

module.exports = async function profileWeb() {
  files();
  await onlyForNew();
  await waitsForOthers();
  await guideOrder();
  await declines();
  await fullFlow();
  await emptySubmit();
  await errorsAndText();
  await draftRestore();
  await menuEntries();
  await knowledgeStates();
  await knowledgePending();
  await calendarOfferSwitch();
  await quietBar();
  await categoriesInKnowledge();
  await categoriesOnFinalScreen();
  await calendarIcon();
};

if (require.main === module) {
  module.exports().then(() => console.log('Web profile checks passed.')).catch(error => { console.error(error); process.exitCode = 1; });
}
