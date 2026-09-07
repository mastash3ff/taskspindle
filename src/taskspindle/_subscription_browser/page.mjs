// Runs entirely in the authenticated page. Only projected subscription fields and
// a one-way account hash leave the page; raw DOM/payloads are never returned.
export function installCapture() {
  if (window.__taskspindleCapture) return;
  const state = window.__taskspindleCapture = { subscription: null, authRequired: false };
  const original = window.fetch.bind(window);
  let latest = 0;
  let latestGrokSession = 0;
  window.fetch = async (...args) => {
    let subscription = false;
    let grokSession = false;
    try {
      const url = new URL(args[0]?.url || String(args[0]), location.href);
      subscription = location.origin === 'https://chatgpt.com' && url.origin === location.origin && url.pathname === '/backend-api/subscriptions';
      grokSession = location.origin === 'https://grok.com' && url.origin === location.origin && url.pathname === '/api/auth/session';
    } catch { /* unrecognized request */ }
    const generation = subscription ? ++latest : 0;
    const sessionGeneration = grokSession ? ++latestGrokSession : 0;
    if (subscription) state.subscription = null;
    if (grokSession) state.grokIdentity = null;
    const response = await original(...args);
    if (subscription) {
      if (response.status === 401 && generation === latest) state.authRequired = true;
      if (response.ok) response.clone().json().then(payload => {
        if (generation !== latest) return;
        const has = key => Object.hasOwn(payload || {}, key);
        const until = has('active_until') ? payload.active_until : payload?.activeUntil;
        const renew = has('will_renew') ? payload.will_renew : payload?.willRenew;
        if ((has('active_until') || has('activeUntil')) && (has('will_renew') || has('willRenew')) &&
            (until === null || typeof until === 'string') && (renew === null || typeof renew === 'boolean')) {
          // Root fields observed on the provider's billing response. Never retain
          // unknown plan strings, account identifiers, or payment details.
          const plan = typeof payload.plan_type === 'string' && /^(?:free|go|plus|pro)$/.test(payload.plan_type) ? payload.plan_type : null;
          state.subscription = { active_until: until, will_renew: renew,
            plan_type: plan, plan_type_unrecognized: has('plan_type') && plan === null,
            is_processor_stripe: typeof payload.is_processor_stripe === 'boolean' ? payload.is_processor_stripe : null };
          state.authRequired = false;
        }
      }).catch(() => {});
    }
    if (grokSession) {
      if (response.status === 401 && sessionGeneration === latestGrokSession) state.authRequired = true;
      if (response.ok) response.clone().json().then(async payload => {
        if (sessionGeneration !== latestGrokSession) return;
        // This exact field was observed in Grok's own session response. Hash it
        // here; no session object, raw email, user ID, or token is retained.
        const value = payload?.session?.email;
        if (typeof value !== 'string' || !/^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$/i.test(value.trim())) return;
        const email = value.trim().toLowerCase();
        const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`taskspindle:subscription:v1:grok:${email}`));
        if (sessionGeneration !== latestGrokSession) return;
        const [local, domain] = email.split('@');
        state.grokIdentity = {
          account_id: [...new Uint8Array(bytes)].map(value => value.toString(16).padStart(2, '0')).join(''),
          account_label: `${local[0]}***@${domain[0]}***.${domain.split('.').at(-1)}`,
        };
        state.authRequired = false;
      }).catch(() => {});
    }
    return response;
  };
}

export async function readEvidence(provider) {
  const capture = window.__taskspindleCapture || {};
  const authHosts = {
    chatgpt: ['auth.openai.com', 'auth0.openai.com'],
    claude: ['claude.ai'],
    google_ai: ['accounts.google.com'],
    grok: ['accounts.x.ai', 'auth.x.ai', 'x.com', 'twitter.com'],
  };
  const hostIsAuth = (authHosts[provider] || []).includes(location.hostname) && !(provider === 'claude' && !/^\/(?:login|signin)(?:\/|$)/i.test(location.pathname));
  const loginURL = hostIsAuth || /\/(?:login|signin)(?:\/|$)/i.test(location.pathname);
  const challenge = Boolean(document.querySelector('iframe[src*="challenges.cloudflare.com"], iframe[src*="recaptcha"], iframe[src*="hcaptcha.com"]')) && /verify (?:that )?you(?: are|'re) human|complete (?:the )?(?:security )?check|checking your browser|just a moment/i.test(document.title || '');
  const authRequired = loginURL || challenge || capture.authRequired || Boolean(document.querySelector('input[type="password"]'));
  if (authRequired) return { auth_required: true };
  const allowedHosts = { chatgpt: 'chatgpt.com', claude: 'claude.ai', google_ai: 'one.google.com', grok: 'grok.com' };
  if (location.hostname !== allowedHosts[provider]) return {};

  const billingRoute = provider === 'chatgpt' ? /settings\/billing/i.test(location.hash)
    : provider === 'claude' ? (/^\/settings\/billing\/?$/.test(location.pathname) ||
      (/^\/new\/?$/.test(location.pathname) && /^#settings\/billing\/?$/.test(location.hash)))
    : provider === 'grok' ? location.pathname === '/' && ['billing', 'usage'].includes(new URLSearchParams(location.search).get('_s'))
    : /^\/settings\/?$/.test(location.pathname);
  if (!billingRoute && !(provider === 'chatgpt' && location.pathname === '/')) return {};
  const conversation = '[data-message-author-role], [data-testid*="conversation"], [data-testid*="chat-message"], [data-testid*="message"], [role="log"], [contenteditable="true"]';
  const visible = el => Boolean(el.getClientRects().length) && !el.closest('[hidden], [aria-hidden="true"]');
  // Settings overlays must be isolated from the chat underneath. A matching URL
  // alone never establishes a billing DOM root. Unknown markup fails closed.
  const rootSelector = ['chatgpt', 'grok'].includes(provider) ? 'dialog, [role="dialog"]' : 'dialog, [role="dialog"], main, [role="main"]';
  const roots = [...document.querySelectorAll(rootSelector)].filter(el => {
    if (!visible(el) || el.closest(conversation) || el.querySelector(conversation)) return false;
    const names = [el.getAttribute('aria-label') || '', ...[...el.querySelectorAll('h1, h2, h3, [role="heading"]')].filter(visible).map(heading => heading.innerText || '')];
    return names.some(name => /^(?:Settings|Billing|Subscription|Manage (?:subscription|membership)|Google One settings)$/i.test(name.trim()) ||
      (provider === 'grok' && /^Usage$/i.test(name.trim())));
  });
  const innermost = roots.filter(root => !roots.some(other => other !== root && root.contains(other)));
  if (innermost.length !== 1) {
    // Logged-out ChatGPT can keep the requested billing hash on its home page.
    // Only a visible application login control, outside conversation content and
    // with no settings root, establishes auth-required; missing billing alone does not.
    if (provider === 'chatgpt' && location.pathname === '/' && roots.length === 0) {
      const loginControls = [...document.querySelectorAll('[data-testid="login-button"], header button, header a, nav button, nav a')];
      if (loginControls.some(el => visible(el) && !el.closest(conversation) && /^(?:Log in|Sign in)$/i.test((el.innerText || el.getAttribute('aria-label') || '').trim()))) return { auth_required: true };
    }
    return {};
  }
  if (!billingRoute) return {};
  const root = innermost[0];
  const text = root.innerText || '';
  const lines = text.split('\n').map(line => line.trim()).filter(Boolean);

  // Account identity only from account controls, email fields or an explicit
  // email row in settings. Never from conversation text or arbitrary body email.
  const controls = [...root.querySelectorAll('input[type="email"], button[aria-label], a[aria-label], button[data-testid*="account"], button[data-testid*="profile"]'), ...document.querySelectorAll('header button[aria-label], header a[aria-label], nav button[aria-label], nav a[aria-label], aside button[aria-label], aside a[aria-label]')];
  const identityTexts = controls.filter(el => visible(el) && !el.closest(conversation))
    .flatMap(el => {
      // ChatGPT's billing panel can contain an editable billing email, which
      // does not establish the signed-in account.
      if (el.matches('input[type="email"]')) return provider === 'chatgpt' ? [] : [el.value];
      const label = el.getAttribute('aria-label') || '';
      if (/account|profile|google account/i.test(label)) return [label, el.innerText || ''];
      if (/account|profile/i.test(el.getAttribute('data-testid') || '')) return [el.innerText || '', label];
      return [];
    });
  for (let i = 0; i < lines.length - 1; i++) if (/^(?:Email|Email address|Account email)$/i.test(lines[i])) identityTexts.push(lines[i + 1]);
  const emails = [...new Set(identityTexts.flatMap(value => value.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi) || []).map(value => value.toLowerCase()))];
  if (provider === 'chatgpt') {
    // Exact provider bootstrap paths observed live and used by CodexBar.
    // Projection remains in-page; no session object or token leaves the page.
    try {
      const script = document.querySelector('script#client-bootstrap[type="application/json"]');
      const bootstrap = script && !script.closest(conversation) ? JSON.parse(script.textContent || '') : null;
      for (const value of [bootstrap?.session?.user?.email, bootstrap?.user?.email]) {
        if (typeof value !== 'string' || !/^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$/i.test(value.trim())) continue;
        const email = value.trim().toLowerCase();
        if (!emails.includes(email)) emails.push(email);
      }
    } catch { /* unavailable bootstrap; eligible account controls still apply */ }
  }
  let account_id = null;
  let account_label = null;
  const grokIdentity = capture.grokIdentity;
  if (emails.length === 1) {
    const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`taskspindle:subscription:v1:${provider}:${emails[0]}`));
    account_id = [...new Uint8Array(bytes)].map(value => value.toString(16).padStart(2, '0')).join('');
    const [local, domain] = emails[0].split('@');
    account_label = `${local[0]}***@${domain[0]}***.${domain.split('.').at(-1)}`;
  }
  if (provider === 'grok' && capture.authRequired) return { auth_required: true };
  if (provider === 'grok' && grokIdentity !== capture.grokIdentity) return {};
  if (provider === 'grok' && grokIdentity) {
    if (emails.length > 1 ||
        (account_id && account_id !== grokIdentity.account_id)) return {};
    account_id = grokIdentity.account_id;
    account_label = grokIdentity.account_label;
  }

  let channel = 'unknown';
  if (/(?:subscribed|subscription|purchased|billed|managed|manage)[^\n]{0,100}(?:Apple|App Store)/i.test(text)) channel = 'apple';
  else if (/(?:subscribed|subscription|purchased|billed|managed|manage)[^\n]{0,100}Google Play/i.test(text)) channel = 'google_play';
  else if (provider === 'grok' && /(?:subscribed|subscription|purchased|billed|managed|manage)[^\n]{0,100}X Premium/i.test(text)) channel = 'x_premium';
  // Card-management controls provide positive evidence of direct-web billing.
  else if (/\b(?:Payment method|Payment details|Billing details|Invoices)\b/i.test(text)) channel = 'provider_web';
  else if (provider === 'chatgpt' && capture.subscription?.is_processor_stripe === true) channel = 'provider_web';
  const plans = {
    chatgpt: /^(?:ChatGPT )?(?:Plus|Pro|Go|Free)$/i,
    claude: /^(?:Claude )?(?:Pro|Max(?:\s*\((?:5x|20x)\))?|Free)(?: plan)?$/i,
    google_ai: /^(?:Google AI (?:Pro|Ultra|Plus)|AI Premium)(?:\s*\(\d+\s*(?:GB|TB)\))?$/i,
    grok: /^(?:SuperGrok(?: Heavy)?|Free)(?: plan)?$/i,
  };
  const planCandidates = lines.filter(line => plans[provider].test(line));
  const canonicalPlans = new Map(planCandidates.map(line => [line.replace(/^(?:ChatGPT|Claude) /i,'').replace(/ plan$/i,'').toLowerCase(), line]));
  const planMatches = [...canonicalPlans.values()];
  let plan = planMatches.length === 1 ? planMatches[0] : null;
  if (provider === 'chatgpt') {
    // A billing panel can advertise other plans. The observed subscription's
    // exact plan is authoritative over those DOM offers.
    const known = { free: 'Free', go: 'Go', plus: 'Plus', pro: 'Pro' };
    if (Object.hasOwn(known, capture.subscription?.plan_type)) plan = known[capture.subscription.plan_type];
    else if (capture.subscription?.plan_type_unrecognized) plan = null;
  }
  // These exact labeled status values are synthetic contract coverage until
  // authenticated provider wording is recorded. Never derive status from dates.
  const states = new Set();
  const statusLabel = provider === 'google_ai' ? /^(?:Google AI|AI Premium) subscription status\s*:?\s*(.*)$/i : /^(?:(?:ChatGPT|Claude|SuperGrok) )?Subscription status\s*:?\s*(.*)$/i;
  const statusValues = { free: 'free', expired: 'expired', 'no subscription': 'none', 'no active subscription': 'none' };
  for (let i = 0; i < lines.length; i++) {
    const match = lines[i].match(statusLabel);
    if (match) {
      const status = statusValues[(match[1] || lines[i + 1] || '').toLowerCase()];
      if (status) states.add(status);
    }
  }
  const explicitStatus = states.size === 1 ? [...states][0] : null;
  // An explicit no-subscription status has no paid plan; "None" represents the
  // observed absence, not a plan inferred from missing API fields.
  if (explicitStatus === 'none' && planMatches.length === 0 &&
      !(provider === 'chatgpt' && (capture.subscription?.plan_type || capture.subscription?.plan_type_unrecognized))) plan = 'None';
  const date = value => {
    const raw = value.trim().replace(/\.$/, '');
    if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw;
    const monthFirst = raw.match(/^(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?) (\d{1,2}),? (\d{4})$/i);
    const dayFirst = raw.match(/^(\d{1,2}) (January|February|March|April|May|June|July|August|September|October|November|December) (\d{4})$/i);
    if (!monthFirst && !dayFirst) return null;
    const monthName = monthFirst ? monthFirst[1] : dayFirst[2];
    const month = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'].indexOf(monthName.slice(0,3).toLowerCase());
    const day = Number(monthFirst ? monthFirst[2] : dayFirst[1]);
    const year = Number((monthFirst || dayFirst)[3]);
    const result = new Date(`${year.toString().padStart(4,'0')}-${String(month + 1).padStart(2,'0')}-${String(day).padStart(2,'0')}T00:00:00Z`);
    return Number.isFinite(result.getTime()) && result.getUTCFullYear() === year && result.getUTCMonth() === month && result.getUTCDate() === day ? result.toISOString().slice(0, 10) : null;
  };
  const values = { renews_at: [], access_ends_at: [] };
  for (let i = 0; i < lines.length; i++) {
    for (const [field, label] of [
      ['renews_at', /^(?:Your (?:subscription|plan) (?:will )?renews on|Renews(?: on)?|Next (?:billing|payment|renewal) date|Next payment)\s*:?\s*(.*)$/i],
      ['access_ends_at', /^(?:Your (?:subscription|plan) (?:will )?ends on|Access ends(?: on)?|Expires(?: on)?|Subscription ends(?: on)?)\s*:?\s*(.*)$/i],
    ]) {
      const match = lines[i].match(label);
      if (match) {
        const parsed = date(match[1] || lines[i + 1] || '');
        if (parsed) values[field].push(parsed);
      }
    }
  }
  const renewal = [...new Set(values.renews_at)];
  const end = [...new Set(values.access_ends_at)];
  let billing = null;
  if (plan && renewal.length === 1 && end.length === 0) billing = { status: 'renewing', renews_at: renewal[0], access_ends_at: null };
  if (plan && end.length === 1 && renewal.length === 0 && /\b(?:cancelled|canceled|will not renew|does not renew)\b/i.test(text)) billing = { status: 'cancelled', renews_at: null, access_ends_at: end[0] };
  if (explicitStatus === 'expired' && plan && !/\bFree\b/i.test(plan) && renewal.length === 0 && end.length <= 1) billing = { status: 'expired', renews_at: null, access_ends_at: end[0] || null };
  if (explicitStatus === 'free' && plan && /\bFree(?: plan)?$/i.test(plan) && renewal.length === 0 && end.length === 0) billing = { status: 'free', renews_at: null, access_ends_at: null };
  if (explicitStatus === 'none' && plan === 'None' && renewal.length === 0 && end.length === 0) billing = { status: 'none', renews_at: null, access_ends_at: null };
  // Free/no-subscription is positively observed in this provider's own billing
  // settings. No payment method is expected; explicit external channel still wins.
  if (channel === 'unknown' && ['free', 'none'].includes(billing?.status)) channel = 'provider_web';
  return { account_id, account_label, billing_channel: channel, plan, billing,
    subscription: provider === 'chatgpt' ? capture.subscription : null };
}
