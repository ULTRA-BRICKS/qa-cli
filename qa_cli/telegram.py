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
two-poll debounce, `_handle_password_step` shows Telegram's password hint (if
any), prompts with `getpass.getpass` (masked, never echoed/logged/written
anywhere), submits into the live page via `browser.submit_password`, and then
waits for Telegram's own verdict (`browser.password_state()`): spinner = still
checking, error label = rejected (re-prompt, bounded attempts), form gone =
accepted. The PNG screenshot is still written on every QR redraw for tooling
that reads the file directly.
"""
from __future__ import annotations

import getpass
import warnings
import io
import os
import sys
import tempfile
import time
from pathlib import Path

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

from qa_cli.auth_input import CODE_FORM, PHONE_FORM

DEFAULT_LOGIN_TIMEOUT = 300.0
DEFAULT_QR_WAIT_TIMEOUT = 60.0
DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_STATUS_SETTLE_TIMEOUT = 20.0
DEFAULT_STATUS_POLL_INTERVAL = 0.5

_QR_URL_PREFIX = "tg://login?token="

_MANUAL_STEP_FALLBACK = "finish this step manually in a headed browser session, then re-run"

# Telegram's verdict on a cloud password is read from its own UI, never
# inferred from elapsed time: while the submit button spins the check is
# still running (SRP proof, DC round trip, main-module load after success),
# however long that takes within the login deadline. Each attempt needs the
# user to type again; nothing is ever resubmitted automatically.
_PASSWORD_MAX_ATTEMPTS = 3
# Form present without spinner or error for this many consecutive polls
# means the submit never reached Telegram's handler.
_PASSWORD_IDLE_POLLS = 5
_FIELD_CONFIRM_POLLS = 15
_FIELD_CONFIRM_INTERVAL = 1.0


def config_dir() -> Path:
    override = os.environ.get("QA_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "qa-cli"


def qr_path() -> Path:
    return config_dir() / "telegram-qr.png"


def render_qr_ascii(data: str) -> str:
    """Render `data` as a terminal-friendly Unicode QR code block."""
    import qrcode  # lazy: only the login QR path needs it; `status` must work without it

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


def _interactive_input_available():
    return bool(getattr(sys.stdin, 'isatty', lambda: False)())


def _secure_getpass(prompt):
    if not _interactive_input_available():
        raise AuthStepRequiredError('Interactive terminal required for auth-code-form/auth-password-form; re-run qa telegram login in a terminal')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        return getpass.getpass(prompt)


def _prompt_and_submit_password(
    browser: BrowserDriver,
    painter: _TerminalQrPainter,
    hint: str | None,
    rejection: str | None,
    attempt: int,
) -> None:
    """Prompt for Telegram's cloud password in the terminal (masked) and
    submit it into the live page.

    The password is never logged, printed, written to a file, or included
    in any exception message -- it exists only as this function's local
    `password` variable until it is handed to `browser.submit_password`,
    which itself sends it to agent-browser over subprocess stdin, never argv.
    """
    lines = []
    if rejection:
        lines.append(
            f"Telegram rejected the password: {rejection} "
            f"(attempt {attempt} of {_PASSWORD_MAX_ATTEMPTS})."
        )
    lines.append(
        "Telegram requires the two-factor cloud password -- "
        "check this terminal for a masked password prompt."
    )
    if hint:
        lines.append(f"Hint: {hint}")
    painter.render("\n".join(lines))
    try:
        password = _secure_getpass("Telegram cloud password (input hidden): ")
    except (EOFError, OSError, getpass.GetPassWarning) as exc:
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


def _await_password_verdict(
    browser: BrowserDriver, poll_interval: float, deadline: float
) -> AuthStatus | str:
    """Poll after a submit until Telegram decides: the form leaving
    `auth-password-form` returns the new AuthStatus; Telegram's error text
    (once its spinner has stopped, so the form accepts input again) is
    returned as a string. Never decides "rejected" from elapsed time.
    """
    idle_polls = 0
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "timed out while Telegram was still checking the cloud password "
                f"(not reported as rejected) [{browser.diagnostic_dump()}]"
            )
        time.sleep(poll_interval)
        status = browser.auth_status()
        if status.waiting_form != AUTH_PASSWORD_FORM_ID:
            return status
        state = browser.password_state()
        if state.checking or not state.present:
            idle_polls = 0
            continue
        if state.error:
            return state.error
        idle_polls += 1
        if idle_polls >= _PASSWORD_IDLE_POLLS:
            raise AuthStepRequiredError(
                "Telegram did not start checking the cloud password (the submit "
                "was not registered); nothing was resubmitted. Re-run qa telegram login."
            )


def _handle_password_step(
    browser: BrowserDriver, painter: _TerminalQrPainter, poll_interval: float, deadline: float
) -> AuthStatus:
    rejection = None
    # Read before the first submit: after a rejection Telegram's single
    # label shows the error text instead of the hint.
    hint = browser.password_state().hint
    for attempt in range(1, _PASSWORD_MAX_ATTEMPTS + 1):
        _prompt_and_submit_password(browser, painter, hint, rejection, attempt)
        verdict = _await_password_verdict(browser, poll_interval, deadline)
        if isinstance(verdict, AuthStatus):
            return verdict
        rejection = verdict
    raise AuthPasswordRejectedError(
        f"Telegram rejected the password: {rejection} "
        f"({_PASSWORD_MAX_ATTEMPTS} attempts); re-run qa telegram login to try again"
    )


def _prompt_terminal_field(browser, painter, form, poll_interval=DEFAULT_POLL_INTERVAL):
    kind = 'code' if form == CODE_FORM else 'phone'
    prompt = 'Telegram verification code (5 digits, input hidden): ' if kind == 'code' else 'Telegram phone (+country code, input hidden): '
    painter.render('Enter the requested login value in this terminal; it is not logged.')
    try:
        value = _secure_getpass(prompt)
    except (EOFError, OSError, getpass.GetPassWarning):
        raise AuthStepRequiredError('Secure interactive terminal input is unavailable; re-run qa telegram login in a terminal') from None
    if browser.auth_status().waiting_form != form:
        raise AuthStepRequiredError('Telegram authentication step changed; no value submitted. Re-run login.')
    if kind == 'code':
        browser.submit_code(value.strip())
    else:
        browser.submit_phone(value.strip())
    for _ in range(_FIELD_CONFIRM_POLLS):
        time.sleep(min(_FIELD_CONFIRM_INTERVAL, max(0, poll_interval)))
        status = browser.auth_status()
        if status.waiting_form != form:
            return status
    raise AuthStepRequiredError('Telegram has not accepted this login step. Check the code/phone and retry; no automatic resubmission.')

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
                    if status.waiting_form in (CODE_FORM, PHONE_FORM):
                        status = _prompt_terminal_field(browser, painter, status.waiting_form, poll_interval)
                        manual_form = None
                        manual_polls = 0
                        continue
                    if status.waiting_form != AUTH_PASSWORD_FORM_ID:
                        raise AuthStepRequiredError(_describe_wait(status.waiting_form))
                    status = _handle_password_step(browser, painter, poll_interval, deadline)
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
    phone: bool = False,
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
            if phone and browser.auth_status().waiting_form == AUTH_QR_FORM_ID:
                browser.switch_to_phone()
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


def _session_state(auth: AuthStatus) -> str:
    """A visible chat shell or auth form is a verdict; anything else is Web A still loading."""
    if auth.logged_in:
        return "logged-in"
    if auth.waiting_form is not None:
        return "logged-out"
    return "unknown"


def status(
    browser: BrowserDriver,
    timeout: float = DEFAULT_STATUS_SETTLE_TIMEOUT,
    poll_interval: float = DEFAULT_STATUS_POLL_INTERVAL,
) -> int:
    # Right after open Web A shows neither the main shell nor an auth form yet, and that
    # loading page reads as loggedIn:false, so one read would report logged-out.
    browser.open(TELEGRAM_WEB_URL)
    deadline = time.monotonic() + timeout
    # The driver caps each page probe at the time left, so a slow last probe cannot overrun.
    set_deadline = getattr(browser, "set_deadline", None)
    if set_deadline:
        set_deadline(deadline)
    try:
        while True:
            state = _session_state(browser.auth_status())
            # Stop unless a whole sleep still leaves time for the next probe; re-check after
            # sleeping, since an overshooting sleep would start a probe past the deadline.
            if state != "unknown" or deadline - time.monotonic() <= poll_interval:
                break
            time.sleep(poll_interval)
            if time.monotonic() >= deadline:
                break
    finally:
        if set_deadline:
            set_deadline(None)
    print(state)
    print(f"session: {SESSION_NAME}")
    print(f"config: {config_dir()}")
    if state == "unknown":
        print(
            f"error: Telegram Web showed neither chats nor a login form after ~{timeout:g}s of polling",
            file=sys.stderr,
        )
        return 1
    return 0
