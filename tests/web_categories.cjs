'use strict';
// Sidebar categories: plus button, inline create and rename, context menu, delete with confirmation.
const assert = require('node:assert/strict'), fs = require('fs'), path = require('path'), {JSDOM} = require('jsdom');
const root = path.join(__dirname, '../app/static');

module.exports = async () => {
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'index.html'), 'utf8'), {
    url: 'https://beresta.invalid/', runScripts: 'outside-only',
  });
  const w = dom.window, $ = id => w.document.getElementById(id);
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  const reply = (data, status = 200) => ({ok: status < 400, status, headers: new Headers(), json: async () => structuredClone(data)});
  const state = {
    categories: [{id: 'c1', name: 'Research', version: 1}, {id: 'c2', name: 'Дом', version: 1}],
    calls: [], nextId: 3,
  };
  w.fetch = async (input, options = {}) => {
    const url = new URL(input, w.location.href), method = options.method || 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    state.calls.push({url, method, body, options});
    if (url.pathname === '/health' || url.pathname === '/api/v1/provider/usage') return reply({simulation: true});
    if (url.pathname === '/api/v1/auth/me') return reply({username: 'tester', csrf_token: 'csrf'});
    if (url.pathname === '/api/v1/jobs' || url.pathname === '/api/v1/notes') return reply([]);
    if (url.pathname === '/api/v1/search/events') return reply(null, 204);
    if (url.pathname === '/api/v1/categories' && method === 'GET') return reply(state.categories);
    if (url.pathname === '/api/v1/categories' && method === 'POST') {
      const created = {id: `c${state.nextId++}`, name: body.name, version: 1};
      state.categories.push(created);
      return reply(created, 201);
    }
    const route = url.pathname.match(/^\/api\/v1\/categories\/(\w+)$/);
    if (route) {
      const category = state.categories.find(item => item.id === route[1]);
      if (method === 'PATCH') {
        assert.equal(body.version, category.version);
        Object.assign(category, {name: body.name, version: category.version + 1});
        return reply(category);
      }
      if (method === 'DELETE') {
        assert.equal(Number(url.searchParams.get('version')), category.version);
        state.categories = state.categories.filter(item => item !== category);
        return reply(null, 204);
      }
    }
    throw new Error(`Unexpected ${method} ${url.pathname}`);
  };
  const settled = async () => { for (let i = 0; i < 25; i++) await new Promise(resolve => setImmediate(resolve)); };
  const sleep = ms => new Promise(resolve => w.setTimeout(resolve, ms));
  const items = () => [...$('category-navigation').children];
  const item = id => items().find(row => row.dataset.categoryId === id);
  const button = id => item(id).querySelector('.category-button');
  const names = () => items().map(row => row.querySelector('.category-button')?.textContent);
  const menu = () => $('category-menu');
  const menuItems = () => [...menu().querySelectorAll('[role=menuitem]')];
  const key = (target, init) => target.dispatchEvent(new w.KeyboardEvent('keydown', {bubbles: true, cancelable: true, ...init}));
  const contextmenu = (target, x = 40, y = 50) =>
    target.dispatchEvent(new w.MouseEvent('contextmenu', {bubbles: true, cancelable: true, clientX: x, clientY: y}));
  const submit = form => form.dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true}));
  const touch = (type, target, x = 10, y = 10) => {
    const event = new w.MouseEvent(type, {bubbles: true, cancelable: true, clientX: x, clientY: y});
    Object.defineProperty(event, 'pointerType', {value: 'touch'});
    target.dispatchEvent(event);
  };
  const calls = (method, pathname) => state.calls.filter(c => c.method === method && c.url.pathname === pathname);
  try {
    w.eval(['app.js', 'workspace.js', 'focus.js'].map(name => fs.readFileSync(path.join(root, name), 'utf8')).join('\n'));
    await settled();
    w.categoryLongPressMs = 30;

    // Heading with a plus button. The old management entry and dialog are gone.
    assert.equal($('category-nav-title').textContent, 'Категории');
    assert.equal($('category-add').getAttribute('aria-label'), 'Новая категория');
    assert.equal($('category-add').title, 'Новая категория');
    assert.equal($('manage-categories'), null);
    assert.equal($('category-dialog'), null);
    assert.deepEqual(names(), ['Все заметки', 'Без категории', 'Research', 'Дом']);
    assert.equal(item('c1').querySelectorAll('.category-more').length, 1);
    assert.equal(items()[0].querySelector('.category-more'), null, 'virtual items have no menu');
    assert.equal(items()[1].querySelector('.category-more'), null, 'virtual items have no menu');

    // Inline create. A duplicate is refused in place, a new name is created and selected.
    $('category-add').click();
    let input = $('category-navigation').querySelector('.category-edit input');
    assert.ok(input); assert.equal(w.document.activeElement, input);
    input.value = ' research '; submit(input.form); await settled();
    assert.equal($('category-navigation').querySelector('.category-edit-error').textContent, 'Такая категория уже есть.');
    assert.equal(calls('POST', '/api/v1/categories').length, 0);
    input = $('category-navigation').querySelector('.category-edit input');
    assert.equal(input.disabled, false);
    input.value = 'Покупки'; submit(input.form); await settled();
    assert.equal(calls('POST', '/api/v1/categories').length, 1);
    assert.equal(calls('POST', '/api/v1/categories')[0].body.name, 'Покупки');
    assert.equal(calls('POST', '/api/v1/categories')[0].options.headers['X-CSRF-Token'], 'csrf');
    assert.equal($('category-navigation').querySelector('.category-edit'), null);
    assert.deepEqual(names(), ['Все заметки', 'Без категории', 'Research', 'Дом', 'Покупки']);
    assert.equal(button('c3').getAttribute('aria-pressed'), 'true');
    assert.equal($('library-title').textContent, 'Покупки');
    assert.equal(calls('GET', '/api/v1/notes').at(-1).url.searchParams.get('category_id'), 'c3');

    // Empty value on blur and Escape both cancel without a request.
    $('category-add').click();
    $('category-navigation').querySelector('.category-edit input').blur();
    assert.equal($('category-navigation').querySelector('.category-edit'), null);
    $('category-add').click();
    input = $('category-navigation').querySelector('.category-edit input'); input.value = 'Черновик';
    key(input, {key: 'Escape'});
    assert.equal($('category-navigation').querySelector('.category-edit'), null);
    assert.equal(w.document.activeElement, $('category-add'));
    assert.equal(calls('POST', '/api/v1/categories').length, 1);

    // Context menu from a right click.
    assert.equal(menu().hidden, true); assert.equal(menu().getAttribute('role'), 'menu');
    const virtual = contextmenu(items()[0].querySelector('.category-button'));
    assert.equal(virtual, true, 'the browser menu stays for virtual items');
    assert.equal(menu().hidden, true);
    assert.equal(contextmenu(button('c1')), false, 'the app menu replaces the browser menu');
    assert.equal(menu().hidden, false);
    assert.deepEqual(menuItems().map(row => row.textContent), ['Переименовать', 'Удалить', 'Новая категория']);
    assert.ok(menuItems().every(row => row.getAttribute('role') === 'menuitem'));
    assert.equal(w.document.activeElement, menuItems()[0]);
    key(menu(), {key: 'ArrowDown'}); assert.equal(w.document.activeElement, menuItems()[1]);
    key(menu(), {key: 'ArrowDown'}); key(menu(), {key: 'ArrowDown'});
    assert.equal(w.document.activeElement, menuItems()[0], 'arrows wrap around');
    key(menu(), {key: 'ArrowUp'}); assert.equal(w.document.activeElement, menuItems()[2]);
    key(menu(), {key: 'Escape'});
    assert.equal(menu().hidden, true); assert.equal(w.document.activeElement, button('c1'));

    // Outside click, scroll and resize close it.
    contextmenu(button('c2')); assert.equal(menu().hidden, false);
    w.document.body.dispatchEvent(new w.MouseEvent('mousedown', {bubbles: true}));
    assert.equal(menu().hidden, true);
    contextmenu(button('c2')); w.document.dispatchEvent(new w.Event('scroll')); assert.equal(menu().hidden, true);
    contextmenu(button('c2')); w.dispatchEvent(new w.Event('resize')); assert.equal(menu().hidden, true);

    // Keyboard: the ContextMenu key, Shift+F10 and the small button.
    button('c1').focus(); key(button('c1'), {key: 'ContextMenu'});
    assert.equal(menu().hidden, false); assert.equal(w.document.activeElement, menuItems()[0]);
    key(menu(), {key: 'Escape'}); assert.equal(w.document.activeElement, button('c1'));
    key(button('c1'), {key: 'F10', shiftKey: true}); assert.equal(menu().hidden, false);
    key(menu(), {key: 'Escape'});
    const more = item('c2').querySelector('.category-more');
    assert.match(more.getAttribute('aria-label'), /Дом/);
    more.click();
    assert.equal(menu().hidden, false); assert.equal(more.getAttribute('aria-expanded'), 'true');
    more.click();
    assert.equal(menu().hidden, true); assert.equal(more.getAttribute('aria-expanded'), 'false');

    // Rename in place: Escape cancels, a duplicate is refused, Enter saves with the version.
    contextmenu(button('c1')); menuItems()[0].click();
    input = item('c1').querySelector('input');
    assert.ok(input); assert.equal(input.value, 'Research'); assert.equal(w.document.activeElement, input);
    key(input, {key: 'Escape'});
    assert.equal(item('c1').querySelector('input'), null); assert.equal(button('c1').textContent, 'Research');
    assert.equal(w.document.activeElement, button('c1'));
    assert.equal(calls('PATCH', '/api/v1/categories/c1').length, 0);
    contextmenu(button('c1')); menuItems()[0].click();
    input = item('c1').querySelector('input'); input.value = 'дом'; submit(input.form); await settled();
    assert.equal(item('c1').querySelector('.category-edit-error').textContent, 'Такая категория уже есть.');
    assert.equal(calls('PATCH', '/api/v1/categories/c1').length, 0);
    input = item('c1').querySelector('input'); input.value = 'Исследования'; submit(input.form); await settled();
    assert.equal(calls('PATCH', '/api/v1/categories/c1').length, 1);
    assert.deepEqual(calls('PATCH', '/api/v1/categories/c1')[0].body, {version: 1, name: 'Исследования'});
    assert.equal(item('c1').querySelector('input'), null); assert.equal(button('c1').textContent, 'Исследования');
    assert.equal(w.document.activeElement, button('c1'));
    assert.equal($('category-filter').querySelector('option[value=c1]').textContent, 'Исследования');

    // The menu entry "Новая категория" opens the same inline field.
    contextmenu(button('c2')); menuItems()[2].click();
    assert.ok($('category-navigation').querySelector('.category-edit input'));
    key($('category-navigation').querySelector('.category-edit input'), {key: 'Escape'});

    // Delete asks first. Cancel does nothing, confirm sends the version.
    contextmenu(button('c2')); menuItems()[1].click();
    assert.equal($('category-delete-dialog').open, true);
    assert.equal($('category-delete-name').textContent, 'Дом');
    assert.match($('category-delete-dialog').textContent, /Заметки из категории останутся и попадут в «Без категории»/);
    $('category-delete-dialog').querySelector('.actions [data-close-dialog]').click();
    assert.equal($('category-delete-dialog').open, false);
    $('category-delete-confirm').click(); await settled();
    assert.equal(calls('DELETE', '/api/v1/categories/c2').length, 0, 'a closed dialog cannot confirm');
    button('c2').click(); await settled();
    assert.equal(button('c2').getAttribute('aria-pressed'), 'true');
    contextmenu(button('c2')); menuItems()[1].click();
    $('category-delete-confirm').click(); await settled();
    const sent = calls('DELETE', '/api/v1/categories/c2');
    assert.equal(sent.length, 1); assert.equal(sent[0].url.searchParams.get('version'), '1');
    assert.equal(sent[0].options.headers['X-CSRF-Token'], 'csrf');
    assert.equal($('category-delete-dialog').open, false);
    assert.equal(item('c2'), undefined);
    assert.deepEqual(names(), ['Все заметки', 'Без категории', 'Исследования', 'Покупки']);
    assert.equal($('library-title').textContent, 'Все заметки', 'a deleted selected category resets the list');
    assert.equal(calls('GET', '/api/v1/notes').at(-1).url.searchParams.has('category_id'), false);
    assert.equal($('message').textContent, 'Категория удалена.');

    // Long press on touch opens the menu and swallows the click that follows.
    const before = calls('GET', '/api/v1/notes').length;
    touch('pointerdown', button('c1')); touch('pointerup', button('c1')); await sleep(60);
    assert.equal(menu().hidden, true, 'a short press is only a tap');
    touch('pointerdown', button('c1')); await sleep(80);
    assert.equal(menu().hidden, false);
    touch('pointerup', button('c1')); button('c1').click(); await settled();
    assert.equal(calls('GET', '/api/v1/notes').length, before, 'the click after a long press selects nothing');
    key(menu(), {key: 'Escape'});
    console.log('Category navigation checks passed: plus, inline create and rename, context menu, long press, delete.');
  } finally { await sleep(10); dom.window.close(); }
};
