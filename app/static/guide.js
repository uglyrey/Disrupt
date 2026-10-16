'use strict';
// Guided tips. A small bubble sits next to a button and says what it does. It has a counter, «Далее» and
// «Больше не показывать». The page stays usable: no overlay, nothing is blocked, Esc puts the tour off until later.
// Two tours: note_tools (the first note a person opens) and categories (the first time the category menu opens).
// Progress is kept on the server (GET and PATCH /api/v1/account/guides). If that fails, sessionStorage keeps it
// for the rest of the session, so nobody is nagged again and again. A tour shows at most once per page load.
// Loaded after mobile-menu.js. It uses api() from app.js and currentNote.
(() => {
  const doc = document;
  const get = id => doc.getElementById(id);
  const noteCard = get('note-card');
  const STORE_KEY = 'beresta-guides';
  const PATH = '/api/v1/account/guides';
  const isPhone = () => (typeof window.matchMedia === 'function' ? window.matchMedia('(max-width:640px)').matches : false);
  const menus = () => window.BerestaMenus || null;
  const profileBusy = () => Boolean(window.BerestaProfile && window.BerestaProfile.pending());
  const noteId = () => (typeof currentNote !== 'undefined' && currentNote ? currentNote.id : null);

  /* ---------- Is a target on the screen ---------- */
  // jsdom has no layout. There every box is empty, so the box check only runs when the page has a size.
  const hasLayout = () => doc.documentElement.getBoundingClientRect().width > 0;
  function shown(node) {
    if (!node || !node.isConnected) return false;
    for (let n = node; n && n !== doc.body; n = n.parentElement) {
      if (n.hidden) return false;
      const style = window.getComputedStyle(n);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
    }
    return !hasLayout() || node.getClientRects().length > 0;
  }
  const firstShown = selector => [...doc.querySelectorAll(selector)].find(shown) || null;

  /* ---------- Tours ---------- */
  // `order` is the saved step number. It does not change between phone and desktop.
  const noteTool = (order, key, lead, text) => ({
    order, lead, text, menu: true, target: () => doc.querySelector(`#note-tools [data-tool-btn="${key}"]`),
  });
  const TOURS = {
    note_tools: {
      places: ['bottom', 'left', 'top', 'right'], wait: 700,
      ready: id => noteId() === id && !noteCard.hidden && !doc.body.classList.contains('has-pop'),
      steps: () => {
        const list = [
          {order: 0, phoneOnly: true, lead: 'Меню заметки.', text: 'Здесь все инструменты этой заметки.', target: () => get('note-menu-button')},
          noteTool(1, 'reminders', 'Напоминания.', 'Поставьте время, и beresta напомнит в Telegram.'),
          noteTool(2, 'tasks', 'Задачи и идеи.', 'Всё, что beresta нашла в записи. Отмечайте сделанное.'),
          noteTool(3, 'related', 'Связанные заметки.', 'Похожие по смыслу записи.'),
          noteTool(4, 'props', 'Свойства.', 'Здесь меняется категория заметки.'),
          noteTool(5, 'original', 'Исходник.', 'Ваш текст до обработки. Он остаётся без изменений.'),
          noteTool(6, 'insights', 'Дополнения ИИ.', 'Выводы и идеи от ИИ. Вы решаете, что принять.'),
          noteTool(7, 'history', 'Версии.', 'Прежние варианты заметки. К ним можно вернуться.'),
        ];
        // New menu items bring their own tip: data-guide="Что делает" and, if needed, data-guide-lead="Название."
        [...doc.querySelectorAll('#note-menu-extra [data-guide]')].forEach((item, index) => {
          const lead = item.dataset.guideLead || `${(item.textContent || '').trim()}.`;
          list.push({order: 100 + index, menu: true, lead, text: item.dataset.guide, target: () => item});
        });
        return list;
      },
    },
    categories: {
      places: ['right', 'bottom', 'top', 'left'], wait: 250,
      ready: () => { const menu = get('category-menu'); return Boolean(menu) && !menu.hidden; },
      steps: () => [
        {order: 0, lead: 'Плюс.', text: 'Создаёт новую категорию.', target: () => get('category-add')},
        {order: 1, lead: 'Шестерёнка.', text: 'Переименовать или удалить категорию.',
          target: () => firstShown('.category-item[data-category-id] .category-more[aria-expanded="true"]')
            || firstShown('.category-item[data-category-id] .category-more')},
        {order: 2, lead: 'Правая кнопка.', text: 'То же меню у любой категории. На телефоне нажмите на категорию и подержите.',
          target: () => firstShown('.category-item[data-category-id] .category-button')},
      ],
    },
  };

  /* ---------- Saved progress ---------- */
  let guides = null, loading = null, queue = Promise.resolve();
  const readLocal = () => { try { return JSON.parse(sessionStorage.getItem(STORE_KEY) || '{}') || {}; } catch { return {}; } };
  const writeLocal = () => { try { sessionStorage.setItem(STORE_KEY, JSON.stringify(guides)); } catch { /* storage may be blocked */ } };
  function normalize(raw) {
    const out = {};
    for (const name of Object.keys(TOURS)) {
      const one = raw && typeof raw === 'object' ? raw[name] : null;
      out[name] = {step: one && Number.isInteger(one.step) && one.step >= 0 ? one.step : 0, done: Boolean(one && one.done === true)};
    }
    return out;
  }
  function loadState() {
    if (!loading) {
      loading = (async () => {
        const local = normalize(readLocal());
        let remote = null;
        try {
          const data = await api(PATH);
          remote = normalize(data && data.guides);
        } catch { remote = null; }
        guides = normalize({});
        for (const name of Object.keys(TOURS)) {
          const mine = local[name], theirs = remote ? remote[name] : mine;
          guides[name] = {step: Math.max(mine.step, theirs.step), done: mine.done || theirs.done};
        }
        return guides;
      })();
    }
    return loading;
  }
  function save(name, change) {
    const now = guides[name];
    const next = {step: change.step !== undefined ? change.step : now.step, done: change.done !== undefined ? change.done : now.done};
    if (next.step === now.step && next.done === now.done) return;
    guides[name] = next; writeLocal();
    // One request after the other, so the server ends with the last word. A failure changes nothing for the person.
    queue = queue.then(() => api(PATH, {method: 'PATCH', body: JSON.stringify({guide: name, step: next.step, done: next.done})}))
      .then(() => {}, () => {});
  }

  /* ---------- The bubble ---------- */
  let active = null, shownThisLoad = false, frame = 0, tracking = false;
  function element(tag, className, text) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function build() {
    const bubble = element('div', 'guide-bubble');
    bubble.setAttribute('role', 'dialog'); bubble.setAttribute('aria-modal', 'false'); bubble.setAttribute('aria-label', 'Подсказка');
    const text = element('p', 'guide-text');
    const foot = element('div', 'guide-foot');
    const count = element('span', 'guide-count');
    const never = element('button', 'quiet guide-never', 'Больше не показывать');
    const next = element('button', 'quiet guide-next', 'Далее');
    never.type = 'button'; next.type = 'button';
    foot.append(count, never, next);
    bubble.append(element('span', 'guide-arrow'), text, foot);
    const live = element('div', 'sr-only'); live.setAttribute('aria-live', 'polite');
    return {bubble, text, count, never, next, live};
  }

  const overlap = (a, b) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  function keepTargetVisible() {
    const target = active.target, sheet = active.parts.bubble.getBoundingClientRect(), box = target.getBoundingClientRect();
    if (!sheet.height || !box.height) return;
    try {
      if (box.bottom > sheet.top - 8) window.scrollBy(0, Math.ceil(box.bottom - (sheet.top - 8)));
      else if (box.top < 8) window.scrollBy(0, Math.floor(box.top - 8));
    } catch { /* no scrolling here */ }
  }
  // The bubble never covers the element it describes, nor the category menu.
  function place() {
    if (!active || !active.target || !active.target.isConnected) return;
    const bubble = active.parts.bubble;
    if (isPhone()) {
      bubble.dataset.placement = 'sheet'; bubble.style.left = ''; bubble.style.top = '';
      keepTargetVisible();
      return;
    }
    const width = bubble.offsetWidth, height = bubble.offsetHeight;
    if (!width || !height) { bubble.dataset.placement = active.places[0]; return; }
    const vw = doc.documentElement.clientWidth || window.innerWidth, vh = window.innerHeight, gap = 12, margin = 8;
    const box = active.target.getBoundingClientRect();
    const avoid = [{left: box.left - 4, top: box.top - 4, right: box.right + 4, bottom: box.bottom + 4}];
    const menu = get('category-menu');
    if (menu && !menu.hidden) avoid.push(menu.getBoundingClientRect());
    const spots = {
      bottom: () => ({left: clamp(box.left + box.width / 2 - width / 2, margin, vw - width - margin), top: box.bottom + gap}),
      top: () => ({left: clamp(box.left + box.width / 2 - width / 2, margin, vw - width - margin), top: box.top - gap - height}),
      right: () => ({left: box.right + gap, top: clamp(box.top + box.height / 2 - height / 2, margin, vh - height - margin)}),
      left: () => ({left: box.left - gap - width, top: clamp(box.top + box.height / 2 - height / 2, margin, vh - height - margin)}),
    };
    const rect = at => ({left: at.left, top: at.top, right: at.left + width, bottom: at.top + height});
    const inside = r => r.left >= margin && r.top >= margin && r.right <= vw - margin && r.bottom <= vh - margin;
    let choice = null;
    for (const side of active.places) {
      const at = spots[side](), r = rect(at);
      if (inside(r) && !avoid.some(a => overlap(r, a))) { choice = {side, at}; break; }
    }
    if (!choice) {
      for (const side of active.places) {
        const at = spots[side](), r = rect(at);
        if (!overlap(r, avoid[0])) { choice = {side, at}; break; }
      }
    }
    if (!choice) choice = {side: 'bottom', at: spots.bottom()};
    bubble.dataset.placement = choice.side;
    bubble.style.left = `${Math.round(choice.at.left)}px`; bubble.style.top = `${Math.round(choice.at.top)}px`;
    const across = choice.side === 'bottom' || choice.side === 'top'
      ? clamp(box.left + box.width / 2 - choice.at.left - 6, 14, width - 26)
      : clamp(box.top + box.height / 2 - choice.at.top - 6, 12, height - 24);
    bubble.style.setProperty('--guide-arrow', `${Math.round(across)}px`);
  }
  function schedule() {
    if (!active || frame) return;
    frame = window.setTimeout(() => { frame = 0; place(); }, 16);
  }

  // A step can come and go while the tour runs. A target that is gone is taken out and the counter follows.
  // The list itself is closed until the tour opens it, so only the boxes inside it count: hidden or display:none stops the step.
  function renderedInMenu(target) {
    if (!target.isConnected) return false;
    for (let n = target; n && n !== doc.body && n.id !== 'note-tools'; n = n.parentElement) {
      if (n.hidden || window.getComputedStyle(n).display === 'none') return false;
    }
    return !target.closest('[hidden]');
  }
  function usable(step) {
    const target = step.target();
    if (!target) return false;
    if (step.phoneOnly && !isPhone()) return false;
    // On a phone the tools sit in a list that the tour opens by itself, so a closed list is no reason to skip.
    if (step.menu && isPhone()) return renderedInMenu(target);
    return shown(target);
  }
  function mark(target) {
    if (active.target && active.target !== target) active.target.classList.remove('guide-target');
    active.target = target; if (target) target.classList.add('guide-target');
  }
  function menuFor(step) {
    const api = menus();
    if (!api || !isPhone()) return;
    active.menuChange = true;
    try {
      if (step.menu) { if (api.openNote({focus: false})) active.openedMenu = true; }
      else if (api.noteOpen()) api.closeNote({restore: false});
    } finally { active.menuChange = false; }
  }
  function render() {
    for (;;) {
      const step = active.steps[active.index];
      if (!step) { finish(true); return; }
      let target = null;
      if (usable(step)) {
        menuFor(step);
        const found = step.target();
        if (found && shown(found)) target = found;
      }
      if (!target) {
        active.steps.splice(active.index, 1);
        if (!active.steps.length) { close(); return; }
        if (active.index >= active.steps.length) { finish(true); return; }
        continue;
      }
      mark(target);
      const parts = active.parts, last = active.index === active.steps.length - 1;
      parts.text.replaceChildren(element('strong', '', step.lead), doc.createTextNode(` ${step.text}`));
      parts.count.textContent = `${active.index + 1} из ${active.steps.length}`;
      parts.next.textContent = last ? 'Готово' : 'Далее';
      save(active.name, {step: step.order});
      try { if (!isPhone() && target.scrollIntoView) target.scrollIntoView({block: 'nearest', inline: 'nearest'}); } catch { /* ignore */ }
      place();
      window.setTimeout(() => { if (active && active.steps[active.index] === step) active.parts.live.textContent = `${step.lead} ${step.text}`; }, 60);
      return;
    }
  }
  function onNext() {
    if (!active) return;
    if (active.index >= active.steps.length - 1) { finish(true); return; }
    active.index++;
    render();
  }
  // The person went through every tip, or asked not to see them again.
  function finish(done) {
    const name = active && active.name;
    if (done && name) save(name, {done: true});
    close();
  }

  function onKey(event) {
    if (event.key === 'Escape' && !event.defaultPrevented) close();
  }
  function onMenu(event) {
    // The person closed the note menu by hand while a phone tour needs it. The tour waits for later.
    if (!active || active.menuChange || !isPhone()) return;
    const detail = event.detail || {};
    if (detail.menu === 'note' && detail.open === false && active.steps[active.index] && active.steps[active.index].menu) close();
    if (detail.menu === 'general' && detail.open === true) close();
  }
  function onNoteRendered() {
    const id = noteId();
    if (active) {
      if (active.name === 'note_tools' && active.noteId !== id) close(); else schedule();
      return;
    }
    if (id) trigger('note_tools', id);
  }
  function onPop() {
    if (active && doc.body.classList.contains('has-pop')) close();
  }
  function startTracking() {
    if (tracking) return;
    tracking = true;
    window.addEventListener('resize', schedule);
    doc.addEventListener('scroll', schedule, true);
    doc.addEventListener('keydown', onKey);
    doc.addEventListener('beresta:menu', onMenu);
  }
  function stopTracking() {
    if (!tracking) return;
    tracking = false;
    window.removeEventListener('resize', schedule);
    doc.removeEventListener('scroll', schedule, true);
    doc.removeEventListener('keydown', onKey);
    doc.removeEventListener('beresta:menu', onMenu);
  }
  // Put the tour off. The saved step stays, nothing is marked as seen.
  function close() {
    if (!active) return false;
    const old = active; active = null;
    if (frame) { window.clearTimeout(frame); frame = 0; }
    if (old.target) old.target.classList.remove('guide-target');
    old.parts.bubble.remove(); old.parts.live.remove();
    stopTracking();
    const api = menus();
    if (api && old.openedMenu && api.noteOpen()) api.closeNote({restore: false});
    return true;
  }

  function begin(name, id) {
    const tour = TOURS[name], saved = guides[name];
    const steps = tour.steps().filter(usable);
    if (!steps.length) return false;
    const from = steps.findIndex(step => step.order >= saved.step);
    if (from === -1) { save(name, {done: true}); return false; }
    const parts = build();
    active = {name, noteId: id, steps, index: from, parts, places: tour.places, target: null, openedMenu: false, menuChange: false};
    shownThisLoad = true;
    parts.never.addEventListener('click', () => finish(true));
    parts.next.addEventListener('click', onNext);
    doc.body.append(parts.live, parts.bubble);
    startTracking();
    render();
    return true;
  }
  const pause = ms => new Promise(resolve => window.setTimeout(resolve, ms));
  async function trigger(name, id = null) {
    if (shownThisLoad || active) return false;
    try {
      await loadState();
      if (shownThisLoad || active || guides[name].done) return false;
      const wait = Number.isFinite(window.guideDelayMs) ? window.guideDelayMs : TOURS[name].wait;
      await pause(wait);
      // The «Знакомство» popup goes first. The tip waits until it is gone.
      for (let i = 0; i < 600 && profileBusy(); i++) await pause(Number.isFinite(window.guideDelayMs) ? Math.max(window.guideDelayMs, 5) : 500);
      if (shownThisLoad || active || profileBusy() || !TOURS[name].ready(id)) return false;
      return begin(name, id);
    } catch { return false; }
  }

  doc.addEventListener('beresta:note-rendered', onNoteRendered);
  new MutationObserver(onPop).observe(doc.body, {attributes: true, attributeFilter: ['class']});
  const categoryMenu = get('category-menu');
  if (categoryMenu) {
    new MutationObserver(() => { if (!categoryMenu.hidden) trigger('categories'); })
      .observe(categoryMenu, {attributes: true, attributeFilter: ['hidden']});
  }

  window.BerestaGuide = {
    // Starts a tour by hand, even if it was seen. Used by tests.
    play: async name => { await loadState(); close(); return begin(name, noteId()); },
    close, next: onNext,
    active: () => (active ? active.name : null),
    state: () => guides, loaded: () => loadState(), shownThisLoad: () => shownThisLoad,
  };
})();
