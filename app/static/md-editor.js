'use strict';
// Markdown notes that are easy to type. Loaded after focus.js.
// Part one is pure text editing (value and selection in, value and selection out), part two glues it to the page.
// The note has a reading view and an editing view. The new-note field gets the same list keys and a small "Аа" toolbar.
(() => {
  const LIST = /^(\s*)(?:([-*+•])|(\d{1,9})([.)]))[ \t]+(\[[ xX]\][ \t]+)?/;
  const width = indent => indent.replace(/\t/g, '  ').length;

  /* ---------- Pure editing ---------- */
  function parseLine(line) {
    const m = LIST.exec(line);
    if (!m) return null;
    return {indent: m[1], ordered: m[3] !== undefined, num: m[3] === undefined ? 0 : parseInt(m[3], 10), delim: m[4] || '',
      task: Boolean(m[5]), prefixLen: m[0].length, rest: line.slice(m[0].length)};
  }
  function locate(lines, pos) {
    let line = 0;
    while (line < lines.length - 1 && pos > lines[line].length) { pos -= lines[line].length + 1; line++; }
    return {line, col: pos};
  }
  function offsetOf(lines, line, col) {
    let pos = 0;
    for (let i = 0; i < line; i++) pos += lines[i].length + 1;
    return pos + col;
  }
  // A caret keeps its place inside the line while the line grows or shrinks.
  // The start of a selection that sits at the start of a line stays there, so the whole line stays selected.
  function remap(before, after, pos, lineStart = false) {
    const at = locate(before, pos), delta = after[at.line].length - before[at.line].length;
    const col = lineStart && at.col === 0 ? 0 : at.col + delta;
    return offsetOf(after, at.line, Math.max(0, Math.min(after[at.line].length, col)));
  }
  function finish(before, after, start, end) {
    return {value: after.join('\n'), start: remap(before, after, start, start !== end), end: remap(before, after, end)};
  }
  // The lines a selection touches. A selection that ends at the very start of a line does not include that line.
  function lineRange(lines, value, start, end) {
    const first = locate(lines, start).line;
    let last = locate(lines, end).line;
    if (end > start && last > first && value[end - 1] === '\n') last--;
    return [first, last];
  }
  // Numbers every ordered list in the block again, nested lists count from 1. Only lines from `from` on may change.
  function renumber(lines, from, resets = []) {
    let a = from, b = from;
    while (a > 0 && parseLine(lines[a - 1])) a--;
    while (b + 1 < lines.length && parseLine(lines[b + 1])) b++;
    const stack = [];
    for (let i = a; i <= b; i++) {
      const info = parseLine(lines[i]);
      if (!info) continue;
      const w = width(info.indent);
      while (stack.length && stack[stack.length - 1].w > w) stack.pop();
      let top = stack[stack.length - 1];
      if (top && top.w === w && top.ordered !== info.ordered) { stack.pop(); top = null; }
      if (!top || top.w < w) { top = {w, ordered: info.ordered, n: 0, fresh: true}; stack.push(top); }
      if (!info.ordered) continue;
      let n;
      if (i < from) n = info.num;
      else if (top.fresh) n = resets.includes(i) ? 1 : info.num;
      else n = top.n + 1;
      top.fresh = false; top.n = n;
      if (i >= from && n !== info.num) lines[i] = lines[i].replace(/^(\s*)\d+/, `$1${n}`);
    }
  }
  function outdentLine(line) {
    if (line.startsWith('\t')) return line.slice(1);
    const spaces = /^ {1,2}/.exec(line);
    return spaces ? line.slice(spaces[0].length) : line;
  }

  // Enter on a list line. Returns null when the browser should do its normal thing.
  function enter(value, start, end) {
    if (start !== end) return null;
    const lines = value.split('\n'), at = locate(lines, start), line = lines[at.line], info = parseLine(line);
    if (!info || at.col < info.prefixLen) return null;
    const next = lines.slice();
    if (!info.rest.trim()) {
      // An empty item ends the list. A nested one first steps out one level.
      next[at.line] = width(info.indent) > 0 ? outdentLine(line) : '';
      if (next[at.line]) renumber(next, at.line);
      const caret = offsetOf(next, at.line, next[at.line].length);
      return {value: next.join('\n'), start: caret, end: caret};
    }
    const marker = info.indent + (info.ordered ? `${info.num + 1}${info.delim}` : '-') + ' ' + (info.task ? '[ ] ' : '');
    next[at.line] = line.slice(0, at.col);
    next.splice(at.line + 1, 0, marker + line.slice(at.col));
    if (info.ordered) renumber(next, at.line + 1);
    const caret = offsetOf(next, at.line + 1, marker.length);
    return {value: next.join('\n'), start: caret, end: caret};
  }
  // Tab and Shift+Tab on list lines. Null when no selected line is a list item (or nothing can move out).
  function tab(value, start, end, shift) {
    const lines = value.split('\n'), [first, last] = lineRange(lines, value, start, end), next = lines.slice(), moved = [];
    for (let i = first; i <= last; i++) {
      if (!parseLine(lines[i])) continue;
      next[i] = shift ? outdentLine(lines[i]) : `  ${lines[i]}`;
      if (next[i] !== lines[i]) moved.push(i);
    }
    if (!moved.length) return null;
    renumber(next, moved[0], shift ? [] : moved);
    return finish(lines, next, start, end);
  }
  // "* " and "+ " at the start of a line become "- ", "1) " becomes "1. ". The caret sits right after the space.
  function autoMarker(value, caret) {
    const lineStart = value.lastIndexOf('\n', caret - 1) + 1, before = value.slice(lineStart, caret);
    let fixed = null;
    if (/^\s*[*+] $/.test(before)) fixed = before.replace(/[*+]/, '-');
    else if (/^\s*\d{1,9}\) $/.test(before)) fixed = before.replace(')', '.');
    if (fixed === null) return null;
    return {value: value.slice(0, lineStart) + fixed + value.slice(caret), start: caret, end: caret};
  }
  function toggleInline(value, start, end, mark) {
    const size = mark.length, selected = value.slice(start, end);
    const star = mark === '*';
    if (start === end) {
      return {value: value.slice(0, start) + mark + mark + value.slice(end), start: start + size, end: start + size};
    }
    const outside = value.slice(start - size, start) === mark && value.slice(end, end + size) === mark
      && !(star && (value[start - 2] === '*' || value[end + 1] === '*'));
    if (outside) {
      return {value: value.slice(0, start - size) + selected + value.slice(end + size), start: start - size, end: end - size};
    }
    const inside = selected.length >= size * 2 + 1 && selected.startsWith(mark) && selected.endsWith(mark)
      && !(star && (selected.startsWith('**') && selected.endsWith('**')));
    if (inside) {
      const inner = selected.slice(size, selected.length - size);
      return {value: value.slice(0, start) + inner + value.slice(end), start, end: start + inner.length};
    }
    return {value: value.slice(0, start) + mark + selected + mark + value.slice(end), start: start + size, end: end + size};
  }
  // Takes the old marker off a line and says what it was.
  function splitPrefix(line) {
    const list = parseLine(line);
    if (list) return {indent: list.indent, kind: list.task ? 'check' : list.ordered ? 'number' : 'bullet', rest: list.rest};
    const quote = /^(\s*)>[ \t]?(.*)$/.exec(line);
    if (quote) return {indent: quote[1], kind: 'quote', rest: quote[2]};
    const head = /^(\s*)(#{1,6})[ \t]+(.*)$/.exec(line);
    if (head) return {indent: head[1], kind: 'heading', level: head[2].length, rest: head[3]};
    return {indent: /^\s*/.exec(line)[0], kind: null, rest: line.replace(/^\s+/, '')};
  }
  // Heading, bullet, number, check and quote work the same way. On lines that already have it, it is taken off.
  function setLinePrefix(value, start, end, kind) {
    const lines = value.split('\n'), [first, last] = lineRange(lines, value, start, end), next = lines.slice();
    const targets = [];
    for (let i = first; i <= last; i++) if (first === last || lines[i].trim()) targets.push(i);
    if (!targets.length) return null;
    const parts = targets.map(i => splitPrefix(lines[i]));
    let level = 0;
    if (kind === 'heading') level = parts[0].kind === 'heading' ? (parts[0].level >= 3 ? 0 : parts[0].level + 1) : 1;
    const remove = kind === 'heading' ? level === 0 : parts.every(part => part.kind === kind);
    targets.forEach((i, n) => {
      const part = parts[n];
      let prefix = '';
      if (!remove) {
        prefix = {bullet: '- ', number: '1. ', check: '- [ ] ', quote: '> ', heading: `${'#'.repeat(level)} `}[kind];
      }
      next[i] = (kind === 'heading' && remove ? '' : part.indent) + prefix + part.rest;
    });
    if (!remove && kind === 'number') renumber(next, targets[0], targets);
    else if (remove) renumber(next, targets[0]);
    return finish(lines, next, start, end);
  }
  const ACTIONS = {
    heading: (v, s, e) => setLinePrefix(v, s, e, 'heading'),
    bold: (v, s, e) => toggleInline(v, s, e, '**'),
    italic: (v, s, e) => toggleInline(v, s, e, '*'),
    bullet: (v, s, e) => setLinePrefix(v, s, e, 'bullet'),
    number: (v, s, e) => setLinePrefix(v, s, e, 'number'),
    check: (v, s, e) => setLinePrefix(v, s, e, 'check'),
    quote: (v, s, e) => setLinePrefix(v, s, e, 'quote'),
  };

  /* ---------- Putting a change into a textarea ---------- */
  // The browser undo stack only knows about changes made through execCommand. Without it (or if it refuses)
  // setRangeText does the same job, undo then starts a new step. Both end with an input event, so autosave sees the change.
  function replaceRange(ta, from, to, text) {
    const expected = ta.value.slice(0, from) + text + ta.value.slice(to);
    ta.setSelectionRange(from, to);
    let done = false;
    if (ta.ownerDocument.activeElement === ta && typeof ta.ownerDocument.execCommand === 'function') {
      try { done = text ? ta.ownerDocument.execCommand('insertText', false, text) : ta.ownerDocument.execCommand('delete'); } catch { done = false; }
    }
    if (done && ta.value === expected) return;
    if (!done) { ta.setRangeText(text, from, to, 'end'); } else { ta.value = expected; }
    ta.dispatchEvent(new ta.ownerDocument.defaultView.Event('input', {bubbles: true}));
  }
  function applyEdit(ta, result) {
    const old = ta.value, now = result.value;
    if (now !== old) {
      if (ta.maxLength > 0 && now.length > ta.maxLength && now.length > old.length) return false;
      let p = 0, s = 0;
      const min = Math.min(old.length, now.length);
      while (p < min && old[p] === now[p]) p++;
      while (s < min - p && old[old.length - 1 - s] === now[now.length - 1 - s]) s++;
      replaceRange(ta, p, old.length - s, now.slice(p, now.length - s));
    }
    ta.setSelectionRange(result.start, result.end);
    return true;
  }

  /* ---------- Keys ---------- */
  function shortcut(event) {
    const code = event.code || '', key = String(event.key || '').toLowerCase();
    if (event.shiftKey) {
      const digit = code.replace(/^(Digit|Numpad)/, '') || key;
      if (digit === '7' || key === '&') return 'number';
      if (digit === '8' || key === '*') return 'bullet';
      if (digit === '9' || key === '(') return 'check';
      return null;
    }
    if (code === 'KeyB' || key === 'b' || key === 'и') return 'bold';
    if (code === 'KeyI' || key === 'i' || key === 'ш') return 'italic';
    return null;
  }
  let busy = false;
  function run(ta, fn) {
    const result = fn(ta.value, ta.selectionStart, ta.selectionEnd);
    if (!result) return false;
    busy = true;
    try { applyEdit(ta, result); } finally { busy = false; }
    return true;
  }
  // Smart keys for one textarea. `onEscape` is only given to the note editor.
  function attachKeys(ta, {onEscape} = {}) {
    ta.addEventListener('keydown', event => {
      if (event.isComposing || event.defaultPrevented) return;
      const mod = event.ctrlKey || event.metaKey;
      if (event.key === 'Enter' && !mod && !event.shiftKey && !event.altKey) {
        if (run(ta, enter)) event.preventDefault();
      } else if (event.key === 'Tab' && !mod && !event.altKey) {
        if (run(ta, (v, s, e) => tab(v, s, e, event.shiftKey))) event.preventDefault();
      } else if (mod && !event.altKey) {
        const action = shortcut(event);
        if (action) { run(ta, ACTIONS[action]); event.preventDefault(); }
      } else if (event.key === 'Escape' && onEscape && !event.shiftKey && !mod) {
        event.preventDefault(); onEscape();
      }
    });
    ta.addEventListener('input', event => {
      if (busy || event.inputType !== 'insertText') return;
      if (ta.selectionStart !== ta.selectionEnd) return;
      const result = autoMarker(ta.value, ta.selectionStart);
      if (result) run(ta, () => result);
    });
  }

  /* ---------- Toolbar ---------- */
  const SVG = 'http://www.w3.org/2000/svg';
  const BUTTONS = [
    {id: 'heading', label: 'Заголовок', text: 'H'},
    {id: 'bold', label: 'Жирный', text: 'B', hint: 'Ctrl/⌘+B'},
    {id: 'italic', label: 'Курсив', text: 'I', hint: 'Ctrl/⌘+I'},
    {id: 'bullet', label: 'Список', hint: 'Ctrl/⌘+Shift+8',
      path: 'M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01'},
    {id: 'number', label: 'Нумерованный список', hint: 'Ctrl/⌘+Shift+7',
      path: 'M10 6h10M10 12h10M10 18h10M4 5l1.5-1v4M3.5 13.5c.5-1 2.5-1 2.5.3 0 1-2.5 1.6-2.5 2.7h2.7'},
    {id: 'check', label: 'Чек-лист', hint: 'Ctrl/⌘+Shift+9',
      path: 'M3.5 5.5h5v5h-5zM4.5 8l1 1 1.5-2M3.5 14h5v5h-5zM12 8h8M12 16.5h8'},
    {id: 'quote', label: 'Цитата', path: 'M4 6v12M9 8h11M9 12h11M9 16h7'},
  ];
  function buildToolbar(bar, ta, {done} = {}) {
    bar.setAttribute('role', 'toolbar'); bar.setAttribute('aria-label', 'Форматирование текста');
    for (const def of BUTTONS) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'md-tool'; button.dataset.md = def.id;
      button.setAttribute('aria-label', def.label); button.title = def.hint ? `${def.label} (${def.hint})` : def.label;
      if (def.text) {
        const glyph = document.createElement('span'); glyph.className = `md-glyph md-glyph-${def.id}`; glyph.textContent = def.text;
        glyph.setAttribute('aria-hidden', 'true'); button.append(glyph);
      } else {
        const svg = document.createElementNS(SVG, 'svg'), path = document.createElementNS(SVG, 'path');
        svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('width', '20'); svg.setAttribute('height', '20');
        svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
        for (const [name, value] of [['d', def.path], ['fill', 'none'], ['stroke', 'currentColor'], ['stroke-width', '1.8'],
          ['stroke-linecap', 'round'], ['stroke-linejoin', 'round']]) path.setAttribute(name, value);
        svg.append(path); button.append(svg);
      }
      // The field keeps focus and its selection while a button is pressed.
      button.addEventListener('mousedown', event => event.preventDefault());
      button.addEventListener('click', () => { if (ta.disabled) return; ta.focus(); run(ta, ACTIONS[def.id]); });
      bar.append(button);
    }
    if (done) {
      const button = document.createElement('button');
      button.type = 'button'; button.id = 'md-done'; button.className = 'md-done'; button.textContent = 'Готово';
      button.title = 'Готово (Esc)';
      button.addEventListener('click', done);
      bar.append(button);
    }
  }

  /* ---------- New-note field ---------- */
  function initThought() {
    const thought = document.getElementById('thought'), bar = document.getElementById('thought-toolbar'),
      toggle = document.getElementById('thought-format');
    if (!thought) return;
    attachKeys(thought);
    if (!bar || !toggle) return;
    buildToolbar(bar, thought);
    toggle.addEventListener('click', () => {
      const open = bar.hidden;
      bar.hidden = !open; toggle.setAttribute('aria-expanded', String(open));
      if (open) thought.focus();
    });
  }

  /* ---------- Note: reading and editing ---------- */
  function initNote() {
    const ta = document.getElementById('markdown'), preview = document.getElementById('preview'),
      bar = document.getElementById('md-toolbar'), title = document.getElementById('title'),
      body = ta && ta.closest('.note-body');
    if (!ta || !preview || !bar) return;
    const isBusy = () => typeof noteBusy !== 'undefined' && Boolean(noteBusy);
    let mode = 'read', shownId = null;

    function toggleTask(line, checked) {
      const lines = ta.value.split('\n');
      const done = () => { renderPreview(line); };
      if (isBusy() || line >= lines.length) return done();
      const fixed = lines[line].replace(/^(\s*(?:[-*+•]|\d{1,9}[.)])[ \t]+)\[[ xX]\]/, `$1[${checked ? 'x' : ' '}]`);
      if (fixed === lines[line]) return done();
      lines[line] = fixed;
      applyEdit(ta, {value: lines.join('\n'), start: ta.selectionStart, end: ta.selectionEnd});
      done();
    }
    function renderPreview(keepFocusLine = null) {
      renderMarkdownInto(preview, ta.value, {onToggle: toggleTask, images:true});
      if (keepFocusLine !== null) {
        const box = preview.querySelector(`input[data-line="${keepFocusLine}"]`);
        if (box) box.focus();
      }
    }
    function setMode(next, {line = null, end = false, start = false, focus = true} = {}) {
      mode = next;
      const editing = next === 'edit';
      if (body) body.classList.toggle('is-editing', editing);
      ta.hidden = !editing; bar.hidden = !editing; preview.hidden = editing;
      if (!editing) { renderPreview(); return; }
      if (window.BerestaFocus && window.BerestaFocus.grow) window.BerestaFocus.grow();
      if (!focus) return;
      ta.focus();
      const lines = ta.value.split('\n');
      const pos = start ? 0 : line !== null && line < lines.length ? offsetOf(lines, line, lines[line].length) : end ? ta.value.length : 0;
      ta.setSelectionRange(pos, pos);
    }
    function leave() {
      if (mode !== 'edit') return;
      setMode('read');
      preview.focus();
      const focus = window.BerestaFocus;
      if (focus) focus.flush().then(() => focus.checkpoint()).catch(() => {});
    }
    attachKeys(ta, {onEscape: leave});
    buildToolbar(bar, ta, {done: leave});

    preview.setAttribute('tabindex', '0');
    preview.setAttribute('aria-label', 'Текст заметки. Нажмите Enter или E, чтобы править');
    preview.addEventListener('click', event => {
      if (isBusy() || event.target.closest('a, input, button, select, figure.note-image')) return;
      const selection = window.getSelection && window.getSelection();
      if (selection && !selection.isCollapsed && preview.contains(selection.anchorNode)) return;
      const host = event.target.closest('[data-line]');
      setMode('edit', {line: host && preview.contains(host) ? Number(host.dataset.line) : null, end: true});
    });
    preview.addEventListener('keydown', event => {
      if (event.target !== preview || event.ctrlKey || event.metaKey || event.altKey || isBusy()) return;
      if (event.key === 'Enter' || event.code === 'KeyE' || String(event.key).toLowerCase() === 'e') {
        event.preventDefault(); setMode('edit', {end: true});
      }
    });
    title.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.isComposing) setMode('edit', {start: true});
    });
    document.addEventListener('beresta:note-rendered', () => {
      const id = typeof currentNote !== 'undefined' && currentNote ? currentNote.id : null, same = id !== null && id === shownId;
      shownId = id;
      if (same && mode === 'edit') { renderPreview(); return; }
      // An empty note starts in the editing view, but does not take the focus away from the title.
      if (!ta.value.trim()) setMode('edit', {focus: false});
      else setMode('read');
    });
    setMode('read');
    window.BerestaMd.setMode = setMode;
    window.BerestaMd.mode = () => mode;
    window.BerestaMd.refresh = renderPreview;
  }

  window.BerestaMd = {parseLine, enter, tab, autoMarker, toggleInline, setLinePrefix, renumber, ACTIONS, applyEdit, shortcut};
  initThought();
  initNote();
})();
