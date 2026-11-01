'use strict';
// «Добавить к «X»», «Отдельная заметка» и «Отменить»: кнопки под результатом и вызовы API (docs/attach-to-note-v1.md).
const assert = require('node:assert/strict');
const {randomUUID} = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const root = path.join(__dirname,'../app/static');
const dom = new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://beresta.invalid/',runScripts:'outside-only'});
const w = dom.window, $ = id => w.document.getElementById(id);
Object.defineProperty(w.crypto,'randomUUID',{value:randomUUID});
const base = {provider:'manual',version:1,conclusions:[],category_id:null,structure_confirmed_at:null,items:[],original_text:'x',attached:[],attach_offer:null};
const target = {...base,id:randomUUID(),capture_id:randomUUID(),title:'Курсовая',markdown:'Курсовая'};
const own = {...base,id:randomUUID(),capture_id:randomUUID(),title:'Глава 3',markdown:'Глава 3',
  attach_offer:{capture_id:randomUUID(),note_id:target.id,title:'Курсовая',expires_at:4102444800}};
const calls = [];
let attached = false, undone = false;
function reply(data,status=200) { return {ok:status >= 200 && status < 300,status,headers:new Headers(),json:async () => structuredClone(data)}; }
function targetView() {
  return {...target,attached:attached && !undone ? [{capture_id:own.attach_offer.capture_id,created_at:1,added_at:2,channel:'web',
    input_kind:'text',original_text:'<b>Глава 3</b>',can_undo:true,undo_until:4102444800}] : []};
}
w.fetch = async (input,options={}) => {
  const url = new URL(input,w.location.href), method = options.method || 'GET';
  calls.push({url,method,body:options.body ? JSON.parse(options.body) : null,headers:options.headers});
  const p = url.pathname;
  if (p === '/health' || p === '/api/v1/provider/usage') return reply({simulation:true});
  if (p === '/api/v1/auth/me') return reply({username:'Тест',csrf_token:'synthetic-csrf'});
  if (p === '/api/v1/categories' || p === '/api/v1/jobs') return reply([]);
  if (p === '/api/v1/timezones') return reply({zones:[]});
  if (p === '/api/v1/telegram/links') return reply({identities:[],pending:[]});
  if (p === '/api/v1/notes') return reply(attached && !undone ? [target] : [target,own]);
  if (p === `/api/v1/notes/${own.id}`) return reply(undone ? {...own,attach_offer:null} : own);
  if (p === `/api/v1/notes/${target.id}`) return reply(targetView());
  if (/\/reminders$/.test(p)) return reply([]);
  if (/\/(opened|original-opened)$/.test(p)) return reply(null,204);
  if (method !== 'GET') assert.equal(options.headers['X-CSRF-Token'],'synthetic-csrf');
  const cid = own.attach_offer.capture_id;
  if (p === `/api/v1/captures/${cid}/attach`) { attached = true; return reply({capture_id:cid,state:'attached',note_id:target.id,note_title:'Курсовая',target_text_kept:false}); }
  if (p === `/api/v1/captures/${cid}/detach`) { undone = true; return reply({capture_id:cid,state:'separate',note_id:own.id,note_title:'Глава 3',target_text_kept:false}); }
  if (p === `/api/v1/captures/${cid}/separate`) return reply({capture_id:cid,state:'separate',note_id:own.id,note_title:'Глава 3',target_text_kept:false});
  throw new Error(`Unexpected request ${method} ${p}`);
};
async function until(condition) {
  for (let n=0;n<400;n++) { if (condition()) return; await new Promise(resolve => n % 10 === 9 ? setTimeout(resolve,5) : setImmediate(resolve)); }
  throw new Error('UI did not settle: ' + $('message').textContent);
}
(async () => {
  try {
    w.eval(fs.readFileSync(path.join(root,'timezone.js'),'utf8'));
    w.eval(fs.readFileSync(path.join(root,'reminders.js'),'utf8'));
    w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8') + '\n' + fs.readFileSync(path.join(root,'attach.js'),'utf8'));
    await until(() => $('notes').querySelectorAll('button').length === 2);
    [...$('notes').querySelectorAll('button')].find(b => b.textContent.includes('Глава 3')).click();
    await until(() => !$('attach-box').hidden);
    assert.ok($('attach-box').textContent.includes('Похоже, это продолжение «Курсовая». Добавить туда?'));
    assert.equal($('attach-yes').textContent,'Добавить к «Курсовая»');
    assert.equal($('attach-no').textContent,'Отдельная заметка');
    $('attach-yes').click();
    await until(() => $('title').value === 'Курсовая' && $('attach-box').querySelector('.attach-undo'));
    const post = calls.find(c => c.url.pathname.endsWith('/attach'));
    assert.deepEqual(post.body,{note_id:target.id});
    assert.ok($('message').textContent.includes('Добавил в «Курсовая»'));
    assert.equal($('attach-box').querySelector('.attach-sources pre').textContent,'<b>Глава 3</b>','Text is shown as text, never as HTML');
    assert.equal($('attach-box').querySelector('.attach-sources b'),null);
    await until(() => $('notes').querySelectorAll('button').length === 1);
    $('attach-box').querySelector('.attach-undo').click();
    await until(() => $('title').value === 'Глава 3');
    assert.ok(calls.some(c => c.url.pathname.endsWith('/detach') && c.method === 'POST'));
    assert.equal($('attach-box').hidden,true,'No buttons stay after the undo');
    console.log('web_attach ok');
  } catch (error) { console.error(error); process.exitCode = 1; }
  finally { w.close(); }
})();
