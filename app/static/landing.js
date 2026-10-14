'use strict';
// The landing directs new accounts to email; the old password form is login-only.
(() => {
  const get = id => document.getElementById(id);
  const auth = get('auth');
  if (!auth) return;
  function showLogin() {
    auth.dataset.intent = 'login';
    get('auth-title').textContent = 'Войти в beresta';
    get('auth-lead').textContent = 'Войдите по почте или через Telegram.';
    get('legacy-login').open = true;
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    get('auth-card').scrollIntoView?.({behavior: reduce ? 'auto' : 'smooth', block: 'center'});
    get('login').focus({preventScroll: true});
  }
  get('landing-login').addEventListener('click', showLogin);
})();
