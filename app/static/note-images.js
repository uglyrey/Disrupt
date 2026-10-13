'use strict';
// Private images are separate Markdown blocks. Moving or sizing them uses the existing versioned autosave.
(() => {
  const get = id => document.getElementById(id);
  const pattern = /^!\[([^\]\r\n]{0,200})\]\((\/api\/v1\/note-images\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\?align=(left|center|right)&width=(25|50|75|100))?\)\s*$/i;
  const parse = line => {const m=pattern.exec(line);return m ? {alt:m[1],url:m[2].toLowerCase(),align:(m[3]||'center').toLowerCase(),width:Number(m[4]||100)} : null;};
  const encode = image => `![${image.alt.replace(/[\[\]\r\n]/g,' ').slice(0,200)}](${image.url}?align=${image.align}&width=${image.width})`;
  const el = (tag,text,cls) => {const node=document.createElement(tag);if(text)node.textContent=text;if(cls)node.className=cls;return node;};
  let uploading=false, dragging=null, draft=[], draftSerial=0;
  function move(text,from,target) {
    const lines=text.split('\n');
    if (!parse(lines[from]||'') || from===target) return text;
    const [line]=lines.splice(from,1);lines.splice(Math.max(0,Math.min(lines.length,target>from ? target-1 : target)),0,line);
    return lines.join('\n');
  }
  function adjacent(text,index,direction) {
    const lines=text.split('\n');let next=index+direction;
    while(next>=0 && next<lines.length && !lines[next].trim())next+=direction;
    if(next<0||next>=lines.length)return text;
    if(direction<0){while(next>0&&lines[next-1].trim()&&!parse(lines[next-1]))next--;}
    else{while(next+1<lines.length&&lines[next+1].trim()&&!parse(lines[next+1]))next++;next++;}
    return move(text,index,next);
  }
  function apply(value,fromUpload=false) {
    if(noteBusy||(uploading&&!fromUpload)||!currentNote)return;
    const field=get('markdown');
    if(!value.trim())return message('Добавьте текст перед удалением последней картинки.');
    window.BerestaMd.applyEdit(field,{value,start:field.selectionStart,end:field.selectionEnd});
    window.BerestaMd.refresh();
  }
  function change(index,url,fn) {
    const lines=get('markdown').value.split('\n'),image=parse(lines[index]||'');
    if(!image||image.url!==url)return;
    apply(fn(lines,image));
  }
  function render(image,index,editable) {
    const figure=el('figure','',`note-image ni-${image.align} ni-w${image.width}`);figure.dataset.line=String(index);
    const img=el('img');img.src=image.url;img.alt=image.alt;img.loading='lazy';img.decoding='async';img.draggable=false;figure.append(img);
    const unavailable=el('span','Картинка недоступна. Войдите в аккаунт или обновите страницу.','ni-error');unavailable.hidden=true;
    img.addEventListener('error',()=>{img.hidden=true;unavailable.hidden=false;});figure.append(unavailable);
    if(editable){
      const controls=el('div','','ni-controls');controls.setAttribute('role','group');controls.setAttribute('aria-label','Расположение картинки');
      const button=(label,action)=>{const b=el('button',label,'quiet');b.type='button';b.addEventListener('click',event=>{event.stopPropagation();action();});b.disabled=Boolean(noteBusy||uploading);controls.append(b);return b;};
      button('Выше',()=>change(index,image.url,()=>adjacent(get('markdown').value,index,-1)));
      button('Ниже',()=>change(index,image.url,()=>adjacent(get('markdown').value,index,1)));
      const align=el('select');align.setAttribute('aria-label','Выравнивание картинки');
      for(const [value,label] of [['left','Слева'],['center','По центру'],['right','Справа']]){const option=el('option',label);option.value=value;align.append(option);}align.value=image.align;
      const width=el('select');width.setAttribute('aria-label','Ширина картинки');
      for(const value of [25,50,75,100]){const option=el('option',value+'%');option.value=String(value);width.append(option);}width.value=String(image.width);
      for(const [select,name] of [[align,'align'],[width,'width']]){select.disabled=Boolean(noteBusy||uploading);select.addEventListener('change',()=>change(index,image.url,(lines,item)=>{item[name]=name==='width'?Number(select.value):select.value;lines[index]=encode(item);return lines.join('\n');}));controls.append(select);}
      button('Убрать',()=>{if(confirm('Убрать картинку из текста? Она останется в истории заметки.'))change(index,image.url,lines=>{lines.splice(index,1);return lines.join('\n');});});
      figure.append(controls);figure.draggable=true;
      figure.addEventListener('dragstart',event=>{if(noteBusy||uploading){event.preventDefault();return;}dragging={index,url:image.url,note:currentNote.id};event.dataTransfer.setData('text/plain','beresta-image');event.dataTransfer.effectAllowed='move';});
      figure.addEventListener('dragend',()=>{dragging=null;});
    }
    return figure;
  }
  function insert(text,position,line) {
    const at=Math.max(0,Math.min(position,text.length));
    // Split at the caret so the image always occupies its own block, also in a paragraph.
    return text.slice(0,at).replace(/\n*$/,'')+'\n\n'+line+'\n\n'+text.slice(at).replace(/^\n*/,'');
  }
  async function upload(file) {
    if(uploading||noteBusy||!currentNote)return;
    if(file.size>5*1024*1024)return message('Картинка должна быть не больше 5 МБ.');
    const note=currentNote.id,account=epoch,field=get('markdown');
    const position=window.BerestaMd.mode()==='edit'?field.selectionStart:field.value.length;
    if(field.value.length>23800)return message('В заметке почти 24 000 знаков. Сократите текст перед добавлением картинки.');
    if(window.BerestaFocus.pending())await window.BerestaFocus.flush();
    if(noteBusy||currentNote?.id!==note||epoch!==account)return;
    let ownsLock=true;
    uploading=true;setNoteBusy(true);message('Загружаем картинку…');
    const form=new FormData();form.append('image',file);const key=window.berestaId();
    try{
      let uploaded;
      for(let attempt=0;attempt<2;attempt++){
        try{uploaded=await api(`/api/v1/notes/${note}/images`,{method:'POST',body:form,headers:{'Idempotency-Key':key}});break;}
        catch(error){if(attempt||error.status&&error.status<500)throw error;}
      }
      if(epoch!==account||currentNote?.id!==note)return;
      const value=insert(field.value,position,encode({url:uploaded.url,alt:'Изображение',align:'center',width:100}));
      if(value.length>24000)return message('Картинка загружена, но в тексте уже 24 000 знаков. Сократите заметку.');
      setNoteBusy(false);ownsLock=false;apply(value,true);window.BerestaMd.setMode('read');
      await window.BerestaFocus.flush();
      if(epoch!==account||currentNote?.id!==note)return;
      if(typeof currentNote.markdown==='string'&&currentNote.markdown!==field.value)return message('Картинка загружена, но текст пока не сохранён. Проверьте сообщение рядом с названием заметки.');
      message('Картинка добавлена. Под ней можно изменить расположение и ширину.');
    }catch(error){if(epoch===account)message(error.message||'Не удалось загрузить картинку. Попробуйте ещё раз.');}
    finally{uploading=false;if(ownsLock&&epoch===account)setNoteBusy(false);document.dispatchEvent(new Event('beresta:note-idle'));}
  }
  const gallery=el('div','','draft-images');gallery.id='capture-images';gallery.hidden=true;get('thought').after(gallery);
  const sourceGallery=el('div','','source-images');get('source-original').after(sourceGallery);
  function syncDraft() {
    gallery.replaceChildren();gallery.hidden=!draft.length;get('thought').required=!draft.length;
    for(const item of draft){
      const box=el('div','','draft-image'),img=el('img');img.src=item.url;img.alt='Картинка в новой записи';
      const remove=el('button','×','quiet');remove.type='button';remove.setAttribute('aria-label','Убрать картинку');remove.disabled=captureBusy;
      remove.onclick=()=>{if(captureBusy)return;URL.revokeObjectURL(item.url);draft=draft.filter(other=>other!==item);draftSerial++;syncDraft();};box.append(img,remove);gallery.append(box);
    }
    const help=el('p','Картинки сохранятся вместе с записью. ИИ обрабатывает только текст.','muted');gallery.append(help);
    document.dispatchEvent(new Event('beresta:images-draft'));
  }
  function addDraft(files) {
    if(captureBusy||recorder||microphonePending)return message('Сначала завершите текущую запись.');
    const incoming=Array.from(files);
    if(incoming.some(file=>!/^image\/(png|jpeg|webp)$/.test(file.type)||file.size>5*1024*1024))return message('Нужны картинки JPG, PNG или WebP до 5 МБ.');
    if(draft.length+incoming.length>20)return message('В запись помещается до 20 картинок.');
    if([...draft.map(item=>item.file),...incoming].reduce((sum,file)=>sum+file.size,0)>10*1024*1024)return message('Все картинки вместе должны быть не больше 10 МБ.');
    for(const file of incoming)draft.push({file,url:URL.createObjectURL(file)});
    draftSerial++;syncDraft();window.BerestaFocus?.closeAll();message('Картинка добавлена в новую запись. Нажмите «Сохранить».');
  }
  function clearDraft() {for(const item of draft)URL.revokeObjectURL(item.url);draft=[];draftSerial++;syncDraft();}
  async function restoreDraft(images) {
    if(captureBusy||draft.length)return false;
    const account=epoch,files=[];setCaptureBusy(true);
    try{
      for(const image of images){
        if(!/^\/api\/v1\/note-images\/[0-9a-f-]{36}$/.test(image.url))throw new Error('Не удалось прочитать картинку из исходника.');
        const response=await fetch(image.url,{credentials:'same-origin'});
        if(!response.ok)throw new Error('Картинка из исходника недоступна. Обновите страницу и попробуйте ещё раз.');
        const blob=await response.blob();files.push(new File([blob],'source.webp',{type:'image/webp'}));
      }
      if(epoch!==account)return false;
      if(files.length>20||files.reduce((sum,file)=>sum+file.size,0)>10*1024*1024)throw new Error('Картинки исходника занимают больше 10 МБ. Откройте исходник и перенесите их частями.');
    }finally{if(epoch===account)setCaptureBusy(false);}
    if(epoch!==account)return false;
    addDraft(files);return draft.length===files.length;
  }
  async function receive(files,isDraft) {
    if(isDraft)return addDraft(files);
    const note=currentNote?.id,account=epoch;
    for(const file of files){if(epoch!==account||currentNote?.id!==note)break;await upload(file);}
  }
  async function paste(isDraft) {
    const account=epoch,note=currentNote?.id;
    try{
      if(!navigator.clipboard?.read)throw new Error('clipboard-unavailable');
      const items=await navigator.clipboard.read(),files=[];
      for(const item of items){const type=item.types.find(type=>/^image\/(png|jpeg|webp)$/.test(type));if(type){const blob=await item.getType(type);files.push(new File([blob],'clipboard.png',{type}));}}
      if(epoch!==account||(!isDraft&&currentNote?.id!==note))return;
      if(!files.length)return message('В буфере нет картинки. Скопируйте изображение и попробуйте ещё раз.');
      await receive(files,isDraft);
    }catch(_){message('Вставьте картинку через Ctrl+V или ⌘V в поле записи. На телефоне выберите файл.');window.BerestaFocus?.closeAll();(isDraft?get('thought'):get('markdown')).focus();}
  }
  function imageTool(isDraft) {
    const key=isDraft?'capture-images':'note-images',tool=el('div','','tool');tool.dataset.tool=key;
    const add=el('button','','tool-btn quiet');add.type='button';add.id=isDraft?'capture-image-add':'note-image-add';add.dataset.toolBtn=key;add.dataset.tip='Картинка';add.setAttribute('aria-label','Добавить картинку');add.setAttribute('aria-haspopup','dialog');add.setAttribute('aria-expanded','false');add.setAttribute('aria-controls','pop-'+key);
    add.innerHTML='<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false"><rect x="3" y="3" width="18" height="18" rx="3" fill="none" stroke="currentColor" stroke-width="1.7"/><circle cx="8" cy="8" r="1.5" fill="currentColor"/><path d="m4 17 5-5 4 4 3-3 4 4" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    const pop=el('div','','tool-pop');pop.id='pop-'+key;pop.hidden=true;pop.tabIndex=-1;pop.setAttribute('role','dialog');pop.setAttribute('aria-label','Добавить картинку');
    const head=el('div','','pop-head'),close=el('button','×','pop-close quiet');close.type='button';close.setAttribute('aria-label','Закрыть окно');head.append(el('span','Картинка','pop-title'),close);
    const body=el('div','','pop-body'),file=el('input');file.type='file';file.id=isDraft?'capture-image-file':'note-image-file';file.accept='image/jpeg,image/png,image/webp';file.multiple=isDraft;file.hidden=true;
    const choose=el('button','Выбрать файл','secondary'),clipboard=el('button','Вставить из буфера','quiet');choose.type=clipboard.type='button';
    choose.onclick=()=>{if(isDraft?captureBusy:noteBusy||uploading)return;file.click();};clipboard.onclick=()=>paste(isDraft);
    file.onchange=()=>{const files=Array.from(file.files||[]);file.value='';if(files.length){window.BerestaFocus?.closeAll();receive(files,isDraft).catch(error=>message(error.message));}};
    body.append(choose,clipboard,file,el('p','JPG, PNG или WebP до 5 МБ. Можно вставить через Ctrl+V или ⌘V.','muted'));pop.append(head,body);tool.append(add,pop);
    (isDraft?get('capture-card').querySelector('.tools'):get('note-tools')).prepend(tool);
  }
  imageTool(true);imageTool(false);
  for(const [field,isDraft] of [[get('thought'),true],[get('markdown'),false],[get('preview'),false]])field.addEventListener('paste',event=>{
    const files=Array.from(event.clipboardData?.items||[]).filter(item=>item.kind==='file'&&item.type.startsWith('image/')).map(item=>item.getAsFile()).filter(Boolean);
    if(!files.length)return;event.preventDefault();receive(files,isDraft).catch(error=>message(error.message));
  });
  function source(images) {sourceGallery.replaceChildren();for(const image of images||[])if(/^\/api\/v1\/note-images\/[0-9a-f-]{36}$/.test(image.url)){const img=el('img');img.src=image.url;img.alt='Картинка из исходной записи';img.loading='lazy';sourceGallery.append(img);}}
  document.addEventListener('beresta:capture-busy',syncDraft);
  const preview=get('preview');
  // renderNote builds fresh controls while its request is locked. Restore their state when that request ends.
  document.addEventListener('beresta:note-idle',()=>{
    for(const control of preview.querySelectorAll('.ni-controls button,.ni-controls select'))control.disabled=Boolean(noteBusy||uploading);
  });
  preview.addEventListener('dragover',event=>{if(dragging&&dragging.note===currentNote?.id&&!noteBusy){event.preventDefault();event.dataTransfer.dropEffect='move';}});
  preview.addEventListener('drop',event=>{
    if(!dragging||dragging.note!==currentNote?.id||noteBusy)return;event.preventDefault();
    const target=event.target.closest('[data-line]'),index=target?Number(target.dataset.line):get('markdown').value.split('\n').length;
    change(dragging.index,dragging.url,()=>move(get('markdown').value,dragging.index,index));dragging=null;
  });
  window.BerestaImages={parse,encode,move,adjacent,insert,render,upload,addDraft,clearDraft,restoreDraft,source,
    hasDraft:()=>draft.length>0,draftKey:()=>draftSerial,
    captureBody:(text,mode)=>{const form=new FormData();form.append('text',text);form.append('processing_mode',mode);for(const item of draft)form.append('images',item.file);return form;}};
})();
