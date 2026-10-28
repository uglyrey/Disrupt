'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const root=path.join(__dirname,'../app/static'),KEY='beresta.cookie-consent.v1';
const saved=(choice,version=2)=>JSON.stringify({choice,version,at:'2026-10-28T12:00:00Z'});
function boot({choice=null,version=2,url='https://app.berestaapp.ru/',file='index.html'}={}){
  const dom=new JSDOM(fs.readFileSync(path.join(root,file),'utf8'),{url,referrer:'https://advertiser.example/private?email=secret',runScripts:'outside-only'});
  const w=dom.window,$=id=>w.document.getElementById(id),calls=[];
  if(choice)w.localStorage.setItem(KEY,saved(choice,version));
  w.ym=(...args)=>calls.push(args);
  w.eval(fs.readFileSync(path.join(root,'cookie-consent.js'),'utf8'));
  w.eval(fs.readFileSync(path.join(root,'metrika.js'),'utf8'));
  const tag=()=>w.document.querySelector('script[src^="https://mc.yandex.ru/"]');
  const loaded=()=>tag().dispatchEvent(new w.Event('load'));
  return {dom,w,$,calls,tag,loaded};
}
module.exports=async()=>{
  // No script or beacon before choice, after refusal, or after legacy preference-only acceptance.
  for(const entry of [{},{choice:'necessary'},{choice:'all',version:1}]){
    const t=boot(entry);
    try{
      assert.equal(t.tag(),null);assert.equal(t.calls.length,0);
      if(entry.choice==='all'){
        assert.ok(t.$('cookie-consent'));assert.equal(t.w.berestaConsent.allowsOptional(),true);
        assert.equal(t.w.berestaConsent.allowsAnalytics(),false);
        t.$('cookie-accept-all').click();assert.ok(t.tag());
      }
    }finally{t.dom.window.close();}
  }
  let t=boot({choice:'all',url:'https://app.berestaapp.ru/?utm_source=direct&yclid=123&email=secret&token=secret&capture=private#note=private'});
  try{
    assert.equal(t.tag().referrerPolicy,'no-referrer');assert.equal(t.calls.length,0);
    t.w.document.title='Private note title';t.loaded();
    const init=t.calls.find(c=>c[1]==='init')[2];
    for(const key of ['clickmap','trackLinks','trackHash','webvisor','ecommerce','sendTitle'])assert.equal(init[key],false,key);
    assert.equal(init.defer,true);assert.equal(init.disableYtm,true);
    const hit=t.calls.find(c=>c[1]==='hit');
    assert.equal(hit[2],'https://app.berestaapp.ru/?utm_source=direct&yclid=123#note');
    assert.equal(hit[3].title,'beresta');assert.equal(hit[3].referer,'https://advertiser.example/');
    t.w.BerestaMetrika.success('/api/v1/captures/text','POST',{capture_id:'private-id',text:'PRIVATE_TEXT'});
    t.w.BerestaMetrika.success('/api/v1/captures/text','POST',{capture_id:'private-id'});
    t.w.BerestaMetrika.success('/api/v1/jobs/job-1','GET',{id:'job-1',status:'failed'});
    t.w.BerestaMetrika.success('/api/v1/jobs/job-1','GET',{id:'job-1',status:'succeeded',text:'PRIVATE_TEXT'});
    t.w.BerestaMetrika.success('/api/v1/auth/email/code/confirm','POST',{username:'PRIVATE_USERNAME'});
    assert.deepEqual(t.calls.filter(c=>c[1]==='reachGoal').map(c=>c[2]),['capture_saved','ai_completed','login_completed']);
    t.w.history.pushState({},'', '/?q=private#search');
    t.w.document.dispatchEvent(new t.w.Event('beresta:navigation'));
    assert.equal(t.calls.filter(c=>c[1]==='hit').at(-1)[2],'https://app.berestaapp.ru/#search');
    assert.doesNotMatch(JSON.stringify(t.calls),/PRIVATE_|private-id|job-1|Private note title|email=|token=|capture=|q=/);
    t.w.document.cookie='_ym_uid=123; path=/; Secure';t.w.document.cookie='unrelated=keep; path=/; Secure';
    t.w.localStorage.setItem('_ym_uid','123');
    t.w.berestaConsent.reopen();t.$('cookie-necessary').click();
    assert.equal(t.calls.at(-1)[1],'destruct');assert.doesNotMatch(t.w.document.cookie,/_ym_uid/);
    assert.match(t.w.document.cookie,/unrelated=keep/);assert.equal(t.w.localStorage.getItem('_ym_uid'),null);
    const before=t.calls.length;t.w.BerestaMetrika.success('/api/v1/captures/audio','POST',{capture_id:'after-optout'});
    assert.equal(t.calls.length,before);
  }finally{t.dom.window.close();}
  // Opt-out while the remote library is downloading must not flush previously queued events.
  t=boot({choice:'all'});
  try{
    t.w.BerestaMetrika.success('/api/v1/captures/text','POST',{capture_id:'queued'});
    t.w.berestaConsent.reopen();t.$('cookie-necessary').click();t.loaded();
    assert.equal(t.calls.length,0);
    t.w.berestaConsent.reopen();t.$('cookie-accept-all').click();
    assert.equal(t.calls.filter(c=>c[1]==='reachGoal').length,0);
    t.w.localStorage.setItem(KEY,saved('necessary'));
    t.w.dispatchEvent(new t.w.StorageEvent('storage',{key:KEY}));
    assert.equal(t.calls.at(-1)[1],'destruct');
  }finally{t.dom.window.close();}
  // A blocked counter has no influence on API/UI functionality. No counter in admin/review or on localhost.
  t=boot({choice:'all'});
  try{t.tag().dispatchEvent(new t.w.Event('error'));assert.equal(t.tag(),null);}finally{t.dom.window.close();}
  for(const url of ['https://app.berestaapp.ru/admin','https://app.berestaapp.ru/review','https://localhost/']){
    t=boot({choice:'all',url});try{assert.equal(t.tag(),null);assert.equal(t.calls.length,0);}finally{t.dom.window.close();}
  }
  console.log('Metrika DOM passed: consent, legacy choice, sanitized URLs/goals, opt-out race, cross-tab revocation and blocked script.');
};
if(require.main===module)module.exports().catch(error=>{console.error(error);process.exitCode=1;});
