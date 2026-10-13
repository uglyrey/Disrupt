'use strict';
const assert = require('node:assert/strict'), fs = require('fs'), path = require('path'), {JSDOM} = require('jsdom');
const {randomUUID} = require('node:crypto');
const read = file => fs.readFileSync(path.join(__dirname,'../app/static',file),'utf8');
const A = '11111111-1111-4111-8111-111111111111', B = '22222222-2222-4222-8222-222222222222';
const C = '33333333-3333-4333-8333-333333333333';
const sleep = ms => new Promise(resolve => setTimeout(resolve,ms));
async function until(test,label) { for (let i=0;i<500;i++) {if(test())return;await sleep(5);}throw new Error('Timed out: '+label); }
function boot({url='https://beresta.invalid/',historyState=null,username='nav_owner'}={}) {
  const dom = new JSDOM(read('index.html'),{url,runScripts:'outside-only'}),w=dom.window,get=id=>w.document.getElementById(id);
  if(historyState)w.history.replaceState(historyState,'',url);
  Object.defineProperty(w.crypto,'randomUUID',{value:randomUUID});w.confirm=()=>false;
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
  w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
  w.HTMLMediaElement.prototype.load=()=>{};w.HTMLMediaElement.prototype.pause=()=>{};
  const note=id=>({id,capture_id:id,title:id===A?'Первая':'Вторая',original_text:'Synthetic original',markdown:'Synthetic note',
    provider:'manual',version:1,created_at:1700000000,updated_at:1700000000,category_id:null,structure_confirmed_at:null,
    items:[],conclusions:[],input_kind:'text',channel:'web'});
  const state={calls:[],notes:{[A]:note(A),[B]:note(B)},hold:null,release:null,releaseSave:null,patches:[]};
  const reply=(data,status=200,headers={})=>({ok:status<400,status,headers:new Headers(headers),json:async()=>structuredClone(data)});
  w.fetch=async (input,options={})=>{
    const url=new URL(input,w.location.href),method=options.method||'GET';state.calls.push({path:url.pathname,method});
    if(url.pathname==='/health')return reply({simulation:true});
    if(url.pathname==='/api/v1/auth/me')return reply({username,csrf_token:'synthetic-csrf',policy_current:true});
    if(url.pathname==='/api/v1/provider/usage')return reply({simulation:true,daily_units_remaining:100,daily_unit_limit:100});
    if(url.pathname==='/api/v1/categories')return reply([]);
    if(url.pathname==='/api/v1/captures/text')return new Promise(resolve=>{state.releaseSave=()=>resolve(reply({note_id:A},202));});
    if(url.pathname==='/api/v1/jobs')return reply([{id:C,capture_id:C,status:'failed',created_at:1700000000,error_code:'stt_failed',original_text:'',note_id:null}]);
    if(url.pathname==='/api/v1/notes')return reply(Object.values(state.notes).map(n=>({id:n.id,title:n.title,updated_at:n.updated_at})));
    const found=/^\/api\/v1\/notes\/([^/]+)$/.exec(url.pathname);
    if(found){
      if(method==='PATCH'){const body=JSON.parse(options.body);state.patches.push(body);Object.assign(state.notes[found[1]],body,{version:state.notes[found[1]].version+1});return reply(state.notes[found[1]]);}
      if(state.hold===found[1])return new Promise(resolve=>{state.release=()=>resolve(reply(state.notes[found[1]]));});
      return state.notes[found[1]] ? reply(state.notes[found[1]]) : reply({detail:'Заметка недоступна'},404);
    }
    if(url.pathname==='/api/v1/captures/'+C)return reply({capture_id:C,input_kind:'audio',note_id:null,job:{id:C,status:'failed'},
      transcript:'Synthetic transcript',transcript_origin:'manual',transcript_version:1,original_text:'',audio_url:'/audio',
      audio_sha256:'a'.repeat(64),audio_bytes:100,duration_seconds:1,channel:'telegram'});
    if(url.pathname.endsWith('/related'))return reply({related:[]});
    if(url.pathname.endsWith('/reminders'))return reply([]);
    if(url.pathname.endsWith('/original-opened')||url.pathname.endsWith('/opened')||url.pathname.endsWith('/search/events'))return reply({});
    if(url.pathname.endsWith('/revisions'))return reply([]);
    throw new Error('Unexpected mock path '+method+' '+url.pathname);
  };
  w.eval(['reminders.js','app.js','workspace.js','brain.js','search-screen.js','focus.js','navigation.js'].map(read).join('\n'));
  return {dom,w,get,state};
}
async function ready(t) {await until(()=>!t.get('workspace').hidden&&t.w.history.state?.berestaNavigation===1,'initial history');}
async function open(t,id) {t.get('notes').querySelector(`[data-note-id="${id}"]`).click();await until(()=>t.w.location.hash==='#note='+id&&!t.get('title').disabled,'open note');}
async function move(t,delta,hash) {t.w.history.go(delta);await until(()=>t.w.location.hash===hash,'URL '+hash);await sleep(40);}
module.exports=async()=>{
  let t=boot();
  try {
    await ready(t);await open(t,A);await open(t,B);
    const count=t.w.history.length;
    await open(t,B);assert.equal(t.w.history.length,count,'Opening the same note does not add duplicate history');
    await move(t,-1,'#note='+A);await until(()=>t.get('title').value==='Первая','first note after Back');
    await move(t,-1,'');assert.equal(t.get('capture-card').hidden,false);assert.equal(t.get('note-card').hidden,true);
    await move(t,1,'#note='+A);await until(()=>t.get('title').value==='Первая','Forward');
    t.get('item-text').value='Unsaved task';t.get('feedback-dialog').showModal();
    t.w.history.back();await sleep(150);
    assert.equal(t.w.location.hash,'#note='+A);assert.equal(t.get('item-text').value,'Unsaved task');
    assert.equal(t.get('feedback-dialog').open,true,'A refused Back does not close the current dialog');
    t.get('item-text').value='';t.get('feedback-dialog').close();
    t.get('markdown').value='Saved before Back';t.get('markdown').dispatchEvent(new t.w.Event('input',{bubbles:true}));
    await move(t,-1,'');await until(()=>t.get('note-card').hidden,'leave after autosave');
    assert.equal(t.state.notes[A].markdown,'Saved before Back');
  }finally{await sleep(50);t.dom.window.close();}
  t=boot();
  try {
    await ready(t);await open(t,A);t.get('mobile-library-toggle').click();assert.equal(t.w.location.hash,'#notes');
    await open(t,B);await move(t,-1,'#notes');
    assert.equal(t.get('workspace').classList.contains('library-open'),true,'Back restores the phone library drawer');
    await move(t,-1,'#note='+A);await until(()=>t.get('title').value==='Первая','note before the drawer');
  }finally{await sleep(50);t.dom.window.close();}
  t=boot();
  try {
    await ready(t);t.get('new-note').click();t.get('thought').value='Synthetic pending capture';t.get('processing-mode').value='manual';
    const save=t.get('capture-form').onsubmit({preventDefault(){}});await until(()=>t.state.releaseSave,'pending capture write');
    t.w.history.back();await sleep(150);assert.equal(t.w.location.hash,'#new');
    assert.equal(t.get('thought').value,'Synthetic pending capture');t.state.releaseSave();await save;
    await until(()=>t.w.location.hash==='#note='+A,'save after refused Back');
    assert.equal(t.state.calls.filter(c=>c.path==='/api/v1/captures/text').length,1,'Back cannot duplicate the save');
  }finally{await sleep(50);t.dom.window.close();}
  t=boot();
  try {
    await ready(t);t.get('nav-search').click();await until(()=>!t.get('search-card').hidden,'search');
    t.get('find-query').value='Synthetic';t.get('find-form').dispatchEvent(new t.w.Event('submit',{cancelable:true}));
    await until(()=>t.get('find-results').querySelector('button'),'results');
    t.get('find-results').querySelector('button').click();await until(()=>t.w.location.hash==='#note='+A,'search result');
    await move(t,-1,'#search');assert.equal(t.get('find-query').value,'Synthetic');assert.equal(t.get('find-results').children.length,2);
    await move(t,1,'#note='+A);await until(()=>t.get('title').value==='Первая','note again');
    const original=[...t.get('jobs').querySelectorAll('button')].find(b=>b.textContent==='Открыть исходник');original.click();
    await until(()=>t.w.location.hash==='#source='+C,'source');
    t.get('transcript-text').value='Unsaved transcript';t.w.history.back();await sleep(150);
    assert.equal(t.w.location.hash,'#source='+C);assert.equal(t.get('transcript-text').value,'Unsaved transcript');
    t.get('transcript-text').value='Synthetic transcript';await move(t,-1,'#note='+A);
    await until(()=>!t.get('note-card').hidden,'back from source');
  }finally{await sleep(50);t.dom.window.close();}
  t=boot();
  try {
    await ready(t);await open(t,A);await open(t,B);t.state.hold=A;
    t.w.history.back();await until(()=>t.state.release,'pending Back read');t.w.history.back();await sleep(30);
    t.state.release();await until(()=>t.w.location.hash===''&&!t.get('capture-card').hidden,'latest Back wins');
    assert.equal(t.get('note-card').hidden,true);
  }finally{await sleep(50);t.dom.window.close();}
  t=boot({url:'https://beresta.invalid/#note='+A});
  try {await ready(t);await until(()=>t.get('title').value==='Первая','bookmarked note');await move(t,-1,'');assert.equal(t.get('capture-card').hidden,false);}
  finally{await sleep(50);t.dom.window.close();}
  t=boot({url:'https://beresta.invalid/#note=44444444-4444-4444-8444-444444444444'});
  try {await ready(t);await until(()=>t.w.location.hash==='','unavailable bookmark returns home');assert.equal(t.get('note-card').hidden,true);}
  finally{await sleep(50);t.dom.window.close();}
  t=boot();
  try {
    await ready(t);t.w.history.replaceState(null,'',t.w.location.href);await open(t,A);
    t.get('item-text').value='Draft on a legacy history entry';t.w.history.back();await sleep(150);
    assert.equal(t.w.location.hash,'#note='+A);assert.equal(t.get('item-text').value,'Draft on a legacy history entry');
  }finally{await sleep(50);t.dom.window.close();}
  t=boot({url:'https://beresta.invalid/#note='+A,username:'another_owner',historyState:{berestaNavigation:1,
    owner:'nav_owner',group:'synthetic-history',index:1,route:{kind:'note',id:A}}});
  try {await ready(t);assert.equal(t.w.location.hash,'');assert.equal(t.state.calls.some(c=>c.path==='/api/v1/notes/'+A),false,'Old account history must not load its note');}
  finally{await sleep(50);t.dom.window.close();}
  console.log('Navigation DOM checks passed: Back/Forward, search context, sources, autosave, refused navigation, rapid Back and account isolation.');
};
if(require.main===module)module.exports().catch(error=>{console.error(error);process.exitCode=1;});
