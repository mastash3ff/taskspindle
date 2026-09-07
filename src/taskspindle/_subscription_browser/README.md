# Dedicated subscription browser

For the normal Chrome profile extension route, see [NORMAL-BROWSER.md](NORMAL-BROWSER.md).
The dedicated helper below remains available as an explicit legacy mode.

Run `npm ci --ignore-scripts`, then `node helper.mjs`. No browser download is
needed: `chrome_path` points to an already installed Chrome executable. Supports
Windows Node/Chrome and native Linux Node/Chrome. `npm test` runs sanitized tests.

One request on stdin, one JSON result line on stdout; diagnostics never contain
raw browser errors, payloads, cookies, email addresses, or payment details.

```json
{"provider":"claude","action":"connect","profile_dir":"/tmp/taskspindle/subscriptions/profiles/claude","chrome_path":"/usr/bin/google-chrome","expected_account_id":null,"timeout_s":300,"timezone":"America/Chicago"}
```

`refresh` requires the connected account's 64-character lowercase SHA256 ID.
The runtime supplies optional `operation_nonce` (UUID), recorded in the ownership
receipt so cleanup can verify that the process belongs to that exact invocation.
Cleanup must match this nonce and OS process-start identity; a busy profile's
existing owner never belongs to the failed invocation.
The profile path must end in `profiles/<provider>`. Existing unmarked directories
are rejected; the first launch creates `.taskspindle-subscription-profile` and
an exclusive operation lock. Locks record PID and OS process-start identity;
an interrupted collector's lock can be reclaimed only after confirming its
original process is gone (including PID reuse). Linux uses boot/start ticks;
Windows uses PowerShell Get-Process StartTime ticks without reading command lines.
Concurrent recovery is serialized. Malformed legacy locks, uncertain OS evidence,
or interruption inside the short recovery critical section require deliberate
operator inspection. Chrome additionally enforces its own persistent-profile lock;
the helper never kills a surviving orphan browser to force reuse.
The helper never reads normal-browser or CLI authentication. It only closes its
own persistent browser context; it never kills browser processes by name.

`connect` is visible and waits for a person to sign in. `refresh` is headless and
returns `AUTH_REQUIRED` when a login page is observed. No clicks, consent handling,
billing mutations, or API inference calls are performed. A user may navigate the
visible browser to billing or account controls when needed. Unknown identity,
billing channel, date, or schema returns `PARSE_CHANGED`.

Account IDs are SHA256 of a versioned provider/email identity inside the page.
Only the hash and a fully masked label leave the page. Identity must appear in
account/profile controls or an explicit email setting; arbitrary body email text
does not establish identity. Multiple different account emails fail closed.

All billing lines and labeled email rows come from one visible, named settings
or billing container. ChatGPT and Grok require a dialog; Claude/Google can use a
dedicated settings main region. A URL/hash by itself is insufficient, and a
container containing conversation/message/log/editor markup is rejected. Account
controls outside that container must be in application header/navigation/sidebar
regions and outside conversation content. The page body is never used for billing
or identity extraction. These conservative container selectors are not claimed to
match authenticated vendor markup until observed; absent markup fails closed.

## Evidence and limits (2026-09-07)

ChatGPT passively observes **exactly** `/backend-api/subscriptions` from its own
billing page fetch. Both `active_until` and `will_renew` (or camelCase aliases)
must exist with the types used by CodexBar. `will_renew=true` supplies only
`renews_at`; false supplies only `access_ends_at`. Empty metadata fails closed,
never proof of a free plan. A recognized provider plan is required for success.
The date must be an ISO timestamp. See `NOTICE` for
the upstream source and MIT attribution. This is source-backed parsing, not
proof of collection from this user's signed-in account.

The other extractors use narrow English billing labels, explicit provider plan
names, and an explicit payment/billing/invoice section indicating web billing:

- Claude's vendor documentation identifies Settings > Billing as its direct-web
  billing surface and distinguishes Apple and Google Play billing:
  https://support.claude.com/en/articles/8325618-paid-plan-billing-faqs
  https://support.claude.com/en/articles/8325617-cancel-your-pro-or-max-subscription
- Google documents web management in Google One settings and that benefits end
  at the current billing period's end:
  https://support.google.com/googleone/answer/9003633
  https://support.google.com/googleone/answer/9056360
- xAI documents `https://grok.com/?_s=billing` for direct web billing, and separates
  App Store, Google Play, and X Premium subscriptions:
  https://docs.x.ai/grok/faq

These documents establish navigation/channel semantics, **not exact authenticated
DOM structure or private JSON schemas**. Claude/Google/Grok test text is synthetic
contract coverage, not captured vendor fixtures. Live DOM identity and label
acceptance remain unverified until recorded by the controller. No unsupported
private JSON schema is fabricated. The collector intentionally reports a gap
instead of deriving a billing date from token expiry, quota reset, invoice,
creation time, or an assumed billing cycle. Date-only text retains date precision.
English-only fallback, multi-account ambiguity, third-party billing, and pages
without positive direct-web evidence fail closed.

Explicit labeled free, no-subscription, and expired states have synthetic tests.
They are not inferred from empty metadata or passed dates. Google AI absence
requires a Google AI/AI Premium status label, preventing confusion with storage
memberships. A positively displayed free/no-subscription state in provider billing
settings does not require payment controls; its channel represents the provider
account surface and does not claim a payment exists. Any external-billing indicator
still prevents success. Exact authenticated wording for these states remains
unverified. Invalid calendar days are rejected, including English month-name dates.
