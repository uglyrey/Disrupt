'use strict';
// «Знакомство» and «Что beresta знает обо мне».
// 1. A popup form asks a few free-form questions once. It shows when /api/v1/auth/me says profile_state is "new",
//    never on top of another dialog, the cookie bar, the policy banner or a guide tip. The text of the questions,
//    the intro and the privacy line come from GET /api/v1/profile.
// 2. A dialog from the menu shows what the AI concluded, lets a person edit or delete each fact, delete everything,
//    take the questions again and pick the channel of reminders.
// app.js calls start() after every sign-in. Loaded after app.js and before guide.js.
// Everything is built from DOM nodes and textContent. Answers are kept in memory and in the consent-aware storage
// until they are sent.
(() => {
  const doc = document;
  const get = id => doc.getElementById(id);
  const PATH = '/api/v1/profile';
  const DRAFT_KEY = 'beresta.profile.draft.v1';
  const MAX_ANSWER = 1000, COUNTER_FROM = 800, MAX_FACT = 500;
  const PRIVACY = 'Ответы хранятся в зашифрованном виде, в админке мы их не видим. ИИ использует их только для разбора ваших заметок. Всё можно посмотреть, исправить или удалить в разделе «Что beresta знает обо мне».';
  const INTRO = 'Мы хотим подстроиться под вас. Ответьте на несколько вопросов своими словами, и beresta лучше разложит заметки, задачи и напоминания. На любой вопрос можно не отвечать.';
  const CHANNELS = [
    ['telegram', 'Только Telegram'],
    ['telegram_calendar', 'Telegram и Google Календарь'],
    ['calendar', 'Только Google Календарь'],
  ];
  const CHANNEL_NOTE = 'Кнопка «В Google Календарь» открывает Google Календарь с готовым событием. beresta не получает доступ к вашему аккаунту Google. Выбор можно изменить в любой момент.';

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
  const validChannel = value => CHANNELS.some(([id]) => id === value);

  let request = null, session = 0;
  let state = null, channel = 'telegram', profile = null, shownThisLoad = false, waitTimer = 0, scheduled = false;
  let popup = null, ui = null, knowledge = null, know = null;

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
    writeDraft(Object.keys(answers).length || ui.channelTouched ? JSON.stringify({answers, channel: ui.channelTouched ? ui.channel : null}) : '');
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

  // Radio choice of the reminders channel. `onPick` returns a promise, a failure puts the old value back.
  function channelBox(idPrefix, current, onPick, hint) {
    const box = el('fieldset', '', 'profile-channel');
    box.append(el('legend', 'Как присылать напоминания', 'profile-legend'));
    const radios = [];
    for (const [value, text] of CHANNELS) {
      const label = el('label', '', 'profile-radio');
      const input = doc.createElement('input');
      input.type = 'radio'; input.name = `${idPrefix}-channel`; input.value = value; input.checked = value === current;
      label.append(input, el('span', text));
      radios.push(input); box.append(label);
    }
    let value = current;
    for (const input of radios) {
      input.addEventListener('change', async () => {
        if (!input.checked || input.value === value) return;
        const wanted = input.value;
        try { await onPick(wanted); value = wanted; } catch { for (const other of radios) other.checked = other.value === value; }
      });
    }
    if (hint !== false) box.append(el('p', CHANNEL_NOTE, 'muted profile-hint'));
    return {box, radios, set: next => { value = next; for (const input of radios) input.checked = input.value === next; }};
  }

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
  // Steps: 0 intro, 1..n one question each, n+1 the channel, n+2 the last screen.
  function renderPopup() {
    if (!popup) return;
    const body = popup.querySelector('.profile-body'), foot = popup.querySelector('.profile-foot');
    const title = popup.querySelector('#profile-title'), progress = popup.querySelector('.profile-progress');
    body.replaceChildren(); foot.replaceChildren();
    popup.querySelector('.profile-status').textContent = '';
    const n = total(), step = ui.step;
    let focus = null;
    progress.hidden = step === 0 || step > n + 1;
    if (!progress.hidden) {
      const bar = progress.querySelector('progress'), count = progress.querySelector('.profile-count-text');
      bar.max = n; bar.value = Math.min(step, n);
      count.textContent = step <= n ? `${step} из ${n}` : 'Последний шаг';
      bar.setAttribute('aria-label', 'Ход знакомства');
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
      const question = profile.questions[step - 1];
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
      area.addEventListener('keydown', event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); go(step + 1); } });
      paint();
      body.append(label, area, counter);
      const back = button('Назад', 'quiet', () => go(step - 1)); back.dataset.action = 'back';
      const skip = button('Пропустить', 'secondary', () => { ui.answers[question.id] = ''; saveDraft(); go(step + 1); }); skip.dataset.action = 'skip';
      const next = button('Далее', 'primary', () => go(step + 1)); next.dataset.action = 'next';
      foot.append(back, skip, next); focus = area;
    } else if (step === n + 1) {
      title.textContent = 'Знакомство';
      const picker = channelBox('profile', ui.channel, async value => { ui.channel = value; ui.channelTouched = true; saveDraft(); });
      body.append(picker.box);
      if (ui.empty) {
        const warn = el('div', '', 'profile-warning'); warn.setAttribute('role', 'alert');
        warn.append(el('p', 'Нужен хотя бы один ответ. Вернитесь к вопросам или откажитесь от знакомства.'));
        const again = button('Вернуться к вопросам', 'secondary', () => { ui.empty = false; go(1); }); again.dataset.action = 'to-questions';
        const no = button('Нет, спасибо', 'quiet', () => decline()); no.dataset.action = 'decline';
        warn.append(again, no); body.append(warn);
      }
      const back = button('Назад', 'quiet', () => go(n)); back.dataset.action = 'back';
      const done = button('Готово', 'primary', submit); done.dataset.action = 'done';
      done.disabled = ui.busy; back.disabled = ui.busy;
      foot.append(back, done); focus = picker.radios.find(input => input.checked) || picker.radios[0];
    } else {
      title.textContent = 'Знакомство';
      body.append(el('p', 'Спасибо! beresta разбирает ответы, это займёт до минуты.', 'profile-lead'),
        el('p', 'Результат можно посмотреть и поправить в меню, в разделе «Что beresta знает обо мне».', 'muted'));
      const close = button('Закрыть', 'primary', () => closeModal(popup)); close.dataset.action = 'close';
      foot.append(close); focus = close;
    }
    if (focus && focus.focus) focus.focus();
  }
  function go(step) {
    if (!popup || ui.busy) return;
    ui.step = Math.max(0, Math.min(total() + 1, step)); ui.confirm = false; ui.empty = false;
    renderPopup();
  }
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
    if (!Object.keys(answers).length) { ui.empty = true; renderPopup(); return; }
    const mine = session;
    busy(true); say('Отправляем…');
    try {
      await request(`${PATH}/answers`, {method: 'POST', body: JSON.stringify({answers, reminder_channel: ui.channel})});
      if (mine !== session || !popup) return;
      state = 'pending'; channel = ui.channel; ui.done = true;
      clearDraft(); ui.answers = {}; ui.channelTouched = false;
      busy(false); ui.step = total() + 2; renderPopup();
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
    closeModal(knowledge);
    const draft = readDraft(), answers = {};
    const known = new Set(profile.questions.map(question => question.id));
    const saved = draft.answers && typeof draft.answers === 'object' ? draft.answers : {};
    const old = profile.answers && typeof profile.answers === 'object' ? profile.answers : {};
    for (const id of known) {
      const text = typeof saved[id] === 'string' ? saved[id] : (Object.keys(saved).length ? '' : (typeof old[id] === 'string' ? old[id] : ''));
      if (text) answers[id] = text.slice(0, MAX_ANSWER);
    }
    ui = {step: 0, answers, manual, confirm: false, empty: false, busy: false, done: false, declined: false,
      channel: validChannel(draft.channel) ? draft.channel : channel, channelTouched: validChannel(draft.channel),
      intro: profile.intro || INTRO, privacy: profile.privacy || PRIVACY};
    popup = makeDialog('profile-dialog', 'profile-title');
    const {head, close} = heading('profile-title', 'Давайте познакомимся', requestClose);
    close.dataset.action = 'x';
    const progress = el('div', '', 'profile-progress');
    const bar = doc.createElement('progress'); bar.max = 1; bar.value = 0;
    progress.append(bar, el('span', '', 'profile-count-text'));
    popup.append(head, progress, el('div', '', 'profile-body'), statusLine(), el('div', '', 'actions profile-foot'));
    popup.addEventListener('cancel', event => { event.preventDefault(); requestClose(); });
    popup.addEventListener('keydown', event => { if (event.key === 'Escape' && !event.defaultPrevented) { event.preventDefault(); requestClose(); } });
    const mine = popup;
    popup.addEventListener('close', () => {
      mine.remove();
      if (popup === mine) { popup = null; }
      doc.dispatchEvent(new CustomEvent('beresta:profile', {detail: {open: false}}));
    });
    openModal(popup);
    doc.dispatchEvent(new CustomEvent('beresta:profile', {detail: {open: true}}));
    renderPopup();
    return true;
  }

  /* ---------- «Что beresta знает обо мне» ---------- */
  const STATE_TEXT = {
    pending: 'Разбираем ответы…',
    failed: 'Не получилось разобрать ответы',
  };
  function knowStatus(text) { if (know && know.status) know.status.textContent = text; }
  function adopt(data) {
    if (!data || typeof data !== 'object') return;
    profile = {...(profile || {}), ...data};
    if (typeof data.state === 'string') state = data.state;
    if (validChannel(data.reminder_channel)) channel = data.reminder_channel;
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
        fact.text = data && typeof data.text === 'string' ? data.text : text;
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
      await request(`${PATH}/facts/${encodeURIComponent(fact.id)}`, {method: 'DELETE'});
      if (mine !== session || !know) return;
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
      await request(PATH, {method: 'DELETE'});
      if (mine !== session || !know) return;
      state = 'declined'; profile = {...profile, state: 'declined', facts: [], answers: null};
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
      } else body.append(el('p', STATE_TEXT.pending, 'profile-lead'));
    } else if (state === 'failed') {
      body.append(el('p', STATE_TEXT.failed, 'profile-lead'), el('p', 'Ваши ответы сохранены. Попробуйте ещё раз.', 'muted'));
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
    know = {confirm: false, slow: false, poll: 0, attempts: 0, opener: opener || null, focusAfter: false};
    knowledge = makeDialog('knowledge-dialog', 'knowledge-title');
    const {head, h2} = heading('knowledge-title', 'Что beresta знает обо мне', () => closeModal(knowledge));
    know.h2 = h2;
    know.body = el('div', '', 'profile-know-body');
    know.status = statusLine();
    const channelWrap = el('div', '', 'profile-know-channel');
    knowledge.append(head, know.body, know.status, channelWrap, el('p', profile && profile.privacy || PRIVACY, 'muted profile-privacy'));
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
    const picker = channelBox('know', channel, async value => {
      knowStatus('Сохраняем…');
      try {
        await request(`${PATH}/reminder-channel`, {method: 'PATCH', body: JSON.stringify({reminder_channel: value})});
        channel = value; knowStatus('Сохранено.');
      } catch (error) { knowStatus(failure(error)); throw error; }
    });
    channelWrap.replaceChildren(picker.box);
    know.picker = picker;
    renderKnowledge();
    if (state === 'pending') poll();
    know.focusAfter = true;
    h2.focus();
    return true;
  }

  /* ---------- Entry points ---------- */
  function reset() {
    session++; stopWaiting(); scheduled = false; shownThisLoad = false; state = null; profile = null; channel = 'telegram';
    const a = popup, b = knowledge;
    popup = null; ui = null; knowledge = null;
    stopPolling(); know = null;
    for (const dialog of [a, b]) if (dialog && dialog.isConnected) { if (typeof dialog.close === 'function') dialog.close(); dialog.remove(); }
  }
  // `user` is the answer of /api/v1/auth/me, `send` is the app's api() with the CSRF header.
  async function start(user, send) {
    reset(); request = send;
    if (!user || typeof user !== 'object') return;
    state = typeof user.profile_state === 'string' ? user.profile_state : null;
    if (validChannel(user.reminder_channel)) channel = user.reminder_channel;
    if (state !== 'new') return;
    const mine = session;
    try {
      const data = await request(PATH);
      if (mine !== session) return;
      if (!data || !Array.isArray(data.questions) || !data.questions.length) return;
      adopt(data);
      // A state that changed since the sign-in (another tab) wins.
      if (state !== 'new') return;
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
    state: () => state, channel: () => channel,
  };
})();
