"""qa — headless Telegram Web testing CLI.

Accessed via: console script `qa = qa_cli.cli:main` and the bin/qa shim.

Assumptions: when argv is passed it is args without argv[0]. Tests inject a
BrowserDriver; production constructs AgentBrowserDriver. argparse SystemExit
from --help/--version/usage is converted to a return code (0 or 2).
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from qa_cli import __version__
from qa_cli.browser import (
    AgentBrowserDriver,
    AuthStepRequiredError,
    BrowserDriver,
    BrowserError,
    BrowserMissingError,
    INSTALL_HINT,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qa",
        description=(
            "Headless Telegram Web testing via agent-browser. "
            "QR login saves ~/.config/qa-cli/telegram-qr.png "
            "(override with QA_CONFIG_DIR). Never opens Telegram.app."
        ),
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"qa {__version__}",
        help="show program version and exit",
    )
    sub = parser.add_subparsers(dest="command")
    tg = sub.add_parser(
        "telegram",
        help="Telegram Web QR login and session status",
        description=(
            "Drive https://web.telegram.org/a/ in headless Chromium via "
            "agent-browser. Login saves a QR screenshot to "
            "~/.config/qa-cli/telegram-qr.png (or $QA_CONFIG_DIR/telegram-qr.png) "
            "for phone scan. Never opens Telegram.app."
        ),
    )
    tg_sub = tg.add_subparsers(dest="telegram_command", required=True)
    login_parser = tg_sub.add_parser(
        "login",
        help="Open Telegram Web, save QR if needed, wait until logged in",
        description=(
            "Open https://web.telegram.org/a/ via agent-browser (session "
            "qa-telegram). If not logged in, wait until the QR is visible, "
            "then render it directly in the terminal -- auto-refreshing as "
            "Telegram rotates the token roughly every 20-30s -- while also "
            "saving a screenshot to ~/.config/qa-cli/telegram-qr.png. Scan "
            "with the Telegram phone app. If Telegram then asks for its "
            "two-factor cloud password, this prompts for it in the "
            "terminal (masked, like a normal password prompt) and submits "
            "it into the page directly. Verification codes are entered in the terminal too; --phone starts phone login. Any other auth step this CLI "
            "can't automate is reported distinctly instead of hanging "
            "until timeout. If already logged in, print status and skip QR."
        ),
    )
    login_parser.add_argument("--phone", action="store_true", help="Use phone-number login and enter its verification code in the terminal")
    tg_sub.add_parser(
        "status",
        help="Print logged-in or logged-out from the persisted session",
        description=(
            "Report Telegram Web session state without opening Telegram.app. "
            "Uses the persisted agent-browser session qa-telegram."
        ),
    )
    probe = tg_sub.add_parser(
        "bot-probe", help="Verify deployed SHA and unsigned-webhook rejection; no chat access",
    )
    probe.add_argument("--base-url", required=True, help="HTTPS origin, without path or credentials")
    probe.add_argument("--expected-sha", required=True, help="Exact deployed 40-character commit SHA")
    _add_chat_parser(tg_sub)
    return parser


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value}")
    return number


def _add_chat_parser(tg_sub) -> None:
    chat = tg_sub.add_parser(
        "chat",
        help="Drive one bot chat for e2e runs: open, read, send, click, wait (JSON output)",
        description=(
            "Automate ONE chat in Telegram Web A for bot end-to-end tests. Runs in "
            "its own agent-browser session (qa-telegram-chat) restored from the "
            "qa-telegram login and never saved back, so `qa telegram status` "
            "cannot close the chat mid-run. Writes go only to --peer and are "
            "refused unless its header says it is a bot (--allow-non-bot "
            "overrides) and the composer holds no draft. Each command prints "
            "one JSON object. Message ids are this account's ids (a bot sees "
            "different ids in a private chat): correlate by content markers. "
            "Opening a chat marks its messages read for the account. With bot "
            "topics (threaded mode) on, a message that is not a reply starts a "
            "NEW topic; --reply-to a main-chat message keeps it in the main chat. "
            "Exit: 0 ok, 1 failure/timeout, 2 refused, 3 not logged in."
        ),
    )
    chat_sub = chat.add_subparsers(dest="chat_command", required=True)

    def sub(name: str, help_text: str) -> argparse.ArgumentParser:
        p = chat_sub.add_parser(name, help=help_text, description=help_text)
        p.add_argument("--peer", type=int, required=True, help="numeric Telegram peer id, e.g. a bot's user id")
        p.add_argument("--username", help="the peer's @username, used to find the chat via search")
        p.add_argument("--allow-non-bot", action="store_true", help="allow writes to a chat that is not a bot")
        return p

    sub("open", "Open the chat at its latest message; print title and last message id")
    read = sub("read", "Print the rendered messages (id, out, text, kind, buttons, reactions)")
    read.add_argument("--after", type=int, help="only messages with an id greater than this")
    read.add_argument("--limit", type=_positive_int, help="only the newest N messages")
    send = sub("send", "Send one line of text (optionally as a reply) and print the confirmed message")
    text = send.add_mutually_exclusive_group(required=True)
    text.add_argument("--text", help="the message text (one line)")
    text.add_argument("--text-file", help="read the message text from this file")
    send.add_argument("--reply-to", type=int, help="reply to this message id")
    send_file = sub("send-file", "Send a file (audio files go as audio) and print the confirmed message")
    send_file.add_argument("--file", required=True, help="path to the file (max 8 MiB)")
    send_file.add_argument("--reply-to", type=int, help="reply to this message id")
    click = sub("click", "Tap an inline keyboard button on a message")
    click.add_argument("--message", type=int, required=True, help="message id that carries the button")
    click.add_argument("--button", required=True, help="button label (exact, else unique substring)")
    wait = sub("wait", "Wait for a message matching the filters and print it")
    wait.add_argument("--after", type=int, help="only messages with an id greater than this")
    wait.add_argument("--message", type=int, help="re-read this message id (e.g. an edited card)")
    wait.add_argument("--contains", help="text the message must contain (NFKC-normalized)")
    wait.add_argument("--from", dest="sender", choices=("peer", "me", "any"), default="peer",
                      help="who sent it (default: peer)")
    wait.add_argument("--timeout", type=float, default=60.0,
                      help="seconds to wait (default 60); the chat is re-opened every 15 s without a match")
    shot = sub("screenshot", "Save a screenshot of the open chat")
    shot.add_argument("--out", required=True, help="PNG path to write")


def _exit_code(exc: SystemExit) -> int:
    code = exc.code
    if code is None:
        return 0
    try:
        return int(code)
    except (TypeError, ValueError):
        return 1


def main(
    argv: Optional[Sequence[str]] = None,
    browser: Optional[BrowserDriver] = None,
) -> int:
    parser = build_parser()
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        return _exit_code(exc)

    if args.command != "telegram":
        parser.print_usage(sys.stderr)
        return 2

    if args.telegram_command == "bot-probe":
        from qa_cli.transport_probe import run_bot_probe
        return run_bot_probe(args.base_url, args.expected_sha)

    try:
        if args.telegram_command == "chat":
            from qa_cli.chat import CHAT_SESSION_NAME, run_chat_command
            chat_driver = browser if browser is not None else AgentBrowserDriver(
                session=CHAT_SESSION_NAME, restore_save="never"
            )
            return run_chat_command(args, chat_driver)
        from qa_cli.telegram import login, status

        driver = browser if browser is not None else AgentBrowserDriver()
        if args.telegram_command == "login":
            return login(driver, phone=True) if args.phone else login(driver)
        if args.telegram_command == "status":
            return status(driver)
        return 2
    except BrowserMissingError as exc:
        print(
            "error: cannot drive Telegram Web — "
            f"`{exc.binary}` is not installed",
            file=sys.stderr,
        )
        print(
            "this command needs the agent-browser CLI for headless Chromium.",
            file=sys.stderr,
        )
        print(f"install it with:  {INSTALL_HINT}", file=sys.stderr)
        if args.telegram_command != "chat":
            print("then re-run:      qa telegram login", file=sys.stderr)
        return 127
    except TimeoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("scan the QR with your phone and re-run: qa telegram login", file=sys.stderr)
        return 1
    except AuthStepRequiredError as exc:
        print(f"action required: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("login cancelled", file=sys.stderr)
        return 130
    except BrowserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
