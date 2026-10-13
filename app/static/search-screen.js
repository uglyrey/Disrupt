'use strict';
(() => {
  const get = id => document.getElementById(id);
  let run = 0, offset = 0, operation = null, filter = null, busy = false;
  const visible = () => !get('workspace').hidden && !get('search-card').hidden;
  const status = text => { get('find-status').textContent = text; };
  function stop() { run++; busy = false; get('find-more').disabled = false; }
  function reset() {
    stop(); filter = null; operation = null; offset = 0;
    get('find-query').value = ''; get('find-source').value = ''; get('find-category').value = '';
    get('find-results').replaceChildren(); get('find-more').hidden = true; status('Введите слова из заметки.');
  }
  async function open() {
    const ticket = ++run, account = epoch;
    try {
      const rows = await api('/api/v1/categories');
      if (ticket !== run || account !== epoch || !visible()) return;
      const value = get('find-category').value;
      get('find-category').replaceChildren(new Option('Все категории',''),new Option('Без категории','none'));
      for (const row of rows) get('find-category').append(new Option(row.name,row.id));
      get('find-category').value = value;
      get('find-query').focus();
      if (!filter) status('Ищем в названиях, тексте, задачах и расшифровках.');
    } catch (error) { if (ticket === run && account === epoch && visible()) status(error.message); }
  }
  async function search(more = false) {
    if (!visible() || (more && busy)) return;
    const ticket = ++run, account = epoch;
    if (!more) {
      offset = 0; filter = {q:get('find-query').value.trim(),category:get('find-category').value,source:get('find-source').value};
      operation = window.berestaId(); get('find-results').replaceChildren(); get('find-more').hidden = true;
    }
    const searchOperation = operation;
    const query = new URLSearchParams({limit:'20',offset:String(offset)});
    if (filter.q) query.set('q',filter.q);
    if (filter.category) query.set('category_id',filter.category);
    for (const [key,value] of Object.entries(sourceQuery[filter.source] || {})) query.set(key,value);
    busy = true; get('find-more').disabled = true; status('Ищем…');
    try {
      // Tracking must not block a real search; no query text is written to analytics.
      if (!more) api('/api/v1/search/events',{method:'POST',body:JSON.stringify({operation_id:searchOperation})}).catch(() => {});
      const {data:rows,headers} = await api('/api/v1/notes?' + query,{},true);
      if (ticket !== run || account !== epoch || !visible()) return;
      for (const note of rows) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'find-result';
        button.append(element('span',note.title,'find-result-title'));
        const when = new Date(note.updated_at * 1000).toLocaleDateString('ru-RU',{day:'numeric',month:'long',year:'numeric'});
        const meta = element('span',when,'muted'); for (const mark of sourceMarks(note)) meta.append(mark);
        button.append(meta);
        button.onclick = () => {
          if (!visible() || account !== epoch) return;
          openNote(note.id,{userAction:true,search:searchOperation}).catch(error => { if (account === epoch) status(error.message); });
        };
        get('find-results').append(button);
      }
      offset += rows.length; get('find-more').hidden = !headers.has('X-Next-Notes-Offset');
      status(offset ? `Показано заметок: ${offset}${get('find-more').hidden ? '' : '. Есть ещё.'}` : 'Ничего не нашли. Попробуйте другие слова или измените фильтр.');
    } catch (error) { if (ticket === run && account === epoch && visible()) status(error.message); }
    finally { if (ticket === run) { busy = false; get('find-more').disabled = false; } }
  }
  get('find-form').onsubmit = event => { event.preventDefault(); search(); };
  get('find-more').onclick = () => search(true);
  get('find-clear').onclick = () => { reset(); get('find-query').focus(); };
  window.BerestaSearch = {open,stop,reset};
  if (visible()) open();
})();
