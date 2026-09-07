export const VERSION = '1';
export const PLANS = Object.freeze({
  chatgpt: /^(?:(?:ChatGPT )?(?:Plus|Pro|Go|Free)|None)$/i,
  claude: /^(?:(?:Claude )?(?:Pro|Max(?:\s*\((?:5x|20x)\))?|Free)(?: plan)?|None)$/i,
  google_ai: /^(?:(?:Google AI (?:Pro|Ultra|Plus)|AI Premium)(?:\s*\(\d+\s*(?:GB|TB)\))?|None)$/i,
  grok: /^(?:(?:SuperGrok(?: Heavy)?|Free)(?: plan)?|None)$/i,
});
export const URLS = Object.freeze({
  chatgpt: 'https://chatgpt.com/#settings/Billing',
  claude: 'https://claude.ai/settings/billing',
  google_ai: 'https://one.google.com/settings',
  grok: 'https://grok.com/?_s=billing',
});
export const MESSAGES = Object.freeze({
  SETUP_REQUIRED: 'Install and pair the Playwright extension in the selected Chrome profile.',
  AUTH_REQUIRED: 'Sign in using the dedicated subscription browser.',
  ACCOUNT_MISMATCH: 'The signed-in account differs from the connected account.',
  PARSE_CHANGED: 'The provider did not expose recognized subscription details.',
  UNSUPPORTED_BILLING_CHANNEL: 'This subscription is billed outside the provider website.',
  INVALID_REQUEST: 'The subscription browser request is invalid.',
  BROWSER_UNAVAILABLE: 'The dedicated subscription browser could not start.',
  PROFILE_BUSY: 'The dedicated subscription profile is already in use.',
  TIMEOUT: 'The subscription browser operation timed out.',
});
export const failure = code => ({ ok: false, error: { code, message: MESSAGES[code] || MESSAGES.PARSE_CHANGED } });

export function validDate(value) {
  if (typeof value !== 'string') return null;
  if (!/^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))?$/.test(value)) return null;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  // Reject rollover dates such as February 30 before any timezone conversion.
  const day = value.slice(0, 10);
  if (new Date(`${day}T00:00:00Z`).toISOString().slice(0, 10) !== day) return null;
  return value.length === 10 ? value : date.toISOString();
}

// CodexBar endpoint/renewal flag semantics; our collector additionally rejects
// empty metadata because it does not establish a meaningful billing status.
export function parseChatGPT(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const has = key => Object.hasOwn(payload, key);
  if (!(has('active_until') || has('activeUntil')) || !(has('will_renew') || has('willRenew'))) return null;
  const until = has('active_until') ? payload.active_until : payload.activeUntil;
  const renew = has('will_renew') ? payload.will_renew : payload.willRenew;
  if (!(until === null || typeof until === 'string') || !(renew === null || typeof renew === 'boolean')) return null;
  if (until === null) return null;
  const date = validDate(until);
  if (!date || date.length === 10 || typeof renew !== 'boolean') return null;
  return { status: renew ? 'renewing' : 'cancelled', renews_at: renew ? date : null, access_ends_at: renew ? null : date };
}

export function normalize(provider, evidence, expected, timezone) {
  if (!Object.hasOwn(URLS, provider) || !evidence) return failure('PARSE_CHANGED');
  if (evidence.auth_required) return failure('AUTH_REQUIRED');
  if (!/^[a-f0-9]{64}$/.test(evidence.account_id || '')) return failure('PARSE_CHANGED');
  if (expected && expected !== evidence.account_id) return failure('ACCOUNT_MISMATCH');
  if (['apple', 'google_play', 'x_premium'].includes(evidence.billing_channel)) return failure('UNSUPPORTED_BILLING_CHANNEL');
  if (evidence.billing_channel !== 'provider_web') return failure('PARSE_CHANGED');
  if (typeof evidence.plan !== 'string' || !PLANS[provider].test(evidence.plan)) return failure('PARSE_CHANGED');
  const parsed = provider === 'chatgpt' && evidence.subscription
    ? (parseChatGPT(evidence.subscription) || evidence.billing) : evidence.billing;
  if (!parsed || !['renewing', 'cancelled', 'expired', 'free', 'none'].includes(parsed.status)) return failure('PARSE_CHANGED');
  if ((parsed.status === 'free') !== /\bFree(?: plan)?$/i.test(evidence.plan) || (parsed.status === 'none') !== /^None$/.test(evidence.plan)) return failure('PARSE_CHANGED');
  const renewal = parsed.renews_at == null ? null : validDate(parsed.renews_at);
  const end = parsed.access_ends_at == null ? null : validDate(parsed.access_ends_at);
  if ((parsed.renews_at && !renewal) || (parsed.access_ends_at && !end)) return failure('PARSE_CHANGED');
  if ((renewal && end) || (renewal && parsed.status !== 'renewing') || (end && !['cancelled', 'expired'].includes(parsed.status))) return failure('PARSE_CHANGED');
  if (parsed.status === 'renewing' && !renewal) return failure('PARSE_CHANGED');
  const url = new URL(URLS[provider]);
  return { ok: true, observation: {
    provider, account_id: evidence.account_id,
    account_label: typeof evidence.account_label === 'string' && /^[^@\s]{1}\*\*\*@[^@\s]{1}\*\*\*\.[a-z]{2,24}$/i.test(evidence.account_label) ? evidence.account_label : 'Connected account',
    billing_channel: 'provider_web', plan: evidence.plan || null, status: parsed.status,
    renews_at: renewal, access_ends_at: end,
    date_precision: (renewal || end) ? ((renewal || end).length === 10 ? 'date' : 'datetime') : null,
    timezone, source_url: `${url.origin}${url.pathname}`, collector_version: VERSION,
  } };
}
