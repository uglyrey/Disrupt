'use strict';
// Typewriter mode: the caret line goes to the middle while typing, and fitting the note field never moves the page.
const assert = require('node:assert/strict'), fs = require('fs'), path = require('path'), {JSDOM} = require('jsdom');
const root = path.join(__dirname, '../app/static');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const reply = (data, status = 200) => ({ok: status < 400, status, headers: new Headers(), json: async () => structuredClone(data)});
const settled = async () => { for (let i = 0; i < 30; i++) await new Promise(resolve => setImmediate(resolve)); };

function boot() {
  const dom = new JSDOM(read('index.html'), {url: 'https://beresta.invalid/', runScripts: 'outside-only'}), w = dom.window;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  try { Object.defineProperty(w.crypto, 'randomUUID', {value: require('node:crypto').randomUUID, configurable: true}); } catch { /* provided */ }
  w.confirm = () => true;
  const frames = [];
  w.requestAnimationFrame = fn => { frames.push(fn); return frames.length; };
  const note = {id: 'n1', capture_id: 'k1', channel: 'web', input_kind: 'text', title: 'Заметка', markdown: 'Один\nДва\nТри',
    original_text: 'Исходник', conclusions: [], version: 1, provider: 'manual', category_id: null,
    category_name: null, structure_confirmed_at: null, created_at: 1791220000, updated_at: 1791220000, items: []};
  w.fetch = async (input, options = {}) => {
    const url = new URL(input, w.location.href), method = options.method || 'GET';
    if (url.pathname === '/health' || url.pathname === '/api/v1/provider/usage') return reply({simulation: true});
    if (url.pathname === '/api/v1/auth/me') return reply({username: 'tester', csrf_token: 'csrf'});
    if (url.pathname === '/api/v1/categories' || url.pathname === '/api/v1/jobs') return reply([]);
    if (url.pathname === '/api/v1/notes') return reply([{id: 'n1', title: 'Заметка', version: 1, updated_at: 1791220000, category_id: null, channel: 'web', input_kind: 'text'}]);
    if (url.pathname === '/api/v1/notes/n1' && method === 'GET') return reply(note);
    if (url.pathname.endsWith('/opened') || url.pathname.endsWith('/original-opened') || url.pathname === '/api/v1/search/events') return reply(null, 204);
    throw new Error(`Unexpected ${method} ${url.pathname}`);
  };
  w.eval(['app.js', 'workspace.js', 'focus.js', 'md-editor.js', 'typewriter.js'].map(read).join('\n') + '\nwindow.__holdScroll = holdScroll;');
  return {dom, w, frames, $: id => w.document.getElementById(id)};
}

module.exports = async () => {
  const html = read('index.html'), css = read('typewriter.css'), source = read('typewriter.js');
  assert.ok(html.includes('/static/typewriter.js') && html.includes('/static/typewriter.css'));
  assert.ok(html.indexOf('/static/md-editor.js') < html.indexOf('/static/typewriter.js'), 'loaded after md-editor.js');
  assert.ok(!/\.style\.cssText|setAttribute\(['"]style|innerHTML/.test(source), 'CSP-safe: no inline style text');
  assert.match(css, /\.tw-mirror\{[^}]*visibility:hidden/);
  assert.match(source, /prefers-reduced-motion: reduce/);
  assert.match(source, /visualViewport/);

  const t = boot(), {w} = t;
  try {
    const plan = w.BerestaTypewriter.plan;
    const base = {lineHeight: 30, areaTop: 0, areaBottom: 400, scrollTop: 1000, maxScroll: 5000};
    // The middle is 200. A band of 1.5 lines (45) or 12% (48) around it does nothing.
    assert.equal(plan({...base, caretMid: 200}), null);
    assert.equal(plan({...base, caretMid: 240}), null);
    assert.equal(plan({...base, caretMid: 160}), null);
    // Outside the band the caret line goes to the middle.
    assert.equal(plan({...base, caretMid: 300}), 1100);
    assert.equal(plan({...base, caretMid: 80}), 880);
    // The ends of the page cannot be passed.
    assert.equal(plan({...base, caretMid: 390, scrollTop: 4950}), 5000);
    assert.equal(plan({...base, caretMid: 20, scrollTop: 100}), 0);
    assert.equal(plan({...base, caretMid: 390, scrollTop: 5000}), null);
    // Opening a note for editing centers at once, without a band.
    assert.equal(plan({...base, caretMid: 230, force: true}), 1030);
    // The keyboard makes the area smaller: the middle moves with it.
    assert.equal(plan({...base, areaBottom: 200, caretMid: 190}), 1090);
    assert.equal(plan({...base, areaBottom: 0, caretMid: 190}), null);
    assert.equal(plan({...base, caretMid: NaN}), null);

    // No layout in jsdom: measuring is skipped, typing does not fail.
    assert.equal(w.BerestaTypewriter.caretBox(t.$('thought'), 0), null);
    assert.equal(w.BerestaTypewriter.reveal(t.$('thought')), false);
    // Typing is throttled to one frame, for both fields.
    for (const id of ['thought', 'markdown']) {
      t.frames.length = 0;
      const field = t.$(id);
      for (let i = 0; i < 5; i++) field.dispatchEvent(new w.Event('input', {bubbles: true}));
      assert.equal(t.frames.length, 1, `${id}: one frame for five keystrokes`);
      t.frames.shift()();
      field.dispatchEvent(new w.KeyboardEvent('keyup', {key: 'ArrowDown', bubbles: true}));
      assert.equal(t.frames.length, 1, `${id}: caret keys also ask for a frame`);
      t.frames.length = 0;
      field.dispatchEvent(new w.KeyboardEvent('keyup', {key: 'a', bubbles: true}));
      assert.equal(t.frames.length, 0, `${id}: a letter key alone does not`);
    }

    // Fitting the field shortens it for a moment and the browser pulls the page up. The place is put back at once,
    // and not with the smooth scroll that the page style sets for html.
    let y = 4200; const scrolls = [];
    Object.defineProperty(w, 'scrollY', {get: () => y, configurable: true});
    w.scrollTo = options => { scrolls.push(options); y = options.top; };
    w.__holdScroll(t.$('markdown'), () => { y = 0; });
    assert.deepEqual(scrolls, [{left: 0, top: 4200, behavior: 'instant'}]);
    scrolls.length = 0;
    w.__holdScroll(t.$('markdown'), () => {});
    assert.equal(scrolls.length, 0, 'nothing moved, nothing to restore');
    assert.throws(() => w.__holdScroll(t.$('markdown'), () => { y = 7; throw new Error('boom'); }), /boom/);
    assert.equal(y, 4200, 'restored even if fitting fails');

    // Opening the note for editing: no scroll from focus itself, the caret line is centered.
    await settled(); t.$('notes').querySelector('button').click(); await settled();
    const field = t.$('markdown'), focus = [];
    field.focus = options => focus.push(options);
    let revealed = 0; w.BerestaTypewriter.reveal = target => { if (target === field) revealed++; };
    w.BerestaMd.setMode('edit', {end: true});
    assert.deepEqual(focus, [{preventScroll: true}]);
    assert.equal(revealed, 1);
    assert.equal(field.selectionStart, field.value.length);
    console.log('Typewriter checks passed: band plan, keyboard area, throttling, scroll kept while fitting, edit entry.');
  } finally {
    for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve));
    t.dom.window.close();
  }
};
