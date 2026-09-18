# qa-cli

Telegram Web QA through `agent-browser`, using the persistent `qa-telegram` session.

```sh
qa telegram login
qa telegram status
```

In an interactive terminal login uses an alternate-screen QR view, refreshes the
QR when Telegram rotates it, and restores the terminal/cursor on exit. The first
QR frame is read with Chromium's native barcode decoder; a module-hook fallback
is retained for browsers without that API. A blank or loading page is never
reported as authenticated. Browser subprocesses have a finite timeout.

If Telegram asks for a cloud password or another manual step, login exits with
code 3 and identifies that step instead of waiting silently. Complete it only in
the same trusted browser session. Never send OTPs or passwords to a bot or chat.
Ctrl-C exits with code 130. QR screenshots are written with private permissions
under `~/.config/qa-cli/telegram-qr.png`. They are credentials: do not attach live
QRs or browser session state to issues, PRs, logs or public QA reports.

Non-interactive output does not emit terminal-control sequences. Tests use fake
browsers and explicitly synthetic QR payloads. A live QR capture proves only QR
capture, not successful account authentication or bot behavior.

```sh
uv run --no-project --with 'pytest>=8,<9' --with qrcode pytest tests -q
```

Install from the current checkout with `pipx install --force .` (or `./install.sh`).
The maintained repository is on Forgejo under `ultrabricks/qa-cli`; the old
GitHub repository is archived. This package does not send chat messages by default.
