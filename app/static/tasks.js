'use strict';
// The «Задачи» screen: every task of the user across notes. Loaded after brain.js, which switches screens.
// It uses the globals of app.js (api, message, openNote). Markup comes only from textContent, never from note text.
(() => {
  const $ = id => document.getElementById(id);
  const PAGE = 50;
  const EMPTY = {
    open: ['Открытых задач нет', 'Новые задачи появятся из ваших записей. Можно добавить их и вручную в самой заметке.'],
    completed: ['Выполненных задач пока нет', 'Когда вы отметите задачу, она окажется здесь.'],
    all: ['Задач пока нет', 'Запишите мысль, и задачи из неё соберутся здесь.'],
  };
  let filter = 'open', rows = [], counts = {open: 0, completed: 0}, hasMore = false, run = 0, busy = false, loaded = false;

  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function plural(n, forms) {
    const mod10 = n % 10, mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return forms[0];
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return forms[1];
    return forms[2];
  }
  const MONTHS_GEN = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
  const startOfDay = date => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  // Only a clear moment is shown, never the quote from the note. No moment means no label.
  function dueLabel(row) {
    const stamp = typeof row.due_resolved_at === 'number' ? row.due_resolved_at : row.due_at;
    if (typeof stamp !== 'number') return null;
    const date = new Date(stamp * 1000), now = new Date();
    const hasTime = row.due_has_time !== false;
    const days = Math.round((startOfDay(date) - startOfDay(now)) / 86400000);
    let day = {'-1': 'вчера', 0: 'сегодня', 1: 'завтра'}[days];
    if (!day) day = `${date.getDate()} ${MONTHS_GEN[date.getMonth()]}${date.getFullYear() === now.getFullYear() ? '' : ` ${date.getFullYear()}`}`;
    const time = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
    const text = hasTime ? `${day}, ${time}` : day;
    const late = row.status === 'open' && (hasTime ? date.getTime() < now.getTime() : days < 0);
    return {text: late ? `Просрочено, срок ${text}` : `Срок ${text}`, late};
  }
  function say(text) { $('tasks-status').textContent = text || ''; }
  function failure(error) {
    return error && typeof error.message === 'string' && error.message ? error.message : 'Не удалось выполнить запрос.';
  }

  function syncFilter() {
    for (const button of $('tasks-filter').querySelectorAll('button')) {
      const status = button.dataset.status;
      button.setAttribute('aria-pressed', String(status === filter));
      const total = status === 'all' ? counts.open + counts.completed : counts[status];
      const label = {open: 'Открытые', completed: 'Выполненные', all: 'Все'}[status];
      button.textContent = loaded ? `${label} ${total}` : label;
    }
  }
  function setBusy(on) {
    busy = on;
    for (const box of $('tasks-list').querySelectorAll('input,.tk-trash')) box.disabled = on;
  }

  function rowNode(row) {
    const item = el('li', '', `tk-row${row.status === 'completed' ? ' is-done' : ''}`);
    item.dataset.itemId = row.id;
    const box = document.createElement('input');
    box.type = 'checkbox'; box.className = 'tk-check'; box.checked = row.status === 'completed';
    const textId = `tk-text-${row.id}`;
    box.setAttribute('aria-labelledby', textId);
    box.onchange = () => toggle(row, box);
    const main = el('div', '', 'tk-main');
    const text = el('span', row.text, 'tk-text'); text.id = textId;
    main.append(text);
    const meta = el('div', '', 'tk-meta');
    const due = dueLabel(row);
    if (due) meta.append(el('span', due.text, due.late ? 'tk-due is-late' : 'tk-due'));
    const link = el('button', row.note_title || 'Без названия', 'tk-note');
    link.type = 'button'; link.title = 'Открыть заметку';
    link.setAttribute('aria-label', `Открыть заметку «${row.note_title || 'Без названия'}»`);
    link.onclick = () => {
      if (typeof openNote !== 'function') return;
      openNote(row.note_id, {userAction: true}).catch(error => message(failure(error)));
    };
    meta.append(link);
    main.append(meta);
    const trash = el('button', '', 'tk-trash'); trash.type = 'button';
    trash.title = 'Удалить задачу'; trash.setAttribute('aria-label', `Удалить задачу «${row.text}»`);
    trash.append(trashIcon());
    trash.onclick = () => remove(row);
    item.append(box, main, trash);
    return item;
  }
  function render() {
    const list = $('tasks-list');
    list.replaceChildren(...rows.map(rowNode));
    syncFilter();
    const empty = loaded && !rows.length && !busy;
    $('tasks-empty').hidden = !empty;
    if (empty) {
      $('tasks-empty-title').textContent = EMPTY[filter][0];
      $('tasks-empty-text').textContent = EMPTY[filter][1];
    }
    $('tasks-more').hidden = !hasMore;
  }

  async function fetchPage(offset) {
    return api(`/api/v1/tasks?status=${filter}&limit=${PAGE}&offset=${offset}`);
  }
  async function load(notice = '') {
    const mine = ++run;
    $('tasks-retry').hidden = true;
    say('Загружаем задачи…');
    setBusy(true);
    try {
      const data = await fetchPage(0);
      if (mine !== run) return;
      rows = data.items; hasMore = data.has_more; counts = data.counts; loaded = true;
      setBusy(false); render();
      say(notice || (rows.length ? `${rows.length} ${plural(rows.length, ['задача', 'задачи', 'задач'])} в списке${hasMore ? ', есть ещё' : ''}.` : ''));
    } catch (error) {
      if (mine !== run) return;
      setBusy(false); render();
      say(`${failure(error)} Список мог устареть.`);
      $('tasks-retry').hidden = false;
    }
  }
  async function more() {
    if (busy || !hasMore) return;
    const mine = ++run;
    setBusy(true); $('tasks-more').disabled = true;
    try {
      const data = await fetchPage(rows.length);
      if (mine !== run) return;
      const known = new Set(rows.map(row => row.id));
      rows = rows.concat(data.items.filter(row => !known.has(row.id)));
      hasMore = data.has_more; counts = data.counts;
      setBusy(false); render();
      say(`Показано ${rows.length}.`);
    } catch (error) {
      if (mine !== run) return;
      setBusy(false); say(failure(error));
    } finally { $('tasks-more').disabled = false; }
  }

  async function toggle(row, box) {
    if (busy) { box.checked = row.status === 'completed'; return; }
    const status = box.checked ? 'completed' : 'open', mine = ++run;
    setBusy(true);
    try {
      const note = await api(`/api/v1/notes/${row.note_id}/items/${row.id}`, {
        method: 'PATCH', body: JSON.stringify({version: row.note_version, kind: 'task', text: row.text, status}),
      });
      if (mine !== run) return;
      // The note moved to a new version, so every task of that note needs it for its next change.
      const stored = new Map((note.items || []).map(item => [item.id, item]));
      for (const other of rows) {
        if (other.note_id !== row.note_id) continue;
        other.note_version = note.version;
        const fresh = stored.get(other.id);
        if (fresh) other.version = fresh.version;
      }
      row.status = status;
      counts = {open: Math.max(0, counts.open + (status === 'completed' ? -1 : 1)),
        completed: Math.max(0, counts.completed + (status === 'completed' ? 1 : -1))};
      const index = rows.indexOf(row);
      if (filter !== 'all') rows.splice(index, 1);
      setBusy(false); render();
      say(status === 'completed' ? 'Задача выполнена.' : 'Задача снова открыта.');
      // Keep the keyboard where it was: the next row, the previous one, or the filter.
      const boxes = $('tasks-list').querySelectorAll('.tk-check');
      const next = filter === 'all' ? boxes[index] : boxes[Math.min(index, boxes.length - 1)];
      (next || $('tasks-filter').querySelector('[aria-pressed=true]'))?.focus();
    } catch (error) {
      if (mine !== run) return;
      setBusy(false);
      if (error && error.status === 409) {
        message('Заметка уже изменилась. Обновили список, попробуйте ещё раз.');
        await load('Заметка уже изменилась, список обновлён. Отметьте задачу ещё раз.');
        return;
      }
      box.checked = row.status === 'completed';
      say(failure(error));
      message(failure(error));
    }
  }

  function trashIcon() {
    const ns = 'http://www.w3.org/2000/svg', icon = document.createElementNS(ns, 'svg'), path = document.createElementNS(ns, 'path');
    for (const [key, value] of Object.entries({viewBox: '0 0 24 24', width: 16, height: 16, 'aria-hidden': 'true', focusable: 'false'})) icon.setAttribute(key, value);
    for (const [key, value] of Object.entries({d: 'M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.7, 'stroke-linecap': 'round', 'stroke-linejoin': 'round'})) path.setAttribute(key, value);
    icon.append(path);
    return icon;
  }
  // The task goes to the trash of its note. A toast for six seconds offers to undo it.
  async function remove(row) {
    if (busy) return;
    const mine = ++run;
    setBusy(true);
    try {
      const note = await api(`/api/v1/notes/${row.note_id}/items/${row.id}?version=${row.note_version}`, {method: 'DELETE'});
      if (mine !== run) return;
      for (const other of rows) if (other.note_id === row.note_id && other !== row) other.note_version = note.version;
      const index = rows.indexOf(row);
      rows.splice(index, 1);
      const key = row.status === 'completed' ? 'completed' : 'open';
      counts = {...counts, [key]: Math.max(0, counts[key] - 1)};
      setBusy(false); render();
      say('Задача удалена.');
      const buttons = $('tasks-list').querySelectorAll('.tk-trash');
      (buttons[Math.min(index, buttons.length - 1)] || $('tasks-filter').querySelector('[aria-pressed=true]'))?.focus();
      const actions = window.BerestaNoteActions;
      if (actions) actions.toast('Задача удалена.', 'Отменить', async () => {
        await api(`/api/v1/notes/${row.note_id}/items/${row.id}/restore`, {method: 'POST'});
        await load('Задача вернулась в список.');
      });
    } catch (error) {
      if (mine !== run) return;
      setBusy(false);
      if (error && error.status === 409) {
        message('Заметка уже изменилась. Обновили список, попробуйте ещё раз.');
        await load('Заметка уже изменилась, список обновлён. Удалите задачу ещё раз.');
        return;
      }
      say(failure(error));
      message(failure(error));
    }
  }

  for (const button of $('tasks-filter').querySelectorAll('button')) {
    button.onclick = () => {
      if (filter === button.dataset.status) return;
      filter = button.dataset.status; rows = []; hasMore = false;
      syncFilter(); load();
    };
  }
  $('tasks-more').onclick = more;
  $('tasks-retry').onclick = () => load();

  window.BerestaTasks = {
    open() { load(); },
    stop() { run++; busy = false; },
    reset() {
      run++; busy = false; rows = []; hasMore = false; loaded = false; filter = 'open'; counts = {open: 0, completed: 0};
      $('tasks-list').replaceChildren(); $('tasks-empty').hidden = true; $('tasks-more').hidden = true; $('tasks-retry').hidden = true;
      say(''); syncFilter();
    },
  };
})();
