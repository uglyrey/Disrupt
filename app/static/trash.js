'use strict';
// Корзина: список удалённых заметок, «Восстановить» и «Удалить навсегда».
// Пункт «Корзина» встаёт в общее меню слева. Нужны note-actions.js и глобалы app.js (api, message, loadNotes).
(() => {
  const get = id => document.getElementById(id);
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const actions = () => window.BerestaNoteActions;
  const failure = error => (error && typeof error.message === 'string' && error.message ? error.message : 'Не удалось выполнить запрос.');
  function plural(n, forms) {
    const mod10 = n % 10, mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return forms[0];
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return forms[1];
    return forms[2];
  }
  const days = n => `${n} ${plural(n, ['день', 'дня', 'дней'])}`;
  function when(note) {
    const now = Date.now() / 1000;
    const gone = Math.max(0, Math.floor((now - note.deleted_at) / 86400));
    const left = Math.max(0, Math.ceil((note.purge_at - now) / 86400));
    const first = gone === 0 ? 'Удалена сегодня.' : `Удалена ${days(gone)} назад.`;
    const second = left === 0 ? 'Скоро исчезнет навсегда.' : `Исчезнет через ${days(left)}.`;
    const merged = note.merged_into_title ? ` Объединена в «${note.merged_into_title}».` : note.merged_into ? ' Объединена в другую заметку.' : '';
    return `${first} ${second}${merged}`;
  }

  let dialog = null, listBox = null, statusLine = null, run = 0, rows = [], busy = false;
  function say(text) { if (statusLine) statusLine.textContent = text || ''; }
  function setBusy(value) {
    busy = value;
    if (!listBox) return;
    for (const control of listBox.querySelectorAll('button')) control.disabled = value;
  }
  function paint() {
    listBox.replaceChildren();
    if (!rows.length) {
      const empty = el('div', '', 'trash-empty');
      empty.append(el('p', 'Корзина пуста', 'trash-empty-title'),
        el('p', 'Удалённые заметки лежат здесь 30 дней. Потом они исчезают навсегда.', 'muted'));
      listBox.append(empty);
      return;
    }
    const list = el('ul', '', 'trash-list');
    for (const note of rows) {
      const row = el('li', '', 'trash-row'); row.dataset.noteId = note.id;
      const info = el('div', '', 'trash-info');
      info.append(el('span', note.title || 'Без названия', 'trash-title'), el('span', when(note), 'trash-when muted'));
      const buttons = el('div', '', 'trash-buttons');
      const restore = el('button', 'Восстановить', 'secondary trash-restore'); restore.type = 'button';
      restore.onclick = () => restoreNote(note);
      const forget = el('button', 'Удалить навсегда', 'quiet trash-forget'); forget.type = 'button';
      forget.onclick = () => forgetNote(note);
      restore.setAttribute('aria-label', `Восстановить «${note.title || 'Без названия'}»`);
      forget.setAttribute('aria-label', `Удалить навсегда «${note.title || 'Без названия'}»`);
      buttons.append(restore, forget);
      row.append(info, buttons); list.append(row);
    }
    listBox.append(list);
  }
  async function load() {
    const mine = ++run;
    say('Загружаем…');
    try {
      const data = await api('/api/v1/trash');
      if (mine !== run) return;
      rows = data.notes || [];
      paint(); say('');
    } catch (error) {
      if (mine !== run) return;
      say(`${failure(error)} Список мог устареть.`);
    }
  }
  async function restoreNote(note) {
    if (busy) return;
    setBusy(true);
    try {
      await api(`/api/v1/trash/${encodeURIComponent(note.id)}/restore`, {method: 'POST'});
      rows = rows.filter(row => row.id !== note.id);
      paint(); say(`Заметка «${note.title || 'Без названия'}» вернулась в список.`);
      message('Заметка восстановлена.');
      if (typeof loadNotes === 'function') await loadNotes().catch(() => {});
    } catch (error) { say(failure(error)); }
    finally { setBusy(false); }
  }
  async function forgetNote(note) {
    if (busy) return;
    const yes = await actions().confirm({
      title: 'Удалить навсегда?',
      text: `Заметка «${note.title || 'Без названия'}» исчезнет вместе с записью, аудио, расшифровкой, задачами и напоминаниями. Вернуть её будет нельзя.`,
      confirmLabel: 'Удалить навсегда',
    });
    if (!yes) return;
    setBusy(true);
    try {
      await api(`/api/v1/trash/${encodeURIComponent(note.id)}`, {method: 'DELETE'});
      rows = rows.filter(row => row.id !== note.id);
      paint(); say('Заметка удалена навсегда.');
    } catch (error) { say(failure(error)); }
    finally { setBusy(false); }
  }
  function open({navigation = true} = {}) {
    if (!actions()) return;
    window.BerestaFocus?.closeAll();
    if (dialog && dialog.isConnected) return;
    dialog = actions().makeDialog('trash', 'Корзина');
    dialog.classList.add('trash-dialog');
    dialog.append(el('p', 'Удалённые заметки хранятся 30 дней. Пока они здесь, их можно вернуть.', 'muted trash-intro'));
    statusLine = el('p', '', 'dialog-status trash-status'); statusLine.setAttribute('role', 'status'); statusLine.setAttribute('aria-live', 'polite');
    listBox = el('div', '', 'trash-body');
    dialog.append(statusLine, listBox);
    dialog.addEventListener('close', () => { run++; dialog = null; listBox = null; statusLine = null; busy = false; window.BerestaNavigation?.trashClosed(); });
    actions().openModal(dialog);
    if (navigation) window.BerestaNavigation?.trashOpened();
    load();
  }
  function install() {
    if (get('open-trash')) return;
    const entry = el('button', 'Корзина', 'nav-action'); entry.type = 'button'; entry.id = 'open-trash';
    entry.onclick = open;
    const menu = document.querySelector('#nav-menu .nav-bottom');
    if (menu) menu.prepend(entry);
    else if (get('nav-menu')) get('nav-menu').prepend(entry);
  }
  install();
  window.BerestaTrash = {open};
})();
