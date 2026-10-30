'use strict';
// Typewriter mode for the two text fields (#markdown, #thought). While a person types, the line with the caret
// stays near the middle of what is visible, above the on-screen keyboard too (window.visualViewport).
// The caret position comes from a hidden mirror of the field (same width, font and padding), no library.
// It reacts only to typing and caret keys, never to scrolling, so the person can still scroll away on their own.
// Loaded after md-editor.js. Pure part: BerestaTypewriter.plan. No inline styles are written as text, CSP stays strict.
(() => {
  const IDS = ['markdown', 'thought'];
  const NAV_KEYS = new Set(['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End']);
  const COPY = ['direction', 'fontStyle', 'fontVariant', 'fontWeight', 'fontStretch', 'fontSize', 'fontFamily',
    'lineHeight', 'letterSpacing', 'wordSpacing', 'textTransform', 'textIndent', 'textAlign', 'tabSize',
    'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'overflowWrap', 'wordBreak'];

  /* ---------- Pure: where should the scroll go ---------- */
  // `caretMid` and the area are in the same coordinates (the screen). Returns the new scrollTop, or null when the caret
  // sits inside the middle band (the larger of one and a half lines and 12% of the area) or nothing can move.
  function plan({caretMid, lineHeight, areaTop, areaBottom, scrollTop, maxScroll, force = false}) {
    const height = areaBottom - areaTop;
    if (!(height > 0) || !Number.isFinite(caretMid)) return null;
    const delta = caretMid - (areaTop + areaBottom) / 2;
    const band = force ? 0 : Math.max(lineHeight * 1.5, height * 0.12);
    if (Math.abs(delta) <= band) return null;
    const target = Math.min(Math.max(scrollTop + delta, 0), Math.max(maxScroll, 0));
    return Math.abs(target - scrollTop) < 1 ? null : target;
  }

  /* ---------- Caret position by mirror ---------- */
  let mirror = null, marker = null;
  function ensureMirror() {
    if (mirror && mirror.isConnected) return;
    mirror = document.createElement('div'); mirror.className = 'tw-mirror'; mirror.setAttribute('aria-hidden', 'true');
    marker = document.createElement('span'); marker.textContent = '​';
    document.body.append(mirror);
  }
  function lineHeightOf(cs) {
    const value = parseFloat(cs.lineHeight);
    return value > 0 ? value : (parseFloat(cs.fontSize) || 16) * 1.2;
  }
  // Top of the caret line and its height, in the field's own coordinates (inside the border, without inner scroll).
  function caretBox(field, position) {
    if (!field.clientWidth) return null;
    ensureMirror();
    const cs = window.getComputedStyle(field);
    for (const name of COPY) mirror.style[name] = cs[name];
    const height = lineHeightOf(cs);
    mirror.style.width = `${field.clientWidth}px`;
    marker.style.height = `${height}px`;
    mirror.replaceChildren(document.createTextNode(field.value.slice(0, position)), marker);
    const top = marker.offsetTop;
    mirror.replaceChildren();
    return {top, height};
  }

  /* ---------- Scrollers and the visible area ---------- */
  const reducedQuery = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  // The page sets html{scroll-behavior:smooth}, so «no animation» has to be asked for as «instant».
const behavior = smooth => (smooth && !(reducedQuery && reducedQuery.matches) ? 'smooth' : 'instant');
  function scrollerOf(field) {
    for (let node = field.parentElement; node && node !== document.body; node = node.parentElement) {
      const overflow = window.getComputedStyle(node).overflowY;
      if ((overflow === 'auto' || overflow === 'scroll' || overflow === 'overlay') && node.scrollHeight > node.clientHeight + 1) return node;
    }
    return document.scrollingElement || document.documentElement;
  }
  function visibleArea(scroller) {
    const view = window.visualViewport;
    let top = view ? view.offsetTop : 0;
    let bottom = top + (view ? view.height : window.innerHeight);
    if (scroller !== document.scrollingElement && scroller !== document.documentElement) {
      const box = scroller.getBoundingClientRect();
      top = Math.max(top, box.top); bottom = Math.min(bottom, box.bottom);
    }
    return {top, bottom};
  }
  function moveTo(scroller, top, smooth) {
    const options = {top, behavior: behavior(smooth)};
    if (scroller === document.scrollingElement || scroller === document.documentElement) window.scrollTo({left: window.scrollX, ...options});
    else if (typeof scroller.scrollTo === 'function') scroller.scrollTo(options);
    else scroller.scrollTop = top;
  }

  /* ---------- Centering ---------- */
  function center(field, {force = false, smooth = true} = {}) {
    if (!field || !field.isConnected || field.hidden || field.disabled) return false;
    const position = field.selectionDirection === 'backward' ? field.selectionStart : field.selectionEnd;
    const box = caretBox(field, position);
    if (!box) return false;
    // The field itself scrolls (the new-note field does): bring the line to the middle of the field first.
    let inner = field.scrollTop;
    if (field.scrollHeight > field.clientHeight + 1) {
      const target = plan({caretMid: box.top + box.height / 2 - inner, lineHeight: box.height, areaTop: 0, areaBottom: field.clientHeight,
        scrollTop: inner, maxScroll: field.scrollHeight - field.clientHeight, force});
      if (target !== null) {
        inner = target;
        if (typeof field.scrollTo === 'function') field.scrollTo({top: target, behavior: behavior(smooth)}); else field.scrollTop = target;
      }
    }
    const scroller = scrollerOf(field), area = visibleArea(scroller), rect = field.getBoundingClientRect();
    const caretMid = rect.top + field.clientTop + box.top + box.height / 2 - inner;
    const target = plan({caretMid, lineHeight: box.height, areaTop: area.top, areaBottom: area.bottom,
      scrollTop: scroller.scrollTop, maxScroll: scroller.scrollHeight - scroller.clientHeight, force});
    if (target === null) return false;
    moveTo(scroller, target, smooth);
    return true;
  }

  /* ---------- Events ---------- */
  let active = null, frame = 0, wantForce = false, touching = false, lastWheel = 0, waits = 0;
  function run() {
    frame = 0;
    const field = active;
    if (!field) return;
    // Never fight a finger or a wheel that is moving the page right now.
    if ((touching || Date.now() - lastWheel < 150) && waits < 8) { waits++; schedule(field, wantForce); return; }
    waits = 0;
    const force = wantForce; wantForce = false;
    center(field, {force, smooth: true});
  }
  function schedule(field, force = false) {
    active = field; wantForce = wantForce || force;
    if (frame) return;
    frame = typeof window.requestAnimationFrame === 'function' ? window.requestAnimationFrame(run) : window.setTimeout(run, 16);
  }
  for (const id of IDS) {
    const field = document.getElementById(id);
    if (!field) continue;
    field.addEventListener('input', () => schedule(field));
    field.addEventListener('compositionend', () => schedule(field));
    field.addEventListener('keyup', event => { if (NAV_KEYS.has(event.key)) schedule(field); });
  }
  document.addEventListener('touchstart', () => { touching = true; }, {passive: true});
  for (const type of ['touchend', 'touchcancel']) document.addEventListener(type, () => { touching = false; }, {passive: true});
  document.addEventListener('wheel', () => { lastWheel = Date.now(); }, {passive: true});
  // The keyboard opens or closes while a field has the focus: the visible area changed, the caret line may be hidden.
  if (window.visualViewport) {
    let timer = 0;
    window.visualViewport.addEventListener('resize', () => {
      const field = IDS.map(id => document.getElementById(id)).find(node => node && node === document.activeElement);
      if (!field) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(() => schedule(field), 80);
    });
  }

  window.BerestaTypewriter = {
    plan, caretBox,
    // Show the caret line in the middle at once (opening a note for editing). No band, no animation.
    reveal(field) { return center(field, {force: true, smooth: false}); },
  };
})();
