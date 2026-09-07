import { access, mkdir, open, readdir, unlink } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { validateRequest } from './helper.mjs';
import { acquireProfileLock, releaseProfileLock } from './ownership.mjs';
import { COLLECTION_URLS, failure, normalize } from './extractors.mjs';
import { installCapture } from './page.mjs';
import { installClaudeCapture } from './claude.mjs';
import { collectEvidence, GOOGLE_PLAY_SUBSCRIPTIONS_URL, readGoogleOneScope } from './collection.mjs';

export const EXTENSION_ID = 'mmlmfjhmonkocbjadbfplnigmagldckm';
const exec = promisify(execFile);
const require = createRequire(import.meta.url);
const codedError = code => Object.assign(new Error(code), { code });
const RELAY_CONNECT_TIMEOUT_MS = 30_000;
const RELAY_CONNECT_MARGIN_MS = 2_000;
// Pinned Playwright 1.63.0's extension relay uses this exact, non-sensitive
// pairing failure. Keep it bounded so arbitrary provider/browser text never
// changes the public error classification.
const RELAY_PAIRING_TIMEOUT = /^Playwright extension did not connect within \d+(?:\.\d+)?s after opening the connect page\. Make sure the extension is installed in the Chrome profile(?: "[^"\r\n]{1,128}")? and PLAYWRIGHT_MCP_EXTENSION_TOKEN matches its token\.$/;

export function validateNormalRequest(request) {
  return validateRequest(request) && typeof request.chrome_profile === 'string' && /^(?:Default|Profile [1-9]\d*)$/.test(request.chrome_profile) &&
    typeof request.operation_nonce === 'string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(request.operation_nonce);
}
export const cancelFile = request => path.join(request.profile_dir, `.taskspindle-cancel-${request.operation_nonce}`);
export const validToken = token => typeof token === 'string' && /^[\x21-\x7e]{1,512}$/.test(token);

export async function preflight(request, token) {
  if (!validToken(token)) return 'SETUP_REQUIRED';
  // Read extension-install metadata only. Never open Cookies, Login Data, Local
  // State, Preferences, or any browser/CLI authentication database.
  let userData;
  if (process.platform === 'win32' && process.env.LOCALAPPDATA) userData = path.join(process.env.LOCALAPPDATA, 'Google', 'Chrome', 'User Data');
  else if (process.platform === 'linux') userData = path.join(os.homedir(), '.config', 'google-chrome');
  else return 'BROWSER_UNAVAILABLE';
  try {
    const versions = await readdir(path.join(userData, request.chrome_profile, 'Extensions', EXTENSION_ID));
    if (!versions.length) return 'SETUP_REQUIRED';
  } catch { return 'SETUP_REQUIRED'; }
  // Refresh never launches a stopped browser. Connect opening Chrome is the
  // runtime's explicit user-facing action, so it also waits for an existing one.
  const startupDeadline = Date.now() + (request.action === 'connect' ? Math.min(5000, request.timeout_s * 1000) : 0);
  do { try {
    if (process.platform === 'win32') {
      const { stdout } = await exec('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', "if (Get-Process -Name chrome -ErrorAction SilentlyContinue) { 'running' } else { 'stopped' }"], { timeout: 5000, windowsHide: true, maxBuffer: 1024 });
      if (stdout.trim() !== 'running') throw codedError('BROWSER_UNAVAILABLE');
    } else await exec('pgrep', ['-x', 'chrome'], { timeout: 5000, maxBuffer: 32768 });
    return null;
  } catch {
    if (Date.now() >= startupDeadline) return 'BROWSER_UNAVAILABLE';
    await new Promise(resolve => setTimeout(resolve, 250));
  } } while (Date.now() < startupDeadline);
  return 'BROWSER_UNAVAILABLE';
}

// Deliberately version-bound exported Playwright internals: this uses the same
// official extension/CDP factory as `playwright-core mcp --extension`, without
// constructing BrowserBackend (which writes raw console artifacts).
export async function connectExtension(request) {
  if (require('playwright-core/package.json').version !== '1.63.0') throw codedError('SETUP_REQUIRED');
  delete process.env.DEBUG; delete process.env.PWDEBUG;
  for (const key of Object.keys(process.env)) if (key.startsWith('PWTEST_')) delete process.env[key];
  const { tools } = await import('playwright-core/lib/coreBundle');
  if (typeof tools.resolveCLIConfigForMCP !== 'function' || typeof tools.createBrowserWithInfo !== 'function') throw codedError('SETUP_REQUIRED');
  const options = { extension: true, browser: 'chrome', executablePath: request.chrome_path, profileDirName: request.chrome_profile, snapshotMode: 'none', imageResponses: 'omit', codegen: 'none' };
  const config = await tools.resolveCLIConfigForMCP(options, {});
  let connection;
  try { connection = await tools.createBrowserWithInfo(config, { clientName: 'TaskSpindle subscriptions', cwd: request.profile_dir }, options); }
  catch (error) {
    // Invalid/rotated tokens never fall back to consent in the official extension.
    // They leave its handshake unfulfilled; only the safe setup code leaves here.
    throw codedError(extensionFailureCode(error));
  }
  if (connection.ownership !== 'attached') throw codedError('BROWSER_UNAVAILABLE');
  return connection.browser;
}

export function extensionFailureCode(error) {
  const message = typeof error?.message === 'string' ? error.message : '';
  return RELAY_PAIRING_TIMEOUT.test(message) || /Extension not found/i.test(message)
    ? 'SETUP_REQUIRED' : 'BROWSER_UNAVAILABLE';
}

export function bounded(operation, signal, timeout) {
  const pending = Promise.resolve(operation);
  return new Promise((resolve, reject) => {
    let timer;
    const finish = (fn, value) => { clearTimeout(timer); signal?.removeEventListener('abort', abort); fn(value); };
    const abort = () => finish(reject, codedError('TIMEOUT'));
    // Observe an already-started operation even when the signal is already
    // aborted; a later rejection must never become unhandled/raw stderr.
    pending.then(value => finish(resolve, value), error => finish(reject, error));
    if (signal?.aborted) { abort(); return; }
    signal?.addEventListener('abort', abort, { once: true });
    timer = setTimeout(() => finish(reject, codedError('TIMEOUT')), timeout);
  });
}

export function extensionConnectBudget(remainingMs) {
  // Give the pinned relay a small scheduling margin beyond its own 30-second
  // handshake timeout, but never exceed the operation's remaining deadline.
  return Math.min(remainingMs, RELAY_CONNECT_TIMEOUT_MS + RELAY_CONNECT_MARGIN_MS);
}

export async function runNormal(request, dependencies = {}) {
  if (!validateNormalRequest(request)) return failure('INVALID_REQUEST');
  const token = dependencies.token ?? process.env.PLAYWRIGHT_MCP_EXTENSION_TOKEN;
  // Missing pairing is always a setup error, even with an injected preflight.
  if (!validToken(token)) return failure('SETUP_REQUIRED');
  const preliminary = await (dependencies.preflight || preflight)(request, token).catch(() => 'BROWSER_UNAVAILABLE');
  if (preliminary) return failure(preliminary);
  let lock;
  let ownedPage;
  let pendingOwnedPage;
  let ownedClose;
  let finishing = false;
  let cleanupFailed = false;
  let poll;
  let deadlineTimer;
  let result = failure('BROWSER_UNAVAILABLE');
  const controller = new AbortController();
  const closeOwned = page => {
    ownedClose ??= bounded(page.isClosed() ? Promise.resolve() : page.close({ runBeforeUnload: false }), null, 2000).catch(() => { cleanupFailed = true; });
    return ownedClose;
  };
  const stop = () => controller.abort();
  const externalSignal = dependencies.signal;
  if (externalSignal?.aborted) stop();
  externalSignal?.addEventListener('abort', stop, { once: true });
  try {
    await mkdir(request.profile_dir, { recursive: true, mode: 0o700 });
    const entries = await readdir(request.profile_dir);
    if (entries.length && !entries.includes('.taskspindle-subscription-profile')) return failure('INVALID_REQUEST');
    const marker = await open(path.join(request.profile_dir, '.taskspindle-subscription-profile'), 'a', 0o600); await marker.close();
    try { lock = await acquireProfileLock(request.profile_dir, request.operation_nonce); } catch { return failure('PROFILE_BUSY'); }
    poll = setInterval(() => { void access(cancelFile(request)).then(stop, () => {}); }, 100);
    const deadline = Date.now() + request.timeout_s * 1000;
    deadlineTimer = setTimeout(stop, request.timeout_s * 1000);
    const remaining = () => Math.max(1, deadline - Date.now());
    const browser = await bounded((dependencies.connect || connectExtension)(request), controller.signal, extensionConnectBudget(remaining()));
    const context = browser.contexts()[0];
    if (!context) throw codedError('BROWSER_UNAVAILABLE');
    // Never enumerate, navigate, change scripts in, or close preexisting pages.
    pendingOwnedPage = Promise.resolve(context.newPage()).then(page => {
      ownedPage = page;
      if (finishing || controller.signal.aborted) void closeOwned(page);
      return page;
    });
    ownedPage = await bounded(pendingOwnedPage, controller.signal, remaining());
    await bounded(ownedPage.addInitScript(installCapture), controller.signal, remaining());
    if (request.provider === 'claude') await bounded(ownedPage.addInitScript(installClaudeCapture), controller.signal, remaining());
    await bounded(ownedPage.goto(COLLECTION_URLS[request.provider], { waitUntil: 'domcontentloaded', timeout: Math.min(30000, remaining()) }), controller.signal, remaining()).catch(error => { if (controller.signal.aborted) throw error; });
    result = failure('PARSE_CHANGED');
    let googlePlayAttempted = false;
    let googleOneAccountId = null;
    while (!controller.signal.aborted && remaining() > 1) {
      const evidence = await bounded(collectEvidence(ownedPage, request.provider), controller.signal, Math.min(5000, remaining())).catch(error => { if (controller.signal.aborted) throw error; return null; });
      result = normalize(request.provider, evidence, request.expected_account_id || googleOneAccountId, request.timezone);
      if (result.ok || ['ACCOUNT_MISMATCH', 'UNSUPPORTED_BILLING_CHANNEL'].includes(result.error.code)) break;
      if (request.provider === 'google_ai' && !googlePlayAttempted &&
          result.error.code === 'PARSE_CHANGED') {
        const scope = await bounded(
          ownedPage.evaluate(readGoogleOneScope), controller.signal, Math.min(5000, remaining()),
        ).catch(error => { if (controller.signal.aborted) throw error; return null; });
        if (/^[a-f0-9]{64}$/.test(scope?.account_id || '')) {
          googlePlayAttempted = true;
          googleOneAccountId = scope.account_id;
          if (request.expected_account_id && request.expected_account_id !== googleOneAccountId) {
            result = failure('ACCOUNT_MISMATCH');
            break;
          }
          await bounded(ownedPage.goto(GOOGLE_PLAY_SUBSCRIPTIONS_URL, {
            waitUntil: 'domcontentloaded', timeout: Math.min(30000, remaining()),
          }), controller.signal, remaining()).catch(error => { if (controller.signal.aborted) throw error; });
          continue;
        }
      }
      if (request.action === 'refresh' && result.error.code === 'AUTH_REQUIRED') break;
      if (ownedPage.isClosed()) break;
      await bounded(new Promise(resolve => setTimeout(resolve, 250)), controller.signal, remaining());
    }
  } catch (error) {
    result = failure(['SETUP_REQUIRED', 'TIMEOUT'].includes(error.code) ? error.code : 'BROWSER_UNAVAILABLE');
  } finally {
    finishing = true;
    clearInterval(poll); clearTimeout(deadlineTimer);
    externalSignal?.removeEventListener('abort', stop);
    // A cancelled newPage RPC can still finish. Retain its ownership and give
    // late creation a bounded chance to finish and close before process exit.
    if (!ownedPage && pendingOwnedPage) await bounded(pendingOwnedPage, null, 2000).catch(() => { cleanupFailed = true; });
    if (ownedPage) await closeOwned(ownedPage);
    if (cleanupFailed) result = failure('BROWSER_UNAVAILABLE');
    if (lock) {
      await releaseProfileLock(lock).catch(() => {});
      await unlink(cancelFile(request)).catch(() => {});
    }
    // Do not call browser.close/context.close. The CLI process exits below,
    // dropping only its relay sockets; Chrome and all preexisting tabs survive.
  }
  return result;
}

async function main() {
  // This process has exactly one public output: the normalized JSON below.
  for (const name of ['log', 'info', 'warn', 'error', 'debug']) console[name] = () => {};
  const controller = new AbortController();
  process.once('SIGTERM', () => controller.abort()); process.once('SIGINT', () => controller.abort());
  let result;
  try {
    let input = '';
    for await (const chunk of process.stdin) { input += chunk; if (input.length > 16384) throw codedError('INVALID_REQUEST'); }
    result = await runNormal(JSON.parse(input), { signal: controller.signal });
  } catch { result = failure('INVALID_REQUEST'); }
  process.stdout.write(`${JSON.stringify(result)}\n`, () => process.exit(0));
}
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) await main();
