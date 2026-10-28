'use strict';
// Optional web analytics. Only fixed event names and scrubbed URLs leave this module.
(() => {
  const COUNTER = 113851319;
  const PUBLIC = new Set(['/','/privacy','/terms','/cookies','/data-deletion']);
  if (location.hostname !== 'app.berestaapp.ru' || !PUBLIC.has(location.pathname)) return;
  let active = false, initialized = false, loaded = false, script = null, lastPage = null;
  const waiting = [], seen = new Set();
  const allowed = () => window.berestaConsent?.allowsAnalytics?.() === true;
  const call = (...args) => { try { window.ym?.(COUNTER,...args); } catch { /* analytics never blocks the app */ } };

  function pageUrl() {
    const url = new URL(location.origin + location.pathname);
    // Campaign labels only. Never account tokens, email, search text, capture or reminder IDs.
    const query = new URLSearchParams(location.search);
    for (const key of ['utm_source','utm_medium','utm_campaign','utm_content','utm_term','yclid','gclid']) {
      const value = query.get(key);
      if (value && /^[A-Za-z0-9_.~-]{1,160}$/.test(value)) url.searchParams.set(key,value);
    }
    if (location.pathname === '/') {
      const hash = location.hash;
      if (/^#(overview|assistant|tasks|search|notes|new|trash)$/.test(hash)) url.hash = hash;
      else if (/^#note=/.test(hash)) url.hash = 'note';
      else if (/^#source=/.test(hash)) url.hash = 'source';
      else if (/^#reset=/.test(hash)) url.hash = 'account-recovery';
      else if (/^#verify-email=/.test(hash)) url.hash = 'email-verification';
    }
    return url.href;
  }
  function referrer() {
    try { const url = new URL(document.referrer); return /^https?:$/.test(url.protocol) ? url.origin + '/' : ''; }
    catch { return ''; }
  }
  function page() {
    if (!active || !initialized || !allowed()) return;
    const url = pageUrl();
    if (url === lastPage) return;
    call('hit',url,{title:'beresta',referer:lastPage || referrer()});
    lastPage = url;
  }
  function initialize() {
    if (!active || !loaded || initialized || !allowed()) return;
    initialized = true;
    call('init',{defer:true,clickmap:false,trackLinks:false,trackHash:false,webvisor:false,
      ecommerce:false,sendTitle:false,disableYtm:true,accurateTrackBounce:false,
      url:pageUrl(),referrer:referrer()});
    page();
    for (const name of waiting.splice(0)) call('reachGoal',name);
  }
  function purge() {
    // Only this origin's Yandex storage, never session or Beresta preference cookies.
    try {
      for (const part of document.cookie.split(';')) {
        const name = part.split('=')[0].trim();
        if (!/^_+ym/.test(name)) continue;
        for (const domain of ['',`; domain=${location.hostname}`,'; domain=berestaapp.ru']) {
          document.cookie = `${name}=; Max-Age=0; path=/; Secure; SameSite=Lax${domain}`;
        }
      }
      for (const store of [window.localStorage,window.sessionStorage]) {
        for (const key of Object.keys(store)) if (/^_+ym/.test(key)) store.removeItem(key);
      }
    } catch { /* blocked storage does not prevent opting out */ }
  }
  function sync() {
    if (!allowed()) {
      active = false; waiting.length = 0; seen.clear(); lastPage = null;
      if (initialized) call('destruct');
      initialized = false; purge(); return;
    }
    active = true;
    if (loaded) { initialize(); return; }
    if (script) return;
    window.ym = window.ym || function() { (window.ym.a = window.ym.a || []).push(arguments); };
    window.ym.l = Date.now();
    script = document.createElement('script');
    script.async = true; script.referrerPolicy = 'no-referrer';
    script.src = `https://mc.yandex.ru/metrika/tag.js?id=${COUNTER}`;
    script.onload = () => { loaded = true; initialize(); };
    script.onerror = () => { script.remove(); script = null; waiting.length = 0; };
    document.head.append(script);
  }
  function goal(name, identifier) {
    if (!active || !allowed()) return;
    // IDs are used only in memory to suppress repeated successful API responses.
    const key = identifier ? `${name}:${identifier}` : name;
    if (seen.has(key)) return;
    if (seen.size >= 500) seen.clear();
    seen.add(key);
    if (initialized) call('reachGoal',name);
    else if (waiting.length < 100) waiting.push(name);
  }
  function success(path, method, data) {
    if (!active || !allowed() || !data || typeof data !== 'object') return;
    if (method === 'POST' && ['/api/v1/captures/text','/api/v1/captures/audio'].includes(path) && data.capture_id) {
      goal('capture_saved',data.capture_id);
    } else if (method === 'GET' && /^\/api\/v1\/jobs\/[^/]+$/.test(path) && data.status === 'succeeded') {
      goal('ai_completed',data.id);
    } else if (method === 'POST' && ['/api/v1/auth/login','/api/v1/auth/email/login','/api/v1/auth/email/code/confirm'].includes(path) && data.username) {
      goal('login_completed');
    } else if (method === 'GET' && /^\/api\/v1\/auth\/telegram\/status\/[^/]+$/.test(path) && data.status === 'ok') {
      goal('login_completed');
    }
  }
  window.BerestaMetrika = {success};
  document.addEventListener('beresta:cookie-consent',sync);
  document.addEventListener('beresta:navigation',page);
  window.addEventListener('popstate',page); window.addEventListener('hashchange',page);
  sync();
})();
