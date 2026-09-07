import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { installCapture, readEvidence } from '../page.mjs';
import { normalize } from '../extractors.mjs';

// Sanitized fixture uses the root field names observed on 2026-09-07;
// identities/dates are synthetic. No provider payload is stored in this test.
const payload = { active_until: '2026-10-09T12:30:00Z', will_renew: false, plan_type: 'pro', is_processor_stripe: true };
const response = (body, status = 200) => ({status, ok:status === 200, clone:()=>({json:async()=>body})});
const tick = () => new Promise(resolve => setImmediate(resolve));
function environment({fetch = async()=>response(payload), text='ChatGPT Pro 20x', bootstrap={session:{user:{email:'alice@example.com'}}}, controls=[]} = {}) {
  globalThis.location = new URL('https://chatgpt.com/#settings/Billing');
  globalThis.window = {fetch};
  const root = {innerText:text,getClientRects:()=>[{}],closest:()=>null,contains:()=>false,
    getAttribute:name=>name === 'aria-label'?'Billing':null,querySelector:()=>null,
    querySelectorAll:selector=>selector.startsWith('input')?controls:[]};
  const script = bootstrap === null ? null : {textContent:JSON.stringify(bootstrap),closest:()=>null};
  globalThis.document = {title:'',querySelector:selector=>selector.startsWith('script#client-bootstrap')?script:null,
    querySelectorAll:selector=>selector.includes('dialog')?[root]:[]};
  installCapture();
  return () => {delete globalThis.window;delete globalThis.location;delete globalThis.document;};
}
async function collect(options={}) {
  const cleanup=environment(options);
  try {await window.fetch('/backend-api/subscriptions');await tick();return await readEvidence('chatgpt');}
  finally {cleanup();}
}
test('live-shaped subscription and bootstrap yield only masked identity and normalized personal plan', async()=>{
  const result=normalize('chatgpt',await collect(),null,'UTC');
  assert.equal(result.ok,true);
  assert.equal(result.observation.plan,'Pro');
  assert.equal(result.observation.status,'cancelled');
  assert.equal(result.observation.access_ends_at,'2026-10-09T12:30:00.000Z');
  assert.equal(result.observation.account_id,createHash('sha256').update('taskspindle:subscription:v1:chatgpt:alice@example.com').digest('hex'));
  assert.equal(result.observation.account_label,'a***@e***.com');
  assert.equal(JSON.stringify(result).includes('alice@example.com'),false);
});
test('bootstrap identities must agree with each other and eligible account DOM rows',async()=>{
  for(const options of [
    {bootstrap:{session:{user:{email:'alice@example.com'}},user:{email:'bob@example.com'}}},
    {text:'Account email\nbob@example.com'},
  ]) assert.equal(normalize('chatgpt',await collect(options),null,'UTC').error.code,'PARSE_CHANGED');
  const same=await collect({bootstrap:{user:{email:' ALICE@example.com '}} ,text:'Account email\nalice@example.com'});
  assert.equal(normalize('chatgpt',same,null,'UTC').ok,true);
  assert.equal(normalize('chatgpt',same,'b'.repeat(64),'UTC').error.code,'ACCOUNT_MISMATCH');
});
test('billing email input and billing-email text never establish or conflict with account identity',async()=>{
  const input={value:'billing@example.com',getClientRects:()=>[{}],closest:()=>null,matches:()=>true};
  const options={text:'Billing email\nbilling@example.com',controls:[input]};
  assert.equal(normalize('chatgpt',await collect(options),null,'UTC').ok,true);
  assert.equal(normalize('chatgpt',await collect({...options,bootstrap:null}),null,'UTC').error.code,'PARSE_CHANGED');
  assert.equal(normalize('chatgpt',await collect({bootstrap:{other:{email:'alice@example.com'}}}),null,'UTC').error.code,'PARSE_CHANGED');
});
test('explicit external billing wins over Stripe and unknown processor does not prove web billing',async()=>{
  for(const text of ['Subscription managed through Apple App Store','Subscription billed through Google Play'])
    assert.equal(normalize('chatgpt',await collect({text}),null,'UTC').error.code,'UNSUPPORTED_BILLING_CHANNEL');
  for(const is_processor_stripe of [false,null,'true'])
    assert.equal(normalize('chatgpt',await collect({fetch:async()=>response({...payload,is_processor_stripe})}),null,'UTC').error.code,'PARSE_CHANGED');
});
test('subscription plan uses exact allowlist, overrides advertised DOM plans, and never stores unknown values',async()=>{
  for(const plan_type of ['pro','plus','go','free']) {
    const evidence=await collect({fetch:async()=>response({...payload,plan_type}),text:'ChatGPT Plus\nChatGPT Pro'});
    assert.equal(evidence.plan.toLowerCase(),plan_type);
  }
  for(const plan_type of ['business','enterprise','unknown-sensitive-value',7,null]) {
    const evidence=await collect({fetch:async()=>response({...payload,plan_type}),text:'ChatGPT Pro'});
    assert.equal(normalize('chatgpt',evidence,null,'UTC').error.code,'PARSE_CHANGED');
    assert.equal(evidence.subscription.plan_type,null);
    assert.equal(JSON.stringify(evidence).includes('unknown-sensitive-value'),false);
  }
  const unknown=await collect({fetch:async()=>response({...payload,active_until:null,will_renew:null,plan_type:'business'}),text:'Subscription status\nNo subscription'});
  assert.equal(normalize('chatgpt',unknown,null,'UTC').error.code,'PARSE_CHANGED');
});
test('capture ignores unrelated routes and copies only allowed subscription fields',async()=>{
  const cleanup=environment({fetch:async()=>response({...payload,access_token:'SECRET',email:'PRIVATE',card:'PAYMENT'})});
  try {
    await window.fetch('/backend-api/subscriptions/other');await window.fetch('https://other.example/backend-api/subscriptions');await tick();
    assert.equal(window.__taskspindleCapture.subscription,null);
    await window.fetch('/backend-api/subscriptions');await tick();
    assert.deepEqual(window.__taskspindleCapture.subscription,{...payload,plan_type_unrecognized:false});
  } finally {cleanup();}
});
test('latest subscription response wins including stale JSON and stale unauthorized response',async()=>{
  for(const stale401 of [false,true]) {
    let finishOld;
    let calls=0;
    const cleanup=environment({fetch:async()=>{
      if(calls++)return response(payload);
      if(stale401)return new Promise(resolve=>{finishOld=()=>resolve(response(null,401));});
      return {status:200,ok:true,clone:()=>({json:()=>new Promise(resolve=>{finishOld=()=>resolve({...payload,plan_type:'plus'});})})};
    }});
    try {
      const old=window.fetch('/backend-api/subscriptions');await tick();
      await window.fetch('/backend-api/subscriptions');await tick();
      finishOld();await old;await tick();
      assert.equal(window.__taskspindleCapture.subscription.plan_type,'pro');
      assert.equal(window.__taskspindleCapture.authRequired,false);
    } finally {cleanup();}
  }
});
test('fresh unauthorized subscription response invalidates older billing capture',async()=>{
  let calls=0;
  const cleanup=environment({fetch:async()=>calls++?response(null,401):response(payload)});
  try {
    await window.fetch('/backend-api/subscriptions');await tick();
    await window.fetch('/backend-api/subscriptions');await tick();
    assert.equal(window.__taskspindleCapture.subscription,null);
    assert.deepEqual(await readEvidence('chatgpt'),{auth_required:true});
  } finally {cleanup();}
});
