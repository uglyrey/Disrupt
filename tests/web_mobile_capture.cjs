'use strict';
// Phone workspace: the new-note field comes first, the list of notes opens by a button, Back walks the same way.
// The desktop layout keeps its three columns and pushes no history entries.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const root = path.join(__dirname, '../app/static');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const reply = (data, status = 200) => ({ok: status < 400, status, headers: new Headers(), json: async () => structuredClone(data)});

const summaries = [
  {id: 'n1', title: 'План поездки', version: 1, updated_at: 1791220000, category_id: null, channel: 'web', input_kind: 'text'},
  {id: 'n2', title: 'Идеи', version: 1, updated_at: 1791210000, category_id: null, channel: 'web', input_kind: 'text'},
];
const full = id => ({id, capture_id: `k-${id}`, channel: 'web', input_kind: 'text', title: summaries.find(row => row.id === id).title,
  markdown: 'Текст', original_text: 'Исходник', conclusions: [], version: 1, provider: 'manual', category_id: null,
  category_name: null, structure_confirmed_at: null, created_at: 1791220000, updated_at: 1791220000, items: []});

function boot({phone}) {
  const dom = new JSDOM(read('index.html'), {url: 'https://beresta.invalid/', runScripts: 'outside-only'}), w = dom.window;
  const $ = id => w.document.getElementById(id);
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  w.confirm = () => true; w.scrollTo = () => {};
  w.noteAutosaveMs = 10; w.noteAutosaveRetryMs = 600000; w.guideDelayMs = 0;
  w.matchMedia = query => ({media: query, matches: /max-width:\s*640px/.test(query) ? phone : false,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}});
  w.fetch = async (input, options = {}) => {
    const url = new URL(input, w.location.href), p = url.pathname, method = options.method || 'GET';
    if (p === '/health' || p === '/api/v1/provider/usage') return reply({simulation: true});
    if (p === '/api/v1/auth/me') return reply({username: 'tester', csrf_token: 'csrf'});
    if (p === '/api/v1/categories' || p === '/api/v1/jobs') return reply([]);
    if (p === '/api/v1/notes') return reply(summaries);
    if (/^\/api\/v1\/notes\/n[12]$/.test(p) && method === 'GET') return reply(full(p.slice(-2)));
    if (/^\/api\/v1\/notes\/n[12]\/(reminders|related|revisions)$/.test(p)) return reply(p.endsWith('related') ? {note_id: 'n1', related: []} : []);
    if (p === '/api/v1/telegram/links') return reply({identities: [], pending: []});
    if (/\/(opened|original-opened)$/.test(p) || p === '/api/v1/search/events') return reply(null, 204);
    if (p === '/api/v1/account/guides') return reply({guides: {note_tools: {step: 0, done: true}, categories: {step: 0, done: true}}});
    throw new Error('Unexpected ' + p);
  };
  // History traversal is emulated: the entries that the code pushes are kept here and Back pops them with a popstate event.
  const entries = [{state: null, url: w.location.href}]; let index = 0;
  const realPush = w.history.pushState.bind(w.history), realReplace = w.history.replaceState.bind(w.history);
  w.history.pushState = (state, title, url) => { realPush(state, title, url); entries.splice(index + 1); entries.push({state, url: w.location.href}); index++; };
  w.history.replaceState = (state, title, url) => { realReplace(state, title, url); entries[index] = {state, url: w.location.href}; };
  const travel = delta => w.setTimeout(() => {
    const target = entries[index + delta]; if (!target) return;
    index += delta; realReplace(target.state, '', target.url); w.dispatchEvent(new w.Event('popstate'));
  }, 0);
  w.history.back = () => travel(-1); w.history.forward = () => travel(1);
  w.eval(['app.js', 'workspace.js', 'brain.js', 'focus.js', 'mobile-menu.js'].map(read).join('\n') + '\nwindow.openNoteForTest = openNote;');
  return {dom, w, $, entries};
}
async function settled(w) {
  for (let i = 0; i < 25; i++) await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => w.setTimeout(resolve, 15));
  for (let i = 0; i < 25; i++) await new Promise(resolve => setImmediate(resolve));
}
const listOpen = $ => $('workspace').classList.contains('library-open');
const label = $ => $('mobile-back-label').textContent;

module.exports = async () => {
  // ---- The CSS says what the phone shows: one view at a time ----
  const css = read('workspace.css');
  assert.match(css, /\.workspace:not\(\.library-open\)>\.library-column/, 'The list is hidden until it is opened');
  assert.match(css, /\.workspace:not\(\.library-open\)>\.workspace-nav/, 'The categories go with the list');
  assert.match(css, /\.workspace\.library-open>\.editor-column\{display:none\}/, 'The list is a view of its own');
  assert.match(css, /#mobile-library-toggle,#mobile-back\{display:none\}/, 'Both buttons are phone only');

  // ---- Phone: the capture field first, the list behind the button ----
  {
    const {dom, w, $} = boot({phone: true});
    try {
      await settled(w);
      assert.equal($('workspace').hidden, false);
      assert.equal($('capture-card').hidden, false, 'After sign-in the new-note field is on screen');
      assert.equal($('note-card').hidden, true);
      assert.equal(listOpen($), false, 'The list is not open');
      assert.equal($('thought').disabled, false); assert.equal($('capture-submit').disabled, false);
      assert.ok($('capture-card').contains($('thought')) && $('capture-card').contains($('capture-submit')));
      assert.equal($('mobile-library-toggle').textContent.includes('Все заметки'), true);
      assert.equal($('mobile-library-count').textContent, '2', 'The button shows how many notes there are');
      assert.equal($('mobile-library-count').hidden, false);
      assert.equal($('workspace').firstElementChild, $('mobile-library-toggle'), 'The button sits at the top of the workspace');
      assert.equal(w.location.hash, '', 'Nothing is pushed yet');

      // The button opens the list.
      $('mobile-library-toggle').click(); await settled(w);
      assert.equal(listOpen($), true);
      assert.equal(label($), 'Новая запись', 'From the capture field the back button says where it leads');
      assert.equal(w.location.hash, '#list'); assert.equal(w.history.state.beresta, 'list');
      assert.equal($('notes').querySelectorAll('button').length, 2);
      // The back button closes it and the field is back.
      $('mobile-back').click(); await settled(w);
      assert.equal(listOpen($), false); assert.equal($('capture-card').hidden, false); assert.equal(w.location.hash, '');

      // The existing «Все заметки» item of the left menu does the same.
      assert.equal($('menu-button').hidden, false);
      $('menu-button').click();
      const item = $('general-menu').querySelector('[data-proxy="nav-notes"]');
      assert.ok(item, 'The menu has «Все заметки»'); item.click(); await settled(w);
      assert.equal(listOpen($), true, 'The menu item opens the list');
      assert.equal(w.location.hash, '#list');
      // The Back button of the browser closes it too.
      w.history.back(); await settled(w);
      assert.equal(listOpen($), false); assert.equal($('capture-card').hidden, false);

      // A note from the list: the list steps aside, Back goes to the list, then to the field.
      $('mobile-library-toggle').click(); await settled(w);
      $('notes').querySelector('button').click(); await settled(w);
      assert.equal($('note-card').hidden, false, 'The note is open'); assert.equal($('title').value, 'План поездки');
      assert.equal(listOpen($), false); assert.equal($('capture-card').hidden, true);
      assert.equal(label($), 'Назад', 'The note came from the list'); assert.equal(w.location.hash, '#note');
      $('mobile-back').click(); await settled(w);
      assert.equal($('note-card').hidden, true, 'Leaving the note'); assert.equal(listOpen($), true, 'goes back to the list');
      assert.equal(w.location.hash, '#list');
      $('notes').querySelectorAll('button')[1].click(); await settled(w);
      assert.equal($('title').value, 'Идеи');
      w.history.back(); await settled(w);
      assert.equal(listOpen($), true, 'The browser Back from a note also lands on the list'); assert.equal($('note-card').hidden, true);
      assert.equal($('menu-button').hidden, false);
      $('mobile-back').click(); await settled(w);
      assert.equal(listOpen($), false); assert.equal($('capture-card').hidden, false, 'and from the list on the capture field');
      assert.equal($('thought').disabled, false);

      // The same note again while it is already open: the list only steps aside.
      $('mobile-library-toggle').click(); await settled(w);
      $('notes').querySelector('button').click(); await settled(w);
      $('menu-button').click(); $('general-menu').querySelector('[data-proxy="nav-notes"]').click(); await settled(w);
      assert.equal(listOpen($), true); assert.equal($('note-card').hidden, false, 'The open note stays under the list');
      assert.equal(label($), 'Назад');
      $('notes').querySelector('button').click(); await settled(w);
      assert.equal(listOpen($), false, 'Choosing the open note closes the list'); assert.equal($('note-card').hidden, false);

      $('mobile-back').click(); await settled(w);
      assert.equal(listOpen($), true, 'This note came from the list too'); $('mobile-back').click(); await settled(w);
      assert.equal($('capture-card').hidden, false);
      // A note that was not opened from the list leads back to the field.
      w.openNoteForTest('n2'); await settled(w);
      assert.equal(label($), 'Новая запись');
      $('mobile-back').click(); await settled(w);
      assert.equal($('note-card').hidden, true); assert.equal($('capture-card').hidden, false);
      assert.equal(listOpen($), false);

      // Unsaved changes stay: Back asks first and a refusal keeps the note.
      $('mobile-library-toggle').click(); await settled(w);
      $('notes').querySelector('button').click(); await settled(w);
      $('markdown').value = 'Несохранённая правка'; $('markdown').dispatchEvent(new w.Event('input', {bubbles: true}));
      w.confirm = () => false;
      $('mobile-back').click(); await settled(w);
      assert.equal($('note-card').hidden, false, 'The draft keeps the note open'); assert.equal($('markdown').value, 'Несохранённая правка');
      w.confirm = () => true;
    } finally { await new Promise(resolve => w.setTimeout(resolve, 10)); dom.window.close(); }
  }

  // ---- Another screen takes the column: the list steps aside ----
  {
    const {dom, w, $} = boot({phone: true});
    try {
      await settled(w);
      $('mobile-library-toggle').click(); await settled(w);
      assert.equal(listOpen($), true);
      $('menu-button').click(); $('general-menu').querySelector('[data-proxy="nav-tasks"]').click(); await settled(w);
      assert.equal(listOpen($), false, 'A section replaces the list');
      assert.equal($('workspace').classList.contains('has-view'), true);
      assert.equal($('mobile-library-toggle').textContent.includes('Все заметки'), true, 'The button is still there to reach the list');
      $('workspace').hidden = true; await settled(w);
      $('workspace').hidden = false; await settled(w);
      assert.equal(listOpen($), false, 'A new sign-in starts on the field, not on the list');
    } finally { await new Promise(resolve => w.setTimeout(resolve, 10)); dom.window.close(); }
  }

  // ---- Desktop: nothing changes ----
  {
    const {dom, w, $, entries} = boot({phone: false});
    try {
      await settled(w);
      assert.equal($('capture-card').hidden, false); assert.equal(listOpen($), false);
      assert.equal($('notes').querySelectorAll('button').length, 2, 'The list is in its own column from the start');
      $('mobile-library-toggle').click(); $('nav-notes').click(); await settled(w);
      assert.equal(listOpen($), false, 'No phone view on a desktop'); assert.equal(w.location.hash, ''); assert.equal(w.history.state, null);
      $('notes').querySelector('button').click(); await settled(w);
      assert.equal($('note-card').hidden, false); assert.equal(w.location.hash, '', 'A desktop adds no history entries');
      $('new-note').click(); await settled(w);
      assert.equal($('capture-card').hidden, false); assert.equal($('note-card').hidden, true);
      assert.equal(entries.length, 1, 'Nothing was pushed');
    } finally { await new Promise(resolve => w.setTimeout(resolve, 10)); dom.window.close(); }
  }
  console.log('Mobile capture checks passed: capture field first, list by button and by menu, back button and browser Back, note and leave, drafts, sections, desktop untouched.');
};
