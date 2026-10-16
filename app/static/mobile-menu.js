'use strict';
// Phone menus. On a narrow screen the header sections and the secondary links sit behind one button on the left
// («Меню»), and the tools of the open note sit behind one button on the right («Меню заметки»).
// The tool buttons and their popovers stay where they are (focus.js owns them). On a phone CSS only turns the
// tool row into a drop-down list, so choosing an item opens the same popover as before.
// Loaded after focus.js. Other scripts use window.BerestaMenus.
(() => {
  const get = id => document.getElementById(id);
  const menuButton = get('menu-button'), general = get('general-menu'), scrim = get('menu-scrim'),
    noteButton = get('note-menu-button'), noteTools = get('note-tools'), noteCard = get('note-card'), nav = get('main-nav');
  if (!menuButton || !general || !noteButton || !noteTools) return;

  const isPhone = () => (typeof window.matchMedia === 'function' ? window.matchMedia('(max-width:640px)').matches : false);
  const visible = el => Boolean(el) && !el.hidden && !el.closest('[hidden]');
  let generalOpen = false, noteOpen = false;

  /* ---------- Scrim: an outside tap only closes the menu ---------- */
  function showScrim(kind) {
    scrim.hidden = false; scrim.dataset.for = kind;
  }
  function hideScrim() { scrim.hidden = true; scrim.removeAttribute('data-for'); }

  function announce(name, opened) {
    document.dispatchEvent(new CustomEvent('beresta:menu', {detail: {menu: name, open: opened}}));
  }

  /* ---------- General menu on the left ---------- */
  // The menu repeats the header sections. The current one is marked the same way as in the header.
  function syncCurrent() {
    for (const item of general.querySelectorAll('[data-proxy^="nav-"]')) {
      const source = get(item.dataset.proxy);
      if (source && source.hasAttribute('aria-current')) item.setAttribute('aria-current', 'page');
      else item.removeAttribute('aria-current');
    }
  }
  function placeGeneral() {
    const box = menuButton.getBoundingClientRect();
    general.style.top = `${Math.round(box.bottom + 6)}px`;
  }
  function openGeneral({focus = true} = {}) {
    if (generalOpen || !isPhone() || menuButton.hidden) return false;
    closeNote({restore: false});
    generalOpen = true;
    syncCurrent(); placeGeneral();
    general.hidden = false; menuButton.setAttribute('aria-expanded', 'true'); showScrim('general');
    if (focus) {
      const first = general.querySelector('.menu-item');
      if (first) first.focus();
    }
    announce('general', true);
    return true;
  }
  function closeGeneral({restore = true} = {}) {
    if (!generalOpen) return false;
    generalOpen = false;
    general.hidden = true; menuButton.setAttribute('aria-expanded', 'false'); hideScrim();
    if (restore) menuButton.focus();
    announce('general', false);
    return true;
  }
  menuButton.addEventListener('click', () => { if (generalOpen) closeGeneral(); else openGeneral(); });
  general.addEventListener('click', event => {
    const item = event.target.closest('.menu-item');
    if (!item) return;
    const proxy = item.dataset.proxy ? get(item.dataset.proxy) : null;
    // The dialogs that the original buttons open give the focus back to the original, which is hidden on a phone.
    // The menu button takes it instead.
    const dialogs = item.dataset.proxy && /^open-(telegram|feedback|delete|trash|timezone|password|profile)$/.test(item.dataset.proxy);
    closeGeneral({restore: !dialogs && !proxy});
    if (proxy) {
      proxy.click();
      const dialog = dialogs ? document.querySelector('dialog[open]') : null;
      if (dialog) dialog.addEventListener('close', () => { if (!generalOpen) menuButton.focus(); }, {once: true});
    }
  });
  // The button shows only to a person who is in, the same as the header sections.
  function syncButton() {
    const shown = Boolean(nav) && !nav.hidden;
    if (menuButton.hidden === shown) menuButton.hidden = !shown;
    if (!shown) closeGeneral({restore: false});
  }
  if (nav) { new MutationObserver(syncButton).observe(nav, {attributes: true, attributeFilter: ['hidden']}); syncButton(); }
  // The same address for the bot everywhere. The header link is the one source.
  const botLink = get('bot-link');
  if (botLink && botLink.href) for (const link of document.querySelectorAll('[data-bot-link]')) link.href = botLink.href;

  /* ---------- Note menu on the right ---------- */
  // Every tool gets a visible name. On a desktop CSS hides it, the icon bar stays as it was.
  for (const button of noteTools.querySelectorAll('.tool-btn')) {
    if (button.querySelector('.tool-label')) continue;
    const label = document.createElement('span');
    label.className = 'tool-label'; label.textContent = button.dataset.tip || button.getAttribute('aria-label') || '';
    button.prepend(label);
  }
  const noteItems = () => [...noteTools.querySelectorAll('.tool-btn, #note-menu-extra button, #note-menu-extra a')]
    .filter(el => visible(el) && !el.disabled);
  function openNote({focus = true} = {}) {
    if (noteOpen || !isPhone() || !visible(noteCard)) return false;
    closeGeneral({restore: false});
    if (window.BerestaFocus) window.BerestaFocus.closeAll();
    noteOpen = true;
    noteTools.classList.add('is-open'); noteButton.setAttribute('aria-expanded', 'true'); showScrim('note');
    if (focus) {
      const first = noteItems()[0];
      if (first) first.focus();
    }
    announce('note', true);
    return true;
  }
  function closeNote({restore = true} = {}) {
    if (!noteOpen) return false;
    noteOpen = false;
    noteTools.classList.remove('is-open'); noteButton.setAttribute('aria-expanded', 'false'); hideScrim();
    if (restore) noteButton.focus();
    announce('note', false);
    return true;
  }
  noteButton.addEventListener('click', () => { if (noteOpen) closeNote(); else openNote(); });
  // Choosing a tool closes the list. The popover that the tool opens takes the focus.
  noteTools.addEventListener('click', event => {
    if (noteOpen && event.target.closest('.tool-btn, #note-menu-extra button, #note-menu-extra a')) closeNote({restore: false});
  });
  noteTools.addEventListener('keydown', event => {
    if (!noteOpen) return;
    const items = noteItems(), index = items.indexOf(document.activeElement);
    if (!items.length) return;
    if (event.key === 'ArrowDown') items[(index + 1) % items.length].focus();
    else if (event.key === 'ArrowUp') items[(index - 1 + items.length) % items.length].focus();
    else if (event.key === 'Home') items[0].focus();
    else if (event.key === 'End') items[items.length - 1].focus();
    else return;
    event.preventDefault();
  });

  /* ---------- Shared: Esc, outside tap, resize, other screens ---------- */
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    if (generalOpen) { event.preventDefault(); closeGeneral(); } else if (noteOpen) { event.preventDefault(); closeNote(); }
  });
  scrim.addEventListener('click', () => { closeGeneral({restore: false}); closeNote({restore: false}); });
  // The tip bubbles of guide.js sit above the page and must not close a menu they describe.
  document.addEventListener('click', event => {
    if (!generalOpen && !noteOpen) return;
    const target = event.target;
    if (!target.closest || target.closest('.guide-bubble')) return;
    if (generalOpen && !general.contains(target) && !menuButton.contains(target)) closeGeneral({restore: false});
    if (noteOpen && !noteTools.contains(target) && !noteButton.contains(target)) closeNote({restore: false});
  });
  function leavePhone() {
    if (isPhone()) return;
    closeGeneral({restore: false}); closeNote({restore: false});
  }
  window.addEventListener('resize', () => { leavePhone(); if (generalOpen) placeGeneral(); });
  const query = typeof window.matchMedia === 'function' ? window.matchMedia('(max-width:640px)') : null;
  if (query && typeof query.addEventListener === 'function') query.addEventListener('change', leavePhone);
  // Another note, or no note: the list goes away.
  document.addEventListener('beresta:note-rendered', () => closeNote({restore: false}));
  new MutationObserver(() => { if (noteCard.hidden) closeNote({restore: false}); })
    .observe(noteCard, {attributes: true, attributeFilter: ['hidden']});
  // A popover that opens by itself (a reminder form, for example) closes the list.
  new MutationObserver(() => { if (document.body.classList.contains('has-pop')) closeNote({restore: false}); })
    .observe(document.body, {attributes: true, attributeFilter: ['class']});

  window.BerestaMenus = {
    isPhone, openGeneral, closeGeneral, openNote, closeNote,
    generalOpen: () => generalOpen, noteOpen: () => noteOpen,
    // focus.js asks for this when a popover closes and its own button is not on screen.
    phoneNote: () => isPhone() && visible(noteCard),
    focusNoteButton: () => noteButton.focus(),
  };
})();
