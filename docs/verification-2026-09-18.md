# Telegram QR login verification — 2026-09-18

Base: Forgejo main b320586 (previous agent work already merged as PR #15).

## Reproduced before the patch

Five new hermetic regression tests failed: blank page incorrectly authenticated,
string `false` accepted as true, unbounded browser subprocess, command data in a
timeout exception, and failure to capture the first QR frame without rotation.

## After the patch

`uv run --no-project --with 'pytest>=8,<9' --with qrcode pytest tests -q`:
61 passed, 1 skipped. The skipped test is the explicitly opt-in live Telegram test.
Tests cover alternate-screen/cursor restoration and no ANSI control in redirected output.

A live `AgentBrowserDriver.get_qr_token()` call on the already-open Telegram Web A
QR page captured a Telegram QR token in 55 ms. Only a boolean and elapsed time were
recorded; no live token/QR/session data is included here. The session remained
logged-out on `auth-qr-form`: QR capture is not authentication proof.

The served public main UI module was inspected read-only and contains literal
`Main`, `LeftColumn`, `MiddleColumn`, and `RightColumn` element IDs. Login detection
requires positive visible main UI; it does not infer authentication from absence
of a login form. A password/manual auth form exits explicitly instead of polling
silently. Full scan-to-authenticated verification requires the account holder to
scan the live QR and complete any Telegram password step.

Codex review was attempted and rejected by the account quota. A separate source
review and red/green tests were performed; this is not an independent AI-review pass.

Follow-up review: the available Opus/Claude and Sonnet/Claude reviewers identified
an initial QR-hook error aborting before the retry window. Fixed by a bounded
15-second grace period and a regression for transient recovery. `wait_for_qr`
was verified to use short per-command polling (not one 60-second subprocess).
Other board seats timed out; they are not counted as successful review passes.

Final review corrections add hidden-element checks, two-poll manual-step debounce,
validated tokens, cached unavailable decoder and unchanged-SVG caching. Cold browser
navigation has a separate 45-second cap; eval has a 10-second cap. Actual QR polling
uses short commands, not one unbounded subprocess. Three integration fixtures
exercise transient/manual auth, CLI exit 3 and screenshot file mode. Live account
login is still not verified. User-authenticated sessions are never simulated.

Review follow-up: a rotation race in the SVG cache was reproduced and fixed by
binding the token to the exact serialized frame decoded (not a later DOM state).
Subprocess failures now expose static error categories, not raw payloads. The
transport deadline resets in finally. Existing default config directories are
hardened; caller-selected arbitrary directories are not chmodded implicitly.
