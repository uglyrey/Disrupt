const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
module.exports=async function(){
 const root=path.join(__dirname,'../app/static'),read=name=>fs.readFileSync(path.join(root,name),'utf8');
 const dom=new JSDOM(read('index.html'),{url:'https://beresta.invalid/',runScripts:'outside-only'}),w=dom.window;
 const id='11111111-1111-4111-8111-111111111111',url='/api/v1/note-images/'+id;
 try{
  w.eval(`var noteBusy=false,epoch=1,currentNote={id:'note-one'};var $=id=>document.getElementById(id);var message=text=>{$('message').textContent=text||'';};var setNoteBusy=value=>{noteBusy=value;};var element=(tag,text,cls)=>{const e=document.createElement(tag);if(text)e.textContent=text;if(cls)e.className=cls;return e;};`);
  const app=read('app.js'),start=app.indexOf('function markdownSafeUrl('),end=app.indexOf('function hasDrafts(',start);
  w.eval(app.slice(start,end));
  w.confirm=()=>true;w.berestaId=()=>id;
  let held=null,calls=0,fail=false;
  w.api=async(p,options)=>{calls++;assert.equal(options.headers['Idempotency-Key'],id);if(fail&&calls===1)throw new TypeError('Lost ack');if(held)await held;return {id,url,width:10,height:10,byte_size:12};};
  w.BerestaFocus={pending:()=>false,flush:async()=>{}};
  w.BerestaMd={mode:()=>'read',setMode:()=>{},applyEdit:(field,result)=>{field.value=result.value;},refresh:()=>w.renderMarkdownInto(w.document.getElementById('preview'),w.document.getElementById('markdown').value,{images:true})};
  w.eval(read('note-images.js'));
  const images=w.BerestaImages,field=w.document.getElementById('markdown'),preview=w.document.getElementById('preview');
  const block=images.encode({url,alt:'<img onerror=x>',align:'center',width:100});
  assert.equal(images.parse(block).url,url);
  for(const bad of ['![bad](https://tracker.invalid/x)','![bad](javascript:alert(1))','![bad](data:image/svg+xml,xx)',`![bad](${url}?align=evil&width=100)`,`![bad](${url}/../other)`])assert.equal(images.parse(bad),null);
  field.value='Первый абзац\n\n'+block+'\n\nВторой абзац';w.BerestaMd.refresh();
  w.eval('noteBusy=true;');w.BerestaMd.refresh();assert.ok(preview.querySelector('button').disabled);
  w.eval('noteBusy=false;');w.document.dispatchEvent(new w.CustomEvent('beresta:note-idle'));assert.equal(preview.querySelector('button').disabled,false);
  assert.equal(preview.querySelectorAll('figure img').length,1);assert.equal(preview.querySelector('img').getAttribute('src'),url);assert.equal(preview.querySelectorAll('[onerror]').length,0);
  const button=text=>[...preview.querySelectorAll('button')].find(b=>b.textContent===text);
  button('Выше').click();assert.ok(field.value.startsWith(block));button('Ниже').click();assert.ok(field.value.startsWith('Первый абзац'));
  const align=preview.querySelector('[aria-label="Выравнивание картинки"]');align.value='right';align.dispatchEvent(new w.Event('change'));
  assert.ok(field.value.includes('align=right'));assert.ok(preview.querySelector('figure').classList.contains('ni-right'));
  const size=preview.querySelector('[aria-label="Ширина картинки"]');size.value='50';size.dispatchEvent(new w.Event('change'));assert.ok(field.value.includes('width=50'));
  button('Убрать').click();assert.equal(preview.querySelector('img'),null);assert.ok(field.value.includes('Первый')&&field.value.includes('Второй'));
  assert.equal(images.move(block+'\n\ntext',0,3),'\ntext\n'+block);
  field.value='Исходный текст';fail=true;calls=0;
  await images.upload(new w.File(['test'],'test.png',{type:'image/png'}));assert.equal(calls,2);assert.ok(field.value.includes(url));assert.equal(preview.querySelectorAll('img').length,1);
  let release;held=new Promise(resolve=>{release=resolve;});const oldValue=field.value;
  const pending=images.upload(new w.File(['test'],'test.png'));await Promise.resolve();
  w.eval("epoch=2;currentNote={id:'other-note'};");field.value='Другой аккаунт';release();await pending;assert.equal(field.value,'Другой аккаунт');
  assert.ok(oldValue.includes(url));
  console.log('Image DOM checks passed: private URLs, safe rendering, placement, width, removal, lost ack and account isolation.');
 }finally{dom.window.close();}
};
