'use strict';
// «Знакомство» and «Что beresta знает обо мне».
// 1. A popup form asks a few free-form questions once. It shows when /api/v1/auth/me says profile_state is "new",
//    never on top of another dialog, the cookie bar, the policy banner or a guide tip. The text of the questions,
//    the intro and the privacy line come from GET /api/v1/profile.
// 2. When the AI has finished and suggests categories, the person confirms them (checkboxes, rename, add one).
//    The block shows on the last screen of the popup if the result arrives there, at the top of the dialog below,
//    and as a quiet bar once after the page loads.
// 3. A dialog from the menu shows what the AI concluded, lets a person edit or delete each fact, delete everything,
//    take the questions again and switch «Предлагать Google Календарь».
// app.js calls start() after every sign-in. Loaded after app.js and before guide.js.
// Everything is built from DOM nodes and textContent. Answers are kept in memory and in the consent-aware storage
// until they are sent.
(() => {
  const doc = document;
  const get = id => doc.getElementById(id);
  const PATH = '/api/v1/profile';
  const DRAFT_KEY = 'beresta.profile.draft.v1';
  const MAX_ANSWER = 1000, COUNTER_FROM = 800, MAX_FACT = 500, MAX_NAME = 100, MAX_NEW = 10;
  const PRIVACY = 'Ответы хранятся в зашифрованном виде, в админке мы их не видим. ИИ использует их только для разбора ваших заметок. Всё можно посмотреть, исправить или удалить в разделе «Что beresta знает обо мне».';
  const INTRO = 'Мы хотим подстроиться под вас. Ответьте на несколько вопросов своими словами, и beresta лучше разложит заметки, задачи и напоминания. На любой вопрос можно не отвечать.';
  const MINUTES_HINT = 'Так долго событие длится в Google Календаре, если у напоминания нет своей длины. Если вы сами назвали длительность («на два часа», «с 14 до 16»), берётся она.';
  const CALENDAR_HINT = 'К новому напоминанию появится ссылка «Добавить в Google Календарь». Она открывает Google Календарь с готовым событием. beresta не получает доступ к вашему аккаунту Google.';

  const el = (tag, text, className) => {
    const node = doc.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const button = (text, className, onclick) => {
    const node = el('button', text, className); node.type = 'button';
    if (onclick) node.addEventListener('click', onclick);
    return node;
  };
  const number = (name, fallback) => (Number.isFinite(window[name]) ? window[name] : fallback);
  const failure = error => (error && typeof error.message === 'string' && error.message ? error.message
    : 'Не удалось выполнить запрос. Попробуйте ещё раз.');

  let request = null, session = 0;
  let state = null, calendarOffer = true, calendarMinutes = 60, profile = null, shownThisLoad = false, waitTimer = 0, scheduled = false;
  let popup = null, ui = null, knowledge = null, know = null, bar = null;

  /* ---------- Draft: memory plus the consent-aware storage ---------- */
  let memoryDraft = '';
  function readDraft() {
    let raw = memoryDraft;
    try { if (!raw && window.berestaConsent) raw = window.berestaConsent.read(DRAFT_KEY) || ''; } catch { raw = raw || ''; }
    if (!raw) { try { raw = window.sessionStorage.getItem(DRAFT_KEY) || ''; } catch { raw = ''; } }
    try {
      const data = JSON.parse(raw || '{}');
      return data && typeof data === 'object' ? data : {};
    } catch { return {}; }
  }
  function writeDraft(value) {
    memoryDraft = value;
    try { if (window.berestaConsent) window.berestaConsent.write(DRAFT_KEY, value); } catch { /* storage may be blocked */ }
    try { window.sessionStorage.setItem(DRAFT_KEY, value); } catch { /* memory only */ }
  }
  function saveDraft() {
    if (!ui) return;
    const answers = {};
    for (const [id, text] of Object.entries(ui.answers)) if (text) answers[id] = text;
    writeDraft(Object.keys(answers).length ? JSON.stringify({answers}) : '');
  }
  const clearDraft = () => writeDraft('');

  /* ---------- Shared pieces ---------- */
  function makeDialog(id, titleId) {
    const dialog = el('dialog', '', 'utility-dialog profile-dialog');
    dialog.id = id; dialog.setAttribute('aria-labelledby', titleId);
    dialog.addEventListener('keydown', event => {
      if (event.key !== 'Tab' || event.defaultPrevented) return;
      const items = [...dialog.querySelectorAll('button, textarea, input, a[href]')].filter(node => !node.disabled && !node.hidden && !node.closest('[hidden]'));
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1], active = doc.activeElement;
      if (event.shiftKey && (active === first || !dialog.contains(active))) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (active === last || !dialog.contains(active))) { event.preventDefault(); first.focus(); }
    });
    return dialog;
  }
  function openModal(dialog) {
    doc.body.append(dialog);
    if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
  }
  function closeModal(dialog) {
    if (!dialog || !dialog.isConnected) return;
    if (typeof dialog.close === 'function') dialog.close();
    else { dialog.removeAttribute('open'); dialog.dispatchEvent(new Event('close')); }
  }
  function heading(titleId, title, onClose) {
    const head = el('div', '', 'dialog-heading');
    const h2 = el('h2', title); h2.id = titleId; h2.tabIndex = -1;
    const close = button('×', 'profile-close', onClose); close.setAttribute('aria-label', 'Закрыть');
    head.append(h2, close);
    return {head, h2, close};
  }
  const statusLine = () => { const p = el('p', '', 'dialog-status profile-status'); p.setAttribute('role', 'status'); p.setAttribute('aria-live', 'polite'); return p; };
  const suggestions = () => (profile && Array.isArray(profile.suggested_categories)
    ? profile.suggested_categories.filter(name => typeof name === 'string' && name.trim()) : []);
  const wantsCategories = () => state === 'ready' && profile && profile.categories_confirmed === false && suggestions().length > 0;
  function refreshCategoryList() {
    try { if (typeof loadCategories === 'function') Promise.resolve(loadCategories()).catch(() => {}); } catch { /* the list refreshes on the next load */ }
  }

  // The single switch «Предлагать Google Календарь». `onPick` returns a promise, a failure puts the old value back.
  function calendarSwitch(current, onPick) {
    const wrap = el('div', '', 'profile-calendar');
    const label = el('label', '', 'switch profile-switch');
    const input = doc.createElement('input');
    input.type = 'checkbox'; input.setAttribute('role', 'switch'); input.checked = Boolean(current); input.id = 'profile-calendar-offer';
    const track = el('span', '', 'switch-track'); track.setAttribute('aria-hidden', 'true');
    label.append(input, track, el('span', 'Предлагать Google Календарь', 'switch-text'));
    let value = Boolean(current);
    input.addEventListener('change', async () => {
      const wanted = input.checked;
      if (wanted === value) return;
      try { await onPick(wanted); value = wanted; } catch { input.checked = value; }
    });
    wrap.append(label, el('p', CALENDAR_HINT, 'muted profile-hint'));
    return {box: wrap, input};
  }

  // «Длительность события в календаре по умолчанию». `onPick` returns a promise, a failure puts the old value back.
  const MINUTE_CHOICES = [15, 30, 60, 90, 120];
  const minuteLabel = minutes => (minutes === 60 ? '1 час' : minutes === 90 ? '1,5 часа' : minutes === 120 ? '2 часа' : `${minutes} минут`);
  function calendarMinutesSelect(current, onPick) {
    const wrap = el('div', '', 'profile-calendar-minutes');
    const label = el('label', 'Длительность события в календаре по умолчанию', 'profile-minutes-label'); label.htmlFor = 'profile-calendar-minutes';
    const select = doc.createElement('select'); select.id = 'profile-calendar-minutes'; select.className = 'profile-minutes';
    for (const minutes of MINUTE_CHOICES) {
      const option = doc.createElement('option'); option.value = String(minutes); option.textContent = minuteLabel(minutes);
      select.append(option);
    }
    select.value = String(MINUTE_CHOICES.includes(current) ? current : 60);
    let value = Number(select.value);
    select.addEventListener('change', async () => {
      const wanted = Number(select.value);
      if (wanted === value) return;
      try { await onPick(wanted); value = wanted; } catch { select.value = String(value); }
    });
    wrap.append(label, select, el('p', MINUTES_HINT, 'muted profile-hint'));
    return {box: wrap, select};
  }

  // «Создадим категории: …». Rows are checkboxes with a name that can be edited. `done` runs after the server answered.
  function categoriesBox(done) {
    const box = el('section', '', 'profile-categories'); box.setAttribute('aria-labelledby', 'profile-categories-title');
    const title = el('h3', 'Создадим категории', 'profile-categories-title'); title.id = 'profile-categories-title';
    const lead = el('p', 'beresta заметила, чем вы занимаетесь. Выберите, какие категории создать. Названия можно поправить.', 'muted profile-hint');
    const rows = el('div', '', 'profile-category-rows'); rows.setAttribute('role', 'group'); rows.setAttribute('aria-label', 'Категории');
    const status = statusLine();
    const row = (name, focus) => {
      const line = el('div', '', 'profile-category-row');
      const check = doc.createElement('input'); check.type = 'checkbox'; check.checked = true;
      check.setAttribute('aria-label', 'Создать категорию');
      const text = doc.createElement('input'); text.type = 'text'; text.value = name; text.maxLength = MAX_NAME;
      text.setAttribute('aria-label', 'Название категории'); text.className = 'profile-category-name';
      text.addEventListener('input', () => { if (text.value.trim() && !check.checked) check.checked = true; paint(); });
      check.addEventListener('change', paint);
      line.append(check, text); rows.append(line);
      if (focus) text.focus();
    };
    suggestions().slice(0, MAX_NEW).forEach(name => row(name, false));
    const chosen = () => {
      const names = [], seen = new Set();
      for (const item of [...rows.querySelectorAll('.profile-category-row')]) {
        const check = item.querySelector('input[type="checkbox"]'), text = item.querySelector('input[type="text"]');
        const name = text.value.trim().replace(/\s+/g, ' ').slice(0, MAX_NAME), key = name.toLowerCase().replaceAll('ё', 'е');
        if (check.checked && name && !seen.has(key)) { seen.add(key); names.push(name); }
      }
      return names;
    };
    function busyAll(value) { for (const node of box.querySelectorAll('button, input')) node.disabled = value; if (!value) paint(); }
    const make = button('Создать', 'primary', async () => {
      const names = chosen(); if (!names.length) return;
      const mine = session;
      busyAll(true); status.textContent = 'Создаём…';
      try {
        const data = await request(`${PATH}/categories`, {method: 'POST', body: JSON.stringify({names})});
        if (mine !== session) return;
        adopt(data); refreshCategoryList();
        done({kind: 'created', names: data && Array.isArray(data.created) ? data.created : names});
      } catch (error) { if (mine === session) { busyAll(false); status.textContent = failure(error); } }
    });
    make.dataset.action = 'categories-create';
    const skip = button('Не нужно', 'quiet', async () => {
      const mine = session;
      busyAll(true); status.textContent = 'Сохраняем…';
      try {
        const data = await request(`${PATH}/categories/skip`, {method: 'POST', body: '{}'});
        if (mine !== session) return;
        adopt(data); done({kind: 'skipped', names: []});
      } catch (error) { if (mine === session) { busyAll(false); status.textContent = failure(error); } }
    });
    skip.dataset.action = 'categories-skip';
    const add = button('+ Своя категория', 'text-button', () => { if (rows.children.length < MAX_NEW) { row('', true); paint(); } });
    add.dataset.action = 'categories-add';
    function paint() { make.disabled = chosen().length === 0; add.disabled = rows.children.length >= MAX_NEW; }
    const actions = el('div', '', 'actions profile-category-actions');
    actions.append(make, skip);
    box.append(title, lead, rows, add, status, actions);
    paint();
    return {box};
  }
  const categoriesResult = result => (result.kind === 'created' && result.names.length
    ? `Готово. Категории созданы: ${result.names.join(', ')}.` : result.kind === 'created' ? 'Готово. Такие категории у вас уже есть.' : 'Хорошо, категории не создаём.');

  /* ---------- Waiting for a quiet moment ---------- */
  function blockedBy() {
    const workspace = get('workspace');
    if (!workspace || workspace.hidden) return 'workspace';
    const policy = get('policy-banner');
    if (policy && !policy.hidden) return 'policy';
    if (get('cookie-consent')) return 'cookies';
    for (const dialog of doc.querySelectorAll('dialog[open]')) if (dialog !== popup) return 'dialog';
    if (doc.body.classList.contains('has-pop')) return 'popover';
    if (doc.querySelector('.guide-bubble') || (window.BerestaGuide && window.BerestaGuide.active())) return 'guide';
    return '';
  }
  function stopWaiting() { if (waitTimer) { window.clearInterval(waitTimer); waitTimer = 0; } }
  function tryShow() {
    if (!scheduled) { stopWaiting(); return; }
    if (shownThisLoad || state !== 'new' || !profile) { scheduled = false; stopWaiting(); return; }
    if (popup || blockedBy()) return;
    stopWaiting(); openQuestions({manual: false});
  }
  function schedule() {
    scheduled = true;
    tryShow();
    if (scheduled && !waitTimer) waitTimer = window.setInterval(tryShow, number('profileWaitMs', 1000));
  }

  /* ---------- Questions popup ---------- */
  const total = () => profile.questions.length;
  // Steps: 0 intro, 1..n one question each, n+1 the last screen.
  // On the last screen the popup waits for the result: the facts, then the suggested categories.
  function pollResult() {
    const run = ui, left = Math.floor(number('profilePollMaxMs', 120000) / number('profilePollMs', 3000));
    let attempts = 0;
    const tick = async () => {
      if (ui !== run || !popup) return;
      attempts++;
      try { await reload(); } catch { /* try again */ }
      if (ui !== run || !popup) return;
      if (state !== 'pending') { renderPopup(); return; }
      if (attempts >= left) return;
      run.poll = window.setTimeout(tick, number('profilePollMs', 3000));
    };
    run.poll = window.setTimeout(tick, number('profilePollMs', 3000));
  }
  function renderPopup() {
    if (!popup) return;
    const body = popup.querySelector('.profile-body'), foot = popup.querySelector('.profile-foot');
    const title = popup.querySelector('#profile-title'), progress = popup.querySelector('.profile-progress');
    body.replaceChildren(); foot.replaceChildren();
    popup.querySelector('.profile-status').textContent = '';
    const n = total(), step = ui.step;
    let focus = null;
    progress.hidden = step === 0 || step > n;
    if (!progress.hidden) {
      const meter = progress.querySelector('progress'), count = progress.querySelector('.profile-count-text');
      meter.max = n; meter.value = Math.min(step, n);
      count.textContent = `${step} из ${n}`;
      meter.setAttribute('aria-label', 'Ход знакомства');
    }
    if (step === 0) {
      title.textContent = 'Давайте познакомимся';
      body.append(el('p', ui.intro, 'profile-lead'), el('p', ui.privacy, 'muted profile-privacy'));
      if (ui.confirm) {
        const row = el('div', '', 'profile-confirm'); row.setAttribute('role', 'group'); row.setAttribute('aria-label', 'Больше не показывать');
        row.append(el('p', 'Больше не показывать?', 'profile-confirm-text'));
        const yes = button('Да', 'primary', () => decline()); yes.dataset.action = 'confirm-decline';
        const back = button('Вернуться', 'quiet', () => { ui.confirm = false; renderPopup(); }); back.dataset.action = 'confirm-back';
        foot.append(row); row.append(yes, back); focus = back;
      } else {
        const start = button('Начать', 'primary large', () => go(1)); start.dataset.action = 'start';
        const no = button('Нет, спасибо', 'quiet', () => decline()); no.dataset.action = 'decline';
        foot.append(start, no); focus = start;
      }
    } else if (step <= n) {
      title.textContent = 'Знакомство';
      const question = profile.questions[step - 1], last = step === n;
      const label = el('label', question.text, 'profile-question'); label.htmlFor = 'profile-answer';
      const area = doc.createElement('textarea');
      area.id = 'profile-answer'; area.rows = 6; area.maxLength = MAX_ANSWER; area.value = ui.answers[question.id] || '';
      area.setAttribute('aria-describedby', 'profile-counter');
      const counter = el('p', '', 'muted profile-counter'); counter.id = 'profile-counter';
      const paint = () => {
        const left = MAX_ANSWER - area.value.length;
        counter.hidden = area.value.length < COUNTER_FROM;
        counter.textContent = counter.hidden ? '' : `Осталось знаков ${Math.max(0, left)}`;
      };
      area.addEventListener('input', () => { ui.answers[question.id] = area.value.slice(0, MAX_ANSWER); paint(); saveDraft(); });
      area.addEventListener('keydown', event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); forward(step); } });
      paint();
      body.append(label, area, counter);
      if (last && ui.empty) {
        const warn = el('div', '', 'profile-warning'); warn.setAttribute('role', 'alert');
        warn.append(el('p', 'Нужен хотя бы один ответ. Вернитесь к вопросам или откажитесь от знакомства.'));
        const again = button('Вернуться к вопросам', 'secondary', () => { ui.empty = false; go(1); }); again.dataset.action = 'to-questions';
        const no = button('Нет, спасибо', 'quiet', () => decline()); no.dataset.action = 'decline';
        warn.append(again, no); body.append(warn);
      }
      const back = button('Назад', 'quiet', () => go(step - 1)); back.dataset.action = 'back';
      const skip = button('Пропустить', 'secondary', () => { ui.answers[question.id] = ''; saveDraft(); forward(step); }); skip.dataset.action = 'skip';
      const next = button(last ? 'Готово' : 'Далее', 'primary', () => forward(step));
      next.dataset.action = last ? 'done' : 'next';
      back.disabled = skip.disabled = next.disabled = ui.busy;
      foot.append(back, skip, next); focus = area;
    } else {
      title.textContent = 'Знакомство';
      const ready = state === 'ready', failed = state === 'failed';
      body.append(el('p', ready ? 'Спасибо! Профиль готов.' : failed ? 'Спасибо! Ответы сохранены, но разобрать их сразу не вышло.'
        : 'Спасибо! beresta разбирает ответы, это займёт до минуты.', 'profile-lead'));
      if (failed) body.append(el('p', 'Попробовать ещё раз можно в меню, в разделе «Что beresta знает обо мне».', 'muted'));
      else if (!ui.categoriesNote) body.append(el('p', 'Результат можно посмотреть и поправить в меню, в разделе «Что beresta знает обо мне».', 'muted'));
      const offer = wantsCategories() && !ui.categoriesNote;
      if (ui.categoriesNote) body.append(el('p', ui.categoriesNote, 'profile-lead profile-categories-result'));
      else if (offer) body.append(categoriesBox(result => { ui.categoriesNote = categoriesResult(result); renderPopup(); }).box);
      const close = button('Закрыть', offer ? 'quiet' : 'primary', () => closeModal(popup)); close.dataset.action = 'close';
      foot.append(close); focus = offer ? body.querySelector('.profile-category-name') : close;
    }
    if (focus && focus.focus) focus.focus();
  }
  function go(step) {
    if (!popup || ui.busy) return;
    ui.step = Math.max(0, Math.min(total() + 1, step)); ui.confirm = false; ui.empty = false;
    renderPopup();
  }
  // «Готово» on the last question sends the answers.
  function forward(step) { if (step >= total()) submit(); else go(step + 1); }
  function say(text) { if (popup) popup.querySelector('.profile-status').textContent = text; }
  function busy(value) {
    ui.busy = value;
    if (popup) for (const node of popup.querySelectorAll('.profile-foot button, .profile-confirm button, .profile-warning button')) node.disabled = value;
  }
  function collect() {
    const answers = {};
    for (const question of profile.questions) {
      const text = String(ui.answers[question.id] || '').trim().slice(0, MAX_ANSWER);
      if (text) answers[question.id] = text;
    }
    return answers;
  }
  async function submit() {
    if (!popup || ui.busy) return;
    const answers = collect();
    if (!Object.keys(answers).length) { ui.empty = true; ui.step = total(); renderPopup(); return; }
    const mine = session;
    busy(true); say('Отправляем…');
    try {
      await request(`${PATH}/answers`, {method: 'POST', body: JSON.stringify({answers})});
      if (mine !== session || !popup) return;
      state = 'pending'; ui.done = true;
      clearDraft(); ui.answers = {};
      busy(false); ui.step = total() + 1; renderPopup(); pollResult();
    } catch (error) {
      if (mine !== session || !popup) return;
      busy(false); say(failure(error));
    }
  }
  async function decline() {
    if (!popup || ui.busy) return;
    const mine = session;
    busy(true); say('Сохраняем…');
    try {
      await request(`${PATH}/decline`, {method: 'POST', body: '{}'});
      if (mine !== session) return;
      state = 'declined'; clearDraft(); ui.answers = {}; ui.declined = true;
      busy(false); closeModal(popup);
    } catch (error) {
      if (mine !== session || !popup) return;
      busy(false); say(failure(error));
    }
  }
  // Esc and the cross. On the first screen of an automatic popup it means «no»; the person is asked once.
  function requestClose() {
    if (!popup || ui.busy) return;
    if (ui.step === 0 && !ui.manual && !ui.done) {
      if (!ui.confirm) { ui.confirm = true; renderPopup(); }
      return;
    }
    closeModal(popup);
  }
  function openQuestions({manual}) {
    if (popup || !profile || !profile.questions.length) return false;
    shownThisLoad = true; scheduled = false; stopWaiting();
    closeModal(knowledge); hideBar();
    const draft = readDraft(), answers = {};
    const known = new Set(profile.questions.map(question => question.id));
    const saved = draft.answers && typeof draft.answers === 'object' ? draft.answers : {};
    const old = profile.answers && typeof profile.answers === 'object' ? profile.answers : {};
    for (const id of known) {
      const text = typeof saved[id] === 'string' ? saved[id] : (Object.keys(saved).length ? '' : (typeof old[id] === 'string' ? old[id] : ''));
      if (text) answers[id] = text.slice(0, MAX_ANSWER);
    }
    ui = {step: 0, answers, manual, confirm: false, empty: false, busy: false, done: false, declined: false, poll: 0, categoriesNote: '',
      intro: profile.intro || INTRO, privacy: profile.privacy || PRIVACY};
    popup = makeDialog('profile-dialog', 'profile-title');
    const {head, close} = heading('profile-title', 'Давайте познакомимся', requestClose);
    close.dataset.action = 'x';
    const progress = el('div', '', 'profile-progress');
    const meter = doc.createElement('progress'); meter.max = 1; meter.value = 0;
    progress.append(meter, el('span', '', 'profile-count-text'));
    popup.append(head, progress, el('div', '', 'profile-body'), statusLine(), el('div', '', 'actions profile-foot'));
    popup.addEventListener('cancel', event => { event.preventDefault(); requestClose(); });
    popup.addEventListener('keydown', event => { if (event.key === 'Escape' && !event.defaultPrevented) { event.preventDefault(); requestClose(); } });
    const mine = popup, mineUi = ui;
    popup.addEventListener('close', () => {
      if (mineUi.poll) window.clearTimeout(mineUi.poll);
      mine.remove();
      if (popup === mine) { popup = null; }
      doc.dispatchEvent(new CustomEvent('beresta:profile', {detail: {open: false}}));
    });
    openModal(popup);
    doc.dispatchEvent(new CustomEvent('beresta:profile', {detail: {open: true}}));
    renderPopup();
    return true;
  }

  /* ---------- Quiet bar: «beresta предлагает категории под вас» ---------- */
  function hideBar() { if (bar) { bar.remove(); bar = null; } }
  function showBar() {
    if (bar || !wantsCategories()) return;
    const anchor = get('timezone-banner') || get('policy-banner');
    if (!anchor) return;
    bar = el('div', '', 'notice-bar profile-bar'); bar.setAttribute('role', 'region'); bar.setAttribute('aria-label', 'Категории под вас');
    const actions = el('div', '', 'profile-bar-actions');
    const open = button('Посмотреть', 'secondary small', event => { const opener = event.currentTarget; hideBar(); openKnowledge(opener); });
    open.dataset.action = 'bar-open';
    const later = button('Позже', 'quiet small', hideBar); later.dataset.action = 'bar-later';
    actions.append(open, later);
    bar.append(el('p', 'beresta предлагает категории под вас'), actions);
    anchor.after(bar);
  }

  /* ---------- «Что beresta знает обо мне» ---------- */
  function knowStatus(text) { if (know && know.status) know.status.textContent = text; }
  function adopt(data) {
    if (!data || typeof data !== 'object') return;
    profile = {...(profile || {}), ...data};
    if (typeof data.state === 'string') state = data.state;
    if (typeof data.calendar_offer === 'boolean') calendarOffer = data.calendar_offer;
    if (MINUTE_CHOICES.includes(data.calendar_default_minutes)) calendarMinutes = data.calendar_default_minutes;
    if (state !== 'ready' || profile.categories_confirmed !== false) hideBar();
  }
  async function reload() {
    const mine = session;
    const data = await request(PATH);
    if (mine === session) adopt(data);
    return data;
  }
  function stopPolling() { if (know && know.poll) { window.clearTimeout(know.poll); know.poll = 0; } }
  function poll() {
    stopPolling();
    if (!know) return;
    const run = know, left = Math.floor(number('profilePollMaxMs', 120000) / number('profilePollMs', 3000));
    run.attempts = 0;
    const tick = async () => {
      if (know !== run) return;
      run.attempts++;
      try { await reload(); } catch { /* try again on the next tick */ }
      if (know !== run) return;
      if (state !== 'pending') { renderKnowledge(); return; }
      if (run.attempts >= left) { run.slow = true; renderKnowledge(); return; }
      run.poll = window.setTimeout(tick, number('profilePollMs', 3000));
    };
    run.poll = window.setTimeout(tick, number('profilePollMs', 3000));
  }
  function factItem(fact) {
    const item = el('li', '', 'profile-fact'); item.dataset.id = fact.id;
    const text = el('span', fact.text, 'profile-fact-text');
    const edit = button('Изменить', 'quiet small', () => editFact(item, fact)); edit.dataset.action = 'edit';
    const del = button('Удалить', 'quiet small', () => deleteFact(item, fact)); del.dataset.action = 'delete';
    const row = el('div', '', 'profile-fact-actions'); row.append(edit, del);
    item.append(text, row);
    return item;
  }
  // PATCH and DELETE answer with the whole profile. An older shape (the fact itself, or nothing) still works.
  function takeFacts(data) { if (data && Array.isArray(data.facts)) adopt(data); }
  function editFact(item, fact) {
    const area = doc.createElement('textarea');
    area.rows = 2; area.maxLength = MAX_FACT; area.value = fact.text; area.setAttribute('aria-label', 'Текст вывода');
    const save = button('Сохранить', 'primary small', async () => {
      const text = area.value.trim();
      if (!text) { knowStatus('Текст не может быть пустым. Чтобы убрать вывод, нажмите «Удалить».'); return; }
      const mine = session;
      save.disabled = true; cancel.disabled = true; knowStatus('Сохраняем…');
      try {
        const data = await request(`${PATH}/facts/${encodeURIComponent(fact.id)}`, {method: 'PATCH', body: JSON.stringify({text})});
        if (mine !== session || !know) return;
        takeFacts(data);
        const own = data && Array.isArray(data.facts) ? data.facts.find(row => row.id === fact.id) : (data && typeof data.text === 'string' ? data : null);
        fact.text = own && typeof own.text === 'string' ? own.text : text;
        item.replaceWith(factItem(fact)); knowStatus('Сохранено.');
      } catch (error) { if (mine === session && know) { save.disabled = false; cancel.disabled = false; knowStatus(failure(error)); } }
    });
    save.dataset.action = 'save';
    const cancel = button('Отмена', 'quiet small', () => { item.replaceWith(factItem(fact)); knowStatus(''); }); cancel.dataset.action = 'cancel';
    const row = el('div', '', 'profile-fact-actions'); row.append(save, cancel);
    item.replaceChildren(area, row); area.focus();
  }
  async function deleteFact(item, fact) {
    const mine = session;
    for (const node of item.querySelectorAll('button')) node.disabled = true;
    knowStatus('Удаляем…');
    try {
      const data = await request(`${PATH}/facts/${encodeURIComponent(fact.id)}`, {method: 'DELETE'});
      if (mine !== session || !know) return;
      takeFacts(data);
      profile.facts = (profile.facts || []).filter(row => row.id !== fact.id);
      const list = item.parentElement, next = item.nextElementSibling || item.previousElementSibling;
      item.remove(); knowStatus('Удалено.');
      if (!profile.facts.length) renderKnowledge(); else if (next && list) (next.querySelector('button') || know.h2).focus();
    } catch (error) {
      if (mine === session && know) { for (const node of item.querySelectorAll('button')) node.disabled = false; knowStatus(failure(error)); }
    }
  }
  async function deleteAll() {
    const mine = session;
    knowStatus('Удаляем…');
    try {
      const data = await request(PATH, {method: 'DELETE'});
      if (mine !== session || !know) return;
      adopt(data);
      state = 'declined'; profile = {...profile, state: 'declined', facts: [], answers: null, suggested_categories: [], categories_confirmed: true};
      know.confirm = false; renderKnowledge(); knowStatus('Всё удалено.');
    } catch (error) { if (mine === session && know) { know.confirm = false; renderKnowledge(); knowStatus(failure(error)); } }
  }
  async function retry() {
    const mine = session;
    knowStatus('Отправляем…');
    try {
      const data = await request(`${PATH}/retry`, {method: 'POST', body: '{}'});
      if (mine !== session || !know) return;
      adopt(data && data.state ? data : {state: 'pending'});
      know.slow = false; renderKnowledge(); poll();
    } catch (error) { if (mine === session && know) knowStatus(failure(error)); }
  }
  function renderKnowledge() {
    if (!know) return;
    const body = know.body;
    body.replaceChildren();
    const keep = know.status.textContent;
    let focus = null;
    if (state === 'ready') {
      if (wantsCategories() && !know.categoriesNote) {
        body.append(categoriesBox(result => { know.categoriesNote = categoriesResult(result); renderKnowledge(); knowStatus(''); }).box);
      } else if (know.categoriesNote) body.append(el('p', know.categoriesNote, 'profile-lead profile-categories-result'));
      const facts = Array.isArray(profile.facts) ? profile.facts : [];
      if (!facts.length) body.append(el('p', 'Пока здесь нет ни одного вывода.', 'muted'));
      else {
        body.append(el('p', 'Вот что beresta поняла о вас. Любой пункт можно поправить или убрать.', 'profile-lead'));
        const list = el('ul', '', 'profile-facts'); list.setAttribute('aria-label', 'Что beresta знает обо мне');
        for (const fact of facts) list.append(factItem(fact));
        body.append(list);
      }
      const row = el('div', '', 'actions profile-tools');
      const again = button('Пройти вопросы заново', 'secondary', () => openQuestions({manual: true})); again.dataset.action = 'retake';
      row.append(again);
      if (know.confirm) {
        const box = el('div', '', 'profile-confirm'); box.setAttribute('role', 'group'); box.setAttribute('aria-label', 'Удалить всё');
        box.append(el('p', 'Удалить все ответы и выводы? Это нельзя отменить.', 'profile-confirm-text'));
        const yes = button('Да, удалить всё', 'danger', deleteAll); yes.dataset.action = 'confirm-delete-all';
        const no = button('Отмена', 'quiet', () => { know.confirm = false; renderKnowledge(); know.all.focus(); }); no.dataset.action = 'cancel-delete-all';
        box.append(yes, no); body.append(row, box); focus = no;
      } else {
        const all = button('Удалить всё', 'danger', () => { know.confirm = true; renderKnowledge(); }); all.dataset.action = 'delete-all';
        know.all = all; row.append(all); body.append(row);
      }
    } else if (state === 'pending') {
      if (know.slow) {
        body.append(el('p', 'Разбор занимает дольше обычного. Загляните сюда чуть позже.', 'profile-lead'));
        const check = button('Проверить', 'secondary', async () => {
          know.slow = false; knowStatus('Проверяем…');
          try { await reload(); } catch (error) { knowStatus(failure(error)); }
          if (!know) return;
          renderKnowledge(); if (state === 'pending') poll();
        });
        check.dataset.action = 'check'; body.append(check);
      } else body.append(el('p', 'Разбираем ответы…', 'profile-lead'));
    } else if (state === 'failed') {
      body.append(el('p', 'Не получилось разобрать ответы', 'profile-lead'),
        el('p', typeof profile.error === 'string' && profile.error ? `${profile.error} Ваши ответы сохранены.` : 'Ваши ответы сохранены. Попробуйте ещё раз.', 'muted'));
      const again = button('Попробовать ещё раз', 'primary', retry); again.dataset.action = 'retry'; body.append(again); focus = again;
    } else {
      body.append(el('p', 'Вы пока не отвечали на вопросы. Расскажите о себе, и beresta лучше подстроится под вас.', 'profile-lead'));
      const start = button('Пройти вопросы', 'primary', () => openQuestions({manual: true})); start.dataset.action = 'start'; body.append(start);
      focus = start;
    }
    know.status.textContent = keep;
    if (focus && know.focusAfter) focus.focus();
  }
  async function openKnowledge(opener) {
    if (knowledge || !request) return false;
    const mine = session;
    know = {confirm: false, slow: false, poll: 0, attempts: 0, opener: opener || null, focusAfter: false, categoriesNote: ''};
    knowledge = makeDialog('knowledge-dialog', 'knowledge-title');
    const {head, h2} = heading('knowledge-title', 'Что beresta знает обо мне', () => closeModal(knowledge));
    know.h2 = h2;
    know.body = el('div', '', 'profile-know-body');
    know.status = statusLine();
    const switchWrap = el('div', '', 'profile-know-calendar');
    knowledge.append(head, know.body, know.status, switchWrap, el('p', profile && profile.privacy || PRIVACY, 'muted profile-privacy'));
    const dialog = knowledge;
    knowledge.addEventListener('close', () => {
      stopPolling(); dialog.remove();
      if (knowledge === dialog) { knowledge = null; know = null; }
      if (dialog._opener && dialog._opener.isConnected) dialog._opener.focus();
    });
    knowledge._opener = opener || null;
    openModal(knowledge);
    know.body.append(el('p', 'Загружаем…', 'muted'));
    try { await reload(); } catch (error) {
      if (mine === session && know) { know.body.replaceChildren(el('p', failure(error), 'muted')); }
    }
    if (mine !== session || !know || knowledge !== dialog) return true;
    const picker = calendarSwitch(calendarOffer, async value => {
      knowStatus('Сохраняем…');
      try {
        await request(`${PATH}/calendar-offer`, {method: 'PATCH', body: JSON.stringify({calendar_offer: value})});
        calendarOffer = value; knowStatus('Сохранено.');
      } catch (error) { knowStatus(failure(error)); throw error; }
    });
    const minutes = calendarMinutesSelect(calendarMinutes, async value => {
      knowStatus('Сохраняем…');
      try {
        await request(`${PATH}/calendar-minutes`, {method: 'PATCH', body: JSON.stringify({calendar_default_minutes: value})});
        calendarMinutes = value; knowStatus('Сохранено.');
      } catch (error) { knowStatus(failure(error)); throw error; }
    });
    switchWrap.replaceChildren(picker.box, minutes.box);
    know.picker = picker; know.minutes = minutes;
    renderKnowledge();
    if (state === 'pending') poll();
    know.focusAfter = true;
    h2.focus();
    return true;
  }

  /* ---------- Entry points ---------- */
  function reset() {
    session++; stopWaiting(); scheduled = false; shownThisLoad = false; state = null; profile = null; calendarOffer = true; calendarMinutes = 60;
    const a = popup, b = knowledge;
    popup = null; ui = null; knowledge = null;
    stopPolling(); know = null; hideBar();
    for (const dialog of [a, b]) if (dialog && dialog.isConnected) { if (typeof dialog.close === 'function') dialog.close(); dialog.remove(); }
  }
  // `user` is the answer of /api/v1/auth/me, `send` is the app's api() with the CSRF header.
  async function start(user, send) {
    reset(); request = send;
    if (!user || typeof user !== 'object') return;
    state = typeof user.profile_state === 'string' ? user.profile_state : null;
    if (typeof user.calendar_offer === 'boolean') calendarOffer = user.calendar_offer;
    if (MINUTE_CHOICES.includes(user.calendar_default_minutes)) calendarMinutes = user.calendar_default_minutes;
    if (state !== 'new' && state !== 'ready') return;
    const mine = session;
    try {
      const data = await request(PATH);
      if (mine !== session) return;
      if (!data || typeof data !== 'object') return;
      adopt(data);
      if (state === 'ready') { showBar(); return; }
      // A state that changed since the sign-in (another tab) wins.
      if (state !== 'new' || !Array.isArray(data.questions)) return;
      profile.questions = data.questions.filter(row => row && typeof row.id === 'string' && typeof row.text === 'string');
      if (!profile.questions.length) return;
      schedule();
    } catch { /* the popup comes back at the next load */ }
  }
  function install() {
    if (get('open-profile')) return;
    const entry = button('Что beresta знает обо мне', 'nav-action', event => openKnowledge(event.currentTarget));
    entry.id = 'open-profile';
    const menu = doc.querySelector('#nav-menu .nav-bottom') || get('nav-menu');
    if (menu) menu.insertBefore(entry, get('open-timezone') || null);
  }
  install();
  const workspace = get('workspace');
  if (workspace && typeof MutationObserver === 'function') {
    new MutationObserver(() => { if (workspace.hidden) reset(); }).observe(workspace, {attributes: true, attributeFilter: ['hidden']});
  }

  window.BerestaProfile = {
    start, openKnowledge, openQuestions: () => openQuestions({manual: true}),
    // The guided tips wait while this is true: the popup is open or about to open.
    pending: () => Boolean(popup) || (scheduled && !shownThisLoad),
    state: () => state, calendarOffer: () => calendarOffer, calendarMinutes: () => calendarMinutes,
  };
})();
