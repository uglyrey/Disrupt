'use strict';
// Действия с заметкой: меню «Действия», удаление в корзину, объединение, ручные связи, удаление выводов и задач.
// Грузится до focus.js, чтобы тот увидел новый значок как обычный инструмент.
// Использует глобалы app.js (api, message, currentNote, openNote, loadNotes, renderNote, setNoteBusy, hasDrafts, noteBusy, leaveNote).
// Разметка строится только через textContent, текст заметок в HTML не попадает.
(() => {
  const get = id => document.getElementById(id);
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const button = (text, className, onclick) => {
    const node = el('button', text, className); node.type = 'button';
    if (onclick) node.onclick = onclick;
    return node;
  };
  const svg = (paths, size = 20) => {
    const ns = 'http://www.w3.org/2000/svg', node = document.createElementNS(ns, 'svg');
    for (const [key, value] of Object.entries({viewBox: '0 0 24 24', width: size, height: size, 'aria-hidden': 'true', focusable: 'false'})) node.setAttribute(key, value);
    const path = document.createElementNS(ns, 'path');
    for (const [key, value] of Object.entries({d: paths, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.7, 'stroke-linecap': 'round', 'stroke-linejoin': 'round'})) path.setAttribute(key, value);
    node.append(path);
    return node;
  };
  const ICONS = {
    dots: 'M5 12h.01M12 12h.01M19 12h.01',
    trash: 'M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6',
    merge: 'M6 4v6a4 4 0 0 0 4 4h4a4 4 0 0 1 4 4v2M18 4v6a4 4 0 0 1-4 4M12 14v6',
    link: 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1',
  };
  const debounceMs = () => (typeof window.noteActionsDebounceMs === 'number' ? window.noteActionsDebounceMs : 300);
  const toastMs = () => (typeof window.undoToastMs === 'number' ? window.undoToastMs : 6000);
  const failure = error => (error && typeof error.message === 'string' && error.message ? error.message : 'Не удалось выполнить запрос.');
  const MERGE_ERRORS = {
    merge_source_missing: 'Одну из заметок уже удалили или изменили. Ничего не объединено. Выберите заметки заново.',
    budget_exhausted: 'Лимит приложения исчерпан. Объединение не выполнено, заметки остались как были.',
    merge_images_unavailable: 'Не хватило места для картинок или готового текста. Сократите заметки. Оригиналы сохранены.',
  };
  const MERGE_RETRY = 'Заметки остались как были. Выберите их и нажмите «Объединить» ещё раз.';

  // ---- dialogs and the undo toast ----
  function makeDialog(id, title) {
    const dialog = el('dialog', '', 'utility-dialog na-dialog');
    dialog.setAttribute('aria-labelledby', `${id}-title`);
    const head = el('div', '', 'dialog-heading');
    const heading = el('h2', title); heading.id = `${id}-title`;
    const close = button('×', '', () => closeModal(dialog)); close.setAttribute('aria-label', 'Закрыть');
    head.append(heading, close); dialog.append(head);
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog);
    return dialog;
  }
  function openModal(dialog) {
    if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
  }
  function closeModal(dialog) {
    if (typeof dialog.close === 'function') dialog.close();
    else { dialog.removeAttribute('open'); dialog.dispatchEvent(new Event('close')); }
  }
  // Resolves true after «confirmLabel», false after anything else.
  function confirmDialog({title, text, confirmLabel = 'Удалить', danger = true}) {
    return new Promise(resolve => {
      const dialog = makeDialog('na-confirm', title);
      let answer = false;
      dialog.classList.add('na-confirm');
      const status = el('p', '', 'dialog-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
      const actions = el('div', '', 'actions');
      const yes = button(confirmLabel, danger ? 'danger' : '', () => { answer = true; closeModal(dialog); });
      yes.dataset.confirm = 'yes';
      const no = button('Отмена', 'secondary', () => closeModal(dialog)); no.dataset.confirm = 'no';
      actions.append(yes, no);
      dialog.append(el('p', text), status, actions);
      dialog.addEventListener('close', () => resolve(answer));
      openModal(dialog);
      no.focus();
    });
  }
  let toastNode = null, toastTimer = 0;
  function hideToast() {
    clearTimeout(toastTimer); toastTimer = 0;
    if (toastNode) { toastNode.remove(); toastNode = null; }
  }
  // One toast at a time. The action runs once, by a click, before the time is up.
  function showToast(text, actionLabel, action) {
    hideToast();
    toastNode = el('div', '', 'na-toast'); toastNode.setAttribute('role', 'status'); toastNode.setAttribute('aria-live', 'polite');
    toastNode.append(el('span', text, 'na-toast-text'));
    if (actionLabel) {
      const undo = button(actionLabel, 'na-toast-action', async () => {
        const run = action; hideToast();
        try { await run(); } catch (error) { message(failure(error)); }
      });
      undo.dataset.toast = 'undo';
      toastNode.append(undo);
    }
    document.body.append(toastNode);
    toastTimer = setTimeout(hideToast, toastMs());
  }

  // ---- shared helpers over the globals of app.js ----
  function busyNow() {
    if (typeof noteBusy !== 'undefined' && noteBusy) { message('Дождитесь сохранения.'); return true; }
    return false;
  }
  async function flushDraft() {
    if (window.BerestaFocus && window.BerestaFocus.pending()) await window.BerestaFocus.flush();
  }
  function reloadList() { return typeof loadNotes === 'function' ? loadNotes().catch(() => {}) : Promise.resolve(); }

  // ---- delete the open note ----
  async function deleteCurrentNote() {
    if (!currentNote || busyNow()) return;
    window.BerestaFocus?.closeAll();
    await flushDraft();
    if (!currentNote || busyNow()) return;
    const note = currentNote, name = note.title || 'Без названия';
    const yes = await confirmDialog({
      title: 'Удалить заметку?',
      text: `Заметка «${name}» уйдёт в корзину на 30 дней. Её можно вернуть оттуда. Потом она удалится навсегда. Напоминания по ней приостановятся.`,
      confirmLabel: 'Удалить',
    });
    if (!yes || !currentNote || currentNote.id !== note.id) return;
    setNoteBusy(true);
    let done = false;
    try {
      await api(`/api/v1/notes/${encodeURIComponent(note.id)}?version=${currentNote.version}`, {method: 'DELETE'});
      done = true;
    } catch (error) {
      message(error.status === 409 ? 'Заметка уже изменилась. Откройте её заново и удалите ещё раз.' : failure(error));
    } finally { setNoteBusy(false); }
    if (!done) return;
    if (leaveNote()) {
      get('capture-card').hidden = false;
      if (!get('thought').disabled) get('thought').focus();
    }
    await reloadList();
    message('Заметка в корзине. Вернуть её можно в разделе «Корзина».');
  }

  // ---- delete a task, an idea or an AI conclusion ----
  const ITEM_DONE = {task: 'Задача удалена.', idea: 'Идея удалена.', goal: 'Цель удалена.', plan: 'План удалён.', note: 'Заметка удалена.'};
  async function removeItem(item) {
    if (!currentNote || busyNow()) return;
    const noteId = currentNote.id;
    await flushDraft();
    if (!currentNote || currentNote.id !== noteId || busyNow()) return;
    if (hasDrafts(item.id)) return message('Сначала сохраните остальные правки.');
    setNoteBusy(true);
    let fresh = null;
    try {
      fresh = await api(`/api/v1/notes/${encodeURIComponent(noteId)}/items/${encodeURIComponent(item.id)}?version=${currentNote.version}`, {method: 'DELETE'});
      if (currentNote && currentNote.id === noteId) { renderNote(fresh); setNoteBusy(true); }
      await reloadList();
    } catch (error) {
      message(error.status === 409 ? 'Заметка уже изменилась. Обновили её, попробуйте ещё раз.' : failure(error));
    } finally { setNoteBusy(false); }
    if (!fresh) return;
    showToast(ITEM_DONE[item.kind] || ITEM_DONE.note, 'Отменить', () => restoreItem(noteId, item.id));
  }
  async function restoreItem(noteId, itemId) {
    const fresh = await api(`/api/v1/notes/${encodeURIComponent(noteId)}/items/${encodeURIComponent(itemId)}/restore`, {method: 'POST'});
    if (typeof currentNote !== 'undefined' && currentNote && currentNote.id === noteId && !noteBusy) {
      renderNote(fresh); await reloadList();
    }
    message('Вернули на место.');
    return fresh;
  }
  async function removeConclusion(conclusion) {
    if (!currentNote || busyNow()) return;
    const noteId = currentNote.id;
    await flushDraft();
    if (!currentNote || currentNote.id !== noteId || busyNow()) return;
    if (hasDrafts()) return message('Сначала сохраните остальные правки.');
    const yes = await confirmDialog({
      title: 'Удалить вывод?', text: 'Вывод ИИ исчезнет из заметки. Вернуть его будет нельзя. Сам текст заметки не изменится.', confirmLabel: 'Удалить',
    });
    if (!yes || !currentNote || currentNote.id !== noteId) return;
    setNoteBusy(true);
    try {
      const fresh = await api(`/api/v1/notes/${encodeURIComponent(noteId)}/conclusions/${encodeURIComponent(conclusion.id)}?version=${currentNote.version}`, {method: 'DELETE'});
      if (currentNote && currentNote.id === noteId) { renderNote(fresh); setNoteBusy(true); }
      await reloadList(); message('Вывод удалён.');
    } catch (error) {
      message(error.status === 409 ? 'Заметка уже изменилась. Обновили её, попробуйте ещё раз.' : failure(error));
    } finally { setNoteBusy(false); }
  }
  function decorateNote() {
    if (typeof currentNote === 'undefined' || !currentNote) return;
    const items = new Map((currentNote.items || []).map(item => [item.id, item]));
    for (const row of get('items').querySelectorAll('.item')) {
      const item = items.get(row.dataset.itemId), tools = row.querySelector('.item-tools');
      if (!item || !tools || tools.querySelector('.item-trash')) continue;
      const trash = button('', 'item-trash', () => removeItem(item));
      trash.setAttribute('aria-label', 'Удалить'); trash.title = 'Удалить';
      trash.append(svg(ICONS.trash, 15));
      tools.prepend(trash);
    }
    const conclusions = currentNote.conclusions || [];
    [...get('conclusions').querySelectorAll('.conclusion')].forEach((block, index) => {
      const conclusion = conclusions[index], actions = block.querySelector('.actions');
      if (!conclusion || !actions || actions.querySelector('.conclusion-delete')) return;
      actions.append(button('Удалить', 'quiet conclusion-delete', () => removeConclusion(conclusion)));
    });
  }

  // ---- note picker (merge and manual links) ----
  async function fetchNotes(query, offset) {
    const params = new URLSearchParams({limit: '20', offset: String(offset)});
    if (query) params.set('q', query);
    const {data, headers} = await api(`/api/v1/notes?${params}`, {}, true);
    const next = headers.get('X-Next-Notes-Offset');
    return {notes: data, next: next === null || next === undefined ? null : Number(next)};
  }
  function picker({id, title, intro, multiple, exclude = new Set(), preselect = null}) {
    const dialog = makeDialog(id, title);
    dialog.classList.add('na-picker');
    dialog.append(el('p', intro, 'na-intro'));
    const extra = el('p', '', 'na-extra muted'); extra.setAttribute('role', 'status'); extra.hidden = true;
    const chosen = el('div', '', 'na-chosen'); chosen.setAttribute('aria-label', 'Выбранные заметки'); chosen.hidden = true;
    const label = el('label', 'Поиск по заметкам', 'sr-only'); label.htmlFor = `${id}-search`;
    const search = document.createElement('input');
    search.id = `${id}-search`; search.type = 'search'; search.placeholder = 'Поиск по заметкам'; search.maxLength = 200; search.autocomplete = 'off';
    const list = el('div', '', 'na-list'); list.setAttribute('role', 'group'); list.setAttribute('aria-label', 'Заметки');
    const more = button('Показать ещё', 'quiet na-more', () => load(false)); more.hidden = true;
    const status = el('p', '', 'dialog-status na-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const actions = el('div', '', 'actions');
    dialog.append(extra, chosen, label, search, list, more, status, actions);

    const selected = new Map();
    if (preselect) selected.set(preselect.id, preselect.title);
    const state = {dialog, selected, search, status, actions, extra, max: multiple ? 5 : 1, onChange: () => {}, busy: false};
    let run = 0, offset = 0, timer = 0;
    const rowFor = note => {
      const row = el('label', '', 'na-row');
      const box = document.createElement('input');
      box.type = multiple ? 'checkbox' : 'radio'; box.name = `${id}-pick`; box.value = note.id;
      box.checked = selected.has(note.id);
      box.disabled = state.busy || (!box.checked && selected.size >= state.max && multiple);
      box.onchange = () => {
        if (!multiple) selected.clear();
        if (box.checked) selected.set(note.id, note.title || 'Без названия'); else selected.delete(note.id);
        sync(); state.onChange();
      };
      const when = typeof note.updated_at === 'number'
        ? new Date(note.updated_at * 1000).toLocaleDateString('ru-RU', {day: 'numeric', month: 'short'}) : '';
      const text = el('span', '', 'na-row-text');
      text.append(el('span', note.title || 'Без названия', 'na-row-title'));
      if (when) text.append(el('span', when, 'na-row-meta'));
      row.append(box, text);
      return row;
    };
    let shown = [];
    // Rows are drawn once per search. A change only updates the boxes and the chips, so the focus stays where it is.
    function sync() {
      for (const box of list.querySelectorAll('input')) {
        box.checked = selected.has(box.value);
        box.disabled = state.busy || (multiple && !box.checked && selected.size >= state.max);
      }
      chosen.replaceChildren();
      if (multiple) {
        for (const [noteId, name] of selected) {
          const chip = button('', 'na-chip', () => { selected.delete(noteId); sync(); state.onChange(); });
          chip.append(el('span', name, 'na-chip-text'), el('span', '×', 'na-chip-x'));
          chip.setAttribute('aria-label', `Убрать из выбора «${name}»`);
          chosen.append(chip);
        }
        chosen.hidden = !selected.size;
      }
    }
    function paint() {
      list.replaceChildren(...shown.map(rowFor));
      sync();
    }
    async function load(reset = true) {
      const mine = ++run;
      if (reset) { offset = 0; shown = []; }
      status.textContent = 'Ищем…';
      try {
        const {notes, next} = await fetchNotes(search.value.trim(), offset);
        if (mine !== run) return;
        const known = new Set(shown.map(note => note.id));
        const fresh = notes.filter(note => !exclude.has(note.id) && !known.has(note.id));
        shown = shown.concat(fresh);
        offset = next === null ? offset + notes.length : next;
        more.hidden = next === null;
        paint();
        status.textContent = shown.length ? '' : (search.value.trim() ? 'Ничего не нашли. Измените запрос.' : 'Других заметок пока нет.');
      } catch (error) {
        if (mine !== run) return;
        status.textContent = failure(error);
      }
    }
    search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => load(), debounceMs()); });
    search.addEventListener('keydown', event => { if (event.key === 'Enter') event.preventDefault(); });
    dialog.addEventListener('close', () => { clearTimeout(timer); run++; });
    state.load = load;
    state.lock = value => {
      state.busy = value;
      search.disabled = value; more.disabled = value;
      for (const control of actions.querySelectorAll('button')) control.disabled = value;
      sync();
    };
    openModal(dialog);
    paint(); load();
    search.focus();
    return state;
  }

  // ---- merge ----
  let pendingMerge = null;
  async function mergeJob(job, currentEpoch) {
    // Up to about five minutes, like the capture jobs. The job goes on without the dialog.
    const deadline = Date.now() + 300000;
    while (Date.now() < deadline) {
      const fresh = await api(`/api/v1/jobs/${encodeURIComponent(job.id)}`);
      if (typeof epoch !== 'undefined' && currentEpoch !== epoch) return null;
      if (fresh.status === 'succeeded' || fresh.status === 'failed') return fresh;
      await new Promise(resolve => setTimeout(resolve, typeof window.mergePollMs === 'number' ? window.mergePollMs : 1000));
    }
    return null;
  }
  // Messages about a saved source or a save without AI make no sense for a merge, so those sentences are dropped.
  function mergeFailure(job) {
    const known = MERGE_ERRORS[job.error_code];
    if (known) return known;
    const text = typeof errors !== 'undefined' && errors[job.error_code];
    if (!text) return `Объединить не получилось. ${MERGE_RETRY} Код: ${job.error_code}.`;
    const kept = text.split(/(?<=[.!?])\s+/u).filter(part => !/сохранён|сохранена|сохранить|сохраните|Нажмите/u.test(part));
    return `${kept.join(' ')} ${MERGE_RETRY}`.trim();
  }
  async function openMerge() {
    if (busyNow()) return;
    window.BerestaFocus?.closeAll();
    await flushDraft();
    if (busyNow()) return;
    const start = typeof currentNote !== 'undefined' && currentNote ? {id: currentNote.id, title: currentNote.title || 'Без названия'} : null;
    const state = picker({
      id: 'na-merge', title: 'Объединить с…', multiple: true, preselect: start,
      intro: 'Отметьте от 2 до 5 заметок. ИИ соберёт из них одну новую заметку. Исходные уйдут в корзину на 30 дней.',
    });
    state.extra.hidden = false;
    state.extra.textContent = 'Стоит 1 единицу ИИ.';
    let remaining = null;
    api('/api/v1/provider/usage').then(usage => {
      remaining = typeof usage.daily_units_remaining === 'number' ? usage.daily_units_remaining : null;
      const total = typeof usage.daily_unit_limit === 'number' && usage.daily_unit_limit > 0 ? usage.daily_unit_limit : null;
      state.extra.textContent = remaining === null ? 'Стоит 1 единицу ИИ.'
        : remaining === 0 ? 'Стоит 1 единицу ИИ. На сегодня лимит исчерпан, объединить не получится.'
        : `Стоит 1 единицу ИИ. Сегодня осталось ${remaining}${total ? ` из ${total}` : ''}.`;
      state.onChange();
    }).catch(() => {});
    const submit = button('Объединить', '', null); submit.dataset.merge = 'go';
    const cancel = button('Отмена', 'secondary', () => closeModal(state.dialog));
    state.actions.append(submit, cancel);
    const count = el('p', '', 'na-count muted'); count.setAttribute('role', 'status'); count.setAttribute('aria-live', 'polite');
    state.actions.before(count);
    state.onChange = () => {
      const n = state.selected.size;
      count.textContent = `Выбрано ${n} из 5`;
      submit.disabled = state.busy || n < 2 || n > 5 || remaining === 0;
    };
    state.onChange();
    submit.onclick = async () => {
      const ids = [...state.selected.keys()];
      if (ids.length < 2 || ids.length > 5) return;
      // A repeat of the same choice keeps the key, so a lost answer never spends a second unit.
      const signature = [...ids].sort().join(',');
      if (!pendingMerge || pendingMerge.signature !== signature) pendingMerge = {signature, key: `merge-${window.berestaId()}`};
      state.busy = true; state.lock(true); submit.disabled = true;
      state.status.textContent = 'Объединяем… Заметки пока на месте.';
      const currentEpoch = typeof epoch !== 'undefined' ? epoch : 0;
      let job;
      try {
        job = await api('/api/v1/notes/merge', {method: 'POST', body: JSON.stringify({note_ids: ids, idempotency_key: pendingMerge.key})});
      } catch (error) {
        state.status.textContent = failure(error);
        state.busy = false; state.lock(false); state.onChange();
        return;
      }
      let finished;
      try { finished = await mergeJob(job, currentEpoch); } catch (error) { finished = null; state.status.textContent = failure(error); }
      state.busy = false;
      if (!finished) {
        state.lock(false); state.onChange();
        if (!state.status.textContent || state.status.textContent === 'Объединяем… Заметки пока на месте.') {
          state.status.textContent = 'Объединение ещё идёт. Новая заметка появится в списке, когда будет готова.';
        }
        return;
      }
      if (finished.status === 'failed') {
        pendingMerge = null;
        state.status.textContent = mergeFailure(finished);
        state.lock(false); state.onChange();
        message(state.status.textContent);
        if (typeof loadProviderUsage === 'function') loadProviderUsage().catch(() => {});
        return;
      }
      pendingMerge = null;
      closeModal(state.dialog);
      await reloadList();
      if (typeof loadProviderUsage === 'function') loadProviderUsage().catch(() => {});
      const free = typeof noteBusy !== 'undefined' && !noteBusy && !hasDrafts();
      if (finished.note_id && free) {
        await openNote(finished.note_id, {userAction: true});
        message('Заметки объединены. Исходные лежат в корзине.');
      } else message('Заметки объединены. Новая заметка есть в списке, исходные лежат в корзине.');
    };
  }

  // ---- manual links ----
  let linkRun = 0, linkedIds = new Set();
  async function loadLinks() {
    const box = get('na-links');
    if (!box || typeof currentNote === 'undefined' || !currentNote) return;
    const id = currentNote.id, mine = ++linkRun;
    try {
      const data = await api(`/api/v1/notes/${encodeURIComponent(id)}/links`);
      if (mine !== linkRun || !currentNote || currentNote.id !== id) return;
      drawLinks(data.links || []);
    } catch (_) { if (mine === linkRun) drawLinks([]); }
  }
  function drawLinks(links) {
    linkedIds = new Set(links.map(link => link.note_id));
    const list = get('na-links-list');
    list.replaceChildren();
    for (const link of links) {
      const row = el('li', '', 'na-link-row');
      const open = button(link.title || 'Без названия', 'na-link-open', () => {
        window.BerestaFocus?.closeAll();
        openNote(link.note_id, {userAction: true}).catch(error => message(failure(error)));
      });
      const drop = button('×', 'quiet na-link-remove', () => removeLink(link));
      drop.setAttribute('aria-label', `Убрать связь с «${link.title || 'Без названия'}»`); drop.title = 'Убрать связь';
      row.append(open, drop); list.append(row);
    }
    get('na-links-empty').hidden = links.length > 0;
  }
  async function removeLink(link) {
    if (!currentNote) return;
    const id = currentNote.id;
    try {
      await api(`/api/v1/notes/${encodeURIComponent(id)}/links/${encodeURIComponent(link.note_id)}`, {method: 'DELETE'});
      message('Связь убрана. Сами заметки остались.');
    } catch (error) { message(failure(error)); }
    await loadLinks();
  }
  async function openLinkPicker() {
    if (!currentNote || busyNow()) return;
    window.BerestaFocus?.closeAll();
    const id = currentNote.id;
    const state = picker({
      id: 'na-link', title: 'Связать с заметкой…', multiple: false, exclude: new Set([id, ...linkedIds]),
      intro: 'Выберите заметку. Связь видна в обеих заметках и ничего в них не меняет.',
    });
    const save = button('Связать', '', null); save.dataset.link = 'go'; save.disabled = true;
    const cancel = button('Отмена', 'secondary', () => closeModal(state.dialog));
    state.actions.append(save, cancel);
    state.onChange = () => { save.disabled = state.busy || state.selected.size !== 1; };
    save.onclick = async () => {
      const [other] = [...state.selected.keys()];
      if (!other) return;
      state.lock(true); save.disabled = true;
      try {
        await api(`/api/v1/notes/${encodeURIComponent(id)}/links`, {method: 'POST', body: JSON.stringify({note_id: other})});
        closeModal(state.dialog);
        message('Заметки связаны.');
        await loadLinks();
      } catch (error) {
        state.status.textContent = error.status === 409 ? 'Эти заметки уже связаны.' : failure(error);
        state.lock(false); state.onChange();
      }
    };
  }

  // ---- the menu ----
  function drawSimilar() {
    const source = get('related-list'), list = get('na-similar-list');
    if (!source || !list) return;
    list.replaceChildren();
    for (const row of source.querySelectorAll('li')) {
      const original = row.querySelector('button.br-rel');
      if (!original) continue;
      const title = original.querySelector('.br-rel-title');
      const item = el('li', '', 'na-link-row');
      const open = button(title ? title.textContent : 'Без названия', 'na-link-open', () => { window.BerestaFocus?.closeAll(); original.click(); });
      item.append(open); list.append(item);
    }
    get('na-similar').hidden = !list.children.length;
  }
  function buildMenu() {
    const tools = get('note-tools');
    if (!tools || get('tool-actions')) return;
    const tool = el('div', '', 'tool'); tool.dataset.tool = 'actions';
    const toggle = button('', 'tool-btn quiet', null);
    toggle.id = 'tool-actions'; toggle.dataset.toolBtn = 'actions'; toggle.dataset.tip = 'Действия';
    toggle.setAttribute('aria-label', 'Действия с заметкой'); toggle.setAttribute('aria-haspopup', 'dialog');
    toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-controls', 'pop-actions');
    toggle.append(svg(ICONS.dots));
    const pop = el('div', '', 'tool-pop'); pop.id = 'pop-actions'; pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-label', 'Действия с заметкой'); pop.tabIndex = -1; pop.hidden = true;
    const head = el('div', '', 'pop-head');
    const close = button('×', 'pop-close quiet', null); close.setAttribute('aria-label', 'Закрыть окно');
    head.append(el('span', 'Действия', 'pop-title'), close);
    const body = el('div', '', 'pop-body');
    const menu = el('div', '', 'na-menu');
    const entries = [
      ['merge', 'Объединить с…', 'merge', openMerge],
      ['link', 'Связать с заметкой…', 'link', openLinkPicker],
      ['delete', 'Удалить заметку', 'trash', deleteCurrentNote],
    ];
    for (const [key, text, icon, handler] of entries) {
      const entry = button('', `na-item${key === 'delete' ? ' is-danger' : ''}`, handler);
      entry.dataset.action = key; entry.append(svg(ICONS[icon], 18), el('span', text));
      menu.append(entry);
    }
    const links = el('section', '', 'na-section'); links.id = 'na-links';
    const linksHead = el('h3', 'Связанные заметки'); linksHead.id = 'na-links-title';
    const linksList = el('ul', '', 'na-links-list'); linksList.id = 'na-links-list'; linksList.setAttribute('aria-labelledby', 'na-links-title');
    const empty = el('p', 'Связей пока нет. Свяжите заметку вручную.', 'muted na-empty'); empty.id = 'na-links-empty';
    links.append(linksHead, linksList, empty);
    const similar = el('section', '', 'na-section'); similar.id = 'na-similar'; similar.hidden = true;
    const similarHead = el('h3', 'Похожие'); similarHead.id = 'na-similar-title';
    const similarList = el('ul', '', 'na-links-list'); similarList.id = 'na-similar-list'; similarList.setAttribute('aria-labelledby', 'na-similar-title');
    similar.append(similarHead, similarList);
    body.append(menu, links, similar);
    pop.append(head, body); tool.append(toggle, pop); tools.append(tool);
    // The old icon with automatic neighbours moved into this menu, under «Похожие».
    const related = get('related-list');
    if (related) new MutationObserver(drawSimilar).observe(related, {childList: true});
  }
  buildMenu();
  // The note draws its conclusions right after this event, so the buttons are added a moment later.
  document.addEventListener('beresta:note-rendered', () => {
    Promise.resolve().then(decorateNote); drawSimilar(); loadLinks();
  });

  window.BerestaNoteActions = {
    confirm: confirmDialog, toast: showToast, hideToast, makeDialog, openModal, closeModal,
    deleteNote: deleteCurrentNote, mergeFailure, openMerge, openLinkPicker, removeItem, restoreItem,
  };
})();
