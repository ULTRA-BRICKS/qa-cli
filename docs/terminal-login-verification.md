# Terminal login verification — 2026-09-18

Commands: `qa telegram login` (QR, then terminal prompts as needed) and `qa telegram login --phone` (phone, code, optional cloud password).

All entered values use masked interactive input. Noninteractive stdin exits before prompting; insecure getpass echo fallback is rejected. Values travel through native browser batches on subprocess stdin, not command-line arguments. Captured browser command output is never forwarded by qa-cli. The official origin and visible login form are checked before filling.

Telegram Web A submits the five-digit code through its own input handler. Phone and password steps explicitly wait for a visible enabled submit button, then click once. A native test without credentials reproduced a missing-button click failure; adding the wait passed all four steps after a delayed button mount. Phone formatting accepts spaces, parentheses and hyphens. Password selection retains its input-type fallback.

94 unit/contract tests passed; the one live-status test is opt-in and also passed separately. Native QR-to-phone switching was verified twice on the actual Telegram Web page without entering credentials. Full authentication was not performed: the user must enter their own code/password in the terminal. Login and status never send chat messages.

The older password-only verification note describes the earlier implementation. Native batch submission replaces that synthetic JavaScript path and its DOM-only tests; current tests assert command order, readiness wait, stdin-only values, wrong/expired-step handling, hidden input and generic error output.

Primary-source verification: Telegram Web A AuthCode.tsx (blob8be88cc9c2f5042eaacca4c8d39c6820130c69b5) defines CODE_LENGTH=5, attaches onInput, and calls setAuthCode when that length is reached. A separate native-browser synthetic input test verified that fill fires the input handler. This verifies the submission contract, not successful authentication to a real account.
