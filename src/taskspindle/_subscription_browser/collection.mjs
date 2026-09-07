import { readClaudeEvidence } from './claude.mjs';
import { readGooglePlayEvidence } from './google.mjs';
import { readEvidence } from './page.mjs';

export const GOOGLE_PLAY_SUBSCRIPTIONS_URL = 'https://play.google.com/store/account/subscriptions';

// This is serialized into the page. A route alone is insufficient to authorize
// fallback navigation: the owned tab must visibly be Google One settings for one
// signed-in Google account.
export async function readGoogleOneScope() {
  if (location.origin !== 'https://one.google.com' || location.pathname !== '/settings') return {};
  const visible = element => Boolean(element.getClientRects().length) &&
    !element.closest('[hidden], [aria-hidden="true"]');
  const accounts = [...document.querySelectorAll('[aria-label^="Google Account:"]')].filter(visible);
  if (accounts.length !== 1) return {};
  const emails = (accounts[0].getAttribute('aria-label') || '')
    .match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi) || [];
  if (emails.length !== 1) return {};
  const conversation = '[data-message-author-role], [data-testid*="conversation"], [data-testid*="chat-message"], [data-testid*="message"], [role="log"], [contenteditable="true"]';
  const mains = [...document.querySelectorAll('main, [role="main"]')].filter(element =>
    visible(element) && !element.closest(conversation) && !element.querySelector(conversation));
  if (mains.length !== 1) return {};
  const firstLine = (mains[0].innerText || '').split('\n').map(line => line.trim()).find(Boolean) || '';
  const names = [...document.querySelectorAll('h1, h2, h3, [role="heading"], [aria-label]')]
    .filter(visible).flatMap(element => [element.innerText || '', element.getAttribute('aria-label') || '']);
  if (firstLine !== 'Manage your Google One membership' &&
      !names.some(name => /^Google One(?: settings)?$/i.test(name.trim()))) return {};
  const email = emails[0].toLowerCase();
  const bytes = await crypto.subtle.digest(
    'SHA-256', new TextEncoder().encode(`taskspindle:subscription:v1:google_ai:${email}`),
  );
  return {
    account_id: [...new Uint8Array(bytes)]
      .map(value => value.toString(16).padStart(2, '0')).join(''),
  };
}

function projectedBilling(billing) {
  if (billing === null) return null;
  return {
    status: billing?.status,
    renews_at: billing?.renews_at,
    access_ends_at: billing?.access_ends_at,
  };
}

// Merge only projected fields. Claude's response capture supplies authoritative
// identity and billing, while the visible billing root establishes plan and
// billing channel without returning raw DOM or response content.
export async function collectEvidence(page, provider) {
  const base = await page.evaluate(readEvidence, provider);
  if (provider === 'google_ai') {
    const play = await page.evaluate(readGooglePlayEvidence);
    if (base?.auth_required || play?.auth_required) return { auth_required: true };
    if (!play || !Object.hasOwn(play, 'billing_channel')) return base;
    return {
      account_id: play.account_id,
      account_label: play.account_label,
      billing_channel: play.billing_channel,
      plan: play.plan,
      billing: projectedBilling(play.billing),
    };
  }
  if (provider !== 'claude') return base;

  const captured = await page.evaluate(readClaudeEvidence);
  if (base?.auth_required || captured?.auth_required) return { auth_required: true };
  if (!base || !Object.hasOwn(base, 'plan') || !Object.hasOwn(base, 'billing_channel')) return {};
  if (!captured || !/^[a-f0-9]{64}$/.test(captured.account_id || '') ||
      typeof captured.account_label !== 'string' ||
      !/^[^@\s]{1}\*\*\*@[^@\s]{1}\*\*\*\.[a-z]{2,24}$/i.test(captured.account_label) ||
      !Object.hasOwn(captured, 'billing')) return {};
  if ((base.account_id && base.account_id !== captured.account_id) ||
      (base.account_label && base.account_label !== captured.account_label)) return {};
  const recognized = new Set(['renewing', 'cancelled', 'expired', 'free', 'none']);
  if (base.billing && captured.billing && base.billing.status !== captured.billing.status) return {};
  const billing = captured.billing === null && recognized.has(base.billing?.status)
    ? base.billing : captured.billing;

  return {
    account_id: captured.account_id,
    account_label: captured.account_label,
    billing_channel: base.billing_channel,
    plan: base.plan,
    billing: projectedBilling(billing),
  };
}
