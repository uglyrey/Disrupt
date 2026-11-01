'use strict';
// Продолжение заметки: «Добавить к «X»» / «Отдельная заметка» под новой записью и «Отменить» после добавления.
// Нужны глобалы app.js: api, message, loadNotes, openNote, currentNote. Документ docs/attach-to-note-v1.md.
(() => {
  const get = id => document.getElementById(id);
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  let busy = false;
  const failure = error => (error && typeof error.message === 'string' && error.message ? error.message : 'Не удалось выполнить запрос.');
  const short = title => { const text = String(title || ''); return text.length > 60 ? text.slice(0, 59) + '…' : text; };

  async function act(capture, action, body, after) {
    if (busy) return;
    busy = true; setDisabled(true);
    try {
      const result = await api(`/api/v1/captures/${encodeURIComponent(capture)}/${action}`, {
        method: 'POST', body: body ? JSON.stringify(body) : undefined,
      });
      await loadNotes();
      await openNote(result.note_id, {userAction: true});
      message(after(result));
    } catch (error) {
      message(failure(error));
    } finally { busy = false; setDisabled(false); }
  }
  function setDisabled(value) {
    const box = get('attach-box');
    if (box) for (const button of box.querySelectorAll('button')) button.disabled = value;
  }
  function render(note) {
    const box = get('attach-box');
    if (!box) return;
    box.replaceChildren();
    const offer = note && note.attach_offer;
    const undo = ((note && note.attached) || []).filter(source => source.can_undo);
    if (offer) {
      const title = short(offer.title);
      box.append(el('p', `Похоже, это продолжение «${title}». Добавить туда?`, 'attach-text'));
      const row = el('div', '', 'actions');
      const yes = el('button', `Добавить к «${title}»`), no = el('button', 'Отдельная заметка', 'quiet');
      yes.type = no.type = 'button'; yes.id = 'attach-yes'; no.id = 'attach-no';
      yes.onclick = () => act(offer.capture_id, 'attach', {note_id: offer.note_id}, result => `Добавил в «${short(result.note_title)}».`);
      no.onclick = () => act(offer.capture_id, 'separate', null, () => 'Оставил отдельной заметкой.');
      row.append(yes, no); box.append(row);
    }
    for (const source of undo) {
      const row = el('div', '', 'attach-undo-row');
      row.append(el('span', 'Запись добавлена в эту заметку.', 'attach-text'));
      const button = el('button', 'Отменить', 'quiet attach-undo');
      button.type = 'button';
      button.onclick = () => act(source.capture_id, 'detach', null, result => result.target_text_kept
        ? 'Запись снова отдельная заметка. Текст в этой заметке вы правили, он остался.' : 'Отменил. Запись снова отдельная заметка.');
      row.append(button); box.append(row);
    }
    const sources = (note && note.attached) || [];
    if (sources.length) {
      const details = el('details', '', 'attach-sources');
      details.append(el('summary', `Добавленные записи: ${sources.length}`));
      for (const source of sources) {
        const when = new Date(source.created_at * 1000).toLocaleString('ru-RU');
        details.append(el('p', when, 'muted'), el('pre', source.original_text || ''));
      }
      box.append(details);
    }
    box.hidden = !box.childNodes.length;
  }
  document.addEventListener('beresta:note-rendered', () => render(typeof currentNote === 'undefined' ? null : currentNote));
  window.BerestaAttach = {render};
})();
