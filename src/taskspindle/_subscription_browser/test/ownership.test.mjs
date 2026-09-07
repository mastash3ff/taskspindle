import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { acquireProfileLock, releaseProfileLock, processIdentity } from '../ownership.mjs';

test('active lock is retained and changed ownership is not released', async () => {
  const dir = await mkdtemp(path.join(tmpdir(),'subscription-lock-'));
  try {
    const operationNonce = '11111111-1111-4111-8111-111111111111';
    const lock = await acquireProfileLock(dir, operationNonce);
    assert.equal(JSON.parse(await readFile(lock.filename,'utf8')).operation_nonce,operationNonce);
    await assert.rejects(acquireProfileLock(dir));
    assert.equal(JSON.parse(await readFile(lock.filename,'utf8')).nonce,lock.owner.nonce);
    await writeFile(lock.filename,JSON.stringify({...lock.owner,nonce:'replacement'}));
    await releaseProfileLock(lock);
    assert.equal(JSON.parse(await readFile(lock.filename,'utf8')).nonce,'replacement');
  } finally { await rm(dir,{recursive:true,force:true}); }
});
test('interrupted owner is recovered on restart using OS PID/start identity', async () => {
  const dir = await mkdtemp(path.join(tmpdir(),'subscription-restart-'));
  const moduleURL = new URL('../ownership.mjs',import.meta.url).href;
  const child = spawn(process.execPath,['--input-type=module','-e',`import {acquireProfileLock} from ${JSON.stringify(moduleURL)}; await acquireProfileLock(${JSON.stringify(dir)}); process.stdout.write('ready'); setInterval(()=>{},1000);`],{stdio:['ignore','pipe','pipe']});
  try {
    const [data] = await once(child.stdout,'data'); assert.equal(String(data),'ready');
    const prior = JSON.parse(await readFile(path.join(dir,'.taskspindle-collector.lock'),'utf8'));
    assert.equal(await processIdentity(child.pid),prior.start);
    const exited = once(child,'exit'); child.kill('SIGKILL'); await exited;
    const replacement = await acquireProfileLock(dir);
    assert.equal(replacement.owner.pid,process.pid);
    assert.notEqual(replacement.owner.nonce,prior.nonce);
    await releaseProfileLock(replacement);
  } finally { child.kill(); await rm(dir,{recursive:true,force:true}); }
});
test('unparseable legacy lock is never presumed abandoned', async () => {
  const dir = await mkdtemp(path.join(tmpdir(),'subscription-unknown-'));
  try {
    await writeFile(path.join(dir,'.taskspindle-collector.lock'),'');
    await assert.rejects(acquireProfileLock(dir));
    assert.equal(await readFile(path.join(dir,'.taskspindle-collector.lock'),'utf8'),'');
  } finally { await rm(dir,{recursive:true,force:true}); }
});
