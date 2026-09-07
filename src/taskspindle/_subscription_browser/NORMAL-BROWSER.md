# Normal Chrome extension bridge

`normal-helper.mjs` attaches to the user's selected normal Chrome profile through
the official Playwright extension. `helper.mjs` remains the explicit dedicated
profile implementation. No extension is installed automatically.

## Runtime contract

The Python runtime enables normal-profile collection on Windows through WSL; native
Linux retains dedicated mode. The helper's portable modules are tested on both Node
platforms. Run Windows Node with `normal-helper.mjs`. Input is
the existing JSON request plus required `chrome_profile` (`Default` or `Profile N`)
and required UUID `operation_nonce`. `profile_dir` remains a TaskSpindle-owned
`profiles/<provider>` operation directory, **not** Chrome's actual profile.
It holds only marker/ownership/cancellation metadata for this mode.

Pass the profile-specific pairing secret through
`PLAYWRIGHT_MCP_EXTENSION_TOKEN`, never a command argument or JSON field. Token
validation matches the Python runtime: 1..512 printable ASCII bytes, excluding
spaces. Missing pairing returns `SETUP_REQUIRED` before attachment. The runtime
opens normal Chrome for Connect; the paired helper creates the sole billing tab.
Unpaired Connect can open the provider page directly and report setup required.

The selected Windows Chrome profile has been privately paired with Playwright extension
**0.4.0**. The token value is never evidence and must not be recorded. The candidate Python
runtime constructs a bounded `WSLENV` entry that forwards the named token to Windows Node;
the token remains outside command arguments and request JSON. This fixes the observed
WSL-to-Windows environment boundary in candidate code. A completed provider checkpoint and
post-restart readback still require controller verification.

Preflight checks only extension directory metadata and Chrome process presence.
It does not open Cookies, Login Data, Local State, Preferences, or CLI stores.
Connect retries briefly for runtime-initiated Chrome startup. Refresh requires
Chrome already running, the extension installed, a token, and expected account
hash. It never launches a stopped browser or handles sign-in/consent. A concurrent
browser shutdown can still make the connection fail. A disabled extension or
rotated token requires pairing repair; install-directory presence alone does not
prove extension readiness.

The receipt is `.taskspindle-collector.lock`, containing helper PID/start identity,
random lock nonce and `operation_nonce`. Cooperative cancellation creates
`.taskspindle-cancel-<operation_nonce>` in the operation directory. The helper polls
every 100 ms, closes its own tab, and releases its receipt. Runtime fallback may
stop only the matching helper PID after verifying PID/start/operation nonce;
never stop a Chrome process or a process tree. Allow 5 seconds before fallback:
cleanup can wait up to 2 seconds for a cancelled tab-creation request, then up to
2 seconds to close that late-created owned tab. An abrupt OS kill, or tab creation
that completes beyond this bounded cleanup window, can leave the owned tab;
this is not authority to close unrelated tabs or the whole browser.

Stdout is exactly one safe normalized JSON line. Errors use existing allowlisted
codes plus `SETUP_REQUIRED`. The helper never returns provider bodies, console
output, raw exception text, payment details, tokens, or unmasked account identity.

## Pinned transport and behavior

Dependency remains exactly `playwright-core` **1.63.0**. Its exported
`playwright-core/lib/coreBundle` exposes `tools.resolveCLIConfigForMCP` and
`tools.createBrowserWithInfo`. The helper uses the same extension/CDP factory as
the packaged `playwright-core mcp --extension --profile-dir-name ...` CLI, and
requires the returned ownership to be `attached`.

These are version-bound implementation APIs, not a promised stable public
interface. A test loads the actual pinned package and verifies factory/config
availability. Upgrades must repeat this check and a live attach/cleanup test.
The latest `@playwright/mcp` package 0.0.80 examined during implementation embeds
an alpha Playwright version and lacks selected-profile CLI support, so it is not
used here.

No MCP `BrowserBackend` is constructed: even snapshot-disabled MCP writes console
artifacts, which is inappropriate for billing collection. Direct factory use adds
no console/network/trace recorders. Existing `installCapture`, `readEvidence`, and
`normalize` run only on the helper-created billing tab. The helper never enumerates
or navigates preexisting tabs and never calls context.close or browser.close.
After closing its own tab, the one-shot helper exits, dropping its relay sockets
and allowing the extension to detach without closing Chrome.

Refresh may briefly open/focus a billing tab: upstream creates tabs without
`active:false`, and extension handshake can focus Chrome. No invisible-background
claim is made. The extension itself creates a connection page; invalid pairing
may leave its error page because no authorized bridge is available to close it.
An invalid nonempty token displays an error rather than falling back to consent.

## Primary implementation evidence

Observed 2026-09-07:

- https://github.com/microsoft/playwright/blob/main/packages/extension/README.md
  documents existing-profile attachment, selected profile, token pairing, and
  separate client tab groups.
- https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/tools/mcp/extensionContextFactory.ts
  creates extension relay and CDP attachment with `noDefaults:true`.
- https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/tools/mcp/browserFactory.ts
  distinguishes attached extension ownership from owned launched browsers.
- https://github.com/microsoft/playwright/blob/main/packages/extension/src/ui/connect.tsx
  rejects invalid nonempty tokens before the consent path.
- Pinned installed `playwright-core/lib/coreBundle.js` independently confirms
  these factory exports, selected-profile option, attached ownership, and the
  absence of BrowserBackend construction from this direct factory.

Verification includes sanitized adapter tests for owned-tab lifecycle, account mismatch,
expired auth, unavailable setup/browser, safe exceptions, cancellation, receipt isolation,
and the pinned extension factory. The validation profile used a privately paired extension 0.4.0.

The current packaged candidate has passed live Connect, Refresh, and refresh after collector
restart for ChatGPT, Claude, and Grok. Each operation opens and closes its own billing tab;
whole-Chrome shutdown/restart was not performed because existing user windows remain open.

- ChatGPT projects typed subscription plan/renewal fields and hashes bootstrap account identity.
- Claude captures account identity and billing dates from its scoped organization endpoints;
  cancellation ending fields have source-backed synthetic coverage, with a narrowly labeled
  DOM fallback. Scheduled downgrades do not replace the current plan.
- Grok uses session identity and the Billing dialog, retaining the configured Usage Connect link.
- Google One settings has no observed billing date. The account-bound Google Play lookup
  classifies the exact Google One product as an unsupported billing channel. App-store tracking
  remains excluded while the purchase channel is clarified.

Current browser tests pass **86/86 on Linux and Windows Node v24.14.0**. Windows invokes each
module through a file-URL import because Node test discovery does not resolve UNC paths.
These fixture checks are separate from the live packaged-provider checks above. The current
Python subscription/web/configuration suite passes 105 tests. Exact build hashes and current
limitations are recorded in the candidate ledger and `docs/subscriptions/evidence.md`.

Live cancelled/expired/free/no-subscription transitions for direct-web providers remain an
evidence gap. Never cancel a subscription for testing. Fixtures use synthetic identities and
shifted dates, and exclude credentials, payment details, and raw response bodies.
