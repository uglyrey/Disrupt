'use strict';
// Sign-in through Telegram, password recovery, privacy consent and account deletion.
// Loaded after app.js. It uses api(), authMessage(), message() and enterApp() from there.
// The one-time start token lives in the Telegram link only. The page keeps the login id and its end time
// in sessionStorage (strictly needed to finish the sign-in after a return from Telegram), nothing else.
(() => {
  const POLL_MS = 2000, SLOW_POLL_MS = 10000, GRACE_MS = 2000;
  const FLOW_KEY = 'beresta-tg-flow';
  const el = id => document.getElementById(id);
  const botLink = url => typeof url === 'string' && url.startsWith('https://t.me/');
  const send = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body || {})});
  const consent = () => el('accept-policy');
  const say = (id, text) => { el(id).textContent = text; };

  function info(text = '') {
    const line = el('auth-info');
    line.textContent = text; line.hidden = !text;
  }

  function clearHash() {
    try { history.replaceState(history.state, '', location.pathname + location.search); } catch { location.hash = ''; }
  }

  function openDialog(id) {
    const dialog = el(id);
    if (!dialog.open) dialog.showModal();
    return dialog;
  }

  // Opening Telegram. The tab is reserved inside the click, before any await, because a browser lets
  // a page open a window only straight from a tap. The address is set when the server has answered.

  const onPhone = () => {
    const coarse = typeof window.matchMedia === 'function'
      && (window.matchMedia('(pointer: coarse)').matches || window.matchMedia('(max-width: 700px)').matches);
    return coarse || /Android|iPhone|iPad|iPod/i.test(navigator.userAgent || '');
  };

  function navigate(url) {
    // window.berestaNavigate is a hook for tests only. The page never defines it.
    if (typeof window.berestaNavigate === 'function') window.berestaNavigate(url); else location.href = url;
  }

  function reserveTarget() {
    if (onPhone()) return {tab: null};
    let tab = null;
    try { tab = window.open('', '_blank'); } catch { tab = null; }
    if (tab) { try { tab.opener = null; } catch { /* a foreign window refuses it */ } }
    return {tab};
  }

  function goTo(target, url) {
    const tab = target.tab;
    if (tab && !tab.closed) {
      try { tab.location.href = url; return; } catch { /* fall back to this tab */ }
    }
    navigate(url);
  }

  function dropTarget(target) {
    try { if (target.tab && !target.tab.closed) target.tab.close(); } catch { /* nothing to close */ }
  }

  // tg://resolve opens the Telegram Desktop app directly. The parts come from the https link.
  function appLink(deepLink) {
    try {
      const url = new URL(deepLink);
      return `tg://resolve?domain=${encodeURIComponent(url.pathname.replace(/^\//, ''))}`
        + `&start=${encodeURIComponent(url.searchParams.get('start') || '')}`;
    } catch { return ''; }
  }

  function showLinks(linkId, appId, deepLink) {
    el(linkId).href = deepLink; el(linkId).hidden = false;
    const app = appLink(deepLink);
    el(appId).href = app || 'tg://resolve'; el(appId).hidden = !app;
  }

  // A sign-in or deletion that waits for the bot survives a reload of this tab.

  const savedFlow = {
    save(data) { try { sessionStorage.setItem(FLOW_KEY, JSON.stringify(data)); } catch { /* storage may be blocked */ } },
    clear() { try { sessionStorage.removeItem(FLOW_KEY); } catch { /* storage may be blocked */ } },
    load() {
      try {
        const data = JSON.parse(sessionStorage.getItem(FLOW_KEY) || 'null');
        if (data && typeof data.id === 'string' && ['login', 'delete'].includes(data.purpose)
          && Number.isFinite(data.expires) && data.expires > Date.now()) return data;
      } catch { /* damaged value */ }
      savedFlow.clear();
      return null;
    },
  };

  const socketUrl = path => `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${path}`;

  // One watcher per flow. check(job) returns true once the flow has finished by itself.
  // The WebSocket tells at once that the bot answered. Polling stays as the safety net: quick without a
  // socket, slow with one, and it runs again whenever the tab comes back.
  function makeWatcher(check, socketPath) {
    let job = null, timer = null, socket = null, running = false, rerun = false;
    const stopTimer = () => { clearTimeout(timer); timer = null; };
    const closeSocket = () => {
      const old = socket; socket = null;
      if (!old) return;
      old.onmessage = old.onclose = old.onerror = null;
      try { old.close(); } catch { /* already closed */ }
    };
    const schedule = () => {
      stopTimer();
      if (job) timer = setTimeout(tick, socket || document.hidden ? SLOW_POLL_MS : POLL_MS);
    };
    function openSocket(mine) {
      closeSocket();
      if (typeof window.WebSocket !== 'function') return;
      let next;
      try { next = new window.WebSocket(socketUrl(socketPath(mine))); } catch { return; }
      socket = next;
      next.onmessage = event => {
        if (socket !== next || job !== mine) return;
        let data = null;
        try { data = JSON.parse(event.data); } catch { return; }
        if (data && data.status && data.status !== 'pending') tick();
      };
      next.onclose = () => {
        if (socket !== next) return;
        socket = null;
        if (job === mine) schedule();
      };
      next.onerror = () => {};
    }
    async function tick() {
      if (running) { rerun = true; return; }
      rerun = false; stopTimer();
      const mine = job;
      if (!mine) return;
      if (Date.now() >= mine.expires + GRACE_MS) { job = null; closeSocket(); mine.onExpired(); return; }
      running = true;
      let finished = false;
      try { finished = await check(mine); }
      catch (e) {
        if (job !== mine) return;
        if ([401, 403, 404, 409].includes(e.status)) { job = null; closeSocket(); mine.onFail(e); return; }
      } finally { running = false; }
      if (job !== mine) return;
      if (finished) { job = null; closeSocket(); return; }
      if (rerun) return tick();
      schedule();
    }
    const wake = () => {
      if (!job) return;
      if (!socket) openSocket(job);
      tick();
    };
    document.addEventListener('visibilitychange', () => { if (document.hidden) schedule(); else wake(); });
    window.addEventListener('pageshow', wake);
    window.addEventListener('focus', wake);
    window.addEventListener('online', wake);
    return {
      start(next) { job = next; openSocket(next); schedule(); },
      stop() { job = null; stopTimer(); closeSocket(); },
    };
  }

  // Sign in through the bot

  const loginButton = el('tg-login'), loginBox = el('tg-login-box');
  const loginPoller = makeWatcher(async job => {
    const result = await api(`/api/v1/auth/telegram/status/${encodeURIComponent(job.id)}`);
    if (result.status === 'expired') { job.onExpired(); return true; }
    if (result.status !== 'ok') return false;
    resetLogin();
    try {
      await enterApp(result);
      message(result.new_account ? 'Аккаунт создан. Добро пожаловать!' : '');
    } catch (e) { message(e.message); }
    return true;
  }, job => `/api/v1/auth/telegram/ws/${encodeURIComponent(job.id)}`);

  function resetLogin() {
    loginPoller.stop(); savedFlow.clear();
    loginBox.hidden = true; loginButton.disabled = false; say('tg-login-status', '');
  }

  function watchLogin(start, quiet) {
    loginPoller.start({
      id: start.login_id,
      expires: Date.parse(start.expires_at),
      onExpired() { resetLogin(); if (!quiet || el('workspace').hidden) authMessage('Время вышло. Нажмите «Войти через Telegram» ещё раз.'); },
      onFail(e) { resetLogin(); if (!quiet || el('workspace').hidden) authMessage(e.message); },
    });
  }

  loginButton.onclick = async () => {
    const box = consent();
    if (!box.checked) {
      authMessage('Чтобы войти через Telegram, дайте согласие на обработку персональных данных.');
      box.focus(); return;
    }
    authMessage(); info(); resetLogin();
    loginButton.disabled = true;
    const target = reserveTarget(); // Still inside the tap, before the first await.
    try {
      const start = await send('/api/v1/auth/telegram/start', {
        accept_policy: true, policy_version: box.dataset.policyVersion,
        marketing_opt_in: el('marketing-consent').checked,
        ...(document.body.dataset.sourceToken ? {source_token:document.body.dataset.sourceToken} : {}),
      });
      if (!botLink(start.deep_link)) throw new Error('Не удалось начать вход. Попробуйте ещё раз.');
      showLinks('tg-login-link', 'tg-login-app', start.deep_link);
      el('tg-login-hint').hidden = false;
      loginBox.hidden = false;
      say('tg-login-status', 'Ждём подтверждения в Telegram…');
      savedFlow.save({id: start.login_id, expires: Date.parse(start.expires_at), purpose: 'login'});
      watchLogin(start, false);
      goTo(target, start.deep_link);
    } catch (e) {
      dropTarget(target);
      loginButton.disabled = false;
      authMessage(e.message);
    }
  };

  el('tg-login-cancel').onclick = () => { resetLogin(); authMessage(); };

  // The tab was reloaded or sent back from Telegram while a sign-in was open.
  function resumeLogin() {
    const saved = savedFlow.load();
    if (!saved || saved.purpose !== 'login') return;
    loginButton.disabled = true;
    el('tg-login-link').hidden = true; el('tg-login-app').hidden = true; el('tg-login-hint').hidden = true;
    loginBox.hidden = false;
    say('tg-login-status', 'Завершаем вход…');
    watchLogin({login_id: saved.id, expires_at: new Date(saved.expires).toISOString()}, true);
  }

  // Forgot password

  const forgot = el('forgot-password');
  forgot.onclick = async () => {
    const username = el('login').value.trim();
    if (!username) {
      authMessage('Введите имя пользователя выше. Ссылка придёт в Telegram.');
      el('login').focus(); return;
    }
    authMessage(); info();
    forgot.disabled = true;
    try {
      const answer = await send('/api/v1/auth/recovery/telegram', {username});
      info(answer.message);
    } catch (e) { authMessage(e.message); }
    finally { forgot.disabled = false; }
  };

  // New password from the link in the bot message. The token leaves the address bar at once.

  let resetToken = null;
  const resetForm = el('reset-form');

  function openReset() {
    const found = /^#reset=([A-Za-z0-9_-]{20,200})$/.exec(location.hash);
    if (!found) return;
    resetToken = found[1];
    clearHash();
    say('reset-status', '');
    openDialog('reset-dialog');
    el('reset-password').focus();
  }

  resetForm.onsubmit = async event => {
    event.preventDefault();
    const first = el('reset-password').value, second = el('reset-password2').value;
    if ([...first].length < 10 || [...first].length > 128) return say('reset-status', 'Пароль должен содержать от 10 до 128 символов.');
    if (first !== second) return say('reset-status', 'Пароли не совпадают.');
    if (!resetToken) return say('reset-status', 'Ссылка устарела. Запросите новую.');
    const button = el('reset-submit');
    button.disabled = true;
    try {
      await send('/api/v1/auth/password-reset', {token: resetToken, password: first});
      resetToken = null;
      el('reset-password').value = ''; el('reset-password2').value = '';
      el('reset-dialog').close();
      // The server ended every session of this account, so a signed-in page must start over.
      if (!el('workspace').hidden) { location.reload(); return; }
      authMessage(); info('Пароль изменён. Войдите с новым паролем.');
    } catch (e) { say('reset-status', e.message); }
    finally { button.disabled = false; }
  };

  window.addEventListener('hashchange', openReset);
  openReset();

  // Privacy policy: older accounts accept the current version once.

  el('policy-accept').onclick = async () => {
    const button = el('policy-accept');
    button.disabled = true;
    try {
      await send('/api/v1/account/accept-policy', {policy_version: consent().dataset.policyVersion});
      el('policy-banner').hidden = true;
    } catch (e) { message(e.message); }
    finally { button.disabled = false; }
  };

  // Optional messages. The checkbox is never inferred from policy acceptance.
  el('open-marketing').onclick = async () => {
    say('marketing-status', 'Загружаем настройку…');
    openDialog('marketing-dialog');
    try {
      const state = await api('/api/v1/account/marketing-consent');
      el('marketing-enabled').checked = state.enabled === true;
      say('marketing-status', '');
    } catch (error) { say('marketing-status', error.message); }
  };
  el('marketing-close').onclick = () => el('marketing-dialog').close();
  el('marketing-form').onsubmit = async event => {
    event.preventDefault();
    const button = el('marketing-save');
    button.disabled = true;
    try {
      const state = await api('/api/v1/account/marketing-consent', {
        method:'PUT', body:JSON.stringify({enabled:el('marketing-enabled').checked}),
      });
      say('marketing-status', state.enabled ? 'Согласие сохранено.' : 'Согласие отозвано.');
    } catch (error) { say('marketing-status', error.message); }
    finally { button.disabled = false; }
  };

  // Account deletion. The operator removes the data; the page only files the request.

  const deletePoller = makeWatcher(async job => {
    const result = await send(`/api/v1/account/delete-request/telegram/${encodeURIComponent(job.id)}`);
    if (result.status === 'expired') { job.onExpired(); return true; }
    if (result.status !== 'requested') return false;
    finishDeletion();
    return true;
  }, job => `/api/v1/account/delete-request/telegram/ws/${encodeURIComponent(job.id)}`);

  function finishDeletion() {
    deletePoller.stop(); savedFlow.clear();
    say('delete-status', 'Запрос принят. Мы вышли из аккаунта.');
    setTimeout(() => location.reload(), 1500);
  }

  function resetDelete() {
    deletePoller.stop(); savedFlow.clear();
    el('delete-telegram-box').hidden = true; el('delete-telegram-start').disabled = false;
    el('delete-password').value = ''; say('delete-status', '');
  }

  el('open-delete').onclick = () => { resetDelete(); openDialog('delete-dialog'); };
  el('delete-dialog').addEventListener('close', resetDelete);

  el('delete-form').onsubmit = async event => {
    event.preventDefault();
    const password = el('delete-password').value;
    if (!password) return say('delete-status', 'Введите пароль или подтвердите в Telegram.');
    const button = el('delete-submit');
    button.disabled = true;
    try {
      await send('/api/v1/account/delete-request', {password});
      finishDeletion();
    } catch (e) { say('delete-status', e.message); }
    finally { button.disabled = false; }
  };

  function watchDelete(start) {
    deletePoller.start({
      id: start.login_id,
      expires: Date.parse(start.expires_at),
      onExpired() { resetDelete(); say('delete-status', 'Время вышло. Начните ещё раз.'); },
      onFail(e) { resetDelete(); say('delete-status', e.message); },
    });
  }

  el('delete-telegram-start').onclick = async () => {
    const button = el('delete-telegram-start');
    button.disabled = true; say('delete-status', '');
    const target = reserveTarget(); // Still inside the tap, before the first await.
    try {
      const start = await send('/api/v1/account/delete-request/telegram');
      if (!botLink(start.deep_link)) throw new Error('Не удалось начать подтверждение. Попробуйте ещё раз.');
      showLinks('delete-telegram-link', 'delete-telegram-app', start.deep_link);
      el('delete-telegram-box').hidden = false;
      say('delete-status', 'Откройте Telegram, нажмите «Запустить», затем «Да, это я». Ждём подтверждения…');
      savedFlow.save({id: start.login_id, expires: Date.parse(start.expires_at), purpose: 'delete'});
      watchDelete(start);
      goTo(target, start.deep_link);
    } catch (e) {
      dropTarget(target);
      button.disabled = false;
      say('delete-status', e.message);
    }
  };

  // The tab came back from Telegram while a deletion confirmation was open. The dialog opens again.
  function resumeDelete() {
    const saved = savedFlow.load();
    if (!saved || saved.purpose !== 'delete') return;
    if (el('workspace').hidden) return; // The observer below calls this again after the sign-in.
    openDialog('delete-dialog');
    el('delete-telegram-start').disabled = true;
    el('delete-telegram-link').hidden = true; el('delete-telegram-app').hidden = true;
    el('delete-telegram-box').hidden = false;
    say('delete-status', 'Завершаем подтверждение…');
    watchDelete({login_id: saved.id, expires_at: new Date(saved.expires).toISOString()});
  }

  el('deletion-cancel').onclick = async () => {
    const button = el('deletion-cancel');
    button.disabled = true;
    try {
      await send('/api/v1/account/delete-request/cancel');
      el('deletion-banner').hidden = true;
      message('Запрос на удаление отменён.');
    } catch (e) { message(e.message); }
    finally { button.disabled = false; }
  };

  resumeLogin();
  resumeDelete();
  new MutationObserver(() => { if (!el('workspace').hidden) resumeDelete(); })
    .observe(el('workspace'), {attributes: true, attributeFilter: ['hidden']});
})();
