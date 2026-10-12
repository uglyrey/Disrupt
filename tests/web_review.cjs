'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const root=path.join(__dirname,'..'),assets=path.join(root,'app/static');
const fixture=JSON.parse(fs.readFileSync(path.join(root,'docs/fixtures/admin-summary.json'),'utf8'));
const dom=new JSDOM(fs.readFileSync(path.join(assets,'review.html'),'utf8'),{url:'https://beresta.invalid/review',runScripts:'outside-only'});
const w=dom.window,$=id=>w.document.getElementById(id),requests=[],downloads=[];
let signedIn=false,expired=false;
const reply=(status,data,headers={})=>({ok:status>=200&&status<300,status,headers:new Headers(headers),json:async()=>structuredClone(data),blob:async()=>new Blob(['synthetic export'])});
w.URL.createObjectURL=()=> 'blob:synthetic';w.URL.revokeObjectURL=()=>{};
w.HTMLAnchorElement.prototype.click=function(){downloads.push(this.download);};
w.fetch=async(input,options)=>{
  const url=new URL(input,w.location.href);requests.push(url.pathname);
  assert.equal(url.origin,w.location.origin);assert.equal(options.credentials,'same-origin');assert.equal(options.cache,'no-store');
  assert.ok(url.pathname.startsWith('/review-api/v1/'));
  if(url.pathname.endsWith('/me'))return signedIn?reply(200,{username:'organizer',csrf_token:'csrf-1'}):reply(401,{});
  if(url.pathname.endsWith('/login')){signedIn=true;return reply(200,{username:'organizer',csrf_token:'csrf-1'});}
  if(url.pathname.endsWith('/logout')){assert.equal(options.headers['X-CSRF-Token'],'csrf-1');signedIn=false;return reply(204,{});}
  if(!signedIn||expired)return reply(401,{});
  if(url.pathname.endsWith('/summary'))return reply(200,{...fixture,budget:null});
  if(url.pathname.endsWith('/export.zip'))return reply(200,null,{'Content-Type':'application/zip'});
  if(url.pathname.endsWith('/export'))return reply(200,null,{'Content-Type':'text/csv'});
  throw new Error('Unexpected route '+url.pathname);
};
const submit=id=>$(id).dispatchEvent(new w.Event('submit',{cancelable:true}));
async function settle(){for(let i=0;i<20;i++)await new Promise(resolve=>setImmediate(resolve));}
async function login(){
  $('admin-username').value='organizer';$('admin-password').value='synthetic-password';$('admin-code').value='123456';
  submit('login-form');await settle();
}
(async()=>{
  try{
    for(const id of ['show-feedback','show-audit','feedback-inbox','audit-view'])assert.equal($(id),null);
    for(const file of ['admin.auth.js','admin.js','admin.tools.js','review.navigation.js'])w.eval(fs.readFileSync(path.join(assets,file),'utf8'));
    await settle();assert.equal($('admin-app').hidden,true);assert.deepEqual(requests,['/review-api/v1/me']);
    await login();assert.equal($('admin-app').hidden,false);assert.equal($('dashboard').hidden,false);
    assert.ok($('cards').textContent.length);assert.ok(w.document.querySelector('[aria-labelledby="budget-title"]').hidden);
    assert.equal($('admin-password').value,'');assert.equal($('admin-code').value,'');
    $('export').click();await settle();assert.ok(downloads.some(name=>name.endsWith('.csv')));
    $('show-archive').click();assert.equal($('statistics-view').hidden,true);assert.equal($('archive-view').hidden,false);
    submit('archive-form');await settle();assert.ok(downloads.some(name=>name.endsWith('.zip')));
    w.document.dispatchEvent(new w.CustomEvent('admin:view',{detail:'audit'}));await settle();assert.ok(!requests.some(url=>url.includes('/audit')));
    $('show-statistics').click();assert.equal($('archive-view').hidden,true);assert.equal($('statistics-view').hidden,false);
    expired=true;submit('filters');await settle();assert.equal($('admin-app').hidden,true);assert.equal($('cards').textContent,'');assert.equal($('export').disabled,true);
    expired=false;await login();$('logout').click();await settle();assert.equal($('admin-app').hidden,true);assert.equal($('cards').textContent,'');
    assert.equal(w.localStorage.length,0);console.log('Observer DOM passed: login, aggregate data, read-only navigation, CSV/ZIP, expiry and logout.');
  }finally{dom.window.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
