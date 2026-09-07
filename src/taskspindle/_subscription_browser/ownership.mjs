import { open, readFile, unlink } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID } from 'node:crypto';
import path from 'node:path';

const exec = promisify(execFile);

// Returns null only after OS evidence the PID is absent. Unknown identity throws
// and must never authorize stale-lock removal. Never reads process command lines.
export async function processIdentity(pid) {
  if (!Number.isSafeInteger(pid) || pid < 1) throw new Error('invalid owner');
  if (process.platform === 'win32') {
    const script = `$ErrorActionPreference='Stop'; $p=Get-Process -Id ${pid} -ErrorAction SilentlyContinue; if ($null -eq $p) { 'missing' } else { $p.StartTime.ToUniversalTime().Ticks.ToString() }`;
    const { stdout } = await exec('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], { timeout: 5000, windowsHide: true, maxBuffer: 1024 });
    const value = stdout.trim();
    if (value === 'missing') return null;
    if (!/^\d+$/.test(value)) throw new Error('unknown owner');
    return `windows:${value}`;
  }
  if (process.platform === 'linux') {
    try {
      const stat = await readFile(`/proc/${pid}/stat`, 'utf8');
      const fields = stat.slice(stat.lastIndexOf(')') + 2).trim().split(/\s+/);
      if (fields[0] === 'Z') return null;
      const boot = (await readFile('/proc/sys/kernel/random/boot_id', 'utf8')).trim();
      if (!/^\d+$/.test(fields[19])) throw new Error('unknown owner');
      return `linux:${boot}:${fields[19]}`;
    } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  }
  throw new Error('unsupported owner platform');
}

async function readOwner(filename) {
  const raw = await readFile(filename, 'utf8');
  const owner = JSON.parse(raw);
  if (!Number.isSafeInteger(owner.pid) || owner.pid < 1 || typeof owner.start !== 'string' || typeof owner.nonce !== 'string') throw new Error('unknown owner');
  return owner;
}

export async function acquireProfileLock(profileDir, operationNonce = null) {
  const filename = path.join(profileDir, '.taskspindle-collector.lock');
  const start = await processIdentity(process.pid);
  if (!start) throw new Error('unknown owner');
  const owner = { pid: process.pid, start, nonce: randomUUID(), operation_nonce: operationNonce };
  const create = async () => {
    const handle = await open(filename, 'wx', 0o600);
    try { await handle.writeFile(JSON.stringify(owner)); await handle.sync(); }
    finally { await handle.close(); }
    return { filename, owner };
  };
  try { return await create(); } catch (error) { if (error.code !== 'EEXIST') throw error; }
  const prior = await readOwner(filename);
  if (await processIdentity(prior.pid) === prior.start) throw new Error('active owner');
  // Serialize stale recovery itself so two simultaneous collectors cannot unlink
  // a newly acquired lock. If killed in this short recovery critical section,
  // conservatively leave the recovery guard for explicit operator inspection.
  const guardPath = `${filename}.recovery`;
  const guard = await open(guardPath, 'wx', 0o600);
  try {
    await guard.writeFile(JSON.stringify(owner));
    const current = await readOwner(filename);
    if (current.nonce !== prior.nonce || await processIdentity(current.pid) === current.start) throw new Error('owner changed');
    await unlink(filename);
    return await create();
  } finally { await guard.close(); await unlink(guardPath); }
}

export async function releaseProfileLock(lock) {
  // A replaced lock is never deleted by a previous owner.
  const current = await readOwner(lock.filename);
  if (current.nonce === lock.owner.nonce) await unlink(lock.filename);
}
