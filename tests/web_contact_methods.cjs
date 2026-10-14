'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const root=path.join(__dirname,'../app/static');
const read=name=>fs.readFileSync(path.join(root,name),'utf8');
const settle=async()=>{for(let i=0;i<15;i++)await new Promise(resolve=>setImmediate(resolve));};
module.exports=async()=>{
 const dom=new JSDOM(read('index.html'),{url:'https://beresta.invalid/',runScripts:'outside-only'});
 const w=dom.window,$=id=>w.document.getElementById(id),calls=[];
 let methods={email_verified:false,telegram_linked:false};
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
 w.HTMLDialogElement.prototype.close=function(){this.open=false;};
 w.api=async(url,options={})=>{
  calls.push({url,body:options.body?JSON.parse(options.body):null});
  if(url.endsWith('/login-methods'))return methods;
  if(url.endsWith('/email/verify')){methods={...methods,email_verified:true};return{status:'verified'};}
  if(url.endsWith('/account/email'))return{status:'sent'};
  throw new Error('Unexpected '+url);
 };
 w.message=text=>{$('message').textContent=text||'';};
 $('workspace').hidden=false;
 w.eval(read('contact-methods.js'));
 try{
  await w.BerestaContact.refresh();assert.equal($('contact-method-banner').hidden,false);
  $('contact-email').click();assert.equal($('contact-email-dialog').open,true);
  $('contact-email-address').value='person@example.test';
  await $('contact-email-form').onsubmit({preventDefault(){}});
  assert.deepEqual(calls.at(-1).body,{email:'person@example.test'});
  assert.match($('contact-email-status').textContent,/Письмо отправлено/);
  const token='a'.repeat(43);
  w.history.replaceState(null,'','#verify-email='+token);
  await w.BerestaContact.refresh();
  assert.equal(calls.at(-2).url,'/api/v1/account/email/verify');
  assert.deepEqual(calls.at(-2).body,{token});
  assert.equal(w.location.hash,'');assert.equal($('contact-method-banner').hidden,true);
  methods={email_verified:false,telegram_linked:true};
  await w.BerestaContact.refresh();assert.equal($('contact-method-banner').hidden,true);
  methods={email_verified:false,telegram_linked:false};
  await w.BerestaContact.refresh();assert.equal($('contact-method-banner').hidden,false);
  $('workspace').hidden=true;await settle();assert.equal($('contact-method-banner').hidden,true);
 }finally{dom.window.close();}
 console.log('Contact method checks passed: old-login notice, email send and verification, Telegram link state.');
};
