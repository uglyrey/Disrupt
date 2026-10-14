'use strict';
// Browser history stores only screen identifiers, never note contents, passwords or session tokens.
(() => {
  const get = id => document.getElementById(id);
  const views = ['overview','assistant','tasks','search'];
  const uuid = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
  const idRoute = new RegExp(`^#(note|source)=(${uuid})$`,'i');
  let current = null, ready = false, serial = 0, running = false, queued = false, timer = null;
  const owner = () => get('username').textContent;
  const visible = () => !get('workspace').hidden;
  const same = (a,b) => a?.kind === b?.kind && a?.id === b?.id && (a?.reminder || null) === (b?.reminder || null);
  function parse() {
    const name = location.hash.slice(1);
    if (views.includes(name)) return {kind:name};
    if (name === 'notes') return {kind:'library'};
    if (name === 'new') return {kind:'new'};
    if (name === 'trash') return {kind:'trash'};
    const found = idRoute.exec(location.hash);
    if (found) return {kind:found[1].toLowerCase(),id:found[2].toLowerCase(),reminder:new URLSearchParams(location.search).get('reminder')};
    return {kind:'home'};
  }
  function url(route) {
    const target = new URL(location.href);
    target.searchParams.delete('capture'); target.searchParams.delete('reminder');
    if (route.kind === 'note' && route.reminder) target.searchParams.set('reminder',route.reminder);
    target.hash = route.kind === 'note' || route.kind === 'source' ? `${route.kind}=${route.id}`
      : route.kind === 'library' ? 'notes' : route.kind === 'home' ? '' : route.kind;
    return target.pathname + target.search + target.hash;
  }
  function state(route, index, group) {
    return {berestaNavigation:1,route,index,group,owner:owner()};
  }
  function ours(value) {
    return value?.berestaNavigation === 1 && Number.isSafeInteger(value.index) && typeof value.group === 'string'
      && value.route && ['home','new','library',...views,'note','source','trash'].includes(value.route.kind);
  }
  function write(route, replace = false) {
    const next = state(route,replace ? current.index : current.index + 1,current.group);
    history[replace ? 'replaceState' : 'pushState'](next,'',url(route)); current = next;
  }
  function start() {
    if (ready || !visible()) return;
    const original = history.state, initial = parse();
    if (ours(original) && original.owner === owner() && same(original.route,initial)) current = original;
    else {
      current = state({kind:'home'},ours(original) ? original.index : 0,ours(original) ? original.group : window.berestaId());
      // Keep one-time password recovery and email-verification fragments until auth consumes them.
      history.replaceState(current,'',/^(#reset=|#verify-email=)/.test(location.hash) ? location.href : url(current.route));
      if (views.includes(initial.kind) || ['note','source','library','new','trash'].includes(initial.kind)) write(initial);
    }
    ready = true;
  }
  function record(route) {
    if (!visible()) return;
    start(); serial++;
    if (!same(current.route,route)) write(route);
  }
  function closeTransient() {
    window.BerestaFocus?.closeAll();
    window.BerestaMenus?.closeGeneral({restore:false}); window.BerestaMenus?.closeNote({restore:false});
    for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
  }
  function library(open) {
    get('workspace').classList.toggle('library-open',open);
    get('mobile-library-toggle').setAttribute('aria-expanded',String(open));
  }
  async function show(route) {
    if (!visible()) return false;
    if (route.kind === 'trash') {
      const parent = route.behind && route.behind.kind !== 'trash' ? route.behind : {kind:'home'};
      if (!await show(parent)) return false;
      window.BerestaTrash?.open({navigation:false});
      return Boolean(document.querySelector('.trash-dialog[open]'));
    }
    const matches = route.kind === 'note' ? currentNote?.id === route.id && !get('note-card').hidden
      : route.kind === 'source' ? currentCapture?.capture_id === route.id && !get('source-card').hidden
      : views.includes(route.kind) ? window.BerestaBrain?.current() === route.kind && !get(`${route.kind}-card`).hidden
      : route.kind === 'library' ? get('workspace').classList.contains('library-open') : !get('capture-card').hidden;
    if (same(current?.route,route) && matches) return true;  // Undoing a refused Back keeps the dialog and draft too.
    // Merely closing the library drawer must not replace a pending edit of that same note.
    if (route.kind === 'note' && currentNote?.id === route.id && !get('note-card').hidden) {
      window.BerestaBrain?.hide(); library(false); closeTransient(); return true;
    }
    if (route.kind === 'source' && currentCapture?.capture_id === route.id && !get('source-card').hidden) {
      window.BerestaBrain?.hide(); library(false); closeTransient(); return true;
    }
    if (recorder || microphonePending) { message('Сначала завершите запись голоса.'); return false; }
    if (captureBusy) { message('Дождитесь сохранения записи.'); return false; }
    if (window.BerestaFocus?.pending()) await window.BerestaFocus.flush();
    if (noteBusy) { message('Дождитесь сохранения.'); return false; }
    viewGeneration++;  // Stop background auto-display only after navigation is allowed to proceed.
    if (views.includes(route.kind)) {
      const result = window.BerestaBrain?.show(route.kind);
      if (result) { library(false); closeTransient(); }
      return Boolean(result);
    }
    if (route.kind === 'note') {
      await openNote(route.id,{navigation:false,userAction:true,reminder:route.reminder});
      if (currentNote?.id !== route.id || get('note-card').hidden) return false;
    } else if (route.kind === 'source') {
      await openCapture(route.id,{navigation:false,userAction:true,standalone:true});
      if (currentCapture?.capture_id !== route.id || get('source-card').hidden) return false;
    } else {
      if (!leaveNote('Есть несохранённые правки. Вернуться к предыдущему экрану?')) return false;
      window.BerestaBrain?.hide(); get('capture-card').hidden = false;
    }
    library(route.kind === 'library'); closeTransient(); return true;
  }
  function undo(target) {
    if (ours(target) && target.group === current.group && target.index !== current.index) history.go(current.index-target.index);
    else history.pushState(current,'',url(current.route));
  }
  async function drain() {
    if (running || !visible() || !ready) return;
    running = true;
    try {
      while (queued && visible()) {
        queued = false;
        const ticket = serial, account = epoch, original = history.state, parsed = parse();
        const trusted = ours(original) && same(original.route,parsed);
        // Legacy/hash-only entries have no reliable position. Give them another group rather than guessing Back direction.
        let target = trusted ? original : state(parsed,0,window.berestaId());
        if (target.owner !== owner()) target = state({kind:'home'},target.index,target.group);
        let accepted = false;
        try { accepted = await show(target.route); }
        catch (error) { if (ticket === serial && account === epoch) message(error.message); }
        if (ticket !== serial || account !== epoch || !visible()) continue;
        if (accepted) { current = target; history.replaceState(target,'',url(target.route)); }
        else if (same(current.route,target.route) && current.index === target.index) {
          // An unavailable bookmarked note/source starts on the library, rather than creating a failed-entry loop.
          current = state({kind:'home'},target.index,target.group);
          history.replaceState(current,'',url(current.route)); await show(current.route);
        } else undo(trusted ? target : null);
      }
    } finally { running = false; }
  }
  function schedule() {
    if (location.hash.startsWith('#verify-email=')) return;
    if (visible() && running) viewGeneration++;  // Cancel a superseded history read, without invalidating a pending save.
    serial++; queued = true; clearTimeout(timer); timer = setTimeout(drain,0);
  }
  async function restoreInitial() {
    if (ready || !visible()) return;
    const original = history.state, target = parse();
    if (ours(original) && original.owner !== owner()) {
      current = state({kind:'home'},original.index,original.group); ready = true;
      history.replaceState(current,'',url(current.route)); return;
    }
    start();
    // A root Telegram link already opened its capture in enterApp. Its record hook initialized history.
    if (target.kind !== 'home' && !same(current.route,target)) return;
    if (target.kind !== 'home') {
      queued = true; serial++; await drain();
    }
  }
  function consumeReminderLink() {
    if (ready) history.replaceState(current,'',url(current.route));
    else if (location.hash === '#remind') history.replaceState(history.state,'',location.pathname + location.search);
  }
  function trashOpened() {
    if (!visible() || current?.route.kind === 'trash') return;
    start(); record({kind:'trash',behind:current.route});
  }
  function trashClosed() { if (!running && ready && current?.route.kind === 'trash' && location.hash === '#trash') history.back(); }
  window.BerestaNavigation = {record,restoreInitial,consumeReminderLink,trashOpened,trashClosed};
  window.addEventListener('popstate',schedule); window.addEventListener('hashchange',schedule);
  get('mobile-library-toggle').addEventListener('click',() => {
    if (get('workspace').classList.contains('library-open')) record({kind:'library'});
    else record(currentNote && !get('note-card').hidden ? {kind:'note',id:currentNote.id} : {kind:'home'});
  });
  get('category-navigation').addEventListener('click',event => {
    if (visible() && event.target.closest('.category-button')) record({kind:'library'});
  });
  new MutationObserver(() => {
    if (!visible()) { serial++; ready = false; current = null; queued = false; clearTimeout(timer); }
  }).observe(get('workspace'),{attributes:true,attributeFilter:['hidden']});
})();
