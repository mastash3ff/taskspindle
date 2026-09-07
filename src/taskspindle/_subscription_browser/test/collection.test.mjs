import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { collectEvidence, readGoogleOneScope } from '../collection.mjs';
import { COLLECTION_URLS, URLS } from '../extractors.mjs';
import { readEvidence } from '../page.mjs';
import { readClaudeEvidence } from '../claude.mjs';
import { readGooglePlayEvidence } from '../google.mjs';

const account = 'a'.repeat(64);
const identity = { account_id: account, account_label: 'a***@e***.com' };
const base = {
  ...identity,
  billing_channel: 'provider_web',
  plan: 'Pro',
  billing: { status: 'cancelled', renews_at: null, access_ends_at: '2026-10-09' },
};
const captured = {
  ...identity,
  billing: { status: 'renewing', renews_at: '2026-11-09', access_ends_at: null },
};

function pageWith(baseEvidence, claudeEvidence, googleEvidence = {}) {
  const calls = [];
  return {
    calls,
    page: {
      async evaluate(fn, argument) {
        calls.push([fn, argument]);
        if (fn === readEvidence) return baseEvidence;
        if (fn === readClaudeEvidence) return claudeEvidence;
        assert.equal(fn, readGooglePlayEvidence);
        return googleEvidence;
      },
    },
  };
}

test('non-Claude providers retain their existing evidence path', async () => {
  const fixture = pageWith(base, captured);
  const result = await collectEvidence(fixture.page, 'chatgpt');
  assert.equal(result, base);
  assert.deepEqual(fixture.calls, [[readEvidence, 'chatgpt']]);
});

test('Claude combines a scoped DOM root with captured identity and billing', async () => {
  const fixture = pageWith(
    {
      ...base,
      billing: { ...captured.billing, renews_at: '2026-11-08' },
      raw_dom: 'DO_NOT_COPY',
    },
    {
      ...captured,
      billing: { ...captured.billing, raw_payment: 'DO_NOT_COPY' },
      raw_response: 'DO_NOT_COPY',
    },
  );
  assert.deepEqual(await collectEvidence(fixture.page, 'claude'), {
    ...identity,
    billing_channel: 'provider_web',
    plan: 'Pro',
    billing: captured.billing,
  });
  assert.deepEqual(fixture.calls, [[readEvidence, 'claude'], [readClaudeEvidence, undefined]]);
});

test('Claude preserves a positively parsed DOM cancellation when capture has no billing', async () => {
  const fixture = pageWith(base, { ...identity, billing: null });
  const result = await collectEvidence(fixture.page, 'claude');
  assert.deepEqual(result.billing, base.billing);
});

test('Claude capture null remains authoritative over unrecognized DOM billing', async () => {
  const fixture = pageWith({ ...base, billing: null }, { ...identity, billing: null });
  assert.equal((await collectEvidence(fixture.page, 'claude')).billing, null);
});

test('conflicting non-null Claude billing statuses fail closed', async () => {
  const fixture = pageWith(base, captured);
  assert.deepEqual(await collectEvidence(fixture.page, 'claude'), {});
});

test('Claude requires a recognized scoped root and captured identity', async () => {
  for (const baseEvidence of [{}, { plan: 'Pro' }, { billing_channel: 'provider_web' }]) {
    assert.deepEqual(await collectEvidence(pageWith(baseEvidence, captured).page, 'claude'), {});
  }
  for (const claudeEvidence of [
    {},
    { account_id: account, billing: captured.billing },
    { account_label: identity.account_label, billing: captured.billing },
    { account_id: 'invalid', account_label: identity.account_label, billing: captured.billing },
    { account_id: account, account_label: 'alice@example.com', billing: captured.billing },
  ]) assert.deepEqual(await collectEvidence(pageWith(base, claudeEvidence).page, 'claude'), {});
});

test('Claude DOM and capture identity conflicts fail closed', async () => {
  const conflictingId = { ...base, account_id: 'b'.repeat(64) };
  const conflictingLabel = { ...base, account_label: 'b***@e***.com' };
  assert.deepEqual(await collectEvidence(pageWith(conflictingId, captured).page, 'claude'), {});
  assert.deepEqual(await collectEvidence(pageWith(conflictingLabel, captured).page, 'claude'), {});
});

test('Claude auth evidence overrides billing evidence', async () => {
  assert.deepEqual(
    await collectEvidence(pageWith({ auth_required: true }, captured).page, 'claude'),
    { auth_required: true },
  );
  assert.deepEqual(
    await collectEvidence(pageWith(base, { auth_required: true }).page, 'claude'),
    { auth_required: true },
  );
});

test('Google Play evidence is read separately and auth overrides base evidence', async () => {
  const play = { ...identity, billing_channel: 'google_play', plan: 'Google AI Pro', billing: null };
  const fixture = pageWith({}, null, { ...play, raw_row: 'DO_NOT_COPY' });
  assert.deepEqual(await collectEvidence(fixture.page, 'google_ai'), play);
  assert.deepEqual(fixture.calls, [[readEvidence, 'google_ai'], [readGooglePlayEvidence, undefined]]);
  assert.deepEqual(
    await collectEvidence(pageWith(base, null, { auth_required: true }).page, 'google_ai'),
    { auth_required: true },
  );
});

test('Google Play fallback scope requires exact Google One settings and unique visible account UI', async () => {
  const node = (text = '', label = '', visible = true, conversation = false) => ({
    innerText: text,
    getAttribute: name => name === 'aria-label' ? label : null,
    getClientRects: () => visible ? [{}] : [],
    closest: selector => conversation && selector.includes('conversation') ? {} : null,
    querySelector: selector => conversation && selector.includes('conversation') ? {} : null,
  });
  const inspect = async ({ url = 'https://one.google.com/settings', accounts = [node('', 'Google Account: Example (alice@example.com)')], mains = [node('Manage your Google One membership\nChange membership plan')], names = [] } = {}) => {
    globalThis.location = new URL(url);
    globalThis.document = { querySelectorAll: selector => selector.startsWith('[aria-label') ? accounts : selector.startsWith('main') ? mains : names };
    try { return await readGoogleOneScope(); }
    finally { delete globalThis.location; delete globalThis.document; }
  };
  const expected = createHash('sha256').update('taskspindle:subscription:v1:google_ai:alice@example.com').digest('hex');
  assert.deepEqual(await inspect(), { account_id: expected });
  assert.deepEqual(await inspect({ mains: [node('Storage')], names: [node('Google One')] }), { account_id: expected });
  assert.deepEqual(await inspect({ url: 'https://one.google.com/settings/other' }), {});
  assert.deepEqual(await inspect({ accounts: [] }), {});
  assert.deepEqual(await inspect({ accounts: [node('', 'Google Account: A'), node('', 'Google Account: B')] }), {});
  assert.deepEqual(await inspect({ accounts: [node('', 'Google Account: A', false)] }), {});
  assert.deepEqual(await inspect({ mains: [node('Storage')], names: [node('Storage')] }), {});
  assert.deepEqual(await inspect({ mains: [node('Manage your Google One membership', '', true, true)] }), {});
});

test('collection navigation uses the billing page without changing provider links', () => {
  assert.equal(URLS.grok, 'https://grok.com/?_s=usage');
  assert.equal(COLLECTION_URLS.grok, 'https://grok.com/?_s=billing');
  for (const provider of ['chatgpt', 'claude', 'google_ai']) {
    assert.equal(COLLECTION_URLS[provider], URLS[provider]);
  }
});
