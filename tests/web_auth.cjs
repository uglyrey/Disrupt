'use strict';
// Telegram sign-in, recovery, reset page, policy banner and account deletion in the browser.
// Timers are replaced with a manual queue so the 2 second polling runs on demand.
const assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),{JSDOM}=require('jsdom');
const root=path.join(__dirname,'../app/static');
const read=name=>fs.readFileSync(path.join(root,name),'utf8');
const reply=(data,status=200)=>({ok:status<400,status,headers:new Headers(),json:async()=>structuredClone(data)});
async function settled(){for(let i=0;i<25;i++)await new Promise(resolve=>setImmediate(resolve));}
const VERSION='2026-10-12', TOKEN='R'.repeat(43);
const user=(extra={})=>({id:'u1',username:'ivan',csrf_token:'csrf-1',policy_current:true,policy_version:VERSION,deletion_requested:false,...extra});

async function open({hash='',me=null,routes={},popup='ok',phone=false,session={}}={}){
 const dom=new JSDOM(read('index.html'),{url:'https://beresta.invalid/'+hash,runScripts:'outside-only'});
 const w=dom.window,$=id=>w.document.getElementById(id);
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
 w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
 // jsdom reports a hidden document unless told otherwise; a real visible tab polls.
 Object.defineProperty(w.document,'hidden',{configurable:true,get:()=>false});
 const timers=new Map(),delays=[];let next=1;
 w.setTimeout=(fn,ms)=>{const id=next++;timers.set(id,fn);delays.push(ms);return id;};
 w.clearTimeout=id=>{timers.delete(id);};
 // jsdom has no WebSocket server, no popups and no navigation. These stand in for them.
 const sockets=[],tabs=[],navigations=[];let openCalls=0;
 w.WebSocket=class{
  constructor(url){this.url=url;this.closed=false;sockets.push(this);}
  close(){this.closed=true;}
  push(data){if(this.onmessage)this.onmessage({data:JSON.stringify(data)});}
  drop(){if(this.onclose)this.onclose();}
 };
 w.open=()=>{
  openCalls++;
  if(popup==='blocked')return null;
  const tab={closed:false,opener:{},location:{href:''},close(){this.closed=true;}};
  tabs.push(tab);return tab;
 };
 w.berestaNavigate=url=>navigations.push(url);
 if(phone)Object.defineProperty(w.navigator,'userAgent',{configurable:true,get:()=>'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15'});
 for(const [key,value] of Object.entries(session))w.sessionStorage.setItem(key,value);
 const calls=[];
 w.fetch=async(input,options={})=>{
  const url=new URL(input,w.location.href),body=options.body?JSON.parse(options.body):null;
  calls.push({path:url.pathname,method:options.method||'GET',body,headers:options.headers||{}});
  if(url.pathname==='/health'||url.pathname==='/api/v1/provider/usage')return reply({simulation:true});
  if(url.pathname==='/api/v1/auth/me')return me?reply(me):reply({detail:'Войдите в приложение'},401);
  if(url.pathname==='/api/v1/categories'||url.pathname==='/api/v1/jobs'||url.pathname==='/api/v1/notes')return reply([]);
  const route=routes[url.pathname]||Object.entries(routes).find(([key])=>key.endsWith('*')&&url.pathname.startsWith(key.slice(0,-1)))?.[1];
  if(route)return route(body,calls.at(-1));
  throw new Error('Unexpected '+url.pathname);
 };
 w.eval(read('app.js')+'\n'+read('auth-extra.js'));
 await settled();
 // One round of due timers. Everything they schedule waits for the next call.
 const tick=async()=>{const due=[...timers.values()];timers.clear();for(const fn of due)await fn();await settled();};
 return {dom,w,$,calls,timers,delays,sockets,tabs,navigations,openCalls:()=>openCalls,tick,closeAll:()=>dom.window.close()};
}
const callsTo=(t,suffix)=>t.calls.filter(c=>c.path.endsWith(suffix));

async function telegramLogin(){
 const login={status:'pending'};
 const t=await open({routes:{
  '/api/v1/auth/telegram/start':()=>reply({login_id:'login-1',deep_link:'https://t.me/beresta_ru_bot?start=login_'+TOKEN,expires_at:new Date(Date.now()+300000).toISOString()},201),
  '/api/v1/auth/telegram/status/*':()=>login.status==='ok'?reply({...user({username:'tg123'}),status:'ok',new_account:true}):login.status==='404'?reply({detail:'Вход не найден. Начните заново'},404):reply({status:login.status}),
 }});
 try{
  assert.equal(t.$('auth').hidden,false);
  t.$('tg-login').click();await settled();
  assert.match(t.$('auth-message').textContent,/согласие на обработку персональных данных/);
  assert.equal(callsTo(t,'/telegram/start').length,0,'no request without consent');
  t.$('accept-policy').checked=true;
  t.$('tg-login').click();await settled();
  assert.match(t.$('auth-message').textContent,/18 лет/);
  assert.equal(callsTo(t,'/telegram/start').length,0,'no request without the age confirmation');
  t.$('confirm-age').checked=true;
  t.$('tg-login').click();await settled();
  const start=callsTo(t,'/telegram/start')[0];
  assert.deepEqual(start.body,{accept_policy:true,policy_version:VERSION,confirm_age:true});
  assert.match(t.$('tg-login-hint').textContent,/Запустить.*Да, это я/);
  assert.equal(t.$('tg-login-box').hidden,false);
  assert.equal(t.$('tg-login-link').href,'https://t.me/beresta_ru_bot?start=login_'+TOKEN);
  assert.equal(t.$('tg-login').disabled,true);
  assert.equal(t.timers.size,1,'polling is scheduled');
  await t.tick();
  assert.equal(callsTo(t,'/status/login-1').length,1);assert.equal(t.timers.size,1,'still waiting');
  assert.equal(t.$('workspace').hidden,true);
  login.status='ok';await t.tick();
  assert.equal(t.$('workspace').hidden,false);assert.equal(t.$('auth').hidden,true);
  assert.equal(t.$('username').textContent,'tg123');
  assert.equal(t.$('message').textContent,'Аккаунт создан. Добро пожаловать!');
  assert.equal(t.timers.size,0,'polling stops after sign-in');
  assert.equal(t.$('policy-banner').hidden,true);
  assert.equal(JSON.stringify(t.w.localStorage.length),'0','nothing is kept in storage');
 }finally{t.closeAll();}
}

async function telegramLoginEnds(){
 for(const [how,expected] of [['expired',/Время вышло/],['404',/Вход не найден/]]){
  const login={status:how==='404'?'404':'pending'};
  const t=await open({routes:{
   '/api/v1/auth/telegram/start':()=>reply({login_id:'login-2',deep_link:'https://t.me/beresta_ru_bot?start=login_'+TOKEN,expires_at:new Date(Date.now()+300000).toISOString()},201),
   '/api/v1/auth/telegram/status/*':()=>login.status==='404'?reply({detail:'Вход не найден. Начните заново'},404):reply({status:login.status}),
  }});
  try{
   t.$('accept-policy').checked=true;t.$('confirm-age').checked=true;t.$('tg-login').click();await settled();
   login.status=how==='404'?'404':'expired';await t.tick();
   assert.match(t.$('auth-message').textContent,expected,how);
   assert.equal(t.$('tg-login-box').hidden,true);assert.equal(t.$('tg-login').disabled,false);
   assert.equal(t.timers.size,0);assert.equal(t.$('workspace').hidden,true);
  }finally{t.closeAll();}
 }
 // A start that has already run out locally ends on the first check, without asking the server.
 let t=await open({routes:{
  '/api/v1/auth/telegram/start':()=>reply({login_id:'login-3',deep_link:'https://t.me/beresta_ru_bot?start=login_'+TOKEN,expires_at:new Date(Date.now()-10000).toISOString()},201),
 }});
 try{
  t.$('accept-policy').checked=true;t.$('confirm-age').checked=true;t.$('tg-login').click();await settled();await t.tick();
  assert.match(t.$('auth-message').textContent,/Время вышло/);assert.equal(callsTo(t,'/status/login-3').length,0);
 }finally{t.closeAll();}
 // Cancel stops polling and a foreign link is never used.
 t=await open({routes:{
  '/api/v1/auth/telegram/start':()=>reply({login_id:'login-4',deep_link:'javascript:alert(1)',expires_at:new Date(Date.now()+300000).toISOString()},201),
 }});
 try{
  t.$('accept-policy').checked=true;t.$('confirm-age').checked=true;t.$('tg-login').click();await settled();
  assert.equal(t.$('tg-login-box').hidden,true);assert.equal(t.timers.size,0);assert.equal(t.$('tg-login').disabled,false);
  assert.ok(t.$('auth-message').textContent.length>0);
 }finally{t.closeAll();}
 t=await open({routes:{
  '/api/v1/auth/telegram/start':()=>reply({login_id:'login-5',deep_link:'https://t.me/beresta_ru_bot?start=login_'+TOKEN,expires_at:new Date(Date.now()+300000).toISOString()},201),
 }});
 try{
  t.$('accept-policy').checked=true;t.$('confirm-age').checked=true;t.$('tg-login').click();await settled();
  assert.equal(t.timers.size,1);t.$('tg-login-cancel').click();
  assert.equal(t.timers.size,0);assert.equal(t.$('tg-login-box').hidden,true);assert.equal(t.$('tg-login').disabled,false);
 }finally{t.closeAll();}
}

async function recovery(){
 const t=await open({routes:{
  '/api/v1/auth/recovery/telegram':body=>body.username==='limited'?reply({detail:'Слишком много попыток. Подождите минуту.'},429):reply({status:'ok',message:'Если у аккаунта подключён Telegram, мы отправили туда ссылку для нового пароля. Она действует 15 минут.'}),
 }});
 try{
  t.$('forgot-password').click();await settled();
  assert.match(t.$('auth-message').textContent,/имя пользователя/);assert.equal(callsTo(t,'/recovery/telegram').length,0);
  t.$('login').value='  ivan ';t.$('forgot-password').click();await settled();
  assert.deepEqual(callsTo(t,'/recovery/telegram')[0].body,{username:'ivan'});
  assert.equal(t.$('auth-info').hidden,false);assert.match(t.$('auth-info').textContent,/Если у аккаунта подключён Telegram/);
  assert.equal(t.$('auth-message').hidden,true);assert.equal(t.$('forgot-password').disabled,false);
  t.$('login').value='limited';t.$('forgot-password').click();await settled();
  assert.match(t.$('auth-message').textContent,/Слишком много попыток/);assert.equal(t.$('auth-info').hidden,true);
 }finally{t.closeAll();}
}

async function resetPage(){
 let outcome='ok';
 const routes={'/api/v1/auth/password-reset':()=>outcome==='ok'?reply({status:'ok'}):reply({detail:'Ссылка недействительна или истекла. Запросите новую'},400)};
 let t=await open({hash:'#reset='+TOKEN,routes});
 try{
  assert.equal(t.$('reset-dialog').open,true,'the link opens the password form');
  assert.equal(t.w.location.hash,'','the token leaves the address bar');
  assert.ok(!t.w.location.href.includes(TOKEN));
  const submit=()=>t.$('reset-form').dispatchEvent(new t.w.Event('submit',{cancelable:true}));
  t.$('reset-password').value='short';t.$('reset-password2').value='short';submit();await settled();
  assert.match(t.$('reset-status').textContent,/от 10 до 128/);
  t.$('reset-password').value='brand-new-password-456';t.$('reset-password2').value='different-password-789';submit();await settled();
  assert.match(t.$('reset-status').textContent,/не совпадают/);assert.equal(callsTo(t,'/password-reset').length,0);
  outcome='dead';t.$('reset-password2').value='brand-new-password-456';submit();await settled();
  assert.match(t.$('reset-status').textContent,/недействительна или истекла/);assert.equal(t.$('reset-dialog').open,true);
  outcome='ok';submit();await settled();
  const sent=callsTo(t,'/password-reset');
  assert.deepEqual(sent.at(-1).body,{token:TOKEN,password:'brand-new-password-456'});
  assert.equal(t.$('reset-dialog').open,false);assert.equal(t.$('reset-password').value,'');
  assert.match(t.$('auth-info').textContent,/Пароль изменён/);
  submit();await settled();
  assert.match(t.$('reset-status').textContent,/Ссылка устарела|от 10 до 128/,'the token is not reused from memory');
  assert.equal(callsTo(t,'/password-reset').length,sent.length);
 }finally{t.closeAll();}
 t=await open({hash:'#reset=short',routes});
 try{assert.equal(t.$('reset-dialog').open,false,'a malformed link opens nothing');}finally{t.closeAll();}
 t=await open({routes});
 try{
  t.w.location.hash='#reset='+TOKEN;t.w.dispatchEvent(new t.w.HashChangeEvent('hashchange'));
  assert.equal(t.$('reset-dialog').open,true,'a link opened in a running page works too');
 }finally{t.closeAll();}
}

async function policyAndDeletion(){
 const routes={
  '/api/v1/account/accept-policy':body=>body.policy_version===VERSION?reply({status:'ok',policy_version:VERSION}):reply({detail:'Политика обновилась'},409),
  '/api/v1/account/delete-request/cancel':()=>reply({status:'cancelled'}),
 };
 let t=await open({me:user({policy_current:false,deletion_requested:true}),routes});
 try{
  assert.equal(t.$('workspace').hidden,false);
  assert.equal(t.$('policy-banner').hidden,false);assert.equal(t.$('deletion-banner').hidden,false);
  assert.equal(t.$('notes').hidden,false,'nothing else is blocked');
  t.$('policy-accept').click();await settled();
  assert.deepEqual(callsTo(t,'/accept-policy')[0].body,{policy_version:VERSION});
  assert.equal(callsTo(t,'/accept-policy')[0].headers['X-CSRF-Token'],'csrf-1');
  assert.equal(t.$('policy-banner').hidden,true);
  t.$('deletion-cancel').click();await settled();
  assert.equal(t.$('deletion-banner').hidden,true);assert.equal(t.$('message').textContent,'Запрос на удаление отменён.');
 }finally{t.closeAll();}
 t=await open({me:user(),routes});
 try{assert.equal(t.$('policy-banner').hidden,true);assert.equal(t.$('deletion-banner').hidden,true);}finally{t.closeAll();}

 const deletion={status:'pending',password:'right'};
 t=await open({me:user(),routes:{
  '/api/v1/account/delete-request':body=>body.password===deletion.password?reply({status:'requested'}):reply({detail:'Неверный пароль'},403),
  '/api/v1/account/delete-request/telegram':()=>reply({login_id:'del-1',deep_link:'https://t.me/beresta_ru_bot?start=login_'+TOKEN,expires_at:new Date(Date.now()+300000).toISOString()},201),
  '/api/v1/account/delete-request/telegram/*':()=>reply({status:deletion.status}),
 }});
 try{
  const link=t.$('open-privacy');assert.equal(link.getAttribute('href'),'/privacy');
  t.$('open-delete').click();assert.equal(t.$('delete-dialog').open,true);
  const submit=()=>t.$('delete-form').dispatchEvent(new t.w.Event('submit',{cancelable:true}));
  submit();await settled();assert.match(t.$('delete-status').textContent,/Введите пароль/);
  t.$('delete-password').value='wrong';submit();await settled();
  assert.equal(t.$('delete-status').textContent,'Неверный пароль');assert.equal(t.timers.size,0);
  t.$('delete-telegram-start').click();await settled();
  assert.equal(t.$('delete-telegram-box').hidden,false);
  assert.equal(t.$('delete-telegram-link').href,'https://t.me/beresta_ru_bot?start=login_'+TOKEN);
  await t.tick();assert.equal(t.timers.size,1);assert.equal(t.$('delete-status').textContent.includes('Запрос принят'),false);
  deletion.status='requested';await t.tick();
  assert.match(t.$('delete-status').textContent,/Запрос принят/);
  assert.equal(t.timers.size,1,'only the page reload is pending');
  assert.equal(callsTo(t,'/delete-request/telegram/del-1').length,2);
 }finally{t.closeAll();}
 t=await open({me:user(),routes:{'/api/v1/account/delete-request':()=>reply({status:'requested'})}});
 try{
  t.$('open-delete').click();t.$('delete-password').value='right';
  t.$('delete-form').dispatchEvent(new t.w.Event('submit',{cancelable:true}));await settled();
  assert.match(t.$('delete-status').textContent,/Запрос принят/);
  assert.deepEqual(callsTo(t,'/delete-request')[0].body,{password:'right'});
 }finally{t.closeAll();}
}

// One tap, WebSocket push, polling as the safety net and resuming after a return from Telegram.
const DEEP='https://t.me/beresta_ru_bot?start=login_'+TOKEN;
const APP_DEEP='tg://resolve?domain=beresta_ru_bot&start=login_'+TOKEN;
const FLOW='beresta-tg-flow';
function liveRoutes(login,extra={}){
 return {
  '/api/v1/auth/telegram/start':()=>reply({login_id:'login-1',deep_link:DEEP,expires_at:new Date(Date.now()+300000).toISOString()},201),
  '/api/v1/auth/telegram/status/*':()=>login.status==='ok'?reply({...user({username:'tg123'}),status:'ok',new_account:false}):reply({status:login.status}),
  ...extra,
 };
}
const startLogin=async t=>{t.$('accept-policy').checked=true;t.$('confirm-age').checked=true;t.$('tg-login').click();};

async function oneTapSignIn(){
 // Desktop: the tab opens inside the click, before the answer of the server. Its address is set afterwards.
 let t=await open({routes:liveRoutes({status:'pending'})});
 try{
  await startLogin(t);
  assert.equal(t.openCalls(),1,'the popup opens synchronously inside the click');
  assert.equal(t.tabs[0].location.href,'','no address before the server has answered');
  assert.equal(callsTo(t,'/telegram/start').length,1,'the request started in the same tap');
  await settled();
  assert.equal(t.tabs[0].location.href,DEEP,'the tab goes straight to Telegram');
  assert.deepEqual(t.navigations,[],'this tab stays');
  assert.equal(t.$('tg-login-box').hidden,false,'the link stays as a fallback');
  assert.equal(t.$('tg-login-link').href,DEEP);
  assert.equal(t.$('tg-login-app').hidden,false);assert.equal(t.$('tg-login-app').getAttribute('href'),APP_DEEP);
  const saved=JSON.parse(t.w.sessionStorage.getItem(FLOW));
  assert.equal(saved.id,'login-1');assert.equal(saved.purpose,'login');assert.ok(saved.expires>Date.now());
  assert.ok(!JSON.stringify(saved).includes(TOKEN),'the start token is not stored');
  assert.equal(t.sockets.length,1);assert.equal(t.sockets[0].url,'wss://beresta.invalid/api/v1/auth/telegram/ws/login-1');
 }finally{t.closeAll();}
 // A blocked popup: this tab goes to Telegram and the box stays.
 t=await open({popup:'blocked',routes:liveRoutes({status:'pending'})});
 try{
  await startLogin(t);await settled();
  assert.equal(t.openCalls(),1);assert.deepEqual(t.navigations,[DEEP]);
  assert.equal(t.$('tg-login-box').hidden,false);
 }finally{t.closeAll();}
 // A phone does not open a popup at all. The flow is already saved for the return.
 t=await open({phone:true,routes:liveRoutes({status:'pending'})});
 try{
  await startLogin(t);await settled();
  assert.equal(t.openCalls(),0);assert.deepEqual(t.navigations,[DEEP]);
  assert.equal(JSON.parse(t.w.sessionStorage.getItem(FLOW)).id,'login-1');
 }finally{t.closeAll();}
 // A failed start closes the reserved tab and goes nowhere.
 t=await open({routes:liveRoutes({status:'pending'},{'/api/v1/auth/telegram/start':()=>reply({detail:'Слишком много попыток. Подождите минуту.'},429)})});
 try{
  await startLogin(t);await settled();
  assert.equal(t.tabs[0].closed,true);assert.deepEqual(t.navigations,[]);
  assert.match(t.$('auth-message').textContent,/Слишком много попыток/);
  assert.equal(t.$('tg-login').disabled,false);assert.equal(t.w.sessionStorage.getItem(FLOW),null);
 }finally{t.closeAll();}
}

async function socketPush(){
 const login={status:'pending'};
 const t=await open({routes:liveRoutes(login)});
 try{
  await startLogin(t);await settled();
  const socket=t.sockets[0];
  assert.ok(t.delays.includes(10000),'with a socket the poll is slow');
  socket.push({status:'pending'});await settled();
  assert.equal(callsTo(t,'/status/login-1').length,0,'pending needs no call');
  login.status='ok';
  socket.push({status:'confirmed'});await settled();
  assert.equal(callsTo(t,'/status/login-1').length,1,'the page asks the usual endpoint');
  assert.equal(t.$('workspace').hidden,false);assert.equal(t.$('username').textContent,'tg123');
  assert.equal(t.timers.size,0);assert.equal(socket.closed,true);
  assert.equal(t.w.sessionStorage.getItem(FLOW),null,'nothing is left to resume');
 }finally{t.closeAll();}
 // Two signals at once make one call.
 const second={status:'ok'};
 const t2=await open({routes:liveRoutes(second)});
 try{
  await startLogin(t2);await settled();
  t2.sockets[0].push({status:'confirmed'});t2.w.dispatchEvent(new t2.w.Event('focus'));await settled();
  assert.equal(t2.$('workspace').hidden,false);assert.equal(t2.$('auth-message').hidden,true);
 }finally{t2.closeAll();}
}

async function socketFailureFallsBackToPolling(){
 const login={status:'pending'};
 const t=await open({routes:liveRoutes(login)});
 try{
  await startLogin(t);await settled();
  const before=t.delays.length;
  t.sockets[0].drop();
  assert.equal(t.timers.size,1);assert.deepEqual(t.delays.slice(before),[2000],'without a socket the poll is quick again');
  login.status='ok';await t.tick();
  assert.equal(t.$('workspace').hidden,false);
 }finally{t.closeAll();}
 // No usable WebSocket in the browser at all.
 const broken=await open({routes:liveRoutes({status:'pending'})});
 try{
  broken.w.WebSocket=function(){throw new Error('blocked');};
  await startLogin(broken);await settled();
  assert.equal(broken.$('tg-login-box').hidden,false);assert.equal(broken.timers.size,1);
  assert.ok(broken.delays.includes(2000));
 }finally{broken.closeAll();}
}

async function comingBack(){
 for(const event of ['focus','pageshow','online','visibilitychange']){
  const login={status:'pending'};
  const t=await open({routes:liveRoutes(login)});
  try{
   await startLogin(t);await settled();
   t.sockets[0].drop();
   login.status='ok';
   if(event==='visibilitychange')t.w.document.dispatchEvent(new t.w.Event('visibilitychange'));
   else t.w.dispatchEvent(new t.w.Event(event));
   await settled();
   assert.equal(t.$('workspace').hidden,false,event+' checks at once');
   assert.equal(t.sockets.length>=2,true,event+' opens the socket again');
  }finally{t.closeAll();}
 }
}

async function resumeAfterReload(){
 const saved=(extra={})=>({[FLOW]:JSON.stringify({id:'login-9',expires:Date.now()+120000,purpose:'login',...extra})});
 const login={status:'pending'};
 let t=await open({session:saved(),routes:liveRoutes(login)});
 try{
  assert.equal(t.$('tg-login-box').hidden,false);assert.equal(t.$('tg-login').disabled,true);
  assert.equal(t.$('tg-login-status').textContent,'Завершаем вход…');
  assert.equal(t.$('tg-login-link').hidden,true);
  assert.equal(t.sockets[0].url,'wss://beresta.invalid/api/v1/auth/telegram/ws/login-9');
  login.status='ok';t.sockets[0].push({status:'confirmed'});await settled();
  assert.equal(callsTo(t,'/status/login-9').length,1);
  assert.equal(t.$('workspace').hidden,false);assert.equal(t.w.sessionStorage.getItem(FLOW),null);
 }finally{t.closeAll();}
 // An ended or damaged record is dropped without noise.
 t=await open({session:saved({expires:Date.now()-1000}),routes:liveRoutes({status:'pending'})});
 try{
  assert.equal(t.$('tg-login-box').hidden,true);assert.equal(t.sockets.length,0);assert.equal(t.w.sessionStorage.getItem(FLOW),null);
 }finally{t.closeAll();}
 t=await open({session:{[FLOW]:'not json'},routes:liveRoutes({status:'pending'})});
 try{assert.equal(t.$('tg-login-box').hidden,true);}finally{t.closeAll();}
 // The server no longer knows the sign-in. A signed-out page says so, a signed-in page stays quiet.
 const gone={'/api/v1/auth/telegram/status/*':()=>reply({detail:'Вход не найден. Начните заново'},404)};
 t=await open({session:saved(),routes:gone});
 try{
  await t.tick();
  assert.match(t.$('auth-message').textContent,/Вход не найден/);assert.equal(t.$('tg-login-box').hidden,true);
  assert.equal(t.$('tg-login').disabled,false);assert.equal(t.w.sessionStorage.getItem(FLOW),null);
 }finally{t.closeAll();}
 t=await open({me:user(),session:saved(),routes:gone});
 try{
  await t.tick();
  assert.equal(t.$('workspace').hidden,false);assert.equal(t.$('auth-message').hidden,true);
  assert.equal(t.w.sessionStorage.getItem(FLOW),null);
 }finally{t.closeAll();}
 // Blocked storage never breaks the sign-in.
 t=await open({routes:liveRoutes({status:'pending'})});
 try{
  t.w.Storage.prototype.setItem=function(){throw new Error('blocked');};
  await startLogin(t);await settled();
  assert.equal(t.$('tg-login-box').hidden,false);
 }finally{t.closeAll();}
}

async function deletionLive(){
 const state={status:'pending'};
 const routes={
  '/api/v1/account/delete-request/telegram':()=>reply({login_id:'del-1',deep_link:DEEP,expires_at:new Date(Date.now()+300000).toISOString()},201),
  '/api/v1/account/delete-request/telegram/*':()=>reply({status:state.status}),
 };
 let t=await open({me:user(),routes});
 try{
  t.$('open-delete').click();t.$('delete-telegram-start').click();
  assert.equal(t.openCalls(),1,'the popup opens inside the tap');
  await settled();
  assert.equal(t.tabs[0].location.href,DEEP);assert.equal(t.$('delete-telegram-app').getAttribute('href'),APP_DEEP);
  assert.equal(t.sockets[0].url,'wss://beresta.invalid/api/v1/account/delete-request/telegram/ws/del-1');
  assert.equal(JSON.parse(t.w.sessionStorage.getItem(FLOW)).purpose,'delete');
  state.status='requested';t.sockets[0].push({status:'confirmed'});await settled();
  assert.match(t.$('delete-status').textContent,/Запрос принят/);assert.equal(t.w.sessionStorage.getItem(FLOW),null);
 }finally{t.closeAll();}
 // The tab came back from Telegram on a phone: the dialog opens again and finishes.
 state.status='pending';
 t=await open({me:user(),routes,session:{[FLOW]:JSON.stringify({id:'del-2',expires:Date.now()+120000,purpose:'delete'})}});
 try{
  await settled();
  assert.equal(t.$('delete-dialog').open,true);assert.equal(t.$('delete-status').textContent,'Завершаем подтверждение…');
  assert.equal(t.sockets[0].url,'wss://beresta.invalid/api/v1/account/delete-request/telegram/ws/del-2');
  state.status='requested';t.sockets[0].push({status:'confirmed'});await settled();
  assert.match(t.$('delete-status').textContent,/Запрос принят/);
 }finally{t.closeAll();}
 // A phone navigates this tab, and a closed dialog forgets the flow.
 state.status='pending';
 t=await open({me:user(),phone:true,routes});
 try{
  t.$('open-delete').click();t.$('delete-telegram-start').click();await settled();
  assert.equal(t.openCalls(),0);assert.deepEqual(t.navigations,[DEEP]);
  t.$('delete-dialog').close();
  assert.equal(t.w.sessionStorage.getItem(FLOW),null);assert.equal(t.timers.size,0);
 }finally{t.closeAll();}
}

function staticChecks(){
 const index=read('index.html'),privacy=read('privacy.html');
 for(const [name,html] of [['index.html',index],['privacy.html',privacy]]){
  assert.ok(!/\sstyle\s*=/.test(html),name+' has no inline styles');
  assert.ok(!/<script(?![^>]*\ssrc=)/.test(html),name+' has no inline scripts');
  assert.ok(!/data:/.test(html.replace(/data-[a-z-]+/g,'')),name+' has no data: URLs');
 }
 const dom=new JSDOM(index);const doc=dom.window.document;
 const consent=doc.getElementById('accept-policy');
 assert.equal(consent.type,'checkbox');assert.equal(consent.dataset.policyVersion,VERSION);
 assert.ok(consent.closest('label').querySelector('a[href="/privacy"]'),'consent links the policy');
 const age=doc.getElementById('confirm-age');
 assert.equal(age.type,'checkbox');assert.equal(age.checked,false,'age is not pre-checked');
 assert.match(age.closest('label').textContent,/18 лет/);
 assert.equal(age.closest('form').id,'auth-form');
 assert.match(doc.getElementById('telegram-settings').textContent,/Да, подключить/);
 assert.ok(doc.querySelector('#landing-footer a[href="/privacy"]'),'landing footer links the policy');
 assert.ok(doc.querySelector('#nav-menu a[href="/privacy"]'),'account menu links the policy');
 assert.ok(doc.getElementById('open-delete'));
 assert.ok(doc.querySelector('script[src="/static/auth-extra.js"]')&&doc.querySelector('link[href="/static/auth-extra.css"]'));
 assert.equal(doc.getElementById('forgot-password').type,'button');
 const privacyDoc=new JSDOM(privacy).window.document;
 assert.equal(privacyDoc.querySelector('main').dataset.policyVersion,VERSION);
 assert.ok(privacyDoc.body.textContent.includes('Пахаруков Андрей Евгеньевич'));
 assert.ok(privacyDoc.body.textContent.includes('support@berestaapp.ru'));
 dom.window.close();privacyDoc.defaultView.close();
}

module.exports=async()=>{
 staticChecks();
 await telegramLogin();
 await telegramLoginEnds();
 await recovery();
 await resetPage();
 await policyAndDeletion();
 await oneTapSignIn();
 await socketPush();
 await socketFailureFallsBackToPolling();
 await comingBack();
 await resumeAfterReload();
 await deletionLive();
 console.log('Auth checks passed: Telegram sign-in, one tap, WebSocket push, resume, recovery, reset page, policy banner, deletion.');
};
if(require.main===module)module.exports().catch(e=>{console.error(e);process.exitCode=1;});
