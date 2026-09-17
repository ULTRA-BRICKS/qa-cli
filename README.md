qa-cli drives Telegram Web (https://web.telegram.org/a/) in headless Chromium via the agent-browser CLI: `qa telegram login` renders the QR directly in your terminal (auto-refreshing as Telegram rotates it, roughly every 20-30s) so you can scan it without opening any file, and also saves a screenshot to ~/.config/qa-cli/telegram-qr.png. It persists the `qa-telegram` session so re-runs skip login, and never opens Telegram.app or sends chat messages. Default tests are hermetic (a fake browser; no network). Live QR uses agent-browser headless; set `QA_TELEGRAM_LIVE=1` to opt into the live pytest.

```
qa telegram login     # renders a scannable QR in your terminal (also saves ~/.config/qa-cli/telegram-qr.png)
qa telegram status
```

Install: `pipx install git+https://github.com/alex-mextner/qa-cli` (or `./install.sh`; pipx-first, symlink fallback).
