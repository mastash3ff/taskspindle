import test from 'node:test';
import assert from 'node:assert/strict';
import { parseChatGPT, normalize, validDate } from '../extractors.mjs';
import { validateRequest } from '../helper.mjs';
import { readEvidence } from '../page.mjs';

const identity = { account_id: 'a'.repeat(64), account_label: 'a***@e***.com', billing_channel: 'provider_web', plan: 'Pro' };
test('observed ChatGPT active_until and will_renew distinguish renewal from cancellation', () => {
  assert.deepEqual(parseChatGPT({active_until:'2026-10-09T12:30:00Z',will_renew:true}), { status:'renewing',renews_at:'2026-10-09T12:30:00.000Z',access_ends_at:null });
  assert.deepEqual(parseChatGPT({activeUntil:'2026-10-09T12:30:00Z',willRenew:false}), { status:'cancelled',renews_at:null,access_ends_at:'2026-10-09T12:30:00.000Z' });
});
test('unavailable metadata never becomes a free plan or inferred billing date', () => {
  assert.equal(parseChatGPT({active_until:null,will_renew:true}), null);
  assert.equal(parseChatGPT({active_until:'2026-10-09',will_renew:true}), null);
  assert.equal(parseChatGPT({active_until:'2026-10-09T12:30:00Z',will_renew:'false'}), null);
  assert.equal(parseChatGPT({active_until:'2026-10-09T12:30:00Z'}), null);
  assert.equal(parseChatGPT({expires_at:1791549000,quota_reset_at:'2026-10-09T12:30:00Z'}), null);
  assert.equal(parseChatGPT({active_until:null,will_renew:false}), null);
  assert.equal(parseChatGPT({active_until:null,will_renew:null}), null);
  assert.equal(validDate('2026-02-30'), null);
});
test('expired sessions, wrong account, and external billing fail closed', () => {
  assert.equal(normalize('claude',{auth_required:true},null,'UTC').error.code,'AUTH_REQUIRED');
  assert.equal(normalize('claude',identity,'b'.repeat(64),'UTC').error.code,'ACCOUNT_MISMATCH');
  for (const billing_channel of ['apple','google_play','x_premium']) assert.equal(normalize('grok',{...identity,billing_channel},null,'UTC').error.code,'UNSUPPORTED_BILLING_CHANNEL');
  assert.equal(normalize('google_ai',{...identity,billing_channel:'unknown'},null,'UTC').error.code,'PARSE_CHANGED');
});
test('only subscription dates appear in safe observation', () => {
  const result = normalize('chatgpt', {...identity,subscription:{active_until:'2026-10-09T12:30:00Z',will_renew:false,access_token:'DO_NOT_COPY'}},null,'America/Chicago');
  assert.equal(result.ok,true);
  assert.equal(result.observation.status,'cancelled');
  assert.equal(result.observation.renews_at,null);
  assert.equal(result.observation.source_url,'https://chatgpt.com/');
  assert.equal(JSON.stringify(result).includes('DO_NOT_COPY'),false);
});
test('ChatGPT needs a recognized plan and meaningful nonempty metadata', () => {
  const subscription = {active_until:'2026-10-09T12:30:00Z',will_renew:true};
  for (const plan of [null, 'Unrecognized', 'SuperGrok', 'alice@example.com']) assert.equal(normalize('chatgpt',{...identity,plan,subscription},null,'UTC').error.code,'PARSE_CHANGED');
  for (const will_renew of [null,false,true]) assert.equal(normalize('chatgpt',{...identity,subscription:{active_until:null,will_renew}},null,'UTC').error.code,'PARSE_CHANGED');
});
test('request rejects normal Chrome profiles and refresh without account binding', () => {
  const request = {provider:'claude',action:'connect',profile_dir:'/tmp/subscriptions/profiles/claude',chrome_path:'/usr/bin/google-chrome',expected_account_id:null,timeout_s:30,timezone:'UTC'};
  assert.equal(validateRequest(request),true);
  assert.equal(validateRequest({...request,profile_dir:'/home/me/.config/google-chrome/Default'}),false);
  assert.equal(validateRequest({...request,action:'refresh'}),false);
  assert.equal(validateRequest({...request,timeout_s:Infinity}),false);
  assert.equal(validateRequest({...request,operation_nonce:'11111111-1111-4111-8111-111111111111'}),true);
  assert.equal(validateRequest({...request,operation_nonce:'not-an-invocation-id'}),false);
});

// Synthetic contract examples, NOT snapshots of signed-in vendor pages. They
// exercise the narrowly labeled DOM fallback without implying live validation.
async function dom(provider, text, extra = {}) {
  const urls = {chatgpt:'https://chatgpt.com/#settings/Billing',claude:'https://claude.ai/settings/billing',google_ai:'https://one.google.com/settings',grok:'https://grok.com/?_s=billing'};
  globalThis.window = {};
  globalThis.location = new URL(extra.pageURL || urls[provider]);
  const root = {
    innerText: text, getClientRects:()=>[{}], closest:()=>null, contains:()=>false,
    getAttribute:name=>name === 'aria-label' ? 'Billing' : null,
    querySelector:()=>null, querySelectorAll:()=>[],
  };
  globalThis.document = {body:{innerText:text},title:'',querySelector:()=>null,querySelectorAll:selector=>selector.includes('dialog')?[root]:[],...extra};
  try { return await readEvidence(provider); }
  finally { delete globalThis.window; delete globalThis.location; delete globalThis.document; }
}
test('Claude labeled renewal preserves date-only precision and ignores usage reset', async () => {
  const evidence = await dom('claude','Email\nalice@example.com\nPro\nPayment method\nNext billing date\nOctober 9, 2026\nUsage resets\nSeptember 10, 2026');
  const result = normalize('claude',evidence,null,'UTC');
  assert.equal(result.ok,true);
  assert.equal(result.observation.renews_at,'2026-10-09');
  assert.equal(result.observation.date_precision,'date');
  assert.equal(result.observation.account_label,'a***@e***.com');
  assert.equal(JSON.stringify(result).includes('alice@example.com'),false);
});
test('Google AI canceled fixture and Grok renewing fixture stay product scoped', async () => {
  const google = await dom('google_ai','Email\nalice@example.com\nGoogle AI Pro\nPayment method\nCancelled\nAccess ends on October 9, 2026');
  assert.equal(normalize('google_ai',google,null,'UTC').observation.access_ends_at,'2026-10-09');
  const grok = await dom('grok','Email\nalice@example.com\nSuperGrok\nBilling details\nRenews on October 9, 2026');
  assert.equal(normalize('grok',grok,null,'UTC').observation.status,'renewing');
});
test('quota/reset/creation/token labels cannot supply billing dates', async () => {
  const evidence = await dom('claude','Email\nalice@example.com\nMax\nPayment method\nCreated October 9, 2026\nToken expires on October 9, 2026\nUsage resets October 9, 2026');
  assert.equal(normalize('claude',evidence,null,'UTC').error.code,'PARSE_CHANGED');
});
test('billing ambiguity and unlabeled email are rejected', async () => {
  const evidence = await dom('grok','alice@example.com\nSuperGrok\nBilling details\nRenews on October 9, 2026');
  assert.equal(normalize('grok',evidence,null,'UTC').error.code,'PARSE_CHANGED');
  const duplicate = await dom('claude','Email\nalice@example.com\nPro\nPayment method\nRenews on October 9, 2026\nRenews on October 10, 2026');
  assert.equal(normalize('claude',duplicate,null,'UTC').error.code,'PARSE_CHANGED');
});
test('app store billing indicator rejects collection', async () => {
  const evidence = await dom('grok','Email\nalice@example.com\nSuperGrok\nSubscription managed through Apple App Store\nRenews on October 9, 2026');
  assert.equal(normalize('grok',evidence,null,'UTC').error.code,'UNSUPPORTED_BILLING_CHANNEL');
});
test('DOM dates reject calendar rollovers in both supported word orders', async () => {
  for (const date of ['February 30, 2026','30 February 2026','April 31, 2026','February 29, 2027']) {
    const evidence = await dom('claude',`Email\nalice@example.com\nPro\nPayment method\nRenews on ${date}`);
    assert.equal(normalize('claude',evidence,null,'UTC').error.code,'PARSE_CHANGED');
  }
  const leap = await dom('claude','Email\nalice@example.com\nPro\nPayment method\nRenews on February 29, 2028');
  assert.equal(normalize('claude',leap,null,'UTC').observation.renews_at,'2028-02-29');
});
test('Grok login redirects and recognizable challenge UI require authentication', async () => {
  for (const url of ['https://x.com/i/flow/login','https://auth.x.ai/authorize','https://accounts.x.ai/']) {
    globalThis.window = {}; globalThis.location = new URL(url);
    globalThis.document = {body:{innerText:''},querySelector:()=>null};
    assert.equal((await readEvidence('grok')).auth_required,true);
    delete globalThis.window; delete globalThis.location; delete globalThis.document;
  }
  const challenge = await dom('grok','Verify you are human',{title:'Verify you are human',querySelector: selector => selector.includes('iframe') ? {} : null});
  assert.equal(challenge.auth_required,true);
});
test('explicit free and no-subscription labels do not require payment controls', async () => {
  const free = await dom('chatgpt','Email\nalice@example.com\nChatGPT Free\nSubscription status\nFree');
  const freeResult = normalize('chatgpt',free,null,'UTC');
  assert.equal(freeResult.observation.status,'free');
  assert.equal(freeResult.observation.renews_at,null);
  assert.equal(freeResult.observation.access_ends_at,null);
  for (const provider of ['claude','grok','google_ai']) {
    const label = provider === 'google_ai' ? 'Google AI subscription status' : 'Subscription status';
    const none = await dom(provider,`Email\nalice@example.com\n${label}\nNo subscription`);
    const result = normalize(provider,none,null,'UTC');
    assert.equal(result.observation.status,'none');
    assert.equal(result.observation.plan,'None');
    assert.equal(result.observation.access_ends_at,null);
  }
});
test('expired must be displayed explicitly, never inferred from an old date', async () => {
  const expired = await dom('claude','Email\nalice@example.com\nPro\nPayment method\nSubscription status\nExpired');
  assert.equal(normalize('claude',expired,null,'UTC').observation.status,'expired');
  assert.equal(normalize('claude',expired,null,'UTC').observation.access_ends_at,null);
  const dated = await dom('claude','Email\nalice@example.com\nPro\nPayment method\nSubscription status\nExpired\nAccess ends on September 1, 2026');
  assert.equal(normalize('claude',dated,null,'UTC').observation.access_ends_at,'2026-09-01');
  const past = await dom('claude','Email\nalice@example.com\nPro\nPayment method\nCancelled\nAccess ends on September 1, 2026');
  assert.equal(normalize('claude',past,null,'UTC').observation.status,'cancelled');
});
test('ambiguous absence, generic Google storage status, and external free channel fail closed', async () => {
  const missing = await dom('chatgpt','Email\nalice@example.com\nChatGPT Free');
  assert.equal(normalize('chatgpt',missing,null,'UTC').error.code,'PARSE_CHANGED');
  const storage = await dom('google_ai','Email\nalice@example.com\nSubscription status\nNo subscription');
  assert.equal(normalize('google_ai',storage,null,'UTC').error.code,'PARSE_CHANGED');
  const external = await dom('grok','Email\nalice@example.com\nFree\nSubscription status\nFree\nSubscription managed through Apple App Store');
  assert.equal(normalize('grok',external,null,'UTC').error.code,'UNSUPPORTED_BILLING_CHANNEL');
});
test('settings hash plus chat text cannot establish billing without a scoped root', async () => {
  const spoof = 'Email\nattacker@example.com\nChatGPT Pro\nPayment method\nRenews on October 9, 2026';
  const evidence = await dom('chatgpt',spoof,{querySelectorAll:()=>[]});
  assert.equal(evidence.account_id,undefined);
  assert.equal(normalize('chatgpt',evidence,null,'UTC').error.code,'PARSE_CHANGED');
});
test('unrelated chat identity and dates underneath a settings dialog are ignored', async () => {
  const spoof = 'Email\nattacker@example.com\nChatGPT Pro\nPayment method\nRenews on October 9, 2026';
  const noIdentity = await dom('chatgpt','ChatGPT Pro\nPayment method\nRenews on October 10, 2026',{body:{innerText:spoof}});
  assert.equal(noIdentity.account_id,null);
  assert.equal(normalize('chatgpt',noIdentity,null,'UTC').error.code,'PARSE_CHANGED');
  const noDate = await dom('chatgpt','Email\nalice@example.com\nChatGPT Pro\nPayment method',{body:{innerText:spoof}});
  assert.equal(noDate.billing,null);
  assert.equal(normalize('chatgpt',noDate,null,'UTC').error.code,'PARSE_CHANGED');
  const real = await dom('chatgpt','Email\nalice@example.com\nChatGPT Pro\nPayment method\nRenews on October 10, 2026',{body:{innerText:spoof}});
  assert.equal(normalize('chatgpt',real,null,'UTC').observation.renews_at,'2026-10-10');
});
test('a purported settings container containing conversation content is rejected', async () => {
  const root = {innerText:'Email\nattacker@example.com\nChatGPT Free\nSubscription status\nFree',getClientRects:()=>[{}],closest:()=>null,querySelector:()=>({})};
  const evidence = await dom('chatgpt','',{querySelectorAll:selector=>selector.includes('dialog')?[root]:[]});
  assert.equal(normalize('chatgpt',evidence,null,'UTC').error.code,'PARSE_CHANGED');
});
test('logged-out ChatGPT home with real navigation login control requires auth', async () => {
  const login = {innerText:'Log in',getClientRects:()=>[{}],closest:()=>null};
  const evidence = await dom('chatgpt','',{querySelectorAll:selector=>selector.includes('login-button')?[login]:[]});
  assert.equal(evidence.auth_required,true);
  const plainHome = await dom('chatgpt','',{pageURL:'https://chatgpt.com/',querySelectorAll:selector=>selector.includes('login-button')?[login]:[]});
  assert.equal(plainHome.auth_required,true);
  const chatLogin = {...login,closest:selector=>selector.includes('data-message-author-role')?{}:null};
  const ignored = await dom('chatgpt','',{querySelectorAll:selector=>selector.includes('login-button')?[chatLogin]:[]});
  assert.equal(ignored.auth_required,undefined);
  const empty = await dom('chatgpt','',{querySelectorAll:()=>[]});
  assert.equal(empty.auth_required,undefined);
});
