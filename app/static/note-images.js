'use strict';
// Private images are separate Markdown blocks. Moving or sizing them uses the existing versioned autosave.
(() => {
  const get = id => document.getElementById(id);
  const pattern = /^!\[([^\]\r\n]{0,200})\]\((\/api\/v1\/note-images\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\?align=(left|center|right)&width=(25|50|75|100))?\)\s*$/i;
  const parse = line => {const m=pattern.exec(line);return m ? {alt:m[1],url:m[2].toLowerCase(),align:(m[3]||'center').toLowerCase(),width:Number(m[4]||100)} : null;};
  const encode = image => `![${image.alt.replace(/[\[\]\r\n]/g,' ').slice(0,200)}](${image.url}?align=${image.align}&width=${image.width})`;
  const el = (tag,text,cls) => {const node=document.createElement(tag);if(text)node.textContent=text;if(cls)node.className=cls;return node;};
  let uploading=false, dragging=null;
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
  function apply(value) {
    if(noteBusy||uploading||!currentNote)return;
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
    if(window.BerestaFocus.pending())await window.BerestaFocus.flush();
    if(noteBusy||currentNote?.id!==note||epoch!==account)return;
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
      uploading=false;setNoteBusy(false);apply(value);window.BerestaMd.setMode('read');
      await window.BerestaFocus.flush();message('Картинка добавлена. Под ней можно изменить расположение и ширину.');
    }catch(error){if(epoch===account)message(error.message||'Не удалось загрузить картинку. Попробуйте ещё раз.');}
    finally{uploading=false;if(epoch===account)setNoteBusy(false);}
  }
  const file=el('input');file.type='file';file.id='note-image-file';file.accept='image/jpeg,image/png,image/webp';file.hidden=true;
  file.addEventListener('change',()=>{const selected=file.files?.[0];file.value='';if(selected)upload(selected).catch(error=>message(error.message));});
  const add=el('button','Добавить картинку','quiet note-image-add');add.type='button';add.id='note-image-add';
  add.addEventListener('mousedown',event=>event.preventDefault());add.addEventListener('click',()=>{if(!noteBusy&&!uploading)file.click();});
  const row=el('div','','note-image-add-row');row.append(add,file);get('preview').before(row);
  const preview=get('preview');
  preview.addEventListener('dragover',event=>{if(dragging&&dragging.note===currentNote?.id&&!noteBusy){event.preventDefault();event.dataTransfer.dropEffect='move';}});
  preview.addEventListener('drop',event=>{
    if(!dragging||dragging.note!==currentNote?.id||noteBusy)return;event.preventDefault();
    const target=event.target.closest('[data-line]'),index=target?Number(target.dataset.line):get('markdown').value.split('\n').length;
    change(dragging.index,dragging.url,()=>move(get('markdown').value,dragging.index,index));dragging=null;
  });
  window.BerestaImages={parse,encode,move,adjacent,insert,render,upload};
})();
