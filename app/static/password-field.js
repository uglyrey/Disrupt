'use strict';
// An eye button inside every password field, on the right: it shows or hides the password.
// Fields that appear later (dialogs, forms built by scripts) are found by the observer.
// The button never takes focus away from the field and never submits the form.
(() => {
  const SHOW = 'Показать пароль', HIDE = 'Скрыть пароль';
  const NS = 'http://www.w3.org/2000/svg';
  const EYE = ['M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z', 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z'];
  const EYE_OFF = ['M3 3l18 18', 'M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4', 'M6.6 6.7C3.7 8.5 2 12 2 12s3.6 7 10 7a9.7 9.7 0 0 0 4-.9', 'M9.9 9.9a3 3 0 0 0 4.2 4.2'];

  function icon(paths) {
    const svg = document.createElementNS(NS, 'svg');
    for (const [key, value] of Object.entries({viewBox: '0 0 24 24', width: '20', height: '20', 'aria-hidden': 'true', focusable: 'false'})) svg.setAttribute(key, value);
    for (const d of paths) {
      const path = document.createElementNS(NS, 'path');
      for (const [key, value] of Object.entries({d, fill: 'none', stroke: 'currentColor', 'stroke-width': '1.7', 'stroke-linecap': 'round', 'stroke-linejoin': 'round'})) path.setAttribute(key, value);
      svg.append(path);
    }
    return svg;
  }

  function render(button, shown) {
    button.setAttribute('aria-label', shown ? HIDE : SHOW);
    button.setAttribute('aria-pressed', String(shown));
    button.title = shown ? HIDE : SHOW;
    button.replaceChildren(icon(shown ? EYE_OFF : EYE));
  }

  function hideAgain(input) {
    const button = input.parentNode && input.parentNode.querySelector('.password-toggle');
    if (input.dataset.passwordShown === 'true') input.type = 'password';
    delete input.dataset.passwordShown;
    if (button) render(button, false);
  }

  function enhance(input) {
    if (input.dataset.passwordField) return;
    input.dataset.passwordField = 'on';
    const wrap = document.createElement('span');
    wrap.className = 'password-field';
    input.parentNode.insertBefore(wrap, input);
    wrap.append(input);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'password-toggle';
    render(button, false);
    // The press must not move focus out of the field, so the caret stays where it was.
    button.addEventListener('mousedown', event => event.preventDefault());
    button.addEventListener('click', event => {
      event.preventDefault();
      const shown = input.type === 'password';
      let from = null, to = null;
      try { from = input.selectionStart; to = input.selectionEnd; } catch { /* some types have no selection */ }
      input.type = shown ? 'text' : 'password';
      if (shown) input.dataset.passwordShown = 'true'; else delete input.dataset.passwordShown;
      render(button, shown);
      input.focus({preventScroll: true});
      // Chromium re-creates the inner editor after a type change and resets the caret on the next frame.
      const restore = () => {
        if (from === null || document.activeElement !== input) return;
        try { input.setSelectionRange(from, to); } catch { /* the caret stays where the browser put it */ }
      };
      restore();
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(restore);
    });
    wrap.append(button);
  }

  function scan(root = document) {
    for (const input of root.querySelectorAll('input[type=password]')) enhance(input);
  }

  // A shown password is hidden again when the form goes away or is sent.
  document.addEventListener('submit', event => {
    for (const input of event.target.querySelectorAll?.('input[data-password-shown]') || []) hideAgain(input);
  }, true);
  document.addEventListener('close', event => {
    for (const input of event.target.querySelectorAll?.('input[data-password-shown]') || []) hideAgain(input);
  }, true);

  scan();
  new MutationObserver(records => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (node.nodeType !== 1) continue;
        if (node.matches('input[type=password]')) enhance(node); else scan(node);
      }
    }
  }).observe(document.documentElement, {childList: true, subtree: true});
  window.BerestaPasswordFields = {scan};
})();
