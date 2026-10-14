'use strict';
// The sign-in window: two main buttons, the email code steps, the old login and the recovery request.
// Telegram sign-in itself lives in auth-extra.js. This file uses api(), authMessage(), message() and enterApp() from app.js.
// The code lives in the field and in memory only. The server keeps the binding cookie.
(() => {
  const RESEND_SECONDS = 60;
  const el = id => document.getElementById(id);
  const card = el('auth-card');
  if (!card) return;
  const send = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body)});
  const copy = {
    main: ['Войти в beresta', 'Аккаунт создастся сам, если его ещё нет.'],
    password: ['Вход по логину и паролю', 'Для аккаунтов, которые вы создали с логином и паролем.'],
    email: ['Вход по почте', 'Пароль не нужен. Мы пришлём код.'],
    code: ['Введите код', 'Письмо может прийти через минуту.'],
    consent: ['Создать аккаунт', 'Осталось подтвердить два пункта.'],
  };
  const forms = {email: 'email-form', code: 'code-form', consent: 'consent-form'};
  let stage = 'main', address = '', code = '', busy = false, timer = null, resendAt = 0;

  function info(text = '') {
    const line = el('auth-info');
    line.textContent = text; line.hidden = !text;
  }

  function show(next, {focus = true} = {}) {
    if (!copy[next]) return;
    stage = next;
    card.dataset.stage = next;
    el('auth-title').textContent = copy[next][0];
    el('auth-lead').textContent = copy[next][1];
    const inForm = next === 'main' || next === 'password';
    el('auth-form').hidden = !inForm;
    el('signin-main').hidden = next !== 'main';
    el('signin-password').hidden = next !== 'password';
    el('signin-links').hidden = next !== 'main';
    for (const [name, id] of Object.entries(forms)) el(id).hidden = name !== next;
    authMessage(); if (next !== 'main') info();
    if (!focus) return;
    const target = {
      main: el('signin-email').disabled ? el('tg-login') : el('signin-email'), password: el('login'),
      email: el('email-address'), code: el('email-code'), consent: el('email-age'),
    }[next];
    target.focus({preventScroll: true});
  }

  function lock(value) {
    busy = value;
    for (const id of ['email-send', 'code-submit', 'consent-submit']) el(id).disabled = value;
    if (!value) renderResend();
  }

  // The resend button waits a minute after each letter, the same wait the server asks for.

  function renderResend() {
    const left = Math.max(0, Math.ceil((resendAt - Date.now()) / 1000));
    const button = el('code-resend');
    button.disabled = busy || left > 0;
    button.textContent = left > 0 ? `Отправить ещё раз через ${left} с` : 'Отправить ещё раз';
    return left;
  }

  function tick() {
    timer = null;
    if (renderResend() > 0) timer = setTimeout(tick, 1000);
  }

  function startWait(seconds) {
    resendAt = Date.now() + seconds * 1000;
    clearTimeout(timer);
    tick();
  }

  async function ask(again) {
    if (busy) return;
    lock(true); authMessage();
    try {
      const answer = await send('/api/v1/auth/email/code/start', {email: address});
      startWait(Number.isFinite(answer.resend_after) ? answer.resend_after : RESEND_SECONDS);
      el('code-lead').textContent = `Мы отправили код на ${address}. Он действует 10 минут.`;
      el('email-code').value = ''; code = '';
      show('code');
      if (again) info('Код отправлен ещё раз.');
    } catch (e) { authMessage(e.message); }
    finally { lock(false); }
  }

  el('signin-email').onclick = () => {
    if (el('signin-email').disabled) return;
    show('email');
  };

  el('email-form').onsubmit = async event => {
    event.preventDefault();
    const value = el('email-address').value.trim().toLowerCase();
    if (!/^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$/.test(value) || value.length > 254) {
      authMessage('Проверьте адрес почты.'); el('email-address').focus(); return;
    }
    address = value;
    await ask(false);
  };
  el('email-back').onclick = () => show('main');
  el('code-change').onclick = () => { clearTimeout(timer); show('email'); };
  el('code-resend').onclick = () => { if (renderResend() === 0) ask(true); };

  const digits = value => value.replace(/\D/g, '').slice(0, 6);
  const codeField = el('email-code');
  codeField.addEventListener('input', () => {
    const clean = digits(codeField.value);
    if (clean !== codeField.value) codeField.value = clean;
  });
  // maxlength would cut «123 456» before the spaces are removed, so a paste is cleaned first.
  codeField.addEventListener('paste', event => {
    const text = event.clipboardData && event.clipboardData.getData('text');
    if (typeof text !== 'string') return;
    event.preventDefault();
    codeField.value = digits(text);
  });

  function consentBody() {
    const policy = el('accept-policy');
    return {accept_policy: true, policy_version: policy.dataset.policyVersion, confirm_age: true};
  }

  async function confirm(extra) {
    if (busy) return;
    lock(true); authMessage();
    try {
      const user = await send('/api/v1/auth/email/code/confirm', {code, ...extra});
      reset();
      await enterApp(user); message();
    } catch (e) {
      if (e.status === 409 && e.data && e.data.code === 'consent_required') {
        el('email-age').checked = el('email-consent').checked = false;
        show('consent');
      } else {
        if (e.status === 400 && stage === 'consent') show('code', {focus: false});
        authMessage(e.message);
      }
    } finally { lock(false); }
  }

  el('code-form').onsubmit = event => {
    event.preventDefault();
    code = digits(codeField.value);
    if (code.length !== 6) { authMessage('Введите шесть цифр из письма.'); codeField.focus(); return; }
    // Boxes ticked on the main step already count, so a new person is not asked twice.
    const ticked = el('accept-policy').checked && el('confirm-age').checked;
    return confirm(ticked ? consentBody() : {});
  };

  el('consent-form').onsubmit = event => {
    event.preventDefault();
    if (!el('email-age').checked) { authMessage('Чтобы создать аккаунт, подтвердите, что вам исполнилось 18 лет.'); el('email-age').focus(); return; }
    if (!el('email-consent').checked) { authMessage('Чтобы создать аккаунт, дайте согласие на обработку персональных данных.'); el('email-consent').focus(); return; }
    return confirm(consentBody());
  };
  el('consent-back').onclick = () => show('code');

  function reset() {
    clearTimeout(timer); timer = null; code = ''; resendAt = 0;
    codeField.value = ''; el('email-age').checked = el('email-consent').checked = false;
    show('main', {focus: false});
  }

  // The old login with a name and a password stays as a small link.

  el('show-password-login').onclick = () => show('password');
  el('password-back').onclick = () => show('main');

  // «Восстановить аккаунт» writes to support. The person does not need to be signed in.

  const recovery = el('recovery-dialog');
  const status = text => { el('recovery-status').textContent = text; };
  function openRecovery() {
    status('');
    el('recovery-submit').disabled = false;
    if (!el('recovery-email').value) el('recovery-email').value = address || el('email-address').value.trim();
    if (!recovery.open) recovery.showModal();
    el('recovery-email').focus();
  }
  el('open-recovery').onclick = openRecovery;
  recovery.addEventListener('close', () => { el('recovery-text').value = ''; el('recovery-consent').checked = false; status(''); });
  el('recovery-form').onsubmit = async event => {
    event.preventDefault();
    const email = el('recovery-email').value.trim(), text = el('recovery-text').value.trim();
    if (!/^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$/.test(email)) { status('Проверьте адрес почты, на него мы ответим.'); el('recovery-email').focus(); return; }
    if (!text) { status('Опишите, что случилось.'); el('recovery-text').focus(); return; }
    if ([...text].length > 2000) { status('Текст длиннее 2000 символов. Сократите его.'); el('recovery-text').focus(); return; }
    if (!el('recovery-consent').checked) { status('Нужно согласие на обработку этого запроса.'); el('recovery-consent').focus(); return; }
    const button = el('recovery-submit');
    button.disabled = true; status('');
    try {
      const answer = await send('/api/v1/support/recovery', {email, text, accept_processing: true});
      el('recovery-text').value = '';
      status(answer.message || 'Запрос отправлен. Мы напишем вам на почту');
    } catch (e) { status(e.message); button.disabled = false; }
  };

  // Email sign-in can be off until the owner connects mail. Telegram stays.
  api('/api/v1/auth/email/options').then(options => {
    if (options.code_login_enabled === true) return;
    el('signin-email').disabled = true;
    el('signin-email-note').hidden = false;
  }).catch(() => {}); // Without an answer the server decides when the code is requested.

  window.BerestaSignin = {show, stage: () => stage};
  show('main', {focus: false});
})();
