import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm, access, writeFile, readFile } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createRequire } from 'node:module';
import { runNormal, validateNormalRequest, cancelFile, validToken, bounded, extensionFailureCode, extensionConnectBudget } from '../normal-helper.mjs';
import { URLS } from '../extractors.mjs';
import { installCapture, readEvidence } from '../page.mjs';
import { readGooglePlayEvidence } from '../google.mjs';
import { GOOGLE_PLAY_SUBSCRIPTIONS_URL, readGoogleOneScope } from '../collection.mjs';
const require = createRequire(import.meta.url);
const token = 'synthetic-pairing-token-for-tests';
const account = 'a'.repeat(64);
const evidence = {account_id:account,account_label:'a***@e***.com',billing_channel:'provider_web',plan:'Pro',subscription:{active_until:'2026-10-09T12:30:00Z',will_renew:true}};

async function fixture(action = 'connect') {
  const base = await mkdtemp(path.join(os.tmpdir(),'subscription-normal-'));
  const request = {provider:'chatgpt',action,profile_dir:path.join(base,'profiles','chatgpt'),chrome_path:process.execPath,chrome_profile:'Default',expected_account_id:action === 'refresh'?account:null,timeout_s:5,timezone:'UTC',operation_nonce:'11111111-1111-4111-8111-111111111111'};
  const calls = [];
  let closed = false;
  const page = {addInitScript:async fn=>{assert.equal(fn,installCapture);calls.push('init');},goto:async url=>{assert.equal(url,URLS.chatgpt);calls.push('goto');},evaluate:async fn=>{assert.equal(fn,readEvidence);calls.push('read');return evidence;},isClosed:()=>closed,close:async()=>{closed=true;calls.push('close-owned');}};
  const context = {newPage:async()=>{calls.push('create-owned');return page;},pages:()=>{throw new Error('must not inspect preexisting pages');},close:()=>{throw new Error('must not close normal context');}};
  const browser = {contexts:()=>[context],close:()=>{throw new Error('must not close normal browser');}};
  const dependencies = {token,preflight:async()=>null,connect:async()=>{calls.push('connect');return browser;}};
  return {base,request,calls,page,context,dependencies,cleanup:()=>rm(base,{recursive:true,force:true})};
}
test('pinned official extension transport factory is available without MCP backend', async () => {
  assert.equal(require('playwright-core/package.json').version,'1.63.0');
  const {tools} = await import('playwright-core/lib/coreBundle');
  assert.equal(typeof tools.createBrowserWithInfo,'function');
  const options = {extension:true,browser:'chrome',profileDirName:'Default',snapshotMode:'none',codegen:'none',imageResponses:'omit'};
  const config = await tools.resolveCLIConfigForMCP(options,{});
  assert.equal(config.extension,true);
  assert.equal(config.snapshot.mode,'none');
  assert.equal(config.browser.userDataDir,undefined);
});
test('normal request binds a selected profile and invocation ownership', async () => {
  const f = await fixture();
  try {
    assert.equal(validateNormalRequest(f.request),true);
    assert.equal(validateNormalRequest({...f.request,chrome_profile:'../../Default'}),false);
    assert.equal(validateNormalRequest({...f.request,operation_nonce:null}),false);
  } finally {await f.cleanup();}
});
test('pairing token matches the runtime printable ASCII length contract', () => {
  assert.equal(validToken('x'),true);
  assert.equal(validToken('x'.repeat(512)),true);
  for (const token of ['', 'x'.repeat(513),'contains space','newline\n','nonascii-é']) assert.equal(validToken(token),false);
});
test('pinned relay pairing timeout maps safely while unrelated errors remain unavailable', async () => {
  const exact = 'Playwright extension did not connect within 30s after opening the connect page. Make sure the extension is installed in the Chrome profile "Default" and PLAYWRIGHT_MCP_EXTENSION_TOKEN matches its token.';
  assert.equal(extensionFailureCode(new Error(exact)),'SETUP_REQUIRED');
  const unrelated = new Error('untrusted provider failure');
  assert.equal(extensionFailureCode(unrelated),'BROWSER_UNAVAILABLE');
  assert.equal(JSON.stringify({error:'The dedicated subscription browser could not start.'}).includes(unrelated.message),false);
  assert.equal(extensionConnectBudget(60_000),32_000);
  assert.equal(extensionConnectBudget(30_000),30_000);
});
test('normal collection creates, reads and closes only its own tab', async () => {
  const f = await fixture();
  try {
    const result = await runNormal(f.request,f.dependencies);
    assert.equal(result.ok,true);
    assert.equal(result.observation.account_id,account);
    assert.deepEqual(f.calls,['connect','create-owned','init','goto','read','close-owned']);
    await assert.rejects(access(path.join(f.request.profile_dir,'.taskspindle-collector.lock')));
  } finally {await f.cleanup();}
});
test('missing pairing or unavailable extension/browser stops before attachment', async () => {
  const f = await fixture('refresh');
  try {
    assert.equal((await runNormal(f.request,{...f.dependencies,token:''})).error.code,'SETUP_REQUIRED');
    for (const code of ['SETUP_REQUIRED','BROWSER_UNAVAILABLE']) assert.equal((await runNormal(f.request,{...f.dependencies,preflight:async()=>code})).error.code,code);
    assert.deepEqual(f.calls,[]);
    await assert.rejects(access(f.request.profile_dir));
  } finally {await f.cleanup();}
});
test('refresh returns immediately on auth-required and closes owned tab', async () => {
  const f = await fixture('refresh');
  f.page.evaluate = async()=>({auth_required:true});
  try {
    const result = await runNormal(f.request,f.dependencies);
    assert.equal(result.error.code,'AUTH_REQUIRED');
    assert.equal(f.calls.at(-1),'close-owned');
  } finally {await f.cleanup();}
});
test('wrong account closes owned tab and fails closed', async () => {
  const f = await fixture('refresh');
  f.request.expected_account_id = 'b'.repeat(64);
  try {assert.equal((await runNormal(f.request,f.dependencies)).error.code,'ACCOUNT_MISMATCH');assert.equal(f.calls.at(-1),'close-owned');}
  finally {await f.cleanup();}
});
test('missing Google One details trigger one scoped fallback on the owned tab', async () => {
  const f = await fixture();
  f.request.provider = 'google_ai';
  f.request.profile_dir = path.join(f.base,'profiles','google_ai');
  let location = 'one';
  const play = {account_id:account,account_label:'a***@e***.com',billing_channel:'google_play',plan:'Google AI Pro',billing:null};
  f.page.goto = async url => {
    f.calls.push(url === GOOGLE_PLAY_SUBSCRIPTIONS_URL ? 'goto-play' : 'goto-one');
    location = url === GOOGLE_PLAY_SUBSCRIPTIONS_URL ? 'play' : 'one';
  };
  f.page.evaluate = async (fn, provider) => {
    if (fn === readEvidence) {
      assert.equal(provider,'google_ai');
      f.calls.push(`read-${location}`);
      return location === 'one'
        ? {account_id:account,account_label:'a***@e***.com',billing_channel:'unknown',plan:null,billing:null}
        : {};
    }
    if (fn === readGooglePlayEvidence) {
      f.calls.push(`read-play-${location}`);
      return location === 'play' ? play : {};
    }
    assert.equal(fn,readGoogleOneScope);
    f.calls.push('scope-one');
    return location === 'one' ? {account_id:account} : {};
  };
  try {
    const result = await runNormal(f.request,f.dependencies);
    assert.equal(result.error.code,'UNSUPPORTED_BILLING_CHANNEL');
    assert.deepEqual(f.calls,[
      'connect','create-owned','init','goto-one','read-one','read-play-one',
      'scope-one','goto-play','read-play','read-play-play','close-owned',
    ]);
  } finally {await f.cleanup();}
});
test('Google auth-required evidence never triggers Play fallback', async () => {
  const f = await fixture();
  f.request.provider = 'google_ai';
  f.request.profile_dir = path.join(f.base,'profiles','google_ai');
  f.page.isClosed = () => true;
  f.page.evaluate = async fn => {
    if (fn === readEvidence) return {auth_required:true};
    assert.equal(fn,readGooglePlayEvidence);
    return {};
  };
  try {
    const result = await runNormal(f.request,f.dependencies);
    assert.equal(result.error.code,'AUTH_REQUIRED');
    assert.equal(f.calls.includes('scope-one'),false);
    assert.equal(f.calls.includes('goto-play'),false);
  } finally {await f.cleanup();}
});
test('recognized Google One billing never navigates to Play', async () => {
  const f = await fixture();
  f.request.provider = 'google_ai';
  f.request.profile_dir = path.join(f.base,'profiles','google_ai');
  const google = {account_id:account,account_label:'a***@e***.com',billing_channel:'provider_web',plan:'Google AI Pro',billing:{status:'renewing',renews_at:'2026-10-09',access_ends_at:null}};
  f.page.evaluate = async (fn, provider) => {
    if (fn === readEvidence) {assert.equal(provider,'google_ai');return google;}
    assert.equal(fn,readGooglePlayEvidence);
    return {};
  };
  try {
    assert.equal((await runNormal(f.request,f.dependencies)).ok,true);
    assert.equal(f.calls.includes('goto-play'),false);
  } finally {await f.cleanup();}
});
test('Google fallback preserves account identity across the provider origins', async () => {
  const f = await fixture();
  f.request.provider = 'google_ai';
  f.request.profile_dir = path.join(f.base,'profiles','google_ai');
  let onPlay = false;
  f.page.goto = async url => {onPlay = url === GOOGLE_PLAY_SUBSCRIPTIONS_URL;};
  f.page.evaluate = async (fn, provider) => {
    if (fn === readEvidence) return onPlay ? {} : {account_id:account,account_label:'a***@e***.com',billing_channel:'unknown',plan:null,billing:null};
    if (fn === readGooglePlayEvidence) return onPlay
      ? {account_id:'b'.repeat(64),account_label:'b***@e***.com',billing_channel:'google_play',plan:'Google AI Pro',billing:null}
      : {};
    assert.equal(fn,readGoogleOneScope);
    return {account_id:account};
  };
  try {
    assert.equal((await runNormal(f.request,f.dependencies)).error.code,'ACCOUNT_MISMATCH');
  } finally {await f.cleanup();}
});
test('invocation cancel sentinel interrupts page work and removes only owned receipt', async () => {
  const f = await fixture('refresh');
  f.page.evaluate = async()=>{
    const receipt = JSON.parse(await readFile(path.join(f.request.profile_dir,'.taskspindle-collector.lock'),'utf8'));
    assert.equal(receipt.operation_nonce,f.request.operation_nonce);
    await writeFile(cancelFile(f.request),'');
    return new Promise(()=>{});
  };
  try {
    assert.equal((await runNormal(f.request,f.dependencies)).error.code,'TIMEOUT');
    assert.equal(f.calls.at(-1),'close-owned');
    await assert.rejects(access(cancelFile(f.request)));
    await assert.rejects(access(path.join(f.request.profile_dir,'.taskspindle-collector.lock')));
  } finally {await f.cleanup();}
});
test('untrusted exception text and pairing token never appear in results', async () => {
  const f = await fixture();
  try {
    const result = await runNormal(f.request,{...f.dependencies,connect:async()=>{throw new Error(`RAW_PAYMENT ${token}`);}});
    assert.equal(result.error.code,'BROWSER_UNAVAILABLE');
    assert.equal(JSON.stringify(result).includes(token),false);
    assert.equal(JSON.stringify(result).includes('RAW_PAYMENT'),false);
  } finally {await f.cleanup();}
});
test('busy normal helper never removes another invocation receipt', async () => {
  const f = await fixture();
  await mkdir(f.request.profile_dir,{recursive:true});
  await writeFile(path.join(f.request.profile_dir,'.taskspindle-subscription-profile'),'');
  await writeFile(path.join(f.request.profile_dir,'.taskspindle-collector.lock'),'unknown-owner');
  try {
    assert.equal((await runNormal(f.request,f.dependencies)).error.code,'PROFILE_BUSY');
    assert.equal(await readFile(path.join(f.request.profile_dir,'.taskspindle-collector.lock'),'utf8'),'unknown-owner');
    assert.deepEqual(f.calls,[]);
  } finally {await f.cleanup();}
});
test('already-aborted bounded operation observes a later rejection', async () => {
  const controller = new AbortController(); controller.abort();
  const unhandled = [];
  const listener = error => unhandled.push(error);
  process.on('unhandledRejection',listener);
  try {
    const operation = new Promise((resolve,reject)=>setTimeout(()=>reject(new Error('RAW_LATE_REJECTION')),20));
    await assert.rejects(bounded(operation,controller.signal,100),{code:'TIMEOUT'});
    await new Promise(resolve=>setTimeout(resolve,40));
    assert.deepEqual(unhandled,[]);
  } finally {process.off('unhandledRejection',listener);}
});
test('cancelled tab creation closes the late-created owned tab without navigation', async () => {
  const f = await fixture('refresh');
  const controller = new AbortController();
  f.context.newPage = async()=>{
    f.calls.push('create-pending');
    setTimeout(()=>controller.abort(),10);
    await new Promise(resolve=>setTimeout(resolve,80));
    f.calls.push('create-finished');
    return f.page;
  };
  try {
    const result = await runNormal(f.request,{...f.dependencies,signal:controller.signal});
    assert.equal(result.error.code,'TIMEOUT');
    assert.deepEqual(f.calls,['connect','create-pending','create-finished','close-owned']);
    await assert.rejects(access(path.join(f.request.profile_dir,'.taskspindle-collector.lock')));
  } finally {await f.cleanup();}
});
