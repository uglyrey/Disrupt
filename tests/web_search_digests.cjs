'use strict';
const assert = require('node:assert/strict'), fs = require('fs'), path = require('path'), {JSDOM} = require('jsdom');
const read = file => fs.readFileSync(path.join(__dirname,'../app/static',file),'utf8');
const wait = () => new Promise(resolve => setTimeout(resolve,10));
const rows = title => [{id:'own-note',title,updated_at:1700000000,channel:'telegram',input_kind:'audio'}];
module.exports = async function () {
  const dom = new JSDOM(read('index.html'),{url:'https://example.test/#search',runScripts:'outside-only'}), w = dom.window;
  const get = id => w.document.getElementById(id);
  get('workspace').hidden = get('search-card').hidden = false;
  const calls = [], opened = []; let slow, slowPage, slowSettings, slowSave;
  const defaults = {morning_enabled:false,evening_enabled:false,morning_time:'09:00',evening_time:'20:00',timezone:'Europe/Moscow',configured:false,telegram_available:true};
  w.testApi = async (url,options = {},headers = false) => {
    calls.push({url,options});
    if (url === '/api/v1/categories') return [{id:'category-own',name:'Работа'}];
    if (url === '/api/v1/search/events') return {};
    if (url.startsWith('/api/v1/notes?')) {
      if (new URL(url,w.location.href).searchParams.get('q') === 'slow') return new Promise(resolve => {slow = resolve;});
      if (new URL(url,w.location.href).searchParams.get('offset') !== '0') return new Promise(resolve => {slowPage = resolve;});
      return {data:rows('<img src=x onerror=alert(1)>'),headers:new Headers({'X-Next-Notes-Offset':'20'})};
    }
    if (url === '/api/v1/telegram/digest-settings') {
      if (options.method === 'PATCH') return new Promise(resolve => {slowSave = resolve;});
      if (slowSettings) return new Promise(resolve => {slowSettings = resolve;});
      return structuredClone(defaults);
    }
    throw new Error('unexpected '+url);
  };
  w.testOpen = (id,options) => {opened.push({id,options});return Promise.resolve();};
  w.eval(`var epoch=0; var api=window.testApi; var openNote=window.testOpen;
    var sourceQuery={telegram:{channel:'telegram'},voice:{input_kind:'audio'},text:{input_kind:'text'}};
    var sourceMarks=()=>[];
    var element=(tag,text,cls)=>{const node=document.createElement(tag);node.textContent=text;node.className=cls||'';return node;};
    window.berestaId=()=>crypto.randomUUID();`);
  try {
    w.eval(read('search-screen.js')); await wait();
    get('find-query').value = 'slow'; get('find-form').dispatchEvent(new w.Event('submit',{cancelable:true})); await wait();
    get('find-query').value = 'new'; get('find-category').value = 'category-own'; get('find-source').value = 'voice';
    get('find-form').dispatchEvent(new w.Event('submit',{cancelable:true})); await wait();
    const latest = new URL(calls.filter(call=>call.url.startsWith('/api/v1/notes?')).at(-1).url,w.location.href);
    assert.deepEqual([...latest.searchParams],[['limit','20'],['offset','0'],['q','new'],['category_id','category-own'],['input_kind','audio']]);
    assert.equal(get('find-results').querySelector('img'),null);
    slow({data:rows('stale'),headers:new Headers()}); await wait();
    assert.equal(get('find-results').textContent.includes('stale'),false);
    get('find-results').querySelector('button').click(); await wait();
    assert.equal(opened[0].id,'own-note'); assert.equal(opened[0].options.userAction,true);
    assert.ok(opened[0].options.search);
    get('find-more').click(); await wait();
    w.eval('epoch++'); get('workspace').hidden = true; w.BerestaSearch.reset();
    slowPage({data:rows('another account'),headers:new Headers()}); await wait();
    assert.equal(get('find-results').textContent,''); assert.equal(get('find-query').value,'');

    get('workspace').hidden = false; w.eval(read('digests.js'));
    get('telegram-dialog').open = true; await wait();
    assert.equal(get('digest-morning').checked,false); assert.equal(get('digest-evening').checked,false);
    assert.equal(get('digest-settings-save').disabled,false);
    assert.equal(calls.some(call=>call.options.method==='PATCH'),false,'reading settings must never enable delivery');
    get('digest-morning').checked = true; get('digest-zone').value = 'Europe/Moscow';
    get('digest-settings-form').dispatchEvent(new w.Event('submit',{cancelable:true})); await wait();
    const patch = calls.find(call=>call.options.method==='PATCH');
    assert.deepEqual(JSON.parse(patch.options.body),{morning_enabled:true,evening_enabled:false,morning_time:'09:00',evening_time:'20:00',timezone:'Europe/Moscow'});
    w.eval('epoch++'); get('workspace').hidden = true; await wait();
    slowSave({...defaults,morning_enabled:true}); await wait();
    assert.equal(get('digest-settings-status').textContent,''); assert.equal(get('digest-morning').checked,false);
    assert.equal(get('digest-settings-save').disabled,true);
    console.log('Search and digest DOM checks passed: filtering, pagination, XSS, stale replies and opt-in.');
  } finally {dom.window.close();}
};
if (require.main === module) module.exports().catch(error=>{console.error(error);process.exitCode=1;});
