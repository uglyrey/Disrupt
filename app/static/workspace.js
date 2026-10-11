'use strict';
(() => {
  const get = id => document.getElementById(id);
  let lastNote = null;
  function syncNote() {
    const title = get('title').value || 'Без названия';
    if(get('note-heading-title').textContent !== title)get('note-heading-title').textContent = title;
    if (currentNote?.id !== lastNote) {lastNote = currentNote?.id; window.BerestaFocus?.closeAll();get('workspace').classList.remove('library-open');get('mobile-library-toggle').setAttribute('aria-expanded','false');}
    get('workspace').classList.toggle('has-note',!get('note-card').hidden);
    get('workspace').classList.toggle('has-source',!get('source-card').hidden);
    // This observer watches the note card subtree: write the attribute only when it changes,
    // otherwise every write queues a new mutation and the page spins forever.
    const audioHint = !(get('original-details').hidden && !get('source-card').hidden);
    if (get('original-audio-hint').hidden !== audioHint) get('original-audio-hint').hidden = audioHint;
    for (const button of get('notes').querySelectorAll('button')) {
      button.setAttribute('aria-current',String(button.dataset.noteId === currentNote?.id));
    }
  }
  new MutationObserver(syncNote).observe(get('note-card'),{attributes:true,attributeFilter:['hidden'],childList:true,subtree:true});
  get('title').addEventListener('input',()=>{get('note-heading-title').textContent=get('title').value || 'Без названия';});
  new MutationObserver(syncNote).observe(get('source-card'),{attributes:true,attributeFilter:['hidden']});
  new MutationObserver(syncNote).observe(get('notes'),{childList:true});
  get('mobile-library-toggle').onclick=()=>{const open=get('workspace').classList.toggle('library-open');get('mobile-library-toggle').setAttribute('aria-expanded',String(open));};
  get('new-note').addEventListener('click',()=>{if(get('capture-card').hidden)return;window.BerestaFocus?.closeAll();if(!get('thought').disabled)get('thought').focus();});
  // ---- Category navigation: plus button, inline create and rename, context menu, delete dialog ----
  window.categoryLongPressMs = 500;
  const list = get('category-navigation'), filter = get('category-filter');
  let editing = null, menuState = null, pendingDelete = null, pressTimer = null, pressStart = null, suppressClickUntil = 0;
  const menu = document.createElement('div');
  menu.id = 'category-menu'; menu.className = 'category-menu'; menu.setAttribute('role','menu');
  menu.setAttribute('aria-label','Действия с категорией'); menu.hidden = true; document.body.append(menu);
  const dotsIcon = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false"><path d="M5 12h.01M12 12h.01M19 12h.01" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"/></svg>';
  function chooseCategory(value, label) {
    filter.value = value; get('library-title').textContent = label;
    get('search-form').requestSubmit(); categoriesNavigation();
  }
  function categoriesNavigation() {
    if (editing) { editing.rebuild = true; return; }
    closeMenu(false);
    list.replaceChildren();
    for (const option of filter.options) {
      const item = document.createElement('div'); item.className = 'category-item';
      const button = document.createElement('button'); button.type = 'button'; button.className = 'category-button';
      const label = option.value === '' ? 'Все заметки' : option.textContent;
      button.textContent = label; button.setAttribute('aria-pressed',String(option.selected));
      button.onclick = () => {
        if (Date.now() < suppressClickUntil) { suppressClickUntil = 0; return; }
        chooseCategory(option.value,label);
      };
      item.append(button);
      if (option.value !== '' && option.value !== 'none') {
        item.dataset.categoryId = option.value;
        button.setAttribute('aria-haspopup','menu');
        const more = document.createElement('button'); more.type = 'button'; more.className = 'category-more';
        more.setAttribute('aria-haspopup','menu'); more.setAttribute('aria-expanded','false');
        more.setAttribute('aria-label',`Действия с категорией «${label}»`); more.title = 'Действия';
        more.innerHTML = dotsIcon; item.append(more);
      }
      list.append(item);
    }
  }
  new MutationObserver(categoriesNavigation).observe(filter,{childList:true});
  get('clear-search').addEventListener('click',()=>{get('library-title').textContent='Все заметки';categoriesNavigation();});
  const categoryItem = id => [...list.querySelectorAll('.category-item[data-category-id]')].find(item => item.dataset.categoryId === id);
  const categoryName = id => categories.find(category => category.id === id)?.name || '';

  // Inline editor shared by create and rename.
  function openEditor(kind, id, host, value, label) {
    finishEditing(false);
    const wrap = document.createElement('form'); wrap.className = 'category-edit'; wrap.noValidate = true;
    const input = document.createElement('input'); input.type = 'text'; input.maxLength = 100; input.value = value;
    input.setAttribute('aria-label',label); input.autocomplete = 'off'; if (kind === 'create') input.placeholder = 'Название';
    const error = document.createElement('p'); error.className = 'category-edit-error'; error.setAttribute('role','alert');
    wrap.append(input,error);
    const state = {kind, id, wrap, input, error, original: value, busy: false, rebuild: false};
    editing = state;
    if (host) host.replaceChildren(wrap); else list.append(wrap);
    wrap.onsubmit = event => { event.preventDefault(); saveEditor(state); };
    input.addEventListener('keydown',event => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); finishEditing(true); }
    });
    input.addEventListener('blur',() => {
      if (editing !== state || state.busy) return;
      const text = input.value.trim();
      if (!text || (kind === 'rename' && text === state.original)) finishEditing(false);
    });
    input.addEventListener('input',() => { error.textContent = ''; });
    wrap.scrollIntoView?.({block:'nearest'});
    input.focus(); if (kind === 'rename') input.select();
  }
  function finishEditing(restoreFocus) {
    const state = editing;
    if (!state) return;
    editing = null;
    categoriesNavigation();
    if (!restoreFocus) return;
    const target = state.kind === 'rename' ? categoryItem(state.id)?.querySelector('.category-button') : get('category-add');
    target?.focus();
  }
  async function saveEditor(state) {
    if (editing !== state || state.busy) return;
    state.busy = true; state.input.disabled = true; state.error.textContent = '';
    try {
      if (state.kind === 'create') {
        const created = await createCategory(state.input.value);
        editing = null; chooseCategory(created.id,created.name);
        categoryItem(created.id)?.querySelector('.category-button')?.focus();
      } else {
        const renamed = await renameCategory(state.id,state.input.value);
        editing = null;
        if (filter.value === state.id) get('library-title').textContent = renamed.name;
        categoriesNavigation();
        categoryItem(state.id)?.querySelector('.category-button')?.focus();
      }
    } catch (error) {
      if (editing !== state) return;
      state.error.textContent = error.message; state.input.disabled = false; state.input.focus();
    } finally { state.busy = false; }
  }
  const startCreate = () => openEditor('create',null,null,'','Название новой категории');
  const startRename = id => {
    finishEditing(false);
    const item = categoryItem(id); if (!item) return;
    openEditor('rename',id,item,categoryName(id),`Новое название категории «${categoryName(id)}»`);
  };
  get('category-add').onclick = startCreate;

  // Context menu
  function menuItems() { return [...menu.querySelectorAll('[role=menuitem]')]; }
  function closeMenu(restoreFocus) {
    if (!menuState) return;
    const {opener, more} = menuState; menuState = null;
    menu.hidden = true; menu.replaceChildren();
    more?.setAttribute('aria-expanded','false');
    if (restoreFocus && opener?.isConnected) opener.focus();
  }
  function openMenu(id, x, y, opener, more = null) {
    if (editing) return;
    closeMenu(false);
    menuState = {id, opener, more, graceUntil: 0}; more?.setAttribute('aria-expanded','true');
    for (const [action, text] of [['rename','Переименовать'],['delete','Удалить'],['create','Новая категория']]) {
      const item = document.createElement('button'); item.type = 'button'; item.setAttribute('role','menuitem');
      item.className = 'category-menu-item'; item.dataset.action = action; item.tabIndex = -1; item.textContent = text;
      menu.append(item);
    }
    menu.hidden = false;
    const width = menu.offsetWidth || 200, height = menu.offsetHeight || 120, edge = 8;
    const left = Math.max(edge,Math.min(x,window.innerWidth - width - edge));
    const top = Math.max(edge,Math.min(y,window.innerHeight - height - edge));
    menu.style.left = `${left}px`; menu.style.top = `${top}px`;
    menuItems()[0].focus();
  }
  function runMenuAction(action) {
    const state = menuState; if (!state) return;
    const {id, opener} = state;
    closeMenu(false);
    if (action === 'rename') startRename(id);
    else if (action === 'create') startCreate();
    else if (action === 'delete') openDeleteDialog(id,opener);
  }
  menu.addEventListener('click',event => {
    const item = event.target.closest('[role=menuitem]'); if (item) runMenuAction(item.dataset.action);
  });
  menu.addEventListener('keydown',event => {
    const items = menuItems(), index = items.indexOf(document.activeElement);
    if (event.key === 'ArrowDown') items[(index + 1) % items.length].focus();
    else if (event.key === 'ArrowUp') items[(index - 1 + items.length) % items.length].focus();
    else if (event.key === 'Home') items[0].focus();
    else if (event.key === 'End') items[items.length - 1].focus();
    else if (event.key === 'Escape') closeMenu(true);
    else if (event.key === 'Tab') closeMenu(true);
    else return;
    event.preventDefault(); event.stopPropagation();
  });
  // After a long press the browser may still send the lifted finger as a mouse click. Ignore it briefly.
  const outsideMenu = event => menuState && Date.now() >= menuState.graceUntil && !menu.contains(event.target) && !menuState.more?.contains(event.target);
  document.addEventListener('mousedown',event => { if (outsideMenu(event)) closeMenu(false); },true);
  document.addEventListener('pointerdown',event => { if (outsideMenu(event)) closeMenu(false); },true);
  document.addEventListener('scroll',() => closeMenu(false),true);
  window.addEventListener('resize',() => closeMenu(false));
  window.addEventListener('blur',() => closeMenu(false));
  const realItem = target => target.closest?.('.category-item[data-category-id]');
  function openForItem(item, x, y, opener, more) {
    const rect = opener.getBoundingClientRect();
    openMenu(item.dataset.categoryId, x ?? rect.left, y ?? rect.bottom, opener, more);
  }
  list.addEventListener('contextmenu',event => {
    const item = realItem(event.target); if (!item) return;
    event.preventDefault();
    const fromKeyboard = event.clientX === 0 && event.clientY === 0;
    openForItem(item, fromKeyboard ? undefined : event.clientX, fromKeyboard ? undefined : event.clientY, item.querySelector('.category-button'));
  });
  list.addEventListener('click',event => {
    const more = event.target.closest('.category-more'); if (!more) return;
    const item = realItem(more);
    if (menuState && menuState.more === more) { closeMenu(true); return; }
    openForItem(item,undefined,undefined,more,more);
  });
  list.addEventListener('keydown',event => {
    const item = realItem(event.target);
    if (!item || !event.target.matches('.category-button, .category-more')) return;
    if (event.key === 'ContextMenu' || (event.key === 'F10' && event.shiftKey)) {
      event.preventDefault(); openForItem(item,undefined,undefined,event.target);
    }
  });
  // Long press opens the same menu on touch screens.
  function cancelPress() { clearTimeout(pressTimer); pressTimer = null; pressStart = null; }
  list.addEventListener('pointerdown',event => {
    cancelPress();
    if (event.pointerType === 'touch') suppressClickUntil = 0;
    const item = realItem(event.target);
    if (!item || event.pointerType !== 'touch' || !event.target.closest('.category-button')) return;
    const start = {x: event.clientX, y: event.clientY};
    pressStart = start;
    pressTimer = setTimeout(() => {
      pressTimer = null; pressStart = null; suppressClickUntil = Date.now() + 700;
      openForItem(item,start.x,start.y,item.querySelector('.category-button'));
      if (menuState) menuState.graceUntil = Date.now() + 700;
    },window.categoryLongPressMs);
  });
  list.addEventListener('pointermove',event => {
    if (pressStart && Math.hypot(event.clientX - pressStart.x,event.clientY - pressStart.y) > 10) cancelPress();
  });
  for (const type of ['pointerup','pointercancel','pointerleave']) list.addEventListener(type,cancelPress);

  // Delete with confirmation
  const deleteDialog = get('category-delete-dialog'), deleteButton = get('category-delete-confirm');
  function openDeleteDialog(id, opener) {
    pendingDelete = id; get('category-delete-name').textContent = categoryName(id);
    openDialog('category-delete-dialog',opener); deleteButton.focus();
  }
  deleteDialog.addEventListener('close',() => { pendingDelete = null; });
  deleteButton.onclick = async () => {
    const id = pendingDelete; if (!id || deleteButton.disabled) return;
    deleteButton.disabled = true;
    try {
      const wasFiltered = await deleteCategory(id);
      deleteDialog.close();
      if (wasFiltered) get('library-title').textContent = 'Все заметки';
      categoriesNavigation(); message('Категория удалена.');
      list.querySelector('.category-button')?.focus();
    } catch (error) { message(error.message); } finally { deleteButton.disabled = false; }
  };
  function openDialog(id,opener) {
    const dialog=get(id);dialog.querySelector('.dialog-status')?.replaceChildren();dialog.showModal();dialog._opener=opener;
  }
  for(const [button,id] of [['open-telegram','telegram-dialog'],['open-feedback','feedback-dialog']])get(button).onclick=event=>openDialog(id,event.currentTarget);
  for(const button of document.querySelectorAll('[data-close-dialog]'))button.onclick=()=>button.closest('dialog').close();
  for(const dialog of document.querySelectorAll('dialog'))dialog.addEventListener('close',()=>dialog._opener?.focus());
  // The existing reminder action opens the Telegram settings; reveal its new dialog too.
  get('reminder-link').addEventListener('click',()=>{if(!get('telegram-dialog').open)openDialog('telegram-dialog',get('reminder-link'));});
  get('feedback-kind').onchange=()=>{get('feedback-bug-fields').hidden=get('feedback-kind').value!=='bug';};
  let pendingFeedback=null,feedbackSession=0;
  new MutationObserver(()=>{
    if(!get('workspace').hidden)return;
    feedbackSession++;
    for(const dialog of document.querySelectorAll('dialog[open]'))dialog.close();
    get('feedback-form').reset();get('feedback-bug-fields').hidden=false;get('feedback-status').textContent='';pendingFeedback=null;
  }).observe(get('workspace'),{attributes:true,attributeFilter:['hidden']});
  get('feedback-form').onsubmit=async event=>{
    event.preventDefault();const button=get('feedback-submit');if(button.disabled)return;
    const payload={kind:get('feedback-kind').value,subject:get('feedback-subject').value.trim(),description:get('feedback-description').value.trim(),steps:get('feedback-kind').value==='bug'?get('feedback-steps').value.trim():'',expected:get('feedback-kind').value==='bug'?get('feedback-expected').value.trim():'',contact:get('feedback-contact').value.trim()};
    if(!payload.subject || !payload.description){get('feedback-status').textContent='Заполните тему и описание.';return;}
    const session=feedbackSession;
    const serialized=JSON.stringify(payload);
    if(!pendingFeedback || pendingFeedback.serialized!==serialized)pendingFeedback={serialized,key:window.berestaId()};
    const fields=[...get('feedback-form').querySelectorAll('input,textarea,select')];fields.forEach(field=>field.disabled=true);
    button.disabled=true;get('feedback-status').textContent='Отправляем…';
    try {
      const result=await api('/api/v1/feedback',{method:'POST',headers:{'Idempotency-Key':pendingFeedback.key},body:serialized});
      if(session!==feedbackSession)return;
      get('feedback-form').reset();pendingFeedback=null;get('feedback-bug-fields').hidden=false;
      get('feedback-status').textContent=`Спасибо! Обращение ${result.id.slice(0,8)} передано команде.`;
    }catch(error){if(session!==feedbackSession)return;get('feedback-status').textContent=`${error.message} Текст сохранён в форме. Попробуйте отправить ещё раз.`;}
    finally{button.disabled=false;fields.forEach(field=>field.disabled=false);}
  };
  // On phones the three secondary actions live behind one menu button next to the category chips.
  const navMore = get('nav-more'), navMenu = get('nav-menu');
  function setNavMenu(open) {
    navMenu.classList.toggle('is-open',open); navMore.setAttribute('aria-expanded',String(open));
  }
  navMore.onclick = () => setNavMenu(!navMenu.classList.contains('is-open'));
  navMenu.addEventListener('click',event => { if (event.target.closest('button')) setNavMenu(false); });
  document.addEventListener('click',event => {
    if (navMenu.classList.contains('is-open') && !event.target.closest('.nav-row')) setNavMenu(false);
  });
  document.addEventListener('keydown',event => {
    if (event.key === 'Escape' && navMenu.classList.contains('is-open')) { setNavMenu(false); navMore.focus(); }
  });
  categoriesNavigation();syncNote();
})();
