# Terminal login verification — 2026-09-18

Commands: `qa telegram login` (QR, then terminal prompts as needed) and `qa telegram login --phone` (phone, code, optional cloud password).

All entered values use masked interactive input. Noninteractive stdin exits before prompting; insecure getpass echo fallback is rejected. Values travel through native browser batches on subprocess stdin, not command-line arguments. Captured browser command output is never forwarded by qa-cli. The official origin and visible login form are checked before filling.

Telegram Web A submits the five-digit code through its own input handler. Phone and password steps explicitly wait for a visible enabled submit button, then click once. A native test without credentials reproduced a missing-button click failure; adding the wait passed all four steps after a delayed button mount. Phone formatting accepts spaces, parentheses and hyphens. Password selection retains its input-type fallback.

94 unit/contract tests passed; the one live-status test is opt-in and also passed separately. Native QR-to-phone switching was verified twice on the actual Telegram Web page without entering credentials. Full authentication was not performed: the user must enter their own code/password in the terminal. Login and status never send chat messages.

The older password-only verification note describes the earlier implementation. Native batch submission replaces that synthetic JavaScript path and its DOM-only tests; current tests assert command order, readiness wait, stdin-only values, wrong/expired-step handling, hidden input and generic error output.

Primary-source verification: Telegram Web A AuthCode.tsx (blob8be88cc9c2f5042eaacca4c8d39c6820130c69b5) defines CODE_LENGTH=5, attaches onInput, and calls setAuthCode when that length is reached. A separate native-browser synthetic input test verified that fill fires the input handler. This verifies the submission contract, not successful authentication to a real account.

## Cloud-password verdict and hint — 2026-10-11

Bug: after submitting a correct cloud password, `qa telegram login` polled `auth_status()` only five times (~5 s) and then raised "Telegram rejected the password", purely because `#auth-password-form` was still mounted; it never read Telegram's verdict and printed no hint. Reproduced with the real AgentBrowserDriver + agent-browser 0.33.1 + Chromium against a PasswordForm DOM mirror served at `https://web.telegram.org/a/` (app JS aborted): a correct password whose check took 9 s was reported as rejected at ~5 s on the old code.

Fix: `password_state()` reads Telegram's own state from the DOM telegram-tt renders (AuthPassword.tsx/PasswordForm.tsx/Button.tsx, confirmed in the deployed bundle on 2026-10-11): submit button class `loading` = still checking (wait, bounded only by the login deadline); `.input-group.password-input.error` = rejected, the label holds Telegram's error text, which is shown before re-prompting (max 3 typed attempts, no automatic resubmission); without an error the label is `hint || "Password"`, so a non-placeholder label in the English UI is printed as `Hint: ...` before the first prompt. Form idle (no spinner, no error) for 5 polls means the submit never registered and is reported as such, not as a rejection. Passwords over Telegram's `maxLength=256` are refused instead of silently truncated.

Verified: unit tests for slow-correct, error label + re-prompt, waiting out the post-error spinner, bounded attempts, hint/no-hint, deadline and unregistered submit; the same mirror run live (wrong then correct after a 9 s check, with and without a hint) logged in. Not verified: a real account's 2FA login (needs the owner's password).
