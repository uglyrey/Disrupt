'use strict';
// Phone menus, the bot links, links in notes, guided tips and the hint in empty fields.
// There is no layout in jsdom. The checks look at attributes, classes, focus and calls, not at pixels.
const assert = require('node:assert/strict'), fs = require('fs'), path = require('path'), {JSDOM} = require('jsdom');
const root = path.join(__dirname, '../app/static');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const reply = (data, status = 200) => ({ok: status < 400, status, headers: new Headers(), json: async () => structuredClone(data)});
async function settled() { for (let i = 0; i < 30; i++) await new Promise(resolve => setImmediate(resolve)); }
async function closeWindow(t) {
  for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve));
  t.dom.window.close();
}

const NOTE_TEXT = 'Смотри https://example.com/a?b=1, а также www.example.org/path.\nСсылка [сайт](https://example.com/b) и javascript:alert(1)';

// `phone` decides what the (max-width:640px) query says. `guides` is what the server keeps, `guidesFail` breaks the guide API.
// Without `guides` both tours count as seen, so that the menu checks are not disturbed by a tip.
const seen = () => ({note_tools: {step: 0, done: true}, categories: {step: 0, done: true}});
const fresh = () => ({note_tools: {step: 0, done: false}, categories: {step: 0, done: false}});
function boot({phone = false, guides, guidesFail = false, categories = [], markdown = NOTE_TEXT} = {}) {
  const dom = new JSDOM(read('index.html'), {url: 'https://beresta.invalid/', runScripts: 'outside-only'}), w = dom.window;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  try { Object.defineProperty(w.crypto, 'randomUUID', {value: require('node:crypto').randomUUID, configurable: true}); } catch { /* already provided */ }
  w.confirm = () => true;
  w.noteAutosaveMs = 10; w.noteAutosaveRetryMs = 600000; w.guideDelayMs = 0;
  const env = {phone};
  w.matchMedia = query => ({
    media: query, matches: /max-width:\s*640px/.test(query) ? env.phone : false,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  });
  // A click on a link must not try to navigate inside jsdom.
  w.document.addEventListener('click', event => { if (event.target.closest && event.target.closest('a[href]')) event.preventDefault(); });
  const state = {
    calls: [], patches: [], guidePatches: [],
    guides: guides || seen(),
    note: {id: 'n1', capture_id: 'k1', channel: 'web', input_kind: 'text', title: 'Заметка', markdown,
      original_text: 'Исходник', conclusions: [], version: 3, provider: 'manual', category_id: null,
      category_name: null, structure_confirmed_at: null, created_at: 1791220000, updated_at: 1791220000, items: []},
  };
  const summaries = [{id: 'n1', title: 'Заметка', version: 3, updated_at: 1791220000, category_id: null, channel: 'web', input_kind: 'text'}];
  w.fetch = async (input, options = {}) => {
    const url = new URL(input, w.location.href), method = options.method || 'GET';
    const body = options.body && typeof options.body === 'string' ? JSON.parse(options.body) : null;
    state.calls.push({url, method, body});
    if (url.pathname === '/health' || url.pathname === '/api/v1/provider/usage') return reply({simulation: true});
    if (url.pathname === '/api/v1/auth/me') return reply({username: 'tester', csrf_token: 'csrf'});
    if (url.pathname === '/api/v1/account/guides') {
      if (guidesFail) return reply({detail: 'Не удалось выполнить запрос'}, 500);
      if (method === 'PATCH') {
        state.guidePatches.push(body); state.guides[body.guide] = {step: body.step, done: body.done};
      }
      return reply({guides: state.guides});
    }
    if (url.pathname === '/api/v1/categories') return reply(categories);
    if (url.pathname === '/api/v1/jobs') return reply([]);
    if (url.pathname === '/api/v1/notes') return reply(summaries);
    if (url.pathname === '/api/v1/notes/n1/revisions') return reply([]);
    if (url.pathname === '/api/v1/notes/n1' && method === 'GET') return reply(state.note);
    if (url.pathname === '/api/v1/notes/n1' && method === 'PATCH') {
      state.patches.push(body);
      if (body.version !== state.note.version) return reply({detail: 'Заметка уже изменена. Обновите её перед сохранением'}, 409);
      Object.assign(state.note, {title: body.title, markdown: body.markdown, version: state.note.version + 1});
      return reply(state.note);
    }
    if (url.pathname.endsWith('/opened') || url.pathname.endsWith('/original-opened') || url.pathname === '/api/v1/search/events') return reply(null, 204);
    throw new Error(`Unexpected ${method} ${url.pathname}`);
  };
  const $ = id => w.document.getElementById(id);
  // The header link names the bot. The other links follow it, so the test changes it before the scripts run.
  $('bot-link').setAttribute('href', 'https://t.me/beresta_ru_bot');
  w.eval(['app.js', 'workspace.js', 'focus.js', 'md-editor.js', 'mobile-menu.js', 'guide.js'].map(read).join('\n')
    + '\nwindow.__render = renderMarkdownInto;\nwindow.__swapNote = value => { currentNote = value; };');
  const wait = ms => new Promise(resolve => w.setTimeout(resolve, ms));
  const until = async (check, what) => { for (let i = 0; i < 200; i++) { if (check()) return; await wait(5); } throw new Error(`Timed out: ${what}`); };
  return {dom, w, $, env, state, wait, until};
}
const key = (w, target, init) => {
  const event = new w.KeyboardEvent('keydown', {bubbles: true, cancelable: true, ...init});
  target.dispatchEvent(event);
  return event;
};
async function openFirst(t) {
  await settled(); t.$('notes').querySelector('button').click(); await settled();
  assert.equal(t.$('title').value, 'Заметка');
}
// The header row is shown by brain.js after sign-in. This page does not load it, so the test does what it does.
async function signedIn(t) {
  await settled();
  t.$('main-nav').hidden = false; await settled();
}
const bubble = t => t.w.document.querySelector('.guide-bubble');
const countText = t => t.w.document.querySelector('.guide-count').textContent;
const leadText = t => t.w.document.querySelector('.guide-text strong').textContent;

/* ---------- Names and links ---------- */
async function namesAndLinks() {
  const t = boot(); const {w, $} = t;
  try {
    await settled();
    assert.equal($('nav-tasks').textContent, 'Все задачи');
    assert.equal($('tasks-title').textContent, 'Все задачи');
    assert.equal(w.document.querySelector('#general-menu [data-proxy="nav-tasks"]').textContent, 'Все задачи');
    assert.deepEqual([...$('main-nav').querySelectorAll('button')].map(b => b.id), ['nav-notes', 'nav-search', 'nav-overview', 'nav-assistant', 'nav-tasks']);
    assert.equal(w.document.querySelector('#general-menu [data-proxy="nav-search"]').textContent, 'Поиск');
    // The header tells the bot from the channel.
    const bot = $('bot-link'), channel = w.document.querySelector('a[href="https://t.me/beresta_app"]');
    assert.equal(bot.getAttribute('href'), 'https://t.me/beresta_ru_bot');
    assert.equal(bot.target, '_blank'); assert.match(bot.rel, /noopener/);
    assert.match(bot.textContent, /Бот/); assert.match(channel.textContent, /Канал/);
    assert.match(bot.getAttribute('aria-label'), /^Бот/); assert.match(channel.getAttribute('aria-label'), /^Канал/);
    assert.ok(bot.compareDocumentPosition(channel) & w.Node.DOCUMENT_POSITION_FOLLOWING, 'bot first, channel next to it');
    assert.equal(channel.target, '_blank'); assert.match(channel.rel, /noopener/);
    // Every other bot link takes its address from the header link.
    const others = [...w.document.querySelectorAll('[data-bot-link]')];
    assert.ok(others.length >= 2, 'menu and landing');
    for (const link of others) { assert.equal(link.href, bot.href); assert.equal(link.target, '_blank'); assert.match(link.rel, /noopener/); }
    assert.ok($('landing-bot-link') && $('auth').contains($('landing-bot-link')), 'bot link on the landing');
    assert.ok($('general-menu').contains(w.document.querySelector('#general-menu [data-bot-link]')));
    assert.ok($('general-menu').querySelector('a[href="https://t.me/beresta_app"]'), 'channel in the menu too');
  } finally { await closeWindow(t); }
  // A different bot name is taken from the one link. The scripts do not name the bot a second time.
  const dom = new JSDOM(read('index.html'), {url: 'https://beresta.invalid/', runScripts: 'outside-only'});
  try {
    dom.window.document.getElementById('bot-link').setAttribute('href', 'https://t.me/other_bot');
    dom.window.fetch = async () => reply({detail: 'Войдите'}, 401);
    dom.window.eval(read('mobile-menu.js'));
    for (const link of dom.window.document.querySelectorAll('[data-bot-link]')) assert.equal(link.href, 'https://t.me/other_bot');
    assert.ok(!/beresta_ru_bot/.test(read('mobile-menu.js') + read('guide.js')));
  } finally { dom.window.close(); }
}

/* ---------- General menu on the left ---------- */
async function generalMenu() {
  const t = boot({phone: true}); const {w, $} = t;
  try {
    await signedIn(t);
    const button = $('menu-button'), menu = $('general-menu');
    assert.equal(button.hidden, false, 'the button shows with the header sections');
    assert.equal(button.getAttribute('aria-label'), 'Меню'); assert.equal(button.getAttribute('aria-controls'), 'general-menu');
    assert.equal(button.getAttribute('aria-expanded'), 'false'); assert.equal(menu.hidden, true);
    assert.deepEqual([...menu.querySelectorAll('[data-proxy^="nav-"]')].map(b => b.textContent), ['Все заметки', 'Поиск', 'Обзор', 'Помощник', 'Все задачи']);
    for (const id of ['open-telegram', 'open-feedback', 'open-delete']) assert.ok(menu.querySelector(`[data-proxy="${id}"]`), id);
    for (const href of ['/privacy', '/terms', '/cookies', '/data-deletion']) assert.ok(menu.querySelector(`a[href="${href}"]`), href);
    assert.ok(menu.querySelector('[data-cookie-settings]'));
    assert.ok($('general-menu-extra'), 'hook for more items');
    // Open: expanded, focus on the first item, the current section is marked.
    // index.html marks «Все заметки» as the current one. brain.js moves the mark and is not loaded here, so the test moves it.
    $('nav-notes').removeAttribute('aria-current');
    $('nav-assistant').setAttribute('aria-current', 'page');
    button.click();
    assert.equal(menu.hidden, false); assert.equal(button.getAttribute('aria-expanded'), 'true');
    assert.equal($('menu-scrim').hidden, false);
    assert.equal(w.document.activeElement, menu.querySelector('.menu-item'));
    assert.equal(menu.querySelector('[data-proxy="nav-assistant"]').getAttribute('aria-current'), 'page');
    assert.equal(menu.querySelector('[data-proxy="nav-notes"]').hasAttribute('aria-current'), false);
    // Esc closes and the focus returns to the button.
    key(w, w.document.activeElement, {key: 'Escape'});
    assert.equal(menu.hidden, true); assert.equal(button.getAttribute('aria-expanded'), 'false'); assert.equal($('menu-scrim').hidden, true);
    assert.equal(w.document.activeElement, button);
    // The button toggles.
    button.click(); assert.equal(menu.hidden, false); button.click(); assert.equal(menu.hidden, true);
    // A tap outside closes it, and so does the scrim.
    button.click(); $('notes').click(); assert.equal(menu.hidden, true);
    button.click(); $('menu-scrim').click(); assert.equal(menu.hidden, true);
    // A section item does what the header button does and closes the menu.
    let clicks = 0;
    $('nav-overview').addEventListener('click', () => { clicks++; });
    button.click(); menu.querySelector('[data-proxy="nav-overview"]').click();
    assert.equal(clicks, 1); assert.equal(menu.hidden, true);
    // The Telegram item opens the same dialog as the old button, and the focus comes back to the menu button.
    button.click(); menu.querySelector('[data-proxy="open-telegram"]').click();
    assert.equal(menu.hidden, true); assert.equal($('telegram-dialog').open, true);
    $('telegram-dialog').close();
    assert.equal(w.document.activeElement, button);
    // Back to a wide screen: the menu goes away by itself.
    button.click(); assert.equal(menu.hidden, false);
    t.env.phone = false; w.dispatchEvent(new w.Event('resize'));
    assert.equal(menu.hidden, true); assert.equal(button.getAttribute('aria-expanded'), 'false');
    assert.equal(w.BerestaMenus.openGeneral(), false, 'not on a wide screen');
    // Signed out: the button hides.
    t.env.phone = true;
    $('main-nav').hidden = true; await settled();
    assert.equal(button.hidden, true);
  } finally { await closeWindow(t); }
}

/* ---------- Note menu on the right ---------- */
async function noteMenu() {
  const t = boot({phone: true}); const {w, $} = t;
  try {
    await signedIn(t);
    await openFirst(t);
    const button = $('note-menu-button'), tools = $('note-tools');
    assert.equal(button.getAttribute('aria-label'), 'Меню заметки');
    assert.equal(button.getAttribute('aria-controls'), 'note-tools'); assert.equal(button.getAttribute('aria-expanded'), 'false');
    assert.ok(button.querySelector('svg'), 'an icon');
    assert.ok($('note-menu-extra') && tools.contains($('note-menu-extra')), 'hook for more items');
    // Every tool has a visible name next to its icon. The popovers and ids stay the same.
    const names = [...tools.querySelectorAll('.tool-btn')].map(b => b.querySelector('.tool-label').textContent);
    assert.deepEqual(names, ['Напоминания', 'Задачи и идеи', 'Связанные заметки', 'Свойства', 'Исходник', 'Дополнения ИИ', 'Версии']);
    assert.equal(tools.querySelectorAll('.tool-btn').length, 7);
    assert.equal($('tool-reminders').getAttribute('aria-controls'), 'pop-reminders');
    // Open: expanded, the first tool has the focus.
    button.click();
    assert.equal(tools.classList.contains('is-open'), true); assert.equal(button.getAttribute('aria-expanded'), 'true');
    assert.equal($('menu-scrim').hidden, false);
    assert.equal(w.document.activeElement, $('tool-reminders'));
    // Arrow keys move through the shown tools. The related tool is hidden, no list item points at it.
    key(w, w.document.activeElement, {key: 'ArrowDown'}); assert.equal(w.document.activeElement, $('tool-tasks'));
    key(w, w.document.activeElement, {key: 'ArrowDown'}); assert.equal(w.document.activeElement, $('tool-props'));
    key(w, w.document.activeElement, {key: 'ArrowUp'}); assert.equal(w.document.activeElement, $('tool-tasks'));
    key(w, w.document.activeElement, {key: 'End'}); assert.equal(w.document.activeElement, $('tool-history'));
    key(w, w.document.activeElement, {key: 'Home'}); assert.equal(w.document.activeElement, $('tool-reminders'));
    // Esc closes and gives the focus back to the button.
    key(w, w.document.activeElement, {key: 'Escape'});
    assert.equal(tools.classList.contains('is-open'), false); assert.equal(button.getAttribute('aria-expanded'), 'false');
    assert.equal(w.document.activeElement, button);
    // Outside tap and scrim close it.
    button.click(); $('markdown').click(); assert.equal(tools.classList.contains('is-open'), false);
    button.click(); $('menu-scrim').click(); assert.equal(tools.classList.contains('is-open'), false);
    // Choosing a tool opens its popover, as before, and closes the list. Esc closes the popover, the focus goes to the menu button.
    button.click(); $('tool-props').click();
    assert.equal($('pop-props').hidden, false); assert.equal($('tool-props').getAttribute('aria-expanded'), 'true');
    assert.equal(tools.classList.contains('is-open'), false); assert.equal(w.BerestaMenus.noteOpen(), false);
    assert.equal($('pop-props').contains(w.document.activeElement), true);
    key(w, $('pop-props'), {key: 'Escape'});
    assert.equal($('pop-props').hidden, true);
    assert.equal(w.document.activeElement, button, 'the tool button is hidden on a phone');
    // The hook takes new items: choosing one closes the list.
    const extra = w.document.createElement('button');
    extra.type = 'button'; extra.className = 'menu-item'; extra.textContent = 'Пример';
    let used = 0; extra.addEventListener('click', () => { used++; });
    $('note-menu-extra').append(extra);
    button.click(); assert.ok(w.BerestaMenus.noteOpen());
    key(w, $('tool-reminders'), {key: 'End'}); assert.equal(w.document.activeElement, extra);
    extra.click(); assert.equal(used, 1); assert.equal(tools.classList.contains('is-open'), false);
    // Another note takes the list away.
    button.click(); assert.ok(w.BerestaMenus.noteOpen());
    w.document.dispatchEvent(new w.CustomEvent('beresta:note-rendered')); assert.equal(w.BerestaMenus.noteOpen(), false);
    // Back to a wide screen: the list closes, the button does nothing.
    button.click(); assert.ok(w.BerestaMenus.noteOpen());
    t.env.phone = false; w.dispatchEvent(new w.Event('resize'));
    assert.equal(w.BerestaMenus.noteOpen(), false); assert.equal(tools.classList.contains('is-open'), false);
    button.click(); assert.equal(tools.classList.contains('is-open'), false);
    // On a wide screen a closed popover gives the focus back to its own button.
    $('tool-props').click(); key(w, $('pop-props'), {key: 'Escape'});
    assert.equal(w.document.activeElement, $('tool-props'));
  } finally { await closeWindow(t); }
}

/* ---------- Links in notes ---------- */
async function links() {
  const t = boot(); const {w, $} = t;
  try {
    await settled();
    const out = text => { const box = w.document.createElement('div'); w.__render(box, text); return box; };
    const anchors = box => [...box.querySelectorAll('a')].map(a => [a.textContent, a.getAttribute('href'), a.target, a.rel]);
    const REL = 'noopener noreferrer';
    assert.deepEqual(anchors(out('Смотри https://example.com/a?b=1, и всё.')), [['https://example.com/a?b=1', 'https://example.com/a?b=1', '_blank', REL]]);
    assert.deepEqual(anchors(out('Сайт www.example.org/path.')), [['www.example.org/path', 'https://www.example.org/path', '_blank', REL]]);
    assert.deepEqual(anchors(out('(http://t.co/x) и http://a.b/c_(d)')).map(a => a[0]), ['http://t.co/x', 'http://a.b/c_(d)']);
    assert.deepEqual(anchors(out('Две: http://one.test и https://two.test!')).map(a => a[0]), ['http://one.test', 'https://two.test']);
    assert.equal(out('Смотри https://example.com/a, и всё.').textContent, 'Смотри https://example.com/a, и всё.', 'the text stays as typed');
    assert.deepEqual(anchors(out('**https://bold.example**')).map(a => a[0]), ['https://bold.example']);
    assert.deepEqual(anchors(out('[сайт](https://example.com/b)')), [['сайт', 'https://example.com/b', '_blank', REL]]);
    assert.equal(out('[https://a.test](https://a.test)').querySelectorAll('a').length, 1, 'no link inside a link');
    assert.equal(out('`http://code.example`').querySelectorAll('a').length, 0, 'code stays code');
    assert.equal(out('- пункт https://list.example').querySelectorAll('li a').length, 1);
    // Anything else than http, https and www stays text.
    for (const text of ['javascript:alert(1)', '[x](javascript:alert(1))', 'data:text/html,<script>alert(1)</script>', 'ftp://x.y/z', 'vbscript:x',
      '[x](data:text/html,aaa)', 'mailto:a@b.c', 'http://', 'https:// пробел', 'xhttps://not.link', 'a@www.site.test']) {
      assert.equal(out(text).querySelectorAll('a').length, 0, text);
    }
    const hostile = out('<img src=x onerror=alert(1)> <script>alert(1)</script> https://ok.test/"onmouseover="alert(1)');
    assert.equal(hostile.querySelector('img'), null); assert.equal(hostile.querySelector('script'), null);
    assert.equal(hostile.querySelectorAll('[onmouseover], [onerror]').length, 0);
    for (const a of hostile.querySelectorAll('a')) assert.match(a.href, /^https?:\/\//);
    assert.ok(hostile.textContent.includes('<script>alert(1)</script>'));

    // In the reading view a click on a link does not switch to editing.
    await openFirst(t);
    assert.equal(w.BerestaMd.mode(), 'read');
    const found = anchors($('preview'));
    assert.deepEqual(found.map(a => a[0]), ['https://example.com/a?b=1', 'www.example.org/path', 'сайт']);
    $('preview').querySelector('a').click();
    assert.equal(w.BerestaMd.mode(), 'read'); assert.equal($('markdown').hidden, true);
    // A click on plain text still edits.
    $('preview').querySelector('span[data-line]').click();
    assert.equal(w.BerestaMd.mode(), 'edit');
  } finally { await closeWindow(t); }
}

/* ---------- Hint in empty fields ---------- */
async function placeholders() {
  const t = boot(); const {$} = t;
  try {
    await settled();
    for (const field of [$('thought'), $('markdown')]) {
      assert.match(field.placeholder, /^Пишите как есть\./);
      assert.ok(field.placeholder.includes('\n'), 'several lines');
      for (const sample of ['«- »', '«1. »', '«- [ ] »']) assert.ok(field.placeholder.includes(sample), sample);
    }
    assert.match(read('guide.css'), /#6f6b5f/, 'quiet but readable colour');
    assert.match(read('guide.css'), /#thought::placeholder/);
  } finally { await closeWindow(t); }
}

/* ---------- Page rules: no inline code, scripts in order ---------- */
function page() {
  const html = read('index.html');
  assert.ok(!/<[^>]+\sstyle\s*=/i.test(html), 'no inline style attributes');
  assert.ok(!/<script(?![^>]*\ssrc=)/i.test(html), 'no inline scripts');
  assert.ok(!/src=["']data:|href=["']data:/i.test(html), 'no data: addresses');
  const order = ['md-editor.js', 'mobile-menu.js', 'guide.js'].map(name => html.indexOf(`/static/${name}`));
  assert.ok(order[0] > 0 && order[0] < order[1] && order[1] < order[2], 'scripts load in order');
  assert.ok(html.includes('/static/mobile-menu.css') && html.includes('/static/guide.css'));
  for (const name of ['mobile-menu.js', 'guide.js']) assert.ok(!/\.style\.cssText|setAttribute\(['"]style/.test(read(name)), name);
}

/* ---------- Guided tips ---------- */
async function guideFlow() {
  // Next, counter, hidden targets skipped, the end.
  let t = boot({guides: fresh()}); let {w, $} = t;
  try {
    await openFirst(t);
    await t.until(() => w.BerestaGuide.active() === 'note_tools', 'tour starts');
    const total = Number(/из (\d+)/.exec(countText(t))[1]);
    assert.equal(bubble(t).getAttribute('role'), 'dialog'); assert.equal(bubble(t).getAttribute('aria-label'), 'Подсказка');
    assert.equal(leadText(t), 'Напоминания.'); assert.equal(countText(t), `1 из ${total}`);
    assert.equal($('tool-reminders').classList.contains('guide-target'), true);
    assert.equal(bubble(t).querySelector('.guide-next').textContent, 'Далее');
    assert.equal(bubble(t).querySelector('.guide-never').textContent, 'Больше не показывать');
    assert.ok(!w.document.querySelector('.guide-scrim, .guide-overlay'), 'no overlay');
    await t.until(() => t.state.guidePatches.length > 0, 'first step saved');
    assert.deepEqual(t.state.guidePatches[0], {guide: 'note_tools', step: 1, done: false});
    const leads = [leadText(t)];
    w.document.querySelector('.guide-next').click();
    assert.equal(countText(t), `2 из ${total}`); assert.equal($('tool-reminders').classList.contains('guide-target'), false);
    leads.push(leadText(t));
    while (w.BerestaGuide.active()) {
      if (bubble(t).querySelector('.guide-next').textContent === 'Готово') assert.equal(countText(t), `${total} из ${total}`);
      w.document.querySelector('.guide-next').click();
      if (w.BerestaGuide.active()) leads.push(leadText(t));
    }
    assert.equal(leads.length, total);
    assert.ok(!leads.includes('Связанные заметки.'), 'a hidden target is skipped');
    assert.equal(leads.at(-1), 'Версии.');
    assert.equal(bubble(t), null); assert.equal(w.document.querySelectorAll('.guide-target').length, 0);
    await settled();
    assert.equal(t.state.guidePatches.at(-1).done, true); assert.equal(t.state.guides.note_tools.done, true);
    assert.equal(t.state.guides.categories.done, false);
    // The same page load shows nothing more, not even for the other tour.
    w.document.dispatchEvent(new w.CustomEvent('beresta:note-rendered')); await t.wait(30);
    assert.equal(w.BerestaGuide.active(), null);
  } finally { await closeWindow(t); }

  // Esc puts it off. The step stays saved, nothing is marked done, and it does not come back in this page load.
  t = boot({guides: fresh()}); ({w, $} = t);
  try {
    await openFirst(t);
    await t.until(() => w.BerestaGuide.active() === 'note_tools', 'tour starts');
    w.document.querySelector('.guide-next').click(); w.document.querySelector('.guide-next').click();
    await settled();
    assert.equal(t.state.guidePatches.at(-1).done, false);
    const step = t.state.guidePatches.at(-1).step;
    assert.equal(step, 4);
    key(w, w.document.body, {key: 'Escape'});
    assert.equal(w.BerestaGuide.active(), null); assert.equal(bubble(t), null);
    await settled();
    assert.deepEqual(t.state.guides.note_tools, {step, done: false});
    w.document.dispatchEvent(new w.CustomEvent('beresta:note-rendered')); await t.wait(30);
    assert.equal(w.BerestaGuide.active(), null);
  } finally { await closeWindow(t); }

  // Esc that closes a popover first does not close the tip.
  t = boot({guides: fresh()}); ({w, $} = t);
  try {
    await openFirst(t);
    await t.until(() => w.BerestaGuide.active() === 'note_tools', 'tour starts');
    const event = new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true});
    event.preventDefault();
    w.document.body.dispatchEvent(event);
    assert.equal(w.BerestaGuide.active(), 'note_tools');
  } finally { await closeWindow(t); }

  // «Больше не показывать» saves done.
  t = boot({guides: fresh()}); ({w, $} = t);
  try {
    await openFirst(t);
    await t.until(() => w.BerestaGuide.active() === 'note_tools', 'tour starts');
    w.document.querySelector('.guide-never').click();
    assert.equal(w.BerestaGuide.active(), null); assert.equal(bubble(t), null);
    await settled();
    assert.equal(t.state.guidePatches.at(-1).guide, 'note_tools'); assert.equal(t.state.guidePatches.at(-1).done, true);
  } finally { await closeWindow(t); }

  // Resume from the saved step.
  t = boot({guides: {note_tools: {step: 4, done: false}, categories: {step: 0, done: false}}}); ({w, $} = t);
  try {
    await openFirst(t);
    await t.until(() => w.BerestaGuide.active() === 'note_tools', 'tour resumes');
    assert.equal(leadText(t), 'Свойства.'); assert.equal($('tool-props').classList.contains('guide-target'), true);
    assert.notEqual(countText(t).startsWith('1 из'), true);
  } finally { await closeWindow(t); }

  // Done on the server: nothing shows.
  t = boot({guides: {note_tools: {step: 7, done: true}, categories: {step: 0, done: true}}}); ({w, $} = t);
  try {
    await openFirst(t); await t.wait(60);
    assert.equal(w.BerestaGuide.active(), null); assert.equal(bubble(t), null);
    assert.equal(t.state.guidePatches.length, 0);
  } finally { await closeWindow(t); }

  // A popover that opens ends the tip, a different note too.
  t = boot({guides: fresh()}); ({w, $} = t);
  try {
    await openFirst(t);
    await t.until(() => w.BerestaGuide.active() === 'note_tools', 'tour starts');
    $('tool-props').click(); await settled();
    assert.equal(w.BerestaGuide.active(), null, 'a popover took the page');
  } finally { await closeWindow(t); }
  t = boot({guides: fresh()}); ({w, $} = t);
  try {
    await openFirst(t);
    await t.until(() => w.BerestaGuide.active() === 'note_tools', 'tour starts');
    w.__swapNote({id: 'other'}); w.document.dispatchEvent(new w.CustomEvent('beresta:note-rendered'));
    assert.equal(w.BerestaGuide.active(), null, 'another note');
  } finally { await closeWindow(t); }

  // The server cannot be reached: the tip still works, and sessionStorage keeps the answer for the session.
  t = boot({guides: fresh(), guidesFail: true}); ({w, $} = t);
  try {
    await openFirst(t);
    await t.until(() => w.BerestaGuide.active() === 'note_tools', 'tour starts without the server');
    w.document.querySelector('.guide-next').click();
    w.document.querySelector('.guide-never').click();
    await settled();
    const kept = JSON.parse(w.sessionStorage.getItem('beresta-guides'));
    assert.equal(kept.note_tools.done, true);
    assert.equal(w.BerestaGuide.active(), null);
    assert.equal(w.document.getElementById('message').textContent, '', 'no error shown to the person');
  } finally { await closeWindow(t); }
  // Blocked storage and a broken server together: still no error.
  t = boot({guides: fresh(), guidesFail: true}); ({w, $} = t);
  try {
    for (const method of ['getItem', 'setItem']) Object.defineProperty(w.Storage.prototype, method, {configurable: true, value() { throw new Error('blocked'); }});
    await openFirst(t);
    await t.until(() => w.BerestaGuide.active() === 'note_tools', 'tour starts');
    w.document.querySelector('.guide-never').click(); await settled();
    assert.equal(w.BerestaGuide.active(), null);
  } finally { await closeWindow(t); }
}

async function guideCategories() {
  const categories = [{id: 'c1', name: 'Дом', version: 1}];
  let t = boot({guides: fresh(), categories}); let {w, $} = t;
  try {
    await settled();
    const gear = $('category-navigation').querySelector('.category-more');
    assert.ok(gear, 'a category with a gear');
    gear.click();
    await t.until(() => w.BerestaGuide.active() === 'categories', 'categories tour');
    assert.equal(countText(t), '1 из 3'); assert.equal(leadText(t), 'Плюс.');
    assert.equal($('category-add').classList.contains('guide-target'), true);
    w.document.querySelector('.guide-next').click();
    assert.equal(leadText(t), 'Шестерёнка.'); assert.equal(countText(t), '2 из 3');
    assert.equal(gear.classList.contains('guide-target'), true);
    w.document.querySelector('.guide-next').click();
    assert.equal(leadText(t), 'Правая кнопка.'); assert.equal(countText(t), '3 из 3');
    assert.match(w.document.querySelector('.guide-text').textContent, /подержите/);
    assert.equal(w.document.querySelector('.guide-next').textContent, 'Готово');
    w.document.querySelector('.guide-next').click();
    await settled();
    assert.equal(w.BerestaGuide.active(), null);
    assert.deepEqual(t.state.guidePatches.at(-1), {guide: 'categories', step: 2, done: true});
    assert.equal(t.state.guidePatches.some(p => p.guide === 'note_tools'), false);
  } finally { await closeWindow(t); }

  // One tour per page load: after a note tour the categories menu stays quiet.
  t = boot({guides: fresh(), categories}); ({w, $} = t);
  try {
    await openFirst(t);
    await t.until(() => w.BerestaGuide.active() === 'note_tools', 'note tour');
    w.document.querySelector('.guide-never').click();
    $('category-navigation').querySelector('.category-more').click(); await t.wait(40);
    assert.equal(w.BerestaGuide.active(), null);
  } finally { await closeWindow(t); }
}

async function guideOnPhone() {
  const t = boot({phone: true, guides: fresh()}); const {w, $} = t;
  try {
    await signedIn(t);
    await openFirst(t);
    await t.until(() => w.BerestaGuide.active() === 'note_tools', 'tour starts on a phone');
    // First the menu button itself, the list is still closed.
    assert.equal(leadText(t), 'Меню заметки.'); assert.equal($('note-menu-button').classList.contains('guide-target'), true);
    assert.equal(w.BerestaMenus.noteOpen(), false);
    const total = Number(/из (\d+)/.exec(countText(t))[1]);
    assert.equal(countText(t), `1 из ${total}`);
    // Next opens the list, so every tool has a place on the screen. The page keeps the focus where it was.
    w.document.querySelector('.guide-next').click();
    assert.equal(w.BerestaMenus.noteOpen(), true); assert.equal(leadText(t), 'Напоминания.');
    assert.equal($('tool-reminders').classList.contains('guide-target'), true);
    assert.notEqual(w.document.activeElement, $('tool-reminders'), 'the focus stays put');
    // A tap on the tip does not close the list.
    w.document.querySelector('.guide-text').click(); assert.equal(w.BerestaMenus.noteOpen(), true);
    w.document.querySelector('.guide-next').click(); assert.equal(leadText(t), 'Задачи и идеи.');
    // Closing the list by hand puts the tour off.
    $('menu-scrim').click();
    assert.equal(w.BerestaGuide.active(), null); assert.equal(w.BerestaMenus.noteOpen(), false);
  } finally { await closeWindow(t); }

  // The end of the tour shuts the list that the tour opened.
  const u = boot({phone: true, guides: fresh()}); const v = u.w;
  try {
    await signedIn(u); await openFirst(u);
    await u.until(() => v.BerestaGuide.active() === 'note_tools', 'tour starts on a phone');
    while (v.BerestaGuide.active()) v.document.querySelector('.guide-next').click();
    assert.equal(v.BerestaMenus.noteOpen(), false); assert.equal(u.$('note-tools').classList.contains('is-open'), false);
    await settled();
    assert.equal(u.state.guides.note_tools.done, true);
  } finally { await closeWindow(u); }
}

// A tool that CSS takes off the screen (note-actions.css does it for «Связанные заметки») gets no tip on a phone.
async function guideSkipsCssHidden() {
  const t = boot({phone: true, guides: fresh()}); const {w, $} = t;
  try {
    const style = w.document.createElement('style');
    style.textContent = '.tool[data-tool="related"]{display:none!important}';
    w.document.head.append(style);
    // A neighbour in the list makes focus.js show the tool, only the style keeps it off the screen.
    const li = w.document.createElement('li'); li.textContent = 'Сосед';
    $('related-list').append(li); $('view-related').hidden = false;
    await signedIn(t);
    await openFirst(t);
    const tool = w.document.querySelector('.tool[data-tool="related"]');
    assert.equal(tool.hidden, false, 'the tool is shown by focus.js');
    assert.equal(w.getComputedStyle(tool).display, 'none', 'and taken away by the style');
    await t.until(() => w.BerestaGuide.active() === 'note_tools', 'tour starts on a phone');
    const total = Number(/из (\d+)/.exec(countText(t))[1]);
    assert.equal(total, 7, 'menu button and six tools, no related tool');
    const leads = [leadText(t)];
    while (w.BerestaGuide.active()) {
      w.document.querySelector('.guide-next').click();
      if (w.BerestaGuide.active()) leads.push(leadText(t));
    }
    assert.equal(leads.length, total);
    assert.ok(!leads.includes('Связанные заметки.'));
  } finally { await closeWindow(t); }
}

module.exports = async () => {
  page();
  await namesAndLinks();
  await generalMenu();
  await noteMenu();
  await links();
  await placeholders();
  await guideFlow();
  await guideCategories();
  await guideOnPhone();
  await guideSkipsCssHidden();
  console.log('Phone menus and guide checks passed: renames, bot links, general and note menus, links in notes, empty-field hint, tips with progress and fallback.');
};
if (require.main === module) module.exports().catch(error => { console.error(error); process.exitCode = 1; });
