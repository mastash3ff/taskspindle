import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readGooglePlayEvidence } from '../google.mjs';
import { normalize } from '../extractors.mjs';

const target='com.google.android.apps.subscriptions.red';
const node = (attrs={},text='',hidden=false) => ({innerText:text,getAttribute:key=>attrs[key] || null,
  getClientRects:()=>hidden?[]:[{}],closest:()=>null});
function row({id=target,text='Google One\nGoogle AI Pro (2 TB)\nCanceled\nEnds on October 9, 2026',ids=[id],manage=1,hidden=false}={}) {
  const element=node({},text,hidden);
  const anchors=ids.map(value=>node({href:`/store/apps/details?id=${value}`}));
  for(const anchor of anchors)anchor.closest=selector=>selector==='tr'?element:null;
  const buttons=Array.from({length:manage},()=>node({},'Manage'));
  element.querySelectorAll=selector=>selector==='a[href]'?anchors:buttons;
  return {element,anchors};
}
async function inspect({rows=[row()],labels=['Google Account: Example Person (alice@example.com)'],url='https://play.google.com/store/account/subscriptions',password=false,challenge=false,title='',body=''}={}) {
  globalThis.location=new URL(url);
  const accounts=labels.map(label=>node({'aria-label':label}));
  globalThis.document={title,body:{innerText:body},querySelectorAll:selector=>{
    if(selector==='a[href]')return rows.flatMap(item=>item.anchors);
    if(selector==='[aria-label^="Google Account:"]')return accounts;
    if(selector==='input[type="password"]')return password?[node()]:[];
    if(selector.startsWith('iframe'))return challenge?[node()]:[];
    return [];
  }};
  try {return await readGooglePlayEvidence();}
  finally {delete globalThis.location;delete globalThis.document;}
}
test('Google One exact product row yields unsupported Play channel with no billing dates or raw identity',async()=>{
  const result=await inspect();
  assert.deepEqual(result,{
    account_id:createHash('sha256').update('taskspindle:subscription:v1:google_ai:alice@example.com').digest('hex'),
    account_label:'a***@e***.com',billing_channel:'google_play',plan:'Google AI Pro (2 TB)',billing:null,
  });
  assert.equal(normalize('google_ai',result,null,'UTC').error.code,'UNSUPPORTED_BILLING_CHANNEL');
  for(const value of ['alice@example.com','Example Person','October','2026','Canceled'])assert.equal(JSON.stringify(result).includes(value),false);
});
test('unrelated expired Pro application and arbitrary body text are excluded',async()=>{
  const other=row({id:'example.unrelated.pro',text:'Other Pro\nGoogle AI Ultra\nExpired\nEnds on January 1, 2020'});
  const result=await inspect({rows:[other,row()],body:'Google AI Ultra\nEmail\nattacker@example.com\nNext payment October 10, 2026'});
  assert.equal(result.plan,'Google AI Pro (2 TB)');assert.equal(result.billing,null);
  assert.deepEqual(await inspect({rows:[other]}),{});
  assert.deepEqual(await inspect({rows:[row({text:'Google One\n2 TB storage\nCanceled'})]}),{});
});
test('only exact HTTPS Play subscriptions route is accepted and known auth requires login',async()=>{
  for(const url of ['http://play.google.com/store/account/subscriptions','https://other.example/store/account/subscriptions','https://play.google.com/store/account/subscriptions/other','https://play.google.com/store/account/subscriptions/','https://play.google.com/store/account'])assert.deepEqual(await inspect({url}),{});
  assert.deepEqual(await inspect({url:'https://accounts.google.com/v3/signin/identifier'}),{auth_required:true});
  assert.deepEqual(await inspect({password:true}),{auth_required:true});
  assert.deepEqual(await inspect({challenge:true,title:'Verify you are human'}),{auth_required:true});
  assert.equal((await inspect({challenge:true,title:'Google Play'})).billing_channel,'google_play');
});
test('multiple target rows, mixed app links, hidden rows, and ambiguous Manage controls fail closed',async()=>{
  for(const rows of [[row(),row()],[row({ids:[target,'example.other']})],[row({ids:[target,target]})],[row({hidden:true})],[row({manage:0})],[row({manage:2})]])assert.deepEqual(await inspect({rows}),{});
  const orphan=row();orphan.anchors[0].closest=()=>null;
  assert.deepEqual(await inspect({rows:[orphan]}),{});
});
test('single recognized Google AI plan is mandatory within the target row',async()=>{
  for(const text of ['Google AI Pro\nGoogle AI Ultra','Google AI Pro\nGoogle AI Pro','Other Pro','Google One Premium','Free','alice@example.com'])assert.deepEqual(await inspect({rows:[row({text})]}),{});
  for(const plan of ['Google AI Pro (2 TB)','Google AI Ultra','Google AI Plus (200 GB)','AI Premium'])assert.equal((await inspect({rows:[row({text:plan})]})).plan,plan);
});
test('only a consistent Google Account label establishes identity',async()=>{
  for(const labels of [[],['Google Account: no email'],['Google Account: alice@example.com bob@example.com'],['Google Account: alice@example.com','Google Account: bob@example.com']])assert.deepEqual(await inspect({labels,body:'alice@example.com'}),{});
  assert.equal((await inspect({labels:['Google Account: ALICE@example.com','Google Account: alice@example.com']})).account_label,'a***@e***.com');
});
test('lookalike app origins, IDs, and duplicate id parameters cannot establish Google One',async()=>{
  for(const href of ['https://other.example/store/apps/details?id='+target,'/store/apps/details?id='+target+'.other','/store/apps/details?id='+target+'&id=other','/store/apps/details/other?id='+target]){
    const candidate=row();candidate.anchors[0].getAttribute=()=>href;
    assert.deepEqual(await inspect({rows:[candidate]}),{});
  }
});
