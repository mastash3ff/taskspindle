import { mkdir, open, readdir } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { COLLECTION_URLS, URLS, failure, normalize } from './extractors.mjs';
import { installCapture } from './page.mjs';
import { installClaudeCapture } from './claude.mjs';
import { collectEvidence, GOOGLE_PLAY_SUBSCRIPTIONS_URL, readGoogleOneScope } from './collection.mjs';
import { acquireProfileLock, releaseProfileLock } from './ownership.mjs';

export function validateRequest(request) {
  if (!request || !Object.hasOwn(URLS, request.provider) || !['connect', 'refresh'].includes(request.action)) return false;
  if (typeof request.profile_dir !== 'string' || !path.isAbsolute(request.profile_dir) || typeof request.chrome_path !== 'string' || !path.isAbsolute(request.chrome_path)) return false;
  // This helper never accepts a normal Chrome profile or CLI credential path.
  if (path.basename(request.profile_dir) !== request.provider || path.basename(path.dirname(request.profile_dir)) !== 'profiles') return false;
  if (request.expected_account_id != null && !/^[a-f0-9]{64}$/.test(request.expected_account_id)) return false;
  if (request.operation_nonce != null && !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(request.operation_nonce)) return false;
  if (request.action === 'refresh' && !request.expected_account_id) return false;
  if (!Number.isFinite(request.timeout_s) || request.timeout_s < 1 || request.timeout_s > 900) return false;
  try { new Intl.DateTimeFormat('en', { timeZone: request.timezone }); } catch { return false; }
  return typeof request.timezone === 'string';
}

export async function run(request) {
  if (!validateRequest(request)) return failure('INVALID_REQUEST');
  let context;
  let lock;
  let timer;
  let timedOut = false;
  try {
    await mkdir(request.profile_dir, { recursive: true, mode: 0o700 });
    // Existing profiles must have been initialized by this dedicated helper.
    const markerPath = path.join(request.profile_dir, '.taskspindle-subscription-profile');
    const entries = await readdir(request.profile_dir);
    if (entries.length && !entries.includes('.taskspindle-subscription-profile')) return failure('INVALID_REQUEST');
    const marker = await open(markerPath, 'a', 0o600); await marker.close();
    try { lock = await acquireProfileLock(request.profile_dir, request.operation_nonce || null); } catch { return failure('PROFILE_BUSY'); }
    const { chromium } = await import('playwright-core');
    const deadline = Date.now() + request.timeout_s * 1000;
    context = await chromium.launchPersistentContext(request.profile_dir, {
      executablePath: request.chrome_path, headless: request.action === 'refresh',
      timeout: Math.min(request.timeout_s * 1000, 30000),
      viewport: null, locale: 'en-US', timezoneId: request.timezone,
      // Permit human login redirects/popups; never automate login or consent.
      ignoreDefaultArgs: ['--enable-automation'],
      args: ['--disable-session-crashed-bubble'],
    });
    timer = setTimeout(() => { timedOut = true; void context.close().catch(() => {}); }, Math.max(1, deadline - Date.now()));
    await context.addInitScript(installCapture);
    if (request.provider === 'claude') await context.addInitScript(installClaudeCapture);
    const page = context.pages()[0] || await context.newPage();
    await page.goto(COLLECTION_URLS[request.provider], { waitUntil: 'domcontentloaded', timeout: Math.min(30000, request.timeout_s * 1000) }).catch(() => {});
    let last = failure('PARSE_CHANGED');
    let googlePlayAttempted = false;
    let googleOneAccountId = null;
    while (Date.now() < deadline) {
      let iteration = failure('PARSE_CHANGED');
      let hasIdentity = false;
      for (const candidate of context.pages()) {
        const evidence = await collectEvidence(candidate, request.provider).catch(() => null);
        if (!evidence) continue;
        const result = normalize(request.provider, evidence, request.expected_account_id || googleOneAccountId, request.timezone);
        if (result.ok || ['ACCOUNT_MISMATCH', 'UNSUPPORTED_BILLING_CHANNEL'].includes(result.error.code)) return result;
        if (evidence.account_id) { hasIdentity = true; iteration = result; }
        else if (!hasIdentity && result.error.code === 'AUTH_REQUIRED') iteration = result;
      }
      last = iteration;
      if (request.provider === 'google_ai' && !googlePlayAttempted &&
          last.error.code === 'PARSE_CHANGED' && !page.isClosed()) {
        const scope = await page.evaluate(readGoogleOneScope).catch(() => null);
        if (/^[a-f0-9]{64}$/.test(scope?.account_id || '')) {
          googlePlayAttempted = true;
          googleOneAccountId = scope.account_id;
          if (request.expected_account_id && request.expected_account_id !== googleOneAccountId) {
            return failure('ACCOUNT_MISMATCH');
          }
          await page.goto(GOOGLE_PLAY_SUBSCRIPTIONS_URL, {
            waitUntil: 'domcontentloaded', timeout: Math.min(30000, Math.max(1, deadline - Date.now())),
          }).catch(() => {});
          continue;
        }
      }
      if (request.action === 'refresh' && last.error.code === 'AUTH_REQUIRED') return last;
      if (context.pages().length === 0) return last;
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    return last;
  } catch {
    return failure(timedOut ? 'TIMEOUT' : 'BROWSER_UNAVAILABLE');
  } finally {
    clearTimeout(timer);
    if (context) await context.close().catch(() => {});
    if (lock) await releaseProfileLock(lock).catch(() => {});
  }
}

async function main() {
  let result;
  try {
    let input = '';
    for await (const chunk of process.stdin) {
      input += chunk;
      if (input.length > 16384) throw new Error('invalid');
    }
    result = await run(JSON.parse(input));
  } catch { result = failure('INVALID_REQUEST'); }
  process.stdout.write(`${JSON.stringify(result)}\n`);
}
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) await main();
