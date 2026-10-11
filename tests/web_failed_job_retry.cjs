'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {randomUUID} = require('node:crypto');
const {JSDOM} = require('jsdom');

async function until(condition) {
  for (let i=0;i<150;i++) {
    if (condition()) return;
    await new Promise(resolve => setImmediate(resolve));
  }
  throw new Error('Failed-job UI did not settle');
}
async function settle() { for (let i=0;i<20;i++) await new Promise(resolve => setImmediate(resolve)); }

async function scenario({kind = 'text', errorCode = 'provider_timeout_unknown', text = 'Synthetic failed text'} = {}) {
  const root = path.join(__dirname,'../app/static');
  const dom = new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{
    url:'https://beresta.invalid/',runScripts:'outside-only',
  });
  const w = dom.window, $ = id => w.document.getElementById(id);
  Object.defineProperty(w.crypto,'randomUUID',{value:randomUUID});
  w.confirm = () => false;
  w.URL.createObjectURL = () => 'blob:synthetic'; w.URL.revokeObjectURL = () => {};
  const failed = {id:randomUUID(),capture_id:randomUUID(),status:'failed',provider:'mock',note_id:null,
    original_text:text,created_at:1791280000,error_code:errorCode};
  const queued = {id:randomUUID(),capture_id:randomUUID(),status:'queued',provider:'mock',note_id:null,
    original_text:text,created_at:1791280100,error_code:null};
  const note = id => ({id,capture_id:queued.capture_id,title:'Synthetic note',original_text:text,
    markdown:'Synthetic',conclusions:[],items:[],provider:'mock',version:1,category_id:null,
    structure_confirmed_at:null,created_at:1791280000,updated_at:1791280000});
  const calls = [];
  function reply(data,status=200) {
    return {ok:status>=200 && status<300,status,headers:new Headers(),json:async () => structuredClone(data)};
  }
  w.fetch = async (input,options={}) => {
    const url = new URL(input,w.location.href), method = options.method || 'GET';
    calls.push({path:url.pathname,method,options});
    if (url.pathname === '/health' || url.pathname === '/api/v1/provider/usage') return reply({simulation:true});
    if (url.pathname === '/api/v1/auth/me') return reply({username:'synthetic',csrf_token:'synthetic-csrf'});
    if (url.pathname === '/api/v1/categories') return reply([]);
    if (url.pathname === '/api/v1/jobs') return reply([failed]);
    if (url.pathname === '/api/v1/notes') return reply([]);
    if (url.pathname.startsWith('/api/v1/notes/')) return reply(note(url.pathname.split('/').at(-1)));
    if (url.pathname === `/api/v1/captures/${failed.capture_id}`) {
      return reply(kind === 'audio'
        ? {capture_id:failed.capture_id,input_kind:'audio',original_text:text,processing_mode:'ai',transcript:text,
          transcript_version:1,transcript_origin:'stt',audio_seconds:1,audio_media_type:'audio/wav',note_id:null,job:failed}
        : {capture_id:failed.capture_id,input_kind:'text',original_text:text,processing_mode:'ai',transcript:null,
          transcript_version:1,transcript_origin:null,audio_seconds:null,audio_media_type:null,note_id:null,job:failed});
    }
    if (url.pathname === '/api/v1/captures/text') {
      if (JSON.parse(options.body).processing_mode === 'manual') return reply({note_id:'manual-note'},201);
      return reply(queued,202);
    }
    if (url.pathname === `/api/v1/jobs/${queued.id}`) return new Promise(() => {});
    if (url.pathname.endsWith('/original-opened')) return reply(null,204);
    throw new Error(`Unexpected synthetic request ${method} ${url.pathname}`);
  };
  w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8'));
  await until(() => !$('workspace').hidden && $('jobs').querySelector('.job'));
  const posts = () => calls.filter(c => c.path === '/api/v1/captures/text' && c.method === 'POST');
  const button = mode => $('jobs').querySelector(`[data-retry="${mode}"]`);
  return {dom,$,w,failed,calls,posts,button};
}

module.exports = async function checkFailedJobRetry() {
  let s;
  try {
    s = await scenario();
    assert.equal(s.button('ai').textContent,'Отправить ещё раз с ИИ');
    assert.equal(s.button('manual').textContent,'Сохранить без ИИ');
    assert.match(s.$('jobs').textContent,/Нажмите «Отправить ещё раз с ИИ» или сохраните без ИИ/);
    assert.doesNotMatch(s.$('jobs').textContent,/Cloud\.ru/);
    await settle();
    assert.equal(s.posts().length,0,'Nothing is sent until the person clicks');

    s.button('ai').click();
    await until(() => s.posts().length === 1);
    assert.equal(s.calls.some(c => c.path === `/api/v1/captures/${s.failed.capture_id}`),true,'The original is read from the capture');
    const first = s.posts()[0];
    assert.deepEqual(JSON.parse(first.options.body),{text:'Synthetic failed text',processing_mode:'ai'});
    assert.ok(first.options.headers['Idempotency-Key']);
    await until(() => !s.$('capture-submit').disabled && s.$('thought').value === '');
    assert.ok(s.button('manual'),'The failed row stays available');

    s.button('manual').click();
    await until(() => s.posts().length === 2);
    const second = s.posts()[1];
    assert.deepEqual(JSON.parse(second.options.body),{text:'Synthetic failed text',processing_mode:'manual'});
    assert.notEqual(second.options.headers['Idempotency-Key'],first.options.headers['Idempotency-Key'],
      'Every retry is a new capture with a new key');
  } finally { s?.dom.window.close(); }

  try {
    s = await scenario();
    s.$('thought').value = 'Another unsent draft';
    s.button('ai').click();
    await until(() => s.calls.some(c => c.path === `/api/v1/captures/${s.failed.capture_id}`));
    await settle();
    assert.equal(s.$('thought').value,'Another unsent draft','Declining keeps the draft');
    assert.equal(s.posts().length,0);
    s.w.confirm = () => true;
    s.button('ai').click();
    await until(() => s.posts().length === 1);
    assert.equal(JSON.parse(s.posts()[0].options.body).text,'Synthetic failed text');
  } finally { s?.dom.window.close(); }

  try {
    s = await scenario();
    s.$('processing-mode').querySelector('option[value="ai"]').disabled = true;
    s.button('ai').click();
    await until(() => s.$('thought').value === 'Synthetic failed text');
    await settle();
    assert.equal(s.posts().length,0,'AI is off, so nothing is sent without a choice');
    assert.equal(s.$('processing-mode').value,'manual');
    assert.match(s.$('message').textContent,/ИИ сейчас недоступен/);
  } finally { s?.dom.window.close(); }

  try {
    s = await scenario({kind:'audio',errorCode:'provider_unavailable',text:'Synthetic transcript'});
    s.button('ai').click();
    await until(() => !s.$('source-card').hidden && /Создать заметку из расшифровки/.test(s.$('message').textContent));
    assert.equal(s.posts().length,0,'A voice note goes through its own transcript flow');
  } finally { s?.dom.window.close(); }

  try {
    s = await scenario({errorCode:'budget_exhausted'});
    assert.equal(s.button('ai'),null,'A spent app limit cannot be fixed by sending again');
    assert.ok(s.button('manual'),'Saving without AI is still possible');
  } finally { s?.dom.window.close(); }
  console.log('Failed-job checks passed: explicit retry with AI, save without AI, draft protection, voice flow.');
};

if (require.main === module) module.exports().catch(error => {console.error(error);process.exitCode=1;});
