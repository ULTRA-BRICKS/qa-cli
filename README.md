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

If Telegram asks for its two-factor cloud password, login prompts for it right
in the terminal (masked, like a normal password prompt) and submits it directly
into the live page -- no headed browser needed. A wrong password, or any other
manual step this CLI can't automate, is reported distinctly instead of hanging
silently or falsely reporting success. Never send OTPs or passwords to a bot or chat.
Ctrl-C exits with code 130. QR screenshots are written with private permissions
under `~/.config/qa-cli/telegram-qr.png`. They are credentials: do not attach live
QRs or browser session state to issues, PRs, logs or public QA reports.

Non-interactive output does not emit terminal-control sequences. Tests use fake
browsers and explicitly synthetic QR payloads. A live QR capture proves only QR
capture, not successful account authentication or bot behavior.

```sh
uv run --no-project --with 'pytest>=8,<9' --with qrcode pytest tests -q
```

## Install

Pick one — each ends with `qa` on your PATH:

```bash
# 1. uv — recommended; macOS, Linux and Windows (incl. Cygwin / Git Bash)
uv tool install hyper-qa

# 2. pipx
pipx install hyper-qa

# 3. one-liner — clones to ~/.local/share/qa-cli, links qa into ~/.local/bin
curl -fsSL https://git.hyperide.ai/ultrabricks/qa-cli/raw/branch/main/install.sh | bash

# 4. from a clone — to hack on it; `git pull` updates the installed tool
git clone https://git.hyperide.ai/ultrabricks/qa-cli && cd qa-cli && ./install.sh
```

- **Unreleased `main`:** `uv tool install git+https://git.hyperide.ai/ultrabricks/qa-cli` (or the same with `pipx install`).
- **Update:** `uv tool upgrade hyper-qa` · `pipx upgrade hyper-qa` · re-run the one-liner · `git pull`.
- **Installed before the rename** (as `qa-cli`, from git)? Remove that first — `uv tool uninstall qa-cli` or `pipx uninstall qa-cli` — or two installs both claim `qa`.
- **Via rig:** clone into `~/xp/qa-cli` and list `qa` under `tools.items` in `~/.config/rig/config.yaml` — `rig apply commit` runs its `install.sh` and keeps the clone fresh.
- **Windows:** use uv (option 1) — `./install.sh` and the one-liner do exactly that under Cygwin / Git Bash. If a command dies with `UnicodeEncodeError` in mintty, run `setx PYTHONUTF8 1` once. More: [rig-cli → Windows](https://git.hyperide.ai/ultrabricks/rig-cli#windows-cygwin-git-bash-powershell).

> **On PyPI as `hyper-qa`** (the command is still `qa`). The canonical repo is
> **git.hyperide.ai/ultrabricks/qa-cli**; `github.com/alex-mextner/qa-cli` is a frozen, archived mirror.

Only `qa telegram chat send|send-file|click` write to a chat, and only to the bot named by `--peer`.

## Bot chat automation (end-to-end runs)

```sh
qa telegram chat open  --peer <bot id> --username <bot username>
qa telegram chat read  --peer <bot id> --after <message id> --limit 20
qa telegram chat send  --peer <bot id> --reply-to <message id> --text '/agent foo ping'
qa telegram chat send-file --peer <bot id> --reply-to <message id> --file note.ogg
qa telegram chat click --peer <bot id> --message <message id> --button Alpha
qa telegram chat wait  --peer <bot id> --after <message id> --contains 'ACK foo' --timeout 120
qa telegram chat screenshot --peer <bot id> --out chat.png
```

Drives ONE bot chat of the logged-in account and prints one JSON object per
command (exit 0 ok, 1 failure/timeout, 2 refused, 3 not logged in). It runs in
its own agent-browser session, `qa-telegram-chat`, restored from the
`qa-telegram` login and never saved back, so `qa telegram status|login` cannot
close the chat mid-run. Writes (send, send-file, click) go only to `--peer`, and
are refused unless the chat header says it is a bot (`--allow-non-bot`
overrides); send and send-file are also refused while the composer holds a
draft (a draft is the account owner's cloud-synced text). Typed text
travels through agent-browser's stdin, never argv. Replies use the message
context menu; files are pasted into the composer (an audio file is sent as
audio). Text matching is NFKC-normalized, so bot names styled with Unicode
math letters match plain ASCII.

Caveats: message ids are this account's ids — in a private chat a bot sees
different ids for the same messages, so correlate by content markers. Opening
the chat marks its messages read for the whole account. With bot topics
(threaded mode) on, a message that is not a reply starts a NEW topic; reply to
a main-chat message to stay in the main chat. Telegram Web's bot-chat view
can stop rendering new messages, so `wait` re-opens the chat after every 15 s
without a match; a wait shorter than that never refreshes. tg-cli's live e2e
harness (`bun scripts/e2e-telegram.ts` in tg-cli) is the main consumer.

## Deployment transport proof

```sh
qa telegram bot-probe --base-url https://your-bot.example --expected-sha <40-character-sha>
```

This token-free command verifies `/healthz`, the exact release SHA and that an
unsigned `/webhook` request is rejected. The request contains only `update_id: 0`,
no message, chat ID or command. Redirects are not followed, responses are bounded,
and server error bodies are never copied into the JSON report. Exit 0 means these
transport checks passed; exit 1 means failure; exit 2 means invalid arguments.
The report explicitly says `authenticated_chat_qa: not_run`. It does not claim
login, summary quality, message delivery, database readiness or history coverage.


### Phone number and verification code in the terminal

Run `qa telegram login --phone` for phone login, or `qa telegram login` to keep QR login.
When Telegram Web requests the number, five-digit code or cloud 2FA password, enter it
in the same interactive terminal. Input is hidden. Never pass codes/passwords as CLI
arguments or environment variables. Code, phone and password submission use native browser batches through subprocess stdin,
only on the visible official Telegram Web A form. Code input triggers Telegram's own
input handler; the CLI does not poll an inbox or read codes from messages.

Noninteractive execution fails with exit 3 before prompting, rather than echoing a
secret or hanging. A wrong/expired code remains unauthenticated; rerun the command to
retry. Successful input submission alone is not authentication: visible logged-in
shell evidence is still required. No chat message is sent by login/status.
