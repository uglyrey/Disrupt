'use strict';
// Landing for signed-out visitors. The sign-in window is always on the page, so the «Войти» buttons only
// take the visitor to it (the hero one shows where the window sits below the text). signin.js owns the steps.
(() => {
  const get = id => document.getElementById(id);
  const auth = get('auth'), card = get('auth-card');
  if (!auth || !card) return;
  const visible = node => !node.hidden && !node.closest('[hidden]');
  function goToWindow() {
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    card.scrollIntoView?.({behavior: reduce ? 'auto' : 'smooth', block: 'center'});
    const target = [...card.querySelectorAll('input:not([type=checkbox]), button')]
      .find(node => visible(node) && !node.disabled && !node.classList.contains('text-link'));
    (target || card).focus({preventScroll: true});
  }
  for (const id of ['landing-login', 'landing-login-bottom']) get(id)?.addEventListener('click', goToWindow);
})();
