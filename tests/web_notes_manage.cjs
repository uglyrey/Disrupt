'use strict';
// Корзина, удаление задач, объединение заметок и ручные связи: окна подтверждения, плашка «Отменить», список корзины,
// выбор заметок для объединения и раздел связей в меню заметки. Сервер подменён, настоящих запросов нет.
const assert = require('node:assert/strict'), fs = require('fs'), path = require('path'), {JSDOM} = require('jsdom');
const root = path.join(__dirname, '../app/static');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const reply = (data, status = 200) => ({ok: status < 400, status, headers: new Headers(), json: async () => structuredClone(data)});
async function settled() { for (let i = 0; i < 30; i++) await new Promise(resolve => setImmediate(resolve)); }

const TITLES = {n1: 'План отпуска', n2: 'Рабочие дела', n3: 'Идеи для дома', n4: 'Четвёртая', n5: 'Пятая', n6: 'Шестая'};

function boot({usage = {}} = {}) {
  const dom = new JSDOM(read('index.html'), {url: 'https://beresta.invalid/', runScripts: 'outside-only'}), w = dom.window;
  const $ = id => w.document.getElementById(id);
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  try { Object.defineProperty(w.crypto, 'randomUUID', {value: require('node:crypto').randomUUID, configurable: true}); } catch { /* provided */ }
  w.confirm = () => true;
  w.noteActionsDebounceMs = 0; w.mergePollMs = 5; w.undoToastMs = 600000; w.reminderDebounceMs = 0;
  const state = {
    calls: [], version: 1, items: [], conclusions: [], links: [{note_id: 'n2', title: 'Рабочие дела', category_id: null, updated_at: 1791220000, created_at: 1791220000}],
    mergeFails: 0, mergePosts: 0, jobPolls: 0, mergeJob: 'ok', jobs: [], noteDeleted: false,
    trash: [
      {id: 't1', title: 'Старая заметка', version: 2, deleted_at: Date.now() / 1000 - 3 * 86400, purge_at: Date.now() / 1000 + 27 * 86400, category_id: null, merged_into: null, merged_into_title: null},
      {id: 't2', title: 'Склеенная', version: 1, deleted_at: Date.now() / 1000 - 86400, purge_at: Date.now() / 1000 + 29 * 86400, category_id: null, merged_into: 'n9', merged_into_title: 'Итог'},
    ],
    tasks: [{id: 'k1', kind: 'task', text: 'Позвонить', status: 'open', version: 1, note_id: 'n1', note_title: 'План отпуска', note_version: 1}],
    usage: {simulation: false, daily_unit_limit: 30, daily_units_used: 3, daily_units_remaining: 27, limit_resets_at: new Date(Date.now() + 3600000).toISOString(), ...usage},
  };
  state.items = [
    {id: 'i1', kind: 'task', text: 'Купить билеты', status: 'open', version: 1},
    {id: 'i2', kind: 'idea', text: 'Взять палатку', status: 'open', version: 1},
  ];
  state.conclusions = [{id: 'c1', text: 'Поездка нужна', source_quote: 'отпуск', status: 'proposed'}];
  const full = id => ({id, capture_id: `k-${id}`, channel: 'web', input_kind: 'text', title: id === 'n9' ? 'Объединённая' : TITLES[id], markdown: 'Текст',
    original_text: 'Исходник', items: id === 'n1' ? state.items : [], conclusions: id === 'n1' ? state.conclusions : [], version: id === 'n1' ? state.version : 1,
    provider: 'manual', category_id: null, structure_confirmed_at: null, created_at: 1791220000, updated_at: 1791220000});
  const summaries = q => Object.entries(TITLES)
    .filter(([id, title]) => !(id === 'n1' && state.noteDeleted) && (!q || title.toLowerCase().includes(q.toLowerCase())))
    .map(([id, title]) => ({id, title, version: 1, updated_at: 1791220000, category_id: null, channel: 'web', input_kind: 'text'}));
  w.fetch = async (input, options = {}) => {
    const u = new URL(input, w.location.href), method = options.method || 'GET', p = u.pathname;
    const body = options.body && typeof options.body === 'string' ? JSON.parse(options.body) : null;
    state.calls.push({url: u, method, body, headers: options.headers || {}});
    if (p === '/health') return reply({simulation: false});
    if (p === '/api/v1/auth/me') return reply({username: 'tester', csrf_token: 'csrf'});
    if (p === '/api/v1/provider/usage') return reply(state.usage);
    if (p === '/api/v1/categories') return reply([]);
    if (p === '/api/v1/jobs' && method === 'GET') return reply(state.jobs);
    if (p === '/api/v1/notes' && method === 'GET') return reply(summaries(u.searchParams.get('q')));
    if (p === '/api/v1/notes/merge' && method === 'POST') {
      state.mergePosts++;
      if (state.mergeFails > 0) { state.mergeFails--; return reply({detail: 'Сервис недоступен'}, 500); }
      return reply({id: 'j1', capture_id: 'kj', status: 'queued', kind: 'merge', merge_note_ids: body.note_ids, note_id: null, error_code: null}, 202);
    }
    if (p === '/api/v1/jobs/j1') {
      state.jobPolls++;
      if (state.mergeJob === 'failed') return reply({id: 'j1', status: 'failed', kind: 'merge', note_id: null, error_code: 'merge_source_missing'});
      return reply({id: 'j1', status: state.jobPolls < 2 ? 'running' : 'succeeded', kind: 'merge', note_id: state.jobPolls < 2 ? null : 'n9', error_code: null});
    }
    if (p === '/api/v1/notes/n9') return reply(full('n9'));
    if (/^\/api\/v1\/notes\/n\d$/.test(p) && method === 'GET') return reply(full(p.slice(-2)));
    if (p === '/api/v1/notes/n1' && method === 'DELETE') { state.noteDeleted = true; return reply(null, 204); }
    if (/^\/api\/v1\/notes\/n\d\/related$/.test(p)) {
      return reply({note_id: 'n1', related: [{note_id: 'n3', title: 'Идеи для дома', category_id: null, updated_at: 1791220000, score: 0.4, shared_terms: ['дом']}]});
    }
    if (/^\/api\/v1\/notes\/n\d\/reminders$/.test(p)) return reply([]);
    if (p === '/api/v1/telegram/links') return reply({identities: [], pending: []});
    if (/\/(opened|original-opened)$/.test(p) || p === '/api/v1/search/events') return reply(null, 204);
    if (p === '/api/v1/notes/n1/links' && method === 'GET') return reply({note_id: 'n1', links: state.links});
    if (p === '/api/v1/notes/n1/links' && method === 'POST') {
      state.links = [...state.links, {note_id: body.note_id, title: TITLES[body.note_id], category_id: null, updated_at: 1791220000, created_at: 1791220001}];
      return reply({note_id: 'n1', links: state.links}, 201);
    }
    if (/^\/api\/v1\/notes\/n\d\/links$/.test(p) && method === 'GET') return reply({note_id: p.split('/')[4], links: []});
    const unlink = p.match(/^\/api\/v1\/notes\/n1\/links\/(n\d)$/);
    if (unlink && method === 'DELETE') { state.links = state.links.filter(link => link.note_id !== unlink[1]); return reply(null, 204); }
    const dropped = p.match(/^\/api\/v1\/notes\/n1\/items\/(i\d)$/);
    if (dropped && method === 'DELETE') {
      state.removed = state.items.find(item => item.id === dropped[1]);
      state.items = state.items.filter(item => item.id !== dropped[1]); state.version++;
      return reply(full('n1'));
    }
    const back = p.match(/^\/api\/v1\/notes\/n1\/items\/(i\d)\/restore$/);
    if (back && method === 'POST') { state.items = [...state.items, state.removed]; state.version++; return reply(full('n1')); }
    if (p === '/api/v1/notes/n1/conclusions/c1' && method === 'DELETE') { state.conclusions = []; state.version++; return reply(full('n1')); }
    if (p === '/api/v1/tasks') {
      const open = state.tasks.filter(task => task.status === 'open');
      return reply({status: 'open', limit: 50, offset: 0, has_more: false, counts: {open: open.length, completed: 0}, items: open});
    }
    const taskDrop = p.match(/^\/api\/v1\/notes\/n1\/items\/(k\d)$/);
    if (taskDrop && method === 'DELETE') { state.removedTask = state.tasks.find(task => task.id === taskDrop[1]); state.tasks = state.tasks.filter(task => task.id !== taskDrop[1]); return reply({...full('n1'), version: 7}); }
    const taskBack = p.match(/^\/api\/v1\/notes\/n1\/items\/(k\d)\/restore$/);
    if (taskBack && method === 'POST') { state.tasks.push(state.removedTask); return reply(full('n1')); }
    if (p === '/api/v1/trash') return reply({trash_days: 30, notes: state.trash});
    const restore = p.match(/^\/api\/v1\/trash\/(t\d)\/restore$/);
    if (restore && method === 'POST') { state.trash = state.trash.filter(row => row.id !== restore[1]); return reply(full('n2')); }
    const forget = p.match(/^\/api\/v1\/trash\/(t\d)$/);
    if (forget && method === 'DELETE') { state.trash = state.trash.filter(row => row.id !== forget[1]); return reply(null, 204); }
    throw new Error(`Unexpected ${method} ${p}`);
  };
  w.eval(['app.js', 'workspace.js', 'brain.js', 'tasks.js', 'note-actions.js', 'trash.js', 'focus.js'].map(read).join('\n'));
  const wait = ms => new Promise(resolve => w.setTimeout(resolve, ms));
  const until = async (check, what) => { for (let i = 0; i < 300; i++) { if (check()) return; await wait(5); } throw new Error(`Timed out: ${what}`); };
  const calls = (method, pattern) => state.calls.filter(c => c.method === method && pattern.test(c.url.pathname + c.url.search));
  return {dom, w, $, state, wait, until, calls};
}
async function openNote(t) {
  await settled();
  t.$('notes').querySelector('button[data-note-id="n1"], button').click();
  await t.until(() => !t.$('note-card').hidden && t.$('title').value === 'План отпуска', 'note opened');
  await settled();
}
const click = (t, selector) => { const node = t.w.document.querySelector(selector); assert.ok(node, `${selector} exists`); node.click(); };
const type = (t, field, value) => { field.value = value; field.dispatchEvent(new t.w.Event('input', {bubbles: true})); };
async function closeWindow(t) { await settled(); t.w.close(); }

async function menuAndLinks() {
  const t = boot();
  try {
    await openNote(t);
    const tool = t.w.document.querySelector('#note-tools .tool[data-tool="actions"]');
    assert.ok(tool, 'The note menu is a tool of the toolbar');
    assert.equal(t.$('tool-actions').getAttribute('aria-label'), 'Действия с заметкой');
    t.$('tool-actions').click();
    assert.equal(t.$('pop-actions').hidden, false, 'focus.js opens the menu like any other tool');
    assert.deepEqual([...t.$('pop-actions').querySelectorAll('.na-item')].map(b => b.textContent.trim()), ['Объединить с…', 'Связать с заметкой…', 'Удалить заметку']);
    // Manual links and the automatic neighbours sit below the actions.
    await t.until(() => t.$('na-links-list').children.length === 1, 'manual links');
    assert.equal(t.$('na-links-list').textContent.includes('Рабочие дела'), true);
    assert.equal(t.$('na-links-empty').hidden, true);
    await t.until(() => !t.$('na-similar').hidden, 'similar notes');
    assert.equal(t.$('na-similar-list').textContent.trim(), 'Идеи для дома');
    assert.equal(t.w.document.querySelector('#na-links-title').textContent, 'Связанные заметки');
    assert.equal(t.w.document.querySelector('#na-similar-title').textContent, 'Похожие');
    // Remove the manual link.
    t.w.document.querySelector('.na-link-remove').click();
    await t.until(() => t.calls('DELETE', /\/notes\/n1\/links\/n2$/).length === 1, 'link removed');
    await t.until(() => t.$('na-links-list').children.length === 0 && !t.$('na-links-empty').hidden, 'links redrawn');
    // Link a note by hand.
    click(t, '[data-action="link"]');
    await t.until(() => t.w.document.querySelectorAll('.na-row input[type=radio]').length === 5, 'picker lists the other notes, not the open one');
    assert.equal(t.w.document.querySelector('.na-row input[value="n1"]'), null);
    assert.equal(t.w.document.querySelector('[data-link="go"]').disabled, true);
    type(t, t.$('na-link-search'), 'Идеи');
    await t.until(() => t.w.document.querySelectorAll('.na-row input[type=radio]').length === 1, 'search narrows the list');
    t.w.document.querySelector('.na-row input[type=radio]').click();
    assert.equal(t.w.document.querySelector('[data-link="go"]').disabled, false);
    t.w.document.querySelector('[data-link="go"]').click();
    await t.until(() => t.calls('POST', /\/notes\/n1\/links$/).length === 1, 'link saved');
    assert.deepEqual(t.calls('POST', /\/notes\/n1\/links$/)[0].body, {note_id: 'n3'});
    await t.until(() => !t.w.document.querySelector('.na-dialog') && t.$('na-links-list').children.length === 1, 'dialog closed, list redrawn');
    assert.equal(t.$('na-links-list').textContent.includes('Идеи для дома'), true);
  } finally { await closeWindow(t); }
}

async function deleteNoteDialog() {
  const t = boot();
  try {
    await openNote(t);
    t.$('tool-actions').click();
    click(t, '[data-action="delete"]');
    await t.until(() => t.w.document.querySelector('.na-confirm'), 'confirm dialog');
    const dialog = t.w.document.querySelector('.na-confirm');
    assert.ok(dialog.open, 'A confirm dialog opens');
    assert.match(dialog.textContent, /План отпуска/);
    assert.match(dialog.textContent, /корзину на 30 дней/);
    assert.equal(dialog.querySelector('[data-confirm=yes]').textContent, 'Удалить');
    dialog.querySelector('[data-confirm=no]').click();
    await settled();
    assert.equal(t.calls('DELETE', /^\/api\/v1\/notes\/n1\?/).length, 0, 'Cancel deletes nothing');
    assert.equal(t.w.document.querySelector('.na-confirm'), null);
    assert.equal(t.$('note-card').hidden, false);
    t.$('tool-actions').click();
    click(t, '[data-action="delete"]');
    await t.until(() => t.w.document.querySelector('.na-confirm'), 'confirm dialog again');
    t.w.document.querySelector('.na-confirm [data-confirm=yes]').click();
    await t.until(() => t.calls('DELETE', /^\/api\/v1\/notes\/n1\?version=1$/).length === 1, 'delete request with the version');
    await t.until(() => t.$('note-card').hidden && !t.$('capture-card').hidden, 'back to the new note screen');
    assert.match(t.$('message').textContent, /Заметка в корзине/);
    await t.until(() => !t.$('notes').querySelector('[data-note-id="n1"]'), 'list without the deleted note');
  } finally { await closeWindow(t); }
}

async function deleteItemsAndToast() {
  const t = boot();
  try {
    await openNote(t);
    click(t, '#tool-tasks');
    const trashes = () => [...t.w.document.querySelectorAll('#items .item .item-trash')];
    assert.equal(trashes().length, 2, 'A task and an idea each have a trash icon');
    assert.equal(trashes()[0].getAttribute('aria-label'), 'Удалить');
    trashes()[0].click();
    await t.until(() => t.calls('DELETE', /\/notes\/n1\/items\/i1\?version=1$/).length === 1, 'item delete with the note version');
    await t.until(() => t.w.document.querySelectorAll('#items .item').length === 1 && t.w.document.querySelector('.na-toast'), 'row left and toast shown');
    const toast = t.w.document.querySelector('.na-toast');
    assert.match(toast.textContent, /Задача удалена\./);
    assert.equal(toast.querySelector('[data-toast=undo]').textContent, 'Отменить');
    assert.equal(toast.getAttribute('role'), 'status');
    toast.querySelector('[data-toast=undo]').click();
    await t.until(() => t.calls('POST', /\/items\/i1\/restore$/).length === 1, 'restore request');
    await t.until(() => t.w.document.querySelectorAll('#items .item').length === 2 && !t.w.document.querySelector('.na-toast'), 'row is back, toast gone');
    // The idea goes the same way and the toast leaves by itself after the set time.
    t.w.undoToastMs = 30;
    t.w.document.querySelector('#items .item.is-idea .item-trash').click();
    await t.until(() => t.w.document.querySelector('.na-toast'), 'toast for the idea');
    assert.match(t.w.document.querySelector('.na-toast').textContent, /Идея удалена\./);
    await t.until(() => !t.w.document.querySelector('.na-toast'), 'toast expires');
    assert.equal(t.calls('POST', /\/items\/i2\/restore$/).length, 0, 'Nothing is restored on its own');
    // An AI conclusion asks first, then goes.
    const remove = t.w.document.querySelector('#conclusions .conclusion-delete');
    assert.ok(remove, 'A conclusion can be removed');
    remove.click();
    await t.until(() => t.w.document.querySelector('.na-confirm'), 'confirm for the conclusion');
    t.w.document.querySelector('.na-confirm [data-confirm=yes]').click();
    await t.until(() => t.calls('DELETE', /\/notes\/n1\/conclusions\/c1\?version=\d+$/).length === 1, 'conclusion removed');
    await t.until(() => !t.w.document.querySelector('#conclusions .conclusion'), 'conclusion gone from the page');
  } finally { await closeWindow(t); }
}

async function mergePicker() {
  const t = boot();
  try {
    await openNote(t);
    t.$('tool-actions').click();
    click(t, '[data-action="merge"]');
    await t.until(() => t.w.document.querySelectorAll('.na-row input[type=checkbox]').length === 6, 'all notes in the picker');
    assert.equal(t.w.document.querySelector('.na-row input[value="n1"]').checked, true, 'The open note is chosen');
    const submit = t.w.document.querySelector('[data-merge="go"]');
    assert.equal(submit.disabled, true, 'One note is not enough');
    await t.until(() => /осталось 27 из 30/.test(t.w.document.querySelector('.na-extra').textContent), 'allowance');
    assert.match(t.w.document.querySelector('.na-extra').textContent, /Стоит 1 единицу ИИ\./);
    assert.match(t.w.document.querySelector('.na-count').textContent, /Выбрано 1 из 5/);
    // Search keeps the choice.
    type(t, t.$('na-merge-search'), 'Рабочие');
    await t.until(() => t.w.document.querySelectorAll('.na-row input[type=checkbox]').length === 1, 'search');
    t.w.document.querySelector('.na-row input[value="n2"]').click();
    assert.equal(submit.disabled, false);
    assert.equal(t.w.document.querySelectorAll('.na-chip').length, 2, 'Both choices stay as chips while searching');
    type(t, t.$('na-merge-search'), '');
    await t.until(() => t.w.document.querySelectorAll('.na-row input[type=checkbox]').length === 6, 'list again');
    // Five at most.
    for (const id of ['n3', 'n4', 'n5']) t.w.document.querySelector(`.na-row input[value="${id}"]`).click();
    assert.match(t.w.document.querySelector('.na-count').textContent, /Выбрано 5 из 5/);
    assert.equal(t.w.document.querySelector('.na-row input[value="n6"]').disabled, true, 'The sixth cannot be chosen');
    t.w.document.querySelector('.na-chip').click();
    assert.equal(t.w.document.querySelector('.na-row input[value="n6"]').disabled, false, 'Removing a chip frees a place');
    t.w.document.querySelector('.na-row input[value="n5"]').click();
    t.w.document.querySelector('.na-row input[value="n4"]').click();
    // A failed request keeps the key, so a repeat never spends a second unit.
    t.state.mergeFails = 1;
    submit.click();
    await t.until(() => t.state.mergePosts === 1 && /Сервис недоступен/.test(t.w.document.querySelector('.na-status').textContent), 'first request failed');
    submit.click();
    await t.until(() => t.state.mergePosts === 2, 'second request');
    const keys = t.calls('POST', /\/notes\/merge$/).map(c => c.body.idempotency_key);
    assert.equal(keys[0], keys[1], 'The same choice keeps the idempotency key');
    assert.ok(/^[A-Za-z0-9_.:-]{1,90}$/.test(keys[0]));
    assert.deepEqual([...t.calls('POST', /\/notes\/merge$/)[1].body.note_ids].sort(), ['n2', 'n3']);
    await t.until(() => !t.w.document.querySelector('.na-dialog') && t.$('title').value === 'Объединённая', 'the new note opens when it is ready');
    assert.match(t.$('message').textContent, /Заметки объединены/);
  } finally { await closeWindow(t); }
}

async function mergeFailureAndLimit() {
  const t = boot();
  try {
    t.state.mergeJob = 'failed';
    await openNote(t);
    t.$('tool-actions').click();
    click(t, '[data-action="merge"]');
    await t.until(() => t.w.document.querySelectorAll('.na-row input[type=checkbox]').length === 6, 'picker');
    t.w.document.querySelector('.na-row input[value="n2"]').click();
    t.w.document.querySelector('[data-merge="go"]').click();
    await t.until(() => /Ничего не объединено/.test(t.w.document.querySelector('.na-status').textContent), 'failure text');
    assert.ok(t.w.document.querySelector('.na-dialog'), 'The picker stays open after a failure');
    assert.equal(t.w.document.querySelector('[data-merge="go"]').disabled, false, 'The notes can be merged again');
    assert.equal(t.$('title').value, 'План отпуска', 'The open note did not change');
  } finally { await closeWindow(t); }
  const spent = boot({usage: {daily_units_remaining: 0}});
  try {
    await openNote(spent);
    spent.$('tool-actions').click();
    click(spent, '[data-action="merge"]');
    await spent.until(() => spent.w.document.querySelectorAll('.na-row input[type=checkbox]').length === 6, 'picker');
    spent.w.document.querySelector('.na-row input[value="n2"]').click();
    await spent.until(() => /лимит исчерпан/.test(spent.w.document.querySelector('.na-extra').textContent), 'spent allowance');
    assert.equal(spent.w.document.querySelector('[data-merge="go"]').disabled, true, 'No unit left, no merge');
  } finally { await closeWindow(spent); }
  // A merge in the history has no «send again» buttons: its text is not a note to re-send.
  const history = boot();
  try {
    history.state.jobs = [{id: 'j1', capture_id: 'kj', status: 'failed', provider: 'mock', kind: 'merge', note_id: null, original_text: '[Объединение заметок]\nтекст',
      created_at: 1791280000, error_code: 'merge_source_missing', merge_note_ids: ['n1', 'n2']}];
    await settled();
    history.$('new-note').click();
    await history.until(() => history.$('jobs').children.length === 1, 'history row');
    assert.match(history.$('jobs').textContent, /Объединение заметок/);
    assert.equal(history.$('jobs').querySelector('[data-retry]'), null);
    assert.equal(history.$('jobs').querySelector('pre'), null, 'The merge text is not shown');
  } finally { await closeWindow(history); }
}

async function trashList() {
  const t = boot();
  try {
    await settled();
    const entry = t.$('open-trash');
    assert.ok(entry, 'The general menu has a Корзина entry');
    assert.equal(entry.textContent, 'Корзина');
    assert.equal(entry.closest('#nav-menu') !== null, true);
    entry.click();
    const dialog = t.w.document.querySelector('.trash-dialog');
    assert.ok(dialog && dialog.open);
    await t.until(() => dialog.querySelectorAll('.trash-row').length === 2, 'trash rows');
    assert.match(dialog.textContent, /Старая заметка/);
    assert.match(dialog.textContent, /Удалена 3 дня назад\. Исчезнет через 27 дней\./);
    assert.match(dialog.textContent, /Объединена в «Итог»/);
    assert.deepEqual([...dialog.querySelectorAll('.trash-row')[0].querySelectorAll('button')].map(b => b.textContent), ['Восстановить', 'Удалить навсегда']);
    // Restore.
    dialog.querySelector('.trash-restore').click();
    await t.until(() => t.calls('POST', /\/trash\/t1\/restore$/).length === 1 && dialog.querySelectorAll('.trash-row').length === 1, 'restored');
    assert.match(t.$('message').textContent, /Заметка восстановлена/);
    // Delete forever asks first.
    dialog.querySelector('.trash-forget').click();
    await t.until(() => t.w.document.querySelector('.na-confirm'), 'confirm forever');
    const confirmDialog = t.w.document.querySelector('.na-confirm');
    assert.match(confirmDialog.textContent, /Вернуть её будет нельзя/);
    assert.equal(confirmDialog.querySelector('[data-confirm=yes]').textContent, 'Удалить навсегда');
    confirmDialog.querySelector('[data-confirm=no]').click();
    await settled();
    assert.equal(t.calls('DELETE', /\/trash\/t2$/).length, 0, 'Cancel keeps the note');
    dialog.querySelector('.trash-forget').click();
    await t.until(() => t.w.document.querySelector('.na-confirm'), 'confirm again');
    t.w.document.querySelector('.na-confirm [data-confirm=yes]').click();
    await t.until(() => t.calls('DELETE', /\/trash\/t2$/).length === 1, 'deleted forever');
    await t.until(() => /Корзина пуста/.test(dialog.textContent), 'empty state');
  } finally { await closeWindow(t); }
}

async function tasksScreenTrash() {
  const t = boot();
  try {
    await settled();
    t.$('nav-tasks').click();
    await t.until(() => t.$('tasks-list').querySelectorAll('.tk-row').length === 1, 'task row');
    const trash = t.$('tasks-list').querySelector('.tk-trash');
    assert.ok(trash, 'A task row has a trash icon');
    trash.click();
    await t.until(() => t.calls('DELETE', /\/notes\/n1\/items\/k1\?version=1$/).length === 1, 'delete with the note version');
    await t.until(() => t.$('tasks-list').querySelectorAll('.tk-row').length === 0 && t.w.document.querySelector('.na-toast'), 'row left, toast shown');
    t.w.document.querySelector('.na-toast [data-toast=undo]').click();
    await t.until(() => t.calls('POST', /\/items\/k1\/restore$/).length === 1, 'restore');
    await t.until(() => t.$('tasks-list').querySelectorAll('.tk-row').length === 1, 'row back after reload');
  } finally { await closeWindow(t); }
}

module.exports = async () => {
  await menuAndLinks();
  await deleteNoteDialog();
  await deleteItemsAndToast();
  await mergePicker();
  await mergeFailureAndLimit();
  await trashList();
  await tasksScreenTrash();
  console.log('Notes manage checks passed: menu and manual links, delete dialog, item trash with undo toast, merge picker, trash list, tasks trash.');
};
if (require.main === module) module.exports().catch(error => { console.error(error); process.exitCode = 1; });
