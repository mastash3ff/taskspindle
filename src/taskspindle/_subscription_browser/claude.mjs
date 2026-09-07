// Claude response capture. Both exported functions are self-contained so
// Playwright can serialize them directly into the provider page.

export function installClaudeCapture() {
  if (window.__taskspindleClaude) return;
  const state = window.__taskspindleClaude = {
    identity: null,
    billing: null,
    auth: { identity: false, billing: false },
    latest: { identity: 0, billing: 0 },
    organizations: [],
    identities: [],
    ambiguous: false,
  };
  const original = window.fetch.bind(window);
  const uuid = '[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}';
  const identityPath = new RegExp(`^/edge-api/bootstrap/(${uuid})/app_start$`, 'i');
  const billingPath = new RegExp(`^/api/organizations/(${uuid})/subscription_details$`, 'i');
  const emailPattern = /^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$/i;
  const remember = (list, value) => {
    if (!list.includes(value)) list.push(value);
    if (list.length > 1) state.ambiguous = true;
  };
  const validDate = value => {
    if (typeof value !== 'string') return null;
    if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      const parsed = new Date(`${value}T00:00:00Z`);
      return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value
        ? value : null;
    }
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null;
    const parsed = new Date(value);
    const day = value.slice(0, 10);
    if (!Number.isFinite(parsed.getTime()) ||
        new Date(`${day}T00:00:00Z`).toISOString().slice(0, 10) !== day) return null;
    return parsed.toISOString();
  };
  const projectIdentity = async payload => {
    const raw = payload?.account?.email_address;
    if (typeof raw !== 'string' || !emailPattern.test(raw.trim())) return null;
    const email = raw.trim().toLowerCase();
    const bytes = await crypto.subtle.digest(
      'SHA-256',
      new TextEncoder().encode(`taskspindle:subscription:v1:claude:${email}`),
    );
    const account_id = [...new Uint8Array(bytes)]
      .map(value => value.toString(16).padStart(2, '0')).join('');
    const [local, domain] = email.split('@');
    const labels = domain.split('.');
    return {
      account_id,
      account_label: `${local[0]}***@${domain[0]}***.${labels.at(-1)}`,
    };
  };
  const projectBilling = payload => {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
    if (payload.status !== 'active' || !Object.hasOwn(payload, 'plan_ending_at') ||
        !Object.hasOwn(payload, 'plan_ending_before')) return null;
    const rawEndingAt = payload.plan_ending_at;
    const rawEndingBefore = payload.plan_ending_before;
    const endingAt = rawEndingAt === null ? null : validDate(rawEndingAt);
    const endingBefore = rawEndingBefore === null ? null : validDate(rawEndingBefore);
    if ((rawEndingAt !== null && !endingAt) || (rawEndingBefore !== null && !endingBefore) ||
        (endingAt && endingBefore && endingAt !== endingBefore)) return null;
    const access_ends_at = endingAt || endingBefore;
    if (access_ends_at) {
      // Independent implementations interpret these fields as pending cancellation:
      // https://github.com/LandonDev/aliax/blob/df34e2771d7e3fe82aba11e39c9b57cbeab93003/src/main/adapters/claude.ts#L197-L203
      // https://github.com/chaehyun2/claudetuner/blob/064b53d7440fb56dfbbb5888d14007eabafa0ac1/bg/plan.js#L44-L57
      return { status: 'cancelled', renews_at: null, access_ends_at };
    }
    const nextAt = payload.next_charge_at == null ? null : validDate(payload.next_charge_at);
    const nextDate = payload.next_charge_date == null ? null : validDate(payload.next_charge_date);
    if ((payload.next_charge_at != null && !nextAt) ||
        (payload.next_charge_date != null && (!nextDate || nextDate.includes('T')))) return null;
    if (nextAt && nextDate && nextAt.slice(0, 10) !== nextDate) return null;
    const renews_at = nextAt || nextDate;
    return renews_at
      ? { status: 'renewing', renews_at, access_ends_at: null }
      : null;
  };

  window.fetch = async (...args) => {
    let kind = null;
    let organization = null;
    try {
      const input = args[0];
      const options = args[1] || {};
      const method = String(options.method || input?.method || 'GET').toUpperCase();
      const url = new URL(input?.url || String(input), location.href);
      const identityMatch = url.pathname.match(identityPath);
      const billingMatch = url.pathname.match(billingPath);
      const match = identityMatch || billingMatch;
      const allowedQueryKeys = identityMatch
        ? new Set(['statsig_hashing_algorithm', 'growthbook_format', 'cache_bust', 'include_system_prompts'])
        : new Set(['cached']);
      const queryAllowed = [...url.searchParams.keys()].every(key => allowedQueryKeys.has(key));
      if (location.origin === 'https://claude.ai' && url.origin === location.origin &&
          !url.hash && queryAllowed && method === 'GET' && match) {
        kind = identityMatch ? 'identity' : 'billing';
        organization = match[1].toLowerCase();
      }
    } catch { /* Unrecognized requests remain completely outside capture. */ }
    if (!kind) return original(...args);

    const generation = ++state.latest[kind];
    state[kind] = null;
    state.auth[kind] = false;
    remember(state.organizations, organization);
    let response;
    try {
      response = await original(...args);
    } catch (error) {
      // The generation was advanced before fetch. An older successful response
      // can therefore never overwrite this newer failed request.
      throw error;
    }
    if (generation !== state.latest[kind]) return response;
    if (response.status === 401 || response.status === 403) {
      state.auth[kind] = true;
      return response;
    }
    if (!response.ok) return response;
    try {
      response.clone().json().then(async payload => {
        if (generation !== state.latest[kind]) return;
        if (kind === 'identity') {
          const projected = await projectIdentity(payload);
          if (generation !== state.latest.identity || !projected) return;
          remember(state.identities, projected.account_id);
          state.identity = projected;
        } else {
          state.billing = projectBilling(payload);
        }
      }).catch(() => {});
    } catch { /* Capture must not change the original fetch result. */ }
    return response;
  };
}

export function readClaudeEvidence() {
  const state = window.__taskspindleClaude;
  if (!state || state.ambiguous || state.organizations?.length !== 1 ||
      state.identities?.length > 1) return {};
  if (state.auth?.identity || state.auth?.billing) return { auth_required: true };
  const identity = state.identity;
  if (!identity || !/^[a-f0-9]{64}$/.test(identity.account_id || '') ||
      typeof identity.account_label !== 'string') return {};
  return {
    account_id: identity.account_id,
    account_label: identity.account_label,
    billing: state.billing || null,
  };
}
