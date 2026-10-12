'use strict';
// Telegram linking in one step. Loaded after app.js and uses its api(), message() and element().
// The code issued here lives in memory only. The request id and its end time wait in sessionStorage, so a tab
// that was sent to Telegram and came back opens the dialog again and finishes the job. The bot's deep link
// (#telegram-confirm) and the "Проверить запрос" button work as before.
(() => {
  const BOT_USERNAME = 'beresta_ru_bot'; // The only place in this file that names the bot.
  const POLL_MS = 3000, SLOW_POLL_MS = 10000, CODE_MS = 10 * 60 * 1000;
  const FLOW_KEY = 'beresta-tg-link';
  const el = id => document.getElementById(id);
  let issued = null, timer = null, busy = false, box = null, statusLine = null, openLink = null, appLink = null, socket = null;

  const botUrl = code => `https://t.me/${BOT_USERNAME}?start=${encodeURIComponent(code)}`;
  const say = text => { if (statusLine) statusLine.textContent = text; };

  // Opening the bot. The tab is reserved inside the click, before the first await, so the browser allows it.
  const onPhone = () => {
    const coarse = typeof window.matchMedia === 'function'
      && (window.matchMedia('(pointer: coarse)').matches || window.matchMedia('(max-width: 700px)').matches);
    return coarse || /Android|iPhone|iPad|iPod/i.test(navigator.userAgent || '');
  };
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
    // window.berestaNavigate is a hook for tests only. The page never defines it.
    if (typeof window.berestaNavigate === 'function') window.berestaNavigate(url); else location.href = url;
  }
  function dropTarget(target) {
    try { if (target.tab && !target.tab.closed) target.tab.close(); } catch { /* nothing to close */ }
  }

  const stored = {
    save(data) { try { sessionStorage.setItem(FLOW_KEY, JSON.stringify(data)); } catch { /* storage may be blocked */ } },
    clear() { try { sessionStorage.removeItem(FLOW_KEY); } catch { /* storage may be blocked */ } },
    load() {
      try {
        const data = JSON.parse(sessionStorage.getItem(FLOW_KEY) || 'null');
        if (data && typeof data.id === 'string' && Number.isFinite(data.expires) && data.expires > Date.now()) return data;
      } catch { /* damaged value */ }
      stored.clear();
      return null;
    },
  };

  // The socket only says that something changed for this account. The REST calls stay the source of truth.
  function closeSocket() {
    const old = socket; socket = null;
    if (!old) return;
    old.onmessage = old.onclose = old.onerror = null;
    try { old.close(); } catch { /* already closed */ }
  }
  function openSocket() {
    closeSocket();
    const mine = issued;
    if (!mine || typeof window.WebSocket !== 'function') return;
    let next;
    try { next = new window.WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/v1/telegram/links/ws`); }
    catch { return; }
    socket = next;
    next.onmessage = () => { if (socket === next && issued === mine) tick(); };
    next.onclose = () => { if (socket === next) { socket = null; if (issued === mine) schedule(); } };
    next.onerror = () => {};
  }

  // The request is finished, expired or the account left. Everything that waited for it stops.
  function forget() {
    issued = null; stopPolling(); closeSocket(); stored.clear();
  }

  function ensureBox() {
    if (box) return;
    box = document.createElement('div'); box.id = 'telegram-open-bot'; box.hidden = true;
    openLink = document.createElement('a'); openLink.className = 'button-link'; openLink.textContent = 'Открыть бота';
    openLink.target = '_blank'; openLink.rel = 'noopener';
    appLink = document.createElement('a'); appLink.className = 'button-link alt'; appLink.textContent = 'Открыть приложение Telegram';
    const hint = element('p', 'Если бот не открылся, отправьте ему это сообщение.', 'muted');
    statusLine = element('p', '', 'muted'); statusLine.id = 'telegram-status'; statusLine.setAttribute('aria-live', 'polite');
    box.append(openLink, appLink, hint, statusLine);
    el('telegram-code').before(box);
  }

  // tg://resolve opens Telegram Desktop directly.
  const appUrl = code => `tg://resolve?domain=${encodeURIComponent(BOT_USERNAME)}&start=${encodeURIComponent(code)}`;

  function showCode(code) {
    ensureBox();
    openLink.href = botUrl(code); openLink.hidden = false; box.hidden = false;
    appLink.href = appUrl(code); appLink.hidden = false;
    el('telegram-code').textContent = `/start ${code}`;
    say('Нажмите «Открыть бота» и «Запустить» в Telegram. Подключение подтвердится само.');
  }

  function clearCode() {
    el('telegram-code').textContent = '';
    if (box) box.hidden = true;
  }

  function stopPolling() { clearTimeout(timer); timer = null; }

  function renderLinks(links) {
    const holder = el('telegram-links'); holder.replaceChildren();
    if (links.identities.length) {
      holder.append(element('p', 'Telegram подключён', 'telegram-linked'));
      holder.append(element('p', `Записывать и получать напоминания можно через @${BOT_USERNAME}.`, 'muted'));
    }
    for (const link of links.pending) {
      const button = element('button', 'Подтвердить подключение Telegram', 'secondary');
      button.onclick = async () => {
        button.disabled = true;
        try { await confirmAndFinish(link.link_request_id); }
        catch (e) { message(e.message); button.disabled = false; }
      };
      holder.append(button);
    }
    if (!links.pending.length && !links.identities.length) holder.append(element('p', 'Telegram ещё не подключён.', 'muted'));
  }

  async function loadLinks() {
    const links = await api('/api/v1/telegram/links');
    renderLinks(links);
    return links;
  }

  async function confirmAndFinish(id) {
    await api(`/api/v1/telegram/links/${encodeURIComponent(id)}/confirm`, {method: 'POST'});
    forget(); clearCode();
    await loadLinks();
    message('Telegram подключён.');
  }

  function expire() {
    forget(); clearCode();
    ensureBox(); box.hidden = false; openLink.hidden = true; appLink.hidden = true;
    say('Код истёк. Нажмите «Подключить Telegram» ещё раз.');
  }

  function schedule() {
    stopPolling();
    if (issued) timer = setTimeout(tick, socket || document.hidden ? SLOW_POLL_MS : POLL_MS);
  }

  async function tick() {
    timer = null;
    const mine = issued;
    if (!mine || el('workspace').hidden) { if (mine) forget(); return; }
    if (Date.now() >= mine.expires) return expire();
    try {
      const links = await api('/api/v1/telegram/links');
      if (issued !== mine) return;
      const pending = links.pending.find(item => item.link_request_id === mine.id);
      if (pending) {
        mine.seen = true;
        if (!busy) {
          busy = true;
          try { await confirmAndFinish(pending.link_request_id); }
          finally { busy = false; }
        }
        if (issued === mine) schedule();
        return;
      }
      if (mine.seen && links.identities.length) {
        // Another tab or button confirmed it already.
        forget(); clearCode(); renderLinks(links); message('Telegram подключён.');
        return;
      }
    } catch (e) {
      if (e.status === 401) { forget(); return; }
      if (e.status === 409 || e.status === 404) { say(e.message); forget(); clearCode(); return; }
    }
    if (issued === mine) schedule();
  }

  el('link-code').textContent = 'Подключить Telegram';
  const intro = el('telegram-settings').querySelector('p');
  if (intro) intro.textContent = 'Нажмите «Подключить Telegram», затем «Открыть бота» и «Запустить» в Telegram. Подключение подтвердится само.';

  el('link-code').onclick = async () => {
    const target = reserveTarget(); // Still inside the tap, before the first await.
    try {
      const link = await api('/api/v1/telegram/link-code', {method: 'POST'});
      forget();
      issued = {id: link.link_request_id, expires: Date.now() + CODE_MS, seen: false};
      stored.save({id: issued.id, expires: issued.expires});
      showCode(link.code);
      openSocket();
      goTo(target, botUrl(link.code));
      await loadLinks();
      schedule();
    } catch (e) { dropTarget(target); message(e.message); }
  };

  el('refresh-links').onclick = async () => {
    try {
      const links = await loadLinks();
      const mine = issued && links.pending.find(item => item.link_request_id === issued.id);
      if (mine && !busy) { busy = true; try { await confirmAndFinish(mine.link_request_id); } finally { busy = false; } }
    } catch (e) { message(e.message); }
  };

  // Back from Telegram, focus, bfcache and a returning network all check at once.
  function wake() {
    if (!issued) return;
    if (!socket) openSocket();
    stopPolling(); tick();
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { if (issued) schedule(); } else wake();
  });
  window.addEventListener('pageshow', wake);
  window.addEventListener('focus', wake);
  window.addEventListener('online', wake);

  function openDialog() {
    const dialog = el('telegram-dialog');
    if (dialog.open) return;
    el('open-telegram').click();
    if (!dialog.open && dialog.showModal) dialog.showModal();
  }

  function clearHash() {
    try { history.replaceState(null, '', location.pathname + location.search); } catch { location.hash = ''; }
  }

  async function handleHash() {
    const hash = location.hash;
    if (hash !== '#telegram' && hash !== '#telegram-confirm') return;
    if (el('workspace').hidden) return; // Wait for login: the observer below calls this again.
    clearHash(); openDialog();
    try {
      const links = await loadLinks();
      if (hash === '#telegram') return;
      if (links.pending.length === 1) {
        busy = true;
        try { await confirmAndFinish(links.pending[0].link_request_id); } finally { busy = false; }
      } else if (links.pending.length > 1) {
        message('Есть несколько запросов. Выберите свой и подтвердите.');
      } else if (links.identities.length) {
        message('Telegram уже подключён.');
      } else {
        ensureBox(); box.hidden = false; openLink.hidden = true;
        say('Запрос не найден или истёк. Нажмите «Подключить Telegram» и откройте бота ещё раз.');
        message('Запрос на подключение не найден или истёк.');
      }
    } catch (e) { message(e.message); }
  }

  // The tab was reloaded or sent back from Telegram while a request was open: show it again and finish.
  async function resume() {
    if (issued || el('workspace').hidden) return;
    const saved = stored.load();
    if (!saved) return;
    issued = {id: saved.id, expires: saved.expires, seen: false};
    openDialog();
    ensureBox(); box.hidden = false; openLink.hidden = true; appLink.hidden = true;
    say('Завершаем подключение…');
    openSocket();
    try {
      const links = await loadLinks();
      if (issued && !links.pending.length && links.identities.length) { forget(); clearCode(); return; }
    } catch (e) {
      if (e.status === 401) { forget(); return; }
    }
    if (issued) tick();
  }

  new MutationObserver(() => {
    if (el('workspace').hidden) forget(); else { handleHash(); resume(); }
  }).observe(el('workspace'), {attributes: true, attributeFilter: ['hidden']});
  window.addEventListener('hashchange', handleHash);
  handleHash();
  resume();
})();
