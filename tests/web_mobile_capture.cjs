'use strict';
// Phone capture and list use the existing browser history router.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const read = name => fs.readFileSync(path.join(__dirname, '../app/static', name), 'utf8');
const ID = '11111111-1111-4111-8111-111111111111';
const note = {id:ID,capture_id:ID,title:'Synthetic note',markdown:'Synthetic text',original_text:'Synthetic source',
  items:[],conclusions:[],version:1,provider:'manual',category_id:null,channel:'web',input_kind:'text',
  created_at:1791220000,updated_at:1791220000};
const reply = (data,status=200) => ({ok:status<400,status,headers:new Headers(),json:async()=>structuredClone(data)});
const wait = ms => new Promise(resolve => setTimeout(resolve,ms));
async function until(test,label) { for(let i=0;i<100;i++){if(test())return;await wait(10);}throw new Error('Timed out: '+label); }

module.exports = async () => {
  const css=read('workspace.css'), html=read('index.html'), buttons=read('buttons.css');
  assert.match(css,/\.workspace:not\(\.library-open\)>\.library-column/);
  assert.match(css,/\.workspace\.library-open>\.editor-column\{display:none\}/);
  assert.match(css,/#mobile-library-toggle,#mobile-back\{display:none\}/);
  assert.ok(html.includes('id="mobile-library-toggle"') && html.includes('id="mobile-back"'));
  assert.ok(html.includes('id="reminder-date"') && html.includes('id="reminder-time"'));
  assert.ok(!html.includes('id="reminder-chips"'));
  assert.ok(buttons.includes('--btn-h-lg:48px'));

  const dom=new JSDOM(html,{url:'https://beresta.invalid/',runScripts:'outside-only'}), w=dom.window;
  const get=id=>w.document.getElementById(id);
  w.matchMedia=query=>({matches:/max-width:\s*640px/.test(query),addEventListener(){},removeEventListener(){}});
  w.confirm=()=>true;w.scrollTo=()=>{};
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
  w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
  w.HTMLMediaElement.prototype.load=()=>{};w.HTMLMediaElement.prototype.pause=()=>{};
  w.fetch=async (input,options={})=>{
    const url=new URL(input,w.location.href), p=url.pathname;
    if(p==='/health'||p==='/api/v1/provider/usage')return reply({simulation:true});
    if(p==='/api/v1/auth/me')return reply({username:'tester',csrf_token:'csrf'});
    if(p==='/api/v1/categories'||p==='/api/v1/jobs')return reply([]);
    if(p==='/api/v1/notes')return reply([note]);
    if(p==='/api/v1/notes/'+ID)return reply(note);
    if(p.endsWith('/reminders')||p.endsWith('/revisions'))return reply([]);
    if(p.endsWith('/related'))return reply({note_id:ID,related:[]});
    if(p.endsWith('/opened')||p.endsWith('/original-opened')||p==='/api/v1/search/events')return reply(null,204);
    if(p==='/api/v1/telegram/links')return reply({identities:[],pending:[]});
    throw new Error('Unexpected mock path '+p);
  };
  try {
    w.eval(['reminders.js','app.js','workspace.js','brain.js','search-screen.js','note-actions.js','trash.js','focus.js','navigation.js'].map(read).join('\n'));
    await until(()=>!get('workspace').hidden&&w.history.state?.berestaNavigation===1,'initial history');
    assert.equal(get('capture-card').hidden,false);
    assert.equal(get('workspace').classList.contains('library-open'),false);
    assert.equal(get('mobile-library-count').textContent,'1');
    get('mobile-library-toggle').click();
    assert.equal(get('workspace').classList.contains('library-open'),true);
    assert.equal(w.location.hash,'#notes');
    get('mobile-back').click();
    await until(()=>w.location.hash===''&&!get('workspace').classList.contains('library-open'),'back to capture');
    get('mobile-library-toggle').click();
    get('notes').querySelector('button[data-note-id]').click();
    await until(()=>w.location.hash==='#note='+ID&&!get('note-card').hidden,'open note');
    assert.equal(get('workspace').classList.contains('library-open'),false);
    get('mobile-back').click();
    await until(()=>w.location.hash==='#notes'&&get('workspace').classList.contains('library-open'),'back to list');
    get('mobile-back').click();
    await until(()=>w.location.hash===''&&!get('capture-card').hidden,'back to capture again');
    // The list over an open note: the button says «Новая запись» and must show the form, not the note it came from.
    get('mobile-library-toggle').click();
    get('notes').querySelector('button[data-note-id]').click();
    await until(()=>w.location.hash==='#note='+ID&&!get('note-card').hidden,'open note again');
    get('nav-notes').click();
    await until(()=>w.location.hash==='#notes'&&get('workspace').classList.contains('library-open'),'list over the note');
    assert.equal(get('mobile-back-label').textContent,'Новая запись');
    assert.equal(w.BerestaNavigation.previous().kind,'note');
    get('mobile-back').click();
    await until(()=>w.location.hash==='#new'&&!get('capture-card').hidden&&get('note-card').hidden,'back button shows the form');
    assert.equal(get('workspace').classList.contains('library-open'),false);
    // «Новая запись» (the button, or the general menu item that clicks it) closes an open list by itself.
    get('mobile-library-toggle').click();
    assert.equal(get('workspace').classList.contains('library-open'),true);
    get('new-note').click();
    await until(()=>!get('workspace').classList.contains('library-open'),'new note closes the list');
    assert.equal(get('capture-card').hidden,false);
    assert.equal(get('mobile-library-toggle').getAttribute('aria-expanded'),'false');
    // The general menu has its own item for it, first in the sections group.
    const first=html.indexOf('data-proxy="new-note"');
    assert.ok(first>0&&first<html.indexOf('data-proxy="nav-notes"'),'general menu item «Новая запись»');
    console.log('Mobile capture checks passed: capture first, list button, note and Back through existing history.');
  } finally {await wait(20);dom.window.close();}
};
