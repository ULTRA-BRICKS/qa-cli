# Telegram 2FA terminal password entry — verification — 2026-09-18

Base: Forgejo main e198e75 (PR #17 already merged). Follow-up to
docs/verification-2026-09-18.md, which hardened `qa telegram login` so an
unhandled Telegram auth step (including the cloud-password 2FA step) fails
fast with `AuthStepRequiredError` (exit 3) instead of hanging or false-
reporting success. That hardening intentionally told the user to finish the
password step manually in a headed browser. This change replaces that ONE
step -- and only that step -- with in-terminal automation, at the user's
explicit request.

## What changed

`auth-password-form` (Telegram Web A's cloud-password / 2FA step) is no
longer escalated to the old "use a headed browser" message. Once it persists
across the existing two-poll debounce (unchanged anti-flicker hardening),
`qa telegram login` now:

1. Prompts for the password in the terminal via `getpass.getpass` (masked,
   never echoed).
2. Submits it into the live page through a new `BrowserDriver.submit_password`
   method, implemented on `AgentBrowserDriver` as
   `agent-browser --session qa-telegram --restore qa-telegram eval --stdin`
   with the JS (embedding `json.dumps(password)`, built by
   `_password_submit_script`) piped via the subprocess `input=` kwarg --
   never appended to argv. This was verified live against the installed
   `agent-browser` binary (`agent-browser eval --help`): `eval` accepts
   `--stdin` ("Read script from stdin (useful for heredocs/multiline)"), and
   `--session`/`--restore` remain global flags placed before the subcommand,
   exactly like every other call in `AgentBrowserDriver.build_argv`.
3. Polls `auth_status()` (`_confirm_password_step`, bounded at
   `_PASSWORD_CONFIRM_POLLS = 5` polls) until `auth-password-form` clears or
   persists across all 5. Telegram's own password check is a network round
   trip, not instant, so a single immediate poll would misreport a
   slow-but-correct password as rejected (see "AI review round" below --
   this bound replaced an initial single-poll version). If it clears, the
   normal login loop continues (success or a different next step). If it
   persists the full bound, `AuthPasswordRejectedError` is raised ("Telegram
   rejected the password (or another step is required); re-run to try
   again") and `qa` exits 1 -- no silent infinite retry, no false success.
4. If `getpass.getpass()` cannot prompt (no controlling terminal -- it raises
   `EOFError` via its stdin fallback path when stdin is closed/non-interactive,
   confirmed against CPython's `getpass` source), that is caught explicitly
   and reported as a distinct `AuthStepRequiredError` (exit 3) instead of
   hanging or raising a raw traceback.

Every other auth-form id (`None`, `auth-qr-form`, and anything other than
`auth-password-form`) is completely unchanged: same two-poll debounce, same
`AuthStepRequiredError`, same "finish it manually in a headed browser"
wording, same exit code 3.

## Password-input DOM selector

Determined from Telegram Web A's own open-source client
(github.com/Ajaxy/telegram-tt, `master` branch), not guessed:

- `src/components/auth/AuthPassword.tsx` renders `<div id="auth-password-form">`
  wrapping a `PasswordForm` component.
- `src/components/common/PasswordForm.tsx` renders the actual control:
  `<input id="sign-in-password" type="password" class="form-control" ...>`
  inside a `<form>`, with a `<button type="submit">` that only mounts once
  the component's local `canSubmit` state (`password.length >= MIN_PASSWORD_LENGTH`)
  is true -- i.e. after a real `input`/`change` event has been observed, not
  merely after `.value` is set.

`_SUBMIT_PASSWORD_JS` (qa_cli/browser.py) targets `#sign-in-password` directly,
with a defensive fallback to `input[type="password"]` anywhere inside
`#auth-password-form` in case that id ever changes upstream. It sets the value
through the native `HTMLInputElement.prototype.value` setter (bypassing any
framework-tracked value) and dispatches real `input`/`change` events so the
page's own change handler fires, then polls (up to 2s) for the submit button
to mount before clicking it, falling back to `form.requestSubmit()` / a
dispatched `submit` event if the button never appears. If the form or input
is missing entirely, it returns a distinct `{ok: false, reason: ...}` payload
instead of silently doing nothing; `submit_password()` turns that into a
`BrowserError` that distinguishes a selector mismatch from an undecodable
agent-browser response (see "AI review round").

This was read against the live upstream source but not exercised against a
live authenticated Telegram session -- doing so requires a real account and
a real 2FA password, which this agent does not have and was instructed not
to simulate. Instead, `_SUBMIT_PASSWORD_JS` itself is exercised hermetically
against a stubbed Node DOM (see "Test run"), the same way `_AUTH_STATUS_JS`
and `_QR_IMAGE_TOKEN_JS` already are. See "Manual verification" below for how
the user can prove the live path themselves.

## AI review round

A synchronous `review diff --staged --task CODE` pass (Gemini/Codex/GLM/Sonnet
panel) was run before committing, per this repo's `ai-review-before-commit`
practice. It found two issues serious enough to block merge on its own
assessment, both fixed before commit:

1. **`wait_for_qr` dead-ended on a persisted session parked at
   `auth-password-form`.** `login()` always calls `browser.wait_for_qr(...)`
   before falling into the QR-login loop; the real driver's `wait_for_qr`
   only returned on `is_logged_in()` or `is_qr_visible()`. Re-running
   `qa telegram login` against a session that resumes straight into the
   password step (no QR re-rendered) would hang the full QR-wait timeout,
   then raise `TimeoutError` with "scan the QR" guidance -- the password
   prompt was never reached on retry, defeating the "re-run to try again"
   contract of `AuthPasswordRejectedError` itself. Fixed: `wait_for_qr` now
   also returns as soon as `auth_status().waiting_form` is a non-QR auth
   form (any form other than `None`/`auth-qr-form`), reusing the same
   `auth_status()` call it already needs for the logged-in check --
   no extra subprocess round trip. Covered by
   `test_wait_for_qr_returns_immediately_on_non_qr_auth_form`; the two
   pre-existing `wait_for_qr` tests were updated to patch `auth_status`
   instead of `is_logged_in` directly, matching the driver's new internal
   call shape (their asserted behavior is unchanged).
2. **A correct-but-slow password could be misreported as rejected.**
   `_SUBMIT_PASSWORD_JS` returns as soon as the submit click/requestSubmit
   fires -- it does not (and cannot, from a fire-and-forget DOM dispatch)
   wait for Telegram's server-side password verification. The original
   implementation checked `auth-password-form` exactly once, one
   `poll_interval` after submit; a real network round trip slower than that
   would raise `AuthPasswordRejectedError` on a password Telegram actually
   accepted. Fixed: `_confirm_password_step` polls up to
   `_PASSWORD_CONFIRM_POLLS = 5` times (still bounded -- never an infinite
   retry) before declaring rejection. Covered by
   `test_password_step_survives_a_slow_correct_password_without_false_rejection`.

Lower-severity findings addressed in the same pass: `AuthPasswordRejectedError`
docstring reworded to third person (matched the sibling exception's style);
`submit_password` now distinguishes "page selector mismatch" (a decodable
`{ok:false,...}`) from "agent-browser returned an unreadable response" (`None`
-- a transport/CLI-shape problem), instead of conflating both as "no-response";
the JS-template placeholder substitution moved into a `_password_submit_script`
helper that asserts the placeholder is actually present, so a future edit
that drops it fails loudly in Python instead of shipping the literal
placeholder text to `agent-browser eval`; the two `_prompt_and_submit_password`
error messages share one `_MANUAL_STEP_FALLBACK` suffix constant instead of
duplicating it.

**Accepted, not fixed (documented limitation):** the overall
`browser.set_deadline(...)` budget set by `login()` keeps counting down while
`getpass.getpass()` blocks on user input. A user who takes several minutes to
type the password can exhaust that budget; the next Telegram Web subprocess
call then raises `TimeoutError("Telegram login deadline exceeded")`, which
`cli.py` reports with its generic "scan the QR and re-run" hint even though
QR scanning is not the actual issue at that point. This was flagged as a
low-severity finding; fixing it well requires plumbing a pause/extend of the
deadline across the interactive prompt through the `BrowserDriver` Protocol,
which is out of scope for this change. The error is at least not silent or
mis-attributed to the password itself -- `qa` still exits non-zero with an
accurate "deadline exceeded" root cause in the message, just a suboptimal
follow-up hint.

## Security

- The password is never logged, printed, written to any file (including
  `telegram-qr.png` or `diagnostic_dump()` output), or included in any
  exception message raised by this code path.
- It travels from `getpass.getpass()` to `agent-browser` exclusively via the
  Python string returned by `getpass` and the subprocess `input=` kwarg
  (stdin). `test_submit_password_sends_via_stdin_never_argv` asserts,
  structurally, that a marker password string is present in the subprocess
  `input` kwarg and absent from every element of the constructed argv list.
- `_invoke`'s nonzero-exit error categorization (shared by both the existing
  argv-based `_run` and the new stdin-based `_run_stdin`) only inspects
  lower-cased phrases in stderr/stdout to pick a static category label; it
  never includes `input_text`, matching the existing scrubbing bar documented
  in docs/verification-2026-09-18.md.

## Test run

```sh
uv run --no-project --with 'pytest>=8,<9' --with qrcode pytest tests -q
```

New/updated tests (beyond the pre-existing suite, none of which was deleted
or weakened):

- `test_password_step_prompts_masked_and_submits_to_page` -- a fake driver
  reporting `auth-password-form`, `getpass.getpass` monkeypatched to a fixed
  test string: proves the fake's `submit_password` receives exactly that
  string, that it appears nowhere else in the fake's recorded calls or the
  rendered terminal output, and that `login()` reaches exit 0 once the form
  clears.
- `test_submit_password_sends_via_stdin_never_argv` -- a real
  `AgentBrowserDriver.submit_password` call with `subprocess.run` mocked:
  proves the constructed argv contains `eval`/`--stdin` and no
  password-derived substring, and that the password only appears in the
  `input=` kwarg.
- `test_submit_password_js_sets_value_and_clicks_submit_when_ready`,
  `test_submit_password_js_falls_back_to_request_submit_when_no_button_mounts`,
  `test_submit_password_js_reports_missing_form_or_input_distinctly` --
  `_SUBMIT_PASSWORD_JS` run against a stubbed Node DOM (no Chromium),
  proving the native-setter value assignment, the submit-button-not-yet-
  mounted poll, the `requestSubmit()` fallback, and the distinct
  `form-not-found`/`input-not-found` failure payloads.
- `test_submit_password_failure_reason_is_reported_not_swallowed`,
  `test_submit_password_undecodable_response_is_a_distinct_error` -- the
  `submit_password` Python method turns a `{ok:false,...}` result into a
  `BrowserError` naming the reason, and a `None` (undecodable) result into a
  distinctly-worded error instead of the same message.
- `test_password_step_survives_a_slow_correct_password_without_false_rejection`
  -- the fake driver keeps `auth-password-form` mounted for two confirm-polls
  after submission before clearing; login still reaches exit 0 (the AI-review
  race fix).
- `test_password_step_rejected_reports_clear_error_and_exits_nonzero` -- the
  fake driver keeps reporting `auth-password-form` for the full confirm-poll
  budget: proves `qa telegram login` exits 1 with a clear "rejected the
  password" message (not the wrong password itself) instead of retrying
  silently or reporting success.
- `test_password_step_no_tty_fails_clearly_instead_of_hanging` --
  `getpass.getpass` monkeypatched to raise `EOFError`: proves login exits 3
  with a message naming the lack of an interactive terminal, instead of
  hanging or raising a raw traceback.
- `test_wait_for_qr_returns_immediately_on_non_qr_auth_form` -- the AI-review
  `wait_for_qr` dead-end fix: a driver reporting `auth-password-form` (no QR
  ever visible) must have `wait_for_qr` return promptly instead of polling
  QR pixels that will never appear.
- `test_wait_for_qr_detects_svg_after_delayed_render`,
  `test_wait_for_qr_returns_when_logged_in_without_qr` (pre-existing,
  updated) -- now patch `auth_status` instead of `is_logged_in` directly, to
  match `wait_for_qr`'s new internal call shape; the asserted behavior
  (detects a delayed QR render; returns once logged in without needing a
  QR) is unchanged.
- `test_persistent_manual_step_returns_code_three` (pre-existing, updated) --
  now uses `auth-phone-number-form` instead of `auth-password-form`, since
  the latter is no longer an immediate-escalate case; still proves a
  persistent, unhandled manual step exits 3.
- `test_login_reports_unsupported_auth_step_distinctly` (pre-existing,
  updated) -- same reasoning, moved off `auth-password-form` onto
  `auth-phone-number-form` so it keeps testing genuinely unsupported steps.
- `test_login_timeout_includes_diagnostic_dump_and_last_state` (pre-existing,
  unchanged assertions) -- kept passing by having the new password-specific
  `_describe_wait` text retain the literal `auth-password-form` substring
  the timeout diagnostic message already asserted on.

## Manual verification (for the account holder)

This agent has no Telegram account or 2FA password and did not simulate
having one. To prove the live end-to-end path:

1. `qa telegram login` in an interactive terminal.
2. Scan the QR with the Telegram phone app.
3. If Telegram then asks for the cloud password, a masked
   `Telegram cloud password (input hidden): ` prompt appears in the same
   terminal. Type the real password and press Enter -- it is never echoed.
4. On a correct password, login proceeds to `logged-in` (exit 0), even if
   Telegram takes a couple of seconds to accept it. On a wrong password, `qa`
   prints `error: Telegram rejected the password (or another step is
   required); re-run to try again` and exits 1 -- re-run and retry; thanks to
   the `wait_for_qr` fix above, the re-run reaches the password prompt again
   immediately instead of hanging on a QR wait.
5. To prove the no-TTY path, run it with **no controlling terminal at all**
   -- redirecting stdin alone is not enough, since `getpass` prefers
   `/dev/tty` and will still prompt normally in an ordinary interactive
   shell even with `< /dev/null`. A genuine no-TTY repro needs to detach
   the controlling terminal, e.g. `setsid qa telegram login < /dev/null
   > /tmp/qa-login.log 2>&1` (Linux) or running it from a non-interactive
   launchd/cron-style context on macOS. It should exit 3 with a message
   about the missing interactive terminal instead of hanging.
