qa-cli drives Telegram Web (https://web.telegram.org/a/) in headless Chromium via the agent-browser CLI: it prints a QR for phone login, persists the `qa-telegram` session so re-runs skip login, and never opens Telegram.app or sends chat messages. Default tests are hermetic (a fake browser; no network). Live QR uses agent-browser headless; set `QA_TELEGRAM_LIVE=1` to opt into the live pytest.

```
qa telegram login     # saves ~/.config/qa-cli/telegram-qr.png ; scan with phone
qa telegram status
```
