'use strict';
// Markdown notes. Smart list keys, shortcuts, toolbar, reading and editing views, safe rendering, the gear on categories.
const assert = require('node:assert/strict'), fs = require('fs'), path = require('path'), {JSDOM} = require('jsdom');
const root = path.join(__dirname, '../app/static');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const reply = (data, status = 200) => ({ok: status < 400, status, headers: new Headers(), json: async () => structuredClone(data)});
async function settled() { for (let i = 0; i < 30; i++) await new Promise(resolve => setImmediate(resolve)); }
async function closeWindow(t) {
  for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve));
  t.dom.window.close();
}

// Lines 5 and 6 are a bullet list, 9 and 10 a numbered one, 12 and 13 are tasks.
const RICH = '# Заголовок\n\nСтрока один\nстрока два\n\n- один\n- два\n  - вложенный\n\n1. a\n2. b\n\n- [ ] задача\n- [x] готово\n\n> цитата\n\n---\n**жирный** *курсив* `код` [сайт](https://example.com/a)';

function boot({markdown = RICH, categories = []} = {}) {
  const dom = new JSDOM(read('index.html'), {url: 'https://beresta.invalid/', runScripts: 'outside-only'}), w = dom.window;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  try { Object.defineProperty(w.crypto, 'randomUUID', {value: require('node:crypto').randomUUID, configurable: true}); } catch { /* already provided */ }
  w.confirm = () => true;
  w.noteAutosaveMs = 10; w.noteAutosaveRetryMs = 600000;
  const state = {
    calls: [], patches: [],
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
    if (url.pathname === '/api/v1/categories') return reply(categories);
    if (url.pathname === '/api/v1/jobs') return reply([]);
    if (url.pathname === '/api/v1/notes') return reply(summaries);
    if (url.pathname === '/api/v1/notes/n1' && method === 'GET') return reply(state.note);
    if (url.pathname === '/api/v1/notes/n1' && method === 'PATCH') {
      state.patches.push(body);
      if (body.checkpoint && body.markdown === state.note.markdown && body.version === state.note.version) return reply(state.note);
      if (body.version !== state.note.version) return reply({detail: 'Заметка уже изменена. Обновите её перед сохранением'}, 409);
      Object.assign(state.note, {title: body.title, markdown: body.markdown, version: state.note.version + 1});
      return reply(state.note);
    }
    if (url.pathname.endsWith('/opened') || url.pathname.endsWith('/original-opened') || url.pathname === '/api/v1/search/events') return reply(null, 204);
    throw new Error(`Unexpected ${method} ${url.pathname}`);
  };
  w.eval(read('app.js') + '\n' + read('workspace.js') + '\n' + read('focus.js') + '\n' + read('md-editor.js') + '\nwindow.__render = renderMarkdownInto;');
  const wait = ms => new Promise(resolve => w.setTimeout(resolve, ms));
  const until = async (check, what) => { for (let i = 0; i < 200; i++) { if (check()) return; await wait(5); } throw new Error(`Timed out: ${what}`); };
  const $ = id => w.document.getElementById(id);
  return {dom, w, $, state, wait, until};
}
async function openFirst(t) {
  await settled(); t.$('notes').querySelector('button').click(); await settled();
  assert.equal(t.$('title').value, 'Заметка');
}
const key = (w, target, init) => {
  const event = new w.KeyboardEvent('keydown', {bubbles: true, cancelable: true, ...init});
  target.dispatchEvent(event);
  return event;
};
// 'a|b' puts the caret, 'a‹b›c' selects.
function put(ta, text) {
  const a = text.indexOf('‹'), b = text.indexOf('›');
  if (a >= 0) { ta.value = text.replace('‹', '').replace('›', ''); ta.setSelectionRange(a, b - 1); return; }
  const i = text.indexOf('|'); ta.value = text.replace('|', ''); ta.setSelectionRange(i, i);
}
function show(ta) {
  const v = ta.value, s = ta.selectionStart, e = ta.selectionEnd;
  return s === e ? `${v.slice(0, s)}|${v.slice(s)}` : `${v.slice(0, s)}‹${v.slice(s, e)}›${v.slice(e)}`;
}
async function editing(t) {
  await openFirst(t);
  t.w.BerestaMd.setMode('edit', {end: true});
  const ta = t.$('markdown'); let inputs = 0;
  ta.addEventListener('input', () => { inputs++; });
  return {ta, inputs: () => inputs};
}

async function rendering() {
  const t = boot(); const {w} = t;
  try {
    // Attached to the page: a checkbox only fires change events there.
    const out = w.document.createElement('div'); w.document.body.append(out);
    w.__render(out, RICH);
    assert.equal(out.querySelector('h1').textContent, 'Заголовок');
    assert.equal(out.querySelector('h1').dataset.line, '0');
    const para = out.querySelector('p');
    assert.equal(para.querySelectorAll('br').length, 1, 'soft line break is kept');
    assert.deepEqual([...para.querySelectorAll('span')].map(s => s.dataset.line), ['2', '3']);
    const lists = out.querySelectorAll('ul');
    assert.equal(lists.length, 3, 'bullets, the nested list and the task list');
    assert.equal(out.querySelector('ul ul > li').textContent, 'вложенный');
    assert.equal(out.querySelector('ul > li:nth-child(2) > ul').parentNode.dataset.line, '6');
    assert.equal(out.querySelectorAll('ol li').length, 2);
    const boxes = [...out.querySelectorAll('input[type=checkbox]')];
    assert.deepEqual(boxes.map(b => [b.checked, b.disabled, b.dataset.line]), [[false, true, '12'], [true, true, '13']]);
    assert.equal(out.querySelector('blockquote').textContent, 'цитата');
    assert.ok(out.querySelector('hr'));
    assert.equal(out.querySelector('strong').textContent, 'жирный');
    assert.equal(out.querySelector('em').textContent, 'курсив');
    assert.equal(out.querySelector('code').textContent, 'код');
    const link = out.querySelector('a');
    assert.deepEqual([link.getAttribute('href'), link.getAttribute('target'), link.getAttribute('rel')], ['https://example.com/a', '_blank', 'noopener noreferrer']);

    // A list that starts at 3 keeps its number, tabs and deeper levels nest.
    w.__render(out, '3. c\n4. d\n- x\n\t- y');
    assert.equal(out.querySelector('ol').getAttribute('start'), '3');
    assert.equal(out.querySelectorAll('ul').length, 2);
    // Checkboxes are live only when the caller gives a handler.
    const toggled = [];
    w.__render(out, '- [ ] a', {onToggle: (line, checked) => toggled.push([line, checked])});
    out.querySelector('input').click();
    assert.deepEqual(toggled, [[0, true]]);

    // Text stays text.
    w.__render(out, '<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n[x](javascript:alert(1))\n[y](data:text/html;base64,AAA)\n[z](//evil.example)\n[ok](http://ok.example/)\n# <b>h</b>');
    assert.equal(out.querySelector('script, img, b, [onerror]'), null);
    assert.ok(!out.innerHTML.includes('<script'));
    assert.ok(out.textContent.includes('<script>alert(1)</script>') && out.textContent.includes('javascript:alert(1)'));
    const links = [...out.querySelectorAll('a')];
    assert.deepEqual(links.map(a => a.getAttribute('href')), ['http://ok.example/']);
  } finally { await closeWindow(t); }
}

async function readingAndEditing() {
  const t = boot(); const {w, $, state, until} = t;
  try {
    await openFirst(t);
    // A note opens in the reading view.
    assert.equal($('preview').hidden, false); assert.equal($('markdown').hidden, true); assert.equal($('md-toolbar').hidden, true);
    assert.equal($('preview').querySelector('h1').textContent, 'Заголовок');
    assert.equal($('preview').getAttribute('tabindex'), '0');
    assert.equal(w.BerestaMd.mode(), 'read');
    // A click on a line opens the editor with the caret at the end of that line.
    const item = $('preview').querySelector('li[data-line="6"]');
    assert.equal(item.firstChild.textContent, 'два');
    item.click();
    assert.equal(w.BerestaMd.mode(), 'edit');
    assert.equal($('markdown').hidden, false); assert.equal($('preview').hidden, true); assert.equal($('md-toolbar').hidden, false);
    assert.equal(w.document.activeElement, $('markdown'));
    const end = RICH.split('\n').slice(0, 6).join('\n').length + 1 + '- два'.length;
    assert.deepEqual([$('markdown').selectionStart, $('markdown').selectionEnd], [end, end]);
    // Done goes back to reading. The text that was typed is saved and the editing session is closed.
    $('markdown').value = RICH.replace('# Заголовок', '# Новый заголовок');
    $('markdown').dispatchEvent(new w.Event('input', {bubbles: true}));
    $('md-done').click();
    assert.equal(w.BerestaMd.mode(), 'read');
    assert.equal($('preview').querySelector('h1').textContent, 'Новый заголовок');
    await until(() => state.patches.some(p => p.checkpoint), 'checkpoint after Done');
    assert.equal(state.patches[0].markdown.startsWith('# Новый заголовок'), true);
    assert.equal(state.patches[0].checkpoint, undefined);
    assert.equal(state.patches.at(-1).markdown, state.note.markdown);
    // Enter and E on the reading view open the editor with the caret at the end, Esc comes back.
    $('preview').focus();
    const enter = key(w, $('preview'), {key: 'Enter'});
    assert.equal(enter.defaultPrevented, true); assert.equal(w.BerestaMd.mode(), 'edit');
    assert.equal($('markdown').selectionStart, $('markdown').value.length);
    assert.equal(key(w, $('markdown'), {key: 'Escape'}).defaultPrevented, true);
    assert.equal(w.BerestaMd.mode(), 'read'); assert.equal(w.document.activeElement, $('preview'));
    key(w, $('preview'), {key: 'e', code: 'KeyE'});
    assert.equal(w.BerestaMd.mode(), 'edit');
    key(w, $('markdown'), {key: 'Escape'});
    // Ctrl+E does not count, links do not open the editor.
    key(w, $('preview'), {key: 'e', code: 'KeyE', ctrlKey: true});
    assert.equal(w.BerestaMd.mode(), 'read');
    $('preview').querySelector('a').addEventListener('click', event => event.preventDefault());
    $('preview').querySelector('a').click();
    assert.equal(w.BerestaMd.mode(), 'read');
    // Enter in the title moves into the text, at the start.
    key(w, $('title'), {key: 'Enter'});
    assert.equal(w.BerestaMd.mode(), 'edit'); assert.equal($('markdown').selectionStart, 0);
    assert.equal(w.document.activeElement, $('markdown'));
    // The same note drawn again while editing (a tool changed it) stays in the editor.
    w.document.dispatchEvent(new w.CustomEvent('beresta:note-rendered'));
    assert.equal(w.BerestaMd.mode(), 'edit');
  } finally { await closeWindow(t); }
}

async function checkboxes() {
  const t = boot(); const {w, $, state, until} = t;
  try {
    await openFirst(t);
    const first = $('preview').querySelector('input[type=checkbox]');
    assert.equal(first.disabled, false);
    first.click();
    assert.equal($('markdown').value.split('\n')[12], '- [x] задача');
    await until(() => state.patches.length === 1, 'autosave after a checkbox');
    assert.equal(state.patches[0].markdown.split('\n')[12], '- [x] задача');
    assert.equal(state.patches[0].version, 3);
    assert.equal(w.BerestaMd.mode(), 'read', 'a checkbox does not open the editor');
    const boxes = [...$('preview').querySelectorAll('input[type=checkbox]')];
    assert.deepEqual(boxes.map(b => b.checked), [true, true]);
    // And back.
    boxes[1].click();
    assert.equal($('markdown').value.split('\n')[13], '- [ ] готово');
    await until(() => state.patches.length === 2, 'second autosave');
    assert.equal(state.patches[1].markdown.split('\n')[13], '- [ ] готово');
    assert.equal(state.note.markdown, $('markdown').value);
  } finally { await closeWindow(t); }
}

async function listKeys() {
  const t = boot(); const {w} = t;
  try {
    const {ta, inputs} = await editing(t);
    const press = (text, init) => { put(ta, text); return key(w, ta, init); };
    const cases = [
      ['- один|', '- один\n- |'], ['* x|', '* x\n- |'], ['• x|', '• x\n- |'], ['+ x|', '+ x\n- |'],
      ['1. a|\n2. b\n3. c', '1. a\n2. |\n3. b\n4. c'], ['1) a|', '1) a\n2) |'],
      ['- [x] a|', '- [x] a\n- [ ] |'], ['- [ ] a|', '- [ ] a\n- [ ] |'], ['  - a|', '  - a\n  - |'],
      ['- ab|cd', '- ab\n- |cd'],
      // An empty item ends the list. A nested one first steps out.
      ['- a\n- |', '- a\n|'], ['1. a\n2. |', '1. a\n|'], ['- a\n  - |', '- a\n- |'],
    ];
    for (const [from, to] of cases) {
      const before = inputs();
      const event = press(from, {key: 'Enter'});
      assert.equal(show(ta), to, from); assert.equal(event.defaultPrevented, true, from);
      assert.ok(inputs() > before, `an input event follows ${from}`);
    }
    // Everything else is left to the browser.
    for (const [from, init] of [['abc|', {key: 'Enter'}], ['-| a', {key: 'Enter'}], ['- a|', {key: 'Enter', shiftKey: true}],
      ['- a|', {key: 'Enter', ctrlKey: true}], ['- ‹a›', {key: 'Enter'}], ['- a|', {key: 'Enter', isComposing: true}]]) {
      const before = inputs(), text = ta.value;
      const event = press(from, init);
      assert.equal(event.defaultPrevented, false, from); assert.equal(inputs(), before);
      assert.equal(ta.value, from.replace('|', '').replace('‹', '').replace('›', ''), text);
    }
    // Numbers below are renumbered when an item is added.
    press('1. a\n2. b|\n3. c\n\n1. other', {key: 'Enter'});
    assert.equal(show(ta), '1. a\n2. b\n3. |\n4. c\n\n1. other');
  } finally { await closeWindow(t); }
}

async function autoMarkers() {
  const t = boot(); const {w} = t;
  try {
    const {ta} = await editing(t);
    const typed = (text, inputType = 'insertText') => {
      put(ta, text); ta.dispatchEvent(new w.InputEvent('input', {bubbles: true, inputType, data: ' '}));
    };
    typed('* |'); assert.equal(show(ta), '- |');
    typed('+ |'); assert.equal(show(ta), '- |');
    typed('abc\n  * |'); assert.equal(show(ta), 'abc\n  - |');
    typed('1) |'); assert.equal(show(ta), '1. |');
    typed('2) a|'); assert.equal(show(ta), '2) a|');
    typed('a * |'); assert.equal(show(ta), 'a * |');
    typed('* |', 'deleteContentBackward'); assert.equal(show(ta), '* |', 'deleting is not typing');
    typed('* |', 'insertFromPaste'); assert.equal(show(ta), '* |', 'pasting is not typing');
    // Headings are plain Markdown, the reading view shows them.
    ta.value = '### Три\n## Два\n# Один'; t.w.BerestaMd.setMode('read');
    assert.deepEqual([...t.$('preview').querySelectorAll('h1,h2,h3')].map(h => h.tagName), ['H3', 'H2', 'H1']);
  } finally { await closeWindow(t); }
}

async function tabKeys() {
  const t = boot(); const {w} = t;
  try {
    const {ta} = await editing(t);
    const press = (text, init) => { put(ta, text); return key(w, ta, {key: 'Tab', ...init}); };
    let event = press('- a\n- b|', {});
    assert.equal(show(ta), '- a\n  - b|'); assert.equal(event.defaultPrevented, true); assert.equal(w.document.activeElement, ta);
    event = key(w, ta, {key: 'Tab', shiftKey: true});
    assert.equal(show(ta), '- a\n- b|'); assert.equal(event.defaultPrevented, true);
    // Nothing to move out: the browser leaves the field, that is the way out with the keyboard.
    event = press('- b|', {shiftKey: true});
    assert.equal(event.defaultPrevented, false); assert.equal(ta.value, '- b');
    // A plain line does nothing special.
    event = press('abc|', {}); assert.equal(event.defaultPrevented, false); assert.equal(ta.value, 'abc');
    event = press('abc|', {shiftKey: true}); assert.equal(event.defaultPrevented, false);
    // Ctrl+Tab and Alt+Tab belong to the browser.
    event = press('- a|', {ctrlKey: true}); assert.equal(event.defaultPrevented, false); assert.equal(ta.value, '- a');
    // Numbers: a nested list counts from 1 and the rest of the list closes the gap.
    press('1. a\n2. b|\n3. c', {});
    assert.equal(show(ta), '1. a\n  1. b|\n2. c');
    key(w, ta, {key: 'Tab', shiftKey: true});
    assert.equal(show(ta), '1. a\n2. b|\n3. c');
    // Several lines at once.
    press('‹- a\n- b\n- c›', {});
    assert.equal(ta.value, '  - a\n  - b\n  - c'); assert.deepEqual([ta.selectionStart, ta.selectionEnd], [0, ta.value.length]);
    key(w, ta, {key: 'Tab', shiftKey: true});
    assert.equal(ta.value, '- a\n- b\n- c');
    // Only list lines move.
    press('‹текст\n- a›', {});
    assert.equal(ta.value, 'текст\n  - a');
  } finally { await closeWindow(t); }
}

async function shortcuts() {
  const t = boot(); const {w} = t;
  try {
    const {ta, inputs} = await editing(t);
    const press = (text, init) => { put(ta, text); return key(w, ta, init); };
    let event = press('a‹bc›d', {key: 'b', code: 'KeyB', ctrlKey: true});
    assert.equal(show(ta), 'a**‹bc›**d'); assert.equal(event.defaultPrevented, true);
    key(w, ta, {key: 'b', code: 'KeyB', ctrlKey: true});
    assert.equal(show(ta), 'a‹bc›d', 'the same shortcut takes bold off');
    press('a‹bc›d', {key: 'i', code: 'KeyI', metaKey: true});
    assert.equal(show(ta), 'a*‹bc›*d');
    press('a|d', {key: 'b', code: 'KeyB', ctrlKey: true});
    assert.equal(show(ta), 'a**|**d', 'no selection: a pair of marks with the caret inside');
    press('a‹bc›d', {key: 'и', code: 'KeyB', ctrlKey: true});
    assert.equal(show(ta), 'a**‹bc›**d', 'other keyboard layouts work through the key code');
    press('abc|', {key: '8', code: 'Digit8', ctrlKey: true, shiftKey: true});
    assert.equal(show(ta), '- abc|');
    press('abc|', {key: '(', code: 'Digit9', metaKey: true, shiftKey: true});
    assert.equal(show(ta), '- [ ] abc|');
    press('a\nb|', {key: '&', code: 'Digit7', ctrlKey: true, shiftKey: true});
    assert.equal(show(ta), 'a\n1. b|');
    press('‹a\nb›', {key: '&', code: 'Digit7', ctrlKey: true, shiftKey: true});
    assert.equal(show(ta), '‹1. a\n2. b›');
    // And off again.
    key(w, ta, {key: '&', code: 'Digit7', ctrlKey: true, shiftKey: true});
    assert.equal(ta.value, 'a\nb');
    // Keys that are not ours.
    for (const init of [{key: 'b', code: 'KeyB'}, {key: 'b', code: 'KeyB', ctrlKey: true, altKey: true}, {key: 'x', code: 'KeyX', ctrlKey: true}]) {
      const before = inputs(); event = press('ab|', init);
      assert.equal(event.defaultPrevented, false); assert.equal(ta.value, 'ab'); assert.equal(inputs(), before);
    }
  } finally { await closeWindow(t); }
}

async function toolbar() {
  const t = boot(); const {w, $, state, until} = t;
  try {
    const {ta, inputs} = await editing(t);
    const bar = $('md-toolbar');
    assert.equal(bar.getAttribute('role'), 'toolbar');
    const buttons = [...bar.querySelectorAll('.md-tool')];
    assert.deepEqual(buttons.map(b => b.getAttribute('aria-label')),
      ['Заголовок', 'Жирный', 'Курсив', 'Список', 'Нумерованный список', 'Чек-лист', 'Цитата']);
    for (const b of buttons) { assert.equal(b.type, 'button'); assert.ok(b.title.startsWith(b.getAttribute('aria-label')), b.title); }
    assert.equal($('md-done').textContent, 'Готово');
    const click = (id, text) => { put(ta, text); bar.querySelector(`[data-md=${id}]`).click(); return show(ta); };
    assert.equal(click('bold', 'a‹bc›d'), 'a**‹bc›**d');
    assert.equal(click('italic', 'a‹bc›d'), 'a*‹bc›*d');
    assert.equal(click('bullet', 'a|'), '- a|');
    assert.equal(click('bullet', '- a|'), 'a|', 'a second press takes it off');
    assert.equal(click('number', 'a\nb|'), 'a\n1. b|');
    assert.equal(click('check', 'a|'), '- [ ] a|');
    assert.equal(click('quote', 'a|'), '> a|');
    assert.equal(click('quote', '> a|'), 'a|');
    assert.equal(click('bullet', '1. a|'), '- a|', 'a list of another kind is converted');
    // The heading button goes through the levels.
    assert.equal(click('heading', 'a|'), '# a|');
    assert.equal(click('heading', '# a|'), '## a|');
    assert.equal(click('heading', '## a|'), '### a|');
    assert.equal(click('heading', '### a|'), 'a|');
    assert.equal(w.document.activeElement, ta, 'the field keeps the focus');
    // A button press is a change like typing: the input event fires and autosave sends it.
    const before = inputs();
    put(ta, 'Новый текст|'); ta.dispatchEvent(new w.Event('input', {bubbles: true}));
    bar.querySelector('[data-md=bullet]').click();
    assert.ok(inputs() > before + 1);
    await until(() => state.patches.some(p => p.markdown === '- Новый текст'), 'autosave of a toolbar change');
    // The pressed button does not take the focus away from the field.
    const down = new w.MouseEvent('mousedown', {bubbles: true, cancelable: true});
    buttons[0].dispatchEvent(down); assert.equal(down.defaultPrevented, true);
  } finally { await closeWindow(t); }
}

async function undoStack() {
  const t = boot(); const {w} = t;
  try {
    const {ta, inputs} = await editing(t);
    // With execCommand the change goes through the browser, so Ctrl+Z can take it back.
    const commands = [];
    w.document.execCommand = (command, ui, value) => {
      commands.push([command, value]);
      const field = w.document.activeElement;
      field.setRangeText(command === 'insertText' ? value : '', field.selectionStart, field.selectionEnd, 'end');
      field.dispatchEvent(new w.InputEvent('input', {bubbles: true, inputType: command === 'insertText' ? 'insertText' : 'deleteContentBackward', data: value}));
      return true;
    };
    put(ta, 'abc|'); ta.focus();
    const event = key(w, ta, {key: '8', code: 'Digit8', ctrlKey: true, shiftKey: true});
    assert.equal(event.defaultPrevented, true);
    assert.equal(show(ta), '- abc|');
    assert.deepEqual(commands, [['insertText', '- ']], 'only the changed part is inserted');
    assert.equal(inputs(), 1, 'one input event for one change');
    put(ta, '- a\n- |'); commands.length = 0;
    key(w, ta, {key: 'Enter'});
    assert.equal(show(ta), '- a\n|');
    assert.deepEqual(commands, [['delete', undefined]]);
    // If execCommand refuses, the text still changes.
    w.document.execCommand = () => false;
    put(ta, '- a|'); key(w, ta, {key: 'Enter'});
    assert.equal(show(ta), '- a\n- |');
    delete w.document.execCommand;
  } finally { await closeWindow(t); }
}

async function newNoteField() {
  const t = boot(); const {w, $} = t;
  try {
    await settled();
    const thought = $('thought'), toggle = $('thought-format'), bar = $('thought-toolbar');
    assert.equal(bar.hidden, true); assert.equal(toggle.textContent, 'Аа');
    assert.equal(toggle.getAttribute('aria-expanded'), 'false'); assert.equal(toggle.getAttribute('aria-controls'), 'thought-toolbar');
    assert.ok(toggle.getAttribute('aria-label'));
    toggle.click();
    assert.equal(bar.hidden, false); assert.equal(toggle.getAttribute('aria-expanded'), 'true');
    assert.equal(bar.querySelectorAll('.md-tool').length, 7);
    put(thought, 'a|'); bar.querySelector('[data-md=bullet]').click();
    assert.equal(show(thought), '- a|');
    toggle.click(); assert.equal(bar.hidden, true);
    put(thought, '1. раз|'); key(w, thought, {key: 'Enter'});
    assert.equal(show(thought), '1. раз\n2. |');
    put(thought, '1. раз\n2. |'); key(w, thought, {key: 'Enter'});
    assert.equal(show(thought), '1. раз\n|');
    put(thought, 'просто|'); assert.equal(key(w, thought, {key: 'Enter'}).defaultPrevented, false);
    // Esc does not close the field, it only exists in the note.
    assert.equal(key(w, thought, {key: 'Escape'}).defaultPrevented, false);
  } finally { await closeWindow(t); }
}

async function gear() {
  // The gear is visible at rest, full green on hover, and light on the dark selected row.
  const buttons = read('buttons.css');
  assert.match(buttons, /button\.category-more\{color:#57715c!important;opacity:1!important\}/);
  assert.match(buttons, /button\.category-more:hover:not\(:disabled\)[^{]*\{color:#345943!important/);
  assert.match(buttons, /@media\(min-width:641px\)\{\s*\.category-item:has\(>\.category-button\[aria-pressed=true\]\)>button\.category-more\{color:#e6efe0!important/);
  const t = boot({categories: [{id: 'c1', name: 'Дом', version: 1}, {id: 'c2', name: 'Работа', version: 1}]}); const {w, $} = t;
  try {
    await settled();
    const items = [...$('category-navigation').querySelectorAll('.category-item[data-category-id]')];
    assert.equal(items.length, 2);
    for (const item of items) {
      const more = item.querySelector('.category-more');
      assert.ok(more.getAttribute('aria-label').startsWith('Настройки категории'), more.getAttribute('aria-label'));
      assert.equal(more.title, 'Настройки категории');
      assert.ok(more.querySelector('svg circle') && more.querySelector('svg path'), 'a gear drawn as SVG');
      assert.equal(more.querySelector('svg').getAttribute('width'), '16');
      assert.ok(!more.innerHTML.includes('M5 12h.01'), 'no row of dots any more');
      assert.equal(more.textContent.trim(), '');
    }
    assert.match(items[0].querySelector('.category-more').getAttribute('aria-label'), /Дом/);
    const more = items[1].querySelector('.category-more'), menu = $('category-menu');
    assert.equal(menu.hidden, true);
    more.click();
    assert.equal(menu.hidden, false); assert.equal(more.getAttribute('aria-expanded'), 'true');
    assert.equal(menu.querySelectorAll('[role=menuitem]').length, 3);
    more.click(); assert.equal(menu.hidden, true);
    // The phone menu next to the chips is about navigation, not about categories. It keeps its icon.
    assert.ok($('nav-more'));
  } finally { await closeWindow(t); }
}

module.exports = async () => {
  await rendering();
  await readingAndEditing();
  await checkboxes();
  await listKeys();
  await autoMarkers();
  await tabKeys();
  await shortcuts();
  await toolbar();
  await undoStack();
  await newNoteField();
  await gear();
  console.log('Markdown editor checks passed: safe renderer, reading and editing views, task checkboxes, list keys, markers, Tab, shortcuts, toolbar, undo path, new-note field, gear.');
};
if (require.main === module) module.exports().catch(error => { console.error(error); process.exitCode = 1; });
