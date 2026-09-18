"""Telegram Web QR login and session status.

Accessed via: `qa telegram login` and `qa telegram status` (qa_cli.cli.main).

Assumptions: a BrowserDriver is provided (AgentBrowserDriver in production,
a fake in hermetic tests). Config dir is ~/.config/qa-cli unless QA_CONFIG_DIR
is set. Never launches Telegram.app and never sends chat messages.

login() renders the live QR as a terminal Unicode block (via the `qrcode`
package) instead of only writing a screenshot: Telegram Web A rotates the QR
token roughly every 20-30s, so a screenshot-only flow can go stale before the
user notices it. The render loop polls browser.get_qr_token() and redraws in
place (ANSI clear + reprint) whenever the token changes, and polls
browser.auth_status() to detect both success and any Telegram auth substep
this CLI cannot automate, reporting it distinctly instead of hanging
silently until timeout -- except Telegram's cloud-password (2FA) step,
`auth-password-form`, which IS handled: once it persists across the usual
two-poll debounce, `_prompt_and_submit_password` prompts for it with
`getpass.getpass` (masked, never echoed/logged/written anywhere) and submits
it directly into the live page via `browser.submit_password`. The PNG
screenshot is still written on every QR redraw for tooling that reads the
file directly.
"""
from __future__ import annotations

import getpass
import io
import os
import sys
import tempfile
import time
from pathlib import Path

import qrcode

from qa_cli.browser import (
    SESSION_NAME,
    TELEGRAM_WEB_URL,
    AUTH_PASSWORD_FORM_ID,
    AUTH_QR_FORM_ID,
    AuthPasswordRejectedError,
    AuthStatus,
    AuthStepRequiredError,
    BrowserDriver,
)

DEFAULT_LOGIN_TIMEOUT = 300.0
DEFAULT_QR_WAIT_TIMEOUT = 60.0
DEFAULT_POLL_INTERVAL = 1.0

_QR_URL_PREFIX = "tg://login?token="

_MANUAL_STEP_FALLBACK = "finish this step manually in a headed browser session, then re-run"

# Telegram's cloud-password check is a network round trip (its own SRP
# verification API call), not instant -- a single poll right after submit
# would misreport a slow-but-correct password as rejected. Bounded so a
# genuinely wrong password (or a submission that silently failed) is still
# reported within a few seconds, never retried silently forever.
_PASSWORD_CONFIRM_POLLS = 5


def config_dir() -> Path:
    override = os.environ.get("QA_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "qa-cli"


def qr_path() -> Path:
    return config_dir() / "telegram-qr.png"


def render_qr_ascii(data: str) -> str:
    """Render `data` as a terminal-friendly Unicode QR code block."""
    qr = qrcode.QRCode(border=4)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, tty=False)
    return buf.getvalue()


def _describe_wait(waiting_form):
    if waiting_form in (None, AUTH_QR_FORM_ID):
        return "waiting for QR scan"
    if waiting_form == AUTH_PASSWORD_FORM_ID:
        return "waiting on the Telegram two-factor cloud password step (auth-password-form)"
    return (
        f"waiting on Telegram auth step '{waiting_form}' — this qa-cli flow "
        f"cannot complete it automatically; {_MANUAL_STEP_FALLBACK}"
    )


class _TerminalQrPainter:
    """Redraws a full status/QR block in place using an ANSI screen clear."""

    def __init__(self, out) -> None:
        self._out = out
        self._drawn = False
        self._tty = bool(getattr(out, "isatty", lambda: False)())
        if self._tty:
            self._out.write("\033[?1049h\033[?25l")

    def close(self) -> None:
        if self._tty:
            self._out.write("\033[?25h\033[?1049l")
            self._out.flush()

    def render(self, message: str, body: str = "") -> None:
        if self._drawn and self._tty:
            self._out.write("\033[2J\033[H")
        self._out.write(message + "\n")
        if body:
            self._out.write("\n" + body)
        self._out.flush()
        self._drawn = True


def _prompt_and_submit_password(browser: BrowserDriver, painter: _TerminalQrPainter) -> None:
    """Prompt for Telegram's cloud password in the terminal (masked) and
    submit it into the live page.

    The password is never logged, printed, written to a file, or included
    in any exception message -- it exists only as this function's local
    `password` variable until it is handed to `browser.submit_password`,
    which itself sends it to agent-browser over subprocess stdin, never argv.
    """
    painter.render(
        "Telegram requires the two-factor cloud password -- "
        "check this terminal for a masked password prompt."
    )
    try:
        password = getpass.getpass("Telegram cloud password (input hidden): ")
    except (EOFError, OSError) as exc:
        raise AuthStepRequiredError(
            "Telegram requires the two-factor cloud password, but this "
            "terminal has no interactive input to prompt for it; re-run "
            f"from an interactive terminal, or {_MANUAL_STEP_FALLBACK}"
        ) from exc
    if not password:
        raise AuthStepRequiredError(
            "Telegram requires the two-factor cloud password, but none "
            f"was entered; re-run and type the password, or {_MANUAL_STEP_FALLBACK}"
        )
    browser.submit_password(password)


def _confirm_password_step(browser: BrowserDriver, poll_interval: float) -> AuthStatus:
    """After `browser.submit_password`, poll until `auth-password-form`
    clears (success, or Telegram moved to a different next step) or it
    persists across `_PASSWORD_CONFIRM_POLLS` polls, in which case the
    password was rejected (or the submission silently failed).
    """
    for _ in range(_PASSWORD_CONFIRM_POLLS):
        time.sleep(poll_interval)
        status = browser.auth_status()
        if status.waiting_form != AUTH_PASSWORD_FORM_ID:
            return status
    raise AuthPasswordRejectedError(
        "Telegram rejected the password (or another step is required); re-run to try again"
    )

def _run_qr_login_loop(
    browser: BrowserDriver,
    timeout: float,
    poll_interval: float,
    out,
) -> None:
    dest = qr_path()
    deadline = time.monotonic() + timeout
    painter = _TerminalQrPainter(out)
    try:
        last_rendered = None
        manual_form = None
        manual_polls = 0
        status: AuthStatus = browser.auth_status()
        while not status.logged_in:
            if time.monotonic() >= deadline:
                dump = browser.diagnostic_dump()
                raise TimeoutError(
                    "timed out waiting for Telegram Web QR login "
                    f"({_describe_wait(status.waiting_form)}) [{dump}]"
                )
            if status.waiting_form in (None, AUTH_QR_FORM_ID):
                manual_form = None
                manual_polls = 0
                token = browser.get_qr_token()
                key = ("qr", token)
                if token and key != last_rendered:
                    with tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".png", delete=False) as protected:
                        protected_path = Path(protected.name)
                    try:
                        browser.screenshot(str(protected_path))
                        protected_path.chmod(0o600)
                        protected_path.replace(dest)
                    finally:
                        protected_path.unlink(missing_ok=True)
                    painter.render(
                        f"{_describe_wait(status.waiting_form)} "
                        f"(auto-refreshes; also saved to {dest})",
                        render_qr_ascii(f"{_QR_URL_PREFIX}{token}"),
                    )
                    last_rendered = key
                elif not token and last_rendered is None:
                    painter.render("waiting for Telegram to generate a QR code…")
                    last_rendered = ("pending", None)
            else:
                manual_polls = manual_polls + 1 if manual_form == status.waiting_form else 1
                manual_form = status.waiting_form
                if manual_polls >= 2:
                    if status.waiting_form != AUTH_PASSWORD_FORM_ID:
                        raise AuthStepRequiredError(_describe_wait(status.waiting_form))
                    _prompt_and_submit_password(browser, painter)
                    status = _confirm_password_step(browser, poll_interval)
                    manual_form = None
                    manual_polls = 0
                    continue
                painter.render("Telegram is changing authentication state…")
            time.sleep(poll_interval)
            status = browser.auth_status()
    finally:
        painter.close()


def login(
    browser: BrowserDriver,
    timeout: float = DEFAULT_LOGIN_TIMEOUT,
    qr_timeout: float = DEFAULT_QR_WAIT_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    out=None,
) -> int:
    directory = config_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not os.environ.get("QA_CONFIG_DIR"):
        directory.chmod(0o700)
    set_deadline = getattr(browser, "set_deadline", None)
    if set_deadline:
        set_deadline(time.monotonic() + timeout + qr_timeout)
    try:
        browser.open(TELEGRAM_WEB_URL)
        if not browser.is_logged_in():
            browser.wait_for_qr(qr_timeout)
            if not browser.is_logged_in():
                _run_qr_login_loop(
                    browser, timeout, poll_interval, out if out is not None else sys.stdout
                )
    finally:
        if set_deadline:
            set_deadline(None)
    target = out if out is not None else sys.stdout
    print("logged-in", file=target)
    print(f"session: {SESSION_NAME}", file=target)
    print(f"config: {config_dir()}", file=target)
    return 0


def status(browser: BrowserDriver) -> int:
    browser.open(TELEGRAM_WEB_URL)
    state = "logged-in" if browser.is_logged_in() else "logged-out"
    print(state)
    print(f"session: {SESSION_NAME}")
    print(f"config: {config_dir()}")
    return 0
