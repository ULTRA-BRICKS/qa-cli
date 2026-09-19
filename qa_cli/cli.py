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
    return parser


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

    from qa_cli.telegram import login, status

    try:
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
