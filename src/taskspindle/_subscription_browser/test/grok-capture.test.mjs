import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { installCapture, readEvidence } from '../page.mjs';
import { normalize } from '../extractors.mjs';

// Only schema and billing labels come from the sanitized live observation.
// All account/session values and dates below are synthetic.
const payload={status:'valid',session:{email:'alice@example.com',sessionId:'PRIVATE_SESSION',userId:'PRIVATE_USER',googleEmail:'OTHER_PRIVATE_EMAIL'}};
const response=(body,status=200)=>({status,ok:status===200,clone:()=>({json:async()=>body})});
const tick=()=>new Promise(resolve=>setTimeout(resolve,5));
const billing='SuperGrok\nInvoices\nRenews September 25, 2026';
const hash=email=>createHash('sha256').update(`taskspindle:subscription:v1:grok:${email}`).digest('hex');
function setup({fetch=async()=>response(payload),text=billing,url='https://grok.com/?_s=billing'}={}) {
  globalThis.window={fetch};globalThis.location=new URL(url);
  const root={innerText:text,getClientRects:()=>[{}],closest:()=>null,contains:()=>false,
    getAttribute:key=>key==='aria-label'?'Billing':null,querySelector:()=>null,querySelectorAll:()=>[]};
  globalThis.document={title:'',querySelector:()=>null,querySelectorAll:selector=>selector.includes('dialog')?[root]:[]};
  installCapture();
  return ()=>{delete globalThis.window;delete globalThis.location;delete globalThis.document;};
}
async function waitIdentity() {
  const deadline=Date.now()+1000;
  while(!window.__taskspindleCapture.grokIdentity && Date.now()<deadline) await tick();
  assert.ok(window.__taskspindleCapture.grokIdentity);
}
test('Grok exact session identity plus observed billing labels produces a safe renewal',async()=>{
  const cleanup=setup();
  try {
    await window.fetch('/api/auth/session');await waitIdentity();
    const evidence=await readEvidence('grok');
    const result=normalize('grok',evidence,null,'UTC');
    assert.equal(result.ok,true);assert.equal(result.observation.account_id,hash('alice@example.com'));
    assert.equal(result.observation.account_label,'a***@e***.com');
    assert.equal(result.observation.renews_at,'2026-09-25');
    assert.equal(result.observation.date_precision,'date');
    assert.deepEqual(window.__taskspindleCapture.grokIdentity,{account_id:hash('alice@example.com'),account_label:'a***@e***.com'});
    for(const value of ['alice@example.com','PRIVATE_SESSION','PRIVATE_USER','OTHER_PRIVATE_EMAIL']) {
      assert.equal(JSON.stringify(window.__taskspindleCapture).includes(value),false);
      assert.equal(JSON.stringify(result).includes(value),false);
    }
    assert.equal(normalize('grok',evidence,hash('bob@example.com'),'UTC').error.code,'ACCOUNT_MISMATCH');
  } finally {cleanup();}
});
test('Grok ignores wrong origin, nonexact paths, and unknown session schema',async()=>{
  for(const route of ['https://other.example/api/auth/session','/api/auth/session/other']) {
    const cleanup=setup();
    try {await window.fetch(route);await tick();assert.equal(window.__taskspindleCapture.grokIdentity,undefined);}
    finally {cleanup();}
  }
  const wrongHost=setup({url:'https://chatgpt.com/#settings/Billing'});
  try {await window.fetch('/api/auth/session');await tick();assert.equal(window.__taskspindleCapture.grokIdentity,undefined);}
  finally {wrongHost();}
  for(const body of [{email:'alice@example.com'},{session:{user:{email:'alice@example.com'}}},{session:{email:'not an email'}},{session:{email:7}},{session:{email:null}}]) {
    const cleanup=setup({fetch:async()=>response(body)});
    try {await window.fetch('/api/auth/session');await tick();assert.equal(window.__taskspindleCapture.grokIdentity,null);assert.equal(normalize('grok',await readEvidence('grok'),null,'UTC').error.code,'PARSE_CHANGED');}
    finally {cleanup();}
  }
});
test('Grok session and eligible DOM identity must agree',async()=>{
  for(const text of [billing+'\nEmail\nbob@example.com',billing+'\nEmail\nalice@example.com\nAccount email\nbob@example.com']) {
    const cleanup=setup({text});
    try {await window.fetch('/api/auth/session');await waitIdentity();assert.equal(normalize('grok',await readEvidence('grok'),null,'UTC').error.code,'PARSE_CHANGED');}
    finally {cleanup();}
  }
  const cleanup=setup({text:billing+'\nAccount email\nALICE@example.com'});
  try {await window.fetch('/api/auth/session');await waitIdentity();assert.equal(normalize('grok',await readEvidence('grok'),null,'UTC').ok,true);}
  finally {cleanup();}
});
test('Grok usage credit expiry cannot supply a renewal and external billing remains rejected',async()=>{
  for(const [text,code] of [
    ['SuperGrok\nInvoices\nCredits expire September 25, 2026\nUsage resets September 26, 2026','PARSE_CHANGED'],
    [billing+'\nSubscription managed through X Premium','UNSUPPORTED_BILLING_CHANNEL'],
  ]) {
    const cleanup=setup({text,url:'https://grok.com/?_s=usage'});
    try {await window.fetch('/api/auth/session');await waitIdentity();assert.equal(normalize('grok',await readEvidence('grok'),null,'UTC').error.code,code);}
    finally {cleanup();}
  }
});
test('Grok latest response wins over stale identity JSON and stale 401',async()=>{
  for(const stale401 of [false,true]) {
    let finishOld;let calls=0;
    const cleanup=setup({fetch:async()=>{
      if(calls++)return response(payload);
      if(stale401)return new Promise(resolve=>{finishOld=()=>resolve(response(null,401));});
      return {status:200,ok:true,clone:()=>({json:()=>new Promise(resolve=>{finishOld=()=>resolve({session:{email:'old@example.com'}});})})};
    }});
    try {
      const old=window.fetch('/api/auth/session');await tick();
      await window.fetch('/api/auth/session');await waitIdentity();
      finishOld();await old;await tick();
      assert.equal(window.__taskspindleCapture.grokIdentity.account_id,hash('alice@example.com'));
      assert.equal(window.__taskspindleCapture.authRequired,false);
    } finally {cleanup();}
  }
});
test('Grok fresh 401 invalidates identity; other HTTP failures do not claim expired authentication',async()=>{
  for(const status of [401,403,500]) {
    let calls=0;const cleanup=setup({fetch:async()=>calls++?response(null,status):response(payload)});
    try {
      await window.fetch('/api/auth/session');await waitIdentity();
      await window.fetch('/api/auth/session');await tick();
      assert.equal(window.__taskspindleCapture.grokIdentity,null);
      assert.equal(normalize('grok',await readEvidence('grok'),null,'UTC').error.code,status===401?'AUTH_REQUIRED':'PARSE_CHANGED');
    } finally {cleanup();}
  }
});
test('Grok stale digest completion cannot publish identity after a newer request',async()=>{
  const descriptor=Object.getOwnPropertyDescriptor(globalThis,'crypto');
  const realDigest=crypto.subtle.digest.bind(crypto.subtle);
  let finishDigest;let digests=0;let calls=0;
  Object.defineProperty(globalThis,'crypto',{configurable:true,value:{subtle:{digest:(...args)=>{
    if(digests++)return realDigest(...args);
    return new Promise(resolve=>{finishDigest=async()=>resolve(await realDigest(...args));});
  }}}});
  const cleanup=setup({fetch:async()=>response({session:{email:calls++?'alice@example.com':'old@example.com'}})});
  try {
    await window.fetch('/api/auth/session');await tick();
    await window.fetch('/api/auth/session');await waitIdentity();
    await finishDigest();await tick();
    assert.equal(window.__taskspindleCapture.grokIdentity.account_id,hash('alice@example.com'));
  } finally {cleanup();Object.defineProperty(globalThis,'crypto',descriptor);}
});
