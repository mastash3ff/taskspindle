// Serialized into the provider page: never import dependencies or return raw
// account labels, row text, payment information, or subscription dates.
export async function readGooglePlayEvidence() {
  if (location.hostname === 'accounts.google.com') return { auth_required: true };
  if (location.origin !== 'https://play.google.com' || location.pathname !== '/store/account/subscriptions') return {};
  const visible = element => Boolean(element.getClientRects().length) && !element.closest('[hidden], [aria-hidden="true"]');
  if ([...document.querySelectorAll('input[type="password"]')].some(visible)) return { auth_required: true };
  const challenge = [...document.querySelectorAll('iframe[src*="challenges.cloudflare.com"], iframe[src*="recaptcha"], iframe[src*="hcaptcha.com"]')].some(visible);
  if (challenge && /verify (?:that )?you(?: are|'re) human|complete (?:the )?(?:security )?check|checking your browser|just a moment/i.test(document.title || '')) return { auth_required: true };
  const appID = anchor => {
    try {
      const url = new URL(anchor.getAttribute('href') || '', location.href);
      if (url.origin !== 'https://play.google.com' || url.pathname !== '/store/apps/details' || url.searchParams.getAll('id').length !== 1) return null;
      return url.searchParams.get('id');
    } catch { return null; }
  };
  const target = 'com.google.android.apps.subscriptions.red';
  const anchors = [...document.querySelectorAll('a[href]')].filter(anchor => visible(anchor) && appID(anchor) === target);
  const rows = [...new Set(anchors.map(anchor => anchor.closest('tr')).filter(row => row && visible(row)))];
  if (rows.length !== 1) return {};
  const row = rows[0];
  const apps = [...row.querySelectorAll('a[href]')].filter(anchor => visible(anchor) && appID(anchor) !== null);
  if (apps.length !== 1 || appID(apps[0]) !== target) return {};
  const manage = [...row.querySelectorAll('button, [role="button"]')].filter(button => visible(button) &&
    /^(?:Manage)$/i.test((button.innerText || button.getAttribute('aria-label') || '').trim()));
  if (manage.length !== 1) return {};
  const plans = (row.innerText || '').split('\n').map(line => line.trim()).filter(line =>
    /^(?:Google AI (?:Pro|Ultra|Plus)|AI Premium)(?:\s*\(\d+\s*(?:GB|TB)\))?$/i.test(line));
  if (plans.length !== 1) return {};

  const accounts = [...document.querySelectorAll('[aria-label^="Google Account:"]')].filter(visible);
  const emails = new Set();
  for (const account of accounts) {
    const matches = (account.getAttribute('aria-label') || '').match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi) || [];
    if (matches.length !== 1) return {};
    emails.add(matches[0].toLowerCase());
  }
  if (emails.size !== 1) return {};
  const email = [...emails][0];
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`taskspindle:subscription:v1:google_ai:${email}`));
  const [local, domain] = email.split('@');
  return {
    account_id: [...new Uint8Array(bytes)].map(value => value.toString(16).padStart(2, '0')).join(''),
    account_label: `${local[0]}***@${domain[0]}***.${domain.split('.').at(-1)}`,
    billing_channel: 'google_play', plan: plans[0], billing: null,
  };
}
