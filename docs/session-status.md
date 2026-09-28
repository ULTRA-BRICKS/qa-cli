# Telegram session status and restoration

`qa telegram status` observes the saved `qa-telegram` browser session. It does not
send messages, open the Telegram desktop application, or request a new login.
An existing official Telegram Web A page is reused without resetting its selected
chat. A blank page or a different origin is navigated to the official Web A URL.
The same page reuse applies when `qa telegram login` finds an authenticated session.

| Output | Evidence | Exit code |
| --- | --- | --- |
| `logged-in` | A visible authenticated Telegram shell on the exact official Web A origin and path | 0 |
| `logged-out` | A visible authentication form on that page | 0 |
| `unknown` | No conclusive shell or form appeared within 15 seconds | 1 |

An unavailable browser or failed browser command is an error, not logout. The
15-second deadline includes opening or restoring the page and each browser probe.
The polling loop waits for observable state rather than sleeping a fixed duration.
It never prints page contents, URL fragments, QR credentials, or authentication input.

`unknown` is not a reason to discard the session or scan another QR: check connectivity
and rerun `qa telegram status`. Use `qa telegram login` when authentication is actually
required. QR remains the default; `--phone` explicitly selects phone-number login.
Verification codes and two-factor passwords are still entered privately in the terminal.

A successful status check proves authentication only. It does not prove that a bot
received a message, answered correctly, or performed a calendar action. Those require
separate, explicitly authorized tests and fresh response evidence.

Regression coverage is in `tests/test_session_status.py`; fixtures are synthetic.
The previous logout fixture lacked an authentication form. It now includes one;
a separate never-ready case asserts `unknown`, rather than weakening the distinction.
