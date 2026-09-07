import test from 'node:test';
import assert from 'node:assert/strict';
import { installClaudeCapture, readClaudeEvidence } from '../claude.mjs';

const ORG = '11111111-1111-4111-8111-111111111111';
const OTHER = '22222222-2222-4222-8222-222222222222';
const identityURL = organization => `https://claude.ai/edge-api/bootstrap/${organization}/app_start`;
const billingURL = organization => `https://claude.ai/api/organizations/${organization}/subscription_details`;

const response = (payload, status = 200) => ({
  status,
  ok: status >= 200 && status < 300,
  clone: () => ({ json: async () => payload }),
});

async function settle() {
  await new Promise(resolve => setTimeout(resolve, 0));
}

async function waitFor(predicate) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await settle();
  }
  assert.fail('capture did not settle');
}

function page(fetch) {
  globalThis.location = new URL('https://claude.ai/new#settings/billing');
  globalThis.window = { fetch };
  const install = (0, eval)(`(${installClaudeCapture.toString()})`);
  const read = (0, eval)(`(${readClaudeEvidence.toString()})`);
  install();
  return async () => {
    await settle();
    return read();
  };
}

function cleanup() {
  delete globalThis.window;
  delete globalThis.location;
}

test('live active schema projects only hashed identity and renewing datetime', async () => {
  const sensitive = {
    account: {
      email_address: 'Alice.Example@example.com',
      full_name: 'Alice Secret',
      memberships: [{ organization: { uuid: ORG } }],
    },
  };
  const billing = {
    status: 'active', next_charge_at: '2026-09-29T02:42:13Z',
    next_charge_date: '2026-09-29', plan_ending_at: null, plan_ending_before: null,
    payment_method: { brand: 'visa', last4: '4242' },
    scheduled_downgrade: { plan_type: 'pro', date: '2026-09-29' },
  };
  const read = page(async url => response(url.includes('app_start') ? sensitive : billing));
  try {
    await window.fetch(identityURL(ORG));
    await window.fetch(billingURL(ORG));
    await waitFor(() => window.__taskspindleClaude.identity !== null);
    const evidence = await read();
    assert.match(evidence.account_id, /^[a-f0-9]{64}$/);
    assert.equal(evidence.account_label, 'a***@e***.com');
    assert.deepEqual(evidence.billing, {
      status: 'renewing', renews_at: '2026-09-29T02:42:13.000Z', access_ends_at: null,
    });
    const serialized = JSON.stringify({ state: window.__taskspindleClaude, evidence });
    for (const secret of ['Alice.Example@example.com', 'Alice Secret', '4242', 'visa', 'scheduled_downgrade']) {
      assert.equal(serialized.includes(secret), false);
    }
    assert.deepEqual(Object.keys(evidence).sort(), ['account_id', 'account_label', 'billing']);
  } finally { cleanup(); }
});

test('capture is independent from generic state and excludes plan projection', async () => {
  const read = page(async url => response(String(url).includes('app_start')
    ? { account: { email_address: 'a@example.com' } }
    : { status: 'active', next_charge_date: '2026-09-29', next_charge_at: null,
      plan_ending_at: null, plan_ending_before: null, plan: 'Max' }));
  window.__taskspindleCapture = { subscription: { access_token: 'unrelated' } };
  try {
    await window.fetch(identityURL(ORG)); await window.fetch(billingURL(ORG));
    await waitFor(() => window.__taskspindleClaude.identity !== null);
    const evidence = await read();
    assert.equal(Object.hasOwn(evidence, 'plan'), false);
    assert.deepEqual(evidence.billing, {
      status: 'renewing', renews_at: '2026-09-29', access_ends_at: null,
    });
    assert.deepEqual(window.__taskspindleCapture, { subscription: { access_token: 'unrelated' } });
  } finally { cleanup(); }
});

test('wrong origin, near-match endpoint, and non-GET requests are ignored', async () => {
  const calls = [];
  const read = page(async (...args) => { calls.push(args); return response({ account: { email_address: 'a@example.com' } }); });
  try {
    await window.fetch(`https://example.com/edge-api/bootstrap/${ORG}/app_start`);
    await window.fetch(`https://claude.ai/edge-api/bootstrap/${ORG}/app_start/extra`);
    await window.fetch(`${identityURL(ORG)}?include=account`);
    await window.fetch(`${identityURL(ORG)}#account`);
    await window.fetch(identityURL(ORG), { method: 'POST' });
    assert.deepEqual(await read(), {});
    assert.equal(calls.length, 5);
  } finally { cleanup(); }
});

test('only observed query keys are allowed and their values or extra payload are never retained', async () => {
  const secret = 'system prompt and cache value';
  const read = page(async url => response(String(url).includes('app_start')
    ? { account: { email_address: 'a@example.com' }, system_prompts: secret }
    : { status: 'active', next_charge_date: '2026-09-29', next_charge_at: null,
      plan_ending_at: null, plan_ending_before: null, cached_response: secret }));
  try {
    const bootstrap = new URL(identityURL(ORG));
    for (const key of ['statsig_hashing_algorithm', 'growthbook_format', 'cache_bust', 'include_system_prompts']) {
      bootstrap.searchParams.append(key, secret);
    }
    await window.fetch(bootstrap);
    await window.fetch(`${billingURL(ORG)}?cached=${encodeURIComponent(secret)}`);
    await waitFor(() => window.__taskspindleClaude.identity !== null);
    assert.deepEqual((await read()).billing, {
      status: 'renewing', renews_at: '2026-09-29', access_ends_at: null,
    });
    assert.equal(JSON.stringify(window.__taskspindleClaude).includes(secret), false);
  } finally { cleanup(); }

  for (const url of [
    `${identityURL(ORG)}?organization=${OTHER}`,
    `${billingURL(ORG)}?include=payment_method`,
  ]) {
    const ignored = page(async () => response({ account: { email_address: 'a@example.com' } }));
    try {
      await window.fetch(url);
      assert.deepEqual(await ignored(), {});
      assert.equal(window.__taskspindleClaude.latest.identity, 0);
      assert.equal(window.__taskspindleClaude.latest.billing, 0);
    } finally { cleanup(); }
  }
});

test('multiple organizations or identities fail closed', async () => {
  let email = 'a@example.com';
  const read = page(async url => response(url.includes('app_start')
    ? { account: { email_address: email } }
    : { status: 'active', next_charge_date: '2026-09-29', next_charge_at: null,
      plan_ending_at: null, plan_ending_before: null }));
  try {
    await window.fetch(identityURL(ORG));
    await waitFor(() => window.__taskspindleClaude.identity !== null);
    email = 'b@example.com';
    await window.fetch(identityURL(ORG));
    await waitFor(() => window.__taskspindleClaude.identities.length > 1);
    assert.deepEqual(await read(), {});
  } finally { cleanup(); }

  const readOrganizations = page(async url => response(url.includes('app_start')
    ? { account: { email_address: 'a@example.com' } }
    : { status: 'active', next_charge_date: '2026-09-29', next_charge_at: null,
      plan_ending_at: null, plan_ending_before: null }));
  try {
    await window.fetch(identityURL(ORG));
    await window.fetch(billingURL(OTHER));
    await waitFor(() => window.__taskspindleClaude.identity !== null);
    assert.deepEqual(await readOrganizations(), {});
  } finally { cleanup(); }
});

test('newer failed request prevents an older response from winning the race', async () => {
  let resolveFirst;
  let count = 0;
  const read = page(async () => {
    count += 1;
    if (count === 1) return new Promise(resolve => { resolveFirst = resolve; });
    throw new Error('new request failed');
  });
  try {
    const first = window.fetch(identityURL(ORG));
    await assert.rejects(window.fetch(identityURL(ORG)));
    resolveFirst(response({ account: { email_address: 'old@example.com' } }));
    await first;
    assert.deepEqual(await read(), {});
  } finally { cleanup(); }
});

test('capture parsing failures preserve the original fetch result and clear stale evidence', async () => {
  let cloneFails = false;
  const uncloneable = {
    status: 200,
    ok: true,
    clone() { throw new Error('body unavailable'); },
  };
  const read = page(async () => cloneFails
    ? uncloneable
    : response({ account: { email_address: 'a@example.com' } }));
  try {
    await window.fetch(identityURL(ORG));
    await waitFor(() => window.__taskspindleClaude.identity !== null);
    cloneFails = true;
    assert.equal(await window.fetch(identityURL(ORG)), uncloneable);
    assert.deepEqual(await read(), {});
  } finally { cleanup(); }
});

test('401 and 403 on exact endpoints yield only auth_required', async () => {
  for (const status of [401, 403]) {
    const read = page(async () => response({ raw: 'do not expose' }, status));
    try {
      await window.fetch(identityURL(ORG));
      assert.deepEqual(await read(), { auth_required: true });
      assert.equal(JSON.stringify(await read()).includes('do not expose'), false);
    } finally { cleanup(); }
  }
});

test('derived pending-cancellation fixtures preserve ending precision and suppress next charge', async () => {
  // These cancellation shapes are derived from the independent implementations
  // linked in claude.mjs; the captured live account fixture is renewing.
  const cases = [
    {
      candidate: { status: 'active', next_charge_at: '2026-10-01T02:42:13Z',
        next_charge_date: '2026-10-01', plan_ending_at: '2026-09-29T02:42:13Z',
        plan_ending_before: null },
      access_ends_at: '2026-09-29T02:42:13.000Z',
    },
    {
      candidate: { status: 'active', next_charge_at: '2026-10-01T02:42:13Z',
        next_charge_date: '2026-10-01', plan_ending_at: null,
        plan_ending_before: '2026-09-29' },
      access_ends_at: '2026-09-29',
    },
    {
      candidate: { status: 'active', next_charge_at: null, next_charge_date: null,
        plan_ending_at: '2026-09-29T02:42:13Z',
        plan_ending_before: '2026-09-28T21:42:13-05:00' },
      access_ends_at: '2026-09-29T02:42:13.000Z',
    },
  ];
  for (const { candidate, access_ends_at } of cases) {
    const read = page(async url => response(url.includes('app_start')
      ? { account: { email_address: 'a@example.com' } } : candidate));
    try {
      await window.fetch(identityURL(ORG)); await window.fetch(billingURL(ORG));
      await waitFor(() => window.__taskspindleClaude.identity !== null);
      assert.deepEqual((await read()).billing, {
        status: 'cancelled', renews_at: null, access_ends_at,
      });
    } finally { cleanup(); }
  }
});

test('a later active response with null endings reverses captured pending cancellation', async () => {
  let candidate = { status: 'active', next_charge_at: '2026-10-01T02:42:13Z',
    next_charge_date: '2026-10-01', plan_ending_at: null,
    plan_ending_before: '2026-09-29' };
  const read = page(async url => response(url.includes('app_start')
    ? { account: { email_address: 'a@example.com' } } : candidate));
  try {
    await window.fetch(identityURL(ORG)); await window.fetch(billingURL(ORG));
    await waitFor(() => window.__taskspindleClaude.identity !== null);
    assert.equal((await read()).billing.status, 'cancelled');
    candidate = { status: 'active', next_charge_at: '2026-10-01T02:42:13Z',
      next_charge_date: '2026-10-01', plan_ending_at: null, plan_ending_before: null };
    await window.fetch(billingURL(ORG));
    assert.deepEqual((await read()).billing, {
      status: 'renewing', renews_at: '2026-10-01T02:42:13.000Z', access_ends_at: null,
    });
  } finally { cleanup(); }
});

test('inactive, incomplete, malformed, and contradictory endings fail closed', async () => {
  const cases = [
    { status: 'canceled', next_charge_date: '2026-09-29', next_charge_at: null,
      plan_ending_at: '2026-09-29T02:42:13Z', plan_ending_before: null },
    { status: 'expired', next_charge_date: null, next_charge_at: null,
      plan_ending_at: '2026-09-29', plan_ending_before: null },
    { status: 'active', next_charge_date: null, next_charge_at: null,
      plan_ending_at: '2026-09-29' },
    { status: 'active', next_charge_date: null, next_charge_at: null,
      plan_ending_at: '2026-02-30', plan_ending_before: null },
    { status: 'active', next_charge_date: null, next_charge_at: null,
      plan_ending_at: '2026-09-29', plan_ending_before: '2026-09-30' },
    { status: 'active', next_charge_date: null, next_charge_at: null,
      plan_ending_at: '2026-09-29', plan_ending_before: '2026-09-29T00:00:00Z' },
    { status: 'active', next_charge_date: '2026-09-28', next_charge_at: '2026-09-29T02:42:13Z',
      plan_ending_at: null, plan_ending_before: null },
    { status: 'active', next_charge_date: 'September 29', next_charge_at: null,
      plan_ending_at: null, plan_ending_before: null },
    { status: 'active', next_charge_date: '2026-02-30', next_charge_at: null,
      plan_ending_at: null, plan_ending_before: null },
    { status: 'active', next_charge_date: null, next_charge_at: '2026-02-30T02:42:13Z',
      plan_ending_at: null, plan_ending_before: null },
  ];
  for (const candidate of cases) {
    const read = page(async url => response(url.includes('app_start')
      ? { account: { email_address: 'a@example.com' } } : candidate));
    try {
      await window.fetch(identityURL(ORG)); await window.fetch(billingURL(ORG));
      await waitFor(() => window.__taskspindleClaude.identity !== null);
      const evidence = await read();
      assert.equal(evidence.billing, null);
    } finally { cleanup(); }
  }
});
